"""练习 9.2 验收测试(课件 2.2)——七条验收逐条落地。

pytest 和 unittest 都能跑:
    python -m pytest test_tool_runtime.py
    python -m unittest test_tool_runtime

按课件要求:失败条件和断言先写清楚,实现只需要满足它们。
"""

from __future__ import annotations

import json
import unittest

from tool_runtime_demo import (
    CONTROL_LOG,
    CreateTicketInput,
    CreateTicketOutput,
    ExecutionContext,
    RetryPolicy,
    SearchOrdersInput,
    SearchOrdersOutput,
    ToolCall,
    ToolDefinition,
    ToolRegistry,
    ToolResultMessage,
    ToolRuntime,
    make_create_ticket_handler,
    make_search_orders_handler,
)

# 全权限上下文用到的两个权限位
PERMS_FULL = frozenset({"ticket:write", "orders:read"})


def build_world() -> tuple[ToolRegistry, ToolSnapshot, ToolRuntime, dict[str, int], list[dict]]:
    """搭一个最小世界:Registry + 冻结快照 + Runtime;probe 统计真实 handler 调用。"""
    audit: list[dict] = []
    probe = {"create_ticket": 0, "search_orders": 0}

    registry = ToolRegistry()
    # 写工具:高风险、需批准、超时 0.15s、重试策略写了 3 次(但写操作绝不自动重试)
    registry.register(
        ToolDefinition(
            name="create_ticket",
            version="1.0.0",
            description="Create a customer service ticket (write, needs approval)",
            input_model=CreateTicketInput,
            output_model=CreateTicketOutput,
            error_codes=("TIMEOUT", "INTERNAL_ERROR"),
            permission="ticket:write",
            risk="high",
            access="write",
            timeout_s=0.15,
            retry=RetryPolicy(max_attempts=3, backoff_s=0.01),
            audit_fields=("title", "priority"),
            dependencies=("ticket-service",),
            handler=make_create_ticket_handler(probe),
        )
    )
    # 读工具:低风险、无需批准、超时 0.1s,读超时可以按策略重试 3 次
    registry.register(
        ToolDefinition(
            name="search_orders",
            version="1.0.0",
            description="Search orders (read-only)",
            input_model=SearchOrdersInput,
            output_model=SearchOrdersOutput,
            error_codes=("TIMEOUT", "INTERNAL_ERROR"),
            permission="orders:read",
            risk="low",
            access="read",
            timeout_s=0.1,
            retry=RetryPolicy(max_attempts=3, backoff_s=0.01),
            audit_fields=("query",),
            dependencies=(),
            handler=make_search_orders_handler(probe),
        )
    )

    snapshot = registry.snapshot(
        {"create_ticket": "1.0.0", "search_orders": "1.0.0"},
        enabled_state={"create_ticket": True, "search_orders": True},
    )
    return registry, snapshot, ToolRuntime(audit), probe, audit


def ticket_call(call_id: str, **overrides) -> ToolCall:
    """快速构造一条 create_ticket 的 ToolCall,字段可覆盖。"""
    args = {"title": "退款失败", "description": "用户已等待两天", "priority": "high"}
    args.update(overrides)
    return ToolCall(id=call_id, name="create_ticket", arguments=args)


async def run_ok(runtime: ToolRuntime, snapshot: ToolSnapshot, call: ToolCall) -> ToolResultMessage:
    """用一个全权限、且批准了当前 call id 的上下文调用(排除权限/批准干扰)。"""
    context = ExecutionContext(
        user_id="u-1",
        tenant_id="t-1",
        permissions=PERMS_FULL,
        approvals=frozenset({call.id}),
    )
    return await runtime.invoke(
        snapshot, call, context,
        enabled_state={"create_ticket": True, "search_orders": True},
        dependencies={"ticket-service": True},
    )


