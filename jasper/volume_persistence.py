# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Persist + restore the speaker's volume setting across daemon restarts.

The Pi is stationary. Speaker volume should not reset on every reboot.
We write the user-perceived listening level to disk whenever it changes,
and restore it at daemon boot.

Core fields tracked:

- `listening_level` (0-100): the canonical user-facing volume. With
  source-aware coordination (volume_coordinator.py), this is the
  number "volume up" / "set volume to 50%" / remote-knob ticks all
  drive. Spotify and Bluetooth push the level to their own sliders.
  AirPlay and idle map the level to CamillaDSP main_volume.

- `main_volume_db`: the underlying CamillaDSP setting. Still tracked
  because we need it captured for boot restore and legacy readers. With
  the coordinator running, main_volume is pinned at 0 dB during
  Spotify/BT push-mode playback (so we don't double-attenuate) and
  tracks listening_level during idle and AirPlay.

- `pre_mute_level` + `mute_token`: the temporary-mute latch and its
  transition identity. The token lets a different process distinguish a
  stale pre-mute renderer observation from a later user change after that
  exact mute has reached the renderer.

Soft regression at boot. If the saved listening_level is from "long
enough ago" (default 30 min), we clamp into a safe range [20%, 70%]
before applying. Yesterday's late-night 90% gets clamped to safe_high
so the morning isn't a blast. Within-session restarts (deploys, fast
crash recovery) preserve continuity.

File format (JSON, atomic write via tmp+rename, v2):

    {
        "version": 2,
        "listening_level": 70,
        "last_used_at": "2026-05-07T15:30:00Z",
        "pre_mute_level": 70,
        "mute_token": "8d2a1c...",
        "main_volume_db": 0.0,
        "updated_at": "2026-05-07T15:30:00Z"
    }

v1 files (pre-coordinator) had only `main_volume_db` and `updated_at`.
On load, a missing `listening_level` is derived from `main_volume_db`
percent — that's exactly what "speaker volume" meant under the old,
Camilla-only path, so the migration preserves the user's last setting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import advisory_file_lock, atomic_write_text
from .volume_curve import (
    DEFAULT_VOLUME_FLOOR_DB,
    VOLUME_CEILING_DB,
    VOLUME_FLOOR_MIN_DB,
    db_to_percent,
    percent_to_db,
)

logger = logging.getLogger(__name__)


# Back-compat names for callers/tests that import the old fixed range.
VOLUME_MIN_DB = DEFAULT_VOLUME_FLOOR_DB
VOLUME_MAX_DB = VOLUME_CEILING_DB


@dataclass(frozen=True)
class VolumeRecord:
    main_volume_db: float
    updated_at: datetime
    # Canonical user-facing volume 0-100 (added in schema v2). When
    # loading a v1 file, this is derived from main_volume_db percent
    # by load(). Once the coordinator runs, listening_level is the
    # source of truth and main_volume is derived from it (or pinned
    # at 0 dB while a source is active).
    listening_level: int | None = None
    # When the user (or an observed source-side slider) last touched
    # the volume. Used by the boot-time idle-reset to decide whether
    # to fall back to a safe default after a long quiet period.
    last_used_at: datetime | None = None
    # Pre-mute listening_level if the speaker is currently muted; None
    # otherwise. Persisted so a per-request coordinator (jasper-control
    # builds a fresh one per HTTP call) can see prior mute state set by
    # an earlier call. Cleared by initialize() at boot — mute state is
    # per-session, not per-deployment.
    pre_mute_level: int | None = None
    # Opaque identity for one temporary-mute transition. A long-lived source
    # observer confirms renderer zero against this token before it may treat a
    # later nonzero observation as a new user intent. None is accepted for
    # rolling-upgrade / legacy records and migrated by the coordinator.
    mute_token: str | None = None


