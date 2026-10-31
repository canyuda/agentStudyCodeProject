"""按供应商独立的熔断器（单进程内存态）。

【教学定位：熔断器模式（Circuit Breaker）】
问题：某供应商持续故障时，若每次请求仍要走完"连接->超时->重试"的
完整流程，既拖慢调用方、又给故障上游雪上加霜。

方案：借鉴电路保险丝的三态状态机——

    closed(闭合/正常) --连续失败达阈值--> open(打开/熔断)
    open --冷却期到--> half_open(半开/试探)
    half_open --试探成功--> closed
    half_open --试探失败--> open（继续冷却）

关键性质：open 期间该供应商被路由层直接跳过（连尝试都不尝试），
请求自动落到后备路由——熔断与故障转移（pipeline）协同工作。

【边界声明（需求 11）】本实现是单进程内存态；多副本部署时各进程
熔断状态不共享，需替换为 Redis 等集中实现——所以这里刻意隔离成
Registry + 小接口，替换时不影响调用方。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from gateway.config import BreakerCfg


@dataclass
class CircuitBreaker:
    """单个供应商的熔断器（状态机本体）。"""

    name: str  # 供应商名
    threshold: int  # 连续失败阈值（默认 5 次打开）
    cooldown_seconds: float  # 冷却期（默认 30s 后放行试探）
    consecutive_failures: int = 0  # 连续失败计数（成功即清零）
    opened_at: float | None = None  # 打开时刻（判断冷却是否结束）
    state: str = "closed"  # closed | open | half_open

    @property
    def is_open(self) -> bool:
        """该供应商当前是否应被路由跳过。

        带副作用的查询（教学取舍）：检查 open 时顺手判断冷却期，
        到期就把状态推进到 half_open 并放行——省掉额外的定时器。
        单进程 asyncio 环境下无并发竞争，这是安全的简化。
        """
        if self.state == "open":
            if time.monotonic() - (self.opened_at or 0) >= self.cooldown_seconds:
                self.state = "half_open"  # 冷却结束：放行一个试探请求
                return False
            return True  # 冷却未到：继续跳过
        return False  # closed 或 half_open（试探中允许请求通过）

    def record_success(self) -> None:
        """任何成功都彻底复位：计数清零、状态回 closed。"""
        self.consecutive_failures = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self) -> None:
        """记一次失败；half_open 中的失败立即重新打开（试探失败），
        closed 中达到阈值也打开。"""
        self.consecutive_failures += 1
        if self.state == "half_open" or self.consecutive_failures >= self.threshold:
            self.state = "open"
            self.opened_at = time.monotonic()


@dataclass
class CircuitBreakerRegistry:
    """熔断器注册表：每个供应商名对应一个独立熔断器（惰性创建）。

    路由层/管道层只依赖这个 Registry，未来换 Redis 实现时只需
    保持 get()/snapshot() 语义不变。
    """

    config: BreakerCfg
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict)

    def get(self, provider_name: str) -> CircuitBreaker:
        """取（或首次创建）某供应商的熔断器。"""
        if provider_name not in self._breakers:
            self._breakers[provider_name] = CircuitBreaker(
                provider_name, self.config.failure_threshold, self.config.cooldown_seconds
            )
        return self._breakers[provider_name]

    def snapshot(self) -> dict[str, dict]:
        """全部熔断器状态快照（供 /admin/routes 展示）。"""
        return {
            name: {
                "state": b.state,
                "consecutive_failures": b.consecutive_failures,
                "threshold": b.threshold,
                "cooldown_seconds": b.cooldown_seconds,
            }
            for name, b in self._breakers.items()
        }
