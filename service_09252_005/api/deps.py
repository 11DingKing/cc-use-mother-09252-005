"""FastAPI 公共依赖。"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..clock import Clock, SystemClock
from ..persistence.database import new_session
from .security import Principal, authenticate


@contextmanager
def transactional(session: Session):
    """提交工作单元。

    路由常常在写入前先查询（SQLAlchemy 已 autobegin），因此这里不能再
    ``session.begin()``：已有事务时提交即可，异常时回滚。
    """
    try:
        yield
        session.commit()
    except Exception:
        session.rollback()
        raise


def get_clock() -> Clock:
    return SystemClock()


def get_session():
    with new_session() as session:
        yield session


def require_principal(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Principal:
    raw = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw = parts[1].strip()
        else:
            raw = authorization.strip()
    principal = authenticate(session, raw)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "缺少或无效的访问令牌"},
        )
    return principal


def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "需要管理员令牌"},
        )
    return principal


def today(clock: Clock = Depends(get_clock)) -> dt.date:
    return clock.now().date()
