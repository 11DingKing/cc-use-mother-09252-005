"""应用服务：承诺、资格核验、匹配、确认、替补、结算等用例。

所有状态变化在写事务内完成并追加版本账本事件；导入用例支持
幂等键（重复导入返回首次结果，不重复占用名额）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from . import domain, matching
from .compensation import CompensationRunner
from .errors import (
    CapacityExceeded,
    Conflict,
    Forbidden,
    IdempotencyConflict,
    InvalidState,
    NotFound,
    QualificationExpired,
    ValidationError,
)
from .ledger import Ledger
from .ports import Clock, IdGenerator
from .privacy import Actor, require_admin, student_view
from .store import Store


def _hash_payload(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AppService:
    def __init__(self, store: Store, ledger: Ledger, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._ids = ids
        self.compensation = CompensationRunner(store, ledger, clock, ids)

    # ================================================================== 幂等
    def _idempotent(
        self,
        endpoint: str,
        key: str | None,
        payload: Any,
        work: Callable[[sqlite3.Connection], dict],
    ) -> tuple[dict, bool]:
        """幂等执行：返回 (结果, 是否重放)。

        同一幂等键 + 相同请求体 → 返回首次存储的结果；
        同一幂等键 + 不同请求体 → 409。
        """
        if not key:
            with self._store.write_tx() as tx:
                return work(tx), False
        digest = _hash_payload({"endpoint": endpoint, "payload": payload})
        with self._store.write_tx() as tx:
            row = tx.execute(
                "SELECT endpoint, request_hash, response FROM idempotency_keys WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if row["endpoint"] != endpoint or row["request_hash"] != digest:
                    raise IdempotencyConflict("幂等键已被不同请求使用")
                return json.loads(row["response"]), True
            result = work(tx)
            tx.execute(
                "INSERT INTO idempotency_keys (key, endpoint, request_hash, response, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, endpoint, digest, json.dumps(result, ensure_ascii=False),
                 self._clock.now().isoformat()),
            )
            return result, False

    # ================================================================== 主体档案
    def create_enterprise(self, actor: Actor, payload: dict) -> dict:
        require_admin(actor)
        name, region = payload.get("name"), payload.get("region")
        if not name or not region:
            raise ValidationError("企业名称与所在地区必填")
        enterprise_id = payload.get("id") or self._ids.new_id("ent")
        now = self._clock.now().isoformat()
        with self._store.write_tx() as tx:
            tx.execute(
                "INSERT INTO enterprises (id, name, region, created_at) VALUES (?, ?, ?, ?)",
                (enterprise_id, name, region, now),
            )
            self._ledger.append(tx, "enterprise.registered", enterprise_id, None,
                                {"name": name, "region": region})
        return {"id": enterprise_id, "name": name, "region": region}

    def create_school(self, actor: Actor, payload: dict) -> dict:
        require_admin(actor)
        name, region = payload.get("name"), payload.get("region")
        if not name or not region:
            raise ValidationError("院校名称与所在地区必填")
        school_id = payload.get("id") or self._ids.new_id("sch")
        now = self._clock.now().isoformat()
        with self._store.write_tx() as tx:
            tx.execute(
                "INSERT INTO schools (id, name, region, created_at) VALUES (?, ?, ?, ?)",
                (school_id, name, region, now),
            )
            self._ledger.append(tx, "school.registered", school_id, None,
                                {"name": name, "region": region})
        return {"id": school_id, "name": name, "region": region}

    # ================================================================== 承诺
    def import_commitment(self, actor: Actor, payload: dict, idem_key: str | None) -> dict:
        """导入企业季度承诺（全量快照语义，幂等）。

        同一 (企业, 季度) 重复导入按自然键覆盖，不会重复占用名额；
        岗位容量下调自动生成补偿作业并立即执行。
        """
        enterprise_id = payload.get("enterprise_id")
        quarter = payload.get("quarter")
        positions = payload.get("positions") or []
        if not enterprise_id or not quarter:
            raise ValidationError("enterprise_id 与 quarter 必填")
        domain.parse_quarter(quarter)
        if actor.role == "enterprise" and actor.id != enterprise_id:
            raise Forbidden("企业只能导入本企业的承诺")
        if actor.role not in ("admin", "enterprise"):
            raise Forbidden("需要企业或平台权限")
        seen_keys: set[str] = set()
        for p in positions:
            if not p.get("position_key") or not p.get("title") or not p.get("region"):
                raise ValidationError("岗位缺少 position_key/title/region")
            if p["position_key"] in seen_keys:
                raise ValidationError("岗位 position_key 重复",
                                      details={"position_key": p["position_key"]})
            seen_keys.add(p["position_key"])
            if not isinstance(p.get("capacity"), int) or p["capacity"] < 0:
                raise ValidationError("岗位 capacity 必须为非负整数")

        def work(tx: sqlite3.Connection) -> dict:
            if tx.execute("SELECT 1 FROM enterprises WHERE id=?",
                          (enterprise_id,)).fetchone() is None:
                raise NotFound(f"企业不存在: {enterprise_id}")
            now = self._clock.now().isoformat()
            row = tx.execute(
                "SELECT id FROM commitments WHERE enterprise_id=? AND quarter=?",
                (enterprise_id, quarter),
            ).fetchone()
            if row is None:
                commitment_id = self._ids.new_id("cmt")
                tx.execute(
                    "INSERT INTO commitments (id, enterprise_id, quarter, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
                    (commitment_id, enterprise_id, quarter, now, now),
                )
            else:
                commitment_id = row["id"]
                tx.execute("UPDATE commitments SET updated_at=? WHERE id=?",
                           (now, commitment_id))
            reductions: list[dict] = []
            upserted: list[dict] = []
            for p in positions:
                skills = sorted(set(p.get("skills") or []))
                existing = tx.execute(
                    "SELECT id, capacity FROM positions WHERE commitment_id=? AND position_key=?",
                    (commitment_id, p["position_key"]),
                ).fetchone()
                if existing is None:
                    position_id = self._ids.new_id("pos")
                    tx.execute(
                        "INSERT INTO positions"
                        " (id, commitment_id, position_key, title, region, skills, capacity,"
                        "  indivisible, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                        (position_id, commitment_id, p["position_key"], p["title"], p["region"],
                         json.dumps(skills, ensure_ascii=False), p["capacity"], now, now),
                    )
                    self._ledger.append(tx, "position.added", position_id, quarter,
                                        {"position_key": p["position_key"],
                                         "capacity": p["capacity"]})
                else:
                    position_id = existing["id"]
                    if existing["capacity"] != p["capacity"]:
                        if p["capacity"] < existing["capacity"]:
                            reductions.append({
                                "position_id": position_id,
                                "old_capacity": existing["capacity"],
                                "new_capacity": p["capacity"],
                            })
                        tx.execute(
                            "UPDATE positions SET capacity=?, updated_at=? WHERE id=?",
                            (p["capacity"], now, position_id),
                        )
                        self._ledger.append(tx, "position.capacity_changed", position_id, quarter,
                                            {"old": existing["capacity"], "new": p["capacity"]})
                    tx.execute(
                        "UPDATE positions SET title=?, region=?, skills=?, updated_at=? WHERE id=?",
                        (p["title"], p["region"], json.dumps(skills, ensure_ascii=False),
                         now, position_id),
                    )
                upserted.append({"position_id": position_id, "position_key": p["position_key"],
                                 "capacity": p["capacity"]})
            # 快照中缺席的岗位视为容量降为 0
            if seen_keys:
                placeholders = ",".join("?" for _ in seen_keys)
                stale = tx.execute(
                    f"SELECT id, capacity FROM positions WHERE commitment_id=?"
                    f" AND position_key NOT IN ({placeholders}) AND capacity > 0",
                    (commitment_id, *seen_keys),
                ).fetchall()
            else:
                stale = tx.execute(
                    "SELECT id, capacity FROM positions WHERE commitment_id=? AND capacity > 0",
                    (commitment_id,),
                ).fetchall()
            for row in stale:
                reductions.append({"position_id": row["id"], "old_capacity": row["capacity"],
                                   "new_capacity": 0})
            self._ledger.append(tx, "commitment.imported", commitment_id, quarter,
                                {"positions": len(positions), "reductions": len(reductions)})
            return {"commitment_id": commitment_id, "enterprise_id": enterprise_id,
                    "quarter": quarter, "positions": upserted, "reductions": reductions}

        result, replayed = self._idempotent("commitments.import", idem_key, payload, work)
        if not replayed:
            jobs = []
            for r in result["reductions"]:
                jobs.append(self._start_compensation(
                    result["commitment_id"], r["position_id"], quarter,
                    r["old_capacity"], r["new_capacity"]))
            result["compensation_jobs"] = jobs
        else:
            result.setdefault("compensation_jobs", [])
        result["replayed"] = replayed
        return result

    def reduce_commitment(self, actor: Actor, commitment_id: str, payload: dict) -> dict:
        """企业主动缩减某岗位容量：先保护已确认，再重排候补。"""
        position_id = payload.get("position_id")
        new_capacity = payload.get("new_capacity")
        if not position_id or not isinstance(new_capacity, int) or new_capacity < 0:
            raise ValidationError("position_id 与非负 new_capacity 必填")
        commitment = self._store.read(
            lambda c: c.execute("SELECT * FROM commitments WHERE id=?",
                                (commitment_id,)).fetchone())
        if commitment is None:
            raise NotFound(f"承诺不存在: {commitment_id}")
        self._check_enterprise_scope(actor, commitment["enterprise_id"])
        position = self._store.read(
            lambda c: c.execute(
                "SELECT * FROM positions WHERE id=? AND commitment_id=?",
                (position_id, commitment_id)).fetchone())
        if position is None:
            raise NotFound(f"岗位不存在: {position_id}")
        if new_capacity >= position["capacity"]:
            raise ValidationError("new_capacity 必须小于当前容量",
                                  details={"capacity": position["capacity"]})
        job_id = self._start_compensation(commitment_id, position_id,
                                          commitment["quarter"],
                                          position["capacity"], new_capacity)
        return self.compensation.get_job(job_id)

    def _start_compensation(self, commitment_id: str, position_id: str, quarter: str,
                            old_capacity: int, new_capacity: int) -> str:
        with self._store.write_tx() as tx:
            job_id = self.compensation.create_job(
                tx, commitment_id=commitment_id, position_id=position_id, quarter=quarter,
                old_capacity=old_capacity, new_capacity=new_capacity)
        self.compensation.run(job_id)
        return job_id

    def get_commitment(self, actor: Actor, commitment_id: str) -> dict:
        def load(conn: sqlite3.Connection) -> dict:
            row = conn.execute("SELECT * FROM commitments WHERE id=?",
                               (commitment_id,)).fetchone()
            if row is None:
                raise NotFound(f"承诺不存在: {commitment_id}")
            positions = conn.execute(
                "SELECT * FROM positions WHERE commitment_id=? ORDER BY position_key",
                (commitment_id,)).fetchall()
            result = {
                "id": row["id"], "enterprise_id": row["enterprise_id"],
                "quarter": row["quarter"], "status": row["status"],
                "positions": [],
            }
            for p in positions:
                counts = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM allocations WHERE position_id=?"
                    " GROUP BY status", (p["id"],)).fetchall()
                by_status = {r["status"]: r["n"] for r in counts}
                result["positions"].append({
                    "id": p["id"], "position_key": p["position_key"], "title": p["title"],
                    "region": p["region"], "skills": json.loads(p["skills"]),
                    "capacity": p["capacity"], "indivisible": bool(p["indivisible"]),
                    "confirmed": by_status.get("CONFIRMED", 0),
                    "proposed": by_status.get("PROPOSED", 0),
                    "waitlisted": by_status.get("WAITLISTED", 0),
                })
            return result

        result = self._store.read(load)
        if actor.role == "enterprise" and actor.id != result["enterprise_id"]:
            raise Forbidden("无权查看其他企业的承诺")
        if actor.role == "student":
            raise Forbidden("学生无权查看承诺明细")
        return result

    # ================================================================== 需求与学生
    def import_demands(self, actor: Actor, payload: dict, idem_key: str | None) -> dict:
        school_id, quarter = payload.get("school_id"), payload.get("quarter")
        demands = payload.get("demands") or []
        if not school_id or not quarter:
            raise ValidationError("school_id 与 quarter 必填")
        domain.parse_quarter(quarter)
        self._check_school_scope(actor, school_id)
        for d in demands:
            if not d.get("region") or not d.get("skill"):
                raise ValidationError("需求缺少 region/skill")
            if not isinstance(d.get("count"), int) or d["count"] < 0:
                raise ValidationError("需求 count 必须为非负整数")

        def work(tx: sqlite3.Connection) -> dict:
            if tx.execute("SELECT 1 FROM schools WHERE id=?", (school_id,)).fetchone() is None:
                raise NotFound(f"院校不存在: {school_id}")
            now = self._clock.now().isoformat()
            upserted = 0
            for d in demands:
                row = tx.execute(
                    "SELECT id, count FROM demands WHERE school_id=? AND quarter=?"
                    " AND region=? AND skill=?",
                    (school_id, quarter, d["region"], d["skill"])).fetchone()
                if row is None:
                    tx.execute(
                        "INSERT INTO demands (id, school_id, quarter, region, skill, count,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self._ids.new_id("dem"), school_id, quarter, d["region"], d["skill"],
                         d["count"], now, now))
                elif row["count"] != d["count"]:
                    tx.execute("UPDATE demands SET count=?, updated_at=? WHERE id=?",
                               (d["count"], now, row["id"]))
                else:
                    continue
                upserted += 1
            self._ledger.append(tx, "demands.imported", school_id, quarter,
                                {"upserted": upserted, "total": len(demands)})
            return {"school_id": school_id, "quarter": quarter, "upserted": upserted}

        result, replayed = self._idempotent("demands.import", idem_key, payload, work)
        result["replayed"] = replayed
        return result

    def import_students(self, actor: Actor, payload: dict, idem_key: str | None) -> dict:
        school_id = payload.get("school_id")
        students = payload.get("students") or []
        if not school_id:
            raise ValidationError("school_id 必填")
        self._check_school_scope(actor, school_id)
        for s in students:
            if not s.get("id") or not s.get("name") or not s.get("region"):
                raise ValidationError("学生缺少 id/name/region")

        def work(tx: sqlite3.Connection) -> dict:
            if tx.execute("SELECT 1 FROM schools WHERE id=?", (school_id,)).fetchone() is None:
                raise NotFound(f"院校不存在: {school_id}")
            now = self._clock.now().isoformat()
            imported, updated, unchanged = 0, 0, 0
            for s in students:
                skills = sorted(set(s.get("skills") or []))
                skills_json = json.dumps(skills, ensure_ascii=False)
                row = tx.execute("SELECT * FROM students WHERE id=?", (s["id"],)).fetchone()
                if row is None:
                    tx.execute(
                        "INSERT INTO students (id, school_id, name, region, skills, phone,"
                        " id_number, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (s["id"], school_id, s["name"], s["region"], skills_json,
                         s.get("phone"), s.get("id_number"), now, now))
                    imported += 1
                else:
                    if row["school_id"] != school_id:
                        raise Conflict("学生已归属其他院校",
                                       details={"student_id": s["id"]})
                    changed = (row["name"] != s["name"] or row["region"] != s["region"]
                               or row["skills"] != skills_json or row["phone"] != s.get("phone")
                               or row["id_number"] != s.get("id_number"))
                    if changed:
                        tx.execute(
                            "UPDATE students SET name=?, region=?, skills=?, phone=?, id_number=?,"
                            " updated_at=? WHERE id=?",
                            (s["name"], s["region"], skills_json, s.get("phone"),
                             s.get("id_number"), now, s["id"]))
                        updated += 1
                    else:
                        unchanged += 1
                safety = s.get("safety")
                if safety:
                    old = tx.execute(
                        "SELECT completed_at, valid_until FROM safety_trainings"
                        " WHERE student_id=?", (s["id"],)).fetchone()
                    completed_at = safety.get("completed_at")
                    valid_until = safety.get("valid_until")
                    if completed_at:
                        domain.parse_date(completed_at, "completed_at")
                    if valid_until:
                        domain.parse_date(valid_until, "valid_until")
                    if old is None:
                        tx.execute(
                            "INSERT INTO safety_trainings (student_id, completed_at, valid_until,"
                            " updated_at) VALUES (?, ?, ?, ?)",
                            (s["id"], completed_at, valid_until, now))
                        self._ledger.append(tx, "safety.registered", s["id"], None,
                                            {"valid_until": valid_until})
                    elif old["completed_at"] != completed_at or old["valid_until"] != valid_until:
                        tx.execute(
                            "UPDATE safety_trainings SET completed_at=?, valid_until=?, updated_at=?"
                            " WHERE student_id=?",
                            (completed_at, valid_until, now, s["id"]))
                        self._ledger.append(tx, "safety.updated", s["id"], None,
                                            {"valid_until": valid_until})
            self._ledger.append(tx, "students.imported", school_id, None,
                                {"imported": imported, "updated": updated,
                                 "unchanged": unchanged})
            return {"school_id": school_id, "imported": imported,
                    "updated": updated, "unchanged": unchanged}

        result, replayed = self._idempotent("students.import", idem_key, payload, work)
        result["replayed"] = replayed
        return result

    def get_student(self, actor: Actor, student_id: str) -> dict:
        row = self._store.read(
            lambda c: c.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone())
        if row is None:
            raise NotFound(f"学生不存在: {student_id}")
        student = dict(row)
        student["skills"] = json.loads(student["skills"])
        if actor.role == "enterprise":
            raise Forbidden("企业请通过分配接口查看学生")
        if actor.role == "school" and actor.id != student["school_id"]:
            raise Forbidden("无权查看其他院校的学生")
        if actor.role == "student" and actor.id != student_id:
            raise Forbidden("学生只能查看本人信息")
        safety = self._store.read(
            lambda c: c.execute("SELECT completed_at, valid_until FROM safety_trainings"
                                " WHERE student_id=?", (student_id,)).fetchone())
        view = student_view(student, actor)
        view["safety"] = dict(safety) if safety else None
        return view

    # ================================================================== 资格核验
    def verify_qualifications(self, actor: Actor, payload: dict) -> dict:
        """批量核验学生在指定季度的实训资格（安全培训有效期 + 在籍）。"""
        quarter = payload.get("quarter")
        student_ids = payload.get("student_ids") or []
        if not quarter or not student_ids:
            raise ValidationError("quarter 与 student_ids 必填")
        domain.parse_quarter(quarter)
        if actor.role == "student" and student_ids != [actor.id]:
            raise Forbidden("学生只能核验本人资格")

        def load(conn: sqlite3.Connection) -> list[dict]:
            results = []
            for sid in student_ids:
                student = conn.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
                if student is None:
                    results.append({"student_id": sid, "eligible": False,
                                    "reasons": ["student_not_found"]})
                    continue
                if actor.role == "school" and student["school_id"] != actor.id:
                    raise Forbidden("无权核验其他院校的学生")
                safety = conn.execute(
                    "SELECT completed_at, valid_until FROM safety_trainings WHERE student_id=?",
                    (sid,)).fetchone()
                reasons = domain.qualification_reasons(dict(safety) if safety else None, quarter)
                results.append({"student_id": sid, "eligible": not reasons,
                                "reasons": reasons})
            return results

        if actor.role not in ("admin", "school", "student", "enterprise"):
            raise Forbidden("无权核验资格")
        results = self._store.read(load)
        return {"quarter": quarter, "results": results}

    # ================================================================== 匹配
    def run_matching(self, actor: Actor, quarter: str) -> dict:
        """按不可拆分岗位与地区公平约束生成分配建议与缺口报告。"""
        require_admin(actor)
        domain.parse_quarter(quarter)

        def work(tx: sqlite3.Connection) -> dict:
            now = self._clock.now().isoformat()
            # 撤销上一轮未确认的建议与候补（已确认/已完成受保护）
            tx.execute(
                "UPDATE allocations SET status='CANCELLED', reason='rematch', updated_at=?"
                " WHERE quarter=? AND status IN ('PROPOSED','WAITLISTED')",
                (now, quarter))
            positions = []
            rows = tx.execute(
                "SELECT p.*, c.enterprise_id FROM positions p"
                " JOIN commitments c ON c.id = p.commitment_id"
                " WHERE c.quarter=? AND c.status='ACTIVE' AND p.capacity > 0"
                " ORDER BY p.id", (quarter,)).fetchall()
            for r in rows:
                occupying = tx.execute(
                    "SELECT COUNT(*) AS n FROM allocations WHERE position_id=?"
                    " AND status IN ('CONFIRMED','COMPLETED')", (r["id"],)).fetchone()["n"]
                remaining = r["capacity"] - occupying
                if remaining > 0:
                    positions.append(matching.Position(
                        id=r["id"], enterprise_id=r["enterprise_id"], region=r["region"],
                        skills=tuple(json.loads(r["skills"])), capacity=remaining))
            candidates = []
            student_rows = tx.execute(
                "SELECT s.*, st.completed_at AS s_completed, st.valid_until AS s_valid"
                " FROM students s LEFT JOIN safety_trainings st ON st.student_id = s.id"
            ).fetchall()
            for s in student_rows:
                active = tx.execute(
                    "SELECT COUNT(*) AS n FROM allocations WHERE quarter=? AND student_id=?"
                    " AND status IN ('PROPOSED','CONFIRMED','COMPLETED')",
                    (quarter, s["id"])).fetchone()["n"]
                if active:
                    continue
                safety = ({"completed_at": s["s_completed"], "valid_until": s["s_valid"]}
                          if s["s_completed"] or s["s_valid"] else None)
                if domain.qualification_reasons(safety, quarter):
                    continue
                candidates.append(matching.Candidate(
                    student_id=s["id"], school_id=s["school_id"], region=s["region"],
                    skills=tuple(json.loads(s["skills"]))))
            region_demands: dict[str, int] = {}
            skill_demands: dict[str, int] = {}
            for d in tx.execute("SELECT region, skill, count FROM demands WHERE quarter=?",
                                (quarter,)).fetchall():
                region_demands[d["region"]] = region_demands.get(d["region"], 0) + d["count"]
                skill_demands[d["skill"]] = skill_demands.get(d["skill"], 0) + d["count"]

            result = matching.match(positions, candidates, region_demands, skill_demands)
            for proposal in result.proposals:
                cand = next(c for c in candidates if c.student_id == proposal.student_id)
                tx.execute(
                    "INSERT INTO allocations (id, quarter, position_id, student_id, school_id,"
                    " region, status, rank, reason, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'PROPOSED', NULL, NULL, ?, ?)",
                    (self._ids.new_id("alc"), quarter, proposal.position_id,
                     proposal.student_id, cand.school_id, cand.region, now, now))
            for entry in result.waitlist:
                cand = next(c for c in candidates if c.student_id == entry.student_id)
                tx.execute(
                    "INSERT INTO allocations (id, quarter, position_id, student_id, school_id,"
                    " region, status, rank, reason, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'WAITLISTED', ?, NULL, ?, ?)",
                    (self._ids.new_id("alc"), quarter, entry.position_id, entry.student_id,
                     cand.school_id, cand.region, entry.rank, now, now))
            report = {"quarter": quarter, **result.gaps}
            version = self._ledger.append(tx, "matching.run", quarter, quarter, {
                "proposals": len(result.proposals), "waitlisted": len(result.waitlist)})
            tx.execute(
                "INSERT INTO matching_runs (quarter, report, version, created_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(quarter) DO UPDATE SET report=excluded.report,"
                " version=excluded.version, created_at=excluded.created_at",
                (quarter, json.dumps(report, ensure_ascii=False), version, now))
            return report

        with self._store.write_tx() as tx:
            return work(tx)

    def get_gaps(self, actor: Actor, quarter: str) -> dict:
        """最近一次匹配的缺口报告（院校/企业/平台均可查看，无隐私数据）。"""
        if actor.role not in ("admin", "school", "enterprise"):
            raise Forbidden("无权查看缺口报告")
        row = self._store.read(
            lambda c: c.execute("SELECT report, version, created_at FROM matching_runs"
                                " WHERE quarter=?", (quarter,)).fetchone())
        if row is None:
            raise NotFound(f"该季度尚未执行匹配: {quarter}")
        report = json.loads(row["report"])
        report["ledger_version"] = row["version"]
        report["generated_at"] = row["created_at"]
        return report

    # ================================================================== 分配生命周期
    def list_allocations(self, actor: Actor, query: dict) -> dict:
        quarter, status = query.get("quarter"), query.get("status")

        def load(conn: sqlite3.Connection) -> list[dict]:
            sql = ("SELECT a.*, p.title AS position_title, p.commitment_id,"
                   " c.enterprise_id, s.name AS student_name, s.skills AS student_skills,"
                   " s.phone AS student_phone, s.id_number AS student_id_number"
                   " FROM allocations a"
                   " JOIN positions p ON p.id = a.position_id"
                   " JOIN commitments c ON c.id = p.commitment_id"
                   " JOIN students s ON s.id = a.student_id WHERE 1=1")
            args: list[Any] = []
            if quarter:
                sql += " AND a.quarter=?"
                args.append(quarter)
            if status:
                sql += " AND a.status=?"
                args.append(status)
            if actor.role == "enterprise":
                sql += " AND c.enterprise_id=?"
                args.append(actor.id)
            elif actor.role == "school":
                sql += " AND a.school_id=?"
                args.append(actor.id)
            elif actor.role == "student":
                sql += " AND a.student_id=?"
                args.append(actor.id)
            sql += " ORDER BY a.created_at, a.id LIMIT 500"
            rows = conn.execute(sql, args).fetchall()
            items = []
            for r in rows:
                student = {
                    "id": r["student_id"], "school_id": r["school_id"],
                    "name": r["student_name"],
                    "skills": json.loads(r["student_skills"]),
                    "phone": r["student_phone"], "id_number": r["student_id_number"],
                }
                items.append({
                    "id": r["id"], "quarter": r["quarter"], "status": r["status"],
                    "position_id": r["position_id"], "position_title": r["position_title"],
                    "enterprise_id": r["enterprise_id"], "rank": r["rank"],
                    "reason": r["reason"], "updated_at": r["updated_at"],
                    "student": student_view(student, actor),
                })
            return items

        return {"allocations": self._store.read(load)}

    def confirm_allocation(self, actor: Actor, allocation_id: str) -> dict:
        """确认分配建议：容量与资格在事务内复核，并发安全。"""
        with self._store.write_tx() as tx:
            alloc = self._must_get_allocation(tx, allocation_id)
            self._check_school_scope(actor, alloc["school_id"])
            if alloc["status"] == domain.Alloc.CONFIRMED:
                return self._allocation_view(tx, alloc)  # 幂等：重复确认返回现状
            if alloc["status"] != domain.Alloc.PROPOSED:
                raise InvalidState(f"当前状态不可确认: {alloc['status']}")
            position = tx.execute("SELECT * FROM positions WHERE id=?",
                                  (alloc["position_id"],)).fetchone()
            occupying = tx.execute(
                "SELECT COUNT(*) AS n FROM allocations WHERE position_id=?"
                " AND status IN ('CONFIRMED','COMPLETED')",
                (alloc["position_id"],)).fetchone()["n"]
            if occupying >= position["capacity"]:
                raise CapacityExceeded("岗位名额已满",
                                       details={"position_id": alloc["position_id"]})
            self._assert_qualified(tx, alloc["student_id"], alloc["quarter"],
                                   json.loads(position["skills"]))
            now = self._clock.now().isoformat()
            cur = tx.execute(
                "UPDATE allocations SET status='CONFIRMED', updated_at=?"
                " WHERE id=? AND status='PROPOSED'", (now, allocation_id))
            if cur.rowcount != 1:
                raise Conflict("确认竞争失败，请重试")
            self._ledger.append(tx, "allocation.confirmed", allocation_id, alloc["quarter"],
                                {"position_id": alloc["position_id"],
                                 "student_id": alloc["student_id"]})
            return self._allocation_view(tx, self._must_get_allocation(tx, allocation_id))

    def cancel_allocation(self, actor: Actor, allocation_id: str, payload: dict) -> dict:
        reason = (payload or {}).get("reason") or "cancelled"
        with self._store.write_tx() as tx:
            alloc = self._must_get_allocation(tx, allocation_id)
            self._check_school_scope(actor, alloc["school_id"])
            if alloc["status"] not in (domain.Alloc.PROPOSED, domain.Alloc.CONFIRMED):
                raise InvalidState(f"当前状态不可取消: {alloc['status']}")
            now = self._clock.now().isoformat()
            tx.execute("UPDATE allocations SET status='CANCELLED', reason=?, updated_at=?"
                       " WHERE id=?", (reason, now, allocation_id))
            self._ledger.append(tx, "allocation.cancelled", allocation_id, alloc["quarter"],
                                {"reason": reason, "from_status": alloc["status"]})
            return self._allocation_view(tx, self._must_get_allocation(tx, allocation_id))

    def substitute_allocation(self, actor: Actor, allocation_id: str) -> dict:
        """替补：取消原分配，并从该岗位候补队列提拔最优合格者。"""
        require_admin(actor)
        with self._store.write_tx() as tx:
            alloc = self._must_get_allocation(tx, allocation_id)
            if alloc["status"] in (domain.Alloc.PROPOSED, domain.Alloc.CONFIRMED):
                tx.execute("UPDATE allocations SET status='REPLACED', reason='substituted',"
                           " updated_at=? WHERE id=?",
                           (self._clock.now().isoformat(), allocation_id))
                self._ledger.append(tx, "allocation.replaced", allocation_id,
                                    alloc["quarter"], {"student_id": alloc["student_id"]})
            elif alloc["status"] != domain.Alloc.CANCELLED:
                raise InvalidState(f"当前状态不可替补: {alloc['status']}")
            position = tx.execute("SELECT * FROM positions WHERE id=?",
                                  (alloc["position_id"],)).fetchone()
            required = json.loads(position["skills"])
            waitlisted = tx.execute(
                "SELECT id, student_id FROM allocations WHERE position_id=?"
                " AND status='WAITLISTED' ORDER BY rank, id",
                (alloc["position_id"],)).fetchall()
            promoted = None
            for row in waitlisted:
                if not CompensationRunner._promotable(tx, row["student_id"],
                                                      alloc["quarter"], required):
                    continue
                now = self._clock.now().isoformat()
                tx.execute("UPDATE allocations SET status='PROPOSED', rank=NULL,"
                           " reason='substituted_in', updated_at=? WHERE id=?",
                           (now, row["id"]))
                tx.execute("UPDATE allocations SET status='CANCELLED',"
                           " reason='promoted_elsewhere', updated_at=?"
                           " WHERE quarter=? AND student_id=? AND status='WAITLISTED'",
                           (now, alloc["quarter"], row["student_id"]))
                self._ledger.append(tx, "allocation.promoted", row["id"], alloc["quarter"],
                                    {"replaces": allocation_id})
                promoted = row["id"]
                break
            return {"replaced": allocation_id, "promoted": promoted,
                    "promoted_allocation": self._allocation_view(
                        tx, self._must_get_allocation(tx, promoted)) if promoted else None}

    def complete_allocation(self, actor: Actor, allocation_id: str) -> dict:
        """实训完成登记（企业或平台），进入结算口径。"""
        with self._store.write_tx() as tx:
            alloc = self._must_get_allocation(tx, allocation_id)
            position = tx.execute(
                "SELECT p.commitment_id, c.enterprise_id FROM positions p"
                " JOIN commitments c ON c.id=p.commitment_id WHERE p.id=?",
                (alloc["position_id"],)).fetchone()
            self._check_enterprise_scope(actor, position["enterprise_id"])
            if alloc["status"] != domain.Alloc.CONFIRMED:
                raise InvalidState(f"仅已确认的分配可登记完成: {alloc['status']}")
            now = self._clock.now().isoformat()
            tx.execute("UPDATE allocations SET status='COMPLETED', updated_at=? WHERE id=?",
                       (now, allocation_id))
            self._ledger.append(tx, "allocation.completed", allocation_id, alloc["quarter"],
                                {"student_id": alloc["student_id"]})
            return self._allocation_view(tx, self._must_get_allocation(tx, allocation_id))

    # ================================================================== 补偿
    def get_compensation(self, actor: Actor, job_id: str) -> dict:
        job = self.compensation.get_job(job_id)
        if actor.role == "enterprise":
            commitment = self._store.read(
                lambda c: c.execute("SELECT enterprise_id FROM commitments WHERE id=?",
                                    (job["commitment_id"],)).fetchone())
            if commitment is None or commitment["enterprise_id"] != actor.id:
                raise Forbidden("无权查看其他企业的补偿作业")
        elif actor.role != "admin":
            raise Forbidden("需要企业或平台权限")
        return job

    def recover_compensations(self) -> list[str]:
        """启动恢复：继续执行未完成的补偿作业。"""
        return self.compensation.recover()

    # ================================================================== 结算
    def run_settlement(self, actor: Actor, payload: dict) -> dict:
        """按季度生成结算草稿；已终审的季度不允许重跑。"""
        require_admin(actor)
        quarter = payload.get("quarter")
        if not quarter:
            raise ValidationError("quarter 必填")
        domain.parse_quarter(quarter)

        def work(tx: sqlite3.Connection) -> dict:
            finalized = tx.execute(
                "SELECT COUNT(*) AS n FROM settlements WHERE quarter=? AND status='FINALIZED'",
                (quarter,)).fetchone()["n"]
            if finalized:
                raise Conflict("该季度存在已终审结算，禁止重跑", details={"quarter": quarter})
            now = self._clock.now().isoformat()
            commitments = tx.execute(
                "SELECT id, enterprise_id FROM commitments WHERE quarter=?", (quarter,)).fetchall()
            settlements = []
            for c in commitments:
                lines = []
                positions = tx.execute(
                    "SELECT * FROM positions WHERE commitment_id=? ORDER BY position_key",
                    (c["id"],)).fetchall()
                for p in positions:
                    counts = {r["status"]: r["n"] for r in tx.execute(
                        "SELECT status, COUNT(*) AS n FROM allocations WHERE position_id=?"
                        " GROUP BY status", (p["id"],)).fetchall()}
                    lines.append({
                        "position_id": p["id"], "position_key": p["position_key"],
                        "title": p["title"], "committed_capacity": p["capacity"],
                        "confirmed": counts.get("CONFIRMED", 0),
                        "completed": counts.get("COMPLETED", 0),
                        "cancelled": counts.get("CANCELLED", 0),
                        "waitlisted_unfilled": counts.get("WAITLISTED", 0),
                    })
                committed = sum(l["committed_capacity"] for l in lines)
                completed = sum(l["completed"] for l in lines)
                body = {
                    "positions": lines,
                    "totals": {
                        "committed_capacity": committed,
                        "confirmed": sum(l["confirmed"] for l in lines),
                        "completed": completed,
                        "fulfillment_rate": (round(completed / committed, 4)
                                             if committed else None),
                    },
                }
                existing = tx.execute(
                    "SELECT id FROM settlements WHERE quarter=? AND enterprise_id=?",
                    (quarter, c["enterprise_id"])).fetchone()
                if existing is None:
                    settlement_id = self._ids.new_id("set")
                    tx.execute(
                        "INSERT INTO settlements (id, quarter, enterprise_id, status, lines,"
                        " created_at, updated_at) VALUES (?, ?, ?, 'DRAFT', ?, ?, ?)",
                        (settlement_id, quarter, c["enterprise_id"],
                         json.dumps(body, ensure_ascii=False), now, now))
                else:
                    settlement_id = existing["id"]
                    tx.execute("UPDATE settlements SET lines=?, updated_at=? WHERE id=?",
                               (json.dumps(body, ensure_ascii=False), now, settlement_id))
                self._ledger.append(tx, "settlement.run", settlement_id, quarter,
                                    {"enterprise_id": c["enterprise_id"],
                                     "completed": completed, "committed": committed})
                settlements.append({"id": settlement_id, "enterprise_id": c["enterprise_id"],
                                    "status": "DRAFT", **body})
            return {"quarter": quarter, "settlements": settlements}

        with self._store.write_tx() as tx:
            return work(tx)

    def finalize_settlement(self, actor: Actor, settlement_id: str) -> dict:
        require_admin(actor)
        with self._store.write_tx() as tx:
            row = tx.execute("SELECT * FROM settlements WHERE id=?",
                             (settlement_id,)).fetchone()
            if row is None:
                raise NotFound(f"结算单不存在: {settlement_id}")
            if row["status"] == "FINALIZED":
                return self._settlement_view(row)
            tx.execute("UPDATE settlements SET status='FINALIZED', updated_at=? WHERE id=?",
                       (self._clock.now().isoformat(), settlement_id))
            self._ledger.append(tx, "settlement.finalized", settlement_id, row["quarter"],
                                {"enterprise_id": row["enterprise_id"]})
            return self._settlement_view(tx.execute(
                "SELECT * FROM settlements WHERE id=?", (settlement_id,)).fetchone())

    def get_settlement(self, actor: Actor, settlement_id: str) -> dict:
        row = self._store.read(
            lambda c: c.execute("SELECT * FROM settlements WHERE id=?",
                                (settlement_id,)).fetchone())
        if row is None:
            raise NotFound(f"结算单不存在: {settlement_id}")
        if actor.role == "enterprise" and actor.id != row["enterprise_id"]:
            raise Forbidden("无权查看其他企业的结算单")
        if actor.role not in ("admin", "enterprise"):
            raise Forbidden("需要企业或平台权限")
        return self._settlement_view(row)

    # ================================================================== 账本
    def ledger_version(self, actor: Actor) -> dict:
        require_admin(actor)
        return {"version": self._store.read(Ledger.current_version)}

    def ledger_events(self, actor: Actor, since: int = 0) -> dict:
        require_admin(actor)
        events = self._store.read(lambda c: Ledger.events_since(c, since))
        return {"events": events,
                "version": self._store.read(Ledger.current_version)}

    # ================================================================== 内部
    @staticmethod
    def _check_school_scope(actor: Actor, school_id: str) -> None:
        if actor.role == "school" and actor.id != school_id:
            raise Forbidden("院校只能操作本校数据")
        if actor.role not in ("admin", "school"):
            raise Forbidden("需要院校或平台权限")

    @staticmethod
    def _check_enterprise_scope(actor: Actor, enterprise_id: str) -> None:
        if actor.role == "enterprise" and actor.id != enterprise_id:
            raise Forbidden("企业只能操作本企业数据")
        if actor.role not in ("admin", "enterprise"):
            raise Forbidden("需要企业或平台权限")

    @staticmethod
    def _must_get_allocation(tx: sqlite3.Connection, allocation_id: str) -> sqlite3.Row:
        row = tx.execute("SELECT * FROM allocations WHERE id=?", (allocation_id,)).fetchone()
        if row is None:
            raise NotFound(f"分配不存在: {allocation_id}")
        return row

    def _assert_qualified(self, tx: sqlite3.Connection, student_id: str,
                          quarter: str, required: list[str]) -> None:
        student = tx.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if student is None:
            raise QualificationExpired("学生不存在")
        if not domain.skills_satisfied(required, json.loads(student["skills"])):
            raise QualificationExpired("学生技能不满足岗位要求")
        safety = tx.execute("SELECT * FROM safety_trainings WHERE student_id=?",
                            (student_id,)).fetchone()
        reasons = domain.qualification_reasons(dict(safety) if safety else None, quarter)
        if reasons:
            raise QualificationExpired("学生安全培训资格在该季度无效",
                                       details={"reasons": reasons})

    @staticmethod
    def _allocation_view(tx: sqlite3.Connection, alloc: sqlite3.Row) -> dict:
        return {
            "id": alloc["id"], "quarter": alloc["quarter"],
            "position_id": alloc["position_id"], "student_id": alloc["student_id"],
            "school_id": alloc["school_id"], "region": alloc["region"],
            "status": alloc["status"], "rank": alloc["rank"], "reason": alloc["reason"],
            "updated_at": alloc["updated_at"],
        }

    @staticmethod
    def _settlement_view(row: sqlite3.Row) -> dict:
        body = json.loads(row["lines"])
        return {"id": row["id"], "quarter": row["quarter"],
                "enterprise_id": row["enterprise_id"], "status": row["status"],
                **body, "updated_at": row["updated_at"]}
