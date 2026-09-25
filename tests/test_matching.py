"""匹配引擎：不可拆分岗位、地区公平与缺口识别。"""
import unittest

from service_09252_005.matching import Candidate, Position, fair_targets, match


def pos(pid, region, capacity, skills=(), enterprise="e1"):
    return Position(id=pid, enterprise_id=enterprise, region=region,
                    skills=tuple(skills), capacity=capacity)


def cand(sid, region, skills=()):
    return Candidate(student_id=sid, school_id="sch", region=region, skills=tuple(skills))


class FairTargetTests(unittest.TestCase):
    def test_equal_split(self):
        self.assertEqual(fair_targets({"A": 10, "B": 10}, 4), {"A": 2, "B": 2})

    def test_demand_cap_and_redistribution(self):
        # B 只要 2，余量全部给 A
        self.assertEqual(fair_targets({"A": 10, "B": 2}, 6), {"A": 4, "B": 2})

    def test_remainder_deterministic(self):
        # 余数按地区名确定性分配
        self.assertEqual(fair_targets({"B": 5, "A": 5}, 3), {"A": 2, "B": 1})

    def test_zero_capacity(self):
        self.assertEqual(fair_targets({"A": 3}, 0), {"A": 0})

    def test_never_exceeds_demand(self):
        self.assertEqual(fair_targets({"A": 1}, 100), {"A": 1})


class MatchTests(unittest.TestCase):
    def test_indivisible_and_capacity_bound(self):
        positions = [pos("p1", "R1", 2, skills=("weld",))]
        candidates = [cand(f"s{i}", "R1", ("weld",)) for i in range(5)]
        result = match(positions, candidates, {"R1": 5})
        # 每名学生至多一条建议，建议总数不超过容量：名额整体授予，不可拆分
        self.assertEqual(len(result.proposals), 2)
        self.assertEqual(len({p.student_id for p in result.proposals}), 2)
        self.assertEqual(len(result.waitlist), 3)

    def test_skill_filter(self):
        positions = [pos("p1", "R1", 2, skills=("weld",))]
        candidates = [cand("s0", "R1", ("weld",)), cand("s1", "R1", ("cnc",))]
        result = match(positions, candidates, {"R1": 2})
        self.assertEqual([p.student_id for p in result.proposals], ["s0"])

    def test_regional_fairness_under_scarcity(self):
        positions = [pos("p1", "R1", 4, skills=("weld",))]
        candidates = ([cand(f"a{i}", "RA", ("weld",)) for i in range(10)]
                      + [cand(f"b{i}", "RB", ("weld",)) for i in range(10)])
        result = match(positions, candidates, {"RA": 10, "RB": 10})
        per_region = {}
        for p in result.proposals:
            region = p.student_id[0]
            per_region[region] = per_region.get(region, 0) + 1
        # 容量 4、两地需求各 10：最大最小公平 -> 2/2
        self.assertEqual(per_region, {"a": 2, "b": 2})

    def test_unused_fair_share_redistributed(self):
        positions = [pos("p1", "R1", 4, skills=("weld",))]
        candidates = [cand(f"a{i}", "RA", ("weld",)) for i in range(10)]
        result = match(positions, candidates, {"RA": 10, "RB": 10})
        # RB 无候选人，其公平份额释放给 RA
        self.assertEqual(len(result.proposals), 4)

    def test_waitlist_prefers_deficit_region(self):
        positions = [pos("p1", "R1", 1, skills=("weld",))]
        candidates = [cand("a0", "RA", ("weld",)), cand("b0", "RB", ("weld",)),
                      cand("b1", "RB", ("weld",))]
        result = match(positions, candidates, {"RA": 2, "RB": 2})
        self.assertEqual(len(result.proposals), 1)
        # RB 地区缺口更大，候补前列应是 RB 学生
        self.assertTrue(result.waitlist[0].student_id.startswith("b"))

    def test_gap_report(self):
        positions = [pos("p1", "R1", 2, skills=("weld",))]
        candidates = [cand(f"s{i}", "R1", ("weld",)) for i in range(2)]
        result = match(positions, candidates, {"R1": 5}, {"weld": 5})
        regions = {r["region"]: r for r in result.gaps["regions"]}
        self.assertEqual(regions["R1"]["demand"], 5)
        self.assertEqual(regions["R1"]["proposed"], 2)
        self.assertEqual(regions["R1"]["gap"], 3)
        skills = {s["skill"]: s for s in result.gaps["skills"]}
        self.assertEqual(skills["weld"]["gap"], 3)


if __name__ == "__main__":
    unittest.main()
