"""酒花分次添加的顺序与时间窗控制。"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import InterlockError, NotFoundError, SequenceError
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import HopAddition, HopStatus

HOP_ADDITIONS = "hop_additions"


class HopSchedule:
    """按配方顺序执行酒花投加。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.additions = store.collection(HOP_ADDITIONS)

    def plan(self, batch_id: str, schedule: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """把配方中的酒花计划复制成批次实际记录。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        created: list[dict[str, Any]] = []
        for raw in schedule:
            addition = HopAddition(
                batch_id=clean_batch,
                position=require_int(raw.get("position"), field="position", minimum=1, maximum=99),
                name=require_text(raw.get("name"), field="hop.name", max_length=60),
                amount_g=require_number(raw.get("amount_g"), field="hop.amount_g", minimum=1.0),
                window_start_min=require_number(raw.get("window_start_min"), field="hop.window_start_min", minimum=0.0),
                window_end_min=require_number(raw.get("window_end_min"), field="hop.window_end_min", minimum=0.0),
                status=HopStatus.PENDING.value,
            )
            key = self._key(clean_batch, addition.position)
            created.append(self.additions.put(key, addition.to_doc()))
        if not created:
            raise SequenceError("酒花计划为空", batch_id=clean_batch)
        return created

    def add(self, batch_id: str, position: int, minute: float, operator: str) -> dict[str, Any]:
        """按顺序投加酒花，时间必须落在配方窗口内。"""

        clean_position = require_int(position, field="position", minimum=1, maximum=99)
        clean_minute = require_number(minute, field="minute", minimum=0.0, maximum=600.0)
        clean_operator = require_text(operator, field="operator", max_length=60)
        key = self._key(batch_id, clean_position)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") == HopStatus.ADDED.value:
                raise SequenceError("该序次酒花已投加", batch_id=batch_id, position=clean_position)
            self._require_previous_added(batch_id, clean_position)
            start = float(document.get("window_start_min", 0.0))
            end = float(document.get("window_end_min", 0.0))
            slack = self.settings.hop_window_slack_min
            if clean_minute < start - slack or clean_minute > end + slack:
                raise InterlockError(
                    "当前时间不在酒花投放窗口内",
                    batch_id=batch_id,
                    position=clean_position,
                    minute=clean_minute,
                    window=[start, end],
                    slack_min=slack,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("status", HopStatus.ADDED.value),
                    ("added_at", now),
                    ("actual_minute", clean_minute),
                    ("operator", clean_operator),
                ],
            )

        with self.store.locks.guard(f"hop:{batch_id}:{clean_position}"):
            result = self.additions.update(key, mutate)
        self.store.append_event(
            "hop.added",
            {
                "batch_id": batch_id,
                "position": clean_position,
                "minute": clean_minute,
                "operator": clean_operator,
            },
        )
        return result

    def mark_missed(self, batch_id: str, current_minute: float) -> list[dict[str, Any]]:
        """把已经错过窗口且仍未投加的酒花标记为漏加。"""

        clean_minute = require_number(current_minute, field="current_minute", minimum=0.0, maximum=600.0)
        missed: list[dict[str, Any]] = []
        for item in self.list_for(batch_id):
            if item.get("status") != HopStatus.PENDING.value:
                continue
            if clean_minute > float(item.get("window_end_min", 0.0)):
                key = self._key(batch_id, int(item["position"]))
                updated = self.additions.update(
                    key,
                    lambda document: merge_documents(
                        document, [("status", HopStatus.MISSED.value)]
                    ),
                )
                missed.append(updated)
        return missed

    def list_for(self, batch_id: str) -> list[dict[str, Any]]:
        """返回批次的酒花记录，按序次排序。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        items = [
            item
            for item in self.additions.all()
            if self._batch_of(item) == clean_batch
        ]
        return sorted(items, key=lambda item: int(item.get("position", 0)))

    def pending(self, batch_id: str) -> list[dict[str, Any]]:
        """返回尚未投加的序次。"""

        return [item for item in self.list_for(batch_id) if item.get("status") == HopStatus.PENDING.value]

    def next_position(self, batch_id: str) -> int | None:
        """返回下一个应投加的序次。"""

        pending = self.pending(batch_id)
        if not pending:
            return None
        return int(pending[0]["position"])

    def summary(self, batch_id: str) -> dict[str, Any]:
        """汇总批次酒花投加情况。"""

        items = self.list_for(batch_id)
        pending = [item for item in items if item.get("status") == HopStatus.PENDING.value]
        return {
            "batch_id": batch_id,
            "total": len(items),
            "added": len([item for item in items if item.get("status") == HopStatus.ADDED.value]),
            "missed": len([item for item in items if item.get("status") == HopStatus.MISSED.value]),
            "pending": len(pending),
            "next_position": self.next_position(batch_id),
            "amount_g": round(sum(float(item.get("amount_g", 0.0)) for item in items), 2),
        }

    def _require_previous_added(self, batch_id: str, position: int) -> None:
        if position <= 1:
            return
        previous = self.additions.get(self._key(batch_id, position - 1))
        if previous is None or previous.get("status") != HopStatus.ADDED.value:
            raise SequenceError(
                "前序酒花尚未投加",
                batch_id=batch_id,
                position=position,
                required_previous=position - 1,
            )

    def _key(self, batch_id: str, position: int) -> str:
        return f"{require_text(batch_id, field='batch_id', max_length=64)}:{int(position):02d}"

    def _batch_of(self, document: dict[str, Any]) -> str:
        return str(document.get("batch_id") or "")

    def require(self, key: str) -> dict[str, Any]:
        """按内部键读取单条记录。"""

        document = self.additions.get(key)
        if document is None:
            raise NotFoundError("酒花记录不存在", key=key)
        return document
