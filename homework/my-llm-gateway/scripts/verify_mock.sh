#!/usr/bin/env bash
# One-shot Mock verification: full pytest suite (20 required scenarios) + ruff.
# Uses in-memory fake upstreams only - never consumes real quota.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".venv/bin/python"
[ -x "$VENV" ] || { echo "ERROR: .venv not found. Run: uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'"; exit 1; }

echo "=== [1/3] Static checks (ruff) ==="
"$VENV" -m ruff check src tests
echo "ruff: PASS"

echo ""
echo "=== [2/3] Mock test suite (pytest) ==="
"$VENV" -m pytest tests/ -q
echo ""

echo "=== [3/3] Scenario coverage map (12.1 required scenarios -> tests) ==="
"$VENV" - <<'PY'
import subprocess, re

SCENARIOS = {
    1:  ("非流式正常调用+用量落账", "tests/test_ledger.py::test_nonstream_usage_record_fields_complete + tests/test_adapters.py::test_both_protocols_basic_nonstream"),
    2:  ("重试→切换后备成功，计数入账", "tests/test_resilience.py::test_retry_then_failover_success_counts_in_ledger"),
    3:  ("结构化输出修复成功", "tests/test_structured.py::test_repair_loop_succeeds"),
    4:  ("结构化输出修复超限→422", "tests/test_structured.py::test_repair_rounds_exhausted_returns_422"),
    5:  ("提示词多版本/激活/按版本/缺变量422/注入拦截", "tests/test_prompts.py::test_version_lifecycle_activation_and_lookup + test_missing_variable_returns_422_never_empty + test_template_injection_blocked_by_sandbox"),
    6:  ("SSE逐块+流中usage+TTFT", "tests/test_streaming.py::test_sse_chunk_by_chunk_headers_ttft_usage"),
    7:  ("流中中断→SSE错误事件不换模型", "tests/test_streaming.py::test_post_first_byte_abort_sends_sse_error_not_model_switch"),
    8:  ("客户端断开→取消499/上游关闭", "tests/test_streaming.py::test_client_disconnect_cancels_and_closes_upstream"),
    9:  ("Responses入口+鉴权401", "tests/test_responses_entry.py::test_responses_entry_nonstream_via_chat_pipeline + tests/test_north_api.py::test_missing_or_wrong_key_returns_401_unified"),
    10: ("限流429统一错误", "tests/test_resilience.py::test_rate_limit_429_per_key_and_alias"),
    11: ("熔断打开跳过/冷却恢复", "tests/test_resilience.py::test_circuit_breaker_opens_and_skips_provider"),
    12: ("脱敏五类往返", "tests/test_masking.py::test_five_rule_roundtrip_nonstream + test_nonstream_response_restore"),
    13: ("脱敏+流式跨块占位符还原", "tests/test_masking.py::test_placeholder_split_across_sse_chunks_still_restored"),
    14: ("脱敏+结构化：JSON值还原/修复回路只见占位符", "tests/test_masking.py::test_structured_output_string_values_restored_repair_loop_masked"),
    15: ("未知供应商/协议→启动失败", "tests/test_config.py::test_unknown_provider_reference_fails_startup + test_unknown_protocol_type_fails_startup"),
    16: ("Responses协议正向改写/反向映射", "tests/test_adapters.py::test_responses_adapter_request_rewrite"),
    17: ("Anthropic协议：顶层system/max_tokens兜底/停止原因", "tests/test_adapters.py::test_anthropic_adapter_request_rewrite + test_anthropic_max_tokens_default_and_length_mapping"),
    18: ("两种上游流式事件统一转换", "tests/test_streaming.py::test_sse_chunk_by_chunk_headers_ttft_usage + test_anthropic_stream_normalized_to_north_chunks"),
    19: ("两种协议错误体归一", "tests/test_adapters.py::test_upstream_error_normalization_5xx_to_502 + test_upstream_error_normalization_anthropic_4xx_passthrough"),
    20: ("无原生约束协议经指令注入+闭环返回合法JSON", "tests/test_structured.py::test_anthropic_instruction_injection_and_repair_loop"),
}

collected = subprocess.run(
    [".venv/bin/python", "-m", "pytest", "--collect-only", "-q", "tests/"],
    capture_output=True, text=True, check=True).stdout

ok = True
for num, (desc, tests) in SCENARIOS.items():
    missing = [t for t in tests.split(" + ") if f"::" in t and t not in collected]
    status = "PASS" if not missing else "MISSING"
    ok = ok and not missing
    print(f"  scenario {num:2d} [{status}] {desc}")
    for t in missing:
        print(f"           -> {t}")
raise SystemExit(0 if ok else 1)
PY

echo ""
echo "ALL MOCK CHECKS PASSED"
