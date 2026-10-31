"""北向请求解析：线上格式 -> ChatIR（中间表示）。

【教学定位】这是两个北向入口的"入口翻译官"：
- Chat Completions 请求 -> parse_chat_request -> ChatIR
- Responses 请求        -> parse_responses_request -> ChatIR

解析要点：
- content 兼容"纯字符串"与"分段数组"两种形态（OpenAI 系协议惯例），
  统一展平为纯文本——分段细节止步于入口，IR 及以下无感知
- response_format 的 json_schema / json_object 都转成约束声明；
 Responses 的 instructions 转成 system 消息（进入 IR 的消息列表，
  由各适配器再翻译到 instructions / 顶层 system 字段）
- prompt_ref 是网关扩展字段，原样进入 IR，由 pipeline._prepare 处理
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from gateway.ir import ChatIR, IRMessage, JSONSchemaConstraint, PromptRef, SamplingParams


def _content_text(content: Any) -> str:
    """Chat content may be a plain string or a list of typed parts; keep text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and "text" in part
        )
    return str(content)


class ChatMessageIn(BaseModel):
    role: str
    content: Any = ""


class JsonSchemaSpec(BaseModel):
    name: str = "response"
    schema_: dict[str, Any] = Field(alias="schema")
    strict: bool = False


class ResponseFormatIn(BaseModel):
    type: str = "text"
    json_schema: JsonSchemaSpec | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessageIn]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    response_format: ResponseFormatIn | None = None
    prompt_ref: PromptRef | None = None  # gateway extension
    stream_options: StreamOptions | None = None
    include_usage: bool = True  # gateway extension: usage on the final stream chunk


def parse_chat_request(payload: ChatCompletionRequest) -> ChatIR:
    constraint: JSONSchemaConstraint | None = None
    if payload.response_format is not None:
        rf = payload.response_format
        if rf.type == "json_schema" and rf.json_schema is not None:
            constraint = JSONSchemaConstraint(name=rf.json_schema.name, schema=rf.json_schema.schema_)
        elif rf.type == "json_object":
            constraint = JSONSchemaConstraint(name="response", schema={"type": "object"})
    return ChatIR(
        model=payload.model,
        messages=[IRMessage(role=m.role, content=_content_text(m.content)) for m in payload.messages],
        sampling=SamplingParams(
            temperature=payload.temperature,
            top_p=payload.top_p,
            max_tokens=payload.max_tokens or payload.max_completion_tokens,
        ),
        stream=payload.stream,
        constraint=constraint,
        include_usage=payload.include_usage
        or bool(payload.stream_options and payload.stream_options.include_usage),
        prompt_ref=payload.prompt_ref,
    )


# ---------------------------------------------------------------- Responses API


class ResponsesInputItem(BaseModel):
    role: str = "user"
    content: Any = ""


class ResponsesRequest(BaseModel):
    model: str
    input: Any = ""
    instructions: str | None = None
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    response_format: ResponseFormatIn | None = None
    prompt_ref: PromptRef | None = None  # gateway extension


def parse_responses_request(payload: ResponsesRequest) -> ChatIR:
    constraint: JSONSchemaConstraint | None = None
    if payload.response_format is not None and payload.response_format.type == "json_schema":
        spec = payload.response_format.json_schema
        if spec is not None:
            constraint = JSONSchemaConstraint(name=spec.name, schema=spec.schema_)
    messages: list[IRMessage] = []
    if payload.instructions:
        messages.append(IRMessage(role="system", content=payload.instructions))
    items: list[ResponsesInputItem] = []
    if isinstance(payload.input, str):
        items = [ResponsesInputItem(role="user", content=payload.input)]
    elif isinstance(payload.input, list):
        items = [ResponsesInputItem.model_validate(i) for i in payload.input]
    messages.extend(IRMessage(role=(it.role or "user"), content=_content_text(it.content)) for it in items)
    return ChatIR(
        model=payload.model,
        messages=messages,
        sampling=SamplingParams(
            temperature=payload.temperature, top_p=payload.top_p, max_tokens=payload.max_output_tokens
        ),
        stream=payload.stream,
        constraint=constraint,
        prompt_ref=payload.prompt_ref,
    )
