"""Scripted fake upstreams speaking the REAL wire formats of both protocols.

httpx.MockTransport intercepts gateway->upstream HTTP; each fake records every
request it receives (URL, headers, JSON body) and replays a scripted behavior
queue (text / stream / error / timeout / mid-stream abort). Tests assert on
the captured requests - protocol fidelity of the adapters is verified against
these captured frames, and no real quota is ever consumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


def _sse(event: str | None, data: dict[str, Any]) -> str:
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


@dataclass
class StreamHandle:
    """Observability handle for one open upstream SSE stream."""

    closed: bool = False


@dataclass
class FakeUpstream:
    protocol: str  # "openai_responses" | "anthropic_messages"
    requests: list[dict[str, Any]] = field(default_factory=list)
    open_streams: list[StreamHandle] = field(default_factory=list)
    # Behavior queue; each request pops one. Empty queue -> default text reply.
    script: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ API

    def default_text(self, text: str) -> None:
        self.script.append({"type": "text", "text": text})

    def enqueue(self, behavior: dict[str, Any]) -> None:
        self.script.append(behavior)

    def clear(self) -> None:
        self.script.clear()
        self.requests.clear()
        self.open_streams.clear()

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # --------------------------------------------------------------- handler

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        behavior = self.script.pop(0) if self.script else {"type": "text", "text": f"hi-{self.protocol}"}
        kind = behavior.get("type")
        if kind == "timeout":
            raise httpx.ConnectTimeout("simulated upstream timeout", request=request)
        if kind == "error":
            return httpx.Response(behavior.get("status", 500), json=self._error_body(behavior))
        if kind == "text":
            return httpx.Response(200, json=self._text_body(behavior))
        if kind == "stream":
            return self._stream_response(behavior)
        raise AssertionError(f"unknown behavior {behavior}")

    # ----------------------------------------------------------- wire format

    def _text_body(self, behavior: dict[str, Any]) -> dict[str, Any]:
        text = behavior.get("text", "")
        usage = behavior.get("usage") or {"input_tokens": 10, "output_tokens": 5}
        if self.protocol == "openai_responses":
            body: dict[str, Any] = {
                "id": "resp_fake",
                "status": "incomplete" if behavior.get("finish") == "length" else "completed",
                "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
                ],
                "usage": dict(usage),
            }
            if body["status"] == "incomplete":
                body["incomplete_details"] = {"reason": "max_output_tokens"}
                body["usage"] = dict(usage)
            return body
        usage_with_cache = {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                            "cache_read_input_tokens": behavior.get("cache_tokens", 0)}
        return {
            "id": "msg_fake",
            "content": [{"type": "text", "text": text}],
            "stop_reason": {"length": "max_tokens", "stop": "end_turn"}.get(
                behavior.get("finish", "stop"), "end_turn"
            ),
            "usage": usage_with_cache,
        }

    def _error_body(self, behavior: dict[str, Any]) -> dict[str, Any]:
        message = behavior.get("message", "upstream boom")
        etype = behavior.get("error_type", "server_error")
        if self.protocol == "openai_responses":
            return {"error": {"type": etype, "message": message, "param": None, "code": None}}
        return {"type": "error", "error": {"type": etype, "message": message}}

    def _stream_response(self, behavior: dict[str, Any]) -> httpx.Response:
        chunks: list[str] = []
        deltas: list[str] = behavior.get("deltas", ["hello"])
        usage = behavior.get("usage") or {"input_tokens": 12, "output_tokens": 7}
        finish = behavior.get("finish", "stop")
        if self.protocol == "openai_responses":
            chunks.append(_sse("response.created", {"type": "response.created", "response": {}}))
            if behavior.get("abort_after") == 0:  # die before any content byte
                chunks.append(_sse("response.failed", {"type": "response.failed",
                                                       "error": {"message": "mid-stream abort"}}))
                return self._sse_response(chunks)
            for i, d in enumerate(deltas):
                chunks.append(_sse("response.output_text.delta",
                                   {"type": "response.output_text.delta", "delta": d}))
                if behavior.get("abort_after") == i + 1:
                    chunks.append(_sse("response.failed", {"type": "response.failed",
                                                           "error": {"message": "mid-stream abort"}}))
                    return self._sse_response(chunks)
            chunks.append(_sse("response.completed", {
                "type": "response.completed",
                "response": {"status": "incomplete" if finish == "length" else "completed", "usage": dict(usage)},
            }))
        else:
            chunks.append(_sse("message_start", {"type": "message_start",
                                                 "message": {"usage": {"input_tokens": usage["input_tokens"]}}}))
            chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": 0,
                                                       "content_block": {"type": "text"}}))
            if behavior.get("abort_after") == 0:  # die before any content byte
                chunks.append(_sse("error", {"type": "error",
                                             "error": {"type": "overloaded_error", "message": "mid-stream abort"}}))
                return self._sse_response(chunks)
            for i, d in enumerate(deltas):
                chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                           "delta": {"type": "text_delta", "text": d}}))
                if behavior.get("abort_after") == i + 1:
                    chunks.append(_sse("error", {"type": "error",
                                                 "error": {"type": "overloaded_error", "message": "mid-stream abort"}}))
                    return self._sse_response(chunks)
            chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
            chunks.append(_sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens" if finish == "length" else "end_turn"},
                "usage": {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                          "cache_read_input_tokens": behavior.get("cache_tokens", 0)},
            }))
            chunks.append(_sse("message_stop", {"type": "message_stop"}))
        return self._sse_response(chunks)

    def _sse_response(self, chunks: list[str]) -> httpx.Response:
        handle = StreamHandle()
        self.open_streams.append(handle)

        async def byte_stream():
            try:
                for chunk in chunks:
                    yield chunk.encode("utf-8")
            finally:
                handle.closed = True  # observed when the gateway closes the upstream stream

        class _ClosingStream(httpx.AsyncByteStream):
            """Real transports close the connection on response.aclose(); the default
            httpx.MockTransport byte stream does NOT propagate aclose, so we simulate it."""

            def __init__(self, source) -> None:
                self._source = source

            async def __aiter__(self):
                async for chunk in self._source:
                    yield chunk

            async def aclose(self) -> None:
                await self._source.aclose()  # propagates GeneratorExit into the content generator
                handle.closed = True

        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=_ClosingStream(byte_stream())
        )
