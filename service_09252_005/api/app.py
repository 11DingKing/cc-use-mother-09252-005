"""FastAPI 应用工厂。

启动时：
1. 建表；
2. 按环境变量准备管理员令牌；
3. 执行所有未完成补偿任务——进程重启后"缩减补偿"会自动继续。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import PROJECT_CODE
from ..persistence.database import configure_engine, init_db, new_session
from ..services.errors import ServiceError
from ..services.reductions import run_pending_compensations
from .routers import (
    admin,
    commitments,
    confirmations,
    demands,
    matching,
    settlement,
    students,
)

logger = logging.getLogger(PROJECT_CODE)


def _resume_compensations() -> None:
    """重启恢复：未完成（pending/failed）的补偿任务继续执行。"""
    try:
        with new_session() as session:
            reports = run_pending_compensations(session)
            session.commit()
        if reports:
            logger.info("启动恢复：执行了 %d 个补偿任务", len(reports))
    except Exception:  # pragma: no cover - 启动恢复失败不能阻断进程启动
        logger.exception("启动恢复补偿任务失败，任务仍保留待下次重试")


def create_app(*, db_url: str | None = None, resume_on_startup: bool = True) -> FastAPI:
    configure_engine(db_url)
    init_db()
    admin.ensure_env_admin()
    if resume_on_startup:
        _resume_compensations()

    app = FastAPI(title="产教融合实训岗位承诺", version="1.0.0")

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "message": exc.message},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": PROJECT_CODE}

    for router in (
        admin.router,
        commitments.router,
        demands.router,
        students.router,
        matching.router,
        confirmations.router,
        settlement.router,
    ):
        app.include_router(router)

    return app


app = create_app()
