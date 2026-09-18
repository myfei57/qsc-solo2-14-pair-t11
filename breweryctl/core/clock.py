"""时间抽象。

业务代码统一通过 :class:`Clock` 取时间，既保证写入快照的时间戳格式一致，
也避免领域模块直接依赖 ``datetime.now``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .errors import ValidationError


class Clock(Protocol):
    """返回带时区 UTC 时间的时钟协议。"""

    def now(self) -> datetime:
        """返回当前时刻。"""


class SystemClock:
    """基于系统时钟的实现。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def format_moment(moment: datetime) -> str:
    """把时刻序列化成秒级 ISO 8601 文本。"""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_moment(text: str) -> datetime:
    """解析 :func:`format_moment` 产出的文本。"""

    if not isinstance(text, str) or not text:
        raise ValidationError("时间文本不能为空", field="moment")
    normalized = text.replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("时间文本格式不正确", value=text) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def elapsed_minutes(start: str, end: str) -> float:
    """返回两个时间文本之间的分钟数。"""

    delta = parse_moment(end) - parse_moment(start)
    return delta.total_seconds() / 60.0
