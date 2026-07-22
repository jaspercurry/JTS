# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guarded production composition for admitted measurement WAV playback.

The feature host owns the real DSP writer/playback lock and supplies one async
issuer that performs its fresh live graph/protection readback while that guard
is held. This module binds an exact feature-owned WAV artifact to one generation
admission, invokes the issuer exactly once, immediately performs independent
playback re-admission plus durable persistence/readback, and emits an immutable
snapshot of the verified no-link source only after the playback-role artifact
verifies.

Once that playback-role artifact exists, every cancellation or failure carries
the verified artifact and an honest possible-audio outcome. The one-shot path
is consumed even when the player never starts, so a caller can never retry an
ambiguous attempt under the same generation admission.

This boundary does not acquire a feature lock, interpret a graph, choose an
ALSA lane, construct safety policy, or turn historical evidence into authority.
The host must keep its guard held across this entire call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import (
    ExcitationAdmission,
    ExcitationLimits,
    ProtectionEvidence,
)
from jasper.audio_measurement.excitation_artifacts import (
    AdmissionArtifactError,
    AdmissionArtifactErrorCode,
    AdmissionAuthority,
    GenerationAdmissionArtifact,
    PlaybackAdmissionArtifact,
    PlaybackAdmissionResult,
    read_generation_admission,
    read_playback_admission,
    readmit_and_persist_playback_admission,
)
from jasper.audio_measurement.playback import (
    PlaybackError,
    PlaybackFailureCode,
    PlaybackResult,
    WavPlaybackCancelledBeforeSpawn,
    WavSourceError,
    WavSourceFailureCode,
    play_verified_wav,
    validate_wav_playback_control,
    verified_wav_source,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

GENERATED_EXCITATION_WAV_SCHEMA_VERSION = 1
_GENERATED_EXCITATION_WAV_KIND = "jts_generated_excitation_wav"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class GeneratedExcitationWav:
    """Exact persisted WAV identity bound to one generation and plan.

    The feature-owned deterministic generator issues this value and persists
    ``to_dict()`` with its own manifest/state. Shared verifies the exact artifact
    bytes and generation binding; it does not infer an opaque feature plan from
    PCM samples.
    """

    generation_artifact_fingerprint: str
    excitation_plan_fingerprint: str
    artifact: ArtifactIdentity
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        generation = _sha256(
            self.generation_artifact_fingerprint,
            field_name="generation_artifact_fingerprint",
        )
        plan = _sha256(
            self.excitation_plan_fingerprint,
            field_name="excitation_plan_fingerprint",
        )
        if not isinstance(self.artifact, ArtifactIdentity):
            raise ValueError("artifact must be an ArtifactIdentity")
        object.__setattr__(self, "generation_artifact_fingerprint", generation)
        object.__setattr__(self, "excitation_plan_fingerprint", plan)
        object.__setattr__(self, "fingerprint", json_fingerprint(self._core()))

    def _core(self) -> dict[str, object]:
        return {
            "schema_version": GENERATED_EXCITATION_WAV_SCHEMA_VERSION,
            "kind": _GENERATED_EXCITATION_WAV_KIND,
            "generation_artifact_fingerprint": (self.generation_artifact_fingerprint),
            "excitation_plan_fingerprint": self.excitation_plan_fingerprint,
            "artifact": self.artifact.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._core(), "fingerprint": self.fingerprint}

    @classmethod
    def from_mapping(cls, raw: object) -> GeneratedExcitationWav:
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "kind",
            "generation_artifact_fingerprint",
            "excitation_plan_fingerprint",
            "artifact",
            "fingerprint",
        }:
            raise ValueError("generated excitation WAV fields are invalid")
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != GENERATED_EXCITATION_WAV_SCHEMA_VERSION
        ):
            raise ValueError("generated excitation WAV schema is unsupported")
        if raw["kind"] != _GENERATED_EXCITATION_WAV_KIND:
            raise ValueError("generated excitation WAV kind is unsupported")
        result = cls(
            generation_artifact_fingerprint=raw["generation_artifact_fingerprint"],
            excitation_plan_fingerprint=raw["excitation_plan_fingerprint"],
            artifact=ArtifactIdentity.from_mapping(raw["artifact"]),
        )
        if raw["fingerprint"] != result.fingerprint:
            raise ValueError("generated excitation WAV fingerprint is invalid")
        return result


