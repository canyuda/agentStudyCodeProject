"""第 6 步的回归测试：Agent Loop 的五个生命周期场景（重点异常路径）。"""

import asyncio
import time

import pytest

from gateway.fake_model import FakeStreamingModel
from gateway.loop import execute_run
from gateway.store import RunStore


def types(run) -> list[str]:
    return [e.type for e in run.events]


async def run_to_end(prompt: str, model: FakeStreamingModel, **loop_kwargs):
    """小工具：同步跑完一个 Run，返回 (store, run)。loop_kwargs 传给 execute_run。"""
    store = RunStore()
    run = store.create_run(prompt)
    await execute_run(store, run, model, **loop_kwargs)
    return store, run


async def test_happy_path_event_order_and_ttft():
    """正常路径：started -> deltas -> checkpoint -> metrics -> completed（顺序即协议）。"""
    _, run = await run_to_end(
        "p",
        FakeStreamingModel(text="一二三四五六七八九十", chunk_size=1, inter_chunk_delay=0.01),
        checkpoint_every=4,
    )
    t = types(run)
    assert t[0] == "run.started"
    assert t.count("text.delta") == 10
    assert t.count("checkpoint.saved") == 2
    assert t.index("run.metrics") == len(t) - 2  # metrics 必须在终态之前
    assert t[-1] == "run.completed"
    metrics = run.events[-2].data
    assert 0 < metrics["ttft_ms"] < 5000
    assert run.events[-1].data["text"] == "一二三四五六七八九十"
    assert run.status == "completed"


async def test_cooperative_cancel_stops_model_immediately():
    """异常路径（取消）：收到取消信号后模型立刻停，partial_text 与账本一致。"""
    store = RunStore()
    run = store.create_run("长文本")
    task = asyncio.create_task(execute_run(
        store, run,
        FakeStreamingModel(text="字" * 60, chunk_size=1,
                           first_token_delay=0.05, inter_chunk_delay=0.05)))
    await asyncio.sleep(0.3)  # 跑到十来个 delta
    cancel_at = time.monotonic()
    store.request_cancel(run)
    await asyncio.wait_for(task, timeout=2.0)

    took = time.monotonic() - cancel_at
    deltas = types(run).count("text.delta")
    assert 0 < deltas < 60            # 真的停了，不是跑完的
    assert took < 0.5                 # 而且是立刻停
    assert run.status == "cancelled"
    assert types(run)[-1] == "run.cancelled"
    assert run.events[-1].data["partial_text"] == "字" * deltas


async def test_model_exception_becomes_failed_terminal():
    """异常路径（模型炸了）：异常被转成 run.failed 终态事件，partial 保留。"""
    _, run = await run_to_end("p", FakeStreamingModel(text="一二三", chunk_size=1, fail_after=2))
    assert run.status == "failed"
    last = run.events[-1]
    assert last.type == "run.failed"
    assert last.data["code"] == "MODEL_STREAM_FAILED"
    assert last.data["retryable"] is True
    assert last.data["partial_text"] == "一二"


async def test_length_truncation_becomes_failed_terminal():
    """异常路径（截断）：finish_reason=length 转 OUTPUT_TRUNCATED 且不可重试。"""
    _, run = await run_to_end("p", FakeStreamingModel(text="abc", finish_reason="length"))
    last = run.events[-1]
    assert last.type == "run.failed"
    assert last.data["code"] == "OUTPUT_TRUNCATED"
    assert last.data["retryable"] is False


async def test_forced_cancel_keeps_terminal_event_and_reraises():
    """异常路径（强杀）：task.cancel 后终态事件不丢（shield），取消信号上抛。"""
    store = RunStore()
    run = store.create_run("强杀")
    task = asyncio.create_task(execute_run(
        store, run,
        FakeStreamingModel(text="字" * 80, chunk_size=1,
                           first_token_delay=0.05, inter_chunk_delay=0.05)))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()                     # 信号没有被吞
    assert run.status == "cancelled"
    assert types(run)[-1] == "run.cancelled"    # 终态事件落账（shield 生效）
    assert run.events[-1].data["reason"] == "task_cancelled"
