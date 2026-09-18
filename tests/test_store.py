"""持久化层：原子快照、追加日志与崩溃重放。"""

from __future__ import annotations

import json
import unittest

from breweryctl.core.errors import PersistenceError
from breweryctl.persistence.snapshot import quarantine, read_snapshot, write_snapshot
from breweryctl.persistence.store import FileStore

from .helpers import StepClock, make_root


class FileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.root = make_root()
        self.store = FileStore(self.root, clock=self.clock, fsync=False).open()

    def test_put_and_read_back(self) -> None:
        collection = self.store.collection("brew_examples")
        collection.put("a", {"value": 1})
        updated = collection.update("a", lambda document: {**document, "value": 2})
        self.assertEqual(2, updated["value"])
        self.assertEqual([{"value": 2}], collection.all())
        self.assertEqual(["a"], collection.keys())
        self.assertEqual(1, collection.count())
        self.assertTrue(self.store.stats()["sequence"] >= 2)

    def test_snapshot_replay_restores_state(self) -> None:
        self.store.collection("brew_examples").put("a", {"value": 1})
        self.store.collection("brew_examples").put("b", {"value": 3})
        reopened = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.assertEqual(2, reopened.collection("brew_examples").count())
        # 快照被人为回退后，日志重放应把序号更大的事件补回来。
        snapshot = read_snapshot(self.root / "state.json")
        snapshot["seq"] = 0
        snapshot["collections"] = {}
        write_snapshot(self.root / "state.json", snapshot, fsync=False)
        replayed = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.assertEqual(2, replayed.collection("brew_examples").count())
        self.assertGreaterEqual(replayed.stats()["replayed_events"], 2)

    def test_delete_is_journaled(self) -> None:
        collection = self.store.collection("brew_examples")
        collection.put("a", {"value": 1})
        self.assertTrue(collection.delete("a"))
        self.assertFalse(collection.delete("a"))
        reopened = FileStore(self.root, clock=self.clock, fsync=False).open()
        self.assertEqual(0, reopened.collection("brew_examples").count())

    def test_quarantine_corrupt_snapshot(self) -> None:
        path = self.root / "state.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PersistenceError):
            read_snapshot(path)
        destination = quarantine(path, "corrupt")
        self.assertTrue(destination.endswith(".bad"))
        self.assertFalse(path.exists())

    def test_snapshot_document_contains_meta(self) -> None:
        self.store.set_meta("booted_at", "2026-01-01T08:00:00+00:00")
        document = self.store.snapshot_now()
        self.assertEqual("2026-01-01T08:00:00+00:00", document["meta"]["booted_at"])
        blob = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        self.assertIn("collections", blob)
