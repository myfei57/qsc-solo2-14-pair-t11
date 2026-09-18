"""温度探头、标定基线与温控设定点。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import InterlockError, NotFoundError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import ReadingQuality, TempProbe, TempReading, TempSetpoint

PROBES = "temp_probes"
READINGS = "temp_readings"
SETPOINTS = "temp_setpoints"

SUSPECT_DEVIATION_C = 1.5
REJECT_DEVIATION_C = 4.0
BAD_STREAK_LIMIT = 3
BASELINE_LAG_C = 0.75


class TemperatureController:
    """维护探头基线、降温和接种温度判断。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.probes = store.collection(PROBES)
        self.readings = store.collection(READINGS)
        self.setpoints = store.collection(SETPOINTS)

    def register_probe(self, brewery_id: str, location: str, baseline_c: float) -> dict[str, Any]:
        """登记温度探头及其标定基线。"""

        baseline = require_number(baseline_c, field="baseline_c", minimum=-10.0, maximum=120.0)
        now = format_moment(self.clock.now())
        probe = TempProbe(
            id=new_id("probe"),
            brewery_id=require_text(brewery_id, field="brewery_id", max_length=64),
            location=require_text(location, field="location", max_length=80),
            baseline_c=baseline,
            calibrated_at=now,
        )
        return self.probes.put(probe.id, probe.to_doc())

    def calibrate(self, probe_id: str, baseline_c: float) -> dict[str, Any]:
        """重新标定探头基线，并清除异常标记。"""

        baseline = require_number(baseline_c, field="baseline_c", minimum=-10.0, maximum=120.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [
                    ("baseline_c", baseline),
                    ("samples", 0),
                    ("suspect", False),
                    ("calibrated_at", format_moment(self.clock.now())),
                ],
            )

        with self.store.locks.guard(f"probe:{probe_id}"):
            return self.probes.update(probe_id, mutate)

    def record_reading(
        self,
        probe_id: str,
        value_c: float,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """接收一次采样，按基线判定质量并累计异常次数。"""

        value = require_number(value_c, field="value_c", minimum=-40.0, maximum=150.0)
        reading_id = new_id("reading")
        document = self.probes.require(probe_id, label="温度探头")
        baseline = float(document.get("baseline_c", 0.0))
        deviation = value - baseline
        magnitude = abs(deviation)
        if magnitude > REJECT_DEVIATION_C:
            quality = ReadingQuality.REJECTED.value
        elif magnitude > SUSPECT_DEVIATION_C:
            quality = ReadingQuality.SUSPECT.value
        else:
            quality = ReadingQuality.GOOD.value
        reading = TempReading(
            id=reading_id,
            probe_id=probe_id,
            batch_id=batch_id,
            value_c=value,
            deviation_c=round(deviation, 3),
            quality=quality,
            taken_at=format_moment(self.clock.now()),
        )
        self.readings.put(reading_id, reading.to_doc())

        def mutate(probe: dict[str, Any]) -> dict[str, Any]:
            streak = int(probe.get("bad_streak", 0))
            if quality == ReadingQuality.GOOD.value:
                streak = 0
            else:
                streak += 1
            suspect = bool(probe.get("suspect", False)) or streak >= BAD_STREAK_LIMIT
            return merge_documents(
                probe,
                [
                    ("last_value_c", value),
                    ("last_seen_at", reading.taken_at),
                    ("samples", int(probe.get("samples", 0)) + 1),
                    ("bad_streak", streak),
                    ("suspect", suspect),
                ],
            )

        with self.store.locks.guard(f"probe:{probe_id}"):
            self.probes.update(probe_id, mutate)
        return reading.to_doc()

    def readings_for(self, probe_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """返回某探头的最近采样。"""

        clean_limit = require_int(limit, field="limit", minimum=1, maximum=500)
        items = [
            item for item in self.readings.all() if item.get("probe_id") == probe_id
        ]
        items.sort(key=lambda item: str(item.get("taken_at", "")))
        return items[-clean_limit:]

    def latest(self, probe_id: str) -> dict[str, Any] | None:
        """返回探头最近一次采样。"""

        items = self.readings_for(probe_id, limit=1)
        return items[0] if items else None

    def set_setpoint(self, batch_id: str, target_c: float, cooling: bool = False) -> dict[str, Any]:
        """设置批次温控目标，读写过程持有批次锁。"""

        target = require_number(target_c, field="target_c", minimum=-5.0, maximum=120.0)
        clean_batch = require_text(batch_id, field="batch_id", max_length=64)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("target_c", target),
                    ("cooling", bool(cooling)),
                    ("reached_at", None),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"setpoint:{clean_batch}"):
            existing = self.setpoints.get(clean_batch)
            if existing is None:
                setpoint = TempSetpoint(batch_id=clean_batch, target_c=target, cooling=bool(cooling), updated_at=format_moment(self.clock.now()))
                document = self.setpoints.put(clean_batch, setpoint.to_doc())
            else:
                document = self.setpoints.update(clean_batch, mutate)
        self.store.append_event(
            "temp.setpoint",
            {"batch_id": clean_batch, "target_c": target, "cooling": bool(cooling)},
        )
        return document

    def get_setpoint(self, batch_id: str) -> dict[str, Any]:
        """读取批次温控目标。"""

        document = self.setpoints.get(batch_id)
        if document is None:
            raise NotFoundError("温控目标不存在", batch_id=batch_id)
        return document

    def start_cooling(self, batch_id: str, target_c: float) -> dict[str, Any]:
        """开启降温到指定目标。"""

        return self.set_setpoint(batch_id, target_c, cooling=True)

    def mark_reached(self, batch_id: str, value_c: float) -> dict[str, Any]:
        """记录温控目标已经达到。"""

        value = require_number(value_c, field="value_c", minimum=-40.0, maximum=150.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            target = float(document.get("target_c", 0.0))
            if value > target + self.settings.temp_tolerance_c:
                raise InterlockError(
                    "温度尚未达到设定值",
                    batch_id=batch_id,
                    target_c=target,
                    actual_c=value,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("cooling", False), ("reached_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"setpoint:{batch_id}"):
            return self.setpoints.update(batch_id, mutate)

    def is_pitch_ready(self, batch_id: str) -> dict[str, Any]:
        """判断是否满足接种温度要求。"""

        document = self.get_setpoint(batch_id)
        target = float(document.get("target_c", 0.0))
        reached = bool(document.get("reached_at"))
        comfortable = target <= self.settings.pitch_temp_max_c
        return {
            "batch_id": batch_id,
            "target_c": target,
            "reached": reached,
            "limit_c": self.settings.pitch_temp_max_c,
            "ready": reached and comfortable,
        }

    def require_pitch_temperature(self, batch_id: str) -> dict[str, Any]:
        """接种前必须完成降温；未满足时抛联锁错误。"""

        report = self.is_pitch_ready(batch_id)
        if not report["ready"]:
            raise InterlockError(
                "尚未降温到接种温度",
                batch_id=batch_id,
                target_c=report["target_c"],
                limit_c=report["limit_c"],
                reached=report["reached"],
            )
        return report

    def detect_baseline_lag(self, probe_id: str, window: int = 5) -> dict[str, Any]:
        """比较基线与近期采样均值，识别标定滞后。"""

        probe = self.probes.require(probe_id, label="温度探头")
        samples = [
            item
            for item in self.readings_for(probe_id, limit=100)
            if item.get("quality")
            in (ReadingQuality.GOOD.value, ReadingQuality.SUSPECT.value)
        ][-max(window, 1) :]
        if not samples:
            return {
                "probe_id": probe_id,
                "baseline_c": float(probe.get("baseline_c", 0.0)),
                "samples": 0,
                "drift_c": 0.0,
                "lagging": False,
            }
        baseline = float(probe.get("baseline_c", 0.0))
        mean = sum(float(item.get("value_c", 0.0)) for item in samples) / len(samples)
        drift = mean - baseline
        lagging = abs(drift) > BASELINE_LAG_C
        if lagging:
            with self.store.locks.guard(f"probe:{probe_id}"):
                self.probes.update(
                    probe_id,
                    lambda document: merge_documents(document, [("suspect", True)]),
                )
        return {
            "probe_id": probe_id,
            "baseline_c": baseline,
            "samples": len(samples),
            "mean_c": round(mean, 3),
            "drift_c": round(drift, 3),
            "lagging": lagging,
        }

    def summary(self) -> dict[str, Any]:
        """汇总探头与温控情况。"""

        probes = self.probes.all()
        return {
            "probes": len(probes),
            "suspect_probes": len([item for item in probes if item.get("suspect")]),
            "readings": self.readings.count(),
            "setpoints": self.setpoints.count(),
        }

    def require_probe(self, probe_id: str) -> dict[str, Any]:
        """读取探头并在标记异常时拒绝使用。"""

        document = self.probes.require(probe_id, label="温度探头")
        if document.get("suspect") and document.get("samples", 0) >= BAD_STREAK_LIMIT:
            raise ValidationError(
                "探头已被标记异常，需要重新标定",
                probe_id=probe_id,
                baseline_c=document.get("baseline_c"),
            )
        return document
