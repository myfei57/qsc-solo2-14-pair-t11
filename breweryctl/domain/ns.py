"""酿造命名空间：工厂、产线与在制配额。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import NotFoundError, QuotaExceededError, ValidationError
from ..core.ids import new_id, slugify
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore

BREWERIES = "breweries"
LINES = "lines"


class NamespaceRegistry:
    """维护工厂与产线，并限制同时在制批次数。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.breweries = store.collection(BREWERIES)
        self.lines = store.collection(LINES)

    def register_brewery(self, name: str, code: str, location: str = "") -> dict[str, Any]:
        """登记一个酿造工厂。"""

        clean_name = require_text(name, field="name", max_length=80)
        clean_code = slugify(require_text(code, field="code", max_length=32))
        if any(item.get("code") == clean_code for item in self.breweries.all()):
            raise ValidationError("工厂编码已存在", code=clean_code)
        now = format_moment(self.clock.now())
        document = {
            "id": new_id("brew"),
            "name": clean_name,
            "code": clean_code,
            "location": require_text(location, field="location", max_length=120) if location else "",
            "active_batches": [],
            "created_at": now,
            "updated_at": now,
        }
        return self.breweries.put(document["id"], document)

    def list_breweries(self) -> list[dict[str, Any]]:
        """返回全部工厂。"""

        return sorted(self.breweries.all(), key=lambda item: item.get("code", ""))

    def get_brewery(self, brewery_id: str) -> dict[str, Any]:
        """按标识读取工厂。"""

        return self.breweries.require(brewery_id, label="工厂")

    def add_line(
        self,
        brewery_id: str,
        name: str,
        capacity_hl: float,
        vessel_count: int = 1,
    ) -> dict[str, Any]:
        """为工厂增加一条产线。"""

        brewery = self.get_brewery(brewery_id)
        clean_name = require_text(name, field="name", max_length=80)
        capacity = require_number(capacity_hl, field="capacity_hl", minimum=1.0, maximum=10_000.0)
        vessels = require_int(vessel_count, field="vessel_count", minimum=1, maximum=64)
        now = format_moment(self.clock.now())
        document = {
            "id": new_id("line"),
            "brewery_id": brewery["id"],
            "name": clean_name,
            "capacity_hl": capacity,
            "vessel_count": vessels,
            "created_at": now,
            "updated_at": now,
        }
        return self.lines.put(document["id"], document)

    def lines_for(self, brewery_id: str) -> list[dict[str, Any]]:
        """返回工厂下的产线。"""

        self.get_brewery(brewery_id)
        return self.lines.find(lambda item: item.get("brewery_id") == brewery_id)

    def reserve_slot(self, brewery_id: str, batch_id: str) -> dict[str, Any]:
        """占用一个在制批次名额，超出配额时拒绝。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            active = list(document.get("active_batches", []))
            if clean_batch in active:
                return document
            if len(active) >= self.settings.max_active_batches:
                raise QuotaExceededError(
                    "工厂在制批次已达配额",
                    brewery_id=brewery_id,
                    limit=self.settings.max_active_batches,
                    active=active,
                )
            active.append(clean_batch)
            document["active_batches"] = active
            document["updated_at"] = format_moment(self.clock.now())
            return document

        self.breweries.update(brewery_id, mutate)
        return self.usage(brewery_id)

    def release_slot(self, brewery_id: str, batch_id: str) -> dict[str, Any]:
        """释放在制批次名额。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            active = [item for item in document.get("active_batches", []) if item != clean_batch]
            document["active_batches"] = active
            document["updated_at"] = format_moment(self.clock.now())
            return document

        self.breweries.update(brewery_id, mutate)
        return self.usage(brewery_id)

    def usage(self, brewery_id: str) -> dict[str, Any]:
        """返回配额使用情况。"""

        brewery = self.get_brewery(brewery_id)
        active = list(brewery.get("active_batches", []))
        limit = self.settings.max_active_batches
        return {
            "brewery_id": brewery["id"],
            "active": len(active),
            "limit": limit,
            "available": max(limit - len(active), 0),
            "batches": active,
        }

    def describe(self) -> dict[str, Any]:
        """汇总命名空间概况。"""

        breweries = self.list_breweries()
        return {
            "breweries": len(breweries),
            "lines": self.lines.count(),
            "quota": {
                item["id"]: self.usage(item["id"])["active"] for item in breweries
            },
        }

    def seed_default(self) -> dict[str, Any]:
        """在没有工厂时建立一个可运行的默认命名空间。"""

        breweries = self.list_breweries()
        if breweries:
            return breweries[0]
        brewery = self.register_brewery("示范酿造厂", "demo", "上海")
        self.add_line(brewery["id"], "一号糖化线", 120.0, 2)
        return brewery
