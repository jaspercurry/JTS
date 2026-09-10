# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Multi-segment excitation-program admission (Wave 2 deliverable B).

Pins: a clean 2-channel MEASURE program is admitted; per-segment refusals for a
band escape and a peak over the driver cap; the artifact whole-file facts
(per-channel true peak <= cap, out-of-segment quiet, manifest-peak match); and
play-time re-admission catching tampered WAV bytes.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
import yaml
from scipy.io import wavfile

from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.crossover_v2.programs import SessionExcitation
from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_admission import (
    ProgramAdmissionError,
    ProgramAdmissionRefusal,
    readmit_program_from_wav,
    readmit_summed_program_from_wav,
)
from jasper.active_speaker.session_volume_plan import session_measurement_volume_db
from jasper.bass_extension.dynamic import DynamicBassDescriptor, dynamic_bass_gain_reserve_db
from jasper.camilla_emit import emit_gain_filter, emit_linkwitz_riley
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    RoleBand,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
    write_program_wav,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_active_speaker_audition import ACTIVE_PCM, _applied_profile
from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION, _trial_candidate
from tests.test_crossover_v2_session_graph import FakeCam, _entry, _graph as _session_graph


def _profile_and_targets(
    *,
    woofer_peak: float = 0.0,
    tweeter_peak: float = -65.0,
    max_sweep_duration_s: float = 6,
    woofer_floor: float = 500,
    woofer_measurement_floor: float | None = None,
    woofer_highpass: float | None = None,
    woofer_upper: float = 20_000,
):
    """Asymmetric caps by default (woofer 0.0, tweeter -65): the realistic
    2-way shape whose ~65 dB spread is exactly what the (fixed) session-volume
    derivation must handle — symmetric fixtures masked the min/max inversion."""
    topology = mono_output_topology()

    def _limits(peak):
        return {
            "max_effective_peak_dbfs": peak,
            "max_sweep_duration_s": max_sweep_duration_s,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 0,
        }

    common = {
        "hard_excitation_band_hz": [500, 20_000],
        "measurement_band_hz": [500, 10_000],
    }
    settings = {
        "drivers": [
            {
                **common,
                "hard_excitation_band_hz": [woofer_floor, woofer_upper],
                "measurement_band_hz": [
                    woofer_floor if woofer_measurement_floor is None else woofer_measurement_floor,
                    min(10_000, woofer_upper),
                ],
                "level_duration_limits": _limits(woofer_peak),
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "W",
                **({"recommended_highpass_hz": woofer_highpass} if woofer_highpass else {}),
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 132,
                    "baffle_width_mm": 210,
                },
            },
            {
                **common,
                "level_duration_limits": _limits(tweeter_peak),
                "target_id": "mono:tweeter",
                "role": "tweeter",
                "model": "T",
                # #2603: the tweeter's low limit is declared once, and its hard
                # band's floor and protective high-pass both derive from it.
                # This fixture used to share the woofer's 500 Hz hard floor
                # while declaring a 5000 Hz protective high-pass -- two numbers
                # for one driver's low limit, which is the shape the ruling
                # collapsed.
                "recommended_highpass_hz": 1500,
                "required_protection_filters": [
                    {"kind": "highpass", "cutoff_hz": 5000, "minimum_slope_db_per_octave": 24}
                ],
                "cabinet": {
                    "enclosure_kind": "sealed",
                    "radiator_count": 1,
                    "effective_radiating_diameter_mm": 25,
                },
            },
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topology,
        manual_settings=settings,
        driver_research=None,
        saved_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topology)}
    return topology, profile, targets


def _roles(woofer_band=(500.0, 1600.0), tweeter_band=(1600.0, 10_000.0)):
    return [
        RoleBand("woofer", 0, FrequencyBand(*woofer_band)),
        RoleBand("tweeter", 1, FrequencyBand(*tweeter_band)),
    ]


def _measure_program(session_volume_db, roles=None, gains=None, courtesy_prelude=False):
    roles = roles or _roles()
    # The default gain plan mirrors the corrected session-volume rule: the
    # woofer (highest cap) runs at the -6 dB digital guard; the tweeter
    # attenuates DOWN so gain + session_volume clears its -65 dB cap.
    gains = gains or {"woofer": -6.0, "tweeter": -46.0}
    return build_measure_program(
        gains, roles, downstream_gain_db=session_volume_db,
        courtesy_prelude=courtesy_prelude,
    )


