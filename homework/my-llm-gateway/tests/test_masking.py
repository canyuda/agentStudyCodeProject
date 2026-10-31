"""Data masking: five rule types round-trip (12), cross-SSE-chunk placeholder restore (13),
structured output + repair loop only ever sees placeholders (14), plus rule-quality units."""

from __future__ import annotations

import json

from gateway.masking.masker import MaskSession
from gateway.masking.restorer import StreamRestorer
from gateway.masking.rules import id_card_ok, luhn_ok
from tests.conftest import AUTH, build_ctx, make_masking_config, parse_sse

# Valid synthetic samples (checksums are correct so the rules really match)
EMAIL = "someone@example.com"
PHONE = "13812345678"
ID_CARD = "11010519491231002X"  # valid GB11643 checksum
BANK = "6212341234123456780"  # 19 digits, valid Luhn
SECRET = "sk-abcdef1234567890abcdef"

ALL_PII = f"mail {EMAIL} tel {PHONE} id {ID_CARD} card {BANK} key {SECRET}"


def test_rule_checksums_accept_only_valid_values():
    assert id_card_ok(ID_CARD)
    assert not id_card_ok("110105194912310021")  # wrong checksum -> not an ID card
    assert luhn_ok(BANK)
    assert not luhn_ok("6212341234123456781")  # Luhn fail -> not a bank card


def test_mask_session_stable_placeholders_and_type_counts():
    session = MaskSession()
    out = session.mask_text(f"a {EMAIL} b {EMAIL} c {PHONE}")
    assert out.count("⟦EMAIL_1⟧") == 2  # same value -> same placeholder, twice
    assert "⟦EMAIL_1⟧" in out and "⟦PHONE_1⟧" in out
    assert session.hits == {"EMAIL": 2, "PHONE": 1}  # counts by type only
    assert session.restore_text(out) == f"a {EMAIL} b {EMAIL} c {PHONE}"


def test_longest_rule_first_prevents_phone_truncating_id_card():
    session = MaskSession()
    out = session.mask_text(ID_CARD)
    assert "IDCARD" in out and "PHONE" not in out  # whole ID matched, not its phone substring


def test_masking_disabled_by_default_leaks_no_placeholders(ctx):
    """Config-level switch defaults to OFF."""
    ctx.resp.default_text("echo")
    assert ctx.chat({"messages": [{"role": "user", "content": ALL_PII}]}).status_code == 200
    assert "⟦" not in ctx.resp.requests[0]["body"]["input"][0]["content"][0]["text"]
    assert ctx.app.state.gateway.config.masking.enabled is False


def _mask_ctx(tmp_path, **cfg_overrides):
    return build_ctx(tmp_path, config=make_masking_config(tmp_path, enabled=True, **cfg_overrides))


def test_five_rule_roundtrip_nonstream(tmp_path):
    """Scenario 12: request masked, response restored, upstream sees placeholders only."""
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        ctx.resp.default_text(f"got: {EMAIL}")  # model echoes a placeholder-free value? no -
        # model output must contain the PLACEHOLDER to test restore: see below
        ctx.resp.default_text("ack")
        r = ctx.chat({"messages": [{"role": "user", "content": ALL_PII}]})
        assert r.status_code == 200
        sent = ctx.resp.requests[0]["body"]["input"][0]["content"][0]["text"]
        for original in (EMAIL, PHONE, ID_CARD, BANK, SECRET):
            assert original not in sent            # masked before upstream
        assert "⟦SECRET_1⟧" in sent and "⟦BANK_1⟧" in sent and "⟦IDCARD_1⟧" in sent
        assert "⟦PHONE_1⟧" in sent and "⟦EMAIL_1⟧" in sent
        # mask hits recorded as type counts, never content
        row = ctx.latest_usage()
        assert row["mask_hits"] == {"SECRET": 1, "BANK": 1, "IDCARD": 1, "PHONE": 1, "EMAIL": 1}


def test_nonstream_response_restore(tmp_path):
    """Response text containing placeholders is restored before reaching the client."""
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        ctx.resp.default_text("user mail is ⟦EMAIL_1⟧ phone ⟦PHONE_1⟧")
        r = ctx.chat({"messages": [{"role": "user", "content": f"{EMAIL} {PHONE}"}]})
        content = r.json()["choices"][0]["message"]["content"]
        assert content == f"user mail is {EMAIL} phone {PHONE}"
        assert "⟦" not in content


