"""领域层：不可拆分岗位、地区公平、资格有效期等纯业务规则。"""
from .model import (
    Demand,
    MatchSuggestion,
    Position,
    Student,
    is_qualified,
    quota_gap,
    region_fairness_score,
)

__all__ = [
    "Demand",
    "MatchSuggestion",
    "Position",
    "Student",
    "is_qualified",
    "quota_gap",
    "region_fairness_score",
]
