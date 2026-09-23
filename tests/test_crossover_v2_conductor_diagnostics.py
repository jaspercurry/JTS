# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: cap-aware composition."""

from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.intervention import LINEARIZATION_MIN_PAIRED_OCCURRENCES
from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db as _compose_sigma_db
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session
from jasper.active_speaker.crossover_v2.programs import GAIN_CAP_BACKOFF_DB, PILOT_LEVEL_DELTA_DB
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    RoleBand,
    BASE_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.program_analysis import (
    ProgramAnalysis,
    analyze_program_capture,
)
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    SESSION,
    _check_analysis_with_solves,
    _conductor,
    _pilot_obs,
    _preset,
    _resp_with_repeats,
    _run_phase,
)


pytestmark = pytest.mark.usefixtures("banked_session_level")


# --- W6.1 Finding A: cap-aware CHECK / MEASURE / VERIFY composition -------------
#
# The conductor fixture (CAPS) knew the caps, but the fake play seam never ran
# admission, so a CHECK/VERIFY program that ignored the caps slipped through the
# hardware-free suite and only surfaced on JTS3 (program_channel_peak_over_cap
# refused the CHECK program). These pins compose the real programs and run them
# through the ACTUAL admission the play seam uses.


def _profiled_conductor(*, woofer_peak: float, tweeter_peak: float):
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )

    from tests.test_active_speaker_program_admission import _profile_and_targets

    topology, profile, targets = _profile_and_targets(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )
    sv = session_measurement_volume_db(profile, targets.values())
    caps = {"woofer": float(woofer_peak), "tweeter": float(tweeter_peak)}
    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=roles,
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=sv,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )
    return c, topology, profile, targets, sv


@pytest.mark.parametrize(
    "woofer_peak,tweeter_peak",
    # The JTS3-shaped 0/-8/-65 cap numbers across the two profile-valid combos
    # (a tweeter capped above code policy, e.g. -8, cannot be confirmed).
    [(0.0, -65.0), (-8.0, -65.0)],
)
def test_composed_programs_admit_at_shaped_caps(tmp_path, woofer_peak, tweeter_peak):
    """CHECK and MEASURE admit at the JTS3-shaped caps; VERIFY (no admission
    path — it rides the applied graph) is clamped to the most restrictive cap.

    This is the pin that was missing (the conductor knew the caps but the fake
    play seam never admitted). The readmit gate REFUSES VERIFY by design
    (test_active_speaker_program_admission.test_verify_program_not_admitted_here
    pins that — VERIFY is mono/summed with no per-driver target), so VERIFY's
    equivalent safety proof is its compose-time clamp: no segment can exceed the
    binding cap that its summed signal reaches every driver at.
    """
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        readmit_program_from_wav,
    )
    from jasper.audio_measurement.program import write_program_wav

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=woofer_peak, tweeter_peak=tweeter_peak
    )

    def _admit(program):
        wav = tmp_path / "program.wav"
        write_program_wav(wav, program)
        return readmit_program_from_wav(
            program, wav, topology=topology, safety_profile=profile,
            role_targets=targets, session_volume_db=sv,
        )

    adm_check = _admit(c.program_for_phase(PHASE_CHECK))
    assert adm_check.allowed, adm_check.refusals

    _run_phase(c, 1, 1)  # CHECK solve → MEASURE composed
    adm_measure = _admit(c.program_for_phase(PHASE_MEASURE))
    assert adm_measure.allowed, adm_measure.refusals

    # VERIFY has no admission path by design; its clamp is the only guard.
    with pytest.raises(ProgramAdmissionError):
        _admit(c.program_for_phase(PHASE_VERIFY))
    binding_cap = min(woofer_peak, tweeter_peak)
    for seg in c.program_for_phase(PHASE_VERIFY).stimulus_segments():
        assert seg.effective_peak_dbfs <= binding_cap + 1e-9


def test_check_pilot_pairs_preserve_delta_and_degrade_honestly():
    """CHECK pilots keep their 10 dB delta after both level bounds apply."""
    c, _topology, _profile, _targets, _sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    check = c.program_for_phase(PHASE_CHECK)

    w_hi = check.segment("pilot_woofer_hi")
    w_lo = check.segment("pilot_woofer_lo")
    assert w_hi.gain_db <= BASE_STIMULUS_PEAK_DBFS
    assert w_hi.gain_db - w_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    t_hi = check.segment("pilot_tweeter_hi")
    t_lo = check.segment("pilot_tweeter_lo")
    assert t_hi.gain_db < BASE_STIMULUS_PEAK_DBFS
    assert t_hi.gain_db - t_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert t_hi.effective_peak_dbfs == pytest.approx(-65.0 - GAIN_CAP_BACKOFF_DB)


