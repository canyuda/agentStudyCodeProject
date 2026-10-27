# Streaming Agent Gateway

> 第一章 02-Streaming 作业：用 Python 实现一个**可观察、可取消、可恢复、可审计**的流式 Agent 网关。
> 教学项目：11 步搭建，每步一个模块，全部中文注释。

## 架构总览

```
 浏览器 / CLI          FastAPI (server.py)                Agent Loop (loop.py)
     │  POST /v1/runs ─────▶ RunStore 登记独立 Run            │
     │  GET  /events  ──SSE──▶ 账本补发 + 心跳      ModelStreamEvent
     │  POST /cancel  ─────▶ 取消信号 + task.cancel            │
     │                                                        ▼
     └── text.delta ◀──SSE── RunEvent 账本 ◀──── 包装身份 ── StreamingModel
                                              (run_id/seq/ts)   ├─ FakeStreamingModel（本地，零依赖）
              SQLite 写穿 (sqlite_store.py)                     └─ DeepSeekChatAdapter（deepseek-v4-flash）
```

两套事件是全项目的核心分层：**模型说什么**（`ModelStreamEvent`）与**网关对客户端说什么**（`RunEvent`）
分离，换模型只改 Adapter，其余全部不动。

## 11 步文件地图

| 步骤 | 文件 | 一句话职责 |
|---|---|---|
| 1 | `gateway/events.py` | RunEvent 统一事件 + 事件类型 + Run 状态机常量 |
| 2 | `gateway/sse.py` | SSE 编码 / 增量解码 / 心跳 / Last-Event-ID 解析 |
| 3 | `gateway/model.py` | StreamingModel 协议 + ModelStreamEvent（零第三方依赖） |
| 4 | `gateway/fake_model.py` `gateway/deepseek_adapter.py` | 假模型（可编程延迟/异常）与 DeepSeek Adapter（延迟 import SDK） |
| 5 | `gateway/store.py` | 内存 Run Store：账本、seq 分配、单一终态守卫、订阅通知 |
| 6 | `gateway/loop.py` | Agent Loop：翻译事件、取消、TTFT、周期 checkpoint、异常转终态 |
| 7 | `gateway/server.py` | 创建 / 订阅（SSE + 断线补发）/ 取消 / 审计查询 |
| 8 | `gateway/static/index.html` `gateway/cli.py` | Web 客户端（四按钮）与 CLI（`python -m gateway.cli`） |
| 9 | `tests/` | 35 个 pytest：编解码、守卫、取消、异常、断线恢复、HTTP 全链路 |
| 10 | `gateway/sqlite_store.py` | SQLite 写穿持久层：重启恢复、孤儿回收、checkpoint 落库 |
| 11 | 本 README + 验收 | 8 项能力与验收标准对照（见下） |

## 8 项系统能力

| # | 能力 | 实现位置 | 怎么看到 |
|---|---|---|---|
| 1 | 创建独立 Run | `store.create_run` + 独立 asyncio Task | POST /v1/runs 返回 run_id；订阅断开不影响执行 |
| 2 | 流式调用模型 | `StreamingModel` 协议 + 两个 Adapter | 文本逐 delta 到达（CLI/Web 可见打字机效果） |
| 3 | 转换统一 Harness Event | loop.py 把 ModelStreamEvent 包装成 RunEvent | 事件流里每个事件都带 run_id/seq/ts |
| 4 | SSE 推送 | `sse.encode_sse` + StreamingResponse + 心跳 | `curl -N .../events` 直接看原始 SSE 文本 |
| 5 | 用户取消 | 协作式检查 + `task.cancel()` 双保险 | Web 取消按钮 / POST cancel，毫秒级停 |
| 6 | 断线恢复 | Last-Event-ID / `?after_seq=` + 账本切片补发 | Web「断开/重连」按钮；CLI `--resume --after` |
| 7 | checkpoint 保存 | loop 每 N delta 存快照 + `checkpoint.saved` 事件 + SQLite | 订阅流里的 checkpoint.saved；`GET /v1/runs/{id}` |
| 8 | TTFT 统计 | loop 记录首 token 时刻，`run.metrics` 事件发布 | 事件流末尾的 `ttft_ms`；CLI 的 `[event] metrics` 行 |

## 验收标准对照

