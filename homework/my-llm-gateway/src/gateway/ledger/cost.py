"""按价格表计算单次调用成本（美元/百万 Token）。

【教学定位】
计价规则（需求 6.4）：
    成本 = 普通输入Token × 输入单价 + 缓存命中输入Token × 缓存折扣价
           + 输出Token × 输出单价            （单价均按"每百万 Token"报）

三个值得注意的工程细节：
1. 未配置价格 -> 记 0（而不是报错）：价格表缺失不该阻断调用链路
2. 缓存折扣价未配置 -> 按普通输入价（宁多不少，账算清比算少重要）
3. cache_tokens 上限约束在 input_tokens 内（min 钳制）——防御上游
   返回畸形数据（cache > input）时把成本算成负数
"""

from __future__ import annotations

from gateway.config import PricingCfg
from gateway.ir import Usage


def compute_cost(pricing: PricingCfg | None, usage: Usage) -> float:
    """计算一次调用的成本；round 到 8 位小数避免浮点误差堆积显示。"""
    if pricing is None:
        return 0.0  # 未配置价格表的别名：成本记 0
    # 缓存命中单价：未配置折扣价则退回普通输入价
    cache_price = (
        pricing.cache_input_per_million
        if pricing.cache_input_per_million is not None
        else pricing.input_per_million
    )
    # 钳制：缓存命中数不可能超过输入数（防上游脏数据算出负的普通输入）
    cache_tokens = min(usage.cache_tokens, usage.input_tokens)
    plain_input = usage.input_tokens - cache_tokens
    cost = (
        plain_input * pricing.input_per_million / 1_000_000
        + cache_tokens * cache_price / 1_000_000
        + usage.output_tokens * pricing.output_per_million / 1_000_000
    )
    return round(cost, 8)
