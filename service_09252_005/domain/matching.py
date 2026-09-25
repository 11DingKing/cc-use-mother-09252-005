"""匹配算法（纯函数）。

约束
----
1. 岗位不可拆分：每个岗位整体给一所院校，且只分配给该地区（岗位地点）
   有需求的院校。
2. 地区公平：循环轮次中，每轮让每个仍有缺口的地区至多取一个岗位，
   地区顺序按当前满足率（已分/需求）升序排列——满足率最低的地区优先；
   地区内部则让剩余需求比例最高的院校优先。这样所有地区先各分到一个
   岗位，再进入下一轮，避免单一地区 / 院校垄断岗位。
3. 学生匹配：岗位分配给院校后，从该院校合格学生（技能 + 跨期安全培训）
   中按稳定顺序补足席位；合格学生不足时岗位仍归属该校，但只占用实际
   能配上学生的席位，剩余席位在缺口报告中体现。
"""
from __future__ import annotations

import datetime as dt

from .model import (
    Demand,
    MatchSuggestion,
    Position,
    Student,
    is_qualified,
)


def _student_rank(student: Student) -> tuple:
    # 稳定排序：资格较"老"（培训到期日更早）的学生优先，保证结果可复现
    return (student.safety_valid_until, student.id)


def build_matches(
    positions: list[Position],
    students: list[Student],
    demands: list[Demand],
    quarter: str,
    *,
    on_date: dt.date | None = None,
) -> list[MatchSuggestion]:
    active = [p for p in positions if p.active]
    demand_by_school = {d.school_id: d for d in demands}
    region_need: dict[str, int] = {}
    for d in demands:
        region_need[d.region] = region_need.get(d.region, 0) + d.seats

    # 学生按院校分组并保持稳定顺序
    students_by_school: dict[str, list[Student]] = {}
    for stu in students:
        students_by_school.setdefault(stu.school_id, []).append(stu)
    for pool in students_by_school.values():
        pool.sort(key=_student_rank)

    # 已用学生（一个学生同一季度只能占一个席位）
    used: set[str] = set()
    remaining: dict[str, int] = {d.school_id: d.seats for d in demands}
    region_got: dict[str, int] = {r: 0 for r in region_need}

    suggestions: list[MatchSuggestion] = []
    open_positions = sorted(active, key=lambda p: (p.location, p.id))

    def region_rate(region: str) -> float:
        total = region_need.get(region, 0)
        return region_got.get(region, 0) / total if total else float("inf")

    def school_rate(school_id: str) -> float:
        d = demand_by_school.get(school_id)
        if d is None or d.seats <= 0:
            return float("inf")
        return remaining[school_id] / d.seats

    def fill(position: Position, school_id: str) -> MatchSuggestion | None:
        matched: list[str] = []
        for stu in students_by_school.get(school_id, []):
            if len(matched) >= position.seats:
                break
            if stu.id in used:
                continue
            if is_qualified(stu, position, quarter, on_date=on_date):
                matched.append(stu.id)
        if not matched:
            return None
        for sid in matched:
            used.add(sid)
        return MatchSuggestion(
            position_id=position.id,
            school_id=school_id,
            region=position.location,
            seats=len(matched),
            matched_student_ids=tuple(matched),
        )

    def assign(position: Position) -> MatchSuggestion | None:
        """岗位不可拆分：在同地区有缺口的院校中轮转，找第一个能配上学生的。"""
        candidates = [
            s
            for s, d in demand_by_school.items()
            if d.region == position.location and remaining.get(s, 0) > 0
        ]
        candidates.sort(key=lambda s: (-school_rate(s), s))
        for school_id in candidates:
            suggestion = fill(position, school_id)
            if suggestion is not None:
                return suggestion
        return None

    while open_positions:
        # 阶段一：每个仍有需求的地区一轮只取一个岗位
        regions_in_need = sorted(
            {p.location for p in open_positions if region_need.get(p.location, 0) > 0},
            key=lambda r: (region_rate(r), r),
        )
        if not regions_in_need:
            break

        progressed = False
        for region in regions_in_need:
            region_positions = [p for p in open_positions if p.location == region]
            # 该地区选一个岗位：优先席位小的岗位（小包先分，减少浪费）
            region_positions.sort(key=lambda p: (p.seats, p.id))
            position = region_positions[0]
            suggestion = assign(position)
            # 无论是否配上学生，该岗位本轮不再处理（下次 run_match 会再评估）
            open_positions.remove(position)
            progressed = True
            if suggestion is None:
                continue
            suggestions.append(suggestion)
            take = min(remaining[suggestion.school_id], suggestion.seats)
            remaining[suggestion.school_id] -= take
            region_got[region] += take

        if not progressed:
            break

    return suggestions
