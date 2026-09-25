"""结算与替补：季度结算草稿、终审保护、候补递补流程。"""
import unittest

from service_09252_005.errors import Conflict
from service_09252_005.privacy import Actor

import support

ADMIN = support.ADMIN
SCHOOL = Actor("school", "sch1")
ENTERPRISE = Actor("enterprise", "ent1")


class SettlementAndSubstitutionTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        support.seed_enterprise(self.app)
        support.seed_school(self.app)
        result = support.seed_commitment(self.app, capacity=2)
        self.cid = result["commitment_id"]
        self.pid = support.position_id(self.app, self.cid)
        support.seed_students(self.app, count=3)
        support.seed_demands(self.app, count=3)
        self.app.service.run_matching(ADMIN, "2026Q1")

    def test_full_lifecycle_and_settlement(self):
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        self.assertEqual(len(proposed), 2)
        waitlisted = support.allocations(self.app, status="WAITLISTED")
        self.assertEqual(len(waitlisted), 1)
        for alloc in proposed:
            svc.confirm_allocation(SCHOOL, alloc["id"])
        # 企业登记 1 人完成
        svc.complete_allocation(ENTERPRISE, proposed[0]["id"])
        settlement = svc.run_settlement(ADMIN, {"quarter": "2026Q1"})
        self.assertEqual(len(settlement["settlements"]), 1)
        doc = settlement["settlements"][0]
        self.assertEqual(doc["totals"]["committed_capacity"], 2)
        self.assertEqual(doc["totals"]["confirmed"], 1)
        self.assertEqual(doc["totals"]["completed"], 1)
        self.assertEqual(doc["totals"]["fulfillment_rate"], 0.5)
        line = doc["positions"][0]
        self.assertEqual(line["waitlisted_unfilled"], 1)
        # 草稿可重跑（幂等覆盖），终审后禁止重跑
        again = svc.run_settlement(ADMIN, {"quarter": "2026Q1"})
        self.assertEqual(again["settlements"][0]["id"], doc["id"])
        svc.finalize_settlement(ADMIN, doc["id"])
        with self.assertRaises(Conflict):
            svc.run_settlement(ADMIN, {"quarter": "2026Q1"})
        # 企业可查看本企业结算单
        view = svc.get_settlement(ENTERPRISE, doc["id"])
        self.assertEqual(view["status"], "FINALIZED")

    def test_substitution_after_withdraw(self):
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        for alloc in proposed:
            svc.confirm_allocation(SCHOOL, alloc["id"])
        svc.cancel_allocation(SCHOOL, proposed[0]["id"], {"reason": "student_withdraw"})
        outcome = svc.substitute_allocation(ADMIN, proposed[0]["id"])
        self.assertIsNotNone(outcome["promoted"])
        promoted_id = outcome["promoted_allocation"]["student_id"]
        waitlisted = support.allocations(self.app, status="WAITLISTED")
        self.assertEqual(waitlisted, [])  # 候补已被提拔
        # 递补建议由院校确认后占用名额
        svc.confirm_allocation(SCHOOL, outcome["promoted"])
        confirmed = support.allocations(self.app, status="CONFIRMED")
        self.assertEqual(len(confirmed), 2)
        self.assertIn(promoted_id, {a["student"]["id"] for a in confirmed})


if __name__ == "__main__":
    unittest.main()
