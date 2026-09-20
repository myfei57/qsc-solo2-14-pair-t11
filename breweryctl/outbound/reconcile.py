"""对账：核对本地发件箱与接收端记录是否一致。

两层核对：

* **本地完整性**：已送达事件的 ``source_seq`` 必须连续无缺口（排除死信），
  缺口意味着有事件尚未送达或被漏处理；
* **远端确认**：把本地一段时间内的事件摘要（``event_id`` 列表）交给远端
  ``/events/reconcile`` 一类接口比对，返回"远端缺失"的事件 id，中继据此重发。

对账只读不改；发现差异后由人工或重新 ``drain`` 触发续传。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..core.errors import ValidationError
from .events import OutboxStatus
from .outbox import Outbox

LOGGER = logging.getLogger("breweryctl.outbound.reconcile")


@dataclass
class ReconcileReport:
    """一次对账的结果。"""

    local_total: int = 0
    local_sent: int = 0
    local_pending: int = 0
    local_dead: int = 0
    gaps: list[int] = field(default_factory=list)
    missing_remote: list[str] = field(default_factory=list)
    unexpected_remote: list[str] = field(default_factory=list)
    matched: int = 0
    consistent: bool = True
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_total": self.local_total,
            "local_sent": self.local_sent,
            "local_pending": self.local_pending,
            "local_dead": self.local_dead,
            "gaps": self.gaps,
            "missing_remote": self.missing_remote,
            "unexpected_remote": self.unexpected_remote,
            "matched": self.matched,
            "consistent": self.consistent,
            "detail": self.detail,
        }


class ReconcileClient(Protocol):
    """远端对账接口协议。"""

    def compare(
        self, sent_ids: list[str], *, batch_id: str | None = None
    ) -> dict[str, Any]:
        """提交本地已送达 id，返回远端缺失/多余的 id。"""


class HttpReconcileClient:
    """标准库实现的远端对账客户端。

    远端契约：POST JSON ``{"event_ids": [...], "batch_id": ...}，
    返回 ``{"missing": [...], "unexpected": [...]}``。
    """

    def __init__(self, endpoint: str, *, token: str | None = None, timeout_sec: float = 8.0) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValidationError("对账地址不合法", endpoint=endpoint)
        self.endpoint = endpoint
        self.token = token
        self.timeout_sec = timeout_sec

    def compare(
        self, sent_ids: list[str], *, batch_id: str | None = None
    ) -> dict[str, Any]:
        body = json.dumps(
            {"event_ids": sent_ids, "batch_id": batch_id}, ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "BreweryCtl-Reconcile/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.endpoint, data=body, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_sec) as response:  # noqa: S310
                parsed = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ValidationError(
                f"对账接口返回 HTTP {exc.code}", endpoint=self.endpoint
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"对账接口不可达：{exc}", endpoint=self.endpoint) from exc
        if not isinstance(parsed, dict):
            raise ValidationError("对账响应结构不正确")
        return parsed


class Reconciler:
    """本地核对 + 可选远端比对。"""

    def __init__(self, outbox: Outbox) -> None:
        self.outbox = outbox

    def local_check(self, *, batch_id: str | None = None) -> ReconcileReport:
        """检查本地已送达序号是否连续、是否有遗留待发/死信。"""

        events = self.outbox.list_events(batch_id=batch_id, limit=0)
        report = ReconcileReport(local_total=len(events))
        sent_seqs: list[int] = []
        for item in events:
            status = item.get("status")
            if status == OutboxStatus.SENT.value:
                report.local_sent += 1
                sent_seqs.append(int(item.get("outbox_seq") or 0))
            elif status == OutboxStatus.DEAD.value:
                report.local_dead += 1
            else:
                report.local_pending += 1
        sent_seqs.sort()
        in_flight_seqs = {
            int(item.get("outbox_seq") or 0)
            for item in events
            if item.get("status") not in (OutboxStatus.SENT.value, OutboxStatus.DEAD.value)
        }
        raw_gaps = _sequence_gaps(sent_seqs)
        # pending/in_flight 占住的序号属于"在途"，而非"丢失缺口"。
        report.gaps = [seq for seq in raw_gaps if seq not in in_flight_seqs]
        report.consistent = not report.gaps and report.local_pending == 0
        # 死信是显式挂起的人工事项，不算"悄悄丢了"，但单独暴露。
        report.detail = (
            None
            if report.consistent and report.local_dead == 0
            else "存在缺口、待发事件或死信，详见各字段"
        )
        return report

    def remote_check(
        self,
        client: ReconcileClient,
        *,
        batch_id: str | None = None,
    ) -> ReconcileReport:
        """在本地核对基础上，再与远端逐条比对 event_id。"""

        report = self.local_check(batch_id=batch_id)
        sent_events = self.outbox.list_events(
            status=OutboxStatus.SENT.value, batch_id=batch_id, limit=0
        )
        sent_ids = [str(item["id"]) for item in sent_events]
        if not sent_ids:
            return report
        outcome = client.compare(sent_ids, batch_id=batch_id)
        missing = outcome.get("missing", [])
        unexpected = outcome.get("unexpected", [])
        report.missing_remote = [str(item) for item in missing] if isinstance(missing, list) else []
        report.unexpected_remote = (
            [str(item) for item in unexpected] if isinstance(unexpected, list) else []
        )
        report.matched = len(sent_ids) - len(report.missing_remote)
        if report.missing_remote:
            report.consistent = False
            report.detail = "远端缺失事件，需要触发续传"
        return report

    def requeue_missing(self, report: ReconcileReport) -> int:
        """把远端缺失的事件从 ``sent`` 退回 ``pending`` 以便重发。

        这是对账与续传之间的显式衔接：只处理报告里列出的 id，
        不自动碰其他事件。
        """

        count = 0
        for event_id in report.missing_remote:
            try:
                document = self.outbox.get(event_id)
            except Exception:
                continue
            if document.get("status") != OutboxStatus.SENT.value:
                continue

            def reset(document: dict[str, Any]) -> dict[str, Any]:
                document["status"] = OutboxStatus.PENDING.value
                document["sent_at"] = None
                document["acknowledged_id"] = None
                document["last_error"] = "对账发现远端缺失，退回重发"
                return document

            self.outbox.events.update(event_id, reset)
            count += 1
        return count


def _sequence_gaps(ordered_seqs: list[int]) -> list[int]:
    """返回连续序列中间缺失的序号（不把 0 之前当缺口）。"""

    if not ordered_seqs:
        return []
    gaps: list[int] = []
    first = max(ordered_seqs[0], 1)
    present = set(ordered_seqs)
    for seq in range(first, ordered_seqs[-1] + 1):
        if seq not in present:
            gaps.append(seq)
    return gaps
