# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin each phase's linearity refusal and each owner's admission site."""

from collections import Counter
from unittest.mock import patch

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_dispatch, refusal_copy, spatial
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.refusal_copy import REASON_AGC_BEHAVIORAL_FAIL
from tests.crossover_v2_fixtures import (
    FakeSeams, _check_analysis, _conductor, _measure_analysis, _run_phase, _stage2_conductor, _verify_analysis,
)

LINEARITY_SITES = {
    capture_dispatch.assess: (PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY),
    spatial.lateral_pose_screens: (PHASE_LATERAL,),
    spatial.entry_baseline_screens: (PHASE_ENTRY_BASELINE,),
    spatial.cloud_position_screens: (PHASE_CLOUD_VERIFY,),
}
DRIVERS = {
    PHASE_CHECK: ("check", _check_analysis),
    PHASE_MEASURE: ("measure", _measure_analysis),
    PHASE_LATERAL: ("measure", _measure_analysis),
    PHASE_ENTRY_BASELINE: ("verify", _verify_analysis),
    PHASE_VERIFY: ("verify", _verify_analysis),
    PHASE_CLOUD_VERIFY: ("verify", _verify_analysis),
}


def _refuse_at(phase):
    phases = (phase,) if phase in (PHASE_VERIFY, PHASE_CLOUD_VERIFY) else (
        (PHASE_CHECK,) if phase == PHASE_CHECK else
        (PHASE_CHECK, PHASE_MEASURE) if phase == PHASE_MEASURE else
        (PHASE_CHECK, PHASE_MEASURE, phase)
    )
    fakes = FakeSeams()
    build = _stage2_conductor if len(phases) == 1 and phase != PHASE_CHECK else _conductor
    conductor = build(fakes, index_phase_map=dict(enumerate(phases, 1)))
    for index in range(1, len(phases)):
        _run_phase(conductor, index, 1)
    seam, analysis = DRIVERS[phase]
    setattr(fakes, seam, lambda program: analysis(program, linearity=False))
    return _run_phase(conductor, len(phases), 1)


@pytest.mark.parametrize("phase", DRIVERS)
def test_a_non_linear_capture_is_refused_as_agc_behavioral_fail(phase):
    verdict = _refuse_at(phase)
    assert verdict["accepted"] is False
    assert verdict["code"] == REASON_AGC_BEHAVIORAL_FAIL


def _linearity_admission_sites():
    return {capture_dispatch.assess, *(
        getattr(spatial, name) for name in spatial.__all__ if name.endswith("_screens")
    )}


def test_every_linearity_admission_site_is_covered_by_a_row_above():
    assert _linearity_admission_sites() == set(LINEARITY_SITES)
    classified = [phase for phases in LINEARITY_SITES.values() for phase in phases]
    assert Counter(classified) == Counter(DRIVERS.keys())


@pytest.mark.parametrize("site,phases", LINEARITY_SITES.items(), ids=lambda value: getattr(value, "__name__", None))
def test_the_tripwire_looks_in_every_module_that_carries_the_rule(site, phases):
    owners = Counter(site.__module__ for site in _linearity_admission_sites())
    assert owners == {capture_dispatch.__name__: 1, spatial.__name__: 3}
    assert owners[flow.__name__] == 0
    for phase in phases:
        with patch(f"{site.__module__}.{site.__name__}", wraps=site) as called:
            verdict = _refuse_at(phase)
        assert verdict["code"] == REASON_AGC_BEHAVIORAL_FAIL
        assert called.call_args_list[-1].args[0].linearity_ok is False


def test_checks_own_linearity_rule_is_deliberately_not_the_plain_one():
    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY != REASON_AGC_BEHAVIORAL_FAIL
    assert refusal_copy.REASON_NOISY_ROOM_LINEARITY in flow.REASON_REGISTRY