def _admit(prog, *, topology, safety_profile, role_targets, session_volume_db,
           pcm=None, declared_sensitivities=None):
    """Admit through the actual play-time door: write ``prog`` to a WAV and
    read it back, the way every real caller does. There is no composition-time
    admission entry point -- ``admit_excitation_program`` had zero production
    callers and was retired; ``pcm`` (default: a clean render) lets a test
    attest tampered bytes the same way a tampered WAV would arrive."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        wav = Path(tmpdir) / "program.wav"
        if pcm is None:
            write_program_wav(wav, prog)
        else:
            clipped = np.clip(pcm, -1.0, 1.0)
            wavfile.write(
                str(wav), prog.sample_rate_hz, (clipped * 32767.0).astype(np.int16)
            )
        return readmit_program_from_wav(
            prog, wav, topology=topology, safety_profile=safety_profile,
            role_targets=role_targets, session_volume_db=session_volume_db,
            declared_sensitivities=declared_sensitivities,
        )


def test_clean_program_is_admitted():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert adm.allowed
    assert all(s.execution_allowed for s in adm.segments)
    # N=3 interleaved (sweep-composition PR-A, #1668): sweep_w/t, _rep, _rep2.
    assert len(adm.segments) == 6
    facts = {c.channel: c for c in adm.channels}
    assert facts[0].peak_within_cap and facts[1].peak_within_cap
    assert facts[0].quiet_out_of_segment and facts[1].quiet_out_of_segment
    assert facts[0].peak_matches_manifest and facts[1].peak_matches_manifest


def test_band_escape_refuses_segment():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    # Woofer band dips below the 500 Hz permitted floor.
    prog = _measure_program(sv, roles=_roles(woofer_band=(150.0, 1600.0)))
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in adm.refusals


def test_peak_over_ceiling_refuses():
    topology, profile, targets = _profile_and_targets()
    # A too-loud session volume pushes the tweeter's effective peak above its
    # -65 cap (gain -46 + volume -10 = -56 dBFS effective > -65).
    prog = _measure_program(-20.0)
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=-10.0,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in adm.refusals
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in adm.refusals


def test_asymmetric_caps_woofer_reaches_reference_while_tweeter_lands_at_cap():
    """The 2026-07-18 gate's asymmetric-cap admission proof (B1).

    Caps (woofer 0.0, tweeter -65): V = min(-20, max(caps)) = -20. The woofer's
    admitted effective peak reaches ≈ V - 6 (the digital guard) — NOT ~40 dB
    under its ceiling as the inverted min(caps) rule produced — while the
    tweeter attenuates down (-45 dB digital) and lands exactly at its own cap.
    Symmetric -65/-65 fixtures could never distinguish the two rules.
    """
    topology, profile, targets = _profile_and_targets(
        woofer_peak=0.0, tweeter_peak=-65.0
    )
    sv = session_measurement_volume_db(profile, targets.values())
    assert sv == -20.0
    prog = _measure_program(sv, gains={"woofer": -6.0, "tweeter": -45.0})
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert adm.allowed
    by_id = {s.segment_id: s for s in adm.segments}
    # Woofer: effective ≈ V - guard = -26 dBFS, far above the old -70.
    assert by_id["sweep_w"].effective_peak_dbfs == pytest.approx(sv - 6.0)
    assert by_id["sweep_w"].execution_allowed
    # Tweeter: digitally attenuated to land exactly at its own -65 cap.
    assert by_id["sweep_t"].effective_peak_dbfs == pytest.approx(-65.0)
    assert by_id["sweep_t"].execution_allowed
    facts = {c.role: c for c in adm.channels}
    assert facts["woofer"].effective_true_peak_dbfs == pytest.approx(sv - 6.0, abs=0.1)
    assert facts["tweeter"].effective_true_peak_dbfs == pytest.approx(-65.0, abs=0.1)
    assert facts["woofer"].peak_within_cap and facts["tweeter"].peak_within_cap


def test_jts3_derived_ceiling_flows_through_production_composition_and_admission():
    """W6.5: the JTS3 shape (woofer cap -8, tweeter cap at its -65 seed) with
    the DECLARED sensitivities (woofer 83.3 dB, tweeter 108.5 dB -- 25.2 dB
    delta, from the declaration, not the profile). Caps are resolved the way
    the production conductor context resolves them (``program_admission=True``
    + the declared mapping), and the gain plan is clamped through the
    production ``back_off_gain`` derivation against those caps -- NOT a
    hand-fed number -- so this pins that the derived -33.2 ceiling
    (-8 - 25.2, the sensitivity arithmetic with no hedge over it) actually
    drives what gets composed, then admits end-to-end with the same mapping.

    The provisional -35 dBFS absolute hedge that used to clamp this by a
    further 1.8 dB was retired 2026-08-20; this is the end-to-end mutation
    guard for that retirement -- restore the hedge and every number below
    moves.
    """
    from jasper.active_speaker.crossover_v2.programs import back_off_gain
    from jasper.active_speaker.excitation_safety_plan import (
        resolve_driver_excitation_ceilings,
    )
    from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS

    declared = {"woofer": 83.3, "tweeter": 108.5}
    topology, profile, targets = _profile_and_targets(
        woofer_peak=-8.0, tweeter_peak=-65.0
    )
    # The production context-site resolution (probe a: these ARE the caps
    # admission enforces below — one derivation, two consumers).
    caps = {}
    for role, fingerprint in targets.items():
        _band, cap = resolve_driver_excitation_ceilings(
            profile,
            fingerprint,
            program_admission=True,
            declared_sensitivities=declared,
        )
        caps[role] = float(cap)
    assert caps == {"woofer": -8.0, "tweeter": pytest.approx(-33.2)}
    sv = session_measurement_volume_db(
        profile, targets.values(), declared_sensitivities=declared
    )
    # max(caps) is still the woofer's -8 (its ceiling is untouched by the HF
    # derivation), so the session volume itself is unaffected by the change.
    assert sv == -20.0
    # The production composition clamp (the same call _compose_measure_program
    # makes): nominal reference gain backed off against each resolved cap. The
    # tweeter's composed level is cap-DRIVEN: -33.2 - sv - 0.01 = -13.21 dB
    # digital -> -33.21 dBFS effective. Under the old -65 cap this program
    # would have been refused as CHANNEL_PEAK_OVER_CAP.
    gains = {
        role: back_off_gain(BASE_STIMULUS_PEAK_DBFS, sv, caps[role])
        for role in caps
    }
    prog = _measure_program(sv, gains=gains)
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
        declared_sensitivities=declared,
    )
    assert adm.allowed
    by_id = {s.segment_id: s for s in adm.segments}
    assert by_id["sweep_t"].effective_peak_dbfs == pytest.approx(-33.21)
    assert by_id["sweep_t"].execution_allowed
    facts = {c.role: c for c in adm.channels}
    assert facts["tweeter"].cap_dbfs == pytest.approx(-33.2)
    assert facts["tweeter"].effective_true_peak_dbfs == pytest.approx(-33.2, abs=0.1)
    assert facts["tweeter"].peak_within_cap
    # The woofer's own cap is untouched (low-frequency role): still -8.
    assert facts["woofer"].cap_dbfs == pytest.approx(-8.0)


def test_channel_manifest_peak_mismatch_refuses():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    pcm = render_program_pcm(prog)
    # Inflate the woofer channel's peak far above its declared -6 dBFS, but keep
    # it quiet enough (effective still <= cap) to isolate the manifest mismatch.
    pcm[prog.segment("sweep_w").start_sample, 0] = 0.9
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv, pcm=pcm,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.MANIFEST_PEAK_MISMATCH in adm.refusals


# --- courtesy-tone prelude (issue #1677) --------------------------------------
#
# test_readmit_courtesy_prelude_wav_is_admitted (below) already pins the
# false-positive-refusal fix -- a prelude-bearing program is allowed -- through
# the production door; these two cover paths it does not.


def test_courtesy_prelude_still_catches_energy_outside_both_stimulus_and_tone():
    """The fix narrows the mask to the tone's own window -- it must not
    accidentally silence the out-of-segment check altogether. Energy leaked
    into the courtesy_gap silence (AFTER the tone, before the rest of the
    program) still refuses."""
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv, courtesy_prelude=True)
    pcm = render_program_pcm(prog)
    gap = prog.segment("courtesy_gap")
    pcm[gap.start_sample:gap.start_sample + 2000, 0] = 0.1
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv, pcm=pcm,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.OUT_OF_SEGMENT_ENERGY in adm.refusals


def test_courtesy_prelude_tampered_louder_than_cap_is_refused():
    """Defense in depth: the whole-file per-channel peak check reads the
    ACTUAL rendered bytes regardless of segment kind, so an (impossible via
    the real composer, but defensively tested) over-loud tone is still
    caught the same way a tampered stimulus would be. Tampers the TWEETER
    channel specifically -- its -65 dB cap is far tighter than the -20 dB
    session volume alone would already enforce on the woofer channel (whose
    0 dB cap a full-scale sample can't exceed once the session volume folds
    in), so this is the channel that actually exercises CHANNEL_PEAK_OVER_CAP."""
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv, courtesy_prelude=True)
    pcm = render_program_pcm(prog)
    tone1 = prog.segment("courtesy_tone_ch1")
    # Inflate the tweeter tone's own rendered samples to full scale -- far
    # above its admitted -65 dB cap even after the session-volume fold.
    pcm[tone1.start_sample:tone1.start_sample + tone1.n_samples, 1] = 0.99
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv, pcm=pcm,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in adm.refusals
    assert ProgramAdmissionRefusal.MANIFEST_PEAK_MISMATCH in adm.refusals


def test_readmit_courtesy_prelude_wav_is_admitted(tmp_path):
    """Play-time re-admission (the ACTUAL seam ``play_program`` uses) also
    admits a prelude-bearing program cleanly from a fresh WAV byte readback."""
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv, courtesy_prelude=True)
    wav = tmp_path / "prog_prelude.wav"
    write_program_wav(wav, prog)
    adm = readmit_program_from_wav(
        prog, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert adm.allowed, adm.refusals


def test_unmapped_role_refuses():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets={"woofer": targets["woofer"]},  # tweeter missing
        session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.TARGET_NOT_MAPPED in adm.refusals


def test_verify_program_not_admitted_here():
    topology, profile, targets = _profile_and_targets()
    prog = build_verify_program(1600.0)
    with pytest.raises(ProgramAdmissionError):
        _admit(
            prog, topology=topology, safety_profile=profile,
            role_targets=targets, session_volume_db=-65.0,
        )


# --- play-time re-admission from the rendered WAV bytes ----------------------


def test_readmit_clean_wav_is_admitted(tmp_path):
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    wav = tmp_path / "prog.wav"
    write_program_wav(wav, prog)
    adm = readmit_program_from_wav(
        prog, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert adm.allowed


def test_readmit_tampered_wav_is_refused(tmp_path):
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    wav = tmp_path / "prog.wav"
    write_program_wav(wav, prog)
    rate, data = wavfile.read(str(wav))
    # Tamper: leak full-scale energy into the leading guard silence (ch0).
    data = data.copy()
    data[0:2000, 0] = 20000
    wavfile.write(str(wav), rate, data)
    adm = readmit_program_from_wav(
        prog, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.OUT_OF_SEGMENT_ENERGY in adm.refusals


def test_readmit_wrong_shape_wav_is_refused(tmp_path):
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    wav = tmp_path / "mono.wav"
    # A 1-channel WAV where the program expects 2 channels.
    wavfile.write(str(wav), prog.sample_rate_hz, np.zeros(prog.total_samples, dtype=np.int16))
    adm = readmit_program_from_wav(
        prog, wav, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.RENDER_SHAPE_MISMATCH in adm.refusals


def test_solved_gain_at_a_deep_driver_cap_is_admitted_in_the_effective_frame():
    """A solved MEASURE gain NUMERICALLY above a driver's cap is admissible.

    The 2026-08-23 jts3 b0 walk refused with
    ``program_segment_outside_limits`` and was triaged as "the woofer's solved
    ``-6.0`` sits 2.0 dB over its declared ``max_effective_peak_dbfs`` of
    ``-8.0``, so the level solver must learn about the cap". Those two numbers
    are not comparable: a solved gain is a per-segment DIGITAL gain, and the
    cap bounds the EFFECTIVE peak, which ``_requested_segment_plan`` builds as
    ``segment.gain_db + session_volume_db``. At that session's ``-12.5`` dB
    volume the woofer segment lands at ``-18.5`` dBFS effective — 10.5 dB
    UNDER its cap — and the program is ADMITTED.

    Pinned so the comparison cannot be "fixed" into existence later: clamping a
    solved gain directly to ``max_effective_peak_dbfs`` would drive MEASURE
    quieter by exactly the session volume, which is the SNR collapse the level
    solve exists to prevent.
    """
    from jasper.active_speaker.crossover_v2.programs import back_off_gain

    topology, profile, targets = _profile_and_targets(woofer_peak=-8.0)
    session_volume_db = -12.5
    # The solve tonight produced these; the composer clamps each against that
    # role's cap in the effective frame, exactly as
    # `SessionExcitation.measure_program` does.
    solved = {"woofer": -6.0, "tweeter": -24.656}
    caps = {"woofer": -8.0, "tweeter": -65.0}
    gains = {
        role: back_off_gain(solved[role], session_volume_db, caps[role])
        for role in solved
    }
    # The woofer's cap does NOT bind here: its ceiling in the digital frame is
    # -8.0 + 12.5 - 0.01, well above the solved -6.0, so the solve rides
    # through untouched. The tweeter's -65 cap DOES bind, and the composer —
    # not the solver — is what lowers it.
    assert gains["woofer"] == pytest.approx(-6.0)
    assert gains["tweeter"] < solved["tweeter"]

    prog = _measure_program(session_volume_db, gains=gains)
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=session_volume_db,
    )
    assert adm.allowed
    assert adm.refusals == ()
    by_id = {s.segment_id: s for s in adm.segments}
    assert by_id["sweep_w"].effective_peak_dbfs == pytest.approx(-18.5)
    facts = {c.role: c for c in adm.channels}
    assert facts["woofer"].cap_dbfs == pytest.approx(-8.0)
    assert facts["woofer"].peak_within_cap


def test_refused_program_log_names_the_refusing_segment(caplog):
    """The refusal log names the failing comparison, both sides of it.

    Every field asserted here is computed by ``_evaluate_program`` regardless;
    before this it was discarded at the log line, leaving only the aggregate
    ``program_segment_outside_limits`` — which cannot distinguish a level
    breach from a band escape from a duration overrun, and which
    ``_map_safety_plan_error`` also returns as its catch-all for a plan that
    raised for a third reason. The band and the duration each carry their own
    limit inline (``band=…/permitted=…``, ``dur=…/max=…``) so the line says
    which comparison failed rather than leaving it to be re-derived; the
    effective peak's limit is the per-role cap in ``role_caps_dbfs``.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="jasper.active_speaker.program_admission")
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    # A tweeter segment driven past its own -65 cap in the EFFECTIVE frame:
    # -40.0 + sv (-20.0) = -60.0 dBFS, 5 dB over.
    prog = _measure_program(sv, gains={"woofer": -6.0, "tweeter": -40.0})
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in adm.refusals

    line = next(
        record.getMessage()
        for record in caplog.records
        if "event=active_speaker.program_admission" in record.getMessage()
        and "result=refused" in record.getMessage()
    )
    assert "segments_refused=" in line
    assert "sweep_t:tweeter" in line
    assert "eff=-60.000" in line
    # Each request value beside the limit it was judged against. The permitted
    # band is the tweeter's resolved excitation band; the duration limit is
    # `min(declared max_sweep_duration_s (6), driver_sweep_duration_s (4.0))`.
    assert "band=1600.0-10000.0/permitted=1500.0-20000.0" in line
    assert "dur=2.9997/max=4.0000" in line
    assert "active_excitation_request_outside_limits" in line
    # The cap it was judged against, and the term whose omission made the
    # 2026-08-23 triage compare a digital gain with an effective-peak ceiling.
    assert "tweeter=-65.000" in line
    assert f"session_volume_db={sv:.3f}" in line
    # The field is what was REFUSED: the woofer's segments passed, so naming
    # them here would be noise a triage has to filter back out by hand.
    assert "sweep_w" not in line


