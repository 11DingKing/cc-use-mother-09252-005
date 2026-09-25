"""承诺与缩减 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...persistence.models import Commitment, CommitmentVersion, PositionRow
from ..deps import get_session, require_principal, today, transactional
from ..schemas import CommitmentIn
from ..security import Principal
from ...services import importers, reductions
from ...services.errors import ServiceError

router = APIRouter(prefix="/commitments", tags=["commitments"])


def _guard_enterprise(principal: Principal, enterprise_id: str) -> None:
    if not principal.owns_enterprise(enterprise_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能维护本企业承诺"})


@router.post("")
def import_commitment(
    body: CommitmentIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    _guard_enterprise(principal, body.enterprise_id)
    # 新建允许；若承诺已存在则必须属于该企业
    existing = session.scalar(select(Commitment).where(Commitment.id == body.commitment_id))
    if existing is not None and not principal.owns_enterprise(existing.enterprise_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "承诺归属其他企业"})
    try:
        with transactional(session):
            result = importers.import_commitment(
                session,
                batch_key=body.batch_key,
                commitment_id=body.commitment_id,
                enterprise_id=body.enterprise_id,
                quarter=body.quarter,
                positions=[p.model_dump() for p in body.positions],
                reason=body.reason,
            )
        return result
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.post("/reduce")
def reduce_commitment(
    body: CommitmentIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
    on_date=Depends(today),
):
    """企业缩减承诺：先保护已确认学生，再对候补重新编排，并保留影响说明。"""
    _guard_enterprise(principal, body.enterprise_id)
    try:
        with transactional(session):
            return reductions.apply_reduction(
                session,
                batch_key=body.batch_key,
                commitment_id=body.commitment_id,
                positions=[p.model_dump() for p in body.positions],
                reason=body.reason or "企业按生产计划缩减承诺",
                on_date=on_date,
            )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/{commitment_id}")
def get_commitment(
    commitment_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    commitment = session.scalar(select(Commitment).where(Commitment.id == commitment_id))
    if commitment is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "承诺不存在"})
    _guard_enterprise(principal, commitment.enterprise_id)
    versions = session.scalars(
        select(CommitmentVersion)
        .where(CommitmentVersion.commitment_id == commitment_id)
        .order_by(CommitmentVersion.version_no)
    ).all()
    positions = session.scalars(
        select(PositionRow).where(PositionRow.commitment_id == commitment_id)
    ).all()
    return {
        "commitment_id": commitment.id,
        "enterprise_id": commitment.enterprise_id,
        "quarter": commitment.quarter,
        "current_version": commitment.current_version,
        "versions": [
            {
                "version_no": v.version_no,
                "change_type": v.change_type,
                "total_seats": v.total_seats,
                "reason": v.reason,
                "batch_id": v.batch_id,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
        "positions": [
            {
                "code": p.code,
                "location": p.location,
                "skills": p.skills,
                "seats": p.seats,
                "active": p.active,
            }
            for p in sorted(positions, key=lambda p: p.code)
        ],
    }
