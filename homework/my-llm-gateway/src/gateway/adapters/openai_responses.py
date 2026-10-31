"""OpenAI Responses API 适配器（上游协议：POST {base_url}/responses）。

【教学定位】本文件是"IR -> 上游协议"正向改写的示范之一。与 Anthropic 适配器
对照着读，能直观看到两种协议的结构性差异分别被消化在哪里。

本协议要点（差异消化位置）：
1. 鉴权    ：Authorization: Bearer 头         -> 基类默认 _inject_auth 即可，无需覆写
2. 系统提示：独立 instructions 字段            -> build_request 里显式拆出
3. 消息结构：input 数组，内容是类型化块        -> input_text / output_text 块
4. 最大输出：max_output_tokens（可选）         -> 字段名与 Chat 的 max_tokens 不同
5. 结构化输出：原生支持 JSON Schema            -> 直接透传到 text.format（无需注入指令）
6. 流式事件：response.output_text.delta 增量 + response.completed 收尾（带用量）
7. 错误体  ：{"error": {type, message, ...}}   -> parse_error_body 归一
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

# 本适配器侧的安全默认（供教学演示；Anthropic 侧的必填兜底见其适配器）
DEFAULT_MAX_OUTPUT_TOKENS = 4096


class OpenAIResponsesAdapter(UpstreamAdapter):
    protocol = "openai_responses"  # 与 config.PROTOCOLS、供应商表 protocol 字段对应

    def endpoint(self, base_url: str) -> str:
        """端点 = base_url + /responses（base_url 形如 https://api.deepseek.com/v1）。"""
        return f"{base_url.rstrip('/')}/responses"

    def build_request(self, ir: ChatIR, model_id: str) -> tuple[dict[str, str], dict[str, Any]]:
        """IR -> Responses 请求体。

        注意 model_id 是路由层传入的"真实上游模型 ID"——IR 里只有别名，
        真实 ID 只在本方法内部出现，绝不会回流到北向响应。
        """
        headers: dict[str, str] = {}  # 鉴权头由基类统一注入（Bearer）
        body: dict[str, Any] = {
            "model": model_id,
            "stream": ir.stream,
            # 消息序列：只放 user/assistant；内容项必须带类型标记，
            # 且 assistant 历史消息用 output_text、用户新输入用 input_text。
            "input": [
                {
                    "role": msg.role,
                    "content": [
                        {"type": "output_text" if msg.role == "assistant" else "input_text", "text": msg.content}
                    ],
                }
                for msg in non_system_messages(ir)
            ],
        }
        # 系统提示独立于消息序列：这是与 Chat 协议最大的结构差异之一
        system = extract_system(ir)
        if system:
            body["instructions"] = system
        # 采样参数：只透传调用方显式设置的（None 表示让上游用默认值）
        if ir.sampling.temperature is not None:
            body["temperature"] = ir.sampling.temperature
        if ir.sampling.top_p is not None:
            body["top_p"] = ir.sampling.top_p
        if ir.sampling.max_tokens is not None:
            body["max_output_tokens"] = ir.sampling.max_tokens  # 注意字段名差异
        if ir.constraint is not None:
            # 该协议原生支持 JSON Schema 约束：直接透传到 text.format，
            # 不需要（也不应该）往提示词里注入格式指令。
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": ir.constraint.name,
                    "schema": ir.constraint.schema,
                    "strict": True,
                }
            }
        return headers, body

    def parse_response(self, data: dict[str, Any]) -> CompletionResult:
        """Responses 非流式响应 -> CompletionResult。

        响应的 output 是"项(item)列表"，文本藏在 type=message 项的
        content 块里、且只认 output_text 类型的块——按类型过滤而非取下标，
        对上游追加新块类型更健壮。
        """
        text = ""
        for item in data.get("output", []) or []:
            if item.get("type") != "message":
                continue  # 跳过 reasoning 等其他类型的项
            for block in item.get("content", []) or []:
                if block.get("type") == "output_text":
                    text += block.get("text", "")
        # 结束原因：status=completed 正常结束；incomplete 且原因是
        # max_output_tokens 类的 => 归一为北向的 "length"
        finish = "stop"
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason", "")
            finish = "length" if "max_output" in reason or "token" in reason else "stop"
        return CompletionResult(text=text, finish_reason=finish, usage=self._usage(data.get("usage")))

    @staticmethod
    def _usage(data: dict[str, Any] | None) -> Usage:
        """用量映射：input_tokens/output_tokens 平铺，缓存命中数嵌套在
        input_tokens_details.cached_tokens 里——两层结构都要处理。
        `or 0` 防御 None/缺字段。"""
        data = data or {}
        usage = Usage(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
        )
        details = data.get("input_tokens_details") or {}
        usage.cache_tokens = int(details.get("cached_tokens", 0) or 0)
        return usage

    def parse_stream_event(self, event: str | None, data: dict[str, Any]) -> StreamEvent | None:
        """Responses 流式事件 -> 统一 Delta/Done 事件（场景 18 的核心）。

        事件序列（简化）：
            response.created               流开始（本网关不转发）
            response.output_text.delta     文本增量（多条）
            response.completed             收尾：状态 + 用量
            response.failed / error        失败
        """
        if event == "response.output_text.delta":
            return DeltaEvent(content=data.get("delta", "") or "")
        if event == "response.completed":
            # 用量挂在收尾事件的 response.usage 上——流式记账依赖这里
            response = data.get("response") or {}
            finish = "length" if response.get("status") == "incomplete" else "stop"
            return DoneEvent(finish_reason=finish, usage=self._usage(response.get("usage")))
        if event in ("response.failed", "error"):
            # 流中错误：抛 UpstreamError，由 pipeline 决定收尾方式
            err = data.get("error") or {}
            raise UpstreamError(500, err.get("message") or json.dumps(data)[:300], "upstream_stream_error")
        return None

    def parse_error_body(self, status: int, body: dict[str, Any]) -> UpstreamError:
        """Responses 错误体：{"error": {type, message}} -> 归一 UpstreamError。

        message 缺失时兜底输出截断后的原始 JSON（[:300]），
        保证"上游错误详情必须透出"的诊断需要。
        """
        err = body.get("error") or {}
        if not isinstance(err, dict):
            err = {"message": str(err)}
        return UpstreamError(
            status, err.get("message") or json.dumps(body)[:300], err.get("type") or "upstream_error"
        )
