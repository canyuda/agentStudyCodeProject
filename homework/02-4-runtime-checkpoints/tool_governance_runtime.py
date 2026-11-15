"""Agent 工具治理 Runtime(课件 2.4,课堂练习 10.1)。

在 invoke() 骨架中补齐课件要求的七个关键检查点:

    1. 根据工具名查找定义            -> TOOL_NOT_FOUND
    2. Pydantic 校验并结构化错误      -> INVALID_ARGUMENT([{path,message}])
    3. PermissionEngine 三态决策      -> allow / deny / confirm(固定顺序,不可交换)
    4. deny/confirm 时 Handler 零调用 -> 拒绝路径仍有 decision 审计,副作用计数为零
    5. 按只读/幂等驱动超时与重试      -> 非幂等写超时返回 TIMEOUT_UNKNOWN,禁止重放
    6. finalize 脱敏与审计            -> 键名递归脱敏;decision/execution 两阶段审计
    7. 返回结果保留原始 tool_call_id  -> to_tool_message() 回填 Agent Loop

运行演示(离线,不需要模型 API,不访问网络):
    python -m pip install "pydantic>=2,<3"
    python exercise_10_1.py

运行验收测试:
    python -m pytest test_tool_governance.py -q
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 受控日志:真实原因(堆栈、内网地址)只留在这里;模型只会看到稳定错误码。
CONTROL_LOG = logging.getLogger("tool_governance.control")
CONTROL_LOG.addHandler(logging.NullHandler())

DecisionAction = Literal["allow", "deny", "confirm"]
Effect = Literal["read", "write"]
Risk = Literal["low", "medium", "high", "critical"]
PermissionMode = Literal["default", "plan", "bypassPermissions", "dontAsk"]

# handler 统一签名:(tool_call_id, 校验后参数, 可信上下文) -> 原始业务 dict
# tool_call_id 作为幂等键传入 handler(课件 call_03:idempotency_key=call_03)。
ToolHandler = Callable[[str, BaseModel, "ExecutionContext"], Awaitable[dict[str, Any]]]
Precheck = Callable[[BaseModel, "ExecutionContext"], None]


class StrictArgs(BaseModel):
    """模型参数基类:拒绝未知字段与隐式类型转换(课件 5.1)。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class PolicyDenied(Exception):
    """业务预检查拒绝(precheck 在 handler 之前抛出)。"""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class TransientToolError(Exception):
    """瞬时故障(下游抖动):只读/幂等工具可按策略有限重试(课件 5.4)。"""


