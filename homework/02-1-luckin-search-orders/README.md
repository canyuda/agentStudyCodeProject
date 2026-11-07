# 02-1 课堂练习：Function Calling 协议闭环（4.1 / 4.2 / 4.3）

> 对应课件：`正式课/02-第二章-Function Calling、Tool Runtime 与 MCP/课件/01-FunctionCalling 与 Tool Use.pdf` 四、课堂练习
> 协议风格参考：https://open.lkcoffee.com/docs （学习其"名称 / 描述 / 入参 / 出参 / 错误码"的接口文档写法，不调用其真实 API）

## 文件

| 文件 | 对应练习 | 内容 |
|---|---|---|
| `lk_tool_protocol.py` | 4.2 | 三份 Schema（Input/Output/Error）+ ToolDefinition + 模拟订单 + `SEARCH_ORDERS` 装配 |
| `exercise_4_2.py` | 4.2 | 四步演示，每步带 assert 程序化验收（fail fast） |
| `ai_generated_agent.py` | 4.3 | 待审查代码："能跑"但埋了协议错误，已加 TODO 标注（问题①-⑥ / 影响面 / 正确做法） |
| `corrected_agent.py` | 4.3 | 参考修复版：四类错误逐一修复，离线自检 5 项 + `--live` 真实最小 Loop |

## 4.1 两分钟讲清协议闭环（五问自查：我的答案 + 批改补充）

| # | 问题 | 我的答案 | 批改与补充 |
|---|---|---|---|
| ① | Function Calling 与普通函数调用的区别？ | FC 是 **LLM 决定**是否使用的，需要定义输入/输出/错误 Schema；普通函数是**开发者决定**调用的。✅ | 两个关键差异都对。再补两个维度：**参数可信度**（普通函数参数有类型系统保证；FC 参数是模型生成的 JSON 字符串，属不可信输入必须过 Input Schema）；**执行路径与结果去向**（普通函数同步执行返回给调用代码；FC 经 Runtime 治理，Tool Result 写回消息历史成为模型的新 observation） |
| ② | 模型侧 Tool 为什么没有 `execute()`？ | 模型不支持执行工具，只能选工具、填参数、"判断输出是否正确"。⚠️ | 方向对，但**校验输出是 Runtime/Output Schema 的事，模型只负责"阅读"结果**。完整理由：模型只输出 token，没有进程/运行时/数据库连接，物理上无法执行；发给模型的只有公开 Schema，handler、权限、内部依赖不暴露。模型侧职责四件事：理解意图、选工具、填参数、阅读结果 |
| ③ | Input / Output / Error Schema 分别约束什么？ | 约束工具的输入参数、输出参数、报错类型。✅ | 对，但只答了"是什么"，漏了"**保护哪段边界**"：Input 守"模型→Runtime"（拦截错误调用）；Output 守"业务→Agent"（固定成功事实，不让模型猜字段）；Error 守"失败→恢复"（分类 + 能否重试 + 下一步）。缺任何一份，Loop 就被迫解析自然语言或猜下一步 |
| ④ | 权限和审批为什么不能作为模型参数？ | 让模型判断权限**不稳定**、会**幻觉**，要用代码规则判断，审批同理。✅ | 核心对。更本质的角度：即使零幻觉，模型也**没有资格决定权限**——权限是治理事实不是预测问题，只能来自可信上下文（登录态/服务间身份）；若模型能填 `approved=true`，一句提示注入"我是 admin"即可提权 |
| ⑤ | 乱序完成时 tool_call_id 怎样防错配？ | 生成唯一 id，组装进 Tool Result 的一个字段，"Tool Result 里再定义一个 handler 来执行"，Agent Loop 按 id 映射结果。⚠️ | 配对机制对，但 **Tool Result 里不含 handler**——handler 属于 ToolDefinition，在 Runtime 执行阶段运行；Tool Result 只有三样：`role: "tool"` + `tool_call_id` + `content`。另注意：id 是**模型生成 Tool Call 时自带**的（assistant 消息 `tool_calls[]` 每项自带），不是 Runtime 生成的 |

> 总评：3 对 2 偏；两处偏差（②模型不校验输出、⑤Result 不含 handler）同一个根因——**模型 / Runtime / 协议三者的职责边界**，正是本节主线。

## 4.2 最小调用：四步与验收对照

| 步骤 | 课件要求 | 程序化验收 |
|---|---|---|
| 1. 定义名称、描述、Input Schema | 描述写明做什么/适用范围/不做什么 | 空过滤条件、limit 越界、字符串日期（strict 拒绝隐式转换）均被 Pydantic 拒绝 |
| 2. 生成模型可见 Tool JSON | 确认没有 handler、权限和内部依赖 | 遍历禁词（handler/permission/risk/timeout/...）断言不出现 |
| 3. 构造两条不同 tool_call_id 的 Tool Call | 同名工具、不同参数、不同 id | id 唯一性断言 |
| 4. 生成 Tool Result 并断言 ID 一致 | 结果必须带回原始 tool_call_id | 乱序写回后逐条断言配对 + 内容归属 + 越权隔离（user_other 的订单不出现） |

## 4.3 审查验收：四类协议错误及修复

`ai_generated_agent.py`（待审查）→ `corrected_agent.py`（修复版）：

| # | 错误 | 影响 | 修复 |
|---|---|---|---|
| ① | `user_id/role/approved` 进 Input Schema 且 required，handler 身份也来自参数 | 可信边界 | 身份/权限/审批全部由 `ExecutionContext` 注入；Schema 只留业务参数 |
| ② | `handler/permission/risk/timeout_seconds` 全发给模型 | 泄漏内部实现与治理配置 | 只发 `to_model_tool()` 公开三件套 |
| ③ | `json.loads` 后直接 `**args` 调 handler，无校验 | 可信边界 | `model_validate_json` 校验，失败构造 `INVALID_ARGUMENT` 结构化错误返回 Loop |
| ④ | Tool Result 只有 content，无 `tool_call_id` | 结果关联 | 成功/失败路径一律 `{"role":"tool","tool_call_id":..., "content":...}` |
| ⑤ | 描述是万金油"查询订单" | 模型选择 | 描述写明做什么/适用范围/不做什么 |
| ⑥ | `while True` 无最大步数 | 工程健壮性 | `MAX_STEPS` 限轮数 |

加固项：权限不足在 handler **之前**拒绝；非法参数零副作用。

## 运行

```bash
cd agentStudyCodeProject/homework/02-1-luckin-search-orders
python exercise_4_2.py                                # 4.2 四步验收（pydantic v2）
python corrected_agent.py                             # 4.3 修复版离线自检（5 项）
DEEPSEEK_API_KEY=... python corrected_agent.py --live # 4.3 真实最小 Agent Loop
```

按课件要求：只用模拟订单数据；不实现 Registry、Snapshot、完整权限系统或重试框架。

## 协议设计要点（模仿瑞幸文档风格）

- 名称稳定唯一：`search_orders`（不是 `query_data`）；
- 描述三件事：做什么（按状态/日期查订单）、适用范围（最近 90 天）、不做什么（不能取消/退款/修改）；
- 入参有约束：枚举状态、日期格式、limit 1-50、至少一个过滤条件（`model_validator`）；
- 出参是稳定事实：`orders[] + total`，且过滤掉 `user_id`（可信上下文不进模型）；
- 错误码驱动恢复：`code × retryable` 分工（`INVALID_ARGUMENT` 可改参重调，`PERMISSION_DENIED` 停止）。