def bind_generated_excitation_wav(
    generation: GenerationAdmissionArtifact,
    artifact: ArtifactIdentity,
) -> GeneratedExcitationWav:
    """Bind a feature-generated WAV artifact to one exact generation decision."""

    if not isinstance(generation, GenerationAdmissionArtifact):
        raise ValueError("generation must be a GenerationAdmissionArtifact")
    plan = generation.admission.request.excitation_plan_fingerprint
    if plan is None:  # An allowed generation cannot reach this branch.
        raise ValueError("generation admission has no excitation plan identity")
    return GeneratedExcitationWav(
        generation_artifact_fingerprint=generation.artifact.fingerprint,
        excitation_plan_fingerprint=plan,
        artifact=artifact,
    )


@dataclass(frozen=True, slots=True)
class CurrentPlaybackAdmissionInputs:
    """Feature-issued policy and proof from one fresh guarded readback."""

    limits: ExcitationLimits
    protection_evidence: ProtectionEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.limits, ExcitationLimits):
            raise ValueError("limits must be ExcitationLimits")
        if not isinstance(self.protection_evidence, ProtectionEvidence):
            raise ValueError("protection_evidence must be ProtectionEvidence")


CurrentPlaybackAdmissionIssuer = Callable[[], Awaitable[CurrentPlaybackAdmissionInputs]]


@dataclass(frozen=True, slots=True)
class AdmittedPlaybackResult:
    """A fully reaped emission and its independently verified authority."""

    playback: PlaybackResult
    admission: PlaybackAdmissionArtifact


class PlaybackAdmissionRefused(RuntimeError):
    """The fresh playback-side decision refused audio emission."""

    def __init__(self, decision: ExcitationAdmission) -> None:
        if not isinstance(decision, ExcitationAdmission) or decision.allowed:
            raise ValueError("decision must be a refused ExcitationAdmission")
        reasons = ",".join(reason.value for reason in decision.refusal_reasons)
        super().__init__(f"playback re-admission refused: {reasons}")
        self.decision = decision


class PlaybackAdmissionCancelled(asyncio.CancelledError):
    """Cancellation after persistence consumed this one-shot admission.

    The attached artifact is verified. ``audio_may_have_started`` distinguishes
    cancellation before the player was spawned from cancellation while its
    outcome was active or uncertain. In both cases the feature must record the
    consumed result and create a new generation admission/id for any retry.
    """

    requires_new_generation = True

    def __init__(
        self,
        admission: PlaybackAdmissionArtifact,
        *,
        audio_may_have_started: bool,
    ) -> None:
        if type(audio_may_have_started) is not bool:
            raise ValueError("audio_may_have_started must be a bool")
        state = (
            "after audio may have started" if audio_may_have_started else "before audio"
        )
        super().__init__(f"playback admission persisted; cancellation occurred {state}")
        self.admission = admission
        self.audio_may_have_started = audio_may_have_started


class PlaybackAdmissionFailed(RuntimeError):
    """Failure after persistence consumed this one-shot admission.

    ``failure`` retains the typed low-level cause. ``audio_may_have_started``
    is false only when the boundary can prove the player was not spawned; true
    includes partial, complete, and operationally uncertain emission outcomes.
    Every retry requires a new generation admission/id.
    """

    requires_new_generation = True

    def __init__(
        self,
        admission: PlaybackAdmissionArtifact,
        *,
        audio_may_have_started: bool,
        failure: Exception,
    ) -> None:
        if not isinstance(admission, PlaybackAdmissionArtifact):
            raise ValueError("admission must be a PlaybackAdmissionArtifact")
        if type(audio_may_have_started) is not bool:
            raise ValueError("audio_may_have_started must be a bool")
        if not isinstance(failure, Exception):
            raise ValueError("failure must be an Exception")
        state = (
            "after audio may have started"
            if audio_may_have_started
            else "before audio"
        )
        super().__init__(f"playback admission persisted; playback failed {state}")
        self.admission = admission
        self.audio_may_have_started = audio_may_have_started
        self.failure = failure


