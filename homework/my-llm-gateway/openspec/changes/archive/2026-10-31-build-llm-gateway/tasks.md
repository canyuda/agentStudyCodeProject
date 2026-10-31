# Tasks

## 1. 脚手架与配置体系

- [x] 1.1 创建 `pyproject.toml`（依赖：fastapi、uvicorn、httpx、jinja2、jsonschema、aiosqlite、pyyaml、python-dotenv；dev：pytest、pytest-asyncio、ruff）与 `src/gateway/` 包骨架，`.gitignore` 忽略 `.env`、`.env.*`、`data/`、`__pycache__/`；验证：`pip install -e ".[dev]"` 成功且 `python -c "import gateway"` 通过
- [x] 1.2 实现 `config.py`：YAML 加载、`${VAR}`/`${VAR:-默认值}` 展开（未定义且无默认值启动失败）、`GATEWAY_CONFIG` 路径解析与内置示例回退、Pydantic 结构校验（供应商表/协议类型枚举/路由引用/价格表/重试熔断限流参数）；验证：单测覆盖两种展开形式、示例回退、语法错误/缺必填项/路由引用未知供应商/未知协议类型 → 以非零码退出（Mock 场景 15）
- [x] 1.3 编写 `config/config.example.yaml`（含两家供应商、priority 与 weighted 各一别名、价格表、脱敏/检查点默认关闭）与 `.env.example`（占位密钥）；验证：无 `GATEWAY_CONFIG`、无 `.env` 时示例配置通过启动校验
- [x] 1.4 实现 `main.py` 应用工厂（启动期装配：配置→账本→路由→韧性模块）与 `errors.py` 统一错误对象（message/type/param/code）+ 全局异常处理器，以及无鉴权 `/healthz`、`/readyz`；验证：TestClient 断言健康检查 200 无需鉴权、错误响应均为统一形状

## 2. 中间表示、双适配器与路由

- [x] 2.1 定义 `ir.py` 中间表示（消息列表/采样参数/约束声明/stream 标志）与 Chat Completions 北向请求解析（Pydantic，含扩展的提示词引用字段占位）；验证：单测覆盖字段解析与非法请求 422
- [x] 2.2 实现 `adapters/base.py` 适配器协议接口与 `openai_responses.py`：Bearer 头、`instructions` 系统位、`input` 数组、`max_output_tokens`、json_schema 原生透传、`output_text` 文本提取、完成原因与 input/output tokens 反向映射、错误体归一；验证：MockTransport 断言发往上游的鉴权头/系统提示位置/消息结构与反向映射（Mock 场景 16、19 半）
- [x] 2.3 实现 `anthropic_messages.py`：`x-api-key` + 协议版本头、顶层 `system`、messages 块列表（仅 user/assistant）、`max_tokens` 必填兜底（未传注入安全默认值）、text 内容块与 `stop_reason` 反向映射、input/output（含缓存计数）用量解析、json_schema 转译为格式指令注入、错误体归一；验证：MockTransport 断言（Mock 场景 17、19 半）
- [x] 2.4 实现 `routing/`：别名表解析、priority 严格顺序与平滑加权轮询（其余路由为后备）、候选健康过滤（禁用/熔断剔除）、未知别名 404、无健康路由 503；验证：单测覆盖两种策略顺序、404/503、真实模型 ID 不出现在北向响应
- [x] 2.5 实现 `api/models.py`（`GET /v1/models` 仅返回公开别名）与 Bearer 网关密钥鉴权依赖（无效/缺失 401 统一错误）；验证：TestClient 断言 401（Mock 场景 9 半）、别名列表无真实模型 ID

## 3. 用量账本与非流式最小闭环

- [x] 3.1 实现 `ledger/store.py`：aiosqlite `usage_records` 表（规格字段全集）+ 调用完成写入（含已取消 499）+ 密钥 SHA-256 前 16 位十六进制短指纹；验证：单测断言字段齐全、指纹非原值、正文与密钥原值绝不出现在库与日志
- [x] 3.2 实现 `ledger/cost.py` 价格计算（每百万 Token 输入/输出单价、缓存命中折扣价、未配置记 0）；验证：单测覆盖三种计价分支
- [x] 3.3 实现 `pipeline.py` 非流式编排（鉴权后：限流占位→提示词渲染占位→脱敏占位→路由→重试循环占位→适配器调用→记账）与 `api/chat.py` `POST /v1/chat/completions` 非流式（响应头回传请求 ID、北向 model 回填公开别名）；验证：Mock 两协议各一次非流式调用成功，账本记录字段与数值一致（Mock 场景 1）

