"""步骤 1：RunEvent —— 整个网关的统一事件模型（Harness Event）。

设计要点：
- 模型 Adapter、Agent Loop、SSE 传输、客户端渲染，全链路只认 RunEvent 这一种格式，
  这就是 8 项能力里"转换统一 Harness Event"的根基。
- 每个事件携带 Run 内单调递增的 seq，它是断线恢复的游标：
  客户端重连时上报 Last-Event-ID=seq，服务端从 seq+1 开始补发。
- 终态事件（completed / cancelled / failed）三选一、只发一次，
  保证验收标准里的"Run 单一终态"。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# ------------------------------------------------------------------ 事件类型 --
# 用 Literal 约束合法取值：写错事件名时类型检查器（和读代码的人）能立刻发现。
RunEventType = Literal[
    "run.created",       # Run 已受理，还没开始调用模型
    "run.started",       # Agent Loop 已开始执行
    "text.delta",        # 一段文本增量（token 流的最小单位）
    "checkpoint.saved",  # 一份进度快照已保存
    "run.metrics",       # 观测指标（TTFT / 总耗时等），在终态事件之前发出
    "run.completed",     # 终态：正常完成
    "run.cancelled",     # 终态：用户取消
    "run.failed",        # 终态：执行失败
]

# 终态事件集合：订阅端收到其中任意一个，就可以安全关闭 SSE 连接。
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"run.completed", "run.cancelled", "run.failed"}
)


def is_terminal_event(event: "RunEvent") -> bool:
    """判断事件是否为终态事件（订阅端据此收尾，Store 据此锁定状态）。"""
    return event.type in TERMINAL_EVENT_TYPES


# ------------------------------------------------------------------ Run 状态 --
# Run 的生命周期状态机：
#   created -> running -> (completed | cancelled | failed)
# 状态与事件是两条线：状态存进 Store 供查询接口用，事件用于流式推送与审计回放。
RUN_STATUS_CREATED = "created"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_FAILED = "failed"

# 已经处于这些状态的 Run 不允许再变更（单一终态的守卫条件）。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {RUN_STATUS_COMPLETED, RUN_STATUS_CANCELLED, RUN_STATUS_FAILED}
)


# -------------------------------------------------------------------- 事件体 --
@dataclass(frozen=True)
class RunEvent:
    """一次 Run 中发生的、可完整序列化的一件事。

    frozen=True 让事件不可变：事件一旦产生就是历史事实，
    审计时可以放心地把事件流当作 append-only 日志。

    Attributes:
        run_id: 事件所属的 Run ID，例如 "run_3f2a..."。
        seq:    Run 内从 0 开始的单调递增序号，断线重连的续传依据。
        type:   事件类型，取值见 RunEventType。
        ts:     事件产生时的 Unix 时间戳（秒），用于审计与耗时统计（TTFT）。
        data:   与类型相关的载荷，例如 text.delta 的 {"delta": "你好"}。
    """

    run_id: str
    seq: int
    type: RunEventType
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    # ---- 序列化 ----
    # SSE 编码（步骤 2）和 SQLite 落盘（步骤 10）都只走这两个方法，
    # 保证事件在内存、网络、磁盘三种形态下字段完全一致。
    def to_dict(self) -> dict[str, Any]:
        """转成可 json.dumps 的普通 dict（dataclass 默认就满足）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunEvent":
        """从 dict 还原事件；对缺失字段做最小容错，fail fast 于显式 KeyError。"""
        return cls(
            run_id=raw["run_id"],
            seq=int(raw["seq"]),
            type=raw["type"],
            ts=float(raw.get("ts", 0.0)),
            data=dict(raw.get("data") or {}),
        )
