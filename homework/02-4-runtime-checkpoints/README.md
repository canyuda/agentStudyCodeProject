# 02-4 · 工具治理与安全边界（课件 2.4 · 课堂练习 10.1）

补齐 `ToolRuntime.invoke()` 骨架的七个关键检查点，并用副作用证据验收。
依赖与运行方式见三个 py 文件顶部的 docstring。

## invoke() 的七个关键检查点

| # | 检查点（课件 10.1） | 实现 | 稳定错误码 / 结果 |
|---|---|---|---|
| 1 | 根据工具名查找定义 | `invoke()` prepare 段，`self._tools.get(call.name)` | `TOOL_NOT_FOUND` |
| 2 | Pydantic 校验并结构化错误 | `StrictArgs`（`extra="forbid"` + `strict=True`），错误归一化为 `[{path, message}]` | `INVALID_ARGUMENT` |
| 3 | PermissionEngine 三态决策 | 十条固定顺序：deny → plan → 执行期白名单 → RBAC → 业务 precheck → 高风险审批 → bypass → allow → 教学 Shell 检测 → 默认允许 | `allow / deny / confirm` |
| 4 | deny/confirm 时 Handler 零调用 | 非 allow 立即返回结构化 ToolResult，仍记 decision 审计 | 副作用计数为 0 |
| 5 | 按只读/幂等驱动超时与重试 | `retries = max_retries if (read or idempotent) else 0`；非幂等写超时不重放 | `TIMEOUT_UNKNOWN` / `TIMEOUT` / `DEPENDENCY_UNAVAILABLE` |
| 6 | finalize 脱敏与审计 | 键名递归脱敏（token/secret/password/authorization + 邮箱）；decision/execution 两阶段、只记参数键名 | 敏感零泄漏 |
| 7 | 返回结果保留原始 tool_call_id | `ToolResult.to_tool_message()` 原样带回 | `role="tool"` 配对回填 Loop |

## 演示工具与策略表

| 工具 | effect | risk | permission | 审批 | 重试 | 用途 |
|---|---|---|---|---|---|---|
| get_order | read | medium | order:read | 否 | 可重试（幂等） | 演示成功路径 + 脱敏 |
| create_refund | write | high | refund:create | **是** | **0（非幂等禁重放）** | 演示 confirm → 审批重入 → 一次性消费 |
| run_shell | write | critical | shell:run | 否 | 0 | 演示 deny-first（deny 规则优先于 bypass） |

审批绑定（课件 §六）：`approval_id` 由服务端签发，绑定 user / tenant / tool / **参数 SHA-256**（递归排序稳定序列化）/ 过期时间 / 一次性 `used`；批准 100 元改 399 元重入即失效。`approval_id` 走 `ExecutionContext`，不进模型参数。

## 验收 ↔ 测试对照（test_tool_governance.py）

| 课件测试 | 测试 | 副作用证据 |
|---|---|---|
| 1 deny-first：bypass 不能覆盖硬拒绝 | `test_deny_first_beats_bypass` | shell_executions == 0 |
| 2 plan：写工具被拒 | `test_plan_blocks_write` | refund_executions == 0 |
| 3 Schema：不能伪造身份/确认 | `test_schema_rejects_forged_identity` | 结构化 path 含 user_id/approved；零调用 |
| 4 RBAC：权限不足零调用 | `test_rbac_denies_without_permission` | refund_executions == 0 |
| 5 审批改参：批 100 改 399 失效 | `test_approval_flow_one_shot_and_param_binding`（6b） | 零调用 |
| 6 结果边界：脱敏 + 保留 tool_call_id | `test_redaction_in_result_and_audit` | 结果与审计双出口无敏感值；两阶段审计齐全 |
| 7 双重白名单：执行期仍拦截 | `test_whitelist_enforced_at_execute` | refund_executions == 0 |
| 8 一次性审批：重放返回 confirm | 同上（6d） | refund_executions 仍 == 1 |

另附三个延伸用例：`TOOL_NOT_FOUND` 结构化返回、非幂等写超时恰好调用 1 次（策略误配 max_retries=3 也被幂等守卫拦住）、只读瞬时失败指数退避重试后成功。

## 演示场景（exercise_10_1.py）

call_01 查询成功（脱敏）→ call_02 confirm 暂停 → call_03 审批重入成功（幂等键 = call_03）→ call_04 伪造字段被拒 → call_05 bypass 下仍 DENY_RULE → call_06 plan 拒写 → call_07 审批改参被拒。末尾对账：退款执行 1 次、Shell 0 次，拒绝调用均有 decision 审计——证明拒绝发生在副作用之前。
