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

from jasper.volume_persistence import (
    VOLUME_MAX_DB,
    VOLUME_MIN_DB,
    VolumePersistence,
    VolumeRecord,
    db_to_percent,
    percent_to_db,
    regress_listening_level_if_stale,
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


def test_load_ignores_retired_loudness_anchor_fields(tmp_path):
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({
        "version": 2,
        "main_volume_db": -20.0,
        "loudness_anchor_dbfs": -28.0,
        "loudness_anchor_updated_at": "2026-05-05T10:00:00Z",
        "updated_at": "2026-05-05T10:00:00Z",
    }))
    p = VolumePersistence(str(path))

    rec = p.load()
    assert rec is not None
    p.save_now(rec.main_volume_db)
    rewritten = json.loads(path.read_text())

    assert rec.main_volume_db == -20.0
    assert "loudness_anchor_dbfs" not in rewritten
    assert "loudness_anchor_updated_at" not in rewritten


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


# ---------- listening_level (schema v2) -----------------------------------
def test_save_listening_level_round_trip(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(70)
    rec = p.load()
    assert rec is not None
    assert rec.listening_level == 70
    assert rec.last_used_at is not None


def test_save_listening_level_clamps(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(150)
    rec = p.load()
    assert rec.listening_level == 100
    p.save_listening_level(-50)
    rec = p.load()
    assert rec.listening_level == 0


def test_save_listening_level_without_main_volume_derives_it(tmp_path):
    """Coordinator may write listening_level without prior save_now —
    main_volume_db should be derived from listening_level so the
    file has a coherent main_volume_db field for legacy callers."""
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(50)
    rec = p.load()
    assert rec is not None
    # 50% on -50..0 dB scale → -25 dB
    assert rec.main_volume_db == -25.0


def test_v1_migration_derives_listening_level(tmp_path):
    """Files written by old code (no listening_level field) should
    have it derived from main_volume_db percent on load."""
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({
        "version": 1,
        "main_volume_db": -15.0,  # 70%
        "updated_at": "2026-05-05T10:00:00Z",
    }))
    rec = VolumePersistence(str(path)).load()
    assert rec is not None
    assert rec.listening_level == 70


def test_listening_level_out_of_range_rejected(tmp_path):
    """A v2 file with garbage listening_level should fall back to
    deriving from main_volume_db rather than carrying through the
    bad value."""
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({
        "version": 2,
        "main_volume_db": -25.0,  # 50%
        "listening_level": 250,   # garbage
        "updated_at": "2026-05-05T10:00:00Z",
    }))
    rec = VolumePersistence(str(path)).load()
    assert rec is not None
    # Out-of-range listening_level rejected → derived from main_volume_db
    assert rec.listening_level == 50


def test_save_listening_level_no_user_change_preserves_last_used_at(tmp_path):
    """Boot-time restore writes listening_level but should NOT bump
    last_used_at — otherwise every reboot resets the staleness clock
    and yesterday's bedtime 90% never gets clamped down."""
    p = VolumePersistence(_path(tmp_path))
    # Seed with an old timestamp.
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p._current_main_volume_db = -25.0
    p._current_listening_level = 70
    p._current_last_used_at = old_ts
    p._write_full()

    p.save_listening_level(70, mark_user_change=False)
    rec = p.load()
    assert rec is not None
    assert rec.last_used_at is not None
    assert abs((rec.last_used_at - old_ts).total_seconds()) < 1.0


# ---------- regress_listening_level_if_stale -------------------------------


def test_regress_listening_level_first_boot():
    pct, reason = regress_listening_level_if_stale(
        None, first_boot_default_pct=42,
    )
    assert pct == 42
    assert "first-boot" in reason


def test_regress_listening_level_fresh_unchanged():
    rec = VolumeRecord(
        main_volume_db=percent_to_db(85),
        updated_at=NOW - timedelta(seconds=60),
        listening_level=85,
        last_used_at=NOW - timedelta(seconds=60),
    )
    pct, reason = regress_listening_level_if_stale(
        rec, now=NOW, stale_after_sec=1800.0,
    )
    assert pct == 85
    assert "regressed" not in reason


