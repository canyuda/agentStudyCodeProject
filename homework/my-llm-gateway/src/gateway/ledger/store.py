"""用量账本：每次调用落一条记录，嵌入式 SQLite（aiosqlite + WAL 模式）。

【教学定位：可观测性的落地形态】
需求 6.1 的字段全集对应表 usage_records 的一列列；需求 6.2 的隐私红线
不是靠"约定"而是靠【表结构】保证——表里根本没有存放消息正文的列，
想违反也没有地方放。这是"用数据结构固化安全约束"的示范。

三条隐私红线（需求 6.2）：
1. 消息正文 / 提示词正文绝不写入用量表（结构上无此列）
2. 调用方密钥只存 16 位不可逆短指纹，绝不存原值
3. 任何日志不输出密钥原值

工程选型：
- aiosqlite：SQLite 的异步封装，单机零运维，适合教学与单副本部署
- WAL 模式：写不阻塞读，多并发请求下查询管理端点不被落账卡住
- 同步单条写入：教学取舍——高吞吐场景应改为异步批量写入（需求 11 已声明）
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

import aiosqlite

# 表结构 = 需求 6.1 的字段全集（教学时可直接对照需求文档逐列讲解）。
# created_at 建降序索引：管理端点"查最近 N 条"是最热路径。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,          -- 网关请求 ID（与响应头对账）
  created_at TEXT NOT NULL,          -- ISO 时间戳
  entrypoint TEXT NOT NULL,          -- 北向入口（/v1/chat/completions | /v1/responses）
  protocol TEXT,                     -- 命中的上游协议（经哪家适配器）
  provider TEXT,                     -- 实际命中的供应商
  alias TEXT NOT NULL,               -- 请求的公开别名
  upstream_model TEXT,               -- 真实上游模型 ID（仅管理可见，绝不北向）
  key_fingerprint TEXT NOT NULL,     -- 调用方密钥指纹（非原值！）
  input_tokens INTEGER NOT NULL DEFAULT 0,   -- Token 分类统计：
  output_tokens INTEGER NOT NULL DEFAULT 0,  --   输入/输出/
  cache_tokens INTEGER NOT NULL DEFAULT 0,   --   缓存命中
  cost REAL NOT NULL DEFAULT 0,      -- 按价格表计算的成本
  total_latency_ms INTEGER,          -- 总延迟
  ttft_ms INTEGER,                   -- 首 Token 延迟（仅流式）
  retries INTEGER NOT NULL DEFAULT 0,     -- 重试次数（含结构化修复轮次）
  failovers INTEGER NOT NULL DEFAULT 0,   -- 后备路由切换次数
  status TEXT NOT NULL,              -- success | failed | cancelled
  http_status INTEGER,               -- 终态 HTTP 状态码（取消=499）
  error_type TEXT,                   -- 错误类型（失败时）
  error_message TEXT,                -- 错误消息（失败时；不含正文）
  prompt_id TEXT,                    -- 使用的提示词 ID（如有引用）
  prompt_version INTEGER,            -- 实际使用的版本
  mask_hits TEXT                     -- 脱敏命中数（JSON，仅按类型计数）
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_records(created_at DESC);
"""


def fingerprint(key: str) -> str:
    """密钥 -> 16 位十六进制短指纹（SHA-256 前 16 位）。

    不可逆：从指纹还原不出原值；确定性：同一密钥指纹恒定，
    可用于按调用方聚合统计而不暴露身份。原值永远不进库、不进日志。
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class UsageRecord:
    """一条用量记录（与表列一一对应）；pipeline._record 组装后写入。"""
    request_id: str
    entrypoint: str
    alias: str
    key_fingerprint: str
    status: str  # success | failed | cancelled
    protocol: str | None = None
    provider: str | None = None
    upstream_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost: float = 0.0
    total_latency_ms: int | None = None
    ttft_ms: int | None = None
    retries: int = 0
    failovers: int = 0
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    mask_hits: dict[str, int] | None = None
    created_at: str = ""


class UsageLedger:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    @property
    def connected(self) -> bool:
        return self._db is not None

    async def connect(self) -> None:
        import os

        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def write(self, record: UsageRecord) -> None:
        assert self._db is not None, "ledger not connected"
        record.created_at = record.created_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        data = asdict(record)
        data["mask_hits"] = json.dumps(data["mask_hits"], ensure_ascii=False) if data["mask_hits"] else None
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        await self._db.execute(
            f"INSERT INTO usage_records ({cols}) VALUES ({marks})", tuple(data.values())
        )
        await self._db.commit()

    async def recent(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        assert self._db is not None, "ledger not connected"
        cursor = await self._db.execute(
            "SELECT * FROM usage_records ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        rows = await cursor.fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["mask_hits"] = json.loads(row["mask_hits"]) if row["mask_hits"] else None
        return result

    async def latest(self) -> dict[str, Any] | None:
        rows = await self.recent(limit=1)
        return rows[0] if rows else None
