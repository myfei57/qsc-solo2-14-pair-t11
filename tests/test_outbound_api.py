"""外发相关 HTTP 接口：状态、事件列表、对账与死信复活。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from breweryctl.outbound.events import OutboxStatus
from breweryctl.outbound.relay import OutboxRelay
from breweryctl.outbound.transport import SendOutcome, TransportError

from .helpers import StepClock, create_batch, make_app, mash_to_filter


class ScriptedTransport:
    def __init__(self) -> None:
        self.fail = False
        self.seen: set[str] = set()

    def send_batch(self, events: list[dict]) -> SendOutcome:
        if self.fail:
            raise TransportError("offline", retryable=True)
        ids = [str(item["id"]) for item in events]
        self.seen.update(ids)
        return SendOutcome(acknowledged=ids)


class OutboundApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock())
        self.transport = ScriptedTransport()
        self.relay = OutboxRelay(self.app.registry.outbox, self.transport, self.app.registry.clock)
        self.app.server.start()
        host, port = self.app.server.address
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.app.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.app.server.stop()
        self.thread.join(timeout=5)
        self.app.close()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_status_events_flush_and_local_reconcile(self) -> None:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        status, overview = self.call("GET", "/api/outbound/status")
        self.assertEqual(200, status)
        self.assertIn("outbox", overview)
        self.assertGreater(overview["outbox"]["pending"], 0)

        status, payload = self.call(
            "GET", f"/api/outbound/events?batch_id={batch_id}&limit=10"
        )
        self.assertEqual(200, status)
        self.assertEqual(payload["count"], len(payload["events"]))
        self.assertTrue(all(item["batch_id"] == batch_id for item in payload["events"]))

        # 断网时 flush 带回失败标记且事件仍垫在本地。
        self.transport.fail = True
        status, flush = self.call("POST", "/api/outbound/flush")
        self.assertEqual(200, status)
        self.assertGreaterEqual(self.app.registry.outbox.pending_count(), 1)
        self.transport.fail = False
        # 恢复后同步续传追平。
        self.relay.drain()
        status, overview = self.call("GET", "/api/outbound/status")
        self.assertEqual(0, overview["outbox"]["pending"])

        status, payload = self.call("POST", "/api/outbound/reconcile", {"batch_id": batch_id})
        self.assertEqual(200, status)
        self.assertEqual("local", payload["scope"])
        self.assertTrue(payload["report"]["consistent"], payload["report"])

    def test_dead_letter_revive_endpoint(self) -> None:
        batch_id = create_batch(self.app)
        outbox = self.app.registry.outbox
        event = outbox.list_events(batch_id=batch_id)[0]
        outbox.claim_next()
        outbox.mark_failed(event["id"], "永久拒收", retryable=False)
        self.assertEqual(OutboxStatus.DEAD.value, outbox.get(event["id"])["status"])
        status, payload = self.call(
            "POST", f"/api/outbound/events/{event['id']}/revive"
        )
        self.assertEqual(200, status)
        self.assertEqual(OutboxStatus.PENDING.value, payload["event"]["status"])


if __name__ == "__main__":
    unittest.main()
