"""应用工厂：配置 -> fail-fast 装配 -> FastAPI 应用。

【教学定位：组合根（Composition Root）】
整个网关的可替换组件都在这里"接线"：配置 -> 账本/模板库(数据库) ->
每供应商 HTTP 客户端 -> 双适配器 -> 路由器/熔断器/限流器 -> 管道 -> 端点。
依赖全部在 create_app 里创建并注入，业务模块彼此不 import——
这是"依赖注入"最朴素的实现，却带来两个实打实的好处：
1. 测试可注入 MockTransport 假上游（tests/conftest.py 就这么干）
2. 熔断/限流换 Redis 实现时只改这里的装配代码

横切关注点三件套：
- request-id 中间件：每个请求生成 UUID，写入 request.state 并回传响应头
- 统一异常处理器：GatewayError / 参数校验(422) / 未捕获异常(500)
  全部转成 OpenAI 风格错误体——错误形状的"单一出口"
- lifespan：启动时连接数据库，关闭时释放 HTTP 客户端与连接

transports 参数是测试注入口：{供应商名: httpx.MockTransport}，
生产环境不传即为真实网络。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.adapters.anthropic_messages import AnthropicMessagesAdapter
from gateway.adapters.base import UpstreamAdapter
from gateway.adapters.openai_responses import OpenAIResponsesAdapter
from gateway.api import admin, chat, health, models, prompts, responses, streams
from gateway.api.deps import AppState
from gateway.checkpoint.store import CheckpointStore
from gateway.config import GatewayConfig, load_config_or_exit
from gateway.errors import GatewayError
from gateway.ledger.store import UsageLedger
from gateway.pipeline import Pipeline
from gateway.prompts.store import PromptStore
from gateway.resilience.breaker import CircuitBreakerRegistry
from gateway.resilience.ratelimit import TokenBucketRateLimiter
from gateway.routing.router import ModelRouter

logger = logging.getLogger("gateway")  # NEVER log keys, message bodies or mask mappings


def build_adapters() -> dict[str, UpstreamAdapter]:
    return {
        OpenAIResponsesAdapter.protocol: OpenAIResponsesAdapter(),
        AnthropicMessagesAdapter.protocol: AnthropicMessagesAdapter(),
    }


def create_app(
    config: GatewayConfig | None = None,
    transports: dict[str, httpx.AsyncBaseTransport] | None = None,  # test injection point
) -> FastAPI:
    cfg = config or load_config_or_exit()
    transports = transports or {}

    ledger = UsageLedger(cfg.database.path)
    prompt_store = PromptStore(cfg.database.path)
    checkpoints = CheckpointStore(cfg.stream_checkpoint.max_chars)
    breakers = CircuitBreakerRegistry(cfg.circuit_breaker)
    router = ModelRouter(cfg, breakers)
    rate_limiter = TokenBucketRateLimiter(cfg.rate_limit)

    clients: dict[str, httpx.AsyncClient] = {
        name: httpx.AsyncClient(
            timeout=httpx.Timeout(provider.timeout.total_seconds, connect=provider.timeout.connect_seconds),
            transport=transports.get(name),
        )
        for name, provider in cfg.providers.items()
    }
    pipeline = Pipeline(cfg, build_adapters(), clients, router, breakers, ledger, prompt_store, checkpoints)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await ledger.connect()
        await prompt_store.connect()
        try:
            yield
        finally:
            for client in clients.values():
                await client.aclose()
            await prompt_store.close()
            await ledger.close()

    app = FastAPI(title=cfg.service.name, version="0.1.0", lifespan=lifespan)
    app.state.gateway = AppState(
        config=cfg,
        pipeline=pipeline,
        router=router,
        breakers=breakers,
        rate_limiter=rate_limiter,
        ledger=ledger,
        prompts=prompt_store,
        checkpoints=checkpoints,
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    def _error_response(request: Request, status: int, payload: dict) -> JSONResponse:
        return JSONResponse(status_code=status, content=payload,
                            headers={"X-Request-Id": getattr(request.state, "request_id", "")})

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        return _error_response(request, exc.status_code, exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"invalid request: {first.get('msg', 'validation failed')}"
        return _error_response(
            request,
            422,
            {"error": {"message": message, "type": "invalid_request_error", "param": loc or None,
                        "code": "invalid_request"}},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error_response(
            request,
            500,
            {"error": {"message": "internal gateway error", "type": "api_error", "param": None, "code": None}},
        )

    for module in (health, chat, responses, models, prompts, admin, streams):
        app.include_router(module.router)
    return app


def main() -> None:  # python -m gateway
    import uvicorn

    cfg = load_config_or_exit()
    uvicorn.run(create_app(cfg), host=cfg.service.host, port=cfg.service.port)


app = create_app()  # module-level app for `uvicorn gateway.main:app`
