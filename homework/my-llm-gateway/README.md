# my-llm-gateway — 统一 LLM 网关

面向 Agent 平台的统一模型入口：对上暴露 OpenAI Compatible API（Chat Completions + Responses 双入口），对下经适配器连接两种**上游协议不同**的模型供应商（OpenAI Responses API / Anthropic Messages API），网关层集中提供模型路由、流式转发、结构化输出校验、提示词版本管理、用量记账、重试/故障转移/熔断/限流与数据脱敏。

```
调用方 (OpenAI SDK / HTTP)
   │  Bearer 网关密钥
   ▼
┌─────────────────────── 北向 API ───────────────────────┐
│ /v1/chat/completions   /v1/responses   /v1/models     │
│ /v1/prompts/*          /admin/usage     /admin/routes  │
└──────────────────────────┬─────────────────────────────┘
                           ▼ 中间表示 IR（消息+采样参数+约束）
   鉴权 → 限流 → 提示词渲染 → 脱敏(可选) → 别名路由
                           ▼
   ┌── 适配器 A：OpenAI Responses（/responses，Bearer）──┐
   └── 适配器 B：Anthropic Messages（/messages，x-api-key）┘
                           ▼
        响应还原(脱敏) → 客户端；每次调用写入用量账本(SQLite)
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+，建议 3.12）
uv venv .venv && uv pip install -p .venv/bin/python -e ".[dev]"
#    或: python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. 准备密钥（.env 已被 .gitignore 忽略，绝不入库）
cp .env.example .env
#    编辑 .env：DEEPSEEK_API_KEY=<你的 DeepSeek 平台密钥>、GATEWAY_API_KEY=<自定网关密钥>

# 3. 启动（未设置 GATEWAY_CONFIG 时使用内置示例配置，开箱可跑）
.venv/bin/python -m uvicorn gateway.main:app --port 8000
#    自定义配置：GATEWAY_CONFIG=config/your.yaml .venv/bin/python -m uvicorn gateway.main:app

# 4. 健康检查（无鉴权，供容器探活）
curl http://127.0.0.1:8000/healthz
```

- 交互式 API 文档：`http://127.0.0.1:8000/docs` （OpenAPI）
- 配置支持 `${VAR}` 与 `${VAR:-默认值}` 两种环境变量展开；密钥**只**经环境变量注入
- 所有配置校验在启动时完成，失败即退出（fail fast）

以下示例使用示例配置中的公开别名 `deepseek-pro`（上游 OpenAI Responses 协议）与 `deepseek-flash`（上游 Anthropic 协议）。

## curl 示例

```bash
# 网关密钥从 .env 读取（示例中的 gw-demo-key-change-me 仅在 .env 未设置该变量时生效）
export KEY=$(grep '^GATEWAY_API_KEY=' .env | cut -d= -f2)
```

### 1) 非流式调用

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-pro",
    "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "一句话介绍网关"}]
  }'
```

### 2) 流式调用（SSE，逐块返回）

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "deepseek-flash", "messages": [{"role": "user", "content": "数到三"}], "stream": true}'
```

### 3) 结构化输出（JSON Schema 约束 + 本地校验 + 自动修复）

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-pro",
    "messages": [{"role": "user", "content": "把工单分类：我被重复扣款了"}],
    "response_format": {"type": "json_schema", "json_schema": {"name": "team", "schema": {
      "type": "object", "properties": {"team": {"type": "string", "enum": ["billing","support","sales"]}},
      "required": ["team"], "additionalProperties": false}}}
  }'
```

### 4) 提示词模板：创建 → 渲染预览 → 引用调用

```bash
# 创建（同一 ID 再创建即产生新版本；首个版本自动激活，可用 activate 切换）
curl -s -X POST http://127.0.0.1:8000/v1/prompts \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"prompt_id": "classifier", "template": "你把工单归入 {{sets}} 之一，只输出类别名。"}'

# 渲染预览（缺变量返回 422 并指出变量名，绝不静默渲染成空串）
curl -s -X POST http://127.0.0.1:8000/v1/prompts/classifier/render \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"variables": {"sets": "billing|support"}}'

# 携带提示词引用发起调用（渲染结果按 position=system 前置注入，或 suffix 后置）
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model": "deepseek-pro", "messages": [{"role": "user", "content": "我被重复扣款了"}],
       "prompt_ref": {"id": "classifier", "variables": {"sets": "billing|support"}}}'
```

### 5) 用量查询与管理

```bash
# 分页查询最近用量（含 Token 分类、成本、延迟、TTFT、重试/切换计数、模板版本）
curl -s "http://127.0.0.1:8000/admin/usage?limit=5" -H "Authorization: Bearer $KEY"

# 路由表 + 各供应商熔断状态
curl -s http://127.0.0.1:8000/admin/routes -H "Authorization: Bearer $KEY"

