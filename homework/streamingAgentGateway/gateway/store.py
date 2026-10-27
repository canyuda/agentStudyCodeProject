"""步骤 5：Run Store —— Run 的登记处、事件账本、状态机守卫。

Store 是整个网关的"事实来源"（source of truth）：
- 每个 Run 的一切都登记在这里：状态、事件账本、取消信号、checkpoint；
- seq 的分配只发生在 Store 的 append_event 里，保证单调、无空洞；
- "Run 单一终态"在这里物理保证：账本一旦出现终态事件，就锁死不再追加。

与课件 1-3 单文件版的区别：课件把 append 放在 RunState 上、RUNS 是裸字典；
本作业抽出独立的 RunStore 类，把"数据(Run)"和"管理(Store)"分开，
第 10 步的 SQLite 持久化将以"写穿"方式挂进来，接口保持不变。

订阅模型：每个 Run 挂一个 asyncio.Condition，
append_event 时 notify_all，第 7 步的 SSE 订阅端 wait 在这上面等新事件。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .events import (
    TERMINAL_STATUSES,
    RUN_STATUS_CREATED,
    RunEvent,
    RunEventType,
    is_terminal_event,
)


@dataclass
class Run:
    """一条 Run 的全部状态。事件账本 events 就是审计日志本身。"""

    run_id: str
    prompt: str
    status: str = RUN_STATUS_CREATED
    created_at: float = field(default_factory=time.time)
    # append-only 事件账本：下标即 seq（events[i].seq == i 是 Store 维护的不变量）
    events: list[RunEvent] = field(default_factory=list)
    # 用户取消信号：HTTP 层 set，Agent Loop 在每个模型事件处检查
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # 最近一次 checkpoint（第 6 步写入、第 10 步落 SQLite）
    checkpoint: dict[str, Any] | None = None
    # 订阅通知：有新事件时唤醒所有等待中的 SSE 订阅者
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)


class RunStore:
    """内存版 Run Store。第 10 步会用 SQLite 做写穿持久化，接口不变。"""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    # ------------------------------------------------------------ 登记查询 --
    def create_run(self, prompt: str) -> Run:
        """登记一个新 Run（只建记录不发事件；run.created 由调用方追加）。"""
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        run = Run(run_id=run_id, prompt=prompt)
        self._runs[run_id] = run
        return run

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    # ------------------------------------------------------------ 事件账本 --
    async def append_event(
        self, run: Run, event_type: RunEventType, data: dict[str, Any]
    ) -> RunEvent:
        """唯一的追加事件入口：分配 seq -> 守卫终态 -> 落账 -> 通知订阅者。

        守卫规则（"Run 单一终态"的物理保证）：
        账本末尾已经是终态事件时，拒绝任何追加 —— 一个 Run 的流
        到终态就真正结束，不可能再出现任何事件。

        为什么是 async：notify_all 必须在 async with 持锁的上下文里调用，
        追加与通知做成一个原子小节，订阅者醒来时账本一定已经更新。
        """
        if run.events and is_terminal_event(run.events[-1]):
            raise RuntimeError(
                f"run {run.run_id} 已处于终态，拒绝追加事件 {event_type}"
            )

        # 下标即序号：账本 append-only，因此 seq == len(events) 恒成立
        event = RunEvent(
            run_id=run.run_id,
            seq=len(run.events),
            type=event_type,
            ts=time.time(),
            data=data,
        )
        async with run.changed:
            run.events.append(event)
            run.changed.notify_all()
        return event

    def events_after(self, run: Run, after_seq: int) -> list[RunEvent]:
        """返回 seq > after_seq 的所有事件 —— 断线恢复的补发窗口。

        因为下标即 seq，这里就是一个切片，O(1) 定位。
        """
        start = after_seq + 1
        if start < 0:
            start = 0
        return run.events[start:]

    # ------------------------------------------------------------ 状态守卫 --
    def set_status(self, run: Run, new_status: str) -> None:
        """带守卫的状态迁移：终态只允许幂等重放，禁止换到别的终态。"""
        if run.status in TERMINAL_STATUSES:
            if new_status == run.status:
                return  # 幂等：重复设置同一终态视为无害重放
            raise RuntimeError(
                f"run {run.run_id} 已终态({run.status})，禁止迁移到 {new_status}"
            )
        run.status = new_status

    # ------------------------------------------------------------- 取消 --
    def request_cancel(self, run: Run) -> bool:
        """设置取消信号。返回 False 表示 Run 已终态，无需（也无法）取消。"""
        if run.status in TERMINAL_STATUSES:
            return False
        run.cancel_event.set()
        return True

    # ---------------------------------------------------------- 订阅等待 --
    async def wait_for_events(self, run: Run, timeout: float) -> bool:
        """等待"账本变长"。超时返回 False（订阅端借此发心跳），有变化返回 True。

        注意：返回 True 只代表"被唤醒过"，醒来后应重新调 events_after
        取增量 —— 这是条件变量的标准用法（防止虚假唤醒漏事件）。
        """
        async with run.changed:
            try:
                await asyncio.wait_for(run.changed.wait(), timeout)
                return True
            except TimeoutError:
                return False

    # ---------------------------------------------------------- checkpoint --
    def save_checkpoint(self, run: Run, payload: dict[str, Any]) -> None:
        """保存最近一次进度快照（覆盖写）。第 10 步将同步写穿到 SQLite。"""
        run.checkpoint = {"saved_at": time.time(), **payload}
