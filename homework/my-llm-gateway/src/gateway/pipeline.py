"""请求编排管道：整个网关的"心脏"。

【教学定位】
北向 API 层只做协议进出的格式转换，真正的大脑在这里。本文件回答：
一次调用从进来到出去，到底经历了什么？

管道顺序（脱敏开启时，需求 10.5）：
    鉴权 -> 限流 -> 提示词渲染 -> 脱敏 -> 路由 -> 上游供应商
    上游响应 -> 还原（脱敏）-> 客户端
（鉴权与限流在 API 层完成，本模块负责"渲染之后"的所有环节）

三个最值得精读的设计：

1.【重试 + 故障转移的嵌套循环】
   外层 for 遍历候选路由（priority 按序 / weighted 选出的首选+后备），
   内层 while 做同一路由的指数退避重试（总尝试 <= max_attempts）。
   关键分支：普通 4xx 不重试直接换路；网络错误/可重试状态码才退避重试。

2.【流式的"首字节"分水岭】（本文件最难也最重要的语义）
   - 首字节【前】失败：客户端还没收到任何内容，此时重试/换路完全无害
     —— 走完整的重试+故障转移循环
   - 首字节【后】失败：客户端已收到部分内容，换一个模型续写会产生
     "两个模型拼接"的语义错乱（重复、风格突变），绝对禁止。
     正确做法：向客户端发一个携带错误对象的 SSE 事件 + 流结束标记后收尾
   - 客户端断开：立即关闭上游连接止损，账本记"已取消"（499）

3.【账本兜底】
   无论成功/失败/取消，每次调用必落一条用量记录（try/except/finally
   全覆盖），这是可观测性需求的根基。注意隐私红线：账本永不落消息正文。

依赖注入说明：Pipeline 不自己创建任何资源（HTTP 客户端、数据库连接都由
main.py 装配后传入），这让测试可以注入 MockTransport 假上游。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from gateway.adapters.base import UpstreamAdapter, UpstreamError
from gateway.checkpoint.store import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    CheckpointStore,
)
from gateway.config import GatewayConfig
from gateway.errors import (
    GatewayError,
    no_healthy_route,
    not_found,
    render_failed,
    structured_invalid,
    upstream_failed,
)
from gateway.ir import ChatIR, CompletionResult, DeltaEvent, DoneEvent, IRMessage, Usage
from gateway.ledger.cost import compute_cost
from gateway.ledger.store import UsageLedger, UsageRecord
from gateway.masking.masker import MaskSession
from gateway.masking.restorer import StreamRestorer
from gateway.masking.rules import build_rules
from gateway.prompts.render import RenderError, render
from gateway.prompts.store import PromptNotFoundError, PromptStore
from gateway.resilience.breaker import CircuitBreakerRegistry
from gateway.resilience.retry import is_retryable_status, sleep_backoff
from gateway.routing.router import ModelRouter
from gateway.structured.validate import repair_instruction, validate_json_schema


@dataclass
class CallState:
    """单次调用的可观测状态（贯穿请求全生命周期，最终整体写入账本）。

    隐私红线：这里【绝不】携带消息正文——账本字段全是可以安全留存的
    元数据（谁、哪个模型、多少 Token、多快、重试了几次）。
    """

    request_id: str  # 网关生成的请求 ID（响应头回传，供对账/检查点查询）
    entrypoint: str  # 北向入口：/v1/chat/completions 或 /v1/responses
    key_fingerprint: str  # 调用方密钥指纹（16 位短哈希，非原值）
    alias: str  # 请求的公开别名
    start_time: float  # time.monotonic() 起点（计算延迟，不受系统改时间影响）
    retries: int = 0  # 重试次数（含结构化修复轮次）
    failovers: int = 0  # 后备路由切换次数
    ttft_ms: int | None = None  # 首 Token 延迟（仅流式，首个内容字节转发时打点）
    prompt_id: str | None = None  # 本次调用使用的提示词 ID（如有引用）
    prompt_version: int | None = None  # 实际使用的版本号
    protocol: str | None = None  # 命中的上游协议（经哪家适配器）
    provider: str | None = None  # 实际命中的供应商
    upstream_model: str | None = None  # 真实上游模型 ID（只进账本，绝不北向）
    usage: Usage | None = None  # Token 用量（流式在收尾事件到达后填充）
    mask_session: MaskSession | None = None  # 脱敏会话（请求内存作用域，结束即弃）
    mask_hits: dict[str, int] | None = None  # 按类型计数的脱敏命中数（不含内容）
    cancelled: bool = False  # 客户端是否中途断开

    @property
    def elapsed_ms(self) -> int:
        """距请求开始的毫秒数（打点用）。"""
        return int((time.monotonic() - self.start_time) * 1000)


@dataclass
class StreamErrorEvent:
    """流开始后的错误事件：由 API 层渲染成 SSE data 事件发给客户端。"""

    error: GatewayError


# 北向流事件 = 增量 | 完成 | 流中错误（三种，API 层各有一种渲染方式）
NorthStreamEvent = DeltaEvent | DoneEvent | StreamErrorEvent


class Pipeline:
    """编排器：装配一次调用的全部横切关注点（渲染/脱敏/路由/韧性/记账）。

    所有依赖经构造函数注入（依赖倒置），便于测试替换。
    """

    def __init__(
        self,
        config: GatewayConfig,
        adapters: dict[str, UpstreamAdapter],  # 协议名 -> 适配器
        clients: dict[str, httpx.AsyncClient],  # 供应商名 -> HTTP 客户端
        router: ModelRouter,
        breakers: CircuitBreakerRegistry,
        ledger: UsageLedger,
        prompts: PromptStore,
        checkpoints: CheckpointStore,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.clients = clients
        self.router = router
        self.breakers = breakers
        self.ledger = ledger
        self.prompts = prompts
        self.checkpoints = checkpoints

    # ------------------------------------------------------------------
    # 准备阶段：提示词渲染注入 -> 脱敏。
    # 顺序刻意是"先渲染后脱敏"（需求 10.5）：渲染结果 = 模板骨架 + 变量值，
    # 整体过一遍脱敏，变量值中的敏感信息自然被替换，骨架不受影响。
    # ------------------------------------------------------------------

    async def _prepare(self, ir: ChatIR, state: CallState) -> ChatIR:
        """返回"已注入模板、已脱敏"的新 IR（函数式：不改原对象）。"""
        messages = list(ir.messages)
        if ir.prompt_ref is not None:
            ref = ir.prompt_ref
            # 1) 取模板（默认激活版本，可显式指定）——不存在则 404
            try:
                stored = await self.prompts.get(ref.id, ref.version)
            except PromptNotFoundError as exc:
                raise not_found(str(exc)) from exc
            # 2) 沙箱渲染——缺变量/注入语法都会 RenderError -> 422（绝不静默空串）
            try:
                rendered = render(stored["template"], ref.variables)
            except RenderError as exc:
                raise render_failed(str(exc)) from exc
            # 3) 注入位置：system 位插最前（角色默认 system），suffix 位追加末尾（默认 user）
            role = ref.role or ("system" if ref.position == "system" else "user")
            injected = IRMessage(role=role, content=rendered)
            messages = ([injected, *messages] if ref.position == "system" else [*messages, injected])
            state.prompt_id = ref.id  # 记账：本次调用实际使用的模板 ID 与版本
            state.prompt_version = int(stored["version"])
        # 4) 脱敏（总开关默认关闭）：对全部消息正文做占位符替换
        if self.config.masking.enabled:
            enabled = {name: rc.enabled for name, rc in self.config.masking.rules.items()}
            session = MaskSession(rules=build_rules(enabled))
            state.mask_session = session  # 请求内存作用域；映射表绝不落盘
            messages = [m.model_copy(update={"content": session.mask_text(m.content)}) for m in messages]
            state.mask_hits = session.hits  # 仅按类型计数，不含任何内容
        return ir.model_copy(update={"messages": messages})

    # ------------------------------------------------------------------
    # 非流式路径
    # ------------------------------------------------------------------

    async def complete(self, ir: ChatIR, state: CallState) -> CompletionResult:
        """非流式调用全流程：准备 -> 路由重试 -> 结构化修复 -> 还原 -> 记账。

        注意还原（restore）的位置——在结构化修复【之后】：
        修复闭环的输入（模型上轮输出）必须保持占位符形态，模型全程
        只见占位符；还原只发生在最终返回客户端的响应处（隐私红线）。
        """
        try:
            ir = await self._prepare(ir, state)
            result = await self._call_upstream(ir, state)
            if ir.constraint is not None:
                result = await self._structured_repair(ir, state, result)
            if state.mask_session is not None:
                result.text = state.mask_session.restore_text(result.text)  # 最终还原
            state.usage = result.usage
            await self._record(state, "success", http_status=200)
            return result
        except GatewayError as exc:
            await self._record(state, "failed", http_status=exc.status_code, error=exc)  # 失败也落账
            raise
        except asyncio.CancelledError:
            self._record_background(state, "cancelled", http_status=499)
            raise

    async def _call_upstream(self, ir: ChatIR, state: CallState) -> CompletionResult:
        """非流式的"路由 + 重试 + 故障转移"核心循环（场景 2 的实现处）。

        结构：外层 for = 候选路由（顺序由路由策略决定），
              内层 while = 同一路由的重试（指数退避，总尝试 <= max_attempts）。
        计数：state.retries 累计重试次数，state.failovers 累计路由切换次数
        （进入 index>0 的候选时 +1）——两者都会写入账本作为验收证据。
        """
        candidates = self.router.candidates(ir.model)  # 别名不存在 404 / 全不健康 503
        retry = self.config.retry
        last_error: UpstreamError | None = None
        for index, candidate in enumerate(candidates):
            if index > 0:
                state.failovers += 1  # 首选( index=0 )不算切换，从第二条路由起计数
            attempt = 0
            while attempt < retry.max_attempts:
                attempt += 1
                if attempt > 1:
                    state.retries += 1  # 首次尝试不算重试
                adapter = self.adapters[candidate.provider.protocol]  # 按协议取适配器
                client = self.clients[candidate.provider_name]  # 每供应商独立客户端
                breaker = self.breakers.get(candidate.provider_name)  # 每供应商独立熔断器
                try:
                    data = await adapter.post_json(client, candidate.provider, candidate.model_id, ir)
                except UpstreamError as exc:
                    last_error = exc
                    breaker.record_failure()  # 连续失败达到阈值 -> 该供应商被熔断
                    retryable = exc.network_failure or is_retryable_status(retry, exc.status)
                    if not retryable:  # 普通 4xx：重试同样的坏请求无意义，直接换路
                        break
                    if attempt < retry.max_attempts:
                        await sleep_backoff(retry, attempt)  # 指数退避 + 抖动后重试
                        continue
                    break  # 本路由尝试耗尽 -> 外层 for 切换下一条候选路由
                breaker.record_success()  # 成功即复位连续失败计数
                self._mark_hit(state, candidate)
                return adapter.parse_response(data)
        # 所有路由耗尽：最后一个错误做终局归一（5xx/网络 -> 502，4xx 透传）
        raise upstream_failed(last_error.status, last_error.message) if last_error else no_healthy_route(ir.model)

    async def _structured_repair(self, ir: ChatIR, state: CallState, result: CompletionResult) -> CompletionResult:
        """结构化输出的本地校验 + 自动修复闭环（场景 3/4，仅非流式）。

        闭环三步（需求 5.2）：
        1. 提取模型文本输出（剥掉 markdown 围栏）-> 本地 jsonschema 校验
        2. 失败 -> 把"模型上一轮输出"回填为 assistant 消息 + 追加一条
           包含【精确校验错误（路径+原因）】与【完整目标 Schema】的修复指令，
           重新请求模型
        3. 轮次有上限（默认 1，上限 3），仍失败 -> 422 终局

        教学要点：修复指令带模型上轮输出时【不做还原】——脱敏开启时
        模型全程只见占位符，原值只在最终响应处还原（需求 10.7）。
        """
        max_rounds = self.config.structured_output.max_repair_rounds
        rounds = 0
        while True:
            verdict = validate_json_schema(result.text, ir.constraint.schema)
            if verdict.ok:
                # 用"提取后的纯 JSON 文本"替换原输出（剥掉了围栏与首尾空白）
                return result.model_copy(update={"text": verdict.json_text})
            if rounds >= max_rounds:
                raise structured_invalid(verdict.error or "invalid JSON")  # 终局 422
            rounds += 1
            state.retries += 1  # 修复轮次计入账本的重试计数（验收可见）
            repair_messages = [
                *ir.messages,
                IRMessage(role="assistant", content=result.text),  # 模型上轮输出原样回填
                IRMessage(role="user", content=repair_instruction(verdict.error or "", ir.constraint.schema)),
            ]
            result = await self._call_upstream(ir.model_copy(update={"messages": repair_messages}), state)

    # ------------------------------------------------------------------
    # 流式路径（本模块最精妙的部分，配合 tests/test_streaming.py 阅读）
    # ------------------------------------------------------------------

    async def stream(self, ir: ChatIR, state: CallState) -> AsyncIterator[NorthStreamEvent]:
        """流式调用：一个异步生成器，产出北向事件流。

        【为什么生成器开始前要"预取"第一个事件】
        API 层会先 await anext() 取出第一个事件再返回 StreamingResponse——
        这样"首字节前"的失败（404/503/全路由耗尽）还能以普通 HTTP 错误
        返回；一旦开始 yield，HTTP 200 + SSE 头已发出，错误只能走 SSE 事件。

        【三层 try 的职责划分】
        - 最外层 try/except：GatewayError（首字节前）→ 记账后重抛给 API 层；
          GeneratorExit/CancelledError（客户端断开）→ 取消记账 + 关上游
        - 中间层 except UpstreamError：上游流中/开流错误，按 delivered 分流
        - 内层 finally：无论哪条路径退出都关闭上游连接（断开止损的保证）
        """
        checkpoints = self.checkpoints if self.config.stream_checkpoint.enabled else None
        if checkpoints is not None:
            checkpoints.start(state.request_id)
        retry = self.config.retry
        upstream: httpx.Response | None = None  # 当前打开的上游流（finally 里关闭）
        delivered = False  # 分水岭标志：是否已向客户端转发过任何内容字节
        try:
            # 注意顺序：_prepare 内部才会创建 mask_session，还原器必须在它之后构造
            ir = await self._prepare(ir, state)
            restorer = StreamRestorer(state.mask_session) if state.mask_session is not None else None
            candidates = self.router.candidates(ir.model)
            last_error: UpstreamError | None = None
            for index, candidate in enumerate(candidates):
                if index > 0:
                    state.failovers += 1
                attempt = 0
                while attempt < retry.max_attempts:
                    attempt += 1
                    if attempt > 1:
                        state.retries += 1
                    adapter = self.adapters[candidate.provider.protocol]
                    client = self.clients[candidate.provider_name]
                    breaker = self.breakers.get(candidate.provider_name)
                    # ---- 步骤 1：打开上游 SSE 连接（失败可重试/换路，尚未下发任何字节）
                    try:
                        upstream = await adapter.open_stream(client, candidate.provider, candidate.model_id, ir)
                    except UpstreamError as exc:
                        last_error = exc
                        breaker.record_failure()
                        if not (exc.network_failure or is_retryable_status(retry, exc.status)):
                            break
                        if attempt < retry.max_attempts:
                            await sleep_backoff(retry, attempt)
                            continue
                        break
                    # ---- 步骤 2：逐事件消费上游流并翻译下发
                    try:
                        done_event: DoneEvent | None = None
                        async for event in adapter.stream_events(upstream):
                            if isinstance(event, DeltaEvent):
                                # 脱敏开启时经还原器：占位符可能被 SSE 分块切开，
                                # 还原器会暂存"待定尾部"，只有确定安全的部分才下发
                                text = restorer.feed(event.content) if restorer else event.content
                                if text:  # 空串不下发（还原器暂存时 feed 返回 ""）
                                    if state.ttft_ms is None:
                                        state.ttft_ms = state.elapsed_ms  # TTFT 打点：首个内容字节转发时
                                    if checkpoints is not None:
                                        checkpoints.append(state.request_id, text)  # 保存"已还原"文本
                                    delivered = True
                                    yield DeltaEvent(content=text)
                            else:
                                done_event = event  # 收尾事件（结束原因 + 用量）
                        if done_event is None:
                            # 上游流结束却没发完成事件 == 连接被截断，视作可重试错误
                            raise UpstreamError(0, "upstream stream ended without a completion event")
                    except UpstreamError as exc:
                        breaker.record_failure()
                        last_error = exc
                        upstream = None  # stream_events 的 finally 已关闭它
                        if delivered:
                            # ★ 首字节后分水岭：绝不换模型续写（避免两模型拼接错乱），
                            #   改为发 SSE 错误事件 + 流结束标记收尾（场景 7）
                            if checkpoints is not None:
                                checkpoints.finish(state.request_id, STATUS_FAILED)
                            error = upstream_failed(exc.status, exc.message)
                            await self._record(state, "failed", http_status=error.status_code, error=error)
                            yield StreamErrorEvent(error)
                            yield DoneEvent(finish_reason="error", usage=state.usage)
                            return
                        # 首字节前：照常走重试/换路
                        if not (exc.network_failure or is_retryable_status(retry, exc.status)):
                            break
                        if attempt < retry.max_attempts:
                            await sleep_backoff(retry, attempt)
                            continue
                        break
                    finally:
                        # 每条退出路径（正常/异常/断开）都关闭当前上游流
                        if upstream is not None:
                            await upstream.aclose()
                            upstream = None
                    # ---- 步骤 3：正常收尾
                    breaker.record_success()
                    self._mark_hit(state, candidate)
                    if done_event is not None and done_event.usage is not None:
                        state.usage = done_event.usage  # 流式用量来自收尾事件
                    final_text = restorer.flush() if restorer else ""  # 冲出还原器暂存的尾部
                    if final_text:
                        if state.ttft_ms is None:  # 极端情况：全部内容都被还原器暂存到结束
                            state.ttft_ms = state.elapsed_ms
                        if checkpoints is not None:
                            checkpoints.append(state.request_id, final_text)
                        delivered = True
                        yield DeltaEvent(content=final_text)
                    if checkpoints is not None:
                        checkpoints.finish(state.request_id, STATUS_SUCCEEDED)
                    await self._record(state, "success", http_status=200)
                    yield done_event or DoneEvent(finish_reason="stop", usage=state.usage)
                    return
            # 首字节前所有路由耗尽 -> 抛 GatewayError（API 层转为普通 HTTP 错误）
            if checkpoints is not None:
                checkpoints.finish(state.request_id, STATUS_FAILED)
            raise upstream_failed(last_error.status, last_error.message) if last_error else no_healthy_route(ir.model)
        except GatewayError as exc:
            # 只可能在没有 delivered 时到达 -> 还能以正常 HTTP 错误响应
            await self._record(state, "failed", http_status=exc.status_code, error=exc)
            raise
        except (GeneratorExit, asyncio.CancelledError):
            # ★ 客户端断开（场景 8）：生成器被 aclose()/取消。
            # 立即停止消费上游 -> finally 关闭连接止损；账本记"已取消"(499)
            state.cancelled = True
            if checkpoints is not None:
                checkpoints.finish(state.request_id, STATUS_CANCELLED)
            self._record_background(state, "cancelled", http_status=499)
            raise
        finally:
            if upstream is not None:  # 双保险：正常路径内层 finally 已关闭
                await upstream.aclose()

    # ------------------------------------------------------------------
    # 账本写入
    # ------------------------------------------------------------------

    def _mark_hit(self, state: CallState, candidate) -> None:  # noqa: ANN001 - Candidate 类型在 routing 模块
        """成功命中某路由后，把"经哪家协议/供应商/真实模型"记入状态。"""
        state.protocol = candidate.provider.protocol
        state.provider = candidate.provider_name
        state.upstream_model = candidate.model_id

    async def _record(
        self,
        state: CallState,
        status: str,  # success | failed | cancelled
        http_status: int,
        error: GatewayError | None = None,
        usage: Usage | None = None,
    ) -> None:
        """把 CallState 组装成一条完整用量记录写入账本（字段全集见需求 6.1）。

        成本按价格表实时计算；未配置价格的别名记 0。
        注意：这里没有任何消息正文——隐私红线在数据结构层面保证。
        """
        usage = usage or state.usage or Usage()
        record = UsageRecord(
            request_id=state.request_id,
            entrypoint=state.entrypoint,
            alias=state.alias,
            key_fingerprint=state.key_fingerprint,
            status=status,
            protocol=state.protocol,
            provider=state.provider,
            upstream_model=state.upstream_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_tokens=usage.cache_tokens,
            cost=compute_cost(self.config.pricing.get(state.alias), usage),
            total_latency_ms=state.elapsed_ms,
            ttft_ms=state.ttft_ms,
            retries=state.retries,
            failovers=state.failovers,
            http_status=http_status,
            error_type=error.error_type if error else None,
            error_message=error.message if error else None,
            prompt_id=state.prompt_id,
            prompt_version=state.prompt_version,
            mask_hits=state.mask_session.hits if state.mask_session is not None else None,
        )
        await self.ledger.write(record)

    def _record_background(self, state: CallState, status: str, http_status: int) -> None:
        """取消路径的记账：GeneratorExit 上下文中不允许 await，
        改用 create_task 异步落账（fire-and-forget，测试中稍等即可见）。"""
        asyncio.get_running_loop().create_task(self._record(state, status, http_status))
