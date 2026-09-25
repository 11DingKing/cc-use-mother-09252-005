"""资格核验查询：安全培训有效期的跨期判断。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.model import parse_quarter, quarter_end, quarter_start
from ..persistence.models import PositionRow, StudentRow
from .errors import NotFound


def verify_student(
    session: Session,
    student_id: str,
    quarter: str,
    *,
    on_date: dt.date,
    position_id: str | None = None,
) -> dict:
    parse_quarter(quarter)
    student = session.scalar(select(StudentRow).where(StudentRow.id == student_id))
    if student is None:
        raise NotFound(f"学生 {student_id} 不存在")

    start, end = quarter_start(quarter), quarter_end(quarter)
    safety = student.safety_valid_until

    result: dict = {
        "student_id": student_id,
        "school_id": student.school_id,
        "quarter": quarter,
        "checked_on": on_date.isoformat(),
        "quarter_start": start.isoformat(),
        "quarter_end": end.isoformat(),
        "safety_valid_until": safety.isoformat() if safety else None,
        "skills": list(student.skills or []),
    }

    if safety is None:
        result["safety_status"] = "missing"
        result["eligible"] = False
        result["reasons"] = ["缺少安全培训记录"]
    elif safety < max(start, on_date):
        result["safety_status"] = "expired"
        result["eligible"] = False
        result["reasons"] = [f"安全培训已过期（有效期至 {safety}）"]
    elif safety < end:
        # 跨期关键情形：证书在季度开始时有效，但在季度结束前过期
        result["safety_status"] = "expires_during_quarter"
        result["eligible"] = False
        result["reasons"] = [f"安全培训不能覆盖整个季度（有效期至 {safety}，季度结束 {end}）"]
    else:
        result["safety_status"] = "valid_through_quarter"
        result["eligible"] = True
        result["reasons"] = []

    if position_id is not None:
        position = session.scalar(select(PositionRow).where(PositionRow.id == position_id))
        if position is None:
            raise NotFound(f"岗位 {position_id} 不存在")
        result["position_id"] = position_id
        result["required_skills"] = list(position.skills or [])
        missing = sorted(set(position.skills or []) - set(student.skills or []))
        result["missing_skills"] = missing
        if missing:
            result["eligible"] = False
            result.setdefault("reasons", []).append(f"技能不满足，缺少: {missing}")

    return result
