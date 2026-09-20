"""组合根：装配存储、领域组件与应用服务。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, SystemClock, format_moment
from ..core.config import Settings
from ..domain.alarms import AlarmCenter
from ..domain.audit import AuditLog
from ..domain.boil import BoilKettle
from ..domain.cip import CIPService
from ..domain.co2 import CO2Controller
from ..domain.ferment import FermentTankService
from ..domain.hop import HopSchedule
from ..domain.mash import MashController
from ..domain.ns import NamespaceRegistry
from ..domain.recipe import RecipeRegistry
from ..domain.temp import TemperatureController
from ..domain.wort import WortSystem
from ..events.hub import EventHub
from ..events.relay import EventRelay
from ..events.transport import HttpEventTransport
from ..persistence.store import FileStore
from .brewing import BrewingService
from .control import ControlService
from .maintenance import MaintenanceService
from .telemetry import TelemetryService


class ComponentRegistry:
    """把全部组件装配到同一个存储之上。"""

    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = settings.validate()
        self.clock = clock or SystemClock()
        self.store = FileStore(self.settings.data_dir, self.clock, fsync=self.settings.fsync).open()
        self.alarms = AlarmCenter(self.store, self.clock)
        self.audit = AuditLog(self.store, self.clock)
        self.namespaces = NamespaceRegistry(self.store, self.settings, self.clock)
        self.recipes = RecipeRegistry(self.store, self.clock)
        self.mash = MashController(self.store, self.settings, self.clock)
        self.wort = WortSystem(self.store, self.clock)
        self.boil = BoilKettle(self.store, self.clock)
        self.hops = HopSchedule(self.store, self.settings, self.clock)
        self.temp = TemperatureController(self.store, self.settings, self.clock)
        self.co2 = CO2Controller(self.store, self.settings, self.clock, self.alarms)
        self.cip = CIPService(self.store, self.settings, self.clock, self.alarms)
        self.tanks = FermentTankService(
            self.store, self.settings, self.clock, self.cip, self.co2, self.alarms
        )
        self.brewing = BrewingService(
            self.store,
            self.settings,
            self.clock,
            self.namespaces,
            self.recipes,
            self.mash,
            self.wort,
            self.boil,
            self.hops,
            self.tanks,
            self.temp,
            self.co2,
            self.alarms,
            self.audit,
        )
        self.control = ControlService(self.temp, self.co2, self.alarms, self.audit)
        self.telemetry = TelemetryService(self.temp, self.alarms, self.audit)
        self.maintenance = MaintenanceService(self.cip, self.tanks, self.audit)
        self.events = self._build_event_hub()

    def _build_event_hub(self) -> EventHub:
        """组装事件枢纽与中继，并把发布点接到审计与告警中心。"""

        transport = None
        if self.settings.event_endpoint:
            transport = HttpEventTransport(
                self.settings.event_endpoint,
                token=self.settings.event_token,
                timeout_s=self.settings.event_timeout_s,
            )
        relay = EventRelay(
            outbox=None,
            transport=transport,
            clock=self.clock,
            batch_size=self.settings.event_batch_size,
            retry_base_s=self.settings.event_retry_base_s,
            retry_max_s=self.settings.event_retry_max_s,
            stale_claim_s=self.settings.event_stale_claim_s,
        )
        hub = EventHub(self.store, self.clock, relay=relay)
        relay.outbox = hub.outbox
        self.audit.set_sink(hub.publish_audit)
        self.alarms.set_sink(hub.publish_alarm)
        return hub

    def start_relay(self) -> None:
        """启动事件外发中继（仅在配置了端点且启用时生效）。"""

        if self.settings.event_relay_enabled:
            self.events.relay.start()  # type: ignore[union-attr]

    def stop_relay(self) -> None:
        """关停事件外发中继。"""

        self.events.relay.stop()  # type: ignore[union-attr]

    def bootstrap(self) -> dict[str, Any]:
        """确保存在可运行的默认命名空间、罐体、探头与配方。"""

        created: dict[str, Any] = {
            "brewery": None,
            "lines": 0,
            "tanks": 0,
            "probes": 0,
            "recipe": None,
            "recovered": None,
        }
        brewery = self.namespaces.seed_default()
        created["brewery"] = brewery["id"]
        if not self.namespaces.lines_for(str(brewery["id"])):
            self.namespaces.add_line(str(brewery["id"]), "一号糖化线", 120.0, 2)
            created["lines"] = 1
        tanks = self.tanks.list_tanks(str(brewery["id"]))
        if not tanks:
            tanks = [
                self.tanks.register_tank(str(brewery["id"]), 1, 1200.0),
                self.tanks.register_tank(str(brewery["id"]), 2, 1200.0),
            ]
            created["tanks"] = len(tanks)
        if not self.cip.circuits_for(str(brewery["id"])):
            self.cip.register_circuit(
                str(brewery["id"]),
                1,
                [str(tank["id"]) for tank in tanks],
                12.0,
            )
        probes = [
            item for item in self.temp.probes.all() if item.get("brewery_id") == brewery["id"]
        ]
        if not probes:
            self.temp.register_probe(str(brewery["id"]), "糖化锅", 65.0)
            self.temp.register_probe(str(brewery["id"]), "发酵罐", 10.0)
            created["probes"] = 2
        if not self.recipes.list():
            recipe = self._seed_recipe(str(brewery["id"]))
            created["recipe"] = recipe["id"]
        created["recovered"] = self.brewing.recover()
        self.store.set_meta("booted_at", format_moment(self.clock.now()))
        # 与本地审计/告警记录对账，把断装或升级前漏发的关键事件补进发件箱。
        created["events_reconciled"] = self.events.reconcile()
        return created

    def state_overview(self) -> dict[str, Any]:
        """汇总平台整体状态，供控制台首页使用。"""

        recipes = self.recipes.list()
        return {
            "settings": self.settings.describe(),
            "store": self.store.stats(),
            "namespace": self.namespaces.describe(),
            "recipes": {
                "total": len(recipes),
                "published": len(
                    [item for item in recipes if item.get("status") == "published"]
                ),
            },
            "batches": self.brewing.summary(),
            "mash": self.mash.summary(),
            "boil": self.boil.summary(),
            "ferment": self.tanks.summary(),
            "maintenance": self.maintenance.summary(),
            "control": self.control.summary(),
            "alarms": self.alarms.summary(),
            "audit_entries": self.audit.count(),
            "events": self.events.relay.status(),
            "tanks": self.tanks.list_tanks(),
        }

    def close(self) -> None:
        """停止中继并关闭存储落盘。"""

        self.stop_relay()
        self.store.close()

    def _seed_recipe(self, brewery_id: str) -> dict[str, Any]:
        recipe = self.recipes.define(
            name="琥珀艾尔",
            style="Pale Ale",
            brewery_id=brewery_id,
            volume_l=1000.0,
            boil_minutes=60.0,
            og_target=1.052,
            fg_target=1.012,
            ibu_target=38.0,
            mash_steps=[
                {"name": "蛋白休止", "target_temp_c": 52.0, "minutes": 15.0},
                {"name": "糖化休止", "target_temp_c": 65.0, "minutes": 45.0},
                {"name": "出糖", "target_temp_c": 76.0, "minutes": 10.0},
            ],
            hop_schedule=[
                {"name": "Magnum", "amount_g": 600.0, "window_start_min": 0.0, "window_end_min": 5.0},
                {"name": "Cascade", "amount_g": 400.0, "window_start_min": 45.0, "window_end_min": 50.0},
                {"name": "Citra", "amount_g": 300.0, "window_start_min": 52.0, "window_end_min": 58.0},
            ],
        )
        return self.recipes.publish(str(recipe["id"]))
