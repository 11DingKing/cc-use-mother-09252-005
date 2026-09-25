"""匹配用例：加载当前版本账本视图 -> 领域匹配 -> 持久化建议与候补。"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..domain import model as dm
from ..domain.matching import build_matches
from ..persistence.models import (
    Allocation,
    AllocationMember,
    Commitment,
    DemandRow,
    PositionRow,
    StudentRow,
    WaitlistEntry,
)
from .errors import NotFound
from .ledger import record


def _load_positions(session: Session, quarter: str) -> list[dm.Position]:
    rows = session.scalars(
        select(PositionRow).where(
            PositionRow.quarter == quarter, PositionRow.active.is_(True)
        )
    ).all()
    return [
        dm.Position(
            id=p.id,
            commit_id=p.commitment_id,
            enterprise_id=p.enterprise_id,
            location=p.location,
            skills=frozenset(p.skills or []),
            seats=p.seats,
            active=p.active,
        )
        for p in rows
    ]


def _load_demands(session: Session, quarter: str, *, deduct_occupied: bool = False) -> list[dm.Demand]:
    rows = session.scalars(select(DemandRow).where(DemandRow.quarter == quarter)).all()
    if not deduct_occupied:
        return [dm.Demand(school_id=r.school_id, region=r.region, seats=r.seats) for r in rows]
    # 已占座（拟分配/已确认/已结算）席位从需求中扣除，得到真实剩余需求，
    # 避免部分确认后重新匹配让已满足的院校再次优先。
    occupied_rows = session.execute(
        select(Allocation.school_id, PositionRow.location, func.count(AllocationMember.id))
        .join(AllocationMember, AllocationMember.allocation_id == Allocation.id)
        .join(PositionRow, PositionRow.id == Allocation.position_id)
        .where(
            Allocation.quarter == quarter,
            Allocation.status != "cancelled",
            AllocationMember.state.in_(["proposed", "confirmed", "settled"]),
        )
        .group_by(Allocation.school_id, PositionRow.location)
    ).all()
    occupied = {(school_id, region): count for school_id, region, count in occupied_rows}
    result = []
    for r in rows:
        remaining = max(0, r.seats - occupied.get((r.school_id, r.region), 0))
        result.append(dm.Demand(school_id=r.school_id, region=r.region, seats=remaining))
    return result


def _occupied_students(session: Session, quarter: str) -> set[str]:
    """已处于有效分配（拟分配/已确认/已结算）的学生视为占位，匹配时不可再用。"""
    rows = session.scalars(
        select(AllocationMember.student_id)
        .join(Allocation, Allocation.id == AllocationMember.allocation_id)
        .where(
            Allocation.quarter == quarter,
            Allocation.status != "cancelled",
            AllocationMember.state.in_(["proposed", "confirmed", "settled"]),
        )
    ).all()
    return set(rows)


def _load_students(session: Session, quarter: str, on_date: dt.date) -> list[dm.Student]:
    occupied = _occupied_students(session, quarter)
    rows = session.scalars(select(StudentRow).where(StudentRow.active.is_(True))).all()
    result = []
    for s in rows:
        if s.id in occupied or s.safety_valid_until is None:
            continue
        result.append(
            dm.Student(
                id=s.id,
                school_id=s.school_id,
                region="",
                skills=frozenset(s.skills or []),
                safety_valid_until=s.safety_valid_until,
                status="eligible",
            )
        )
    return result


def gap_report(session: Session, quarter: str) -> dict:
    positions = _load_positions(session, quarter)
    demands = _load_demands(session, quarter)
    report = dm.quota_gap(positions, demands)
    report["quarter"] = quarter
    return report


def run_match(session: Session, quarter: str, *, on_date: dt.date | None = None) -> dict:
    """生成（或重新生成）匹配建议。

    只替换处于 ``proposed`` 的旧建议；已确认 / 已结算的分配与学生一律不动，
    且这些学生不再参与新的匹配，避免重复占用。
    """
    on_date = on_date or dt.date.today()
    commitments = session.scalars(
        select(Commitment).where(Commitment.quarter == quarter)
    ).all()
    if not commitments:
        raise NotFound(f"季度 {quarter} 尚无企业承诺")

    # 第一步：先失效上一轮的拟分配建议（锁定岗位的分配受保护，不动）。
    # 必须先落库（autoflush=False），后续占用/需求统计才不会读到旧 proposed 状态。
    old_allocations = session.scalars(
        select(Allocation).where(
            Allocation.quarter == quarter, Allocation.status == "proposed"
        )
    ).all()
    cancelled_members = 0
    regenerated_position_ids = {a.position_id for a in old_allocations}
    for alloc in old_allocations:
        for member in alloc.members:
            if member.state == "proposed":
                member.state = "cancelled"
                cancelled_members += 1
        alloc.status = "cancelled"
        alloc.impact_note = (alloc.impact_note + " | 被新一轮匹配取代").strip(" |")

    # 只重建"重新生成建议"岗位的候补；锁定岗位（已确认）的候补保留，
    # 否则院校在确认后做替补时名单会丢失。
    if regenerated_position_ids:
        old_waiting = session.scalars(
            select(WaitlistEntry).where(
                WaitlistEntry.quarter == quarter,
                WaitlistEntry.state == "waiting",
                WaitlistEntry.position_id.in_(regenerated_position_ids),
            )
        ).all()
        for w in old_waiting:
            w.state = "cancelled"
    session.flush()

    # 第二步：基于清理后的当前视图加载匹配输入
    positions = _load_positions(session, quarter)
    # 已有已确认/已结算分配的岗位视为锁定，不参与重新匹配（保护已确认学生）
    locked_rows = session.scalars(
        select(Allocation.position_id).where(
            Allocation.quarter == quarter,
            Allocation.status.in_(["confirmed", "settled", "revoked_protected"]),
        )
    ).all()
    locked = set(locked_rows)

    demands = _load_demands(session, quarter, deduct_occupied=True)
    students = _load_students(session, quarter, on_date)

    suggestions = build_matches(
        [p for p in positions if p.id not in locked],
        students,
        demands,
        quarter,
        on_date=on_date,
    )
    matched_student_ids = {sid for s in suggestions for sid in s.matched_student_ids}

    all_previous = session.scalars(
        select(Allocation).where(Allocation.quarter == quarter)
    ).all()
    run_no = (max((a.run_no for a in all_previous), default=0) + 1)

    created_allocations = []
    for sug in suggestions:
        alloc = Allocation(
            id=f"alloc-{uuid.uuid4().hex[:16]}",
            quarter=quarter,
            position_id=sug.position_id,
            school_id=sug.school_id,
            seats=sug.seats,
            status="proposed",
            run_no=run_no,
        )
        session.add(alloc)
        session.flush()
        for sid in sug.matched_student_ids:
            session.add(
                AllocationMember(
                    allocation_id=alloc.id,
                    student_id=sid,
                    quarter=quarter,
                    state="proposed",
                )
            )
        created_allocations.append(
            {
                "allocation_id": alloc.id,
                "position_id": sug.position_id,
                "school_id": sug.school_id,
                "region": sug.region,
                "seats": sug.seats,
                "student_ids": list(sug.matched_student_ids),
            }
        )

    _build_waitlist(
        session,
        quarter,
        {sug.position_id: sug.school_id for sug in suggestions},
        students,
        matched_student_ids,
        on_date,
    )

    fairness = dm.region_fairness_score(suggestions, demands)
    record(
        session,
        event_type="match.run",
        aggregate_type="quarter",
        aggregate_id=quarter,
        quarter=quarter,
        payload={
            "run_no": run_no,
            "allocations": len(created_allocations),
            "matched_seats": sum(a["seats"] for a in created_allocations),
            "cancelled_proposed_members": cancelled_members,
            "fairness_score": fairness,
        },
    )
    session.flush()
    return {
        "quarter": quarter,
        "run_no": run_no,
        "fairness_score": fairness,
        "allocations": created_allocations,
        "gap": gap_report(session, quarter),
    }


def _build_waitlist(
    session: Session,
    quarter: str,
    position_schools: dict[str, str],
    students: list[dm.Student],
    matched_student_ids: set[str],
    on_date: dt.date,
) -> int:
    """对每个已分配岗位，把"获得该岗位院校"中合格但未入选的学生排成候补。"""
    positions = session.scalars(
        select(PositionRow).where(PositionRow.id.in_(position_schools))
    ).all() if position_schools else []
    pos_by_id = {p.id: p for p in positions}
    added = 0
    for position_id, school_id in position_schools.items():
        pos = pos_by_id.get(position_id)
        if pos is None:
            continue
        position = dm.Position(
            id=pos.id,
            commit_id=pos.commitment_id,
            enterprise_id=pos.enterprise_id,
            location=pos.location,
            skills=frozenset(pos.skills or []),
            seats=pos.seats,
            active=pos.active,
        )
        rank = 0
        for stu in sorted(students, key=lambda s: (s.safety_valid_until, s.id)):
            if stu.school_id != school_id:
                continue
            if stu.id in matched_student_ids:
                continue
            if not dm.is_qualified(stu, position, quarter, on_date=on_date):
                continue
            rank += 1
            session.add(
                WaitlistEntry(
                    quarter=quarter,
                    position_id=position_id,
                    student_id=stu.id,
                    rank=rank,
                    skills=list(stu.skills),
                    state="waiting",
                )
            )
            added += 1
    return added
