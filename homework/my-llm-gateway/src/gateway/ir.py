"""中间表示（IR, Intermediate Representation）：协议无关的请求契约。

【教学定位：统一抽象层的枢纽】
这是整个网关最重要的设计决策之一。两种北向协议（Chat Completions /
Responses）先各自"降维"成 ChatIR，再由适配器翻译成任意一种上游协议：

    Chat Completions 请求 ─┐
                          ├─解析─> ChatIR ─适配器─> Responses / Anthropic 上游
    Responses 请求        ─┘

好处（也是判断该不该引入 IR 的标准）：
- 协议组合爆炸被消除：N 种北向 × M 种上游，直连需要 N×M 套翻译器，
  有了 IR 只需要 N+M 套。本项目 2+2 就省了一半，协议越多省得越多。
- "北向入口协议"与"上游协议"完全解耦：用 /v1/responses 入口调用
  Anthropic 上游的模型毫无障碍（有测试覆盖该组合）。
- 新增第三种上游协议 = 只加一个适配器文件，其余分层不动。

IR 内容 = 需求文档所说的"消息列表 + 采样参数 + 约束声明"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

# 角色用 Literal 收紧类型：写错 role（如 "System"）在解析期就会 422，
# 而不是把脏数据带进上游请求。
Role = Literal["system", "user", "assistant"]


class IRMessage(BaseModel):
    """一条对话消息。content 统一为纯字符串——多模态/分段内容在解析入口
    就被展平成文本，IR 层不关心北向协议的花样。"""

    role: Role
    content: str


@dataclass
class JSONSchemaConstraint:
    """结构化输出的"约束声明"：目标 JSON Schema（+ 名称）。

    为什么是普通 dataclass 而不是 Pydantic 模型？因为字段必须叫 `schema`
    （与线上协议同名），而 `schema` 是 Pydantic BaseModel 的内建方法名，
    用 Pydantic 会触发字段遮蔽警告。这里不需要校验能力，dataclass 正合适。
    """

    name: str = "response"
    schema: dict[str, Any] = field(default_factory=dict)


class PromptRef(BaseModel):
    """网关扩展字段：提示词模板引用（调用方不必自己维护完整系统提示词）。

    请求里带 prompt_ref 后，网关在发往上游前完成"模板渲染 + 消息注入"：
    - position="system"：渲染结果作为系统消息插到最前（系统位）
    - position="suffix"：渲染结果追加到对话末尾（末尾位）
    version 不填则取该提示词的"激活版本"。
    """

    id: str
    version: int | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    position: Literal["system", "suffix"] = "system"
    role: Role | None = None  # 缺省规则：system 位用 system 角色，suffix 位用 user 角色


class SamplingParams(BaseModel):
    """采样参数（三个协议共通的子集）。全 None = 不传，由上游用默认值。"""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None  # 注意：Anthropic 上游要求必填，由适配器兜底


class ChatIR(BaseModel):
    """协议无关的聊天请求（IR 主体）。

    model 存的是"公开别名"——真实上游模型 ID 只在路由命中后、进适配器时
    才以参数传入（build_request(ir, model_id)），IR 本身绝不携带真实 ID，
    从数据流上杜绝了"真实模型 ID 泄漏到北向"的可能。
    """

    model: str  # 公开别名（不是上游真实模型 ID）
    messages: list[IRMessage]
    sampling: SamplingParams = Field(default_factory=SamplingParams)
    stream: bool = False
    constraint: JSONSchemaConstraint | None = None  # 结构化输出约束（可选）
    include_usage: bool = True  # 流式末块是否携带 usage（对应 Chat 的 stream_options）
    prompt_ref: PromptRef | None = None  # 提示词引用（网关扩展，可选）


class Usage(BaseModel):
    """Token 用量的统一结构（账本与北向响应共用）。

    cache_tokens = 缓存命中的输入 Token（Anthropic 的
    cache_read_input_tokens / OpenAI 的 cached_tokens 都归一到这里）。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0


class CompletionResult(BaseModel):
    """非流式补全结果（任意上游协议归一后的形态）。

    finish_reason 已归一为北向语义：stop（正常结束）/ length（达到
    max_tokens 被截断）/ content_filter 等。
    """

    text: str
    finish_reason: str
    usage: Usage = Field(default_factory=Usage)


# ---- 流式事件的统一模型（适配器产出，与上游协议无关）----
# 只有两种事件，刻意保持最小：
# - DeltaEvent：一段增量文本
# - DoneEvent：流结束（结束原因 + 用量）
# "错误"不走事件模型——适配器直接抛 UpstreamError，由 pipeline 决定
# 是重试切换（首字节前）还是发 SSE 错误事件收尾（首字节后）。

class DeltaEvent(BaseModel):
    content: str


class DoneEvent(BaseModel):
    finish_reason: str
    usage: Usage | None = None


StreamEvent = DeltaEvent | DoneEvent


def extract_system(ir: ChatIR) -> str:
    """抽出全部 system 消息拼接为系统提示词。

    两种上游协议对系统提示的"安放位置"完全不同（Responses 的
    instructions 字段 vs Anthropic 的顶层 system 字段），但它们都要求
    系统提示与消息序列分离——所以 IR 保留 system 角色消息，由各适配器
    在改写时调用本函数抽出来放到位。
    """
    return "\n\n".join(m.content for m in ir.messages if m.role == "system")


def non_system_messages(ir: ChatIR) -> list[IRMessage]:
    """抽出非 system 消息（进入各协议的消息序列：input / messages）。"""
    return [m for m in ir.messages if m.role != "system"]
