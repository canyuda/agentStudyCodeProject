"""适配器基类：上游协议适配的"模板方法"层 + 共享的 HTTP/SSE 管道。

【教学定位：适配器模式的核心】
网关北向（对调用方）只说 Chat Completions/Responses 协议；南向（对上游）要
面对 OpenAI Responses 与 Anthropic Messages 两种完全不同的协议。适配器层的
职责就是把"协议差异"全部关进各自的适配器文件里：

    北向请求 --解析--> 中间表示 IR --适配器A/B改写--> 各协议上游请求
    各协议上游响应/SSE事件 --适配器A/B翻译--> 统一的 CompletionResult/StreamEvent

这样未来新增第三种协议（如 Gemini）只需要：
1. 在 config.PROTOCOLS 注册协议名
2. 新建一个 UpstreamAdapter 子类，实现 5 个抽象方法
其余分层（路由、重试、流式管道、账本……）一行都不用动。

【模板方法模式】
本基类把"与协议无关的通用流程"固化下来：
    组装请求头(含鉴权) -> 发 HTTP -> 检查状态码 -> 解析错误体/正常体
子类只负责填空（build_request 怎么写、错误体长什么样），不重复写传输代码。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gateway.config import ProviderCfg
from gateway.ir import ChatIR, CompletionResult, StreamEvent, Usage


class UpstreamError(Exception):
    """一次失败的上游调用（已经归一化，message 来自上游错误体原文）。

    status 约定（教学重点，重试策略依赖它做决策）：
    - status > 0：上游返回了 HTTP 错误状态码（如 429/500）
    - status == 0：网络层失败（DNS 解析失败、连接拒绝、超时）——
      根本没拿到 HTTP 响应，与"上游说 500"是两种不同性质的失败，
      但两者都值得重试。
    """

    def __init__(self, status: int, message: str, error_type: str = "upstream_error") -> None:
        super().__init__(message)
        self.status = status  # 0 => 网络错误 / 超时（没有 HTTP 状态码可用）
        self.message = message
        self.error_type = error_type

    @property
    def network_failure(self) -> bool:
        """是否为网络层失败（重试判断时与"可重试状态码"取或）。"""
        return self.status == 0


class UpstreamAdapter(ABC):
    """上游适配器抽象基类：一个子类对应一种上游协议。

    子类必须实现的 5 个抽象方法正好覆盖协议差异的五个面：
    endpoint        调用哪个端点（/responses vs /messages）
    build_request   请求体与协议特有头怎么写（含鉴权方式差异）
    parse_response  非流式响应如何提取文本/结束原因/用量
    parse_stream_event 流式事件如何翻译成统一的 Delta/Done 事件
    parse_error_body 各自的错误体结构如何归一为 UpstreamError
    """

    protocol: str  # 与 config.PROTOCOLS 中的名字对应，路由层按它查找适配器

    @abstractmethod
    def endpoint(self, base_url: str) -> str: ...

    @abstractmethod
    def build_request(self, ir: ChatIR, model_id: str) -> tuple[dict[str, str], dict[str, Any]]:
        """把中间表示 IR 改写为该协议的上游请求，返回 (额外请求头, JSON请求体)。

        注意返回的 headers 是"协议特有头"，鉴权头由基类的 auth_headers 统一
        注入——子类不用（也不应）自己碰密钥。
        """

    @abstractmethod
    def parse_response(self, data: dict[str, Any]) -> CompletionResult: ...

    @abstractmethod
    def parse_stream_event(self, event: str | None, data: dict[str, Any]) -> StreamEvent | None:
        """把一条上游 SSE 事件映射为 DeltaEvent/DoneEvent。

        返回 None 表示该事件对北向无意义（如 Anthropic 的 ping 事件）；
        遇到错误事件直接 raise UpstreamError。
        """

    @abstractmethod
    def parse_error_body(self, status: int, body: dict[str, Any]) -> UpstreamError: ...

    # ------------------------------------------------------------------
    # 以下是与协议无关的通用传输代码（模板方法的"固定骨架"部分）
    # ------------------------------------------------------------------

    def auth_headers(self, provider: ProviderCfg) -> dict[str, str]:
        """组装鉴权头：供应商附加头 + 协议鉴权头 + Content-Type。

        两种协议的鉴权差异（Bearer vs x-api-key+版本头）由 _inject_auth
        的子类覆写解决，这里只做拼装。
        """
        headers = dict(provider.headers)  # 先拷贝，避免污染配置对象
        self._inject_auth(headers, provider.api_key)
        headers["Content-Type"] = "application/json"
        return headers

    def _inject_auth(self, headers: dict[str, str], api_key: str) -> None:  # 子类按协议覆写
        """默认实现：OpenAI 风格的 Authorization: Bearer 头。"""
        headers["Authorization"] = f"Bearer {api_key}"

    async def post_json(
        self, client: httpx.AsyncClient, provider: ProviderCfg, model_id: str, ir: ChatIR
    ) -> dict[str, Any]:
        """非流式调用：POST JSON 并返回解析后的响应体（>=400 时抛 UpstreamError）。

        httpx 的 TimeoutException / HTTPError 都归一为 status=0 的
        UpstreamError——上层重试逻辑只认这一种异常，不必关心网络细节。
        """
        extra, body = self.build_request(ir, model_id)
        headers = {**self.auth_headers(provider), **extra}  # 鉴权头在前，协议特有头可覆盖
        try:
            resp = await client.post(self.endpoint(provider.base_url), headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise UpstreamError(0, f"upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(0, f"upstream network error: {exc}") from exc
        return self._checked_json(resp)

    async def open_stream(
        self, client: httpx.AsyncClient, provider: ProviderCfg, model_id: str, ir: ChatIR
    ) -> httpx.Response:
        """流式调用第一步：建立 SSE 连接，返回"已打开但未读完"的响应对象。

        关键点：stream=True 让 httpx 不把响应体一次性读进内存，
        后续由 stream_events 逐行消费。调用方（pipeline）负责在
        客户端断开时 aclose() 它——这是"断开止损"的实现基础。
        """
        extra, body = self.build_request(ir, model_id)
        headers = {**self.auth_headers(provider), **extra}
        try:
            return await client.send(
                client.build_request(
                    "POST", self.endpoint(provider.base_url), headers=headers, json=body
                ),
                stream=True,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(0, f"upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(0, f"upstream network error: {exc}") from exc

    def _checked_json(self, resp: httpx.Response) -> dict[str, Any]:
        """状态码检查：>=400 交给子类的错误体解析器归一，否则返回 JSON。"""
        if resp.status_code >= 400:
            raise self.parse_error_body(resp.status_code, _safe_json(resp))
        return _safe_json(resp)

    async def stream_events(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        """流式调用第二步：把打开的 SSE 响应翻译成统一事件流（异步生成器）。

        结构：
        1. 若开流时就是错误状态码（如 401/429），读出错误体并抛 UpstreamError
        2. 逐行解析 SSE（_sse_lines），每条 data 交给子类的 parse_stream_event
        3. finally 里 aclose 响应——无论正常结束、出错还是消费方提前退出，
           上游连接都会被关闭（绝不泄漏连接）
        """
        if response.status_code >= 400:
            body = await response.aread()
            raise self.parse_error_body(response.status_code, _safe_bytes(body))
        try:
            async for event, data in _sse_lines(response):
                mapped = self.parse_stream_event(event, data)
                if mapped is not None:
                    yield mapped
        finally:
            await response.aclose()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    return _safe_bytes(resp.content)


def _safe_bytes(raw: bytes) -> dict[str, Any]:
    """防御性 JSON 解析：永远返回 dict，解析失败时保留原始文本片段。

    上游的错误响应未必是合法 JSON（可能是 HTML 错误页、空 body），
    这里把非 dict 或解析失败的内容包成 {"raw": ...}，
    保证错误信息"尽量带上上游原文"而不是二次崩溃。
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"raw": str(data)}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw": raw[:500].decode("utf-8", errors="replace")}


