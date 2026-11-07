# 02-1 作业：cancel_order 高风险写工具的受控执行

> 对应课件 2.1（1.5 工具治理）：模型可以提出动作，但不能决定权限。
> 与查询工具（02-1-luckin-search-orders）对照：写操作的治理严格一档。

## 文件

| 文件 | 内容 |
|---|---|
| `cancel_order_tool.py` | 三份 Schema + ToolDefinition 装配 + 可信上下文 + 受控执行器 + 审计日志 + 幂等重试守卫 |
| `test_cancel_order.py` | 8 条要求逐条验收（`python test_cancel_order.py`，兼容 pytest） |

## 8 条要求与实现对照

| # | 要求 | 实现要点 |
|---|---|---|
| 1 | Input 仅 order_id+reason，禁止其他字段 | `StrictModel(extra="forbid")` + `model_validator` 显式拒绝身份字段 |
| 2 | Output = 订单id/取消后状态/操作编号 | `CancelOrderOutput(order_id, status="cancelled", operation_id)` |
| 3 | 复用本节 ToolError | 与课件 `order_schemas.py` 同构（code × retryable） |
| 4 | order:write / high / 非幂等 | `CANCEL_ORDER` 装配 + `max_retries=0` |
| 5 | 模型只见 Schema，身份审批不可填 | `to_model_tool()` 只导出公开三件套；身份/审批在 `ExecutionContext` |
| 6 | 非法/无权限/未确认 → handler 调用数为 0 | 执行器顺序：名称→Input→权限→审批→**才**执行；`service.calls` 计数断言 |
| 7 | 超时后不重试取消动作 | `should_retry()` 幂等守卫（非幂等一律 False，防配置被改）+ `wait_for` 超时 |
| 8 | 成功/拒绝/失败均可定位 | `AuditLog` 每路径落记录，`(trace_id, tool_call_id)` 双键查询 |

## 关键治理顺序（拒绝发生在 handler 之前）

```
名称 -> Input Schema -> 权限(ctx) -> 审批(ctx, risk=high) -> 执行(超时受控) -> Output 验收 -> Tool Result(带原 id) -> 审计
```

任何一步拒绝：返回结构化 ToolError（带原始 tool_call_id）+ 审计记录，真实业务动作零发生。

## 运行

```bash
cd agentStudyCodeProject/homework/02-1-cancel-order
python test_cancel_order.py
```
