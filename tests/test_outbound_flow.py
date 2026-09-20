"""端到端：关键工艺动作进箱、断网续传、与本地记录对账一致。"""

from __future__ import annotations

import unittest

from breweryctl.outbound.events import OutboxStatus
from breweryctl.outbound.relay import OutboxRelay
from breweryctl.outbound.transport import SendOutcome, TransportError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    make_app,
    mash_to_filter,
)


class ScriptedTransport:
    """先断网 N 轮再恢复，并记录全部投递。"""

    def __init__(self, fail_rounds: int = 0) -> None:
        self.fail_rounds = fail_rounds
        self.deliveries: list[dict] = []
        self.acknowledged_ids: set[str] = set()

    def send_batch(self, events: list[dict]) -> SendOutcome:
        if self.fail_rounds > 0:
            self.fail_rounds -= 1
            raise TransportError("断网", retryable=True)
        ids = [str(item["id"]) for item in events]
        # 对端按 event_id 幂等：重复投递只生效一次，但照常确认。
        for event in events:
            if event["id"] not in self.acknowledged_ids:
                self.deliveries.append(event)
                self.acknowledged_ids.add(event["id"])
        return SendOutcome(acknowledged=ids)


class OutboundFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.registry = self.app.registry
        self.transport = ScriptedTransport(fail_rounds=0)
        self.relay = OutboxRelay(self.registry.outbox, self.transport, self.clock)

    def test_critical_actions_enqueued_and_match_local_audit(self) -> None:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        # 每个关键审计动作对应一条发件事件，且 id 与审计 id 一致。
        pending = self.registry.outbox.list_events(batch_id=batch_id)
        audit_ids = {
            entry["id"]
            for entry in self.registry.audit.history(batch_id=batch_id, limit=0)
        }
        event_ids = {item["id"] for item in pending}
        self.assertTrue(event_ids, "关键工艺动作应产生外发事件")
        self.assertTrue(event_ids.issubset(audit_ids))
        # 普通温度采样等非关键动作不应进箱。
        kinds = {item["kind"] for item in pending}
        self.assertIn("mash.charged", kinds)
        self.assertIn("boil.ignited", kinds)
        self.assertIn("hop.added", kinds)
        self.assertNotIn("telemetry.reading", kinds)

    def test_outage_buffers_and_replay_matches_local_records(self) -> None:
        # 断网跑完糖化关键节点。
        self.transport.fail_rounds = 5
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        buffered = self.registry.outbox.list_events(batch_id=batch_id)
        self.assertGreater(len(buffered), 0)
        self.assertTrue(
            all(item["status"] == OutboxStatus.PENDING.value for item in buffered)
        )
        # 恢复网络：同步追平。
        self.transport.fail_rounds = 0
        result = self.relay.drain()
        self.assertEqual(0, result.failed)
        stats = self.registry.outbox.stats()
        self.assertEqual(0, stats["pending"])
        self.assertEqual(0, stats["in_flight"])
        # 对端收到的去重后事件集合 == 本地已送达集合。
        local_sent_ids = {
            item["id"]
            for item in self.registry.outbox.list_events(
                status=OutboxStatus.SENT.value, batch_id=batch_id
            )
        }
        self.assertEqual(local_sent_ids, self.transport.acknowledged_ids)
        # 对账本地一致。
        report = self.registry.reconciler.local_check(batch_id=batch_id)
        self.assertTrue(report.consistent, report.as_dict())

    def test_alarm_events_are_emitted(self) -> None:
        alarm = self.registry.alarms.raise_alarm(
            brewery_id="ns-test",
            source="probe:p1",
            severity="critical",
            code="probe_fault",
            message="探头故障",
        )
        events = self.registry.outbox.list_events()
        kinds = [item["kind"] for item in events if item["payload"].get("alarm_id") == alarm["id"]]
        self.assertEqual(["alarm.raised"], kinds)
        acked = self.registry.alarms.acknowledge(alarm["id"], "op")
        resolved = self.registry.alarms.resolve(alarm["id"], "op", "复位")
        self.assertEqual("resolved", resolved["status"])
        lifecycle = [
            item["kind"]
            for item in self.registry.outbox.list_events()
            if str(item.get("payload", {}).get("alarm_id")) == alarm["id"]
        ]
        self.assertEqual(["alarm.raised", "alarm.acknowledged", "alarm.resolved"], lifecycle)


if __name__ == "__main__":
    unittest.main()
