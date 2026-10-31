"""POST /v1/responses —— 第二北向入口（OpenAI Responses 协议，场景 9）。

【教学定位：证明"北向入口协议"与"上游协议"彻底解耦】
本端点说 Responses 协议，但请求解析成 ChatIR 后走的是与
Chat Completions 完全相同的管道（鉴权、限流、模板、脱敏、路由、
适配器、账本）——下游无论是 Responses 还是 Anthropic 供应商都照常工作。
两个入口唯一的区别就是"线上格式"，这正是中间表示（IR）的价值。

非流式：组装 Responses 风格响应对象（output 数组 + output_text 块 + usage）。
流式：产出 Responses 风格 SSE 事件：
    event: response.created            流开始
    event: response.output_text.delta  文本增量（多条）
    event: response.completed          收尾（带 usage）
    data: [DONE]                       结束标记
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from gateway.api.deps import AppState, get_state, require_auth
from gateway.api.schemas import ResponsesRequest, parse_responses_request
from gateway.ir import CompletionResult, DeltaEvent, DoneEvent
from gateway.ledger.store import fingerprint
from gateway.pipeline import CallState, NorthStreamEvent, StreamErrorEvent

router = APIRouter()


def _sse(event: str, data: dict) -> str:
    """组装一条 SSE 帧：event 行 + data 行 + 空行（帧结束符）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _response_object(request_id: str, alias: str, text: str | None, usage) -> dict:
    """组装 Responses 风格的响应对象（北向出口的反向翻译）。"""
    obj: dict = {
        "id": f"resp-{request_id}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": alias,
        "output": []
        if text is None
        else [
            {
                "type": "message",
                "id": f"msg-{request_id}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }
    if usage is not None:
        obj["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
        }
    return obj


@router.post("/v1/responses")
async def create_response(
    payload: ResponsesRequest,
    request: Request,
    state: AppState = Depends(get_state),
    key: str = Depends(require_auth),
):
    ir = parse_responses_request(payload)
    state.rate_limiter.check(fingerprint(key), ir.model)
    call_state = CallState(
        request_id=request.state.request_id,
        entrypoint="/v1/responses",
        key_fingerprint=fingerprint(key),
        alias=ir.model,
        start_time=time.monotonic(),
    )
    if not ir.stream:
        result: CompletionResult = await state.pipeline.complete(ir, call_state)
        return JSONResponse(
            _response_object(call_state.request_id, ir.model, result.text, result.usage),
            headers={"X-Request-Id": call_state.request_id},
        )

    generator = state.pipeline.stream(ir, call_state)
    first: NorthStreamEvent = await generator.__anext__()

    async def event_stream() -> AsyncIterator[str]:
        yield _sse("response.created", _response_object(call_state.request_id, ir.model, None, None))
        async for out in _render_then_rest(first, generator):
            yield out
        yield "data: [DONE]\n\n"

    async def _render_then_rest(first_event, gen) -> AsyncIterator[str]:
        for chunk in _render(first_event):
            yield chunk
        async for event in gen:
            for chunk in _render(event):
                yield chunk

    def _render(event: NorthStreamEvent) -> list[str]:
        if isinstance(event, DeltaEvent):
            return [
                _sse(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": event.content},
                )
            ]
        if isinstance(event, DoneEvent):
            if event.finish_reason == "error":
                return []
            return [
                _sse(
                    "response.completed",
                    {"type": "response.completed", "response": _response_object(
                        call_state.request_id, ir.model, "", event.usage)},
                )
            ]
        if isinstance(event, StreamErrorEvent):
            return [
                _sse(
                    "response.failed",
                    {"type": "response.failed", "response": {"error": event.error.to_payload()["error"]}},
                )
            ]
        return []

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-Id": call_state.request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
