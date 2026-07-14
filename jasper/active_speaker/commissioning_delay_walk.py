# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Production host boundary for an Active crossover delay null walk.

Shared :mod:`jasper.audio_measurement.null_walk` owns bounded scheduling and
exact, cancellation-draining predecessor restoration.  This adapter supplies
the missing Active authority around it: one DSP-writer transaction, a fresh
context-bearing live read after every candidate load, exact graph-content
confirmation, and strict admitted null evidence.

The callbacks are deliberately narrow and contain all hardware I/O.  Unit
tests can therefore prove the transaction without opening CamillaDSP or an
audio device, while a production caller can provide the real graph loader,
live read-back, admission/capture path, and exact restore operation.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeAlias, cast

from jasper.audio_measurement.delay_graph import (
    DelayCandidateConfirmation,
    DelayGraphProofError,
    DelayGraphSnapshot,
    DelayLaneBinding,
    confirm_delay_candidate,
)
from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.excitation_admission import ExcitationAdmission
from jasper.audio_measurement.null_walk import (
    DspPredecessor,
    DspRestoreConfirmation,
    DelayCandidate,
    NullWalkError,
    NullWalkSpec,
    run_null_walk,
)
from jasper.dsp_apply import (
    DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S,
    DspWriterLockTimeout,
    dsp_writer_lock,
)
from jasper.log_event import log_event

from .alignment_walk import DRIVER_DELAY_WALK_SCOPE
from .commissioning_run import CommissioningAttemptHandle

DEFAULT_ACTIVE_DELAY_WALK_LOCK_TIMEOUT_S = DEFAULT_DSP_WRITER_LOCK_TIMEOUT_S
MAX_ACTIVE_DELAY_WALK_LOCK_TIMEOUT_S = 30.0
ACTIVE_DELAY_WALK_CAPTURES_PER_CANDIDATE = 5
_WRITER_SOURCE = "active_speaker_delay_walk"

ActiveDelayWalkFailureCode: TypeAlias = Literal[
    "argument_invalid",
    "attempt_stale",
    "candidate_apply_invalid",
    "capture_admission_refused",
    "capture_correlation_mismatch",
    "capture_duplicate",
    "capture_identity_invalid",
    "capture_invalid",
    "capture_quality_refused",
    "capture_snr_refused",
    "context_invalid",
    "live_readback_duplicate",
    "live_readback_invalid",
    "stale_context",
]

logger = logging.getLogger(__name__)


