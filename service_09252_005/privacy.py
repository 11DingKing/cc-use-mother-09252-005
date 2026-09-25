"""隐私与权限：角色模型与学生敏感字段脱敏。

角色：admin（平台）、enterprise（企业）、school（院校）、student（学生）。
学生联系方式与证件号仅对平台、所属院校及本人可见；企业仅可见
匹配所需的非敏感信息（脱敏姓名、地区、技能、资格状态）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import Forbidden, Unauthorized

ROLES = ("admin", "enterprise", "school", "student")


@dataclass(frozen=True)
class Actor:
    role: str
    id: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def actor_from_headers(headers: dict[str, str]) -> Actor:
    """从请求头解析操作者；缺失或非法即未认证。"""
    role = (headers.get("x-actor-role") or "").strip().lower()
    actor_id = (headers.get("x-actor-id") or "").strip()
    if not role or not actor_id:
        raise Unauthorized("缺少 X-Actor-Role / X-Actor-Id 请求头")
    if role not in ROLES:
        raise Unauthorized(f"未知角色: {role}")
    return Actor(role=role, id=actor_id)


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise Forbidden("需要平台管理员权限")


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "***"


def mask_id_number(id_number: str | None) -> str | None:
    if not id_number:
        return id_number
    if len(id_number) >= 8:
        return id_number[:4] + "*" * (len(id_number) - 8) + id_number[-4:]
    return "****"


def mask_name(name: str | None) -> str | None:
    if not name:
        return name
    return name[0] + "*"


def can_view_student_pii(actor: Actor, student: dict) -> bool:
    """是否可见学生敏感信息。"""
    if actor.is_admin:
        return True
    if actor.role == "school" and actor.id == student.get("school_id"):
        return True
    if actor.role == "student" and actor.id == student.get("id"):
        return True
    return False


def student_view(student: dict, actor: Actor) -> dict:
    """按角色输出学生视图；无权限者得到脱敏副本。"""
    view = dict(student)
    if can_view_student_pii(actor, student):
        return view
    view["name"] = mask_name(view.get("name"))
    view["phone"] = mask_phone(view.get("phone"))
    view["id_number"] = mask_id_number(view.get("id_number"))
    view["pii_masked"] = True
    return view
