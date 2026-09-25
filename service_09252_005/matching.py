"""匹配引擎：不可拆分岗位 + 地区公平约束 + 缺口识别。

纯函数模块，不触碰持久化；输入由应用服务装配，输出为
分配建议（PROPOSED）、候补（WAITLISTED）与缺口报告。

约束语义：
- 不可拆分：每个岗位名额整体授予一名学生，学生与名额一一对应，不存在份额拆分；
- 地区公平：各地区可确认名额不超过其申报需求；容量不足时按最大最小公平
  （max-min fairness）在地区间分配；地区用不完的公平份额在第二轮重新分配，
  避免名额浪费。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Position:
    id: str
    enterprise_id: str
    region: str
    skills: tuple[str, ...]
    capacity: int  # 本轮可用名额（已扣除确认占用）


@dataclass(frozen=True)
class Candidate:
    student_id: str
    school_id: str
    region: str
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Proposal:
    position_id: str
    student_id: str


@dataclass(frozen=True)
class WaitEntry:
    position_id: str
    student_id: str
    rank: int


@dataclass
class MatchResult:
    proposals: list[Proposal] = field(default_factory=list)
    waitlist: list[WaitEntry] = field(default_factory=list)
    gaps: dict = field(default_factory=dict)


def fair_targets(demands: dict[str, int], capacity: int) -> dict[str, int]:
    """最大最小公平分配：各地区目标名额。

    每轮把剩余容量均分给仍未满足的地区（余数按地区名确定性分配），
    地区达到需求上限后退出，剩余容量继续分配给未满足地区。
    """
    targets = {region: 0 for region in demands}
    remaining = max(0, capacity)
    active = {r for r, d in demands.items() if d > 0}
    while remaining > 0 and active:
        k = len(active)
        base, extra = divmod(remaining, k)
        granted = 0
        for i, region in enumerate(sorted(active)):
            grant = base + (1 if i < extra else 0)
            room = demands[region] - targets[region]
            take = min(grant, room)
            if take:
                targets[region] += take
                remaining -= take
                granted += take
        active = {r for r in active if targets[r] < demands[r]}
        if granted == 0:
            break
    return targets


def _eligible(candidate: Candidate, position: Position) -> bool:
    return set(position.skills).issubset(set(candidate.skills))


def _pick(
    position: Position,
    pool: list[Candidate],
    placed: set[str],
    allocated: dict[str, int],
    targets: dict[str, int] | None,
) -> Candidate | None:
    """为岗位挑选一名候选人：同城优先，其余按学号确定性排序。"""
    best: Candidate | None = None
    for cand in pool:
        if cand.student_id in placed or not _eligible(cand, position):
            continue
        if targets is not None and allocated.get(cand.region, 0) >= targets.get(cand.region, 0):
            continue
        if best is None:
            best = cand
            continue
        key = (cand.region != position.region, cand.student_id)
        best_key = (best.region != position.region, best.student_id)
        if key < best_key:
            best = cand
    return best


def match(
    positions: list[Position],
    candidates: list[Candidate],
    region_demands: dict[str, int],
    skill_demands: dict[str, int] | None = None,
) -> MatchResult:
    """生成匹配建议、候补与缺口报告。"""
    result = MatchResult()
    remaining = {p.id: p.capacity for p in positions}
    ordered = sorted(positions, key=lambda p: (p.region, p.enterprise_id, p.id))
    placed: set[str] = set()
    allocated: dict[str, int] = {}
    total_capacity = sum(remaining.values())
    targets = fair_targets(region_demands, total_capacity)

    def grant(position: Position, cand: Candidate) -> None:
        placed.add(cand.student_id)
        remaining[position.id] -= 1
        allocated[cand.region] = allocated.get(cand.region, 0) + 1
        result.proposals.append(Proposal(position_id=position.id, student_id=cand.student_id))

    # 第一轮：受地区公平目标约束
    for position in ordered:
        while remaining[position.id] > 0:
            cand = _pick(position, candidates, placed, allocated, targets)
            if cand is None:
                break
            grant(position, cand)

    # 第二轮：地区目标用不尽时释放剩余名额（仍需技能匹配）
    for position in ordered:
        while remaining[position.id] > 0:
            cand = _pick(position, candidates, placed, allocated, None)
            if cand is None:
                break
            grant(position, cand)

    # 候补：未安置且对某岗位合格的候选人，登记到其最优岗位
    deficits = {r: max(0, targets.get(r, 0) - allocated.get(r, 0)) for r in region_demands}
    unplaced = [c for c in candidates if c.student_id not in placed]
    unplaced.sort(key=lambda c: (-deficits.get(c.region, 0), c.student_id))
    for rank, cand in enumerate(unplaced, start=1):
        options = [p for p in ordered if _eligible(cand, p)]
        if not options:
            continue
        options.sort(key=lambda p: (p.region != cand.region, p.region, p.enterprise_id, p.id))
        result.waitlist.append(
            WaitEntry(position_id=options[0].id, student_id=cand.student_id, rank=rank)
        )

    result.gaps = _gap_report(positions, region_demands, skill_demands or {}, allocated, result, candidates)
    return result


def _gap_report(
    positions: list[Position],
    region_demands: dict[str, int],
    skill_demands: dict[str, int],
    allocated: dict[str, int],
    result: MatchResult,
    candidates: list[Candidate],
) -> dict:
    """承诺缺口：按地区与按技能的需求-供给对照，供院校尽早识别。"""
    regions = []
    for region in sorted(set(region_demands) | {p.region for p in positions}):
        demand = region_demands.get(region, 0)
        proposed = allocated.get(region, 0)
        regions.append(
            {
                "region": region,
                "demand": demand,
                "proposed": proposed,
                "gap": max(0, demand - proposed),
            }
        )
    skills: dict[str, dict] = {}
    for position in positions:
        for skill in position.skills:
            entry = skills.setdefault(
                skill, {"skill": skill, "demand": 0, "remaining_capacity": 0, "gap": 0}
            )
            entry["remaining_capacity"] += position.capacity
    for skill, demand in skill_demands.items():
        entry = skills.setdefault(
            skill, {"skill": skill, "demand": 0, "remaining_capacity": 0, "gap": 0}
        )
        entry["demand"] += demand
    for entry in skills.values():
        entry["gap"] = max(0, entry["demand"] - entry["remaining_capacity"])
    return {
        "regions": regions,
        "skills": sorted(skills.values(), key=lambda e: e["skill"]),
        "totals": {
            "positions": len(positions),
            "capacity": sum(p.capacity for p in positions),
            "candidates": len(candidates),
            "proposed": len(result.proposals),
            "waitlisted": len(result.waitlist),
        },
    }
