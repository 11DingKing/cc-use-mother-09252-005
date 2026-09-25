"""匹配 API：运行匹配、查看建议与缺口、查看候补。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_principal, today, transactional
from ..security import Principal, mask_student
from ...persistence.models import (
    Allocation,
    PositionRow,
    StudentRow,
    WaitlistEntry,
)
from ...services import matching as matching_service
from ...services.errors import ServiceError

router = APIRouter(tags=["matching"])


def _allocation_view(session: Session, alloc: Allocation, principal: Principal) -> dict:
    position = session.scalar(select(PositionRow).where(PositionRow.id == alloc.position_id))
    students = {
        s.id: s for s in session.scalars(select(StudentRow)).all()
    }
    members = []
    for m in alloc.members:
        if m.state in ("cancelled", "displaced"):
            continue
        view = {
            "student_id": m.student_id,
            "state": m.state,
            # 非本校/非管理员的学生 PII 按隐私边界处理
            "student": mask_student(students[m.student_id], principal)
            if m.student_id in students
            else None,
        }
        if principal.is_admin or principal.scope == "school":
            view["created_at"] = m.created_at.isoformat()
        members.append(view)
    return {
        "allocation_id": alloc.id,
        "quarter": alloc.quarter,
        "position_id": alloc.position_id,
        "enterprise_id": position.enterprise_id if position else None,
        "location": position.location if position else None,
        "school_id": alloc.school_id,
        "seats": alloc.seats,
        "status": alloc.status,
        "protected": alloc.protected,
        "impact_note": _note_for(principal, alloc),
        "run_no": alloc.run_no,
        "members": members,
    }


def _note_for(principal: Principal, alloc: Allocation) -> str:
    # 影响说明对所有合作方可见，但外校看不到具体学生姓名（说明里只有编号）
    return alloc.impact_note


@router.post("/match/{quarter}")
def run_match(
    quarter: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
    on_date=Depends(today),
):
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "管理员统一编排匹配"})
    try:
        with transactional(session):
            return matching_service.run_match(session, quarter, on_date=on_date)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/match/{quarter}/allocations")
def list_allocations(
    quarter: str,
    status_filter: str | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    q = select(Allocation).where(Allocation.quarter == quarter)
    if status_filter:
        q = q.where(Allocation.status == status_filter)
    allocations = session.scalars(q.order_by(Allocation.id)).all()
    if principal.scope == "school":
        allocations = [a for a in allocations if principal.owns_school(a.school_id)]
    elif principal.scope == "enterprise":
        # 企业只能看涉及本企业岗位的分配
        ids = {p.id for p in session.scalars(select(PositionRow)).all() if principal.owns_enterprise(p.enterprise_id)}
        allocations = [a for a in allocations if a.position_id in ids]
    return {
        "quarter": quarter,
        "allocations": [_allocation_view(session, a, principal) for a in allocations],
    }


@router.get("/match/{quarter}/waitlist")
def list_waitlist(
    quarter: str,
    position_id: str | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    q = select(WaitlistEntry).where(WaitlistEntry.quarter == quarter)
    if position_id:
        q = q.where(WaitlistEntry.position_id == position_id)
    rows = session.scalars(q.order_by(WaitlistEntry.position_id, WaitlistEntry.rank)).all()
    student_rows = {s.id: s for s in session.scalars(select(StudentRow)).all()}
    out = []
    for w in rows:
        stu = student_rows.get(w.student_id)
        item = {
            "position_id": w.position_id,
            "student_id": w.student_id,
            "rank": w.rank,
            "state": w.state,
        }
        if stu is not None:
            masked = mask_student(stu, principal)
            item["school_id"] = stu.school_id
            item["pii"] = masked["_pii"]
        out.append(item)
    return {"quarter": quarter, "waitlist": out}


@router.get("/match/{quarter}/gap")
def gap(
    quarter: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    return matching_service.gap_report(session, quarter)