# 公开别名列表（真实上游模型 ID 绝不出现）
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $KEY"
```

## 端点一览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/chat/completions` | POST | Chat Completions 兼容入口（流式/非流式） |
| `/v1/responses` | POST | Responses 协议第二入口（共用同一管道） |
| `/v1/models` | GET | 公开别名列表 |
| `/v1/prompts`、`/v1/prompts/{id}`(/activate /render) | POST/GET | 提示词创建/列表/查询/激活/渲染预览 |
| `/admin/usage` | GET | 分页查询用量记录 |
| `/admin/routes` | GET | 路由表与熔断状态 |
| `/v1/streams/{request_id}/checkpoint` | GET | 流检查点（默认关闭） |
| `/healthz`、`/readyz` | GET | 无鉴权探活 |

每个响应头回传 `X-Request-Id`，供对账与检查点查询。所有错误为统一 OpenAI 风格对象 `{"error": {message, type, param, code}}`；状态码语义：401 密钥无效 / 404 不存在 / 422 校验失败 / 429 限流 / 502 上游失败 / 503 无健康路由。上游 4xx 状态码原样透传（附上游详情），上游 5xx 归一为 502。

## 能力边界声明（重要）

- **流式不做结构化修复**：已下发的内容无法撤回，流式请求的 Schema 只透传、不本地纠错；严格业务协议请使用非流式接口。
- **include_usage 提示**：流式调用只有在上游返回 usage 事件时才能精确记账（Chat 入口默认在末块带用量）；若上游未返回，Token 记为 0。
- **模板不能消除提示词注入**：模板降低重复与配置漂移，生产仍需隔离不可信输入、限制工具权限、把模型输出当不可信数据。
- **脱敏的同形限制**：用户有意发送的同形字符串（测试样例、代码片段）也会被替换，属可接受行为；模型改写/丢失占位符时该占位符原样保留，**绝不**把原值重新发给模型修补。脱敏映射表仅存在于请求内存作用域，不写日志/账本/任何持久化存储。
- **流检查点会持久化模型正文**：开启前必须先确定加密、权限与数据保留策略；它解决断线找回，**不突破模型上下文窗口限制**——超长任务仍应由上层 Agent 分段、摘要或检索式续写。
- **熔断器与限流器为单进程内存态**：多副本部署需替换为 Redis 等共享存储（两者已隔离为可替换模块）。
- **账本隐私红线**：消息正文与提示词正文绝不写入用量表；密钥仅存 16 位不可逆短指纹；日志不输出密钥原值。

## 验证

```bash
# Mock 全量验证（内存伪造上游，双协议真实报文格式，20 个必测场景；不消耗真实额度）
./scripts/verify_mock.sh

# 真实调用验收（读 .env 真实密钥；七项逐项 PASS/FAIL，任一 FAIL 非零退出）
#   先用验证配置启动网关（含故意失效的首选路由，供第 6 项重试验证）：
GATEWAY_CONFIG=config/config.verify.yaml .venv/bin/python -m uvicorn gateway.main:app --port 8000 &
.venv/bin/python scripts/verify_real.py            # 输出留存 out/verify_real_*.txt
.venv/bin/python scripts/verify_real.py --dry-run  # 只做结构与配置自检，不发真实请求
```

真实端点说明：`config/config.verify.yaml` 中两家供应商的 `base_url` 与真实模型 ID（`deepseek-v4-pro` / `deepseek-v4-flash`）按 DeepSeek 平台实际文档调整；协议类型（`openai_responses` / `anthropic_messages`）不可改——协议差异适配是本项目训练重点。

## 目录结构

```
src/gateway/
├── main.py            # 应用工厂（启动期装配与校验）
├── config.py          # YAML + ${VAR} 展开 + fail-fast 校验
├── ir.py              # 中间表示（消息 + 采样参数 + 约束声明）
├── pipeline.py        # 编排：渲染→脱敏→路由→重试/切换→修复→还原→记账
├── errors.py          # 统一 OpenAI 风格错误封装
├── api/               # 北向端点（chat/responses/models/prompts/admin/streams/health）
├── adapters/          # openai_responses.py + anthropic_messages.py（含流式翻译/错误归一）
├── routing/           # 别名路由（priority / 加权）
├── resilience/        # retry / breaker / ratelimit（隔离可替换）
├── prompts/           # 版本存储 + 沙箱渲染
├── structured/        # 本地 JSON Schema 校验 + 修复闭环
├── masking/           # 表驱动规则 + 占位符往返 + 流式尾部缓冲还原
├── ledger/            # aiosqlite 用量账本 + 成本计算
└── checkpoint/        # 可选流检查点（默认关闭）
tests/                 # Mock 上游（双协议真实报文）+ 20 场景
scripts/               # verify_mock.sh / verify_real.py
```
