"""Anthropic Messages API 适配器（上游协议：POST {base_url}/messages）。

【教学定位】与 openai_responses.py 对照阅读——同样的 IR 进，两种完全不同的
线上协议出。本适配器集中消化 Anthropic 协议的全部结构性差异：

1. 鉴权    ：x-api-key 头 + anthropic-version 协议版本头（没有 Bearer）
2. 系统提示：顶层 system 字段；messages 里只允许 user/assistant 角色
3. 消息结构：content 是"类型化块列表"（[{"type":"text","text":...}]）
4. 最大输出：max_tokens 是【必填】字段——调用方没传时必须注入安全默认值
5. 结构化输出：无原生 JSON Schema 约束能力
   -> 把 Schema 转译为"格式指令"注入系统提示，靠网关本地校验+修复闭环兜底
6. 流式事件：content_block_delta 文本增量 + message_delta 收尾（带停止原因与用量）
7. 停止原因：stop_reason 枚举（end_turn/max_tokens/stop_sequence）需映射回北向语义
"""

from __future__ import annotations

import json
from typing import Any

from gateway.ir import (
    ChatIR,
    CompletionResult,
    DeltaEvent,
    DoneEvent,
    StreamEvent,
    Usage,
    extract_system,
    non_system_messages,
)

from .base import UpstreamAdapter, UpstreamError

# max_tokens 在 Anthropic 协议中是必填项：调用方未传时注入的安全默认值。
# 取 4096 是"够用且不昂贵"的折中——教学项目里也方便改成配置。
DEFAULT_MAX_TOKENS = 4096

# Anthropic 停止原因 -> 北向 Chat 语义的映射表。
# 映射表（而不是 if-else 链）是教学推荐写法：新增枚举值只改数据不改逻辑。
_STOP_REASON_MAP = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}


class AnthropicMessagesAdapter(UpstreamAdapter):
    protocol = "anthropic_messages"

    def endpoint(self, base_url: str) -> str:
        """端点 = base_url + /messages。"""
        return f"{base_url.rstrip('/')}/messages"

    def _inject_auth(self, headers: dict[str, str], api_key: str) -> None:
        """覆写基类的 Bearer 注入：Anthropic 用 x-api-key + 协议版本头。

        setdefault：若供应商配置里已显式给出 anthropic-version，尊重配置。
        """
        headers["x-api-key"] = api_key
        headers.setdefault("anthropic-version", "2023-06-01")

    def build_request(self, ir: ChatIR, model_id: str) -> tuple[dict[str, str], dict[str, Any]]:
        """IR -> Anthropic Messages 请求体。"""
        headers: dict[str, str] = {}
        body: dict[str, Any] = {
            "model": model_id,
            # 【必填兜底】`or` 的妙用：max_tokens 为 None 时落到安全默认。
            # 不兜底的后果是上游直接 400，而调用方在北向协议里 max_tokens
            # 本来就是可选的——这类"协议必填差异"就该由适配器消化。
            "max_tokens": ir.sampling.max_tokens or DEFAULT_MAX_TOKENS,
            "stream": ir.stream,
            # content 必须是块列表（不是纯字符串）；system 角色绝不能出现在 messages 里
            "messages": [
                {"role": msg.role, "content": [{"type": "text", "text": msg.content}]}
                for msg in non_system_messages(ir)
            ],
        }
        system = extract_system(ir)
        if ir.constraint is not None:
            # 无原生约束：先在系统提示后追加格式指令（见 _append_format_instruction）
            system = self._append_format_instruction(system, ir)
        if system:
            body["system"] = system  # 系统提示放顶层 system 字段
        if ir.sampling.temperature is not None:
            body["temperature"] = ir.sampling.temperature
        if ir.sampling.top_p is not None:
            body["top_p"] = ir.sampling.top_p
        return headers, body

    @staticmethod
    def _append_format_instruction(system: str, ir: ChatIR) -> str:
        """把 JSON Schema 约束转译为提示词格式指令（需求 4.2 / 场景 20）。

        为什么"提示词注入"可行：Schema 是严格的机器规格，模型对
        "只输出符合此 Schema 的 JSON"这类指令遵循度很高；再配合网关
        本地 jsonschema 二次校验 + 自动修复闭环，最终仍能保证返回合法 JSON。
        ensure_ascii=False 保留中文原样，减少模型误读转义序列。
        """
        instruction = (
            "\n\n## Structured output requirement\n"
            "You MUST reply with a single valid JSON value that conforms exactly to the "
            "following JSON Schema. Output ONLY the JSON - no prose, no markdown fences.\n"
            f"JSON Schema: {json.dumps(ir.constraint.schema, ensure_ascii=False)}"
        )
        return (system + instruction) if system else instruction.strip()

    def parse_response(self, data: dict[str, Any]) -> CompletionResult:
        """Anthropic 非流式响应 -> CompletionResult。

        文本 = content 数组中所有 type=text 块的拼接；
        stop_reason 经映射表转成北向 finish_reason。
        """
        text = "".join(
            block.get("text", "") for block in data.get("content", []) or [] if block.get("type") == "text"
        )
        finish = _STOP_REASON_MAP.get(data.get("stop_reason", ""), "stop")
        return CompletionResult(text=text, finish_reason=finish, usage=self._usage(data.get("usage")))

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> Usage:
        """用量映射：缓存命中数在 cache_read_input_tokens 字段（与 OpenAI 的
        嵌套结构不同——又一个协议差异的消化点）。"""
        data = data or {}
        return Usage(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_tokens=int(data.get("cache_read_input_tokens", 0) or 0),
        )

    def parse_stream_event(self, event: str | None, data: dict[str, Any]) -> StreamEvent | None:
        """Anthropic 流式事件 -> 统一 Delta/Done 事件。

        事件序列（简化）：
            message_start          消息元信息（本网关不转发）
            content_block_start    内容块开始
            content_block_delta    文本增量（delta.type == "text_delta"）
            content_block_stop     内容块结束
            message_delta          收尾：stop_reason + 用量
            message_stop           消息结束
            ping                   保活心跳（直接忽略）
        """
        if event == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                return DeltaEvent(content=delta.get("text", "") or "")
            return None  # 其他类型的块增量（如工具调用）本网关不处理
        if event == "message_delta":
            # 收尾事件同时携带停止原因与用量——两个都从这里取
            stop = (data.get("delta") or {}).get("stop_reason")
            return DoneEvent(
                finish_reason=_STOP_REASON_MAP.get(stop, "stop"), usage=self._usage(data.get("usage"))
            )
        if event == "error":
            err = data.get("error") or {}
            raise UpstreamError(500, err.get("message") or json.dumps(data)[:300], "upstream_stream_error")
        return None  # message_start / content_block_start / ping / message_stop 与北向文本无关

    def parse_error_body(self, status: int, body: dict[str, Any]) -> UpstreamError:
        """Anthropic 错误体：{"type":"error","error":{type,message}}（外层多包了一层
        type 标记，type 枚举体系也与 OpenAI 不同）-> 归一 UpstreamError。"""
        err = body.get("error") or {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        return UpstreamError(
            status, err.get("message") or json.dumps(body)[:300], err.get("type") or "upstream_error"
        )
