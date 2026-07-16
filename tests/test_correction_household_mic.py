# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper/correction/household_mic.py — the durable record of the
household's remembered measurement microphone (Wave-2 persistence).

Before this module, nothing about the measurement mic survived across
sessions: the phone relay's setup validated against a per-run
``setup_binding_id`` and an uploaded calibration carried no serial, so
neither path could ever be found again on a later run. These tests pin the
record's round-trip, its fail-soft behavior on a malformed file, and the
content-hash resolution path that makes an upload (no serial) findable
again.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jasper.audio_measurement import calibration
from jasper.correction import household_mic as hm

SAMPLE_CAL = "20 -1\n100 0\n1000 1\n20000 2\n"


def _store(tmp_path: Path, **overrides) -> calibration.CalibrationRecord:
    kwargs = dict(
        text=SAMPLE_CAL,
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        root=tmp_path / "calibrations",
    )
    kwargs.update(overrides)
    return calibration.store_calibration(**kwargs)


# --- record round-trip -------------------------------------------------------


def test_household_mic_record_round_trips(tmp_path: Path):
    path = tmp_path / "household_mic.json"
    assert hm.read_household_mic(path=path) is None  # missing file -> None

    record = _store(tmp_path)
    household = hm.household_mic_from_calibration(record, serial="SN-123456")
    hm.write_household_mic(household, path=path)

    loaded = hm.read_household_mic(path=path)
    assert loaded == household
    assert loaded.model_key == "other"
    assert loaded.label == "Lab mic"
    assert loaded.calibration_id == record.calibration_id
    assert loaded.curve_sha256 == record.file_sha256
    assert loaded.provider == "manual_upload"
    assert loaded.serial_display == "3456"  # last-4 only, never the raw serial
    assert loaded.updated_at > 0


def test_household_mic_record_never_persists_raw_serial(tmp_path: Path):
    path = tmp_path / "household_mic.json"
    record = _store(tmp_path)
    household = hm.household_mic_from_calibration(record, serial="SECRET-700-1234")
    hm.write_household_mic(household, path=path)

    raw = path.read_text()
    assert "SECRET" not in raw
    assert "700-1234" not in raw
    assert "1234" in raw  # last-4 display form is expected to be present


def test_household_mic_record_mode_0644(tmp_path: Path):
    path = tmp_path / "household_mic.json"
    record = _store(tmp_path)
    hm.write_household_mic(hm.household_mic_from_calibration(record), path=path)
    assert (path.stat().st_mode & 0o777) == 0o644


def test_clear_household_mic_is_idempotent(tmp_path: Path):
    path = tmp_path / "household_mic.json"
    record = _store(tmp_path)
    hm.write_household_mic(hm.household_mic_from_calibration(record), path=path)
    assert path.exists()
    hm.clear_household_mic(path=path)
    assert not path.exists()
    hm.clear_household_mic(path=path)  # missing file: no error


# --- malformed-file fail-soft -------------------------------------------------


def test_read_household_mic_treats_malformed_json_as_absent(tmp_path: Path, caplog):
    path = tmp_path / "household_mic.json"
    path.write_text("not json at all")
    caplog.set_level(logging.WARNING, logger="jasper.correction.household_mic")

    assert hm.read_household_mic(path=path) is None
    assert "event=correction.household_mic_invalid" in caplog.text


def test_read_household_mic_treats_wrong_schema_as_absent(tmp_path: Path, caplog):
    path = tmp_path / "household_mic.json"
    path.write_text(json.dumps({"schema": 99, "model_key": "x"}))
    caplog.set_level(logging.WARNING, logger="jasper.correction.household_mic")

    assert hm.read_household_mic(path=path) is None
    assert "event=correction.household_mic_invalid" in caplog.text


def test_read_household_mic_treats_missing_fields_as_absent(tmp_path: Path):
    path = tmp_path / "household_mic.json"
    path.write_text(json.dumps({"schema": 1, "model_key": ""}))
    assert hm.read_household_mic(path=path) is None


def test_household_mic_record_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError):
        hm.HouseholdMicRecord.from_dict([1, 2, 3])  # type: ignore[arg-type]


# --- serial display -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("123", "123"),
        ("700-1234", "1234"),
        ("  700 1234  ", "1234"),
    ],
)
def test_serial_display_from_raw(raw, expected):
    assert hm.serial_display_from_raw(raw) == expected


# --- upload findability by content hash --------------------------------------
#
# The low-level `find_stored_calibration_by_content_hash` primitive is pinned
# in tests/test_correction_calibration.py next to its sibling
# `find_stored_calibration`. These tests cover the household-record-level
# resolution built on top of it.


def test_resolve_household_mic_calibration_finds_upload_by_id(tmp_path: Path):
    root = tmp_path / "calibrations"
    record = _store(tmp_path)
    household = hm.household_mic_from_calibration(record)

    resolved = hm.resolve_household_mic_calibration(household, root=root)
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id


def test_resolve_household_mic_calibration_falls_back_to_content_hash(
    tmp_path: Path,
):
    """If the household record's exact calibration_id can no longer be found
    (e.g. a future ID-scheme change), resolution still succeeds purely from
    the content hash — the additive lookup this feature depends on for
    uploads, which never carry a serial."""
    root = tmp_path / "calibrations"
    record = _store(tmp_path)
    household = hm.household_mic_from_calibration(record)
    # Simulate calibration_id drift: the record on disk is unchanged, but the
    # household pointer no longer matches it by ID.
    stale = hm.HouseholdMicRecord(
        model_key=household.model_key,
        label=household.label,
        calibration_id="some-other-id-that-does-not-exist",
        curve_sha256=household.curve_sha256,
        orientation=household.orientation,
        provider=household.provider,
        updated_at=household.updated_at,
    )

    resolved = hm.resolve_household_mic_calibration(stale, root=root)
    assert resolved is not None
    assert resolved.calibration_id == record.calibration_id


def test_resolve_household_mic_calibration_returns_none_when_unresolvable(
    tmp_path: Path,
):
    root = tmp_path / "calibrations"
    root.mkdir(parents=True)
    household = hm.HouseholdMicRecord(
        model_key="other",
        label="Lab mic",
        calibration_id="missing-id",
        curve_sha256="0" * 64,
        orientation="unknown",
        provider="manual_upload",
        updated_at=1.0,
    )
    assert hm.resolve_household_mic_calibration(household, root=root) is None


def test_resolve_household_mic_calibration_fails_soft_on_corrupt_metadata_file(
    tmp_path: Path,
):
    """A calibration_id lookup can find a FILE with that id but a corrupt/
    incomplete body (e.g. `CalibrationRecord.from_dict` raising `KeyError`
    on a missing field) — this must degrade to the content-hash fallback (or
    None), never raise, since every caller of this function documents it as
    fail-soft."""
    root = tmp_path / "calibrations"
    record = _store(tmp_path, root=root)
    metadata_path = Path(record.metadata_path)
    metadata_path.write_text(json.dumps({"calibration_id": record.calibration_id}))

    household = hm.household_mic_from_calibration(record)
    # The exact-ID lookup hits the corrupt file (KeyError inside
    # CalibrationRecord.from_dict) and must fall through cleanly, not raise.
    assert hm.resolve_household_mic_calibration(household, root=root) is None