def test_declared_sweep_duration_equal_to_the_composed_length_refuses_every_measure():
    """A ``max_sweep_duration_s`` equal to the composer's nominal sweep refuses.

    The 2026-08-23 jts3 b0 walk's refusal, reproduced — and it is a
    level-independent, structural one. ``build_measure_program`` asks for
    ``DEFAULT_WOOFER_SWEEP_S`` (4.0 s), and the synchronized sweep rounds that
    request to the nearest phase-closing length, which for many bands is
    LONGER. Admission then compares the realized length against
    ``min(declared max_sweep_duration_s, driver_sweep_duration_s(role))``, so a
    declaration whose limit IS 4.0 refuses by a few milliseconds — every time,
    forever, at any level and any session volume — while that same segment's
    effective peak sits well inside its cap. On jts3 the woofer's 150-4000 Hz
    band realized 4.0058 s against a declared 4.0.
    """
    topology, profile, targets = _profile_and_targets(max_sweep_duration_s=4)
    sv = session_measurement_volume_db(profile, targets.values())
    # A woofer band whose phase-closing round lands ABOVE the 4 s request. The
    # rounding is a property of the band ratio, so which side of the limit a
    # given band falls on is incidental — the module default (500-1600) happens
    # to round DOWN to 3.9989 s and would pass. On jts3 the woofer's real
    # 150-4000 Hz band rounds UP to 4.0058 s.
    prog = _measure_program(sv, roles=_roles(woofer_band=(500.0, 2000.0)))
    adm = _admit(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in adm.refusals
    refused = {s.segment_id for s in adm.segments if s.refusals}
    # The woofer's 4.0 s sweep overshoots; the tweeter's 3.0 s one does not.
    assert "sweep_w" in refused
    assert "sweep_t" not in refused
    woofer = next(s for s in adm.segments if s.segment_id == "sweep_w")
    # Not a level breach: the refused segment is inside its cap.
    facts = {c.role: c for c in adm.channels}
    assert woofer.effective_peak_dbfs < facts["woofer"].cap_dbfs
    assert facts["woofer"].peak_within_cap
    assert prog.segment("sweep_w").n_samples / prog.sample_rate_hz > 4.0


@pytest.mark.parametrize(
    "change, refusal",
    [
        ("none", None),
        ("too_loud", ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP),
        ("too_long", ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS),
        ("unprotected_low_band", ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS),
        ("missing_target", ProgramAdmissionRefusal.TARGET_NOT_MAPPED),
        ("missing_hp", ProgramAdmissionRefusal.GRAPH_NOT_PROVEN),
        ("boost_without_headroom", ProgramAdmissionRefusal.GRAPH_NOT_PROVEN),
        ("wav_length", ProgramAdmissionRefusal.RENDER_SHAPE_MISMATCH),
        ("outside_schedule", ProgramAdmissionRefusal.OUT_OF_SEGMENT_ENERGY),
    ],
)
def test_summed_admission_proves_the_whole_graph_and_actual_audio(tmp_path, change, refusal):
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
    from jasper.active_speaker.profile import ActiveSpeakerPreset
    from tests.test_active_speaker_audition import ACTIVE_PCM, _applied_profile

    topology, profile, targets = _profile_and_targets(
        woofer_floor=500 if change == "unprotected_low_band" else 100,
        max_sweep_duration_s=4,
    )
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    graph_yaml = compile_tuning_graph(MeasurementGraphProfile(
        preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        applied_profile=applied,
    ), scope="speaker_tune")
    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs={"woofer": 0.0, "tweeter": -65.0},
        session_volume_db=-20.0, fc_hz=2000,
        sweep_duration_limits_s={} if change == "too_long" else {"woofer": 4, "tweeter": 4},
    ).verify_program()
    if change == "missing_hp":
        graph_yaml = graph_yaml.replace(
            "type: LinkwitzRileyHighpass\n      freq: 1600.0000",
            "type: LinkwitzRileyHighpass\n      freq: 100.0000",
        )
    elif change == "boost_without_headroom":
        graph_yaml = graph_yaml.replace("gain: 2.0000", "gain: 12.0000")
    wav = tmp_path / "summed.wav"
    write_program_wav(wav, program)
    if change in ("too_loud", "wav_length", "outside_schedule"):
        rate, pcm = wavfile.read(wav)
        if change == "too_loud":
            pcm = (pcm.astype(np.int32) * 2).astype(np.int16)
        elif change == "wav_length":
            pcm = pcm[:-1]
        else:
            pcm[-24000:] = 1000
        wavfile.write(wav, rate, pcm)
    admission = readmit_summed_program_from_wav(
        program, wav, graph_yaml=graph_yaml, topology=topology,
        safety_profile=profile,
        role_targets={"woofer": targets["woofer"]} if change == "missing_target" else targets,
        session_volume_db=-20.0,
    )
    if refusal is None:
        assert admission.allowed, admission.to_dict()
        assert {segment.role for segment in admission.segments} == {"woofer", "tweeter"}
        assert min(segment.band[0] for segment in admission.segments if segment.role == "tweeter") == 150
        assert admission.channels[0].cap_dbfs == -65
    else:
        assert not admission.allowed
        assert refusal in admission.refusals


