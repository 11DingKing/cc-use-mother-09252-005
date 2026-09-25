"""领域模型与规则（纯 Python，不依赖数据库与 Web 框架）。

关键规则
--------
* 岗位不可拆分：一个岗位只能整体分配给一所院校，不能拆成多份。
* 地区公平：匹配采用两轮轮转，让不同地区的院校都有机会获得岗位，
  而不是由需求量最大的地区垄断所有岗位。
* 学生资格：技能要求逐项满足，且安全培训证书必须覆盖整个季度，
  即 ``培训有效期至 >= 季度结束日``（跨期资格核验的核心）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")

_QUARTER_END_MONTH_DAY = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}
_QUARTER_START_MONTH_DAY = {
    1: (1, 1),
    2: (4, 1),
    3: (7, 1),
    4: (10, 1),
}


def parse_quarter(quarter: str) -> tuple[int, int]:
    match = QUARTER_RE.match(quarter or "")
    if not match:
        raise ValueError(f"季度格式必须为 YYYY-QN（例如 2026-Q3），收到: {quarter!r}")
    return int(match.group(1)), int(match.group(2))


def quarter_start(quarter: str) -> dt.date:
    year, q = parse_quarter(quarter)
    month, day = _QUARTER_START_MONTH_DAY[q]
    return dt.date(year, month, day)


def quarter_end(quarter: str) -> dt.date:
    year, q = parse_quarter(quarter)
    month, day = _QUARTER_END_MONTH_DAY[q]
    return dt.date(year, month, day)


@dataclass(frozen=True)
class Position:
    """企业承诺的单个不可拆分岗位。"""

    id: str
    commit_id: str
    enterprise_id: str
    location: str
    skills: frozenset[str]
    seats: int
    # 被缩减承诺版本占用时为 False，代表该岗位已经退出本季度可分配池
    active: bool = True


@dataclass(frozen=True)
class Student:
    id: str
    school_id: str
    region: str
    skills: frozenset[str]
    safety_valid_until: dt.date
    status: str = "eligible"  # eligible / confirmed / waitlisted / settled / displaced


@dataclass(frozen=True)
class Demand:
    """院校在某季度、某地区的岗位需求量。"""

    school_id: str
    region: str
    seats: int


@dataclass(frozen=True)
class MatchSuggestion:
    position_id: str
    school_id: str
    region: str
    seats: int
    matched_student_ids: tuple[str, ...] = field(default_factory=tuple)


def is_qualified(
    student: Student,
    position: Position,
    quarter: str,
    *,
    on_date: dt.date | None = None,
) -> bool:
    """学生是否可被分配到该季度的该岗位。

    跨期规则：安全培训必须在季度开始日（或核验当日，取较晚者）仍然有效，
    并且覆盖到季度结束日。证书在季度中途过期即不合格。
    """
    start = quarter_start(quarter)
    if on_date is not None:
        start = max(start, on_date)
    end = quarter_end(quarter)
    if student.safety_valid_until < start:
        return False
    if student.safety_valid_until < end:
        return False
    if not position.skills.issubset(student.skills):
        return False
    if student.status not in ("eligible", "waitlisted", "displaced"):
        return False
    return True


def quota_gap(positions: list[Position], demands: list[Demand]) -> dict:
    """承诺缺口：按地区统计承诺席位与院校需求的差值。"""
    supply: dict[str, int] = {}
    for pos in positions:
        if pos.active:
            supply[pos.location] = supply.get(pos.location, 0) + pos.seats
    need: dict[str, int] = {}
    for dem in demands:
        need[dem.region] = need.get(dem.region, 0) + dem.seats
    regions = sorted(set(supply) | set(need))
    return {
        "regions": [
            {
                "region": r,
                "committed_seats": supply.get(r, 0),
                "required_seats": need.get(r, 0),
                "gap": need.get(r, 0) - supply.get(r, 0),
            }
            for r in regions
        ],
        "total_committed": sum(supply.values()),
        "total_required": sum(need.values()),
    }


def region_fairness_score(assignments: list[MatchSuggestion], demands: list[Demand]) -> float:
    """地区公平得分（0~1，越高越公平）。

    以"各地区需求满足率"的极差衡量：所有地区满足率一致时为 1，
    最大差距越大分数越低。
    """
    got: dict[str, int] = {}
    for item in assignments:
        got[item.region] = got.get(item.region, 0) + item.seats
    need = {d.region: d.seats for d in demands}
    rates = [got.get(r, 0) / n for r, n in need.items() if n > 0]
    if not rates:
        return 1.0
    return round(max(0.0, 1.0 - (max(rates) - min(rates))), 6)
