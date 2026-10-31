# Proposal

## Why

Agent 平台需要一个统一模型入口：调用方只持网关密钥、面向一套 OpenAI 兼容 API，而网关在下方对接两家**上游协议不同**的模型供应商（DeepSeek v4 pro 走 OpenAI Responses API、DeepSeek V4 flash 走 Anthropic Messages API），并集中提供路由、流式转发、结构化输出、提示词版本管理、用量记账、韧性与数据脱敏。本仓库目前为空白项目，需按《LLM 网关复刻需求文档》从零复刻该服务端。

## What Changes

- 新建 Python 3.10+ / FastAPI 服务（代码置于仓库根，新增 `src/`、`tests/`、`config/`、`scripts/` 等目录），从零实现全部网关能力。
- 北向契约：`/v1/chat/completions`（流式 + 非流式）、`/v1/models`（仅公开别名）、`/v1/prompts` 系列、`/admin/usage`、`/admin/routes`、健康/就绪检查、交互式 OpenAPI 文档；**含 `/v1/responses` 第二北向入口**（需求文档 §3.1 与 §12.1 场景 9 矛盾，已与用户确认：实现）。
- 统一抽象层：北向请求解析为中间表示（消息列表 + 采样参数 + 约束声明），双适配器完成「中间表示 ↔ Responses / Anthropic 协议」双向翻译（含流式事件归一为北向 SSE 块、错误归一、用量解析、结构化输出适配差异）。
- 模型别名路由：priority / 加权两种策略、路由失败语义（未知别名 404、无健康路由 503）、启动期配置 fail-fast 校验。
- 三大能力：SSE 流式（TTFT、首字节前可重试切换、首字节后中断只收尾不换模型、客户端断开取消）、结构化输出（本地二次校验 + 自动修复闭环）、提示词版本管理（沙箱渲染 + 严格未定义变量检查）。
- 可观测性：嵌入式 SQLite 用量账本（Token 分类统计、成本、延迟、重试/切换计数、密钥指纹），管理端分页查询。
- 韧性：统一 OpenAI 风格错误封装、指数退避重试（同路由上限 3 次）、故障转移、按供应商熔断、按「密钥 + 别名」令牌桶限流（429）。
- 可开关数据脱敏：五类规则表驱动、占位符往返还原、流式跨块还原的尾部缓冲。
- 可选流检查点（默认关闭）。
- 交付物：README（启动指南 + curl 示例 + 能力边界声明）、Mock 全场景 pytest（内存伪造上游，按两种协议真实报文格式）、真实验证脚本（读 `.env` 密钥，逐项 PASS/FAIL 输出、非零退出码）。

## Capabilities

### New Capabilities

- `north-api`: 北向 API 契约 —— 端点清单、Bearer 网关密钥鉴权（401）、统一错误封装、请求 ID 响应头、`/v1/models` 别名不泄露、OpenAPI 文档、无鉴权探活。
- `adapters`: 统一抽象层 —— 中间表示、OpenAI Responses / Anthropic Messages 双适配器的请求改写与反向映射、流式事件统一翻译、错误归一、用量字段映射。
- `model-routing`: 模型别名路由表 —— priority / 加权策略、候选路由故障转移顺序、路由失败语义（404/503）、启动期引用与协议类型校验。
- `streaming`: 流式输出 —— SSE 逐块转发、TTFT 记录、首字节前/后差异化失败语义、客户端断开取消与 499 记账、流式 usage 解析、流式响应头。
- `structured-output`: 结构化输出 —— `json_schema` 按协议适配（原生透传 / 指令注入）、网关本地二次校验、代码围栏容错、修复闭环（默认 1 轮、上限 3 轮、422 终局）、流式不修复边界。
- `prompt-templates`: 提示词版本管理 —— 创建即新版本、激活版本、按版本引用、沙箱渲染 + 严格未定义变量（422）、渲染预览、请求侧引用注入、账本记录版本。
- `usage-ledger`: 用量账本 —— 字段全集（协议/供应商/别名/真实模型/Token 分类/成本/延迟/TTFT/重试切换/状态/提示词版本）、密钥指纹、隐私红线、成本计算、管理查询端点。
- `resilience`: 韧性 —— 指数退避 + 抖动重试（可重试状态码集合）、故障转移计数、按供应商熔断（阈值/冷却）、按密钥 + 别名令牌桶限流（429）。
- `data-masking`: 数据脱敏 —— 五类规则表驱动与执行顺序、占位符设计与请求内映射、流式跨块还原尾部缓冲、与修复回路/账本/检查点的交互红线。
- `stream-checkpoint`: 流检查点（默认关闭）—— 批量保存增量、按请求 ID 查询、状态机、隐私警示边界。
- `configuration`: 配置体系 —— YAML + `${VAR}` / `${VAR:-默认}` 环境变量展开、密钥仅经 `.env` 注入、环境变量指定配置路径、内置示例配置回退、启动期全量校验失败即退出。

### Modified Capabilities

（无 —— 空白项目，全部为新建能力。）

## Impact

- **代码**：仓库根新增全部源码（`src/` 应用分层、`tests/` Mock 测试、`config/config.example.yaml` 示例配置、`scripts/` 验证脚本、`.env.example`）；不涉及存量代码修改。
- **API**：对外新增上表全部端点；上游仅出站调用 DeepSeek 两协议端点。
- **依赖**：fastapi、uvicorn、httpx、jinja2（SandboxedEnvironment）、jsonschema、aiosqlite、pyyaml、pytest（含 async 插件）、respx 或等价 Mock 传输层。
- **密钥**：DeepSeek API Key 仅写入根目录 `.env`（`.gitignore` 忽略 `.env` 与 `.env.*`），主配置经环境变量引用注入；代码、配置样例、文档均不出现真实密钥。
- **验收**：12.1 全部 20 个 Mock 场景 + 12.2 七项真实验收须可执行并留存证据；静态检查与测试全绿。
