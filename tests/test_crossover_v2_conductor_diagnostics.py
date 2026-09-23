# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: cap-aware composition and per-capture diagnostic logging."""

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.intervention import LINEARIZATION_MIN_PAIRED_OCCURRENCES
from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db as _compose_sigma_db
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
)
from jasper.active_speaker.crossover_v2.programs import PILOT_LEVEL_DELTA_DB
from jasper.audio_measurement.program_analysis import (
    ProgramAnalysis,
    analyze_program_capture,
)
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    _check_analysis_with_solves,
    _conductor,
    _pilot_obs,
    _resp_with_repeats,
    _run_phase,
)


#
# The conductor fixture (CAPS) knew the caps, but the fake play seam never ran
# admission, so a CHECK/VERIFY program that ignored the caps slipped through the
# hardware-free suite and only surfaced on JTS3 (program_channel_peak_over_cap
# refused the CHECK program). These pins compose the real programs and run them
# through the ACTUAL admission the play seam uses.


#
# Every CHECK/MEASURE/VERIFY capture now logs its full numeric diagnostics via
# ``log_event`` on BOTH the accepted path and every rejection — before this
# change a failed hardware run left no numbers to look at (only a partial
# ``program_analysis.glitch`` line existed, and only for a glitch MEASURE).
# These tests pin the event names + key fields on accept AND reject.


pytestmark = pytest.mark.usefixtures("banked_session_level")


def test_check_priors_carry_fc_for_the_measure_level_solve():
    """#1825: CHECK's gain solve scopes each band's SNR requirement by whether
    the band sits inside the crossover overlap window, so Fc has to reach the
    CHECK analysis. It used to run on bare defaults."""
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    phase, _prog_phase, _result, priors, _geometry = fakes.analyzed[0]
    assert phase == "check"
    assert priors.crossover_fc_hz == pytest.approx(FC_HZ)


def test_check_pilot_delta_is_the_delta_measure_pilots_actually_use():
    """#1825's pilot floor reserves `hi_seg.gain_db - lo_seg.gain_db` read off
    the CHECK program — because that is what MEASURE's own leading pair will
    drop its quiet side by (`SessionExcitation.pilot_gains` /
    `PILOT_LEVEL_DELTA_DB`). If the
    two ever diverged the floor would be mis-sized in silence, so pin them
    equal at the composers that produce them."""

    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)

    check = c.program_for_phase("check")
    for role in ("woofer", "tweeter"):
        lo = check.segment(f"pilot_{role}_lo")
        hi = check.segment(f"pilot_{role}_hi")
        assert hi.gain_db - lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    assert _run_phase(c, 1, 1).ok is True
    measure = c.program_for_phase("measure")
    m_lo = measure.segment("pilot_woofer_lo")
    m_hi = measure.segment("pilot_woofer_hi")
    assert m_hi.gain_db - m_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)


def test_measure_program_keeps_solved_gains_per_role_and_identical_per_repeat():
    """Constraint the drift estimator depends on: the CHECK solve moves each
    ROLE's gain independently, but every repeat of a role stays bit-identical
    (`program.build_measure_program`'s own promise) — per-ROLE differs,
    per-REPEAT must not."""
    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1).ok is True
    measure = c.program_for_phase("measure")
    w_gains = {
        measure.segment(sid).gain_db
        for sid in ("sweep_w", "sweep_w_rep", "sweep_w_rep2")
    }
    t_gains = {
        measure.segment(sid).gain_db
        for sid in ("sweep_t", "sweep_t_rep", "sweep_t_rep2")
    }
    assert len(w_gains) == 1 and len(t_gains) == 1
    assert w_gains != t_gains


# Layer-1a driver linearization (#1668 PR-C)


def test_compose_sigma_db_none_when_own_under_paired_threshold():
    own = _resp_with_repeats("woofer", 1)  # 2 total occurrences, < 3
    sibling = _resp_with_repeats("tweeter", 4)  # 5 total, plenty
    assert 1 + len(own.repeat_responses) < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is None