class VolumePersistence:
    """Atomic on-disk persistence of speaker volume.

    Writes are debounced: callers may report observed volume frequently
    and we'll only actually hit the SD card on real changes that haven't
    been written recently. Explicit user-initiated changes (set_volume
    voice tool) bypass debounce via `save_now`.
    """

    DEFAULT_PATH = "/var/lib/jasper/speaker_volume.json"
    # Don't write to flash more often than this in the polling path.
    DEBOUNCE_SEC = 30.0
    # If a polled main_volume changes by less than this from the last
    # persisted value, treat it as noise and don't write. Keeps SD-card
    # writes proportional to real user activity, not every drift in
    # Camilla's reported value.
    MIN_DELTA_DB = 0.5

    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path or self.DEFAULT_PATH)
        self._state_lock_path = self._path.with_name(f".{self._path.name}.lock")
        self._operation_lock_path = self._path.with_name(
            f".{self._path.name}.operation.lock",
        )
        self._last_written_db: float | None = None
        self._last_written_at_mono: float = 0.0
        # In-memory copy of all persisted fields, so we can write the
        # full record whenever any single field changes (avoids losing
        # one field when only another updates).
        self._current_main_volume_db: float | None = None
        self._current_listening_level: int | None = None
        self._current_last_used_at: datetime | None = None
        self._current_pre_mute_level: int | None = None
        self._current_mute_token: str | None = None
        self._state_transaction_depth = 0

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _state_update(self):
        """Serialize one fresh read-modify-write across daemon processes."""
        if self._state_transaction_depth > 0:
            yield
            return
        with advisory_file_lock(
            self._state_lock_path,
            mode=0o660,
            group_from_parent=True,
        ):
            self._state_transaction_depth += 1
            try:
                self.load()
                yield
            finally:
                self._state_transaction_depth -= 1

    @asynccontextmanager
    async def operation_lock(self, *, timeout_sec: float = 10.0):
        """Serialize source-changing volume intents across all daemons.

        The state-file lock above is deliberately tiny. This separate lease may
        span a slow Spotify/Bluetooth actuator without blocking unrelated state
        readers or handoff diagnostics. Acquisition runs off the event loop so
        jasper-voice remains responsive while another process owns the lease.
        """
        lock = advisory_file_lock(
            self._operation_lock_path,
            mode=0o660,
            group_from_parent=True,
            timeout_sec=timeout_sec,
        )
        acquire = asyncio.create_task(asyncio.to_thread(lock.__enter__))
        acquired = False
        try:
            try:
                await asyncio.shield(acquire)
                acquired = True
            except asyncio.CancelledError:
                # The worker thread cannot be cancelled while blocked in
                # flock(). Wait for its bounded acquisition, release if it
                # succeeded, and collect an acquisition failure so it cannot
                # replace the caller's cancellation or become an unretrieved
                # task exception.
                try:
                    await acquire
                except (OSError, ValueError):
                    # Acquisition result is secondary to caller cancellation.
                    pass
                else:
                    acquired = True
                raise
            yield
        finally:
            if acquired:
                await asyncio.shield(
                    asyncio.to_thread(lock.__exit__, None, None, None),
                )

    def load(self) -> VolumeRecord | None:
        """Read the persisted record. Returns None on missing /
        corrupt / out-of-range main_volume (caller decides default).

        Legacy fields that no longer have runtime meaning are ignored."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("volume persistence: read failed (%s)", e)
            return None
        try:
            data = json.loads(raw)
            db = float(data["main_volume_db"])
            ts = data.get("updated_at")
            updated_at = (
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if ts else datetime.now(timezone.utc)
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("volume persistence: parse failed (%s)", e)
            return None
        if not (VOLUME_FLOOR_MIN_DB - 1.0 <= db <= VOLUME_MAX_DB + 1.0):
            # Out of plausible range — refuse rather than restore a
            # bogus value that could cause loud / silent surprise.
            logger.warning(
                "volume persistence: stored main_volume_db=%.1f out of range; ignoring",
                db,
            )
            return None
        # listening_level + last_used_at — schema v2. v1 files lack
        # both; migrate by deriving listening_level from main_volume_db using
        # the historical default curve. That value was the user's last
        # commanded volume under the Camilla-only path, so the migration
        # preserves intent even if a calibrated floor setting now exists.
        listening_level: int | None = None
        try:
            raw_level = data.get("listening_level")
            if raw_level is not None:
                lvl = int(raw_level)
                if 0 <= lvl <= 100:
                    listening_level = lvl
                else:
                    logger.warning(
                        "volume persistence: stored listening_level=%d out of "
                        "[0,100]; ignoring",
                        lvl,
                    )
        except (TypeError, ValueError):
            listening_level = None
        if listening_level is None:
            listening_level = db_to_percent(db, floor_db=DEFAULT_VOLUME_FLOOR_DB)
            logger.info(
                "volume persistence: deriving listening_level=%d%% from v1 "
                "main_volume_db=%.1f (migration)",
                listening_level, db,
            )
        last_used_at: datetime | None = None
        try:
            raw_last_used = data.get("last_used_at")
            if raw_last_used is not None:
                last_used_at = datetime.fromisoformat(
                    raw_last_used.replace("Z", "+00:00")
                )
        except (TypeError, ValueError, AttributeError):
            last_used_at = None
        # pre_mute_level — set when the speaker is in the muted state
        # so the next unmute (potentially from a different coordinator
        # instance) can restore the prior level.
        pre_mute_level: int | None = None
        try:
            raw_pre_mute = data.get("pre_mute_level")
            if raw_pre_mute is not None:
                pml = int(raw_pre_mute)
                if 0 <= pml <= 100:
                    pre_mute_level = pml
                else:
                    logger.warning(
                        "volume persistence: stored pre_mute_level=%d out of "
                        "[0,100]; ignoring",
                        pml,
                    )
        except (TypeError, ValueError):
            pre_mute_level = None
        mute_token: str | None = None
        raw_mute_token = data.get("mute_token")
        if (
            pre_mute_level is not None
            and isinstance(raw_mute_token, str)
            and 0 < len(raw_mute_token) <= 128
        ):
            mute_token = raw_mute_token
        elif raw_mute_token is not None:
            logger.warning(
                "volume persistence: invalid mute_token; ignoring",
            )
        # Cache loaded values so subsequent partial-update writes don't
        # lose any field.
        self._current_main_volume_db = db
        self._current_listening_level = listening_level
        self._current_last_used_at = last_used_at
        self._current_pre_mute_level = pre_mute_level
        self._current_mute_token = mute_token
        return VolumeRecord(
            main_volume_db=db,
            updated_at=updated_at,
            listening_level=listening_level,
            last_used_at=last_used_at,
            pre_mute_level=pre_mute_level,
            mute_token=mute_token,
        )

    def save_now(self, main_volume_db: float) -> None:
        """Force-write main_volume to disk immediately. Used for
        explicit user actions (set_volume voice tool, mute) where we
        want the new level captured before any restart could lose it."""
        with self._state_update():
            self._current_main_volume_db = float(main_volume_db)
            self._write_full()
        self._last_written_db = self._current_main_volume_db
        self._last_written_at_mono = time.monotonic()

    def maybe_save(self, main_volume_db: float) -> bool:
        """Debounced main_volume write — for poll-driven detection of
        external changes (mpc, hardware knob, etc). Returns True if
        we wrote.

        Refreshes from disk before writing so the file's
        listening_level + last_used_at fields (which might have been
        updated by another process — e.g. jasper-control via remote)
        aren't trampled by this process's stale in-memory state.
        Only main_volume_db is treated as owned by this writer."""
        db = float(main_volume_db)
        now = time.monotonic()
        last_db = self._last_written_db
        if last_db is not None:
            if abs(db - last_db) < self.MIN_DELTA_DB:
                return False
            if now - self._last_written_at_mono < self.DEBOUNCE_SEC:
                return False
        with self._state_update():
            self._current_main_volume_db = db
            self._write_full()
        self._last_written_db = db
        self._last_written_at_mono = now
        return True

    def save_pre_mute_level(self, level: int | None) -> None:
        """Persist the pre-mute level (or clear it with None).

        Compatibility wrapper for callers that predate transition tokens.
        VolumeCoordinator uses ``save_mute_state`` so the latch and token land
        atomically.
        """
        with self._state_update():
            token = self._current_mute_token if level is not None else None
            self._save_mute_state_locked(level, token)

    def save_mute_state(
        self,
        level: int | None,
        mute_token: str | None,
    ) -> None:
        """Atomically persist or clear one temporary-mute transition.

        ``level`` is the restore target. ``mute_token`` identifies this exact
        mute across jasper-control and jasper-voice so an observer cannot
        mistake the renderer's stale pre-push value for a post-mute user edit.
        """
        with self._state_update():
            self._save_mute_state_locked(level, mute_token)

    def _save_mute_state_locked(
        self,
        level: int | None,
        mute_token: str | None,
    ) -> None:
        """Apply a mute-state partial update while ``_state_update`` is held."""
        if level is not None:
            level = max(0, min(100, int(level)))
            if mute_token is not None:
                mute_token = str(mute_token)
                if not mute_token or len(mute_token) > 128:
                    raise ValueError("mute_token must contain 1..128 characters")
        else:
            mute_token = None
        self._current_pre_mute_level = level
        self._current_mute_token = mute_token
        if self._current_main_volume_db is None:
            # No disk record and no main-volume context. A mute latch by itself
            # is not enough to invent a synthetic listening level.
            logger.debug(
                "volume persistence: skipping pre_mute write "
                "(no main_volume context yet)",
            )
            return
        self._write_full()

    def save_listening_level(
        self, percent: int, *, mark_user_change: bool = True,
    ) -> None:
        """Force-write the canonical listening_level (0-100) to disk.
        Not debounced: listening_level changes are infrequent compared
        with poll-driven main-volume observations, and we want every
        change durable so a crash doesn't lose the user's last command.

        `mark_user_change` controls whether last_used_at is bumped to
        now. Set False for boot-time restore writes — otherwise every
        daemon restart would reset the idle-reset clock, masking truly
        stale levels.

        If `main_volume_db` hasn't been written yet (no save_now call
        in this process), derive it from listening_level so the file
        always has a coherent main_volume_db field for diagnostics and
        external readers of the persisted schema."""
        clamped = max(0, min(100, int(percent)))
        with self._state_update():
            self._current_listening_level = clamped
            if mark_user_change:
                self._current_last_used_at = datetime.now(timezone.utc)
            if self._current_main_volume_db is None:
                self._current_main_volume_db = percent_to_db(clamped)
            self._write_full()

    def _write_full(self) -> None:
        """Write the current in-memory state to disk atomically.
        Always writes known independent state fields so a partial update
        doesn't lose the others."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "volume persistence: mkdir failed for %s (%s)",
                self._path.parent, e,
            )
            return
        if self._current_main_volume_db is None:
            # Shouldn't happen — caller should always set main_volume
            # before writing. Defensive: skip.
            logger.debug(
                "volume persistence: skipping write (no main_volume in memory)",
            )
            return
        now_iso = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        ).replace("+00:00", "Z")
        payload: dict[str, Any] = {
            "version": 2,
            "main_volume_db": round(self._current_main_volume_db, 2),
            "updated_at": now_iso,
        }
        if self._current_listening_level is not None:
            payload["listening_level"] = int(self._current_listening_level)
        if self._current_last_used_at is not None:
            payload["last_used_at"] = self._current_last_used_at.isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z")
        if self._current_pre_mute_level is not None:
            payload["pre_mute_level"] = int(self._current_pre_mute_level)
            if self._current_mute_token is not None:
                payload["mute_token"] = self._current_mute_token
        body = json.dumps(payload, indent=2)
        try:
            # Atomic write via the canonical helper (same-dir tempfile +
            # os.replace). mode 0660 (group rw): WS1 Phase 3b drops jasper-voice
            # and jasper-mux to non-root service users in the shared `jasper`
            # group, and all three of voice/mux/control read+write this file
            # fresh. Group rw + the group-shared /var/lib/jasper dir lets them
            # converge; the file carries no secret (just the listening level),
            # so group read is not a disclosure concern.
            atomic_write_text(self._path, body, mode=0o660)
        except OSError as e:
            logger.warning(
                "volume persistence: write to %s failed (%s)",
                self._path, e,
            )
            return
        parts = [
            f"main_volume={self._current_main_volume_db:.1f} dB "
            f"({db_to_percent(self._current_main_volume_db)}%)",
        ]
        if self._current_listening_level is not None:
            parts.append(f"listening_level={self._current_listening_level}%")
        logger.info("volume persistence: saved %s", ", ".join(parts))


