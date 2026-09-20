"""基于快照与追加日志的文件型存储。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.clock import Clock, SystemClock, format_moment
from ..core.errors import NotFoundError, PersistenceError
from .journal import Journal
from .locks import KeyedLocks
from .snapshot import quarantine, read_snapshot, snapshot_size, write_snapshot

SNAPSHOT_NAME = "state.json"
JOURNAL_NAME = "journal.jsonl"


class Collection:
    """快照内的一个命名集合。"""

    def __init__(self, store: "FileStore", name: str) -> None:
        self._store = store
        self.name = name

    def get(self, key: str) -> dict[str, Any] | None:
        """返回文档副本，不存在时返回 ``None``。"""

        document = self._store._collection_data(self.name).get(key)
        if document is None:
            return None
        return _copy(document)

    def require(self, key: str, label: str | None = None) -> dict[str, Any]:
        """返回文档副本，不存在时抛 :class:`NotFoundError`。"""

        document = self.get(key)
        if document is None:
            raise NotFoundError(f"{label or self.name} 不存在", key=key, collection=self.name)
        return document

    def put(self, key: str, document: dict[str, Any]) -> dict[str, Any]:
        """写入文档并立即落盘。"""

        return self._store._put(self.name, key, document)

    def update(self, key: str, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """在对象锁内读改写同一文档。"""

        with self._store.locks.guard(f"{self.name}:{key}"):
            current = self.require(key)
            updated = mutator(current)
            if not isinstance(updated, dict):
                raise PersistenceError("更新回调必须返回文档", collection=self.name, key=key)
            return self._store._put(self.name, key, updated)

    def delete(self, key: str) -> bool:
        """删除文档，返回是否确实删除。"""

        return self._store._delete(self.name, key)

    def all(self) -> list[dict[str, Any]]:
        """返回集合内全部文档副本。"""

        return [_copy(item) for item in self._store._collection_data(self.name).values()]

    def keys(self) -> list[str]:
        """返回排序后的键列表。"""

        return sorted(self._store._collection_data(self.name).keys())

    def count(self) -> int:
        """返回文档数量。"""

        return len(self._store._collection_data(self.name))

    def find(self, predicate: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        """按条件筛选文档。"""

        return [item for item in self.all() if predicate(item)]


class FileStore:
    """串行化写入 + 快照恢复的本地存储。"""

    def __init__(
        self,
        data_dir: Path | str,
        clock: Clock | None = None,
        fsync: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.clock = clock or SystemClock()
        self.fsync = fsync
        self.locks = KeyedLocks()
        self._write_lock = threading.RLock()
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}
        self._meta: dict[str, Any] = {}
        self._sequence = 0
        self._snapshot_path = self.data_dir / SNAPSHOT_NAME
        self._journal: Journal | None = None
        self._opened_at: str | None = None
        self._quarantined: str | None = None
        self._replayed = 0
        self._writes = 0

    @property
    def journal(self) -> Journal:
        """返回已打开的日志对象。"""

        if self._journal is None:
            raise PersistenceError("存储尚未打开")
        return self._journal

    def open(self) -> "FileStore":
        """加载快照并重放日志。"""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._journal = Journal(self.data_dir / JOURNAL_NAME, self.clock, fsync=self.fsync)
        try:
            document = read_snapshot(self._snapshot_path)
        except PersistenceError as exc:
            self._quarantined = quarantine(self._snapshot_path, exc.message)
            document = None
        document = document or {"seq": 0, "collections": {}, "meta": {}}
        raw_collections = document.get("collections")
        if not isinstance(raw_collections, dict):
            raw_collections = {}
        self._collections = {}
        for name, items in raw_collections.items():
            if not isinstance(items, dict):
                continue
            self._collections[name] = {
                str(key): value for key, value in items.items() if isinstance(value, dict)
            }
        meta = document.get("meta")
        self._meta = dict(meta) if isinstance(meta, dict) else {}
        self._sequence = int(document.get("seq", 0) or 0)
        self._replayed = 0
        for event in self.journal.iter_events():
            sequence = int(event.get("seq", 0))
            if sequence <= self._sequence:
                continue
            self._apply_event(event)
            self._replayed += 1
        self._opened_at = format_moment(self.clock.now())
        return self

    def close(self) -> None:
        """关闭前写出最终快照。"""

        if self._journal is None:
            return
        self.snapshot_now()

    def collection(self, name: str) -> Collection:
        """取得命名集合。"""

        if not name or not isinstance(name, str):
            raise PersistenceError("集合名不合法", collection=name)
        return Collection(self, name)

    def append_event(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """只写日志、不改状态，用于记录已经落盘的事实。"""

        with self._write_lock:
            self._sequence += 1
            return self.journal.append(self._sequence, kind, payload)

    def next_sequence(self) -> int:
        """在写锁内分配并落库一个新的全局单调序号。

        发件箱用它给待发事件排序：序号与业务动作同源自增序列，
        断网恢复后续传顺序与本地提交顺序一致。
        """

        with self._write_lock:
            self._sequence += 1
            self.journal.append(
                self._sequence,
                "sequence.tick",
                {"purpose": "outbox_order"},
            )
            return self._sequence

    def increment_meta_counter(self, key: str) -> int:
        """在同一写锁、同一次快照内自增一个元数据计数器。"""

        with self._write_lock:
            current = int(self._meta.get(key, 0) or 0) + 1
            self._meta[key] = current
            self._write_snapshot()
            return current

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回最近的日志事件。"""

        return self.journal.tail(limit)

    def snapshot_now(self) -> dict[str, Any]:
        """立即写出快照。"""

        with self._write_lock:
            return self._write_snapshot()

    def set_meta(self, key: str, value: Any) -> None:
        """写入少量全局元数据并落盘。"""

        with self._write_lock:
            self._meta[key] = value
            self._write_snapshot()

    def meta(self, key: str, default: Any = None) -> Any:
        """读取全局元数据。"""

        return self._meta.get(key, default)

    def stats(self) -> dict[str, Any]:
        """返回存储运行指标。"""

        return {
            "data_dir": str(self.data_dir.resolve()),
            "snapshot_path": str(self._snapshot_path.resolve()),
            "journal_path": str((self.data_dir / JOURNAL_NAME).resolve()),
            "sequence": self._sequence,
            "replayed_events": self._replayed,
            "writes": self._writes,
            "collections": {name: len(items) for name, items in sorted(self._collections.items())},
            "snapshot_bytes": snapshot_size(self._snapshot_path),
            "journal_bytes": self.journal.size_bytes(),
            "journal_max_seq": self.journal.max_sequence(),
            "opened_at": self._opened_at,
            "lock_keys": self.locks.active_keys(),
            "quarantined_snapshot": self._quarantined,
        }

    def _collection_data(self, name: str) -> dict[str, dict[str, Any]]:
        return self._collections.setdefault(name, {})

    def _put(self, collection: str, key: str, document: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise PersistenceError("只能写入对象文档", collection=collection, key=key)
        with self._write_lock:
            self._ensure_collection(collection)
            stored = _copy(document)
            self._collections[collection][key] = stored
            self._sequence += 1
            self.journal.append(
                self._sequence,
                "collection.put",
                {"collection": collection, "key": key, "document": stored},
            )
            self._write_snapshot()
            self._writes += 1
            return _copy(stored)

    def _delete(self, collection: str, key: str) -> bool:
        with self._write_lock:
            self._ensure_collection(collection)
            if key not in self._collections[collection]:
                return False
            del self._collections[collection][key]
            self._sequence += 1
            self.journal.append(
                self._sequence,
                "collection.delete",
                {"collection": collection, "key": key},
            )
            self._write_snapshot()
            self._writes += 1
            return True

    def _ensure_collection(self, name: str) -> None:
        self._collections.setdefault(name, {})

    def _apply_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if kind == "collection.put":
            collection = str(payload.get("collection", ""))
            key = payload.get("key")
            document = payload.get("document")
            if collection and isinstance(key, str) and isinstance(document, dict):
                self._ensure_collection(collection)
                self._collections[collection][key] = _copy(document)
        elif kind == "collection.delete":
            collection = str(payload.get("collection", ""))
            key = payload.get("key")
            if collection in self._collections and isinstance(key, str):
                self._collections[collection].pop(key, None)
        self._sequence = max(self._sequence, int(event.get("seq", 0)))

    def _write_snapshot(self) -> dict[str, Any]:
        document = {
            "seq": self._sequence,
            "meta": self._meta,
            "collections": self._collections,
        }
        write_snapshot(self._snapshot_path, document, fsync=self.fsync)
        return document


def _copy(document: dict[str, Any]) -> dict[str, Any]:
    """轻量深拷贝，避免调用方持有内部引用。"""

    copied: dict[str, Any] = {}
    for key, value in document.items():
        copied[key] = _copy_value(value)
    return copied


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def merge_documents(base: dict[str, Any], patch: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """在副本上合并字段，供服务层构造更新。"""

    merged = _copy(base)
    for key, value in patch:
        merged[key] = _copy_value(value)
    return merged
