# Design

## Context

空白仓库，从零构建（见 proposal.md - Why）。唯一信息源为《LLM 网关复刻需求文档》，其外部契约（端点、字段、两种上游协议要素）已固化在 11 个能力规格中；本设计只决定实现方式。约束：单进程部署、密钥只经 `.env` 注入、Mock 测试不消耗真实额度、熔断/限流须隔离为可替换模块。

## Goals / Non-Goals

**Goals:**
- 分层清晰：北向入口 → 中间表示（IR）→ 路由 → 适配器 → 上游；新增第三种上游协议只加一个适配器文件
- 两种上游协议（OpenAI Responses / Anthropic Messages）的请求改写、流式事件归一、错误归一、用量映射全部收敛在适配器内
- 20 个 Mock 场景 + 7 项真实验收可用一套代码跑通，Mock 与真实仅切换 HTTP 传输层
- 熔断器、限流器为独立模块接口，单机内存实现可被 Redis 实现替换

**Non-Goals:**
- 多副本部署的一致性（熔断/限流/账本均为单机语义）
- 高吞吐优化（同步单条写入 SQLite 足够）
- `/v1/responses` 北向入口的工具调用、内置工具等高级特性（仅覆盖与 Chat 等价的核心字段）
- 生产化加固（TLS/WAF/多租户），仅在文档声明

## Decisions

### D1 技术栈
Python 3.10+；FastAPI + uvicorn（SSE 用 `StreamingResponse`）；httpx.AsyncClient（超时/连接池，Mock 用 `httpx.MockTransport` 注入）；Jinja2 `SandboxedEnvironment(undefined=StrictUndefined)`；`jsonschema` 校验；aiosqlite 账本与提示词存储；pytest + pytest-asyncio。备选 Flask/aiohttp 被否：FastAPI 原生满足 OpenAPI 文档与 SSE，生态最贴合文档建议。加载 `.env` 用 python-dotenv（仅启动时读一次）。

### D2 模块布局（仓库根）
```
src/gateway/
├── main.py          # app 工厂；启动期加载+校验配置，失败即 sys.exit(非零)
├── config.py        # YAML 加载、${VAR}/${VAR:-默认} 展开、结构校验（Pydantic 模型）
├── errors.py        # 统一错误对象 + 上游错误归一映射表 + 全局异常处理器
├── ir.py            # 中间表示：ChatRequestIR(messages, sampling, constraint, stream)
├── pipeline.py      # 编排：渲染→脱敏→路由→调用→还原→记账（纯函数组合，状态经 Context 对象传递）
├── api/             # 路由层：chat.py / responses.py / models.py / prompts.py / admin.py / streams.py / health.py
├── adapters/        # base.py(协议接口) + openai_responses.py + anthropic_messages.py
├── routing/         # 别名表、priority/加权策略、健康候选过滤
├── resilience/      # retry.py / breaker.py / ratelimit.py（接口 + 单机内存实现）
├── prompts/         # store.py(SQLite 版本表) + render.py(沙箱渲染)
├── structured/      # validate.py(围栏剥离+jsonschema) + repair.py(修复闭环)
├── masking/         # rules.py(表驱动规则) + masker.py + restorer.py(流式尾部缓冲)
├── ledger/          # store.py(用量表) + cost.py(价格计算)
└── checkpoint/      # 可选流检查点（内存 + 落盘由配置决定，默认关闭）
```
函数式优先：转换逻辑（IR↔协议、脱敏、还原）写成纯函数；副作用（IO、记账）收敛在 pipeline 与 store 边界。启动校验 fail fast，任何配置错误打印可定位信息后退出。

### D3 中间表示与适配器接口
`ir.py` 定义统一请求：消息列表（role/content）、采样参数（temperature/top_p/max_tokens 等）、约束声明（json_schema 或空）、stream 标志。北向两入口各自解析为 IR；适配器接口两个方向：
- `build_request(ir, provider_cfg) -> (url, headers, json_body)`：注入各协议鉴权头与系统提示位置
- `parse_response / stream_events(上游响应或事件流) -> 北向响应或 SSE 块生成器`

Anthropic `max_tokens` 必填：IR 未携带时适配器注入安全默认值（如 4096，可配置）。Responses 协议透传 json_schema；Anthropic 转译为格式指令（含完整 Schema 的 JSON 文本 + 「只输出 JSON」指令）追加到系统提示。TTFT 在流式转换层打点（首个内容字节转发时）。

### D4 路由与故障转移
配置结构：`aliases: {别名: {strategy: priority|weighted, routes: [{provider, model, weight}]}}`。priority 严格按序；weighted 用平滑加权轮询选首选，其余按配置序作后备。候选过滤：供应商禁用或熔断打开的剔除；全部剔除 → 503；别名不存在 → 404。真实模型 ID 只进适配器与账本，北向响应 `model` 字段回填公开别名。

### D5 重试语义（消歧）
文档 §2「最多3次」与 §7.2「总尝试次数上限 3 次」统一解释为：**同一候选路由总尝试次数 ≤ 3**（首次 + 最多 2 次重试），基准 0.25s、倍增、上限 4s、±25% 抖动，均可配置。可重试集合默认 {408,409,429,500,502,503,504} + 网络错误/超时；其余 4xx 不重试。路由耗尽 → 下一候选；候选耗尽 → 终局错误（上游 5xx→502、上游限流→429、上游 4xx 透传最后一次）。