def configured_path() -> str:
    """The volume-state file this box uses: JASPER_VOLUME_STATE_PATH or the default.

    Reads the environment fresh on every call — long-lived daemons must not
    cache this from os.environ at import or init time.
    """
    return os.environ.get("JASPER_VOLUME_STATE_PATH", VolumePersistence.DEFAULT_PATH)


def _regress_percent(
    pct: int | None,
    age_sec: float | None,
    *,
    stale_after_sec: float,
    safe_low_pct: int,
    safe_high_pct: int,
    first_boot_default_pct: int,
) -> tuple[int, str]:
    """Shared regression rules. Returns (target_percent, reason).

    No record (pct is None) → first-boot default.
    Fresh → use as-is. Stale + extreme → clamp into [safe_low, safe_high].
    Stale but already in safe band → use as-is.
    """
    if pct is None or age_sec is None:
        return first_boot_default_pct, f"first-boot default ({first_boot_default_pct}%)"
    if age_sec < stale_after_sec:
        return pct, f"restored from disk ({pct}%, age={int(age_sec)}s)"
    if pct < safe_low_pct:
        return (
            safe_low_pct,
            f"regressed up: stale ({int(age_sec)}s) and was {pct}% "
            f"(< {safe_low_pct}%), clamped to {safe_low_pct}%",
        )
    if pct > safe_high_pct:
        return (
            safe_high_pct,
            f"regressed down: stale ({int(age_sec)}s) and was {pct}% "
            f"(> {safe_high_pct}%), clamped to {safe_high_pct}%",
        )
    return (
        pct,
        f"restored from disk ({pct}%, stale {int(age_sec)}s but within "
        f"safe band [{safe_low_pct}, {safe_high_pct}])",
    )