| 标准 | 结论 | 证据 |
|---|---|---|
| 首个 delta 快速展示 | ✅ | Fake 首延迟 0.3s 精确生效；TTFT 随 `run.metrics` 发布可审计 |
| 不依赖模型 SDK | ✅ | 核心链路（events/sse/model/store/loop/server）grep 无 openai；SDK 只在 DeepSeek 工厂内延迟 import；默认 fake 全程无 SDK |
| Run 单一终态 | ✅ | 账本守卫 + 状态守卫（终态后追加/迁移均抛错）；三选一终态只发一次 |
| 取消真停止 | ✅ | 协作式取消 8ms 内返回、delta 数远小于全量；强制取消 shield 保住终态事件 |
| 断线不重跑 | ✅ | 断开只停订阅；重连从断点续传；测试断言模型调用次数恒为 1 |

## 快速开始

```bash
cd project/homework/streamingAgentGateway
PY=../../.venv/bin/python     # 解释器：project/.venv

# 1) 起服务（默认 fake + SQLite 持久化，配置见 project/.env）
$PY -m uvicorn gateway.server:app --port 8300

# 2) 打开浏览器 http://127.0.0.1:8300/ （Web 客户端）
#    或用 CLI：
$PY -m gateway.cli "用一段话解释 SSE"
$PY -m gateway.cli --resume <run_id> --after <seq>   # 断点续读

# 3) 切真实模型（读 project/.env 的 DEEPSEEK_API_KEY）
GATEWAY_MODEL=deepseek $PY -m uvicorn gateway.server:app --port 8300

# 4) 跑测试（35 个）
$PY -m pytest
```

> PyCharm：右键本目录 → Mark Directory as → Sources Root；`tests/` → Test Sources Root；
> 解释器选 `project/.venv/bin/python`。所有命令从本目录发出。

## 环境变量（project/.env）

| 变量 | 默认 | 作用 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | DeepSeek 认证（切真实模型时必需） |
| `GATEWAY_MODEL` | `fake` | `fake` / `deepseek` |
| `GATEWAY_DB` | `gateway.db` | SQLite 路径；置空回到纯内存模式 |
| `DEEPSEEK_BASE_URL` | 官方地址 | 可选覆盖 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | 可选覆盖 |

`gateway.db*` 已被根 `.gitignore` 排除；`project/.env` 同样不入库。

## 设计要点备忘（面试可讲）

- **下标即 seq**：账本 append-only，`events[i].seq == i`，断线补发就是一个切片；
- **取消两层**：协作式（每个模型事件检查信号，break 关闭生成器）+ 强制式（task.cancel 打断网络 IO），
  终态写入用 `asyncio.shield` 防二次取消丢事件；
- **孤儿回收**：重启时仍处 running 的 Run 补 `PROCESS_RESTARTED` 失败终态，保证每个 Run 都有终态；
- **浏览器 EventSource 不能自定义请求头**：手动重连的断点走 `?after_seq=`，自动重连走原生 Last-Event-ID；
- **取消的异步本质**：cancel 响应返回时终态事件可能尚未落账，判断 Run 结局要以账本为准。

---

## 开发过程实录（本会话 11 步完整记录）

> 逐步开发、每步停下确认；每步都当场验证通过才进入下一步。记录里也保留了
> 踩过的坑和修法——对教学项目来说，错误和修复与成功同样有价值。

### 第 1 步：定义 RunEvent（`gateway/events.py`）

- 定义 8 种事件类型（`RunEventType`）、Run 状态机常量（created → running → 三选一终态）、
  `TERMINAL_EVENT_TYPES` / `TERMINAL_STATUSES` 集合与 `is_terminal_event()` 判断函数。
- `RunEvent` frozen dataclass：`run_id / seq / type / ts / data`；`to_dict() / from_dict()`
  作为唯一序列化出入口，SSE 与 SQLite 共用。
- 验证：JSON 往返字段不丢（含中文）、`frozen` 拦截修改、终态判断正确。
- 同时建立包骨架 `gateway/__init__.py` 与 `requirements.txt`。

### 第 2 步：SSE 编解码（`gateway/sse.py`）

- `encode_sse`（id/event/data + 空行）、`encode_heartbeat`（`: keep-alive`）、
  `SSEDecoder`（逐行喂入、空行触发组包、多行 data 拼接、`last_event_id` 游标属性）、
  `parse_last_event_id`（None→-1、非法值抛 ValueError fail fast）。
- 教学点：解码器按"行"喂而不是按"块"——网络流任意切割，边界（空行）位置不可预知。
- 踩坑：初稿 `data:` 行解析写了一段 `if False else` 死分支，当即清理。
- 验证：编码→逐行解码往返相等、心跳不污染后续事件、data 含真实换行不丢、头解析三种情况。
- 环境插曲：用户 PyCharm 报 `from gateway.events import` 红线——确立**包内一律相对导入**
  （`from .events import`），所有命令从项目目录发出。

### 第 3 步：抽象 StreamingModel（`gateway/model.py`）

