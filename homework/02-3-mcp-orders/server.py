"""订单查询 MCP Server（课件 2.3 §7.2 复刻）。

把内部订单能力收敛成任务级契约：两个 Tool（查询 + 售后工单）、一个只读
Resource（状态说明）、一个 Prompt（任务模板），stdio 与 Streamable HTTP
共用同一套业务声明。

依赖（锁定版本，见课件 §7.1）：
    Python 3.12 / mcp==2.0.0

运行方式：
    python server.py            # stdio 传输（由 host.py 作为子进程启动）
    python server.py --http     # Streamable HTTP，监听 http://127.0.0.1:8000
"""

from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

# In stdio mode stdout carries the JSON-RPC protocol, so server-side logs
# MUST go to stderr or they would corrupt the message stream.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("order-service")

mcp = MCPServer(
    "order-service",
    instructions="查询订单必须先调用 get_order，不要猜测订单状态。",
)

# Demo data only; a real server would call the order repository here.
ORDERS = {
    "ord_1001": {"order_id": "ord_1001", "status": "shipped", "amount": 199},
    "ord_1002": {"order_id": "ord_1002", "status": "pending", "amount": 88},
}


@mcp.tool(structured_output=True)
def get_order(order_id: str) -> dict[str, object]:
    """按订单号查询订单状态。找不到订单时返回工具错误。"""
    log.info("tool call: get_order(order_id=%r)", order_id)
    order = ORDERS.get(order_id)
    if not order:
        log.warning("get_order -> ORDER_NOT_FOUND (order_id=%r)", order_id)
        # Stable failure semantics: the model can retry with another
        # order id, the Runtime must NOT treat this as a network error.
        raise ToolError("ORDER_NOT_FOUND")
    return {"ok": True, **order}


@mcp.tool(structured_output=True)
def create_ticket(order_id: str, reason: str) -> dict[str, object]:
    """为已存在的订单创建售后工单。"""
    log.info("tool call: create_ticket(order_id=%r, reason=%r)", order_id, reason)
    if order_id not in ORDERS:
        log.warning("create_ticket -> ORDER_NOT_FOUND (order_id=%r)", order_id)
        raise ToolError("ORDER_NOT_FOUND")
    if not 4 <= len(reason) <= 100:
        log.warning("create_ticket -> INVALID_REASON (len(reason)=%d)", len(reason))
        raise ToolError("INVALID_REASON")
    # Demo returns a fixed id; a production version must delegate
    # idempotency keys and the business transaction to the order system.
    return {"ok": True, "ticket_id": "ticket_2001", "order_id": order_id}


@mcp.resource("policy://order-status")
def order_status_policy() -> str:
    """订单状态说明，只提供只读上下文。"""
    log.info("resource read: policy://order-status")
    return "pending=待处理；shipped=已发货；cancelled=已取消"


@mcp.prompt()
def order_assistant(order_id: str) -> str:
    """生成订单查询任务模板。"""
    log.info("prompt call: order_assistant(order_id=%r)", order_id)
    return f"查询订单 {order_id}。必须使用工具获取真实状态，不要猜测。"


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.run("streamable-http", host="127.0.0.1", port=8000)
    else:
        mcp.run("stdio")
