"""并发确认：容量检查与状态迁移在事务内原子完成。"""
import threading
import unittest

from service_09252_005.errors import DomainError
from service_09252_005.ledger import Ledger
from service_09252_005.privacy import Actor

import support

SCHOOL = Actor("school", "sch1")


class ConcurrentConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.app = support.make_app(self)
        support.seed_enterprise(self.app)
        support.seed_school(self.app)

    def _race(self, capacity, proposals, threads_per_allocation):
        result = support.seed_commitment(self.app, capacity=capacity)
        pid = support.position_id(self.app, result["commitment_id"])
        students = support.seed_students(self.app, count=proposals)
        aids = [support.insert_allocation(self.app, pid, sid, "2026Q1", "PROPOSED")
                for sid in students]
        barrier = threading.Barrier(len(aids) * threads_per_allocation)
        outcomes = []
        lock = threading.Lock()

        def worker(aid):
            barrier.wait(timeout=10)
            try:
                view = self.app.service.confirm_allocation(SCHOOL, aid)
                with lock:
                    outcomes.append(("ok", view["status"]))
            except DomainError as exc:
                with lock:
                    outcomes.append(("error", exc.code))

        threads = [threading.Thread(target=worker, args=(aid,))
                   for aid in aids for _ in range(threads_per_allocation)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        confirmed = support.allocations(self.app, status="CONFIRMED")
        events = self.app.store.read(
            lambda c: Ledger.events_since(c, 0, limit=1000))
        confirm_events = [e for e in events if e["kind"] == "allocation.confirmed"]
        return outcomes, confirmed, confirm_events

    def test_single_slot_race(self):
        outcomes, confirmed, events = self._race(capacity=1, proposals=3,
                                                 threads_per_allocation=4)
        # 容量 1：无论多少线程竞争，只有一条分配被确认、一条账本事件
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(outcomes), 12)
        self.assertTrue(all(o[0] == "ok" or o[1] == "capacity_exceeded"
                            for o in outcomes))

    def test_two_slots_race(self):
        _, confirmed, events = self._race(capacity=2, proposals=6,
                                          threads_per_allocation=2)
        self.assertEqual(len(confirmed), 2)
        self.assertEqual(len(events), 2)

    def test_same_allocation_confirm_is_idempotent(self):
        result = support.seed_commitment(self.app, capacity=1)
        pid = support.position_id(self.app, result["commitment_id"])
        student = support.seed_students(self.app, count=1)[0]
        aid = support.insert_allocation(self.app, pid, student, "2026Q1", "PROPOSED")
        barrier = threading.Barrier(6)
        outcomes, lock = [], threading.Lock()

        def worker():
            barrier.wait(timeout=10)
            try:
                self.app.service.confirm_allocation(SCHOOL, aid)
                with lock:
                    outcomes.append("ok")
            except DomainError as exc:
                with lock:
                    outcomes.append(exc.code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        # 同一分配重复确认幂等：全部成功，账本只记一次
        self.assertEqual(outcomes, ["ok"] * 6)
        events = self.app.store.read(lambda c: Ledger.events_since(c, 0, 1000))
        self.assertEqual(len([e for e in events
                              if e["kind"] == "allocation.confirmed"]), 1)


if __name__ == "__main__":
    unittest.main()
