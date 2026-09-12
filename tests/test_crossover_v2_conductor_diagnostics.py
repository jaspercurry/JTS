# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: cap-aware composition and per-capture diagnostic logging."""

from __future__ import annotations

import logging
import numpy as np
import pytest
from jasper.active_speaker.crossover_v2.intervention import LINEARIZATION_MIN_PAIRED_OCCURRENCES
from jasper.active_speaker.crossover_v2.intervention import compose_sigma_db as _compose_sigma_db
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2 import diagnostics
from jasper.active_speaker.crossover_v2_flow import (
    ALIGNMENT_CONFIDENCE_TRUST_FLOOR,
    GAIN_CAP_BACKOFF_DB,
    PILOT_LEVEL_DELTA_DB,
    CrossoverV2Session,
    _analysis_json,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.mic_meter import (
    MIC_USABLE_MAX_DBFS,
    MIC_USABLE_MIN_DBFS,
)
from jasper.audio_measurement.program import (
    RoleBand,
    BASE_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.program_analysis import (
    CHANNEL_MAP_TARGET_RISE_DB,
    CHANNEL_MAP_MIN_ISOLATION_DB,
    AlignmentEstimate,
    CrossoverCandidate,
    DriftEstimate,
    GainPlan,
    ProgramAnalysis,
    analyze_program_capture,
)
from tests._log_events import event_field_maps, event_fields, event_records
from tests.crossover_v2_fixtures import (
    FC_HZ,
    FakeSeams,
    SESSION,
    _DIAG_LOGGER,
    _alignment,
    _check_analysis_with_solves,
    _conductor,
    _driver_response_diag,
    _loc,
    _measure_analysis,
    _pilot_obs,
    _preset,
    _profiled_conductor,
    _resp_with_repeats,
    _run_phase,
    _stage2_after_measure,
    _verify_analysis,
)


# --- W6.1 Finding A: cap-aware CHECK / MEASURE / VERIFY composition -------------
#
# The conductor fixture (CAPS) knew the caps, but the fake play seam never ran
# admission, so a CHECK/VERIFY program that ignored the caps slipped through the
# hardware-free suite and only surfaced on JTS3 (program_channel_peak_over_cap
# refused the CHECK program). These pins compose the real programs and run them
# through the ACTUAL admission the play seam uses.


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
    """CHECK pilots keep the 10 dB behavioral delta where headroom allows, and
    degrade honestly (recorded in the program) where a driver cap compresses the
    level — the JTS3 tweeter drops ~33 dB but its pair stays 10 dB apart."""
    c, _topology, _profile, _targets, sv = _profiled_conductor(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    check = c.program_for_phase(PHASE_CHECK)

    # Woofer: cap (-8) leaves headroom, so the pair rides the reference base and
    # keeps the full 10 dB delta.
    w_hi = check.segment("pilot_woofer_hi")
    w_lo = check.segment("pilot_woofer_lo")
    assert w_hi.gain_db == pytest.approx(BASE_STIMULUS_PEAK_DBFS)
    assert w_hi.gain_db - w_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    # Tweeter: cap (-65) compresses the base ~33 dB down, honestly recorded in
    # the segment gains + effective peak — but the 10 dB delta is preserved so
    # the behavioral-linearity check still has its two known levels.
    t_hi = check.segment("pilot_tweeter_hi")
    t_lo = check.segment("pilot_tweeter_lo")
    assert t_hi.gain_db < BASE_STIMULUS_PEAK_DBFS
    assert t_hi.gain_db - t_lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)
    assert t_hi.effective_peak_dbfs <= -65.0 + 1e-9
    assert t_hi.effective_peak_dbfs >= -65.0 - PILOT_LEVEL_DELTA_DB


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


# --- W6.5: the sensitivity-derived HF ceiling drives PRODUCTION composition -----
#
# The 2026-07-19 gate blocker: the derived ceiling existed in admission but the
# conductor context resolved caps WITHOUT the proven-HP flag, so every composed
# level (CHECK pilot bases, MEASURE back_off_gain, VERIFY min(caps)) still
# clamped to the legacy -65 — reviewer-measured composed CHECK pilot: -65.01.
# This pin drives the conductor with caps resolved EXACTLY the way the fixed
# resolve_conductor_context resolves them (program_admission=True + the
# declaration's sensitivities) and asserts the composed tweeter hi pilot lands
# at the derived cap, then that admission (same declared mapping) agrees.


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
    # Probe (b): the composed CHECK tweeter hi pilot rides the DERIVED cap
    # (back_off margin under -33.2), not the legacy -65.01 the gate measured.
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


# --- per-capture diagnostic logging (durable observability, Part 1) -------------
#
# Every CHECK/MEASURE/VERIFY capture now logs its full numeric diagnostics via
# ``log_event`` on BOTH the accepted path and every rejection — before this
# change a failed hardware run left no numbers to look at (only a partial
# ``program_analysis.glitch`` line existed, and only for a glitch MEASURE).
# These tests pin the event names + key fields on accept AND reject.


def test_diag_logging_bug_cannot_crash_or_flip_the_verdict(caplog, monkeypatch):
    """The diag-logging call is wrapped defensively (``_safe_log_diag``),
    symmetric with the capture-retention path's own best-effort guarantee —
    a bug in a ``_log_*_diag`` method must degrade to a WARN, never crash
    the capture or change the verdict already decided above it. Exercises
    all three phases through the SAME shared wrapper."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)

    monkeypatch.setattr(
        c, "_log_check_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(AttributeError("boom")),
    )
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True  # the verdict is completely unaffected
    fields = event_fields(caplog, "correction.crossover_v2_diag_log_failed")
    assert fields["phase"] == "check"
    caplog.clear()

    monkeypatch.setattr(
        c, "_log_measure_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(TypeError("boom")),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_diag_log_failed")
    assert fields["phase"] == "measure"
    caplog.clear()

    fakes.apply_done = True
    monkeypatch.setattr(
        c, "_log_verify_diag",
        lambda analysis, verdict: (_ for _ in ()).throw(ValueError("boom")),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_diag_log_failed")
    assert fields["phase"] == "verify"


def test_check_diag_logs_full_numbers_on_accept(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(
            _pilot_obs("woofer", snr_db=20.0, target_rise_db=18.0, cross_rise_db=1.0),
            _pilot_obs("tweeter", snr_db=15.0, target_rise_db=22.0, cross_rise_db=2.0),
        ),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_check_diag")
    assert fields["accepted"] == "true"
    assert fields["pilot_snr_ok"] == "true"
    # The two independent rung-2 tells (#2647): both False here, so this pin
    # also covers the accepted, neither-fired shape.
    assert fields["anchor_ambiguous"] == "false"
    assert fields["delta_implausible"] == "false"
    assert fields["woofer_delta_implausible"] == "false"
    assert fields["tweeter_delta_implausible"] == "false"
    assert fields["woofer_snr_db"] == "20.0"
    assert fields["tweeter_snr_db"] == "15.0"
    assert fields["woofer_captured_delta_db"] == "10.0"
    assert fields["woofer_programmed_delta_db"] == "10.0"
    assert fields["woofer_channel_map_target_rise_db"] == "18.0"
    assert fields["tweeter_channel_map_cross_rise_db"] == "2.0"
    # Both RAW rises AND the ratio derived from them: the raws keep a sweep of
    # these lines comparable across the 2026-08-21 metric switch, the ratio is
    # what the CROSS verdict is now decided on.
    assert fields["woofer_channel_map_isolation_db"] == "17.0"
    assert fields["tweeter_channel_map_isolation_db"] == "20.0"


def test_check_emits_a_named_row_per_driver_with_its_absolute_level(caplog):
    """#1922: the CHECK verdict reduces every per-driver fact to one boolean.

    So a ``channel_map_mismatch`` cannot say WHICH driver was silent, though
    the system knows. One row per role restores the attribution, and carries
    the fact the whole gate chain never asks: an ABSOLUTE level. Every shipped
    rung is relative (rise above ambient, ratio within a pilot pair, SNR
    floor), so a 12 dB rise in a very quiet room passes arbitrarily quiet and
    nothing bounds the top at all.

    Advisory, and pinned as such: the verdict is unchanged by the level window
    (#1894 — adjudication explores inside the declared fence, never moves it).
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(
            _pilot_obs("woofer", peak_hi_dbfs=-24.0),
            # Present, correct, and far under the usable window's floor: the
            # exact shape every relative rung passes.
            _pilot_obs("tweeter", peak_hi_dbfs=-72.0, mic_meter_status="too_quiet"),
        ),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    # ADVISORY: the quiet driver does not refuse the phase.
    assert verdict["accepted"] is True

    rows = event_field_maps(caplog, "correction.crossover_v2_check_pilot")
    assert [row["role"] for row in rows] == ["woofer", "tweeter"]
    assert rows[0]["level_sanity"] == "usable"
    assert rows[1]["level_sanity"] in {"low", "too_quiet"}
    # The window travels with the reading, so an old row stays readable after
    # the constants move.
    assert rows[1]["level_usable_min_dbfs"] == str(MIC_USABLE_MIN_DBFS)
    assert rows[1]["level_usable_max_dbfs"] == str(MIC_USABLE_MAX_DBFS)
    # The per-driver verdicts the aggregate collapsed, named.
    assert rows[1]["channel_map_ok"] == "true"
    assert rows[1]["peak_hi_dbfs"] == "-72.0"


def test_check_diag_names_the_isolation_ratio_and_its_bound_on_a_refusal(caplog):
    """A `channel_map_mismatch` refusal has to be readable from the journal.

    The household-facing copy stays number-free by design (the Language guide:
    one reason, one action), so the diag line IS the operator's record of the
    refusal. Since 2026-08-21 the CROSS verdict is decided on the ISOLATION
    RATIO rather than an additive cross-rise bound, so the line has to carry
    both the ratio and the bound it was graded against — without the bound, a
    journal of old lines is silently reinterpreted the next time the constant
    moves, and the operator cannot tell a refusal from a near miss.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        ambient_report={"bands": [{"level_dbfs": -70.0}]},
        pilots=(
            # The swap shape: the driver played (target clears the floor) but
            # the other band rose with it, so the isolation collapses.
            _pilot_obs("woofer", target_rise_db=40.0, cross_rise_db=39.0,
                       channel_map_ok=False),
            _pilot_obs("tweeter", target_rise_db=22.0, cross_rise_db=2.0),
        ),
        linearity_ok=True, channel_map_ok=False, pilot_snr_ok=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "channel_map_mismatch"
    fields = event_fields(caplog, "correction.crossover_v2_check_diag")
    assert fields["woofer_channel_map_isolation_db"] == "1.0"
    assert fields["channel_map_min_isolation_db"] == str(CHANNEL_MAP_MIN_ISOLATION_DB)
    # ...and the threshold, without which a sub-bound isolation figure on a
    # QUIET capture would read as the cause of a refusal that never happened:
    # below it the ratio is published but decides nothing.
    assert fields["channel_map_cross_judged_above_db"] == str(
        CHANNEL_MAP_TARGET_RISE_DB
    )
    # The raws that produced the ratio are still there to attribute it with.
    assert fields["woofer_channel_map_target_rise_db"] == "40.0"
    assert fields["woofer_channel_map_cross_rise_db"] == "39.0"


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


def test_check_diag_discloses_the_per_driver_measure_level_solve(caplog):
    """#1825 honesty: the solved MEASURE level and the ambient evidence it
    rests on land in the journal, one event per driver."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)
    assert _run_phase(c, 1, 1)["accepted"] is True
    event = "correction.crossover_v2_measure_level_solve"
    maps = event_field_maps(caplog, event)
    assert len(maps) == 2
    (woofer_fields,) = (m for m in maps if m["role"] == "woofer")
    assert woofer_fields["solved_gain_db"] == "-19.0"
    assert woofer_fields["flat_target_gain_db"] == "-11.0"
    assert woofer_fields["reduction_db"] == "8.0"
    assert woofer_fields["bound_by"] == "room_snr"
    assert woofer_fields["ambient_dbfs"] == "-60.0"
    assert woofer_fields["required_snr_db"] == "41.0"
    assert woofer_fields["band_lo_hz"] == "150.0"
    assert woofer_fields["band_hi_hz"] == "2000.0"
    (tweeter_fields,) = (m for m in maps if m["role"] == "tweeter")
    assert tweeter_fields["solved_gain_db"] == "-31.0"
    assert tweeter_fields["reduction_db"] == "18.0"
    assert tweeter_fields["ambient_dbfs"] == "-72.0"


def test_check_diag_discloses_the_level_solve_on_a_rejected_check_too(caplog):
    """Knowing what level the solve WOULD have chosen is exactly what an
    `snr_floor` refusal needs read beside it — so the disclosure rides the
    diagnostic path, not the accept path."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis_with_solves(
        program, snr_floor_ok=False,
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False and verdict["code"] == "snr_floor"
    assert len(event_records(caplog, "correction.crossover_v2_measure_level_solve")) == 2


def test_check_diag_survives_a_gain_plan_without_solves(caplog):
    """A legacy/fixture plan carries no ``role_solves``; the disclosure must
    simply not fire rather than crash the diagnostic path."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)  # default _check_analysis, no role_solves
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert event_records(caplog, "correction.crossover_v2_check_diag")
    assert not event_records(caplog, "correction.crossover_v2_measure_level_solve")
    assert not event_records(caplog, "correction.crossover_v2_diag_log_failed")


def test_check_pilot_delta_is_the_delta_measure_pilots_actually_use():
    """#1825's pilot floor reserves `hi_seg.gain_db - lo_seg.gain_db` read off
    the CHECK program — because that is what MEASURE's own leading pair will
    drop its quiet side by (`SessionExcitation.pilot_gains` /
    `PILOT_LEVEL_DELTA_DB`). If the
    two ever diverged the floor would be mis-sized in silence, so pin them
    equal at the composers that produce them."""
    from jasper.active_speaker.crossover_v2_flow import PILOT_LEVEL_DELTA_DB

    fakes = FakeSeams()
    fakes.check = _check_analysis_with_solves
    c = _conductor(fakes)

    check = c.program_for_phase("check")
    for role in ("woofer", "tweeter"):
        lo = check.segment(f"pilot_{role}_lo")
        hi = check.segment(f"pilot_{role}_hi")
        assert hi.gain_db - lo.gain_db == pytest.approx(PILOT_LEVEL_DELTA_DB)

    assert _run_phase(c, 1, 1)["accepted"] is True
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
    assert _run_phase(c, 1, 1)["accepted"] is True
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


def test_check_diag_logs_full_numbers_on_rejection_too(caplog):
    """The bug this fixes: a rejected CHECK used to leave no numbers behind."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        pilots=(
            _pilot_obs("woofer", snr_db=5.0, snr_valid=False),
            _pilot_obs("tweeter", snr_db=15.0),
        ),
        linearity_ok=True, channel_map_ok=True, pilot_snr_ok=False,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "snr_floor"
    fields = event_fields(caplog, "correction.crossover_v2_check_diag")
    assert fields["accepted"] == "false"
    assert fields["code"] == "snr_floor"
    assert fields["pilot_snr_ok"] == "false"
    # Numbers still present on the rejected capture.
    assert fields["woofer_snr_db"] == "5.0"
    assert fields["tweeter_snr_db"] == "15.0"


def test_check_diag_names_which_rung_2_signal_fired(caplog):
    """#2647's SHOULD-FIX: the shared ``anchor_ambiguous`` code cannot say
    which of the two independent tells fired. The structured fields must,
    down to which driver's delta was the implausible one.
    """
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.check = lambda program: ProgramAnalysis(
        phase="check", program_id=program.program_id,
        locations=(_loc("pilot_woofer_hi", "pilot"),),
        pilots=(
            _pilot_obs("woofer", captured_delta_db=-55.0, delta_implausible=True),
            _pilot_obs("tweeter"),
        ),
        linearity_ok=True, channel_map_ok=False, pilot_snr_ok=True,
        anchor_ambiguous=False, delta_implausible=True,
        gain_plan=GainPlan(
            gain_db={"woofer": -11.0, "tweeter": -13.0},
            predicted_peak_dbfs=-11.0, snr_floor_ok=True,
        ),
    )
    c = _conductor(fakes)
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is False
    assert verdict["code"] == "anchor_ambiguous"
    fields = event_fields(caplog, "correction.crossover_v2_check_diag")
    assert fields["code"] == "anchor_ambiguous"
    assert fields["anchor_ambiguous"] == "false"
    assert fields["delta_implausible"] == "true"
    assert fields["woofer_delta_implausible"] == "true"
    assert fields["tweeter_delta_implausible"] == "false"


def test_measure_diag_logs_full_numbers_on_accept(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
            repeat_level_delta_db=0.05,
        ),
        driver_responses=(
            _driver_response_diag(
                "woofer", window_ms=8.0, floor_hz=180.0, snr_db=25.0, snr_verdict="ok",
            ),
            _driver_response_diag(
                "tweeter", window_ms=9.0, snr_db=8.0, snr_verdict="insufficient",
            ),
        ),
        alignment=AlignmentEstimate(
            delay_us=150.0, raw_delay_us=161.0, parallax_us=11.0,
            polarity="normal", polarity_sign=1, polarity_agrees_with_sum=True,
            confidence=0.9, seed_delay_us=120.0,
            confidence_source="gcc_phat_seed",
        ),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
            alignment_seed_ripple_db=4.56, flatness_improvement_db=3.33,
            anchor_delay_us=145.0, snap_delta_us=5.0, snap_found=True,
            alignment_objective="flat_sum_committed", seed_polarity_sign=-1,
            left_anchor_lobe=True,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )
    monkeypatch.setattr(diagnostics, "analysis_json", lambda analysis: {"drift_us": 37.5})
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["accepted"] == "true"
    assert fields["alignment_confidence"] == "0.9"
    assert fields["alignment_confidence_source"] == "gcc_phat_seed"
    assert fields["alignment_seed_delay_us"] == "120.0"
    assert fields["alignment_refinement_delta_us"] == "37.5"
    assert fields["gate_window_ms"] == "8.0"  # min(8.0, 9.0)
    assert fields["validity_floor_hz"] == "180.0"  # max(180.0) — only one floor set
    assert fields["epsilon_ppm"] == "30.0"
    assert fields["max_residual_samples"] == "0.2"
    assert fields["repeat_level_delta_db"] == "0.05"
    assert fields["delay_role"] == "tweeter"  # positive delay_us ⇒ tweeter delayed
    # ``polarity`` here is the candidate-facing keep/invert action
    # (``alignment_to_candidate_fields``'s third return value), not the raw
    # AlignmentEstimate.polarity ("normal"/"inverted") — "normal" maps to
    # POLARITY_KEEP ("keep").
    assert fields["polarity"] == "keep"
    assert fields["predicted_ripple_db"] == "1.23"
    assert fields["alignment_seed_ripple_db"] == "4.56"
    assert fields["flatness_improvement_db"] == "3.33"
    assert fields["anchor_delay_us"] == "145.0"
    assert fields["snap_delta_us"] == "5.0"
    assert fields["snap_found"] == "true"
    assert fields["woofer_snr_db"] == "25.0"
    assert fields["woofer_snr_verdict"] == "ok"
    assert fields["tweeter_snr_db"] == "8.0"
    assert fields["tweeter_snr_verdict"] == "insufficient"
    # #2598 / #2607 S2: WHICH objective committed the (polarity, delay) pair,
    # what correlation answered, whether the two agreed, and whether the
    # committed delay left the comb lobe its anchor owns. The lobe flag is the
    # compensating control for the ±1-period search — a wrong-lobe commit is
    # magnitude-flat, so an on-axis VERIFY cannot contradict it and the journal
    # and the receipt are where it has to be legible.
    assert fields["alignment_objective"] == "flat_sum_committed"
    assert fields["seed_polarity"] == "inverted"
    assert fields["polarity_agrees_with_sum"] == "true"
    assert fields["left_anchor_lobe"] == "true"
    evidence = _analysis_json(fakes.measure(c.program_for_phase(PHASE_MEASURE)))
    assert evidence["alignment_confidence_source"] == "gcc_phat_seed"
    assert evidence["alignment_seed_delay_us"] == 120.0
    assert evidence["alignment_seed_ripple_db"] == 4.56
    assert evidence["flatness_improvement_db"] == 3.33
    assert evidence["anchor_delay_us"] == 145.0
    assert evidence["snap_delta_us"] == 5.0
    assert evidence["snap_found"] is True
    assert evidence["alignment_objective"] == "flat_sum_committed"
    assert evidence["seed_polarity"] == "inverted"
    assert evidence["polarity_agrees_with_sum"] is True
    assert evidence["left_anchor_lobe"] is True


def _measure_snr_analysis(program, *, woofer, tweeter):
    """A minimal accepted MEASURE whose two driver responses carry SNR blocks."""
    return ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(_loc("sweep_w"), _loc("sweep_t"), _loc("sweep_w_rep")),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
        ),
        driver_responses=(woofer, tweeter),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )


def test_measure_diag_names_the_band_behind_each_driver_snr_pair(caplog):
    """#2613: `*_snr_db` and `*_snr_verdict` say how bad and how trusted,
    never WHICH band. Fourteen consecutive jts3 rounds logged
    `tweeter_snr_db=-1.2 tweeter_snr_verdict=insufficient` and the band that
    actually limited them — one the tweeter sweep never entered — had to be
    re-derived from the crossover frequency and the declared driver bands
    because no persisted artifact carried it. `*_snr_band` is that band,
    read off the same `worst_relevant` entry as the pair beside it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    woofer = _driver_response_diag(
        "woofer", snr_db=25.0, snr_verdict="ok", snr_band="mid",
    )
    tweeter = _driver_response_diag(
        "tweeter", snr_db=-1.2, snr_verdict="insufficient", snr_band="upper_bass",
    )
    fakes.measure = lambda program: _measure_snr_analysis(
        program, woofer=woofer, tweeter=tweeter,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["woofer_snr_band"] == "mid"
    assert fields["tweeter_snr_band"] == "upper_bass"
    # Each band is the one its OWN worst_relevant entry carries, so a
    # role-crossed read cannot pass.
    assert woofer.snr["worst_relevant"]["band_id"] == "mid"
    assert tweeter.snr["worst_relevant"]["band_id"] == "upper_bass"
    # The #2613 line, now self-describing rather than a bare number.
    assert fields["tweeter_snr_db"] == "-1.2"
    assert fields["tweeter_snr_verdict"] == "insufficient"


def test_measure_diag_snr_band_is_null_when_the_worst_band_carries_no_id(caplog):
    """`worst_band_verdict` filters candidates on band overlap and verdict
    rank, never on identity, so it can select a band with no `band_id`. That
    has to log as `null` — Python's `None` stringified into `band=None` would
    read as a band literally named "None"."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_snr_analysis(
        program,
        woofer=_driver_response_diag(
            "woofer", snr_db=25.0, snr_verdict="ok", snr_band=None,
        ),
        tweeter=_driver_response_diag("tweeter"),  # no snr block at all
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["woofer_snr_band"] == "null"  # not the Python str(None) "None"
    # The number beside it still reports, so the field says "this band has no
    # id", not "there was no measurement".
    assert fields["woofer_snr_db"] == "25.0"
    # A driver with no SNR block at all reaches the same literal.
    assert fields["tweeter_snr_band"] == "null"


def test_measure_diag_logs_per_role_repeat_epsilon_ppm(caplog):
    """#1668 PR-A/PR-C: DriftEstimate.per_role_epsilon_ppm (a first-vs-last
    per-role epsilon, one entry per role with >=2 located occurrences) now
    surfaces as woofer_repeat_epsilon_ppm / tweeter_repeat_epsilon_ppm on
    the measure_diag event — diagnostic only, never gated."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: ProgramAnalysis(
        phase="measure", program_id=program.program_id,
        locations=(
            _loc("sweep_w"), _loc("sweep_t"),
            _loc("sweep_w_rep"), _loc("sweep_t_rep"),
        ),
        drift=DriftEstimate(
            epsilon_ppm=30.0,
            max_residual_samples=0.2, glitch_detected=False,
            per_role_epsilon_ppm={"woofer": 31.5, "tweeter": -4.25},
        ),
        driver_responses=(
            _driver_response_diag("woofer", window_ms=8.0),
            _driver_response_diag("tweeter", window_ms=9.0),
        ),
        alignment=_alignment(),
        candidate=CrossoverCandidate(
            trim_db={"woofer": -3.0, "tweeter": 0.0}, polarity="normal",
            delay_us=150.0, predicted_ripple_db=1.23, confidence=0.9,
        ),
        linearity_ok=True,
        predicted_sum=(np.linspace(100.0, 20000.0, 64), np.zeros(64)),
        glitch_detected=False,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["woofer_repeat_epsilon_ppm"] == "31.5"
    assert fields["tweeter_repeat_epsilon_ppm"] == "-4.25"


def test_measure_diag_per_role_repeat_epsilon_ppm_none_safe_for_legacy_drift(caplog):
    """A DriftEstimate predating per_role_epsilon_ppm (empty mapping — the
    field's own default) or a role absent from it must log None, never
    raise or fabricate a 0.0."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    # log_event renders None as the JSON literal "null", not Python's "None".
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["woofer_repeat_epsilon_ppm"] == "null"
    assert fields["tweeter_repeat_epsilon_ppm"] == "null"


def test_measure_diag_logs_full_numbers_on_glitch_rejection_too(caplog):
    """The headline bug this fixes: today a rejected MEASURE persists none of
    confidence/gate_window/epsilon — this proves they're all still logged."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(program, glitch=True)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "drift_baselines_disagree"
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["accepted"] == "false"
    assert fields["code"] == "drift_baselines_disagree"
    assert fields["gate_window_ms"] == "8.0"
    assert fields["epsilon_ppm"] == "30.0"
    assert fields["alignment_confidence"] == "0.8"
    assert fields["predicted_ripple_db"] == "0.8"
    # The pre-existing glitch check, not G2 — guard stays empty.
    assert fields["guard"] == ""


def test_measure_diag_logs_full_numbers_on_low_alignment_confidence(caplog):
    """The numbers still ride the diag, on a capture that is now ACCEPTED.

    Transformed with the demotion, the same way the ripple test below was: what
    the capture measured is unchanged, and only the consequence moved. ``guard``
    now names the disclosure, because its siblings name checks that REFUSED and
    leaving a refusal's vocabulary on an accepting path would mislead exactly
    the reader that field exists for."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    assert 0.55 < ALIGNMENT_CONFIDENCE_TRUST_FLOOR  # keep the fixture below the floor
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, alignment=_alignment(confidence=0.55),
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    assert not verdict.get("code")
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["alignment_confidence"] == "0.55"
    assert fields["predicted_ripple_db"] == "0.8"
    assert fields["guard"] == "alignment_confidence_disclosure"
    # The dedicated event is the stable line to alert or count on — ``guard``
    # is one field on a diagnostic that fires on every capture.
    fields = event_fields(caplog, "correction.crossover_v2_alignment_confidence_disclosed")
    assert fields["trust_floor"] == "0.6"


def test_measure_diag_logs_guard_field_on_ripple_disclosure(caplog):
    """The diag ``guard`` field still names G1 on an ACCEPTED capture (#2087).

    Transformed from the pre-ruling pin, which asserted ``guard=ripple_ceiling``
    on a refusal. The value changed with the behaviour: its siblings name
    checks that REFUSED, so a path that now accepts must not keep a refusal's
    vocabulary. This is what keeps the existing per-capture telemetry able to
    find these captures — and asserting ``accepted`` alongside it is the point,
    since a reader of this field can no longer infer a rejection from it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.measure = lambda program: _measure_analysis(
        program, predicted_ripple_db=27.316,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    verdict = _run_phase(c, 2, 2)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["guard"] == "ripple_disclosure"
    assert fields["predicted_ripple_db"] == "27.316"


def test_measure_diag_logs_guard_field_on_sweep_schedule_fire(caplog):
    """The diag ``guard`` field distinguishes a G2 fire from the pre-
    existing glitch_detected branch — both share the reused
    drift_baselines_disagree code (see the glitch test above for the "guard
    empty" counterpart)."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    fakes.measure = lambda program: _measure_analysis(
        program,
        sweep_locations=(
            _loc("sweep_w", confidence=0.8,
                 residual_samples=-25e-3 * program.sample_rate_hz),
            _loc("sweep_t", confidence=0.8),
            _loc("sweep_w_rep", confidence=0.8),
        ),
    )
    verdict = _run_phase(c, 2, 2)
    assert verdict["code"] == "drift_baselines_disagree"
    fields = event_fields(caplog, "correction.crossover_v2_measure_diag")
    assert fields["guard"] == "sweep_schedule"
    assert fields["sweep_residual_ms_worst"] == "-25.0"
    assert fields["sweep_locate_confidence_min"] == "0.8"


def test_verify_diag_logs_full_numbers_on_accept(caplog):
    """VERIFY discloses its numbers even when round trust cannot be established."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: ProgramAnalysis(
        phase="verify", program_id=program.program_id,
        locations=(_loc("sweep_verify", "summed_sweep"),),
        summed_response=_driver_response_diag("summed", window_ms=8.5, floor_hz=900.0),
        summed_ripple_db=1.1,
        verify_tracking={
            "rms_db": 0.4, "max_db": 0.9, "max_db_notch_excluded": 0.9,
            "tracking_band_hz": [800.0, 3200.0],
        },
        linearity_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["accepted"] == "true"
    assert fields["max_db_notch_excluded"] == "0.9"
    assert fields["verify_tolerance_db"] == "1.5"
    assert fields["verify_gate_window_ms"] == "8.5"
    assert fields["measure_gate_window_ms"] == "8.0"
    assert fields["validity_floor_hz"] == "900.0"
    assert fields["tracking_band_lo_hz"] == "800.0"
    assert fields["tracking_band_hi_hz"] == "3200.0"
    assert fields["rms_db"] == "0.4"
    # No pilots on this fixture (a legacy-shaped ProgramAnalysis) — G3's
    # fields render as absent, never a false 0.0.
    assert fields["pilot_transfer_db"] == "null"
    assert fields["pilot_transfer_step_db"] == "null"
    assert fields["guard"] == ""


def test_verify_diag_logs_full_numbers_on_an_advisory_tracking_failure(caplog):
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    fakes.verify = lambda program: _verify_analysis(program, max_db=5.0, gate_ms=8.5)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_code == "verify_out_of_tolerance"
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["accepted"] == "true"
    assert fields["code"] == ""
    assert fields["max_db_notch_excluded"] == "5.0"
    assert fields["verify_gate_window_ms"] == "8.5"
    assert fields["measure_gate_window_ms"] == "8.0"


def test_verify_diag_logs_full_numbers_on_inconclusive_rejection(caplog):
    """A too-short VERIFY gate rejects as ``verify_inconclusive`` BEFORE the
    tracking-error branch even runs — confirms the diag log still fires and
    still carries the two gate-window numbers that decided it."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    # measure_gate_window_ms defaults to 8.0 (the happy-path MEASURE fixture);
    # a VERIFY gate narrower than that is inconclusive per §5.2.
    fakes.verify = lambda program: _verify_analysis(program, gate_ms=4.0)
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    _run_phase(c, 2, 2)
    fakes.apply_done = True
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_inconclusive"
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["verify_gate_window_ms"] == "4.0"
    assert fields["measure_gate_window_ms"] == "8.0"


def test_verify_diag_logs_guard_field_and_pilot_transfer_on_level_shift_fire(caplog):
    """Measurement-honesty gate G3's own diagnostics: the baseline-setting
    attempt logs its raw transfer with a null step and empty guard; the
    fired attempt logs its own transfer, the computed step, and
    guard=pilot_level_shift."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)
    # transfer = level_hi_dbfs(-20.0) - programmed_hi_gain_db(-20.0) = 0.0.
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["pilot_transfer_db"] == "0.0"
    assert fields["pilot_transfer_step_db"] == "null"
    assert fields["guard"] == ""
    caplog.clear()

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_level_shift"
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    # transfer = level_hi_dbfs(-19.44) - programmed_hi_gain_db(-20.0) = 0.56.
    assert fields["pilot_transfer_db"] == "0.56"
    assert fields["pilot_transfer_step_db"] == "0.56"
    assert fields["guard"] == "pilot_level_shift"


def test_verify_diag_pilot_transfer_step_does_not_leak_across_an_early_return(caplog):
    """Adversarial-review fix (S1): ``_verify_pilot_transfer_step_db`` must
    reset at the TOP of every ``_verify_verdict`` call (mirrors
    ``_last_measure_guard``'s method-top reset in ``_measure_verdict``) — an
    early return BEFORE the G3 block even runs (locate_failed here) must not
    leave a PRIOR attempt's REAL step number for ``_log_verify_diag`` (which
    runs unconditionally) to misreport as if it were computed this attempt."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    # Attempt 1 (N-1): establishes the baseline (independently out of
    # tolerance, so a retry is admitted).
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)

    # Attempt 2 (N): a REAL, non-None step gets computed and logged (0.1 dB,
    # within the ceiling — independently out of tolerance too, so a 3rd
    # attempt is admitted). ``max_db`` is deliberately more than
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, max_db=8.0,
    )
    _run_phase(c, 3, 4)
    maps = event_field_maps(caplog, "correction.crossover_v2_verify_diag")
    assert maps[-1]["pilot_transfer_step_db"] == "0.1"
    caplog.clear()

    # Attempt 3 (N+1): locate_failed — returns BEFORE the G3 block runs at
    # all. Without the S1 fix this would still show attempt 2's stale 0.1;
    # with it, the diag must show null.
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.1, locate_confidence=0.01,
    )
    verdict = _run_phase(c, 3, 5)
    assert verdict["code"] == "locate_failed"
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["pilot_transfer_step_db"] == "null"


# --------------------------------------------------------------------------- #
# Layer-1a driver linearization (#1668 PR-C)
# --------------------------------------------------------------------------- #


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
