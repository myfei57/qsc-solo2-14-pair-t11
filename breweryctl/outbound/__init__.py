"""关键工艺事件可靠外发。

本地业务动作落盘后立刻写入同一份存储中的发件箱（outbox），由后台中继
按序发送；网络中断期间事件只累积在本地，链路恢复后按全局序号续传，
接收端依据事件幂等键去重，保证"至少一次送达 + 对端只生效一次"。
"""

from .events import CRITICAL_EVENTS, OutboxEvent, OutboxStatus, is_critical
from .outbox import Outbox
from .publisher import EventPublisher
from .reconcile import (
    HttpReconcileClient,
    ReconcileReport,
    Reconciler,
)
from .relay import OutboxRelay, SendResult
from .transport import HttpTransport, SendOutcome, SinkTransport, TransportError

__all__ = [
    "CRITICAL_EVENTS",
    "EventPublisher",
    "HttpReconcileClient",
    "HttpTransport",
    "Outbox",
    "OutboxEvent",
    "OutboxRelay",
    "OutboxStatus",
    "ReconcileReport",
    "Reconciler",
    "SendOutcome",
    "SendResult",
    "SinkTransport",
    "TransportError",
    "is_critical",
]
