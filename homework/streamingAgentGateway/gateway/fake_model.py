"""步骤 4a：FakeStreamingModel —— 本地可编程的假模型。

作用：
- 让整个网关"不依赖模型 SDK"也能完整演示：创建 Run、流式吐字、
  取消、断线恢复全部照常工作（因为模型只是延迟和文字是可以模拟的）；
- 为第 9 步异常测试提供可编程的失败点（fail_after）；
- 用 first_token_delay 精确控制 TTFT，方便观察"首个 delta 快速展示"。

它不继承任何基类，只靠第 3 步的 StreamingModel 协议（鸭子类型）工作。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .model import Message, ModelStreamEvent

# 默认输出文本：一篇有点长度的小短文，方便肉眼观察流式效果
DEFAULT_FAKE_TEXT = (
    "流式输出的价值在于尽快看见第一个字。"
    "这个假模型每隔一小段时间吐出一个词，"
    "用来模拟真实大模型逐 token 生成的节奏。"
    "你可以随时取消我，也可以断开连接再重连，"
    "网关会从你断开的位置继续补发事件，而不会重新调用模型。"
)


@dataclass
class FakeStreamingModel:
    """可编程假模型：所有"坏行为"都可以配置出来用于演示与测试。

    Attributes:
        text:               要输出的完整文本。
        chunk_size:         每个 delta 吐几个字符。
        first_token_delay:  首个 delta 前的等待秒数（模拟模型"思考"，
                            直接决定 TTFT 指标的大小）。
        inter_chunk_delay:  后续相邻 delta 的间隔秒数。
        finish_reason:      模拟的结束原因；设为 "length" 可以在第 6 步
                            触发 OUTPUT_TRUNCATED 失败分支。
        fail_after:         第 N 个 delta 之后抛出异常（None 表示不失败），
                            第 9 步异常测试的注入点。
        fail_error:         要抛出的异常实例，默认 RuntimeError。
    """

    text: str = DEFAULT_FAKE_TEXT
    chunk_size: int = 2
    first_token_delay: float = 0.3
    inter_chunk_delay: float = 0.08
    finish_reason: str = "stop"
    fail_after: int | None = None
    fail_error: Exception | None = None

    async def stream(self, messages: list[Message]) -> AsyncIterator[ModelStreamEvent]:
        # 首 token 延迟：await 点也是协作式取消的检查点之一
        await asyncio.sleep(self.first_token_delay)

        emitted = 0
        for start in range(0, len(self.text), self.chunk_size):
            delta = self.text[start : start + self.chunk_size]
            yield ModelStreamEvent(type="text.delta", data={"delta": delta})
            emitted += 1

            # 可编程失败点：fail fast，异常交给 Agent Loop 转成 run.failed
            if self.fail_after is not None and emitted >= self.fail_after:
                raise self.fail_error or RuntimeError(
                    f"fake model failed after {emitted} deltas"
                )

            await asyncio.sleep(self.inter_chunk_delay)

        # 模拟一份 usage（教学口径：字符数近似 token 数）
        yield ModelStreamEvent(
            type="model.usage",
            data={
                "input_tokens": sum(len(m["content"]) for m in messages),
                "output_tokens": len(self.text),
                "total_tokens": sum(len(m["content"]) for m in messages)
                + len(self.text),
            },
        )
        yield ModelStreamEvent(
            type="model.finished",
            data={"finish_reason": self.finish_reason},
        )
