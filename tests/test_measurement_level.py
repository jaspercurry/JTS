# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A program's anchor-relative level, resolved to absolute dB SPL — or refused.

The load-bearing property: there is no relative fallback. Every missing input
(no banked anchor, no readable mic calibration, no preset behind the ceiling)
ends as a refusal naming that input, because a number that looks absolute and
was guessed is worse than no number.
"""

from __future__ import annotations

import dataclasses

import pytest

from jasper.active_speaker import measurement_level as ml
from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker.seat_level_reference import (
    SeatLevelTarget,
    write_seat_level_reference,
)
from jasper.audio_measurement.calibration import (
    REFUSE_MIC_CALIBRATION_UNAVAILABLE,
)

ANCHOR_DB_SPL = 77.5
REFERENCE_VOLUME_DB = -18.0
CEILING_DB_SPL = 85.0
CAL_WITH_SENS = '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
CAL_CURVE_ONLY = "10.0\t-6.6\n10.2\t-6.5\n"


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


def _program(level_re_anchor_db: float) -> mp.MeasurementProgram:
    return dataclasses.replace(
        mp.program("baseline", "express"), level_re_anchor_db=level_re_anchor_db
    )


@pytest.mark.parametrize(
    ("banked", "cal_text", "level_re_anchor_db", "reason"),
    [
        pytest.param(True, CAL_WITH_SENS, 0.0, None, id="at_the_anchor"),
        # The mutation guard on the ceiling: +7 clears 85 and resolves, +10
        # does not and refuses, so a dropped comparison fails the second row.
        pytest.param(True, CAL_WITH_SENS, 7.0, None, id="under_the_ceiling"),
        pytest.param(
            True, CAL_WITH_SENS, 10.0, ml.LEVEL_OVER_CEILING, id="over_the_ceiling"
        ),
        pytest.param(
            False, CAL_WITH_SENS, 0.0, ml.SEAT_REFERENCE_MISSING, id="no_anchor"
        ),
        pytest.param(
            True,
            CAL_CURVE_ONLY,
            0.0,
            REFUSE_MIC_CALIBRATION_UNAVAILABLE,
            id="no_absolute_mic_reference",
        ),
    ],
)
def test_a_level_resolves_or_names_the_input_it_is_missing(
    tmp_path, monkeypatch, anchor, banked, cal_text, level_re_anchor_db, reason
):
    if not banked:
        monkeypatch.setenv(
            "JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE",
            str(tmp_path / "absent.json"),
        )
    cal = tmp_path / "mic.txt"
    cal.write_text(cal_text)

    def _resolve():
        return ml.resolve_program_level(
            _program(level_re_anchor_db),
            ceiling_db_spl=CEILING_DB_SPL,
            calibration_file=str(cal),
        )

    if reason is not None:
        with pytest.raises(ml.LevelUnresolved) as excinfo:
            _resolve()
        assert excinfo.value.reason == reason
        assert excinfo.value.reason in ml.LEVEL_REFUSAL_REASONS
        assert excinfo.value.detail
        return

    assert _resolve() == ml.ResolvedLevel(
        target_db_spl=ANCHOR_DB_SPL + level_re_anchor_db,
        anchor_db_spl=ANCHOR_DB_SPL,
        level_re_anchor_db=level_re_anchor_db,
        reference_volume_db=REFERENCE_VOLUME_DB,
        mic_sens_factor_db=-12.07,
        mic_serial="8108494",
    )


def test_a_walk_with_no_program_drives_at_the_anchor_itself(tmp_path, anchor):
    """``None`` is the free-form walk: anchor-relative zero, one receipt shape."""
    cal = tmp_path / "mic.txt"
    cal.write_text(CAL_WITH_SENS)

    level = ml.resolve_program_level(
        None, ceiling_db_spl=CEILING_DB_SPL, calibration_file=str(cal)
    )

    assert (level.level_re_anchor_db, level.target_db_spl) == (0.0, ANCHOR_DB_SPL)


def test_the_mic_looked_up_is_the_one_the_anchor_was_banked_with(anchor, monkeypatch):
    """With no explicit mic stated, the record names WHICH mic to resolve now."""
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(ml, "resolve_mic_sensitivity", _capture)

    with pytest.raises(ml.LevelUnresolved) as excinfo:
        ml.resolve_program_level(_program(0.0), ceiling_db_spl=CEILING_DB_SPL)

    assert excinfo.value.reason == REFUSE_MIC_CALIBRATION_UNAVAILABLE
    assert seen == {"calibration_file": None, "mic_serial": "8108494"}


def test_the_ceiling_comes_from_the_presets_own_declaration(monkeypatch):
    """One owner for the hard stop: the preset's ``max_commissioning_level_db_spl``."""
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.output_topology import load_output_topology_strict

    preset = resolve_capture_preset(load_output_topology_strict())
    assert ml._preset_ceiling_db_spl() == float(
        preset.safety.max_commissioning_level_db_spl
    )

    def _unresolvable(*_a, **_k):
        raise ValueError("no preset on this box")

    monkeypatch.setattr(
        "jasper.active_speaker.commission_wiring.resolve_capture_preset", _unresolvable
    )
    with pytest.raises(ml.LevelUnresolved) as excinfo:
        ml._preset_ceiling_db_spl()
    assert excinfo.value.reason == ml.PRESET_UNAVAILABLE
