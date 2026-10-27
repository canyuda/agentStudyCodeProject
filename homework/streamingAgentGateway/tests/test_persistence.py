"""第 10 步的回归测试：SQLite 写穿、重启恢复与孤儿回收。"""

import asyncio
import json
import sqlite3

import pytest

from gateway.fake_model import FakeStreamingModel
from gateway.loop import execute_run
from gateway.sqlite_store import SqliteRunStore
from gateway.store import RunStore  # 用于断言接口兼容（里氏替换）


@pytest.fixture
def db_path(tmp_path):
    """每个测试独立的临时 SQLite 文件。"""
    return tmp_path / "gateway.db"


async def run_to_end(store: SqliteRunStore, prompt: str,
                     fake_kwargs: dict | None = None, **loop_kwargs):
    run = store.create_run(prompt)
    await store.append_event(run, "run.created", {"prompt": prompt})
    await execute_run(store, run,
                      FakeStreamingModel(**(fake_kwargs or {})), **loop_kwargs)
    return run


# ------------------------------------------------------------------ 写穿 --
def test_sqlite_store_is_a_run_store(db_path):
    """接口兼容：SqliteRunStore 可以无缝替换 RunStore（第 5 步插槽的兑现）。"""
    assert isinstance(SqliteRunStore(db_path), RunStore)


async def test_write_through_tables(db_path):
    """完整跑一个 Run 后，三张表都有正确内容。"""
    store = SqliteRunStore(db_path)
    run = await run_to_end(store, "持久化测试",
                           fake_kwargs=dict(text="一二三四五六七八九十", chunk_size=1,
                                            inter_chunk_delay=0.01),
                           checkpoint_every=4)

    conn = sqlite3.connect(db_path)
    status, = conn.execute("SELECT status FROM runs WHERE run_id=?", (run.run_id,)).fetchone()
    rows = conn.execute("SELECT seq, type FROM events WHERE run_id=? ORDER BY seq",
                        (run.run_id,)).fetchall()
    cp_count, = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    conn.close()

    assert status == "completed"
    assert [s for s, _ in rows] == list(range(len(rows)))       # 库内 seq 同样稠密
    assert rows[0] == (0, "run.created")
    assert rows[-1][1] == "run.completed"
    assert cp_count == 1                                        # checkpoint 覆盖写
    assert len(rows) == len(run.events)                         # 内存与库一致


# ------------------------------------------------------------------ 重启 --
async def test_restore_completed_run_after_restart(db_path):
    """重启（新开一个 Store 指向同一文件）后：Run、事件、checkpoint 全部回来。"""
    store1 = SqliteRunStore(db_path)
    run = await run_to_end(store1, "重启恢复",
                           fake_kwargs=dict(text="重启后我还在", chunk_size=2),
                           checkpoint_every=3)  # 3 个 delta：末尾快照含完整文本
    original_events = [e.to_dict() for e in run.events]
    store1.close()  # 模拟进程退出

    store2 = SqliteRunStore(db_path)  # 模拟重启
    restored = store2.restore_all()

    assert len(restored) == 1
    again = store2.get_run(run.run_id)
    assert again is not None
    assert again.status == "completed"
    assert [e.to_dict() for e in again.events] == original_events   # 事件逐条相等
    assert again.checkpoint is not None
    assert again.checkpoint["text_so_far"] == "重启后我还在"

    # 恢复后的账本照样支持断线续传窗口
    assert [e.seq for e in store2.events_after(again, 1)] == list(range(2, len(original_events)))
    store2.close()


async def test_orphan_running_run_is_failed_on_restore(db_path):
    """孤儿回收：跑一半"断电"的 Run，重启后被补 PROCESS_RESTARTED 终态。

    模拟方式：用 Store API 把 Run 推到 running 并留下一个 checkpoint，
    然后 close 连接——相当于进程带着 running 状态直接死亡
    （不走 task.cancel，那会触发 Loop 干净收尾写成 cancelled）。
    """
    store1 = SqliteRunStore(db_path)
    run = store1.create_run("孤儿")
    await store1.append_event(run, "run.created", {})
    store1.set_status(run, "running")
    await store1.append_event(run, "run.started", {})
    await store1.append_event(run, "text.delta", {"delta": "一"})
    store1.save_checkpoint(run, {"text_so_far": "一", "deltas": 1})
    store1.close()  # "断电"：库里 status=running，账本停在半路

    store2 = SqliteRunStore(db_path)
    store2.restore_all()
    again = store2.get_run(run.run_id)

    assert again.status == "failed"                    # 不再悬在 running
    assert again.events[-1].type == "run.failed"
    assert again.events[-1].data["code"] == "PROCESS_RESTARTED"
    assert again.events[-1].data["partial_text"] == "一"   # 进度来自 checkpoint
    assert again.events[-1].seq == len(again.events) - 1   # 补的事件 seq 仍然稠密
    store2.close()


async def test_restored_run_supports_full_replay(db_path):
    """跨重启的"历史回放"：恢复后订阅语义等价于账本切片。"""
    store1 = SqliteRunStore(db_path)
    run = await run_to_end(store1, "回放", fake_kwargs=dict(text="回放测试", chunk_size=2))
    store1.close()

    store2 = SqliteRunStore(db_path)
    store2.restore_all()
    again = store2.get_run(run.run_id)
    # 从头回放：events_after(-1) 即完整审计日志
    assert [e.type for e in store2.events_after(again, -1)][-1] == "run.completed"
    store2.close()
