"""接口层请求 / 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PositionIn(BaseModel):
    code: str = Field(min_length=1)
    location: str = Field(min_length=1)
    skills: list[str] = Field(default_factory=list)
    seats: int = Field(gt=0)


class CommitmentIn(BaseModel):
    batch_key: str = Field(min_length=1)
    commitment_id: str = Field(min_length=1)
    enterprise_id: str = Field(min_length=1)
    quarter: str = Field(min_length=4)
    reason: str = ""
    positions: list[PositionIn] = Field(min_length=1)


class DemandItem(BaseModel):
    school_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    seats: int = Field(gt=0)


class DemandIn(BaseModel):
    batch_key: str = Field(min_length=1)
    quarter: str
    demands: list[DemandItem] = Field(min_length=1)


class StudentIn(BaseModel):
    id: str = Field(min_length=1)
    school_id: str = Field(min_length=1)
    name: str = ""
    external_no: str = ""
    skills: list[str] = Field(default_factory=list)
    # ISO 日期；安全培训有效期至（必须覆盖季度结束日才算合格）
    safety_valid_until: str | None = None


class StudentBatchIn(BaseModel):
    batch_key: str = Field(min_length=1)
    students: list[StudentIn] = Field(min_length=1)


class ConfirmIn(BaseModel):
    student_ids: list[str] | None = None


class TokenIn(BaseModel):
    raw_token: str = Field(min_length=1)
    scope: str
    subject_id: str | None = None
    label: str = ""