def regress_listening_level_if_stale(
    record: VolumeRecord | None,
    *,
    now: datetime | None = None,
    stale_after_sec: float = 1800.0,
    safe_low_pct: int = 20,
    safe_high_pct: int = 70,
    first_boot_default_pct: int = 50,
) -> tuple[int, str]:
    """Compute the listening_level (0-100) to restore at boot.

    Prefers `last_used_at` for staleness if present (the timestamp of
    the last user-initiated change), falling back to `updated_at`.
    Returns (target_percent, reason_string).
    """
    now = now or datetime.now(timezone.utc)
    if record is None:
        return _regress_percent(
            None, None,
            stale_after_sec=stale_after_sec,
            safe_low_pct=safe_low_pct,
            safe_high_pct=safe_high_pct,
            first_boot_default_pct=first_boot_default_pct,
        )
    pct = record.listening_level
    if pct is None:
        # No level recorded — derive from main_volume_db (same as
        # load() does, but record is the in-memory shape).
        pct = db_to_percent(record.main_volume_db)
    age_anchor = record.last_used_at or record.updated_at
    age_sec = (now - age_anchor).total_seconds()
    return _regress_percent(
        pct, age_sec,
        stale_after_sec=stale_after_sec,
        safe_low_pct=safe_low_pct,
        safe_high_pct=safe_high_pct,
        first_boot_default_pct=first_boot_default_pct,
    )
