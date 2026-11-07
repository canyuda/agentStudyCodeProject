"""cancel_order 作业验收测试：8 条要求逐条对应。

运行：python test_cancel_order.py（也兼容 pytest 收集）
"""

import asyncio
import json

from cancel_order_tool import (
    CANCEL_ORDER,
    AuditLog,
    CancelOrderInput,
    DemoCancelService,
    ExecutionContext,
    ToolError,
    _Call,
    execute_tool_call,
    should_retry,
)


def make_ctx(service: DemoCancelService, *, permissions=None, approvals=None, trace_id="trace_t1"):
    return ExecutionContext(
        user_id="user_demo",
        permissions=frozenset(permissions if permissions is not None else {"order:write"}),
        approvals=frozenset(approvals if approvals is not None else {"ord_1001"}),
        trace_id=trace_id,
        service=service,
    )


def run(coro):
    return asyncio.run(coro)


# ---------- 要求 1：Input Schema 仅 order_id + reason，禁止其他字段 ----------

def test_req1_input_schema():
    CancelOrderInput(order_id="ord_1001", reason="买重复了")

    for forbidden_payload in (
        {"order_id": "ord_1001", "reason": "x", "user_id": "user_demo"},   # 身份
        {"order_id": "ord_1001", "reason": "x", "role": "admin"},          # 角色
        {"order_id": "ord_1001", "reason": "x", "approved": True},         # 审批
        {"order_id": "ord_1001", "reason": "x", "risk": "low"},            # 治理字段
    ):
        try:
            CancelOrderInput.model_validate(forbidden_payload)
        except Exception:
            pass
        else:
            raise AssertionError(f"应拒绝额外字段: {forbidden_payload}")

    schema_keys = set(CancelOrderInput.model_json_schema()["properties"])
    assert schema_keys == {"order_id", "reason"}, schema_keys
    print("✓ 要求1 Input Schema 只有 order_id+reason，身份/审批/任意额外字段均被拒")


# ---------- 要求 2：Output Schema 三字段 ----------

def test_req2_output_schema():
    out = CANCEL_ORDER.output_model.model_validate(
        {"order_id": "ord_1001", "status": "cancelled", "operation_id": "op_abc123"}
    )
    assert set(out.model_dump()) == {"order_id", "status", "operation_id"}
    assert out.status == "cancelled"
    print("✓ 要求2 Output = 订单id + 取消后状态 + 业务操作编号")


# ---------- 要求 3：Error Schema 复用 ToolError ----------

def test_req3_error_schema():
    err = ToolError(code="APPROVAL_REQUIRED", message="需人工审批")
    assert CANCEL_ORDER.error_model is ToolError
    assert err.model_dump()["retryable"] is False
    print("✓ 要求3 错误复用本节 ToolError（code × retryable）")


# ---------- 要求 4：权限 / 风险 / 幂等装配 ----------

def test_req4_governance_flags():
    assert CANCEL_ORDER.permission == "order:write"
    assert CANCEL_ORDER.risk == "high"
    assert CANCEL_ORDER.idempotent is False
    assert CANCEL_ORDER.max_retries == 0
    print("✓ 要求4 order:write / high / 非幂等 / max_retries=0")


# ---------- 要求 5：模型可见 Schema，身份审批不可见 ----------

def test_req5_model_visible():
    tool_json = json.dumps(CANCEL_ORDER.to_model_tool())
    assert set(json.loads(tool_json)["function"]) == {"name", "description", "parameters"}
    for word in ("user_id", "role", "approved", "permission", "risk",
                 "handler", "timeout", "approvals"):
        assert word not in tool_json, f"模型可见 JSON 泄漏: {word}"
    print("✓ 要求5 模型只见公开 Schema；身份/审批/治理字段零泄漏")


# ---------- 要求 6：三类拒绝场景下 handler 调用次数必须为零 ----------

def _reject_case(name, arguments, *, permissions, approvals):
    service = DemoCancelService()
    ctx = make_ctx(service, permissions=permissions, approvals=approvals, trace_id=f"trace_{name}")
    audit = AuditLog()
    result = run(execute_tool_call(_Call(f"call_{name}", "cancel_order", arguments), ctx, audit))
    code = json.loads(result["content"])["code"]
    assert service.calls == 0, f"{name}: handler 被调用了 {service.calls} 次！"
    assert result["tool_call_id"] == f"call_{name}"
    return code


