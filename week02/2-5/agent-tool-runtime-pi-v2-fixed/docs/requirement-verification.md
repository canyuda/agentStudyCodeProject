# 需求逐句验证矩阵

| 原始要求 | 实现位置 | 自动或人工验证 |
|---|---|---|
| 基于 pi，不再自研 Agent Loop | `runtime/src/pi/agent-runner.ts`、`coding-agent-extension.ts` | `npm run build`；源码检查 `runAgentLoop`/`registerTool` |
| 阶段一 OpenAI-compatible API | `gateway/app/main.py` | `/v1/models`、`/v1/chat/completions` 冒烟 |
| 网关使用 LiteLLM | `gateway/app/service.py` | 依赖固定 `litellm==1.76.1`；真实测试调用 `litellm.acompletion` |
| 多模型路由与降级 | `config/gateway.yaml`、`service.py` | flash 重试后切 pro；流已经发出则不切换 |
| Streaming | `main.py`、`service.py` | OpenAI SSE 与 `[DONE]` |
| Structured Output | `ChatCompletionRequest.response_format` | 原样经白名单请求模型 |
| Prompt 模板管理 | `config/prompts.yaml`、`PromptRegistry` | 版本、严格变量、内容哈希测试 |
| Token、Cost、Latency | `GatewayRepository` | `llm_calls` 与 `/internal/usage` |
| 错误、重试、限流 | `LiteLLMGateway` | 稳定错误码、有限重试、Semaphore/RPM |
| ToolRegistry | `runtime/src/registry.ts` | 注册、重名、模型名碰撞单测 |
| ToolSchema | `runtime/src/domain.ts` | 元数据包含风险、权限、超时、重试、来源等 |
| JSON Schema / Pydantic | `OrderStatusInput`、导出脚本、pi validation | Python 与 Node 两端拒绝非法参数 |
| Function Calling | pi `AgentTool` 与 Gateway tools 透传 | 真实 DeepSeek `test:live` |
| 权限与风险 | `policy.ts`、`platform.yaml` | RBAC、Agent 白名单、租户范围、plan 模式测试 |
| 审计 | `store.ts`、`runtime.ts` | 允许、拒绝、确认、异常均落 SQLite；敏感字段脱敏 |
| 超时、重试、降级 | `runtime.ts` | 幂等限定、指数退避、AbortSignal、fallback 单测 |
| MCP Client | `mcp/adapter.ts` | 正式 `my-coffee` Streamable HTTP；本地 stdio 协议测试 |
| MCP 必须使用指定服务器 | `config/platform.yaml` | 服务器名、URL、Authorization 占位逐项比对 |
| Tool Trace | `store.ts`、`agent-runner.ts` | pi 事件与 Runtime 事件共用 trace_id |
| Harness 与 Loop | `LoopGuard`、Policy、Runtime | 步数、工具数、重复调用、Token、Cost、时长、审批停止 |
| TDD | `runtime/tests`、`gateway/tests`、`tests/live` | 确定性测试不模拟模型；Loop 只做真实 DeepSeek 测试 |
| 人与模型职责分离 | 逐字稿“人、模型、pi 各做什么” | 检查提示词、代码与验收表 |
| CLI、配置、审批 API | `cli.ts`、YAML、`approval-server.ts` | CLI 与 HTTP 冒烟 |

## 凭据相关边界

代码完整实现了真实调用路径，但压缩包不含用户密钥。没有 `DEEPSEEK_API_KEY` 时，真实 Loop 测试以退出码 2 失败；没有 `LUCKIN_MCP_TOKEN` 时，真实 MCP 发现测试同样失败。失败是验收条件，不允许静默替换为 Fake Provider 或其他 MCP Server。
