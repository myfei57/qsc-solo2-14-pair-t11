"""事件中继：后台线程把发件箱里的事件投出去。

- 启动后常驻，事件入箱或退避到期时被唤醒；
- 单工作线程按发生时间顺序投递，保证对端观察到的次序与本地一致；
- 临时故障指数退避（上限 ``retry_max_s``），断链期间不丢事件；
- 投递成功只依赖对端幂等语义，重试不会产生重复。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Any

from ..core.clock import Clock
from .outbox import EventOutbox
from .transport import (
    EventTransport,
    TransportRejected,
    TransportUnavailable,
    envelope_for,
)

LOGGER = logging.getLogger("breweryctl.events")


class EventRelay:
    """把 :class:`EventOutbox` 与 :class:`EventTransport` 串起来的后台中继。"""

    def __init__(
        self,
        outbox: EventOutbox | None,
        transport: EventTransport | None,
        clock: Clock,
        *,
        batch_size: int = 16,
        retry_base_s: float = 1.0,
        retry_max_s: float = 60.0,
        stale_claim_s: int = 120,
    ) -> None:
        self.outbox = outbox
        self.transport = transport
        self.clock = clock
        self.batch_size = batch_size
        self.retry_base_s = retry_base_s
        self.retry_max_s = retry_max_s
        self.stale_claim_s = stale_claim_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivered = 0
        self._failures = 0
        self._last_error: str | None = None
        self._last_pump_at: str | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        """启动后台线程；未配置传输时不启动。"""

        if self.transport is None or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="breweryctl-event-relay", daemon=True
        )
        self._thread.start()
        LOGGER.info("事件中继已启动")

    def stop(self, timeout: float = 5.0) -> None:
        """通知线程退出并等待收尾。"""

        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=timeout)
        self._thread = None

    def notify(self) -> None:
        """有新事件入箱时唤醒中继立即尝试。"""

        self._wake.set()

    def flush(self, timeout: float = 5.0) -> bool:
        """循环投递直到待发事件清空或超时，返回是否清空。"""

        if self.outbox is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.outbox.pending():
                return True
            self.pump()
            if self.outbox.pending():
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        return not self.outbox.pending()

    # ------------------------------------------------------------------ 投递

    def pump(self) -> int:
        """领取并投递一批到期事件，返回本次成功投递条数。

        临时故障会阻塞队列：队首失败后，同批尾部退回排队并共享退避时刻，
        保证对端观察到的事件顺序与本地发生顺序一致，不会乱序超车。
        """

        if self.transport is None or self.outbox is None:
            return 0
        delivered_now = 0
        claimed = self.outbox.claim_next(self.stale_claim_s, self.batch_size)
        for index, event in enumerate(claimed):
            result, blocked_until = self._deliver_one(event)
            if result:
                delivered_now += 1
                continue
            if blocked_until is not None:
                tail = [str(item["event_id"]) for item in claimed[index + 1 :]]
                self.outbox.release_tail(tail, blocked_until)
                break
        self._last_pump_at = _format(self.clock)
        return delivered_now

    def _deliver_one(self, event: dict[str, Any]) -> tuple[bool, Any]:
        """投递单条，返回 (是否成功, 临时故障时的下次可发时刻)。"""

        event_id = str(event["event_id"])
        try:
            self.transport.deliver(envelope_for(event))  # type: ignore[union-attr]
        except TransportUnavailable as exc:
            attempts = int(event.get("attempts", 0))
            backoff = min(self.retry_max_s, self.retry_base_s * 2 ** max(0, attempts - 1))
            next_at = self.clock.now() + timedelta(seconds=backoff)
            self.outbox.requeue(event_id, str(exc), next_at)
            self._failures += 1
            self._last_error = str(exc)
            LOGGER.warning("事件 %s 投递失败，%.2fs 后重试：%s", event_id, backoff, exc)
            return False, next_at
        except TransportRejected as exc:
            self.outbox.mark_dead(event_id, str(exc))
            self._last_error = str(exc)
            LOGGER.error("事件 %s 被对端拒收，转入死信：%s", event_id, exc)
            return False, None
        except Exception as exc:  # noqa: BLE001 - 未预期异常按临时故障重试，不能丢事件
            attempts = int(event.get("attempts", 0))
            backoff = min(self.retry_max_s, self.retry_base_s * 2 ** max(0, attempts - 1))
            next_at = self.clock.now() + timedelta(seconds=backoff)
            self.outbox.requeue(event_id, f"unexpected: {exc}", next_at)
            self._failures += 1
            self._last_error = str(exc)
            LOGGER.exception("事件 %s 投递异常", event_id)
            return False, next_at
        self.outbox.mark_delivered(event_id)
        self._delivered += 1
        LOGGER.info("事件 %s(%s) 投递成功", event_id, event.get("kind"))
        return True, None

    # ------------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        """返回中继运行指标。"""

        stats = self.outbox.stats() if self.outbox is not None else {}
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "endpoint_configured": self.transport is not None,
            "delivered_total": self._delivered,
            "failure_total": self._failures,
            "last_error": self._last_error,
            "last_pump_at": self._last_pump_at,
            "outbox": stats,
        }

    # ------------------------------------------------------------------ 线程

    def _run(self) -> None:
        while not self._stop.is_set():
            self.pump()
            wait_s = self._next_wait_seconds()
            self._wake.wait(timeout=wait_s)
            self._wake.clear()

    def _next_wait_seconds(self) -> float:
        if self.outbox is None:
            return self.retry_max_s
        pending = self.outbox.pending()
        if not pending:
            return self.retry_max_s
        # 有待发事件时按最近的退避到期时间唤醒，最多 1 秒醒一次响应新事件。
        return min(1.0, self.retry_max_s)


def _format(clock: Clock) -> str:
    from ..core.clock import format_moment

    return format_moment(clock.now())
