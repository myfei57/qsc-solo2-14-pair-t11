"""后台中继：领取 → 发送 → 确认的循环。

单进程内只运行一个中继实例。每轮：

1. 按全局序号领取一批事件（``pending`` 或回收的僵死 ``in_flight``）；
2. 整批走传输层；整批成功才逐条标记 ``sent``，任一失败则整批回退，
   并从第一条开始重发——对端按 ``event_id`` 幂等，重复安全；
3. 链路持续失败时指数退避（上限 60s），恢复后立刻追平积压；
4. 达到最大尝试次数的事件进入死信，不阻塞后续事件。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from ..core.clock import Clock, SystemClock, format_moment
from .outbox import Outbox
from .transport import SinkTransport, TransportError

LOGGER = logging.getLogger("breweryctl.outbound.relay")

DEFAULT_BATCH_SIZE = 32
DEFAULT_IDLE_INTERVAL_SEC = 2.0
DEFAULT_MAX_INTERVAL_SEC = 60.0


@dataclass
class SendResult:
    """一轮发送的统计结果。"""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    dead: int = 0
    retried: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed,
            "sent": self.sent,
            "failed": self.failed,
            "dead": self.dead,
            "retried": self.retried,
            "error": self.error,
        }


class OutboxRelay:
    """在后台线程中持续转发发件箱事件。"""

    def __init__(
        self,
        outbox: Outbox,
        transport: SinkTransport,
        clock: Clock | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_interval_sec: float = DEFAULT_IDLE_INTERVAL_SEC,
        max_interval_sec: float = DEFAULT_MAX_INTERVAL_SEC,
    ) -> None:
        self.outbox = outbox
        self.transport = transport
        self.clock = clock or SystemClock()
        self.batch_size = batch_size
        self.idle_interval_sec = idle_interval_sec
        self.max_interval_sec = max_interval_sec
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._backoff_sec = idle_interval_sec
        self._round_lock = threading.Lock()
        self._total_sent = 0
        self._total_failed = 0
        self._last_error: str | None = None
        self._last_run_at: str | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台发送线程；重复启动无副作用。"""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="outbox-relay", daemon=True
        )
        self._thread.start()
        LOGGER.info("事件外发中继已启动")

    def stop(self, timeout_sec: float = 10.0) -> None:
        """通知线程退出并等待其收尾。"""

        self._stopping.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_sec)
        self._thread = None

    def kick(self) -> None:
        """有新事件时唤醒中继立即发送，不必等轮询间隔。"""

        self._wake.set()

    def is_running(self) -> bool:
        """后台线程是否存活。"""

        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ 发送循环

    def drain(self, max_rounds: int | None = None) -> SendResult:
        """同步连续发送，直到队列清空，供测试与启动时追平积压。"""

        aggregate = SendResult()
        rounds = 0
        while True:
            result = self.run_once()
            aggregate.claimed += result.claimed
            aggregate.sent += result.sent
            aggregate.failed += result.failed
            aggregate.dead += result.dead
            rounds += 1
            if result.retried or result.claimed == 0:
                break
            if max_rounds is not None and rounds >= max_rounds:
                break
        return aggregate

    def run_once(self) -> SendResult:
        """执行一轮"领取-发送-确认"，永不把异常抛给调用方。"""

        with self._round_lock:
            result = SendResult()
            try:
                events = self.outbox.claim_next(limit=self.batch_size)
            except Exception as exc:  # pragma: no cover - 领取异常属于存储故障
                result.retried = True
                result.error = f"领取事件失败：{exc}"
                self._record_failure(result.error)
                return result
            result.claimed = len(events)
            if not events:
                return result
            try:
                outcome = self.transport.send_batch(events)
            except TransportError as exc:
                self._handle_transport_error(events, exc, result)
                return result
            except Exception as exc:  # 传输层实现自身的缺陷也不能打挂线程
                self._handle_transport_error(
                    events, TransportError(f"传输层异常：{exc}", retryable=True), result
                )
                return result
            for event in events:
                event_id = str(event["id"])
                if event_id in outcome.rejected:
                    # 对端明确永久拒收该事件：直接进死信，不拖累同批其它事件。
                    updated = self.outbox.mark_failed(
                        event_id,
                        f"对端拒收：{outcome.rejected[event_id]}",
                        retryable=False,
                    )
                    result.failed += 1
                    if updated.get("status") == "dead":
                        result.dead += 1
                    continue
                ack = event_id if event_id in outcome.acknowledged else None
                self.outbox.mark_sent(event_id, acknowledged_id=ack)
                result.sent += 1
            self._record_success(result.sent)
            return result

    # ------------------------------------------------------------------ 指标

    def status(self) -> dict[str, Any]:
        """中继与发件箱的合并视图，供健康检查页面使用。"""

        stats = self.outbox.stats()
        return {
            "running": self.is_running(),
            "backoff_sec": round(self._backoff_sec, 3),
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "last_error": self._last_error,
            "last_run_at": self._last_run_at,
            "outbox": stats,
        }

    # ------------------------------------------------------------------ 内部

    def _run_loop(self) -> None:
        while not self._stopping.is_set():
            result = self.run_once()
            self._last_run_at = format_moment(self.clock.now())
            if result.claimed == 0:
                self._backoff_sec = self.idle_interval_sec
                self._wait(self.idle_interval_sec)
                continue
            if result.sent == result.claimed and not result.retried:
                self._backoff_sec = self.idle_interval_sec
                # 还有积压时立刻发下一批。
                if self.outbox.pending_count() > 0:
                    self._wake.clear()
                    continue
                self._wait(self.idle_interval_sec)
                continue
            # 发送失败：指数退避，上限 60s。
            wait_sec = min(self._backoff_sec, self.max_interval_sec)
            self._backoff_sec = min(self._backoff_sec * 2, self.max_interval_sec)
            self._wait(wait_sec)

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(0.05, seconds))
        self._wake.clear()

    def _handle_transport_error(
        self, events: list[dict[str, Any]], exc: TransportError, result: SendResult
    ) -> None:
        result.retried = True
        result.error = str(exc)
        # 链路级失败（断网、超时、5xx）对所有事件都可重试；只有事件本身
        # 达到最大尝试次数才进入死信。非重试错误只可能来自具体事件，
        # 不应把同批其它健康事件一并打入死信。
        for event in events:
            updated = self.outbox.mark_failed(
                str(event["id"]), str(exc), retryable=True
            )
            result.failed += 1
            if updated.get("status") == "dead":
                result.dead += 1
        if result.dead:
            LOGGER.warning(
                "%d 条关键事件进入死信；其余事件将继续重试", result.dead
            )
        self._record_failure(str(exc))

    def _record_success(self, sent: int) -> None:
        self._total_sent += sent
        self._last_error = None

    def _record_failure(self, error: str) -> None:
        self._total_failed += 1
        self._last_error = error
        LOGGER.warning("事件外发受阻：%s", error)