def test_template_variable_values_are_masked(tmp_path):
    """Variable values get masked after render; skeleton text stays intact."""
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        ctx.client.post("/v1/prompts", json={"prompt_id": "p", "template": "Contact {{email}} now"},
                        headers=AUTH)
        ctx.resp.default_text("ok")
        r = ctx.chat({"prompt_ref": {"id": "p", "variables": {"email": EMAIL}}})
        assert r.status_code == 200
        instructions = ctx.resp.requests[0]["body"]["instructions"]
        assert EMAIL not in instructions and "⟦EMAIL_1⟧" in instructions
        assert instructions.startswith("Contact")  # skeleton untouched


def test_placeholder_split_across_sse_chunks_still_restored(tmp_path):
    """Scenario 13: ⟦EMAIL_1⟧ sliced across chunks is restored whole via the tail buffer."""
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        # upstream emits the placeholder SPLIT at an arbitrary boundary
        ctx.resp.enqueue({"type": "stream", "deltas": ["reach ", "⟦EMA", "IL_1⟧", " now"],
                          "usage": {"input_tokens": 2, "output_tokens": 4}})
        with ctx.chat({"messages": [{"role": "user", "content": f"mail {EMAIL}"}], "stream": True}) as r:
            assert r.status_code == 200
            text = r.read().decode()
    contents = [e["choices"][0]["delta"]["content"] for e in parse_sse(text)
                if e.get("choices") and e["choices"][0].get("delta", {}).get("content")]
    joined = "".join(contents)
    assert "⟦" not in joined and "EMA" not in joined  # no placeholder fragments leak
    assert joined == f"reach {EMAIL} now"  # restored whole across the chunk boundary


def test_lost_placeholder_is_kept_verbatim_never_resent(tmp_path):
    """Model rewrites/drops a placeholder -> it passes through as-is; no re-send of the original."""
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        ctx.resp.default_text("the address is [EMAIL_1] maybe")
        r = ctx.chat({"messages": [{"role": "user", "content": EMAIL}]})
        assert r.json()["choices"][0]["message"]["content"] == "the address is [EMAIL_1] maybe"
        assert len(ctx.resp.requests) == 1  # no repair/patch round with the raw value


def test_structured_output_string_values_restored_repair_loop_masked(tmp_path):
    """Scenario 14: JSON string values restored; repair rounds only ever see placeholders."""
    schema = {
        "type": "object",
        "properties": {"email": {"type": "string"}, "note": {"type": "string"}},
        "required": ["email", "note"],
    }
    ctx = _mask_ctx(tmp_path)
    with ctx.client:
        # round 1: invalid JSON (repair), round 2: valid JSON echoing placeholders
        ctx.resp.default_text("{oops not json")
        ctx.resp.default_text(json.dumps({"email": "⟦EMAIL_1⟧", "note": "hi"}, ensure_ascii=False))
        r = ctx.chat(
            {
                "messages": [{"role": "user", "content": f"register {EMAIL}"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "r", "schema": schema}},
            }
        )
        assert r.status_code == 200
        parsed = json.loads(r.json()["choices"][0]["message"]["content"])
        assert parsed["email"] == EMAIL  # placeholder inside a JSON string value restored
        # BOTH upstream calls carried placeholders only - never the raw address
        for captured in ctx.resp.requests:
            body = json.dumps(captured["body"], ensure_ascii=False)
            assert EMAIL not in body
            assert "⟦EMAIL_1⟧" in body  # repair round saw the masked model output too


def test_masking_rule_can_be_disabled_individually(tmp_path):
    from gateway.config import MaskingCfg, MaskRuleCfg
    from tests.conftest import make_config

    cfg = make_config(tmp_path).model_copy(
        update={"masking": MaskingCfg(enabled=True, rules={"email": MaskRuleCfg(enabled=False)})}
    )
    ctx = build_ctx(tmp_path, config=cfg)
    with ctx.client:
        ctx.resp.default_text("ok")
        assert ctx.chat({"messages": [{"role": "user", "content": EMAIL}]}).status_code == 200
        sent = ctx.resp.requests[0]["body"]["input"][0]["content"][0]["text"]
        assert EMAIL in sent  # email rule off -> not masked


def test_stream_restorer_tail_buffer_unit():
    session = MaskSession()
    masked = session.mask_text(EMAIL)
    restorer = StreamRestorer(session)
    # feed byte-by-byte: nothing may leak until the placeholder is complete
    pieces = [masked[i:i + 3] for i in range(0, len(masked), 3)]
    emitted = "".join(restorer.feed(p) for p in pieces) + restorer.flush()
    assert "⟦" not in emitted
    assert emitted == EMAIL
