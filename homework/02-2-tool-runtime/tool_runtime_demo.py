"""最小但完整的 Agent Tool Runtime(课件 2.2,练习 9.2)。

invoke() 实现练习要求的五个检查点:

    1. 快照查找            -> TOOL_NOT_FOUND(绝不读 Registry 最新版本)
    2. 参数校验            -> INVALID_ARGUMENT(extra='forbid' 挡掉伪造字段)
    3. 策略拦截            -> TOOL_DISABLED / PERMISSION_DENIED /
                              APPROVAL_REQUIRED / DEPENDENCY_UNAVAILABLE
    4. 受控执行            -> 超时;读操作可按策略重试,写操作绝不自动重试;
                              INTERNAL_ERROR 归一化(不泄漏堆栈和内网地址)
    5. 结果包装            -> 输出校验、脱敏、审计,始终保留 tool_call_id

运行演示(不需要模型 API,不访问网络):
    python -m pip install "pydantic>=2,<3"
    python tool_runtime_demo.py

运行验收测试(课件 9.2 七条):
    python -m pytest test_tool_runtime.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 受控日志:真实原因(堆栈、内网地址)只留在这里;模型只会看到稳定错误码。
# 库代码按惯例挂 NullHandler,由使用方决定日志目的地。
CONTROL_LOG = logging.getLogger("tool_runtime.control")
CONTROL_LOG.addHandler(logging.NullHandler())

# handler 统一签名:入参是(校验后的参数, 可信上下文),返回原始 dict
ToolHandler = Callable[[BaseModel, "ExecutionContext"], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# 第一组:调用契约——调用双方约定好的东西
# ---------------------------------------------------------------------------


class CreateTicketInput(BaseModel):
    """extra='forbid':模型伪造的多余字段(如 approved=true)在这里就被挡掉。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    priority: Literal["low", "normal", "high"] = "normal"


class CreateTicketOutput(BaseModel):
    status: str
    ticket_id: str


class SearchOrdersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)


class SearchOrdersOutput(BaseModel):
    order_ids: list[str]


# ---------------------------------------------------------------------------
# 第二组:运行治理——Runtime 怎样对待这个工具(模型看不到这些)
# 第三组:真实实现——handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    backoff_s: float = 0.05


@dataclass(frozen=True)
class ToolDefinition:
    """不可变的注册单元:契约 + 治理 + 实现,三组字段缺一不可。"""

    # 第一组:调用契约
    name: str
    version: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    error_codes: tuple[str, ...]
    # 第二组:运行治理
    permission: str
    risk: Literal["low", "medium", "high"]
    access: Literal["read", "write"]  # 消费者是重试策略:读可重试,写绝不
    timeout_s: float
    retry: RetryPolicy
    audit_fields: tuple[str, ...]
    dependencies: tuple[str, ...]
    execution_mode: Literal["sequential", "parallel"] = field(
        default="sequential", kw_only=True
    )  # kw_only 仅为满足 dataclass "无默认值字段必须在有默认值之前"的规则
    # 第三组:真实实现
    handler: ToolHandler


@dataclass(frozen=True)
class ExecutionContext:
    """可信身份:从认证体系解析而来,永远不从模型参数里取。"""

    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    approvals: frozenset[str] = frozenset()  # 已批准的 tool_call_id(来自人工批准系统)


