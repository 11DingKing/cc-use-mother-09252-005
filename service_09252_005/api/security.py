"""令牌鉴权与隐私权限。

三类主体
--------
* ``admin``：平台管理员，可见全部数据，可导入、匹配、结算、执行补偿；
* ``enterprise``：合作企业（subject_id = 企业编号），只能导入/查看本企业承诺，
  学生信息对其脱敏（不可见姓名、证件号）；
* ``school``：院校（subject_id = 院校编号），只能维护本校需求与学生，
  只能确认/替补本校分配，不可见外校学生明细。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select

from ..persistence.models import ApiToken, StudentRow

SCOPE_ADMIN = "admin"
SCOPE_ENTERPRISE = "enterprise"
SCOPE_SCHOOL = "school"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    scope: str
    subject_id: str | None
    label: str = ""

    @property
    def is_admin(self) -> bool:
        return self.scope == SCOPE_ADMIN

    def owns_enterprise(self, enterprise_id: str) -> bool:
        return self.is_admin or (
            self.scope == SCOPE_ENTERPRISE and self.subject_id == enterprise_id
        )

    def owns_school(self, school_id: str) -> bool:
        return self.is_admin or (
            self.scope == SCOPE_SCHOOL and self.subject_id == school_id
        )


def authenticate(session, raw_token: str | None) -> Principal | None:
    if not raw_token:
        return None
    token = session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(raw_token), ApiToken.active.is_(True)
        )
    )
    if token is None:
        return None
    return Principal(scope=token.scope, subject_id=token.subject_id, label=token.label)


def mask_student(row: StudentRow, principal: Principal) -> dict:
    """按主体对学生 PII 做隐私脱敏。"""
    data = {
        "id": row.id,
        "school_id": row.school_id,
        "skills": list(row.skills or []),
        "safety_valid_until": row.safety_valid_until.isoformat()
        if row.safety_valid_until
        else None,
    }
    if principal.is_admin or principal.owns_school(row.school_id):
        data["name"] = row.name
        data["external_no"] = row.external_no
        data["_pii"] = "full"
    elif principal.scope == SCOPE_ENTERPRISE:
        # 企业只需要知道"有没有合格的人"，不需要知道是谁
        data["name"] = "***"
        data["external_no"] = "***"
        data["_pii"] = "masked"
    else:
        # 外校主体：完全不可见
        data["name"] = "***"
        data["external_no"] = "***"
        data["skills"] = []
        data["safety_valid_until"] = None
        data["_pii"] = "hidden"
    return data
