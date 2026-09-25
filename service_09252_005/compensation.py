"""企业缩减承诺的补偿执行器：可断点续跑。

缩减承诺时创建补偿作业，按 protect → replan → finalize 三步执行，
每步独立事务并落盘检查点；进程重启后由 recover() 拾起未完成作业
继续执行，步骤幂等，不会重复占用或重复记录影响。

步骤语义：
1. protect  已确认学生全部保留（即使超出新容量，记入影响说明）；
2. replan   剩余名额内保留原有建议，多余建议取消，空额从候补按序递补；
3. finalize 汇总影响说明并关闭作业。
"""
from __future__ import annotations

import json
import sqlite3

from . import domain
from .ledger import Ledger
from .ports import Clock, IdGenerator
from .store import Store

STEPS = ("protect", "replan", "finalize")


class CompensationRunner:
    def __init__(self, store: Store, ledger: Ledger, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._ledger = ledger
        self._clock = clock
        self._ids = ids

    # ------------------------------------------------------------------ 作业创建
    def create_job(
        self,
        tx: sqlite3.Connection,
        *,
        commitment_id: str,
        position_id: str,
        quarter: str,
        old_capacity: int,
        new_capacity: int,
    ) -> str:
        """在调用方事务内创建作业，返回作业 ID。"""
        job_id = self._ids.new_id("cmp")
        now = self._clock.now().isoformat()
        payload = {
            "commitment_id": commitment_id,
            "position_id": position_id,
            "quarter": quarter,
            "old_capacity": old_capacity,
            "new_capacity": new_capacity,
        }
        tx.execute(
            "INSERT INTO compensation_jobs"
            " (id, commitment_id, position_id, quarter, status, step, payload, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)",
            (
                job_id,
                commitment_id,
                position_id,
                quarter,
                STEPS[0],
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._ledger.append(
            tx,
            "compensation.created",
            job_id,
            quarter,
            {"position_id": position_id, "old_capacity": old_capacity, "new_capacity": new_capacity},
        )
        return job_id

    # ------------------------------------------------------------------ 执行
    def run(self, job_id: str, max_steps: int | None = None) -> dict:
        """从作业检查点继续执行；max_steps 用于故障演练。"""
        executed = 0
        while True:
            job = self._store.read(
                lambda c: c.execute(
                    "SELECT * FROM compensation_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            )
            if job is None:
                from .errors import NotFound

                raise NotFound(f"补偿作业不存在: {job_id}")
            if job["status"] == "DONE":
                break
            if max_steps is not None and executed >= max_steps:
                break
            step = job["step"]
            payload = json.loads(job["payload"])
            with self._store.write_tx() as tx:
                if job["status"] == "PENDING":
                    tx.execute(
                        "UPDATE compensation_jobs SET status='RUNNING', updated_at=? WHERE id=?",
                        (self._clock.now().isoformat(), job_id),
                    )
                if step == "protect":
                    self._step_protect(tx, job_id, payload)
                    self._checkpoint(tx, job_id, "replan")
                elif step == "replan":
                    self._step_replan(tx, job_id, payload)
                    self._checkpoint(tx, job_id, "finalize")
                elif step == "finalize":
                    self._step_finalize(tx, job_id, payload)
                    self._checkpoint(tx, job_id, "done", status="DONE")
                else:  # pragma: no cover - 防御未知检查点
                    raise RuntimeError(f"未知补偿步骤: {step}")
            executed += 1
        return self.get_job(job_id)

    def recover(self) -> list[str]:
        """重启恢复：继续所有未完成的补偿作业，返回完成的作业 ID。"""
        pending = self._store.read(
            lambda c: c.execute(
                "SELECT id FROM compensation_jobs WHERE status IN ('PENDING','RUNNING') ORDER BY created_at"
            ).fetchall()
        )
        done = []
        for row in pending:
            self.run(row["id"])
            done.append(row["id"])
        return done

    def get_job(self, job_id: str) -> dict:
        def load(conn: sqlite3.Connection) -> dict:
            job = conn.execute(
                "SELECT * FROM compensation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                from .errors import NotFound

                raise NotFound(f"补偿作业不存在: {job_id}")
            impacts = conn.execute(
                "SELECT student_id, from_status, to_status, reason, created_at"
                " FROM compensation_impacts WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
            return {
                "id": job["id"],
                "commitment_id": job["commitment_id"],
                "position_id": job["position_id"],
                "quarter": job["quarter"],
                "status": job["status"],
                "step": job["step"],
                "payload": json.loads(job["payload"]),
                "impacts": [dict(i) for i in impacts],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            }

        return self._store.read(load)

    # ------------------------------------------------------------------ 步骤
    def _checkpoint(
        self, tx: sqlite3.Connection, job_id: str, next_step: str, status: str | None = None
    ) -> None:
        if status is None:
            tx.execute(
                "UPDATE compensation_jobs SET step=?, updated_at=? WHERE id=?",
                (next_step, self._clock.now().isoformat(), job_id),
            )
        else:
            tx.execute(
                "UPDATE compensation_jobs SET step=?, status=?, updated_at=? WHERE id=?",
                (next_step, status, self._clock.now().isoformat(), job_id),
            )

    def _impact(
        self,
        tx: sqlite3.Connection,
        job_id: str,
        student_id: str,
        from_status: str,
        to_status: str,
        reason: str,
    ) -> None:
        # UNIQUE(job_id, student_id, to_status) + OR IGNORE 保证重跑不重复记录
        tx.execute(
            "INSERT OR IGNORE INTO compensation_impacts"
            " (job_id, student_id, from_status, to_status, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, student_id, from_status, to_status, reason, self._clock.now().isoformat()),
        )

    def _step_protect(self, tx: sqlite3.Connection, job_id: str, payload: dict) -> None:
        """已确认学生全部保留；容量下调落账。"""
        position_id = payload["position_id"]
        new_capacity = payload["new_capacity"]
        tx.execute("UPDATE positions SET capacity=?, updated_at=? WHERE id=?", (
            new_capacity,
            self._clock.now().isoformat(),
            position_id,
        ))
        confirmed = tx.execute(
            "SELECT student_id, status FROM allocations"
            " WHERE position_id = ? AND status IN ('CONFIRMED','COMPLETED')"
            " ORDER BY created_at, id",
            (position_id,),
        ).fetchall()
        if len(confirmed) > new_capacity:
            for row in confirmed:
                self._impact(
                    tx, job_id, row["student_id"], row["status"], row["status"], "protected_overflow"
                )
        self._ledger.append(
            tx,
            "commitment.reduced",
            payload["commitment_id"],
            payload["quarter"],
            {
                "position_id": position_id,
                "old_capacity": payload["old_capacity"],
                "new_capacity": new_capacity,
                "confirmed_protected": len(confirmed),
            },
        )

    def _step_replan(self, tx: sqlite3.Connection, job_id: str, payload: dict) -> None:
        """对候补重新编排：保留名额内建议，取消溢出建议，空额递补。"""
        position_id = payload["position_id"]
        quarter = payload["quarter"]
        new_capacity = payload["new_capacity"]
        now = self._clock.now().isoformat()

        occupying = tx.execute(
            "SELECT COUNT(*) AS n FROM allocations"
            " WHERE position_id = ? AND status IN ('CONFIRMED','COMPLETED')",
            (position_id,),
        ).fetchone()["n"]
        remaining = max(0, new_capacity - occupying)

        proposed = tx.execute(
            "SELECT id, student_id FROM allocations"
            " WHERE position_id = ? AND status = 'PROPOSED' ORDER BY created_at, id",
            (position_id,),
        ).fetchall()
        keep, drop = proposed[:remaining], proposed[remaining:]
        for row in drop:
            tx.execute(
                "UPDATE allocations SET status='CANCELLED', reason='capacity_reduced', updated_at=?"
                " WHERE id=? AND status='PROPOSED'",
                (now, row["id"]),
            )
            self._impact(tx, job_id, row["student_id"], "PROPOSED", "CANCELLED", "capacity_reduced")
            self._ledger.append(
                tx, "allocation.cancelled", row["id"], quarter,
                {"job_id": job_id, "reason": "capacity_reduced"},
            )

        leftover = remaining - len(keep)
        if leftover <= 0:
            return
        position = tx.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        required = json.loads(position["skills"])
        waitlisted = tx.execute(
            "SELECT id, student_id FROM allocations"
            " WHERE position_id = ? AND status = 'WAITLISTED' ORDER BY rank, id",
            (position_id,),
        ).fetchall()
        for row in waitlisted:
            if leftover <= 0:
                break
            if not self._promotable(tx, row["student_id"], quarter, required):
                continue
            tx.execute(
                "UPDATE allocations SET status='PROPOSED', rank=NULL, reason='replanned', updated_at=?"
                " WHERE id=? AND status='WAITLISTED'",
                (now, row["id"]),
            )
            # 该学生在其他岗位上的候补登记一并取消
            tx.execute(
                "UPDATE allocations SET status='CANCELLED', reason='promoted_elsewhere', updated_at=?"
                " WHERE quarter=? AND student_id=? AND status='WAITLISTED'",
                (now, quarter, row["student_id"]),
            )
            self._impact(
                tx, job_id, row["student_id"], "WAITLISTED", "PROPOSED", "replanned_from_waitlist"
            )
            self._ledger.append(
                tx, "allocation.promoted", row["id"], quarter,
                {"job_id": job_id, "reason": "replanned_from_waitlist"},
            )
            leftover -= 1

    def _step_finalize(self, tx: sqlite3.Connection, job_id: str, payload: dict) -> None:
        impacts = tx.execute(
            "SELECT COUNT(*) AS n FROM compensation_impacts WHERE job_id=?", (job_id,)
        ).fetchone()["n"]
        self._ledger.append(
            tx,
            "compensation.completed",
            job_id,
            payload["quarter"],
            {"position_id": payload["position_id"], "impacts": impacts},
        )

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _promotable(
        tx: sqlite3.Connection, student_id: str, quarter: str, required: list[str]
    ) -> bool:
        """候补是否仍可递补：资格有效、技能匹配、本季度无其他生效分配。"""
        active = tx.execute(
            "SELECT COUNT(*) AS n FROM allocations"
            " WHERE quarter=? AND student_id=? AND status IN ('PROPOSED','CONFIRMED','COMPLETED')",
            (quarter, student_id),
        ).fetchone()["n"]
        if active:
            return False
        student = tx.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if student is None:
            return False
        if not domain.skills_satisfied(required, json.loads(student["skills"])):
            return False
        safety = tx.execute(
            "SELECT * FROM safety_trainings WHERE student_id=?", (student_id,)
        ).fetchone()
        safety_dict = dict(safety) if safety else None
        return not domain.qualification_reasons(safety_dict, quarter)
