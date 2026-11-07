"""cancel_order 工具：高风险、非幂等写操作的受控执行。

作业要求（8 条）与实现位置对照：
  1. Input Schema 仅 order_id + reason，禁止其他字段 -> CancelOrderInput(StrictModel)
  2. Output Schema 返回 order_id / 取消后状态 / operation_id -> CancelOrderOutput
  3. 复用本节 ToolError -> 下方 ToolError（与课件 order_schemas.py 同构）
  4. order:write / high / 非幂等 -> CANCEL_ORDER 装配
  5. 模型只见 Schema；身份与审批来自 ExecutionContext -> to_model_tool + ctx
  6. 非法参数 / 无权限 / 未确认 -> handler 调用次数必须为零 -> execute_tool_call
     的校验顺序（全部拒绝在 handler 之前）+ service.calls 计数
  7. 超时后不重试取消动作 -> should_retry 幂等守卫（非幂等一律 False）
  8. 成功 / 拒绝 / 失败均可用 trace_id + tool_call_id 定位 -> AuditLog
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """extra=forbid 禁止额外字段；strict 拒绝隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


# ---------- 1. Input Schema：只有订单号和原因 ----------

class CancelOrderInput(StrictModel):
    order_id: str = Field(min_length=3, description="要取消的订单号，如 ord_1001")
    reason: str = Field(min_length=1, max_length=200, description="取消原因")

    @model_validator(mode="after")
    def no_identity_fields(self) -> "CancelOrderInput":
        # StrictModel 已从结构上禁止 user_id/role/approved 等任何额外字段；
        # 这里再显式断言一次，防御未来有人放宽 model_config。
        for forbidden in ("user_id", "role", "approved", "permission"):
            if forbidden in self.model_fields_set:
                raise ValueError(f"身份/审批字段 {forbidden} 禁止由模型填写")
        return self


# ---------- 2. Output Schema：订单号 + 取消后状态 + 业务操作编号 ----------

class CancelOrderOutput(StrictModel):
    order_id: str
    status: Literal["cancelled"]
    operation_id: str = Field(description="本次取消的业务操作编号，可用于对账/审计")


# ---------- 3. Error Schema：复用本节 ToolError（课件 order_schemas.py） ----------

class ToolError(StrictModel):
    code: Literal[
        "INVALID_ARGUMENT",    # 模型修正参数后可再次调用
        "TOOL_NOT_FOUND",      # 工具不存在
        "PERMISSION_DENIED",   # 停止，提示切换身份
        "APPROVAL_REQUIRED",   # 进入人工确认流程
        "TIMEOUT",             # 非幂等操作超时后绝不原参数重试
        "UPSTREAM_ERROR",      # 仅幂等且 retryable 时有限重试
        "INVALID_OUTPUT",      # 工具违反返回协议，记录工程故障
    ]
    message: str
    retryable: bool = False