class ToolCall(BaseModel):
    """模型提出的候选动作;此时业务还没有发生。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResultMessage:
    """可以写回 Agent Loop 的结果;始终保留原始 tool_call_id。"""

    tool_call_id: str
    content: dict[str, Any] | None = None
    error: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Registry 与 Snapshot:平台有哪些工具,本轮允许看到哪些
# ---------------------------------------------------------------------------


class ToolRegistry:
    """只负责注册阶段的职责:尽早发现定义冲突。"""

    def __init__(self) -> None:
        # 组合键 (name, version):同名同版本重复注册直接拒绝,不静默覆盖
        self._tools: dict[tuple[str, str], ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        key = (tool.name, tool.version)
        if key in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}@{tool.version}")
        if tool.timeout_s <= 0 or tool.retry.max_attempts < 1:
            raise ValueError("timeout and retry policy must be positive")
        self._tools[key] = tool

    def get(self, name: str, version: str) -> ToolDefinition:
        try:
            return self._tools[(name, version)]
        except KeyError as exc:
            raise KeyError(f"tool not registered: {name}@{version}") from exc

    def snapshot(
        self,
        routes: Mapping[str, str],
        enabled_state: Mapping[str, bool],
    ) -> "ToolSnapshot":
        """冻结本轮:按 routes 解析 name+version,过滤掉已停用工具。"""
        tools: dict[str, ToolDefinition] = {}
        for name, version in routes.items():
            tool = self.get(name, version)  # 路由指向未注册版本,注册期就炸(fail fast)
            if enabled_state.get(name, True):
                tools[name] = tool
        return ToolSnapshot(tools)


class ToolSnapshot:
    """本轮冻结的视图;创建后绝不更新(快照不是缓存)。"""

    def __init__(self, tools: dict[str, ToolDefinition]) -> None:
        self._tools = MappingProxyType(dict(tools))  # 只读映射,防止被改

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def provider_tools(self) -> list[dict[str, Any]]:
        """安全投影:只给模型 name + description + parameters。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_model.model_json_schema(),
                },
            }
            for t in self._tools.values()
        ]


# ---------------------------------------------------------------------------
# Runtime:prepare -> execute -> finalize
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _mask_sensitive(value: Any) -> Any:
    """after 阶段的脱敏挂载点:把输出里手机号样式的字符串打码。"""
    if isinstance(value, str):
        return _PHONE_RE.sub(lambda m: f"{m.group(0)[:3]}****{m.group(0)[-4:]}", value)
    if isinstance(value, dict):
        return {k: _mask_sensitive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_sensitive(v) for v in value]
    return value


class ToolRuntime:
    """夹在 Agent Loop 与真实 handler 之间的执行层。"""

    def __init__(self, audit_log: list[dict[str, Any]]) -> None:
        self._audit_log = audit_log

    def error(self, call: ToolCall, code: str, message: str) -> ToolResultMessage:
        """统一稳定错误结构;每条错误都保留原 tool_call_id。"""
        return ToolResultMessage(
            tool_call_id=call.id,
            error={"code": code, "message": message},
        )

    async def invoke(
        self,
        snapshot: ToolSnapshot,
        call: ToolCall,
        context: ExecutionContext,
        *,
        enabled_state: Mapping[str, bool] | None = None,
        dependencies: Mapping[str, bool] | None = None,
    ) -> ToolResultMessage:
        # 每个出口都审计一次:outcome 记录"放行还是拒绝,为什么"
        result = await self._invoke(snapshot, call, context, enabled_state or {}, dependencies or {})
        self._audit(snapshot, call, context, result)
        return result

    async def _invoke(
        self,
        snapshot: ToolSnapshot,
        call: ToolCall,
        context: ExecutionContext,
        enabled_state: Mapping[str, bool],
        dependencies: Mapping[str, bool],
    ) -> ToolResultMessage:
        # ---- prepare:不该执行的调用绝不进入业务系统 --------------------
        # 检查点 1:在本轮快照里查找,绝不读 Registry 最新版本
        tool = snapshot.get(call.name)
        if tool is None:
            return self.error(call, "TOOL_NOT_FOUND", "tool is not available in this snapshot")

        # 检查点 2:Schema 校验;伪造的多余字段死在这里
        try:
            params = tool.input_model.model_validate(call.arguments)
        except ValidationError:
            return self.error(call, "INVALID_ARGUMENT", "arguments failed schema validation")

        # 检查点 3:策略拦截;每个失败都停在正确位置
        if not enabled_state.get(call.name, True):  # 重新读最新启停:管理员可能中途停用
            return self.error(call, "TOOL_DISABLED", "tool is temporarily disabled")
        if tool.permission not in context.permissions:
            return self.error(call, "PERMISSION_DENIED", "permission denied")
        if tool.risk == "high" and call.id not in context.approvals:
            # 批准必须来自可信 ExecutionContext 并绑定当前 tool_call_id,
            # 永远不读 arguments.approved——那等于让模型自己填批准结果
            return self.error(call, "APPROVAL_REQUIRED", "human approval required")
        if not all(dependencies.get(dep, True) for dep in tool.dependencies):
            return self.error(
                call, "DEPENDENCY_UNAVAILABLE", "required dependency is temporarily unavailable"
            )

        # ---- execute:控制业务动作怎样运行 ------------------------------
        # 写操作绝不自动重试(课件 7.2):写超时只表示调用方没及时收到结果,
        # 不代表下游没有成功;没有幂等保证就重试,可能重复创建/扣款/删除。
        attempts = tool.retry.max_attempts if tool.access == "read" else 1
        raw_output: dict[str, Any] | None = None
        try:
            for attempt in range(attempts):
                try:
                    raw_output = await asyncio.wait_for(
                        tool.handler(params, context), timeout=tool.timeout_s
                    )
                    break
                except TimeoutError:
                    if attempt == attempts - 1:
                        raise  # 重试额度用完,交给外层统一返回 TIMEOUT
                    await asyncio.sleep(tool.retry.backoff_s)
        except TimeoutError:
            return self.error(call, "TIMEOUT", "tool execution timed out")
        except Exception:
            # 归一化:真实原因进受控日志,模型永远看不到堆栈和内网地址
            CONTROL_LOG.exception("handler failed: %s@%s", tool.name, tool.version)
            return self.error(call, "INTERNAL_ERROR", "internal error")

        # ---- finalize:无论成功失败,都产生可关联、可消费的结果 -----------
        # 检查点 5:输出校验、脱敏,保留 tool_call_id
        try:
            output = tool.output_model.model_validate(raw_output)
        except ValidationError:
            return self.error(call, "OUTPUT_INVALID", "handler output failed validation")

        # 审计统一由 invoke() 包装器记录(成功/拒绝恰好各一条),这里只做脱敏
        return ToolResultMessage(tool_call_id=call.id, content=_mask_sensitive(output.model_dump()))

    def _audit(
        self,
        snapshot: ToolSnapshot,
        call: ToolCall,
        context: ExecutionContext,
        result: ToolResultMessage,
    ) -> None:
        """放行和拒绝都审计:重建一次调用的决策链,只记字段名,不记值。"""
        tool = snapshot.get(call.name)
        self._audit_log.append(
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "version": tool.version if tool else None,  # 查找就失败的调用没有版本可记
                "tenant_id": context.tenant_id,
                "user_id": context.user_id,
                "outcome": result.error["code"] if result.error else "OK",
                "argument_fields": sorted(call.arguments.keys()),
            }
        )


