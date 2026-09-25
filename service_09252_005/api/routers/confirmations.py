"""确认与替补 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_principal, today, transactional
from ..schemas import ConfirmIn
from ..security import Principal
from ...persistence.models import Allocation
from ...services import confirmations, lifecycle
from ...services.errors import ServiceError

router = APIRouter(tags=["confirmations"])


def _guard_school_allocation(principal: Principal, allocation: Allocation) -> None:
    if principal.scope == "school" and not principal.owns_school(allocation.school_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能确认本校分配"})
    if principal.scope == "enterprise":
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "确认由院校发起"})


@router.post("/allocations/{allocation_id}/confirm")
def confirm(
    allocation_id: str,
    body: ConfirmIn | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
    on_date=Depends(today),
):
    allocation = session.scalar(select(Allocation).where(Allocation.id == allocation_id))
    if allocation is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "分配不存在"})
    _guard_school_allocation(principal, allocation)
    try:
        with transactional(session):
            return confirmations.confirm_allocation(
                session,
                allocation_id,
                on_date=on_date,
                student_ids=body.student_ids if body else None,
            )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.post("/allocations/{allocation_id}/substitute")
def substitute(
    allocation_id: str,
    body: ConfirmIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
    on_date=Depends(today),
):
    """在册学生（body.student_ids 的第一个）退出，按候补顺序自动替补。"""
    if not body.student_ids:
        raise HTTPException(status_code=400, detail={"code": "bad_request", "message": "需指定退出的学生"})
    allocation = session.scalar(select(Allocation).where(Allocation.id == allocation_id))
    if allocation is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "分配不存在"})
    _guard_school_allocation(principal, allocation)
    try:
        with transactional(session):
            return lifecycle.substitute(
                session, allocation_id, body.student_ids[0], on_date=on_date
            )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
