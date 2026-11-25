"""Tests for the reliable OutputStreamer.

Model: feed() enqueues {seq, ts, stream, text} events; a single sender loop
sends them in FIFO batches and only dequeues after a 2xx. Transient error ->
retries the same batch (nothing is dropped). LeaseLostError (409) -> aborts
and discards the backlog. aclose() drains everything pending.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from orcaster_worker.api import LeaseLostError
from orcaster_worker.output_streamer import OutputStreamer


class _FakeAPI:
    """Records each batch; can fail the first `fail_first` calls."""

    def __init__(self, fail_first: int = 0, lease_lost_at: int | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.calls = 0
        self._fail_first = fail_first
        self._lease_lost_at = lease_lost_at

    async def output(self, task_id: int, *, lease_token: str, events: list[dict[str, Any]]) -> None:
        self.calls += 1
        if self._lease_lost_at is not None and self.calls >= self._lease_lost_at:
            raise LeaseLostError("lease lost")
        if self.calls <= self._fail_first:
            raise RuntimeError("network down")
        self.batches.append(events)

    def all_events(self) -> list[dict[str, Any]]:
        return [ev for batch in self.batches for ev in batch]


def _mk(api: _FakeAPI, **kw: Any) -> OutputStreamer:
    kw.setdefault("max_bytes", 8)
    kw.setdefault("interval", 0.01)
    kw.setdefault("buffer_max_bytes", 1024 * 1024)
    return OutputStreamer(api, 1, "tok", **kw)  # type: ignore[arg-type]


async def test_drain_on_aclose_without_start() -> None:
    """aclose() drains even if start() was never called (synthetic helper)."""
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(stdout="hello")
    await s.aclose()
    assert [e["text"] for e in api.all_events()] == ["hello"]


async def test_seq_is_monotonic_and_ts_iso8601() -> None:
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(stdout="a")
    await s.feed(stderr="b")
    await s.feed(stdout="c")
    await s.aclose()
    evs = api.all_events()
    assert [e["seq"] for e in evs] == [1, 2, 3]
    assert [e["stream"] for e in evs] == ["stdout", "stderr", "stdout"]
    for e in evs:
        assert datetime.fromisoformat(e["ts"]).tzinfo is not None


async def test_feed_with_both_emits_two_events() -> None:
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(stdout="a", stderr="b")
    await s.aclose()
    assert [(e["seq"], e["stream"], e["text"]) for e in api.all_events()] == [
        (1, "stdout", "a"),
        (2, "stderr", "b"),
    ]


async def test_sender_confirms_before_dequeue_and_retries_same_batch() -> None:
    """Transient failure: the batch is NOT discarded; the same one is retried and
    the sequence arrives complete and in order (no seq gaps)."""
    api = _FakeAPI(fail_first=2)
    s = _mk(api)
    s.start()
    await s.feed(stdout="one")
    await s.feed(stdout="two")
    await s.aclose()
    evs = api.all_events()
    assert [e["seq"] for e in evs] == [1, 2]
    assert [e["text"] for e in evs] == ["one", "two"]
    assert api.calls >= 3  # 2 failures + at least 1 success


async def test_lease_lost_aborts_and_discards_backlog() -> None:
    api = _FakeAPI(lease_lost_at=1)
    s = _mk(api)
    s.start()
    await s.feed(stdout="x")
    await s.aclose()
    assert api.batches == []  # nothing persisted
    # later feed = silent no-op (dead streamer)
    await s.feed(stdout="ignored")
    await s.aclose()
    assert api.batches == []


async def test_live_flush_by_timer_while_running() -> None:
    """The healthy case keeps the live latency: with the sender running,
    events arrive without waiting for aclose()."""
    api = _FakeAPI()
    s = _mk(api, interval=0.02, max_bytes=10_000)
    s.start()
    await s.feed(stdout="tick")
    for _ in range(50):  # up to ~1 s
        await asyncio.sleep(0.02)
        if api.all_events():
            break
    assert [e["text"] for e in api.all_events()] == ["tick"]
    await s.aclose()


async def test_concurrent_feed_no_data_loss() -> None:
    api = _FakeAPI()
    s = _mk(api)
    await asyncio.gather(s.feed(stdout="AAAA"), s.feed(stdout="BBBB"))
    await s.aclose()
    evs = api.all_events()
    assert sorted("".join(e["text"] for e in evs)) == sorted("AAAABBBB")
    assert sorted(e["seq"] for e in evs) == [1, 2]


async def test_drain_deadline_drops_backlog() -> None:
    """Shutdown: aclose(drain_deadline=...) cuts the drain short and loses the rest
    (same policy as a worker crash)."""
    api = _FakeAPI(fail_first=10_000)  # server "down"
    s = _mk(api)
    s.start()
    await s.feed(stdout="doomed")
    await asyncio.wait_for(s.aclose(drain_deadline=0.2), timeout=5.0)
    assert api.batches == []


async def test_aclose_idempotent_and_reusable() -> None:
    """A feed after aclose() is drained in the next aclose() --
    needed for _run_external's synthetic post-dispatch paths."""
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(stdout="first")
    await s.aclose()
    await s.feed(stderr="late diagnostic")
    await s.aclose()
    evs = api.all_events()
    assert [(e["seq"], e["stream"], e["text"]) for e in evs] == [
        (1, "stdout", "first"),
        (2, "stderr", "late diagnostic"),
    ]


