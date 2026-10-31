"""Adapter protocol fidelity: scenarios 16 / 17 / 19 against captured upstream frames."""

from __future__ import annotations

import pytest

from tests.conftest import build_ctx


def test_responses_adapter_request_rewrite(ctx):
    """Scenario 16: Bearer header, instructions field, input array, max_output_tokens, usage."""
    ctx.resp.default_text("ok")
    r = ctx.chat(
        {
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "bye"},
            ],
            "max_tokens": 77,
        }
    )
    assert r.status_code == 200
    req = ctx.resp.last_request
    assert req["url"].endswith("/responses")
    # auth: Authorization Bearer, upstream key from provider config
    assert req["headers"]["authorization"].startswith("Bearer ")
    # system prompt lives in `instructions`, NOT inside the message sequence
    assert req["body"]["instructions"] == "be brief"
    inp = req["body"]["input"]
    assert [m["role"] for m in inp] == ["user", "assistant", "user"]
    assert inp[0]["content"][0]["type"] == "input_text"
    assert inp[1]["content"][0]["type"] == "output_text"
    assert req["body"]["max_output_tokens"] == 77
    # usage mapped back north
    assert r.json()["usage"]["prompt_tokens"] == 10
    assert r.json()["usage"]["completion_tokens"] == 5


def test_anthropic_adapter_request_rewrite(ctx):
    """Scenario 17: x-api-key + version header, top-level system, block content,
    mandatory max_tokens default, stop_reason mapping, cache usage."""
    ctx.anth.default_text("ok-anth")
    r = ctx.chat(
        {
            "model": "flash",
            "messages": [
                {"role": "system", "content": "sys-prompt"},
                {"role": "user", "content": "hello"},
            ],
        }
    )
    assert r.status_code == 200
    req = ctx.anth.last_request
    assert req["url"].endswith("/messages")
    assert req["headers"]["x-api-key"]  # x-api-key header, not Bearer
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    body = req["body"]
    assert body["system"] == "sys-prompt"           # top-level system field
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])  # no system in messages
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "hello"}  # block list
    assert body["max_tokens"] > 0                    # mandatory upstream -> default injected
    assert body["stream"] is False
    # response side: text from `content` blocks, stop_reason -> finish_reason
    assert r.json()["choices"][0]["finish_reason"] == "stop"


def test_anthropic_max_tokens_default_and_length_mapping(ctx):
    ctx.anth.enqueue({"type": "text", "text": "cut", "finish": "length"})
    r = ctx.chat({"model": "flash"})
    assert r.status_code == 200
    assert ctx.anth.last_request["body"]["max_tokens"] == 4096  # safe default
    assert r.json()["choices"][0]["finish_reason"] == "length"  # max_tokens -> length


def test_upstream_error_normalization_5xx_to_502(ctx):
    """Scenario 19 (half): upstream 5xx exhausted -> 502 with upstream detail; body unified."""
    for _ in range(3):  # exhaust all attempts of the single-route alias
        ctx.anth.enqueue({"type": "error", "status": 500, "message": "responses-boom"})
    r = ctx.chat({"model": "flash"})
    assert r.status_code == 502
    assert "responses-boom" in r.json()["error"]["message"]
    assert set(r.json()["error"]) == {"message", "type", "param", "code"}


def test_upstream_error_normalization_anthropic_4xx_passthrough(ctx):
    """Scenario 19 (half): Anthropic 4xx status passes through with upstream detail."""
    ctx.anth.enqueue({"type": "error", "status": 400, "message": "anth-invalid",
                      "error_type": "invalid_request_error"})
    r = ctx.chat({"model": "flash"})
    assert r.status_code == 400
    assert "anth-invalid" in r.json()["error"]["message"]


def test_upstream_error_shapes_are_protocol_native(ctx):
    """Both protocols error with their OWN wire shapes; gateway output is one shape."""
    # resp case routes through pro (resp -> anth fallback): exhaust BOTH routes
    for _ in range(3):
        ctx.resp.enqueue({"type": "error", "status": 500, "message": "boom-resp"})
        ctx.anth.enqueue({"type": "error", "status": 500, "message": "boom-resp"})
    r = ctx.chat({})
    assert r.status_code == 502
    assert r.json()["error"]["message"].endswith("boom-resp")
    for _ in range(3):
        ctx.anth.enqueue({"type": "error", "status": 500, "message": "boom-anth"})
    r = ctx.chat({"model": "flash"})
    assert r.status_code == 502
    assert r.json()["error"]["message"].endswith("boom-anth")


@pytest.mark.parametrize("protocol_alias,upstream", [("pro", "resp"), ("flash", "anth")])
def test_both_protocols_basic_nonstream(tmp_path, protocol_alias, upstream):
    """Both adapters are really hit end-to-end (also feeds scenario 1)."""
    ctx = build_ctx(tmp_path)
    with ctx.client:
        fake = getattr(ctx, upstream)
        fake.default_text(f"answer-{upstream}")
        r = ctx.chat({"model": protocol_alias})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == f"answer-{upstream}"
        assert r.json()["model"] == protocol_alias  # alias echoed, not real model id
