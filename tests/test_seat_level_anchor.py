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
from pathlib import Path

import pytest

from jasper.active_speaker import seat_level_reference as slr
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    write_seat_level_reference,
)

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


@pytest.mark.parametrize(
    ("banked", "cal_text", "ceiling_db_spl", "reason"),
    [
        pytest.param(True, CAL_WITH_SENS, CEILING_DB_SPL, None, id="under_the_ceiling"),
        # The mutation guard on the ceiling: the anchor exactly at it resolves,
        # a hair under it refuses, so a dropped comparison fails the next row.
        pytest.param(True, CAL_WITH_SENS, ANCHOR_DB_SPL, None, id="at_the_ceiling"),
        pytest.param(
            True,
            CAL_WITH_SENS,
            ANCHOR_DB_SPL - 0.5,
            slr.LEVEL_OVER_CEILING,
            id="over_the_ceiling",
        ),
        pytest.param(
            False, CAL_WITH_SENS, CEILING_DB_SPL, slr.ANCHOR_UNUSABLE, id="no_anchor",
        ),
        pytest.param(
            True, CAL_CURVE_ONLY, CEILING_DB_SPL, slr.ANCHOR_UNUSABLE,
            id="no_absolute_mic_reference",
        ),
        # The pair that pins the banked-vs-live comparison: a mic recalibrated
        # since the anchor refuses, and a re-quoted one inside the tolerance
        # still resolves, so neither dropping the comparison nor tightening it
        # to equality passes both rows.
        pytest.param(
            True, CAL_RECALIBRATED, CEILING_DB_SPL, slr.ANCHOR_UNUSABLE,
            id="recalibrated",
        ),
        pytest.param(
            True, CAL_REQUOTED, CEILING_DB_SPL, None, id="requoted_within_tolerance",
        ),
    ],
)
def test_a_level_resolves_or_names_the_input_it_is_missing(
    tmp_path, monkeypatch, anchor, banked, cal_text, ceiling_db_spl, reason
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
            ceiling_db_spl=ceiling_db_spl, calibration_file=str(cal),
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
        mic_serial="8108494",
    )


def test_the_mic_looked_up_is_the_one_the_anchor_was_banked_with(anchor, monkeypatch):
    """With no explicit mic stated, the record names WHICH mic to resolve now."""
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(MIC_LOOKUP, _capture)

    with pytest.raises(slr.LevelUnresolved) as excinfo:
        slr.resolve_anchor_level(ceiling_db_spl=CEILING_DB_SPL)

    assert excinfo.value.reason == slr.ANCHOR_UNUSABLE
    assert seen == {"calibration_file": None, "mic_serial": "8108494"}


def test_the_ceiling_comes_from_the_presets_own_declaration(
    tmp_path, anchor, monkeypatch
):
    """The hard stop is read through the one reader that owns it.

    Stubbed, not read off this box: a ceiling compared against the same live
    resolution it came from asserts nothing, and would move with whatever
    topology the machine running the suite happens to have.
    """
    cal = tmp_path / "mic.txt"
    cal.write_text(CAL_WITH_SENS)
    monkeypatch.setattr(
        "jasper.output_topology.load_output_topology_strict", lambda: "topology"
    )
    monkeypatch.setattr(
        "jasper.active_speaker.commission_wiring.commissioning_spl_ceiling_db",
        lambda _topology: ANCHOR_DB_SPL + 1.0,
    )

    resolved = slr.resolve_anchor_level(calibration_file=str(cal))
    assert resolved.anchor_db_spl == ANCHOR_DB_SPL

    def _unresolvable(*_a, **_k):
        raise ValueError("no preset on this box")

    monkeypatch.setattr(
        "jasper.active_speaker.commission_wiring.commissioning_spl_ceiling_db",
        _unresolvable,
    )
    with pytest.raises(slr.LevelUnresolved) as excinfo:
        slr.resolve_anchor_level(calibration_file=str(cal))
    assert excinfo.value.reason == slr.PRESET_UNAVAILABLE


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
