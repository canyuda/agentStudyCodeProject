"""步骤 3：StreamingModel 抽象 —— 定义 Agent Loop 眼中"模型"的样子。

本作业最核心的分层设计：两套事件，各管一段。

    DeepSeek SDK chunk           供应商私有格式，网关其余部分一律不认识
          │  第 4 步 Adapter 负责翻译
          ▼
    ModelStreamEvent             模型侧统一中间事件：纯"模型说了什么"
          │  第 6 步 Agent Loop 负责补身份（run_id / seq / ts）
          ▼
    RunEvent                     网关对外统一事件：可推送、可审计、可恢复

为什么分两层、不一步到位：
- 模型不知道 Run 的存在：它没有 run_id，也不知道 seq 该从几开始，
  这些"身份信息"是网关的职责，所以补全只能发生在 Agent Loop；
- 更换模型供应商只影响 Adapter 及以下，RunEvent、SSE、Store、客户端
  全部不动 —— 这就是验收标准"不依赖模型 SDK"的分层保障。

本文件不 import 任何第三方库（更不 import openai），
是"抽象层零依赖"的物理证明。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# -------------------------------------------------------- 模型侧统一事件 --
ModelStreamEventType = Literal[
    "text.delta",     # 一段文本增量
    "model.finished", # 模型报告结束原因（stop / length / ...）
    "model.usage",    # token 用量统计（可选，Fake 模型也会模拟一份）
]


@dataclass(frozen=True)
class ModelStreamEvent:
    """Adapter 把供应商 chunk 翻译后的统一中间事件。

    与 RunEvent 的区别一句话：ModelStreamEvent 是"内容"，RunEvent 是
    "带身份与序号的内容"。前者由 Adapter 产出，后者由 Agent Loop 包装。
    """

    type: ModelStreamEventType
    data: dict[str, Any] = field(default_factory=dict)


# OpenAI 风格消息：{"role": "system" | "user" | "assistant", "content": "..."}
# 这是行业事实标准，教学项目直接沿用，不自造格式；
# 若未来某供应商消息格式不同，翻译责任同样落在它的 Adapter 里。
Message = dict[str, str]


# -------------------------------------------------------------- 抽象协议 --
@runtime_checkable
class StreamingModel(Protocol):
    """任何"能流式吐字"的东西都算模型：DeepSeek、Fake、甚至文件回放。

    用 Protocol 而不是 ABC：实现类不需要继承任何基类，
    只要具备相同签名的 stream 方法即可（结构化子类型 / 鸭子类型）。
    第 4 步的 FakeStreamingModel 与 DeepSeekChatAdapter 因此地位完全平等，
    Agent Loop 永远只认这个协议，不看具体类型。
    """

    async def stream(
        self,
        messages: list[Message],
    ) -> AsyncIterator[ModelStreamEvent]:
        """按生成顺序异步产出模型事件流，流自然耗尽即模型输出结束。

        实现方必须遵守的契约（Agent Loop 依赖这些假设）：
        1. text.delta 按真实生成顺序到达，data["delta"] 是非空字符串；
        2. model.finished 至多出现一次，通常在流末尾；
        3. 过程中抛出的任何异常都表示"本次调用失败"，
           Agent Loop 会统一转换成 run.failed 终态事件。
        """
        ...
