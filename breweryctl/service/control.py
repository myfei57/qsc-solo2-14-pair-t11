"""温控、压力与阀门操作服务。"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..core.validators import require_choice, require_number, require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.co2 import CO2Controller, VALVE_PURPOSES
from ..domain.models import ValveState
from ..domain.temp import TemperatureController


class ControlService:
    """把操作员对温控与压力的请求落到领域组件并写审计。"""

    def __init__(
        self,
        temp: TemperatureController,
        co2: CO2Controller,
        alarms: AlarmCenter,
        audit: AuditLog,
    ) -> None:
        self.temp = temp
        self.co2 = co2
        self.alarms = alarms
        self.audit = audit

    def set_temperature(
        self,
        batch_id: str,
        target_c: float,
        actor: str,
        cooling: bool = False,
    ) -> dict[str, Any]:
        """设置批次温控目标。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.temp.set_setpoint(batch_id, target_c, cooling=cooling)
        self.audit.record(
            str(document.get("brewery_id", "unknown")),
            batch_id,
            clean_actor,
            "control.temperature_set",
            {"target_c": document.get("target_c"), "cooling": document.get("cooling")},
        )
        return document

    def pitch_readiness(self, batch_id: str) -> dict[str, Any]:
        """返回接种温度就绪报告。"""

        document = self.temp.get_setpoint(batch_id)
        report = self.temp.is_pitch_ready(batch_id)
        report["brewery_id"] = document.get("brewery_id")
        return report

    def cooling_progress(self, batch_id: str, current_c: float) -> dict[str, Any]:
        """给出当前温度与目标的对比。"""

        current = require_number(current_c, field="current_c", minimum=-40.0, maximum=150.0)
        document = self.temp.get_setpoint(batch_id)
        target = float(document.get("target_c", 0.0))
        return {
            "batch_id": batch_id,
            "target_c": target,
            "current_c": current,
            "delta_c": round(current - target, 3),
            "cooling": bool(document.get("cooling")),
        }

    def set_pressure(self, tank_id: str, pressure_bar: float, actor: str) -> dict[str, Any]:
        """更新发酵罐压力，必要时触发闩锁。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.co2.set_pressure(tank_id, pressure_bar)
        self.audit.record(
            str(document.get("brewery_id", f"tank:{tank_id}")),
            None,
            clean_actor,
            "control.pressure_set",
            {"tank_id": tank_id, "pressure_bar": document.get("pressure_bar"), "state": document.get("state")},
        )
        return document

    def relieve_pressure(self, tank_id: str, target_bar: float, actor: str) -> dict[str, Any]:
        """排气到目标压力。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        document = self.co2.relieve(tank_id, target_bar)
        self.audit.record(
            str(document.get("brewery_id", f"tank:{tank_id}")),
            None,
            clean_actor,
            "control.pressure_relieved",
            {"tank_id": tank_id, "target_bar": target_bar},
        )
        return document

    def reset_latch(self, tank_id: str, operator: str) -> dict[str, Any]:
        """复位压力闩锁。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        document = self.co2.reset_latch(tank_id, clean_operator)
        self.audit.record(
            str(document.get("brewery_id", f"tank:{tank_id}")),
            None,
            clean_operator,
            "control.latch_reset",
            {"tank_id": tank_id},
        )
        return document

    def operate_valve(
        self,
        tank_id: str,
        purpose: str,
        state: str,
        operator: str,
    ) -> dict[str, Any]:
        """操作发酵罐阀门。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_purpose = require_choice(purpose, field="purpose", choices=VALVE_PURPOSES)
        clean_state = require_choice(state, field="state", choices=[item.value for item in ValveState])
        valve = self.co2.operate_valve(tank_id, clean_purpose, clean_state, clean_operator)
        self.audit.record(
            str(valve.get("brewery_id", f"tank:{tank_id}")),
            None,
            clean_operator,
            "control.valve_operated",
            {"tank_id": tank_id, "purpose": clean_purpose, "state": clean_state},
        )
        return valve

    def tank_snapshot(self, tank_id: str) -> dict[str, Any]:
        """返回罐压与阀门状态。"""

        snapshot = self.co2.snapshot(tank_id)
        if not snapshot.get("valves"):
            raise ValidationError("发酵罐尚未登记阀门", tank_id=tank_id)
        return snapshot

    def summary(self) -> dict[str, Any]:
        """汇总控制资源。"""

        return {
            "temperature": self.temp.summary(),
            "pressure": self.co2.summary(),
            "active_alarms": self.alarms.active_count(),
            "latched_tanks": self.co2.latched_tanks(),
        }