@pytest.mark.parametrize("scope", ["base", "speaker_tune", "candidate", "candidate_branches"])
@pytest.mark.parametrize("damage", [
    None, "missing", "wrong_output", "low_corner", "shallow_slope", "gain", "after_limiter",
    "upper_band", "lowpass_slope", "lowpass_missing", "lowpass_wrong_output",
])
def test_summed_scopes_preserve_declared_protection_before_admission(tmp_path, scope, damage):
    topology, safety, targets = _profile_and_targets(
        woofer_floor=40, woofer_highpass=40, max_sweep_duration_s=4,
        woofer_measurement_floor=60,
        woofer_upper=2000 if damage == "upper_band" else 4000,
    )
    assert not any(req["kind"] == "lowpass" for target in safety["targets"]
                   for req in target["required_protection_filters"])
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    preset = replace(preset, crossover_regions=(replace(
        preset.crossover_regions[0], fc_hz=2500, order=2 if damage == "lowpass_slope" else 4,
    ),))
    applied["recomposition_snapshot"]["preset"] = preset.to_dict()
    saved = deepcopy(applied)
    measurement = MeasurementGraphProfile(
        preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        protection_sections_by_role=confirmed_protection_sections(safety, targets),
        applied_profile=applied,
    )
    text = compile_tuning_graph(
        measurement, scope="base" if scope == "candidate" else scope,
        candidate=_trial_candidate(measurement) if scope in {"candidate", "candidate_branches"} else None,
    )
    graph = yaml.safe_load(text)
    highpasses = {
        name: value for name, value in graph["filters"].items()
        if value.get("parameters", {}).get("type") == "LinkwitzRileyHighpass"
    }
    assert {40, 2500} <= {value["parameters"]["freq"] for value in highpasses.values()}
    assert not any(name.startswith("bass_ext_") for name in graph["filters"])
    assert graph["devices"]["volume_limit"] == 0.0
    assert applied == saved
    name = next(name for name, value in highpasses.items() if value["parameters"]["freq"] == 40)
    step = next(step for step in graph["pipeline"] if name in step.get("names", []))
    assert step["channels"] == [0]
    limiter = next(value for value in step["names"] if graph["filters"][value]["type"] == "Limiter")
    assert step["names"].index(name) < step["names"].index(limiter)
    original_graph = deepcopy(graph)
    if damage in {"missing", "wrong_output", "after_limiter"}:
        step["names"].remove(name)
        if damage == "wrong_output":
            next(step for step in graph["pipeline"] if step.get("channels") == [1])["names"].append(name)
        elif damage == "after_limiter":
            step["names"].append(name)
    elif damage == "low_corner":
        graph["filters"][name]["parameters"]["freq"] = 30
    elif damage == "shallow_slope":
        graph["filters"][name]["parameters"]["order"] = 2
    elif damage == "gain":
        graph["filters"][name] = {"type": "Gain", "parameters": {"gain": 6}}
    elif damage in {"lowpass_missing", "lowpass_wrong_output"}:
        lowpass = next(name for name, value in graph["filters"].items()
                       if value.get("parameters", {}).get("type") == "LinkwitzRileyLowpass")
        step["names"].remove(lowpass)
        if damage == "lowpass_wrong_output":
            next(step for step in graph["pipeline"] if step.get("channels") == [1])["names"].append(lowpass)
    for original, changed in zip(original_graph["pipeline"], graph["pipeline"]):
        if "names" in original:
            text = text.replace(
                f"names: [{', '.join(original['names'])}]",
                f"names: [{', '.join(changed['names'])}]",
            )
    if damage in {"low_corner", "shallow_slope", "gain"}:
        replacement = (
            emit_gain_filter(name, 6) if damage == "gain" else emit_linkwitz_riley(
                name, highpass=True, freq_hz=30 if damage == "low_corner" else 40,
                order=2 if damage == "shallow_slope" else 4,
            )
        )
        text = text.replace(
            "\n".join(emit_linkwitz_riley(name, highpass=True, freq_hz=40, order=4)),
            "\n".join(replacement),
        )
    session_graph = _session_graph(
        FakeCam(entry_path=_entry(tmp_path)), tmp_path=tmp_path,
        emit_scoped=lambda *_: text,
    )
    session_graph.select_scope(scope, "trial" if scope in {"candidate", "candidate_branches"} else "")
    asyncio.run(session_graph.install())
    submitted = session_graph.installed_graph_yaml()
    submitted_graph = yaml.safe_load(submitted)
    submitted_graph.pop("description")
    assert submitted_graph == yaml.safe_load(text)
    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs={"woofer": 0, "tweeter": -65},
        session_volume_db=-20, fc_hz=2500,
        sweep_duration_limits_s={"woofer": 4, "tweeter": 4},
        summed_sweep_band_hz=(20, 20000),
    ).verify_program()
    if scope == "candidate_branches":
        from jasper.audio_measurement.branch_program import build_branch_program
        program = build_branch_program(program, measurement.role_channels)
    wav = tmp_path / "summed.wav"
    write_program_wav(wav, program)
    admission = readmit_summed_program_from_wav(
        program, wav, graph_yaml=submitted, topology=topology,
        safety_profile=safety, role_targets=targets, session_volume_db=-20,
    )
    assert admission.allowed is (damage is None), admission.to_dict()
    if damage in {"upper_band", "lowpass_slope"}:
        assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in admission.refusals
    elif damage:
        assert ProgramAdmissionRefusal.GRAPH_NOT_PROVEN in admission.refusals


