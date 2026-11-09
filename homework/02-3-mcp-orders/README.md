# 02-3 · 订单查询 MCP Server + Host（课件 2.3 §7 复刻）

把上一节 ToolRuntime 的本地函数工具迁到独立 MCP Server（stdio / Streamable HTTP），
Host 侧用 MCP Adapter 发现远端工具、适配成统一 `ToolDefinition`、接回原有 Runtime 与
Agent Loop——**主链零改动，只替换工具的接入方式**。依赖与运行方式见两个 py 文件顶部的
docstring。

## 文件地图

| 文件 | 职责 |
|---|---|
| `server.py` | 订单 MCP Server：`get_order` / `create_ticket`（Tool，`structured_output=True` + 稳定错误码）、`policy://order-status`（Resource，只读）、`order_assistant`（Prompt，任务模板）；`--http` 切 Streamable HTTP，否则 stdio |
| `host.py` | MCP Adapter（`discover_tools` 限定名 + handler 包装）→ `ToolRuntime`（唯一执行入口：查表→Schema 校验→handler）→ `run_agent`（Loop：回灌→再决策，`max_rounds` 兜底）；`main()` 内嵌确定性契约测试 |

## 验收结果（实际运行输出）

四条路径全部走通（课件 §7.4）：

```
PROTOCOL 2026-07-28
TOOLS ['orders.create_ticket', 'orders.get_order']
RESOURCES ['policy://order-status']
PROMPTS ['order_assistant']
CONTRACT SUCCESS shipped          # ① 成功：结构化结果关联回 tool_call_id
REJECT INVALID_ARGUMENT           # ② 拒绝：参数校验发生在远端执行之前
FAIL MCP_TOOL_ERROR               # ③ 失败：Tool Error ≠ 网络故障，稳定错误码
LOOP 1 tool_call orders_get_order # ④ 恢复：真实模型查 ord_missing → ORDER_NOT_FOUND
LOOP 2 tool_call orders_get_order #    观察失败结果改计划 → 查 ord_1002
LOOP 3 final                      #    正常收敛，给出 pending 的最终答案
```

Streamable HTTP 单独验证：`server.py --http` 在 `127.0.0.1:8000` 完成启动与关闭。

## 作业中发现的真实坑（课件未覆盖）

**限定名与 Function Calling 命名约束冲突**：限定名 `orders.get_order` 含点号，
DeepSeek（OpenAI-compatible）拒绝——`tools[0].function.name` 只接受
`^[a-zA-Z0-9_-]+$`。修复方式恰好印证课件"限定名是 Host 的内部治理标识"：

- 模型可见名做**投影**：`ToolDefinition.model_name = name.replace(".", "_")`
  （呼应 02-2 的"Schema 是模型可见的安全投影"）；
- `ToolRuntime.execute` 先按限定名查表，miss 再按 `model_name` 映射回限定名执行；
- 审计与治理标识始终是限定名，模型层的命名差异由 Host 吸收——Server、Runtime
  结构、Loop 逻辑均不变。

## 与课件的对应关系

| 课件设计点 | 本作业落点 |
|---|---|
| 任务级契约而非裸暴露路由 | Server 只发布 `get_order`/`create_ticket`，不暴露 `ORDERS` 更新能力 |
| 稳定失败语义 | `ORDER_NOT_FOUND` / `INVALID_REASON` 经 `ToolError` → `result_payload` 归一化为 `MCP_TOOL_ERROR` + 原始 message |
| 参数拒绝先于远端执行 | `Runtime.execute` 的 jsonschema 校验在 `client.call_tool()` 之前 |
| 结果归一化与限长 | `result_payload`：`is_error`→结构化失败、`structured_content` 优先、`RESULT_TOO_LARGE` |
| 限定名 + Run 级工具目录 | `discover_tools` 产出 `orders.*` 快照 dict，Loop 全程使用同一份 |
| 单次调用只是 RPC | `run_agent` 的回灌循环 + 三个终止条件（正常 / `MODEL_REPLY_INVALID` / `MAX_ROUNDS_EXCEEDED`） |
