"""应用层统一异常，由接口层映射为 HTTP 状态码。"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str, *, code: str = "business_rule", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class NotFound(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_found", status=404)


class Conflict(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="conflict", status=409)


class PermissionDenied(ServiceError):
    def __init__(self, message: str = "无权访问该资源") -> None:
        super().__init__(message, code="forbidden", status=403)