def test_compose_sigma_db_none_when_sibling_under_paired_threshold():
    """An under-repeated SIBLING voids the pair's trust even though ``own``
    alone clears the threshold — this is the PAIRED gate, not a per-driver
    one."""
    own = _resp_with_repeats("woofer", 4)  # 5 total, plenty
    sibling = _resp_with_repeats("tweeter", 1)  # 2 total, < 3
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is None


def test_compose_sigma_db_returns_array_when_both_meet_threshold():
    own = _resp_with_repeats("woofer", 2)  # 3 total, exactly at the gate
    sibling = _resp_with_repeats("tweeter", 2)
    sigma = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert not np.isnan(sigma).any()


def test_compose_sigma_db_floors_at_the_tiers_own_tolerable_value():
    """Identical repeats -> live sigma ~ 0 everywhere -> floored up to the
    tier's own sigma_tolerable (consumer: 1.0 dB)."""
    own = _resp_with_repeats("woofer", 2)
    sibling = _resp_with_repeats("tweeter", 2)
    sigma = _compose_sigma_db(own, sibling, tier="consumer", valid_band_hz=(150.0, 4000.0))
    assert sigma is not None
    assert np.all(sigma >= 1.0 - 1e-9)
    assert np.allclose(sigma, 1.0, atol=1e-6)


def test_compose_sigma_db_floor_is_behaviorally_inert_on_repeatability_limit():
    """The docstring's 'currently does nothing' claim, proven end-to-end:
    repeatability_limit(floored_sigma) must equal repeatability_limit(
    raw_live_sigma) bin-for-bin, because any live sigma <=
    sigma_tolerable already saturates repeatability_limit's own
    min(1, ...) at its ceiling — flooring a value already at/below the
    floor changes nothing."""
    from jasper.active_speaker.linearization_envelope import (
        compute_sigma_curve,
        repeatability_limit,
    )

    own = _resp_with_repeats("woofer", 2)
    sibling = _resp_with_repeats("tweeter", 2)
    floored = _compose_sigma_db(own, sibling, tier="reference", valid_band_hz=(150.0, 4000.0))
    raw = compute_sigma_curve(own, valid_band_hz=(150.0, 4000.0))
    assert floored is not None and raw is not None
    assert not np.allclose(floored, raw)  # the floor DID change the sigma values themselves...
    limit_floored = repeatability_limit(floored, tier="reference")
    limit_raw = repeatability_limit(raw, tier="reference")
    np.testing.assert_allclose(limit_floored, limit_raw)  # ...but not the envelope term they feed


@pytest.mark.parametrize(
    ("levels", "grades", "status"),
    [([], [], None), ([-24.0], ["usable"], "usable"),
     ([-24.0, -50.0], ["usable", "low"], "low"),
     ([-24.0, -72.0], ["usable", "too_quiet"], "too_quiet"),
     ([-72.0, -10.0], ["too_quiet", "too_loud"], "too_loud"),
     ([-10.0, -72.0], ["too_loud", "too_quiet"], "too_loud")],
)
def test_analysis_owns_mic_grades(monkeypatch, levels, grades, status):
    program = _conductor(FakeSeams()).program_for_phase(PHASE_CHECK)
    monkeypatch.setattr(
        "jasper.audio_measurement.program_analysis.dispatch._analyze_check",
        lambda *args: ProgramAnalysis(
            program.phase, program.program_id, (),
            pilots=tuple(_pilot_obs(str(index), peak_hi_dbfs=level, mic_meter_status=None)
                         for index, level in enumerate(levels)),
        ),
    )
    analysis = analyze_program_capture(program, np.zeros(program.total_samples), program.sample_rate_hz)
    assert [pilot.mic_meter_status for pilot in analysis.pilots] == grades
    assert analysis.mic_meter_status == status