class GeneratedStimulusFailureCode(str, Enum):
    """Closed binding failures before audio can be emitted."""

    GENERATION_MISMATCH = "generation_mismatch"
    PLAN_MISMATCH = "plan_mismatch"
    DURATION_MISMATCH = "duration_mismatch"


class GeneratedStimulusError(RuntimeError):
    def __init__(self, message: str, *, code: GeneratedStimulusFailureCode) -> None:
        super().__init__(message)
        self.code = code


class AdmittedPlaybackFailureCode(str, Enum):
    """Stable guarded-boundary terminal failure classes."""

    STIMULUS_INVALID = "stimulus_invalid"
    INVALID_REQUEST = "invalid_request"
    GENERATION_INVALID = "generation_invalid"
    FRESH_INPUTS_FAILED = "fresh_inputs_failed"
    READMISSION_FAILED = "readmission_failed"
    PLAYBACK_FAILED = "playback_failed"
    SOURCE_CLEANUP_FAILED = "source_cleanup_failed"
    CANCELLED_BEFORE_AUDIO = "cancelled_before_audio"
    CANCELLED_DURING_AUDIO = "cancelled_during_audio"


def _validate_stimulus_binding(
    stimulus: GeneratedExcitationWav,
    generation: GenerationAdmissionArtifact,
) -> None:
    if not isinstance(stimulus, GeneratedExcitationWav):
        raise ValueError("stimulus must be a GeneratedExcitationWav")
    if stimulus.generation_artifact_fingerprint != generation.artifact.fingerprint:
        raise GeneratedStimulusError(
            "generated WAV is tied to another generation admission",
            code=GeneratedStimulusFailureCode.GENERATION_MISMATCH,
        )
    if (
        stimulus.excitation_plan_fingerprint
        != generation.admission.request.excitation_plan_fingerprint
    ):
        raise GeneratedStimulusError(
            "generated WAV is tied to another excitation plan",
            code=GeneratedStimulusFailureCode.PLAN_MISMATCH,
        )


def _readmit_and_verify(
    authority: AdmissionAuthority,
    generation: GenerationAdmissionArtifact,
    current: CurrentPlaybackAdmissionInputs,
) -> PlaybackAdmissionResult:
    result = readmit_and_persist_playback_admission(
        authority,
        generation,
        current_limits=current.limits,
        current_protection_evidence=current.protection_evidence,
    )
    if result.artifact is None:
        return result
    verified = read_playback_admission(
        authority,
        generation,
        result.artifact.artifact,
    )
    if verified != result.artifact:
        raise AdmissionArtifactError(
            AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
            "playback admission changed during final readback",
        )
    return PlaybackAdmissionResult(decision=result.decision, artifact=verified)


