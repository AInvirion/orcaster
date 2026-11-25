"""In-memory pub/sub bus for the dashboard SSE, indexed by workspace.

Lives inside ONE uvicorn process. Each SSE connection subscribes with its own
queue; the Postgres listener (orcaster_server.sse_listener) publishes the events
that arrive via LISTEN. The fan-out only walks the queues of the event's
workspace, guaranteeing tenant isolation. Coalescing avoids the notification
storm from output (inserted line by line).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

# Per-queue cap: if a client does not drain (slow network), we drop instead of
# growing without bound. A full queue means that client will lose events, but on
# reconnect the handler re-reads the current state from the DB.
_QUEUE_MAXSIZE = 256


@dataclass(frozen=True)
class SseMessage:
    """An SSE event ready to serialize. `event` is the name (the protocol's
    `event:` field); `data` is the body (empty when the event is only a signal
    to trigger an hx-get on the client)."""

    event: str
    data: str = ""


@dataclass
class _Subscriber:
    """A subscribed queue with its optional topic filter.

    `topics` is a set of type prefixes (the part before the first `-` of the
    event name); `None` means receive everything."""

    queue: asyncio.Queue[SseMessage]
    topics: frozenset[str] | None = None


@dataclass
class _Pending:
    """Trailing state per (workspace_id, coalesce_key): the last message
    dropped by coalescing and the timer that will resend it when the window
    expires."""

    message: SseMessage
    timer: asyncio.TimerHandle = field(default=None)  # type: ignore[assignment]


class SseBus:
    """Registry of subscribers per workspace with optional coalescing."""

    def __init__(self, coalesce_ms: int = 250) -> None:
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._coalesce_ms = coalesce_ms
        # last send per (workspace_id, coalesce_key) in monotonic seconds
        self._last_sent: dict[tuple[str, str], float] = {}
        # pending trailing messages per (workspace_id, coalesce_key)
        self._pending: dict[tuple[str, str], _Pending] = {}
        # monotonic mark of the last amortized sweep of `_last_sent`
        self._last_gc: float = time.monotonic()

    def subscribe(
        self,
        workspace_id: str,
        topics: frozenset[str] | None = None,
    ) -> asyncio.Queue[SseMessage]:
        queue: asyncio.Queue[SseMessage] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.setdefault(workspace_id, []).append(_Subscriber(queue, topics))
        return queue

    def unsubscribe(self, workspace_id: str, queue: asyncio.Queue[SseMessage]) -> None:
        subs = self._subscribers.get(workspace_id)
        if subs is None:
            return
        subs[:] = [s for s in subs if s.queue is not queue]
        if not subs:
            del self._subscribers[workspace_id]
            # Empty workspace: purge the throttle and cancel that workspace's
            # pending trailing timers so as not to leak memory or fire on a
            # workspace with no subscribers.
            self._purge_workspace(workspace_id)

    def _purge_workspace(self, workspace_id: str) -> None:
        """Remove `_last_sent` entries and cancel the trailing timers of the
        given workspace."""
        stale_keys = [k for k in self._last_sent if k[0] == workspace_id]
        for k in stale_keys:
            del self._last_sent[k]
        pending_keys = [k for k in self._pending if k[0] == workspace_id]
        for k in pending_keys:
            pending = self._pending.pop(k)
            if pending.timer is not None:
                pending.timer.cancel()

    def subscriber_count(self, workspace_id: str) -> int:
        return len(self._subscribers.get(workspace_id, ()))

    def publish(
        self,
        workspace_id: str,
        message: SseMessage,
        *,
        coalesce_key: str | None = None,
    ) -> None:
        if coalesce_key is not None and self._should_drop(workspace_id, coalesce_key, message):
            return
        self._fan_out(workspace_id, message)

    def _fan_out(self, workspace_id: str, message: SseMessage) -> None:
        """Enqueue the message on each subscriber of the workspace whose topic
        filter admits it."""
        event_type = message.event.split("-", 1)[0]
        for sub in self._subscribers.get(workspace_id, ()):
            if sub.topics is not None and event_type not in sub.topics:
                continue
            # Stuck client: drop this event for that client.
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(message)

    def _should_drop(
        self,
        workspace_id: str,
        coalesce_key: str,
        message: SseMessage,
    ) -> bool:
        now = time.monotonic()
        self._maybe_gc(now)
        key = (workspace_id, coalesce_key)
        last = self._last_sent.get(key)
        if last is not None and (now - last) * 1000 < self._coalesce_ms:
            # Inside the window: drop the immediate send but schedule (or
            # update) a trailing flush so the last event of the burst is not lost.
            self._schedule_trailing(workspace_id, coalesce_key, message, last, now)
            return True
        self._last_sent[key] = now
        return False

    def _maybe_gc(self, now: float) -> None:
        """Amortized sweep of `_last_sent`: removes entries whose age is
        >= coalesce_ms (they no longer affect the throttle). It only runs when at
        least one window has passed since the last sweep, keeping the O(n) cost
        bounded to that frequency instead of per publish."""
        window_s = self._coalesce_ms / 1000.0
        if now - self._last_gc < window_s:
            return
        self._last_gc = now
        stale = [k for k, t in self._last_sent.items() if (now - t) >= window_s]
        for k in stale:
            # Do not purge if a trailing send is pending for that key (the timer
            # will update `_last_sent` when it fires).
            if k not in self._pending:
                del self._last_sent[k]

    def _schedule_trailing(
        self,
        workspace_id: str,
        coalesce_key: str,
        message: SseMessage,
        last: float,
        now: float,
    ) -> None:
        """Store `message` as the pending trailing one and, if no timer is already
        scheduled for this key, schedule a `call_later` that will resend it when
        the window expires. Degrades to leading-only if no loop is running."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (e.g. synchronous test): leading-only behaviour.
            return
        key = (workspace_id, coalesce_key)
        pending = self._pending.get(key)
        if pending is not None:
            # A timer is already scheduled: only update the message to resend
            # (we want the most recent one of the burst).
            pending.message = message
            return
        remaining_s = max(0.0, (self._coalesce_ms / 1000.0) - (now - last))
        pending = _Pending(message=message)
        pending.timer = loop.call_later(
            remaining_s,
            self._flush_trailing,
            workspace_id,
            coalesce_key,
        )
        self._pending[key] = pending

    def _flush_trailing(self, workspace_id: str, coalesce_key: str) -> None:
        """Trailing timer callback: resends the key's last pending message ONLY
        if no other send happened in between. Runs on the same loop as `publish`
        (single-thread asyncio)."""
        key = (workspace_id, coalesce_key)
        pending = self._pending.pop(key, None)
        if pending is None:
            return
        now = time.monotonic()
        last = self._last_sent.get(key)
        # If a later send happened inside the window, do not resend (another
        # leading send already covered the burst and would schedule its own
        # trailing one).
        if last is not None and (now - last) * 1000 < self._coalesce_ms:
            return
        self._last_sent[key] = now
        self._fan_out(workspace_id, pending.message)
