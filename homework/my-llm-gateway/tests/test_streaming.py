"""Streaming: scenario 6 (SSE + TTFT + usage), 7 (post-first-byte abort), 8 (client disconnect),
18 (both protocols' events normalized to north SSE chunks)."""

from __future__ import annotations

import asyncio
import time

from gateway.api.schemas import ChatCompletionRequest, parse_chat_request
from gateway.ir import ChatIR, SamplingParams
from gateway.ledger.store import fingerprint
from gateway.pipeline import CallState
from tests.conftest import parse_sse


def test_sse_chunk_by_chunk_headers_ttft_usage(ctx):
    """Scenario 6 + 18 (responses protocol): normalized SSE, headers, TTFT, usage."""
    ctx.resp.enqueue({"type": "stream", "deltas": ["Hello", " ", "world"],
                      "usage": {"input_tokens": 11, "output_tokens": 7}})
    with ctx.chat({"stream": True}) as r:
        assert r.status_code == 200
        assert r.headers["x-request-id"]
        assert "no-cache" in r.headers.get("cache-control", "")
        assert r.headers.get("x-accel-buffering") == "no"
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(chunk.decode() for chunk in r.iter_raw(chunk_size=1))
    events = parse_sse(text)
    contents = [e["choices"][0]["delta"]["content"] for e in events
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert contents == ["Hello", " ", "world"]  # chunk-by-chunk, no buffering
    finals = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert finals and finals[0]["choices"][0]["finish_reason"] == "stop"
    assert finals[0]["usage"]["prompt_tokens"] == 11  # stream usage parsed
    assert events[-1] == {"_done": True}  # [DONE] marker
    assert all(e["model"] == "pro" for e in events if "model" in e)
    row = ctx.latest_usage()
    assert row["ttft_ms"] is not None and row["ttft_ms"] >= 0  # TTFT recorded
    assert row["input_tokens"] == 11 and row["output_tokens"] == 7
    assert row["status"] == "success"


def test_anthropic_stream_normalized_to_north_chunks(ctx):
    """Scenario 18 (anthropic protocol): content_block_delta -> north chunks."""
    ctx.anth.enqueue({"type": "stream", "deltas": ["Salut", "!"], "usage": {"input_tokens": 9, "output_tokens": 4},
                      "finish": "length", "cache_tokens": 3})
    with ctx.chat({"model": "flash", "stream": True}) as r:
        assert r.status_code == 200
        text = r.read().decode()
    events = parse_sse(text)
    contents = [e["choices"][0]["delta"]["content"] for e in events
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert contents == ["Salut", "!"]
    finals = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert finals[0]["choices"][0]["finish_reason"] == "length"  # max_tokens -> length
    row = ctx.latest_usage()
    assert row["cache_tokens"] == 3  # cache counter parsed
    assert row["ttft_ms"] is not None


def test_post_first_byte_abort_sends_sse_error_not_model_switch(ctx):
    """Scenario 7: upstream dies mid-stream -> SSE error event + end marker, NO failover."""
    ctx.resp.enqueue({"type": "stream", "deltas": ["partial ", "answer"], "abort_after": 2})
    with ctx.chat({"stream": True}) as r:
        assert r.status_code == 200
        text = r.read().decode()
    events = parse_sse(text)
    assert any("error" in e for e in events)  # SSE data event carries the unified error object
    assert events[-1] == {"_done": True}
    contents = [e["choices"][0]["delta"]["content"] for e in events
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert contents == ["partial ", "answer"]
    assert len(ctx.anth.requests) == 0  # never switched to the other model
    row = ctx.latest_usage()
    assert row["status"] == "failed" and row["http_status"] == 502


def test_pre_first_byte_stream_failure_fails_over(ctx):
    """Stream error BEFORE any content byte: retry/failover to the other route."""
    ctx.resp.enqueue({"type": "stream", "deltas": [], "abort_after": 0})  # fails before content
    ctx.anth.enqueue({"type": "stream", "deltas": ["saved"], "usage": {"input_tokens": 5, "output_tokens": 2}})
    with ctx.chat({"stream": True}) as r:
        assert r.status_code == 200
        text = r.read().decode()
    events = parse_sse(text)
    contents = [e["choices"][0]["delta"]["content"] for e in events
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert contents == ["saved"]
    assert ctx.latest_usage()["failovers"] >= 1


async def test_client_disconnect_cancels_and_closes_upstream(tmp_path):
    """Scenario 8: client disconnect -> upstream closed, status cancelled (499), no more tokens."""
    ctx = __import__("tests.conftest", fromlist=["build_ctx"]).build_ctx(tmp_path)
    with ctx.client:
        ctx.resp.enqueue({"type": "stream", "deltas": ["a", "b", "c", "d"]})
        state = ctx.app.state.gateway
        ir = ChatIR(model="pro", messages=[__import__("gateway.ir", fromlist=["IRMessage"]).IRMessage(
            role="user", content="hi")], stream=True, sampling=SamplingParams())
        call_state = CallState(
            request_id="req-disc", entrypoint="/v1/chat/completions",
            key_fingerprint=fingerprint("k"), alias="pro", start_time=time.monotonic(),
        )
        generator = state.pipeline.stream(ir, call_state)
        first = await generator.__anext__()  # first content byte forwarded
        assert first.content == "a"
        await generator.aclose()             # client disconnects right after
        await asyncio.sleep(0.05)            # let the background ledger write land
        handle = ctx.resp.open_streams[-1]
        assert handle.closed                 # upstream connection closed
        row = await state.ledger.latest()
        assert row["status"] == "cancelled" and row["http_status"] == 499
        assert len(ctx.resp.requests) == 1   # no further upstream consumption


def test_stream_usage_omitted_when_include_usage_false(ctx):
    ctx.resp.enqueue({"type": "stream", "deltas": ["x"], "usage": {"input_tokens": 3, "output_tokens": 2}})
    with ctx.chat({"stream": True, "include_usage": False}) as r:
        text = r.read().decode()
    finals = [e for e in parse_sse(text) if isinstance(e, dict) and e.get("choices")
              and e["choices"][0].get("finish_reason")]
    assert finals and "usage" not in finals[0]
    # ledger still records the parsed usage
    assert ctx.latest_usage()["output_tokens"] == 2


def test_request_parsing_extracts_content_parts():
    payload = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}]},
            {"role": "assistant", "content": "plain"},
        ]}
    )
    ir = parse_chat_request(payload)
    assert [m.content for m in ir.messages] == ["part1part2", "plain"]
