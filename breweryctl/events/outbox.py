"""事件发件箱：与业务数据同一存储的待发事件集合。

状态机::

    queued ──claim──▶ sending ──成功──▶ delivered
                        │
                        ├──临时故障──▶ queued（attempts 累加，next_attempt_at 退避）
                        └──明确拒收──▶ dead

``source`` + ``source_id`` 唯一标识一条本地事实，重复入箱会被幂等忽略，
保证重复发布（包括崩溃恢复后对账补箱）不会产生重复事件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.clock import Clock, format_moment, parse_moment
from ..core.ids import new_id
from ..core.validators import require_int, require_text
from ..persistence.store import FileStore, merge_documents

OUTBOX_EVENTS = "event_outbox"

EVENT_SCHEMA = "1.0"


def _format_schedule(moment: datetime) -> str:
    """调度字段（退避/认领时刻）保留微秒，避免亚秒退避被秒级截断。"""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def order_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """事件排序键：先发生时间，再入箱顺序，最后事件 ID 兜底。"""

    return (
        str(item.get("occurred_at", "")),
        int(item.get("enqueue_order", 0) or 0),
        str(item.get("event_id", "")),
    )


class OutboxStatus:
    """发件箱事件状态常量。"""

    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    DEAD = "dead"


ALL_STATUSES = (OutboxStatus.QUEUED, OutboxStatus.SENDING, OutboxStatus.DELIVERED, OutboxStatus.DEAD)
_PENDING_STATUSES = (OutboxStatus.QUEUED, OutboxStatus.SENDING)


class EventOutbox:
    """在 :class:`FileStore` 上维护待外发事件。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.events = store.collection(OUTBOX_EVENTS)

    # ------------------------------------------------------------------ 写入

    def enqueue(
        self,
        *,
        kind: str,
        source: str,
        source_id: str,
        payload: dict[str, Any],
        brewery_id: str | None = None,
        batch_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """幂等入箱；同一来源事实已经在箱内时返回 ``None``。"""

        clean_kind = require_text(kind, field="kind", max_length=80)
        clean_source = require_text(source, field="source", max_length=40)
        clean_source_id = require_text(source_id, field="source_id", max_length=80)
        existing = self._find_source(clean_source, clean_source_id)
        if existing is not None:
            return None
        now_text = format_moment(self.clock.now())
        occurred_text = format_moment(occurred_at or self.clock.now())
        # 记录入箱瞬间的存储序列号，作为同一发生秒内的稳定次序，
        # 避免排序退化成 event_id 字典序导致顺序与业务发生顺序不一致。
        enqueue_order = int(self.store.stats()["sequence"])
        document = {
            "event_id": new_id("evt"),
            "schema": EVENT_SCHEMA,
            "kind": clean_kind,
            "source": clean_source,
            "source_id": clean_source_id,
            "brewery_id": brewery_id,
            "batch_id": batch_id,
            "payload": dict(payload or {}),
            "status": OutboxStatus.QUEUED,
            "occurred_at": occurred_text,
            "created_at": now_text,
            "attempts": 0,
            "last_error": None,
            "claimed_at": None,
            "delivered_at": None,
            "next_attempt_at": now_text,
            "sequence": 0,
            "enqueue_order": enqueue_order,
        }
        return self.events.put(document["event_id"], document)

    def claim_next(self, stale_after_s: int, limit: int) -> list[dict[str, Any]]:
        """领取到期可发的事件，按发生时间升序，返回被置为 sending 的副本。"""

        require_int(limit, field="limit", minimum=1, maximum=1024)
        claimed: list[dict[str, Any]] = []
        candidates = sorted(
            (item for item in self.events.all() if item.get("status") in _PENDING_STATUSES),
            key=order_key,
        )
        now = self.clock.now()
        for item in candidates:
            if len(claimed) >= limit:
                break
            if not self._is_due(item, now, stale_after_s):
                continue
            status = str(item.get("status"))
            attempts = int(item.get("attempts", 0)) + (1 if status == OutboxStatus.QUEUED else 0)

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                return merge_documents(
                    document,
                    [
                        ("status", OutboxStatus.SENDING),
                        ("claimed_at", _format_schedule(now)),
                        ("attempts", attempts),
                    ],
                )

            claimed.append(self.events.update(str(item["event_id"]), mutate))
        return claimed

    def mark_delivered(self, event_id: str) -> dict[str, Any] | None:
        """标记投递成功。"""

        if self.events.get(event_id) is None:
            return None
        now_text = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("status", OutboxStatus.DELIVERED),
                    ("delivered_at", now_text),
                    ("claimed_at", None),
                    ("last_error", None),
                ],
            )

        return self.events.update(event_id, mutate)

    def requeue(self, event_id: str, error: str, next_attempt_at: datetime) -> dict[str, Any] | None:
        """投递遇到临时故障，按给定退避时刻重新排队。"""

        if self.events.get(event_id) is None:
            return None

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("status", OutboxStatus.QUEUED),
                    ("claimed_at", None),
                    ("last_error", error[:300]),
                    ("next_attempt_at", _format_schedule(next_attempt_at)),
                ],
            )

        return self.events.update(event_id, mutate)

    def release_tail(self, event_ids: list[str], not_before: datetime) -> None:
        """把已领取但本轮不发送的事件放回队列。

        队首事件临时失败时，为保证投递顺序不超车，同批尾部事件退回排队，
        并与队首同一退避时刻后再发；attempts 回退（本轮不算一次尝试）。
        """

        when = _format_schedule(not_before)
        for event_id in event_ids:
            if self.events.get(event_id) is None:
                continue

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                if document.get("status") != OutboxStatus.SENDING:
                    return document
                return merge_documents(
                    document,
                    [
                        ("status", OutboxStatus.QUEUED),
                        ("claimed_at", None),
                        ("attempts", max(0, int(document.get("attempts", 1)) - 1)),
                        ("next_attempt_at", when),
                    ],
                )

            self.events.update(event_id, mutate)

    def mark_dead(self, event_id: str, error: str) -> dict[str, Any] | None:
        """对端明确拒收，转入死信不再自动续传。"""

        if self.events.get(event_id) is None:
            return None
        now_text = format_moment(self.clock.now())

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("status", OutboxStatus.DEAD),
                    ("claimed_at", None),
                    ("dead_at", now_text),
                    ("last_error", error[:300]),
                ],
            )

        return self.events.update(event_id, mutate)

    def replay_dead(self, event_id: str | None = None) -> int:
        """把死信（或指定死信）重新排队，返回复活条数。"""

        now_text = _format_schedule(self.clock.now())
        targets = self.events.find(lambda item: item.get("status") == OutboxStatus.DEAD)
        revived = 0
        for item in targets:
            if event_id is not None and str(item.get("event_id")) != event_id:
                continue

            def mutate(document: dict[str, Any]) -> dict[str, Any]:
                return merge_documents(
                    document,
                    [("status", OutboxStatus.QUEUED), ("next_attempt_at", now_text), ("dead_at", None)],
                )

            self.events.update(str(item["event_id"]), mutate)
            revived += 1
        return revived

    # ------------------------------------------------------------------ 查询

    def get(self, event_id: str) -> dict[str, Any] | None:
        """按事件 ID 读取。"""

        return self.events.get(event_id)

    def list_events(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """列出事件，最早发生在前。"""

        items = self.events.all()
        if status is not None:
            if status not in ALL_STATUSES:
                raise ValueError(f"未知事件状态: {status}")
            items = [item for item in items if item.get("status") == status]
        items.sort(key=order_key)
        if limit > 0:
            items = items[:limit]
        return items

    def pending(self) -> list[dict[str, Any]]:
        """返回所有未送达（排队中或发送中）的事件。"""

        return [item for item in self.events.all() if item.get("status") in _PENDING_STATUSES]

    def stats(self) -> dict[str, int]:
        """按状态计数。"""

        counts = {status: 0 for status in ALL_STATUSES}
        attempts = 0
        for item in self.events.all():
            status = str(item.get("status"))
            if status in counts:
                counts[status] += 1
            attempts += int(item.get("attempts", 0))
        counts["total"] = sum(counts[status] for status in ALL_STATUSES)
        counts["attempts"] = attempts
        return counts

    def assign_sequences(self) -> int:
        """给箱内事件补上与发生时间一致的顺序号，返回新编号的条数。

        顺序号仅用于人工核对与运维展示，不参与状态机；重复执行是幂等的。
        """

        items = sorted(
            self.events.all(),
            key=order_key,
        )
        assigned = 0
        for index, item in enumerate(items, start=1):
            if int(item.get("sequence", 0)) == index:
                continue

            def mutate(document: dict[str, Any], number: int = index) -> dict[str, Any]:
                return merge_documents(document, [("sequence", number)])

            self.events.update(str(item["event_id"]), mutate)
            assigned += 1
        return assigned

    # ------------------------------------------------------------------ 内部

    def _find_source(self, source: str, source_id: str) -> dict[str, Any] | None:
        matches = self.events.find(
            lambda item: item.get("source") == source and item.get("source_id") == source_id
        )
        return matches[0] if matches else None

    def _is_due(self, item: dict[str, Any], now: datetime, stale_after_s: int) -> bool:
        status = str(item.get("status"))
        if status == OutboxStatus.QUEUED:
            attempt_text = item.get("next_attempt_at")
            if not attempt_text:
                return True
            try:
                return parse_moment(str(attempt_text)) <= now
            except Exception:  # noqa: BLE001 - 损坏的时间文本不应卡住队列
                return True
        # sending：只有超过认领时长（中继疑似崩溃）才允许再次领取
        claimed_text = item.get("claimed_at")
        if not claimed_text:
            return True
        try:
            age = (now - parse_moment(str(claimed_text))).total_seconds()
        except Exception:  # noqa: BLE001
            return True
        return age >= stale_after_s
