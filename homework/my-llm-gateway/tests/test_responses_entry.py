"""POST /v1/responses north entry (scenario 9) + stream checkpoint (task 9.2)."""

from __future__ import annotations

import json

from gateway.config import CheckpointCfg
from tests.conftest import AUTH, build_ctx


def _responses_call(ctx, payload):
    return ctx.client.post("/v1/responses", json=payload, headers=AUTH)


def test_responses_entry_nonstream_via_chat_pipeline(ctx):
    """Scenario 9: /v1/responses normal call -> same pipeline, Responses wire format out."""
    ctx.resp.default_text("hi from responses")
    r = _responses_call(ctx, {"model": "pro", "instructions": "be nice", "input": "hello there"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "response" and body["status"] == "completed"
    assert body["model"] == "pro"  # alias only
    block = body["output"][0]["content"][0]
    assert block["type"] == "output_text" and block["text"] == "hi from responses"
    assert body["usage"]["input_tokens"] == 10
    # north Responses shape translated to the IR and out again: instructions -> upstream instructions
    upstream = ctx.resp.requests[0]["body"]
    assert upstream["instructions"] == "be nice"
    assert upstream["input"][0]["content"][0]["text"] == "hello there"
    row = ctx.latest_usage()
    assert row["entrypoint"] == "/v1/responses" and row["status"] == "success"


def test_responses_entry_auth_401(ctx):
    r = ctx.client.post("/v1/responses", json={"model": "pro", "input": "x"})  # no auth
    assert r.status_code == 401


def test_responses_entry_streaming(ctx):
    ctx.resp.enqueue({"type": "stream", "deltas": ["a", "b"], "usage": {"input_tokens": 4, "output_tokens": 2}})
    with ctx.client.stream(
        "POST",
        "/v1/responses",
        json={"model": "pro", "input": "hi", "stream": True},
        headers=AUTH,
    ) as r:
        assert r.status_code == 200
        assert r.headers["x-request-id"]
        text = r.read().decode()
    deltas = [json.loads(line[6:])["delta"] for line in text.splitlines()
              if line.startswith("data:") and "output_text.delta" in line]
    assert deltas == ["a", "b"]
    assert "event: response.completed" in text
    assert text.strip().endswith("data: [DONE]")


def test_responses_entry_routes_to_anthropic_protocol(ctx):
    """North Responses protocol + Anthropic upstream: protocols are independent layers."""
    ctx.anth.default_text("from-anthropic")
    r = _responses_call(ctx, {"model": "flash", "input": "ping"})
    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == "from-anthropic"
    upstream = ctx.anth.requests[0]["body"]
    assert upstream["messages"][0]["content"][0]["text"] == "ping"  # translated to Anthropic wire format
    assert ctx.latest_usage()["protocol"] == "anthropic_messages"


# ------------------------------------------------------------------ checkpoint


def test_checkpoint_disabled_returns_404(ctx):
    ctx.resp.enqueue({"type": "stream", "deltas": ["x"]})
    with ctx.chat({"stream": True}) as r:
        r.read()
    rid = r.headers["x-request-id"]
    got = ctx.client.get(f"/v1/streams/{rid}/checkpoint", headers=AUTH)
    assert got.status_code == 404
    assert ctx.app.state.gateway.config.stream_checkpoint.enabled is False  # default off


def test_checkpoint_enabled_lifecycle(tmp_path):
    from tests.conftest import make_config

    cfg = make_config(tmp_path).model_copy(
        update={"stream_checkpoint": CheckpointCfg(enabled=True, interval_seconds=0.05, max_chars=50)}
    )
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.resp.enqueue({"type": "stream", "deltas": ["hello ", "world ", "again"],
                          "usage": {"input_tokens": 1, "output_tokens": 3}})
        with ctx.chat({"stream": True}) as r:
            rid = r.headers["x-request-id"]
            r.read()
        cp = ctx.client.get(f"/v1/streams/{rid}/checkpoint", headers=AUTH).json()
        assert cp["status"] == "succeeded"
        assert cp["text"] == "hello world again"  # client-visible (restored) text
        # unknown request id -> 404
        assert ctx.client.get("/v1/streams/nope/checkpoint", headers=AUTH).status_code == 404


def test_checkpoint_cap_and_failed_status(tmp_path):
    from tests.conftest import make_config

    cfg = make_config(tmp_path).model_copy(
        update={"stream_checkpoint": CheckpointCfg(enabled=True, interval_seconds=0.05, max_chars=10)}
    )
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.resp.enqueue({"type": "stream", "deltas": ["0123456789", "MORE"], "abort_after": 2})
        with ctx.chat({"stream": True}) as r:
            rid = r.headers["x-request-id"]
            r.read()
        cp = ctx.client.get(f"/v1/streams/{rid}/checkpoint", headers=AUTH).json()
        assert cp["status"] == "failed"
        assert cp["text"] == "0123456789"  # capped at max_chars, extra growth dropped
