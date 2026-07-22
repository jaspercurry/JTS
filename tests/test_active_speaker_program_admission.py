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

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker.driver_safety import build_driver_safety_profile
from jasper.active_speaker.measurement import active_driver_targets
from jasper.active_speaker.program_admission import (
    ProgramAdmissionError,
    ProgramAdmissionRefusal,
    admit_excitation_program,
    readmit_program_from_wav,
)
from jasper.active_speaker.session_volume_plan import session_measurement_volume_db
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    RoleBand,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
    write_program_wav,
)
from tests.active_speaker_fixtures import mono_output_topology


def _profile_and_targets(*, woofer_peak: float = 0.0, tweeter_peak: float = -65.0):
    """Asymmetric caps by default (woofer 0.0, tweeter -65): the realistic
    2-way shape whose ~65 dB spread is exactly what the (fixed) session-volume
    derivation must handle — symmetric fixtures masked the min/max inversion."""
    topology = mono_output_topology()

    def _limits(peak):
        return {
            "max_effective_peak_dbfs": peak,
            "max_sweep_duration_s": 6,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 0,
        }

    common = {
        "hard_excitation_band_hz": [500, 20_000],
        "measurement_band_hz": [500, 10_000],
        "crossover_search_band_hz": [1500, 2500],
    }
    settings = {
        "drivers": [
            {
                **common,
                "level_duration_limits": _limits(woofer_peak),
                "target_id": "mono:woofer",
                "role": "woofer",
                "model": "W",
                "required_protection_filters": [
                    {"kind": "lowpass", "cutoff_hz": 3000, "minimum_slope_db_per_octave": 24}
                ],
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
        confirm=True,
        confirmed_at="2026-07-13T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topology)}
    return topology, profile, targets


def _roles(woofer_band=(500.0, 1600.0), tweeter_band=(1600.0, 10_000.0)):
    return [
        RoleBand("woofer", 0, FrequencyBand(*woofer_band)),
        RoleBand("tweeter", 1, FrequencyBand(*tweeter_band)),
    ]


def _measure_program(session_volume_db, roles=None, gains=None):
    roles = roles or _roles()
    # The default gain plan mirrors the corrected session-volume rule: the
    # woofer (highest cap) runs at the -6 dB digital guard; the tweeter
    # attenuates DOWN so gain + session_volume clears its -65 dB cap.
    gains = gains or {"woofer": -6.0, "tweeter": -46.0}
    return build_measure_program(gains, roles, downstream_gain_db=session_volume_db)


def test_clean_program_is_admitted():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    adm = admit_excitation_program(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
    )
    assert adm.allowed
    assert all(s.execution_allowed for s in adm.segments)
    assert len(adm.segments) == 3  # sweep_w, sweep_t, sweep_w_rep
    facts = {c.channel: c for c in adm.channels}
    assert facts[0].peak_within_cap and facts[1].peak_within_cap
    assert facts[0].quiet_out_of_segment and facts[1].quiet_out_of_segment
    assert facts[0].peak_matches_manifest and facts[1].peak_matches_manifest


def test_band_escape_refuses_segment():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    # Woofer band dips below the 500 Hz permitted floor.
    prog = _measure_program(sv, roles=_roles(woofer_band=(150.0, 1600.0)))
    adm = admit_excitation_program(
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
    adm = admit_excitation_program(
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
    adm = admit_excitation_program(
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
    hand-fed number -- so this pins that the derived -35 ceiling
    (min(-8 - 25.2, -35) = -35, abs ceiling binds) actually drives what gets
    composed, then admits end-to-end with the same mapping.
    """
    from jasper.active_speaker.crossover_v2_flow import back_off_gain
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
    assert caps == {"woofer": -8.0, "tweeter": pytest.approx(-35.0)}
    sv = session_measurement_volume_db(
        profile, targets.values(), declared_sensitivities=declared
    )
    # max(caps) is still the woofer's -8 (its ceiling is untouched by the HF
    # derivation), so the session volume itself is unaffected by the change.
    assert sv == -20.0
    # The production composition clamp (the same call _compose_measure_program
    # makes): nominal reference gain backed off against each resolved cap. The
    # tweeter's composed level is cap-DRIVEN: -35 - sv - 0.01 = -15.01 dB
    # digital -> -35.01 dBFS effective. Under the old -65 cap this program
    # would have been refused as CHANNEL_PEAK_OVER_CAP.
    gains = {
        role: back_off_gain(BASE_STIMULUS_PEAK_DBFS, sv, caps[role])
        for role in caps
    }
    prog = _measure_program(sv, gains=gains)
    adm = admit_excitation_program(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv,
        declared_sensitivities=declared,
    )
    assert adm.allowed
    by_id = {s.segment_id: s for s in adm.segments}
    assert by_id["sweep_t"].effective_peak_dbfs == pytest.approx(-35.01)
    assert by_id["sweep_t"].execution_allowed
    facts = {c.role: c for c in adm.channels}
    assert facts["tweeter"].cap_dbfs == pytest.approx(-35.0)
    assert facts["tweeter"].effective_true_peak_dbfs == pytest.approx(-35.0, abs=0.1)
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
    adm = admit_excitation_program(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv, pcm=pcm,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.MANIFEST_PEAK_MISMATCH in adm.refusals


def test_out_of_segment_energy_refuses():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    pcm = render_program_pcm(prog)
    # Leak a tone into the leading guard silence on the woofer channel.
    pcm[0:2000, 0] = 0.1
    adm = admit_excitation_program(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=sv, pcm=pcm,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.OUT_OF_SEGMENT_ENERGY in adm.refusals


def test_unmapped_role_refuses():
    topology, profile, targets = _profile_and_targets()
    sv = session_measurement_volume_db(profile, targets.values())
    prog = _measure_program(sv)
    adm = admit_excitation_program(
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
        admit_excitation_program(
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
