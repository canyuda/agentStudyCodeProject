"""Prompt templates: scenario 5 (versions, activation, missing var 422, injection sandbox)
+ request-side reference injection and ledger recording."""

from __future__ import annotations

from tests.conftest import AUTH


def _create(ctx, template, pid="classifier"):
    return ctx.client.post("/v1/prompts", json={"prompt_id": pid, "template": template}, headers=AUTH)


def test_version_lifecycle_activation_and_lookup(ctx):
    """Scenario 5: re-create => new version; activate; default vs explicit version; 404s."""
    v1 = _create(ctx, "Classify into {{category_set}}").json()
    assert v1["version"] == 1 and v1["is_active"] is True  # first version auto-active
    v2 = _create(ctx, "Sort into [{{category_set}}] be strict").json()
    assert v2["version"] == 2 and v2["is_active"] is False

    got = ctx.client.get("/v1/prompts/classifier", headers=AUTH).json()
    assert got["version"] == 1  # default = active version

    ctx.client.post("/v1/prompts/classifier/activate", json={"version": 2}, headers=AUTH)
    got = ctx.client.get("/v1/prompts/classifier", headers=AUTH).json()
    assert got["version"] == 2 and "be strict" in got["template"]
    explicit = ctx.client.get("/v1/prompts/classifier?version=1", headers=AUTH).json()
    assert explicit["version"] == 1

    assert ctx.client.get("/v1/prompts/ghost", headers=AUTH).status_code == 404
    assert ctx.client.get("/v1/prompts/classifier?version=9", headers=AUTH).status_code == 404
    listing = ctx.client.get("/v1/prompts", headers=AUTH).json()["data"]
    assert listing[0]["active_version"] == 2


def test_missing_variable_returns_422_never_empty(ctx):
    """Scenario 5: strict undefined-variable check."""
    _create(ctx, "Hello {{name}}, you are {{age}} years old")
    r = ctx.client.post("/v1/prompts/classifier/render", json={"variables": {"name": "Ada"}}, headers=AUTH)
    assert r.status_code == 422
    assert "age" in r.json()["error"]["message"]  # names the missing variable
    ok = ctx.client.post(
        "/v1/prompts/classifier/render", json={"variables": {"name": "Ada", "age": "36"}}, headers=AUTH
    )
    assert ok.status_code == 200
    assert ok.json()["rendered"] == "Hello Ada, you are 36 years old"


def test_template_injection_blocked_by_sandbox(ctx):
    """Scenario 5: sandbox escapes are rejected, not executed."""
    malicious = "{{ ''.__class__.__mro__[1].__subclasses__() }}"
    _create(ctx, malicious)
    r = ctx.client.post("/v1/prompts/classifier/render", json={"variables": {}}, headers=AUTH)
    assert r.status_code == 422
    assert "sandbox" in r.json()["error"]["message"].lower()


def test_prompt_reference_injection_front_and_ledger(ctx):
    """Reference call: rendered prompt injected as a system message; ledger records id+version."""
    _create(ctx, "You classify tickets into {{sets}}. Be terse.")
    ctx.resp.default_text("billing")
    r = ctx.chat(
        {
            "messages": [{"role": "user", "content": "double charge"}],
            "prompt_ref": {"id": "classifier", "variables": {"sets": "billing|support"}},
        }
    )
    assert r.status_code == 200
    upstream = ctx.resp.requests[0]["body"]
    assert upstream["instructions"].startswith("You classify tickets into billing|support.")
    row = ctx.latest_usage()
    assert row["prompt_id"] == "classifier" and row["prompt_version"] == 1


def test_prompt_reference_suffix_position_and_version_pin(ctx):
    _create(ctx, "v1 text")
    _create(ctx, "FINAL RULE: answer in {{lang}}")
    ctx.resp.default_text("ok")
    r = ctx.chat(
        {
            "prompt_ref": {"id": "classifier", "version": 2, "variables": {"lang": "English"},
                           "position": "suffix"},
        }
    )
    assert r.status_code == 200
    body = ctx.resp.requests[0]["body"]
    # suffix: appended at the END of the message list (Responses `input`)
    assert body["input"][-1]["role"] == "user"
    assert "FINAL RULE: answer in English" in body["input"][-1]["content"][0]["text"]
    assert ctx.latest_usage()["prompt_version"] == 2


def test_prompt_reference_unknown_returns_404(ctx):
    r = ctx.chat({"prompt_ref": {"id": "ghost", "variables": {}}})
    assert r.status_code == 404
    assert ctx.latest_usage()["status"] == "failed"  # failure also recorded


def test_anthropic_reference_uses_top_level_system(ctx):
    _create(ctx, "sys via ref {{x}}")
    ctx.anth.default_text("ok")
    r = ctx.chat(
        {
            "model": "flash",
            "prompt_ref": {"id": "classifier", "variables": {"x": "1"}},
        }
    )
    assert r.status_code == 200
    assert ctx.anth.requests[0]["body"]["system"] == "sys via ref 1"  # translated to top-level system
