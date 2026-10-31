# structured-output Specification

## Purpose

定义结构化输出能力：json_schema 按上游协议适配、网关本地二次校验（纵深防御）、Markdown 代码围栏容错、非流式自动修复闭环（默认 1 轮、上限 3 轮、422 终局）与流式不修复边界。

## Requirements

### Requirement: json_schema 协议适配与本地二次校验
response_format 的 json_schema 类型 SHALL 由适配器按上游协议处理：原生支持约束的协议直接透传/转译；不支持的协议把 Schema 转译为格式指令注入。即使上游声称支持约束输出，网关仍 MUST 在本地用标准 JSON Schema 校验器做二次校验。

#### Scenario: 本地二次校验兜底
- **WHEN** 上游返回的 JSON 文本未通过本地 Schema 校验
- **THEN** 网关不直接放行，进入修复闭环

### Requirement: 解析容错
本地校验 SHALL 容忍被 Markdown 代码围栏（```json … ```）包裹的 JSON。

#### Scenario: 围栏包裹仍可校验
- **WHEN** 模型输出为 Markdown 代码围栏包裹的合法 JSON
- **THEN** 网关提取正文后校验通过，不触发修复

### Requirement: 非流式修复闭环
仅非流式调用支持修复闭环，网关 SHALL：
1. 提取模型文本输出并本地校验；
2. 失败则把模型上一轮输出作为 assistant 消息回填对话，并追加一条包含精确校验错误（出错路径 + 原因）与完整目标 Schema 的修复指令，重新请求模型；
3. 修复轮次有上限（默认 1 轮、上限 3 轮），仍失败 MUST 返回 422。

#### Scenario: 修复成功（场景 3）
- **WHEN** 模型首轮输出不合法 JSON，修复轮输出合法且通过 Schema 校验
- **THEN** 客户端收到合法结果，该次用量的重试计数反映修复轮次

#### Scenario: 修复超限 422（场景 4）
- **WHEN** 模型连续多轮输出均不合规，修复轮次达到配置上限
- **THEN** 返回 422，错误体为统一封装，语义为结构化输出终局不合规

### Requirement: 流式不修复
流式结构化输出 MUST NOT 本地纠错（已发出的内容无法撤回），Schema 只透传；此边界 MUST 在交付文档中明确：严格业务协议建议使用非流式接口。

#### Scenario: 流式 schema 请求不触发修复
- **WHEN** 携带 json_schema 发起流式请求
- **THEN** 增量原样下发，网关不拦截、不修复
