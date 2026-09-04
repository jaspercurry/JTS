# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one crossover-v2 conductor/session round-trip split out of
``test_audio_measurement_program_analysis.py`` (Wave 0, PR 0b).

Quarantined here — not merged back into that file — because it is the only
test there that builds a ``CrossoverV2Session`` (via
``tests/crossover_v2_fixtures.py``'s ``_conductor``/``FakeSeams``), so the
census's largest class-A file can stay session-free.
"""
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import CrossoverSection, crossover_response_complex
from jasper.audio_measurement import program_analysis
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.program_analysis import (
    MeasurementPriors,
    analyze_program_capture,
)

from tests.test_audio_measurement_program_analysis import (
    FC_HZ,
    SR,
    _band_impulse,
    _configured_path_priors,
    _synthesize,
    _transfers,
)


@pytest.mark.parametrize("identity_protection", [False, True])
def test_configured_path_matches_legacy_through_analyzer_and_fitter(
    monkeypatch, identity_protection,
):
    """One shared H makes neutral M*C/P and legacy H*C equivalent end to end.

    The plants are NON-ZERO across the whole rfft grid, including outside each
    role's swept band: the fixture's first shape zeroed them at exactly the band
    edges, which made every out-of-band composition choice invisible — and that
    is where a spectrum spliced at the driven-band edge steps by tens of dB.
    ``identity_protection`` is §4.2's required ``P == C_configured`` subcase on
    the tweeter, with the woofer keeping a role-specific ``P`` (the JTS3 shape).
    """
    configured = {
        "woofer": (CrossoverSection(FC_HZ, 4, False),),
        "tweeter": (CrossoverSection(FC_HZ, 4, True),),
    }
    protection = {
        "woofer": (CrossoverSection(6000.0, 4, False),),
        "tweeter": (
            configured["tweeter"] if identity_protection
            else (CrossoverSection(300.0, 4, True),)
        ),
    }
    tweeter_lo_hz = 1300.0 if identity_protection else 300.0
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(tweeter_lo_hz, 20000.0)),
    ]
    n_fft = 16_384
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    plants = {
        "woofer": (0.8 + 0.1 * np.cos(freqs / 911.0)) * np.exp(-2j * np.pi * freqs * 180 / SR),
        "tweeter": (0.6 + 0.1 * np.sin(freqs / 733.0)) * np.exp(-2j * np.pi * freqs * 205 / SR),
    }
    mode = {"neutral": True}

    def exact_deconvolution(_capture, segment, *_args, **_kwargs):
        role = segment.role
        sections = protection[role] if mode["neutral"] else configured[role]
        sign = -1 if role == "tweeter" and not mode["neutral"] else 1
        response = crossover_response_complex(freqs, sections)
        return np.fft.irfft(plants[role] * response * sign, n=n_fft), 192

    monkeypatch.setattr(
        program_analysis.dispatch, "_deconvolve_window", exact_deconvolution
    )
    program = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, roles,
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    capture = _synthesize(
        program, woofer_ir=_band_impulse(180, 150, 6000, 0.8),
        tweeter_ir=_band_impulse(205, tweeter_lo_hz, 20000, -0.6), noise=0.0,
    )
    neutral = analyze_program_capture(
        program, capture, SR,
        priors=dataclasses.replace(
            _configured_path_priors(_transfers(protection), _transfers(configured)),
            mic_tier="reference",
        ),
    )
    mode["neutral"] = False
    legacy = analyze_program_capture(
        program, capture, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ, mic_tier="reference"),
    )

    # §4.2 compares the arms "on the same conditioning-valid bins", and the dB
    # arrays are float32: once the plants are honestly non-zero out of band they
    # reach -168 dB, where ONE float32 ULP is 1.53e-5 dB, 15x the plan's 1e-6 dB
    # — unassertable at that precision. Nothing is excused: the whole grid is
    # pinned at rtol+atol 1e-6 (float32 eps is 1.2e-7, ~8 ULP), then at the
    # plan's 1e-6 ABSOLUTE over the band a role declared. Every other field,
    # including every complex response, already agrees to <= 1.02e-10.
    claimed = np.fft.rfftfreq(n_fft, 1.0 / SR) <= max(
        role.band.upper_hz for role in roles
    )

    def assert_same(left, right):
        if isinstance(left, np.ndarray):
            if left.dtype == np.float32 and left.shape == claimed.shape:
                np.testing.assert_allclose(left, right, rtol=1e-6, atol=1e-6)
                left, right = left[claimed], right[claimed]
            np.testing.assert_allclose(left, right, rtol=0, atol=1e-6)
        elif isinstance(left, dict):
            assert left.keys() == right.keys()
            for key in left:
                assert_same(left[key], right[key])
        elif isinstance(left, (list, tuple)):
            assert len(left) == len(right)
            for a, b in zip(left, right):
                assert_same(a, b)
        elif isinstance(left, (float, int, np.number)):
            assert float(left) == pytest.approx(float(right), abs=1e-6)
        else:
            assert left == right

    # Provenance MUST differ: §4.2 says the arms' sources do, and fingerprint
    # equality is not a compatibility criterion.
    assert neutral.configured_path_composed is True
    assert legacy.configured_path_composed is False
    assert_same(
        dataclasses.asdict(dataclasses.replace(neutral, configured_path_composed=False)),
        dataclasses.asdict(legacy),
    )
    from tests.crossover_v2_fixtures import FakeSeams, _conductor
    fitted = []
    for analysis in (neutral, legacy):
        conductor = _conductor(FakeSeams())
        conductor._measure_program = program
        # The planner since #2291 Phase 2b, which returns what the fitter used
        # to leave on the conductor. The three values compared are the same
        # three: the committed trims, the emitted filters, and the linearized
        # VERIFY prediction.
        plan = conductor._plan_linearization(analysis, analysis.candidate, None)
        fitted.append((
            dict(plan.role_attenuations_db),
            dict(plan.linearization),
            plan.linearized_predicted_sum,
        ))
    assert_same(fitted[0], fitted[1])
