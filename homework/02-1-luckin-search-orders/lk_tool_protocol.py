"""模仿瑞幸开放平台协议风格，定义 search_orders 工具协议。

对应课件 2.1 课堂练习 4.2 第 1 步：定义名称、描述和 Input Schema。
协议风格参考 https://open.lkcoffee.com/docs ：
接口名稳定唯一；描述说明"做什么 / 何时用 / 不做什么"；
入参有类型与范围约束；错误码可驱动恢复。
数据全部为模拟订单（课件要求：不接真实 API）。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """拒绝额外字段与隐式类型转换（课件：StrictModel 是默认行为）。"""

    model_config = ConfigDict(extra="forbid", strict=True)


# ---------- Input Schema：模型 -> Runtime 的参数协议 ----------

class SearchOrdersInput(StrictModel):
    status: Literal["pending", "paid", "shipped", "cancelled"] | None = Field(
        default=None, description="订单状态；不传则查询所有状态"
    )
    created_from: date | None = Field(
        default=None, description="创建日期下限（含当天），格式 YYYY-MM-DD"
    )
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限，1-50")

    @model_validator(mode="after")
    def require_filter(self) -> "SearchOrdersInput":
        if self.status is None and self.created_from is None:
            raise ValueError("status 与 created_from 至少提供一个")
        return self


# ---------- Output Schema：业务 -> Agent 的事实协议 ----------

class OrderSummary(StrictModel):
    order_id: str
    status: Literal["pending", "paid", "shipped", "cancelled"]
    created_at: str
    amount_cents: int = Field(ge=0)


class SearchOrdersOutput(StrictModel):
    orders: list[OrderSummary]
    total: int = Field(ge=0)


# ---------- Error Schema：失败 -> 恢复的协议 ----------

class ToolError(StrictModel):
    code: Literal[
        "INVALID_ARGUMENT",    # 模型修正参数后可再次调用
        "PERMISSION_DENIED",   # 停止，提示切换身份
        "APPROVAL_REQUIRED",   # 进入人工确认
        "TIMEOUT",             # 仅幂等时 Runtime 有限重试
        "UPSTREAM_ERROR",      # 仅幂等时 Runtime 有限重试
        "INVALID_OUTPUT",      # 工具违反返回协议，记工程故障
    ]
    message: str
    retryable: bool = False


# ---------- ToolDefinition：完整契约，公开部分与治理部分分离 ----------

Permission = Literal["order:read", "order:write"]
RiskLevel = Literal["low", "medium", "high"]
ToolHandler = Callable[[BaseModel, "ExecutionContext"], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolDefinition:
    """模型侧（name/description/input_model）+ 应用侧（其余字段）。

    to_model_tool() 只导出公开 Schema：
    handler、permission、risk、timeout 等运行时字段一律不出现在模型可见 JSON 中。
    """

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
    handler: ToolHandler

    def to_model_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


# ---------- 可信上下文与模拟数据 ----------

@dataclass(frozen=True)
class ExecutionContext:
    """可信上下文：user_id / permissions 由登录态生成，绝不来自模型参数。"""

    user_id: str
    permissions: frozenset[str]
    trace_id: str
    order_service: "DemoOrderService"


class DemoOrderService:
    """模拟订单数据：user_demo 三条 + 其他用户一条（验证越权隔离）。"""

    _records = [
        {"user_id": "user_demo", "order_id": "ord_1001", "status": "pending",
         "created_at": "2026-08-18T10:30:00+08:00", "amount_cents": 2999},
        {"user_id": "user_demo", "order_id": "ord_1002", "status": "paid",
         "created_at": "2026-10-20T09:00:00+08:00", "amount_cents": 1599},
        {"user_id": "user_demo", "order_id": "ord_1003", "status": "pending",
         "created_at": "2026-10-21T15:20:00+08:00", "amount_cents": 4200},
        {"user_id": "user_other", "order_id": "ord_2001", "status": "pending",
         "created_at": "2026-10-21T16:00:00+08:00", "amount_cents": 9900},
    ]

    async def search(
        self,
        *,
        user_id: str,
        status: str | None,
        created_from: date | None,
        limit: int,
    ) -> list[dict]:
        records = [
            record
            for record in self._records
            if record["user_id"] == user_id
            and (status is None or record["status"] == status)
            and (
                created_from is None
                or date.fromisoformat(record["created_at"][:10]) >= created_from
            )
        ]
        # Output 降噪：user_id 属于可信上下文，不进入模型可见结果
        return [
            {key: value for key, value in record.items() if key != "user_id"}
            for record in records[:limit]
        ]


async def search_orders(
    args: SearchOrdersInput,
    ctx: "ExecutionContext",
) -> SearchOrdersOutput:
    rows = await ctx.order_service.search(
        user_id=ctx.user_id,
        status=args.status,
        created_from=args.created_from,
        limit=args.limit,
    )
    return SearchOrdersOutput(
        orders=[OrderSummary.model_validate(row) for row in rows],
        total=len(rows),
    )


# 描述模仿瑞幸文档风格：做什么 / 适用范围 / 明确不做的事
SEARCH_ORDERS = ToolDefinition(
    name="search_orders",
    description=(
        "根据订单状态与创建日期查询当前用户的订单列表，"
        "仅支持查询最近 90 天内的订单。"
        "只读查询：不能取消、退款或修改订单。"
    ),
    input_model=SearchOrdersInput,
    output_model=SearchOrdersOutput,
    error_model=ToolError,
    permission="order:read",
    risk="low",
    timeout_seconds=5,
    max_retries=2,
    idempotent=True,
    handler=search_orders,
)
