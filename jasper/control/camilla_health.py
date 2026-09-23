# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CamillaDSP observation for the audio-health collector: the bounded runtime
probe and the CamillaDSP journal scan, and the rate-storm detector they feed.

CamillaDSP sits on every source's audio path, so none of this is AirPlay's.
:class:`~jasper.control.airplay_health.AirPlayHealthSampler` composes one
:class:`CamillaHealth`, publishes it as ``snapshot()["current"]["camilla"]``
and ``snapshot()["storm"]``, and records its journal events in the AirPlay
event bucket.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any

from jasper.camilla import CamillaController
from jasper.camilla_config_contract import (
    DEFAULT_CAMILLA_PORT,
    read_camilla_devices_config,
)
from jasper.control._health_fields import _as_int
from jasper.control.camilla_rate_storm import (
    STORM_SAMPLE_INTERVAL_SEC,
    CamillaRateStorm,
)

logger = logging.getLogger(__name__)

CAMILLA_UNIT = "jasper-camilla"
CAMILLA_INTERVAL_SEC = 30.0
CAMILLA_SHORT_READ_RE = re.compile(
    r"Capture read (?P<read>\d+) frames instead of the requested (?P<requested>\d+)",
)

# CamillaDSP logs a warning for any partial ALSA read, then immediately loops to
# read the remaining frames before emitting the chunk. Tiny recovered partials
# are normal with the plug/dsnoop/rate-adjust path and do not indicate an
# audio-path recovery event by themselves.
BENIGN_CAMILLA_SHORT_READ_DEFICIT_RATIO = 0.01


def classify_camilla_line(unit: str, line: str) -> dict[str, Any] | None:
    """Classify one CamillaDSP journal line into the compact dashboard event
    shape. The patterns are literal; unknown lines and other units' lines are
    ignored.
    """
    if unit != CAMILLA_UNIT:
        return None
    m = CAMILLA_SHORT_READ_RE.search(line)
    if m:
        frames_read = _as_int(m.group("read"))
        frames_requested = _as_int(m.group("requested"))
        deficit = max(0, frames_requested - frames_read)
        if frames_requested > 0:
            benign_deficit = math.ceil(
                frames_requested * BENIGN_CAMILLA_SHORT_READ_DEFICIT_RATIO,
            )
            if deficit <= benign_deficit:
                return None
        return {
            "type": "camilla_short_read",
            "subsystem": "camilla",
            "severity": "watch",
            "title": "Camilla short read",
            "detail": (
                f"capture delivered {frames_read}/{frames_requested} "
                "frames"
            ),
            "frames_read": frames_read,
            "frames_requested": frames_requested,
            "deficit_frames": deficit,
        }
    if (
        "Prepare playback after buffer underrun" in line
        or "playback_underrun" in line
        or "Could not write" in line
        or "Broken pipe" in line
    ):
        return {
            "type": "camilla_playback_underrun",
            "subsystem": "camilla",
            "severity": "issue",
            "title": "Camilla playback underrun",
            "detail": "playback buffer underrun",
        }
    return None


class CamillaHealth:
    """The CamillaDSP runtime probe (:meth:`sample`) and journal scan
    (:meth:`scan_journal`), each run when due and each feeding the composed
    :class:`~jasper.control.camilla_rate_storm.CamillaRateStorm`. ``time_fn``
    starts the journal cursor.
    """

    def __init__(
        self,
        *,
        probe: Callable[[], dict[str, Any] | None] | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_CAMILLA_PORT,
        rate_storm: CamillaRateStorm | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._probe = probe or (lambda: self._read_camilla_state(host, port))
        self._rate_storm = rate_storm or CamillaRateStorm()
        self._current: dict[str, Any] | None = None
        self._last_sample_at = 0.0
        self._last_scan_at = 0.0
        self._journal_since = time_fn()

    @property
    def current(self) -> dict[str, Any] | None:
        """The latest runtime probe, or None when CamillaDSP was unreadable.
        The live dict, not a copy: readers must not mutate it."""
        return self._current

    def storm_snapshot(self) -> dict[str, Any]:
        return self._rate_storm.snapshot()

    def sample(self, now: float) -> None:
        """Probe CamillaDSP every :data:`CAMILLA_INTERVAL_SEC`. While storming,
        probe at the faster storm cadence and append a trajectory row each
        time (Tier 2).
        """
        interval = (
            STORM_SAMPLE_INTERVAL_SEC
            if self._rate_storm.active else CAMILLA_INTERVAL_SEC
        )
        if now - self._last_sample_at < interval:
            return
        try:
            current = self._probe()
        except Exception:  # noqa: BLE001
            logger.debug("camilla state probe failed", exc_info=True)
            current = None
        self._current = current if isinstance(current, dict) else None
        self._last_sample_at = now
        if self._rate_storm.active:
            self._rate_storm.append_trajectory(now, self._current)

    def scan_journal(
        self,
        now: float,
        *,
        suppress: bool,
        interval_sec: float,
        scan: Callable[..., list[dict[str, Any]]],
        active_source: str | None,
    ) -> None:
        """Scan CamillaDSP's journal every ``interval_sec`` through the
        collector's ``scan(unit, since, now, classify)``, which records each
        event and returns them, and feed the scan to the storm detector. A
        suppressed tick skips its window instead.
        """
        if suppress:
            self._journal_since = max(self._journal_since, now)
            self._last_scan_at = now
            return
        if now - self._last_scan_at < interval_sec:
            return
        scan_window = now - self._last_scan_at if self._last_scan_at else 0.0
        events = scan(CAMILLA_UNIT, self._journal_since, now, classify_camilla_line)
        self._journal_since = now
        self._last_scan_at = now
        self._rate_storm.update(
            now,
            sum(event.get("type") == "camilla_short_read" for event in events),
            scan_window,
            camilla=self._current,
            active_source=active_source,
        )

    @staticmethod
    def _read_camilla_state(host: str, port: int) -> dict[str, Any] | None:
        try:
            async def read() -> tuple[dict[str, Any], str | None]:
                controller = CamillaController(host, port)
                try:
                    status = await controller.get_runtime_status()
                    if status is None or not all(
                        key in status
                        for key in (
                            "buffer_level", "rate_adjust", "capture_rate",
                        )
                    ):
                        raise OSError("incomplete CamillaDSP runtime status")
                    config_path = await controller.get_config_file_path(
                        best_effort=True,
                    )
                    return status, config_path
                finally:
                    await controller.close()

            out, config_path = asyncio.run(read())
            if config_path:
                out["config_path"] = config_path
                devices = read_camilla_devices_config(config_path)
                if devices:
                    out.update(devices)
            return out
        except Exception:  # noqa: BLE001
            return None