async def _settle_persistence(
    task: asyncio.Task[PlaybackAdmissionResult],
) -> PlaybackAdmissionResult:
    """Drain a publication task before honoring repeated caller cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    # Persistence failure is more safety-relevant than simultaneous caller
    # cancellation, so never hide an unknown publication outcome.
    result = task.result()
    if cancellation is not None:
        if result.artifact is not None:
            raise PlaybackAdmissionCancelled(
                result.artifact,
                audio_may_have_started=False,
            ) from cancellation
        raise cancellation
    return result


def _failure_code(
    error: Exception,
    *,
    phase: str,
) -> AdmittedPlaybackFailureCode:
    if (
        isinstance(error, PlaybackError)
        and error.code is PlaybackFailureCode.INVALID_REQUEST
    ):
        return AdmittedPlaybackFailureCode.INVALID_REQUEST
    if (
        isinstance(error, WavSourceError)
        and error.code is WavSourceFailureCode.CLEANUP_FAILED
    ):
        return AdmittedPlaybackFailureCode.SOURCE_CLEANUP_FAILED
    if isinstance(error, (GeneratedStimulusError, WavSourceError)):
        return AdmittedPlaybackFailureCode.STIMULUS_INVALID
    if phase == "stimulus_validation":
        return AdmittedPlaybackFailureCode.STIMULUS_INVALID
    if phase == "generation_verification":
        return AdmittedPlaybackFailureCode.GENERATION_INVALID
    if phase == "fresh_inputs":
        return AdmittedPlaybackFailureCode.FRESH_INPUTS_FAILED
    if phase == "readmission":
        return AdmittedPlaybackFailureCode.READMISSION_FAILED
    return AdmittedPlaybackFailureCode.PLAYBACK_FAILED


def _failure_detail(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    return code.value if isinstance(code, Enum) else None


def _audio_may_have_started_after_failure(
    error: Exception,
    *,
    playback_completed: bool,
) -> bool:
    if playback_completed:
        return True
    if isinstance(error, WavSourceError):
        # The only post-persistence WavSourceError before a completed playback
        # comes from the immutable snapshot's final pre-spawn verification.
        return False
    if isinstance(error, PlaybackError):
        return error.code not in {
            PlaybackFailureCode.INVALID_REQUEST,
            PlaybackFailureCode.MISSING_FILE,
            PlaybackFailureCode.START_FAILED,
        }
    # An unclassified error inside the playback phase cannot prove that spawn
    # did not happen. Fail conservatively rather than licensing a silent retry.
    return True


async def play_admitted_wav(
    stimulus_bundle_dir: str | Path,
    *,
    stimulus: GeneratedExcitationWav,
    authority: AdmissionAuthority,
    generation: GenerationAdmissionArtifact,
    issue_current_inputs: CurrentPlaybackAdmissionIssuer,
    alsa_device: str,
    timeout_s: float,
) -> AdmittedPlaybackResult:
    """Verify exact WAV bytes, re-admit from fresh proof, persist, then play.

    The caller must hold its existing DSP writer/playback guard for the whole
    await. ``issue_current_inputs`` must perform the live readback and compose
    the exact current limits/protection proof; passing precomputed values is
    intentionally unsupported.
    """

    if not isinstance(authority, AdmissionAuthority):
        raise ValueError("authority must be an AdmissionAuthority")
    if not isinstance(generation, GenerationAdmissionArtifact):
        raise ValueError("generation must be a GenerationAdmissionArtifact")
    if not callable(issue_current_inputs):
        raise ValueError("issue_current_inputs must be callable")

    phase = "stimulus_validation"
    persisted_admission: PlaybackAdmissionArtifact | None = None
    playback_completed = False
    terminal_logged = False
    try:
        _validate_stimulus_binding(stimulus, generation)
        validate_wav_playback_control(
            Path(stimulus_bundle_dir).joinpath(
                *stimulus.artifact.relative_path.split("/")
            ),
            alsa_device=alsa_device,
            timeout_s=timeout_s,
        )
        async with verified_wav_source(
            stimulus_bundle_dir,
            stimulus.artifact,
        ) as source:
            frame_tolerance = 1.0 / source.sample_rate_hz
            if (
                abs(source.duration_s - generation.admission.request.duration_s)
                > frame_tolerance
            ):
                raise GeneratedStimulusError(
                    "generated WAV duration does not match the admitted plan",
                    code=GeneratedStimulusFailureCode.DURATION_MISMATCH,
                )

            phase = "generation_verification"
            verified_generation = await asyncio.to_thread(
                read_generation_admission,
                authority,
                generation.artifact,
            )
            if verified_generation != generation:
                raise AdmissionArtifactError(
                    AdmissionArtifactErrorCode.ARTIFACT_INTEGRITY_MISMATCH,
                    "generation admission changed before the fresh playback readback",
                )

            phase = "fresh_inputs"
            current = await issue_current_inputs()
            if not isinstance(current, CurrentPlaybackAdmissionInputs):
                raise ValueError(
                    "issue_current_inputs must return CurrentPlaybackAdmissionInputs"
                )

            phase = "readmission"
            persistence = asyncio.create_task(
                asyncio.to_thread(_readmit_and_verify, authority, generation, current)
            )
            admitted = await _settle_persistence(persistence)

            if admitted.artifact is None:
                log_event(
                    logger,
                    "audio_measurement.admitted_playback",
                    result="refused",
                    bundle_id=authority.bundle_id,
                    admission_id=generation.admission_id,
                    refusal_codes=",".join(
                        reason.value for reason in admitted.decision.refusal_reasons
                    ),
                    level=logging.WARNING,
                )
                terminal_logged = True
                raise PlaybackAdmissionRefused(admitted.decision)

            persisted_admission = admitted.artifact
            phase = "playback"
            try:
                playback = await play_verified_wav(
                    source,
                    alsa_device=alsa_device,
                    timeout_s=timeout_s,
                )
            except WavPlaybackCancelledBeforeSpawn as exc:
                raise PlaybackAdmissionCancelled(
                    admitted.artifact,
                    audio_may_have_started=False,
                ) from exc
            except asyncio.CancelledError as exc:
                raise PlaybackAdmissionCancelled(
                    admitted.artifact,
                    audio_may_have_started=True,
                ) from exc
            playback_completed = True
            completed_admission = admitted.artifact
        log_event(
            logger,
            "audio_measurement.admitted_playback",
            result="completed",
            bundle_id=authority.bundle_id,
            admission_id=generation.admission_id,
            artifact_sha256=completed_admission.artifact.sha256,
            stimulus_sha256=stimulus.artifact.sha256,
        )
        terminal_logged = True
        return AdmittedPlaybackResult(
            playback=playback,
            admission=completed_admission,
        )
    except PlaybackAdmissionRefused:
        raise
    except PlaybackAdmissionCancelled as exc:
        cancellation_code = (
            AdmittedPlaybackFailureCode.CANCELLED_DURING_AUDIO
            if exc.audio_may_have_started
            else AdmittedPlaybackFailureCode.CANCELLED_BEFORE_AUDIO
        )
        log_event(
            logger,
            "audio_measurement.admitted_playback",
            result="cancelled",
            failure_code=cancellation_code.value,
            bundle_id=authority.bundle_id,
            admission_id=generation.admission_id,
            artifact_sha256=exc.admission.artifact.sha256,
            audio_may_have_started=exc.audio_may_have_started,
        )
        terminal_logged = True
        raise
    except asyncio.CancelledError:
        failure = (
            AdmittedPlaybackFailureCode.CANCELLED_DURING_AUDIO
            if phase == "playback"
            else AdmittedPlaybackFailureCode.CANCELLED_BEFORE_AUDIO
        )
        log_event(
            logger,
            "audio_measurement.admitted_playback",
            result="cancelled",
            failure_code=failure.value,
            bundle_id=authority.bundle_id,
            admission_id=generation.admission_id,
        )
        terminal_logged = True
        raise
    finally:
        error = sys.exc_info()[1]
        if isinstance(error, Exception) and not terminal_logged:
            if persisted_admission is not None:
                admission_failure = PlaybackAdmissionFailed(
                    persisted_admission,
                    audio_may_have_started=_audio_may_have_started_after_failure(
                        error,
                        playback_completed=playback_completed,
                    ),
                    failure=error,
                )
                log_event(
                    logger,
                    "audio_measurement.admitted_playback",
                    result="failed",
                    failure_code=_failure_code(error, phase=phase).value,
                    failure_detail=_failure_detail(error),
                    error_type=type(error).__name__,
                    bundle_id=authority.bundle_id,
                    admission_id=generation.admission_id,
                    artifact_sha256=persisted_admission.artifact.sha256,
                    audio_may_have_started=(
                        admission_failure.audio_may_have_started
                    ),
                    level=logging.WARNING,
                )
                raise admission_failure from error
            log_event(
                logger,
                "audio_measurement.admitted_playback",
                result="failed",
                failure_code=_failure_code(error, phase=phase).value,
                failure_detail=_failure_detail(error),
                error_type=type(error).__name__,
                bundle_id=authority.bundle_id,
                admission_id=generation.admission_id,
                level=logging.WARNING,
            )
