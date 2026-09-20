"""发件箱核心：登记、幂等、领取、失败退避与死信。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, ValidationError
from breweryctl.outbound.events import OutboxStatus, is_critical
from breweryctl.outbound.outbox import Outbox
from breweryctl.persistence.store import FileStore

from .helpers import StepClock, make_root


class OutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.root = make_root()
        self.store = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.outbox = Outbox(self.store, self.clock)

    def test_publish_assigns_monotonic_seq_and_pending_status(self) -> None:
        first = self.outbox.publish("mash.charged", {"grain_kg": 220.0}, batch_id="b1")
        second = self.outbox.publish("boil.ignited", {}, batch_id="b1")
        self.assertEqual(OutboxStatus.PENDING.value, first["status"])
        self.assertLess(first["source_seq"], second["source_seq"])

    def test_non_critical_event_rejected(self) -> None:
        self.assertFalse(is_critical("telemetry.reading"))
        with self.assertRaises(ValidationError):
            self.outbox.publish("telemetry.reading", {"value_c": 65.0})

    def test_duplicate_event_id_is_idempotent(self) -> None:
        first = self.outbox.publish("hop.added", {"position": 1}, event_id="audit-x")
        second = self.outbox.publish("hop.added", {"position": 1}, event_id="audit-x")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, self.outbox.stats()["total"])

    def test_claim_and_mark_sent(self) -> None:
        self.outbox.publish("mash.charged", {}, event_id="e1")
        self.outbox.publish("boil.ignited", {}, event_id="e2")
        claimed = self.outbox.claim_next(limit=10)
        self.assertEqual(["e1", "e2"], [item["id"] for item in claimed])
        self.assertTrue(all(item["status"] == OutboxStatus.IN_FLIGHT.value for item in claimed))
        # 已领取的不会再被领出。
        self.assertEqual([], self.outbox.claim_next())
        self.outbox.mark_sent("e1")
        self.assertEqual("sent", self.outbox.get("e1")["status"])
        stats = self.outbox.stats()
        self.assertEqual(1, stats["sent"])
        self.assertEqual(1, stats["in_flight"])

    def test_failure_returns_to_pending_then_dead_letter(self) -> None:
        event = self.outbox.publish("mash.charged", {}, event_id="retry", max_attempts=3)
        for expected_status in ("pending", "pending"):
            claimed = self.outbox.claim_next()
            failed = self.outbox.mark_failed("retry", "connection reset", retryable=True)
            self.assertEqual(expected_status, failed["status"])
            self.assertEqual(event["id"], failed["id"])
        # 第三次失败：进入死信，不再续传。
        self.outbox.claim_next()
        dead = self.outbox.mark_failed("retry", "still down", retryable=True)
        self.assertEqual(OutboxStatus.DEAD.value, dead["status"])
        self.assertEqual(0, len(self.outbox.claim_next()))
        # 死信不能直接确认。
        with self.assertRaises(ConflictError):
            self.outbox.mark_sent("retry")
        revived = self.outbox.revive("retry")
        self.assertEqual(OutboxStatus.PENDING.value, revived["status"])
        self.assertEqual(0, revived["attempts"])

    def test_non_retryable_error_goes_straight_to_dead(self) -> None:
        self.outbox.publish("mash.charged", {}, event_id="bad")
        self.outbox.claim_next()
        # 单事件级的永久性拒收（如对端明确返回该事件非法）直接进死信。
        dead = self.outbox.mark_failed("bad", "HTTP 400 bad payload", retryable=False)
        self.assertEqual(OutboxStatus.DEAD.value, dead["status"])

    def test_stale_in_flight_is_reclaimed(self) -> None:
        self.outbox.publish("mash.charged", {}, event_id="stuck")
        self.outbox.claim_next(ttl_min=10.0)
        # 未超过 TTL：仍在途。
        self.assertEqual(0, self.outbox.reclaim_stale(ttl_min=10.0))
        self.clock.advance(11)
        self.assertEqual(1, self.outbox.reclaim_stale(ttl_min=10.0))
        reclaimed = self.outbox.claim_next()
        self.assertEqual(["stuck"], [item["id"] for item in reclaimed])

    def test_events_survive_reopen_in_order(self) -> None:
        self.outbox.publish("mash.charged", {}, event_id="a", batch_id="b1")
        self.outbox.publish("boil.ignited", {}, event_id="b", batch_id="b1")
        self.outbox.claim_next()
        self.outbox.mark_sent("a")
        self.outbox.mark_failed("b", "offline", retryable=True)
        reopened_store = FileStore(self.root, clock=self.clock, fsync=False).open()
        reopened = Outbox(reopened_store, self.clock)
        pending = reopened.list_events(status=OutboxStatus.PENDING.value)
        self.assertEqual(["b"], [item["id"] for item in pending])
        self.assertEqual(1, reopened.stats()["sent"])
        # 序号顺序保持。
        ordered = reopened.list_events()
        self.assertEqual(["a", "b"], [item["id"] for item in ordered])


if __name__ == "__main__":
    unittest.main()
