"""令牌桶限流：维度 = 调用方密钥指纹 + 模型别名（每模型独立限流）。

【教学定位：令牌桶算法（Token Bucket）】
想象一个最多装 burst 个令牌的桶，令牌以 requests_per_minute/60 的
速率匀速滴入；每个请求取走 1 个令牌，取不到立即拒绝（429）。

为什么选令牌桶而不是固定窗口计数？
- 固定窗口在边界处会放过 2 倍流量（窗口末尾+下个窗口开头连发）
- 令牌桶天然允许"突发"（桶里攒的令牌一次消耗），又能约束长期平均速率，
  与真实 LLM 调用"偶发成簇"的模式更匹配

维度设计（需求 7.4）：(密钥指纹, 别名) 二元组各一个桶——
A 用户打爆 deepseek-pro 不影响他调 deepseek-flash，也不影响 B 用户。
超限立即 429（统一错误形状），不排队不等待。

【边界声明】单进程内存态，多副本需换 Redis（同熔断器的隔离理由）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from gateway.config import RateLimitCfg
from gateway.errors import rate_limited


@dataclass
class _Bucket:
    """一个令牌桶：当前令牌数 + 上次更新时刻（惰性补充，见 check）。"""

    tokens: float
    updated: float


@dataclass
class TokenBucketRateLimiter:
    """限流器：按 (密钥指纹, 别名) 维护独立令牌桶。"""

    config: RateLimitCfg
    _buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    @property
    def _capacity(self) -> float:
        """桶容量 = 允许的瞬时突发量。"""
        return float(self.config.burst)

    @property
    def _refill_per_second(self) -> float:
        """令牌滴入速率（每分钟请求数换算成每秒）。"""
        return self.config.requests_per_minute / 60.0

    def check(self, key_fingerprint: str, alias: str) -> None:
        """放行则静默返回；令牌不足立即抛 429（统一错误封装）。

        惰性补充（lazy refill）：不需要后台任务持续加水——每次 check 时
        按"距上次的时长 × 滴入速率"一次性补足（封顶桶容量），
        时间复杂度 O(1) 且零线程开销，是令牌桶的标准工程实现。
        """
        key = (key_fingerprint, alias)
        now = time.monotonic()
        bucket = self._buckets.get(key) or _Bucket(tokens=self._capacity, updated=now)
        # 补充自上次以来滴入的令牌（min 封顶：桶不能溢出）
        bucket.tokens = min(self._capacity, bucket.tokens + (now - bucket.updated) * self._refill_per_second)
        bucket.updated = now
        if bucket.tokens < 1.0:
            self._buckets[key] = bucket
            raise rate_limited(alias)  # 立即 429，不做排队等待
        bucket.tokens -= 1.0  # 消耗一个令牌
        self._buckets[key] = bucket
