"""课堂练习 10.1 离线演示:一次调用链路上的七个治理检查点。

场景对齐课件 §二 的六条治理日志,并追加"审批绑定参数被修改"场景:

    call_01  get_order      -> allow,邮箱与 service_token 已脱敏
    call_02  create_refund  -> confirm / APPROVAL_REQUIRED,handler 零调用
    call_03  create_refund  -> 审批重入后 allow,幂等键 = tool_call_id
    call_04  create_refund  -> 伪造 user_id/approved 被 extra="forbid" 拒绝
    call_05  run_shell      -> deny-first:命中 DENY_RULE(bypass 也不能越过)
    call_06  create_refund  -> plan 模式禁止写操作
    call_07  create_refund  -> 批准 100 元改 399 元重入,审批哈希不匹配拒绝

运行(离线,不需要模型 API):
    python -m pip install "pydantic>=2,<3"
    python exercise_10_1.py
"""

from __future__ import annotations

import asyncio
import json

from tool_governance_runtime import (
    ToolCall,
    build_demo_runtime,
    demo_context,
)


def _show(title: str, result) -> None:  # noqa: ANN001 - 演示脚本从简
    payload = json.dumps(
        {
            "tool_call_id": result.tool_call_id,
            "tool_name": result.tool_name,
            "ok": result.ok,
            "action": result.action,
            "code": result.code,
            "content": result.content,
        },
        ensure_ascii=False,
    )
    print(f"{title}\n  {payload}\n")


async def main() -> None:
    runtime, approvals, side_effects = build_demo_runtime()

    # call_01 查询成功:成功也要治理,敏感字段脱敏后才进模型上下文
    result = await runtime.invoke(
        ToolCall("call_01", "get_order", {"order_id": "ord_1001"}),
        demo_context(trace_id="trace_demo"),
    )
    _show("call_01 get_order -> allow(邮箱/token 已脱敏)", result)

    # call_02 高风险资金操作:confirm 暂停,模型只发起申请,钱还没扣
    refund_args = {"order_id": "ord_1001", "amount": 399.0, "reason": "商品存在质量问题"}
    result = await runtime.invoke(
        ToolCall("call_02", "create_refund", dict(refund_args)),
        demo_context(trace_id="trace_demo"),
    )
    _show("call_02 create_refund -> confirm / APPROVAL_REQUIRED", result)

    # call_03 用户确认后服务端签发审批,原参数带 approval_id 重入 -> 执行成功
    approval_id = approvals.issue("user_456", "tenant_a", "create_refund", refund_args)
    result = await runtime.invoke(
        ToolCall("call_03", "create_refund", dict(refund_args)),
        demo_context(trace_id="trace_demo", approval_id=approval_id),
    )
    _show("call_03 create_refund -> allow(审批重入,幂等键=call_03)", result)

    # call_04 伪造身份字段:extra="forbid" 直接拒,handler 零调用
    forged = dict(refund_args, user_id="admin", approved=True)
    result = await runtime.invoke(
        ToolCall("call_04", "create_refund", forged),
        demo_context(trace_id="trace_demo"),
    )
    _show("call_04 create_refund -> deny / INVALID_ARGUMENT(结构化错误)", result)

    # call_05 deny-first:deny 规则优先于一切,任何模式都拦
    result = await runtime.invoke(
        ToolCall("call_05", "run_shell", {"command": "rm -rf /"}),
        demo_context(trace_id="trace_demo", mode="bypassPermissions"),
    )
    _show("call_05 run_shell(bypass) -> deny / DENY_RULE", result)

    # call_06 plan 模式:只读边界由执行层强制
    result = await runtime.invoke(
        ToolCall("call_06", "create_refund", dict(refund_args)),
        demo_context(trace_id="trace_demo", mode="plan"),
    )
    _show("call_06 create_refund(plan) -> deny / PLAN_MODE_DENIED", result)

    # call_07 审批绑定参数:批准 100 元改成 399 元重入,旧审批失效
    small_args = {"order_id": "ord_1001", "amount": 100.0, "reason": "部分退款协商"}
    changed_approval = approvals.issue("user_456", "tenant_a", "create_refund", small_args)
    result = await runtime.invoke(
        ToolCall("call_07", "create_refund", {"order_id": "ord_1001", "amount": 399.0, "reason": "部分退款协商"}),
        demo_context(trace_id="trace_demo", approval_id=changed_approval),
    )
    _show("call_07 create_refund(批 100 改 399) -> deny / INVALID_APPROVAL", result)

    # 对账:副作用计数与审计数量
    decisions = runtime.audit.by_trace("trace_demo")
    print("side_effects:", json.dumps(side_effects))
    print(
        "audit_records:", len(decisions),
        "| decision 阶段:", sum(1 for r in decisions if r.phase == "decision"),
        "| execution 阶段:", sum(1 for r in decisions if r.phase == "execution"),
    )
    print("\n验收要点:退款只执行 1 次(call_03),Shell 为 0 次;")
    print("拒绝的调用均有 decision 审计 -> 拒绝发生在副作用之前。")


if __name__ == "__main__":
    asyncio.run(main())
