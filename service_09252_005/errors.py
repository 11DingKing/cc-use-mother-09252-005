"""统一领域错误：应用服务抛出，接口边界映射为 HTTP 状态。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Unauthorized(DomainError):
    status = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


class InvalidState(Conflict):
    code = "invalid_state"


class CapacityExceeded(Conflict):
    code = "capacity_exceeded"


class IdempotencyConflict(Conflict):
    code = "idempotency_conflict"


class QualificationExpired(Conflict):
    code = "qualification_expired"
