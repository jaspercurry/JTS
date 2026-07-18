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


def _profile_and_targets():
    topology = mono_output_topology()
    common = {
        "hard_excitation_band_hz": [500, 20_000],
        "measurement_band_hz": [500, 10_000],
        "crossover_search_band_hz": [1500, 2500],
        "level_duration_limits": {
            "max_effective_peak_dbfs": -65,
            "max_sweep_duration_s": 6,
            "max_repeat_count": 3,
            "minimum_cooldown_s": 0,
        },
    }
    settings = {
        "drivers": [
            {
                **common,
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
    topology = mono_output_topology()
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
    gains = gains or {"woofer": -6.0, "tweeter": -6.0}
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
    # A too-loud session volume pushes every effective peak above the -65 cap.
    prog = _measure_program(-65.0)
    adm = admit_excitation_program(
        prog, topology=topology, safety_profile=profile,
        role_targets=targets, session_volume_db=-40.0,
    )
    assert not adm.allowed
    assert ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS in adm.refusals
    assert ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP in adm.refusals


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
