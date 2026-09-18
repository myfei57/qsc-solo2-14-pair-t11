"""控制平台统一错误层级。

领域层只抛出这一组错误，HTTP 层再把 ``code`` 与 ``http_status`` 映射成
JSON 响应，避免每个模块各自拼接错误报文。
"""

from __future__ import annotations

from typing import Any


class BreweryError(Exception):
    """所有平台错误的基类。"""

    code = "brewery_error"
    http_status = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            doc["details"] = self.details
        return doc


class ValidationError(BreweryError):
    """请求参数或业务数值不合法。"""

    code = "validation_error"
    http_status = 400


class NotFoundError(BreweryError):
    """引用的对象不存在。"""

    code = "not_found"
    http_status = 404


class ConflictError(BreweryError):
    """当前状态不允许该操作。"""

    code = "conflict"
    http_status = 409


class SequenceError(ConflictError):
    """工艺步骤顺序被破坏。"""

    code = "sequence_violation"


class InterlockError(ConflictError):
    """联锁条件未满足。"""

    code = "interlock_blocked"


class LatchError(ConflictError):
    """告警闩锁未复位。"""

    code = "latch_engaged"


class MaintenanceRequiredError(InterlockError):
    """清洗或维护凭证不可用。"""

    code = "maintenance_required"


class QuotaExceededError(ConflictError):
    """命名空间配额已满。"""

    code = "quota_exceeded"


class PersistenceError(BreweryError):
    """落盘、快照或日志异常。"""

    code = "persistence_error"
