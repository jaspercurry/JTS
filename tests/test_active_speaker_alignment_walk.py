# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker.alignment_walk import driver_delay_walk_spec
from jasper.bass_alignment import sub_mains_delay_walk_spec


def test_active_driver_and_bass_adapters_share_the_same_null_walk_contract():
    active = driver_delay_walk_spec(
        crossover_fc_hz=2000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0343,
        step_us=50.0,
    )
    bass = sub_mains_delay_walk_spec(
        corner_hz=2000.0,
        sub_path_minus_mains_m=0.0343,
        step_us=50.0,
    )

    assert active.candidate_delays_us() == bass.candidate_delays_us()
    assert active.geometry_seed_us == pytest.approx(100.0)
    assert active.half_period_us == pytest.approx(250.0)
    assert active.dsp_candidate(100.0).delay_target == "upper"
    assert bass.dsp_candidate(100.0).delay_target == "mains"
