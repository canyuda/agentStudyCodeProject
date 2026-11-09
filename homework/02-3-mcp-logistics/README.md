# 02-3 · 课堂练习 10.1：扩展 get_logistics（基于 02-3-mcp-orders 拷贝）

把 `02-3-mcp-orders` 的 server.py / host.py 原样拷贝过来，仅做本题改动：
Server 侧新增 `get_logistics` 契约，**Host 侧主链（ToolRuntime / result_payload /
discover_tools / run_agent）一行不改**——新工具靠既有 Adapter 自动进入统一执行入口。
依赖与运行方式见两个 py 文件顶部的 docstring。

## 五条练习要求 ↔ 实现与验证

| # | 要求 | 实现落点 | 验证输出 |
|---|---|---|---|
| 1 | 仍使用 `orders.get_logistics` 限定名 | Host `discover_tools` 聚合自动产生，Server 不改名 | `TOOLS [... 'orders.get_logistics']` |
| 2 | 找不到物流返回稳定业务错误，不抛未处理异常 | Server：订单不存在→`ORDER_NOT_FOUND`；订单存在但无物流（如 pending）→`LOGISTICS_NOT_FOUND`，均经 `ToolError` → Host 归一化为 `MCP_TOOL_ERROR` + 原始错误码 | `LOGISTICS MISSING ... LOGISTICS_NOT_FOUND` |
| 3 | Runtime 不增加任何针对物流工具的特殊分支 | 与 orders 版 host.py 逐行 diff：43 行差异全部在 `main()` 的契约测试用例、LIVE 输入与注释（diff 从 284 行起）；`ToolRuntime` / `result_payload` / `discover_tools` / `run_agent` 零 diff | `LOGISTICS SUCCESS SF Express` |
| 4 | 结果超上限返回 `RESULT_TOO_LARGE` | 复用既有 `result_payload` 限长（2000 字符）；构造演示订单 `ord_1003`（60 条超长轨迹）专门触发 | `LOGISTICS TOO_LARGE RESULT_TOO_LARGE` |
| 5 | 模型先查订单，仅 `status=shipped` 才查物流 | 真实模型链路：`LOOP 1` 查 ord_1001 → `LOOP 2` shipped 才查物流 → `LOOP 3` 查 ord_1002 → `LOOP 4` final；**pending 的 ord_1002 未查物流** | `LIVE` 总结（见下） |

## 验收问题（10.2）：我的回答 + 批改

**① 一句话区分 Function Calling、MCP、Tool Runtime 与 Agent Loop**

【我的回答】FC 是 Agent 内写的工具，适合通用的功能，比如获取当前时间、查询当前登录人基本信息；MCP 是外部的、通用能力的封装，有点像微服务提供的接口，只不过是给 LLM 判断使用；TR 是所有工具调用共同经过的执行入口，解决参数校验、超时、归一化，就像手用来使用工具，包含指甲（内部工具）、扫把（外部工具）；AL 是把 Tool Result 回灌上下文并决定是否继续的控制循环。

【批改】TR、AL 与课件口径一致；"手 + 指甲 + 扫把"的比喻很好——同一只手（Runtime）操作长在手上的指甲（本地工具）和拿过来的扫把（外部工具），正是"所有工具经过同一入口"。MCP 基本对，微服务接口的类比成立，两点修正：**"给 LLM 判断使用"应为"给 LLM 选择"**（最终执行者是 Runtime，不是模型）；且 MCP 不等于"远程"——stdio 子进程同样在 Host 进程之外（本地文件工具也走 MCP），"外部"的准确含义是**不在 Host 进程内、不由 Agent 仓库提供**。**FC 说错了**：Function Calling 不是"Agent 内写的工具"，而是**模型输出工具名 + 结构化参数的机制**——它是一种决策输出能力，不负责真实执行，也不是工具的归属分类。"本地工具 vs MCP 工具"才是按归属分的那组对比，而 FC 对两者一视同仁：模型看到的都是 ToolDefinition 投影出的 Schema，根本不知道工具在哪个进程。"获取当前时间、查当前登录人"是**本地工具**的典型例子，不是 FC 的定义。修正后的一句话：FC 是模型"说要调什么"的机制，MCP 是"怎么把外部能力接进来"的协议，TR 是"这次调用能否执行、怎样执行"的入口，AL 是"拿到结果后接下来做什么"的循环。

