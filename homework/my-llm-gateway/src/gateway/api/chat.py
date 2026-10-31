"""POST /v1/chat/completions —— 主北向入口（OpenAI Chat Completions 兼容）。

【教学定位：一个端点函数里的完整管道接线】
职责边界刻意收窄：本文件只做"协议进出"（解析请求/组装响应/SSE 渲染），
所有业务语义（重试、切换、修复、脱敏、TTFT、记账）都在 pipeline 里。

流式路径有个精妙的两段式（配合 pipeline.stream 的注释阅读）：
    first = await generator.__anext__()   # 先取出第一个事件再返回响应
为什么要预取？因为 StreamingResponse 一旦返回，HTTP 200 + SSE 头就
发出去了，再想报 404/503/429 就不可能。预取第一个事件让"首字节前"
的失败仍能以普通 HTTP 错误返回——这是流式网关的经典陷阱与解法。

SSE 渲染规则（_render）：
- DeltaEvent -> chat.completion.chunk（增量内容）
- DoneEvent  -> 末块（finish_reason + 可选 usage）+ data: [DONE]
- StreamErrorEvent -> data: {"error": {...}}（流中错误的载体）
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.api.deps import AppState, get_state, require_auth
from gateway.api.schemas import ChatCompletionRequest, parse_chat_request
from gateway.ir import CompletionResult, DeltaEvent, DoneEvent
from gateway.ledger.store import fingerprint
from gateway.pipeline import CallState, NorthStreamEvent, StreamErrorEvent

router = APIRouter()


def _sse(data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


def _chat_chunk(request_id: str, alias: str, delta: dict | None = None, finish_reason: str | None = None,
                usage: dict | None = None) -> dict:
    chunk: dict = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": alias,  # public alias only - upstream model ids never leak north
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def _usage_payload(result_usage) -> dict:
    return {
        "prompt_tokens": result_usage.input_tokens,
        "completion_tokens": result_usage.output_tokens,
        "total_tokens": result_usage.input_tokens + result_usage.output_tokens,
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    state: AppState = Depends(get_state),
    key: str = Depends(require_auth),
):
    ir = parse_chat_request(payload)
    # pipeline order: auth -> rate limit -> (render -> mask -> route happen inside pipeline)
    state.rate_limiter.check(fingerprint(key), ir.model)
    call_state = CallState(
        request_id=request.state.request_id,
        entrypoint="/v1/chat/completions",
        key_fingerprint=fingerprint(key),
        alias=ir.model,
        start_time=time.monotonic(),
    )
    if not ir.stream:
        result: CompletionResult = await state.pipeline.complete(ir, call_state)
        return JSONResponse(
            {
                "id": f"chatcmpl-{call_state.request_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": ir.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": result.finish_reason,
                    }
                ],
                "usage": _usage_payload(result.usage),
            },
            headers={"X-Request-Id": call_state.request_id},
        )

    generator = state.pipeline.stream(ir, call_state)
    first: NorthStreamEvent = await generator.__anext__()  # pre-first-byte errors -> normal HTTP error

    async def event_stream() -> AsyncIterator[str]:
        for chunk in _render(first):
            yield chunk
        async for event in generator:
            for chunk in _render(event):
                yield chunk
        yield "data: [DONE]\n\n"

    def _render(event: NorthStreamEvent) -> list[str]:
        if isinstance(event, DeltaEvent):
            return [_sse(_chat_chunk(call_state.request_id, ir.model, delta={"content": event.content}))]
        if isinstance(event, DoneEvent):
            if event.finish_reason == "error":
                return []  # error event already sent; [DONE] closes the stream
            usage = _usage_payload(event.usage) if (event.usage and ir.include_usage) else None
            return [
                _sse(_chat_chunk(call_state.request_id, ir.model, finish_reason=event.finish_reason, usage=usage))
            ]
        if isinstance(event, StreamErrorEvent):
            return [_sse(event.error.to_payload())]
        return []

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-Id": call_state.request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable reverse-proxy buffering
        },
    )