def test_req6_zero_handler_calls_on_rejection():
    assert _reject_case(
        "invalid", json.dumps({"order_id": "x", "reason": "r"}),  # order_id 过短
        permissions={"order:write"}, approvals={"ord_1001"},
    ) == "INVALID_ARGUMENT"
    assert _reject_case(
        "noperm", json.dumps({"order_id": "ord_1001", "reason": "不想要了"}),
        permissions=frozenset(), approvals={"ord_1001"},          # 无 order:write
    ) == "PERMISSION_DENIED"
    assert _reject_case(
        "noapproval", json.dumps({"order_id": "ord_1001", "reason": "不想要了"}),
        permissions={"order:write"}, approvals=frozenset(),       # 无审批记录
    ) == "APPROVAL_REQUIRED"
    print("✓ 要求6 非法参数/无权限/未确认 -> 拒绝码正确，且 handler 真实执行次数 = 0")


# ---------- 要求 7：超时后不重试取消动作 ----------

def test_req7_timeout_no_retry():
    # 幂等守卫：即使 max_retries 被调大，非幂等也拦截
    timeout_error = ToolError(code="TIMEOUT", message="", retryable=True)
    assert should_retry(CANCEL_ORDER, timeout_error) is False

    # 慢服务（0.3s > timeout 0.05s）：真实取消动作只发生 1 次，无重试
    service = DemoCancelService(delay_seconds=0.3)
    ctx = make_ctx(service, trace_id="trace_timeout")
    audit = AuditLog()
    result = run(execute_tool_call(
        _Call("call_timeout", "cancel_order",
              json.dumps({"order_id": "ord_1001", "reason": "不想要了"})),
        ctx, audit,
    ))
    assert json.loads(result["content"])["code"] == "TIMEOUT"
    assert service.calls == 1, f"超时后不应重试，但 handler 调用了 {service.calls} 次"
    assert result["tool_call_id"] == "call_timeout"
    print("✓ 要求7 超时 -> TIMEOUT(结果未知, 禁止自动重试)，handler 仅执行 1 次")


# ---------- 要求 8：成功/拒绝/失败均可按 trace_id+tool_call_id 定位 ----------

def test_req8_audit_traceability():
    audit = AuditLog()

    # 成功路径
    svc_ok = DemoCancelService()
    ctx_ok = make_ctx(svc_ok, trace_id="trace_mix")
    run(execute_tool_call(_Call("call_ok", "cancel_order",
        json.dumps({"order_id": "ord_1001", "reason": "买重复了"})), ctx_ok, audit))

    # 拒绝路径（未审批）
    ctx_deny = make_ctx(DemoCancelService(), approvals=frozenset(), trace_id="trace_mix")
    run(execute_tool_call(_Call("call_deny", "cancel_order",
        json.dumps({"order_id": "ord_1001", "reason": "不想要了"})), ctx_deny, audit))

    # 失败路径（业务状态不允许：paid 订单，审批已通过）
    svc_paid = DemoCancelService()
    ctx_paid = make_ctx(svc_paid, approvals={"ord_1002"}, trace_id="trace_mix")
    run(execute_tool_call(_Call("call_paid", "cancel_order",
        json.dumps({"order_id": "ord_1002", "reason": "不想要了"})), ctx_paid, audit))

    assert len(audit) == 3
    assert audit.find(trace_id="trace_mix", tool_call_id="call_ok")[0].outcome == "SUCCESS"
    assert audit.find(trace_id="trace_mix", tool_call_id="call_deny")[0].outcome == "APPROVAL_REQUIRED"
    assert audit.find(trace_id="trace_mix", tool_call_id="call_paid")[0].outcome == "INVALID_ARGUMENT"
    print("✓ 要求8 成功/拒绝/失败 三条路径均有审计，(trace_id, tool_call_id) 双键可定位")


if __name__ == "__main__":
    for test in (
        test_req1_input_schema, test_req2_output_schema, test_req3_error_schema,
        test_req4_governance_flags, test_req5_model_visible,
        test_req6_zero_handler_calls_on_rejection, test_req7_timeout_no_retry,
        test_req8_audit_traceability,
    ):
        test()
    print("=" * 60)
    print("8 条作业要求全部验收通过")
