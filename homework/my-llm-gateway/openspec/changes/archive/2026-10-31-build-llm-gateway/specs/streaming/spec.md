# Spec Delta

## Purpose

定义流式输出行为：SSE 逐块转发、首 Token 延迟（TTFT）记录、首字节前后的差异化失败语义、客户端断开取消止损，以及流式用量解析与响应头要求。

## ADDED Requirements

### Requirement: SSE 逐块转发
请求携带 stream=true 时，网关 SHALL 以 SSE 逐块向客户端推送，MUST NOT 做整包缓冲；上游事件经适配器翻译为北向格式后立即下发。流式响应头 MUST 包含：请求 ID、no-cache、禁用反向代理缓冲。

#### Scenario: SSE 逐块与响应头（场景 6）
- **WHEN** 发起 stream=true 请求且上游逐块返回增量
- **THEN** 客户端以 SSE 逐块收到增量内容（非一次性整包），响应头含请求 ID、no-cache 与禁用代理缓冲指令

### Requirement: 首 Token 延迟记录
网关 SHALL 记录 TTFT：自请求开始到第一个内容字节转发给客户端的时长；TTFT 仅在流式调用时记录并写入用量账本。

#### Scenario: TTFT 入账（场景 6）
- **WHEN** 完成一次流式调用后查询该次用量记录
- **THEN** 记录包含 TTFT 数值

### Requirement: 首字节前失败可重试可切换
首字节尚未下发时首选路由失败，网关 SHALL 按韧性策略重试，并可切换后备路由。

#### Scenario: 首字节前切换后备
- **WHEN** 首选路由在向客户端下发任何字节前失败且重试耗尽
- **THEN** 网关切换后备路由重发请求，客户端最终正常收到响应

### Requirement: 首字节后中断不换模型
首字节已下发后上游中断，网关 MUST NOT 切换到另一个模型续写，SHALL 向客户端发送一个 SSE 错误事件 + 流结束标记后收尾。

#### Scenario: 流中中断收尾（场景 7）
- **WHEN** 流式响应已下发若干增量后上游连接中断
- **THEN** 客户端收到携带错误对象的 SSE 错误事件与流结束标记，而非另一个模型续写的内容

### Requirement: 客户端断开止损
客户端断开连接时，网关 SHALL 立即关闭上游连接止损，用量记录标记「已取消」（对应状态码语义 499），MUST NOT 继续消耗 Token。

#### Scenario: 断开即取消（场景 8）
- **WHEN** 客户端在流式过程中断开连接
- **THEN** 网关关闭上游连接，用量记录最终状态为已取消（499），且不再产生新增用量

### Requirement: 流式用量解析
流式过程中网关 SHALL 同步解析上游 usage 事件以统计 Token；调用方传 include_usage 时上游才返回准确用量，此边界 MUST 在交付文档中提示。

#### Scenario: 流中 usage 入账（场景 6）
- **WHEN** 一次流式调用的上游事件流包含 usage 事件
- **THEN** 用量账本该记录包含正确的输入/输出 Token 统计
