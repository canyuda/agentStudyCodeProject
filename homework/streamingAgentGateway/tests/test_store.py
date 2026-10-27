"""第 5 步的回归测试：Run Store 的账本、守卫、补发、取消与等待。"""

import asyncio

import pytest

from gateway.store import RunStore


async def append_deltas(store: RunStore, run, count: int) -> None:
    for i in range(count):
        await store.append_event(run, "text.delta", {"delta": str(i)})


async def test_seq_is_dense_and_monotonic():
    """seq 从 0 连续递增，无空洞无重复（断线补发依赖此不变量）。"""
    store = RunStore()
    run = store.create_run("p")
    await append_deltas(store, run, 5)
    assert [e.seq for e in run.events] == [0, 1, 2, 3, 4]


async def test_ledger_locks_after_terminal_event():
    """异常场景：终态事件入账后，任何追加都被拒绝（单一终态的物理保证）。"""
    store = RunStore()
    run = store.create_run("p")
    await append_deltas(store, run, 2)
    await store.append_event(run, "run.completed", {"text": "01"})
    with pytest.raises(RuntimeError, match="终态"):
        await store.append_event(run, "text.delta", {"delta": "x"})


async def test_status_transition_guards():
    """异常场景：终态后换终态被拒；同值幂等重放允许。"""
    store = RunStore()
    run = store.create_run("p")
    store.set_status(run, "running")
    store.set_status(run, "completed")
    store.set_status(run, "completed")  # 幂等：无害
    with pytest.raises(RuntimeError, match="禁止迁移"):
        store.set_status(run, "cancelled")


async def test_events_after_replay_window():
    """断线补发窗口：events_after(n) 恰好返回 seq > n 的事件。"""
    store = RunStore()
    run = store.create_run("p")
    await append_deltas(store, run, 6)
    assert [e.seq for e in store.events_after(run, 2)] == [3, 4, 5]
    assert [e.seq for e in store.events_after(run, -1)] == [0, 1, 2, 3, 4, 5]
    assert store.events_after(run, 5) == []  # 已追平


async def test_cancel_signal_idempotent_on_terminal():
    """终态 Run 的取消请求返回 False（幂等），活动 Run 则设置信号。"""
    store = RunStore()
    active = store.create_run("a")
    assert store.request_cancel(active) is True
    assert active.cancel_event.is_set()
    assert store.request_cancel(active) is True  # 重复请求无害

    done = store.create_run("b")
    store.set_status(done, "completed")
    assert store.request_cancel(done) is False


async def test_wait_for_events_timeout_and_wakeup():
    """订阅等待：超时返回 False（心跳路径）；追加事件唤醒返回 True。"""
    store = RunStore()
    run = store.create_run("p")

    assert await store.wait_for_events(run, timeout=0.05) is False

    waiter = asyncio.create_task(store.wait_for_events(run, timeout=2.0))
    await asyncio.sleep(0.02)
    await append_deltas(store, run, 1)
    assert await waiter is True


async def test_checkpoint_overwrite():
    """checkpoint 覆盖写，并带 saved_at 时间戳。"""
    store = RunStore()
    run = store.create_run("p")
    store.save_checkpoint(run, {"text_so_far": "你", "deltas": 1})
    store.save_checkpoint(run, {"text_so_far": "你好", "deltas": 2})
    assert run.checkpoint["text_so_far"] == "你好"
    assert "saved_at" in run.checkpoint


def test_get_run_unknown_returns_none():
    """查不到的 Run 返回 None（路由层据此回 404）。"""
    assert RunStore().get_run("run_nope") is None
