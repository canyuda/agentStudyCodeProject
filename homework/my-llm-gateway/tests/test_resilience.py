"""Resilience: retry + failover (scenario 2), rate limit (scenario 10), circuit breaker (scenario 11)."""

from __future__ import annotations

import time

from gateway.config import BreakerCfg, RateLimitCfg
from tests.conftest import build_ctx, make_config


def test_retry_then_failover_success_counts_in_ledger(ctx):
    """Scenario 2: first route fails -> backoff retries -> failover succeeds."""
    for _ in range(3):  # exhaust resp attempts (alias pro: resp -> anth)
        ctx.resp.enqueue({"type": "error", "status": 500, "message": "down"})
    ctx.anth.default_text("from-anthropic")
    r = ctx.chat({})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "from-anthropic"
    assert len(ctx.resp.requests) == 3  # 1 original + 2 retries on the first route
    assert len(ctx.anth.requests) == 1  # fallback hit once
    row = ctx.latest_usage()
    assert row["retries"] == 2          # retry counter
    assert row["failovers"] == 1        # failover counter
    assert row["provider"] == "anth" and row["protocol"] == "anthropic_messages"
    assert row["status"] == "success"


def test_plain_4xx_is_not_retried(ctx):
    ctx.resp.enqueue({"type": "error", "status": 400, "message": "bad-request"})
    ctx.anth.enqueue({"type": "error", "status": 400, "message": "bad-anth"})
    r = ctx.chat({})
    assert r.status_code == 400  # last error passed through
    assert len(ctx.resp.requests) == 1  # NO retry on plain 4xx
    assert len(ctx.anth.requests) == 1  # failover allowed, also no retry there


def test_rate_limit_429_per_key_and_alias(tmp_path):
    """Scenario 10: token bucket per (key, alias); over-limit -> unified 429."""
    cfg = make_config(tmp_path, rate_limit=RateLimitCfg(requests_per_minute=60, burst=2))
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.resp.default_text("x")
        assert ctx.chat({"model": "flash"}).status_code == 200  # bucket: flash key+alias
        assert ctx.chat({"model": "flash"}).status_code == 200
        r = ctx.chat({"model": "flash"})  # 3rd within the same second
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limit_exceeded"
        assert set(r.json()["error"]) == {"message", "type", "param", "code"}
        # other alias unaffected (per-model isolation)
        assert ctx.chat({"model": "pro"}).status_code == 200
        # other caller unaffected (per-key isolation)
        assert (
            ctx.client.post(
                "/v1/chat/completions",
                json={"model": "flash", "messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": "Bearer test-gateway-key"},  # same key, sanity
            ).status_code
            == 429
        )
        other = ctx.client.post(
            "/v1/chat/completions",
            json={"model": "flash", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer another-gateway-key"},
        )
        assert other.status_code in (200, 401)  # second key not in config -> 401
        cfg2_keys = ctx.client.app.state.gateway.config.auth.gateway_keys
        assert cfg2_keys == ["test-gateway-key"]


def test_rate_limit_isolated_per_key(tmp_path):
    from gateway.config import AuthCfg

    cfg = make_config(tmp_path, rate_limit=RateLimitCfg(requests_per_minute=60, burst=2))
    cfg = cfg.model_copy(update={"auth": AuthCfg(gateway_keys=["key-a", "key-b"])})
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.resp.default_text("x")
        ctx.anth.default_text("x")
        for _ in range(2):
            assert ctx.client.post(
                "/v1/chat/completions",
                json={"model": "flash", "messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": "Bearer key-a"},
            ).status_code == 200
        assert ctx.client.post(
            "/v1/chat/completions",
            json={"model": "flash", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer key-a"},
        ).status_code == 429
        # key-b has its own bucket
        assert ctx.client.post(
            "/v1/chat/completions",
            json={"model": "flash", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer key-b"},
        ).status_code == 200


def test_circuit_breaker_opens_and_skips_provider(tmp_path):
    """Scenario 11: threshold consecutive failures -> provider skipped; cooldown recovers."""
    cfg = make_config(tmp_path, circuit_breaker=BreakerCfg(failure_threshold=5, cooldown_seconds=0.2))
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.anth.default_text("fallback-ok")
        # two requests x 3 failing attempts on resp = 6 consecutive failures >= 5
        for _ in range(2):
            for _ in range(3):
                ctx.resp.enqueue({"type": "error", "status": 500, "message": "dead"})
            assert ctx.chat({}).status_code == 200  # served by the anth fallback
        assert ctx.resp.requests and ctx.anth.requests
        resp_calls_before = len(ctx.resp.requests)
        # breaker now open: next request must SKIP resp entirely (0 new resp calls)
        ctx.resp.enqueue({"type": "error", "status": 500, "message": "should-not-be-called"})
        assert ctx.chat({}).status_code == 200
        assert len(ctx.resp.requests) == resp_calls_before  # skipped while open
        row = ctx.latest_usage()
        assert row["provider"] == "anth"
        # after the cooldown elapses the provider is allowed a trial again
        time.sleep(0.25)
        ctx.resp.clear()
        ctx.resp.default_text("resurrected")
        assert ctx.chat({}).status_code == 200
        assert ctx.latest_usage()["provider"] == "resp"


def test_no_healthy_route_503(ctx):
    breaker = ctx.app.state.gateway.breakers.get("resp")
    breaker.state = "open"
    breaker.opened_at = time.monotonic()  # still within cooldown
    ctx.anth.clear()
    for _ in range(3):
        ctx.anth.enqueue({"type": "error", "status": 500, "message": "nope"})
    # both providers unavailable -> pro alias has no healthy route... anth still healthy here,
    # so exhaust differently: block resp via breaker and fail anth
    r = ctx.chat({"model": "pro"})
    # resp skipped (open breaker) -> anth exhausted -> 502; the 503 path needs BOTH blocked:
    breaker2 = ctx.app.state.gateway.breakers.get("anth")
    breaker2.state = "open"
    breaker2.opened_at = time.monotonic()
    r2 = ctx.chat({"model": "pro"})
    assert r2.status_code == 503
    assert r2.json()["error"]["code"] == "no_healthy_route"
    assert r.status_code == 502
