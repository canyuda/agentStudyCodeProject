"""提示词模板存储：不可变版本 + 每个模板 ID 一个"激活版本"。

【教学定位：版本化设计（需求 5.3）】
核心规则：同一 prompt_id 再次创建【永远产生新版本】，历史版本不可变——
这带来三个直接收益：
1. 可回滚：新版本效果变差？activate 回旧版本，秒级恢复
2. 可审计：用量账本记录"某次调用用的是 v2"，出问题能定位到具体版本
3. 无竞态灰度：改模板 = 发新版本 + 切激活，正在进行的旧请求不受影响

激活规则：首个版本自动激活（否则默认查询无版本可用）；
后续版本需显式 POST /activate 切换。查询默认取激活版本，可指定版本号。

存储与账本共用同一个 SQLite 文件（教学简化：单库零运维），
主键 (prompt_id, version) 天然防止版本号冲突。
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite

# 一行 = 一个不可变的模板版本；is_active 标记激活版（同 ID 至多一个）。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompts (
  prompt_id TEXT NOT NULL,
  version INTEGER NOT NULL,          -- 从 1 递增，永不复用
  template TEXT NOT NULL,            -- 模板正文（Jinja2 语法）
  is_active INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (prompt_id, version)
);
"""


class PromptNotFoundError(Exception):
    """提示词或版本不存在（API 层转 404）。"""


class PromptStore:
    """模板版本存储（异步 CRUD + 激活切换）。"""
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

    async def create(self, prompt_id: str, template: str) -> dict[str, Any]:
        """Create a new version; the first version becomes active automatically."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM prompts WHERE prompt_id = ?", (prompt_id,)
        )
        (current_max,) = await cursor.fetchone()
        version = int(current_max) + 1
        is_active = 1 if version == 1 else 0
        await self._db.execute(
            "INSERT INTO prompts (prompt_id, version, template, is_active, created_at) VALUES (?,?,?,?,?)",
            (prompt_id, version, template, is_active, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        )
        await self._db.commit()
        return {"prompt_id": prompt_id, "version": version, "is_active": bool(is_active)}

    async def get(self, prompt_id: str, version: int | None = None) -> dict[str, Any]:
        """Default to the active version; explicit version must exist. 404-style error otherwise."""
        assert self._db is not None
        if version is None:
            cursor = await self._db.execute(
                "SELECT * FROM prompts WHERE prompt_id = ? AND is_active = 1", (prompt_id,)
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM prompts WHERE prompt_id = ? AND version = ?", (prompt_id, version)
            )
        row = await cursor.fetchone()
        if row is None:
            raise PromptNotFoundError(
                f"prompt '{prompt_id}'" + (f" version {version}" if version is not None else " (no active version)")
            )
        return dict(row)

    async def set_active(self, prompt_id: str, version: int) -> None:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT 1 FROM prompts WHERE prompt_id = ? AND version = ?", (prompt_id, version)
        )
        if await cursor.fetchone() is None:
            raise PromptNotFoundError(f"prompt '{prompt_id}' version {version}")
        await self._db.execute("UPDATE prompts SET is_active = 0 WHERE prompt_id = ?", (prompt_id,))
        await self._db.execute(
            "UPDATE prompts SET is_active = 1 WHERE prompt_id = ? AND version = ?", (prompt_id, version)
        )
        await self._db.commit()

    async def list_all(self) -> list[dict[str, Any]]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT prompt_id, version, is_active, created_at FROM prompts ORDER BY prompt_id, version"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(row["prompt_id"], {"prompt_id": row["prompt_id"], "versions": []})
            entry["versions"].append(row["version"])
            if row["is_active"]:
                entry["active_version"] = row["version"]
        return list(grouped.values())
