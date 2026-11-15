"""课堂练习 10.1 验收测试:七检查点 + 课件八项测试的核心项。

断言副作用计数,不只断言错误文本(课件 §十二:拒绝路径必须证明 handler 零调用)。

运行:
    python -m pytest test_tool_governance.py -q
"""

from __future__ import annotations

import asyncio
import unittest

from tool_governance_runtime import (
    AuditSink,
    PermissionEngine,
    PolicyDenied,
    StrictArgs,
    ToolCall,
    ToolDefinition,
    ToolPolicy,
    ToolRuntime,
    TransientToolError,
    build_demo_runtime,
    demo_context,
)

REFUND_ARGS = {"order_id": "ord_1001", "amount": 399.0, "reason": "商品存在质量问题"}


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    """课件八项测试在课堂练习范围内的落地(副作用计数断言)。"""

    def setUp(self) -> None:
        self.runtime, self.approvals, self.side_effects = build_demo_runtime()

    # 1. deny-first:bypass 不能覆盖硬拒绝(课件测试一)
    async def test_deny_first_beats_bypass(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_d1", "run_shell", {"command": "ls"}),
            demo_context(mode="bypassPermissions"),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "DENY_RULE")
        self.assertEqual(self.side_effects["shell_executions"], 0)

    # 2. plan:写工具在审批与 allow 以前被拒绝(课件测试二)
    async def test_plan_blocks_write(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_p1", "create_refund", dict(REFUND_ARGS)),
            demo_context(mode="plan"),
        )
        self.assertEqual(result.code, "PLAN_MODE_DENIED")
        self.assertEqual(self.side_effects["refund_executions"], 0)

    # 3. Schema:模型不能伪造身份和确认字段(课件测试三)
    async def test_schema_rejects_forged_identity(self) -> None:
        forged = dict(REFUND_ARGS, user_id="admin", approved=True)
        result = await self.runtime.invoke(
            ToolCall("call_s1", "create_refund", forged),
            demo_context(),
        )
        self.assertEqual(result.code, "INVALID_ARGUMENT")
        self.assertEqual(result.action, "deny")
        # 结构化错误:每项带 path + message,不是异常字符串
        paths = {item["path"] for item in result.content}
        self.assertIn("user_id", paths)
        self.assertIn("approved", paths)
        self.assertEqual(self.side_effects["refund_executions"], 0)

    async def test_unknown_tool_returns_structured_result(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_u1", "no_such_tool", {}),
            demo_context(),
        )
        self.assertEqual(result.code, "TOOL_NOT_FOUND")
        self.assertEqual(result.tool_call_id, "call_u1")

    # 4. RBAC:权限不足时 handler 零调用(课件测试四)
    async def test_rbac_denies_without_permission(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_r1", "create_refund", dict(REFUND_ARGS)),
            demo_context(permissions=frozenset({"order:read"})),
        )
        self.assertEqual(result.code, "PERMISSION_DENIED")
        self.assertEqual(result.content, "缺少业务权限 refund:create")
        self.assertEqual(self.side_effects["refund_executions"], 0)

    # 5. 双重白名单:发现期不可见之外,执行期也要拦(课件测试七)
    async def test_whitelist_enforced_at_execute(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_w1", "create_refund", dict(REFUND_ARGS)),
            demo_context(allowed_tools=frozenset({"get_order"})),
        )
        self.assertEqual(result.code, "TOOL_NOT_ALLOWED")
        self.assertEqual(self.side_effects["refund_executions"], 0)

    # 6. 审批流:confirm -> 服务端签发 -> 原参数重入 -> 一次性消费
    async def test_approval_flow_one_shot_and_param_binding(self) -> None:
        # 6a 未审批:confirm,零调用
        result = await self.runtime.invoke(
            ToolCall("call_a1", "create_refund", dict(REFUND_ARGS)),
            demo_context(),
        )
        self.assertEqual(result.action, "confirm")
        self.assertEqual(result.code, "APPROVAL_REQUIRED")
        self.assertEqual(self.side_effects["refund_executions"], 0)

        # 6b 批准 100 元,却用 399 元重入:参数哈希不匹配,旧审批失效(课件测试五)
        small_args = {"order_id": "ord_1001", "amount": 100.0, "reason": "部分退款协商"}
        approval_id = self.approvals.issue("user_456", "tenant_a", "create_refund", small_args)
        result = await self.runtime.invoke(
            ToolCall("call_a2", "create_refund", dict(REFUND_ARGS)),
            demo_context(approval_id=approval_id),
        )
        self.assertEqual(result.code, "INVALID_APPROVAL")
        self.assertEqual(self.side_effects["refund_executions"], 0)

        # 6c 原参数重入:allow,副作用恰好 1 次
        result = await self.runtime.invoke(
            ToolCall("call_a3", "create_refund", dict(small_args)),
            demo_context(approval_id=approval_id),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.code, "OK")
        self.assertEqual(result.content["idempotency_key"], "call_a3")
        self.assertEqual(self.side_effects["refund_executions"], 1)

        # 6d 同一审批重放:一次性,拒绝,副作用仍为 1(课件测试八)
        result = await self.runtime.invoke(
            ToolCall("call_a4", "create_refund", dict(small_args)),
            demo_context(approval_id=approval_id),
        )
        self.assertEqual(result.code, "INVALID_APPROVAL")
        self.assertEqual(self.side_effects["refund_executions"], 1)

    # 7. 结果边界:模型结果与审计都脱敏,审计保留 tool_call_id(课件测试六)
    async def test_redaction_in_result_and_audit(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_e1", "get_order", {"order_id": "ord_1001"}),
            demo_context(),
        )
        self.assertTrue(result.ok)
        # 模型视图:邮箱与 token 已脱敏(键保留、值替换,同课件 _redact)
        self.assertEqual(result.content["customer_email"], "***@***")
        self.assertEqual(result.content["service_token"], "***")
        # 审计:两阶段记录都在,参数只记键名,敏感值零泄漏
        records = self.runtime.audit.by_trace("trace_demo")
        self.assertEqual([r.phase for r in records], ["decision", "execution"])
        self.assertTrue(all(r.tool_call_id == "call_e1" for r in records))
        self.assertEqual(records[0].argument_keys, ("order_id",))
        serialized = "\n".join(repr(r) for r in records)
        self.assertNotIn("demo-token-123", serialized)
        self.assertNotIn("user01@example.com", serialized)

    # 8. 非幂等写超时:不重试,返回 TIMEOUT_UNKNOWN(handler 恰好调用 1 次)
    async def test_write_timeout_never_retries(self) -> None:
        calls = {"count": 0}

        class SlowWriteArgs(StrictArgs):
            payload: str

        async def slow_handler(tool_call_id: str, arguments: SlowWriteArgs, context) -> dict:  # noqa: ANN001
            calls["count"] += 1
            await asyncio.sleep(0.5)
            return {"done": True}

        policy = ToolPolicy(
            effect="write", risk="high", permission="any:write",
            requires_approval=False, timeout_seconds=0.05, max_retries=3, idempotent=False,
        )
        # 即使策略误配 max_retries=3,幂等守卫也应保证写操作 0 重试
        tool = ToolDefinition(
            name="slow_write", description="慢写工具", parameters_model=SlowWriteArgs,
            policy=policy, handler=slow_handler,
        )
        runtime = ToolRuntime(
            {"slow_write": tool}, PermissionEngine(self.approvals), AuditSink()
        )
        result = await runtime.invoke(
            ToolCall("call_t1", "slow_write", {"payload": "x"}),
            demo_context(permissions=frozenset({"any:write"}), allowed_tools=frozenset({"slow_write"})),
        )
        self.assertEqual(result.code, "TIMEOUT_UNKNOWN")
        self.assertEqual(calls["count"], 1)

    # 9. 只读瞬时失败:按策略有限重试(指数退避)后成功
    async def test_read_transient_error_retries_then_succeeds(self) -> None:
        calls = {"count": 0}

        class FlakyArgs(StrictArgs):
            payload: str

        async def flaky_handler(tool_call_id: str, arguments: FlakyArgs, context) -> dict:  # noqa: ANN001
            calls["count"] += 1
            if calls["count"] < 3:
                raise TransientToolError("downstream jitter")
            return {"ok": True}

        policy = ToolPolicy(
            effect="read", risk="low", permission="any:read",
            requires_approval=False, timeout_seconds=1.0, max_retries=2, idempotent=True,
        )
        tool = ToolDefinition(
            name="flaky_read", description="抖动只读工具", parameters_model=FlakyArgs,
            policy=policy, handler=flaky_handler,
        )
        runtime = ToolRuntime(
            {"flaky_read": tool}, PermissionEngine(self.approvals), AuditSink()
        )
        result = await runtime.invoke(
            ToolCall("call_f1", "flaky_read", {"payload": "x"}),
            demo_context(permissions=frozenset({"any:read"}), allowed_tools=frozenset({"flaky_read"})),
        )
        self.assertTrue(result.ok)
        self.assertEqual(calls["count"], 3)

    # 10. 业务预检查:状态不允许时拒绝发生在 handler 之前
    async def test_precheck_blocks_before_handler(self) -> None:
        result = await self.runtime.invoke(
            ToolCall("call_b1", "create_refund", {"order_id": "ord_9999", "amount": 1.0, "reason": "不存在的订单"}),
            demo_context(),
        )
        self.assertEqual(result.code, "BUSINESS_RULE_DENIED")
        self.assertEqual(result.content, "订单不存在或状态不可退款")
        self.assertEqual(self.side_effects["refund_executions"], 0)

    # 11. 投影边界:to_model_tool 不含 handler/权限/审批(课件 4.4)
    async def test_model_projection_leaks_nothing(self) -> None:
        runtime, _, _ = build_demo_runtime()
        projection = runtime._tools["create_refund"].to_model_tool()["function"]  # noqa: SLF001
        self.assertEqual(set(projection), {"name", "description", "parameters"})
        serialized = repr(projection)
        self.assertNotIn("handler", serialized)
        self.assertNotIn("requires_approval", serialized)


if __name__ == "__main__":
    unittest.main()
