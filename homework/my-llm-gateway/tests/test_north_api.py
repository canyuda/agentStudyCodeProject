"""North API contract: health probes, auth 401, /v1/models, request-id header, OpenAPI (tasks 1.4/2.5)."""

from __future__ import annotations

from tests.conftest import AUTH


def test_healthz_readyz_unauthenticated(ctx):
    for path in ("/healthz", "/readyz"):
        r = ctx.client.get(path)  # no auth header
        assert r.status_code == 200, r.text
        assert r.json()["status"] in ("ok", "ready")


def test_missing_or_wrong_key_returns_401_unified(ctx):
    for headers in ({}, {"Authorization": "Bearer wrong-key"}, {"Authorization": "Basic abc"}):
        r = ctx.client.post(
            "/v1/chat/completions",
            json={"model": "pro", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        assert r.status_code == 401
        body = r.json()
        assert set(body["error"]) == {"message", "type", "param", "code"}
        assert body["error"]["code"] == "invalid_api_key"


def test_models_lists_public_aliases_only(ctx):
    r = ctx.client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert set(ids) == {"pro", "flash"}
    raw = r.text
    assert "real-pro" not in raw and "real-flash" not in raw  # upstream ids never leak


def test_request_id_header_on_responses(ctx):
    ctx.resp.default_text("hello")
    r = ctx.chat({"model": "pro"})
    assert r.status_code == 200
    assert r.headers.get("x-request-id")  # every response carries the gateway request id


def test_openapi_docs_available(ctx):
    assert ctx.client.get("/docs").status_code == 200
    spec = ctx.client.get("/openapi.json").json()
    assert "/v1/chat/completions" in spec["paths"]
    assert "/v1/responses" in spec["paths"]


def test_unknown_alias_404(ctx):
    r = ctx.chat({"model": "no-such-model"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