async def test_outer_cancellation_does_not_orphan_sender() -> None:
    """If the task running aclose() is cancelled from outside (hard shutdown),
    the inner sender must end up cancelled/finished -- not keep running."""
    api = _FakeAPI(fail_first=10_000)  # server "down": retries indefinitely
    s = _mk(api)
    s.start()
    await s.feed(stdout="orphan-test")

    # We launch aclose() without a deadline in a separate task and cancel it.
    task = asyncio.create_task(s.aclose())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The sender must have been cancelled by aclose()'s finally.
    assert s._sender is None or s._sender.done()

    calls_after_cancel = api.calls
    await asyncio.sleep(0.1)
    # No more calls must accumulate -- we tolerate +1 in flight on cancel.
    assert api.calls <= calls_after_cancel + 1


async def test_degraded_ram_hard_cap_drops_events() -> None:
    """With a small buffer_max_bytes and a stopped sender, events that exceed
    the hard cap (2x) are dropped without raising an exception."""
    api = _FakeAPI(fail_first=10_000)  # never sends
    s = _mk(api, buffer_max_bytes=8)
    # We feed 10 events of ~10 bytes each (> 2 × 8 = 16 bytes of hard cap).
    for i in range(10):
        await s.feed(stdout=f"ev{i:04d}---")  # 9 bytes UTF-8
    # The queue must not exceed the hard cap × 2 + a one-event margin.
    assert s._queue_bytes <= 8 * 2 + 10
    # At least some events were discarded.
    assert s._degraded_dropped > 0
    # The feed raised no exception -- we check that we can close cleanly
    # (with a deadline so as not to block on the downed server).
    await asyncio.wait_for(s.aclose(drain_deadline=0.1), timeout=2.0)


async def test_spill_preserves_fifo_order(tmp_path) -> None:
    """On exceeding buffer_max_bytes events go to disk; the drain delivers
    EVERYTHING in FIFO order (RAM first, spill after) with no seq gaps."""
    api = _FakeAPI()
    s = OutputStreamer(
        api,
        1,
        "tok",
        max_bytes=10_000,
        interval=999.0,
        buffer_max_bytes=8,  # tiny: forces a spill right away
        spill_dir=tmp_path,
    )  # type: ignore[arg-type]
    for i in range(10):
        await s.feed(stdout=f"chunk-{i:02d}")
    spills = list(tmp_path.glob("*.jsonl"))
    assert spills  # exceeded the threshold -> spilled
    await s.aclose()
    evs = api.all_events()
    assert [e["seq"] for e in evs] == list(range(1, 11))
    assert [e["text"] for e in evs] == [f"chunk-{i:02d}" for i in range(10)]
    assert list(tmp_path.glob("*.jsonl")) == []  # cleaned up after a successful drain


async def test_spill_removed_on_lease_lost(tmp_path) -> None:
    api = _FakeAPI(lease_lost_at=1)
    s = OutputStreamer(
        api,
        1,
        "tok",
        max_bytes=10_000,
        interval=999.0,
        buffer_max_bytes=8,
        spill_dir=tmp_path,
    )  # type: ignore[arg-type]
    for i in range(5):
        await s.feed(stdout=f"x{i}")
    await s.aclose()
    assert list(tmp_path.glob("*.jsonl")) == []


