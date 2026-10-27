"""步骤 10：SQLite 持久层 —— 事件账本与 checkpoint 的写穿（write-through）存储。

三张表：
  runs         一行一个 Run：run_id / prompt / status / created_at
  events       事件账本：(run_id, seq) 主键，data 存 JSON —— 审计与回放的唯一事实
  checkpoints  每个 Run 最近一份进度快照

设计要点：
- 继承内存版 RunStore，只覆写四个"变更点"
  （create_run / append_event / set_status / save_checkpoint），
  Agent Loop 与 HTTP 层零改动 —— 第 5 步留的插槽在这里兑现；
- restore_all()：进程重启后从库重建内存账本。
  已终态的 Run 完整恢复（事件可回放、订阅可续传）；
  重启时还挂在 running/created 的 Run 是"孤儿"——进程都没了不可能再有事件，
  诚实做法是补一条 run.failed(PROCESS_RESTARTED) 终态事件回收，
  并把 checkpoint 里最后进度放进 partial_text。

教学取舍（生产系统会怎么做，注释里说清楚）：
- sqlite3 是同步库，这里直接在事件循环里调用：本地 SQLite 单行写是微秒级，
  教学可接受；生产会走 WAL + 异步写队列/独立写入线程；
- 写穿顺序是"先内存、后落库"（super() 先执行）。重启以库为准重建，
  极端断电窗口内可能出现"内存有、库里没有"的尾部事件丢失；
  生产做法是先写库再广播（write-ahead），本作业不展开。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from .events import (
    TERMINAL_STATUSES,
    RUN_STATUS_FAILED,
    RunEvent,
    RunEventType,
)
from .store import Run, RunStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    prompt     TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    type   TEXT NOT NULL,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id    TEXT PRIMARY KEY,
    saved_at  REAL NOT NULL,
    payload   TEXT NOT NULL
);
"""


class SqliteRunStore(RunStore):
    """内存 RunStore + SQLite 写穿。接口与父类完全一致。"""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL：写不阻塞读，崩溃后自动恢复到最近提交 —— 教学环境最划算的两条 PRAGMA
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------ 变更点：写穿 --
    def create_run(self, prompt: str) -> Run:
        run = super().create_run(prompt)
        self._conn.execute(
            "INSERT INTO runs (run_id, prompt, status, created_at) VALUES (?, ?, ?, ?)",
            (run.run_id, run.prompt, run.status, run.created_at),
        )
        self._conn.commit()
        return run

    async def append_event(
        self, run: Run, event_type: RunEventType, data: dict
    ) -> RunEvent:
        event = await super().append_event(run, event_type, data)
        self._insert_event(event)
        return event

    def set_status(self, run: Run, new_status: str) -> None:
        super().set_status(run, new_status)
        self._conn.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?", (new_status, run.run_id)
        )
        self._conn.commit()

    def save_checkpoint(self, run: Run, payload: dict) -> None:
        super().save_checkpoint(run, payload)
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints (run_id, saved_at, payload) VALUES (?, ?, ?)",
            (run.run_id, run.checkpoint["saved_at"],
             json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()

    # ------------------------------------------------------------ 恢复 --
    def restore_all(self) -> list[Run]:
        """重启后调用：从库重建全部 Run（含事件与 checkpoint），回收孤儿 Run。

        返回恢复出的 Run 列表。孤儿回收直接改内存账本 + 写库，
        不走异步 append_event —— 启动期还没有任何订阅者，无需 notify。
        """
        runs: dict[str, Run] = {}
        for run_id, prompt, status, created_at in self._conn.execute(
            "SELECT run_id, prompt, status, created_at FROM runs"
        ):
            runs[run_id] = Run(
                run_id=run_id, prompt=prompt, status=status, created_at=created_at
            )

        for run_id, seq, type_, ts, data_json in self._conn.execute(
            "SELECT run_id, seq, type, ts, data FROM events ORDER BY run_id, seq"
        ):
            runs[run_id].events.append(
                RunEvent(
                    run_id=run_id, seq=seq, type=type_, ts=ts,
                    data=json.loads(data_json),
                )
            )

        for run_id, saved_at, payload_json in self._conn.execute(
            "SELECT run_id, saved_at, payload FROM checkpoints"
        ):
            runs[run_id].checkpoint = {
                "saved_at": saved_at, **json.loads(payload_json)
            }

        self._runs.update(runs)

        # ---- 孤儿回收：非终态 Run 补一条 PROCESS_RESTARTED 失败终态 ----
        restored: list[Run] = []
        for run in runs.values():
            restored.append(run)
            if run.status in TERMINAL_STATUSES:
                continue

            partial = (run.checkpoint or {}).get("text_so_far", "")
            orphan = RunEvent(
                run_id=run.run_id,
                seq=len(run.events),          # 下标即 seq 的不变量同样适用于恢复
                type="run.failed",
                ts=time.time(),
                data={
                    "code": "PROCESS_RESTARTED",
                    "retryable": False,
                    "partial_text": partial,
                    "note": "进程重启时该 Run 仍在运行，已按孤儿回收",
                },
            )
            run.events.append(orphan)         # 直接改内存（无订阅者，免 notify）
            run.status = RUN_STATUS_FAILED
            self._insert_event(orphan)
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?",
                (RUN_STATUS_FAILED, run.run_id),
            )
        self._conn.commit()

        orphans = sum(1 for r in restored if r.events[-1].data.get("code") == "PROCESS_RESTARTED")
        print(
            f"[sqlite-store] 恢复 {len(restored)} 个 Run（其中孤儿回收 {orphans} 个）",
            file=sys.stderr,
        )
        return restored

    # ------------------------------------------------------------ 内部 --
    def _insert_event(self, event: RunEvent) -> None:
        self._conn.execute(
            "INSERT INTO events (run_id, seq, type, ts, data) VALUES (?, ?, ?, ?, ?)",
            (event.run_id, event.seq, event.type, event.ts,
             json.dumps(event.data, ensure_ascii=False)),
        )
        self._conn.commit()
