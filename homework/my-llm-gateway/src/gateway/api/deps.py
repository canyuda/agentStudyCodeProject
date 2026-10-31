"""API 层共享依赖：应用状态访问器 + 网关密钥鉴权。

【教学定位：FastAPI 依赖注入（Depends）的典型用法】
AppState 是"应用级单例"的容器（配置、管道、路由器、熔断器……），
create_app 装配后挂在 app.state.gateway 上；各端点函数通过
Depends(get_state) 取用——端点函数保持无状态、可独立测试。

require_auth 是"横切关注点做成依赖"的示范：任何端点声明
`key: str = Depends(require_auth)` 即获得鉴权（未授权直接 401），
不需要在每个端点里重复写鉴权代码。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request

from gateway.checkpoint.store import CheckpointStore
from gateway.config import GatewayConfig
from gateway.errors import invalid_api_key
from gateway.ledger.store import UsageLedger
from gateway.pipeline import Pipeline
from gateway.prompts.store import PromptStore
from gateway.resilience.breaker import CircuitBreakerRegistry
from gateway.resilience.ratelimit import TokenBucketRateLimiter
from gateway.routing.router import ModelRouter


@dataclass
class AppState:
    """应用状态容器：所有共享组件在此聚合（main.create_app 装配）。"""

    config: GatewayConfig  # 已校验的配置
    pipeline: Pipeline  # 请求编排管道（大脑）
    router: ModelRouter  # 别名路由器
    breakers: CircuitBreakerRegistry  # 熔断器注册表
    rate_limiter: TokenBucketRateLimiter  # 令牌桶限流器
    ledger: UsageLedger  # 用量账本
    prompts: PromptStore  # 提示词模板存储
    checkpoints: CheckpointStore  # 流检查点（可选能力）


def get_state(request: Request) -> AppState:
    """从当前 app 取共享状态（依赖注入入口）。"""
    return request.app.state.gateway


def require_auth(request: Request) -> str:
    """校验 Bearer 网关密钥；通过则返回密钥原文（供指纹计算）。

    安全细节：比较用 hmac.compare_digest 而不是 ==——
    恒定时间比较，杜绝"逐字符试错 + 计时"侧信道攻击。
    （教学点：密钥比较是少数必须放弃 == 便利性的地方。）
    """
    state = get_state(request)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise invalid_api_key()  # 缺头或格式不对 -> 401 统一错误体
    key = header[7:].strip()  # 去掉 "Bearer " 前缀
    if not any(hmac.compare_digest(key, candidate) for candidate in state.config.auth.gateway_keys):
        raise invalid_api_key()  # 与密钥表中任何一把都不匹配
    return key
