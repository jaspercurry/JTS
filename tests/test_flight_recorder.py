# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.flight_recorder — the Tier C log flight recorder.

Exercises the ring buffer, the auto-flush-on-WARNING + explicit-dump
paths (writing a tagged burst to a stream), and install()'s logging
surgery (logger DEBUG / console INFO / ring attached / toggle applied),
all without touching real journald or signals.
"""
from __future__ import annotations

import io
import logging

import pytest

from jasper import debug_mode
from jasper import flight_recorder as fr
from jasper.log_event import log_event


def _rec(level, msg, name="jasper.test"):
    return logging.LogRecord(name, level, "f.py", 1, msg, None, None)


# ----------------------------------------------------------- RingFlushHandler


def test_ring_drops_oldest_beyond_capacity():
    ring = fr.RingFlushHandler(capacity=3, dump_stream=io.StringIO())
    for i in range(5):
        ring.emit(_rec(logging.DEBUG, f"m{i}"))
    assert len(ring.buffer) == 3  # oldest two dropped


def test_flush_writes_tagged_burst_and_clears():
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    ring.emit(_rec(logging.DEBUG, "hello-context"))
    n = ring.flush_buffer("test")
    out = s.getvalue()
    assert n == 1
    assert "event=flightrec.dump reason=test records=1" in out
    assert "hello-context" in out
    assert "event=flightrec.dump.end reason=test" in out
    assert len(ring.buffer) == 0


def test_auto_flush_on_warning_includes_prior_context():
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    ring.emit(_rec(logging.DEBUG, "ctx1"))
    ring.emit(_rec(logging.DEBUG, "ctx2"))
    ring.emit(_rec(logging.WARNING, "boom"))  # triggers the dump
    out = s.getvalue()
    assert "reason=auto:warning" in out
    assert "ctx1" in out and "ctx2" in out and "boom" in out
    assert len(ring.buffer) == 0


def test_auto_flush_is_floored_per_signature(monkeypatch):
    """A chronic warning dumps the ring once per floor interval; a
    different call site still gets its own dump immediately. A repeat
    within the floor (#4122) stays suppressed; one past it lands."""
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    now = [1_000.0]
    monkeypatch.setattr(fr.time, "monotonic", lambda: now[0])
    ring.emit(_rec(logging.WARNING, "churn"))
    ring.emit(_rec(logging.WARNING, "churn"))
    assert s.getvalue().count("event=flightrec.dump ") == 1
    # Same message, different logger -> different signature.
    ring.emit(_rec(logging.WARNING, "churn", name="jasper.other"))
    assert s.getvalue().count("event=flightrec.dump ") == 2
    now[0] += fr.AUTO_FLUSH_MIN_INTERVAL_SEC - 100.0  # short of the floor
    ring.emit(_rec(logging.WARNING, "churn"))
    assert s.getvalue().count("event=flightrec.dump ") == 2  # still suppressed
    now[0] += fr.AUTO_FLUSH_MIN_INTERVAL_SEC
    ring.emit(_rec(logging.WARNING, "churn"))
    assert s.getvalue().count("event=flightrec.dump ") == 3  # lands past the floor


def test_auto_flush_floor_keys_log_event_on_the_event_name():
    """`log_event` renders field VALUES into the message, so a varying
    field would make every occurrence a fresh signature. The floor keys
    those records on the event name instead."""
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    logger = logging.getLogger("jasper.test.logevent")
    logger.addHandler(ring)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        for attempt in range(3):
            log_event(
                logger,
                "live.reconnect",
                level=logging.WARNING,
                attempt=attempt,
            )
        assert s.getvalue().count("event=flightrec.dump ") == 1
        # A different event from the same logger is not suppressed.
        log_event(logger, "live.dropped", level=logging.WARNING)
        assert s.getvalue().count("event=flightrec.dump ") == 2
    finally:
        logger.removeHandler(ring)


def test_explicit_dump_is_never_floored():
    """The floor is for automatic dumps only — an operator's SIGUSR1 or
    a 'flag that' must always write what is in the ring."""
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    ring.emit(_rec(logging.WARNING, "churn"))
    ring.emit(_rec(logging.WARNING, "churn"))  # floored
    assert ring.flush_buffer("manual") == 1


def test_no_flush_on_info_or_debug():
    s = io.StringIO()
    ring = fr.RingFlushHandler(10, s)
    ring.emit(_rec(logging.INFO, "x"))
    ring.emit(_rec(logging.DEBUG, "y"))
    assert s.getvalue() == ""
    assert len(ring.buffer) == 2


def test_flush_empty_is_noop():
    assert fr.RingFlushHandler(3, io.StringIO()).flush_buffer("x") == 0


def test_direct_flush_holds_handler_lock():
    class TrackingLock:
        def __init__(self):
            self.calls: list[str] = []
            self.depth = 0

        def acquire(self):
            self.calls.append("acquire")
            self.depth += 1

        def release(self):
            self.calls.append("release")
            self.depth -= 1

    ring = fr.RingFlushHandler(3, io.StringIO())
    ring.emit(_rec(logging.DEBUG, "context"))
    tracking = TrackingLock()
    ring.lock = tracking

    assert ring.flush_buffer("manual") == 1
    assert tracking.calls == ["acquire", "release"]
    assert tracking.depth == 0


def test_ring_stores_formatted_strings_not_records():
    """Eager formatting: the ring holds str lines, not LogRecord objects, so
    a large object passed as a log arg is rendered to text and not pinned in
    the ring (the bounded-RAM property)."""
    ring = fr.RingFlushHandler(10, io.StringIO())
    payload = ["chunk"] * 500  # a big object passed as a log arg
    ring.emit(logging.LogRecord(
        "jasper.x", logging.DEBUG, "f.py", 1, "payload=%s", (payload,), None))
    assert len(ring.buffer) == 1
    assert isinstance(ring.buffer[0], str)  # a string, not the LogRecord/list
    assert "chunk" in ring.buffer[0]


# -------------------------------------------------------------------- install


def test_install_sets_logger_debug_console_info_and_attaches_ring(
    logging_sandbox, monkeypatch, tmp_path
):
    console = logging_sandbox
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    ok = fr.install("voice", capacity=50, dump_stream=io.StringIO())
    assert ok is True
    assert logging.getLogger("jasper").level == logging.DEBUG
    assert console.level == logging.INFO          # DEBUG stays out of the journal
    assert fr._ring in logging.getLogger("jasper").handlers


def test_install_twice_leaves_one_ring_handler_attached(
    logging_sandbox, monkeypatch, tmp_path
):
    """install() is idempotent: a second call must not stack a second
    RingFlushHandler onto the jasper logger (issue #3471 item 1)."""
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    fr.install("voice", capacity=50, dump_stream=io.StringIO())
    fr.install("voice", capacity=50, dump_stream=io.StringIO())
    ring_handlers = [
        h for h in logging.getLogger("jasper").handlers
        if isinstance(h, fr.RingFlushHandler)
    ]
    assert len(ring_handlers) == 1


def test_install_applies_active_toggle_to_console(
    logging_sandbox, monkeypatch, tmp_path
):
    console = logging_sandbox
    f = tmp_path / "debug.env"
    f.write_text("JASPER_DEBUG_VOICE=1\n")
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(f))
    fr.install("voice", capacity=50, dump_stream=io.StringIO())
    assert console.level == logging.DEBUG          # toggled on -> journal shows DEBUG


