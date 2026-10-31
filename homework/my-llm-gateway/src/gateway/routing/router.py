"""模型别名路由：公开别名 -> 有序候选路由（含健康过滤）。

【教学定位】
路由表是"逻辑模型"与"物理供应商"之间的解耦层：

    公开别名 deepseek-pro  ──>  [ 候选1: resp 供应商 / real-pro   (首选)
                                  候选2: anth 供应商 / real-flash  (后备) ]

两种策略（需求 4.3）：
- priority：严格按配置顺序尝试——简单、可预测，适合"主备"场景
- weighted：平滑加权轮询选首选（按权重分摊流量），其余路由保留为后备

安全不变式：真实上游模型 ID 只存在于 Candidate 对象里，随适配器请求
发往上游、随账本记录留痕，但绝不进入任何北向响应（/v1/models 只给别名）。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from gateway.config import AliasCfg, GatewayConfig, ProviderCfg
from gateway.errors import model_not_found, no_healthy_route
from gateway.resilience.breaker import CircuitBreakerRegistry


@dataclass(frozen=True)
class Candidate:
    """一条已解析的候选路由（配置里的 provider 字符串已展开为对象引用）。

    frozen=True：候选在本次请求内不可变，避免路由过程被意外篡改。
    """

    provider_name: str
    provider: ProviderCfg
    model_id: str  # 真实上游模型 ID（注意：绝不北向）
    weight: float


@dataclass
class _SmoothWeighted:
    """平滑加权轮询（Smooth Weighted Round-Robin，Nginx 同款算法）。

    为什么不用"纯随机按权重抽"？随机会导致相邻请求分布抖动；
    平滑加权能保证长期比例精确等于权重比，且短期内也均匀交错。

    算法（以权重 A=5, B=1, total=6 为例）：
    1. 每轮所有节点的 current += 自身 weight
    2. 选 current 最大的节点作为本轮命中
    3. 命中节点的 current -= total
    5:1 的权重会得到 A A B A A A A A B ... 这样的平滑序列。
    """

    current: dict[str, float] = field(default_factory=dict)  # 各候选的当前权重
    lock_seq: itertools.count = itertools.count()  # 预留的序列号（单进程内无需加锁）

    def pick(self, candidates: list[Candidate]) -> Candidate:
        """按平滑加权选出本次首选候选。key 用 供应商+模型 双元组区分。"""
        total = sum(c.weight for c in candidates)
        for c in candidates:
            key = c.provider_name + c.model_id
            self.current[key] = self.current.get(key, 0) + c.weight  # 步骤 1
        best = max(candidates, key=lambda c: self.current[c.provider_name + c.model_id])  # 步骤 2
        self.current[best.provider_name + best.model_id] -= total  # 步骤 3
        return best


class ModelRouter:
    """别名路由器：解析候选、过滤不健康路由、暴露管理视图。"""

    def __init__(self, config: GatewayConfig, breakers: CircuitBreakerRegistry) -> None:
        self._aliases: dict[str, AliasCfg] = config.aliases
        self._providers: dict[str, ProviderCfg] = config.providers
        self._breakers = breakers  # 健康过滤依赖熔断器状态
        self._weighted = _SmoothWeighted()

    def candidates(self, alias: str) -> list[Candidate]:
        """返回"有序且健康"的候选列表；失败语义直接抛 GatewayError。

        - 别名不存在 -> 404 model_not_found（需求 4.4）
        - 候选全部不健康（供应商被禁用或熔断打开）-> 503 no_healthy_route
        - weighted 策略：算法选出首选放第一位，其余按配置序排后（后备）
        - priority 策略：配置顺序即尝试顺序
        """
        cfg = self._aliases.get(alias)
        if cfg is None:
            raise model_not_found(alias)
        routes = [Candidate(r.provider, self._providers[r.provider], r.model, r.weight) for r in cfg.routes]
        healthy = [c for c in routes if self._is_healthy(c)]
        if not healthy:
            raise no_healthy_route(alias)
        if cfg.strategy == "weighted":
            primary = self._weighted.pick(healthy)
            return [primary] + [c for c in healthy if c != primary]
        return healthy  # priority：严格按配置顺序

    def _is_healthy(self, candidate: Candidate) -> bool:
        """健康判定 = 供应商启用开关 AND 熔断器未打开。"""
        if not candidate.provider.enabled:
            return False
        return not self._breakers.get(candidate.provider_name).is_open

    def describe(self) -> dict:
        """路由表快照（供 /admin/routes 管理端点）。

        管理端点可以看到真实模型 ID 与熔断状态——这是运维视角；
        与北向 /v1/models（只给别名）形成"内外有别"的对比。
        """
        return {
            alias: {
                "strategy": cfg.strategy,
                "routes": [
                    {
                        "provider": r.provider,
                        "model": r.model,
                        "weight": r.weight,
                        "provider_enabled": self._providers[r.provider].enabled,
                        "circuit_open": self._breakers.get(r.provider).is_open,
                    }
                    for r in cfg.routes
                ],
            }
            for alias, cfg in self._aliases.items()
        }

    def public_models(self) -> list[str]:
        """对外公开的别名列表（/v1/models 用，排序保证输出稳定）。"""
        return sorted(self._aliases.keys())
