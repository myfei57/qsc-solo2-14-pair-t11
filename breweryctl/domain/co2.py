"""压力与排气联锁控制。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import InterlockError, LatchError, NotFoundError, SequenceError
from ..core.ids import new_id
from ..core.validators import require_choice, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import PressureLatch, PressureState, Valve, ValveState

PRESSURE_STATES = "pressure_states"
VALVES = "valves"

VALVE_PURPOSES = ("relief", "sample", "transfer", "carb")


class CO2Controller:
    """管理发酵罐压力、排气与闩锁复位。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.pressures = store.collection(PRESSURE_STATES)
        self.valves = store.collection(VALVES)

    def ensure_tank(self, tank_id: str, brewery_id: str) -> dict[str, Any]:
        """为发酵罐建立压力状态与阀门。"""

        clean_tank = require_text(tank_id, field="tank_id", max_length=64)
        existing = self.pressures.get(clean_tank)
        if existing is not None:
            return existing
        now = format_moment(self.clock.now())
        state = PressureLatch(
            tank_id=clean_tank,
            brewery_id=require_text(brewery_id, field="brewery_id", max_length=64),
            updated_at=now,
        )
        document = self.pressures.put(clean_tank, state.to_doc())
        for purpose in VALVE_PURPOSES:
            valve = Valve(
                id=new_id("valve"),
                tank_id=clean_tank,
                purpose=purpose,
                state=ValveState.CLOSED.value,
                updated_at=now,
            )
            self.valves.put(valve.id, valve.to_doc())
        self.store.append_event(
            "co2.tank_registered",
            {"tank_id": clean_tank, "brewery_id": brewery_id, "valves": list(VALVE_PURPOSES)},
        )
        return document

    def set_pressure(self, tank_id: str, pressure_bar: float) -> dict[str, Any]:
        """更新罐压；超过上限立即闩锁并打开排气阀。"""

        pressure = require_number(pressure_bar, field="pressure_bar", minimum=0.0, maximum=10.0)
        self.pressures.require(tank_id, label="压力状态")

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            limit = self.settings.pressure_limit_bar
            if pressure > limit:
                return merge_documents(
                    document,
                    [
                        ("state", PressureState.LATCHED.value),
                        ("pressure_bar", pressure),
                        ("latch_reason", f"压力 {pressure:.2f} bar 超过上限 {limit:.2f} bar"),
                        ("latched_at", now),
                        ("released_at", None),
                        ("reset_by", None),
                        ("updated_at", now),
                    ],
                )
            return merge_documents(
                document,
                [("pressure_bar", pressure), ("updated_at", now)],
            )

        with self.store.locks.guard(f"pressure:{tank_id}"):
            document = self.pressures.update(tank_id, mutate)
        if document.get("state") == PressureState.LATCHED.value:
            self._set_valve(tank_id, "relief", ValveState.OPEN.value)
            self.alarms.raise_alarm(
                brewery_id=str(self._brewery_of(document, tank_id)),
                source=f"co2:{tank_id}",
                severity="critical",
                code="pressure_latch",
                message=str(document.get("latch_reason")),
                latching=True,
                context={"tank_id": tank_id, "pressure_bar": pressure},
            )
        return document

    def relieve(self, tank_id: str, target_bar: float) -> dict[str, Any]:
        """主动排气到目标压力；闩锁未复位时拒绝。"""

        target = require_number(target_bar, field="target_bar", minimum=0.0, maximum=10.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("state") == PressureState.LATCHED.value:
                raise LatchError(
                    "压力闩锁未复位，禁止操作排气阀",
                    tank_id=tank_id,
                    latch_reason=document.get("latch_reason"),
                )
            current = float(document.get("pressure_bar", 0.0))
            if current <= target:
                raise SequenceError(
                    "当前压力已经低于目标，无需排气",
                    tank_id=tank_id,
                    pressure_bar=current,
                    target_bar=target,
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("state", PressureState.RELIEVING.value),
                    ("pressure_bar", target),
                    ("released_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"pressure:{tank_id}"):
            document = self.pressures.update(tank_id, mutate)
        self._set_valve(tank_id, "relief", ValveState.OPEN.value)
        return document

    def reset_latch(self, tank_id: str, operator: str) -> dict[str, Any]:
        """压力回落到安全区后人工复位闩锁。"""

        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("state") != PressureState.LATCHED.value:
                raise SequenceError("当前没有压力闩锁", tank_id=tank_id, state=document.get("state"))
            current = float(document.get("pressure_bar", 0.0))
            safe_limit = self.settings.pressure_limit_bar * 0.9
            if current > safe_limit:
                raise InterlockError(
                    "压力尚未回到安全区，禁止复位闩锁",
                    tank_id=tank_id,
                    pressure_bar=current,
                    safe_limit=round(safe_limit, 3),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("state", PressureState.NORMAL.value),
                    ("latch_reason", None),
                    ("latched_at", None),
                    ("released_at", now),
                    ("reset_by", clean_operator),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"pressure:{tank_id}"):
            document = self.pressures.update(tank_id, mutate)
        self._set_valve(tank_id, "relief", ValveState.CLOSED.value)
        self.alarms.raise_alarm(
            brewery_id=str(self._brewery_of(document, tank_id)),
            source=f"co2:{tank_id}",
            severity="info",
            code="pressure_latch_reset",
            message=f"{clean_operator} 已复位压力闩锁",
            latching=False,
            context={"tank_id": tank_id, "operator": clean_operator},
        )
        return document

    def operate_valve(self, tank_id: str, purpose: str, target_state: str, operator: str) -> dict[str, Any]:
        """操作阀门；闩锁期间只允许打开排气阀。"""

        clean_purpose = require_choice(purpose, field="purpose", choices=VALVE_PURPOSES)
        clean_state = require_choice(
            target_state, field="state", choices=[item.value for item in ValveState]
        )
        require_text(operator, field="operator", max_length=60)
        pressure = self.pressures.require(tank_id, label="压力状态")
        if pressure.get("state") == PressureState.LATCHED.value and clean_purpose != "relief":
            raise LatchError(
                "压力闩锁期间禁止操作非排气阀门",
                tank_id=tank_id,
                purpose=clean_purpose,
                latch_reason=pressure.get("latch_reason"),
            )
        if clean_purpose == "relief" and clean_state == ValveState.CLOSED.value and pressure.get("state") == PressureState.LATCHED.value:
            raise LatchError("压力闩锁期间排气阀必须保持开启", tank_id=tank_id)
        return self._set_valve(tank_id, clean_purpose, clean_state)

    def valve_for(self, tank_id: str, purpose: str) -> dict[str, Any]:
        """读取指定阀门。"""

        clean_purpose = require_choice(purpose, field="purpose", choices=VALVE_PURPOSES)
        for item in self.valves.all():
            if item.get("tank_id") == tank_id and item.get("purpose") == clean_purpose:
                return item
        raise NotFoundError("阀门不存在", tank_id=tank_id, purpose=clean_purpose)

    def snapshot(self, tank_id: str) -> dict[str, Any]:
        """返回罐压与全部阀门状态。"""

        pressure = self.pressures.require(tank_id, label="压力状态")
        valves = [
            item for item in self.valves.all() if item.get("tank_id") == tank_id
        ]
        return {
            "tank_id": tank_id,
            "state": pressure.get("state"),
            "pressure_bar": pressure.get("pressure_bar"),
            "latch_reason": pressure.get("latch_reason"),
            "limit_bar": self.settings.pressure_limit_bar,
            "valves": sorted(valves, key=lambda item: str(item.get("purpose"))),
        }

    def latched_tanks(self) -> list[str]:
        """返回处于闩锁状态的发酵罐。"""

        return [
            str(item.get("tank_id"))
            for item in self.pressures.all()
            if item.get("state") == PressureState.LATCHED.value
        ]

    def summary(self) -> dict[str, Any]:
        """汇总压力控制状态。"""

        states = self.pressures.all()
        counts: dict[str, int] = {}
        for item in states:
            key = str(item.get("state"))
            counts[key] = counts.get(key, 0) + 1
        return {"tanks": len(states), "by_state": counts, "valves": self.valves.count()}

    def _set_valve(self, tank_id: str, purpose: str, state: str) -> dict[str, Any]:
        valve = self.valve_for(tank_id, purpose)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("state", state), ("updated_at", format_moment(self.clock.now()))],
            )

        with self.store.locks.guard(f"valve:{valve['id']}"):
            updated = self.valves.update(str(valve["id"]), mutate)
        self.store.append_event(
            "co2.valve",
            {"tank_id": tank_id, "purpose": purpose, "state": state},
        )
        return updated

    def _brewery_of(self, document: dict[str, Any], tank_id: str) -> str:
        brewery_id = document.get("brewery_id")
        if brewery_id:
            return str(brewery_id)
        return f"tank:{tank_id}"
