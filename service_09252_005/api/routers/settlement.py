"""结算、补偿恢复与账本查询 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_admin, today, transactional
from ...persistence.models import CompensationTask, LedgerEvent
from ...services import lifecycle
from ...services.errors import ServiceError
from ...services.reductions import run_pending_compensations

router = APIRouter(tags=["settlement"])


@router.post("/quarters/{quarter}/settle")
def settle(
    quarter: str,
    session: Session = Depends(get_session),
    admin=Depends(require_admin),
    on_date=Depends(today),
):
    try:
        with transactional(session):
            return lifecycle.settle_quarter(session, quarter, on_date=on_date)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.post("/compensations/run")
def run_compensations(
    session: Session = Depends(get_session),
    admin=Depends(require_admin),
):
    """重启恢复入口：执行所有未完成补偿任务（幂等）。"""
    with transactional(session):
        reports = run_pending_compensations(session)
    return {"executed": len(reports), "reports": reports}


@router.get("/compensations")
def list_compensations(
    session: Session = Depends(get_session),
    admin=Depends(require_admin),
):
    tasks = session.scalars(select(CompensationTask).order_by(CompensationTask.id)).all()
    return {
        "tasks": [
            {
                "id": t.id,
                "task_type": t.task_type,
                "idempotency_key": t.idempotency_key,
                "status": t.status,
                "attempts": t.attempts,
                "last_error": t.last_error,
            }
            for t in tasks
        ]
    }


@router.get("/ledger")
def read_ledger(
    quarter: str | None = None,
    aggregate_type: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
    admin=Depends(require_admin),
):
    q = select(LedgerEvent)
    if quarter:
        q = q.where(LedgerEvent.quarter == quarter)
    if aggregate_type:
        q = q.where(LedgerEvent.aggregate_type == aggregate_type)
    events = session.scalars(q.order_by(LedgerEvent.id.desc()).limit(min(limit, 500))).all()
    return {
        "events": [
            {
                "id": e.id,
                "quarter": e.quarter,
                "aggregate_type": e.aggregate_type,
                "aggregate_id": e.aggregate_id,
                "event_type": e.event_type,
                "payload": e.payload,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]
    }
