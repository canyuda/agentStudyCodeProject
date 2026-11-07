# 02-1 课堂练习：模仿瑞幸协议接口的 search_orders 最小调用

> 对应课件：`正式课/02-第二章-Function Calling、Tool Runtime 与 MCP/课件/01-FunctionCalling 与 Tool Use.pdf` 四、课堂练习 4.2
> 协议风格参考：https://open.lkcoffee.com/docs （学习其"名称 / 描述 / 入参 / 出参 / 错误码"的接口文档写法，不调用其真实 API）

## 文件

| 文件 | 内容 |
|---|---|
| `lk_tool_protocol.py` | 三份 Schema（Input/Output/Error）+ ToolDefinition + 模拟订单数据 + `SEARCH_ORDERS` 装配 |
| `exercise_4_2.py` | 四步演示，每步带 assert 程序化验收（fail fast） |

## 四步与验收对照

| 步骤 | 课件要求 | 程序化验收 |
|---|---|---|
| 1. 定义名称、描述、Input Schema | 描述写明做什么/适用范围/不做什么 | 空过滤条件、limit 越界被 Pydantic 拒绝 |
| 2. 生成模型可见 Tool JSON | 确认没有 handler、权限和内部依赖 | 遍历禁词（handler/permission/risk/timeout/...）断言不出现 |
| 3. 构造两条不同 tool_call_id 的 Tool Call | 同名工具、不同参数、不同 id | id 唯一性断言 |
| 4. 生成 Tool Result 并断言 ID 一致 | 结果必须带回原始 tool_call_id | 乱序写回后逐条断言配对 + 内容归属 + 越权隔离（user_other 的订单不出现） |

## 运行

```bash
cd agentStudyCodeProject/homework/02-1-luckin-search-orders
python exercise_4_2.py   # 依赖 pydantic v2
```

按课件要求：只用模拟订单数据；不实现 Registry、Snapshot、完整权限系统或重试框架。

## 协议设计要点（模仿瑞幸文档风格）

- 名称稳定唯一：`search_orders`（不是 `query_data`）；
- 描述三件事：做什么（按状态/日期查订单）、适用范围（最近 90 天）、不做什么（不能取消/退款/修改）；
- 入参有约束：枚举状态、日期格式、limit 1-50、至少一个过滤条件（`model_validator`）；
- 出参是稳定事实：`orders[] + total`，且过滤掉 `user_id`（可信上下文不进模型）；
- 错误码驱动恢复：`code × retryable` 分工（`INVALID_ARGUMENT` 可改参重调，`PERMISSION_DENIED` 停止）。
