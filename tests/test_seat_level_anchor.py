# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The banked anchor resolved to absolute dB SPL — or refused.

The load-bearing property: there is no relative fallback. Every input that
stops the anchor being usable ends as a refusal naming it, because a number
that looks absolute and was guessed is worse than no number.
"""

from __future__ import annotations

import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

from jasper.active_speaker import seat_level_reference as slr
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    write_seat_level_reference,
)
from jasper.audio_measurement import calibration

ANCHOR_DB_SPL = 77.5
REFERENCE_VOLUME_DB = -18.0
CEILING_DB_SPL = 85.0
CAL_WITH_SENS = '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
# The same mic, recalibrated (3 dB away) and re-quoted (0.04 dB away, inside
# :data:`slr.SENS_FACTOR_TOLERANCE_DB`) since the anchor banked -12.07.
CAL_RECALIBRATED = '"Sens Factor =-9.0dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
CAL_REQUOTED = '"Sens Factor =-12.03dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
CAL_CURVE_ONLY = "10.0\t-6.6\n10.2\t-6.5\n"

REPO_ROOT = Path(__file__).resolve().parents[1]
# The mic lookup is resolved through its own module so a test can replace it
# without importing it at seat-reference import time (see the numpy pin below).
MIC_LOOKUP = "jasper.audio_measurement.calibration.resolve_mic_sensitivity"


@pytest.fixture
def anchor(tmp_path, monkeypatch):
    """A converged seat-level reference on the env-resolved path."""
    path = tmp_path / "seat_level_reference.json"
    write_seat_level_reference(
        reference_volume_db=REFERENCE_VOLUME_DB,
        measured_db_spl=ANCHOR_DB_SPL,
        target=SeatLevelTarget(target_db_spl=ANCHOR_DB_SPL, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07, "serial": "8108494"},
        max_main_volume_db=-6.0,
        state_path=path,
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE", str(path))
    return path


@pytest.mark.parametrize("banked,cal_text,reason", [
    (True, CAL_WITH_SENS, None), (False, CAL_WITH_SENS, slr.ANCHOR_UNUSABLE),
    (True, CAL_CURVE_ONLY, slr.ANCHOR_UNUSABLE),
    (True, CAL_RECALIBRATED, slr.ANCHOR_UNUSABLE), (True, CAL_REQUOTED, None),
])
def test_a_level_resolves_or_names_the_input_it_is_missing(
    tmp_path, monkeypatch, anchor, banked, cal_text, reason
):
    if not banked:
        monkeypatch.setenv(
            "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE",
            str(tmp_path / "absent.json"),
        )
    cal = tmp_path / "mic.txt"
    cal.write_text(cal_text)

    def _resolve():
        return slr.resolve_anchor_level(
            calibration_file=str(cal),
        )

    if reason is not None:
        with pytest.raises(slr.LevelUnresolved) as excinfo:
            _resolve()
        assert excinfo.value.reason == reason
        # The three ways an anchor goes unusable share one slug, so the
        # sentence is what separates them.
        assert excinfo.value.detail
        return

    assert _resolve() == slr.ResolvedLevel(
        anchor_db_spl=ANCHOR_DB_SPL,
        reference_volume_db=REFERENCE_VOLUME_DB,
        mic_serial="8108494", session_id=slr.load_seat_level_reference()["session_id"],
        leveled_at=slr.load_seat_level_reference()["leveled_at"], target_db_spl=ANCHOR_DB_SPL,
    )


def test_the_mic_looked_up_is_the_one_the_anchor_was_banked_with(anchor, monkeypatch):
    """With no explicit mic stated, the record names WHICH mic to resolve now."""
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(MIC_LOOKUP, _capture)

    with pytest.raises(slr.LevelUnresolved) as excinfo:
        slr.resolve_anchor_level()

    assert excinfo.value.reason == slr.ANCHOR_UNUSABLE
    assert seen == {"calibration_file": None, "mic_serial": "8108494"}


@pytest.mark.parametrize("stored_serial", ["810-8494", "8108494", "810-8495"])
def test_banked_anchor_resolves_legacy_minidsp_serial_formats(
    tmp_path, monkeypatch, anchor, stored_serial,
):
    root = tmp_path / "calibrations"
    record = calibration.store_calibration(
        text=CAL_WITH_SENS.replace("8108494", stored_serial.replace("-", "")) + "1000\t0\n",
        provider="minidsp", model="minidsp_umik2", source="vendor_lookup",
        serial=stored_serial, orientation="0deg", sign_convention="response", root=root,
    )
    assert record.serial_hash == calibration.serial_hash(stored_serial)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(calibration, "find_stored_calibration", partial(
        calibration.find_stored_calibration, root=root,
    ))

    def no_vendor_fetch(*_args):
        pytest.fail("stored calibration lookup must not fetch")

    if stored_serial == "810-8495":
        with pytest.raises(slr.LevelUnresolved) as excinfo:
            slr.resolve_anchor_level()
        assert excinfo.value.reason == slr.ANCHOR_UNUSABLE
    else:
        assert slr.resolve_anchor_level() == slr.ResolvedLevel(
            anchor_db_spl=ANCHOR_DB_SPL, reference_volume_db=REFERENCE_VOLUME_DB,
            mic_serial="8108494", session_id=slr.load_seat_level_reference()["session_id"],
            leveled_at=slr.load_seat_level_reference()["leveled_at"], target_db_spl=ANCHOR_DB_SPL,
        )
        cached = calibration.fetch_vendor_calibration(
            model_key="minidsp_umik2", serial="8108494", root=root, opener=no_vendor_fetch,
        )
        assert cached.calibration_id == record.calibration_id
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_the_seat_reference_imports_without_numpy() -> None:
    """jasper-doctor and ``session_volume_plan`` read this module on a 1 GB Pi.

    Only :func:`resolve_anchor_level` needs the mic lookup, and reaching it
    costs ``jasper.audio_measurement`` — and therefore numpy — so that import
    is function-local. A subprocess, because the suite has numpy loaded long
    before this file runs.
    """
    probe = (
        "import sys, jasper.active_speaker.seat_level_reference; "
        "sys.exit('numpy' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, (
        "importing jasper.active_speaker.seat_level_reference now pulls numpy "
        "— something in its import chain grew a top-level import of "
        "jasper.audio_measurement (or another heavy sibling). Make it "
        "function-local at the point of use.\n\n" + result.stderr
    )


@pytest.mark.parametrize("version", [1, 2])
def test_session_schema_requires_leveling_after_upgrade(anchor, version):
    import json
    from jasper.active_speaker.session_volume_plan import measurement_reference_volume_db
    raw = json.loads(anchor.read_text())
    raw["artifact_schema_version"] = version
    anchor.write_text(json.dumps(raw))
    if version == 1:
        assert slr.load_seat_level_reference() is None
        with pytest.raises(slr.LevelUnresolved) as refused:
            measurement_reference_volume_db()
        assert refused.value.reason == slr.ANCHOR_UNUSABLE
    else:
        assert measurement_reference_volume_db() == REFERENCE_VOLUME_DB
        assert raw["session_id"] and raw["leveled_at"]
