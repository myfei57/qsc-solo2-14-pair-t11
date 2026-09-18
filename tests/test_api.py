"""控制台 HTTP 接口：路由、错误映射与页面分发。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from .helpers import StepClock, create_batch, make_app


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock())
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

    def test_state_and_pages(self) -> None:
        status, overview = self.call("GET", "/api/state")
        self.assertEqual(200, status)
        self.assertIn("banner", overview)
        status, pages = self.call("GET", "/api/pages")
        self.assertEqual(4, len(pages["pages"]))
        self.assertGreaterEqual(len(pages["routes"]), 50)

    def test_sequence_error_maps_to_conflict(self) -> None:
        batch_id = create_batch(self.app)
        status, payload = self.call(
            "POST", f"/api/batches/{batch_id}/charge", {"grain_kg": 220, "actor": "api"}
        )
        self.assertEqual(409, status)
        self.assertEqual("sequence_violation", payload["error"])

    def test_unknown_route_returns_not_found(self) -> None:
        status, payload = self.call("GET", "/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_static_pages_are_served(self) -> None:
        with urllib.request.urlopen(self.base + "/mash", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertIn("糖化控制", html)
        with urllib.request.urlopen(self.base + "/static/app.js", timeout=10) as response:
            script = response.read().decode("utf-8")
        self.assertIn("initMashPage", script)
