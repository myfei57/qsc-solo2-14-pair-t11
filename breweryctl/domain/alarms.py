"""告警中心：产生、确认、复位与闩锁保持。"""

from __future__ import annotations

from typing import Any, Callable

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_choice, require_text
from ..persistence.store import FileStore, merge_documents
from .models import Alarm, AlarmSeverity, AlarmStatus

ALARMS = "alarms"

#: 告警生命周期回调：(告警文档, 迁移名 raised/repeated/acknowledged/resolved)。
AlarmSink = Callable[[dict[str, Any], str], None]


class AlarmCenter:
    """集中管理来自各工艺组件的告警。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.alarms = store.collection(ALARMS)
        self._sink: AlarmSink | None = None

    def set_sink(self, sink: AlarmSink | None) -> None:
        """挂接告警生命周期发布回调。"""

        self._sink = sink

    def _dispatch(self, alarm: dict[str, Any], transition: str) -> None:
        """把已落盘的告警迁移交给发布回调；回调异常不得影响业务操作。"""

        sink = self._sink
        if sink is None:
            return
        try:
            sink(alarm, transition)
        except Exception:  # noqa: BLE001 - 外发链路故障不能反向打挂告警处理
            import logging

            logging.getLogger("breweryctl.events").exception(
                "告警事件发布失败 alarm=%s transition=%s", alarm.get("id"), transition
            )

    def raise_alarm(
        self,
        brewery_id: str,
        source: str,
        severity: str,
        code: str,
        message: str,
        latching: bool = False,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """产生告警；同一来源同一编码的活跃告警会累计重复次数。"""

        clean_source = require_text(source, field="source", max_length=60)
        clean_code = require_text(code, field="code", max_length=60)
        clean_severity = require_choice(
            severity, field="severity", choices=[item.value for item in AlarmSeverity]
        )
        clean_message = require_text(message, field="message", max_length=240)
        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        repeated = self.alarms.find(
            lambda item: item.get("source") == clean_source
            and item.get("code") == clean_code
            and item.get("status") == AlarmStatus.ACTIVE.value
        )
        if repeated:
            existing = repeated[0]

            def repeat(document: dict[str, Any]) -> dict[str, Any]:
                merged_context = dict(document.get("context", {}))
                merged_context.update(context or {})
                merged_context["repeat_count"] = int(merged_context.get("repeat_count", 1)) + 1
                return merge_documents(document, [("context", merged_context)])

            updated = self.alarms.update(str(existing["id"]), repeat)
            self._dispatch(updated, "repeated")
            return updated
        now = format_moment(self.clock.now())
        alarm = Alarm(
            id=new_id("alarm"),
            brewery_id=clean_brewery,
            source=clean_source,
            severity=clean_severity,
            code=clean_code,
            message=clean_message,
            status=AlarmStatus.ACTIVE.value,
            latching=latching,
            context=dict(context or {}),
            raised_at=now,
        )
        document = self.alarms.put(alarm.id, alarm.to_doc())
        self._dispatch(document, "raised")
        return document

    def acknowledge(self, alarm_id: str, operator: str) -> dict[str, Any]:
        """确认告警，表示操作员已经看到。"""

        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") == AlarmStatus.RESOLVED.value:
                raise ConflictError("告警已经解除", alarm_id=alarm_id)
            now = format_moment(self.clock.now())
            merged_context = dict(document.get("context", {}))
            merged_context["acknowledged_by"] = clean_operator
            return merge_documents(
                document,
                [
                    ("status", AlarmStatus.ACKNOWLEDGED.value),
                    ("acknowledged_at", now),
                    ("context", merged_context),
                ],
            )

        with self.store.locks.guard(f"alarm:{alarm_id}"):
            updated = self.alarms.update(alarm_id, mutate)
        self._dispatch(updated, "acknowledged")
        return updated

    def resolve(self, alarm_id: str, operator: str, note: str) -> dict[str, Any]:
        """解除告警；闩锁告警必须先确认。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_note = require_text(note, field="note", max_length=240)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            status = document.get("status")
            if status == AlarmStatus.RESOLVED.value:
                return document
            if document.get("latching") and status != AlarmStatus.ACKNOWLEDGED.value:
                raise ConflictError("闩锁告警必须先确认才能解除", alarm_id=alarm_id)
            now = format_moment(self.clock.now())
            merged_context = dict(document.get("context", {}))
            merged_context["resolved_by"] = clean_operator
            return merge_documents(
                document,
                [
                    ("status", AlarmStatus.RESOLVED.value),
                    ("resolved_at", now),
                    ("resolution_note", clean_note),
                    ("context", merged_context),
                ],
            )

        with self.store.locks.guard(f"alarm:{alarm_id}"):
            was_resolved = self.get(alarm_id).get("status") == AlarmStatus.RESOLVED.value
            updated = self.alarms.update(alarm_id, mutate)
        if not was_resolved:
            self._dispatch(updated, "resolved")
        return updated

    def get(self, alarm_id: str) -> dict[str, Any]:
        """读取单条告警。"""

        document = self.alarms.get(alarm_id)
        if document is None:
            raise NotFoundError("告警不存在", alarm_id=alarm_id)
        return document

    def list_alarms(
        self,
        status: str | None = None,
        brewery_id: str | None = None,
        severity: str | None = None,
    ) -> list[dict[str, Any]]:
        """按条件列出告警，最新在前。"""

        items = self.alarms.all()
        if status:
            clean_status = require_choice(
                status, field="status", choices=[item.value for item in AlarmStatus]
            )
            items = [item for item in items if item.get("status") == clean_status]
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        if severity:
            clean_severity = require_choice(
                severity, field="severity", choices=[item.value for item in AlarmSeverity]
            )
            items = [item for item in items if item.get("severity") == clean_severity]
        return sorted(items, key=lambda item: str(item.get("raised_at", "")), reverse=True)

    def active_count(self, brewery_id: str | None = None) -> int:
        """返回活跃告警数量。"""

        return len(self.list_alarms(status=AlarmStatus.ACTIVE.value, brewery_id=brewery_id))

    def summary(self, brewery_id: str | None = None) -> dict[str, Any]:
        """汇总告警状态。"""

        items = self.list_alarms(brewery_id=brewery_id)
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(items),
            "by_status": counts,
            "active": len([item for item in items if item.get("status") == AlarmStatus.ACTIVE.value]),
            "latching": len(
                [
                    item
                    for item in items
                    if item.get("latching") and item.get("status") != AlarmStatus.RESOLVED.value
                ]
            ),
        }

    def guard(self, alarm_id: str) -> dict[str, Any]:
        """读取并要求告警处于活跃或已确认状态。"""

        document = self.get(alarm_id)
        if document.get("status") == AlarmStatus.RESOLVED.value:
            raise ValidationError("告警已经解除，无需操作", alarm_id=alarm_id)
        return document
