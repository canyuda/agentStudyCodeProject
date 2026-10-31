"""Structured output: scenarios 3 (repair ok), 4 (repair limit 422), 20 (instruction-injected
protocol + local validation + repair loop), fence tolerance, streaming no-repair."""

from __future__ import annotations

import json

from tests.conftest import parse_sse

SCHEMA = {
    "type": "object",
    "properties": {"team": {"type": "string", "enum": ["billing", "support", "sales"]}},
    "required": ["team"],
    "additionalProperties": False,
}


def _structured(payload_overrides=None):
    body = {
        "model": "pro",
        "messages": [{"role": "user", "content": "classify: I was charged twice"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "team", "schema": SCHEMA}},
    }
    body.update(payload_overrides or {})
    return body


def test_repair_loop_succeeds(ctx):
    """Scenario 3: first output invalid -> repair round returns valid JSON."""
    ctx.resp.default_text("not json at all")
    ctx.resp.default_text(json.dumps({"team": "billing"}))
    r = ctx.chat(_structured())
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == '{"team": "billing"}'
    assert len(ctx.resp.requests) == 2  # original + 1 repair round
    # repair round carries the model's previous output + precise error + full schema
    repair_body = ctx.resp.requests[1]["body"]
    messages = repair_body["input"]
    assert messages[-2]["role"] == "assistant"          # model's bad output fed back
    assert messages[-1]["role"] == "user"
    instruction = messages[-1]["content"][0]["text"]
    assert "invalid JSON" in instruction                 # precise validation error
    assert '"team"' in instruction and "billing" in instruction  # full target schema
    assert ctx.latest_usage()["retries"] >= 1           # repair rounds surface in ledger


def test_schema_violation_error_path_in_repair_prompt(ctx):
    ctx.resp.default_text(json.dumps({"team": "unknown-team"}))  # valid JSON, wrong enum
    ctx.resp.default_text(json.dumps({"team": "support"}))
    r = ctx.chat(_structured())
    assert r.status_code == 200
    instruction = ctx.resp.requests[1]["body"]["input"][-1]["content"][0]["text"]
    assert "/team" in instruction                        # exact error path
    ctx.resp.default_text(json.dumps({"team": "billing"}))


def test_repair_rounds_exhausted_returns_422(ctx):
    """Scenario 4: default 1 repair round, still invalid -> 422."""
    for _ in range(3):
        ctx.resp.default_text("still not json")
    r = ctx.chat(_structured())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "structured_output_invalid"
    assert len(ctx.resp.requests) == 2  # original + 1 repair (default max_repair_rounds=1)
    assert ctx.latest_usage()["status"] == "failed"


def test_markdown_fence_tolerance(ctx):
    """Fenced JSON passes without triggering a repair round."""
    ctx.resp.default_text("```json\n{\"team\": \"sales\"}\n```")
    r = ctx.chat(_structured())
    assert r.status_code == 200
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"team": "sales"}
    assert len(ctx.resp.requests) == 1  # no repair needed


def test_streaming_never_repairs(ctx):
    """Streaming + schema: pass-through only, no local correction."""
    ctx.resp.enqueue({"type": "stream", "deltas": ["{", "\"team\":", " oops}"],
                      "usage": {"input_tokens": 3, "output_tokens": 3}})
    with ctx.chat({**_structured(), "stream": True}) as r:
        assert r.status_code == 200
        text = r.read().decode()
    contents = [e["choices"][0]["delta"]["content"] for e in parse_sse(text)
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    assert "".join(contents) == '{"team": oops}'  # invalid JSON delivered as-is
    assert len(ctx.resp.requests) == 1            # no repair round on streams


def test_anthropic_instruction_injection_and_repair_loop(tmp_path):
    """Scenario 20: no native constraint -> format instruction injected + local
    validation + repair loop still yields valid JSON."""
    from tests.conftest import build_ctx

    ctx = build_ctx(tmp_path)
    with ctx.client:
        ctx.anth.default_text("I think billing")            # invalid
        ctx.anth.default_text(json.dumps({"team": "billing"}))  # valid after repair
        r = ctx.chat({**_structured(), "model": "flash"})
        assert r.status_code == 200
        assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"team": "billing"}
        # upstream request must NOT contain a native schema field, but the system
        # prompt must carry the JSON Schema format instruction
        first = ctx.anth.requests[0]["body"]
        assert "response_format" not in first and "json_schema" not in json.dumps(first)[:80]
        assert "JSON Schema" in first["system"] and "billing" in first["system"]
        # repair round also went through the instruction-injected path
        assert "JSON Schema" in ctx.anth.requests[1]["body"]["system"]


def test_responses_protocol_native_schema_passthrough(ctx):
    """Responses upstream: json_schema passed through natively (text.format)."""
    ctx.resp.default_text(json.dumps({"team": "support"}))
    r = ctx.chat(_structured())
    assert r.status_code == 200
    body = ctx.resp.requests[0]["body"]
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["schema"] == SCHEMA
