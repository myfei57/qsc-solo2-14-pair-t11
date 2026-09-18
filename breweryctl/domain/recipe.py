"""配方定义、发布与回滚。"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.clock import Clock, format_moment
from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_int, require_number, require_text
from ..persistence.store import FileStore
from .models import HopStatus, Recipe, RecipeStatus, RecipeStep

RECIPES = "recipes"


class RecipeRegistry:
    """配方版本注册表。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.recipes = store.collection(RECIPES)

    def define(
        self,
        name: str,
        style: str,
        brewery_id: str,
        volume_l: float,
        boil_minutes: float,
        og_target: float,
        fg_target: float,
        ibu_target: float,
        mash_steps: Iterable[dict[str, Any]],
        hop_schedule: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        """建立一个草稿配方。"""

        steps = self._normalize_steps(mash_steps)
        hops = self._normalize_hops(hop_schedule)
        clean_og = require_number(og_target, field="og_target", minimum=1.0, maximum=1.2)
        clean_fg = require_number(fg_target, field="fg_target", minimum=0.95, maximum=1.1)
        if clean_fg >= clean_og:
            raise ValidationError("终止比重必须低于初始比重", og=clean_og, fg=clean_fg)
        now = format_moment(self.clock.now())
        recipe_id = new_id("recipe")
        content = {
            "version": 1,
            "status": RecipeStatus.DRAFT.value,
            "name": require_text(name, field="name", max_length=80),
            "style": require_text(style, field="style", max_length=40),
            "brewery_id": require_text(brewery_id, field="brewery_id", max_length=64),
            "volume_l": require_number(volume_l, field="volume_l", minimum=5.0, maximum=20_000.0),
            "boil_minutes": require_number(boil_minutes, field="boil_minutes", minimum=15.0, maximum=240.0),
            "og_target": clean_og,
            "fg_target": clean_fg,
            "ibu_target": require_number(ibu_target, field="ibu_target", minimum=0.0, maximum=120.0),
            "mash_steps": [step.to_doc() for step in steps],
            "hop_schedule": [dict(hop) for hop in hops],
            "created_at": now,
            "published_at": None,
            "rolled_back_from": None,
        }
        document = Recipe(
            id=recipe_id,
            name=content["name"],
            style=content["style"],
            brewery_id=content["brewery_id"],
            volume_l=content["volume_l"],
            boil_minutes=content["boil_minutes"],
            og_target=content["og_target"],
            fg_target=content["fg_target"],
            ibu_target=content["ibu_target"],
            mash_steps=content["mash_steps"],
            hop_schedule=content["hop_schedule"],
            current_version=1,
            status=RecipeStatus.DRAFT.value,
            created_at=now,
            published_at=None,
            versions=[content],
        ).to_doc()
        return self.recipes.put(recipe_id, document)

    def publish(self, recipe_id: str) -> dict[str, Any]:
        """发布当前草稿版本。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            version = self._current_version(document)
            if version.get("status") == RecipeStatus.PUBLISHED.value:
                raise ConflictError("该版本已经发布", recipe_id=recipe_id, version=version["version"])
            self._validate_content(version)
            now = format_moment(self.clock.now())
            version["status"] = RecipeStatus.PUBLISHED.value
            version["published_at"] = now
            document["status"] = RecipeStatus.PUBLISHED.value
            document["published_at"] = now
            return document

        return self.recipes.update(recipe_id, mutate)

    def revise(self, recipe_id: str, **patch: Any) -> dict[str, Any]:
        """基于当前版本创建新的草稿版本。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            current = self._current_version(document)
            version_number = int(document.get("current_version", 1)) + 1
            content = dict(current)
            content.update(patch)
            content["version"] = version_number
            content["status"] = RecipeStatus.DRAFT.value
            content["created_at"] = format_moment(self.clock.now())
            content["published_at"] = None
            content["rolled_back_from"] = None
            steps = self._normalize_steps(content.get("mash_steps", []))
            hops = self._normalize_hops(content.get("hop_schedule", []))
            content["mash_steps"] = [step.to_doc() for step in steps]
            content["hop_schedule"] = [hop.to_doc() for hop in hops]
            document["current_version"] = version_number
            document["status"] = RecipeStatus.DRAFT.value
            document["published_at"] = None
            document["versions"] = list(document.get("versions", [])) + [content]
            self._sync_head(document, content)
            return document

        return self.recipes.update(recipe_id, mutate)

    def rollback(self, recipe_id: str, version_number: int) -> dict[str, Any]:
        """把历史版本内容复制成新版本，避免覆盖已发布记录。"""

        clean_version = require_int(version_number, field="version", minimum=1)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            source = self._version(document, clean_version)
            next_number = int(document.get("current_version", 1)) + 1
            content = dict(source)
            content["version"] = next_number
            content["status"] = RecipeStatus.DRAFT.value
            content["created_at"] = format_moment(self.clock.now())
            content["published_at"] = None
            content["rolled_back_from"] = clean_version
            document["current_version"] = next_number
            document["status"] = RecipeStatus.DRAFT.value
            document["versions"] = list(document.get("versions", [])) + [content]
            self._sync_head(document, content)
            return document

        return self.recipes.update(recipe_id, mutate)

    def archive(self, recipe_id: str) -> dict[str, Any]:
        """归档配方，归档后不可再用于新批次。"""

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            document["status"] = RecipeStatus.ARCHIVED.value
            return document

        return self.recipes.update(recipe_id, mutate)

    def get(self, recipe_id: str) -> dict[str, Any]:
        """按标识读取配方。"""

        return self.recipes.require(recipe_id, label="配方")

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出配方，可按状态过滤。"""

        items = self.recipes.all()
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: (item.get("style", ""), item.get("name", "")))

    def current_content(self, recipe_id: str) -> dict[str, Any]:
        """返回当前版本的工艺内容。"""

        document = self.get(recipe_id)
        return dict(self._current_version(document))

    def content_for_batch(self, recipe_id: str) -> dict[str, Any]:
        """返回可用于开新批次的已发布内容。"""

        document = self.get(recipe_id)
        if document.get("status") == RecipeStatus.ARCHIVED.value:
            raise ConflictError("配方已归档", recipe_id=recipe_id)
        version = self.current_content(recipe_id)
        if version.get("status") != RecipeStatus.PUBLISHED.value:
            raise ConflictError("配方尚未发布", recipe_id=recipe_id, version=version.get("version"))
        return dict(version)

    def history(self, recipe_id: str) -> list[dict[str, Any]]:
        """返回配方版本历史摘要。"""

        document = self.get(recipe_id)
        return [
            {
                "version": item.get("version"),
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "published_at": item.get("published_at"),
                "rolled_back_from": item.get("rolled_back_from"),
            }
            for item in document.get("versions", [])
        ]

    def _normalize_steps(self, steps: Iterable[dict[str, Any]]) -> list[RecipeStep]:
        normalized: list[RecipeStep] = []
        for position, raw in enumerate(steps, start=1):
            if not isinstance(raw, dict):
                raise ValidationError("糖化步骤必须是对象", step=raw)
            normalized.append(
                RecipeStep(
                    position=position,
                    name=require_text(raw.get("name"), field="step.name", max_length=60),
                    target_temp_c=require_number(
                        raw.get("target_temp_c"), field="step.target_temp_c", minimum=5.0, maximum=100.0
                    ),
                    minutes=require_number(raw.get("minutes"), field="step.minutes", minimum=1.0, maximum=600.0),
                )
            )
        if not normalized:
            raise ValidationError("配方至少需要一个糖化步骤")
        temperatures = [step.target_temp_c for step in normalized]
        if temperatures != sorted(temperatures):
            raise ValidationError("糖化步骤温度必须非递减", temperatures=temperatures)
        return normalized

    def _normalize_hops(self, hops: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for position, raw in enumerate(hops, start=1):
            if not isinstance(raw, dict):
                raise ValidationError("酒花计划必须是对象", hop=raw)
            start = require_number(raw.get("window_start_min"), field="hop.window_start_min", minimum=0.0, maximum=600.0)
            end = require_number(raw.get("window_end_min"), field="hop.window_end_min", minimum=0.0, maximum=600.0)
            if end < start:
                raise ValidationError("酒花投放窗口结束时间不能早于开始时间", hop=raw)
            normalized.append(
                {
                    "position": position,
                    "name": require_text(raw.get("name"), field="hop.name", max_length=60),
                    "amount_g": require_number(raw.get("amount_g"), field="hop.amount_g", minimum=1.0, maximum=100_000.0),
                    "window_start_min": start,
                    "window_end_min": end,
                    "status": HopStatus.PENDING.value,
                    "added_at": None,
                    "actual_minute": None,
                }
            )
        if not normalized:
            raise ValidationError("配方至少需要一条酒花计划")
        starts = [item["window_start_min"] for item in normalized]
        if starts != sorted(starts):
            raise ValidationError("酒花计划必须按投放时间升序排列", starts=starts)
        return normalized

    def _validate_content(self, content: dict[str, Any]) -> None:
        if not content.get("mash_steps"):
            raise ValidationError("配方缺少糖化步骤")
        if not content.get("hop_schedule"):
            raise ValidationError("配方缺少酒花计划")
        if float(content.get("fg_target", 0)) >= float(content.get("og_target", 0)):
            raise ValidationError("终止比重必须低于初始比重")

    def _current_version(self, document: dict[str, Any]) -> dict[str, Any]:
        number = int(document.get("current_version", 1))
        return self._version(document, number)

    def _version(self, document: dict[str, Any], number: int) -> dict[str, Any]:
        for item in document.get("versions", []):
            if int(item.get("version", 0)) == number:
                return item
        raise NotFoundError("配方版本不存在", recipe_id=document.get("id"), version=number)

    def _sync_head(self, document: dict[str, Any], content: dict[str, Any]) -> None:
        for key in (
            "name",
            "style",
            "brewery_id",
            "volume_l",
            "boil_minutes",
            "og_target",
            "fg_target",
            "ibu_target",
            "mash_steps",
            "hop_schedule",
        ):
            document[key] = content.get(key)
