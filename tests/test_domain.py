"""领域规则测试：季度边界、跨期资格、不可拆分岗位、地区公平轮转。"""
import datetime as dt
import unittest

from service_09252_005.domain.model import (
    Demand,
    Position,
    Student,
    is_qualified,
    parse_quarter,
    quarter_end,
    quarter_start,
    quota_gap,
    region_fairness_score,
)
from service_09252_005.domain.matching import build_matches


def stu(sid, school, skills, until, status="eligible"):
    return Student(
        id=sid,
        school_id=school,
        region="",
        skills=frozenset(skills),
        safety_valid_until=dt.date.fromisoformat(until),
        status=status,
    )


def pos(pid, location, skills, seats, commit="C1"):
    return Position(
        id=pid,
        commit_id=commit,
        enterprise_id="E1",
        location=location,
        skills=frozenset(skills),
        seats=seats,
    )


class QuarterTests(unittest.TestCase):
    def test_parse_and_bounds(self):
        self.assertEqual(parse_quarter("2026-Q3"), (2026, 3))
        self.assertEqual(quarter_start("2026-Q3"), dt.date(2026, 7, 1))
        self.assertEqual(quarter_end("2026-Q3"), dt.date(2026, 9, 30))
        with self.assertRaises(ValueError):
            parse_quarter("2026Q3")
        with self.assertRaises(ValueError):
            parse_quarter("2026-Q5")


class QualificationTests(unittest.TestCase):
    P = pos("C1:P1", "南京", ["焊接"], 1)

    def test_covers_whole_quarter(self):
        # 证书有效期恰至季度结束日：合格（边界）
        s = stu("u1", "S1", ["焊接"], "2026-09-30")
        self.assertTrue(is_qualified(s, self.P, "2026-Q3"))

    def test_expires_one_day_before_quarter_end(self):
        # 跨期关键情形：季度中途过期
        s = stu("u2", "S1", ["焊接"], "2026-09-29")
        self.assertFalse(is_qualified(s, self.P, "2026-Q3"))

    def test_already_expired_when_quarter_begins(self):
        s = stu("u3", "S1", ["焊接"], "2026-06-30")
        self.assertFalse(is_qualified(s, self.P, "2026-Q3"))

    def test_expired_on_check_date(self):
        # 证书在季度开始后、核验日之前过期
        s = stu("u4", "S1", ["焊接"], "2026-08-31")
        self.assertFalse(is_qualified(s, self.P, "2026-Q3", on_date=dt.date(2026, 9, 1)))

    def test_skill_missing(self):
        s = stu("u5", "S1", ["电工"], "2026-12-31")
        self.assertFalse(is_qualified(s, self.P, "2026-Q3"))

    def test_cross_quarter_recheck(self):
        # 同一张证书在 Q3 合格、在 Q4 不再覆盖
        s = stu("u6", "S1", ["焊接"], "2026-10-15")
        self.assertTrue(is_qualified(s, self.P, "2026-Q3"))
        self.assertFalse(is_qualified(s, self.P, "2026-Q4"))

    def test_inactive_student(self):
        s = stu("u7", "S1", ["焊接"], "2027-01-01", status="settled")
        self.assertFalse(is_qualified(s, self.P, "2026-Q3"))


class MatchingTests(unittest.TestCase):
    def test_position_is_atomic_one_school_only(self):
        positions = [pos("C1:P1", "南京", ["焊接"], 2)]
        students = [
            stu("u1", "S1", ["焊接"], "2027-01-01"),
            stu("u2", "S2", ["焊接"], "2027-01-01"),
        ]
        demands = [Demand("S1", "南京", 1), Demand("S2", "南京", 1)]
        result = build_matches(positions, students, demands, "2026-Q3")
        self.assertEqual(len(result), 1)
        # 岗位不可拆分：两所院校不能共享一个岗位
        self.assertEqual(len({r.school_id for r in result}), 1)

    def test_region_round_robin_fairness(self):
        positions = [
            pos("C1:P1", "南京", ["焊接"], 1),
            pos("C1:P2", "南京", ["焊接"], 1),
            pos("C1:P3", "苏州", ["电工"], 1),
        ]
        students = [
            stu("u1", "S1", ["焊接"], "2027-01-01"),
            stu("u2", "S1", ["焊接"], "2027-01-01"),
            stu("u3", "S2", ["电工"], "2027-01-01"),
        ]
        demands = [Demand("S1", "南京", 2), Demand("S2", "苏州", 1)]
        result = build_matches(positions, students, demands, "2026-Q3")
        schools = sorted({(r.school_id, r.region) for r in result})
        self.assertEqual(schools, [("S1", "南京"), ("S2", "苏州")])
        self.assertEqual(region_fairness_score(result, demands), 1.0)

    def test_same_region_schools_share_in_rounds(self):
        positions = [
            pos("C1:P1", "南京", ["焊接"], 1),
            pos("C1:P2", "南京", ["焊接"], 1),
        ]
        students = [
            stu("u1", "S1", ["焊接"], "2027-01-01"),
            stu("u2", "S2", ["焊接"], "2027-01-01"),
        ]
        demands = [Demand("S1", "南京", 1), Demand("S2", "南京", 1)]
        result = build_matches(positions, students, demands, "2026-Q3")
        winners = {r.school_id for r in result}
        self.assertEqual(winners, {"S1", "S2"})

    def test_deterministic_and_skips_unqualified(self):
        positions = [pos("C1:P1", "南京", ["焊接"], 2)]
        students = [
            stu("u1", "S1", ["焊接"], "2026-08-01"),  # 季度内过期
            stu("u2", "S1", ["焊接"], "2027-01-01"),
        ]
        demands = [Demand("S1", "南京", 2)]
        r1 = build_matches(positions, students, demands, "2026-Q3")
        r2 = build_matches(positions, students, demands, "2026-Q3")
        self.assertEqual([s for r in r1 for s in r.matched_student_ids], ["u2"])
        self.assertEqual(r1, r2)

    def test_gap_report(self):
        positions = [pos("C1:P1", "南京", ["焊接"], 1)]
        demands = [Demand("S1", "南京", 3), Demand("S2", "苏州", 2)]
        report = quota_gap(positions, demands)
        by_region = {r["region"]: r for r in report["regions"]}
        self.assertEqual(by_region["南京"]["gap"], 2)
        self.assertEqual(by_region["苏州"]["gap"], 2)
        self.assertEqual(report["total_required"], 5)


if __name__ == "__main__":
    unittest.main()
