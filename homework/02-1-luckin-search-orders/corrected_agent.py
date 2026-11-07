"""课堂练习 4.3 参考修复版：对照 ai_generated_agent.py 的协议修复。

修复清单（对应四类协议错误）：
  ① 身份/权限从可信上下文 ExecutionContext 注入 —— Input Schema 里
     没有 user_id / role / approved，模型填不了；
  ② 发给模型的只有公开 Schema（to_model_tool()）—— handler、permission、
     risk、timeout 不出现在模型可见 JSON；
  ③ 参数经 Pydantic StrictModel 校验（model_validate_json）—— 非法参数
     构造结构化 INVALID_ARGUMENT 错误，绝不展开调用 handler；
  ④ Tool Result 一律携带原始 tool_call_id —— 成功、失败路径都带。

额外加固：权限不足在 handler 之前拒绝；MAX_STEPS 防循环失控。

运行：
  python corrected_agent.py           # 离线自检（不连模型，验证三条路径）
  DEEPSEEK_API_KEY=... python corrected_agent.py --live   # 真实最小 Loop
"""

import asyncio
import json
import os
import sys
from typing import Any

from pydantic import ValidationError

from lk_tool_protocol import (
    SEARCH_ORDERS,
    DemoOrderService,
    ExecutionContext,
    SearchOrdersInput,
    ToolError,
)

MAX_STEPS = 4


def tool_result(call_id: str, payload: dict | ToolError) -> dict:
    """构造 Tool Result：无论成功失败，必须携带原始 tool_call_id。"""
    if isinstance(payload, ToolError):
        content = payload.model_dump_json()
    else:
        content = json.dumps(payload, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": call_id, "content": content}


async def execute_tool_call(call: Any, ctx: ExecutionContext) -> dict:
    """单次工具调用的受控执行：权限 -> 参数 -> handler -> 输出验收。"""
    call_id = call.id  # id 跟随调用对象，不靠顺序猜

    # ① 权限来自可信上下文，在 handler 之前拒绝
    if SEARCH_ORDERS.permission not in ctx.permissions:
        return tool_result(
            call_id,
            ToolError(
                code="PERMISSION_DENIED",
                message=f"缺少 {SEARCH_ORDERS.permission}，请切换身份后重试",
            ),
        )

    # ③ 参数必须过 Input Schema，非法直接返回结构化错误
    try:
        args = SearchOrdersInput.model_validate_json(call.function.arguments)
    except ValidationError as exc:
        return tool_result(
            call_id,
            ToolError(
                code="INVALID_ARGUMENT",
                message=f"参数校验失败: {str(exc)[:200]}",
            ),
        )

    output = await SEARCH_ORDERS.handler(args, ctx)
    validated = SEARCH_ORDERS.output_model.model_validate(output)  # Output 验收
    return tool_result(call_id, json.loads(validated.model_dump_json()))


async def run_agent(question: str, ctx: ExecutionContext) -> str:
    """真实最小 Agent Loop（需要 DEEPSEEK_API_KEY）。"""
    from openai import OpenAI  # 仅 live 路径需要 SDK

    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
    messages: list[dict] = [{"role": "user", "content": question}]
    for _step in range(MAX_STEPS):  # 防失控：最多 MAX_STEPS 轮
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            tools=[SEARCH_ORDERS.to_model_tool()],  # ② 只发公开 Schema
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return msg.content
        for call in msg.tool_calls:
            messages.append(await execute_tool_call(call, ctx))  # ④ 带原 id 写回
    return "已达最大步数，停止执行"


class _FakeCall:
    """离线自检用的最小 call 桩（模拟模型输出的 tool_call）。"""

    def __init__(self, call_id: str, arguments: str) -> None:
        self.id = call_id
        self.function = type("F", (), {"arguments": arguments})()


async def offline_self_check() -> None:
    """不连模型，验证三条路径都守住协议（即 4.3 要求的'相应测试'）。"""
    # 模型可见 JSON 零泄漏（对应错误②）
    dumped = json.dumps(SEARCH_ORDERS.to_model_tool())
    for word in ("handler", "permission", "risk", "timeout"):
        assert word not in dumped
    print("✓ 模型可见 JSON 无 handler / 权限 / 内部依赖")

    # Input Schema 无身份字段（对应错误①）
    schema = json.dumps(SEARCH_ORDERS.input_model.model_json_schema())
    for word in ("user_id", "role", "approved"):
        assert word not in schema
    print("✓ Input Schema 中没有 user_id / role / approved")

    ctx = ExecutionContext(
        user_id="user_demo",
        permissions=frozenset({"order:read"}),
        trace_id="trace_fix_001",
        order_service=DemoOrderService(),
    )

    # 路径 1：合法调用 -> 成功结果带原 id
    ok = await execute_tool_call(
        _FakeCall("call_ok", json.dumps({"status": "pending"})), ctx
    )
    assert ok["tool_call_id"] == "call_ok"
    print("✓ 合法调用成功，结果携带原始 tool_call_id")

    # 路径 2：非法参数（limit 越界）-> INVALID_ARGUMENT，且不进 handler
    bad = await execute_tool_call(
        _FakeCall("call_bad", json.dumps({"status": "pending", "limit": 99})), ctx
    )
    assert bad["tool_call_id"] == "call_bad"
    assert json.loads(bad["content"])["code"] == "INVALID_ARGUMENT"
    print("✓ 非法参数被拒，结构化错误同样携带原始 tool_call_id")

    # 路径 3：无权限上下文 -> PERMISSION_DENIED，发生在 handler 之前
    poor_ctx = ExecutionContext(
        user_id="user_demo",
        permissions=frozenset(),  # 无任何权限
        trace_id="trace_fix_002",
        order_service=DemoOrderService(),
    )
    denied = await execute_tool_call(
        _FakeCall("call_deny", json.dumps({"status": "pending"})), poor_ctx
    )
    assert json.loads(denied["content"])["code"] == "PERMISSION_DENIED"
    print("✓ 权限不足在 handler 之前被拒绝（无法靠重试绕过）")


async def main() -> None:
    if "--live" in sys.argv:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SystemExit("live 模式需要 DEEPSEEK_API_KEY")
        ctx = ExecutionContext(
            user_id="user_demo",
            permissions=frozenset({"order:read"}),
            trace_id="trace_live_001",
            order_service=DemoOrderService(),
        )
        print(await run_agent("查一下我还没有支付的订单", ctx))
    else:
        await offline_self_check()
        print("=" * 60)
        print("离线自检全部通过：四类协议错误均有对应防线与测试")


if __name__ == "__main__":
    asyncio.run(main())
