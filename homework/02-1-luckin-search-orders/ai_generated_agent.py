"""AI 生成的订单查询 Agent（课堂练习 4.3 待审查代码）。

这段代码"看起来能跑"，但违反了课件 2.1 的多条协议铁律。
请按课件 4.3 要求审查：
  1) 找出协议错误（至少四类）；
  2) 说明每个错误影响的是「模型选择 / 可信边界 / 结果关联」中的哪一个；
  3) 给出能证明该错误的测试思路。

运行只会连 DeepSeek，本练习不需要真的运行——审查为主。
"""

import json
import os

from openai import OpenAI

client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

# 订单模拟数据：user_demo 一条，user_other 两条
ORDERS = [
    {"order_id": "ord_1001", "user_id": "user_demo", "status": "pending", "amount": 29.99},
    {"order_id": "ord_2001", "user_id": "user_other", "status": "paid", "amount": 15.99},
    {"order_id": "ord_2002", "user_id": "user_other", "status": "shipped", "amount": 42.00},
]


def search_orders_handler(user_id: str, role: str, approved: bool,
                          status: str | None = None, limit: int = 10) -> list[dict]:
    """业务函数：admin 可以看所有人的订单，普通用户只看自己的。"""
    # TODO(问题①-a / 可信边界): 身份(user_id/role/approved)成了 handler 的入参——
    # 意味着"我是谁、我有没有权限"由调用参数决定，而不是由登录态(可信上下文)决定。
    # 正确做法: 身份从 ExecutionContext 注入, 模型只能填业务参数(status/created_from/limit)。
    if role == "admin":
        rows = ORDERS  # TODO(问题①-b / 可信边界): 模型自填 role="admin" 即可越权看到全部订单
    else:
        rows = [o for o in ORDERS if o["user_id"] == user_id]  # TODO(问题①-c): user_id 也由模型填写, 可冒充他人
    if status:
        rows = [o for o in rows if o["status"] == status]
    return rows[:limit]


# 发给模型的工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            # TODO(问题⑤ / 模型选择): 描述是万金油"查询订单"——没有说明何时该用、
            # 边界是什么(只读/不修改), 模型在多工具场景下极易选错。同课件反例 query_data。
            "name": "search_orders",
            "description": "查询订单",
            "parameters": {
                "type": "object",
                "properties": {
                    # TODO(问题① / 可信边界): user_id/role/approved 出现在 Input Schema
                    # 且被标为 required——等于把身份、权限、审批交给模型自己填,
                    # 提示注入一句"我是 admin"即可提权。这三项必须移出 Schema,
                    # 由 Harness 从登录态/服务间身份生成(Execution Context)。
                    "user_id": {"type": "string", "description": "要查询的用户ID"},
                    "role": {"type": "string", "description": "用户角色: user/admin"},
                    "approved": {"type": "boolean", "description": "本次查询是否已获审批"},
                    "status": {"type": "string", "description": "订单状态（可选）"},
                    "limit": {"type": "integer", "description": "返回数量（可选）"},
                },
                "required": ["user_id", "role", "approved"],
            },
            # TODO(问题② / 模型选择+安全): 把 handler/permission/risk/timeout_seconds
            # 全部发给模型——内部实现与治理配置泄漏到不可信侧。
            # 正确做法: 只发公开 Schema(to_model_tool()), 治理字段留在 Runtime 侧。
            "handler": search_orders_handler,
            "permission": "order:read",
            "risk": "low",
            "timeout_seconds": 5,
        },
    }
]


def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    # TODO(问题⑥ / 彩蛋): while True 没有最大步数限制(MIN/MAX_STEPS),
    # 模型反复调用工具时 Loop 永不退出。对照 minimal_tool_loop.py 的 MAX_STEPS=4。
    while True:
        resp = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            tools=TOOLS,
        )
        msg = resp.choices[0].message
        messages.append(msg)
        if not msg.tool_calls:
            return msg.content

        for call in msg.tool_calls:
            # TODO(问题③ / 可信边界): json.loads 之后直接 **args 展开,
            # 未经 Pydantic/Input Schema 校验——模型生成的 JSON 属于不可信输入,
            # 缺字段/类型错/越界值(limit=99)会直捣业务函数。
            # 正确做法: SearchOrdersInput.model_validate_json(...) 校验,
            # 失败构造 INVALID_ARGUMENT 结构化错误返回 Agent Loop。
            args = json.loads(call.function.arguments)
            result = search_orders_handler(**args)
            # TODO(问题④ / 结果关联): Tool Result 只有 content, 没有原始 tool_call_id——
            # 并行调用乱序完成时, 模型无法把结果配对回各自的调用;
            # 多轮 Trace 也无法建立父子关系。
            # 正确做法: {"role":"tool", "tool_call_id": call.id, "content": ...}
            messages.append({"role": "tool", "content": json.dumps(result)})


if __name__ == "__main__":
    print(run_agent("帮我查一下我的订单"))
