# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Publish jasper-usbsink's playing state to /run/jasper-usbsink/state.json.

State shape (read by jasper.source_state.usbsink_playing, the
/state aggregator on jasper-control, and jasper-doctor):

    {
      "playing": bool,         RMS-based, hysteresis-debounced
      "preempted": bool,       mux has silenced us
      "host_connected": bool,  /proc/asound/UAC2Gadget present
      "rms_dbfs": float,       last observed (-inf when silent)
      "updated_at": str        ISO 8601 UTC
    }

Atomic writes (tempfile + os.replace) so partial JSON never lands on
disk. World-readable file mode (0644) so jasper-voice, jasper-control,
jasper-mux can all read without being in the service's group.

Hysteresis is the load-bearing piece:

  RMS active for >= ACTIVE_DEBOUNCE_SEC      → playing transitions to True
  RMS inactive for >= INACTIVE_DEBOUNCE_SEC  → playing transitions to False

Without hysteresis, mux would see "playing" flap on every short pause
between tracks, and the latest-source-wins semantics would chaotically
re-preempt. The asymmetric debounces (faster to recognise start than
stop) match how users perceive transport: "I just hit play" should
feel near-instant; "I paused and now I'm doing something else" can
tolerate a 2-second tail.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..log_event import log_event

logger = logging.getLogger(__name__)


# Threshold for "audio is happening". -50 dBFS is well above the
# digital noise floor of any real source and well below conversational
# music levels even at low volumes. Hosts that emit pure silence
# during pauses (most macOS apps) stay decisively below this; music
# stays decisively above it.
RMS_ACTIVE_DBFS = -50.0

# Hysteresis durations — see module docstring.
ACTIVE_DEBOUNCE_SEC = 1.0
INACTIVE_DEBOUNCE_SEC = 2.0

# State file location. Created by systemd's RuntimeDirectory= directive
# (mode 0755, owned by the service user). The tmpfile lives in the
# same directory so os.replace is an atomic rename within one fs.
DEFAULT_STATE_PATH = "/run/jasper-usbsink/state.json"
DEFAULT_HOST_CARD_PATH = "/proc/asound/UAC2Gadget"

# Publish cadence. State changes are written immediately on transition,
# but we also write at this cadence so the `updated_at` field has a
# steady cadence — useful for staleness detection in jasper-doctor.
TICK_SEC = 1.0


@dataclass
class _DebounceState:
    """Internal hysteresis tracker. Read by the publish loop only."""
    last_active_change_mono: float = 0.0
    currently_above_threshold: bool = False
    published_playing: bool = False


class StatePublisher:
    """Reads `AudioBridge.last_rms_dbfs` on a 1 Hz tick, applies
    hysteresis, writes JSON state atomically.

    The publisher does NOT directly read `is_preempted` from the
    bridge for the published `playing` field — `playing` reflects the
    user's intent (RMS-active, regardless of mux preempt). `preempted`
    is published separately so the mux's release-on-idle logic can
    see both.

    This split matters because of how mux detects edges. If `playing`
    flipped to False whenever preempted, the daemon would emit a
    spurious "stopped" event on every preempt — which mux would
    re-evaluate as "USBSINK newly inactive, but USBSINK can still come
    back later". Keeping `playing` purely RMS-driven means USBSINK's
    edges in the state file reflect only what the user does at the
    host (pause/resume), which is the signal mux wants.
    """

    def __init__(
        self,
        bridge,
        state_path: str = DEFAULT_STATE_PATH,
        host_card_path: str = DEFAULT_HOST_CARD_PATH,
        *,
        rms_active_dbfs: float = RMS_ACTIVE_DBFS,
        active_debounce_sec: float = ACTIVE_DEBOUNCE_SEC,
        inactive_debounce_sec: float = INACTIVE_DEBOUNCE_SEC,
    ) -> None:
        self._bridge = bridge
        self._state_path = Path(state_path)
        self._host_card_path = Path(host_card_path)
        self._threshold = rms_active_dbfs
        self._active_debounce = active_debounce_sec
        self._inactive_debounce = inactive_debounce_sec
        self._debounce = _DebounceState()
        self._last_written = None  # type: dict | None

    async def run(self) -> None:
        """Tick-driven loop. Cancellable from the daemon's shutdown
        path. A failing _tick logs and continues — one bad write (e.g.
        ENOSPC, transient FS issue) shouldn't take the whole daemon
        down. Persistent failures get caught by the daemon's diag
        log + jasper-doctor's staleness warning."""
        # Ensure parent dir exists. RuntimeDirectory= should have
        # already done this, but be defensive for non-systemd dev runs.
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            while True:
                await asyncio.sleep(TICK_SEC)
                try:
                    self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        "usbsink.state_tick_error",
                        error=e,
                        level=logging.WARNING,
                    )
        except asyncio.CancelledError:
            # On shutdown, write a final state with playing=False so
            # downstream consumers don't see stale "still playing".
            self._debounce.published_playing = False
            try:
                self._write_state(force_log=False)
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "usbsink.final_state_write_failed",
                    error=e,
                    level=logging.DEBUG,
                )
            raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        rms = self._bridge.last_rms_dbfs
        now = time.monotonic()
        above = rms > self._threshold
        if above != self._debounce.currently_above_threshold:
            self._debounce.currently_above_threshold = above
            self._debounce.last_active_change_mono = now
        # Decide whether to flip published_playing.
        held_for = now - self._debounce.last_active_change_mono
        if (
            above
            and not self._debounce.published_playing
            and held_for >= self._active_debounce
        ):
            self._debounce.published_playing = True
            log_event(
                logger,
                "usbsink.playing_started",
                rms_dbfs=f"{rms:.1f}",
                held_sec=f"{held_for:.1f}",
            )
        elif (
            not above
            and self._debounce.published_playing
            and held_for >= self._inactive_debounce
        ):
            self._debounce.published_playing = False
            log_event(
                logger,
                "usbsink.playing_stopped",
                rms_dbfs=f"{rms:.1f}",
                held_sec=f"{held_for:.1f}",
            )

        self._write_state()

    def _write_state(self, *, force_log: bool = False) -> None:
        host_connected = self._host_card_path.exists()
        payload = {
            "playing": self._debounce.published_playing,
            "preempted": self._bridge.is_preempted,
            "host_connected": host_connected,
            "rms_dbfs": self._bridge.last_rms_dbfs,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # State-change log surfaces preempted / host_connected edges
        # too, not just playing edges — both are useful in incident
        # debugging.
        if self._last_written is not None:
            for key in ("preempted", "host_connected"):
                if payload[key] != self._last_written.get(key):
                    log_event(
                        logger,
                        f"usbsink.{key}_changed",
                        value=payload[key],
                    )

        try:
            self._atomic_write(payload)
            self._last_written = payload
        except OSError as e:
            log_event(
                logger,
                "usbsink.state_write_failed",
                path=self._state_path,
                error=e,
                level=logging.WARNING,
            )

    def _atomic_write(self, payload: dict) -> None:
        """Write tempfile + os.replace. Same pattern as
        jasper.mic_mute_persistence — partial JSON on crash is
        impossible."""
        data = json.dumps(payload, indent=None, separators=(",", ":"))
        # Tempfile in the same directory so os.replace is fs-atomic.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".state.", suffix=".json.tmp",
            dir=str(self._state_path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            # World-readable so jasper-voice / jasper-control / mux can
            # see it without privilege.
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, self._state_path)
        except Exception:  # noqa: BLE001
            # Tidy up the tempfile on any failure.
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