class TestAcceptance(unittest.IsolatedAsyncioTestCase):
    # ---- 验收 1:非法参数不会进入 handler --------------------------------
    async def test_invalid_arguments_never_reach_handler(self) -> None:
        _, snapshot, runtime, probe, _ = build_world()
        bad_calls = [
            ticket_call("c-1a", title=""),  # 空标题
            # 注意不能再用 ticket_call("c-1b"):默认参数里 title 存在,会变成合法调用
            ToolCall(id="c-1b", name="create_ticket", arguments={"description": "缺标题"}),
            ticket_call("c-1c", priority="urgent"),  # 不在枚举里
            ticket_call("c-1d", approved=True),  # 伪造的多余字段
        ]
        for call in bad_calls:
            result = await run_ok(runtime, snapshot, call)
            self.assertEqual(
                result.error,
                {"code": "INVALID_ARGUMENT", "message": "arguments failed schema validation"},
            )
        self.assertEqual(probe["create_ticket"], 0, "拒绝路径必须不进入 handler")

    # ---- 验收 2:权限不足不产生业务副作用 --------------------------------
    async def test_permission_denied_no_side_effect(self) -> None:
        _, snapshot, runtime, probe, audit = build_world()
        context = ExecutionContext(
            user_id="u-1", tenant_id="t-1", permissions=frozenset({"orders:read"}), approvals=frozenset({"c-2"})
        )
        result = await runtime.invoke(
            snapshot, ticket_call("c-2"), context,
            enabled_state={"create_ticket": True}, dependencies={"ticket-service": True},
        )
        self.assertEqual(result.error, {"code": "PERMISSION_DENIED", "message": "permission denied"})
        self.assertEqual(probe["create_ticket"], 0, "权限不足不允许产生业务副作用")
        self.assertEqual(audit[-1]["outcome"], "PERMISSION_DENIED")

    # ---- 验收 3:模型伪造 approved=true 不能绕过批准 ----------------------
    async def test_forged_approval_cannot_bypass(self) -> None:
        _, snapshot, runtime, probe, _ = build_world()
        # 变体 A:参数里塞 approved=true -> 被 schema(extra=forbid)直接挡掉
        forged = ticket_call("c-3a", approved=True)
        result = await run_ok(runtime, snapshot, forged)  # 上下文甚至批准了 c-3a
        self.assertEqual(result.error["code"], "INVALID_ARGUMENT")
        # 变体 B:参数干净但上下文没有批准记录 -> 拒绝
        clean = ticket_call("c-3b")
        context = ExecutionContext(
            user_id="u-1", tenant_id="t-1", permissions=PERMS_FULL, approvals=frozenset()
        )
        result = await runtime.invoke(
            snapshot, clean, context,
            enabled_state={"create_ticket": True}, dependencies={"ticket-service": True},
        )
        self.assertEqual(result.error["code"], "APPROVAL_REQUIRED")
        self.assertEqual(probe["create_ticket"], 0, "批准不能来自模型参数")

    # ---- 验收 4:执行阶段不能读取 Registry 的最新版本 ---------------------
    async def test_invoke_uses_snapshot_not_registry(self) -> None:
        registry, snapshot, runtime, probe, _ = build_world()
        # 快照冻结之后才注册 v1.0.1:它必须对本轮调用完全不可见
        registry.register(
            ToolDefinition(
                name="create_ticket",
                version="1.0.1",
                description="v2 handler that would return a different ticket id",
                input_model=CreateTicketInput,
                output_model=CreateTicketOutput,
                error_codes=(),
                permission="ticket:write",
                risk="high",
                access="write",
                timeout_s=0.15,
                retry=RetryPolicy(max_attempts=1),
                audit_fields=(),
                dependencies=(),
                handler=make_create_ticket_handler(probe),  # 共用 probe:能数出 v1.0.1 是否被误用
            )
        )
        result = await run_ok(runtime, snapshot, ticket_call("c-4"))
        self.assertIsNone(result.error)
        self.assertEqual(result.content, {"status": "created", "ticket_id": "T-1001"})
        self.assertEqual(probe["create_ticket"], 1, "只有 v1.0.0 的 handler 应该被执行")

    # ---- 验收 5:成功和失败都保留原 tool_call_id ---------------------------
    async def test_all_results_keep_tool_call_id(self) -> None:
        _, snapshot, runtime, _, _ = build_world()
        calls = [
            ticket_call("c-5-ok"),  # 成功
            ticket_call("c-5-bad"),  # 缺 description -> INVALID_ARGUMENT
            ticket_call("c-5-slow", title="__slow__"),  # 超时 -> TIMEOUT
            ticket_call("c-5-boom", title="__boom__"),  # 内部异常 -> INTERNAL_ERROR
        ]
        for call in calls:
            result = await run_ok(runtime, snapshot, call)
            self.assertEqual(result.tool_call_id, call.id, call.id)

    # ---- 验收 6:写操作超时且无幂等保证时不自动重试 ------------------------
    async def test_write_timeout_never_auto_retries(self) -> None:
        _, snapshot, runtime, probe, _ = build_world()
        result = await run_ok(runtime, snapshot, ticket_call("c-6w", title="__slow__"))
        self.assertEqual(result.error["code"], "TIMEOUT")
        self.assertEqual(
            probe["create_ticket"], 1,
            "写操作只执行一次,尽管 retry.max_attempts=3",
        )

    # 对照组:读操作超时按策略重试,重试额度用完才返回 TIMEOUT
    async def test_read_timeout_retries_up_to_policy(self) -> None:
        _, snapshot, runtime, probe, _ = build_world()
        call = ToolCall(id="c-6r", name="search_orders", arguments={"query": "__slow__"})
        result = await run_ok(runtime, snapshot, call)
        self.assertEqual(result.error["code"], "TIMEOUT")
        self.assertEqual(probe["search_orders"], 3, "读操作按 RetryPolicy.max_attempts=3 重试")

    # ---- 验收 7:内部异常归一化为稳定错误,返回不含 Python 堆栈 ------------
    async def test_internal_error_normalized_and_logged(self) -> None:
        _, snapshot, runtime, probe, _ = build_world()
        with self.assertLogs(CONTROL_LOG, level="ERROR") as captured:
            result = await run_ok(runtime, snapshot, ticket_call("c-7", title="__boom__"))
        self.assertEqual(result.error["code"], "INTERNAL_ERROR")
        # 返回给模型的信息里没有内网地址、异常类名和堆栈
        self.assertNotIn("10.0.0.7", result.error["message"])
        self.assertNotIn("RuntimeError", result.error["message"])
        self.assertNotIn("Traceback", result.error["message"])
        # 真实原因(含内网地址)留在受控日志里,运维能查到
        self.assertIn("10.0.0.7", "\n".join(captured.output))

    # ---- 审计:重建决策链,只记字段名,不记值 ------------------------------
    async def test_audit_records_names_only(self) -> None:
        _, snapshot, runtime, _, audit = build_world()
        await run_ok(runtime, snapshot, ticket_call("c-a"))
        await run_ok(runtime, snapshot, ticket_call("c-b", title=""))  # 被拒绝
        record = audit[0]
        self.assertEqual(
            {k for k in record},
            {"tool_call_id", "tool", "version", "tenant_id", "user_id", "outcome", "argument_fields"},
        )
        self.assertEqual(record["version"], "1.0.0")
        dumped = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn("退款失败", dumped, "参数值绝不能出现在审计里")
        self.assertNotIn("用户已等待两天", dumped)
        self.assertEqual(audit[1]["outcome"], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