# ---------------------------------------------------------------------------
# 演示:课件运行结果里的六条路径
# ---------------------------------------------------------------------------


def make_create_ticket_handler(probe: dict[str, int]) -> ToolHandler:
    """假的内部工单服务调用;probe 用来证明拒绝路径没有进入 handler。"""

    async def handler(params: CreateTicketInput, context: ExecutionContext) -> dict[str, Any]:
        probe["create_ticket"] += 1
        if params.title == "__boom__":
            # 故意带内网地址和密码信息的异常,验证归一化不泄漏
            raise RuntimeError("connect to db 10.0.0.7:27017 failed: password rejected")
        if params.title == "__slow__":
            await asyncio.sleep(5)  # 演示/测试超时用
        await asyncio.sleep(0.01)  # 模拟一点真实延迟
        return {"status": "created", "ticket_id": "T-1001"}

    return handler


def make_search_orders_handler(probe: dict[str, int]) -> ToolHandler:
    """假的订单搜索;读操作超时可以按重试策略退避重试。"""

    async def handler(params: SearchOrdersInput, context: ExecutionContext) -> dict[str, Any]:
        probe["search_orders"] += 1
        if params.query == "__slow__":
            await asyncio.sleep(5)
        return {"order_ids": ["o-1001", "o-1002"]}

    return handler


