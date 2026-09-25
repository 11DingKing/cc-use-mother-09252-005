"""学生档案与资格核验 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_principal, today, transactional
from ..schemas import StudentBatchIn
from ..security import Principal, mask_student
from ...persistence.models import StudentRow
from ...services import importers, qualifications
from ...services.errors import ServiceError

router = APIRouter(tags=["students"])


@router.post("/students/import")
def import_students(
    body: StudentBatchIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    if not principal.is_admin:
        if principal.scope != "school":
            raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "院校或管理员才能维护学生"})
        for item in body.students:
            if not principal.owns_school(item.school_id):
                raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能维护本校学生"})
    try:
        with transactional(session):
            return importers.import_students(
                session,
                batch_key=body.batch_key,
                students=[s.model_dump() for s in body.students],
            )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


@router.get("/students/{student_id}")
def get_student(
    student_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
):
    row = session.scalar(select(StudentRow).where(StudentRow.id == student_id))
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "学生不存在"})
    return mask_student(row, principal)


@router.get("/students/{student_id}/eligibility")
def check_eligibility(
    student_id: str,
    quarter: str,
    position_id: str | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_principal),
    on_date=Depends(today),
):
    """资格核验：技能 + 安全培训跨期有效性。企业端结果中不含学生身份明细。"""
    row = session.scalar(select(StudentRow).where(StudentRow.id == student_id))
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "学生不存在"})
    if principal.scope == "school" and not principal.owns_school(row.school_id):
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "只能核验本校学生"})
    try:
        result = qualifications.verify_student(
            session, student_id, quarter, on_date=on_date, position_id=position_id
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    if not principal.is_admin and not principal.owns_school(row.school_id):
        # 企业：结论可返回（原因只含日期/技能缺口，不含身份信息），标记脱敏级别
        result["pii"] = "masked"
    return result
