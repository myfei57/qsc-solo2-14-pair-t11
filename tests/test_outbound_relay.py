"""中继：断网垫发、恢复续传、去重确认与死信不阻塞。"""

from __future__ import annotations

import threading
import unittest

from breweryctl.outbound.events import OutboxStatus
from breweryctl.outbound.outbox import Outbox
from breweryctl.outbound.relay import OutboxRelay
from breweryctl.outbound.transport import SendOutcome, TransportError
from breweryctl.persistence.store import FileStore

from .helpers import StepClock, make_root


class FakeTransport:
    """记录收到的批次，可按脚本切换"断网/恢复"。"""

    def __init__(self) -> None:
        self.received: list[list[str]] = []
        self.seen_event_ids: set[str] = set()
        self.fail_times = 0
        self.non_retryable = False
        self.lock = threading.Lock()

    def send_batch(self, events: list[dict]) -> SendOutcome:
        with self.lock:
            ids = [str(item["id"]) for item in events]
            self.received.append(ids)
            if self.non_retryable:
                raise TransportError("链路被拒", retryable=False)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise TransportError("链路中断", retryable=True)
            self.seen_event_ids.update(ids)
            return SendOutcome(acknowledged=ids)


class OutboxRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.root = make_root()
        self.store = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.outbox = Outbox(self.store, self.clock)
        self.transport = FakeTransport()
        self.relay = OutboxRelay(self.outbox, self.transport, self.clock)

    def test_happy_path_sends_and_confirms(self) -> None:
        for index in range(3):
            self.outbox.publish("mash.charged", {"i": index}, event_id=f"e{index}")
        result = self.relay.drain()
        self.assertEqual(3, result.sent)
        self.assertEqual(0, result.failed)
        self.assertEqual({"e0", "e1", "e2"}, self.transport.seen_event_ids)
        self.assertEqual(3, self.outbox.stats()["sent"])

    def test_outage_buffers_then_resumes_in_order_with_duplicate_safe(self) -> None:
        # 先发两条成功。
        self.outbox.publish("mash.charged", {}, event_id="e1")
        self.outbox.publish("boil.ignited", {}, event_id="e2")
        self.relay.drain()
        # 断网：新事件全部垫着。
        self.transport.fail_times = 1
        self.outbox.publish("hop.added", {}, event_id="e3")
        self.outbox.publish("mash.resting", {}, event_id="e4")
        failed_round = self.relay.run_once()
        self.assertTrue(failed_round.retried)
        self.assertEqual(2, failed_round.failed)
        # 断网期间继续产生事件，仍只是本地累积。
        self.outbox.publish("alarm.raised", {}, event_id="e5")
        pending = self.outbox.list_events(status=OutboxStatus.PENDING.value)
        self.assertEqual({"e3", "e4", "e5"}, {item["id"] for item in pending})
        # 恢复：按序追平；e3/e4 是第二次投递（对端幂等去重）。
        result = self.relay.drain()
        self.assertEqual(3, result.sent)
        sent_order = [event_id for batch in self.transport.received for event_id in batch]
        self.assertEqual(["e3", "e4", "e5"], sent_order[-3:])
        self.assertEqual(5, self.outbox.stats()["sent"])

    def test_retry_is_deduplicated_on_receiver(self) -> None:
        self.transport.fail_times = 1  # 第一批失败一次
        self.outbox.publish("mash.charged", {}, event_id="dup1")
        first = self.relay.run_once()
        self.assertEqual(1, first.failed)
        second = self.relay.run_once()
        self.assertEqual(1, second.sent)
        # 同一 event_id 出现两次（首次失败可能其实已到对端），对端按 id 去重。
        deliveries = [event_id for batch in self.transport.received for event_id in batch]
        self.assertEqual(["dup1", "dup1"], deliveries)
        self.assertEqual({"dup1"}, self.transport.seen_event_ids)

    def test_dead_letter_does_not_block_later_events(self) -> None:
        # 整批遭遇永久性链路错误：达到最大尝试次数的进死信，其余回 pending。
        self.transport.non_retryable = True
        self.outbox.publish("mash.charged", {}, event_id="dead", max_attempts=1)
        self.outbox.publish("boil.ignited", {}, event_id="alive")
        first = self.relay.run_once()
        # 整批同时失败：死信事件进 dead，存活事件回到 pending。
        self.assertEqual(2, first.failed)
        self.assertEqual(1, first.dead)
        self.assertEqual(OutboxStatus.DEAD.value, self.outbox.get("dead")["status"])
        # 死信不应阻塞后续事件。
        self.transport.non_retryable = False
        result = self.relay.drain()
        self.assertEqual(1, result.sent)
        self.assertEqual("sent", self.outbox.get("alive")["status"])
        self.assertEqual(1, self.outbox.stats()["dead"])

    def test_background_thread_flushes_after_kick(self) -> None:
        relay = OutboxRelay(
            self.outbox, self.transport, self.clock, idle_interval_sec=0.05
        )
        relay.start()
        try:
            self.outbox.publish("mash.charged", {}, event_id="bg1")
            relay.kick()
            self._wait_until(lambda: self.outbox.stats()["sent"] == 1)
        finally:
            relay.stop()
        self.assertEqual(1, self.outbox.stats()["sent"])

    @staticmethod
    def _wait_until(predicate, timeout: float = 2.0) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("条件在超时前未满足")


if __name__ == "__main__":
    unittest.main()
