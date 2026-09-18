"""按键加锁，保护同一业务对象的读改写序列。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class KeyedLocks:
    """为不同 key 提供独立可重入锁的注册表。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, key: str) -> threading.RLock:
        """返回指定 key 的锁，必要时创建。"""

        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def guard(self, key: str) -> Iterator[None]:
        """上下文管理器形式持有 key 锁。"""

        lock = self.lock_for(key)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def active_keys(self) -> int:
        """返回已登记锁的数量，用于运行状态展示。"""

        with self._guard:
            return len(self._locks)
