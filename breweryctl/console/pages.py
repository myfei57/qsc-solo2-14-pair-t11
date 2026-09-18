"""前端页面目录。"""

from __future__ import annotations

from typing import Any

from ..core.errors import NotFoundError

PAGE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "slug": "mash",
        "title": "糖化控制",
        "file": "mash.html",
        "description": "投料水温确认、升温保温与麦汁过滤",
        "api": [
            "/api/batches",
            "/api/batches/{batch_id}/water",
            "/api/batches/{batch_id}/charge",
            "/api/batches/{batch_id}/heat",
        ],
    },
    {
        "slug": "ferment",
        "title": "发酵控制",
        "file": "ferment.html",
        "description": "降温、转罐、接种与成熟",
        "api": [
            "/api/batches/{batch_id}/transfer",
            "/api/batches/{batch_id}/pitch",
            "/api/batches/{batch_id}/mature",
            "/api/control/tanks/{tank_id}/pressure",
        ],
    },
    {
        "slug": "cip",
        "title": "清洗 CIP",
        "file": "cip.html",
        "description": "清洗步骤推进与合格凭证",
        "api": [
            "/api/maintenance/tanks/{tank_id}/clean",
            "/api/maintenance/cycles/{cycle_id}/advance",
            "/api/maintenance/cycles/{cycle_id}/finish",
        ],
    },
    {
        "slug": "alarms",
        "title": "告警与审计",
        "file": "alarms.html",
        "description": "告警确认、解除与批次审计查询",
        "api": ["/api/alarms", "/api/alarms/{alarm_id}/ack", "/api/audit"],
    },
)


def page_catalog() -> list[dict[str, Any]]:
    """返回页面目录副本。"""

    return [dict(item) for item in PAGE_CATALOG]


def page_file(slug: str) -> str:
    """按 slug 返回页面文件名。"""

    for item in PAGE_CATALOG:
        if item["slug"] == slug:
            return str(item["file"])
    raise NotFoundError("页面不存在", slug=slug)
