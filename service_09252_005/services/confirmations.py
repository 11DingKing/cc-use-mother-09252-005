"""确认用例。

并发安全有两层保护：

1. 行级检查 ``member.state == 'proposed'`` 的条件更新；
2. 数据库部分唯一索引 ``ux_student_active_seat``——两个并发请求不可能同时
   把同一学生确认到两个席位，IntegrityError 被翻译为 409。

确认时会以"当前版本账本"重新核验技能与安全培训有效期（必须覆盖季度结束日），
资格失效的学生不能确认，其席位留作候补替补。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain.model import quarter_end, quarter_start
from ..persistence.models import Allocation, PositionRow, StudentRow
from .errors import Conflict, NotFound, ServiceError
from .ledger import record


def _recheck(student: StudentRow, position: PositionRow, quarter: str, on_date: dt.date) -> str | None:
    """返回不通过原因；通过返回 None。"""
    if student.safety_valid_until is None:
        return "缺少安全培训记录"
    start, end = quarter_start(quarter), quarter_end(quarter)
    if student.safety_valid_until < max(start, on_date):
        return f"安全培训已过期（有效期至 {student.safety_valid_until}，核验日 {on_date}）"
    if student.safety_valid_until < end:
        return f"安全培训将于季度结束前过期（有效期至 {student.safety_valid_until}，季度结束 {end}）"
    missing = set(position.skills or []) - set(student.skills or [])
    if missing:
        return f"技能不满足，缺少: {sorted(missing)}"
    return None


def confirm_allocation(
    session: Session,
    allocation_id: str,
    *,
    on_date: dt.date,
    student_ids: list[str] | None = None,
) -> dict:
    allocation = session.scalar(
        select(Allocation).where(Allocation.id == allocation_id).with_for_update()
    )
    if allocation is None:
        raise NotFound(f"分配 {allocation_id} 不存在")
    if allocation.status not in ("proposed", "confirmed"):
        raise Conflict(f"分配状态为 {allocation.status}，不能确认")

    position = session.scalar(select(PositionRow).where(PositionRow.id == allocation.position_id))
    if position is None or not position.active:
        raise Conflict("岗位已被缩减撤销，不能确认；受保护的已确认学生请查看影响说明")

    wanted = set(student_ids) if student_ids is not None else None
    confirmed, failed = [], []
    for member in allocation.members:
        if member.state not in ("proposed", "confirmed"):
            continue
        if wanted is not None and member.student_id not in wanted:
            continue
        if member.state == "confirmed":
            confirmed.append(member.student_id)
            continue
        student = session.scalar(select(StudentRow).where(StudentRow.id == member.student_id))
        reason = _recheck(student, position, allocation.quarter, on_date)
        if reason is not None:
            member.state = "displaced"
            failed.append({"student_id": member.student_id, "reason": reason})
            continue
        try:
            # savepoint：唯一索引冲突时只回滚这一条，便于给出明确错误
            with session.begin_nested():
                member.state = "confirmed"
                session.flush()
        except IntegrityError:
            raise Conflict(f"学生 {member.student_id} 已在其他席位占用名额，并发确认失败")
        confirmed.append(member.student_id)

    if not confirmed and not failed:
        raise ServiceError("没有可确认的学生")

    if confirmed:
        allocation.status = "confirmed"
        allocation.protected = True

    record(
        session,
        event_type="allocation.confirm",
        aggregate_type="allocation",
        aggregate_id=allocation.id,
        quarter=allocation.quarter,
        payload={
            "confirmed": confirmed,
            "disqualified": failed,
            "on_date": on_date.isoformat(),
        },
    )
    session.flush()
    return {
        "allocation_id": allocation.id,
        "status": allocation.status,
        "confirmed": confirmed,
        "disqualified": failed,
        "freed_seats": len(failed),
    }
