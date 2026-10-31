"""指数退避 + 随机抖动的重试延时计算。

【教学要点：为什么需要"抖动"(jitter)】
如果所有失败客户端都按同样的节奏重试（0.25s, 0.5s, 1s...），
它们会在完全相同的时刻再次冲击上游——"惊群效应"可能把刚恢复的
上游再次打挂。给每个延时叠加 ±25% 的随机偏移，让重试在时间轴上
自然散开，这是分布式系统的标准做法（AWS 架构博客经典内容）。

延时公式（需求 7.2）：
    delay = min(base * 2^(attempt-1), max_delay)   # 指数增长 + 封顶
    final = delay + uniform(-delay*jitter, +delay*jitter)
示例（base=0.25, max=4）：第1次重试约 0.25s，第2次约 0.5s，第3次约 1s。
"""

from __future__ import annotations

import asyncio
import random

from gateway.config import RetryCfg


def backoff_delay(cfg: RetryCfg, attempt: int) -> float:
    """计算第 attempt 次（1 起）重试前应等待的秒数。"""
    # 指数增长：2 ** (attempt-1) => 1, 2, 4, 8 ... 倍；min 封顶防止无限增长
    delay = min(cfg.base_delay_seconds * (2 ** (attempt - 1)), cfg.max_delay_seconds)
    spread = delay * cfg.jitter  # 抖动幅度 = 当前延时 × 抖动比例（默认 25%）
    return max(0.0, delay + random.uniform(-spread, spread))  # max 防止抖成负数


async def sleep_backoff(cfg: RetryCfg, attempt: int) -> None:
    """异步等待一个退避时长（重试循环里 await 它）。"""
    await asyncio.sleep(backoff_delay(cfg, attempt))


def is_retryable_status(cfg: RetryCfg, status: int) -> bool:
    """状态码是否属于可重试集合（网络错误不在这里判断——
    UpstreamError.network_failure 单独处理，两者取"或"）。"""
    return status in cfg.retryable_statuses