# ---------------------------------------------------------------- 对象定义
@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """策略表七问的结构化答案(课件 4.1)。"""

    effect: Effect
    risk: Risk
    permission: str
    requires_approval: bool
    timeout_seconds: float
    max_retries: int
    idempotent: bool


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """一个对象、两组消费者:模型只见投影,运行侧持有治理与实现。"""

    name: str
    description: str
    parameters_model: type[StrictArgs]
    policy: ToolPolicy
    handler: ToolHandler
    precheck: Precheck | None = None

    def to_model_tool(self) -> dict[str, Any]:
        # 投影边界:只暴露 name/description/JSON Schema(课件 4.4)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """可信上下文:授权事实全部来自服务端,模型无权提供(课件 4.3)。"""

    trace_id: str
    user_id: str
    tenant_id: str
    mode: PermissionMode
    permissions: frozenset[str]
    allowed_tools: frozenset[str]
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型提出的候选动作(不是执行许可证)。"""

    tool_call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """稳定、可解释、可追踪的执行结果(课件 §二)。"""

    tool_call_id: str
    tool_name: str
    ok: bool
    action: DecisionAction
    code: str
    content: Any

    def to_tool_message(self) -> dict[str, Any]:
        # 检查点 7:原样保留 tool_call_id,Loop 靠它配对
        return {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.content}


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """三态决策:allow / deny / confirm(课件 §三)。"""

    action: DecisionAction
    code: str
    reason: str
    source: str


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """最小审计记录:只记参数键名,不记参数值(课件 7.1)。"""

    trace_id: str
    tool_call_id: str
    tool_name: str
    user_id: str
    tenant_id: str
    phase: Literal["decision", "execution"]
    decision: str
    code: str
    argument_keys: tuple[str, ...]
    latency_ms: int | None = None


class AuditSink:
    """append-only 审计池:拒绝的调用也记 decision(证明副作用为零)。"""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)

    def by_trace(self, trace_id: str) -> list[AuditRecord]:
        return [r for r in self.records if r.trace_id == trace_id]


# ---------------------------------------------------------------- 审批
def canonical_target(arguments: Mapping[str, Any]) -> str:
    """递归排序、稳定序列化后的 SHA-256:审批绑定的是参数本身(课件 §六)。"""
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Approval:
    """服务端审批记录:绑定主体与参数摘要,一次性消费。"""

    approval_id: str
    user_id: str
    tenant_id: str
    tool_name: str
    arguments_digest: str
    expires_at: float
    used: bool = False


class ApprovalStore:
    """审批存储:校验存在性/一次性/过期/主体/参数哈希(课件 §六)。"""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = ttl_seconds
        self._approvals: dict[str, Approval] = {}

    def issue(
        self, user_id: str, tenant_id: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> str:
        """可信界面确认后由服务端签发,模型无权生成 approval_id。"""
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        self._approvals[approval_id] = Approval(
            approval_id=approval_id,
            user_id=user_id,
            tenant_id=tenant_id,
            tool_name=tool_name,
            arguments_digest=canonical_target(dict(arguments)),
            expires_at=time.monotonic() + self._ttl,
        )
        return approval_id

    def consume(
        self,
        approval_id: str,
        user_id: str,
        tenant_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PermissionDecision:
        approval = self._approvals.get(approval_id)
        if approval is None:
            return PermissionDecision("deny", "INVALID_APPROVAL", "审批不存在", "approval")
        if approval.used:
            return PermissionDecision("deny", "INVALID_APPROVAL", "审批已消费(一次性)", "approval")
        if time.monotonic() > approval.expires_at:
            return PermissionDecision("deny", "INVALID_APPROVAL", "审批已过期", "approval")
        if (approval.user_id, approval.tenant_id, approval.tool_name) != (user_id, tenant_id, tool_name):
            return PermissionDecision("deny", "INVALID_APPROVAL", "审批主体不匹配", "approval")
        if approval.arguments_digest != canonical_target(dict(arguments)):
            # 批准 100 元改成 399 元重入:参数哈希不一致,旧审批失效
            return PermissionDecision("deny", "INVALID_APPROVAL", "审批绑定的参数被修改", "approval")
        self._approvals[approval_id] = replace(approval, used=True)
        return PermissionDecision("allow", "APPROVED", "审批校验通过", "approval")


# ---------------------------------------------------------------- 决策引擎
class PermissionEngine:
    """只做三态决策,不执行 handler(课件 §三/§五)。

    固定顺序(课件 §九,不可交换):
        1 deny 规则 -> 2 plan -> 3 执行期白名单 -> 4 RBAC -> 5 业务 precheck
        -> 6 高风险审批 -> 7 bypass 只跳过普通确认 -> 8 allow
        -> 9 教学危险 Shell 检测 -> 10 默认允许
    """

    def __init__(
        self,
        approvals: ApprovalStore,
        deny_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._approvals = approvals
        self._deny_tools = deny_tools

    async def decide(
        self, tool: ToolDefinition, arguments: BaseModel, context: ExecutionContext
    ) -> PermissionDecision:
        # 1. deny-first:硬拒绝优先于一切模式(含 bypassPermissions)
        if tool.name in self._deny_tools:
            return PermissionDecision("deny", "DENY_RULE", "命中 deny 规则", "deny_rules")
        # 2. plan 模式:只读边界由执行层强制,写操作/Shell 一律拒绝
        if context.mode == "plan" and tool.policy.effect == "write":
            return PermissionDecision("deny", "PLAN_MODE_DENIED", "plan 模式禁止写操作和 Shell", "plan")
        # 3. 执行期重查白名单(双重白名单的第二道,课件 4.2.1)
        if tool.name not in context.allowed_tools:
            return PermissionDecision("deny", "TOOL_NOT_ALLOWED", "工具不在本轮执行白名单", "whitelist")
        # 4. RBAC:检查细粒度权限点,而非角色名称(课件 4.2.2)
        if tool.policy.permission not in context.permissions:
            return PermissionDecision(
                "deny", "PERMISSION_DENIED", f"缺少业务权限 {tool.policy.permission}", "rbac"
            )
        # 5. 业务预检查:资源归属/状态/额度,事实来自业务侧(课件 5.2)
        if tool.precheck is not None:
            try:
                tool.precheck(arguments, context)
            except PolicyDenied as error:
                return PermissionDecision("deny", error.code, error.reason, "precheck")
        # 6. 高风险审批:一次性参数绑定;bypass 不可跳过,dontAsk 直接拒绝
        if tool.policy.requires_approval:
            if context.mode == "dontAsk":
                return PermissionDecision(
                    "deny", "CONFIRM_UNAVAILABLE", "无人工交互场景,需确认动作直接拒绝", "mode"
                )
            if context.approval_id is None:
                # confirm:暂停等待可信界面确认,handler 零调用
                return PermissionDecision("confirm", "APPROVAL_REQUIRED", "需要确认本次具体动作", "approval")
            return self._approvals.consume(
                context.approval_id,
                context.user_id,
                context.tenant_id,
                tool.name,
                dict(arguments.model_dump()),
            )
        # 9. 教学用危险 Shell 模式检测(deny 规则之外的兜底)
        if "shell" in tool.name:
            return PermissionDecision("deny", "DANGEROUS_SHELL", "教学环境禁止真实 Shell", "teaching_guard")
        # 8/10. allow 规则 -> 默认允许
        return PermissionDecision("allow", "OK", "策略放行", "default")


# ---------------------------------------------------------------- 脱敏
SENSITIVE_KEY = re.compile(r"token|secret|password|authorization", re.I)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


def redact(value: Any) -> Any:
    """递归脱敏:敏感键名替换值、字符串脱敏邮箱(课件 5.5,键名导向)。"""
    if isinstance(value, Mapping):
        return {
            key: "***" if SENSITIVE_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return EMAIL_PATTERN.sub("***@***", value)
    return value


# ---------------------------------------------------------------- Runtime
class ToolRuntime:
    """唯一执行入口:模型/CLI/离线演示/测试共用同一个 invoke()(课件 §八)。"""

    def __init__(
        self,
        tools: Mapping[str, ToolDefinition],
        engine: PermissionEngine,
        audit: AuditSink,
        side_effects: dict[str, int] | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._engine = engine
        self.audit = audit
        self.side_effects = side_effects if side_effects is not None else {}

    async def invoke(self, call: ToolCall, context: ExecutionContext) -> ToolResult:
        started = time.monotonic()
        argument_keys = tuple(sorted(call.arguments.keys()))

        # ---------------- prepare ----------------
        # 检查点 1:根据工具名查找定义
        tool = self._tools.get(call.name)
        if tool is None:
            self._audit_decision(call, context, "deny", "TOOL_NOT_FOUND", argument_keys, started)
            return self._blocked_result(call, "deny", "TOOL_NOT_FOUND", f"未知工具 {call.name}")
        # 检查点 2:Pydantic 校验,错误结构化为 [{path, message}]
        try:
            arguments = tool.parameters_model.model_validate(dict(call.arguments))
        except ValidationError as error:
            detail = [
                {"path": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
                for item in error.errors()
            ]
            self._audit_decision(call, context, "deny", "INVALID_ARGUMENT", argument_keys, started)
            return self._blocked_result(call, "deny", "INVALID_ARGUMENT", detail)
        # 检查点 3:PermissionEngine 三态决策(固定顺序)
        decision = await self._engine.decide(tool, arguments, context)
        # 每次调用都记 decision 审计:allow/deny/confirm 均记(课件 7.2 两阶段)
        self._audit_decision(call, context, decision.action, decision.code, argument_keys, started)
        # 检查点 4:deny/confirm 立即返回,handler 零调用
        if decision.action != "allow":
            return self._blocked_result(call, decision.action, decision.code, decision.reason)

        # ---------------- execute ----------------
        # 检查点 5:按只读/幂等属性驱动超时与重试
        raw = await self._execute_with_recovery(tool, call, arguments, context)

        # ---------------- finalize ----------------
        # 检查点 6:脱敏与审计(execution 阶段);检查点 7:保留原始 tool_call_id
        latency_ms = int((time.monotonic() - started) * 1000)
        self.audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="execution",
                decision="executed" if raw["ok"] else "failed",
                code=raw["code"],
                argument_keys=argument_keys,
                latency_ms=latency_ms,
            )
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            ok=bool(raw["ok"]),
            action="allow",
            code=raw["code"],
            content=redact(raw["content"]),
        )

    async def _execute_with_recovery(
        self, tool: ToolDefinition, call: ToolCall, arguments: BaseModel, context: ExecutionContext
    ) -> dict[str, Any]:
        """超时 + 恢复:重试资格由副作用与幂等属性决定(课件 5.4)。"""
        # 幂等守卫写进代码:非只读且非幂等 -> 一律 0 次,策略被改坏也拦得住
        retries = tool.policy.max_retries if (tool.policy.effect == "read" or tool.policy.idempotent) else 0
        backoff = 0.05
        last_code = "INTERNAL_ERROR"
        for attempt in range(retries + 1):
            try:
                async with asyncio.timeout(tool.policy.timeout_seconds):
                    payload = await tool.handler(call.tool_call_id, arguments, context)
                return {"ok": True, "code": "OK", "content": payload}
            except TransientToolError:
                last_code = "DEPENDENCY_UNAVAILABLE"
                if attempt == retries:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 0.2)
            except (asyncio.TimeoutError, TimeoutError):
                # 非幂等写超时:状态未知,禁止重放,交给上层查真实状态
                if tool.policy.effect == "write" and not tool.policy.idempotent:
                    return {
                        "ok": False,
                        "code": "TIMEOUT_UNKNOWN",
                        "content": "写操作超时,状态未知;请查询真实业务状态,不要重放",
                    }
                last_code = "TIMEOUT"
                if attempt == retries:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 0.2)
            except PolicyDenied as error:
                # handler 内业务规则拒绝:确定失败,不重试
                CONTROL_LOG.warning("handler denied %s: %s", call.tool_call_id, error.reason)
                return {"ok": False, "code": error.code, "content": error.reason}
        CONTROL_LOG.error("tool %s failed after retries: code=%s", call.name, last_code)
        return {"ok": False, "code": last_code, "content": "工具执行失败,已记录受控日志"}

    def _audit_decision(
        self,
        call: ToolCall,
        context: ExecutionContext,
        action: DecisionAction,
        code: str,
        argument_keys: tuple[str, ...],
        started: float,
    ) -> None:
        """decision 审计:每次调用必有一条(allow 也记,课件 7.2)。"""
        self.audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="decision",
                decision=action,
                code=code,
                argument_keys=argument_keys,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        )

    @staticmethod
    def _blocked_result(
        call: ToolCall,
        action: DecisionAction,
        code: str,
        content: Any,
    ) -> ToolResult:
        """拒绝/确认路径:handler 零调用,仅构造结构化结果(审计已由 _audit_decision 记)。"""
        return ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            ok=False,
            action=action,
            code=code,
            content=content,
        )


# ---------------------------------------------------------------- 演示装配
# 模拟订单表:复合键 (tenant_id, order_id),归属隔离写在查询条件里(课件 5.2)
ORDERS: dict[tuple[str, str], dict[str, Any]] = {
    ("tenant_a", "ord_1001"): {
        "status": "paid",
        "refundable": 399.0,
        "customer_email": "user01@example.com",
        "service_token": "demo-token-123",
    }
}


class GetOrderArgs(StrictArgs):
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")


class CreateRefundArgs(StrictArgs):
    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")
    amount: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=4, max_length=200)


class RunShellArgs(StrictArgs):
    command: str = Field(min_length=1)


def _refund_precheck(arguments: CreateRefundArgs, context: ExecutionContext) -> None:
    """业务预检查:归属 + 状态 + 额度,事实来自业务表而非模型(课件 5.2)。"""
    order = ORDERS.get((context.tenant_id, arguments.order_id))
    if order is None or order["status"] != "paid":
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或状态不可退款")
    if arguments.amount > float(order["refundable"]):
        raise PolicyDenied("BUSINESS_RULE_DENIED", "退款金额超过可退金额")


def _order_precheck(arguments: GetOrderArgs, context: ExecutionContext) -> None:
    if (context.tenant_id, arguments.order_id) not in ORDERS:
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或不属于当前租户")


def build_demo_runtime() -> tuple[ToolRuntime, ApprovalStore, dict[str, int]]:
    """装配课件 §四 的三个教学工具,返回 (runtime, approvals, side_effects)。"""
    side_effects: dict[str, int] = {"refund_executions": 0, "shell_executions": 0}
    audit = AuditSink()

    async def order_handler(
        tool_call_id: str, arguments: GetOrderArgs, context: ExecutionContext
    ) -> dict[str, Any]:
        return dict(ORDERS[(context.tenant_id, arguments.order_id)])

    async def refund_handler(
        tool_call_id: str, arguments: CreateRefundArgs, context: ExecutionContext
    ) -> dict[str, Any]:
        # 非幂等写:每执行一次就多一笔真实退款(演示用计数器代替资金变动)
        side_effects["refund_executions"] += 1
        return {
            "refund_id": f"ref_{9000 + side_effects['refund_executions']}",
            "idempotency_key": tool_call_id,
            "tenant_id": context.tenant_id,
            "order_id": arguments.order_id,
            "amount": arguments.amount,
            "status": "accepted",
        }

    async def shell_handler(
        tool_call_id: str, arguments: RunShellArgs, context: ExecutionContext
    ) -> dict[str, Any]:
        # 永远不应到达:deny-first 在决策层就拦截;若到达即治理失效
        side_effects["shell_executions"] += 1
        return {"executed": True, "command": arguments.command}

    tools: dict[str, ToolDefinition] = {
        "get_order": ToolDefinition(
            name="get_order",
            description="查询订单状态与可退金额",
            parameters_model=GetOrderArgs,
            policy=ToolPolicy(
                effect="read", risk="medium", permission="order:read",
                requires_approval=False, timeout_seconds=2.0, max_retries=2, idempotent=True,
            ),
            handler=order_handler,
            precheck=_order_precheck,
        ),
        "create_refund": ToolDefinition(
            name="create_refund",
            description="对已支付订单创建退款,需人工确认",
            parameters_model=CreateRefundArgs,
            policy=ToolPolicy(
                effect="write", risk="high", permission="refund:create",
                requires_approval=True, timeout_seconds=2.0, max_retries=0, idempotent=False,
            ),
            handler=refund_handler,
            precheck=_refund_precheck,
        ),
        "run_shell": ToolDefinition(
            name="run_shell",
            description="教学模拟 Shell(真实场景禁止暴露给模型)",
            parameters_model=RunShellArgs,
            policy=ToolPolicy(
                effect="write", risk="critical", permission="shell:run",
                requires_approval=False, timeout_seconds=2.0, max_retries=0, idempotent=False,
            ),
            handler=shell_handler,
        ),
    }
    approvals = ApprovalStore()
    engine = PermissionEngine(approvals, deny_tools=frozenset({"run_shell"}))
    runtime = ToolRuntime(tools, engine, audit, side_effects)
    return runtime, approvals, side_effects


def demo_context(
    mode: PermissionMode = "default",
    permissions: frozenset[str] | None = None,
    allowed_tools: frozenset[str] | None = None,
    approval_id: str | None = None,
    trace_id: str = "trace_demo",
) -> ExecutionContext:
    """构造演示用可信上下文:身份/权限/白名单全部来自服务端。"""
    return ExecutionContext(
        trace_id=trace_id,
        user_id="user_456",
        tenant_id="tenant_a",
        mode=mode,
        permissions=permissions
        if permissions is not None
        else frozenset({"order:read", "refund:create", "shell:run"}),
        allowed_tools=allowed_tools
        if allowed_tools is not None
        else frozenset({"get_order", "create_refund", "run_shell"}),
        approval_id=approval_id,
    )
