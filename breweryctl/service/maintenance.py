"""CIP 清洗与发酵罐维护服务。"""

from __future__ import annotations

from typing import Any

from ..core.errors import NotFoundError, ValidationError
from ..core.validators import require_number, require_text
from ..domain.audit import AuditLog
from ..domain.cip import CIPService, STAGE_SEQUENCE
from ..domain.ferment import FermentTankService


class MaintenanceService:
    """为发酵罐安排清洗、推进步骤并确认可用。"""

    def __init__(
        self,
        cip: CIPService,
        tanks: FermentTankService,
        audit: AuditLog,
    ) -> None:
        self.cip = cip
        self.tanks = tanks
        self.audit = audit

    def start_clean(self, tank_id: str, operator: str) -> dict[str, Any]:
        """为发酵罐启动一次清洗。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        tank = self.tanks.get(tank_id)
        circuit = self._circuit_for(str(tank["id"]), str(tank["brewery_id"]))
        cycle = self.cip.start_cycle(str(circuit["id"]), str(tank["id"]), clean_operator)
        self.audit.record(
            str(tank["brewery_id"]),
            tank.get("batch_id"),
            clean_operator,
            "maintenance.clean_started",
            {"tank_id": tank_id, "cycle_id": cycle["id"], "circuit_id": circuit["id"]},
        )
        return cycle

    def advance_clean(self, cycle_id: str, operator: str) -> dict[str, Any]:
        """推进一个清洗步骤。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        cycle = self.cip.advance_cycle(cycle_id)
        self.audit.record(
            self._brewery_of_cycle(cycle),
            None,
            clean_operator,
            "maintenance.clean_step",
            {"cycle_id": cycle_id, "stage": cycle.get("stage")},
        )
        return cycle

    def finish_clean(self, cycle_id: str, operator: str) -> dict[str, Any]:
        """完成清洗、签发凭证并把发酵罐置为可用。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        cycle = self.cip.complete_cycle(cycle_id)
        tank = self.tanks.sanitize(str(cycle["tank_id"]), clean_operator)
        certificate = self.cip.certificate_for(str(tank["id"]))
        self.audit.record(
            str(tank["brewery_id"]),
            tank.get("batch_id"),
            clean_operator,
            "maintenance.clean_finished",
            {
                "tank_id": tank["id"],
                "cycle_id": cycle_id,
                "certificate_id": certificate["id"] if certificate else None,
            },
        )
        return {"cycle": cycle, "tank": tank, "certificate": certificate}

    def certificate_status(self, tank_id: str) -> dict[str, Any]:
        """返回发酵罐清洗凭证状态。"""

        tank = self.tanks.get(tank_id)
        certificate = self.cip.certificate_for(tank_id)
        if certificate is None:
            return {
                "tank_id": tank_id,
                "valid": False,
                "stage": tank.get("stage"),
                "required_stages": list(STAGE_SEQUENCE),
            }
        return {
            "tank_id": tank_id,
            "valid": True,
            "stage": tank.get("stage"),
            "certificate": certificate,
            "verified_stages": certificate.get("verified_stages"),
        }

    def empty_tank(self, tank_id: str, operator: str) -> dict[str, Any]:
        """清空成熟批次后的发酵罐，并写审计。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        tank = self.tanks.empty(tank_id, clean_operator)
        self.audit.record(
            str(tank["brewery_id"]),
            tank.get("batch_id"),
            clean_operator,
            "maintenance.tank_emptied",
            {"tank_id": tank_id, "code": tank.get("code")},
        )
        return tank

    def flush_circuit(self, circuit_id: str, flow_m3h: float, operator: str) -> dict[str, Any]:
        """按回路额定流量执行一次冲洗水量核算。"""

        clean_flow = require_number(flow_m3h, field="flow_m3h", minimum=0.5, maximum=200.0)
        clean_operator = require_text(operator, field="operator", max_length=60)
        circuit = self.cip.circuits.require(circuit_id, label="CIP 回路")
        rated = float(circuit.get("flow_m3h", 0.0))
        if clean_flow > rated * 1.2:
            raise ValidationError(
                "冲洗流量超过回路额定值",
                circuit_id=circuit_id,
                rated_m3h=rated,
                requested_m3h=clean_flow,
            )
        water_m3 = clean_flow / 60.0 * 15.0
        self.audit.record(
            str(circuit.get("brewery_id")),
            None,
            clean_operator,
            "maintenance.circuit_flushed",
            {"circuit_id": circuit_id, "flow_m3h": clean_flow, "water_m3": round(water_m3, 3)},
        )
        return {
            "circuit_id": circuit_id,
            "flow_m3h": clean_flow,
            "minutes": 15.0,
            "water_m3": round(water_m3, 3),
        }

    def summary(self) -> dict[str, Any]:
        """汇总清洗与凭证状态。"""

        return self.cip.summary()

    def _circuit_for(self, tank_id: str, brewery_id: str) -> dict[str, Any]:
        for circuit in self.cip.circuits_for(brewery_id):
            if tank_id in circuit.get("tanks", []):
                return circuit
        raise NotFoundError("发酵罐没有可用的 CIP 回路", tank_id=tank_id, brewery_id=brewery_id)

    def _brewery_of_cycle(self, cycle: dict[str, Any]) -> str:
        circuit = self.cip.circuits.get(str(cycle.get("circuit_id")))
        if circuit:
            return str(circuit.get("brewery_id"))
        tank = self.tanks.get(str(cycle.get("tank_id")))
        return str(tank.get("brewery_id"))
