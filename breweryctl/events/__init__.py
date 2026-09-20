"""关键工艺事件外发。

事件产生点（审计日志、告警中心）把事件写入与业务动作同一存储的发件箱，
后台中继在链路可用时投递；断链期间事件一直垫在箱内，恢复后按顺序去重续传。
"""

from .categories import CRITICAL_ACTIONS
from .hub import EventHub
from .outbox import OUTBOX_EVENTS, EventOutbox, OutboxStatus
from .relay import EventRelay
from .transport import EventTransport, HttpEventTransport, TransportRejected, TransportUnavailable

__all__ = [
    "CRITICAL_ACTIONS",
    "EventHub",
    "EventOutbox",
    "OutboxStatus",
    "OUTBOX_EVENTS",
    "EventRelay",
    "EventTransport",
    "HttpEventTransport",
    "TransportRejected",
    "TransportUnavailable",
]