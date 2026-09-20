"""关键事件登记入口。

服务层不直接操作发件箱，而是通过 :class:`EventPublisher`：它把"业务动作"
映射成外发事件，统一生成幂等键、补齐批次/工厂信息，并在未配置外发时
安静降级，保证现有单测与离线部署不受影响。
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from ..core.errors import BreweryError
from .events import is_critical

if TYPE_CHECKING:
    from .outbox import Outbox
    from .relay import OutboxRelay

LOGGER = logging.getLogger("breweryctl.outbound.publisher")


class EventPublisher:
    """把领域动作登记进发件箱的薄封装。"""

    def __init__(
        self,
        outbox: "Outbox | None" = None,
        relay: "OutboxRelay | None" = None,
    ) -> None:
        self.outbox = outbox
        self.relay = relay

    def bind_relay(self, relay: "OutboxRelay") -> None:
        """装配阶段补发件中继，使新事件可以立刻触发发送。"""

        self.relay = relay

    @property
    def enabled(self) -> bool:
        """是否实际配置了发件箱。"""

        return self.outbox is not None

    def publish_from_audit(
        self,
        action: str,
        *,
        audit_id: str,
        brewery_id: str | None,
        batch_id: str | None,
        detail: dict[str, Any],
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        """审计动作落盘后调用：关键动作同步进箱。

        幂等键固定为 ``audit_id``：同一条审计记录重放（崩溃恢复、重试）
        不会产生第二条外发事件。
        """

        if self.outbox is None or not is_critical(action):
            return None
        payload: dict[str, Any] = dict(detail or {})
        if actor:
            payload["actor"] = actor
        try:
            document = self.outbox.publish(
                action,
                payload,
                brewery_id=str(brewery_id) if brewery_id else None,
                batch_id=str(batch_id) if batch_id else None,
                event_id=audit_id,
            )
            self._notify()
            return document
        except BreweryError as exc:
            # 外发登记失败不能回滚已经成功的工艺动作；记录日志待对账发现。
            LOGGER.error("关键事件登记失败 action=%s: %s", action, exc.message)
            return None

    def publish_alarm(self, alarm: dict[str, Any], *, event_suffix: str = "raised") -> dict[str, Any] | None:
        """把告警生命周期动作登记成事件。"""

        if self.outbox is None:
            return None
        kind = f"alarm.{event_suffix}"
        if not is_critical(kind):
            return None
        try:
            document = self.outbox.publish(
                kind,
                {
                    "alarm_id": alarm.get("id"),
                    "source": alarm.get("source"),
                    "severity": alarm.get("severity"),
                    "code": alarm.get("code"),
                    "message": alarm.get("message"),
                    "latching": alarm.get("latching", False),
                    "context": alarm.get("context") or {},
                },
                brewery_id=str(alarm.get("brewery_id") or "") or None,
                batch_id=(alarm.get("context") or {}).get("batch_id"),
                event_id=f"{alarm.get('id')}:{event_suffix}",
            )
            self._notify()
            return document
        except BreweryError as exc:
            LOGGER.error("告警事件登记失败 alarm=%s: %s", alarm.get("id"), exc.message)
            return None

    def _notify(self) -> None:
        """新事件进箱后唤醒中继，实现"产生即发"。"""

        if self.relay is not None:
            self.relay.kick()