### D6 熔断与限流的隔离
`resilience/` 定义抽象接口（`CircuitBreaker` / `RateLimiter`），提供单机内存实现（按供应商连续失败计数熔断：阈值 5、冷却 30s、半开放行试探；令牌桶：维度 = 密钥指纹+别名，RPM + 突发容量）。app 启动时以依赖注入装配，多副本时替换为 Redis 实现而不动调用方。

### D7 流式管道
`StreamingResponse` + async generator：上游 `client.stream()` 逐事件读 → 适配器翻译为北向 chunk → （脱敏开启时）经 restorer → yield。关键语义：
- **首字节前**失败：generator 尚未产出，完整走重试/切换；
- **首字节后**失败：抛出流中错误事件（SSE data 携带统一错误对象）+ `[DONE]` 收尾，绝不换路由；
- **客户端断开**：FastAPI 取消 generator → `finally` 中关闭上游响应（`.aclose()`），记账状态=已取消（499）。
TTFT = 请求开始到第一个内容块 yield 的时刻；usage 从上游完成事件解析。

### D8 脱敏与流式还原
规则表驱动（列表顺序即执行顺序：密钥→银行卡→身份证→手机→邮箱），每条规则 = 名称 + 启停 + 正则 + 可选校验器（身份证校验码、银行卡 Luhn）。占位符设计：`⟦TYPE_N⟧`（U+27E6/U+27E7 数学白方括号 + 类型 + 序号，如 `⟦EMAIL_1⟧`）——自然文本几乎不出现、UTF-8 安全、SSE 可穿越、模型极少改写。请求级 `MaskContext`（contextvar）持有 原值↔占位符 双向映射，请求结束丢弃；对消息正文与模板变量值脱敏。流式还原器维护待定尾部缓冲：仅当缓冲尾部不可能是任何占位符前缀（按 `⟦` 出现位置判断）才下发；未匹配占位符原样保留。修复回路输入（模型上轮输出）不还原——模型全程只见占位符。

### D9 用量账本与提示词存储
同一个 aiosqlite 文件（默认 `data/gateway.db`），两张表：`usage_records`（规格字段全集，密钥存 SHA-256 前 16 hex 指纹）、`prompts`（prompt_id, version, template, is_active, created_at，(prompt_id, version) 唯一）。调用完成时同步写一条（含已取消 499）。成本：按别名价格表（input/output/cache 每百万 Token 单价）计算，未配置记 0。

### D10 配置与 .env
`GATEWAY_CONFIG` 环境变量指定 YAML 路径；未设置时用内置 `config/config.example.yaml`（Mock 友好、开箱可跑）。启动顺序：python-dotenv 读根目录 `.env` → 展开 `${VAR}`/`${VAR:-默认值}`（正则替换，未定义且无默认值即启动失败）→ Pydantic 结构校验（含路由引用、协议类型枚举）→ 装配。DeepSeek API Key 只存在 `.env`（`DEEPSEEK_API_KEY=...`），主配置写 `${DEEPSEEK_API_KEY}`；`.gitignore` 含 `.env` 与 `.env.*`，另提供 `.env.example`（占位值）。

### D11 Mock 测试与真实验证
- `tests/upstreams/` 用 `httpx.MockTransport` 实现两个假上游，按两种协议的**真实报文格式**伪造（含 SSE 事件序列：Responses 的 `response.output_text.delta`/`response.completed`，Anthropic 的 `content_block_delta`/`message_delta`），支持脚本化故障注入（超时、5xx、流中中断、限流）。适配器收到的上游请求可断言（鉴权头、instructions/system 位置、max_tokens 兜底等）。
- `tests/` 按 20 个场景组织（场景编号与规格对齐）。
- `scripts/verify_mock.sh`：跑全量 pytest。
- `scripts/verify_real.py`：读 `.env` 真实密钥，对 12.2 七项逐项打印 PASS/FAIL + 关键数据（请求 ID、Token、延迟、块数），任一 FAIL 以非零码退出；完整输出 tee 到 `out/verify_real_YYYYMMDD.txt` 留证。脱敏开启时输出不得出现敏感原值。

## Risks / Trade-offs

- [V4 pro / V4 flash 为假想模型，真实上游 base URL 与模型 ID 不可预知] → 全部配置化；验证脚本对配置失败给出明确报错；Mock 测试不依赖真实端点
- [占位符被模型改写/丢失导致还原失败] → 选用罕见定界符降低概率；规格已声明「原样保留」为已知限制，绝不回发原值
- [SSE 取消传播不及时导致上游继续消耗] → generator finally 关闭上游流 + httpx 超时兜底；Mock 场景 8 直接断言上游连接关闭
- [修复闭环放大延迟与成本] → 轮次上限默认 1、最大 3，账本记录重试计数可见
- [Jinja2 沙箱并非绝对安全] → StrictUndefined + SandboxedEnvironment + 模板来源仅限 `/v1/prompts` 创建者；文档声明不能消除提示词注入
- [单机 SQLite 写入与请求并发] → WAL 模式 + 单写者串行化；吞吐非目标
- [`/v1/responses` 北向入口为额外范围] → 只实现与 Chat 等价的核心翻译（instructions/input/output_text/usage），不实现工具调用等高级特性

## Migration Plan

全新项目，无迁移。启动：安装依赖 → 准备 `.env` 与配置 → `uvicorn gateway.main:app`；回滚即停止进程删除工作目录数据库。

## Open Questions

- 真实 DeepSeek 两协议端点的准确 base URL 与模型 ID 字符串：验证阶段从平台文档/控制台确认后改配置即可，不影响代码与任务拆分。
