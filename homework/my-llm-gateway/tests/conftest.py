"""Shared fixtures: fast config, app factory with mock upstreams, helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from gateway.config import (
    AliasCfg,
    DatabaseCfg,
    GatewayConfig,
    MaskingCfg,
    MaskRuleCfg,
    PricingCfg,
    ProviderCfg,
    RateLimitCfg,
    RetryCfg,
    RouteCfg,
    ServiceCfg,
)
from gateway.main import create_app
from tests.upstreams.fake import FakeUpstream

GW_KEY = "test-gateway-key"
AUTH = {"Authorization": f"Bearer {GW_KEY}"}


def make_config(tmp_path, **overrides) -> GatewayConfig:
    cfg = GatewayConfig(
        service=ServiceCfg(name="test-gateway"),
        auth={"gateway_keys": [GW_KEY]},
        database=DatabaseCfg(path=str(tmp_path / "gw.db")),
        retry=RetryCfg(max_attempts=3, base_delay_seconds=0.01, max_delay_seconds=0.02),
        rate_limit=RateLimitCfg(requests_per_minute=1_000_000, burst=1_000_000),
        providers={
            "resp": ProviderCfg(
                protocol="openai_responses",
                base_url="https://resp.test/v1",
                api_key="{{SECRET_2fp8bn2g}}",
            ),
            "anth": ProviderCfg(
                protocol="anthropic_messages",
                base_url="https://anth.test",
                api_key="{{SECRET_1fppy8gt}}",
                headers={"anthropic-version": "2023-06-01"},
            ),
        },
        aliases={
            # fallback chain for failover tests: responses first, anthropic second
            "pro": AliasCfg(
                strategy="priority",
                routes=[
                    RouteCfg(provider="resp", model="real-pro"),
                    RouteCfg(provider="anth", model="real-flash"),
                ],
            ),
            "flash": AliasCfg(strategy="weighted", routes=[RouteCfg(provider="anth", model="real-flash")]),
        },
        pricing={"pro": PricingCfg(input_per_million=4.0, output_per_million=16.0, cache_input_per_million=1.0)},
    )
    return cfg.model_copy(update=overrides) if overrides else cfg


def make_masking_config(tmp_path, enabled=True, **overrides) -> GatewayConfig:
    cfg = make_config(tmp_path, **overrides)
    return cfg.model_copy(
        update={
            "masking": MaskingCfg(
                enabled=enabled,
                rules={r: MaskRuleCfg() for r in ("secret_key", "bank_card", "id_card", "phone", "email")},
            )
        }
    )


@dataclass
class TestContext:
    client: TestClient
    app: object
    resp: FakeUpstream
    anth: FakeUpstream
    tmp_path: object
    extra: dict = field(default_factory=dict)

    def chat(self, payload: dict, stream: bool | None = None):
        body = {"model": "pro", "messages": [{"role": "user", "content": "hi"}], **payload}
        if stream is None:
            stream = bool(payload.get("stream", False))
        if stream:
            return self.client.stream("POST", "/v1/chat/completions", json=body, headers=AUTH)
        return self.client.post("/v1/chat/completions", json=body, headers=AUTH)

    def latest_usage(self) -> dict:
        rows = self.client.get("/admin/usage?limit=1", headers=AUTH).json()["records"]
        assert rows, "no usage records"
        return rows[0]


def build_ctx(tmp_path, config=None, resp: FakeUpstream | None = None, anth: FakeUpstream | None = None) -> TestContext:
    resp = resp or FakeUpstream("openai_responses")
    anth = anth or FakeUpstream("anthropic_messages")
    cfg = config or make_config(tmp_path)
    app = create_app(cfg, transports={"resp": resp.transport(), "anth": anth.transport()})
    client = TestClient(app)
    return TestContext(client=client, app=app, resp=resp, anth=anth, tmp_path=tmp_path)


@pytest.fixture
def ctx(tmp_path):
    context = build_ctx(tmp_path)
    with context.client as client:
        context.client = client
        yield context


def parse_sse(text: str) -> list[dict]:
    """Parse `data: {...}` payloads from an SSE byte stream."""
    import json

    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                events.append({"_done": True})
            else:
                events.append(json.loads(payload))
    return events


def now_ms() -> float:
    return time.monotonic()
