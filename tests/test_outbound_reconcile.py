"""对账：本地序号缺口、远端缺失比对与缺失重排队。"""

from __future__ import annotations

import unittest

from breweryctl.outbound.events import OutboxStatus
from breweryctl.outbound.outbox import Outbox
from breweryctl.outbound.reconcile import Reconciler
from breweryctl.persistence.store import FileStore

from .helpers import StepClock, make_root


class FakeReconcileClient:
    """按脚本返回远端缺失/多余事件。"""

    def __init__(self, missing: list[str] | None = None, unexpected: list[str] | None = None) -> None:
        self.missing = missing or []
        self.unexpected = unexpected or []
        self.last_ids: list[str] | None = None
        self.last_batch: str | None = None

    def compare(self, sent_ids: list[str], *, batch_id: str | None = None) -> dict:
        self.last_ids = sent_ids
        self.last_batch = batch_id
        return {"missing": self.missing, "unexpected": self.unexpected}


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.root = make_root()
        self.store = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.outbox = Outbox(self.store, self.clock)
        self.reconciler = Reconciler(self.outbox)

    def _publish(self, event_id: str, kind: str = "mash.charged", batch_id: str = "b1") -> dict:
        return self.outbox.publish(kind, {}, batch_id=batch_id, event_id=event_id)

    def test_local_consistent_when_all_sent(self) -> None:
        self._publish("a")
        self._publish("b")
        self._publish("c")
        self.outbox.claim_next()
        for event_id in ("a", "b", "c"):
            self.outbox.mark_sent(event_id)
        report = self.reconciler.local_check()
        self.assertTrue(report.consistent)
        self.assertEqual([], report.gaps)
        self.assertEqual(3, report.local_sent)

    def test_local_check_flags_pending(self) -> None:
        self._publish("a")
        self._publish("b")
        self._publish("c")
        self.outbox.claim_next()
        self.outbox.mark_sent("a")
        self.outbox.mark_sent("b")
        # c 尚未送达：不算缺口，但报告显示有待发事件，整体不一致。
        report = self.reconciler.local_check()
        self.assertFalse(report.consistent)
        self.assertEqual(1, report.local_pending)
        self.assertEqual([], report.gaps)

    def test_local_check_flags_true_gap_when_middle_event_dead(self) -> None:
        self._publish("a")
        middle = self._publish("b")
        self._publish("c")
        self.outbox.claim_next()
        self.outbox.mark_sent("a")
        self.outbox.mark_sent("c")
        # 中间事件进入死信：序号上出现真实缺口（既没送达也不在途）。
        self.outbox.mark_failed("b", "永久拒收", retryable=False)
        report = self.reconciler.local_check()
        self.assertFalse(report.consistent)
        self.assertEqual([middle["outbox_seq"]], report.gaps)
        self.assertEqual(1, report.local_dead)

    def test_dead_letter_is_reported_not_silently_lost(self) -> None:
        self._publish("a", batch_id="b1")
        self.outbox.claim_next()
        self.outbox.mark_failed("a", "400", retryable=False)
        report = self.reconciler.local_check()
        self.assertEqual(1, report.local_dead)
        self.assertIn("死信", report.detail or "")

    def test_remote_check_finds_missing_and_requeue_resends(self) -> None:
        for event_id in ("a", "b", "c"):
            self._publish(event_id)
        self.outbox.claim_next()
        for event_id in ("a", "b", "c"):
            self.outbox.mark_sent(event_id)
        # 远端说 b 实际没收到。
        client = FakeReconcileClient(missing=["b"])
        report = self.reconciler.remote_check(client, batch_id="b1")
        self.assertEqual(["a", "b", "c"], client.last_ids)
        self.assertEqual("b1", client.last_batch)
        self.assertFalse(report.consistent)
        self.assertEqual(["b"], report.missing_remote)
        requeued = self.reconciler.requeue_missing(report)
        self.assertEqual(1, requeued)
        self.assertEqual(
            OutboxStatus.PENDING.value, self.outbox.get("b")["status"]
        )
        self.assertIsNone(self.outbox.get("b")["sent_at"])
        # a/c 不受影响。
        self.assertEqual(OutboxStatus.SENT.value, self.outbox.get("a")["status"])
        self.assertEqual(OutboxStatus.SENT.value, self.outbox.get("c")["status"])

    def test_batch_filter_scopes_reconciliation(self) -> None:
        self._publish("a", batch_id="b1")
        self._publish("x", batch_id="b2")
        self.outbox.claim_next()
        self.outbox.mark_sent("a")
        self.outbox.mark_sent("x")
        report = self.reconciler.local_check(batch_id="b1")
        self.assertEqual(1, report.local_total)
        self.assertTrue(report.consistent)


if __name__ == "__main__":
    unittest.main()