## 4. 韧性：重试、故障转移、熔断、限流

- [x] 4.1 实现 `resilience/retry.py`（指数退避 + ±25% 抖动、基准 0.25s 上限 4s 可配置、同路由总尝试 ≤3、可重试状态码集合 {408,409,429,500,502,503,504} + 网络错误/超时、普通 4xx 不重试）与故障转移（耗尽切下一候选、切换计数入账）；验证：Mock 场景 2（首选失败→重试→切换后备成功，重试/切换计数 >0 入账）+ 400 不重试单测
- [x] 4.2 实现 `resilience/breaker.py` 按供应商独立熔断（连续失败阈值默认 5、冷却默认 30s、半开放行试探、参数可配置、接口可替换）并接入候选过滤；验证：Mock 场景 11（打开被跳过、冷却后恢复）
- [x] 4.3 实现 `resilience/ratelimit.py` 令牌桶（维度 = 密钥指纹 + 别名、RPM 与突发可配置、接口可替换）并接入管道（鉴权后、渲染前）；验证：Mock 场景 10（超限 429 统一错误体、密钥间与别名间互不影响）

## 5. 流式输出

- [x] 5.1 实现两适配器的流式事件翻译：上游 SSE 事件 → 北向 Chat SSE 块（增量/结束原因/usage），TTFT 在首个内容字节转发时打点；验证：Mock 两协议流式事件序列统一转换断言（Mock 场景 18）+ 流中 usage 解析入账（场景 6 半）
- [x] 5.2 实现 `api/chat.py` 流式路径：`StreamingResponse` 逐块下发不整包缓冲、响应头含请求 ID/no-cache/禁代理缓冲、账本含 TTFT；验证：Mock 场景 6（逐块 + 响应头 + TTFT 入账）
- [x] 5.3 实现首字节前/后差异化语义：首字节前失败走重试切换；首字节后中断发 SSE 错误事件 + 流结束标记收尾、绝不换模型续写；验证：Mock 场景 7
- [x] 5.4 实现客户端断开止损：generator 取消时关闭上游连接、记账状态=已取消（499）、不再产生用量；验证：Mock 场景 8

## 6. 结构化输出

- [x] 6.1 实现 `structured/validate.py`：Markdown 围栏剥离 + `jsonschema` 本地二次校验（输出精确错误路径与原因）；验证：单测覆盖围栏容错与错误定位
- [x] 6.2 实现 `structured/repair.py` 非流式修复闭环（assistant 回填 + 精确校验错误 + 完整 Schema 修复指令、轮次默认 1 上限 3 可配置、超限 422）；验证：Mock 场景 3（修复成功）与场景 4（超限 422）
- [x] 6.3 流式路径 Schema 只透传不纠错；验证：单测断言流式 json_schema 请求不触发修复，README 边界声明在 10.x 落盘
- [x] 6.4 Anthropic 无原生约束协议的端到端：指令注入 + 本地校验 + 修复闭环返回合法 JSON；验证：Mock 场景 20

## 7. 提示词版本管理

- [x] 7.1 实现 `prompts/store.py`（同 ID 再创建即新版本、激活版本设置、默认取激活/显式版本、不存在 404）与 `api/prompts.py`（创建/列表/查询/设激活/渲染预览端点）；验证：Mock 场景 5 的多版本生命周期部分
- [x] 7.2 实现 `prompts/render.py`：Jinja2 `SandboxedEnvironment` + `StrictUndefined`，缺变量 422 指明变量名、危险语法被沙箱拦截；验证：Mock 场景 5 的缺变量 422 与模板注入拦截部分
- [x] 7.3 实现请求侧提示词引用注入（ID+变量+可选版本+前置系统位/后置末尾），渲染结果进入 IR 再由适配器翻译到 `instructions`/顶层 `system`，账本记录提示词 ID 与版本；验证：Mock 断言上游收到注入后消息序列 + 账本含版本（对应真实验收第 4 项的 Mock 等价）

