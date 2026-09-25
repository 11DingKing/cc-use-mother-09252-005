"""院校需求 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_principal, transactional
from ..schemas import DemandIn
from ..security import Principal
from ...persistence.models import DemandRow
from ...services import importers
from ...services.errors import ServiceError

router = APIRouter(prefix="/demands", tags=["demands"])


@router.post("")
def import_demands(
    body: DemandIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    # 院校令牌只能导入本校需求；管理员可批量导入任意院校
    if not principal.is_admin:
        if principal.scope != "school":
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "院校或管理员才能维护需求"})
        for item in body.demands:
            if not principal.owns_school(item.school_id):
                raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能维护本校需求"})
    try:
        with transactional(session):
            return importers.import_demands(
                session,
                batch_key=body.batch_key,
                quarter=body.quarter,
                demands=[d.model_dump() for d in body.demands],
            )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("")
def list_demands(
    quarter: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    rows = session.scalars(select(DemandRow).where(DemandRow.quarter == quarter)).all()
    if principal.scope == "school":
        rows = [r for r in rows if principal.owns_school(r.school_id)]
    return {
        "quarter": quarter,
        "demands": [
            {"school_id": r.school_id, "region": r.region, "seats": r.seats}
            for r in sorted(rows, key=lambda r: (r.region, r.school_id))
        ],
    }
