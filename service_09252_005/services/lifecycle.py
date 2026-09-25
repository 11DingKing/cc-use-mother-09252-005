"""替补与结算用例。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import (
    Allocation,
    AllocationMember,
    PositionRow,
    StudentRow,
    WaitlistEntry,
)
from .errors import Conflict, NotFound
from .ledger import record
from .reductions import _append_note, _free_seats, _insert_member, _qualifies


def substitute(
    session: Session,
    allocation_id: str,
    student_id: str,
    *,
    on_date: dt.date,
) -> dict:
    """在册学生退出 -> 按候补 rank 提升第一人补位。

    已确认学生退出后其席位空缺；补位学生以 ``proposed`` 进入，需走确认流程。
    全过程写入该分配的影响说明与账本。
    """
    allocation = session.scalar(
        select(Allocation).where(Allocation.id == allocation_id).with_for_update()
    )
    if allocation is None:
        raise NotFound(f"分配 {allocation_id} 不存在")
    if allocation.status not in ("proposed", "confirmed"):
        raise Conflict(f"分配状态为 {allocation.status}，不能替补")

    member = next(
        (m for m in allocation.members if m.student_id == student_id and m.state in ("proposed", "confirmed")),
        None,
    )
    if member is None:
        raise NotFound(f"学生 {student_id} 不在该分配的在册名单中")

    previous_state = member.state
    member.state = "cancelled"
    _append_note(allocation, f"学生 {student_id}（{previous_state}）退出，释放 1 个席位，启动候补替补")

    position = session.scalar(select(PositionRow).where(PositionRow.id == allocation.position_id))
    promotion = None
    expired = []
    while _free_seats(allocation, position) > 0:
        entry = session.scalars(
            select(WaitlistEntry)
            .join(StudentRow, StudentRow.id == WaitlistEntry.student_id)
            .where(
                WaitlistEntry.position_id == position.id,
                WaitlistEntry.state == "waiting",
                StudentRow.school_id == allocation.school_id,
            )
            .order_by(WaitlistEntry.rank, WaitlistEntry.id)
        ).first()
        if entry is None:
            break
        if not _qualifies(session, entry.student_id, position, allocation.quarter, on_date):
            entry.state = "expired"
            expired.append(entry.student_id)
            session.flush()  # autoflush=False：不立即落库会被重复读到形成死循环
            continue
        entry.state = "promoted"
        if not _insert_member(session, allocation.id, entry.student_id, allocation.quarter, "proposed"):
            entry.state = "expired"
            expired.append(entry.student_id)
            continue
        _append_note(allocation, f"候补第 {entry.rank} 位学生 {entry.student_id} 替补入岗 {position.code}（待确认）")
        promotion = {"student_id": entry.student_id, "rank": entry.rank}
        break

    record(
        session,
        event_type="allocation.substitute",
        aggregate_type="allocation",
        aggregate_id=allocation.id,
        quarter=allocation.quarter,
        payload={
            "withdrawn": student_id,
            "previous_state": previous_state,
            "promoted": promotion,
            "expired_waitlist": expired,
        },
    )
    session.flush()
    return {
        "allocation_id": allocation.id,
        "withdrawn": student_id,
        "promoted": promotion,
        "expired_waitlist": expired,
    }


def settle_quarter(
    session: Session,
    quarter: str,
    *,
    on_date: dt.date,
) -> dict:
    """季度结算：已确认席位落定为 settled；未确认的拟分配释放并说明。

    结算幂等：全部已结算时原样返回汇总，不重复处理。
    """
    # 先取全部未取消分配（含已结算），用于区分"无可结算对象"与"幂等重放"
    all_allocations = session.scalars(
        select(Allocation).where(
            Allocation.quarter == quarter, Allocation.status != "cancelled"
        )
    ).all()
    if not all_allocations:
        raise NotFound(f"季度 {quarter} 没有可结算的分配")

    if all(a.status == "settled" for a in all_allocations):
        settled_members = session.scalars(
            select(AllocationMember)
            .join(Allocation, Allocation.id == AllocationMember.allocation_id)
            .where(Allocation.quarter == quarter, AllocationMember.state == "settled")
        ).all()
        return {
            "quarter": quarter,
            "idempotent_replay": True,
            "settled_allocations": len(all_allocations),
            "settled_seats": len(settled_members),
            "released_seats": 0,
        }

    allocations = [a for a in all_allocations if a.status != "settled"]

    settled_seats = 0
    released = []
    for allocation in allocations:
        confirmed = [m for m in allocation.members if m.state == "confirmed"]
        for member in allocation.members:
            if member.state == "confirmed":
                member.state = "settled"
                settled_seats += 1
            elif member.state == "proposed":
                member.state = "cancelled"
                released.append(member.student_id)
        allocation.seats = len(confirmed)
        allocation.status = "settled"
        if released:
            _append_note(allocation, f"季度结算：{len(released)} 个未确认拟分配席位释放")

    # 未消耗候补全部过期
    waiting = session.scalars(
        select(WaitlistEntry).where(
            WaitlistEntry.quarter == quarter, WaitlistEntry.state == "waiting"
        )
    ).all()
    for w in waiting:
        w.state = "expired"

    record(
        session,
        event_type="quarter.settle",
        aggregate_type="quarter",
        aggregate_id=quarter,
        quarter=quarter,
        payload={
            "settled_allocations": len(allocations),
            "settled_seats": settled_seats,
            "released_seats": len(released),
            "released_students": released,
            "on_date": on_date.isoformat(),
        },
    )
    session.flush()
    return {
        "quarter": quarter,
        "idempotent_replay": False,
        "settled_allocations": len(allocations),
        "settled_seats": settled_seats,
        "released_seats": len(released),
        "released_students": released,
    }