async def demo() -> None:
    audit_log: list[dict[str, Any]] = []
    probe = {"create_ticket": 0, "search_orders": 0}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="create_ticket",
            version="1.0.0",
            description="Create a customer service ticket (write, needs approval)",
            input_model=CreateTicketInput,
            output_model=CreateTicketOutput,
            error_codes=(
                "INVALID_ARGUMENT",
                "PERMISSION_DENIED",
                "APPROVAL_REQUIRED",
                "TIMEOUT",
                "INTERNAL_ERROR",
            ),
            permission="ticket:write",
            risk="high",
            access="write",
            timeout_s=0.2,
            retry=RetryPolicy(max_attempts=2, backoff_s=0.05),
            audit_fields=("title", "priority"),
            dependencies=("ticket-service",),
            handler=make_create_ticket_handler(probe),
        )
    )

    routes = {"create_ticket": "1.0.0"}
    enabled_state = {"create_ticket": True}
    snapshot = registry.snapshot(routes, enabled_state)  # 在这里冻结(v1.0.0)
    runtime = ToolRuntime(audit_log=audit_log)

    print("模型可见工具:", ", ".join(t["function"]["name"] for t in snapshot.provider_tools()))

    def ctx(
        approvals: frozenset[str],
        permissions: frozenset[str] = frozenset({"ticket:write"}),
    ) -> ExecutionContext:
        return ExecutionContext(
            user_id="u-1", tenant_id="t-1", permissions=permissions, approvals=approvals
        )

    valid_args = {"title": "退款失败", "description": "用户已等待两天", "priority": "high"}
    cases: list[tuple[ToolCall, ExecutionContext]] = [
        # call-1:缺必填字段 -> INVALID_ARGUMENT
        (
            ToolCall(id="call-1", name="create_ticket", arguments={"description": "缺标题"}),
            ctx(frozenset({"call-1"})),
        ),
        # call-2:没有 ticket:write 权限 -> PERMISSION_DENIED
        (
            ToolCall(id="call-2", name="create_ticket", arguments=dict(valid_args)),
            ctx(frozenset({"call-2"}), permissions=frozenset()),
        ),
        # call-3:参数合法但高风险无人工批准 -> APPROVAL_REQUIRED
        (
            ToolCall(id="call-3", name="create_ticket", arguments=dict(valid_args)),
            ctx(frozenset()),
        ),
        # call-4:全部放行 -> OK
        (
            ToolCall(id="call-4", name="create_ticket", arguments=dict(valid_args)),
            ctx(frozenset({"call-4", "call-5"})),
        ),
        # call-5:已批准但 handler 内部异常 -> INTERNAL_ERROR
        (
            ToolCall(id="call-5", name="create_ticket", arguments={**valid_args, "title": "__boom__"}),
            ctx(frozenset({"call-4", "call-5"})),
        ),
    ]

    results: list[ToolResultMessage] = []
    for call, context in cases:
        result = await runtime.invoke(
            snapshot, call, context,
            enabled_state=enabled_state, dependencies={"ticket-service": True},
        )
        results.append(result)

    # call-6:管理员在模型"思考"的几秒里把工具停用了——快照没变,
    # 但 invoke() 重新读了最新启停状态,拒绝执行(旧快照,新状态)
    enabled_state["create_ticket"] = False
    call6 = ToolCall(id="call-6", name="create_ticket", arguments=dict(valid_args))
    results.append(
        await runtime.invoke(
            snapshot, call6, ctx(frozenset({"call-6"})),
            enabled_state=enabled_state, dependencies={"ticket-service": True},
        )
    )
    cases.append((call6, ctx(frozenset({"call-6"}))))

    for call, result in zip((c for c, _ in cases), results):
        if result.error is not None:
            print(f"{call.id} -> {result.error['code']}")
        else:
            print(f"{call.id} -> OK {json.dumps(result.content, ensure_ascii=False)}")

    # ---- 验收(课件 9.2)----------------------------------------------
    # 拒绝路径 call-1/2/3/6 都没进 handler;只有 call-4(成功)和 call-5(内部异常)进了
    assert probe["create_ticket"] == 2, probe
    internal = results[4].error
    assert internal is not None and internal["code"] == "INTERNAL_ERROR"
    assert "10.0.0.7" not in internal["message"], internal
    assert "RuntimeError" not in internal["message"], internal
    assert all(r.tool_call_id == c.id for (c, _), r in zip(cases, results))
    assert len(audit_log) == 6, len(audit_log)  # 6 次调用,放行/拒绝各恰好一条审计

    print("验收通过: 4 条拒绝路径未进入 handler，内部异常也没有泄漏 Python 堆栈")
    print("验收通过: 成功结果保留 tool_call_id，旧快照中的停用工具仍被拒绝")
    print("审计记录数:", len(audit_log))


if __name__ == "__main__":
    asyncio.run(demo())
