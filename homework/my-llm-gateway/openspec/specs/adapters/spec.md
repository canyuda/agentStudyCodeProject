# adapters Specification

## Purpose

定义统一抽象层的行为契约：北向请求解析为中间表示，OpenAI Responses 与 Anthropic Messages 两种上游协议适配器完成双向协议翻译（请求改写与鉴权注入、流式事件归一、错误归一、用量映射、结构化输出适配差异），保证未来新增协议只加适配器。

## Requirements

### Requirement: 中间表示与适配器边界
北向 Chat Completions / Responses 请求 SHALL 先解析为中间表示（消息列表 + 采样参数 + 约束声明）；每个适配器 SHALL 仅负责「中间表示 ↔ 单一上游协议」的双向转换。新增第三种上游协议时 MUST NOT 要求改动北向入口、路由与其他分层的行为。

#### Scenario: 两个北向入口归一到同一管道
- **WHEN** 相同别名分别从 Chat Completions 与 Responses 两个北向入口发起语义相同的请求
- **THEN** 两者被归一处理并路由到同一适配器，返回语义一致的结果

### Requirement: OpenAI Responses 协议正向改写与反向映射
Responses 适配器向上游 `POST /v1/responses` 发送请求时 SHALL：
- 使用 `Authorization: Bearer` 鉴权头
- 系统提示写入 `instructions` 字段，与消息序列分离
- 消息写入 `input` 数组（角色 + 内容项）
- 最大输出映射为 `max_output_tokens`（可选）
- 原生 JSON Schema 约束能力可直接透传/转译

响应侧 SHALL 从输出数组中类型为 `output_text` 的内容块提取文本，从响应状态 / 完成原因字段映射停止原因，从 input_tokens / output_tokens 解析用量。

#### Scenario: Responses 请求改写正确（场景 16）
- **WHEN** 中间表示包含系统提示与多轮消息，经 Responses 适配器发往上游
- **THEN** 上游收到的请求满足：Bearer 鉴权头正确、系统提示位于 instructions 字段、消息位于 input 数组且结构正确

#### Scenario: Responses 用量与文本反向映射
- **WHEN** 上游返回含 output_text 内容块与 input_tokens / output_tokens 的响应
- **THEN** 文本、完成原因与用量均被正确反向映射进北向响应与统一用量结构

### Requirement: Anthropic 协议正向改写与反向映射
Anthropic 适配器向上游 `POST /v1/messages` 发送请求时 SHALL：
- 使用 `x-api-key` 头 + 协议版本头鉴权
- 系统提示写入顶层 `system` 字段；`messages` 只含 user/assistant 角色，内容为块列表
- `max_tokens` 为必填：调用方未传时 MUST 注入安全默认值
- 无原生 JSON Schema 约束：结构化输出 MUST 转译为格式指令注入

响应侧 SHALL 从 `content` 数组中类型为 `text` 的内容块提取文本，映射 `stop_reason`（end_turn / max_tokens / stop_sequence 等）为北向完成原因，解析 input_tokens / output_tokens（含缓存读写计数）。

#### Scenario: max_tokens 必填兜底（场景 17）
- **WHEN** 北向请求未携带最大输出参数且路由到 Anthropic 协议供应商
- **THEN** 发往上游的请求包含适配器注入的安全默认 max_tokens

#### Scenario: 顶层 system 注入与停止原因映射（场景 17）
- **WHEN** 中间表示包含系统提示且上游以 stop_reason 结束
- **THEN** 上游请求的系统提示位于顶层 system 字段且 messages 无 system 角色；stop_reason 被正确映射为北向完成原因

### Requirement: 流式事件统一翻译
两种上游的 typed 流式事件流 SHALL 由各自适配器统一转换为北向 Chat Completions SSE 块格式（增量内容 + 结束原因 + 用量），对调用方完全透明；TTFT 与用量统计 MUST 在转换层完成。

#### Scenario: Responses 流式事件统一转换（场景 18）
- **WHEN** Responses 协议上游逐个发出文本增量事件与携带用量的完成事件
- **THEN** 客户端收到北向格式 SSE 块：逐块增量内容、结束原因与用量均正确，TTFT 被记录

#### Scenario: Anthropic 流式事件统一转换（场景 18）
- **WHEN** Anthropic 协议上游发出内容块增量事件与携带用量及停止原因的收尾事件
- **THEN** 客户端收到北向格式 SSE 块：逐块增量内容、结束原因与用量均正确，TTFT 被记录

### Requirement: 错误体归一
两种协议的上游错误体（error 对象结构与 type 枚举体系不同）SHALL 统一映射为 OpenAI 风格错误对象（message / type / param / code）。

#### Scenario: 两种上游错误归一（场景 19）
- **WHEN** Responses 协议与 Anthropic 协议上游分别返回各自格式的错误体
- **THEN** 两者均被归一为统一的 OpenAI 风格错误对象返回给调用方

### Requirement: 无原生约束协议的结构化输出
当上游协议无原生 JSON Schema 约束能力时，适配器 SHALL 把 Schema 转译为格式指令注入请求；网关本地校验 + 自动修复闭环始终兜底。

#### Scenario: 指令注入 + 本地校验闭环返回合法 JSON（场景 20）
- **WHEN** 对无原生约束的协议发起带 json_schema 的请求，且模型首轮输出不合规
- **THEN** 网关经指令注入、本地校验与修复闭环后仍返回合法且通过 Schema 校验的 JSON
