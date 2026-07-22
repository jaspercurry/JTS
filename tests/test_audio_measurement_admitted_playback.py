# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import logging
import os
import threading
import wave
from dataclasses import replace
from pathlib import Path

import pytest

import jasper.audio_measurement.admitted_playback as guarded
import jasper.audio_measurement.playback as low_level_playback
from jasper.audio_measurement.admitted_playback import (
    AdmittedPlaybackFailureCode,
    CurrentPlaybackAdmissionInputs,
    GeneratedExcitationWav,
    GeneratedStimulusError,
    GeneratedStimulusFailureCode,
    PlaybackAdmissionCancelled,
    PlaybackAdmissionFailed,
    PlaybackAdmissionRefused,
    bind_generated_excitation_wav,
    play_admitted_wav,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRefusalReason,
    ExcitationRequest,
    FrequencyBand,
    ProtectionEvidence,
    admit_excitation,
)
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    create_admission_authority,
    persist_generation_admission,
    read_playback_admission,
)
from jasper.audio_measurement.playback import (
    MAX_VERIFIED_WAV_BYTES,
    PlaybackError,
    PlaybackFailureCode,
    PlaybackResult,
    WavSourceError,
    WavSourceFailureCode,
)

TARGET = "1" * 64
PROFILE = "2" * 64
REQUIREMENT = "3" * 64
PLAN = "4" * 64
GENERATION_PROOF = "5" * 64
PLAYBACK_PROOF = "6" * 64
BUNDLE_KIND = "jts_active_speaker_commissioning_authority"
BUNDLE_ID = "authority-session-1"
ADMISSION_ID = "combined-main-repeat-1"
SAMPLE_RATE = 8_000


def _limits(**changes: object) -> ExcitationLimits:
    values: dict[str, object] = {
        "permitted_band": FrequencyBand(500, 10_000),
        "maximum_effective_peak_dbfs": -12,
        "maximum_duration_s": 8,
        "maximum_repeat_count": 3,
        "target_fingerprint": TARGET,
        "safety_profile_fingerprint": PROFILE,
        "protection_requirement_fingerprint": REQUIREMENT,
        "excitation_plan_fingerprint": PLAN,
    }
    values.update(changes)
    return ExcitationLimits(**values)  # type: ignore[arg-type]


def _evidence(limits: ExcitationLimits, proof: str) -> ProtectionEvidence:
    return ProtectionEvidence(
        target_fingerprint=limits.target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        protection_requirement_fingerprint=(limits.protection_requirement_fingerprint),
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
        evidence_fingerprint=proof,
        current=True,
    )


def _generation(
    tmp_path: Path,
    *,
    bundle_id: str = BUNDLE_ID,
    admission_id: str = ADMISSION_ID,
):
    authority = create_admission_authority(
        tmp_path / bundle_id,
        bundle_kind=BUNDLE_KIND,
        bundle_id=bundle_id,
    )
    limits = _limits()
    request = ExcitationRequest(
        band=FrequencyBand(1_000, 8_000),
        effective_peak_dbfs=-18,
        duration_s=4,
        repeat_count=3,
        target_fingerprint=limits.target_fingerprint,
        safety_profile_fingerprint=limits.safety_profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=limits.excitation_plan_fingerprint,
    )
    admission = admit_excitation(
        request,
        limits,
        protection_evidence=_evidence(limits, GENERATION_PROOF),
    )
    generation = persist_generation_admission(
        authority,
        admission_id=admission_id,
        admission=admission,
    )
    return authority, limits, generation


def _artifact(path: Path, *, relative_path: str | None = None) -> ArtifactIdentity:
    raw = path.read_bytes()
    return ArtifactIdentity(
        bundle_kind="jts_active_speaker_commissioning",
        bundle_id="evidence-session-1",
        relative_path=relative_path or path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def _wav(
    tmp_path: Path,
    generation,
    *,
    duration_s: float = 4,
    filename: str = "stimulus.wav",
):
    path = tmp_path / filename
    frame_count = round(duration_s * SAMPLE_RATE)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"\0\0" * frame_count)
    stimulus = bind_generated_excitation_wav(generation, _artifact(path))
    return path, stimulus


