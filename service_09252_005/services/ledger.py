"""追加式账本辅助。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..persistence.models import LedgerEvent


def record(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict | None = None,
    quarter: str | None = None,
) -> LedgerEvent:
    event = LedgerEvent(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
        quarter=quarter,
    )
    session.add(event)
    return event
