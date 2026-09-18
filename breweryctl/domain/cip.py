"""CIP 清洗回路与合格凭证。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable

from ..core.clock import Clock, format_moment, parse_moment
from ..core.config import Settings
from ..core.errors import MaintenanceRequiredError, NotFoundError, SequenceError, ValidationError
from ..core.ids import circuit_code, new_id, slugify
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import CipCertificate, CipCircuit, CipCycle, CipStage

CIRCUITS = "cip_circuits"
CYCLES = "cip_cycles"
CERTIFICATES = "cip_certificates"

STAGE_SEQUENCE = (
    CipStage.PRERINSE.value,
    CipStage.CAUSTIC.value,
    CipStage.INTERMEDIATE_RINSE.value,
    CipStage.ACID.value,
    CipStage.FINAL_RINSE.value,
    CipStage.COMPLETE.value,
)


class CIPService:
    """执行固定顺序的清洗流程，并签发有时效的合格凭证。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.circuits = store.collection(CIRCUITS)
        self.cycles = store.collection(CYCLES)
        self.certificates = store.collection(CERTIFICATES)

    def register_circuit(
        self,
        brewery_id: str,
        index: int,
        tank_ids: Iterable[str],
        flow_m3h: float,
    ) -> dict[str, Any]:
        """登记一条 CIP 回路。"""

        tanks = [require_text(item, field="tank_id", max_length=64) for item in tank_ids]
        if not tanks:
            raise ValidationError("CIP 回路至少需要关联一个发酵罐")
        code = circuit_code(index)
        if any(item.get("code") == code and item.get("brewery_id") == brewery_id for item in self.circuits.all()):
            raise ValidationError("CIP 回路编码已存在", code=code)
        now = format_moment(self.clock.now())
        circuit = CipCircuit(
            id=new_id("cipc"),
            code=code,
            brewery_id=require_text(brewery_id, field="brewery_id", max_length=64),
            tanks=tanks,
            flow_m3h=require_number(flow_m3h, field="flow_m3h", minimum=0.5, maximum=200.0),
            updated_at=now,
        )
        return self.circuits.put(circuit.id, circuit.to_doc())

    def start_cycle(self, circuit_id: str, tank_id: str, operator: str) -> dict[str, Any]:
        """在指定发酵罐上开始清洗。"""

        circuit = self.circuits.require(circuit_id, label="CIP 回路")
        clean_tank = require_text(tank_id, field="tank_id", max_length=64)
        if clean_tank not in circuit.get("tanks", []):
            raise ValidationError("该发酵罐不属于这条 CIP 回路", tank_id=clean_tank, circuit_id=circuit_id)
        active = [
            item
            for item in self.cycles.all()
            if item.get("tank_id") == clean_tank and not item.get("finished_at")
        ]
        if active:
            raise SequenceError("该发酵罐已有未完成的清洗", tank_id=clean_tank, cycle_id=active[0]["id"])
        now = format_moment(self.clock.now())
        cycle = CipCycle(
            id=new_id("cip"),
            circuit_id=circuit["id"],
            tank_id=clean_tank,
            stage=CipStage.PRERINSE.value,
            completed_stages=[CipStage.PRERINSE.value],
            started_at=now,
            operator=require_text(operator, field="operator", max_length=60),
            updated_at=now,
        )
        document = self.cycles.put(cycle.id, cycle.to_doc())
        self.store.append_event(
            "cip.started",
            {"cycle_id": cycle.id, "tank_id": clean_tank, "circuit_id": circuit["id"]},
        )
        return document

    def advance_cycle(self, cycle_id: str) -> dict[str, Any]:
        """推进到下一个清洗步骤；跳步会被拒绝。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            current = str(document.get("stage"))
            if current == CipStage.COMPLETE.value:
                raise SequenceError("清洗已经完成", cycle_id=cycle_id)
            index = STAGE_SEQUENCE.index(current)
            next_stage = STAGE_SEQUENCE[index + 1]
            completed = list(document.get("completed_stages", []))
            if next_stage not in completed:
                completed.append(next_stage)
            now = format_moment(self.clock.now())
            updated = merge_documents(
                document,
                [
                    ("stage", next_stage),
                    ("completed_stages", completed),
                    ("updated_at", now),
                ],
            )
            if next_stage == CipStage.COMPLETE.value:
                updated["finished_at"] = now
            return updated

        with self.store.locks.guard(f"cip:{cycle_id}"):
            document = self.cycles.update(cycle_id, mutate)
        if document.get("stage") == CipStage.COMPLETE.value:
            certificate = self._issue_certificate(document)
            document["certificate_id"] = certificate["id"]
            self.cycles.put(cycle_id, document)
            self.alarms.raise_alarm(
                brewery_id=self._brewery_of(document),
                source=f"cip:{document['tank_id']}",
                severity="info",
                code="cip_certificate_issued",
                message=f"发酵罐 {document['tank_id']} 清洗合格凭证已签发",
                latching=False,
                context={"cycle_id": cycle_id, "certificate_id": certificate["id"]},
            )
        return document

    def complete_cycle(self, cycle_id: str) -> dict[str, Any]:
        """连续推进直到清洗完成。"""

        document = self.cycles.require(cycle_id, label="清洗过程")
        while document.get("stage") != CipStage.COMPLETE.value:
            document = self.advance_cycle(cycle_id)
        return document

    def certificate_for(self, tank_id: str) -> dict[str, Any] | None:
        """返回发酵罐当前有效的清洗凭证。"""

        now = self.clock.now()
        valid: list[dict[str, Any]] = []
        for item in self.certificates.all():
            if item.get("tank_id") != tank_id:
                continue
            if parse_moment(str(item.get("expires_at"))) <= now:
                continue
            valid.append(item)
        if not valid:
            return None
        valid.sort(key=lambda item: str(item.get("issued_at", "")), reverse=True)
        return valid[0]

    def require_certificate(self, tank_id: str) -> dict[str, Any]:
        """要求清洗凭证有效，否则阻止转罐。"""

        certificate = self.certificate_for(tank_id)
        if certificate is None:
            raise MaintenanceRequiredError(
                "发酵罐缺少有效的清洗凭证",
                tank_id=tank_id,
            )
        return certificate

    def certificate(self, certificate_id: str) -> dict[str, Any]:
        """按标识读取凭证。"""

        document = self.certificates.get(certificate_id)
        if document is None:
            raise NotFoundError("清洗凭证不存在", certificate_id=certificate_id)
        return document

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        """按标识读取清洗过程。"""

        document = self.cycles.get(cycle_id)
        if document is None:
            raise NotFoundError("清洗过程不存在", cycle_id=cycle_id)
        return document

    def circuits_for(self, brewery_id: str) -> list[dict[str, Any]]:
        """返回工厂下的 CIP 回路。"""

        return [item for item in self.circuits.all() if item.get("brewery_id") == brewery_id]

    def expired_certificates(self) -> list[dict[str, Any]]:
        """返回已经过期的凭证。"""

        now = self.clock.now()
        return [
            item
            for item in self.certificates.all()
            if parse_moment(str(item.get("expires_at"))) <= now
        ]

    def summary(self) -> dict[str, Any]:
        """汇总清洗资源与进度。"""

        cycles = self.cycles.all()
        counts: dict[str, int] = {}
        for item in cycles:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "circuits": self.circuits.count(),
            "cycles": len(cycles),
            "by_stage": counts,
            "certificates": self.certificates.count(),
            "expired": len(self.expired_certificates()),
        }

    def _issue_certificate(self, cycle: dict[str, Any]) -> dict[str, Any]:
        completed = list(cycle.get("completed_stages", []))
        missing = [stage for stage in STAGE_SEQUENCE if stage not in completed]
        if missing:
            raise SequenceError("清洗步骤不完整，无法签发凭证", missing=missing)
        issued = self.clock.now()
        certificate = CipCertificate(
            id=new_id("cert"),
            tank_id=str(cycle.get("tank_id")),
            cycle_id=str(cycle.get("id")),
            issued_at=format_moment(issued),
            expires_at=format_moment(issued + timedelta(minutes=self.settings.cip_certificate_ttl_min)),
            verified_stages=completed,
        )
        return self.certificates.put(certificate.id, certificate.to_doc())

    def _brewery_of(self, cycle: dict[str, Any]) -> str:
        circuit_id = cycle.get("circuit_id")
        circuit = self.circuits.get(str(circuit_id))
        if circuit:
            return str(circuit.get("brewery_id"))
        return slugify(str(cycle.get("tank_id", "unknown-tank")))
