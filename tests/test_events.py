"""事件外发：发件箱、中继、发布点与对账。"""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from breweryctl.domain.alarms import AlarmCenter
from breweryctl.domain.audit import AuditLog
from breweryctl.events.categories import is_critical_action
from breweryctl.events.hub import EventHub
from breweryctl.events.outbox import OUTBOX_EVENTS, EventOutbox, OutboxStatus
from breweryctl.events.relay import EventRelay
from breweryctl.events.transport import (
    HttpEventTransport,
    RecordingTransport,
    TransportRejected,
    TransportUnavailable,
    envelope_for,
)
from breweryctl.persistence.store import FileStore

from .helpers import StepClock, make_root


def build_hub(
    *,
    transport=None,
    retry_base_s: float = 0.01,
    retry_max_s: float = 0.05,
) -> tuple[FileStore, StepClock, EventHub, EventRelay, RecordingTransport]:
    clock = StepClock()
    store = FileStore(make_root(), clock=clock, fsync=False).open()
    transport = transport if transport is not None else RecordingTransport()
    relay = EventRelay(
        outbox=None,
        transport=transport,
        clock=clock,
        batch_size=8,
        retry_base_s=retry_base_s,
        retry_max_s=retry_max_s,
        stale_claim_s=60,
    )
    hub = EventHub(store, clock, relay=relay)
    relay.outbox = hub.outbox
    return store, clock, hub, relay, transport


class OutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.store = FileStore(make_root(), clock=self.clock, fsync=False).open()
        self.outbox = EventOutbox(self.store, self.clock)

    def test_enqueue_is_idempotent_by_source(self) -> None:
        first = self.outbox.enqueue(
            kind="batch.created", source="audit", source_id="a1", payload={"x": 1}
        )
        second = self.outbox.enqueue(
            kind="batch.created", source="audit", source_id="a1", payload={"x": 2}
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(1, self.outbox.stats()["total"])

    def test_claim_delivery_lifecycle_and_backoff(self) -> None:
        event = self.outbox.enqueue(kind="batch.created", source="audit", source_id="a1", payload={})
        claimed = self.outbox.claim_next(stale_after_s=60, limit=10)
        self.assertEqual(1, len(claimed))
        self.assertEqual(OutboxStatus.SENDING, claimed[0]["status"])
        self.assertEqual(1, claimed[0]["attempts"])
        # 未到期不能重复领取。
        self.assertEqual([], self.outbox.claim_next(stale_after_s=60, limit=10))
        self.clock.advance(61)
        # 超过认领时长可重新领取（中继崩溃恢复），attempts 不重复累计。
        reclaimed = self.outbox.claim_next(stale_after_s=60, limit=10)
        self.assertEqual(1, len(reclaimed))
        self.assertEqual(1, reclaimed[0]["attempts"])
        self.outbox.mark_delivered(str(event["event_id"]))
        self.assertEqual(OutboxStatus.DELIVERED, self.outbox.get(str(event["event_id"]))["status"])
        self.assertEqual([], self.outbox.pending())

    def test_requeue_respects_next_attempt_time(self) -> None:
        event = self.outbox.enqueue(kind="batch.created", source="audit", source_id="a1", payload={})
        claimed = self.outbox.claim_next(stale_after_s=60, limit=10)[0]
        self.clock.advance(1)
        self.outbox.requeue(str(event["event_id"]), "断链", self.clock.now() + __import__("datetime").timedelta(seconds=30))
        self.assertEqual(OutboxStatus.QUEUED, self.outbox.get(str(event["event_id"]))["status"])
        self.assertEqual([], self.outbox.claim_next(stale_after_s=60, limit=10))
        self.clock.advance(30)
        self.assertEqual(1, len(self.outbox.claim_next(stale_after_s=60, limit=10)))

    def test_dead_letter_and_replay(self) -> None:
        event = self.outbox.enqueue(kind="bad", source="audit", source_id="a1", payload={})
        self.outbox.claim_next(stale_after_s=60, limit=10)
        self.outbox.mark_dead(str(event["event_id"]), "400 bad payload")
        self.assertEqual(1, self.outbox.stats()["dead"])
        self.assertEqual([], self.outbox.claim_next(stale_after_s=60, limit=10))
        self.assertEqual(1, self.outbox.replay_dead(str(event["event_id"])))
        self.assertEqual(1, len(self.outbox.claim_next(stale_after_s=60, limit=10)))

    def test_sequences_follow_occurrence_order(self) -> None:
        first = self.outbox.enqueue(kind="a", source="audit", source_id="a1", payload={})
        self.clock.advance(5)
        second = self.outbox.enqueue(kind="b", source="audit", source_id="a2", payload={})
        self.outbox.assign_sequences()
        self.assertEqual(1, self.outbox.get(str(first["event_id"]))["sequence"])
        self.assertEqual(2, self.outbox.get(str(second["event_id"]))["sequence"])

    def test_state_survives_reopen(self) -> None:
        root = self.store.data_dir
        self.outbox.enqueue(kind="a", source="audit", source_id="a1", payload={"v": 1})
        reopened_store = FileStore(root, clock=self.clock, fsync=False).open()
        reopened = EventOutbox(reopened_store, self.clock)
        self.assertEqual(1, reopened.stats()["total"])
        self.assertEqual({"v": 1}, reopened.list_events()[0]["payload"])


class RelayTest(unittest.TestCase):
    def test_delivers_queued_events(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        hub.publish_raw("batch.created", {"code": "B0001"})
        count = relay.pump()
        self.assertEqual(1, count)
        self.assertEqual(1, len(transport.delivered))
        envelope = transport.delivered[0]
        self.assertEqual("batch.created", envelope["kind"])
        self.assertIn("event_id", envelope)
        self.assertEqual("1.0", envelope["schema"])

    def test_outage_buffers_and_resumes_without_gaps(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        hub.publish_raw("batch.created", {"n": 1})
        hub.publish_raw("mash.charged", {"n": 2})
        transport.fail_next(1)  # 模拟断链一轮
        relay.pump()
        self.assertEqual([], transport.delivered)
        statuses = {item["kind"]: item["status"] for item in hub.outbox.list_events()}
        # 队首失败时，队尾退回排队而不是超车先发，保证顺序不乱。
        self.assertEqual(OutboxStatus.QUEUED, statuses["batch.created"])
        self.assertEqual(OutboxStatus.QUEUED, statuses["mash.charged"])
        # 退避未到期不重试。
        self.assertEqual(0, relay.pump())
        self.assertEqual([], transport.delivered)
        # 恢复链路并推进过退避时刻，按发生顺序补送。
        clock.advance(0.02)
        relay.pump()
        self.assertEqual(["batch.created", "mash.charged"], [item["kind"] for item in transport.delivered])
        self.assertEqual(0, len(hub.outbox.pending()))

    def test_rejected_goes_dead_and_does_not_block_later_events(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        transport.reject_kind("alarm.raised")
        hub.publish_raw("alarm.raised", {})
        clock.advance(1)
        hub.publish_raw("batch.completed", {})
        relay.pump()
        stats = hub.outbox.stats()
        self.assertEqual(1, stats["dead"])
        self.assertEqual(1, stats["delivered"])
        delivered_kinds = [item["kind"] for item in transport.delivered]
        self.assertEqual(["batch.completed"], delivered_kinds)

    def test_duplicate_delivery_is_idempotent(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        hub.publish_raw("batch.created", {})
        relay.pump()
        # 对端已收过同一幂等键时按成功处理；重放已送达事件不会产生第二条。
        envelope = envelope_for(hub.outbox.list_events(status=OutboxStatus.DELIVERED)[0])
        ok, blocked = relay._deliver_one(envelope)
        self.assertTrue(ok)
        self.assertIsNone(blocked)
        self.assertEqual(1, len(transport.delivered))


class PublishingPointTest(unittest.TestCase):
    def test_audit_sink_publishes_only_critical_actions(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        audit = AuditLog(store, clock)
        audit.set_sink(hub.publish_audit)
        audit.record("br-1", "bt-1", "tester", "batch.created", {"code": "B1"})
        audit.record("br-1", "bt-1", "tester", "telemetry.reading", {"v": 1})
        relay.pump()
        kinds = [item["kind"] for item in transport.delivered]
        self.assertEqual(["batch.created"], kinds)

    def test_alarm_lifecycle_publishes_each_transition(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        alarms = AlarmCenter(store, clock)
        alarms.set_sink(hub.publish_alarm)
        alarm = alarms.raise_alarm("br-1", "probe:p1", "critical", "x", "过热")
        alarms.acknowledge(str(alarm["id"]), "op")
        alarms.resolve(str(alarm["id"]), "op", "降温")
        relay.pump()
        kinds = [item["kind"] for item in transport.delivered]
        self.assertEqual(["alarm.raised", "alarm.acknowledged", "alarm.resolved"], kinds)

    def test_repeated_alarm_emits_separate_event(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        alarms = AlarmCenter(store, clock)
        alarms.set_sink(hub.publish_alarm)
        first = alarms.raise_alarm("br-1", "probe:p1", "warning", "x", "偏离")
        alarms.raise_alarm("br-1", "probe:p1", "warning", "x", "偏离")
        relay.pump()
        kinds = sorted(item["kind"] for item in transport.delivered)
        self.assertEqual(["alarm.raised", "alarm.repeated"], kinds)
        self.assertIsNone(hub.publish_alarm(first, "raised"), "同一生命周期不重复入箱")

    def test_sink_failure_does_not_break_business_operation(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        audit = AuditLog(store, clock)

        def broken_sink(entry: object) -> None:
            raise RuntimeError("发布点挂了")

        audit.set_sink(broken_sink)
        entry = audit.record("br-1", None, "tester", "batch.created", {})
        self.assertEqual("audit", entry["id"].split("-")[0])


class ReconcileTest(unittest.TestCase):
    def test_backfills_missing_events_from_local_records(self) -> None:
        clock = StepClock()
        store = FileStore(make_root(), clock=clock, fsync=False).open()
        audit = AuditLog(store, clock)
        alarms = AlarmCenter(store, clock)
        # 发布点尚未接入时已经产生的本地关键事实。
        audit.record("br-1", "bt-1", "tester", "batch.created", {})
        audit.record("br-1", "bt-1", "tester", "telemetry.reading", {})
        alarm = alarms.raise_alarm("br-1", "probe:p1", "critical", "x", "过热")
        alarms.acknowledge(str(alarm["id"]), "op")

        relay = EventRelay(None, RecordingTransport(), clock)
        hub = EventHub(store, clock, relay=relay)
        report = hub.reconcile()
        self.assertEqual(3, report["backfilled"])  # 1 条审计 + raised + acknowledged
        self.assertEqual(3, report["matched"])
        self.assertEqual([], report["missing_in_outbox"])
        # 再次对账是幂等的，不产生新事件。
        second = hub.reconcile()
        self.assertEqual(0, second["backfilled"])
        self.assertEqual(3, second["outbox_total"])

    def test_orphan_detection(self) -> None:
        store, clock, hub, relay, transport = build_hub()
        hub.publish_raw("custom.kind", {})
        report = hub.match_report()
        # manual 来源不参与审计/告警对账，不算孤儿。
        self.assertEqual([], report["orphan_events"])
        hub.outbox.enqueue(kind="ghost", source="audit", source_id="audit-missing", payload={})
        report = hub.match_report()
        self.assertEqual("audit-missing", report["orphan_events"][0]["source_id"])

    def test_recovered_after_restart_flushes_buffer(self) -> None:
        # 第一次进程：没有端点，事件只入箱。
        clock = StepClock()
        root = make_root()
        store = FileStore(root, clock=clock, fsync=False).open()
        hub_offline = EventHub(store, clock, relay=None)
        audit = AuditLog(store, clock)
        audit.set_sink(hub_offline.publish_audit)
        audit.record("br-1", "bt-1", "tester", "batch.created", {})
        audit.record("br-1", "bt-1", "tester", "mash.charged", {})
        store.close()

        # 第二次进程：端点恢复，开箱即从本地存储恢复待发事件并送出去。
        transport = RecordingTransport()
        store2 = FileStore(root, clock=clock, fsync=False).open()
        relay = EventRelay(None, transport, clock)
        hub2 = EventHub(store2, clock, relay=relay)
        relay.outbox = hub2.outbox
        hub2.reconcile()
        relay.pump()
        self.assertEqual(["batch.created", "mash.charged"], [item["kind"] for item in transport.delivered])
        store2.close()


class HttpTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.received: list[dict[str, str]] = []
        self.duplicate_keys: set[str] = set()
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                import json

                body = json.loads(self.rfile.read(length).decode("utf-8"))
                key = self.headers.get("Idempotency-Key", "")
                with outer.lock:
                    if key in outer.duplicate_keys:
                        self.send_response(409)
                        self.end_headers()
                        return
                    outer.duplicate_keys.add(key)
                    outer.received.append({"key": key, "body": body})
                if body.get("kind") == "reject.me":
                    self.send_response(400)
                    self.end_headers()
                    return
                self.send_response(202)
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.transport = HttpEventTransport(f"http://127.0.0.1:{self.port}/events", timeout_s=3.0)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_post_envelope_with_idempotency_key(self) -> None:
        envelope = {"event_id": "evt-1", "kind": "batch.created"}
        self.transport.deliver(envelope)
        self.transport.deliver(envelope)  # 重试：对端 409，去重命中
        self.assertEqual(1, len(self.received))
        self.assertEqual("evt-1", self.received[0]["key"])

    def test_400_is_rejected_500_style_unavailable(self) -> None:
        with self.assertRaises(TransportRejected):
            self.transport.deliver({"event_id": "evt-2", "kind": "reject.me"})

    def test_unreachable_endpoint_is_unavailable(self) -> None:
        transport = HttpEventTransport("http://127.0.0.1:1/events", timeout_s=0.5)
        with self.assertRaises(TransportUnavailable):
            transport.deliver({"event_id": "evt-3", "kind": "x"})


class CategoriesTest(unittest.TestCase):
    def test_critical_filter(self) -> None:
        self.assertTrue(is_critical_action("batch.created"))
        self.assertTrue(is_critical_action("maintenance.clean_finished"))
        self.assertTrue(is_critical_action("control.valve_operated"))
        self.assertFalse(is_critical_action("telemetry.reading"))
        self.assertFalse(is_critical_action("telemetry.calibrated"))


if __name__ == "__main__":
    unittest.main()
