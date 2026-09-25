"""可替换端口：时间与标识生成。

业务代码只依赖这里的协议，测试可注入固定时钟与序列号生成器，
以便稳定复现状态变化（见 README 的端口约定）。
"""
from __future__ import annotations

import itertools
import threading
import uuid
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """时间端口。"""

    def now(self) -> datetime: ...


class SystemClock:
    """默认时钟：UTC 当前时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试时钟：时间固定，可推进。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = moment


class IdGenerator(Protocol):
    """标识端口。"""

    def new_id(self, prefix: str) -> str: ...


class UuidIds:
    """默认标识生成器。"""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SequentialIds:
    """测试标识生成器：确定性序列。"""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def new_id(self, prefix: str) -> str:
        with self._lock:
            n = next(self._counter)
        return f"{prefix}_{n:06d}"