def _issuer(limits: ExcitationLimits):
    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        return CurrentPlaybackAdmissionInputs(
            limits=limits,
            protection_evidence=_evidence(limits, PLAYBACK_PROOF),
        )

    return issue_current_inputs


def _assert_boundary_log(
    caplog: pytest.LogCaptureFixture,
    *,
    result: str,
    failure_code: AdmittedPlaybackFailureCode | None = None,
    bundle_id: str = BUNDLE_ID,
    admission_id: str = ADMISSION_ID,
) -> None:
    expected = [
        record.message
        for record in caplog.records
        if "event=audio_measurement.admitted_playback" in record.message
        and f"result={result}" in record.message
        and f"bundle_id={bundle_id}" in record.message
        and f"admission_id={admission_id}" in record.message
    ]
    assert len(expected) == 1
    if failure_code is not None:
        assert any(f"failure_code={failure_code.value}" in row for row in expected)


@pytest.mark.asyncio
async def test_exact_stimulus_fresh_issuer_readmission_and_readback_precede_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    wav_path, stimulus = _wav(tmp_path, generation)
    events: list[str] = []
    original_read = guarded.read_generation_admission
    original_readback = guarded.read_playback_admission

    def observed_generation(*args, **kwargs):
        events.append("verify_generation")
        return original_read(*args, **kwargs)

    def observed_playback_readback(*args, **kwargs):
        events.append("verify_playback_artifact")
        return original_readback(*args, **kwargs)

    issuer_calls = 0

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        events.append("fresh_issuer")
        return await _issuer(limits)()

    async def fake_play_wav(source, *, alsa_device, timeout_s):
        events.append("play")
        assert source.path == wav_path
        assert source.artifact == stimulus.artifact
        assert timeout_s == 12
        return PlaybackResult(source.path, alsa_device, 0)

    monkeypatch.setattr(guarded, "read_generation_admission", observed_generation)
    monkeypatch.setattr(guarded, "read_playback_admission", observed_playback_readback)
    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    caplog.set_level(logging.INFO, logger=guarded.__name__)

    result = await play_admitted_wav(
        tmp_path,
        stimulus=stimulus,
        authority=authority,
        generation=generation,
        issue_current_inputs=issue_current_inputs,
        alsa_device="measurement_lane",
        timeout_s=12,
    )

    assert issuer_calls == 1
    assert events == [
        "verify_generation",
        "fresh_issuer",
        "verify_playback_artifact",
        "play",
    ]
    assert result.playback.returncode == 0
    assert result.admission.admission.protection_evidence == _evidence(
        limits,
        PLAYBACK_PROOF,
    )
    assert (
        read_playback_admission(authority, generation, result.admission.artifact)
        == result.admission
    )
    _assert_boundary_log(caplog, result="completed")


@pytest.mark.asyncio
async def test_fresh_refusal_never_calls_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, _limits_at_generation, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    narrowed = _limits(maximum_duration_s=3)
    played = False
    original_close = low_level_playback._VerifiedWavSource.close

    async def fake_play_wav(*args, **kwargs):
        nonlocal played
        played = True
        raise AssertionError("refused playback reached audio")

    def close_then_fail(source):
        original_close(source)
        raise OSError("refused snapshot close failed")

    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    monkeypatch.setattr(
        low_level_playback._VerifiedWavSource,
        "close",
        close_then_fail,
    )
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(PlaybackAdmissionRefused) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(narrowed),
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert caught.value.decision.refusal_reasons == (
        ExcitationRefusalReason.AUTHORITY_IDENTITY_MISMATCH,
        ExcitationRefusalReason.DURATION_ABOVE_LIMIT,
    )
    assert played is False
    assert not (authority.directory / "admission/v1/playback").exists()
    assert any(
        "suppressed verified WAV cleanup failure" in note
        for note in caught.value.__notes__
    )
    _assert_boundary_log(caplog, result="refused")


@pytest.mark.asyncio
async def test_stale_playback_protection_proof_never_calls_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    played = False

    async def issue_stale_inputs() -> CurrentPlaybackAdmissionInputs:
        return CurrentPlaybackAdmissionInputs(
            limits=limits,
            protection_evidence=replace(
                _evidence(limits, PLAYBACK_PROOF),
                current=False,
            ),
        )

    async def fake_play_wav(*args, **kwargs):
        nonlocal played
        played = True

    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    with pytest.raises(PlaybackAdmissionRefused) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_stale_inputs,
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert caught.value.decision.refusal_reasons == (
        ExcitationRefusalReason.PROTECTION_EVIDENCE_STALE,
    )
    assert played is False


