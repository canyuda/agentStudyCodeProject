"""订单查询 MCP Server（课件 2.3 §10.1 课堂练习：扩展 get_logistics）。

在 02-3-mcp-orders 基础上新增物流查询 Tool：三个 Tool（订单查询 + 售后
工单 + 物流轨迹）、一个只读 Resource、一个 Prompt，stdio 与 Streamable
HTTP 共用同一套业务声明。物流契约要点：只有 shipped 订单有物流；找不到
物流返回稳定业务错误 LOGISTICS_NOT_FOUND（不抛未处理异常）；Host 侧对超
大结果归一化为 RESULT_TOO_LARGE（由 ord_1003 的超长轨迹触发）。

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
    # Shipped order whose tracking payload exceeds the Host's 2 KB limit,
    # used to exercise RESULT_TOO_LARGE end to end.
    "ord_1003": {"order_id": "ord_1003", "status": "shipped", "amount": 1299},
}


def _oversized_logistics(order_id: str) -> dict[str, object]:
    """Build a >2 KB tracking payload to exercise the Host-side limit."""
    return {
        "order_id": order_id,
        "carrier": "Demo-Logistics",
        "tracking_no": "LG" + "0" * 18,
        "events": [
            {
                "seq": i,
                "time": f"2026-11-{(i % 28) + 1:02d} 00:{i % 60:02d}",
                "station": f"中转站-{i:03d}",
                "status": "运输中",
                "operator": "operator_001",
                "remark": "例行扫描记录，无异常。" * 4,
            }
            for i in range(60)
        ],
    }


# Only shipped orders carry tracking info; pending ones legitimately have none.
LOGISTICS = {
    "ord_1001": {
        "order_id": "ord_1001",
        "carrier": "SF Express",
        "tracking_no": "SF1234567890",
        "events": [
            {"time": "2026-11-05 09:00", "status": "揽收"},
            {"time": "2026-11-06 14:30", "status": "到达武汉转运中心"},
            {"time": "2026-11-07 08:10", "status": "派送中"},
            {"time": "2026-11-07 12:00", "status": "已签收"},
        ],
    },
    "ord_1003": _oversized_logistics("ord_1003"),
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


@mcp.tool(structured_output=True)
def get_logistics(order_id: str) -> dict[str, object]:
    """查询订单的物流轨迹。仅已发货（status=shipped）的订单才有物流；
    请先调用 get_order 确认订单状态，pending 订单不会有物流。"""
    log.info("tool call: get_logistics(order_id=%r)", order_id)
    order = ORDERS.get(order_id)
    if not order:
        log.warning("get_logistics -> ORDER_NOT_FOUND (order_id=%r)", order_id)
        # Stable business error, NOT an unhandled exception.
        raise ToolError("ORDER_NOT_FOUND")
    if order_id not in LOGISTICS:
        log.warning(
            "get_logistics -> LOGISTICS_NOT_FOUND (order_id=%r, status=%r)",
            order_id,
            order["status"],
        )
        # Pending (not-yet-shipped) orders have no tracking info: a stable
        # business error the model can act on, distinct from a network fault.
        raise ToolError("LOGISTICS_NOT_FOUND")
    return {"ok": True, **LOGISTICS[order_id]}


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
