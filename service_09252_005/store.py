"""SQLite 持久化：模式定义与事务边界。

设计要点：
- 单连接 + 可重入写锁串行化所有访问；写事务使用 BEGIN IMMEDIATE，
  使“检查再写入”在并发确认等场景下保持原子。
- 分配表用部分唯一索引保证不变量：
  * 同一岗位对同一学生至多一条生效记录；
  * 同一学生在同一季度至多一条生效分配（PROPOSED/CONFIRMED/COMPLETED）。
- 运行数据文件由调用方指定路径，绝不写入源码目录。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_events (
    version    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    quarter    TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    endpoint     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enterprises (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schools (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    id            TEXT PRIMARY KEY,
    enterprise_id TEXT NOT NULL REFERENCES enterprises(id),
    quarter       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (enterprise_id, quarter)
);

CREATE TABLE IF NOT EXISTS positions (
    id            TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    position_key  TEXT NOT NULL,
    title         TEXT NOT NULL,
    region        TEXT NOT NULL,
    skills        TEXT NOT NULL,
    capacity      INTEGER NOT NULL CHECK (capacity >= 0),
    indivisible   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (commitment_id, position_key)
);

CREATE TABLE IF NOT EXISTS demands (
    id         TEXT PRIMARY KEY,
    school_id  TEXT NOT NULL REFERENCES schools(id),
    quarter    TEXT NOT NULL,
    region     TEXT NOT NULL,
    skill      TEXT NOT NULL,
    count      INTEGER NOT NULL CHECK (count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (school_id, quarter, region, skill)
);

CREATE TABLE IF NOT EXISTS students (
    id         TEXT PRIMARY KEY,
    school_id  TEXT NOT NULL REFERENCES schools(id),
    name       TEXT NOT NULL,
    region     TEXT NOT NULL,
    skills     TEXT NOT NULL,
    phone      TEXT,
    id_number  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS safety_trainings (
    student_id   TEXT PRIMARY KEY REFERENCES students(id),
    completed_at TEXT,
    valid_until  TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    id          TEXT PRIMARY KEY,
    quarter     TEXT NOT NULL,
    position_id TEXT NOT NULL REFERENCES positions(id),
    student_id  TEXT NOT NULL REFERENCES students(id),
    school_id   TEXT NOT NULL,
    region      TEXT NOT NULL,
    status      TEXT NOT NULL,
    rank        INTEGER,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alloc_position_student_active
    ON allocations (position_id, student_id)
    WHERE status IN ('PROPOSED', 'CONFIRMED', 'WAITLISTED', 'COMPLETED');
CREATE UNIQUE INDEX IF NOT EXISTS uq_alloc_quarter_student_active
    ON allocations (quarter, student_id)
    WHERE status IN ('PROPOSED', 'CONFIRMED', 'COMPLETED');
CREATE INDEX IF NOT EXISTS idx_alloc_quarter_status ON allocations (quarter, status);
CREATE INDEX IF NOT EXISTS idx_alloc_position_status ON allocations (position_id, status);

CREATE TABLE IF NOT EXISTS matching_runs (
    quarter    TEXT PRIMARY KEY,
    report     TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compensation_jobs (
    id            TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL,
    position_id   TEXT NOT NULL,
    quarter       TEXT NOT NULL,
    status        TEXT NOT NULL,
    step          TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compensation_status ON compensation_jobs (status);

CREATE TABLE IF NOT EXISTS compensation_impacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL REFERENCES compensation_jobs(id),
    student_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status  TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, student_id, to_status)
);

CREATE TABLE IF NOT EXISTS settlements (
    id            TEXT PRIMARY KEY,
    quarter       TEXT NOT NULL,
    enterprise_id TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    lines         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (quarter, enterprise_id)
);
"""


class Store:
    """SQLite 存储：串行化事务与读取。"""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._local = threading.local()
        with self._lock:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，提交或回滚。禁止嵌套。"""
        with self._lock:
            if getattr(self._local, "in_tx", False):
                raise RuntimeError("不允许嵌套写事务")
            self._local.in_tx = True
            begun = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                begun = True
                yield self._conn
                self._conn.execute("COMMIT")
                begun = False
            finally:
                if begun:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                self._local.in_tx = False

    def read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """串行化读取：fn 接收连接并返回结果。"""
        with self._lock:
            return fn(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
