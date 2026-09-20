"""外发通道抽象与 HTTP 实现。

传输层只负责"把一批事件送到对端并拿回确认"，不做状态管理：状态全部在
:class:`~breweryctl.outbound.outbox.Outbox` 里。发送失败时通过
:attr:`SendOutcome.retryable` 区分"断网/5xx（可重试）"与"4xx（不可重试）"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.errors import ValidationError

LOGGER = logging.getLogger("breweryctl.outbound")

DEFAULT_TIMEOUT_SEC = 5.0
#: 408/429 以及 5xx 视为可重试，其余 4xx 视为永久性失败。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(Exception):
    """外发失败。``retryable`` 为真时中继应稍后重发。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class SendOutcome:
    """一批事件的发送结果。"""

    acknowledged: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    server_seq: int | None = None
    raw: dict[str, Any] | None = None


class SinkTransport(Protocol):
    """外发通道协议：同步发送一批事件。"""

    def send_batch(self, events: list[dict[str, Any]]) -> SendOutcome:
        """发送并返回被确认的事件 id 列表。"""


class HttpTransport:
    """把事件 POST 到远端 HTTP 收集端的标准库实现。

    远端契约：请求体 ``{"events": [...]}``；成功响应 JSON 至少包含
    ``{"acknowledged": [event_id, ...]}``。对端必须按 ``event_id`` 幂等，
    重复投递返回相同确认即可。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise ValidationError("外发地址不合法", endpoint=endpoint)
        self.endpoint = endpoint
        self.token = token
        self.timeout_sec = timeout_sec

    def send_batch(self, events: list[dict[str, Any]]) -> SendOutcome:
        """同步 POST 一批事件。"""

        if not events:
            return SendOutcome(acknowledged=[])
        body = json.dumps({"events": [_wire_document(item) for item in events]}, ensure_ascii=False)
        request = Request(
            self.endpoint,
            data=body.encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:  # noqa: S310
                raw_body = response.read().decode("utf-8")
                status = getattr(response, "status", 200)
        except HTTPError as exc:
            retryable = exc.code in RETRYABLE_STATUS
            raise TransportError(
                f"外发被拒绝（HTTP {exc.code}）", retryable=retryable
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"外发链路不可达：{exc}", retryable=True) from exc
        if status >= 400:
            raise TransportError(f"外发返回 HTTP {status}", retryable=status in RETRYABLE_STATUS)
        return _parse_outcome(raw_body, expected_ids=[str(item["id"]) for item in events])

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "BreweryCtl-Outbox/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def _wire_document(document: dict[str, Any]) -> dict[str, Any]:
    """挑出对外传输的字段，内部投递状态不外泄。"""

    return {
        "event_id": document.get("id"),
        "kind": document.get("kind"),
        "source_seq": document.get("source_seq"),
        "occurred_at": document.get("occurred_at"),
        "brewery_id": document.get("brewery_id"),
        "batch_id": document.get("batch_id"),
        "payload": document.get("payload") or {},
        "attempt": document.get("attempts", 0),
    }


def _parse_outcome(raw_body: str, *, expected_ids: list[str]) -> SendOutcome:
    """解析并校验对端确认：未确认的事件一律视为可重试失败。"""

    try:
        parsed = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise TransportError("外发响应不是合法 JSON", retryable=True) from exc
    if not isinstance(parsed, dict):
        raise TransportError("外发响应结构不正确", retryable=True)
    acknowledged_raw = parsed.get("acknowledged", [])
    if not isinstance(acknowledged_raw, list):
        raise TransportError("外发响应缺少 acknowledged 列表", retryable=True)
    acknowledged = [str(item) for item in acknowledged_raw]
    rejected_raw = parsed.get("rejected", {})
    rejected: dict[str, str] = {}
    if isinstance(rejected_raw, dict):
        rejected = {str(key): str(value) for key, value in rejected_raw.items()}
    missing = [
        event_id
        for event_id in expected_ids
        if event_id not in acknowledged and event_id not in rejected
    ]
    if missing:
        raise TransportError(
            "部分事件未被对端确认", retryable=True
        )
    server_seq = parsed.get("server_seq")
    return SendOutcome(
        acknowledged=acknowledged,
        rejected=rejected,
        server_seq=int(server_seq) if isinstance(server_seq, int) else None,
        raw=parsed,
    )
