"""Tests for jasper.volume_persistence.

Covers:
- atomic write + read round-trip
- corrupt / missing file handling
- debounce: maybe_save respects min-delta and time gates
- save_now bypasses debounce
- regress_if_stale: first boot, fresh, stale-low, stale-high, stale-safe
- out-of-range stored values are rejected
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jasper.volume_persistence import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    VolumePersistence,
    VolumeRecord,
    db_to_percent,
    percent_to_db,
    regress_if_stale,
)


# ---------- helpers --------------------------------------------------------

def _path(tmp_path, name="speaker_volume.json"):
    return str(tmp_path / name)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------- mapping --------------------------------------------------------

def test_percent_dbfs_round_trip():
    for p in [0, 10, 25, 50, 75, 90, 100]:
        assert db_to_percent(percent_to_db(p)) == p


def test_percent_clamps():
    assert db_to_percent(VOLUME_MIN_DB - 50) == 0
    assert db_to_percent(VOLUME_MAX_DB + 50) == 100
    assert percent_to_db(-100) == VOLUME_MIN_DB
    assert percent_to_db(200) == VOLUME_MAX_DB


# ---------- write / read --------------------------------------------------

def test_load_missing_returns_none(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    assert p.load() is None


def test_save_now_then_load(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_now(-22.5)
    rec = p.load()
    assert rec is not None
    assert rec.main_volume_db == -22.5


def test_atomic_no_partial_file(tmp_path):
    """No `.tmp` file should remain after a successful save."""
    path = _path(tmp_path)
    p = VolumePersistence(path)
    p.save_now(-10.0)
    leftover = list(tmp_path.glob(".speaker_volume.*.tmp"))
    assert leftover == []


def test_load_rejects_out_of_range(tmp_path):
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({
        "version": 1,
        "main_volume_db": 50.0,  # absurdly high
        "updated_at": "2026-01-01T00:00:00Z",
    }))
    assert VolumePersistence(str(path)).load() is None


def test_load_rejects_corrupt_json(tmp_path):
    path = tmp_path / "speaker_volume.json"
    path.write_text("{ not valid json")
    assert VolumePersistence(str(path)).load() is None


def test_load_handles_missing_field(tmp_path):
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({"version": 1}))
    assert VolumePersistence(str(path)).load() is None


# ---------- debounce ------------------------------------------------------

def test_maybe_save_writes_first_call(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    assert p.maybe_save(-15.0) is True


def test_maybe_save_skips_micro_changes(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_now(-20.0)  # baseline
    # Change smaller than MIN_DELTA_DB → no write.
    assert p.maybe_save(-20.1) is False


def test_maybe_save_skips_within_debounce_window(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_now(-20.0)
    # Big change, but right after a write → debounce blocks it.
    assert p.maybe_save(-10.0) is False


def test_save_now_always_writes(tmp_path):
    """Voice-tool path bypasses debounce entirely."""
    p = VolumePersistence(_path(tmp_path))
    p.save_now(-20.0)
    p.save_now(-21.0)  # tiny change, but explicit
    rec = p.load()
    assert rec is not None
    assert rec.main_volume_db == -21.0


# ---------- regression ----------------------------------------------------

NOW = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)


def test_regress_first_boot_returns_default():
    db, reason = regress_if_stale(None, now=NOW, first_boot_default_pct=50)
    assert db_to_percent(db) == 50
    assert "first-boot" in reason


def test_regress_fresh_record_unchanged():
    rec = VolumeRecord(
        main_volume_db=percent_to_db(85),
        updated_at=NOW - timedelta(seconds=60),
    )
    db, reason = regress_if_stale(
        rec, now=NOW, stale_after_sec=1800.0,
        safe_low_pct=20, safe_high_pct=70,
    )
    assert db_to_percent(db) == 85
    assert "restored from disk" in reason
    assert "regressed" not in reason


def test_regress_stale_high_clamped_down():
    rec = VolumeRecord(
        main_volume_db=percent_to_db(90),
        updated_at=NOW - timedelta(hours=12),
    )
    db, reason = regress_if_stale(
        rec, now=NOW, stale_after_sec=1800.0,
        safe_low_pct=20, safe_high_pct=70,
    )
    assert db_to_percent(db) == 70
    assert "regressed down" in reason


def test_regress_stale_low_clamped_up():
    rec = VolumeRecord(
        main_volume_db=percent_to_db(5),
        updated_at=NOW - timedelta(hours=12),
    )
    db, reason = regress_if_stale(
        rec, now=NOW, stale_after_sec=1800.0,
        safe_low_pct=20, safe_high_pct=70,
    )
    assert db_to_percent(db) == 20
    assert "regressed up" in reason


def test_regress_stale_safe_unchanged():
    """Stale but inside [safe_low, safe_high]: keep as-is.
    Modest middle volumes don't deserve to be punished."""
    rec = VolumeRecord(
        main_volume_db=percent_to_db(45),
        updated_at=NOW - timedelta(hours=12),
    )
    db, reason = regress_if_stale(
        rec, now=NOW, stale_after_sec=1800.0,
        safe_low_pct=20, safe_high_pct=70,
    )
    assert db_to_percent(db) == 45
    assert "within safe band" in reason
    assert "regressed" not in reason


def test_regress_threshold_exact_boundary():
    """Right at the threshold (just barely stale) → still regresses
    if extreme. The contract is: > threshold → stale."""
    rec = VolumeRecord(
        main_volume_db=percent_to_db(95),
        updated_at=NOW - timedelta(seconds=1801),
    )
    db, _ = regress_if_stale(rec, now=NOW, stale_after_sec=1800.0)
    assert db_to_percent(db) == 70
