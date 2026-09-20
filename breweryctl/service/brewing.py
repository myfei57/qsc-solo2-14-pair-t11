"""批次全流程编排。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, NotFoundError, SequenceError, ValidationError
from ..core.ids import batch_code, new_id
from ..core.validators import require_choice, require_number, require_text
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.boil import BoilKettle
from ..domain.co2 import CO2Controller
from ..domain.ferment import FermentTankService
from ..domain.hop import HopSchedule
from ..domain.mash import MashController
from ..domain.models import Batch, BatchStage, MashStage
from ..domain.ns import NamespaceRegistry
from ..domain.recipe import RecipeRegistry
from ..domain.temp import TemperatureController
from ..domain.wort import WortSystem
from ..persistence.store import FileStore, merge_documents
from ..outbound.publisher import EventPublisher

BATCHES = "batches"


class BrewingService:
    """把配方、糖化、煮沸、发酵与审计串成可回放的批次流程。"""

    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        clock: Clock,
        namespaces: NamespaceRegistry,
        recipes: RecipeRegistry,
        mash: MashController,
        wort: WortSystem,
        boil: BoilKettle,
        hops: HopSchedule,
        tanks: FermentTankService,
        temp: TemperatureController,
        co2: CO2Controller,
        alarms: AlarmCenter,
        audit: AuditLog,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.namespaces = namespaces
        self.recipes = recipes
        self.mash = mash
        self.wort = wort
        self.boil = boil
        self.hops = hops
        self.tanks = tanks
        self.temp = temp
        self.co2 = co2
        self.alarms = alarms
        self.audit = audit
        self.publisher = publisher or EventPublisher()
        self.batches = store.collection(BATCHES)

    def create_batch(
        self,
        recipe_id: str,
        volume_l: float,
        actor: str,
        priority: str = "normal",
        notes: str = "",
    ) -> dict[str, Any]:
        """按已发布配方开一个新批次。"""

        clean_actor = require_text(actor, field="actor", max_length=60)
        volume = require_number(volume_l, field="volume_l", minimum=10.0, maximum=100_000.0)
        clean_priority = require_choice(priority, field="priority", choices=("low", "normal", "high"))
        content = self.recipes.content_for_batch(recipe_id)
        brewery_id = str(content["brewery_id"])
        sequence = self.batches.count() + 1
        now = format_moment(self.clock.now())
        batch_id = new_id("batch")
        batch = Batch(
            id=batch_id,
            code=batch_code(sequence, str(content["style"])),
            brewery_id=brewery_id,
            recipe_id=recipe_id,
            recipe_version=int(content["version"]),
            volume_l=volume,
            stage=BatchStage.MASHING.value,
            priority=clean_priority,
            notes=notes.strip() if isinstance(notes, str) else "",
            created_at=now,
            updated_at=now,
        )
        self.namespaces.reserve_slot(brewery_id, batch_id)
        try:
            self.batches.put(batch_id, batch.to_doc())
            self.mash.start(batch_id, float(content["mash_steps"][0]["target_temp_c"]))
            self.wort.start(batch_id)
            self.hops.plan(batch_id, content["hop_schedule"])
        except Exception:
            self.namespaces.release_slot(brewery_id, batch_id)
            self.batches.delete(batch_id)
            raise
        entry = self.audit.record(
            brewery_id,
            batch_id,
            clean_actor,
            "batch.created",
            {
                "recipe_id": recipe_id,
                "recipe_version": content["version"],
                "volume_l": volume,
                "code": batch.code,
            },
        )
        self.publisher.publish_from_audit(
            "batch.created",
            audit_id=str(entry["id"]),
            brewery_id=brewery_id,
            batch_id=batch_id,
            detail={
                "recipe_id": recipe_id,
                "recipe_version": content["version"],
                "volume_l": volume,
                "code": batch.code,
                "priority": clean_priority,
            },
            actor=clean_actor,
        )
        return self.status(batch_id)

    def confirm_water(self, batch_id: str, temp_c: float, probe_id: str, actor: str) -> dict[str, Any]:
        """确认糖化投料水温。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.MASHING.value)
        run = self.mash.confirm_water(batch_id, temp_c, probe_id)
        self._audit(batch, actor, "mash.water_confirmed", {"temp_c": run.get("water_temp_c"), "probe_id": probe_id})
        return self.status(batch_id)

    def charge_mash(self, batch_id: str, grain_kg: float, actor: str) -> dict[str, Any]:
        """投料。"""

        batch = self._require_batch(batch_id)
        run = self.mash.charge(batch_id, grain_kg)
        self._audit(batch, actor, "mash.charged", {"grain_kg": run.get("grain_kg")})
        return self.status(batch_id)

    def heat_mash(self, batch_id: str, setpoint_c: float, actor: str) -> dict[str, Any]:
        """开始升温到第一个保温温度。"""

        batch = self._require_batch(batch_id)
        run = self.mash.heat(batch_id, setpoint_c)
        self._audit(batch, actor, "mash.heating", {"setpoint_c": run.get("setpoint_c")})
        return self.status(batch_id)

    def rest_mash(self, batch_id: str, actual_temp_c: float, actor: str) -> dict[str, Any]:
        """温度到位后进入保温。"""

        batch = self._require_batch(batch_id)
        run = self.mash.mark_resting(batch_id, actual_temp_c)
        self._audit(batch, actor, "mash.resting", {"actual_temp_c": actual_temp_c})
        return self.status(batch_id)

    def filter_mash(
        self,
        batch_id: str,
        gravity_plato: float,
        volume_l: float,
        ph: float,
        actor: str,
    ) -> dict[str, Any]:
        """过滤并转入煮沸。"""

        batch = self._require_batch(batch_id)
        self.require_mash_active(batch_id)
        content = self.recipes.content_for_batch(str(batch["recipe_id"]))
        self.mash.mark_filtered(batch_id)
        run = self.wort.record_gravity(batch_id, gravity_plato, volume_l)
        run = self.wort.adjust_ph(batch_id, ph)
        self.wort.transfer_to_boil(batch_id, float(content["og_target"]))
        boil_minutes = float(content["boil_minutes"])
        self.boil.start(batch_id, boil_minutes)
        self._set_stage(batch, BatchStage.BOILING.value, boil_id=self.boil.get(batch_id)["id"])
        self._audit(
            batch,
            actor,
            "wort.transferred",
            {
                "gravity_plato": run.get("gravity_plato"),
                "volume_l": run.get("run_off_l"),
                "ph": run.get("ph"),
            },
        )
        return self.status(batch_id)

    def ignite_boil(self, batch_id: str, actor: str) -> dict[str, Any]:
        """开启煮沸锅加热。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.BOILING.value)
        self.boil.ignite(batch_id)
        self._audit(batch, actor, "boil.ignited", {})
        return self.status(batch_id)

    def boil_rolling(self, batch_id: str, actor: str) -> dict[str, Any]:
        """确认麦汁沸腾。"""

        batch = self._require_batch(batch_id)
        self.boil.mark_boiling(batch_id)
        self._audit(batch, actor, "boil.rolling", {})
        return self.status(batch_id)

    def add_hop(self, batch_id: str, position: int, minute: float, actor: str) -> dict[str, Any]:
        """在配方窗口内按序投加酒花。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.BOILING.value)
        addition = self.hops.add(batch_id, position, minute, actor)
        self._audit(
            batch,
            actor,
            "hop.added",
            {"position": position, "minute": minute, "name": addition.get("name")},
        )
        return self.status(batch_id)

    def whirlpool(self, batch_id: str, minute: float, actor: str) -> dict[str, Any]:
        """停止煮沸并进入回旋沉淀，随后开始降温。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.BOILING.value)
        missed = self.hops.mark_missed(batch_id, minute)
        self.boil.mark_whirlpool(batch_id)
        self.boil.complete(batch_id)
        self.temp.start_cooling(batch_id, self.settings.pitch_temp_max_c)
        self._set_stage(batch, BatchStage.COOLING.value)
        for item in missed:
            self.alarms.raise_alarm(
                brewery_id=str(batch["brewery_id"]),
                source=f"hop:{batch_id}",
                severity="warning",
                code="hop_window_missed",
                message=f"酒花 {item.get('name')} 错过投放窗口",
                context={"batch_id": batch_id, "position": item.get("position")},
            )
        self._audit(batch, actor, "boil.completed", {"missed_hops": len(missed)})
        return self.status(batch_id)

    def cool_down(self, batch_id: str, target_c: float, actor: str) -> dict[str, Any]:
        """调整降温目标。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.COOLING.value)
        self.temp.start_cooling(batch_id, target_c)
        self._audit(batch, actor, "temp.cooling", {"target_c": target_c})
        return self.status(batch_id)

    def mark_cooled(self, batch_id: str, value_c: float, actor: str) -> dict[str, Any]:
        """记录温度已经达到接种要求。"""

        batch = self._require_batch(batch_id)
        self.temp.mark_reached(batch_id, value_c)
        self._audit(batch, actor, "temp.reached", {"value_c": value_c})
        return self.status(batch_id)

    def transfer_to_tank(self, batch_id: str, tank_id: str, actor: str) -> dict[str, Any]:
        """把冷却后的麦汁转入已清洗的发酵罐。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.COOLING.value)
        self.temp.require_pitch_temperature(batch_id)
        tank = self.tanks.transfer(tank_id, batch_id, float(batch["volume_l"]))
        self._set_stage(batch, BatchStage.COOLING.value, tank_id=tank_id, cip_certificate_id=tank.get("cip_certificate_id"))
        self._audit(
            batch,
            actor,
            "ferment.transferred",
            {"tank_id": tank_id, "certificate_id": tank.get("cip_certificate_id")},
        )
        return self.status(batch_id)

    def pitch_yeast(self, batch_id: str, tank_id: str, temp_c: float, volume_l: float, actor: str) -> dict[str, Any]:
        """接种酵母并进入发酵。"""

        batch = self._require_batch(batch_id)
        self.temp.require_pitch_temperature(batch_id)
        tank = self.tanks.pitch(tank_id, temp_c, volume_l)
        self.tanks.start_fermentation(tank_id)
        self.co2.set_pressure(tank_id, min(0.2, self.settings.pressure_limit_bar * 0.5))
        self._set_stage(batch, BatchStage.FERMENTING.value, tank_id=tank_id)
        self._audit(
            batch,
            actor,
            "ferment.pitched",
            {"tank_id": tank_id, "temp_c": temp_c, "volume_l": volume_l},
        )
        return self.status(batch_id)

    def mature_batch(self, batch_id: str, tank_id: str, days: float, actor: str) -> dict[str, Any]:
        """发酵结束进入成熟。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.FERMENTING.value)
        self.tanks.mature(tank_id, days)
        self._set_stage(batch, BatchStage.MATURING.value, tank_id=tank_id)
        self._audit(batch, actor, "ferment.matured", {"tank_id": tank_id, "days": days})
        return self.status(batch_id)

    def complete_batch(self, batch_id: str, actor: str) -> dict[str, Any]:
        """完成成熟并释放配额。"""

        batch = self._require_batch(batch_id)
        self._require_stage(batch, BatchStage.MATURING.value)
        now = format_moment(self.clock.now())
        updated = merge_documents(
            batch,
            [
                ("stage", BatchStage.COMPLETED.value),
                ("completed_at", now),
                ("updated_at", now),
            ],
        )
        self.batches.put(batch_id, updated)
        self.namespaces.release_slot(str(batch["brewery_id"]), batch_id)
        self._audit(updated, actor, "batch.completed", {})
        return self.status(batch_id)

    def abort_batch(self, batch_id: str, reason: str, actor: str) -> dict[str, Any]:
        """中止批次并释放命名空间配额。"""

        batch = self._require_batch(batch_id)
        if batch.get("stage") in (BatchStage.COMPLETED.value, BatchStage.ABORTED.value):
            raise ConflictError("批次已经结束", batch_id=batch_id, stage=batch.get("stage"))
        clean_reason = require_text(reason, field="reason", max_length=200)
        now = format_moment(self.clock.now())
        updated = merge_documents(
            batch,
            [
                ("stage", BatchStage.ABORTED.value),
                ("abort_reason", clean_reason),
                ("updated_at", now),
            ],
        )
        self.batches.put(batch_id, updated)
        self.namespaces.release_slot(str(batch["brewery_id"]), batch_id)
        self.alarms.raise_alarm(
            brewery_id=str(batch["brewery_id"]),
            source=f"batch:{batch_id}",
            severity="warning",
            code="batch_aborted",
            message=f"批次 {batch.get('code')} 已中止：{clean_reason}",
            context={"batch_id": batch_id, "reason": clean_reason},
        )
        self._audit(updated, actor, "batch.aborted", {"reason": clean_reason})
        return self.status(batch_id)

    def status(self, batch_id: str) -> dict[str, Any]:
        """返回批次的完整视图。"""

        batch = self._require_batch(batch_id)
        recipe = self.recipes.get(str(batch["recipe_id"]))
        view: dict[str, Any] = {
            "batch": batch,
            "recipe": {
                "id": recipe.get("id"),
                "name": recipe.get("name"),
                "style": recipe.get("style"),
                "version": batch.get("recipe_version"),
            },
            "recipe_mash_steps": self.mash_step_names(str(batch["recipe_id"])),
            "mash": self.mash.get(batch_id),
            "wort": self.wort.get(batch_id),
            "hops": self.hops.summary(batch_id),
        }
        mash_run = view["mash"]
        grain_kg = float(mash_run.get("grain_kg", 0.0))
        if grain_kg > 0 and float(view["wort"].get("gravity_plato", 0.0)) > 0:
            view["efficiency"] = self.wort.efficiency(batch_id, grain_kg)
        boil_document = self.boil.runs.get(batch_id)
        if boil_document is not None:
            view["boil"] = boil_document
            view["boil_elapsed_min"] = round(self.boil.elapsed(batch_id), 3)
        setpoint = self.temp.setpoints.get(batch_id)
        if setpoint is not None:
            view["temperature"] = setpoint
        if batch.get("tank_id"):
            view["tank"] = self.tanks.status(str(batch["tank_id"]))
        return view

    def list_batches(self, stage: str | None = None) -> list[dict[str, Any]]:
        """列出批次，可按阶段过滤。"""

        items = self.batches.all()
        if stage:
            clean_stage = require_choice(
                stage, field="stage", choices=[item.value for item in BatchStage]
            )
            items = [item for item in items if item.get("stage") == clean_stage]
        return sorted(items, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def audit_history(self, batch_id: str) -> dict[str, Any]:
        """返回批次审计包。"""

        self._require_batch(batch_id)
        return self.audit.export_batch(batch_id)

    def summary(self) -> dict[str, Any]:
        """汇总批次状态。"""

        items = self.batches.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("stage"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(items),
            "by_stage": counts,
            "active": len(
                [
                    item
                    for item in items
                    if item.get("stage")
                    not in (BatchStage.COMPLETED.value, BatchStage.ABORTED.value)
                ]
            ),
        }

    def recover(self) -> dict[str, Any]:
        """启动时检查批次与糖化中间态是否一致。"""

        repaired = self.mash.recover()
        repaired_batches: list[str] = []
        for run in repaired:
            batch = self.batches.get(str(run.get("batch_id")))
            if batch and batch.get("stage") == BatchStage.MASHING.value:
                now = format_moment(self.clock.now())
                updated = merge_documents(
                    batch,
                    [("stage", BatchStage.ABORTED.value), ("abort_reason", run.get("failure_reason")), ("updated_at", now)],
                )
                self.batches.put(str(batch["id"]), updated)
                self.namespaces.release_slot(str(batch["brewery_id"]), str(batch["id"]))
                repaired_batches.append(str(batch["id"]))
        return {"mash_runs": repaired, "batches": repaired_batches}

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        document = self.batches.get(batch_id)
        if document is None:
            raise NotFoundError("批次不存在", batch_id=batch_id)
        return document

    def _require_stage(self, batch: dict[str, Any], stage: str) -> None:
        if batch.get("stage") != stage:
            raise SequenceError(
                "批次当前阶段不允许该操作",
                batch_id=batch.get("id"),
                stage=batch.get("stage"),
                required=stage,
            )

    def _set_stage(self, batch: dict[str, Any], stage: str, **patch: Any) -> dict[str, Any]:
        now = format_moment(self.clock.now())
        merged = merge_documents(batch, [("stage", stage), ("updated_at", now)])
        for key, value in patch.items():
            merged[key] = value
        return self.batches.put(str(batch["id"]), merged)

    def _audit(self, batch: dict[str, Any], actor: str, action: str, detail: dict[str, Any]) -> None:
        entry = self.audit.record(
            str(batch["brewery_id"]),
            str(batch["id"]),
            actor,
            action,
            detail,
        )
        # 关键工艺动作与审计同源自登记，event_id 复用审计 id 保证生产侧幂等。
        self.publisher.publish_from_audit(
            action,
            audit_id=str(entry["id"]),
            brewery_id=str(batch["brewery_id"]),
            batch_id=str(batch["id"]),
            detail=detail,
            actor=actor,
        )

    def mash_step_names(self, recipe_id: str) -> list[str]:
        """返回配方中的糖化步骤名，供控制台展示。"""

        content = self.recipes.content_for_batch(recipe_id)
        return [str(item.get("name")) for item in content.get("mash_steps", [])]

    def require_mash_active(self, batch_id: str) -> dict[str, Any]:
        """确认糖化运行仍可继续。"""

        run = self.mash.get(batch_id)
        if run.get("stage") == MashStage.FAILED.value:
            raise ValidationError("糖化运行已经失败", batch_id=batch_id, reason=run.get("failure_reason"))
        return run
