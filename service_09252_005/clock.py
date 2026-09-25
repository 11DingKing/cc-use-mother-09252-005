"""时间端口。

业务服务只依赖 :class:`Clock` 协议，测试时注入 :class:`FrozenClock`
即可稳定复现任意时间点上的资格与补偿行为。
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    def now(self) -> dt.datetime: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now()


class FixedClock:
    """可手动推进的冻结时钟，供测试与复现使用。"""

    def __init__(self, moment: dt.datetime) -> None:
        self._moment = moment

    def now(self) -> dt.datetime:
        return self._moment

    def advance(self, **delta) -> "FixedClock":
        self._moment += dt.timedelta(**delta)
        return self
