"""API 输出序列化。"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.errors import BreweryError


def error_payload(error: BreweryError) -> dict[str, Any]:
    """把领域错误转成响应体。"""

    return error.to_doc()


def unexpected_payload(error: Exception) -> dict[str, Any]:
    """把未预期异常转成响应体。"""

    return {
        "error": "internal_error",
        "message": "服务器处理请求时发生未预期异常",
        "details": {"type": type(error).__name__, "reason": str(error)},
    }


def recipe_summary(document: dict[str, Any]) -> dict[str, Any]:
    """配方列表项。"""

    return {
        "id": document.get("id"),
        "name": document.get("name"),
        "style": document.get("style"),
        "status": document.get("status"),
        "version": document.get("current_version"),
        "volume_l": document.get("volume_l"),
        "og_target": document.get("og_target"),
        "ibu_target": document.get("ibu_target"),
        "mash_steps": len(document.get("mash_steps", [])),
        "hop_additions": len(document.get("hop_schedule", [])),
    }


def batch_summary(document: dict[str, Any]) -> dict[str, Any]:
    """批次列表项。"""

    return {
        "id": document.get("id"),
        "code": document.get("code"),
        "stage": document.get("stage"),
        "recipe_id": document.get("recipe_id"),
        "recipe_version": document.get("recipe_version"),
        "volume_l": document.get("volume_l"),
        "tank_id": document.get("tank_id"),
        "priority": document.get("priority"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }


def batch_collection(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批次列表序列化。"""

    return [batch_summary(item) for item in documents]


def tank_summary(document: dict[str, Any]) -> dict[str, Any]:
    """发酵罐列表项。"""

    return {
        "id": document.get("id"),
        "code": document.get("code"),
        "stage": document.get("stage"),
        "capacity_l": document.get("capacity_l"),
        "batch_id": document.get("batch_id"),
        "sanitized_at": document.get("sanitized_at"),
        "cip_certificate_id": document.get("cip_certificate_id"),
    }


def alarm_view(document: dict[str, Any]) -> dict[str, Any]:
    """告警列表项。"""

    return {
        "id": document.get("id"),
        "source": document.get("source"),
        "severity": document.get("severity"),
        "code": document.get("code"),
        "message": document.get("message"),
        "status": document.get("status"),
        "latching": bool(document.get("latching")),
        "raised_at": document.get("raised_at"),
        "acknowledged_at": document.get("acknowledged_at"),
        "resolved_at": document.get("resolved_at"),
        "context": document.get("context", {}),
    }


def alarm_collection(documents: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """告警列表序列化。"""

    return [alarm_view(item) for item in documents]


def page_banner(overview: dict[str, Any]) -> dict[str, Any]:
    """前端页眉使用的关键指标。"""

    return {
        "brewery": overview.get("namespace", {}).get("breweries", 0),
        "active_batches": overview.get("batches", {}).get("active", 0),
        "busy_tanks": overview.get("ferment", {}).get("busy", 0),
        "active_alarms": overview.get("alarms", {}).get("active", 0),
        "latching_alarms": overview.get("alarms", {}).get("latching", 0),
    }


def telemetry_view(reading: dict[str, Any]) -> dict[str, Any]:
    """温度采样输出。"""

    return {
        "probe_id": reading.get("probe_id"),
        "batch_id": reading.get("batch_id"),
        "value_c": reading.get("value_c"),
        "deviation_c": reading.get("deviation_c"),
        "quality": reading.get("quality"),
        "taken_at": reading.get("taken_at"),
    }
