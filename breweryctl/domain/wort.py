"""麦汁过滤、浓度与 pH 记录。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.errors import NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .models import WortRun

WORT_RUNS = "wort_runs"


class WortSystem:
    """维护麦汁批次数据。"""

    def __init__(self, store: FileStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock
        self.runs = store.collection(WORT_RUNS)

    def start(self, batch_id: str) -> dict[str, Any]:
        """建立麦汁记录。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        if self.runs.get(clean_batch) is not None:
            return self.runs.require(clean_batch)
        now = format_moment(self.clock.now())
        run = WortRun(id=new_id("wort"), batch_id=clean_batch, updated_at=now)
        return self.runs.put(clean_batch, run.to_doc())

    def record_gravity(self, batch_id: str, gravity_plato: float, volume_l: float) -> dict[str, Any]:
        """记录过滤后的麦汁浓度与体积。"""

        gravity = require_number(gravity_plato, field="gravity_plato", minimum=1.0, maximum=30.0)
        volume = require_number(volume_l, field="volume_l", minimum=1.0, maximum=20_000.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("gravity_plato", gravity),
                    ("run_off_l", volume),
                    ("filtered_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"wort:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def adjust_ph(self, batch_id: str, ph: float) -> dict[str, Any]:
        """记录并校正麦汁 pH。"""

        value = require_number(ph, field="ph", minimum=4.0, maximum=7.0)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            return merge_documents(
                document,
                [("ph", value), ("updated_at", format_moment(self.clock.now()))],
            )

        with self.store.locks.guard(f"wort:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def transfer_to_boil(self, batch_id: str, og_target: float) -> dict[str, Any]:
        """把麦汁送入煮沸锅，浓度必须在目标附近。"""

        target = require_number(og_target, field="og_target", minimum=0.9, maximum=1.3)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            gravity = float(document.get("gravity_plato", 0.0))
            if gravity <= 0:
                raise SequenceError("尚未记录麦汁浓度，禁止转入煮沸", batch_id=batch_id)
            if document.get("transferred_at"):
                raise SequenceError("麦汁已经转入煮沸锅", batch_id=batch_id)
            low = (target - 1.0) * 100.0 - 1.5
            high = (target - 1.0) * 100.0 + 1.5
            if not low <= gravity <= high:
                raise ValidationError(
                    "麦汁浓度偏离配方目标",
                    batch_id=batch_id,
                    gravity_plato=gravity,
                    expected_range=[round(low, 2), round(high, 2)],
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [("transferred_at", now), ("updated_at", now)],
            )

        with self.store.locks.guard(f"wort:{batch_id}"):
            return self.runs.update(batch_id, mutate)

    def get(self, batch_id: str) -> dict[str, Any]:
        """读取麦汁记录。"""

        document = self.runs.get(batch_id)
        if document is None:
            raise NotFoundError("麦汁记录不存在", batch_id=batch_id)
        return document

    def efficiency(self, batch_id: str, grain_kg: float) -> dict[str, Any]:
        """按浓度与投料量估算糖化收得率。"""

        grain = require_number(grain_kg, field="grain_kg", minimum=0.1, maximum=10_000.0)
        document = self.get(batch_id)
        gravity = float(document.get("gravity_plato", 0.0))
        volume = float(document.get("run_off_l", 0.0))
        extract_kg = volume * gravity / 100.0 * 1.04
        ratio = extract_kg / grain if grain else 0.0
        return {
            "batch_id": batch_id,
            "extract_kg": round(extract_kg, 3),
            "grain_kg": grain,
            "extract_ratio": round(ratio, 4),
            "gravity_plato": gravity,
        }
