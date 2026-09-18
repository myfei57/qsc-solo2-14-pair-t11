"""温度遥测与探头健康检查。"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.validators import require_int, require_number, require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.models import ReadingQuality
from ..domain.temp import TemperatureController


class TelemetryService:
    """接收采样、识别异常探头并输出批次温度趋势。"""

    def __init__(
        self,
        temp: TemperatureController,
        alarms: AlarmCenter,
        audit: AuditLog,
    ) -> None:
        self.temp = temp
        self.alarms = alarms
        self.audit = audit

    def ingest(
        self,
        probe_id: str,
        value_c: float,
        batch_id: str | None,
        actor: str,
    ) -> dict[str, Any]:
        """接收一次温度采样并按质量触发告警。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        reading = self.temp.record_reading(probe_id, value_c, batch_id=batch_id)
        probe = self.temp.probes.require(probe_id, label="温度探头")
        quality = str(reading.get("quality"))
        if quality == ReadingQuality.REJECTED.value:
            self.alarms.raise_alarm(
                brewery_id=str(probe.get("brewery_id")),
                source=f"probe:{probe_id}",
                severity="critical",
                code="probe_reading_rejected",
                message=f"探头 {probe_id} 采样严重偏离基线",
                context={
                    "probe_id": probe_id,
                    "value_c": reading.get("value_c"),
                    "deviation_c": reading.get("deviation_c"),
                },
            )
        elif quality == ReadingQuality.SUSPECT.value:
            self.alarms.raise_alarm(
                brewery_id=str(probe.get("brewery_id")),
                source=f"probe:{probe_id}",
                severity="warning",
                code="probe_reading_suspect",
                message=f"探头 {probe_id} 采样偏离基线",
                context={
                    "probe_id": probe_id,
                    "value_c": reading.get("value_c"),
                    "deviation_c": reading.get("deviation_c"),
                },
            )
        if probe.get("suspect"):
            self.alarms.raise_alarm(
                brewery_id=str(probe.get("brewery_id")),
                source=f"probe:{probe_id}",
                severity="warning",
                code="probe_suspect",
                message=f"探头 {probe_id} 连续异常，需要重新标定",
                latching=True,
                context={"probe_id": probe_id, "baseline_c": probe.get("baseline_c")},
            )
        self.audit.record(
            str(probe.get("brewery_id")),
            batch_id,
            clean_actor,
            "telemetry.reading",
            {
                "probe_id": probe_id,
                "value_c": reading.get("value_c"),
                "quality": quality,
            },
        )
        return reading

    def calibrate(self, probe_id: str, baseline_c: float, actor: str) -> dict[str, Any]:
        """重新标定探头。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        probe = self.temp.calibrate(probe_id, baseline_c)
        self.audit.record(
            str(probe.get("brewery_id")),
            None,
            clean_actor,
            "telemetry.calibrated",
            {"probe_id": probe_id, "baseline_c": probe.get("baseline_c")},
        )
        return probe

    def probe_report(self, probe_id: str, window: int = 5) -> dict[str, Any]:
        """输出探头基线滞后报告。"""

        clean_window = require_int(window, field="window", minimum=1, maximum=100)
        report = self.temp.detect_baseline_lag(probe_id, window=clean_window)
        if report.get("lagging"):
            probe = self.temp.probes.require(probe_id, label="温度探头")
            self.alarms.raise_alarm(
                brewery_id=str(probe.get("brewery_id")),
                source=f"probe:{probe_id}",
                severity="warning",
                code="probe_baseline_lag",
                message=f"探头 {probe_id} 基线可能滞后",
                latching=True,
                context=report,
            )
        return report

    def batch_trend(self, batch_id: str, limit: int = 50) -> dict[str, Any]:
        """返回批次的温度采样趋势。"""

        clean_limit = require_int(limit, field="limit", minimum=1, maximum=500)
        readings = [
            item
            for item in self.temp.readings.all()
            if item.get("batch_id") == batch_id
        ]
        readings.sort(key=lambda item: str(item.get("taken_at", "")))
        readings = readings[-clean_limit:]
        values = [float(item.get("value_c", 0.0)) for item in readings]
        return {
            "batch_id": batch_id,
            "samples": len(readings),
            "min_c": round(min(values), 3) if values else None,
            "max_c": round(max(values), 3) if values else None,
            "mean_c": round(sum(values) / len(values), 3) if values else None,
            "readings": readings,
        }

    def probe_health(self, probe_id: str) -> dict[str, Any]:
        """综合最近采样判断探头是否可用。"""

        probe = self.temp.probes.require(probe_id, label="温度探头")
        latest = self.temp.latest(probe_id)
        if latest is None:
            return {
                "probe_id": probe_id,
                "healthy": False,
                "reason": "尚无采样",
                "baseline_c": probe.get("baseline_c"),
            }
        deviation = abs(float(latest.get("deviation_c", 0.0)))
        healthy = deviation <= 1.5 and not probe.get("suspect")
        return {
            "probe_id": probe_id,
            "healthy": healthy,
            "baseline_c": probe.get("baseline_c"),
            "last_value_c": latest.get("value_c"),
            "deviation_c": latest.get("deviation_c"),
            "samples": probe.get("samples"),
            "suspect": bool(probe.get("suspect")),
        }

    def require_recent(self, probe_id: str, batch_id: str) -> dict[str, Any]:
        """要求探头在当前批次有采样，否则拒绝继续。"""

        self.temp.require_probe(probe_id)
        readings = self.batch_trend(batch_id, limit=1)["readings"]
        if not readings:
            raise ValidationError("该批次尚无温度采样", batch_id=batch_id, probe_id=probe_id)
        return readings[-1]

    def ingest_many(self, samples: list[dict[str, Any]], actor: str) -> dict[str, Any]:
        """批量接收采样，供控制台一次提交多点数据。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        if not samples:
            raise ValidationError("采样列表不能为空")
        accepted: list[dict[str, Any]] = []
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValidationError("采样必须是对象", sample=sample)
            probe_id = require_text(sample.get("probe_id"), field="probe_id", max_length=64)
            value = require_number(sample.get("value_c"), field="value_c", minimum=-40.0, maximum=150.0)
            batch_id = sample.get("batch_id")
            accepted.append(self.ingest(probe_id, value, batch_id if isinstance(batch_id, str) else None, clean_actor))
        return {"accepted": len(accepted), "readings": accepted}
