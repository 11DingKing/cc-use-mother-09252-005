"""服务层集成测试：版本账本、缩减补偿、替补、结算、重启恢复与并发占座。"""
import datetime as dt
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from service_09252_005.persistence import database as db
from service_09252_005.persistence.database import configure_engine, new_session
from service_09252_005.persistence.models import (
    Allocation,
    AllocationMember,
    CompensationTask,
    ImportBatch,
    LedgerEvent,
    PositionRow,
    WaitlistEntry,
)
from service_09252_005.services import (
    confirmations,
    importers,
    lifecycle,
    matching,
    reductions,
)

Q = "2026-Q3"
TODAY = dt.date(2026, 7, 1)


def _commitment(s, batch="c1", seats_p1=2, seats_p2=1):
    return importers.import_commitment(
        s,
        batch_key=batch,
        commitment_id="C1",
        enterprise_id="E1",
        quarter=Q,
        positions=[
            {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": seats_p1},
            {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": seats_p2},
        ],
    )


def _seed(s):
    _commitment(s)
    importers.import_demands(
        s,
        batch_key="d1",
        quarter=Q,
        demands=[
            {"school_id": "S1", "region": "南京", "seats": 2},
            {"school_id": "S2", "region": "苏州", "seats": 1},
        ],
    )
    importers.import_students(
        s,
        batch_key="st1",
        students=[
            {"id": "u1", "school_id": "S1", "name": "甲", "skills": ["焊接"], "safety_valid_until": "2027-12-31"},
            {"id": "u2", "school_id": "S1", "name": "乙", "skills": ["焊接"], "safety_valid_until": "2027-12-31"},
            {"id": "u3", "school_id": "S2", "name": "丙", "skills": ["电工"], "safety_valid_until": "2027-12-31"},
            {"id": "u4", "school_id": "S1", "name": "丁", "skills": ["焊接"], "safety_valid_until": "2026-08-31"},
        ],
    )


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        configure_engine(f"sqlite:///{Path(self.tmp.name) / 't.db'}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_import_is_idempotent(self):
        with new_session() as s:
            r1 = _commitment(s, batch="dup")
            s.commit()
        with new_session() as s:
            r2 = _commitment(s, batch="dup")
            s.commit()
        self.assertFalse(r1["idempotent_replay"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(r2["version"], 1)
        with new_session() as s:
            batches = s.scalars(select(ImportBatch)).all()
            self.assertEqual(len(batches), 1)
            # 岗位没有重复创建，席位总数不变
            positions = s.scalars(select(PositionRow)).all()
            self.assertEqual(sorted(p.code for p in positions), ["P1", "P2"])
            self.assertEqual(sum(p.seats for p in positions), 3)

    def test_match_confirm_flow_and_fairness(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            by_school = {a["school_id"]: a for a in res["allocations"]}
            self.assertEqual(set(by_school), {"S1", "S2"})
            self.assertEqual(res["fairness_score"], 1.0)
            # u4 证书季度内过期，不进匹配
            matched = {sid for a in res["allocations"] for sid in a["student_ids"]}
            self.assertNotIn("u4", matched)
            alloc_s1 = by_school["S1"]["allocation_id"]

            # 确认时复核：u4 即使被塞进拟分配也不能确认
            r = confirmations.confirm_allocation(s, alloc_s1, on_date=TODAY)
            s.commit()
            self.assertEqual(set(r["confirmed"]), {"u1", "u2"})

    def test_reduction_protects_confirmed_and_reorchestrates(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            alloc_s1 = next(a["allocation_id"] for a in res["allocations"] if a["school_id"] == "S1")
        with new_session() as s:
            confirmations.confirm_allocation(s, alloc_s1, on_date=TODAY, student_ids=["u1"])
            s.commit()

        # 缩减 P1 2->1：u1 已确认必须保留，u2 拟分配被挤出进候补
        with new_session() as s:
            out = reductions.apply_reduction(
                s,
                batch_key="red1",
                commitment_id="C1",
                positions=[
                    {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": 1},
                    {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
                ],
                reason="产线调整",
                on_date=TODAY,
            )
        impact = out["compensation"][0]["impact"]
        self.assertEqual(impact["displaced"], ["u2"])
        self.assertEqual(out["import"]["change_type"], "reduce")
        with new_session() as s:
            allocation = s.scalar(select(Allocation).where(Allocation.id == alloc_s1))
            states = {m.student_id: m.state for m in allocation.members}
            self.assertEqual(states["u1"], "confirmed")
            self.assertEqual(states["u2"], "displaced")
            self.assertTrue(allocation.protected)
            self.assertIn("保护已确认", allocation.impact_note)
            # u2 进入 P1 候补
            waiting = s.scalars(
                select(WaitlistEntry).where(WaitlistEntry.state == "waiting")
            ).all()
            self.assertTrue(any(w.student_id == "u2" for w in waiting))
            # 版本账本不可变：存在 create 与 reduce 两个版本
            from service_09252_005.persistence.models import CommitmentVersion

            versions = s.scalars(select(CommitmentVersion).order_by(CommitmentVersion.version_no)).all()
            self.assertEqual([v.change_type for v in versions], ["create", "reduce"])
            events = s.scalars(select(LedgerEvent).where(LedgerEvent.event_type == "commitment.reduced")).all()
            self.assertEqual(len(events), 1)

    def test_reduction_below_confirmed_count_still_protects(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            alloc_s1 = next(a["allocation_id"] for a in res["allocations"] if a["school_id"] == "S1")
        with new_session() as s:
            confirmations.confirm_allocation(s, alloc_s1, on_date=TODAY)
            s.commit()
        # 企业想把 P1 砍到 0（移除岗位）：两名已确认学生仍然受保护
        with new_session() as s:
            reductions.apply_reduction(
                s,
                batch_key="red0",
                commitment_id="C1",
                positions=[
                    {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
                ],
                reason="整体撤岗",
                on_date=TODAY,
            )
        with new_session() as s:
            allocation = s.scalar(select(Allocation).where(Allocation.id == alloc_s1))
            self.assertEqual(allocation.status, "revoked_protected")
            confirmed = [m for m in allocation.members if m.state == "confirmed"]
            self.assertEqual(len(confirmed), 2)
            self.assertIn("撤销", allocation.impact_note)

    def test_repeated_reduction_batch_does_not_replay(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            matching.run_match(s, Q, on_date=TODAY)
            s.commit()
        payload = [
            {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": 1},
            {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
        ]
        with new_session() as s:
            r1 = reductions.apply_reduction(s, batch_key="redX", commitment_id="C1", positions=payload, reason="x", on_date=TODAY)
        self.assertEqual(r1["compensation"][0]["status"], "done")
        with new_session() as s:
            r2 = reductions.apply_reduction(s, batch_key="redX", commitment_id="C1", positions=payload, reason="x", on_date=TODAY)
        self.assertEqual(r2["compensation"], "already_scheduled")
        with new_session() as s:
            tasks = s.scalars(select(CompensationTask)).all()
            self.assertEqual(len(tasks), 1)

    def test_substitute_promotes_waitlist_in_rank_order(self):
        with new_session() as s:
            _seed(s)
            # u5：合格但岗位席位已满，将进入 P1 候补
            importers.import_students(
                s,
                batch_key="st2",
                students=[
                    {"id": "u5", "school_id": "S1", "name": "戊", "skills": ["焊接"], "safety_valid_until": "2028-01-01"},
                ],
            )
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            alloc_s1 = next(a["allocation_id"] for a in res["allocations"] if a["school_id"] == "S1")
        with new_session() as s:
            confirmations.confirm_allocation(s, alloc_s1, on_date=TODAY)
            s.commit()
        # 已确认的 u1 退出，候补第一位（u5）提升为拟分配，等待院校再确认
        with new_session() as s:
            r = lifecycle.substitute(s, alloc_s1, "u1", on_date=TODAY)
            s.commit()
            self.assertEqual(r["withdrawn"], "u1")
            self.assertEqual(r["promoted"], {"student_id": "u5", "rank": 1})
            allocation = s.scalar(select(Allocation).where(Allocation.id == alloc_s1))
            states = {m.student_id: m.state for m in allocation.members}
            self.assertEqual(states["u1"], "cancelled")
            self.assertEqual(states["u5"], "proposed")
            self.assertIn("替补", allocation.impact_note)
        # 再次确认 u5 后正式占座
        with new_session() as s:
            r = confirmations.confirm_allocation(s, alloc_s1, on_date=TODAY, student_ids=["u5"])
            s.commit()
            self.assertEqual(r["confirmed"], ["u5"])

    def test_substitute_skips_expired_waitlist(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            alloc_s1 = next(a["allocation_id"] for a in res["allocations"] if a["school_id"] == "S1")
        # u4 证书季度内过期，直接造一条候补：替补时必须被跳过
        with new_session() as s:
            s.add(WaitlistEntry(quarter=Q, position_id="C1:P1", student_id="u4", rank=1, skills=["焊接"], state="waiting"))
            s.commit()
        with new_session() as s:
            r = lifecycle.substitute(s, alloc_s1, "u1", on_date=TODAY)
            s.commit()
            self.assertIsNone(r["promoted"])
            self.assertEqual(r["expired_waitlist"], ["u4"])
            entry = s.scalar(select(WaitlistEntry).where(WaitlistEntry.student_id == "u4"))
            self.assertEqual(entry.state, "expired")

    def test_settle_is_idempotent_and_releases_proposed(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            matching.run_match(s, Q, on_date=TODAY)
            s.commit()
        with new_session() as s:
            r1 = lifecycle.settle_quarter(s, Q, on_date=TODAY)
            s.commit()
            self.assertFalse(r1["idempotent_replay"])
            self.assertEqual(r1["settled_seats"], 0)
            self.assertEqual(r1["released_seats"], 3)
        with new_session() as s:
            r2 = lifecycle.settle_quarter(s, Q, on_date=TODAY)
            s.commit()
            self.assertTrue(r2["idempotent_replay"])

    def test_partial_confirm_then_rematch_locks_and_deducts_demand(self):
        with new_session() as s:
            _seed(s)
            # u6：苏州合格学生，岗位只有 1 席，首轮进入 P2 候补
            importers.import_students(
                s,
                batch_key="st3",
                students=[
                    {"id": "u6", "school_id": "S2", "name": "己", "skills": ["电工"], "safety_valid_until": "2028-01-01"},
                ],
            )
            s.commit()
        with new_session() as s:
            res = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            alloc_s2 = next(a["allocation_id"] for a in res["allocations"] if a["school_id"] == "S2")
        # 只确认 S2（苏州）；S1（南京）尚未确认
        with new_session() as s:
            confirmations.confirm_allocation(s, alloc_s2, on_date=TODAY)
            s.commit()
        # 重新匹配：苏州岗位锁定保留，南京岗位重新生成建议，且不会重复给苏州派岗
        with new_session() as s:
            res2 = matching.run_match(s, Q, on_date=TODAY)
            s.commit()
            self.assertEqual([a["school_id"] for a in res2["allocations"]], ["S1"])
            active = s.scalars(
                select(Allocation).where(
                    Allocation.quarter == Q, Allocation.status != "cancelled"
                )
            ).all()
            by_position = {a.position_id: a for a in active}
            self.assertEqual(by_position["C1:P2"].status, "confirmed")
            self.assertEqual(by_position["C1:P1"].status, "proposed")
            # 锁定岗位的候补不被重新匹配清除，替补名单仍然可用
            waiting = s.scalars(
                select(WaitlistEntry).where(
                    WaitlistEntry.position_id == "C1:P2", WaitlistEntry.state == "waiting"
                )
            ).all()
            self.assertEqual([w.student_id for w in waiting], ["u6"])

        # u3 退出后 u6 可从保留的候补直接替补
        with new_session() as s:
            r = lifecycle.substitute(s, alloc_s2, "u3", on_date=TODAY)
            s.commit()
            self.assertEqual(r["promoted"]["student_id"], "u6")

    def test_pending_compensation_resumes_after_restart(self):
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            matching.run_match(s, Q, on_date=TODAY)
            s.commit()
        # 直接制造一个 pending 任务（模拟"已提交缩减、补偿尚未执行就宕机/重启"）
        with new_session() as s:
            importers.import_commitment(
                s,
                batch_key="red-restart",
                commitment_id="C1",
                enterprise_id="E1",
                quarter=Q,
                positions=[
                    {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": 1},
                    {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
                ],
                reason="重启前缩减",
            )
            s.add(
                CompensationTask(
                    task_type=reductions.TASK_TYPE,
                    idempotency_key="red-restart",
                    payload={
                        "commitment_id": "C1",
                        "quarter": Q,
                        "shrunk": {"P1": 1},
                        "removed": [],
                        "reason": "重启前缩减",
                        "on_date": TODAY.isoformat(),
                    },
                    status="pending",
                )
            )
            s.commit()

        # 模拟重启：新引擎指向同一个数据库文件，启动恢复逻辑自动续跑
        db_url = str(db.engine.url)
        from service_09252_005.api.app import create_app

        app = create_app(db_url=db_url, resume_on_startup=True)
        self.assertIsNotNone(app)
        with new_session() as s:
            task = s.scalar(select(CompensationTask).where(CompensationTask.idempotency_key == "red-restart"))
            self.assertEqual(task.status, "done")
            self.assertEqual(task.attempts, 1)
            allocations = s.scalars(
                select(Allocation).where(Allocation.quarter == Q, Allocation.status != "cancelled")
            ).all()
            p1 = next(a for a in allocations if a.position_id == "C1:P1")
            states = {m.student_id: m.state for m in p1.members}
            # 缩减后只保留 1 个拟分配席位
            occupying = [sid for sid, st in states.items() if st in ("proposed", "confirmed")]
            self.assertEqual(len(occupying), 1)
            self.assertIn("保护已确认", p1.impact_note)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        configure_engine(f"sqlite:///{Path(self.tmp.name) / 'c.db'}")
        with new_session() as s:
            _seed(s)
            s.commit()
        with new_session() as s:
            matching.run_match(s, Q, on_date=TODAY)
            s.commit()
        with new_session() as s:
            allocs = {
                a.position_id: a.id
                for a in s.scalars(select(Allocation).where(Allocation.quarter == Q)).all()
            }
            # u1 是 P1（S1）的拟分配学生；alloc_p2 属于 S2
            self.alloc_p1 = allocs["C1:P1"]
            self.alloc_p2 = allocs["C1:P2"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_seat_claim_only_one_wins(self):
        """并发地把同一学生塞进两个岗位的占座状态：数据库唯一索引只放行一个。"""
        barrier = threading.Barrier(2)
        results = []

        def claim(allocation_id):
            session = db.SessionLocal()
            try:
                barrier.wait()
                session.add(
                    AllocationMember(
                        allocation_id=allocation_id,
                        student_id="u4",
                        quarter=Q,
                        state="proposed",
                    )
                )
                session.commit()
                results.append("ok")
            except IntegrityError:
                session.rollback()
                results.append("conflict")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(claim, self.alloc_p1), pool.submit(claim, self.alloc_p2)]
            for f in futs:
                f.result()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        with new_session() as s:
            rows = s.scalars(
                select(AllocationMember).where(AllocationMember.student_id == "u4")
            ).all()
            self.assertEqual(len(rows), 1)

    def test_concurrent_confirm_same_allocation_is_serialized(self):
        """两个线程同时确认同一分配（重复请求）：行锁串行化，结果幂等不重复占座。"""
        barrier = threading.Barrier(2)
        outcomes = []

        def do_confirm():
            session = db.SessionLocal()
            try:
                barrier.wait()
                r = confirmations.confirm_allocation(session, self.alloc_p1, on_date=TODAY)
                session.commit()
                outcomes.append(("ok", sorted(r["confirmed"])))
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                outcomes.append(("error", type(exc).__name__))
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(do_confirm), pool.submit(do_confirm)]
            for f in futs:
                f.result()
        self.assertTrue(all(k == "ok" for k, _ in outcomes), outcomes)
        with new_session() as s:
            p1 = s.scalar(select(Allocation).where(Allocation.id == self.alloc_p1))
            self.assertEqual(p1.status, "confirmed")
            confirmed = [m for m in p1.members if m.state == "confirmed"]
            self.assertEqual(sorted(m.student_id for m in confirmed), ["u1", "u2"])
            # 每个学生只有一条成员记录，没有重复占座
            rows = s.scalars(
                select(AllocationMember).where(
                    AllocationMember.allocation_id == self.alloc_p1
                )
            ).all()
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
