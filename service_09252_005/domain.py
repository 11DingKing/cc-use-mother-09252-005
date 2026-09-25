"""领域基础：季度口径、分配状态、资格规则。

季度格式为 ``2026Q1`` …… ``2026Q4``。学生是否具备某季度实训资格，
取决于安全培训是否覆盖完整季度，以及技能是否满足岗位要求。
"""
from __future__ import annotations

import re
from datetime import date

from .errors import ValidationError

QUARTER_RE = re.compile(r"^(\d{4})Q([1-4])$")

# 季度 -> (起始月, 结束月)
_QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}
_QUARTER_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


def parse_quarter(quarter: str) -> tuple[int, int]:
    """解析季度字符串，返回 (年份, 季度序)。"""
    if not isinstance(quarter, str):
        raise ValidationError("季度必须是字符串", details={"quarter": quarter})
    m = QUARTER_RE.match(quarter.strip())
    if not m:
        raise ValidationError(
            "季度格式应为 2026Q1 形式", details={"quarter": quarter}
        )
    return int(m.group(1)), int(m.group(2))


def quarter_start(quarter: str) -> date:
    year, q = parse_quarter(quarter)
    return date(year, _QUARTER_MONTHS[q][0], 1)


def quarter_end(quarter: str) -> date:
    year, q = parse_quarter(quarter)
    end_month = _QUARTER_MONTHS[q][1]
    return date(year, end_month, _QUARTER_END_DAY[end_month])


def parse_date(value: str, field: str) -> date:
    """解析 YYYY-MM-DD 日期。"""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是日期字符串")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(
            f"{field} 应为 YYYY-MM-DD 格式", details={field: value}
        ) from None


class Alloc:
    """分配状态常量。

    PROPOSED   匹配建议（未确认，不占已确认名额）
    CONFIRMED  院校已确认（占用名额，受缩减保护）
    WAITLISTED 候补（不占用名额，可被重新编排）
    CANCELLED  已取消
    REPLACED   已被替补替换
    COMPLETED  实训完成（占用名额，进入结算）
    """

    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    WAITLISTED = "WAITLISTED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"
    COMPLETED = "COMPLETED"


#: 占用岗位名额的状态（确认与完成）
OCCUPYING = (Alloc.CONFIRMED, Alloc.COMPLETED)

#: 每名学生每季度至多一条的“生效中”状态
ACTIVE = (Alloc.PROPOSED, Alloc.CONFIRMED, Alloc.COMPLETED)


def qualification_reasons(safety: dict | None, quarter: str) -> list[str]:
    """安全培训在指定季度的失效原因；空列表表示资格有效。

    规则：培训须在季度开始前完成，且有效期覆盖整个季度。
    """
    if safety is None:
        return ["no_safety_training"]
    reasons: list[str] = []
    start, end = quarter_start(quarter), quarter_end(quarter)
    completed_at = safety.get("completed_at")
    valid_until = safety.get("valid_until")
    if not completed_at:
        reasons.append("no_safety_training")
    elif parse_date(completed_at, "completed_at") > start:
        reasons.append("training_completed_after_quarter_start")
    if not valid_until:
        reasons.append("safety_validity_missing")
    elif parse_date(valid_until, "valid_until") < end:
        reasons.append("safety_expired_before_quarter_end")
    return reasons


def skills_satisfied(required: list[str] | tuple[str, ...], owned: list[str] | tuple[str, ...]) -> bool:
    """岗位要求技能是否被学生技能覆盖。"""
    return set(required).issubset(set(owned))