@pytest.mark.parametrize("damage", ["swapped_inputs", "loud_second_channel"])
def test_branch_admission_checks_both_input_routes_and_actual_channels(tmp_path, damage):
    from jasper.audio_measurement.branch_program import build_branch_program
    topology, safety, targets = _profile_and_targets(woofer_floor=40, woofer_highpass=40, max_sweep_duration_s=4)
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    profile = MeasurementGraphProfile(preset, topology,
        {"woofer": 1, "tweeter": 0} if damage == "swapped_inputs" else {"woofer": 0, "tweeter": 1},
        ACTIVE_PCM, protection_sections_by_role=confirmed_protection_sections(safety, targets), applied_profile=applied)
    graph = compile_tuning_graph(profile, scope="candidate_branches", candidate=_trial_candidate(profile))
    program = build_branch_program(SessionExcitation(
        roles=tuple(_roles()), caps_dbfs={"woofer": 0, "tweeter": -65}, session_volume_db=-20,
        fc_hz=1600, sweep_duration_limits_s={"woofer": 4, "tweeter": 4},
    ).cloud_program(), {"woofer": 0, "tweeter": 1})
    wav = tmp_path / "branches.wav"
    write_program_wav(wav, program)
    if damage == "loud_second_channel":
        rate, pcm = wavfile.read(wav)
        pcm[:, 1] *= 2
        wavfile.write(wav, rate, pcm)
    result = readmit_summed_program_from_wav(program, wav, graph_yaml=graph, topology=topology,
        safety_profile=safety, role_targets=targets, session_volume_db=-20)
    assert not result.allowed
    assert (ProgramAdmissionRefusal.GRAPH_NOT_PROVEN if damage == "swapped_inputs"
            else ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP) in result.refusals


