"""课堂练习 4.2：模仿瑞幸协议接口，完成 search_orders 最小调用闭环。

四步（课件 2.1 Page 18）：
  1. 定义名称、描述和 Input Schema；
  2. 生成模型可见的 Tool JSON，确认其中没有 handler、权限和内部依赖；
  3. 构造两条不同 tool_call_id 的 Tool Call；
  4. 分别生成 Tool Result，并断言结果 ID 与原调用一致。

每一步都用 assert 程序化验收（fail fast：断言不过立即失败）。
运行：python exercise_4_2.py（依赖 pydantic v2）
"""

import asyncio
import json
from datetime import date

from pydantic import ValidationError

from lk_tool_protocol import (
    SEARCH_ORDERS,
    DemoOrderService,
    ExecutionContext,
    SearchOrdersInput,
    SearchOrdersOutput,
)


def step1_definition() -> None:
    """第 1 步：名称、描述和 Input Schema。"""
    print("=" * 60)
    print("第 1 步 · 工具定义（名称 / 描述 / Input Schema）")
    print("=" * 60)
    assert SEARCH_ORDERS.name == "search_orders"
    assert "不能取消" in SEARCH_ORDERS.description  # 描述写明"不做什么"

    # Input Schema 三层约束：完整性（必填组合）、类型、范围
    # 注意 strict=True 拒绝隐式转换：Python 侧必须传 date 对象，
    # 不能传字符串（模型侧的 JSON 字符串走 model_validate_json，见第 4 步）
    SearchOrdersInput(status="pending", limit=5)  # 合法参数
    SearchOrdersInput(created_from=date.fromisoformat("2026-10-01"))  # 合法：只给日期
    try:
        SearchOrdersInput(status="pending", created_from="2026-10-01")  # 字符串日期
    except ValidationError:
        print("✓ strict 模式拒绝字符串日期（拒绝隐式类型转换）")
    else:
        raise AssertionError("strict 模式下字符串日期应当被拒绝")
    try:
        SearchOrdersInput()  # 非法：过滤条件全空
    except ValidationError:
        print("✓ 空过滤条件被 Input Schema 拒绝")
    else:
        raise AssertionError("空过滤条件应当校验失败")

    try:
        SearchOrdersInput(status="pending", limit=99)  # 非法：超出范围
    except ValidationError:
        print("✓ limit=99 超出 1-50 范围被拒绝")
    else:
        raise AssertionError("limit=99 应当校验失败")
    print("✓ 名称 / 描述 / Input Schema 就绪\n")


def step2_model_visible_json() -> dict:
    """第 2 步：模型可见 Tool JSON，程序化确认无内部依赖泄漏。"""
    print("=" * 60)
    print("第 2 步 · 模型可见 Tool JSON（公开 Schema）")
    print("=" * 60)
    tool_json = SEARCH_ORDERS.to_model_tool()
    print(json.dumps(tool_json, ensure_ascii=False, indent=2))

    # 必须存在的公开三件套
    assert tool_json["type"] == "function"
    assert set(tool_json["function"]) == {"name", "description", "parameters"}

    # 禁止泄漏：handler / 权限 / 风险 / 超时 / 重试 / 幂等 / 内部模型
    dumped = json.dumps(tool_json)
    for word in (
        "handler", "permission", "risk", "timeout",
        "retries", "idempotent", "output_model", "error_model",
    ):
        assert word not in dumped, f"模型可见 JSON 泄漏了内部字段: {word}"
    print("✓ 模型可见 JSON 中没有 handler / 权限 / 内部依赖\n")
    return tool_json


def step3_two_tool_calls() -> list[dict]:
    """第 3 步：构造两条不同 tool_call_id 的 Tool Call（模拟模型输出）。"""
    print("=" * 60)
    print("第 3 步 · 同一工具、两条不同 tool_call_id 的调用")
    print("=" * 60)
    tool_calls = [
        {
            "id": "call_A",
            "function": {
                "name": "search_orders",
                "arguments": json.dumps(
                    {"status": "pending", "limit": 10}
                ),
            },
        },
        {
            "id": "call_B",
            "function": {
                "name": "search_orders",
                "arguments": json.dumps(
                    {"created_from": "2026-10-01", "limit": 10}
                ),
            },
        },
    ]
    assistant_message = {"role": "assistant", "tool_calls": tool_calls}
    print(json.dumps(assistant_message, ensure_ascii=False, indent=2))

    ids = [call["id"] for call in tool_calls]
    assert len(set(ids)) == 2, "两条调用的 tool_call_id 必须不同"
    assert all(
        call["function"]["name"] == "search_orders" for call in tool_calls
    ), "同名工具调用两次，只能靠 id 区分"
    print("✓ 两条调用 id 唯一、工具同名、参数不同\n")
    return tool_calls


async def step4_tool_results(tool_calls: list[dict]) -> list[dict]:
    """第 4 步：执行并生成 Tool Result，断言 ID 配对（故意乱序写回）。"""
    print("=" * 60)
    print("第 4 步 · Tool Result 生成与 ID 配对断言（乱序写回）")
    print("=" * 60)
    ctx = ExecutionContext(
        user_id="user_demo",
        permissions=frozenset({"order:read"}),
        trace_id="trace_lk_001",
        order_service=DemoOrderService(),
    )

    # 逐条执行（实际系统可并行：id 跟随调用对象进入异步任务）
    results: dict[str, dict] = {}
    for call in tool_calls:
        args = SearchOrdersInput.model_validate_json(call["function"]["arguments"])
        output: SearchOrdersOutput = await SEARCH_ORDERS.handler(args, ctx)
        results[call["id"]] = {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": output.model_dump_json(),
        }

    # 故意乱序写回：call_B 先、call_A 后（模拟完成顺序 ≠ 生成顺序）
    messages = [results["call_B"], results["call_A"]]
    for message in messages:
        print(json.dumps(message, ensure_ascii=False))

    # 断言：结果 ID 与原调用一一对应，内容归属正确
    call_by_id = {call["id"]: call for call in tool_calls}
    for message in messages:
        call_id = message["tool_call_id"]
        assert call_id in call_by_id, f"未知的 tool_call_id: {call_id}"

        original_args = json.loads(call_by_id[call_id]["function"]["arguments"])
        content = json.loads(message["content"])
        for order in content["orders"]:
            if "status" in original_args:  # call_A：只应看到 pending
                assert order["status"] == original_args["status"], (
                    f"{call_id} 结果与参数不匹配"
                )
            if "created_from" in original_args:  # call_B：日期下限生效
                assert order["created_at"][:10] >= original_args["created_from"], (
                    f"{call_id} 结果日期越界"
                )
        # 越权隔离：模拟数据里 user_other 的订单绝不能出现
        assert all(
            order["order_id"] != "ord_2001" for order in content["orders"]
        ), "泄漏了其他用户的订单"

    print("✓ 乱序写回后，两条结果仍与原调用正确配对（含内容与越权校验）")


async def main() -> None:
    step1_definition()
    step2_model_visible_json()
    tool_calls = step3_two_tool_calls()
    await step4_tool_results(tool_calls)
    print("=" * 60)
    print("四步全部验收通过：协议闭环成立")


if __name__ == "__main__":
    asyncio.run(main())