**② 能否用测试区分参数拒绝、Tool Error、网络失败和 Loop 恢复**

【我的回答】参数拒绝 = Schema 校验失败；Tool Error = 归一化为错误信息和错误码；网络失败 = 可以有限重试；Loop 恢复 = tool 报业务失败，然后 LLM 判断是否改规划。

【批改】四条分类全部正确，但题目问的是**用测试区分**——每条还要落到可断言的证据，这层漏了。课件口径："测试不能只断言最终一句话，要验证**副作用与执行轨迹**——Server 是否真的收到调用、调用次数、参数和顺序。"逐条补全：

| 情况 | 测试断言（分类之外的证据） |
|---|---|
| 参数拒绝 | 返回 `INVALID_ARGUMENT` **且 Server 从未收到调用**（服务端日志零记录、零副作用）——若 Server 已执行过仍属不合格 |
| Tool Error | 返回 `MCP_TOOL_ERROR` + 原始错误码，**Server 确实执行过**（日志有轨迹），且不触发重试 |
| 网络失败 | Transport 异常由 Client/ConnectionManager 捕获，断言**重试次数有限**（如 ≤N），重试期间不把异常当业务失败回灌 Loop |
| Loop 恢复 | 断言完整轨迹：第 1 轮 tool_call 业务失败 → tool result 回灌消息 → 第 2 轮模型改计划（换参数/换工具）→ final（LIVE 的 LOOP 序列即证据） |

**③ 权限、审计与多租户为什么放在接入之后单独治理**

【我的回答】不接入你怎么知道这是什么工具、有什么权限；接入先做是先让工具可见，然后才是校验权限、是否执行做审计、根据工具属性判断是否多租户。

【批改】"先可见、后治理"的顺序逻辑对——权限判断的输入（工具名、Schema、读写属性、风险等级）来自接入层的发现结果，没有工具目录就没有治理对象。再补三层：

1. **协议分工**：MCP SDK/规范本身不提供权限、审批、审计、多租户（SDK 与 Harness 边界表）——"知道工具存在"和"允许这次调用"是两个问题，分属两层。
2. **架构解耦**：治理若混进接入层，换传输（stdio→HTTP）或换 Server 都要重写治理逻辑；单独治理才能在 `Runtime.execute()` 前后形成**统一插入点**，一处覆盖所有工具（本地与 MCP 同样过检）——下一节工具治理的落点。
3. **工程节奏**：主链跑通前写治理规则，没有真实调用链和轨迹可验证；治理的设计输入（谁会调、调用模式、危险动作清单）依赖先跑起来的真实目录。

小修正：多租户不只是"根据工具属性判断"——更准确是**连接与凭证层的隔离**（Client 绑定租户凭证、ConnectionManager 按 tenant+server_id 路由连接），属于连接管理而非工具属性。

## 与 02-3-mcp-orders 的差异清单

| 文件 | 改动 |
|---|---|
| `server.py` | 新增 `get_logistics` Tool 与 `LOGISTICS` 数据；`ORDERS` 增加演示订单 `ord_1003`（shipped + 60 条超长轨迹，专为触发要求 4） |
| `host.py` | 仅 `main()`：追加三条物流契约用例（成功/无物流/超限）+ LIVE 输入改为"先查订单、shipped 才查物流"（要求 5），`max_rounds` 提到 8；主链零改动 |

LIVE 摘录（真实模型，DeepSeek；两订单的查询顺序由模型自主决策）：

```
LOOP 1 tool_call orders_get_order      # 先查订单状态（两单各一次）
LOOP 2 tool_call orders_get_order
LOOP 3 tool_call orders_get_logistics  # 仅对 shipped 的 ord_1001 查物流 → 已签收
LOOP 4 final                           # pending 的 ord_1002 全程未调物流工具
```
