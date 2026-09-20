"""事件外发传输。

把网络故障（连接拒绝、超时、5xx）与对端明确拒收（4xx 且语义为永久失败）
区分开：前者无限退避重试，后者转死信，避免坏数据反复冲链路。
对端返回 409 表示事件已经收过（去重命中），按投递成功处理。
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request


class TransportUnavailable(Exception):
    """链路暂时不可用，事件应退避重试。"""


class TransportRejected(Exception):
    """对端明确拒收，事件应转死信。"""


class EventTransport(Protocol):
    """外发传输协议，测试中可替换成假实现。"""

    def deliver(self, envelope: dict[str, Any]) -> None:
        """投递一条事件；幂等键在 ``envelope["event_id"]``。"""
        ...


class HttpEventTransport:
    """基于标准库 ``urllib`` 的 JSON POST 传输。"""

    def __init__(self, endpoint: str, token: str | None = None, timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout_s = timeout_s

    def deliver(self, envelope: dict[str, Any]) -> None:
        body = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = urllib_request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers=self._headers(envelope),
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout_s) as response:
                response.read()
        except urllib_error.HTTPError as exc:
            self._classify_http(exc)
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise TransportUnavailable(f"事件端点不可达: {exc}") from exc

    def _headers(self, envelope: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": str(envelope.get("event_id", "")),
            "X-Breweryctl-Event": str(envelope.get("kind", "")),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _classify_http(exc: urllib_error.HTTPError) -> None:
        status = exc.code
        if status == 409:
            # 对端已收过同一 Idempotency-Key：去重命中，视为成功。
            return
        if 500 <= status < 600 or status == 408 or status == 429:
            raise TransportUnavailable(f"事件端点临时故障 HTTP {status}") from exc
        detail = ""
        try:
            raw = exc.read()
            detail = raw.decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001 - 读取响应体失败不影响分类
            pass
        raise TransportRejected(f"事件端点拒收 HTTP {status} {detail}".strip()) from exc


def envelope_for(event: dict[str, Any]) -> dict[str, Any]:
    """把发件箱文档转成线上投递的信封。"""

    return {
        "event_id": event.get("event_id"),
        "schema": event.get("schema", "1.0"),
        "kind": event.get("kind"),
        "source": event.get("source"),
        "source_id": event.get("source_id"),
        "brewery_id": event.get("brewery_id"),
        "batch_id": event.get("batch_id"),
        "occurred_at": event.get("occurred_at"),
        "payload": event.get("payload", {}),
    }


class RecordingTransport:
    """内存假传输，用于测试与未配置端点时的离线占位。"""

    def __init__(self) -> None:
        self.delivered: list[dict[str, Any]] = []
        self.unavailable_until: int = 0
        self.reject: set[str] = set()
        self._calls = 0
        self._lock = __import__("threading").Lock()

    def fail_next(self, count: int) -> None:
        """接下来的 ``count`` 次投递抛临时故障。"""

        with self._lock:
            self.unavailable_until = self._calls + count

    def reject_kind(self, kind: str) -> None:
        """让指定 kind 永久拒收。"""

        self.reject.add(kind)

    def deliver(self, envelope: dict[str, Any]) -> None:
        with self._lock:
            self._calls += 1
            if self._calls <= self.unavailable_until:
                raise TransportUnavailable("模拟断链")
            if str(envelope.get("kind")) in self.reject:
                raise TransportRejected("模拟拒收")
            # 同一幂等键投递两次时，第二次报 409 语义。
            if any(item["event_id"] == envelope["event_id"] for item in self.delivered):
                return
            self.delivered.append(envelope)
