# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Storm-triggered forensic capture for CamillaDSP's rate-adjust loop:
Tier 1 onset/offset events plus the Tier 2 in-storm controller trajectory.

A "storm" is a sustained run of *material* Camilla short reads — the
rate-adjust PI loop hunting against a drifting DAC clock (the Apple USB-C
dongle). It is INAUDIBLE: across 137k short reads over 72 h of real use it
produced zero playback underruns, because CamillaDSP loops to fill every
short read before emitting the chunk. But it is slow-developing (tens of
minutes into a listening session), intermittent, and metastable (a config
reload / restart clears it), so it cannot be reproduced on demand. These
hooks capture the rate-controller state WHEN IT ACTUALLY HAPPENS so the
mechanism and any future at-source tuning can be evaluated from real data
instead of reconstructed after the fact. It fires on any source's audio
path, not only AirPlay's.
"""
from __future__ import annotations

import copy
import datetime
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from jasper.control._health_fields import read_int_file, read_text_file
from jasper.control.system_metrics import read_thermal_zone_temp_c
from jasper.install_profile import BUILD_MANIFEST_FILE
from jasper.log_event import log_event
from jasper.service_units import CAMILLA_SERVICE, read_unit_states, unit_uptime_sec

logger = logging.getLogger(__name__)

STORM_ENTER_PER_MIN = 120.0
STORM_EXIT_PER_MIN = 30.0
STORM_EXIT_DEBOUNCE_SEC = 90.0
STORM_MIN_SCAN_WINDOW_SEC = 15.0
STORM_SAMPLE_INTERVAL_SEC = 5.0
STORM_TRAJECTORY_DIR = "/var/lib/jasper/rate-storms"
STORM_TRAJECTORY_MAX_ROWS = 4000
STORM_TRAJECTORY_KEEP_FILES = 20

CPU_GOVERNOR_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
CPU_FREQ_PATH = "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"


def _read_soc_temp_c() -> float | None:
    raw = read_thermal_zone_temp_c()
    return round(raw, 1) if raw is not None else None


def _seconds_since_camilla_restart() -> float | None:
    """Seconds since jasper-camilla last (re)started — the controller-reset age.

    A camilla restart resets the rate controller, so "did this storm start
    shortly after a restart/deploy?" is the field that settles whether
    restarts SEED storms (vs only clearing them — the open question from the
    deploy-correlation investigation).
    """
    states = read_unit_states((CAMILLA_SERVICE,))
    uptime = unit_uptime_sec((states or {}).get(CAMILLA_SERVICE))
    return round(uptime, 1) if uptime is not None else None


def _seconds_since_deploy(now_wall: float) -> float | None:
    """Seconds since the last install wrote the build marker — the deploy age."""
    try:
        mtime = os.stat(BUILD_MANIFEST_FILE).st_mtime
    except OSError:
        return None
    return round(max(0.0, now_wall - mtime), 1)


def _default_context_probe(now_wall: float) -> dict[str, Any]:
    """Cheap correlation context captured once at storm onset."""
    return {
        "soc_temp_c": _read_soc_temp_c(),
        "cpu_governor": read_text_file(CPU_GOVERNOR_PATH),
        "cpu_freq_khz": read_int_file(CPU_FREQ_PATH),
        "sec_since_camilla_restart": _seconds_since_camilla_restart(),
        "sec_since_deploy": _seconds_since_deploy(now_wall),
    }


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


class _StormTrajectory:
    """Bounded CSV artifact for one storm's controller trajectory (Tier 2).

    Fail-soft by construction: any directory/open/write error leaves ``path``
    None and makes every method a no-op, so a filesystem problem never
    disturbs the sampler loop or the Tier-1 onset/offset events. Capped per
    storm (``max_rows``) and across storms (``keep_files`` retained, oldest
    pruned), mirroring the wake-events ring.
    """

    _HEADER = (
        "t_sec", "rate_adjust", "capture_rate", "buffer_level",
        "soc_temp_c", "cpu_freq_khz", "material_per_min",
    )

    def __init__(
        self, dir_path: str | None, onset_stamp: str, *,
        max_rows: int, keep_files: int,
    ) -> None:
        self.path: str | None = None
        self._fh: Any = None
        self._rows = 0
        self._max_rows = max_rows
        if not dir_path:
            return
        try:
            os.makedirs(dir_path, exist_ok=True)
            self._prune(dir_path, keep_files)
            path = os.path.join(dir_path, f"storm-{onset_stamp}.csv")
            fh = open(path, "w", encoding="utf-8")
            fh.write(",".join(self._HEADER) + "\n")
            fh.flush()
            self._fh = fh
            self.path = path
        except OSError:
            logger.debug("storm trajectory open failed", exc_info=True)
            self._fh = None
            self.path = None

    def append(self, row: dict[str, Any]) -> None:
        if self._fh is None or self._rows >= self._max_rows:
            return
        try:
            self._fh.write(
                ",".join(_csv_cell(row.get(k)) for k in self._HEADER) + "\n"
            )
            self._fh.flush()
            self._rows += 1
        except OSError:
            logger.debug("storm trajectory append failed", exc_info=True)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        self._fh = None

    @staticmethod
    def _prune(dir_path: str, keep_files: int) -> None:
        try:
            existing = [
                os.path.join(dir_path, name)
                for name in os.listdir(dir_path)
                if name.startswith("storm-") and name.endswith(".csv")
            ]
        except OSError:
            return
        existing.sort(key=_safe_mtime)
        # Keep room for the file about to be opened: retain keep_files-1.
        cutoff = max(0, len(existing) - max(0, keep_files - 1))
        for old in existing[:cutoff]:
            try:
                os.remove(old)
            except OSError:
                pass


class CamillaRateStorm:
    """Fed each Camilla journal scan (:meth:`update`) and, while
    :attr:`active`, a Camilla status sample every
    :data:`STORM_SAMPLE_INTERVAL_SEC` (:meth:`append_trajectory`).
    """

    def __init__(
        self, *,
        exit_debounce_sec: float = STORM_EXIT_DEBOUNCE_SEC,
        trajectory_dir: str | None = STORM_TRAJECTORY_DIR,
        context_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._exit_debounce_sec = exit_debounce_sec
        self._trajectory_dir = trajectory_dir
        self._context_probe = context_probe or (
            lambda: _default_context_probe(time.time())
        )
        self._active = False
        self._started_at: float | None = None
        self._peak_per_min = 0.0
        self._below_exit_since: float | None = None
        self._onset: dict[str, Any] | None = None
        self._trajectory: _StormTrajectory | None = None
        self._extent: dict[str, Any] = {}
        self._samples = 0
        self._count = 0
        self._last_material_per_min = 0.0

    @property
    def active(self) -> bool:
        return self._active

    def snapshot(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "count": self._count,
            "started_at": self._started_at,
            "material_per_min": round(self._last_material_per_min, 1),
            "peak_per_min": (
                round(self._peak_per_min, 1) if self._active else None
            ),
            "samples": self._samples if self._active else 0,
            "onset": copy.deepcopy(self._onset),
        }

    def update(
        self, now: float, material_short_reads: int, scan_window: float, *,
        camilla: dict[str, Any] | None, active_source: str | None,
    ) -> None:
        """Edge-detect the rate-loop short-read storm from one journal scan.

        Enter on a sustained material-short-read rate (guarded by a minimum
        scan window so a tiny-window rate spike can't false-trigger); exit on
        a debounced drop below the hysteresis floor. Fail-soft: any error here
        is observability-only and must never perturb the sampler.
        """
        material_per_min = (
            material_short_reads / scan_window * 60.0 if scan_window > 0 else 0.0
        )
        self._last_material_per_min = material_per_min
        try:
            if not self._active:
                if (
                    scan_window >= STORM_MIN_SCAN_WINDOW_SEC
                    and material_per_min >= STORM_ENTER_PER_MIN
                ):
                    self._enter(now, material_per_min, camilla, active_source)
                return
            if material_per_min > self._peak_per_min:
                self._peak_per_min = material_per_min
            if material_per_min < STORM_EXIT_PER_MIN:
                if self._below_exit_since is None:
                    self._below_exit_since = now
                elif now - self._below_exit_since >= self._exit_debounce_sec:
                    self._exit(now)
            else:
                self._below_exit_since = None
        except Exception:  # noqa: BLE001
            logger.debug("storm state update failed", exc_info=True)

    def _enter(
        self, now: float, material_per_min: float,
        camilla: dict[str, Any] | None, active_source: str | None,
    ) -> None:
        # Build the onset snapshot BEFORE committing storm state, so a context
        # probe failure can't leave a half-entered storm (no onset event /
        # trajectory but `_active` latched True). Any raise here propagates
        # to update()'s single guard before state is touched.
        context = self._safe_context()
        cam = camilla if isinstance(camilla, dict) else {}
        onset: dict[str, Any] = {
            "material_per_min": round(material_per_min, 1),
            "rate_adjust": cam.get("rate_adjust"),
            "capture_rate": cam.get("capture_rate"),
            "buffer_level": cam.get("buffer_level"),
            "active_source": active_source,
            **context,
        }
        self._active = True
        self._started_at = now
        self._peak_per_min = material_per_min
        self._below_exit_since = None
        self._samples = 0
        self._extent = {}
        self._count += 1
        self._onset = onset
        log_event(
            logger, "camilla_rate.storm_onset",
            level=logging.WARNING, fields=onset,
        )
        self._trajectory = _StormTrajectory(
            self._trajectory_dir, self._stamp(now),
            max_rows=STORM_TRAJECTORY_MAX_ROWS,
            keep_files=STORM_TRAJECTORY_KEEP_FILES,
        )
        # Row 0 captures the onset state itself.
        self.append_trajectory(now, camilla)

    def _exit(self, now: float) -> None:
        duration = now - (self._started_at or now)
        ext = self._extent
        traj = self._trajectory
        offset: dict[str, Any] = {
            "duration_sec": round(duration, 1),
            "peak_per_min": round(self._peak_per_min, 1),
            "samples": self._samples,
            "rate_adjust_min": ext.get("rate_adjust_min"),
            "rate_adjust_max": ext.get("rate_adjust_max"),
            "buffer_min": ext.get("buffer_min"),
            "buffer_max": ext.get("buffer_max"),
            "soc_temp_start_c": ext.get("soc_temp_start"),
            "soc_temp_end_c": ext.get("soc_temp_end"),
            "artifact": traj.path if traj is not None else None,
        }
        log_event(
            logger, "camilla_rate.storm_offset",
            level=logging.WARNING, fields=offset,
        )
        if traj is not None:
            traj.close()
        self._trajectory = None
        self._active = False
        self._started_at = None
        self._below_exit_since = None
        self._onset = None

    def append_trajectory(self, now: float, camilla: dict[str, Any] | None) -> None:
        cam = camilla if isinstance(camilla, dict) else {}
        rate_adjust = cam.get("rate_adjust")
        buffer_level = cam.get("buffer_level")
        soc_temp = _read_soc_temp_c()
        row = {
            "t_sec": round(now - (self._started_at or now), 1),
            "rate_adjust": rate_adjust,
            "capture_rate": cam.get("capture_rate"),
            "buffer_level": buffer_level,
            "soc_temp_c": soc_temp,
            "cpu_freq_khz": read_int_file(CPU_FREQ_PATH),
            "material_per_min": round(self._last_material_per_min, 1),
        }
        ext = self._extent
        if isinstance(rate_adjust, (int, float)):
            ext["rate_adjust_min"] = min(ext.get("rate_adjust_min", rate_adjust), rate_adjust)
            ext["rate_adjust_max"] = max(ext.get("rate_adjust_max", rate_adjust), rate_adjust)
        if isinstance(buffer_level, (int, float)):
            ext["buffer_min"] = min(ext.get("buffer_min", buffer_level), buffer_level)
            ext["buffer_max"] = max(ext.get("buffer_max", buffer_level), buffer_level)
        if soc_temp is not None:
            ext.setdefault("soc_temp_start", soc_temp)
            ext["soc_temp_end"] = soc_temp
        self._samples += 1
        if self._trajectory is not None:
            self._trajectory.append(row)

    def _safe_context(self) -> dict[str, Any]:
        # The default probe is internally fail-soft (every reader returns None
        # on error) and an injected probe is test-controlled, so this need not
        # catch: any unexpected raise propagates to update()'s guard, the
        # single "forensics never break the sampler" backstop.
        ctx = self._context_probe()
        return ctx if isinstance(ctx, dict) else {}

    @staticmethod
    def _stamp(ts: float) -> str:
        """Filesystem-safe UTC stamp for the trajectory artifact name."""
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
