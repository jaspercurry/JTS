# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Log flight recorder — Tier C of the observability plan.

Keeps the last N DEBUG+ log records per daemon in a bounded in-RAM ring
and dumps them to the journal (tagged ``event=flightrec.dump``) only
when an anomaly fires — a WARNING/ERROR record (automatic), or an
explicit :func:`dump`: the "flag that" voice tool or an operator
``systemctl kill -s USR1 jasper-voice``. A doctor-fail auto-trigger
was considered and dropped; see ``docs/HANDOFF-observability.md``.

*Why.* The intermittent bugs that matter most already happened before
anyone could flip the Tier-B debug toggle. The ring captures the
verbose window around every anomaly automatically, at the cost of RAM
(not SD writes) — the same idea as the wake-event 6 s audio rings,
generalized to logs. See ``docs/HANDOFF-observability.md``.

*Mechanism — decouple the logger level from the journal level.*

* the ``jasper`` logger runs at DEBUG (so DEBUG records exist),
* the live journal ``StreamHandler`` stays at INFO (so DEBUG never hits
  the SD card) — the Tier-B Debug card lowers *this* handler to DEBUG,
* :class:`RingFlushHandler` at DEBUG buffers everything and writes the
  buffer to a separate dump stream only on flush.

Additive-only: the recorder never lowers a level; it only adds the ring
and dumps on anomaly. Off via ``JASPER_FLIGHT_RECORDER=disabled`` (then
the Tier-B toggle falls back to the plain logger-level path).
"""
from __future__ import annotations

import collections
import logging
import os
import signal
import sys

from . import debug_mode

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 1000  # stores formatted lines (~0.3 KB) -> ~0.3 MB/daemon
FLUSH_LEVEL = logging.WARNING
_FLIGHTREC_FORMAT = "%(asctime)s flightrec %(levelname)s %(name)s: %(message)s"

_ring: "RingFlushHandler | None" = None


class RingFlushHandler(logging.Handler):
    """A bounded ring of recent *formatted log lines*, written to
    ``dump_stream`` (as a tagged burst) only when a WARNING+ record passes
    through or :meth:`flush_buffer` is called explicitly.

    Records are formatted to strings **eagerly** in :meth:`emit` and only
    the string is kept, so the ring's memory is bounded by line length and
    can never pin a large object passed as a log arg (the storing-LogRecord
    tail risk). Unlike stdlib ``MemoryHandler`` it never flushes on capacity
    (the ``deque`` drops oldest instead) and its dump target is a plain
    stream, not an INFO-filtered handler that would drop the DEBUG lines.
    """

    def __init__(self, capacity: int, dump_stream) -> None:
        super().__init__(level=logging.DEBUG)
        self.buffer: collections.deque = collections.deque(maxlen=capacity)
        self.dump_stream = dump_stream
        self.setFormatter(logging.Formatter(_FLIGHTREC_FORMAT))
        self._dumping = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._dumping:
            return  # don't re-buffer anything emitted during a dump
        try:
            line = self.format(record)  # store the formatted string, not the record
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive; never crash the caller
            return
        self.buffer.append(line)
        if record.levelno >= FLUSH_LEVEL:
            self.flush_buffer("auto:" + record.levelname.lower())

    def flush_buffer(self, reason: str) -> int:
        """Write the buffered lines to the dump stream and clear the ring.
        Returns the number of lines dumped. Best-effort — a dump must never
        crash the daemon it's recording."""
        if self._dumping:
            return 0
        lines = list(self.buffer)
        self.buffer.clear()
        if not lines:
            return 0
        self._dumping = True
        n = len(lines)
        try:
            self.dump_stream.write(
                f"flightrec event=flightrec.dump reason={reason} records={n}\n"
            )
            for line in lines:
                self.dump_stream.write(line + "\n")
            self.dump_stream.write(
                f"flightrec event=flightrec.dump.end reason={reason} records={n}\n"
            )
            self.dump_stream.flush()
        except Exception:  # noqa: BLE001  # pragma: no cover - defensive
            pass
        finally:
            self._dumping = False
        return n


def _disabled() -> bool:
    return os.environ.get("JASPER_FLIGHT_RECORDER", "").strip().lower() == "disabled"


def _install_sigusr1() -> None:
    # SIGUSR1 -> dump. Lets an operator force a dump without a code path of
    # their own. signal.signal only works on the main thread;
    # a daemon installing from a worker thread silently skips it.
    try:
        signal.signal(signal.SIGUSR1, lambda *_: dump("signal"))
    except (ValueError, OSError):  # pragma: no cover - non-main-thread
        pass


def install(
    subsystem: str, *, capacity: int = DEFAULT_CAPACITY, dump_stream=None,
) -> bool:
    """Install the flight recorder for a daemon (call right after
    ``logging.basicConfig``). Sets the ``jasper`` logger to DEBUG, pins the
    live journal handler at INFO, attaches the ring, applies the Tier-B
    debug toggle, and wires SIGUSR1. Returns whether the recorder was
    installed (False if disabled by env)."""
    global _ring
    # Install the SIGUSR1 -> dump handler UNCONDITIONALLY, even when the
    # recorder is disabled: an *unhandled* SIGUSR1 terminates the process by
    # default, and an operator can `systemctl kill -s USR1 jasper-voice` to
    # force a dump — so a missing handler would kill the daemon. dump() is a
    # safe no-op while _ring is None.
    _install_sigusr1()
    if _disabled():
        debug_mode.apply_for(subsystem)  # plain Tier-B toggle, no ring
        logger.info("flight recorder: disabled via JASPER_FLIGHT_RECORDER")
        return False
    debug_mode.set_console_debug(False)  # pin journal at INFO — keep DEBUG off the SD card
    logging.getLogger("jasper").setLevel(logging.DEBUG)  # records exist for the ring
    # NOTE: with the logger pinned at DEBUG, `logger.isEnabledFor(DEBUG)` is
    # always True for jasper.* — so a per-frame `logger.debug(...)` on a hot
    # audio path is no longer free (it builds a record + a formatted string
    # every frame). Keep hot-loop logging coarser than DEBUG, or rate-limit it.
    _ring = RingFlushHandler(capacity, dump_stream or sys.stderr)
    logging.getLogger("jasper").addHandler(_ring)
    # Apply the persisted Tier-B debug toggle for this subsystem (raises the
    # journal handler to DEBUG when this subsystem is toggled on).
    try:
        debug_mode.apply_for(subsystem)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive; startup must survive
        pass
    logger.info(
        "flight recorder: installed for %s (ring=%d records; "
        "dump -> journal on WARNING+/flag/signal)",
        subsystem, capacity,
    )
    return True


def dump(reason: str = "manual") -> int:
    """Explicitly flush the ring to the journal. Used by the "flag that"
    voice tool and the SIGUSR1 handler. No-op (0) if not installed."""
    if _ring is None:
        return 0
    try:
        return _ring.flush_buffer(reason)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return 0
