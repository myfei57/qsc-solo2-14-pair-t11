"""糖化锅状态机。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, NotFoundError, SequenceError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import MashRun, MashStage

MASH_RUNS = "mash_runs"

ACTIVE_STAGES = {
    MashStage.AWAITING_WATER.value,
    MashStage.WATER_CONFIRMED.value,
    MashStage.CHARGED.value,
    MashStage.HEATING.value,
    MashStage.RESTING.value,
}


class MashController:
    """执行「确认水温 → 投料 → 升温 → 保温 → 过滤」顺序。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.runs = store.collection(MASH_RUNS)

    def start(self, batch_id: str, water_target_c: float) -> dict[str, Any]:
        """为批次建立糖化运行，要求没有未结束的同类运行。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        target = require_number(water_target_c, field="water_target_c", minimum=5.0, maximum=95.0)
        existing = self.runs.get(clean_batch)
        if existing and existing.get("stage") in ACTIVE_STAGES:
            raise ConflictError("该批次已有进行中的糖化运行", batch_id=clean_batch, stage=existing.get("stage"))
        now = format_moment(self.clock.now())
        run = MashRun(
            id=new_id("mash"),
            batch_id=clean_batch,
            stage=MashStage.AWAITING_WATER.value,
            water_target_c=target,
            updated_at=now,
        )
        return self.runs.put(clean_batch, run.to_doc())

    def confirm_water(self, batch_id: str, temp_c: float, probe_id: str) -> dict[str, Any]:
        """先落盘水温确认结果，之后才允许投料。"""

        clean_probe = require_text(probe_id, field="probe_id", max_length=64)
        temperature = require_number(temp_c, field="temp_c", minimum=-5.0, maximum=110.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            stage = document.get("stage")
            if stage != MashStage.AWAITING_WATER.value:
                raise SequenceError(
                    "当前糖化状态不允许确认水温",
                    batch_id=batch_id,
                    stage=stage,
                )
            target = float(document.get("water_target_c", 0.0))
            deviation = abs(temperature - target)
            if deviation > self.settings.temp_tolerance_c:
                raise InterlockError(
                    "水温偏离目标，禁止投料",
                    batch_id=batch_id,
                    target_c=target,
                    actual_c=temperature,
                    tolerance_c=self.settings.temp_tolerance_c,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", MashStage.WATER_CONFIRMED.value),
                    ("water_temp_c", temperature),
                    ("water_probe_id", clean_probe),
                    ("water_confirmed_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def charge(self, batch_id: str, grain_kg: float) -> dict[str, Any]:
        """投料；必须读到已经落盘的水温确认。"""

        grain = require_number(grain_kg, field="grain_kg", minimum=1.0, maximum=10_000.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != MashStage.WATER_CONFIRMED.value:
                raise SequenceError(
                    "水温尚未确认落盘，禁止投料",
                    batch_id=batch_id,
                    stage=document.get("stage"),
                    water_confirmed_at=document.get("water_confirmed_at"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", MashStage.CHARGED.value),
                    ("grain_kg", grain),
                    ("charged_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def heat(self, batch_id: str, setpoint_c: float) -> dict[str, Any]:
        """开始升温。"""

        setpoint = require_number(setpoint_c, field="setpoint_c", minimum=5.0, maximum=100.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != MashStage.CHARGED.value:
                raise SequenceError(
                    "投料尚未完成，禁止升温",
                    batch_id=batch_id,
                    stage=document.get("stage"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", MashStage.HEATING.value),
                    ("setpoint_c", setpoint),
                    ("heating_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def mark_resting(self, batch_id: str, actual_temp_c: float) -> dict[str, Any]:
        """升温到位后进入保温。"""

        actual = require_number(actual_temp_c, field="actual_temp_c", minimum=-5.0, maximum=110.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != MashStage.HEATING.value:
                raise SequenceError("当前状态不是升温", batch_id=batch_id, stage=document.get("stage"))
            setpoint = float(document.get("setpoint_c", 0.0))
            if actual < setpoint - self.settings.temp_tolerance_c:
                raise InterlockError(
                    "温度尚未达到保温设定值",
                    batch_id=batch_id,
                    setpoint_c=setpoint,
                    actual_c=actual,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", MashStage.RESTING.value),
                    ("resting_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def mark_filtered(self, batch_id: str) -> dict[str, Any]:
        """保温结束后进入过滤。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != MashStage.RESTING.value:
                raise SequenceError("保温未完成，禁止过滤", batch_id=batch_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", MashStage.FILTERED.value),
                    ("filtered_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def fail(self, batch_id: str, reason: str) -> dict[str, Any]:
        """把糖化运行标记为失败并记录原因。"""

        clean_reason = require_text(reason, field="reason", max_length=200)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("stage", MashStage.FAILED.value),
                    ("failure_reason", clean_reason),
                    ("updated_at", format_moment(self.clock.now())),
                ],
            )

        with self.store.locks.guard(f"mash:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def get(self, batch_id: str) -> dict[str, Any]:
        """读取糖化运行。"""

        document = self.runs.get(batch_id)
        if document is None:
            raise NotFoundError("糖化运行不存在", batch_id=batch_id)
        return document

    def active(self) -> list[dict[str, Any]]:
        """返回所有未结束的糖化运行。"""

        return [
            item
            for item in self.runs.all()
            if item.get("stage") in ACTIVE_STAGES
        ]

    def summary(self) -> dict[str, Any]:
        """按阶段统计糖化运行。"""

        counts: dict[str, int] = {}
        for item in self.runs.all():
            stage = str(item.get("stage"))
            counts[stage] = counts.get(stage, 0) + 1
        return {"total": self.runs.count(), "by_stage": counts}

    def recover(self) -> list[dict[str, Any]]:
        """启动时修复缺失落盘标记的中间态运行。"""

        repaired: list[dict[str, Any]] = []
        for run in self.runs.all():
            stage = run.get("stage")
            if stage == MashStage.HEATING.value and not run.get("heating_at"):
                repaired.append(self.fail(str(run.get("batch_id")), "升温标记缺失，重启后无法续跑"))
            elif stage == MashStage.CHARGED.value and not run.get("charged_at"):
                repaired.append(self.fail(str(run.get("batch_id")), "投料标记缺失，重启后无法续跑"))
        return repaired
