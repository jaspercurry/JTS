"""Persist + restore the speaker's volume setting across daemon restarts.

The Pi is stationary. Speaker volume should not reset on every reboot.
We write the user-perceived listening level to disk whenever it changes,
and restore it at daemon boot.

Two related fields are tracked:

- `listening_level` (0-100): the canonical user-facing volume. With
  source-aware coordination (volume_coordinator.py), this is the
  number "volume up" / "set volume to 50%" / dial-knob ticks all
  drive. When a source (AirPlay/Spotify/BT) is active the level is
  pushed to that source's own slider; during idle/MPD it maps to
  CamillaDSP main_volume.

- `main_volume_db`: the underlying CamillaDSP setting. Still tracked
  because (a) it's what TtsVolumeTracker reads as its ceiling, and
  (b) we need it captured for the boot-restore path. With the
  coordinator running, main_volume is pinned at 0 dB during source-
  active operation (so we don't double-attenuate) and tracks
  listening_level during idle.

- `loudness_anchor_dbfs`: last observed playback RMS while music was
  actually playing. TTS uses this during silence so it doesn't get
  loud just because main_volume is high.

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
        "main_volume_db": 0.0,
        "loudness_anchor_dbfs": -28.0,
        "updated_at": "2026-05-07T15:30:00Z"
    }

v1 files (pre-coordinator) had only `main_volume_db` and `updated_at`.
On load, a missing `listening_level` is derived from `main_volume_db`
percent — that's exactly what "speaker volume" meant under the old,
Camilla-only path, so the migration preserves the user's last setting.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Camilla's main_volume range. Mirrors the percent↔dB mapping in
# tools/audio.py — keep these in sync if either side changes.
VOLUME_MIN_DB = -50.0
VOLUME_MAX_DB = 0.0


def db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    p = (float(db) - VOLUME_MIN_DB) / span * 100.0
    return max(0, min(100, round(p)))


def percent_to_db(percent: float) -> float:
    p = max(0, min(100, float(percent)))
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return VOLUME_MIN_DB + span * p / 100.0


# Default loudness anchor when no record exists yet (first boot, file
# deleted, etc). -30 dBFS RMS maps to 40% on the linear dB scale —
# combined with the headroom formula in TtsVolumeTracker, gives an
# effective TTS output around -18 dBFS, a normal conversational level.
# Conservative on purpose: blasting on first boot is the bad failure
# mode, so the default sits comfortably below "loud."
DEFAULT_ANCHOR_DBFS = -30.0


@dataclass(frozen=True)
class VolumeRecord:
    main_volume_db: float
    updated_at: datetime
    # Last observed playback RMS while music was playing. None means
    # "never recorded" → callers fall back to DEFAULT_ANCHOR_DBFS.
    # Persists WITHOUT expiry; the Pi doesn't move and the room
    # context is stable.
    loudness_anchor_dbfs: float | None = None
    anchor_updated_at: datetime | None = None
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


class VolumePersistence:
    """Atomic on-disk persistence of speaker volume.

    Writes are debounced: the volume tracker can call `maybe_save` on
    every poll (4 Hz) and we'll only actually hit the SD card on real
    changes that haven't been written recently. Explicit user-initiated
    changes (set_volume voice tool) bypass debounce via `save_now`.
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
        self._last_written_db: float | None = None
        self._last_written_at_mono: float = 0.0
        # Mirror state for the anchor field — independent debounce
        # so a busy music session doesn't trigger flash writes for
        # every tiny RMS fluctuation.
        self._last_written_anchor: float | None = None
        self._last_written_anchor_at_mono: float = 0.0
        # In-memory copy of all persisted fields, so we can write the
        # full record whenever any single field changes (avoids losing
        # one field when only another updates).
        self._current_main_volume_db: float | None = None
        self._current_anchor_dbfs: float | None = None
        self._current_listening_level: int | None = None
        self._current_last_used_at: datetime | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> VolumeRecord | None:
        """Read the persisted record. Returns None on missing /
        corrupt / out-of-range main_volume (caller decides default).

        The loudness_anchor field is optional — older files written
        before the anchor feature have no anchor field; we tolerate
        that and return None for the anchor so the caller can apply
        the conservative default."""
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
        if not (VOLUME_MIN_DB - 1.0 <= db <= VOLUME_MAX_DB + 1.0):
            # Out of plausible range — refuse rather than restore a
            # bogus value that could cause loud / silent surprise.
            logger.warning(
                "volume persistence: stored main_volume_db=%.1f out of range; ignoring",
                db,
            )
            return None
        # Optional anchor fields. Tolerant of missing / malformed:
        # if anything looks off, treat as "no anchor recorded" and let
        # the caller fall back to DEFAULT_ANCHOR_DBFS.
        anchor: float | None = None
        anchor_ts: datetime | None = None
        try:
            raw_anchor = data.get("loudness_anchor_dbfs")
            if raw_anchor is not None:
                a = float(raw_anchor)
                # Sanity bound: -120 dBFS to 0 dBFS. Anything outside
                # this is a malformed write or an attacker editing the
                # file; either way we don't trust it.
                if -120.0 <= a <= 0.0:
                    anchor = a
                else:
                    logger.warning(
                        "volume persistence: stored anchor=%.1f dBFS out of range; ignoring",
                        a,
                    )
        except (TypeError, ValueError):
            anchor = None
        try:
            raw_anchor_ts = data.get("loudness_anchor_updated_at")
            if raw_anchor_ts is not None:
                anchor_ts = datetime.fromisoformat(
                    raw_anchor_ts.replace("Z", "+00:00")
                )
        except (TypeError, ValueError, AttributeError):
            anchor_ts = None
        # listening_level + last_used_at — schema v2. v1 files lack
        # both; migrate by deriving listening_level from main_volume_db.
        # That value was the user's last commanded volume under the
        # Camilla-only path, so the migration preserves intent.
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
            listening_level = db_to_percent(db)
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
        # Cache loaded values so subsequent partial-update writes don't
        # lose any field.
        self._current_main_volume_db = db
        self._current_anchor_dbfs = anchor
        self._current_listening_level = listening_level
        self._current_last_used_at = last_used_at
        return VolumeRecord(
            main_volume_db=db,
            updated_at=updated_at,
            loudness_anchor_dbfs=anchor,
            anchor_updated_at=anchor_ts,
            listening_level=listening_level,
            last_used_at=last_used_at,
        )

    def save_now(self, main_volume_db: float) -> None:
        """Force-write main_volume to disk immediately. Used for
        explicit user actions (set_volume voice tool, mute) where we
        want the new level captured before any restart could lose it.
        Anchor (if any) is preserved as-is from in-memory state."""
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
        updated by another process — e.g. jasper-control via dial)
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
        # Refresh listening_level + last_used_at from disk so a write
        # by another process isn't lost.
        self.load()
        self._current_main_volume_db = db
        self._write_full()
        self._last_written_db = db
        self._last_written_at_mono = now
        return True

    def save_listening_level(
        self, percent: int, *, mark_user_change: bool = True,
    ) -> None:
        """Force-write the canonical listening_level (0-100) to disk.
        Not debounced: listening_level changes are infrequent compared
        to anchor updates, and we want every change durable so a
        crash doesn't lose the user's last command.

        `mark_user_change` controls whether last_used_at is bumped to
        now. Set False for boot-time restore writes — otherwise every
        daemon restart would reset the idle-reset clock, masking truly
        stale levels.

        If `main_volume_db` hasn't been written yet (no save_now call
        in this process), derive it from listening_level so the file
        always has a coherent main_volume_db field for legacy
        callers (boot-time regress_if_stale, external readers, etc.)."""
        clamped = max(0, min(100, int(percent)))
        self._current_listening_level = clamped
        if mark_user_change:
            self._current_last_used_at = datetime.now(timezone.utc)
        if self._current_main_volume_db is None:
            self._current_main_volume_db = percent_to_db(clamped)
        self._write_full()

    def maybe_save_anchor(self, anchor_dbfs: float) -> bool:
        """Debounced anchor write. The anchor moves continuously while
        music plays; we don't want to write to flash on every poll.
        Returns True if we wrote.

        Refreshes from disk before writing — same multi-process safety
        as maybe_save. Anchor is owned by this writer (TtsVolumeTracker)
        but listening_level / main_volume_db may have changed
        externally."""
        a = float(anchor_dbfs)
        now = time.monotonic()
        last_a = self._last_written_anchor
        if last_a is not None:
            if abs(a - last_a) < self.MIN_DELTA_DB:
                return False
            if now - self._last_written_anchor_at_mono < self.DEBOUNCE_SEC:
                return False
        self.load()
        self._current_anchor_dbfs = a
        self._write_full()
        self._last_written_anchor = a
        self._last_written_anchor_at_mono = now
        return True

    def _write_full(self) -> None:
        """Write the current in-memory state to disk atomically.
        Always writes both main_volume and anchor if known, so a
        partial update doesn't lose the other field."""
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
            # before any anchor-only save. Defensive: skip.
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
        if self._current_anchor_dbfs is not None:
            payload["loudness_anchor_dbfs"] = round(
                self._current_anchor_dbfs, 2,
            )
            payload["loudness_anchor_updated_at"] = now_iso
        if self._current_listening_level is not None:
            payload["listening_level"] = int(self._current_listening_level)
        if self._current_last_used_at is not None:
            payload["last_used_at"] = self._current_last_used_at.isoformat(
                timespec="seconds",
            ).replace("+00:00", "Z")
        body = json.dumps(payload, indent=2)
        try:
            # Atomic write: write to a tmp file in the same directory,
            # then rename. POSIX rename is atomic, so a crash mid-write
            # leaves either the old file or the new one — never a
            # half-written record.
            fd, tmp_path = tempfile.mkstemp(
                prefix=".speaker_volume.",
                suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(body)
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
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
        if self._current_anchor_dbfs is not None:
            parts.append(f"anchor={self._current_anchor_dbfs:.1f} dBFS")
        logger.info("volume persistence: saved %s", ", ".join(parts))


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


def regress_if_stale(
    record: VolumeRecord | None,
    *,
    now: datetime | None = None,
    stale_after_sec: float = 1800.0,
    safe_low_pct: int = 20,
    safe_high_pct: int = 70,
    first_boot_default_pct: int = 50,
) -> tuple[float, str]:
    """Compute the main_volume_db to apply at boot. Operates on the
    legacy main_volume_db field only — for callers that haven't been
    moved to the listening_level coordinator yet.

    Rules:
      - No record → first-boot default (50%).
      - Record fresh (now - updated_at < stale_after_sec) → use as-is.
      - Record stale + extreme:
          percent < safe_low_pct  → clamp up to safe_low_pct
          percent > safe_high_pct → clamp down to safe_high_pct
      - Record stale but already in [safe_low, safe_high] → use as-is.

    Returns (main_volume_db, reason_string).
    """
    now = now or datetime.now(timezone.utc)
    if record is None:
        db = percent_to_db(first_boot_default_pct)
        return db, f"first-boot default ({first_boot_default_pct}%)"
    age_sec = (now - record.updated_at).total_seconds()
    pct = db_to_percent(record.main_volume_db)
    target_pct, reason = _regress_percent(
        pct, age_sec,
        stale_after_sec=stale_after_sec,
        safe_low_pct=safe_low_pct,
        safe_high_pct=safe_high_pct,
        first_boot_default_pct=first_boot_default_pct,
    )
    # If the regressor didn't move us, return the original db
    # (preserves sub-percent precision); if it clamped, compute the
    # target db from the new percent.
    if target_pct == pct:
        return record.main_volume_db, reason
    return percent_to_db(target_pct), reason
