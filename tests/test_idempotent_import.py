"""幂等导入：重复导入不得重复占用名额。"""
import unittest

from service_09252_005.errors import IdempotencyConflict
from service_09252_005.ledger import Ledger

import support

ADMIN = support.ADMIN


def commitment_payload(capacity=2):
    return {
        "enterprise_id": "ent1", "quarter": "2026Q1",
        "positions": [{"position_key": "p1", "title": "焊工", "region": "苏州",
                       "skills": ["weld"], "capacity": capacity}],
    }


class IdempotentImportTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        support.seed_enterprise(self.app)
        support.seed_school(self.app)

    def ledger_version(self):
        return self.app.store.read(Ledger.current_version)

    def test_same_key_replay_does_not_double_book(self):
        svc = self.app.service
        first = svc.import_commitment(ADMIN, commitment_payload(capacity=2), "key-1")
        version_after_first = self.ledger_version()
        second = svc.import_commitment(ADMIN, commitment_payload(capacity=2), "key-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["commitment_id"], second["commitment_id"])
        # 重放不产生新的账本事件，也不重复占用名额
        self.assertEqual(self.ledger_version(), version_after_first)
        commitment = svc.get_commitment(ADMIN, first["commitment_id"])
        self.assertEqual(len(commitment["positions"]), 1)
        self.assertEqual(commitment["positions"][0]["capacity"], 2)

    def test_same_key_different_payload_conflicts(self):
        svc = self.app.service
        svc.import_commitment(ADMIN, commitment_payload(capacity=2), "key-2")
        with self.assertRaises(IdempotencyConflict):
            svc.import_commitment(ADMIN, commitment_payload(capacity=5), "key-2")

    def test_natural_upsert_without_key(self):
        svc = self.app.service
        first = svc.import_commitment(ADMIN, commitment_payload(capacity=2), None)
        second = svc.import_commitment(ADMIN, commitment_payload(capacity=3), None)
        self.assertEqual(first["commitment_id"], second["commitment_id"])
        commitment = svc.get_commitment(ADMIN, first["commitment_id"])
        # 自然键覆盖：仍是同一岗位，容量被更新而非累加
        self.assertEqual(len(commitment["positions"]), 1)
        self.assertEqual(commitment["positions"][0]["capacity"], 3)

    def test_students_import_replay(self):
        svc = self.app.service
        payload = {"school_id": "sch1", "students": [
            {"id": "stu0", "name": "甲", "region": "苏州", "skills": ["weld"],
             "safety": {"completed_at": "2025-12-01", "valid_until": "2026-03-31"}}]}
        first = svc.import_students(ADMIN, payload, "stu-key")
        self.assertEqual(first["imported"], 1)
        replay = svc.import_students(ADMIN, payload, "stu-key")
        self.assertTrue(replay["replayed"])
        again = svc.import_students(ADMIN, payload, None)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(again["imported"], 0)
        rows = self.app.store.read(
            lambda c: c.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"])
        self.assertEqual(rows, 1)
        safety = self.app.store.read(
            lambda c: c.execute("SELECT COUNT(*) AS n FROM safety_trainings").fetchone()["n"])
        self.assertEqual(safety, 1)

    def test_demands_import_replay(self):
        svc = self.app.service
        payload = {"school_id": "sch1", "quarter": "2026Q1",
                   "demands": [{"region": "苏州", "skill": "weld", "count": 3}]}
        first = svc.import_demands(ADMIN, payload, "dem-key")
        self.assertEqual(first["upserted"], 1)
        replay = svc.import_demands(ADMIN, payload, "dem-key")
        self.assertTrue(replay["replayed"])
        rows = self.app.store.read(
            lambda c: c.execute("SELECT COUNT(*) AS n FROM demands").fetchone()["n"])
        self.assertEqual(rows, 1)

    def test_replayed_import_does_not_restart_compensation(self):
        svc = self.app.service
        payload = commitment_payload(capacity=2)
        svc.import_commitment(ADMIN, payload, "key-3")
        smaller = commitment_payload(capacity=1)
        svc.import_commitment(ADMIN, smaller, "key-4")
        jobs = self.app.store.read(
            lambda c: c.execute("SELECT COUNT(*) AS n FROM compensation_jobs").fetchone()["n"])
        self.assertEqual(jobs, 1)
        # 重放缩减导入：不得再次触发补偿
        svc.import_commitment(ADMIN, smaller, "key-4")
        jobs = self.app.store.read(
            lambda c: c.execute("SELECT COUNT(*) AS n FROM compensation_jobs").fetchone()["n"])
        self.assertEqual(jobs, 1)


if __name__ == "__main__":
    unittest.main()
