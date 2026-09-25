"""接口边界：基于标准库 http.server 的 JSON API。

路由层只负责解析与序列化；鉴权（角色解析）与业务校验在服务层。
所有 /api 路由要求 X-Actor-Role / X-Actor-Id 请求头。
"""
from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .privacy import Actor, actor_from_headers
from .services import AppService

Handler = Callable[[Actor, dict, dict, dict], tuple[int, Any]]


class Router:
    """极简路由：dispatch 与底层 HTTP 解耦，便于进程内测试。"""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler, bool]] = []

    def add(self, method: str, pattern: str, handler: Handler, *, auth: bool = True) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, handler, auth))

    def dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict]:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urlparse(path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            payload: dict = {}
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ValidationError("请求体必须是合法 JSON")
                if not isinstance(payload, dict):
                    raise ValidationError("请求体必须是 JSON 对象")
            for route_method, regex, handler, auth in self._routes:
                m = regex.match(parsed.path)
                if route_method != method.upper() or not m:
                    continue
                actor = actor_from_headers(headers) if auth else Actor("anonymous", "-")
                status, result = handler(actor, m.groupdict(), query, payload)
                return status, result if isinstance(result, dict) else {"data": result}
            return 404, {"error": {"code": "not_found", "message": "路由不存在", "details": {}}}
        except DomainError as exc:
            return exc.status, exc.to_dict()
        except sqlite3.IntegrityError as exc:
            return 409, {"error": {"code": "conflict", "message": str(exc), "details": {}}}
        except Exception as exc:  # noqa: BLE001 - 边界兜底，避免泄露内部细节
            return 500, {"error": {"code": "internal_error", "message": str(exc), "details": {}}}


def build_router(service: AppService) -> Router:
    router = Router()

    router.add("GET", "/health", lambda a, p, q, b: (200, {"status": "ok"}), auth=False)

    router.add("POST", "/api/v1/enterprises",
               lambda a, p, q, b: (201, service.create_enterprise(a, b)))
    router.add("POST", "/api/v1/schools",
               lambda a, p, q, b: (201, service.create_school(a, b)))

    router.add("POST", "/api/v1/commitments/import",
               lambda a, p, q, b: (200, service.import_commitment(
                   a, b, q.get("idempotency_key") or b.get("idempotency_key"))))
    router.add("POST", "/api/v1/commitments/{cid}/reduce",
               lambda a, p, q, b: (200, service.reduce_commitment(a, p["cid"], b)))
    router.add("GET", "/api/v1/commitments/{cid}",
               lambda a, p, q, b: (200, service.get_commitment(a, p["cid"])))

    router.add("POST", "/api/v1/demands/import",
               lambda a, p, q, b: (200, service.import_demands(
                   a, b, q.get("idempotency_key") or b.get("idempotency_key"))))
    router.add("POST", "/api/v1/students/import",
               lambda a, p, q, b: (200, service.import_students(
                   a, b, q.get("idempotency_key") or b.get("idempotency_key"))))
    router.add("GET", "/api/v1/students/{sid}",
               lambda a, p, q, b: (200, service.get_student(a, p["sid"])))

    router.add("POST", "/api/v1/qualifications/verify",
               lambda a, p, q, b: (200, service.verify_qualifications(a, b)))

    router.add("POST", "/api/v1/matching/{quarter}/run",
               lambda a, p, q, b: (200, service.run_matching(a, p["quarter"])))
    router.add("GET", "/api/v1/matching/{quarter}/gaps",
               lambda a, p, q, b: (200, service.get_gaps(a, p["quarter"])))

    router.add("GET", "/api/v1/allocations",
               lambda a, p, q, b: (200, service.list_allocations(a, q)))
    router.add("POST", "/api/v1/allocations/{aid}/confirm",
               lambda a, p, q, b: (200, service.confirm_allocation(a, p["aid"])))
    router.add("POST", "/api/v1/allocations/{aid}/cancel",
               lambda a, p, q, b: (200, service.cancel_allocation(a, p["aid"], b)))
    router.add("POST", "/api/v1/allocations/{aid}/substitute",
               lambda a, p, q, b: (200, service.substitute_allocation(a, p["aid"])))
    router.add("POST", "/api/v1/allocations/{aid}/complete",
               lambda a, p, q, b: (200, service.complete_allocation(a, p["aid"])))

    router.add("GET", "/api/v1/compensations/{jid}",
               lambda a, p, q, b: (200, service.get_compensation(a, p["jid"])))

    router.add("POST", "/api/v1/settlements/run",
               lambda a, p, q, b: (200, service.run_settlement(a, b)))
    router.add("POST", "/api/v1/settlements/{sid}/finalize",
               lambda a, p, q, b: (200, service.finalize_settlement(a, p["sid"])))
    router.add("GET", "/api/v1/settlements/{sid}",
               lambda a, p, q, b: (200, service.get_settlement(a, p["sid"])))

    router.add("GET", "/api/v1/ledger/version",
               lambda a, p, q, b: (200, service.ledger_version(a)))
    router.add("GET", "/api/v1/ledger/events",
               lambda a, p, q, b: (200, service.ledger_events(a, int(q.get("since", 0)))))

    return router


def make_http_handler(router: Router) -> type[BaseHTTPRequestHandler]:
    class JsonHandler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()}
            status, payload = router.dispatch(self.command, self.path, headers, body)
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, *args: Any) -> None:  # 静默访问日志
            pass

    return JsonHandler


def serve(router: Router, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_http_handler(router))
    server.serve_forever()
    return server