def test_verify_pilot_pair_preserves_delta_after_clamp():
    """VERIFY's summed pilot pair rides the min-cap-clamped level but keeps its
    10 dB delta (no admission gate protects VERIFY, so the clamp must not
    silently collapse the pair to one level)."""
    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    verify = c.program_for_phase(PHASE_VERIFY)
    v_hi = verify.segment("pilot_summed_hi")
    v_lo = verify.segment("pilot_summed_lo")
    assert v_hi.gain_db - v_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert v_hi.effective_peak_dbfs <= -65.0 + 1e-9
    # And the summed sweep itself is clamped to the same binding cap.
    assert verify.segment("sweep_verify").effective_peak_dbfs <= -65.0 + 1e-9


def test_uncapped_check_program_would_be_refused_regression(tmp_path):
    """The pre-W6.1 shape: a CHECK program composed at the shared reference base
    (ignoring caps) is refused by admission on the JTS3 tweeter — the exact
    program_channel_peak_over_cap refusal hardware run 2 hit."""
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionRefusal,
        readmit_program_from_wav,
    )
    from jasper.audio_measurement.program import build_check_program, write_program_wav

    c, topology, profile, targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    uncapped = build_check_program(c._roles, downstream_gain_db=sv)  # no role bases
    wav = tmp_path / "uncapped.wav"
    write_program_wav(wav, uncapped)
    adm = readmit_program_from_wav(
        uncapped, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in adm.refusals


def test_verify_wav_rendered_sample_peak_respects_min_cap(tmp_path):
    """Byte-level pin for the VERIFY clamp (W6.1 gate nit): VERIFY has NO
    play-time readmit — the rendered WAV's actual sample peak is what the
    speaker emits — so assert the WAV bytes themselves, not just the schedule:
    sample peak + session volume ≤ min cap (+0.1 dB int16 quantization slack)."""
    import math as _math

    from scipy.io import wavfile

    from jasper.audio_measurement.program import write_program_wav

    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    wav = tmp_path / "verify_program.wav"
    write_program_wav(wav, c.program_for_phase(PHASE_VERIFY))
    rate, data = wavfile.read(str(wav))
    assert rate == c.program_for_phase(PHASE_VERIFY).sample_rate_hz
    peak = float(np.max(np.abs(data.astype(np.float64) / 32767.0)))
    assert peak > 0.0  # the clamped program still carries signal
    peak_dbfs = 20.0 * _math.log10(peak)
    binding_cap = -65.0
    assert peak_dbfs + sv <= binding_cap + 0.1
    # And it is not clamped into oblivion: the sweep sits within a few dB of
    # the cap-backoff level (the clamp targets the cap, not silence).
    assert peak_dbfs + sv >= binding_cap - 1.0


def test_jts3_derived_hf_ceiling_drives_production_conductor_composition(tmp_path):
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.program_admission import readmit_program_from_wav
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )
    from jasper.audio_measurement.program import write_program_wav

    from tests.test_active_speaker_program_admission import _profile_and_targets

    # JTS3 declaration: Epique E150HE-44 83.3 dB / B&C DE250-8 108.5 dB.
    declared = {"woofer": 83.3, "tweeter": 108.5}
    topology, profile, targets = _profile_and_targets(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    # PRODUCTION cap resolution — the exact call the fixed context site makes.
    caps = {}
    for role, fingerprint in targets.items():
        _band, cap = resolve_driver_excitation_ceilings(
            profile,
            fingerprint,
            program_admission=True,
            declared_sensitivities=declared,
        )
        caps[role] = float(cap)
    # Probe (a): context caps == admission caps == the derived {-8, -33.2}.
    # -33.2 is the sensitivity arithmetic (-8 less the 25.2 dB delta); the
    # provisional -35 dBFS absolute hedge over it was retired 2026-08-20.
    assert caps == {"woofer": -8.0, "tweeter": pytest.approx(-33.2)}
    sv = session_measurement_volume_db(
        profile, targets.values(), declared_sensitivities=declared
    )
    assert sv == -20.0  # max(caps) is still the woofer's — volume unchanged

    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10000.0)),
    ]
    c = CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=roles,
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=sv,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )
    t_hi = c.program_for_phase(PHASE_CHECK).segment("pilot_tweeter_hi")
    assert t_hi.effective_peak_dbfs == pytest.approx(-33.2 - GAIN_CAP_BACKOFF_DB)
    # And the play-time gate (same declared mapping, as bind_production_play
    # now threads it) admits what the conductor composed.
    wav = tmp_path / "check.wav"
    write_program_wav(wav, c.program_for_phase(PHASE_CHECK))
    adm = readmit_program_from_wav(
        c.program_for_phase(PHASE_CHECK), wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
        declared_sensitivities=declared,
    )
    assert adm.allowed, adm.refusals
    facts = {f.role: f for f in adm.channels}
    assert facts["tweeter"].cap_dbfs == pytest.approx(-33.2)
    # Without the declared mapping (the pre-fix admission view) the SAME
    # composed program is refused — the incoherence the threading closes.
    stale = readmit_program_from_wav(
        c.program_for_phase(PHASE_CHECK), wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not stale.allowed


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
