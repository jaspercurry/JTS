"""Persist + restore the speaker's volume setting across daemon restarts.

The Pi is stationary. Speaker volume should not reset on every reboot.
We write CamillaDSP's `main_volume` to disk whenever it changes, and
restore it at daemon boot.

Why persist at all. Without this, every restart leaves Camilla at
whatever Camilla's own state-load picks (typically the YAML default,
0 dB = 100%). Combined with the TTS volume tracker's silence-fallback
formula (main_volume + offset), an unintended high main_volume after
a restart is a "blast the room" hazard. Persisting closes that loop.

Why we persist `main_volume` and not the observed playback RMS
(`anchor`):
the user-facing "speaker volume" is *the Camilla setting itself*. iPhone
AirPlay sliders and Spotify Connect sliders are upstream attenuators
the user is aware of and treats separately; "the speaker is at 60%"
means main_volume sits at 60% of its scale, regardless of what the
source is doing. The TTS gain tracker already follows observed RMS
in real-time during music — there's no need to also persist it.

Soft regression at boot. If the saved value is from "long enough ago"
(default 30 min), we clamp the restored value into a safe percent range
[20%, 70%] before applying. So the speaker doesn't suddenly come up
at yesterday's 90% bedtime listening level after a power cycle, but a
modest 50% setting is preserved. Within-session restarts (deploys,
crashes recovered in seconds) skip regression — continuity is preserved.

File format (JSON, atomic write via tmp+rename):

    {
        "version": 1,
        "main_volume_db": -20.0,
        "updated_at": "2026-05-05T15:30:00Z"
    }
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
    # NEW: last observed playback RMS while music was playing. None
    # means "never recorded" → callers should fall back to
    # DEFAULT_ANCHOR_DBFS. Persists across restarts WITHOUT expiry.
    # The Pi doesn't move; the user's room context doesn't change
    # often enough to warrant time-based invalidation.
    loudness_anchor_dbfs: float | None = None
    anchor_updated_at: datetime | None = None


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
        # main_volume when only anchor changes, and vice-versa).
        self._current_main_volume_db: float | None = None
        self._current_anchor_dbfs: float | None = None

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
        # Cache loaded values so subsequent partial-update writes
        # (only main_volume, only anchor) don't lose the other field.
        self._current_main_volume_db = db
        self._current_anchor_dbfs = anchor
        return VolumeRecord(
            main_volume_db=db,
            updated_at=updated_at,
            loudness_anchor_dbfs=anchor,
            anchor_updated_at=anchor_ts,
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
        external changes (moOde UI, mpc, etc). Returns True if we wrote."""
        db = float(main_volume_db)
        now = time.monotonic()
        last_db = self._last_written_db
        if last_db is not None:
            if abs(db - last_db) < self.MIN_DELTA_DB:
                return False
            if now - self._last_written_at_mono < self.DEBOUNCE_SEC:
                return False
        self._current_main_volume_db = db
        self._write_full()
        self._last_written_db = db
        self._last_written_at_mono = now
        return True

    def maybe_save_anchor(self, anchor_dbfs: float) -> bool:
        """Debounced anchor write. The anchor moves continuously while
        music plays; we don't want to write to flash on every poll.
        Returns True if we wrote."""
        a = float(anchor_dbfs)
        now = time.monotonic()
        last_a = self._last_written_anchor
        if last_a is not None:
            if abs(a - last_a) < self.MIN_DELTA_DB:
                return False
            if now - self._last_written_anchor_at_mono < self.DEBOUNCE_SEC:
                return False
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
        payload = {
            "version": 1,
            "main_volume_db": round(self._current_main_volume_db, 2),
            "updated_at": now_iso,
        }
        if self._current_anchor_dbfs is not None:
            payload["loudness_anchor_dbfs"] = round(
                self._current_anchor_dbfs, 2,
            )
            payload["loudness_anchor_updated_at"] = now_iso
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
        if self._current_anchor_dbfs is not None:
            logger.info(
                "volume persistence: saved main_volume=%.1f dB (%d%%) "
                "anchor=%.1f dBFS",
                self._current_main_volume_db,
                db_to_percent(self._current_main_volume_db),
                self._current_anchor_dbfs,
            )
        else:
            logger.info(
                "volume persistence: saved main_volume=%.1f dB (%d%%) "
                "(no anchor yet)",
                self._current_main_volume_db,
                db_to_percent(self._current_main_volume_db),
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
    """Compute the main_volume to apply at boot.

    Rules:
      - No record → first-boot default (50%).
      - Record fresh (now - updated_at < stale_after_sec) → use as-is.
      - Record stale + extreme:
          percent < safe_low_pct  → clamp up to safe_low_pct
          percent > safe_high_pct → clamp down to safe_high_pct
      - Record stale but already in [safe_low, safe_high] → use as-is.

    Returns (main_volume_db, reason_string). The reason is a short
    explanation logged at boot so it's clear why the speaker came up
    at the level it did.
    """
    now = now or datetime.now(timezone.utc)
    if record is None:
        db = percent_to_db(first_boot_default_pct)
        return db, f"first-boot default ({first_boot_default_pct}%)"
    age_sec = (now - record.updated_at).total_seconds()
    if age_sec < stale_after_sec:
        return (
            record.main_volume_db,
            f"restored from disk ({db_to_percent(record.main_volume_db)}%, "
            f"age={int(age_sec)}s)",
        )
    pct = db_to_percent(record.main_volume_db)
    if pct < safe_low_pct:
        regressed_db = percent_to_db(safe_low_pct)
        return (
            regressed_db,
            f"regressed up: stale ({int(age_sec)}s) and was {pct}% "
            f"(< {safe_low_pct}%), clamped to {safe_low_pct}%",
        )
    if pct > safe_high_pct:
        regressed_db = percent_to_db(safe_high_pct)
        return (
            regressed_db,
            f"regressed down: stale ({int(age_sec)}s) and was {pct}% "
            f"(> {safe_high_pct}%), clamped to {safe_high_pct}%",
        )
    return (
        record.main_volume_db,
        f"restored from disk ({pct}%, stale {int(age_sec)}s but within "
        f"safe band [{safe_low_pct}, {safe_high_pct}])",
    )
