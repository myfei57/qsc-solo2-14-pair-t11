"""发酵罐状态机：清洗、转罐、接种、发酵与成熟。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, NotFoundError, SequenceError
from ..core.ids import new_id, tank_code
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .cip import CIPService
from .co2 import CO2Controller
from .models import FermentStage, FermentTank

TANKS = "ferment_tanks"


class FermentTankService:
    """保证转罐前已清洗、接种前已降温。"""

    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        clock: Clock,
        cip: CIPService,
        co2: CO2Controller,
        alarms: AlarmCenter,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.cip = cip
        self.co2 = co2
        self.alarms = alarms
        self.tanks = store.collection(TANKS)

    def register_tank(self, brewery_id: str, index: int, capacity_l: float) -> dict[str, Any]:
        """登记发酵罐并建立压力联锁。"""

        capacity = require_number(capacity_l, field="capacity_l", minimum=10.0, maximum=100_000.0)
        code = tank_code(index)
        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        for item in self.tanks.all():
            if item.get("brewery_id") == clean_brewery and item.get("code") == code:
                raise ConflictError("发酵罐编号已存在", brewery_id=clean_brewery, code=code)
        now = format_moment(self.clock.now())
        tank = FermentTank(
            id=new_id("tank"),
            code=code,
            brewery_id=clean_brewery,
            capacity_l=capacity,
            updated_at=now,
        )
        document = self.tanks.put(tank.id, tank.to_doc())
        self.co2.ensure_tank(tank.id, clean_brewery)
        return document

    def list_tanks(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        """列出发酵罐。"""

        items = self.tanks.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        return sorted(items, key=lambda item: str(item.get("code", "")))

    def get(self, tank_id: str) -> dict[str, Any]:
        """读取发酵罐。"""

        document = self.tanks.get(tank_id)
        if document is None:
            raise NotFoundError("发酵罐不存在", tank_id=tank_id)
        return document

    def sanitize(self, tank_id: str, operator: str) -> dict[str, Any]:
        """清洗完成并拿到凭证后，把罐标记为可用。"""

        clean_operator = require_text(operator, field="operator", max_length=60)
        certificate = self.cip.require_certificate(tank_id)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") not in (FermentStage.IDLE.value, FermentStage.SANITIZED.value):
                raise ConflictError(
                    "发酵罐正在占用，无法重新清洗",
                    tank_id=tank_id,
                    stage=document.get("stage"),
                )
            now = format_moment(self.clock.now())
            merged_context = {"operator": clean_operator}
            self.alarms.raise_alarm(
                brewery_id=str(document.get("brewery_id")),
                source=f"ferment:{tank_id}",
                severity="info",
                code="tank_sanitized",
                message=f"发酵罐 {document.get('code')} 已完成清洗确认",
                context={**merged_context, "certificate_id": certificate["id"]},
            )
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.SANITIZED.value),
                    ("sanitized_at", now),
                    ("cip_certificate_id", certificate["id"]),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            return self.tanks.update(tank_id, mutate)

    def transfer(self, tank_id: str, batch_id: str, volume_l: float) -> dict[str, Any]:
        """把冷却后的麦汁转入发酵罐；必须先清洗且凭证有效。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        volume = require_number(volume_l, field="volume_l", minimum=10.0, maximum=100_000.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != FermentStage.SANITIZED.value:
                raise SequenceError(
                    "发酵罐尚未完成清洗确认，禁止转罐",
                    tank_id=tank_id,
                    stage=document.get("stage"),
                )
            if volume > float(document.get("capacity_l", 0.0)):
                raise InterlockError(
                    "转罐体积超过发酵罐容量",
                    tank_id=tank_id,
                    volume_l=volume,
                    capacity_l=document.get("capacity_l"),
                )
            certificate = self.cip.require_certificate(tank_id)
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.FILLED.value),
                    ("batch_id", clean_batch),
                    ("cip_certificate_id", certificate["id"]),
                    ("filled_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            return self.tanks.update(tank_id, mutate)

    def pitch(self, tank_id: str, temp_c: float, volume_l: float) -> dict[str, Any]:
        """接种酵母；温度必须已经降到接种上限以下。"""

        temperature = require_number(temp_c, field="temp_c", minimum=-5.0, maximum=60.0)
        volume = require_number(volume_l, field="volume_l", minimum=1.0, maximum=10_000.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != FermentStage.FILLED.value:
                raise SequenceError(
                    "发酵罐尚未转罐，禁止接种",
                    tank_id=tank_id,
                    stage=document.get("stage"),
                )
            if temperature > self.settings.pitch_temp_max_c:
                raise InterlockError(
                    "麦汁温度过高，禁止接种酵母",
                    tank_id=tank_id,
                    temp_c=temperature,
                    limit_c=self.settings.pitch_temp_max_c,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.PITCHED.value),
                    ("pitched_at", now),
                    ("pitch_temp_c", temperature),
                    ("pitch_volume_l", volume),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            document = self.tanks.update(tank_id, mutate)
        self.alarms.raise_alarm(
            brewery_id=str(document.get("brewery_id")),
            source=f"ferment:{tank_id}",
            severity="info",
            code="yeast_pitched",
            message=f"发酵罐 {document.get('code')} 已完成接种",
            context={"batch_id": document.get("batch_id"), "temp_c": temperature},
        )
        return document

    def start_fermentation(self, tank_id: str) -> dict[str, Any]:
        """进入恒温发酵阶段。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != FermentStage.PITCHED.value:
                raise SequenceError("尚未接种，无法开始发酵", tank_id=tank_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.FERMENTING.value),
                    ("fermenting_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            return self.tanks.update(tank_id, mutate)

    def mature(self, tank_id: str, days: float) -> dict[str, Any]:
        """发酵完成后进入成熟。"""

        duration = require_number(days, field="days", minimum=0.5, maximum=400.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") != FermentStage.FERMENTING.value:
                raise SequenceError("尚未进入发酵，无法成熟", tank_id=tank_id, stage=document.get("stage"))
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.MATURED.value),
                    ("matured_at", now),
                    ("maturation_days", duration),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            return self.tanks.update(tank_id, mutate)

    def empty(self, tank_id: str, operator: str) -> dict[str, Any]:
        """清空发酵罐并回到待清洗状态。"""

        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("stage") not in (FermentStage.MATURED.value, FermentStage.IDLE.value):
                raise SequenceError(
                    "发酵罐尚未成熟，禁止清空",
                    tank_id=tank_id,
                    stage=document.get("stage"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("stage", FermentStage.IDLE.value),
                    ("batch_id", None),
                    ("sanitized_at", None),
                    ("cip_certificate_id", None),
                    ("emptied_by", clean_operator),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"tank:{tank_id}"):
            return self.tanks.update(tank_id, mutate)

    def status(self, tank_id: str) -> dict[str, Any]:
        """返回发酵罐与压力联锁的组合状态。"""

        tank = self.get(tank_id)
        return {
            "tank": tank,
            "pressure": self.co2.snapshot(tank_id),
            "certificate": self.cip.certificate_for(tank_id),
        }

    def active_tanks(self) -> list[dict[str, Any]]:
        """返回正在使用中的发酵罐。"""

        busy = {
            FermentStage.FILLED.value,
            FermentStage.PITCHED.value,
            FermentStage.FERMENTING.value,
            FermentStage.MATURED.value,
        }
        return [item for item in self.tanks.all() if item.get("stage") in busy]

    def summary(self) -> dict[str, Any]:
        """汇总发酵罐状态。"""

        items = self.tanks.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        capacity = sum(float(item.get("capacity_l", 0.0)) for item in items)
        return {
            "tanks": len(items),
            "by_stage": counts,
            "total_capacity_l": round(capacity, 2),
            "busy": len(self.active_tanks()),
        }
