# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker.alignment_walk import (
    DRIVER_DELAY_WALK_SCOPE,
    driver_delay_walk_spec,
)


def test_driver_delay_walk_spec_derives_geometry_seed_and_targets():
    active = driver_delay_walk_spec(
        crossover_fc_hz=2000.0,
        positive_delay_target_role="upper",
        negative_delay_target_role="lower",
        signed_acoustic_path_difference_m=0.0343,
        step_us=50.0,
    )

    assert active.geometry_seed_us == pytest.approx(100.0)
    assert active.half_period_us == pytest.approx(250.0)
    assert active.dsp_candidate(100.0).delay_target == "upper"
    assert DRIVER_DELAY_WALK_SCOPE == "active_crossover"
