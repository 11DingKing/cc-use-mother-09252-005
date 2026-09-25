"""ORM 模型。

版本账本由三类结构共同组成：

* ``commitment_versions``：企业承诺每次变更（含缩减）形成不可变版本链；
* ``ledger_events``：所有状态变化的追加式事件流（匹配、确认、替补、结算、补偿）；
* 业务表上的 ``active`` / 状态字段：当前可读视图。

并发安全依赖两条部分唯一索引（SQLite/PostgreSQL 均支持）：

* 一个岗位同一季度同时只存在一条有效分配；
* 一个学生同一季度只能以 拟分配/已确认 之一占用一个席位
  （候补在独立的 ``waitlist_entries`` 表中），
  因此并发确认 / 重复导入不可能重复占用名额。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text as TextCol,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> dt.datetime:
    return dt.datetime.now()


class Base(DeclarativeBase):
    pass


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    # admin / enterprise / school
    scope: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enterprise_id: Mapped[str] = mapped_column(String(64), index=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    versions: Mapped[list["CommitmentVersion"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan"
    )
    positions: Mapped[list["PositionRow"]] = relationship(
        back_populates="commitment", cascade="all, delete-orphan"
    )


class CommitmentVersion(Base):
    """承诺的不可变版本记录（创建 / 更新 / 缩减）。"""

    __tablename__ = "commitment_versions"
    __table_args__ = (
        UniqueConstraint("commitment_id", "version_no", name="uq_commit_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    change_type: Mapped[str] = mapped_column(String(16))  # create / update / reduce
    total_seats: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(TextCol, default="")
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    commitment: Mapped[Commitment] = relationship(back_populates="versions")


class PositionRow(Base):
    """企业承诺中的一个不可拆分岗位（席位在岗位内）。"""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("commitment_id", "code", name="uq_position_code"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    commitment_id: Mapped[str] = mapped_column(ForeignKey("commitments.id"))
    enterprise_id: Mapped[str] = mapped_column(String(64), index=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    code: Mapped[str] = mapped_column(String(64))
    location: Mapped[str] = mapped_column(String(64), index=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    seats: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    commitment: Mapped[Commitment] = relationship(back_populates="positions")


class DemandRow(Base):
    __tablename__ = "demands"
    __table_args__ = (
        UniqueConstraint("school_id", "quarter", "region", name="uq_demand"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[str] = mapped_column(String(64), index=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    region: Mapped[str] = mapped_column(String(64))
    seats: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class StudentRow(Base):
    __tablename__ = "students"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    school_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    external_no: Mapped[str] = mapped_column(String(64), default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    # 安全培训有效期（当前视图）；历史变更写入 ledger_events
    safety_valid_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ImportBatch(Base):
    """导入幂等键：同一 batch_key 重复导入直接返回首次结果。"""

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class Allocation(Base):
    """一条岗位 -> 院校 的分配建议（岗位不可拆分的载体）。"""

    __tablename__ = "allocations"
    __table_args__ = (
        # 有效分配（未取消 / 未随岗位撤销）在岗位+季度上唯一
        Index(
            "ux_active_allocation",
            "position_id",
            "quarter",
            unique=True,
            sqlite_where=text("status NOT IN ('cancelled','revoked_protected')"),
            postgresql_where=text("status NOT IN ('cancelled','revoked_protected')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"))
    school_id: Mapped[str] = mapped_column(String(64), index=True)
    seats: Mapped[int] = mapped_column(Integer)
    # proposed / confirmed / settled / cancelled
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    # 缩减等编排动作留下的影响说明
    impact_note: Mapped[str] = mapped_column(TextCol, default="")
    protected: Mapped[bool] = mapped_column(default=False)
    run_no: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    members: Mapped[list["AllocationMember"]] = relationship(
        back_populates="allocation", cascade="all, delete-orphan"
    )


class AllocationMember(Base):
    """学生在某条分配中的席位状态。"""

    __tablename__ = "allocation_members"
    __table_args__ = (
        UniqueConstraint("allocation_id", "student_id", name="uq_member"),
        # 学生在同一季度的"占座"状态全局唯一：并发确认 / 重复匹配都会被数据库拒绝
        Index(
            "ux_student_active_seat",
            "student_id",
            "quarter",
            unique=True,
            sqlite_where=text("state IN ('proposed','confirmed')"),
            postgresql_where=text("state IN ('proposed','confirmed')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[str] = mapped_column(ForeignKey("allocations.id"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"))
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    # proposed / confirmed / settled / displaced / cancelled
    state: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    allocation: Mapped[Allocation] = relationship(back_populates="members")


class WaitlistEntry(Base):
    """候补名单：缩减释放名额后按序替补，不占正式席位。"""

    __tablename__ = "waitlist_entries"
    __table_args__ = (
        # 同一岗位的有效候补（waiting）中学生唯一；历史 promoted/expired 保留审计，
        # 不阻止下一轮匹配重新生成候补。
        Index(
            "ux_waitlist_active",
            "position_id",
            "student_id",
            "quarter",
            unique=True,
            sqlite_where=text("state = 'waiting'"),
            postgresql_where=text("state = 'waiting'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    skills: Mapped[list] = mapped_column(JSON, default=list)
    # waiting / promoted / expired / cancelled
    state: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class LedgerEvent(Base):
    """追加式账本事件，只增不改，是所有状态变化的审计轨迹。"""

    __tablename__ = "ledger_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    quarter: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)


class CompensationTask(Base):
    """缩减承诺后的补偿编排任务；pending 状态在进程重启后继续执行。"""

    __tablename__ = "compensation_tasks"
    __table_args__ = (
        UniqueConstraint("task_type", "idempotency_key", name="uq_comp_task"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending / done / failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(TextCol, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
