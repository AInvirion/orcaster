"""Reliable output streaming to the server (POST /v1/tasks/{id}/output).

Event model: each stdout/stderr chunk is an event ``{seq, ts, stream, text}``;
``seq`` is monotonic per execution (1-indexed) and ``ts`` is the UTC ISO8601
time of the read.

Reliability:

* ``feed()`` only ENQUEUES (producer). A single asyncio "sender" task
  (consumer) drains the queue in FIFO order: it builds a batch and dequeues it
  only after a server ``2xx`` (batch persisted).
* Transient error (network, 5xx/429 once the internal retries of ``api.output``
  are exhausted): own exponential backoff (1 s → 30 s, jitter) and retry of the
  SAME batch. Nothing is discarded.
* ``LeaseLostError`` (409): the attempt is no longer ours → the backlog (RAM +
  spill) is discarded and the streamer is dead; a later ``feed()`` is a no-op.
* Backlog: RAM queue bounded to ``buffer_max_bytes``; once exceeded, new events
  are appended to a ``.jsonl`` file (spill). With no spill available, a hard RAM
  cap of 2× applies and events are dropped with a logged error (degraded mode).
* ``aclose()`` drains everything pending (re-invocable: a later ``feed`` is
  drained by the next ``aclose``). Without a deadline, ``aclose()`` blocks until
  the server answers (2xx or 409); if the server is down it retries
  indefinitely — which is why ALL production paths pass ``drain_deadline``:
  short on shutdown/synthetic streams, derived from the lease timeout on the
  final drain of the happy path (the real reliability window). With
  ``drain_deadline`` it cuts off in time and loses the rest — the same policy
  as a crash.

Masking: with an active ``redactor``, ``feed()`` replaces the secrets delivered
by the server with ``***`` before enqueueing (also before the spill to disk).
The dispatcher delivers raw chunks of up to 64 KiB, not lines, so a value may
fall split across two chunks: per stream, the tail that could be the start of a
value is held back and prepended to the next chunk. What is held is released
with the next chunk, before the echo of the next command
(``feed(command=...)``) and in ``aclose()``. Limit: if the process stops
writing right after a prefix, those few characters wait until the next chunk or
the close.

Concurrency: all state is mutated only from coroutines of the same event loop
and no internal synchronous method does an ``await`` between reading and
writing the shared state, so no locks are needed (the ``_buf_lock`` of the
best-effort version served the same invariant, and the property is kept).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orcaster_worker.api import LeaseLostError, OrchestratorAPI
from orcaster_worker.secret_redaction import SecretRedactor

log = logging.getLogger("orcaster.worker.output_streamer")

# Maximum batch when draining the backlog (internal constant, not config): keeps
# requests bounded without slowing the drain below the healthy-case 8 KiB pace.
_DRAIN_BATCH_MAX_BYTES = 256 * 1024
# Sender backoff on a transient error.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
# Hard RAM cap when no spill is available: 2× buffer_max_bytes.
_DEGRADED_RAM_FACTOR = 2

# Per-event character cap. The server rejects with 422 any `text` above its
# OUTPUT_EVENT_TEXT_MAX_CHARS (128 KiB chars), and a 422 makes the sender retry
# the same batch indefinitely (only a 409 aborts), so the oversized event would
# never persist. stdout/stderr never exceed it (read chunks of ≤64 KiB), but the
# command echo comes from the payload and is NOT bounded: it is truncated here
# to preserve the invariant "no legitimate worker event triggers a 422". 64 KiB
# keeps parity with the read chunk and leaves the server a 2× margin. With an
# active secret, the tail held back by masking is prepended to the next chunk
# and may exceed the cap: stdout/stderr are split into several events
# (``_split_event_text``) and never truncated.
_EVENT_TEXT_MAX_CHARS = 64 * 1024
_TRUNCATION_MARKER = "… [truncated]"

SPILL_SUBDIR = "output-spill"


def _cap_event_text(text: str) -> str:
    """Bound `text` to `_EVENT_TEXT_MAX_CHARS` chars, with a truncation marker.

    Guarantees that no queued event exceeds the server cap (which counts code
    points), avoiding a 422 that the sender would retry endlessly."""
    if len(text) <= _EVENT_TEXT_MAX_CHARS:
        return text
    return text[: _EVENT_TEXT_MAX_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def _split_event_text(text: str) -> list[str]:
    """Split ``text`` into pieces of at most `_EVENT_TEXT_MAX_CHARS` chars,
    losing nothing (real stdout/stderr output, which is not truncated)."""
    return [text[i : i + _EVENT_TEXT_MAX_CHARS] for i in range(0, len(text), _EVENT_TEXT_MAX_CHARS)]


def cleanup_spill_orphans(state_dir: Path) -> None:
    """Delete orphaned spills from previous executions (worker crash).

    Consistent with the "a crash loses the backlog" policy: at boot there is no
    way to resend them (the old attempt's lease is no longer valid server-side,
    /output would answer 409)."""
    spill_dir = state_dir / SPILL_SUBDIR
    if not spill_dir.is_dir():
        return
    for f in spill_dir.glob("*.jsonl"):
        with contextlib.suppress(OSError):
            f.unlink()
            log.info("removed orphan output spill %s", f)


def _ev_size(ev: dict[str, Any]) -> int:
    return len(ev["text"].encode("utf-8", errors="replace"))


class OutputStreamer:
    def __init__(
        self,
        api: OrchestratorAPI,
        task_id: int,
        lease_token: str,
        *,
        max_bytes: int,
        interval: float,
        buffer_max_bytes: int,
        spill_dir: Path | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._api = api
        # With no values to mask the streamer behaves exactly as it did before
        # masking existed (no tail retention).
        self._redactor = redactor if redactor is not None and redactor.active else None
        self._held: dict[str, str] = {"stdout": "", "stderr": ""}
        self._task_id = task_id
        self._lease_token = lease_token
        self._max_bytes = max_bytes  # coalescing target of the healthy case
        self._interval = interval  # maximum coalescing wait of the healthy case
        self._buffer_max_bytes = buffer_max_bytes

        self._queue: deque[dict[str, Any]] = deque()
        self._queue_bytes = 0
        self._seq = 0
        self._wakeup = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None
        self._closed = False
        self._dead = False
        self._degraded_dropped = 0

        # Unique name per instance — two streamers of the same task_id (e.g. the
        # main streamer + an ephemeral synthetic one) must not share a file.
        self._spill_path: Path | None = (
            spill_dir / f"{task_id}-{uuid.uuid4().hex[:8]}.jsonl" if spill_dir is not None else None
        )
        self._spill_write = None  # type: ignore[var-annotated]  # TextIO | None
        self._spill_read_offset = 0
        self._spill_pending = 0
        self._spill_broken = False

    @property
    def last_seq(self) -> int:
        """Last assigned seq (for diagnostics/tests)."""
        return self._seq

    # --- Producer -----------------------------------------------------------

    async def feed(
        self,
        stdout: str | None = None,
        stderr: str | None = None,
        command: str | None = None,
    ) -> None:
        """``on_output`` callback of the dispatcher: turns chunks into events and
        queues them. It sends nothing by itself (that is the sender's job).
        ``command`` is the echo of the payload command ($ <cmd>) emitted before
        its output."""
        if self._dead:
            return
        ts = datetime.now(UTC).isoformat()
        if self._redactor is not None:
            # The echo of a new command closes the previous one's output: what is
            # held is released so it is not reordered behind the echo.
            final = command is not None
            stdout = self._mask("stdout", stdout, final=final)
            stderr = self._mask("stderr", stderr, final=final)
            if command is not None:
                command = self._redactor.redact(command)
        if stdout is not None:
            self._enqueue_stream(ts, "stdout", stdout)
        if stderr is not None:
            self._enqueue_stream(ts, "stderr", stderr)
        if command is not None:
            # The dispatcher calls feed(command=...) in a call BEFORE the
            # feed(stdout=.../stderr=...) of the same command, so the 'command'
            # event gets a lower seq. If some future caller passed command
            # together with stdout/stderr in a single call, command would get
            # the highest seq (it is queued last) and would end up out of order
            # relative to its output.
            self._seq += 1
            self._enqueue(
                {"seq": self._seq, "ts": ts, "stream": "command", "text": _cap_event_text(command)}
            )
        self._wakeup.set()

    def _mask(self, stream: str, chunk: str | None, *, final: bool) -> str | None:
        """Mask ``chunk`` together with the stream's held tail. Returns the text
        to enqueue, or None if nothing is emittable yet."""
        assert self._redactor is not None
        held = self._held[stream]
        if chunk is None and not (final and held):
            return None
        text = held + (chunk or "")
        if final:
            self._held[stream] = ""
            out = self._redactor.redact(text)
        else:
            out, self._held[stream] = self._redactor.split(text)
        return out or None

    def _flush_held(self) -> None:
        """Enqueue what masking held back (stream close)."""
        if self._redactor is None or self._dead:
            return
        ts = datetime.now(UTC).isoformat()
        for stream in ("stdout", "stderr"):
            text = self._mask(stream, None, final=True)
            if text is not None:
                self._enqueue_stream(ts, stream, text)

    def _enqueue_stream(self, ts: str, stream: str, text: str) -> None:
        """Enqueue stdout/stderr output, split into events within the cap."""
        for part in _split_event_text(text):
            self._seq += 1
            self._enqueue({"seq": self._seq, "ts": ts, "stream": stream, "text": part})

    def _enqueue(self, ev: dict[str, Any]) -> None:
        size = _ev_size(ev)
        use_spill = (
            self._spill_path is not None
            and not self._spill_broken
            and (self._spill_pending > 0 or self._queue_bytes + size > self._buffer_max_bytes)
        )
        if use_spill:
            try:
                self._spill_append(ev)
                self._spill_pending += 1
                return
            except OSError as e:
                self._spill_broken = True
                log.error(
                    "output spill unavailable (%s); degrading to capped RAM buffer task_id=%s",
                    e,
                    self._task_id,
                )
        if self._queue_bytes + size > self._buffer_max_bytes * _DEGRADED_RAM_FACTOR:
            self._degraded_dropped += 1
            if self._degraded_dropped == 1 or self._degraded_dropped % 1000 == 0:
                log.error(
                    "output backlog over hard RAM cap and no spill available; "
                    "dropped %d events so far (captura-total degradada) task_id=%s",
                    self._degraded_dropped,
                    self._task_id,
                )
            return
        self._queue.append(ev)
        self._queue_bytes += size

    def _spill_append(self, ev: dict[str, Any]) -> None:
        # Deliberate synchronous I/O: small lines (≤64 KiB) to local disk; the
        # same trade-off as the worker's synchronous logging.
        if self._spill_write is None:
            assert self._spill_path is not None
            self._spill_path.parent.mkdir(parents=True, exist_ok=True)
            self._spill_write = self._spill_path.open("a", encoding="utf-8")
        self._spill_write.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._spill_write.flush()

    def _refill_from_spill(self) -> None:
        """Move up to _DRAIN_BATCH_MAX_BYTES from the spill to the RAM queue, in
        order. Synchronous → atomic with respect to feed() (single-threaded asyncio)."""
        assert self._spill_path is not None
        loaded = 0
        try:
            with self._spill_path.open("r", encoding="utf-8") as f:
                f.seek(self._spill_read_offset)
                while self._spill_pending > 0 and loaded < _DRAIN_BATCH_MAX_BYTES:
                    line = f.readline()
                    if not line:
                        break
                    self._spill_read_offset = f.tell()
                    ev = json.loads(line)
                    self._queue.append(ev)
                    size = _ev_size(ev)
                    self._queue_bytes += size
                    loaded += size
                    self._spill_pending -= 1
        except (OSError, json.JSONDecodeError) as e:
            # Unreadable or corrupt spill: everything left in the spill is lost
            # (all-or-nothing, degraded mode).
            log.error(
                "output spill unreadable (%s); dropping %d spilled events", e, self._spill_pending
            )
            self._spill_pending = 0
        if self._spill_pending == 0:
            self._close_and_unlink_spill()

    def _close_and_unlink_spill(self) -> None:
        if self._spill_write is not None:
            with contextlib.suppress(OSError):
                self._spill_write.close()
            self._spill_write = None
        self._spill_read_offset = 0
        if self._spill_path is not None:
            with contextlib.suppress(OSError):
                self._spill_path.unlink(missing_ok=True)

    # --- Consumidor (sender) --------------------------------------------------

    def start(self) -> None:
        """Start the sender. Idempotent under single-threaded asyncio."""
        if self._sender is None:
            self._sender = asyncio.create_task(
                self._run_sender(), name=f"output-sender-{self._task_id}"
            )

    async def _run_sender(self) -> None:
        while not self._dead:
            if not self._queue and self._spill_pending > 0:
                self._refill_from_spill()
            if not self._queue:
                if self._closed and self._spill_pending == 0:
                    return
                self._wakeup.clear()
                # Re-check after clear: feed() may have enqueued before the clear.
                if self._queue or (self._closed and self._spill_pending == 0):
                    continue
                await self._wakeup.wait()
                continue
            # Healthy-case coalescing: gather up to max_bytes or interval
            # (parity with the 8 KiB / 2 s triggers of the best-effort design).
            if not self._closed and self._queue_bytes < self._max_bytes:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._interval
                while (
                    not self._closed
                    and not self._dead
                    and self._queue_bytes < self._max_bytes
                    and (remaining := deadline - loop.time()) > 0
                ):
                    self._wakeup.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wakeup.wait(), timeout=remaining)
                if self._dead:
                    return
            batch, batch_bytes = self._peek_batch()
            if not batch:
                continue
            await self._send_with_retry(batch)
            if self._dead:
                return
            self._dequeue(len(batch), batch_bytes)

    def _peek_batch(self) -> tuple[list[dict[str, Any]], int]:
        """First events of the queue up to _DRAIN_BATCH_MAX_BYTES, WITHOUT
        dequeuing them (the pop happens only after the 2xx confirmation)."""
        batch: list[dict[str, Any]] = []
        total = 0
        for ev in self._queue:
            size = _ev_size(ev)
            if batch and total + size > _DRAIN_BATCH_MAX_BYTES:
                break
            batch.append(ev)
            total += size
        return batch, total

    def _dequeue(self, n: int, nbytes: int) -> None:
        for _ in range(n):
            self._queue.popleft()
        self._queue_bytes -= nbytes

    async def _send_with_retry(self, batch: list[dict[str, Any]]) -> None:
        attempt = 0
        while not self._dead:
            try:
                await self._api.output(self._task_id, lease_token=self._lease_token, events=batch)
                return
            except LeaseLostError:
                log.warning(
                    "lease lost for task_id=%s; discarding output backlog (%d events queued)",
                    self._task_id,
                    len(self._queue),
                )
                self._abort()
                return
            except Exception as e:  # noqa: BLE001 — transitorio (red/5xx agotados)
                attempt += 1
                delay = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                delay *= 0.5 + random.random() / 2  # jitter
                log.warning(
                    "output send failed task_id=%s attempt=%d: %s; retrying in "
                    "%.1fs (%d events held, nothing discarded)",
                    self._task_id,
                    attempt,
                    e,
                    delay,
                    len(batch),
                )
                await asyncio.sleep(delay)

    def _abort(self) -> None:
        """409: the attempt is no longer ours. Discard everything and kill the streamer."""
        self._dead = True
        self._queue.clear()
        self._queue_bytes = 0
        self._spill_pending = 0
        self._close_and_unlink_spill()
        self._wakeup.set()

    # --- Close ----------------------------------------------------------------

    async def aclose(self, *, drain_deadline: float | None = None) -> None:
        """Drain the whole backlog and stop the sender.

        Without a deadline it blocks until the server answers (2xx or 409); if
        the server is down it retries indefinitely. With ``drain_deadline``
        (shutdown / synthetic diagnostics) it cuts off in time and loses the
        rest. Re-invocable: a later ``feed()`` is drained by the next
        ``aclose()``."""
        self._flush_held()
        self._closed = True
        self._wakeup.set()
        if self._sender is None:
            self._sender = asyncio.create_task(self._run_sender())
        try:
            if drain_deadline is None:
                await self._sender
            else:
                await asyncio.wait_for(asyncio.shield(self._sender), timeout=drain_deadline)
        except TimeoutError:
            log.warning(
                "output drain deadline (%.1fs) reached task_id=%s; dropping %d pending events",
                drain_deadline,
                self._task_id,
                len(self._queue),
            )
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
            self._queue.clear()
            self._queue_bytes = 0
            self._spill_pending = 0
            self._close_and_unlink_spill()
        finally:
            # aclose() cancelled from outside (hard shutdown): do not orphan the
            # sender — without this it would keep running and calling api.output().
            if self._sender is not None and not self._sender.done():
                self._sender.cancel()
            # Re-invocable: the next aclose() creates a new sender that drains
            # any later feed() (the sender's exit condition is "queue empty and
            # _closed"). A dead streamer (409) stays dead.
            self._sender = None
