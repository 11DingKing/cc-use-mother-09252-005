"""组合根：装配存储、账本、服务与接口边界，并在启动时恢复未完成补偿。"""
from __future__ import annotations

import os
import tempfile

from .api import Router, build_router, serve
from .ledger import Ledger
from .ports import Clock, IdGenerator, SystemClock, UuidIds
from .services import AppService
from .store import Store

DEFAULT_DB = os.path.join(tempfile.gettempdir(), "service_09252_005", "app.db")


class App:
    """应用门面：进程内 dispatch 与 HTTP 服务共用同一套路由。"""

    def __init__(
        self,
        db_path: str | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.db_path = db_path or os.environ.get("APP_DB_PATH") or DEFAULT_DB
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIds()
        self.store = Store(self.db_path)
        self.ledger = Ledger(self.clock)
        self.service = AppService(self.store, self.ledger, self.clock, self.ids)
        self.router: Router = build_router(self.service)
        # 重启恢复：继续执行未完成的补偿作业
        self.recovered_jobs = self.service.recover_compensations()

    def handle(self, method: str, path: str, headers: dict | None = None,
               body: dict | None = None) -> tuple[int, dict]:
        """进程内调用入口（测试与嵌入场景）。"""
        import json

        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        return self.router.dispatch(method, path, headers, raw)

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        serve(self.router, host, port)

    def close(self) -> None:
        self.store.close()


def create_app(db_path: str | None = None, clock: Clock | None = None,
               ids: IdGenerator | None = None) -> App:
    return App(db_path=db_path, clock=clock, ids=ids)
