# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stored capture admission still verifies its exact artifacts."""

from pathlib import Path
import wave

import pytest

from jasper.active_speaker import bundles
from jasper.active_speaker.commissioning_admission import (
    ActiveCaptureAdmissionHandoff,
    ActiveCommissioningAdmissionError,
    validate_capture_admission_handoff,
)
from jasper.active_speaker.measurement import active_driver_targets
from jasper.audio_measurement.admitted_playback import bind_generated_excitation_wav
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    persist_generation_admission,
    readmit_and_persist_playback_admission,
)
from tests.active_speaker_fixtures import mono_output_topology
from tests.test_audio_measurement_excitation_artifacts import (
    _admission,
    _evidence,
    _identity,
    _limits,
)


def _driver_comparison_set(topology):
    from jasper.active_speaker.capture_geometry import comparison_set_fingerprint

    core = {
        "schema_version": 2,
        "comparison_set_id": "1" * 32,
        "created_at": "2026-07-11T12:00:00Z",
        "topology_id": topology.topology_id,
        "profile_context_id": "profile-1",
        "setup_sha256": "2" * 64,
        "device_sha256": "3" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            target["target_id"]: {
                "target_id": target["target_id"],
                "speaker_group_id": target["speaker_group_id"],
                "role": target["role"],
                "tone_frequency_hz": (
                    250.0 if target["role"] == "woofer" else 6250.0
                ),
                "tone_peak_dbfs": -12.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -4.0,
            }
            for target in active_driver_targets(topology)
        },
    }
    return {**core, "fingerprint": comparison_set_fingerprint(core)}


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("stimulus", ActiveCommissioningAdmissionError),
        ("playback", AdmissionArtifactError),
        ("handoff", ValueError),
    ],
)
def test_stored_handoff_reopens_exact_artifacts_and_rejects_changes(
    tmp_path, monkeypatch, tamper, error,
):
    monkeypatch.setenv(bundles.SESSIONS_DIR_ENV, str(tmp_path))
    topology = mono_output_topology()
    target = next(row for row in active_driver_targets(topology) if row["role"] == "woofer")
    comparison = _driver_comparison_set(topology)
    bundle = bundles.open_bundle(topology, calibration_id="", build_sha="test")
    assert bundle is not None
    comparison["bundle_session_id"] = bundle["session_id"]
    authority = bundles.open_bundle_admission_authority(
        bundle["bundle_dir"], expected_session_id=bundle["session_id"],
    )
    limits = _limits(target_fingerprint=target["target_fingerprint"])
    generation = persist_generation_admission(
        authority, admission_id="stored-driver-1", admission=_admission(limits=limits),
    )
    playback = readmit_and_persist_playback_admission(
        authority, generation,
        current_limits=limits,
        current_protection_evidence=_evidence(limits, "6" * 64),
    ).artifact
    assert playback is not None
    wav_path = authority.directory / "stimulus.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48_000)
        wav.writeframes(b"\0\0" * 48_000 * 4)
    stimulus = bind_generated_excitation_wav(
        generation,
        _identity(authority=authority, relative_path=wav_path.name, raw=wav_path.read_bytes()),
    )
    handoff = ActiveCaptureAdmissionHandoff(
        session_id=bundle["session_id"],
        comparison_set_id=comparison["comparison_set_id"],
        comparison_set_fingerprint=comparison["fingerprint"],
        admission_id=generation.admission_id,
        target_id=target["target_id"],
        target_fingerprint=target["target_fingerprint"],
        authority_fingerprint=authority.fingerprint,
        generation_artifact=generation.artifact,
        playback_artifact=playback.artifact,
        stimulus=stimulus,
        admission=playback.admission.to_dict(),
        graph_fingerprint="7" * 64,
        graph_evidence_fingerprint="6" * 64,
    ).to_dict()

    def reopen():
        return validate_capture_admission_handoff(
            handoff, topology=topology, comparison_set=comparison,
            speaker_group_id="mono", role="woofer",
        )

    assert reopen() == handoff
    if tamper == "handoff":
        handoff["fingerprint"] = "0" * 64
    else:
        path = wav_path if tamper == "stimulus" else Path(
            authority.directory, playback.artifact.relative_path,
        )
        path.write_bytes(b"changed")
    with pytest.raises(error):
        reopen()
