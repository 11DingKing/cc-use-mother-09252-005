"""版本账本：承诺、需求、资格与分配变化写入同一 append-only 事件流。

每条事件获得全局单调递增的版本号；状态表是投影，账本是事实来源。
"""
from __future__ import annotations

import json
import sqlite3

from .ports import Clock


class Ledger:
    """单一版本账本。"""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def append(
        self,
        tx: sqlite3.Connection,
        kind: str,
        entity_id: str,
        quarter: str | None,
        payload: dict,
    ) -> int:
        """在调用方的事务内追加事件，返回事件版本号。"""
        cur = tx.execute(
            "INSERT INTO ledger_events (kind, entity_id, quarter, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                kind,
                entity_id,
                quarter,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                self._clock.now().isoformat(),
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM ledger_events").fetchone()
        return int(row["v"])

    @staticmethod
    def events_since(conn: sqlite3.Connection, since: int = 0, limit: int = 500) -> list[dict]:
        rows = conn.execute(
            "SELECT version, kind, entity_id, quarter, payload, created_at"
            " FROM ledger_events WHERE version > ? ORDER BY version LIMIT ?",
            (since, limit),
        ).fetchall()
        return [
            {
                "version": r["version"],
                "kind": r["kind"],
                "entity_id": r["entity_id"],
                "quarter": r["quarter"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
