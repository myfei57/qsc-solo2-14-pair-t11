"""煮沸锅与回旋沉淀控制。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, elapsed_minutes, format_moment
from ..core.errors import ConflictError, NotFoundError, SequenceError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import BoilRun

BOIL_RUNS = "boil_runs"


class BoilKettle:
    """执行「点火 → 沸腾计时 → 回旋沉淀」流程。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.runs = store.collection(BOIL_RUNS)

    def start(self, batch_id: str, minutes_target: float) -> dict[str, Any]:
        """建立煮沸运行。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        minutes = require_number(minutes_target, field="minutes_target", minimum=15.0, maximum=240.0)
        existing = self.runs.get(clean_batch)
        if existing and not existing.get("completed_at"):
            raise ConflictError("该批次已有进行中的煮沸运行", batch_id=clean_batch)
        now = format_moment(self.clock.now())
        run = BoilRun(
            id=new_id("boil"),
            batch_id=clean_batch,
            minutes_target=minutes,
            stage="idle",
            updated_at=now,
        )
        return self.runs.put(clean_batch, run.to_doc())

    def ignite(self, batch_id: str) -> dict[str, Any]:
        """开启加热。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != "idle":
                raise SequenceError("当前煮沸状态不允许点火", batch_id=batch_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("stage", "heating"), ("heat_on_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"boil:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def mark_boiling(self, batch_id: str) -> dict[str, Any]:
        """确认麦汁已经沸腾并开始计时。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != "heating":
                raise SequenceError("加热尚未开始", batch_id=batch_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("stage", "boiling"), ("boiling_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"boil:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def elapsed(self, batch_id: str) -> float:
        """返回从沸腾开始经过的分钟数。"""

        document = self.get(batch_id)
        started = document.get("boiling_at")
        if not started:
            return 0.0
        return elapsed_minutes(str(started), format_moment(self.clock.now()))

    def mark_whirlpool(self, batch_id: str) -> dict[str, Any]:
        """煮沸时间达标后进入回旋沉淀。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != "boiling":
                raise SequenceError("煮沸尚未开始", batch_id=batch_id, stage=document.get("stage"))
            if not document.get("boiling_at"):
                raise SequenceError("缺少沸腾开始时间", batch_id=batch_id)
            required = float(document.get("minutes_target", 0.0)) * 0.9
            actual = self.elapsed(batch_id)
            if actual < required:
                raise ConflictError(
                    "煮沸时间不足，禁止回旋沉淀",
                    batch_id=batch_id,
                    required_minutes=round(required, 2),
                    actual_minutes=round(actual, 2),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("stage", "whirlpool"), ("whirlpool_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"boil:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def complete(self, batch_id: str) -> dict[str, Any]:
        """结束煮沸并沉淀。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != "whirlpool":
                raise SequenceError("回旋沉淀尚未开始", batch_id=batch_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("stage", "complete"), ("completed_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"boil:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def get(self, batch_id: str) -> dict[str, Any]:
        """读取煮沸运行。"""

        document = self.runs.get(batch_id)
        if document is None:
            raise NotFoundError("煮沸运行不存在", batch_id=batch_id)
        return document

    def summary(self) -> dict[str, Any]:
        """汇总煮沸锅状态。"""

        items = self.runs.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        return {"total": len(items), "by_stage": counts}
