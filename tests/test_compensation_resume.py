"""企业缩减承诺：保护已确认、重排候补、保留影响说明；重启后续跑。"""
import unittest

from service_09252_005 import create_app
from service_09252_005.privacy import Actor
from service_09252_005.ports import FixedClock, SequentialIds

import support

ADMIN = support.ADMIN
SCHOOL = Actor("school", "sch1")


def make_full_scene(app, capacity=4, students=6):
    """容量 capacity 的岗位 + students 名合格学生，完成一轮匹配。"""
    support.seed_enterprise(app)
    support.seed_school(app)
    result = support.seed_commitment(app, capacity=capacity)
    pid = support.position_id(app, result["commitment_id"])
    support.seed_students(app, count=students)
    support.seed_demands(app, count=students)
    app.service.run_matching(ADMIN, "2026Q1")
    return result["commitment_id"], pid


class CompensationTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)

    def test_reduction_protects_confirmed_and_records_impacts(self):
        cid, pid = make_full_scene(self.app, capacity=3, students=5)
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        self.assertEqual(len(proposed), 3)
        # 确认 2 名学生
        for alloc in proposed[:2]:
            svc.confirm_allocation(SCHOOL, alloc["id"])
        # 企业缩减到 1：已确认 2 人受保护，溢出建议取消，候补不动
        job = svc.reduce_commitment(ADMIN, cid, {"position_id": pid, "new_capacity": 1})
        self.assertEqual(job["status"], "DONE")
        confirmed = support.allocations(self.app, status="CONFIRMED")
        self.assertEqual(len(confirmed), 2)  # 已确认学生全部保留
        reasons = {(i["student_id"], i["reason"]) for i in job["impacts"]}
        self.assertEqual(sum(1 for i in job["impacts"] if i["reason"] == "protected_overflow"), 2)
        self.assertEqual(sum(1 for i in job["impacts"] if i["reason"] == "capacity_reduced"), 1)
        self.assertTrue(any(i[0] == proposed[2]["student"]["id"]
                            for i in reasons if i[1] == "capacity_reduced"))
        position = svc.get_commitment(ADMIN, cid)["positions"][0]
        self.assertEqual(position["capacity"], 1)

    def test_replan_promotes_waitlist(self):
        cid, pid = make_full_scene(self.app, capacity=3, students=5)
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        svc.confirm_allocation(SCHOOL, proposed[0]["id"])
        # 取消另外两条建议，使空额只能由候补递补
        svc.cancel_allocation(SCHOOL, proposed[1]["id"], {"reason": "school_drop"})
        svc.cancel_allocation(SCHOOL, proposed[2]["id"], {"reason": "school_drop"})
        job = svc.reduce_commitment(ADMIN, cid, {"position_id": pid, "new_capacity": 2})
        promoted = [i for i in job["impacts"] if i["reason"] == "replanned_from_waitlist"]
        self.assertEqual(len(promoted), 1)  # 确认 1 + 递补 1 = 新容量 2
        proposals = support.allocations(self.app, status="PROPOSED")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["student"]["id"], promoted[0]["student_id"])

    def test_restart_resumes_unfinished_compensation(self):
        cid, pid = make_full_scene(self.app, capacity=4, students=6)
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        for alloc in proposed[:3]:
            svc.confirm_allocation(SCHOOL, alloc["id"])
        # 手工创建缩减 4 -> 1 的作业，只执行一步后“宕机”
        with self.app.store.write_tx() as tx:
            job_id = svc.compensation.create_job(
                tx, commitment_id=cid, position_id=pid, quarter="2026Q1",
                old_capacity=4, new_capacity=1)
        svc.compensation.run(job_id, max_steps=1)
        job = svc.compensation.get_job(job_id)
        self.assertEqual(job["status"], "RUNNING")
        self.assertEqual(job["step"], "replan")
        # 模拟重启：新应用实例挂载同一数据库，启动恢复应完成该作业
        app2 = create_app(db_path=self.app.db_path,
                          clock=FixedClock(support.FIXED_NOW), ids=SequentialIds())
        self.addCleanup(app2.close)
        self.assertIn(job_id, app2.recovered_jobs)
        done = app2.service.compensation.get_job(job_id)
        self.assertEqual(done["status"], "DONE")
        # 影响说明完整且不重复：3 条溢出保护 + 1 条建议取消
        self.assertEqual(
            sum(1 for i in done["impacts"] if i["reason"] == "protected_overflow"), 3)
        self.assertEqual(
            sum(1 for i in done["impacts"] if i["reason"] == "capacity_reduced"), 1)
        # 再次触发恢复为无操作，影响说明不翻倍
        app2.service.recover_compensations()
        again = app2.service.compensation.get_job(job_id)
        self.assertEqual(len(again["impacts"]), len(done["impacts"]))
        confirmed = support.allocations(self.app, status="CONFIRMED")
        self.assertEqual(len(confirmed), 3)  # 已确认学生始终受保护

    def test_import_triggered_reduction_creates_compensation(self):
        cid, pid = make_full_scene(self.app, capacity=2, students=4)
        svc = self.app.service
        proposed = support.allocations(self.app, status="PROPOSED")
        svc.confirm_allocation(SCHOOL, proposed[0]["id"])
        # 通过导入下调容量（生产计划变化），自动生成并执行补偿
        result = support.seed_commitment(self.app, capacity=1)
        self.assertEqual(len(result["compensation_jobs"]), 1)
        job = svc.get_compensation(ADMIN, result["compensation_jobs"][0])
        self.assertEqual(job["status"], "DONE")
        self.assertEqual(job["payload"]["old_capacity"], 2)
        self.assertEqual(job["payload"]["new_capacity"], 1)


if __name__ == "__main__":
    unittest.main()
