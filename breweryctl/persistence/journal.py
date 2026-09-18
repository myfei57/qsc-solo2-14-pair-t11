"""追加型 JSONL 日志。

日志只负责记录事实，快照负责表达当前状态。启动时先读快照，再重放序号更大的
日志事件，保证崩溃后不会丢失已经确认落盘的业务动作。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from ..core.clock import Clock, format_moment
from ..core.errors import PersistenceError


class Journal:
    """线程安全的追加日志。"""

    def __init__(self, path: Path, clock: Clock, fsync: bool = True) -> None:
        self.path = Path(path)
        self.clock = clock
        self.fsync = fsync
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, sequence: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """写入一条事件并返回完整事件对象。"""

        event = {
            "seq": int(sequence),
            "kind": kind,
            "at": format_moment(self.clock.now()),
            "payload": payload,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    if self.fsync:
                        os.fsync(handle.fileno())
            except OSError as exc:
                raise PersistenceError("追加日志写入失败", path=str(self.path)) from exc
        return event

    def all(self) -> list[dict[str, Any]]:
        """读取全部事件。"""

        return list(self.iter_events())

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """逐行解析事件，跳过空行。"""

        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        yield json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise PersistenceError(
                            "日志行不是合法 JSON", path=str(self.path), line=number
                        ) from exc
        except OSError as exc:
            raise PersistenceError("日志读取失败", path=str(self.path)) from exc

    def max_sequence(self) -> int:
        """返回日志中的最大事件序号。"""

        latest = 0
        for event in self.iter_events():
            latest = max(latest, int(event.get("seq", 0)))
        return latest

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """返回最后 ``limit`` 条事件。"""

        if limit <= 0:
            return []
        events = self.all()
        return events[-limit:]

    def size_bytes(self) -> int:
        """返回日志文件大小。"""

        if not self.path.exists():
            return 0
        return self.path.stat().st_size
