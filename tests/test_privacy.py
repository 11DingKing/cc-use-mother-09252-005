"""隐私权限：角色化访问控制与学生敏感字段脱敏。"""
import unittest

import support


def hdr(role, actor_id):
    return {"X-Actor-Role": role, "X-Actor-Id": actor_id}


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        support.seed_enterprise(self.app, "ent1")
        support.seed_enterprise(self.app, "ent2", region="无锡")
        support.seed_school(self.app, "sch1")
        support.seed_school(self.app, "sch2", region="无锡")
        support.seed_commitment(self.app, eid="ent1", capacity=1)
        support.seed_students(self.app, sid="sch1", count=2)
        support.seed_demands(self.app, count=2)
        self.app.service.run_matching(support.ADMIN, "2026Q1")
        self.commitment_id = self.app.store.read(
            lambda c: c.execute("SELECT id FROM commitments").fetchone()["id"])

    def test_unauthenticated_rejected(self):
        status, body = self.app.handle("GET", "/api/v1/allocations?quarter=2026Q1")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_enterprise_sees_masked_student_pii(self):
        status, body = self.app.handle(
            "GET", "/api/v1/allocations?quarter=2026Q1", headers=hdr("enterprise", "ent1"))
        self.assertEqual(status, 200)
        self.assertTrue(body["allocations"])
        student = body["allocations"][0]["student"]
        self.assertEqual(student["phone"], "138****0000")
        self.assertTrue(student["id_number"].startswith("3205"))
        self.assertIn("*", student["id_number"])
        self.assertEqual(student["name"], "学*")
        self.assertTrue(student["pii_masked"])

    def test_own_school_sees_full_pii(self):
        status, body = self.app.handle(
            "GET", "/api/v1/allocations?quarter=2026Q1", headers=hdr("school", "sch1"))
        self.assertEqual(status, 200)
        student = body["allocations"][0]["student"]
        self.assertEqual(student["phone"], "13812340000")
        self.assertNotIn("*", student["id_number"])
        self.assertNotIn("pii_masked", student)

    def test_enterprise_cannot_read_foreign_commitment(self):
        status, _ = self.app.handle(
            "GET", f"/api/v1/commitments/{self.commitment_id}",
            headers=hdr("enterprise", "ent2"))
        self.assertEqual(status, 403)
        status, _ = self.app.handle(
            "GET", f"/api/v1/commitments/{self.commitment_id}",
            headers=hdr("enterprise", "ent1"))
        self.assertEqual(status, 200)

    def test_school_cannot_read_foreign_student(self):
        status, _ = self.app.handle(
            "GET", "/api/v1/students/stu0", headers=hdr("school", "sch2"))
        self.assertEqual(status, 403)
        status, body = self.app.handle(
            "GET", "/api/v1/students/stu0", headers=hdr("school", "sch1"))
        self.assertEqual(status, 200)
        self.assertEqual(body["phone"], "13812340000")

    def test_enterprise_cannot_read_student_directly(self):
        status, _ = self.app.handle(
            "GET", "/api/v1/students/stu0", headers=hdr("enterprise", "ent1"))
        self.assertEqual(status, 403)

    def test_student_self_access_only(self):
        status, body = self.app.handle(
            "GET", "/api/v1/students/stu0", headers=hdr("student", "stu0"))
        self.assertEqual(status, 200)
        self.assertEqual(body["phone"], "13812340000")
        status, _ = self.app.handle(
            "GET", "/api/v1/students/stu1", headers=hdr("student", "stu0"))
        self.assertEqual(status, 403)

    def test_enterprise_scope_on_allocations(self):
        # ent2 在该季度没有岗位，看不到任何分配
        status, body = self.app.handle(
            "GET", "/api/v1/allocations?quarter=2026Q1", headers=hdr("enterprise", "ent2"))
        self.assertEqual(status, 200)
        self.assertEqual(body["allocations"], [])

    def test_ledger_requires_admin(self):
        status, _ = self.app.handle(
            "GET", "/api/v1/ledger/events", headers=hdr("school", "sch1"))
        self.assertEqual(status, 403)
        status, body = self.app.handle(
            "GET", "/api/v1/ledger/events", headers=hdr("admin", "root"))
        self.assertEqual(status, 200)
        self.assertGreater(body["version"], 0)


if __name__ == "__main__":
    unittest.main()