def test_regress_listening_level_stale_high_clamped():
    rec = VolumeRecord(
        main_volume_db=percent_to_db(90),
        updated_at=NOW - timedelta(hours=12),
        listening_level=90,
        last_used_at=NOW - timedelta(hours=12),
    )
    pct, reason = regress_listening_level_if_stale(
        rec, now=NOW, safe_high_pct=70,
    )
    assert pct == 70
    assert "regressed down" in reason


def test_regress_listening_level_uses_last_used_at_not_updated_at():
    """When both timestamps exist, last_used_at is the staleness anchor
    (it's "when did the user actually move the slider", not "when did
    the daemon last write the file")."""
    rec = VolumeRecord(
        main_volume_db=percent_to_db(90),
        updated_at=NOW - timedelta(seconds=60),  # daemon wrote recently
        listening_level=90,
        last_used_at=NOW - timedelta(hours=12),  # user touched it long ago
    )
    pct, _ = regress_listening_level_if_stale(
        rec, now=NOW, stale_after_sec=1800.0, safe_high_pct=70,
    )
    # Stale via last_used_at → clamped to 70
    assert pct == 70


def test_regress_listening_level_falls_back_to_updated_at_if_no_last_used():
    """v1-migrated records have no last_used_at; fallback to updated_at."""
    rec = VolumeRecord(
        main_volume_db=percent_to_db(90),
        updated_at=NOW - timedelta(hours=12),
        listening_level=90,
        last_used_at=None,
    )
    pct, _ = regress_listening_level_if_stale(
        rec, now=NOW, stale_after_sec=1800.0, safe_high_pct=70,
    )
    assert pct == 70


# ---------- pre_mute_level persistence ------------------------------------

def test_pre_mute_level_round_trip(tmp_path):
    """Persisting pre_mute_level lets a per-request coordinator (e.g.
    jasper-control building one per HTTP call) see prior mute state."""
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(70)
    p.save_pre_mute_level(70)
    rec = p.load()
    assert rec is not None
    assert rec.pre_mute_level == 70


def test_pre_mute_level_clear(tmp_path):
    """save_pre_mute_level(None) drops the field from disk so a fresh
    read sees pre_mute=None (the unmuted state)."""
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(70)
    p.save_pre_mute_level(70)
    p.save_pre_mute_level(None)
    rec = p.load()
    assert rec is not None
    assert rec.pre_mute_level is None


def test_pre_mute_level_clamps(tmp_path):
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(50)
    p.save_pre_mute_level(150)
    rec = p.load()
    assert rec.pre_mute_level == 100


def test_pre_mute_level_out_of_range_in_file_rejected(tmp_path):
    """A hand-edited / corrupted file with pre_mute outside [0,100] is
    treated as 'not muted' rather than respected."""
    path = tmp_path / "speaker_volume.json"
    path.write_text(json.dumps({
        "version": 2,
        "main_volume_db": -20.0,
        "listening_level": 60,
        "pre_mute_level": -5,
        "updated_at": "2026-05-10T10:00:00Z",
    }))
    rec = VolumePersistence(str(path)).load()
    assert rec is not None
    assert rec.pre_mute_level is None


def test_pre_mute_preserved_across_partial_updates(tmp_path):
    """Saving listening_level shouldn't clobber a previously persisted
    pre_mute (the field is independent state set by mute())."""
    p = VolumePersistence(_path(tmp_path))
    p.save_listening_level(70)
    p.save_pre_mute_level(70)
    # Independent listening_level update (e.g., observer-driven write
    # while still muted) must preserve pre_mute.
    p.save_listening_level(0)
    rec = p.load()
    assert rec is not None
    assert rec.pre_mute_level == 70
    assert rec.listening_level == 0


def test_save_pre_mute_works_on_fresh_persistence(tmp_path):
    """save_pre_mute_level called before any save_listening_level
    needs to bootstrap a coherent file — the persistence has no
    main_volume yet at that point. Either it writes a coherent file
    with derived main_volume, or it skips silently. Either way it
    must not crash."""
    p = VolumePersistence(_path(tmp_path))
    p.save_pre_mute_level(50)  # No prior state — should be a no-op or no-crash.