async def _sse_lines(response: httpx.Response) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    """SSE（Server-Sent Events）行解析器。

    SSE 文本协议长这样（事件名可选，data 是 JSON）：
        event: response.output_text.delta
        data: {"type":"response.output_text.delta","delta":"你好"}
        (空行 = 一个事件结束)

    解析规则：
    - "event:" 行记录事件名，"data:" 行解析 JSON，两者配对 yield
    - 空行重置事件名（SSE 规范：事件以空行分隔）
    - data 为 "[DONE]" 时流结束（OpenAI 风格的结束标记）
    - JSON 解析失败不抛异常，包成 {"raw": ...} 让子类自行判断
    """
    event_name: str | None = None
    async for line in response.aiter_lines():
        line = line.rstrip("\r")
        if not line:
            event_name = None
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            payload = line.split(":", 1)[1].strip()
            if payload == "[DONE]":
                return
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"raw": payload}
            yield event_name, data if isinstance(data, dict) else {"raw": data}


def merge_usage(base: Usage | None, update: dict[str, Any], *fields: tuple[str, str]) -> Usage:
    """合并用量数字：后到的值覆盖先到的（流式过程中 usage 可能分多次到达）。

    fields 是 (上游字段名, 本地字段名) 对，只更新确实出现且为 int 的字段，
    避免用 None/缺省值把已统计的数字清零。
    """
    usage = base or Usage()
    for src, dst in fields:
        if isinstance(update.get(src), int):
            setattr(usage, dst, update[src])
    return usage