class ActiveDelayWalkError(NullWalkError):
    """Active host authority refused a delay-walk input or observation."""

    def __init__(self, code: ActiveDelayWalkFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _refuse(code: ActiveDelayWalkFailureCode, message: str) -> NoReturn:
    raise ActiveDelayWalkError(code, message)


def _trimmed(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _refuse("argument_invalid", f"{field_name} must be non-empty trimmed text")
    return value


def _positive_finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("argument_invalid", f"{field_name} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        _refuse("argument_invalid", f"{field_name} must be a positive number")
    return result


def _sha256(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _refuse(
            "context_invalid",
            f"{field_name} must be a lowercase SHA-256 fingerprint",
        )
    return value


@dataclass(frozen=True, slots=True)
class ActiveDelayWalkContext:
    """Immutable attempt, region, safety, threshold, and placement authority."""

    attempt: CommissioningAttemptHandle
    topology_id: str
    speaker_group_id: str
    region_id: str
    crossover_fc_hz: float
    safety_profile_fingerprint: str
    threshold_profile_fingerprint: str
    placement_fingerprint: str
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, CommissioningAttemptHandle):
            _refuse("context_invalid", "attempt must be CommissioningAttemptHandle")
        object.__setattr__(
            self,
            "topology_id",
            _trimmed(self.topology_id, field_name="topology_id"),
        )
        object.__setattr__(
            self,
            "speaker_group_id",
            _trimmed(self.speaker_group_id, field_name="speaker_group_id"),
        )
        object.__setattr__(
            self,
            "region_id",
            _trimmed(self.region_id, field_name="region_id"),
        )
        object.__setattr__(
            self,
            "crossover_fc_hz",
            _positive_finite(
                self.crossover_fc_hz,
                field_name="crossover_fc_hz",
            ),
        )
        for name in (
            "safety_profile_fingerprint",
            "threshold_profile_fingerprint",
            "placement_fingerprint",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), field_name=name),
            )
        try:
            fingerprint = json_fingerprint(
                self._core(),
                field_name="Active delay-walk context",
            )
        except EvidenceIdentityError as exc:
            raise ActiveDelayWalkError("context_invalid", str(exc)) from exc
        object.__setattr__(self, "fingerprint", fingerprint)

    def _core(self) -> dict[str, Any]:
        run = self.attempt.run
        return {
            "schema_version": 1,
            "kind": "jts_active_delay_walk_context",
            "session_id": run.session_id,
            "session_fingerprint": run.session_fingerprint,
            "run_id": run.run_id,
            "owner_id": run.owner_id,
            "owner_generation": run.owner_generation,
            "attempt_id": self.attempt.attempt_id,
            "attempt_number": self.attempt.attempt_number,
            "target_id": self.attempt.target_id,
            "target_fingerprint": self.attempt.target_fingerprint,
            "topology_id": self.topology_id,
            "speaker_group_id": self.speaker_group_id,
            "region_id": self.region_id,
            "crossover_fc_hz": self.crossover_fc_hz,
            "safety_profile_fingerprint": self.safety_profile_fingerprint,
            "threshold_profile_fingerprint": self.threshold_profile_fingerprint,
            "placement_fingerprint": self.placement_fingerprint,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the exact serialized context and deterministic fingerprint."""

        return {**self._core(), "fingerprint": self.fingerprint}


@dataclass(frozen=True, init=False, slots=True)
class ActiveDelayLiveGraph:
    """One callback-owned fresh read of the live Active CamillaDSP graph.

    ``readback_id`` is an operation identity, not a graph-content hash.  The
    adapter rejects reuse during a walk, including a content-identical replay.
    The topology and crossover fields bind every live read to the current host
    context independently of the pure Shared graph proof.
    """

    readback_id: str
    topology_id: str
    crossover_fc_hz: float
    _frozen: DspPredecessor = field(repr=False)

    def __init__(
        self,
        *,
        readback_id: str,
        topology_id: str,
        crossover_fc_hz: float,
        graph: Mapping[str, Any],
    ) -> None:
        read_id = _trimmed(readback_id, field_name="readback_id")
        topology = _trimmed(topology_id, field_name="topology_id")
        fc = _positive_finite(crossover_fc_hz, field_name="crossover_fc_hz")
        try:
            frozen = DspPredecessor({"graph": graph})
        except NullWalkError as exc:
            raise ActiveDelayWalkError(
                "live_readback_invalid",
                "live graph read-back must be non-empty exact JSON data",
            ) from exc
        object.__setattr__(self, "readback_id", read_id)
        object.__setattr__(self, "topology_id", topology)
        object.__setattr__(self, "crossover_fc_hz", fc)
        object.__setattr__(self, "_frozen", frozen)

    @property
    def graph(self) -> dict[str, Any]:
        """Return a fresh copy of the frozen graph read-back."""

        graph = self._frozen.state["graph"]
        assert isinstance(graph, dict)
        return graph


ReadLiveGraph: TypeAlias = Callable[
    [DelayCandidate | None],
    Awaitable[ActiveDelayLiveGraph] | ActiveDelayLiveGraph,
]
BuildAndLoadCandidate: TypeAlias = Callable[
    [DelayGraphSnapshot, DelayCandidate],
    Awaitable[bool | None] | bool | None,
]
CaptureAdmittedNull: TypeAlias = Callable[
    [DelayCandidate, int, DelayCandidateConfirmation],
    Awaitable[Mapping[str, Any]] | Mapping[str, Any],
]
RestorePredecessor: TypeAlias = Callable[
    [DspPredecessor],
    Awaitable[DspRestoreConfirmation] | DspRestoreConfirmation,
]
AttemptIsCurrent: TypeAlias = Callable[
    [CommissioningAttemptHandle],
    Awaitable[bool] | bool,
]


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _refuse(
            "capture_identity_invalid",
            f"{field_name} must be non-empty trimmed text",
        )
    return value


def _same_delay(left: object, right: float) -> bool:
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        return False
    value = float(left)
    return math.isfinite(value) and math.isclose(
        value,
        right,
        rel_tol=1e-9,
        abs_tol=1e-6,
    )


def _validated_capture(
    raw: object,
    *,
    context: ActiveDelayWalkContext,
    candidate: DelayCandidate,
    confirmation: DelayCandidateConfirmation,
    capture_ids: set[str],
    admission_ids: set[str],
    null_ids: set[str],
) -> Mapping[str, Any]:
    """Return one frozen capture after enforcing Active evidence authority."""

    if not isinstance(raw, Mapping):
        _refuse("capture_invalid", "admitted null capture must be a mapping")
    try:
        frozen = DspPredecessor({"capture": raw}).state["capture"]
    except NullWalkError as exc:
        raise ActiveDelayWalkError(
            "capture_invalid",
            "admitted null capture must be exact JSON data",
        ) from exc
    assert isinstance(frozen, dict)

    run = context.attempt.run
    expected_correlation: dict[str, object] = {
        "session_id": run.session_id,
        "run_id": run.run_id,
        "attempt_id": context.attempt.attempt_id,
        "target_id": context.attempt.target_id,
        "target_fingerprint": context.attempt.target_fingerprint,
        "context_fingerprint": context.fingerprint,
        "topology_id": context.topology_id,
        "speaker_group_id": context.speaker_group_id,
        "region_id": context.region_id,
        "crossover_fc_hz": context.crossover_fc_hz,
        "threshold_profile_fingerprint": context.threshold_profile_fingerprint,
        "safety_profile_fingerprint": context.safety_profile_fingerprint,
        "placement_fingerprint": context.placement_fingerprint,
    }
    correlation_mismatch = any(
        (
            not _same_delay(frozen.get(name), cast(float, expected))
            if name == "crossover_fc_hz"
            else frozen.get(name) != expected
        )
        for name, expected in expected_correlation.items()
    )
    if correlation_mismatch:
        _refuse(
            "capture_correlation_mismatch",
            "null capture belongs to a stale run, attempt, target, or region context",
        )

    capture_id = _identifier(frozen.get("capture_id"), field_name="capture_id")
    admission = frozen.get("capture_admission")
    if not isinstance(admission, Mapping):
        _refuse(
            "capture_admission_refused",
            "capture_admission must retain the server-owned playback handoff",
        )
    admission_id = _identifier(
        admission.get("admission_id"),
        field_name="capture_admission.admission_id",
    )
    try:
        admission_decision = ExcitationAdmission.from_dict(admission.get("admission"))
    except ValueError as exc:
        raise ActiveDelayWalkError(
            "capture_admission_refused",
            "capture playback admission is not a valid typed decision",
        ) from exc
    if not admission_decision.allowed:
        _refuse(
            "capture_admission_refused",
            "capture playback must carry an affirmative excitation admission",
        )
    if (
        admission_decision.request.target_fingerprint
        != context.attempt.target_fingerprint
        or admission_decision.request.safety_profile_fingerprint
        != context.safety_profile_fingerprint
    ):
        _refuse(
            "capture_admission_refused",
            "capture admission request belongs to another target or safety profile",
        )

    quality = frozen.get("quality")
    if not isinstance(quality, Mapping) or quality.get("accepted") is not True:
        _refuse(
            "capture_quality_refused",
            "capture quality must be affirmatively accepted",
        )

    acoustic = frozen.get("acoustic")
    snr = acoustic.get("snr") if isinstance(acoustic, Mapping) else None
    if (
        not isinstance(snr, Mapping)
        or snr.get("decision_class") != "alignment"
        or snr.get("verdict") != "ok"
    ):
        _refuse(
            "capture_snr_refused",
            "null capture requires affirmative alignment SNR",
        )

    null_identity = frozen.get("null_identity")
    if not isinstance(null_identity, Mapping):
        _refuse(
            "capture_identity_invalid",
            "null_identity must bind the exact candidate confirmation",
        )
    null_id = _identifier(null_identity.get("null_id"), field_name="null_id")
    if (
        null_identity.get("candidate_fingerprint") != confirmation.candidate_fingerprint
        or null_identity.get("snapshot_fingerprint")
        != confirmation.snapshot_fingerprint
        or null_identity.get("expect_null") is not True
        or not _same_delay(
            null_identity.get("relative_delay_us"),
            candidate.relative_delay_us,
        )
    ):
        _refuse(
            "capture_identity_invalid",
            "null capture is not bound to the exact live candidate confirmation",
        )

    if (
        capture_id in capture_ids
        or admission_id in admission_ids
        or null_id in null_ids
    ):
        _refuse(
            "capture_duplicate",
            "capture, admission, and null identities must be unique within the walk",
        )
    capture_ids.add(capture_id)
    admission_ids.add(admission_id)
    null_ids.add(null_id)
    return cast(Mapping[str, Any], frozen)


def _failure_code(error: BaseException) -> str:
    if isinstance(error, ActiveDelayWalkError):
        return error.code
    if isinstance(error, DelayGraphProofError):
        return error.code
    if isinstance(error, DspWriterLockTimeout):
        return "writer_lock_timeout"
    if isinstance(error, NullWalkError):
        return "null_walk_refused"
    if isinstance(error, BaseExceptionGroup):
        codes = {_failure_code(nested) for nested in error.exceptions}
        return codes.pop() if len(codes) == 1 else "multiple_failures"
    return "cancelled" if isinstance(error, asyncio.CancelledError) else "other"


async def run_active_delay_walk(
    spec: NullWalkSpec,
    *,
    config_dir: str | Path,
    context: ActiveDelayWalkContext,
    positive_lane: DelayLaneBinding,
    negative_lane: DelayLaneBinding,
    attempt_is_current: AttemptIsCurrent,
    read_live_graph: ReadLiveGraph,
    build_and_load_candidate: BuildAndLoadCandidate,
    capture_admitted_null: CaptureAdmittedNull,
    restore_predecessor: RestorePredecessor,
    lock_timeout_s: float = DEFAULT_ACTIVE_DELAY_WALK_LOCK_TIMEOUT_S,
    restore_timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Run one writer-locked Active delay walk and restore the exact entry graph.

    The lock covers the initial live snapshot, every candidate mutation and
    fresh read-back, all five admitted captures per candidate, and exact
    restoration. ``read_live_graph(None)`` supplies the zero-relative entry
    graph; later calls receive the exact candidate that was just loaded. The
    required ``attempt_is_current`` authority is checked before lock admission,
    again after admission, and around every callback. If it becomes stale after
    mutation, Shared restores the predecessor before the refusal propagates.

    The capture callback receives the same typed confirmation produced from
    that candidate's fresh read-back. It must return JSON evidence containing:
    a unique ``capture_id``; a server-owned ``capture_admission`` whose nested
    admission has ``allowed=true`` and no refusal reasons; ``quality.accepted``;
    affirmative alignment SNR in ``acoustic.snr``; and a unique
    ``null_identity`` bound to the confirmation's candidate/snapshot
    fingerprints and relative delay. The capture also echoes the context's
    exact session, run, attempt, target, topology, group, crossover region,
    safety, threshold, and placement identities. Missing, false, stale, or
    duplicate authority raises before the shared selector can consume it.
    """

    if not isinstance(spec, NullWalkSpec):
        _refuse("argument_invalid", "spec must be NullWalkSpec")
    if not isinstance(context, ActiveDelayWalkContext):
        _refuse("context_invalid", "context must be ActiveDelayWalkContext")
    if not math.isclose(
        spec.crossover_fc_hz,
        context.crossover_fc_hz,
        rel_tol=1e-9,
        abs_tol=1e-6,
    ):
        _refuse("context_invalid", "spec crossover does not match the walk context")
    topology = context.topology_id
    if not isinstance(positive_lane, DelayLaneBinding) or not isinstance(
        negative_lane, DelayLaneBinding
    ):
        _refuse("argument_invalid", "delay lanes must be typed bindings")
    lock_timeout = _positive_finite(lock_timeout_s, field_name="lock_timeout_s")
    if lock_timeout > MAX_ACTIVE_DELAY_WALK_LOCK_TIMEOUT_S:
        _refuse(
            "argument_invalid",
            "lock_timeout_s exceeds the bounded Active writer admission limit",
        )
    config_path = Path(config_dir)

    snapshot: DelayGraphSnapshot | None = None
    seen_readbacks: set[str] = set()
    confirmations: dict[float, DelayCandidateConfirmation] = {}
    capture_ids: set[str] = set()
    admission_ids: set[str] = set()
    null_ids: set[str] = set()
    authority_stale_after_restore = False
    authority_error_after_restore: BaseException | None = None

    async def require_current_attempt() -> None:
        current = await _resolve(attempt_is_current(context.attempt))
        if current is not True:
            _refuse(
                "attempt_stale",
                "commissioning attempt is no longer current",
            )

    def accept_live_readback(value: object) -> ActiveDelayLiveGraph:
        if not isinstance(value, ActiveDelayLiveGraph):
            _refuse(
                "live_readback_invalid",
                "read_live_graph must return ActiveDelayLiveGraph",
            )
        if value.readback_id in seen_readbacks:
            _refuse(
                "live_readback_duplicate",
                "live graph read-back identity was replayed",
            )
        seen_readbacks.add(value.readback_id)
        if value.topology_id != topology or not math.isclose(
            value.crossover_fc_hz,
            spec.crossover_fc_hz,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            _refuse(
                "stale_context",
                "live graph read-back belongs to a stale topology or crossover",
            )
        return value

    async def snapshot_predecessor() -> DspPredecessor:
        nonlocal snapshot
        await require_current_attempt()
        live_result = await _resolve(read_live_graph(None))
        await require_current_attempt()
        live = accept_live_readback(live_result)
        predecessor = DspPredecessor(
            {
                "schema_version": 1,
                "kind": "jts_active_delay_walk_predecessor",
                "context_fingerprint": context.fingerprint,
                "topology_id": topology,
                "crossover_fc_hz": spec.crossover_fc_hz,
                "entry_readback_id": live.readback_id,
                "active_raw": live.graph,
            }
        )
        snapshot = DelayGraphSnapshot(
            spec,
            scope=DRIVER_DELAY_WALK_SCOPE,
            topology_id=topology,
            positive_lane=positive_lane,
            negative_lane=negative_lane,
            predecessor=predecessor,
        )
        return predecessor

    async def apply_and_confirm(candidate: DelayCandidate) -> bool:
        if snapshot is None:
            _refuse("candidate_apply_invalid", "delay snapshot is unavailable")
        await require_current_attempt()
        loaded = await _resolve(build_and_load_candidate(snapshot, candidate))
        await require_current_attempt()
        if loaded is not None and loaded is not True:
            _refuse(
                "candidate_apply_invalid",
                "build_and_load_candidate must return True or None",
            )
        live_result = await _resolve(read_live_graph(candidate))
        await require_current_attempt()
        live = accept_live_readback(live_result)
        confirmation = confirm_delay_candidate(
            snapshot,
            candidate,
            live.graph,
            expected_snapshot_fingerprint=snapshot.fingerprint,
            expected_scope=DRIVER_DELAY_WALK_SCOPE,
            expected_topology_id=topology,
            expected_crossover_fc_hz=spec.crossover_fc_hz,
        )
        confirmations[candidate.relative_delay_us] = confirmation
        return True

    async def capture(
        candidate: DelayCandidate,
        index: int,
    ) -> Mapping[str, Any]:
        confirmation = confirmations.get(candidate.relative_delay_us)
        if confirmation is None:
            _refuse(
                "capture_identity_invalid",
                "candidate has no fresh live-graph confirmation",
            )
        await require_current_attempt()
        raw = await _resolve(capture_admitted_null(candidate, index, confirmation))
        await require_current_attempt()
        return _validated_capture(
            raw,
            context=context,
            candidate=candidate,
            confirmation=confirmation,
            capture_ids=capture_ids,
            admission_ids=admission_ids,
            null_ids=null_ids,
        )

    async def restore_and_observe(
        predecessor: DspPredecessor,
    ) -> DspRestoreConfirmation:
        nonlocal authority_error_after_restore, authority_stale_after_restore
        restored = await _resolve(restore_predecessor(predecessor))
        try:
            current = await _resolve(attempt_is_current(context.attempt))
        except asyncio.CancelledError as error:
            # Exact restore has already returned. Preserve its typed result so
            # Shared can verify it before this cancellation propagates.
            authority_error_after_restore = error
        except Exception as error:  # noqa: BLE001 - authority callback boundary
            # Exact restore has already returned. Preserve its typed result so
            # Shared can verify it before this authority failure propagates.
            authority_error_after_restore = error
        else:
            if current is not True:
                authority_stale_after_restore = True
        return cast(DspRestoreConfirmation, restored)

    log_event(
        logger,
        "correction.crossover_delay_walk_started",
        session=context.attempt.run.session_id,
        run_id=context.attempt.run.run_id,
        attempt_id=context.attempt.attempt_id,
        group=context.speaker_group_id,
        region=context.region_id,
        topology_id=topology,
        crossover_fc_hz=spec.crossover_fc_hz,
        candidate_count=spec.candidate_count,
        captures_per_candidate=ACTIVE_DELAY_WALK_CAPTURES_PER_CANDIDATE,
    )
    try:
        await require_current_attempt()
        async with dsp_writer_lock(
            config_path,
            source=_WRITER_SOURCE,
            timeout_s=lock_timeout,
        ):
            await require_current_attempt()
            result = await run_null_walk(
                spec,
                apply_candidate=apply_and_confirm,
                capture_null=capture,
                snapshot_predecessor=snapshot_predecessor,
                restore_predecessor=restore_and_observe,
                scope=DRIVER_DELAY_WALK_SCOPE,
                captures_per_candidate=ACTIVE_DELAY_WALK_CAPTURES_PER_CANDIDATE,
                restore_timeout_s=restore_timeout_s,
            )
            if authority_error_after_restore is not None:
                raise authority_error_after_restore
            if authority_stale_after_restore:
                _refuse(
                    "attempt_stale",
                    "commissioning attempt became stale during exact restoration",
                )
            await require_current_attempt()
    except BaseException as error:
        log_event(
            logger,
            "correction.crossover_delay_walk_failed",
            level=(
                logging.INFO
                if isinstance(error, asyncio.CancelledError)
                else logging.WARNING
            ),
            session=context.attempt.run.session_id,
            run_id=context.attempt.run.run_id,
            attempt_id=context.attempt.attempt_id,
            group=context.speaker_group_id,
            region=context.region_id,
            topology_id=topology,
            crossover_fc_hz=spec.crossover_fc_hz,
            snapshot_fingerprint=(snapshot.fingerprint if snapshot else None),
            failure_code=_failure_code(error),
            error_type=type(error).__name__,
        )
        raise
    log_event(
        logger,
        "correction.crossover_delay_walk_completed",
        session=context.attempt.run.session_id,
        run_id=context.attempt.run.run_id,
        attempt_id=context.attempt.attempt_id,
        group=context.speaker_group_id,
        region=context.region_id,
        topology_id=topology,
        crossover_fc_hz=spec.crossover_fc_hz,
        snapshot_fingerprint=(snapshot.fingerprint if snapshot else None),
        status=result.get("status"),
        reason=result.get("reason"),
        selected_relative_delay_us=result.get("selected_relative_delay_us"),
    )
    return result
