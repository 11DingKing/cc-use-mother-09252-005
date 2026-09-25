"""接口层测试：鉴权、隐私权限、跨期资格核验与完整 HTTP 流程。"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from service_09252_005.api.app import create_app
from service_09252_005.api.deps import get_clock
from service_09252_005.clock import FixedClock

Q = "2026-Q3"
FIXED_NOW = dt.datetime(2026, 7, 1, 9, 0)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_url = f"sqlite:///{Path(self.tmp.name) / 'api.db'}"
        self.app = create_app(db_url=db_url, resume_on_startup=False)
        self.clock = FixedClock(FIXED_NOW)
        self.app.dependency_overrides[get_clock] = lambda: self.clock
        self.client = TestClient(self.app)
        self._bootstrap()

    def tearDown(self):
        self.client.close()
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()

    def _bootstrap(self):
        r = self.client.post("/admin/bootstrap-token", json={"raw_token": "admin-1", "scope": "admin"})
        self.assertEqual(r.status_code, 200)
        self.admin = {"Authorization": "Bearer admin-1"}
        # 第二个管理员引导令牌必须被拒绝
        r2 = self.client.post("/admin/bootstrap-token", json={"raw_token": "x", "scope": "admin"})
        self.assertEqual(r2.status_code, 403)
        for raw, scope, subject in [
            ("ent-e1", "enterprise", "E1"),
            ("ent-e2", "enterprise", "E2"),
            ("sch-s1", "school", "S1"),
            ("sch-s2", "school", "S2"),
        ]:
            resp = self.client.post(
                "/admin/tokens",
                json={"raw_token": raw, "scope": scope, "subject_id": subject},
                headers=self.admin,
            )
            self.assertEqual(resp.status_code, 200, resp.text)
        self.ent1 = {"Authorization": "Bearer ent-e1"}
        self.ent2 = {"Authorization": "Bearer ent-e2"}
        self.s1 = {"Authorization": "Bearer sch-s1"}
        self.s2 = {"Authorization": "Bearer sch-s2"}

    def _seed(self):
        r = self.client.post(
            "/commitments",
            json={
                "batch_key": "c1",
                "commitment_id": "C1",
                "enterprise_id": "E1",
                "quarter": Q,
                "positions": [
                    {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": 2},
                    {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
                ],
            },
            headers=self.ent1,
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/demands",
            json={
                "batch_key": "d1",
                "quarter": Q,
                "demands": [
                    {"school_id": "S1", "region": "南京", "seats": 2},
                    {"school_id": "S2", "region": "苏州", "seats": 1},
                ],
            },
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/students/import",
            json={
                "batch_key": "st1",
                "students": [
                    {"id": "u1", "school_id": "S1", "name": "张三", "external_no": "ID-1",
                     "skills": ["焊接"], "safety_valid_until": "2027-12-31"},
                    {"id": "u2", "school_id": "S1", "name": "李四", "external_no": "ID-2",
                     "skills": ["焊接"], "safety_valid_until": "2027-12-31"},
                    {"id": "u3", "school_id": "S2", "name": "王五", "external_no": "ID-3",
                     "skills": ["电工"], "safety_valid_until": "2027-12-31"},
                    {"id": "u4", "school_id": "S1", "name": "赵六", "external_no": "ID-4",
                     "skills": ["焊接"], "safety_valid_until": "2026-08-31"},
                ],
            },
            headers=self.admin,
        )
        self.assertEqual(r.status_code, 200, r.text)

    # ---------- 鉴权 ----------

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/demands", params={"quarter": Q}).status_code, 401)
        self.assertEqual(
            self.client.get("/demands", params={"quarter": Q},
                            headers={"Authorization": "Bearer nope"}).status_code,
            401,
        )

    def test_scope_boundaries(self):
        # 院校不能导入承诺
        r = self.client.post(
            "/commitments",
            json={"batch_key": "x", "commitment_id": "X", "enterprise_id": "E1",
                  "quarter": Q, "positions": [{"code": "P", "location": "南京", "seats": 1}]},
            headers=self.s1,
        )
        self.assertEqual(r.status_code, 403)
        # E2 不能替 E1 维护承诺
        r = self.client.post(
            "/commitments",
            json={"batch_key": "y", "commitment_id": "C1", "enterprise_id": "E1",
                  "quarter": Q, "positions": [{"code": "P1", "location": "南京", "seats": 2}]},
            headers=self.ent2,
        )
        self.assertEqual(r.status_code, 403)
        # S2 不能导入 S1 的学生
        r = self.client.post(
            "/students/import",
            json={"batch_key": "z", "students": [
                {"id": "u9", "school_id": "S1", "skills": [], "safety_valid_until": "2027-01-01"}]},
            headers=self.s2,
        )
        self.assertEqual(r.status_code, 403)
        # 企业不能统一编排匹配
        self._seed()
        self.assertEqual(self.client.post(f"/match/{Q}", headers=self.ent1).status_code, 403)
        # 院校不能结算
        self.assertEqual(self.client.post(f"/quarters/{Q}/settle", headers=self.s1).status_code, 403)

    # ---------- 隐私权限 ----------

    def test_pii_visibility_by_scope(self):
        self._seed()
        # 本校院校：完整 PII
        r = self.client.get("/students/u1", headers=self.s1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "张三")
        self.assertEqual(r.json()["external_no"], "ID-1")
        self.assertEqual(r.json()["_pii"], "full")
        # 企业：脱敏
        r = self.client.get("/students/u1", headers=self.ent1)
        data = r.json()
        self.assertEqual(data["name"], "***")
        self.assertEqual(data["external_no"], "***")
        self.assertEqual(data["_pii"], "masked")
        # 外校：隐藏
        r = self.client.get("/students/u1", headers=self.s2)
        data = r.json()
        self.assertEqual(data["name"], "***")
        self.assertEqual(data["_pii"], "hidden")
        self.assertEqual(data["skills"], [])
        # 管理员完整可见
        self.assertEqual(self.client.get("/students/u1", headers=self.admin).json()["name"], "张三")

    # ---------- 跨期资格 ----------

    def test_eligibility_cross_period(self):
        self._seed()
        r = self.client.get(
            "/students/u4/eligibility",
            params={"quarter": Q, "position_id": "C1:P1"},
            headers=self.s1,
        )
        data = r.json()
        self.assertFalse(data["eligible"])
        self.assertEqual(data["safety_status"], "expires_during_quarter")
        # 时间推进到证书过期后：状态变为已过期
        self.clock.advance(days=70)  # 2026-09-09
        r = self.client.get(
            "/students/u4/eligibility",
            params={"quarter": Q, "position_id": "C1:P1"},
            headers=self.s1,
        )
        self.assertEqual(r.json()["safety_status"], "expired")
        # 合格学生
        r = self.client.get(
            "/students/u1/eligibility",
            params={"quarter": Q, "position_id": "C1:P1"},
            headers=self.s1,
        )
        self.assertTrue(r.json()["eligible"])
        # 缺技能
        r = self.client.get(
            "/students/u3/eligibility",
            params={"quarter": Q, "position_id": "C1:P1"},
            headers=self.admin,
        )
        self.assertFalse(r.json()["eligible"])
        self.assertIn("焊接", r.json()["missing_skills"])

    # ---------- 完整业务流程 ----------

    def test_full_flow_match_confirm_reduce_substitute_settle(self):
        self._seed()
        # 匹配
        r = self.client.post(f"/match/{Q}", headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["fairness_score"], 1.0)
        allocations = {a["school_id"]: a for a in body["allocations"]}
        self.assertEqual(set(allocations), {"S1", "S2"})
        s1_alloc = allocations["S1"]["allocation_id"]
        s2_alloc = allocations["S2"]["allocation_id"]
        # u4 不合格，未出现在任何建议
        matched = {sid for a in body["allocations"] for sid in a["student_ids"]}
        self.assertNotIn("u4", matched)

        # S2 不能确认 S1 的分配
        self.assertEqual(
            self.client.post(f"/allocations/{s1_alloc}/confirm", json={}, headers=self.s2).status_code,
            403,
        )
        # S1 确认、S2 确认
        r = self.client.post(f"/allocations/{s1_alloc}/confirm", json={}, headers=self.s1)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(r.json()["confirmed"]), ["u1", "u2"])
        r = self.client.post(f"/allocations/{s2_alloc}/confirm", json={}, headers=self.s2)
        self.assertEqual(sorted(r.json()["confirmed"]), ["u3"])

        # 企业 E1 缩减 P1 2->1；已确认的 u1/u2 必须被保护
        r = self.client.post(
            "/commitments/reduce",
            json={
                "batch_key": "red1",
                "commitment_id": "C1",
                "enterprise_id": "E1",
                "quarter": Q,
                "reason": "产线调整",
                "positions": [
                    {"code": "P1", "location": "南京", "skills": ["焊接"], "seats": 1},
                    {"code": "P2", "location": "苏州", "skills": ["电工"], "seats": 1},
                ],
            },
            headers=self.ent1,
        )
        self.assertEqual(r.status_code, 200, r.text)
        impact = r.json()["compensation"][0]["impact"]
        # 两个都已确认 -> 不产生 displaced，说明已确认优先保护
        self.assertEqual(impact["displaced"], [])
        view = self.client.get(
            f"/match/{Q}/allocations",
            params={"status_filter": "confirmed"},
            headers=self.s1,
        ).json()["allocations"]
        p1 = next(a for a in view if a["position_id"] == "C1:P1")
        self.assertTrue(p1["protected"])
        self.assertEqual(len(p1["members"]), 2)
        self.assertIn("保护已确认", p1["impact_note"])

        # 缺口报告体现缩减
        gap = self.client.get(f"/match/{Q}/gap", headers=self.admin).json()
        nanjing = next(x for x in gap["regions"] if x["region"] == "南京")
        self.assertEqual(nanjing["committed_seats"], 1)
        self.assertEqual(nanjing["gap"], 1)

        # 企业只看得到涉及本企业岗位的分配
        ent_view = self.client.get(f"/match/{Q}/allocations", headers=self.ent1).json()
        self.assertEqual(len(ent_view["allocations"]), 2)
        # 企业视图中学生 PII 脱敏
        self.assertTrue(
            all(m["student"]["name"] == "***" for a in ent_view["allocations"] for m in a["members"])
        )

        # 结算
        r = self.client.post(f"/quarters/{Q}/settle", headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["settled_seats"], 3)
        # 结算幂等
        r2 = self.client.post(f"/quarters/{Q}/settle", headers=self.admin)
        self.assertTrue(r2.json()["idempotent_replay"])

        # 账本可审计
        ledger = self.client.get("/ledger", params={"quarter": Q, "limit": 50}, headers=self.admin).json()
        types = {e["event_type"] for e in ledger["events"]}
        self.assertIn("commitment.reduced", types)
        self.assertIn("quarter.settle", types)
        self.assertIn("allocation.confirm", types)

    def test_duplicate_commitment_import_does_not_consume_seats(self):
        self._seed()
        payload = {
            "batch_key": "dup-c",
            "commitment_id": "CDUP",
            "enterprise_id": "E1",
            "quarter": Q,
            "positions": [{"code": "P9", "location": "南京", "skills": ["焊接"], "seats": 2}],
        }
        r1 = self.client.post("/commitments", json=payload, headers=self.ent1)
        r2 = self.client.post("/commitments", json=payload, headers=self.ent1)
        self.assertFalse(r1.json()["idempotent_replay"])
        self.assertTrue(r2.json()["idempotent_replay"])
        gap = self.client.get(f"/match/{Q}/gap", headers=self.admin).json()
        nanjing = next(x for x in gap["regions"] if x["region"] == "南京")
        # 南京供给 = P1 的 2 + P9 的 2 = 4，重复导入没有变成 6
        self.assertEqual(nanjing["committed_seats"], 4)


if __name__ == "__main__":
    unittest.main()