def test_cleanup_spill_orphans(tmp_path) -> None:
    """On worker boot, orphaned spills from previous crashes are deleted."""
    from orcaster_worker.output_streamer import SPILL_SUBDIR, cleanup_spill_orphans

    spill_dir = tmp_path / SPILL_SUBDIR
    spill_dir.mkdir(parents=True)
    (spill_dir / "42.jsonl").write_text('{"seq": 1}\n')
    (spill_dir / "43.jsonl").write_text('{"seq": 1}\n')
    cleanup_spill_orphans(tmp_path)
    assert list(spill_dir.glob("*.jsonl")) == []


async def test_feed_command_emits_command_event() -> None:
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(command="$ ls\n")
    await s.aclose()
    evs = api.all_events()
    assert len(evs) == 1
    assert evs[0]["stream"] == "command"
    assert evs[0]["text"] == "$ ls\n"
    assert evs[0]["seq"] == 1


async def test_feed_command_seq_interleaves_with_streams() -> None:
    api = _FakeAPI()
    s = _mk(api)
    await s.feed(command="$ echo a\n")
    await s.feed(stdout="a\n")
    await s.aclose()
    evs = api.all_events()
    assert [(e["stream"], e["seq"]) for e in evs] == [("command", 1), ("stdout", 2)]


async def test_feed_truncates_oversized_command_below_server_cap() -> None:
    """A payload command larger than the server cap is truncated in feed.

    The server rejects with 422 any event whose `text` exceeds
    OUTPUT_EVENT_TEXT_MAX_CHARS (128 KiB chars); a 422 makes the sender
    retry the batch indefinitely (only the 409 aborts). The command echo
    comes from the payload, not from a bounded read, so feed must bound it."""
    api = _FakeAPI()
    s = _mk(api)
    huge = "x" * (200 * 1024)
    await s.feed(command=f"$ {huge}\n")
    await s.aclose()
    evs = api.all_events()
    assert len(evs) == 1
    assert evs[0]["stream"] == "command"
    assert len(evs[0]["text"]) <= 128 * 1024
    assert "truncated" in evs[0]["text"]


async def test_feed_preserves_normal_sized_stdout_chunk() -> None:
    """A stdout chunk of the worker's read size (64 KiB) is not truncated:
    the cap only trims text that exceeds the safe limit."""
    api = _FakeAPI()
    s = _mk(api)
    chunk = "y" * (64 * 1024)
    await s.feed(stdout=chunk)
    await s.aclose()
    evs = api.all_events()
    assert evs[0]["text"] == chunk


async def test_spill_broken_degrades_to_capped_ram(tmp_path, monkeypatch) -> None:
    """OSError on spill -> _spill_broken; events fall to the RAM buffer with a
    2x hard cap and the streamer keeps working (degradation, not a crash).

    Math: buffer_max_bytes=8, hard cap=16 (2×8), each event "x0".."x9"
    weighs 2 bytes UTF-8.
    - Events 0-3 (8 bytes): go to RAM directly (they do not exceed buffer_max_bytes).
    - Event 4 (2 bytes): exceeds -> tries spill -> OSError -> _spill_broken=True
      -> falls to the degraded RAM path: 8+2=10 ≤ 16 -> fits in RAM.
    - Events 5-7 (6 bytes): _spill_broken, RAM, 10->16 bytes -> they fit.
    - Events 8-9: 16+2=18 > 16 -> discarded. _degraded_dropped=2.
    Total in the RAM queue: 8 events × 2 bytes = 16 bytes."""
    api = _FakeAPI()
    s = OutputStreamer(
        api,
        1,
        "tok",
        max_bytes=10_000,
        interval=999.0,
        buffer_max_bytes=8,
        spill_dir=tmp_path,
    )  # type: ignore[arg-type]

    def _boom(ev):
        raise OSError("disk full")

    monkeypatch.setattr(s, "_spill_append", _boom)
    for i in range(10):
        await s.feed(stdout=f"x{i}")

    assert s._spill_broken is True
    assert s._degraded_dropped == 2
    assert s._queue_bytes == 16  # 8 events × 2 bytes
    await s.aclose()
    evs = api.all_events()
    assert len(evs) == 8  # the 2 discarded never reached the server
