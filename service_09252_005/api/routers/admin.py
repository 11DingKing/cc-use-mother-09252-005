"""管理 API：令牌签发（含首启引导）。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..deps import get_session, require_admin, transactional
from ..schemas import TokenIn
from ..security import Principal, hash_token
from ...persistence.models import ApiToken

router = APIRouter(prefix="/admin", tags=["admin"])

_VALID_SCOPES = {"admin", "enterprise", "school"}


@router.post("/bootstrap-token")
def bootstrap_token(
    body: TokenIn,
    session: Session = Depends(get_session),
):
    """仅当系统中还没有任何令牌时开放，用于首启创建管理员。"""
    count = session.scalar(select(func.count()).select_from(ApiToken))
    if count:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "系统已初始化"})
    if body.scope != "admin":
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "首个令牌必须是管理员"})
    with transactional(session):
        session.add(
            ApiToken(
                token_hash=hash_token(body.raw_token),
                scope="admin",
                subject_id=None,
                label=body.label or "bootstrap-admin",
            )
        )
    return {"status": "created", "scope": "admin"}


@router.post("/tokens")
def create_token(
    body: TokenIn,
    session: Session = Depends(get_session),
    admin: Principal = Depends(require_admin),
):
    if body.scope not in _VALID_SCOPES:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "scope 必须为 admin/enterprise/school"})
    if body.scope != "admin" and not body.subject_id:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "企业/院校令牌必须指定 subject_id"})
    if session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(body.raw_token))):
        raise HTTPException(status_code=409, detail={"code": "conflict", "message": "令牌已存在"})
    with transactional(session):
        session.add(
            ApiToken(
                token_hash=hash_token(body.raw_token),
                scope=body.scope,
                subject_id=body.subject_id,
                label=body.label,
            )
        )
    return {"status": "created", "scope": body.scope, "subject_id": body.subject_id}


def ensure_env_admin() -> None:
    """启动时若配置了 ``ADMIN_TOKEN`` 环境变量，则确保它存在。"""
    raw = os.environ.get("ADMIN_TOKEN")
    if not raw:
        return
    # 延迟导入避免循环依赖
    from ...persistence.database import new_session

    with new_session() as session:
        exists = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
        if exists is None:
            session.add(
                ApiToken(
                    token_hash=hash_token(raw),
                    scope="admin",
                    label="env-admin",
                )
            )
            session.commit()
