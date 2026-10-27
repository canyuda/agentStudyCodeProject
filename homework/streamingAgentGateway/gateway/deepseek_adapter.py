"""步骤 4b：DeepSeekChatAdapter —— 真实模型的接入点。

关键设计：
- 本文件顶层依然不 import openai：SDK 只在工厂函数 build_deepseek_model()
  内部延迟导入。不调用工厂 = 整个进程没有 openai，"不依赖模型 SDK"可验证。
- Adapter 的唯一职责是"翻译"：DeepSeek chunk -> ModelStreamEvent，
  它不知道 RunEvent、run_id、seq 的存在（那是 Agent Loop 的事）。
- 网络参数集中为模块常量，并支持环境变量覆盖，不在业务代码里散落魔法值：
    DEEPSEEK_API_KEY    必需，从 project/.env 读取（该文件已被 gitignore）
    DEEPSEEK_BASE_URL   可选，默认官方地址
    DEEPSEEK_MODEL      可选，默认 deepseek-v4-flash
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import load_dotenv

from .model import Message, ModelStreamEvent

# ---- 集中管理的默认配置（均可用环境变量覆盖） ----
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS = 1024
# project/.env 位于本文件向上三级（gateway/ -> streamingAgentGateway/ -> homework/ -> project/）
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


class DeepSeekChatAdapter:
    """把 DeepSeek 的流式 chunk 翻译成 ModelStreamEvent。

    注意构造函数接收的是"已经建好的 AsyncOpenAI client"，
    这样类本身与 SDK 类型解耦，测试时可以注入任何长得一样的对象。
    """

    def __init__(self, client: object, model: str) -> None:
        self.client = client
        self.model = model

    async def stream(self, messages: list[Message]) -> AsyncIterator[ModelStreamEvent]:
        # 参数与课件 1-3 保持一致：关 thinking、带 usage 的纯文本流
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            max_tokens=DEFAULT_MAX_TOKENS,
            stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "disabled"}},
        )

        async for chunk in response:
            choice = chunk.choices[0] if chunk.choices else None

            if choice and choice.delta.content:
                yield ModelStreamEvent(
                    type="text.delta",
                    data={"delta": choice.delta.content},
                )

            if choice and choice.finish_reason:
                yield ModelStreamEvent(
                    type="model.finished",
                    data={"finish_reason": choice.finish_reason},
                )

            if chunk.usage is not None:
                yield ModelStreamEvent(
                    type="model.usage",
                    data={
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    },
                )


def build_deepseek_model(env_path: Path | None = None) -> "DeepSeekChatAdapter":
    """工厂：加载 .env -> 校验 key -> 延迟导入 SDK -> 组装 Adapter。

    fail fast：缺 key 在这里就抛 RuntimeError，绝不带着空 key 走到网络层。
    """
    load_dotenv(env_path or DEFAULT_ENV_PATH)

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"缺少 DEEPSEEK_API_KEY：请确认 {DEFAULT_ENV_PATH} 存在且包含该变量"
        )

    # 延迟导入：只有真正使用 DeepSeek 时才要求安装 openai SDK
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        timeout=60.0,
        max_retries=0,  # 重试策略由网关自己的 Agent Loop 决定，SDK 层不偷偷重试
    )
    model = os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
    return DeepSeekChatAdapter(client=client, model=model)