@pytest.mark.asyncio
async def test_changed_content_fails_before_issuer_or_persistence(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    path, stimulus = _wav(tmp_path, generation)
    path.write_bytes(b"different content")
    issuer_calls = 0

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        return await _issuer(limits)()

    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(WavSourceError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert caught.value.code is WavSourceFailureCode.CONTENT_MISMATCH
    assert issuer_calls == 0
    assert not (authority.directory / "admission/v1/playback").exists()
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.STIMULUS_INVALID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alsa_device", "timeout_s"),
    [("", 12), ("measurement_lane", 0)],
)
async def test_invalid_playback_control_fails_before_issuer_or_persistence(
    tmp_path: Path,
    alsa_device: str,
    timeout_s: float,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    issuer_calls = 0

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        return await _issuer(limits)()

    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(PlaybackError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,
            alsa_device=alsa_device,
            timeout_s=timeout_s,
        )
    assert caught.value.code is PlaybackFailureCode.INVALID_REQUEST
    assert issuer_calls == 0
    assert not (authority.directory / "admission/v1/playback").exists()
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.INVALID_REQUEST,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["generation", "plan"])
async def test_wrong_generation_or_plan_binding_fails_before_issuer(
    tmp_path: Path,
    failure: str,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    if failure == "generation":
        other_authority, _other_limits, other = _generation(
            tmp_path,
            bundle_id="authority-session-2",
            admission_id="combined-main-repeat-2",
        )
        authority = other_authority
        generation = other
        expected = GeneratedStimulusFailureCode.GENERATION_MISMATCH
    else:
        stimulus = replace(stimulus, excitation_plan_fingerprint="f" * 64)
        expected = GeneratedStimulusFailureCode.PLAN_MISMATCH
    issuer_calls = 0

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        return await _issuer(limits)()

    with pytest.raises(GeneratedStimulusError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert caught.value.code is expected
    assert issuer_calls == 0


@pytest.mark.asyncio
async def test_malformed_or_wrong_duration_wav_fails_before_issuer(
    tmp_path: Path,
):
    authority, limits, generation = _generation(tmp_path)
    malformed = tmp_path / "malformed.wav"
    malformed.write_bytes(b"not-wave")
    malformed_stimulus = bind_generated_excitation_wav(
        generation,
        _artifact(malformed),
    )
    with pytest.raises(WavSourceError) as malformed_error:
        await play_admitted_wav(
            tmp_path,
            stimulus=malformed_stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert malformed_error.value.code is WavSourceFailureCode.INVALID_WAV

    _path, short_stimulus = _wav(
        tmp_path,
        generation,
        duration_s=3,
        filename="short.wav",
    )
    with pytest.raises(GeneratedStimulusError) as duration_error:
        await play_admitted_wav(
            tmp_path,
            stimulus=short_stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert duration_error.value.code is GeneratedStimulusFailureCode.DURATION_MISMATCH


@pytest.mark.asyncio
async def test_declared_oversized_wav_fails_before_open_or_issuer(tmp_path: Path):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    oversized = bind_generated_excitation_wav(
        generation,
        replace(stimulus.artifact, byte_size=MAX_VERIFIED_WAV_BYTES + 1),
    )
    issuer_calls = 0

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        return await _issuer(limits)()

    with pytest.raises(WavSourceError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=oversized,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert caught.value.code is WavSourceFailureCode.RESOURCE_LIMIT
    assert issuer_calls == 0


@pytest.mark.asyncio
async def test_symlink_artifact_is_refused_without_following_it(tmp_path: Path):
    authority, limits, generation = _generation(tmp_path)
    target, _target_stimulus = _wav(tmp_path, generation)
    link = tmp_path / "linked.wav"
    os.symlink(target.name, link)
    stimulus = bind_generated_excitation_wav(
        generation,
        _artifact(target, relative_path=link.name),
    )

    with pytest.raises(WavSourceError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert caught.value.code is WavSourceFailureCode.UNSAFE_PATH


@pytest.mark.asyncio
async def test_failed_generation_verification_prevents_issuer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    issuer_calls = 0

    def fail_generation(*args, **kwargs):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "generation changed",
        )

    async def issue_current_inputs() -> CurrentPlaybackAdmissionInputs:
        nonlocal issuer_calls
        issuer_calls += 1
        return await _issuer(limits)()

    monkeypatch.setattr(guarded, "read_generation_admission", fail_generation)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(AdmissionArtifactError):
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert issuer_calls == 0
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.GENERATION_INVALID,
    )


@pytest.mark.asyncio
async def test_final_artifact_readback_failure_prevents_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    played = False

    def fail_readback(*args, **kwargs):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "final readback changed",
        )

    async def fake_play_wav(*args, **kwargs):
        nonlocal played
        played = True

    monkeypatch.setattr(guarded, "read_playback_admission", fail_readback)
    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    with pytest.raises(AdmissionArtifactError):
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert played is False


@pytest.mark.asyncio
async def test_persistence_failure_never_calls_playback_and_is_correlated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    played = False

    def fail_persistence(*args, **kwargs):
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            "publish result unknown",
        )

    async def fake_play_wav(*args, **kwargs):
        nonlocal played
        played = True

    monkeypatch.setattr(guarded, "_readmit_and_verify", fail_persistence)
    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(AdmissionArtifactError) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )
    assert played is False
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.READMISSION_FAILED,
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_persistence_and_requires_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    played = False
    original_readmit = guarded._readmit_and_verify

    def blocking_readmit(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        try:
            return original_readmit(*args, **kwargs)
        finally:
            finished.set()

    async def fake_play_wav(*args, **kwargs):
        nonlocal played
        played = True

    monkeypatch.setattr(guarded, "_readmit_and_verify", blocking_readmit)
    monkeypatch.setattr(guarded, "play_verified_wav", fake_play_wav)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    task = asyncio.create_task(
        play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
    release.set()
    with pytest.raises(PlaybackAdmissionCancelled) as caught:
        await task

    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is False
    assert caught.value.admission == read_playback_admission(
        authority,
        generation,
        caught.value.admission.artifact,
    )
    assert finished.is_set()
    assert played is False
    _assert_boundary_log(
        caplog,
        result="cancelled",
        failure_code=AdmittedPlaybackFailureCode.CANCELLED_BEFORE_AUDIO,
    )


@pytest.mark.asyncio
async def test_cancellation_during_final_wav_recheck_is_typed_before_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    started = threading.Event()
    release = threading.Event()
    spawned = False
    original_verify = low_level_playback._verify_open_wav_source

    def blocking_verify(source):
        started.set()
        release.wait(timeout=5)
        return original_verify(source)

    async def fail_if_spawned(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("cancelled final recheck spawned audio")

    monkeypatch.setattr(
        low_level_playback,
        "_verify_open_wav_source",
        blocking_verify,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_if_spawned)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    task = asyncio.create_task(
        play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    release.set()
    with pytest.raises(PlaybackAdmissionCancelled) as caught:
        await task

    assert caught.value.admission == read_playback_admission(
        authority,
        generation,
        caught.value.admission.artifact,
    )
    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is False
    assert spawned is False
    _assert_boundary_log(
        caplog,
        result="cancelled",
        failure_code=AdmittedPlaybackFailureCode.CANCELLED_BEFORE_AUDIO,
    )


@pytest.mark.asyncio
async def test_persistence_failure_wins_over_simultaneous_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    started = threading.Event()
    release = threading.Event()

    def blocking_failure(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN,
            "publish result unknown",
        )

    monkeypatch.setattr(guarded, "_readmit_and_verify", blocking_failure)
    task = asyncio.create_task(
        play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(AdmissionArtifactError) as caught:
        await task
    assert (
        caught.value.code is AdmissionArtifactErrorCode.ARTIFACT_PERSIST_OUTCOME_UNKNOWN
    )


@pytest.mark.asyncio
async def test_malformed_fresh_issuer_result_fails_closed_and_is_correlated(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    authority, _limits_at_generation, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)

    async def issue_current_inputs():
        return object()

    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(ValueError, match="CurrentPlaybackAdmissionInputs"):
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=issue_current_inputs,  # type: ignore[arg-type]
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.FRESH_INPUTS_FAILED,
    )


@pytest.mark.asyncio
async def test_snapshot_close_failure_has_one_typed_failed_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    path, stimulus = _wav(tmp_path, generation)
    original_close = low_level_playback._VerifiedWavSource.close

    def close_then_fail(source):
        original_close(source)
        raise OSError("snapshot close failed")

    async def complete_playback(*args, **kwargs):
        return PlaybackResult(path, "measurement_lane", 0)

    monkeypatch.setattr(
        low_level_playback._VerifiedWavSource,
        "close",
        close_then_fail,
    )
    monkeypatch.setattr(guarded, "play_verified_wav", complete_playback)
    caplog.set_level(logging.INFO, logger=guarded.__name__)

    with pytest.raises(PlaybackAdmissionFailed) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is True
    assert caught.value.admission == read_playback_admission(
        authority,
        generation,
        caught.value.admission.artifact,
    )
    assert isinstance(caught.value.failure, WavSourceError)
    assert caught.value.failure.code is WavSourceFailureCode.CLEANUP_FAILED
    assert isinstance(caught.value.failure.__cause__, OSError)
    assert caught.value.__cause__ is caught.value.failure
    terminal = [
        record.message
        for record in caplog.records
        if "event=audio_measurement.admitted_playback" in record.message
        and f"bundle_id={authority.bundle_id}" in record.message
        and f"admission_id={generation.admission_id}" in record.message
    ]
    assert len(terminal) == 1
    assert "result=failed" in terminal[0]
    assert "failure_code=source_cleanup_failed" in terminal[0]
    assert f"artifact_sha256={caught.value.admission.artifact.sha256}" in terminal[0]
    assert "audio_may_have_started=true" in terminal[0]


@pytest.mark.asyncio
async def test_playback_cancellation_preserves_admission_when_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    original_close = low_level_playback._VerifiedWavSource.close
    entered = asyncio.Event()

    def close_then_fail(source):
        original_close(source)
        raise OSError("cancelled snapshot close failed")

    async def block_playback(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        low_level_playback._VerifiedWavSource,
        "close",
        close_then_fail,
    )
    monkeypatch.setattr(guarded, "play_verified_wav", block_playback)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    task = asyncio.create_task(
        play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(PlaybackAdmissionCancelled) as caught:
        await task

    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is True
    assert caught.value.admission == read_playback_admission(
        authority,
        generation,
        caught.value.admission.artifact,
    )
    assert any(
        "suppressed verified WAV cleanup failure" in note
        for note in caught.value.__notes__
    )
    _assert_boundary_log(
        caplog,
        result="cancelled",
        failure_code=AdmittedPlaybackFailureCode.CANCELLED_DURING_AUDIO,
    )


@pytest.mark.asyncio
async def test_playback_failure_and_cancellation_have_correlated_terminal_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    authority, limits, generation = _generation(tmp_path)
    path, stimulus = _wav(tmp_path, generation)

    async def fail_playback(*args, **kwargs):
        raise PlaybackError(
            "could not start aplay",
            code=PlaybackFailureCode.START_FAILED,
            wav_path=path,
            alsa_device="measurement_lane",
        )

    monkeypatch.setattr(guarded, "play_verified_wav", fail_playback)
    caplog.set_level(logging.INFO, logger=guarded.__name__)
    with pytest.raises(PlaybackAdmissionFailed) as failed:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    assert failed.value.requires_new_generation is True
    assert failed.value.audio_may_have_started is False
    assert failed.value.admission == read_playback_admission(
        authority,
        generation,
        failed.value.admission.artifact,
    )
    assert isinstance(failed.value.failure, PlaybackError)
    assert failed.value.failure.code is PlaybackFailureCode.START_FAILED
    assert failed.value.__cause__ is failed.value.failure
    _assert_boundary_log(
        caplog,
        result="failed",
        failure_code=AdmittedPlaybackFailureCode.PLAYBACK_FAILED,
    )

    authority2, limits2, generation2 = _generation(
        tmp_path,
        bundle_id="authority-session-2",
        admission_id="combined-main-repeat-2",
    )
    _path2, stimulus2 = _wav(tmp_path, generation2, filename="stimulus-2.wav")
    entered = asyncio.Event()

    async def block_playback(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(guarded, "play_verified_wav", block_playback)
    task = asyncio.create_task(
        play_admitted_wav(
            tmp_path,
            stimulus=stimulus2,
            authority=authority2,
            generation=generation2,
            issue_current_inputs=_issuer(limits2),
            alsa_device="measurement_lane",
            timeout_s=12,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(PlaybackAdmissionCancelled) as caught:
        await task
    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is True
    assert caught.value.admission == read_playback_admission(
        authority2,
        generation2,
        caught.value.admission.artifact,
    )
    _assert_boundary_log(
        caplog,
        result="cancelled",
        failure_code=AdmittedPlaybackFailureCode.CANCELLED_DURING_AUDIO,
        bundle_id=authority2.bundle_id,
        admission_id=generation2.admission_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "audio_may_have_started"),
    [
        (
            PlaybackError(
                "aplay exited after partial playback",
                code=PlaybackFailureCode.PROCESS_FAILED,
                wav_path=Path("stimulus.wav"),
                alsa_device="measurement_lane",
                returncode=1,
            ),
            True,
        ),
        (
            PlaybackError(
                "aplay timed out after partial playback",
                code=PlaybackFailureCode.TIMEOUT,
                wav_path=Path("stimulus.wav"),
                alsa_device="measurement_lane",
            ),
            True,
        ),
        (
            PlaybackError(
                "aplay wait failed after spawn",
                code=PlaybackFailureCode.WAIT_FAILED,
                wav_path=Path("stimulus.wav"),
                alsa_device="measurement_lane",
            ),
            True,
        ),
        (
            WavSourceError(
                "immutable source changed during final verification",
                code=WavSourceFailureCode.CONTENT_MISMATCH,
                wav_path=Path("stimulus.wav"),
            ),
            False,
        ),
    ],
)
async def test_post_persistence_failure_preserves_admission_and_audio_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    audio_may_have_started: bool,
):
    authority, limits, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)

    async def fail_playback(*args, **kwargs):
        raise failure

    monkeypatch.setattr(guarded, "play_verified_wav", fail_playback)
    caplog.set_level(logging.INFO, logger=guarded.__name__)

    with pytest.raises(PlaybackAdmissionFailed) as caught:
        await play_admitted_wav(
            tmp_path,
            stimulus=stimulus,
            authority=authority,
            generation=generation,
            issue_current_inputs=_issuer(limits),
            alsa_device="measurement_lane",
            timeout_s=12,
        )

    assert caught.value.requires_new_generation is True
    assert caught.value.audio_may_have_started is audio_may_have_started
    assert caught.value.failure is failure
    assert caught.value.__cause__ is failure
    assert caught.value.admission == read_playback_admission(
        authority,
        generation,
        caught.value.admission.artifact,
    )
    terminal = [
        record.message
        for record in caplog.records
        if "event=audio_measurement.admitted_playback" in record.message
        and "result=failed" in record.message
    ]
    assert len(terminal) == 1
    assert f"artifact_sha256={caught.value.admission.artifact.sha256}" in terminal[0]
    assert (
        f"audio_may_have_started={str(audio_may_have_started).lower()}" in terminal[0]
    )


def test_generated_stimulus_schema_round_trip_and_tamper_refusal(tmp_path: Path):
    _authority, _limits_at_generation, generation = _generation(tmp_path)
    _path, stimulus = _wav(tmp_path, generation)
    assert GeneratedExcitationWav.from_mapping(stimulus.to_dict()) == stimulus
    tampered = stimulus.to_dict()
    tampered["excitation_plan_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="fingerprint"):
        GeneratedExcitationWav.from_mapping(tampered)


def test_guarded_module_has_no_direct_powerful_host_reference():
    source = Path(guarded.__file__).read_text()
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = (
        "jasper.active_speaker",
        "jasper.camilla",
        "jasper.correction",
        "jasper.dsp_apply",
        "jasper.web",
    )
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported
        for prefix in forbidden
    )
    parameters = inspect.signature(play_admitted_wav).parameters
    assert "issue_current_inputs" in parameters
    assert not {"camilla", "dsp", "host", "writer_lock"} & set(parameters)
