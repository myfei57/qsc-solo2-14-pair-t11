"""事件枢纽：把审计与告警发布点接入发件箱，并负责与本地记录对账。

枢纽持有各发布点（:class:`~breweryctl.domain.audit.AuditLog`、
:class:`~breweryctl.domain.alarms.AlarmCenter`），这些发布点在业务动作成功
落盘后同步调用枢纽；枢纽只做入箱（同存储事务），网络投递完全交给后台中继，
因此外发链路故障不会影响工艺操作本身。
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.clock import Clock, format_moment, parse_moment
from ..persistence.store import FileStore
from .categories import SOURCE_ALARM, SOURCE_AUDIT, is_critical_action
from .outbox import EventOutbox, OutboxStatus
from .relay import EventRelay

LOGGER = logging.getLogger("breweryctl.events")


class EventHub:
    """对外暴露统一的事件发布与对账接口。"""

    def __init__(self, store: FileStore, clock: Clock, relay: EventRelay | None = None) -> None:
        self.store = store
        self.clock = clock
        self.outbox = EventOutbox(store, clock)
        self.relay = relay
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------ 发布

    def publish_audit(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """审计记录落盘后调用，只外发关键工艺动作。"""

        action = str(entry.get("action", ""))
        if not is_critical_action(action):
            return None
        return self._publish(
            kind=action,
            source=SOURCE_AUDIT,
            source_id=str(entry["id"]),
            payload={
                "actor": entry.get("actor"),
                "action": action,
                "detail": dict(entry.get("detail", {})),
                "recorded_at": entry.get("recorded_at"),
            },
            brewery_id=_as_text(entry.get("brewery_id")),
            batch_id=_as_text(entry.get("batch_id")),
            occurred_at_text=_as_text(entry.get("recorded_at")),
        )

    def publish_alarm(self, alarm: dict[str, Any], transition: str) -> dict[str, Any] | None:
        """告警实时迁移时调用，按当前状态做一次守卫避免误发。"""

        if transition not in {"raised", "repeated", "acknowledged", "resolved"}:
            return None
        status = str(alarm.get("status", ""))
        if transition == "raised" and status != "active":
            return None
        if transition == "acknowledged" and status != "acknowledged":
            return None
        if transition == "resolved" and status != "resolved":
            return None
        return self.emit_alarm_transition(alarm, transition)

    def emit_alarm_transition(
        self, alarm: dict[str, Any], transition: str
    ) -> dict[str, Any] | None:
        """按告警文档上的迁移时间戳构造事件，不做当前状态守卫。

        对账时告警文档可能已经处于更后的生命周期阶段，只能依据各自的
        时间戳判定迁移是否发生过。
        """

        if transition == "repeated":
            repeat_count = int(dict(alarm.get("context", {})).get("repeat_count", 1))
            now_text = format_moment(self.clock.now())
            return self._publish(
                kind="alarm.repeated",
                source=SOURCE_ALARM,
                source_id=f"{alarm['id']}:repeated:{repeat_count}",
                payload={
                    "alarm_id": alarm.get("id"),
                    "code": alarm.get("code"),
                    "severity": alarm.get("severity"),
                    "source": alarm.get("source"),
                    "message": alarm.get("message"),
                    "repeat_count": repeat_count,
                    "context": dict(alarm.get("context", {})),
                    "transition_at": now_text,
                },
                brewery_id=_as_text(alarm.get("brewery_id")),
                batch_id=_as_text(_batch_from_context(alarm.get("context"))),
                occurred_at_text=now_text,
            )
        if transition not in {"raised", "acknowledged", "resolved"}:
            return None
        occurred_at_text = {
            "raised": _as_text(alarm.get("raised_at")),
            "acknowledged": _as_text(alarm.get("acknowledged_at")),
            "resolved": _as_text(alarm.get("resolved_at")),
        }[transition]
        if not occurred_at_text:
            return None
        return self._publish(
            kind=f"alarm.{transition}",
            source=SOURCE_ALARM,
            source_id=f"{alarm['id']}:{transition}",
            payload={
                "alarm_id": alarm.get("id"),
                "code": alarm.get("code"),
                "severity": alarm.get("severity"),
                "source": alarm.get("source"),
                "message": alarm.get("message"),
                "latching": bool(alarm.get("latching")),
                "status": str(alarm.get("status", "")),
                "context": dict(alarm.get("context", {})),
                "transition_at": occurred_at_text,
            },
            brewery_id=_as_text(alarm.get("brewery_id")),
            batch_id=_as_text(_batch_from_context(alarm.get("context"))),
            occurred_at_text=occurred_at_text,
        )

    def publish_raw(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        brewery_id: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """供工艺组件直接发布自定义关键事件。"""

        from ..core.ids import new_id

        return self._publish(
            kind=kind,
            source="manual",
            source_id=new_id("evtraw"),
            payload=payload,
            brewery_id=brewery_id,
            batch_id=batch_id,
            occurred_at_text=format_moment(self.clock.now()),
        )

    # ------------------------------------------------------------------ 对账

    def reconcile(self) -> dict[str, Any]:
        """扫描本地审计与告警记录，把漏发的关键事件补入发件箱。

        对账只追加缺失事件，绝不改动已送达记录；因此重复执行是安全的，
        补箱后箱内事件与本地事实逐条对得上。
        """

        missing_audit = self._backfill_audit()
        missing_alarms = self._backfill_alarms()
        assigned = self.outbox.assign_sequences()
        report = {
            "checked_at": format_moment(self.clock.now()),
            "backfilled": len(missing_audit) + len(missing_alarms),
            "backfilled_audit": missing_audit,
            "backfilled_alarms": missing_alarms,
            "sequences_assigned": assigned,
            **self.match_report(),
        }
        if report["backfilled"] and self.relay is not None:
            self.relay.notify()
        return report

    def match_report(self) -> dict[str, Any]:
        """把发件箱与本地关键记录逐条核对，返回对账明细。"""

        expected: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.store.collection("audit_entries").all():
            if is_critical_action(str(entry.get("action", ""))):
                expected[(SOURCE_AUDIT, str(entry["id"]))] = entry
        for alarm in self.store.collection("alarms").all():
            for transition, at_field in (
                ("raised", "raised_at"),
                ("acknowledged", "acknowledged_at"),
                ("resolved", "resolved_at"),
            ):
                if alarm.get(at_field):
                    expected[(SOURCE_ALARM, f"{alarm['id']}:{transition}")] = alarm
        outbox_index = {
            (str(item.get("source")), str(item.get("source_id"))): item
            for item in self.outbox.events.all()
        }
        missing = [
            {"source": source, "source_id": source_id}
            for (source, source_id) in expected
            if (source, source_id) not in outbox_index
        ]
        orphan = [
            {"event_id": str(item.get("event_id")), "source": source, "source_id": source_id}
            for (source, source_id), item in outbox_index.items()
            if (source, source_id) not in expected and source in {SOURCE_AUDIT, SOURCE_ALARM}
        ]
        stats = self.outbox.stats()
        pending = [
            {
                "event_id": str(item.get("event_id")),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "attempts": item.get("attempts", 0),
                "last_error": item.get("last_error"),
                "occurred_at": item.get("occurred_at"),
            }
            for item in self.outbox.list_events(status=OutboxStatus.QUEUED, limit=0)
            + self.outbox.list_events(status=OutboxStatus.SENDING, limit=0)
        ]
        pending.sort(key=lambda item: str(item.get("occurred_at", "")))
        return {
            "local_critical_records": len(expected),
            "outbox_total": stats["total"],
            "queued": stats[OutboxStatus.QUEUED],
            "sending": stats[OutboxStatus.SENDING],
            "delivered": stats[OutboxStatus.DELIVERED],
            "dead": stats[OutboxStatus.DEAD],
            "missing_in_outbox": missing,
            "orphan_events": orphan,
            "pending_events": pending,
            "matched": len(expected) - len(missing),
        }

    # ------------------------------------------------------------------ 内部

    def _publish(
        self,
        *,
        kind: str,
        source: str,
        source_id: str,
        payload: dict[str, Any],
        brewery_id: str | None,
        batch_id: str | None,
        occurred_at_text: str | None,
    ) -> dict[str, Any] | None:
        with self._lock:
            occurred = parse_moment(occurred_at_text) if occurred_at_text else None
            event = self.outbox.enqueue(
                kind=kind,
                source=source,
                source_id=source_id,
                payload=payload,
                brewery_id=brewery_id,
                batch_id=batch_id,
                occurred_at=occurred,
            )
            if event is None:
                return None
            self.outbox.assign_sequences()
        if self.relay is not None:
            self.relay.notify()
        return event

    def _backfill_audit(self) -> list[str]:
        backfilled: list[str] = []
        for entry in self.store.collection("audit_entries").all():
            event = self.publish_audit(entry)
            if event is not None:
                backfilled.append(str(event["event_id"]))
        return backfilled

    def _backfill_alarms(self) -> list[str]:
        backfilled: list[str] = []
        for alarm in self.store.collection("alarms").all():
            for transition, at_field in (
                ("raised", "raised_at"),
                ("acknowledged", "acknowledged_at"),
                ("resolved", "resolved_at"),
            ):
                if not alarm.get(at_field):
                    continue
                event = self.emit_alarm_transition(alarm, transition)
                if event is not None:
                    backfilled.append(str(event["event_id"]))
            repeat_count = int(dict(alarm.get("context", {})).get("repeat_count", 0))
            if repeat_count > 1:
                event = self.emit_alarm_transition(alarm, "repeated")
                if event is not None:
                    backfilled.append(str(event["event_id"]))
        return backfilled


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _batch_from_context(context: Any) -> Any:
    if isinstance(context, dict):
        return context.get("batch_id")
    return None
