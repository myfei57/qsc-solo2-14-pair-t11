"""外发事件的文档结构、状态机与关键事件清单。

事件状态：

``pending`` 已登记，尚未送达；
``in_flight`` 已被某个中继领取并发送，结果未知（进程崩溃后会被回收）；
``sent`` 接收端已确认；
``dead`` 超过最大尝试次数，等待人工处理，不再自动续传。

幂等键 ``event_id`` 全局唯一；``source_seq`` 来自本地存储的单调序号，
接收端可据此识别乱序与重复。``batch_id``/``brewery_id`` 用于对账分组。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.errors import ValidationError


class OutboxStatus(str, Enum):
    """外发事件生命周期。"""

    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SENT = "sent"
    DEAD = "dead"


#: 关键工艺事件白名单：阶段跃迁、关键物料动作、安全联锁与告警。
#: 普通温度采样与审计流水不在外发范围，避免把断网积压打爆。
CRITICAL_EVENTS: frozenset[str] = frozenset(
    {
        # 批次生命周期
        "batch.created",
        "batch.completed",
        "batch.aborted",
        # 糖化关键节点
        "mash.water_confirmed",
        "mash.charged",
        "mash.heating",
        "mash.resting",
        "mash.filtered",
        # 煮沸与酒花
        "boil.ignited",
        "boil.rolling",
        "hop.added",
        "hop.missed",
        "boil.whirlpool",
        # 回旋沉淀完成（服务层实际审计动作）
        "boil.completed",
        # 降温、转罐、接种、成熟
        "wort.cooled",
        "temp.reached",
        "ferment.transferred",
        "ferment.pitched",
        "ferment.matured",
        # 安全与告警
        "alarm.raised",
        "alarm.acknowledged",
        "alarm.resolved",
        "pressure.relieving",
        "pressure.latched",
        "pressure.released",
        # 卫生放行
        "cip.completed",
        "cip.certificate_issued",
    }
)


def is_critical(kind: str) -> bool:
    """事件类型是否属于需要外发的关键工艺事件。"""

    return isinstance(kind, str) and kind in CRITICAL_EVENTS


def ensure_critical(kind: str) -> str:
    """拒绝登记非关键事件，防止普通采样灌爆外发通道。"""

    if not is_critical(kind):
        raise ValidationError("不是需要外发的关键工艺事件", kind=kind)
    return kind


class DocMixin:
    """数据类转可持久化文档。"""

    def to_doc(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]


@dataclass
class OutboxEvent(DocMixin):
    """发件箱中的一条外发事件。"""

    id: str
    kind: str
    source_seq: int
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    brewery_id: str | None = None
    batch_id: str | None = None
    status: str = OutboxStatus.PENDING.value
    attempts: int = 0
    max_attempts: int = 20
    outbox_seq: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_error: str | None = None
    sent_at: str | None = None
    acknowledged_id: str | None = None

    @staticmethod
    def from_doc(document: dict[str, Any]) -> "OutboxEvent":
        """从存储文档还原事件，缺失字段按默认值补齐。"""

        return OutboxEvent(
            id=str(document["id"]),
            kind=str(document["kind"]),
            source_seq=int(document.get("source_seq", 0)),
            occurred_at=str(document.get("occurred_at", "")),
            payload=dict(document.get("payload") or {}),
            brewery_id=document.get("brewery_id"),
            batch_id=document.get("batch_id"),
            status=str(document.get("status", OutboxStatus.PENDING.value)),
            attempts=int(document.get("attempts", 0)),
            max_attempts=int(document.get("max_attempts", 20)),
            outbox_seq=int(document.get("outbox_seq", 0)),
            created_at=str(document.get("created_at", "")),
            updated_at=str(document.get("updated_at", "")),
            last_error=document.get("last_error"),
            sent_at=document.get("sent_at"),
            acknowledged_id=document.get("acknowledged_id"),
        )