Permission = Literal["order:read", "order:write"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    error_model: type[BaseModel]
    permission: Permission
    risk: RiskLevel
    timeout_seconds: float
    max_retries: int
    idempotent: bool
    handler: Callable[[BaseModel, "ExecutionContext"], Awaitable[BaseModel]]

    def to_model_tool(self) -> dict:
        """模型可见的只有公开 Schema——身份、审批、治理配置一概不出现在这里。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


# ---------- 5. 可信上下文：身份与审批由 Harness 注入 ----------

@dataclass(frozen=True)
class ExecutionContext:
    user_id: str                                    # 来自登录态
    permissions: frozenset[str]                     # 来自鉴权系统
    approvals: frozenset[str]                       # 已审批的 order_id（人工确认记录）
    trace_id: str                                   # 全链路追踪 ID
    service: "DemoCancelService"                    # 业务服务


# ---------- 模拟业务服务：真实"取消"动作只会发生在这里 ----------

class DemoCancelService:
    """orders 里的订单才存在；只有 pending 可取消。calls 计数真实执行次数。"""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.orders = {"ord_1001": "pending", "ord_1002": "paid"}
        self.calls = 0
        self.delay_seconds = delay_seconds

    async def cancel(self, *, user_id: str, order_id: str, reason: str) -> dict:
        self.calls += 1  # 每一次真实副作用都在这里计数
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        status = self.orders.get(order_id)
        if status is None:
            raise LookupError(f"订单不存在: {order_id}")
        if status != "pending":
            raise PermissionError(f"订单 {order_id} 状态为 {status}，不允许取消")
        self.orders[order_id] = "cancelled"
        return {
            "order_id": order_id,
            "status": "cancelled",
            "operation_id": f"op_{uuid.uuid4().hex[:12]}",
        }


async def cancel_order(args: CancelOrderInput, ctx: ExecutionContext) -> CancelOrderOutput:
    """handler：身份取自 ctx（模型填不了），只消费业务参数。"""
    result = await ctx.service.cancel(
        user_id=ctx.user_id,
        order_id=args.order_id,
        reason=args.reason,
    )
    return CancelOrderOutput.model_validate(result)


# ---------- 4. 工具装配：order:write / high / 非幂等 / 零重试 ----------

CANCEL_ORDER = ToolDefinition(
    name="cancel_order",
    description=(
        "取消当前用户指定的 pending 状态订单。"
        "高风险写操作、不可恢复：需要 order:write 权限并完成人工审批；"
        "不能取消已支付(paid)或已发货(shipped)的订单。"
    ),
    input_model=CancelOrderInput,
    output_model=CancelOrderOutput,
    error_model=ToolError,
    permission="order:write",
    risk="high",
    timeout_seconds=0.05,   # 演示用小超时（测试注入慢服务触发 TIMEOUT）
    max_retries=0,
    idempotent=False,
    handler=cancel_order,
)


# ---------- 8. 审计：每条记录都能按 trace_id + tool_call_id 定位 ----------

Outcome = Literal[
    "SUCCESS", "TOOL_NOT_FOUND", "INVALID_ARGUMENT", "PERMISSION_DENIED",
    "APPROVAL_REQUIRED", "TIMEOUT", "UPSTREAM_ERROR", "INVALID_OUTPUT",
]


@dataclass(frozen=True)
class AuditRecord:
    trace_id: str
    tool_call_id: str
    outcome: Outcome
    detail: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AuditLog:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def record(self, *, trace_id: str, tool_call_id: str, outcome: Outcome, detail: str) -> None:
        self._records.append(
            AuditRecord(trace_id=trace_id, tool_call_id=tool_call_id,
                        outcome=outcome, detail=detail)
        )

    def find(self, *, trace_id: str, tool_call_id: str) -> list[AuditRecord]:
        """按 (trace_id, tool_call_id) 双键定位——成功/拒绝/失败一视同仁。"""
        return [
            r for r in self._records
            if r.trace_id == trace_id and r.tool_call_id == tool_call_id
        ]

    def __len__(self) -> int:
        return len(self._records)


# ---------- 7. 幂等守卫：非幂等操作永不重试 ----------

def should_retry(tool: ToolDefinition, error: ToolError) -> bool:
    """超时只说明调用方没拿到结果，不代表业务没发生——非幂等一律不重试。

    即使有人把 max_retries 改成 2，这里也会拦下非幂等操作的重试。
    """
    if not tool.idempotent:
        return False
    return (
        tool.max_retries > 0
        and error.retryable
        and error.code in {"TIMEOUT", "UPSTREAM_ERROR"}
    )


# ---------- 受控执行器：拒绝全部发生在 handler 之前 ----------

class _Call:
    """模拟模型输出的 tool_call（离线测试用）。"""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = type("F", (), {"name": name, "arguments": arguments})()


def _tool_result(call_id: str, payload: BaseModel) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,  # 无论成功失败，原始 tool_call_id 必须保留
        "content": payload.model_dump_json(),
    }


async def execute_tool_call(
    call: _Call, ctx: ExecutionContext, audit: AuditLog
) -> dict:
    """治理顺序（课件 1.5 最小执行顺序）：

    名称 -> Input Schema -> 权限 -> 审批 -> 执行(超时) -> Output 验收 -> Tool Result。
    前四步任何一步拒绝，handler 的真实执行次数都是零。
    """
    tool = CANCEL_ORDER

    def deny(code: str, message: str) -> dict:
        error = ToolError(code=code, message=message)  # type: ignore[arg-type]
        audit.record(trace_id=ctx.trace_id, tool_call_id=call.id,
                     outcome=code, detail=message)
        return _tool_result(call.id, error)

    # 1) 工具名称
    if call.function.name != tool.name:
        return deny("TOOL_NOT_FOUND", f"未知工具: {call.function.name}")

    # 2) Input Schema：非法参数在 handler 之前拒绝
    try:
        args = tool.input_model.model_validate_json(call.function.arguments)
    except Exception as exc:
        return deny("INVALID_ARGUMENT", f"参数校验失败: {str(exc)[:160]}")

    # 3) 权限：来自可信上下文，模型填不了也改不了
    if tool.permission not in ctx.permissions:
        return deny("PERMISSION_DENIED", f"缺少 {tool.permission}，请切换身份")

    # 4) 审批：高风险操作必须有该订单的人工确认记录
    if tool.risk == "high" and args.order_id not in ctx.approvals:
        return deny("APPROVAL_REQUIRED", f"订单 {args.order_id} 需人工审批后才能取消")

    # 5) 执行：超时受控；非幂等守卫保证超时后绝不原参数重试
    try:
        raw = await asyncio.wait_for(
            tool.handler(args, ctx), timeout=tool.timeout_seconds
        )
    except asyncio.TimeoutError:
        if should_retry(tool, ToolError(code="TIMEOUT", message="")):
            pass  # CANCEL_ORDER 永远走不到这里（非幂等）
        return deny("TIMEOUT", f"取消 {args.order_id} 超时：结果未知，禁止自动重试")
    except (LookupError, PermissionError) as exc:
        return deny("INVALID_ARGUMENT", str(exc))

    # 6) Output 验收：handler 必须兑现自己的返回协议
    try:
        output = tool.output_model.model_validate(raw)
    except Exception as exc:
        return deny("INVALID_OUTPUT", f"工具返回违反协议: {str(exc)[:160]}")

    # 7) 成功也入审计（要求 8：成功同样可定位）
    audit.record(
        trace_id=ctx.trace_id, tool_call_id=call.id, outcome="SUCCESS",
        detail=f"订单 {output.order_id} 已取消，operation_id={output.operation_id}",
    )
    return _tool_result(call.id, output)