- `ModelStreamEvent`（模型侧中间事件：text.delta / model.finished / model.usage）、
  `Message` 类型别名、`StreamingModel` Protocol（`@runtime_checkable`）。
- 核心分层：ModelStreamEvent 是"模型说了什么"，RunEvent 是"带身份的内容"；
  补 run_id/seq 只能发生在 Agent Loop，因为模型不知道 Run 的存在。
- 该文件零第三方 import——抽象层不依赖 SDK 的物理证明。
- 验证：不继承任何基类的内联 EchoModel 通过 `isinstance(m, StreamingModel)`（鸭子类型），
  按契约产出完整事件流。

### 第 4 步：接入 Adapter（`fake_model.py` + `deepseek_adapter.py`）

- `FakeStreamingModel`：`first_token_delay`（精确控制 TTFT）、`inter_chunk_delay`、
  `fail_after`（第 N 个 delta 后抛异常，测试注入点）、`finish_reason="length"`（截断分支）。
- `DeepSeekChatAdapter`：参数对齐课件 1-3（关 thinking、include_usage）；
  **openai 只在工厂 `build_deepseek_model()` 内延迟 import**；配置集中为常量，
  key 从 `project/.env` 读取（路径按文件位置向上推导三级）。
- 环境插曲：系统 `python3` 没装依赖，发现课件用的解释器是 `project/.venv`，
  此后所有命令统一用该 venv。
- 验证：Fake 首延迟 0.2s 实测 0.20s、`fail_after=3` 按时抛错；`import` 模块后
  `sys.modules` 无 openai、调用工厂后才出现；工厂构造 `deepseek-v4-flash` 成功（key 有效，未打印）。

### 第 5 步：Run Store（`gateway/store.py`）

- `Run`（数据）+ `RunStore`（管理）：`create_run / append_event / events_after /
  set_status / request_cancel / wait_for_events / save_checkpoint`。
- 关键设计：**下标即 seq**（append-only，补发就是切片）；**单一终态双重守卫**
  （账本末尾终态→拒绝追加；终态状态→同值幂等/换值抛错）；追加与 notify 在
  持锁小节内原子完成；`wait_for_events` 返回"是否被唤醒"而非事件内容（防虚假唤醒）。
- 踩坑：初稿 `append_event` 是同步方法且 notify 写法错误——`Condition.notify_all`
  必须在 `async with` 持锁内调用，改为与课件一致的 async 版本。
- 验证：seq 稠密 `[0,1,2,3]`、两守卫抛错、`events_after(2)→[3,4]`、终态取消返回
  False、等待超时→False / 并发追加→True、checkpoint 覆盖写。
- 其间一次断言失败是验证脚本自己数错了账本长度，Store 行为无误。

### 第 6 步：Agent Loop（`gateway/loop.py`）

- `execute_run()`：消费 ModelStreamEvent → 包装 RunEvent 入账；**取消两层**
  （协作式：每事件检查信号即 return，生成器关闭、token 不再消费；强制式：
  task.cancel 打断网络 IO）；终态写入用 `asyncio.shield` 防二次取消丢事件；
  TTFT 用 monotonic 计时随 `run.metrics` 在终态前发布；每 N delta 存 checkpoint；
  异常统一转 `run.failed`（区分 MODEL_STREAM_FAILED 可重试 / OUTPUT_TRUNCATED 不可重试）。
- 踩坑：`Run` 误从 events 导入（应在 store）；删掉带死参数的 `finalize` 包装。
- 验证五场景：正常完成（顺序/TTFT/checkpoint）、协作取消（60 字停在第 7 字、
  8ms 返回）、模型异常（partial=一二）、length 截断、强杀（`task.cancelled()` 为 True
  且终态事件落账）。其间发现验证脚本忘了覆盖默认 `first_token_delay=0.3s`，
  导致取消发生在首 token 前、演示效果失真，调整后重测通过。

### 第 7 步：创建/订阅/取消接口（`gateway/server.py`）

- `create_app(store, model)` 工厂 + 依赖注入；`POST /v1/runs`（run.created 入账后
  独立 Task 启动 Loop）、`GET .../events`（Last-Event-ID 补发 + 心跳 + 断开只停订阅）、
  `POST .../cancel`（信号 + task.cancel 双保险）、`GET /v1/runs/{id}`（审计查询）。
- "断线不重跑"的实现位置：客户端断开只是订阅生成器 return，Loop Task 无感知。
- 验证（httpx ASGITransport 直打 app，注入 CountingModel 统计模型调用次数）：
  订阅读 3 个事件断开 → `Last-Event-ID: 2` 重连从 seq 3 续读到终态，两段拼接连续
  无重复，**模型调用次数 = 1**；取消幂等；终态后订阅 = 历史回放；404/400。