## 8. 数据脱敏

- [x] 8.1 实现 `masking/rules.py` 表驱动五类规则（密钥/银行卡 Luhn/身份证校验码/手机号非数字边界/邮箱）与执行顺序（长规则优先），每类可启停可扩展；验证：单测覆盖五类识别、Luhn/校验码降误报、长短规则不截断
- [x] 8.2 实现 `masking/masker.py`（占位符 `⟦TYPE_N⟧`、同值同占位符、请求级 contextvar 映射、请求结束丢弃）并接入管道（渲染后、路由前；对象限消息正文与模板变量值）；验证：Mock 场景 12 的请求侧 + 同值同占位符单测
- [x] 8.3 实现非流式还原与结构化 JSON 字符串值还原（还原后仍合法且过 Schema）；验证：Mock 场景 12 的响应侧 + 场景 14 的 JSON 还原
- [x] 8.4 实现流式还原器 `masking/restorer.py`：待定尾部缓冲（不可能构成占位符前缀才下发）、跨块占位符完整还原、丢失占位符原样保留；验证：Mock 场景 13（跨 SSE 块切开仍还原）
- [x] 8.5 修复回路与红线：修复指令只携带脱敏后输出、账本可选按类型计数命中数、任何日志无原值/映射表；验证：Mock 场景 14 的修复回路全程只见占位符

## 9. Responses 北向入口与流检查点

- [x] 9.1 实现 `api/responses.py`：`POST /v1/responses` 请求（instructions/input）解析为 IR 进入统一管道，响应反向组装为 Responses 格式（output_text/完成原因/usage），流式以 SSE 事件返回；验证：Mock 场景 9（Responses 入口正常调用；401 已由 2.5 覆盖）
- [x] 9.2 实现 `checkpoint/` 可选流检查点（默认关闭、关闭时查询 404；开启后按间隔批量保存已还原文本增量、最大字符上限、状态机 流式中/成功/失败/已取消）；验证：单测覆盖默认 404、开启后断线找回、上限截断、状态流转

## 10. 交付物：文档、验证脚本与证据

- [x] 10.1 编写 README：项目说明、启动指南（依赖安装、`.env` 与主配置准备、启动命令与端口、健康检查）、全部能力边界声明（流式不修复、模板不能消除提示词注入、脱敏同形字符串限制、检查点隐私警示与不突破上下文窗口、include_usage 提示、熔断限流单机语义）、覆盖主要端点的 curl 示例（非流式、`curl -N` 流式、结构化输出、模板创建与引用、用量查询）；验证：按 README 从零执行启动步骤可跑通全部 curl 示例
- [x] 10.2 编写 `scripts/verify_mock.sh` 一键全量 Mock 验证（跑 pytest 并汇总 20 场景逐项结果）；验证：脚本一键执行退出码 0
- [x] 10.3 编写 `scripts/verify_real.py` 真实验收脚本：读 `.env` 密钥，对 12.2 七项逐项打印 PASS/FAIL 与关键数据（请求 ID、Token、延迟、块数、模板版本、重试/切换计数、429），任一 FAIL 非零退出，完整输出留存 `out/verify_real_*.txt`；验证：`--dry-run` 模式（不消耗额度）自检脚本结构完整
- [x] 10.4 配置故意失效首选路由 + 真实后备的演示配置与限流极低的演示配置（供验收第 6/7 项使用），并把真实 DeepSeek 端点与模型 ID 确认后写入 `.env` 与主配置；验证：配置加载校验通过且不泄露密钥到任何被跟踪文件（`git status` 复核）

## 11. 最终集成检查

- [x] 11.1 全量 Mock 回归：`pytest` 20 个必测场景全部通过、`ruff check` 静态检查零告警；验证：命令输出全绿
- [x] 11.2 真实调用验收：运行 `scripts/verify_real.py` 七项全部 PASS，证据文件留存 `out/`，输出中无任何敏感原值；验证：脚本退出码 0 且证据文件存在
