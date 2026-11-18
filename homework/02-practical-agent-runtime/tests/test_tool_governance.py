import asyncio
import json
from dataclasses import asdict

import pytest

import tool_governance_demo as demo
from tool_governance_demo import (
    ACCOUNTS,
    ExecutionContext,
    PermissionMode,
    ToolCall,
    ToolResult,
    TransferArgs,
)

TENANT = "tenant_a"
PAYER = "ACC-A-123456"
RECEIVER = "ACC-A-654321"
LOW = "ACC-A-888888"


def balances() -> dict[str, float]:
    return {account: ACCOUNTS[(TENANT, account)] for account in (PAYER, RECEIVER, LOW)}


def action_of(result: ToolResult) -> str:
    if result.ok:
        return "ALLOW"
    return "CONFIRM" if result.error_code == "APPROVAL_REQUIRED" else "DENY"


def approve(approvals, approval_id: str, ctx: ExecutionContext, arguments: dict) -> None:
    approvals.approve(
        approval_id,
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        tool_name="transfer",
        args=TransferArgs.model_validate(arguments),
    )


@pytest.fixture(autouse=True)
def restore_accounts():
    snapshot = dict(ACCOUNTS)
    yield
    ACCOUNTS.clear()
    ACCOUNTS.update(snapshot)


@pytest.fixture
def env():
    runtime, approvals, audit = demo.build_governance()
    ctx = ExecutionContext(
        trace_id="trace_test",
        user_id="u_200",
        tenant_id=TENANT,
        permissions=frozenset({"transfer:execute"}),
        allowed_tools=frozenset({"transfer"}),
    )
    return runtime, approvals, audit, ctx


def test_transfer_parameter_validation(env):
    runtime, _, _, ctx = env
    before = balances()
    invalid_cases = [
        {"from_account": PAYER, "to_account": RECEIVER, "amount": 1000, "approved": True, "user_id": "u_999"},
        {"from_account": "ACC-A-12345", "to_account": RECEIVER, "amount": 1000},
        {"from_account": PAYER, "to_account": RECEIVER, "amount": 0},
        {"from_account": PAYER, "to_account": RECEIVER, "amount": 100001},
        # strict=True：数值字符串不得被隐式转换放行，必须在参数校验阶段拒绝。
        {"from_account": PAYER, "to_account": RECEIVER, "amount": "1000"},
    ]
    for index, arguments in enumerate(invalid_cases, start=1):
        result = asyncio.run(runtime.invoke(ToolCall(f"t1_{index}", "transfer", arguments), ctx))
        assert result.ok is False
        assert result.error_code == "INVALID_ARGUMENT"
    assert balances() == before


def test_transfer_precheck(env):
    runtime, _, _, ctx = env
    before = balances()
    exceeded = asyncio.run(
        runtime.invoke(
            ToolCall(
                "t2_1",
                "transfer",
                {"from_account": PAYER, "to_account": RECEIVER, "amount": 60000},
            ),
            ctx,
        )
    )
    insufficient = asyncio.run(
        runtime.invoke(
            ToolCall(
                "t2_2",
                "transfer",
                {"from_account": LOW, "to_account": RECEIVER, "amount": 30000},
            ),
            ctx,
        )
    )
    foreign = asyncio.run(
        runtime.invoke(
            ToolCall(
                "t2_3",
                "transfer",
                {"from_account": "ACC-B-111111", "to_account": RECEIVER, "amount": 1000},
            ),
            ctx,
        )
    )
    assert exceeded.error_code == "EXCEED_LIMIT"
    assert insufficient.error_code == "INSUFFICIENT_BALANCE"
    assert foreign.error_code == "ACCOUNT_NOT_FOUND"
    assert balances() == before


def test_transfer_permission_and_mode(env):
    runtime, approvals, _, ctx = env
    arguments = {"from_account": PAYER, "to_account": RECEIVER, "amount": 1000}
    approve(approvals, "approval_t3", ctx, arguments)
    no_permission = ExecutionContext(
        **{**asdict(ctx), "permissions": frozenset(), "approval_id": "approval_t3"}
    )
    not_whitelisted = ExecutionContext(
        **{**asdict(ctx), "allowed_tools": frozenset(), "approval_id": "approval_t3"}
    )
    plan_mode = ExecutionContext(
        **{**asdict(ctx), "mode": PermissionMode.PLAN, "approval_id": "approval_t3"}
    )
    assert (
        asyncio.run(runtime.invoke(ToolCall("t3_1", "transfer", arguments), no_permission)).error_code
        == "PERMISSION_DENIED"
    )
    assert (
        asyncio.run(runtime.invoke(ToolCall("t3_2", "transfer", arguments), not_whitelisted)).error_code
        == "TOOL_NOT_ALLOWED"
    )
    assert (
        asyncio.run(runtime.invoke(ToolCall("t3_3", "transfer", arguments), plan_mode)).error_code
        == "PLAN_MODE_DENIED"
    )


def test_transfer_approval_flow(env):
    runtime, approvals, audit, ctx = env
    arguments = {"from_account": PAYER, "to_account": RECEIVER, "amount": 1000}
    pending = asyncio.run(runtime.invoke(ToolCall("t4_1", "transfer", arguments), ctx))
    assert pending.error_code == "APPROVAL_REQUIRED"
    assert action_of(pending) == "CONFIRM"
    # 审计契约：审批请求记为 confirm 三态，而不是 deny。
    pending_audit = next(r for r in audit.records if r["tool_call_id"] == "t4_1")
    assert pending_audit["decision"] == "confirm"
    assert pending_audit["stage"] == "before_tool_call"

    approve(approvals, "approval_t4", ctx, arguments)
    approved_ctx = ExecutionContext(**{**asdict(ctx), "approval_id": "approval_t4"})

    changed = {**arguments, "amount": 2000}
    assert (
        asyncio.run(runtime.invoke(ToolCall("t4_2", "transfer", changed), approved_ctx)).error_code
        == "APPROVAL_REQUIRED"
    )

    success = asyncio.run(runtime.invoke(ToolCall("t4_3", "transfer", arguments), approved_ctx))
    assert success.ok is True
    assert success.content["txn_id"] == ctx.trace_id[-6:]
    assert success.content["status"] == "accepted"
    assert ACCOUNTS[(TENANT, PAYER)] == 99000.0
    assert ACCOUNTS[(TENANT, RECEIVER)] == 6000.0

    payload = json.dumps(success.content, ensure_ascii=False)
    assert "ACC-A-****3456" in payload
    assert PAYER not in payload

    assert (
        asyncio.run(runtime.invoke(ToolCall("t4_4", "transfer", arguments), approved_ctx)).error_code
        == "APPROVAL_REQUIRED"
    )

    print(json.dumps({"redacted_content": success.content}, ensure_ascii=False))
    print(json.dumps({"audit_tail": audit.records[-1]}, ensure_ascii=False, default=str))


def test_transfer_timeout(env):
    runtime, approvals, audit, ctx = env
    arguments = {"from_account": PAYER, "to_account": RECEIVER, "amount": 90000}
    before = balances()
    approve(approvals, "approval_t5", ctx, arguments)
    timeout_ctx = ExecutionContext(**{**asdict(ctx), "approval_id": "approval_t5"})
    result = asyncio.run(runtime.invoke(ToolCall("t5_1", "transfer", arguments), timeout_ctx))
    assert result.error_code == "TIMEOUT_UNKNOWN"
    assert balances() == before
    assert audit.records[-1]["error_code"] == "TIMEOUT_UNKNOWN"
    assert audit.records[-1]["tool_name"] == "transfer"