@pytest.mark.parametrize("change", [None, "missing_descriptor", "processor", "woofer_peak"])
def test_dynamic_bass_admission_proves_graph_and_reserves_its_maximum_lift(tmp_path, change):
    topology, safety, targets = _profile_and_targets(
        woofer_floor=40, woofer_highpass=40, woofer_peak=-24,
        tweeter_peak=0, max_sweep_duration_s=4,
    )
    applied = _applied_profile(topology)
    applied["recomposition_snapshot"]["bass_extension"] = BASS_EXTENSION
    measurement = MeasurementGraphProfile(
        ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"]),
        topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        protection_sections_by_role=confirmed_protection_sections(safety, targets),
        applied_profile=applied,
    )
    text = compile_tuning_graph(measurement, scope="applied")
    if change == "processor":
        text = text.replace("makeup_gain: 0.0", "makeup_gain: 3.0")
    program = build_verify_program(
        1600, gain_db=-6 if change == "woofer_peak" else -12,
        downstream_gain_db=-20, sweep_s=2,
    )
    wav = tmp_path / "bass.wav"
    write_program_wav(wav, program)
    admission = readmit_summed_program_from_wav(
        program, wav, graph_yaml=text, topology=topology,
        safety_profile=safety, role_targets=targets, session_volume_db=-20,
        bass_extension=None if change == "missing_descriptor" else BASS_EXTENSION,
    )
    if change in {"missing_descriptor", "processor"}:
        assert admission.refusals == (ProgramAdmissionRefusal.GRAPH_NOT_PROVEN,)
    elif change == "woofer_peak":
        assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in admission.refusals
        assert all(s.execution_allowed for s in admission.segments if s.role == "tweeter")
        assert any(not s.execution_allowed for s in admission.segments if s.role == "woofer")
    else:
        assert admission.allowed, admission.to_dict()
        assert admission.channels[0].cap_dbfs == pytest.approx(
            -24 - dynamic_bass_gain_reserve_db(DynamicBassDescriptor(**BASS_EXTENSION))
        )


