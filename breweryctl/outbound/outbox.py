"""发件箱：关键事件的本地登记表。

与业务文档共用同一个 :class:`~breweryctl.persistence.store.FileStore`，
因此"业务动作落盘"和"事件进箱"在同一条写日志里，要么都在、要么都不在，
不需要分布式事务。事件按全局序号 ``seq`` 排序，中继严格按序领取。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_text
from ..persistence.store import FileStore
from .events import OutboxEvent, OutboxStatus, ensure_critical

OUTBOX_EVENTS = "outbox_events"
MIN_BATCH = 1
MAX_BATCH = 500
DEFAULT_MAX_ATTEMPTS = 20
#: 领取后超过该分钟数仍未确认的事件视为僵死，重新放回待发。
DEFAULT_CLAIM_TTL_MIN = 10.0


class Outbox:
    """关键事件的持久化发件箱。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.events = store.collection(OUTBOX_EVENTS)
        self._enqueue_lock = threading.Lock()

    # ------------------------------------------------------------------ 登记

    def publish(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        brewery_id: str | None = None,
        batch_id: str | None = None,
        event_id: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """登记一条关键事件并立即落盘。

        同一业务动作可用固定 ``event_id`` 重放：登记已存在时直接返回旧文档，
        不产生重复事件（生产侧去重）。
        """

        clean_kind = ensure_critical(kind)
        if payload is not None and not isinstance(payload, dict):
            raise ValidationError("事件负载必须是对象", kind=clean_kind)
        clean_id = require_text(event_id or new_id("evt"), field="event_id", max_length=64)
        existing = self.events.get(clean_id)
        if existing is not None:
            return existing
        now = format_moment(self.clock.now())
        with self._enqueue_lock:
            # 锁内复查，避免并发请求用同一幂等键各写一份。
            if self.events.get(clean_id) is not None:
                return self.events.require(clean_id)
            # 先占一个存储序号，保证发件顺序与业务提交顺序一致。
            source_seq = self.store.next_sequence()
            outbox_seq = self._next_outbox_seq(brewery_id)
            event = OutboxEvent(
                id=clean_id,
                kind=clean_kind,
                source_seq=source_seq,
                occurred_at=now,
                payload=dict(payload or {}),
                brewery_id=brewery_id,
                batch_id=batch_id,
                status=OutboxStatus.PENDING.value,
                max_attempts=max_attempts,
                outbox_seq=outbox_seq,
                created_at=now,
                updated_at=now,
            )
            return self.events.put(clean_id, event.to_doc())

    def _next_outbox_seq(self, brewery_id: str | None) -> int:
        """分配工厂内发件序号：对账时按它检查连续性，不受其它集合写序号影响。"""

        scope = brewery_id or "_global"
        return self.store.increment_meta_counter(f"outbox_seq:{scope}")

    def publish_many(self, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量登记；任一条目不合法时整体拒绝。"""

        clean_items = list(items)
        for item in clean_items:
            if not isinstance(item, dict):
                raise ValidationError("事件条目必须是对象")
            ensure_critical(str(item.get("kind", "")))
        return [
            self.publish(
                str(item["kind"]),
                item.get("payload") if isinstance(item.get("payload"), dict) else {},
                brewery_id=item.get("brewery_id"),
                batch_id=item.get("batch_id"),
                event_id=item.get("event_id"),
            )
            for item in clean_items
        ]

    # ------------------------------------------------------------------ 领取

    def claim_next(self, limit: int = 32, ttl_min: float = DEFAULT_CLAIM_TTL_MIN) -> list[dict[str, Any]]:
        """按全局序号领取最早的一批可发送事件。

        ``pending`` 与僵死的 ``in_flight`` 都会被领取；领取动作是条件更新，
        并发中继不会拿到同一条。返回的文档状态为 ``in_flight``。
        """

        if not isinstance(limit, int) or not (MIN_BATCH <= limit <= MAX_BATCH):
            raise ValidationError("领取数量超出范围", limit=limit)
        self.reclaim_stale(ttl_min=ttl_min)
        candidates = [
            item
            for item in self.events.all()
            if item.get("status") == OutboxStatus.PENDING.value
        ]
        candidates.sort(key=_candidate_order)
        claimed: list[dict[str, Any]] = []
        for candidate in candidates:
            if len(claimed) >= limit:
                break
            event_id = str(candidate["id"])
            try:
                claimed.append(self._compare_and_set_status(event_id, OutboxStatus.PENDING, OutboxStatus.IN_FLIGHT))
            except ConflictError:
                # 被别的中继抢先领取，跳过。
                continue
        return claimed

    def reclaim_stale(self, ttl_min: float = DEFAULT_CLAIM_TTL_MIN) -> int:
        """把领取后超时未确认的事件退回 ``pending``，返回回收条数。"""

        if ttl_min < 0:
            raise ValidationError("领取超时不能为负", ttl_min=ttl_min)
        from ..core.clock import elapsed_minutes

        now = format_moment(self.clock.now())
        reclaimed = 0
        for item in self.events.all():
            if item.get("status") != OutboxStatus.IN_FLIGHT.value:
                continue
            updated_at = str(item.get("updated_at") or item.get("created_at") or "")
            if not updated_at:
                continue
            try:
                stuck = elapsed_minutes(updated_at, now) >= ttl_min
            except ValidationError:
                stuck = True
            if not stuck:
                continue
            event_id = str(item["id"])
            try:
                self._compare_and_set_status(event_id, OutboxStatus.IN_FLIGHT, OutboxStatus.PENDING)
                reclaimed += 1
            except ConflictError:
                continue
        return reclaimed

    # ------------------------------------------------------------------ 结果

    def mark_sent(self, event_id: str, acknowledged_id: str | None = None) -> dict[str, Any]:
        """接收端确认后标记送达；重复确认是幂等操作。"""

        now = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            status = document.get("status")
            if status == OutboxStatus.SENT.value:
                return document
            if status == OutboxStatus.DEAD.value:
                raise ConflictError("事件已进入死信状态，不能直接确认", event_id=event_id)
            document["status"] = OutboxStatus.SENT.value
            document["sent_at"] = now
            document["updated_at"] = now
            document["last_error"] = None
            if acknowledged_id is not None:
                document["acknowledged_id"] = str(acknowledged_id)
            return document

        return self.events.update(event_id, mutate)

    def mark_failed(self, event_id: str, error: str, *, retryable: bool = True) -> dict[str, Any]:
        """登记一次发送失败。

        可重试错误：退回 ``pending``，``attempts`` 加一；
        达到 ``max_attempts`` 或错误明确不可重试时进入 ``dead`` 死信。
        """

        clean_error = require_text(error, field="error", max_length=300)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            attempts = int(document.get("attempts", 0)) + 1
            document["attempts"] = attempts
            document["last_error"] = clean_error
            document["updated_at"] = format_moment(self.clock.now())
            exhausted = attempts >= int(document.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
            if not retryable or exhausted:
                document["status"] = OutboxStatus.DEAD.value
            else:
                document["status"] = OutboxStatus.PENDING.value
            return document

        return self.events.update(event_id, mutate)

    def revive(self, event_id: str) -> dict[str, Any]:
        """人工把死信事件重新放回待发队列。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") != OutboxStatus.DEAD.value:
                raise ConflictError("只有死信事件可以复活", event_id=event_id)
            document["status"] = OutboxStatus.PENDING.value
            document["attempts"] = 0
            document["last_error"] = None
            document["updated_at"] = format_moment(self.clock.now())
            return document

        return self.events.update(event_id, mutate)

    # ------------------------------------------------------------------ 查询

    def get(self, event_id: str) -> dict[str, Any]:
        """读取单条事件，不存在时抛 :class:`NotFoundError`。"""

        document = self.events.get(event_id)
        if document is None:
            raise NotFoundError("外发事件不存在", event_id=event_id)
        return document

    def list_events(
        self,
        status: str | None = None,
        batch_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """按状态/批次查看事件，默认按全局序号升序。"""

        items = self.events.all()
        if status is not None:
            expected = require_text(status, field="status", max_length=20)
            items = [item for item in items if item.get("status") == expected]
        if batch_id is not None:
            clean_batch = require_text(batch_id, field="batch_id", max_length=64)
            items = [item for item in items if item.get("batch_id") == clean_batch]
        items.sort(key=_candidate_order)
        if limit > 0:
            items = items[:limit]
        return items

    def pending_count(self) -> int:
        """尚未送达（含在途）的事件数。"""

        return sum(
            1
            for item in self.events.all()
            if item.get("status") in (OutboxStatus.PENDING.value, OutboxStatus.IN_FLIGHT.value)
        )

    def stats(self) -> dict[str, Any]:
        """发件箱计数与最新序号，供控制台首页与对账使用。"""

        items = self.events.all()
        by_status = {status.value: 0 for status in OutboxStatus}
        max_seq = 0
        max_sent_seq = 0
        for item in items:
            status = str(item.get("status"))
            by_status[status] = by_status.get(status, 0) + 1
            seq = int(item.get("outbox_seq") or item.get("source_seq", 0))
            max_seq = max(max_seq, seq)
            if status == OutboxStatus.SENT.value:
                max_sent_seq = max(max_sent_seq, seq)
        return {
            "total": len(items),
            "pending": by_status.get(OutboxStatus.PENDING.value, 0),
            "in_flight": by_status.get(OutboxStatus.IN_FLIGHT.value, 0),
            "sent": by_status.get(OutboxStatus.SENT.value, 0),
            "dead": by_status.get(OutboxStatus.DEAD.value, 0),
            "max_outbox_seq": max_seq,
            "max_sent_outbox_seq": max_sent_seq,
        }

    # ------------------------------------------------------------------ 内部

    def _compare_and_set_status(
        self,
        event_id: str,
        expected: OutboxStatus,
        target: OutboxStatus,
    ) -> dict[str, Any]:
        """在对象锁内做状态条件转移，失败抛冲突。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") != expected.value:
                raise ConflictError(
                    "事件状态已被其他中继修改",
                    event_id=event_id,
                    expected=expected.value,
                    actual=document.get("status"),
                )
            document["status"] = target.value
            document["updated_at"] = format_moment(self.clock.now())
            return document

        return self.events.update(event_id, mutate)


def _candidate_order(document: dict[str, Any]) -> tuple[int, str]:
    """按发件序号排序；序号缺失时退化到存储序号，保证顺序稳定。"""

    order = int(document.get("outbox_seq", 0) or 0)
    if order == 0:
        order = int(document.get("source_seq", 0) or 0)
    return order, str(document.get("id", ""))
