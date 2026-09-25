"""数据库引擎与会话工厂。

默认使用 SQLite（文件位于系统临时目录，见 :mod:`service_09252_005.config`），
也可以通过 ``TRAINING_DB_URL`` 切换到其它 SQLAlchemy 后端；
:func:`configure_engine` 供测试在独立数据库上重建引擎。
SQLite 打开外键约束与 WAL，以支持并发确认测试。
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import default_db_url
from .models import Base

engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("TRAINING_DB_URL") or default_db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    if url.startswith("sqlite") and ":memory:" not in url:
        from pathlib import Path

        db_path = url.split("sqlite:///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite") and ":memory:" not in url:

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    return eng


def configure_engine(url: str | None = None) -> Engine:
    """重建全局引擎与会话工厂（主要给测试使用）。"""
    global engine, SessionLocal
    engine = _make_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)
    return engine


def init_db() -> None:
    if engine is None:
        configure_engine()
    else:
        Base.metadata.create_all(engine)


@contextmanager
def new_session() -> Session:
    if SessionLocal is None:
        configure_engine()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
