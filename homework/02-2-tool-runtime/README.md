# 02-2 · Tool Runtime 设计(课件 2.2 · 练习 9.2)

不看完整代码,只根据接口补出 `invoke()` 的五个检查点,并满足课件七条验收。
依赖与运行方式见两个 py 文件顶部的 docstring。

## 9.1 口头验收:脱离代码讲清边界

链路默画:注册 → 发现 → 快照 → Tool Call → prepare → execute → finalize → Tool Result

四组区别(以课件口径为准,附易错点):

| 区别 | 正确口径 | 易错点 |
|---|---|---|
| Tool Schema 与 ToolDefinition | Schema 是**模型可见的安全投影**(name + description + parameters);Definition = Schema **+ 治理字段 + handler** | 投影少是故意的:handler、权限、密钥绝不给模型 |
| Registry 与 Runtime | Registry 管"**有哪些**"(name@version 目录,不执行);Runtime 管"**这一次能否执行、怎样执行、回写**" | 注册归 Registry,发现归 Harness,执行归 Runtime——Runtime **不包含**注册和发现 |
| 工具发现与模型选工具 | 发现发生在**模型请求之前**(Harness 按任务/模式筛候选集合),决定模型**可以选什么**;模型选工具发生在请求之后(FC 在候选集中生成 Tool Call),决定**这一次想选什么** | 两个动作、两个时间点、两个决策者 |
| handler、Harness 与 Agent Loop | handler 只做业务动作;Harness 装配本轮上下文(模型/工具/Hook),组织运行但**不能绕过 Runtime 的检查**;Agent Loop 是控制循环(请求模型/保存消息/交 Runtime/决定是否继续) | 意图识别、选工具的是**模型**,Loop 只是驱动循环 |

## invoke() 的五个检查点

| # | 检查点 | 稳定错误码 |
|---|---|---|
| 1 | 从**本轮 Snapshot** 查找(绝不读 Registry 最新版本) | `TOOL_NOT_FOUND` |
| 2 | Schema 参数校验(`extra="forbid"` 挡掉伪造字段) | `INVALID_ARGUMENT` |
| 3 | 策略拦截:启停(重读最新状态)→ 权限 → 人工批准 → 依赖 | `TOOL_DISABLED` / `PERMISSION_DENIED` / `APPROVAL_REQUIRED` / `DEPENDENCY_UNAVAILABLE` |
| 4 | 受控执行:超时 + 读操作按策略重试(写操作**绝不自动重试**) | `TIMEOUT` / `INTERNAL_ERROR` |
| 5 | 结果包装:输出校验 → 脱敏 → 审计 → 保留 `tool_call_id` | `OUTPUT_INVALID` + 成功 content |

## 七条验收 ↔ 测试对照

| 验收(课件 9.2) | 测试 |
|---|---|
| 1. 非法参数不会进入 handler | `test_invalid_arguments_never_reach_handler` |
| 2. 权限不足不会产生业务副作用 | `test_permission_denied_no_side_effect` |
| 3. 模型伪造 approved=true 不能绕过批准 | `test_forged_approval_cannot_bypass` |
| 4. 执行阶段不能读取 Registry 的最新版本 | `test_invoke_uses_snapshot_not_registry` |
| 5. 所有成功和失败结果都保留原 tool_call_id | `test_all_results_keep_tool_call_id` |
| 6. 写操作超时且没有幂等保证时不自动重试 | `test_write_timeout_never_auto_retries`(+ 读操作重试对照组) |
| 7. 内部异常归一化为稳定错误,返回不含 Python 堆栈 | `test_internal_error_normalized_and_logged` |

另附审计专项:`test_audit_records_names_only`(审计能重建决策链,只记字段名、不记参数值)。

## 关键设计点

- **批准来自可信上下文**:`ExecutionContext.approvals` 存已批准的 `tool_call_id`(人工批准系统写入);模型参数里的 `approved` 被 `extra="forbid"` 结构性拒绝,Runtime 逻辑上**从不读**它——双防线。
- **快照即冻结**:模型请求与 `runtime.invoke()` 用同一个 `snapshot` 对象;启停状态(`enabled_state`)独立于定义保存,建快照时过滤一次、执行前重读一次(管理员可运行中即时停用)。
- **写操作不自动重试**:写超时只表示调用方没及时收到结果,不代表下游失败;无幂等保证时重试会重复创建/扣款/删除。读操作按 `RetryPolicy` 退避重试。
- **信息分级**:给模型的是稳定错误码和通用 message;真实原因(堆栈、内网地址)进 `tool_runtime.control` 受控日志——测试验证了两边各自的内容。
- **审计放行与拒绝各记一条**:`tool_call_id / tool / version / tenant / user / outcome / argument_fields`,其中参数**只记字段名**,绝不记值。
