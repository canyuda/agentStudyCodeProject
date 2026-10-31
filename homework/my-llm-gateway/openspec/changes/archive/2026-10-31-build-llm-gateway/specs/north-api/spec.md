# Spec Delta

## Purpose

定义网关对调用方暴露的北向 API 契约：端点清单、Bearer 密钥鉴权、统一错误封装与状态码语义、请求 ID 回传、公开别名模型列表与交互式文档。

## ADDED Requirements

### Requirement: 端点清单
网关 SHALL 暴露以下端点：
- `POST /v1/chat/completions`：Chat Completions 兼容入口，支持流式与非流式
- `POST /v1/responses`：第二北向入口（Responses 协议），支持流式与非流式
- `GET /v1/models`：返回网关公开别名列表
- `POST /v1/prompts` 及按 ID 的子路径：提示词创建、列表、查询、渲染预览
- `GET /admin/usage`：分页查询最近用量记录
- `GET /admin/routes`：查询模型路由表与各供应商熔断状态
- `GET /v1/streams/{请求ID}/checkpoint`：流检查点查询（该能力开启时）
- 健康检查 / 就绪检查（GET）：无鉴权，供容器探活

#### Scenario: 健康检查无鉴权可访问
- **WHEN** 未携带任何鉴权信息请求健康检查与就绪检查端点
- **THEN** 两者均返回成功状态，不返回 401

#### Scenario: Responses 北向入口可用（场景 9）
- **WHEN** 携带有效网关密钥向 `/v1/responses` 发起合法请求
- **THEN** 请求进入统一处理管道并正常返回，与 Chat Completions 入口共享同一别名路由、鉴权与错误封装

### Requirement: Bearer 网关密钥鉴权
除健康/就绪检查外，所有端点 SHALL 要求 `Authorization: Bearer <网关密钥>`；密钥缺失或无效 MUST 返回 401，错误体为统一错误封装。调用方 SHALL NOT 需要感知或持有任何上游供应商密钥。

#### Scenario: 缺失或错误密钥返回 401（场景 9）
- **WHEN** 携带错误密钥或缺失 Authorization 头调用任一受保护端点
- **THEN** 返回 401，错误体为 `message / type / param / code` 结构的 OpenAI 风格对象

### Requirement: 统一错误封装与状态码语义
所有错误响应 SHALL 为 OpenAI 风格对象：`message / type / param / code`。状态码语义：
- 401：网关密钥无效
- 404：模型别名 / 提示词 / 检查点不存在
- 422：参数校验失败、模板渲染失败、结构化输出终局不合规
- 429：网关限流，或上游限流且已无可回退路由
- 502：上游失败
- 503：无健康路由

上游 4xx 错误 MUST 状态码原样透传并附上游错误详情；上游 5xx MUST 归一为 502 且消息中附上游原始错误信息。流开始后的错误 MUST 以 SSE 数据事件携带错误对象，随后发送流结束标记。

#### Scenario: 上游 5xx 归一为 502
- **WHEN** 上游供应商返回 500 且已无可回退路由
- **THEN** 客户端收到 502，错误对象 message 中包含上游原始错误信息

#### Scenario: 上游 4xx 原样透传
- **WHEN** 上游供应商返回 400（不可重试类）且无切换必要
- **THEN** 客户端收到 400，错误体附上游错误详情

#### Scenario: 流中错误以 SSE 事件携带
- **WHEN** 流式响应已开始下发后发生错误
- **THEN** 客户端收到携带统一错误对象的 SSE 数据事件，随后收到流结束标记

### Requirement: 请求 ID 响应头
网关 SHALL 为每个请求生成唯一请求 ID，并通过响应头在流式与非流式响应中回传，供对账与检查点查询。

#### Scenario: 非流式与流式响应均回传请求 ID
- **WHEN** 分别发起一次非流式与一次流式调用
- **THEN** 两次响应头中均可读取对应请求的网关生成请求 ID

### Requirement: 公开别名不泄露真实模型
`GET /v1/models` SHALL 仅返回网关公开别名；真实上游模型 ID 与上游协议细节 MUST NOT 出现在任何北向响应中。

#### Scenario: 模型列表仅含别名
- **WHEN** 请求 `/v1/models`
- **THEN** 返回别名列表，且响应中不出现任何真实上游模型 ID

### Requirement: 交互式 API 文档
网关 SHALL 提供交互式 OpenAPI 文档。

#### Scenario: OpenAPI 文档可访问
- **WHEN** 访问文档端点
- **THEN** 返回可交互的 OpenAPI 文档页面
