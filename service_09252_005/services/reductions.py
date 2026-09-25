"""承诺缩减与补偿编排。

规则
----
* 已确认学生一律保护：席位缩减时先移出 *拟分配* 学生；即使缩减到已确认人数
  以下，已确认学生仍保留（企业须履行），分配上写影响说明。
* 岗位整体撤销时，已确认学生随岗位保护并附说明；其余学生进入候补重编排。
* 候补重新编排：被挤出的学生优先补入本校其他岗位的空缺席位，补不上的按序
  进入这些岗位的候补；随后所有岗位的候补按 rank 统一提升。
* 补偿以任务形式落库（pending），执行成功才标记 done；进程重启后
  :func:`run_pending_compensations` 会继续执行未完成任务。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain.model import quarter_end
from ..persistence.models import (
    Allocation,
    AllocationMember,
    Commitment,
    CompensationTask,
    PositionRow,
    StudentRow,
    WaitlistEntry,
)
from .errors import NotFound
from .importers import import_commitment
from .ledger import record

TASK_TYPE = "commitment_reduction"

_OCCUPYING = ("proposed", "confirmed")


def _active_allocation(session: Session, position_id: str) -> Allocation | None:
    return session.scalar(
        select(Allocation).where(
            Allocation.position_id == position_id,
            Allocation.status.in_(["proposed", "confirmed"]),
        )
    )


def _occupying_members(allocation: Allocation) -> list[AllocationMember]:
    return [m for m in allocation.members if m.state in _OCCUPYING]


def _qualifies(session: Session, student_id: str, position: PositionRow, quarter: str, on_date: dt.date) -> bool:
    student = session.scalar(select(StudentRow).where(StudentRow.id == student_id))
    if student is None or student.safety_valid_until is None:
        return False
    if student.safety_valid_until < quarter_end(quarter):
        return False
    if student.safety_valid_until < on_date:
        return False
    return set(position.skills or []).issubset(set(student.skills or []))


def _append_note(allocation: Allocation, note: str) -> None:
    allocation.impact_note = (allocation.impact_note + " | " + note).strip(" |")


def apply_reduction(
    session: Session,
    *,
    batch_key: str,
    commitment_id: str,
    positions: list[dict],
    reason: str,
    on_date: dt.date,
) -> dict:
    """导入缩减后的承诺版本并执行（或排队）补偿编排。"""
    result = import_commitment(
        session,
        batch_key=batch_key,
        commitment_id=commitment_id,
        enterprise_id=_enterprise_id(session, commitment_id),
        quarter=_quarter(session, commitment_id),
        positions=positions,
        reason=reason,
    )

    if result.get("idempotent_replay"):
        # 重复导入：补偿任务以 batch_key 幂等，绝不重复执行
        return {"import": result, "compensation": "already_scheduled"}

    shrunk = {
        c["code"]: c["seats"][1]
        for c in result.get("positions_changed", [])
        if "seats" in c
    }
    removed = [r["code"] for r in result.get("positions_removed", [])]
    if not shrunk and not removed:
        return {"import": result, "compensation": "not_needed"}

    payload = {
        "commitment_id": commitment_id,
        "quarter": _quarter(session, commitment_id),
        "shrunk": shrunk,
        "removed": removed,
        "reason": reason,
        "on_date": on_date.isoformat(),
    }
    _enqueue(session, batch_key, payload)
    # 先落库"新版本 + pending 任务"：即使补偿执行失败或进程此刻退出，
    # 重启后仍能凭该任务继续，不会丢失缩减事实。
    session.commit()
    reports = run_pending_compensations(session)
    session.commit()
    return {"import": result, "compensation": reports}


def _insert_member(session: Session, allocation_id: str, student_id: str, quarter: str, state: str) -> bool:
    """占座插入；若学生已在别处占用席位（并发编排），返回 False 而不是中断事务。"""
    from ..persistence.models import AllocationMember

    try:
        with session.begin_nested():
            session.add(
                AllocationMember(
                    allocation_id=allocation_id, student_id=student_id, quarter=quarter, state=state
                )
            )
        return True
    except IntegrityError:
        return False


def _enterprise_id(session: Session, commitment_id: str) -> str:
    commitment = session.scalar(select(Commitment).where(Commitment.id == commitment_id))
    if commitment is None:
        raise NotFound(f"承诺 {commitment_id} 不存在")
    return commitment.enterprise_id


def _quarter(session: Session, commitment_id: str) -> str:
    commitment = session.scalar(select(Commitment).where(Commitment.id == commitment_id))
    if commitment is None:
        raise NotFound(f"承诺 {commitment_id} 不存在")
    return commitment.quarter


def _enqueue(session: Session, batch_key: str, payload: dict) -> None:
    session.add(
        CompensationTask(
            task_type=TASK_TYPE,
            idempotency_key=batch_key,
            payload=payload,
            status="pending",
        )
    )


def run_pending_compensations(session: Session) -> list[dict]:
    """执行所有未完成（pending / 之前 failed）的补偿任务。"""
    tasks = session.scalars(
        select(CompensationTask)
        .where(CompensationTask.status.in_(["pending", "failed"]))
        .order_by(CompensationTask.id)
    ).all()
    reports = []
    for task in tasks:
        report = _run_one(session, task)
        reports.append(report)
    return reports


def _run_one(session: Session, task: CompensationTask) -> dict:
    task.attempts += 1
    task.status = "pending"
    try:
        if task.task_type == TASK_TYPE:
            impact = _reorganize(session, task.payload, dt.date.fromisoformat(task.payload["on_date"]))
        else:  # pragma: no cover - 未来任务类型的防御分支
            raise ValueError(f"未知补偿任务类型: {task.task_type}")
        task.status = "done"
        task.last_error = ""
        record(
            session,
            event_type="compensation.done",
            aggregate_type="compensation_task",
            aggregate_id=str(task.id),
            quarter=task.payload.get("quarter"),
            payload={"idempotency_key": task.idempotency_key, "impact": impact, "attempts": task.attempts},
        )
        return {"task_id": task.id, "status": "done", "impact": impact}
    except Exception as exc:  # 失败保留任务，重启后继续
        task.status = "failed"
        task.last_error = repr(exc)
        record(
            session,
            event_type="compensation.failed",
            aggregate_type="compensation_task",
            aggregate_id=str(task.id),
            quarter=task.payload.get("quarter") if task.payload else None,
            payload={"error": repr(exc), "attempts": task.attempts},
        )
        return {"task_id": task.id, "status": "failed", "error": repr(exc)}


def _reorganize(session: Session, payload: dict, on_date: dt.date) -> dict:
    quarter = payload["quarter"]
    displaced: list[str] = []
    protected: list[dict] = []
    touched: list[str] = []

    for code in payload.get("removed", []):
        position_id = f"{payload['commitment_id']}:{code}"
        allocation = _active_allocation(session, position_id)
        if allocation is None:
            continue
        proposed = [m for m in allocation.members if m.state == "proposed"]
        confirmed = [m for m in allocation.members if m.state == "confirmed"]
        for member in proposed:
            member.state = "displaced"
            displaced.append(member.student_id)
        for member in confirmed:  # 岗位撤销也要保护已确认学生
            protected.append({"student_id": member.student_id, "position_id": position_id})
        allocation.protected = True
        allocation.status = "revoked_protected"
        allocation.seats = len(allocation.members) - len(proposed)
        _append_note(
            allocation,
            f"岗位 {code} 已被企业撤销（{payload.get('reason','')}）；"
            f"{len(confirmed)} 名已确认学生受保护保留，{len(proposed)} 名拟分配学生进入候补重编排",
        )
        touched.append(allocation.id)

    for code, new_seats in payload.get("shrunk", {}).items():
        position_id = f"{payload['commitment_id']}:{code}"
        allocation = _active_allocation(session, position_id)
        if allocation is None:
            continue
        confirmed = sorted(
            [m for m in allocation.members if m.state == "confirmed"],
            key=lambda m: (m.created_at or dt.datetime.min, m.id),
        )
        proposed = sorted(
            [m for m in allocation.members if m.state == "proposed"],
            key=lambda m: (m.created_at or dt.datetime.min, m.id),
        )
        # 先保已确认；拟分配只保留总名额内的部分
        keep_proposed = max(0, new_seats - len(confirmed))
        moved = proposed[keep_proposed:]
        for member in moved:
            member.state = "displaced"
            displaced.append(member.student_id)
        allocation.seats = len(confirmed) + min(len(proposed), keep_proposed)
        if len(confirmed) > new_seats:
            allocation.protected = True
        _append_note(
            allocation,
            f"岗位 {code} 席位缩减为 {new_seats}：保护已确认 {len(confirmed)} 人，"
            f"移出拟分配 {len(moved)} 人进入候补重编排",
        )
        touched.append(allocation.id)

    session.flush()
    placements = _reorchestrate_students(session, quarter, displaced, on_date)
    promotions = _promote_waitlists(session, quarter, on_date)

    record(
        session,
        event_type="commitment.reduced",
        aggregate_type="commitment",
        aggregate_id=payload["commitment_id"],
        quarter=quarter,
        payload={
            "removed": payload.get("removed", []),
            "shrunk": payload.get("shrunk", {}),
            "displaced": displaced,
            "protected": protected,
            "re_placed": placements["placed"],
            "re_waitlisted": placements["waitlisted"],
            "promotions": promotions,
            "reason": payload.get("reason", ""),
        },
    )
    return {
        "touched_allocations": touched,
        "protected_confirmed": protected,
        "displaced": displaced,
        "re_placed": placements["placed"],
        "re_waitlisted": placements["waitlisted"],
        "waitlist_promotions": promotions,
    }


def _school_allocations(session: Session, quarter: str, school_id: str) -> list[tuple[Allocation, PositionRow]]:
    rows = session.scalars(
        select(Allocation)
        .join(PositionRow, PositionRow.id == Allocation.position_id)
        .where(
            Allocation.quarter == quarter,
            Allocation.status != "cancelled",
            Allocation.school_id == school_id,
            PositionRow.active.is_(True),
        )
    ).all()
    out = []
    for alloc in rows:
        pos = session.scalar(select(PositionRow).where(PositionRow.id == alloc.position_id))
        out.append((alloc, pos))
    return out


def _free_seats(allocation: Allocation, position: PositionRow) -> int:
    occupied = len(_occupying_members(allocation))
    return max(0, position.seats - occupied)


def _reorchestrate_students(
    session: Session, quarter: str, displaced: list[str], on_date: dt.date
) -> dict:
    """被挤出的学生优先补本校其他岗位空席，其余进入这些岗位候补。"""
    placed, waitlisted = [], []
    for student_id in displaced:
        student = session.scalar(select(StudentRow).where(StudentRow.id == student_id))
        if student is None:
            continue
        options = [
            (alloc, pos)
            for alloc, pos in _school_allocations(session, quarter, student.school_id)
            if _qualifies(session, student_id, pos, quarter, on_date)
        ]
        options.sort(key=lambda pair: (pair[1].location, pair[1].id))
        target = next(((a, p) for a, p in options if _free_seats(a, p) > 0), None)
        if target is not None:
            alloc, pos = target
            if _insert_member(session, alloc.id, student_id, quarter, "proposed"):
                _append_note(alloc, f"缩减补偿：学生 {student_id} 重新编排至本岗位 {pos.code}")
                placed.append({"student_id": student_id, "position_id": pos.id})
            else:
                waitlisted.append({"student_id": student_id, "position_id": pos.id})
            continue
        # 补不上：进入所有合格本校岗位的候补，rank 排在现有候补之后
        for alloc, pos in options:
            max_rank = session.scalar(
                select(WaitlistEntry.rank)
                .where(
                    WaitlistEntry.position_id == pos.id,
                    WaitlistEntry.state == "waiting",
                )
                .order_by(WaitlistEntry.rank.desc())
            )
            try:
                with session.begin_nested():
                    session.add(
                        WaitlistEntry(
                            quarter=quarter,
                            position_id=pos.id,
                            student_id=student_id,
                            rank=(max_rank or 0) + 1,
                            skills=list(student.skills or []),
                            state="waiting",
                        )
                    )
                waitlisted.append({"student_id": student_id, "position_id": pos.id})
            except IntegrityError:
                # 该学生已在此岗位候补（重复编排），跳过
                continue
    return {"placed": placed, "waitlisted": waitlisted}


def _promote_waitlists(session: Session, quarter: str, on_date: dt.date) -> list[dict]:
    """所有有空缺的岗位按候补 rank 顺序提升。"""
    promotions = []
    allocations = session.scalars(
        select(Allocation).where(
            Allocation.quarter == quarter, Allocation.status != "cancelled"
        )
    ).all()
    for alloc in sorted(allocations, key=lambda a: a.id):
        position = session.scalar(select(PositionRow).where(PositionRow.id == alloc.position_id))
        if position is None or not position.active:
            continue
        while _free_seats(alloc, position) > 0:
            entry = session.scalars(
                select(WaitlistEntry)
                .join(StudentRow, StudentRow.id == WaitlistEntry.student_id)
                .where(
                    WaitlistEntry.position_id == position.id,
                    WaitlistEntry.state == "waiting",
                    StudentRow.school_id == alloc.school_id,
                )
                .order_by(WaitlistEntry.rank, WaitlistEntry.id)
            ).first()
            if entry is None:
                break
            if not _qualifies(session, entry.student_id, position, quarter, on_date):
                entry.state = "expired"
                session.flush()  # autoflush=False：避免同一事务内重复读到
                continue
            entry.state = "promoted"
            if _insert_member(session, alloc.id, entry.student_id, quarter, "proposed"):
                _append_note(alloc, f"候补提升：学生 {entry.student_id} 补入岗位 {position.code}")
                promotions.append({"student_id": entry.student_id, "position_id": position.id})
            else:
                # 学生在别处占座（并发），该候补留给下一个
                entry.state = "expired"
                continue
            session.flush()
    return promotions