### 第 8 步：Web/CLI 客户端（`static/index.html` + `cli.py` + server 增强）

- 关键补充：浏览器 **EventSource 不能自定义请求头**，订阅端点增加 `?after_seq=N`
  查询参数通道（头优先、query 次之、默认 -1）。
- Web 页面：四按钮（创建/断开/重连/取消）对应四种能力；localStorage 保存
  run_id + 断点，刷新只恢复显示不自动重连；每事件推进 lastSeq 游标。
- CLI：创建模式与 `--resume RUN_ID --after N` 续读模式；正文走 stdout、元信息走
  stderr；Ctrl+C 只断订阅不取消 Run，退出前打印续读命令。
- 踩坑：cli.py 初稿留了一段半成品的猴子补丁死代码，整文件重写干净。
- 真实服务验证六项：CLI 全流程（TTFT=301ms 对应 Fake 0.3s 配置）、curl 强制断线后
  头/query 双通道续读数量精确、审计查询、取消终态、`--resume --after 50` 只收尾部。

### 第 9 步：异常测试（`tests/` 5 个文件，30 个）

- 覆盖：事件 roundtrip/frozen、SSE 往返/心跳/多行 data、Store 双守卫/补发/取消幂等、
  Loop 五场景、HTTP 全链路（含 422/404/400）。
- 裁判设计：`CountingModel` 包装统计 `stream()` 调用次数，断言断线/重连全程 `== 1`。
- 踩坑三处（都在测试自身）：fixture 普通 `def` 里用 `async with`；httpx 客户端没有
  `.state` 属性改 yield `SimpleNamespace`；多行调用没被字符串替换命中。
- **真实竞态发现**：cancel 响应返回时终态事件尚未落账，立刻查状态看到 running——
  不是 bug，是取消异步生效的本质；测试改为轮询等账本，并立下教学口径：
  *判断 Run 结局以账本为准，而非响应为准*。

### 第 10 步：数据库与 checkpoint（`gateway/sqlite_store.py`）

- `SqliteRunStore(RunStore)` 只覆写四个变更点（create/append/status/checkpoint），
  **loop.py 与 HTTP 层零改动**——第 5 步插槽兑现；三张表 runs/events/checkpoints，
  WAL 模式。
- **孤儿回收**：重启时仍处 running 的 Run 补 `run.failed(PROCESS_RESTARTED)` 终态，
  partial_text 取 checkpoint 最后进度，保证每个 Run 都有终态。
- 教学取舍入注释：sqlite3 同步写在事件循环（本地微秒级可接受）；先内存后落库
  （极端断电丢尾部，生产用 write-ahead）。
- 测试修三处：`checkpoint_every` 参数归属、孤儿模拟应直接留 running 在库
  （`task.cancel()` 会干净收尾写成 cancelled，不算"断电"）、一份 checkpoint 期望值算错。
- 真实杀进程演示：重启日志"恢复 2 个 Run（孤儿回收 1 个）"；已完成的 Run 跨进程
  完整回放 62 事件；孤儿补 PROCESS_RESTARTED、seq 仍稠密。全套测试 35 个全绿。

### 第 11 步：端到端验收（README + 验收清单）

- 环境变量统一进 `project/.env`（追加时只提取键名不读值，DEEPSEEK_API_KEY 原行未动）；
  server.py 顶部 `load_dotenv`（override=False：命令行优先）；`GATEWAY_DB=gateway.db`
  默认开持久化；`gateway.db*` 加入根 .gitignore。
- 验收插曲：`import gateway.server` 后 sys.modules 出现 openai，追查发现用户已把
  `.env` 的 `GATEWAY_MODEL` 改为 `deepseek`——**行为符合设计**（工厂延迟导入按预期
  触发），顺带证明 key 有效；据此修正验收断言前提，并按用户"不差钱"指示改用真实模型验收。
- 最终验收（真实 DeepSeek）：35 测试全绿；SDK 隔离双证明（grep 仅工厂一处 +
  fake 模式进程无 openai）；流式 TTFT=696ms；真实模型中途取消（230 delta 后即停、
  终态 cancelled、265 事件封账）；`Last-Event-ID: 1` 续读恰好补发 3 个事件。

### 会话中的两条经验线

- **IDE 集成**：PyCharm 对嵌套子项目需标记 Sources Root / Test Sources Root、
  解释器选 `project/.venv`；包内相对导入 + 命令从项目目录发出是根治方案。
- **验证纪律**：11 步中每次"断言失败"都先分清是被测代码错了还是验证脚本自己错了
  （后者占多数：参数归属、期望值、竞态前提），修复后重跑——不让错误的验证污染结论。