def test_install_disabled_falls_back_to_plain_toggle(
    logging_sandbox, monkeypatch, tmp_path
):
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    monkeypatch.setenv("JASPER_FLIGHT_RECORDER", "disabled")
    ok = fr.install("voice", dump_stream=io.StringIO())
    assert ok is False
    assert fr._ring is None


def test_install_disabled_still_installs_sigusr1_handler(
    logging_sandbox, monkeypatch, tmp_path
):
    """SIGUSR1 defaults to *terminate*, and an operator can force a dump
    with `systemctl kill -s USR1`. The handler must be installed even
    when the recorder is off so that path is a safe no-op."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers require the main thread")
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    monkeypatch.setenv("JASPER_FLIGHT_RECORDER", "disabled")
    prev = signal.getsignal(signal.SIGUSR1)
    try:
        assert fr.install("voice", dump_stream=io.StringIO()) is False
        assert fr._ring is None
        assert signal.getsignal(signal.SIGUSR1) not in (signal.SIG_DFL, signal.SIG_IGN)
        assert fr.dump("signal") == 0  # firing it with no ring is a safe no-op
    finally:
        signal.signal(signal.SIGUSR1, prev)


def test_dump_flushes_installed_ring_with_captured_context(
    logging_sandbox, monkeypatch, tmp_path
):
    monkeypatch.setattr(debug_mode, "DEBUG_FILE", str(tmp_path / "debug.env"))
    dump_stream = io.StringIO()
    fr.install("voice", capacity=50, dump_stream=dump_stream)
    logging.getLogger("jasper.somewhere").debug("a debug breadcrumb")
    n = fr.dump("voice_flagged")
    out = dump_stream.getvalue()
    assert n >= 1
    assert "reason=voice_flagged" in out
    assert "a debug breadcrumb" in out


def test_dump_without_install_is_noop(logging_sandbox):
    assert fr.dump("x") == 0
