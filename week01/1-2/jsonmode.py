import json
import os
from openai import OpenAI
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class AgentAction(BaseModel):
    # 概念：先用 Pydantic 定义“期望输出结构”，把输出格式从“自然语言约定”
    # 升级成“代码里的显式契约”。这样既方便人阅读，也方便程序做自动校验。
    step: Literal["inspect_logs", "run_tests", "read_code", "ask_user"] = Field(
        description="Agent 下一步要执行的动作"
    )
    reason: str = Field(description="选择这个动作的原因")
    needs_user_input: bool = Field(description="是否需要向用户补充提问")
    confidence: float = Field(ge=0, le=1, description="当前判断的置信度，范围 0 到 1")

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
    max_retries=0,
)

# Pydantic 可以直接把模型转成 JSON Schema，避免我们手写字段说明。
# 好处是：提示词、数据结构、校验规则三者共用一份定义，不容易随着迭代逐渐漂移。
schema = AgentAction.model_json_schema()

response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {
            "role": "system",
            "content": (
                "你是 Agent 决策器。必须输出 json。"
                "输出必须符合以下 JSON Schema：\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            ),
        },
        {
            "role": "user",
            "content": "目标：检查 tests/test_api.py 为什么失败。当前还没有测试日志。",
        },
    ],
    # JSON mode 的价值：强约束模型返回合法 JSON，减少多余解释、Markdown 包裹、
    # 漏括号等格式问题，让下游代码更容易稳定解析。
    response_format={"type": "json_object"},
    max_tokens=1024,
    extra_body={"thinking": {"type": "disabled"}},
)

raw_text = response.choices[0].message.content
if not raw_text:
    raise RuntimeError("MODEL_EMPTY_RESPONSE: 模型没有返回任何内容")

try:
    # Pydantic 的价值：JSON mode 只能尽量保证“像 JSON”，但不能保证字段名、枚举值、
    # 类型和数值范围都完全符合业务要求；这里再做一次模型校验，才能把输出真正变成
    # “可直接进入业务逻辑”的结构化数据。
    action = AgentAction.model_validate_json(raw_text)
except ValidationError as exc:
    raise RuntimeError(f"MODEL_SCHEMA_INVALID: {exc}") from exc

print(action)