@pytest.mark.parametrize("low_hz", [10, 20, 40, 60])
@pytest.mark.parametrize("highpass", [None, 40])
def test_summed_room_band_uses_hard_floor_without_adding_highpass(tmp_path, low_hz, highpass):
    topology, safety, targets = _profile_and_targets(
        woofer_floor=40, woofer_measurement_floor=60, max_sweep_duration_s=4,
        woofer_highpass=highpass,
    )
    applied = _applied_profile(topology)
    measurement = MeasurementGraphProfile(
        ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"]),
        topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        protection_sections_by_role=confirmed_protection_sections(safety, targets),
        applied_profile=applied,
    )
    graph = compile_tuning_graph(measurement, scope="room_tune")
    program = SessionExcitation(
        roles=tuple(_roles()), caps_dbfs={"woofer": 0, "tweeter": -65},
        session_volume_db=-20, fc_hz=1600,
        sweep_duration_limits_s={"woofer": 4, "tweeter": 4},
        summed_sweep_band_hz=(low_hz, 20_000),
    ).verify_program()
    wav = tmp_path / "room.wav"
    write_program_wav(wav, program)
    admission = readmit_summed_program_from_wav(
        program, wav, graph_yaml=graph, topology=topology,
        safety_profile=safety, role_targets=targets, session_volume_db=-20,
    )
    assert admission.allowed is (low_hz >= 20 and (low_hz >= 40 or highpass is not None)), admission.to_dict()
