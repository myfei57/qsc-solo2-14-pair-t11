"""测试公共夹具：临时数据目录、可推进时钟与常用工艺脚本。"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from breweryctl.core.config import Settings
from breweryctl.runtime.app import Application


class StepClock:
    """可手动推进的时钟，便于验证煮沸计时与时效判定。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._base = start or datetime(2026, 1, 1, 8, 0, 0, tzinfo=timezone.utc)
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return self._base + self._offset

    def advance(self, minutes: float) -> None:
        self._offset += timedelta(minutes=minutes)


def make_root(prefix: str = "breweryctl-test-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def make_app(**overrides: Any) -> Application:
    clock = overrides.pop("clock", None) or StepClock()
    data_dir = overrides.pop("data_dir", None) or make_root()
    settings = Settings(data_dir=data_dir, host="127.0.0.1", port=0, fsync=False, **overrides)
    app = Application(settings, clock=clock)
    app.bootstrap()
    return app


def published_recipe(app: Application) -> str:
    return str(app.registry.recipes.list(status="published")[0]["id"])


def create_batch(app: Application, volume_l: float = 1000.0) -> str:
    view = app.registry.brewing.create_batch(published_recipe(app), volume_l, "tester")
    return str(view["batch"]["id"])


def mash_to_filter(app: Application, batch_id: str, *, gravity: float = 5.2) -> None:
    """跑完糖化并转入煮沸。"""

    brewing = app.registry.brewing
    brewing.confirm_water(batch_id, 52.0, "probe-mash", "tester")
    brewing.charge_mash(batch_id, 220.0, "tester")
    brewing.heat_mash(batch_id, 65.0, "tester")
    brewing.rest_mash(batch_id, 65.0, "tester")
    brewing.filter_mash(batch_id, gravity, 1020.0, 5.4, "tester")


def boil_to_cooling(app: Application, batch_id: str, hop_minutes: tuple[float, ...] = (1.0, 47.0, 55.0)) -> None:
    """跑完煮沸、投加全部酒花并进入降温。"""

    brewing = app.registry.brewing
    brewing.ignite_boil(batch_id, "tester")
    brewing.boil_rolling(batch_id, "tester")
    for index, minute in enumerate(hop_minutes, start=1):
        brewing.add_hop(batch_id, index, minute, "tester")
    app.registry.clock.advance(60)  # type: ignore[attr-defined]
    brewing.whirlpool(batch_id, 59.0, "tester")


def sanitize_tank(app: Application, tank_id: str) -> dict[str, Any]:
    """完整跑一遍 CIP 并让发酵罐拿到有效凭证。"""

    maintenance = app.registry.maintenance
    cycle = maintenance.start_clean(tank_id, "tester")
    return maintenance.finish_clean(str(cycle["id"]), "tester")


def first_tank(app: Application) -> str:
    return str(app.registry.tanks.list_tanks()[0]["id"])
