"""可选的流检查点存储（功能开关控制，默认关闭！）。

【教学定位】动机：长文本流式生成到 90% 时网络断线，已生成内容全部
丢失，只能从头再来。检查点按时间间隔把"已解析的文本增量"存下来，
客户端凭响应头里的请求 ID 即可找回（需求 9）。

两条必须写进交付物的警示（本文件与 README 均有声明）：
1. 隐私：开启即持久化"模型正文"（已还原后的客户端可见文本）——
   含用户数据！开启前必须先确定加密、访问控制与数据保留策略。
2. 能力边界：它解决"断线找回"，【不突破模型上下文窗口限制】——
   超长任务仍应由上层 Agent 分段、摘要或检索式续写。

实现为单进程内存 dict（教学取舍）；max_chars 硬上限防止超长流
把内存撑爆（需求 9 "设最大字符上限防止无限增长"）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 检查点状态机（需求 9）：流式中 -> 成功 | 失败 | 已取消
STATUS_STREAMING = "streaming"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


@dataclass
class Checkpoint:
    """一个请求的检查点：状态 + 累计文本（截断到 max_chars）。"""
    request_id: str
    status: str = STATUS_STREAMING
    text: str = ""
    updated_at: float = field(default_factory=time.monotonic)

    def as_payload(self) -> dict:
        return {"request_id": self.request_id, "status": self.status, "text": self.text}


class CheckpointStore:
    """In-memory single-process store; batched appends capped at max_chars."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._checkpoints: dict[str, Checkpoint] = {}

    def start(self, request_id: str) -> None:
        self._checkpoints[request_id] = Checkpoint(request_id=request_id)

    def append(self, request_id: str, text: str) -> None:
        cp = self._checkpoints.get(request_id)
        if cp is None:
            return
        if len(cp.text) >= self._max_chars:
            return  # hard cap: never grow unbounded
        cp.text = (cp.text + text)[: self._max_chars]
        cp.updated_at = time.monotonic()

    def finish(self, request_id: str, status: str) -> None:
        cp = self._checkpoints.get(request_id)
        if cp is not None:
            cp.status = status
            cp.updated_at = time.monotonic()

    def get(self, request_id: str) -> Checkpoint | None:
        return self._checkpoints.get(request_id)
