"""关键工艺事件范围。

审计动作覆盖面很广（含温度采样等高频记录），外发只挑与批次推进、安全联锁、
清洗凭证和关键操作相关的动作。告警无论等级一律外发。
"""

from __future__ import annotations

#: 需要外发的审计动作前缀；命中其一即视为关键工艺事件。
CRITICAL_ACTION_PREFIXES: tuple[str, ...] = (
    "batch.",
    "mash.",
    "boil.",
    "hop.",
    "wort.",
    "ferment.",
    "maintenance.",
    "control.pressure_",
    "control.latch_",
    "control.valve_",
    "cip.",
)

#: 需要外发的具体审计动作。
CRITICAL_ACTIONS: frozenset[str] = frozenset(
    {
        "batch.created",
        "batch.completed",
        "batch.aborted",
        "mash.water_confirmed",
        "mash.charged",
        "mash.heating",
        "mash.resting",
        "boil.ignited",
        "boil.rolling",
        "boil.completed",
        "hop.added",
        "wort.transferred",
        "temp.cooling",
        "temp.reached",
        "ferment.transferred",
        "ferment.pitched",
        "ferment.matured",
        "maintenance.clean_started",
        "maintenance.clean_step",
        "maintenance.clean_finished",
        "maintenance.tank_emptied",
        "maintenance.circuit_flushed",
        "control.pressure_set",
        "control.pressure_relieved",
        "control.latch_reset",
        "control.valve_operated",
        "cip.started",
    }
)

#: 事件来源类型。
SOURCE_AUDIT = "audit"
SOURCE_ALARM = "alarm"


def is_critical_action(action: str) -> bool:
    """判断审计动作是否属于关键工艺事件。"""

    if action in CRITICAL_ACTIONS:
        return True
    return any(action.startswith(prefix) for prefix in CRITICAL_ACTION_PREFIXES)
