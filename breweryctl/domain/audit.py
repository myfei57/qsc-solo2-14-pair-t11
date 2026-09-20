"""批次审计日志。"""

from __future__ import annotations

from typing import Any, Callable

from ..core.clock import Clock, format_moment
from ..core.errors import NotFoundError
from ..core.ids import new_id
from ..core.validators import require_text
from ..persistence.store import FileStore
from .models import AuditEntry

AUDIT_ENTRIES = "audit_entries"

#: 审计记录落盘后的回调（用于事件外发枢纽订阅）。
AuditSink = Callable[[dict[str, Any]], None]


class AuditLog:
    """记录不可变审计事实。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.entries = store.collection(AUDIT_ENTRIES)
        self._sink: AuditSink | None = None

    def set_sink(self, sink: AuditSink | None) -> None:
        """挂接审计记录发布回调。"""

        self._sink = sink

    def record(
        self,
        brewery_id: str,
        batch_id: str | None,
        actor: str,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入一条审计记录。"""

        entry = AuditEntry(
            id=new_id("audit"),
            brewery_id=require_text(brewery_id, field="brewery_id", max_length=64),
            batch_id=batch_id,
            actor=require_text(actor, field="actor", max_length=60),
            action=require_text(action, field="action", max_length=80),
            detail=dict(detail or {}),
            recorded_at=format_moment(self.clock.now()),
        )
        document = self.entries.put(entry.id, entry.to_doc())
        self._dispatch(document)
        return document

    def _dispatch(self, entry: dict[str, Any]) -> None:
        """把已落盘记录交给发布回调；回调异常不得影响业务操作。"""

        sink = self._sink
        if sink is None:
            return
        try:
            sink(entry)
        except Exception:  # noqa: BLE001 - 外发链路故障不能反向打挂工艺操作
            import logging

            logging.getLogger("breweryctl.events").exception(
                "审计事件发布失败 action=%s", entry.get("action")
            )

    def history(self, batch_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """返回审计记录，可按批次过滤。"""

        items = self.entries.all()
        if batch_id:
            items = [item for item in items if item.get("batch_id") == batch_id]
        items.sort(key=lambda item: str(item.get("recorded_at", "")))
        if limit > 0:
            items = items[-limit:]
        return items

    def get(self, entry_id: str) -> dict[str, Any]:
        """读取单条审计记录。"""

        document = self.entries.get(entry_id)
        if document is None:
            raise NotFoundError("审计记录不存在", entry_id=entry_id)
        return document

    def export_batch(self, batch_id: str) -> dict[str, Any]:
        """导出批次的审计包。"""

        entries = self.history(batch_id=batch_id, limit=0)
        return {
            "batch_id": batch_id,
            "count": len(entries),
            "first_at": entries[0]["recorded_at"] if entries else None,
            "last_at": entries[-1]["recorded_at"] if entries else None,
            "actions": sorted({str(item.get("action")) for item in entries}),
            "entries": entries,
        }

    def count(self) -> int:
        """返回审计记录总数。"""

        return self.entries.count()
