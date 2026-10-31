"""Scenario 1 + ledger fields + admin endpoints (tasks 3.1-3.3, 4.2-4.3, 10 partially)."""

from __future__ import annotations

import re

from tests.conftest import AUTH


def test_nonstream_usage_record_fields_complete(ctx):
    """Scenario 1: non-stream call on both protocols + full ledger row."""
    for alias, upstream, protocol, provider, real_model in (
        ("pro", ctx.resp, "openai_responses", "resp", "real-pro"),
        ("flash", ctx.anth, "anthropic_messages", "anth", "real-flash"),
    ):
        upstream.default_text(f"hello-{alias}")
        r = ctx.chat({"model": alias})
        assert r.status_code == 200
        row = ctx.latest_usage()
        assert re.fullmatch(r"[0-9a-f]{16}", row["key_fingerprint"])  # fingerprint, never raw key
        assert row["entrypoint"] == "/v1/chat/completions"
        assert row["protocol"] == protocol
        assert row["provider"] == provider
        assert row["alias"] == alias
        assert row["upstream_model"] == real_model
        assert row["input_tokens"] == 10 and row["output_tokens"] == 5
        assert row["status"] == "success" and row["http_status"] == 200
        assert row["retries"] == 0 and row["failovers"] == 0
        assert row["total_latency_ms"] is not None and row["total_latency_ms"] >= 0
        assert row["ttft_ms"] is None  # non-stream has no TTFT
    # cost computed from the price table for `pro` (input 10*4 + output 5*16 per million)
    pro_row = ctx.client.get("/admin/usage?limit=2", headers=AUTH).json()["records"][1]
    assert abs(pro_row["cost"] - (10 * 4 / 1e6 + 5 * 16 / 1e6)) < 1e-9


def test_ledger_stores_no_message_bodies_or_raw_keys(ctx):
    ctx.resp.default_text("secret answer")
    ctx.chat({"messages": [{"role": "user", "content": "SENSITIVE-BODY-MARKER"}]})
    import sqlite3

    db = ctx.tmp_path / "gw.db"
    raw = sqlite3.connect(db).execute("SELECT * FROM usage_records").fetchall()
    joined = repr(raw)
    assert "SENSITIVE-BODY-MARKER" not in joined
    assert "secret answer" not in joined
    assert "test-gateway-key" not in joined  # no raw key anywhere in the ledger


def test_admin_usage_pagination(ctx):
    ctx.resp.default_text("x")
    for _ in range(3):
        ctx.chat({})
    r = ctx.client.get("/admin/usage?limit=2&offset=0", headers=AUTH).json()
    assert len(r["records"]) == 2
    r2 = ctx.client.get("/admin/usage?limit=2&offset=2", headers=AUTH).json()
    assert len(r2["records"]) == 1
    ids = [rec["id"] for rec in r["records"] + r2["records"]]
    assert len(set(ids)) == 3  # descending, no overlap


def test_admin_routes_shows_table_and_breakers(ctx):
    ctx.resp.default_text("x")
    ctx.chat({})
    r = ctx.client.get("/admin/routes", headers=AUTH).json()
    assert "pro" in r["aliases"] and "flash" in r["aliases"]
    assert r["aliases"]["pro"]["routes"][0]["model"] == "real-pro"  # admin sees real ids
    assert "resp" in r["circuit_breakers"]


def test_invalid_request_body_returns_unified_422(ctx):
    r = ctx.client.post(
        "/v1/chat/completions", json={"messages": []}, headers=AUTH  # model missing
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["param"]  # points at the offending field
