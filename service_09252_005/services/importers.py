"""导入用例：企业承诺（含版本化缩减）、院校需求、学生资格。

所有导入都必须携带 ``batch_key``：同一批次键重复提交时直接返回首次结果，
不会重复创建岗位、需求或学生，更不会重复占用名额。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.model import parse_quarter
from ..persistence.models import (
    Commitment,
    CommitmentVersion,
    DemandRow,
    ImportBatch,
    PositionRow,
    StudentRow,
)
from .errors import Conflict, ServiceError
from .ledger import record


def _replay_or_start(session: Session, batch_key: str, kind: str) -> dict | None:
    existing = session.scalar(select(ImportBatch).where(ImportBatch.batch_key == batch_key))
    if existing is not None:
        if existing.kind != kind:
            raise Conflict(f"批次键 {batch_key} 已用于 {existing.kind} 导入")
        return {"idempotent_replay": True, **existing.summary}
    return None


def _finish_batch(session: Session, batch_key: str, kind: str, summary: dict) -> dict:
    session.add(ImportBatch(batch_key=batch_key, kind=kind, summary=summary))
    return {"idempotent_replay": False, **summary}


def import_commitment(
    session: Session,
    *,
    batch_key: str,
    commitment_id: str,
    enterprise_id: str,
    quarter: str,
    positions: list[dict],
    reason: str = "",
) -> dict:
    parse_quarter(quarter)
    if not positions:
        raise ServiceError("承诺至少包含一个岗位")
    seen_codes: set[str] = set()
    for pos in positions:
        code = pos["code"]
        if code in seen_codes:
            raise ServiceError(f"同一承诺内岗位编码重复: {code}")
        seen_codes.add(code)
        if int(pos["seats"]) <= 0:
            raise ServiceError(f"岗位 {code} 的席位必须为正数")

    replay = _replay_or_start(session, batch_key, "commitment")
    if replay is not None:
        return replay

    commitment = session.scalar(
        select(Commitment).where(Commitment.id == commitment_id)
    )
    created = commitment is None
    if commitment is None:
        commitment = Commitment(
            id=commitment_id, enterprise_id=enterprise_id, quarter=quarter, current_version=0
        )
        session.add(commitment)
        session.flush()

    old_total = sum(p.seats for p in commitment.positions if p.active)
    new_total = sum(int(p["seats"]) for p in positions)
    by_code = {p.code: p for p in commitment.positions}
    incoming_codes = {p["code"] for p in positions}

    removed, added, changed = [], [], []
    for item in positions:
        code = item["code"]
        skills = sorted(item.get("skills", []))
        seats = int(item["seats"])
        location = item["location"]
        existing_pos = by_code.get(code)
        if existing_pos is None:
            session.add(
                PositionRow(
                    id=f"{commitment_id}:{code}",
                    commitment_id=commitment_id,
                    enterprise_id=enterprise_id,
                    quarter=quarter,
                    code=code,
                    location=location,
                    skills=skills,
                    seats=seats,
                    active=True,
                )
            )
            added.append(code)
        else:
            changes = {}
            if not existing_pos.active:
                existing_pos.active = True
                changes["reactivated"] = True
            if existing_pos.seats != seats:
                changes["seats"] = [existing_pos.seats, seats]
                existing_pos.seats = seats
            if existing_pos.location != location:
                changes["location"] = [existing_pos.location, location]
                existing_pos.location = location
            if existing_pos.skills != skills:
                changes["skills"] = [existing_pos.skills, skills]
                existing_pos.skills = skills
            if changes:
                changed.append({"code": code, **changes})

    for old in commitment.positions:
        if old.active and old.code not in incoming_codes:
            old.active = False
            removed.append({"code": old.code, "seats": old.seats})

    next_version = commitment.current_version + 1
    if created:
        change_type = "create"
    elif removed or new_total < old_total:
        change_type = "reduce"
    else:
        change_type = "update"

    session.add(
        CommitmentVersion(
            commitment_id=commitment_id,
            version_no=next_version,
            change_type=change_type,
            total_seats=new_total,
            reason=reason,
            batch_id=batch_key,
        )
    )
    commitment.current_version = next_version

    record(
        session,
        event_type=f"commitment.{change_type}",
        aggregate_type="commitment",
        aggregate_id=commitment_id,
        quarter=quarter,
        payload={
            "version": next_version,
            "enterprise_id": enterprise_id,
            "total_seats": new_total,
            "added": added,
            "changed": changed,
            "removed": removed,
            "reason": reason,
            "batch_key": batch_key,
        },
    )
    session.flush()

    summary = {
        "commitment_id": commitment_id,
        "version": next_version,
        "change_type": change_type,
        "total_seats": new_total,
        "positions_added": added,
        "positions_changed": changed,
        "positions_removed": removed,
    }
    return _finish_batch(session, batch_key, "commitment", summary)


def import_demands(
    session: Session,
    *,
    batch_key: str,
    quarter: str,
    demands: list[dict],
) -> dict:
    parse_quarter(quarter)
    replay = _replay_or_start(session, batch_key, "demand")
    if replay is not None:
        return replay

    created, updated = [], []
    for item in demands:
        if int(item["seats"]) <= 0:
            raise ServiceError("需求席位必须为正数")
        row = session.scalar(
            select(DemandRow).where(
                DemandRow.school_id == item["school_id"],
                DemandRow.quarter == quarter,
                DemandRow.region == item["region"],
            )
        )
        if row is None:
            session.add(
                DemandRow(
                    school_id=item["school_id"],
                    quarter=quarter,
                    region=item["region"],
                    seats=int(item["seats"]),
                    batch_id=batch_key,
                )
            )
            created.append(f"{item['school_id']}@{item['region']}")
        elif row.seats != int(item["seats"]):
            updated.append(
                {"key": f"{item['school_id']}@{item['region']}", "seats": [row.seats, int(item["seats"])]}
            )
            row.seats = int(item["seats"])

    record(
        session,
        event_type="demand.import",
        aggregate_type="quarter",
        aggregate_id=quarter,
        quarter=quarter,
        payload={"created": created, "updated": updated, "batch_key": batch_key},
    )
    summary = {
        "quarter": quarter,
        "created": len(created),
        "updated": len(updated),
    }
    return _finish_batch(session, batch_key, "demand", summary)


def import_students(
    session: Session,
    *,
    batch_key: str,
    students: list[dict],
) -> dict:
    import datetime as dt

    replay = _replay_or_start(session, batch_key, "student")
    if replay is not None:
        return replay

    created, updated, qualifications = [], [], []
    for item in students:
        safety = item.get("safety_valid_until")
        if isinstance(safety, str):
            safety = dt.date.fromisoformat(safety)
        skills = sorted(item.get("skills", []))
        row = session.scalar(select(StudentRow).where(StudentRow.id == item["id"]))
        if row is None:
            session.add(
                StudentRow(
                    id=item["id"],
                    school_id=item["school_id"],
                    name=item.get("name", ""),
                    external_no=item.get("external_no", ""),
                    skills=skills,
                    safety_valid_until=safety,
                )
            )
            created.append(item["id"])
            qualifications.append(
                {"student_id": item["id"], "safety_valid_until": safety.isoformat() if safety else None}
            )
        else:
            changes = {}
            if row.safety_valid_until != safety:
                changes["safety_valid_until"] = [
                    row.safety_valid_until.isoformat() if row.safety_valid_until else None,
                    safety.isoformat() if safety else None,
                ]
                row.safety_valid_until = safety
            if row.skills != skills:
                changes["skills"] = [row.skills, skills]
                row.skills = skills
            if item.get("name"):
                row.name = item["name"]
            row.active = True
            if changes:
                updated.append({"student_id": item["id"], **changes})
                qualifications.append(
                    {"student_id": item["id"], **changes}
                )

    record(
        session,
        event_type="student.import",
        aggregate_type="student_batch",
        aggregate_id=batch_key,
        payload={"created": created, "updated": updated, "batch_key": batch_key},
    )
    summary = {"created": len(created), "updated": len(updated)}
    return _finish_batch(session, batch_key, "student", summary)
