# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper import bass_alignment
from jasper.audio_measurement.null_walk import NullWalkError


def test_sub_mains_timing_uses_shared_geometry_bounded_null_walk():
    spec = bass_alignment.sub_mains_delay_walk_spec(
        corner_hz=80.0,
        sub_path_minus_mains_m=0.343,
        transport_delay_ms=2.0,
        step_us=100.0,
    )

    assert spec.crossover_fc_hz == 80.0
    assert spec.geometry_seed_us == pytest.approx(3000.0)
    assert spec.half_period_us == pytest.approx(6250.0)
    assert spec.step_us == 100.0


def test_sub_mains_signed_delay_targets_the_correct_dsp_path():
    spec = bass_alignment.sub_mains_delay_walk_spec(
        corner_hz=5000.0,
        sub_path_minus_mains_m=0.0,
    )

    assert spec.dsp_candidate(100.0).delay_target == "mains"
    assert spec.dsp_candidate(-100.0).delay_target == "subwoofer"


def test_wireless_sub_transport_can_move_geometry_seed_across_zero():
    acoustic_only = bass_alignment.sub_mains_delay_walk_spec(
        corner_hz=5000.0,
        sub_path_minus_mains_m=-0.0343,
    )
    with_sub_transport = bass_alignment.sub_mains_delay_walk_spec(
        corner_hz=5000.0,
        sub_path_minus_mains_m=-0.0343,
        transport_delay_ms=0.2,
    )

    assert acoustic_only.geometry_seed_us == pytest.approx(-100.0)
    assert acoustic_only.dsp_candidate(-100.0).delay_target == "subwoofer"
    assert with_sub_transport.geometry_seed_us == pytest.approx(100.0)
    assert with_sub_transport.dsp_candidate(100.0).delay_target == "mains"


def test_sub_mains_timing_refuses_when_no_bass_corner(monkeypatch):
    monkeypatch.setattr(bass_alignment, "active_crossover_corner_hz", lambda: None)
    with pytest.raises(NullWalkError, match="no active crossover corner"):
        bass_alignment.sub_mains_delay_walk_spec(sub_path_minus_mains_m=0.0)
