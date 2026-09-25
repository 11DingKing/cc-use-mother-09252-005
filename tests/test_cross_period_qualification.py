"""跨期资格：安全培训有效期必须覆盖整个实训季度。"""
import unittest

from service_09252_005.errors import QualificationExpired
from service_09252_005.privacy import Actor

import support

ADMIN = support.ADMIN
SCHOOL = Actor("school", "sch1")


class CrossPeriodQualificationTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        support.seed_enterprise(self.app)
        support.seed_school(self.app)
        svc = self.app.service
        # stu0: 有效期到 Q1 末（Q2 失效）；stu1: Q2 开始后才完成培训；
        # stu2: 有效期覆盖 Q1、Q2；stu3: 无安全培训记录
        svc.import_students(ADMIN, {"school_id": "sch1", "students": [
            {"id": "stu0", "name": "甲", "region": "苏州", "skills": ["weld"],
             "safety": {"completed_at": "2025-12-01", "valid_until": "2026-03-31"}},
            {"id": "stu1", "name": "乙", "region": "苏州", "skills": ["weld"],
             "safety": {"completed_at": "2026-04-15", "valid_until": "2026-06-30"}},
            {"id": "stu2", "name": "丙", "region": "苏州", "skills": ["weld"],
             "safety": {"completed_at": "2025-12-10", "valid_until": "2026-06-30"}},
            {"id": "stu3", "name": "丁", "region": "苏州", "skills": ["weld"]},
            {"id": "stu4", "name": "戊", "region": "苏州", "skills": ["weld"],
             "safety": {"completed_at": "2025-11-01", "valid_until": "2026-06-30"}},
        ]}, None)

    def verify(self, quarter):
        result = self.app.service.verify_qualifications(
            ADMIN, {"quarter": quarter,
                    "student_ids": ["stu0", "stu1", "stu2", "stu3"]})
        return {r["student_id"]: r for r in result["results"]}

    def test_q1_eligibility(self):
        results = self.verify("2026Q1")
        self.assertTrue(results["stu0"]["eligible"])
        self.assertTrue(results["stu2"]["eligible"])
        self.assertFalse(results["stu1"]["eligible"])
        self.assertIn("training_completed_after_quarter_start", results["stu1"]["reasons"])
        self.assertFalse(results["stu3"]["eligible"])
        self.assertIn("no_safety_training", results["stu3"]["reasons"])

    def test_q2_eligibility_expiry(self):
        results = self.verify("2026Q2")
        self.assertFalse(results["stu0"]["eligible"])
        self.assertIn("safety_expired_before_quarter_end", results["stu0"]["reasons"])
        self.assertTrue(results["stu2"]["eligible"])

    def test_matching_excludes_expired_for_q2(self):
        support.seed_commitment(self.app, quarter="2026Q2", capacity=3)
        support.seed_demands(self.app, quarter="2026Q2", count=5)
        self.app.service.run_matching(ADMIN, "2026Q2")
        proposed = support.allocations(self.app, status="PROPOSED", quarter="2026Q2")
        # 仅 stu2、stu4 的培训覆盖整个 Q2
        self.assertEqual({a["student"]["id"] for a in proposed}, {"stu2", "stu4"})
        # 资格失效者连候补都不进入
        waitlisted = support.allocations(self.app, status="WAITLISTED", quarter="2026Q2")
        self.assertEqual({a["student"]["id"] for a in waitlisted}, set())

    def test_confirm_rechecks_qualification(self):
        result = support.seed_commitment(self.app, quarter="2026Q2", capacity=2)
        pid = support.position_id(self.app, result["commitment_id"])
        aid = support.insert_allocation(self.app, pid, "stu0", "2026Q2", "PROPOSED")
        with self.assertRaises(QualificationExpired):
            self.app.service.confirm_allocation(SCHOOL, aid)

    def test_substitution_skips_expired_candidate(self):
        result = support.seed_commitment(self.app, quarter="2026Q2", capacity=1)
        pid = support.position_id(self.app, result["commitment_id"])
        # 已确认者退出后，候补第一顺位 stu0 资格已失效，应跳过并递补 stu4
        confirmed = support.insert_allocation(self.app, pid, "stu2", "2026Q2", "CONFIRMED")
        support.insert_allocation(self.app, pid, "stu0", "2026Q2", "WAITLISTED", rank=1)
        support.insert_allocation(self.app, pid, "stu4", "2026Q2", "WAITLISTED", rank=2)
        self.app.service.cancel_allocation(SCHOOL, confirmed, {"reason": "withdraw"})
        outcome = self.app.service.substitute_allocation(ADMIN, confirmed)
        self.assertIsNotNone(outcome["promoted"])
        promoted = outcome["promoted_allocation"]
        self.assertEqual(promoted["student_id"], "stu4")
        self.assertEqual(promoted["status"], "PROPOSED")

    def test_substitution_with_no_valid_candidate(self):
        result = support.seed_commitment(self.app, quarter="2026Q2", capacity=1)
        pid = support.position_id(self.app, result["commitment_id"])
        confirmed = support.insert_allocation(self.app, pid, "stu2", "2026Q2", "CONFIRMED")
        support.insert_allocation(self.app, pid, "stu0", "2026Q2", "WAITLISTED", rank=1)
        support.insert_allocation(self.app, pid, "stu1", "2026Q2", "WAITLISTED", rank=2)
        self.app.service.cancel_allocation(SCHOOL, confirmed, {"reason": "withdraw"})
        # stu0 已失效、stu1 培训未完成 -> 无人可递补
        outcome = self.app.service.substitute_allocation(ADMIN, confirmed)
        self.assertIsNone(outcome["promoted"])


if __name__ == "__main__":
    unittest.main()
