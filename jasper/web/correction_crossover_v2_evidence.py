# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session evidence publication and production bindings."""

from __future__ import annotations

from jasper.active_speaker.crossover_v2.capture_provenance import analysis_provenance
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.web import correction_crossover_v2_volume as v2volume


import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_MEASURE
from jasper.active_speaker.capture_provenance import CaptureProvenanceRecorder, record_capture_provenance
from jasper.audio_measurement.calibration import configured_calibration_root
from jasper.audio_measurement.household_mic import (
    household_mic_path,
    resolve_setup_calibration as resolve_household_setup_calibration,
)
from jasper.log_event import log_event

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_v2_flow import AnalyzeCapture

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# production seam bindings (S1a/S1e)
# --------------------------------------------------------------------------- #


def _wav_bytes_to_samples(wav_bytes: bytes) -> tuple[Any, int]:
    """This binding's decode, now owned beside its encoder.

    Lifted to :func:`~jasper.audio_measurement.wired_capture.decode_wav_to_mono`
    so the engine's offline ``analyze`` can decode a banked capture without
    reaching into ``jasper.web`` — the dependency runs the other way, and this
    was the one piece of the analyze-seam assembly the truth layer needed.
    """
    from jasper.audio_measurement.wired_capture import decode_wav_to_mono

    return decode_wav_to_mono(wav_bytes)


def resolve_setup_calibration(setup: Any, device: Any) -> Any:
    """The production mic-calibration resolver for a v2 capture.

    Consumes ``household_mic.resolve_setup_calibration`` — the ONE point the
    capture's ``setup.calibration`` reference becomes a stored
    ``CalibrationRecord``. Returns the record, or ``None`` when the capture
    declared no calibration or its reference names a DIFFERENT mic than the
    one this capture reports (the 2026-07-20 incident). ``device`` is this
    capture's realized input device (``CaptureAnswer.device``) — threaded
    through so that mismatch is caught where the calibration is resolved for
    THIS capture, not applied blind to whichever mic actually recorded.
    """
    return resolve_household_setup_calibration(
        setup if isinstance(setup, Mapping) else None,
        device=device if isinstance(device, Mapping) else None,
        root=configured_calibration_root(),
        path=household_mic_path(),
    )


def default_setup_calibration_for_v2() -> Any | None:
    """The v2 session's OPTIONAL household-mic prefill hint (W6.12).

    Every v2 capture logged ``crossover_v2_uncalibrated_capture`` even when
    the household had a resolvable stored mic (a UMIK-2 by serial, ingested
    through ``jasper-mic-calibration``). Root cause:
    ``resolve_setup_calibration`` is only as good as the reference the capture
    carries in ``setup.calibration``, and a v2 session has no
    calibration-picker screen of its own (design: CHECK's own pilot pairs
    solve gain), so nothing carried the household's remembered mic into it.

    Reuses ``correction_capture._default_setup_calibration_for_spec`` — the ONE
    household-mic-hint resolver. Session specs forward it to
    ``build_crossover_sweep_spec`` through ``**spec_kwargs``, and
    the measurement source mints the capture's own reference from it
    through ``wired_capture.setup_from_hint``. Fail-soft: any
    resolution miss yields no hint, never blocks session open.
    """
    from .correction_capture import _default_setup_calibration_for_spec

    try:
        return _default_setup_calibration_for_spec()
    except (OSError, RuntimeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_default_calibration_hint_failed",
            level=logging.WARNING,
        )
        return None


def _setup_calibration_observation(setup: Any) -> tuple[str, str]:
    """What the capture's own setup reference held, redacted-safe (W6.13).

    Returns ``(mode, calibration_id)`` for the uncalibrated-capture WARN so a
    live journal line settles empirically whether the capture carried NO setup
    at all (``mode="absent"``) or one whose calibration didn't resolve (e.g.
    ``mode="none"``, or a stale ``calibration_id``). Only the mode and the
    calibration_id (a stored-record id, not a secret) are ever extracted.
    """
    if not isinstance(setup, Mapping):
        return "absent", ""
    calibration = setup.get("calibration")
    if not isinstance(calibration, Mapping):
        return "absent", ""
    return (
        str(calibration.get("mode") or ""),
        str(calibration.get("calibration_id") or ""),
    )


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_setup_calibration,
    meta: dict[str, Any] | None = None,
) -> "AnalyzeCapture":
    """The real ``analyze`` seam: CaptureResult → ``analyze_program_capture``.

    Design §5.6.4 applies the mic cal to every gated response, so this binding
    resolves the calibration from the capture's phone-reported setup (the same
    machinery the legacy flows use)
    and threads BOTH the resolved curve and the conductor's declared geometry
    into ``analyze_program_capture``. When no calibration resolves, the
    analysis still runs — relative timing/level stay valid per the design —
    but the fact is never silent: a WARN ``event=`` fires and ``meta``
    (persisted with the session's evidence refs) records the per-phase
    ``{"applied": False}`` annotation.

    ``phase`` (required, keyword-only) is the conductor's own flow phase —
    ``correction_run_host.bind_plan_analysis`` always passes it, and
    ``crossover_v2_flow.AnalyzeCapture`` declares it. It is NOT the same
    value as ``program.phase``: every cloud position plays the verify-shaped
    summed sweep, so ``program.phase == "verify"`` even during
    PHASE_CLOUD_MEASURE/PHASE_CLOUD_VERIFY. It keys the per-phase calibration
    annotation and labels this binding's log lines, so those name the capture
    rather than the shared program object.
    """

    def _analyze(
        program: Any, result: Any, priors: Any, geometry: Any, *, phase: str,
    ) -> Any:
        from jasper.audio_measurement import program_analysis as _pa
        from jasper.audio_measurement.calibration import mic_tier_for_model

        wav = getattr(result, "wav", result)
        samples, rate = _wav_bytes_to_samples(wav)
        setup = getattr(result, "setup", None)
        record = None
        if resolve_calibration is not None:
            try:
                record = resolve_calibration(
                    setup, getattr(result, "device", None)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                # A resolver failure downgrades to an annotated-uncalibrated
                # analysis, never a crashed capture — but it is logged.
                log_event(
                    logger,
                    "correction.crossover_v2_calibration_resolve_failed",
                    level=logging.WARNING,
                    phase=phase,
                )
                record = None
        curve = getattr(record, "curve", None)
        if record is not None and curve is None:
            # A bare CalibrationCurve (tests / future callers) is accepted too;
            # anything else stays None (annotated uncalibrated, never a crash).
            from jasper.audio_measurement.calibration import CalibrationCurve

            if isinstance(record, CalibrationCurve):
                curve = record
        if curve is None:
            # W6.13 round-5 diagnostic: name what the phone-reported setup
            # actually held at resolve time so a live journal line
            # distinguishes "the phone sent nothing" (setup_mode=absent)
            # from "the phone sent a choice that didn't resolve"
            # (setup_mode=none/stored/..., with its id). Redacted-safe —
            # see _setup_calibration_observation.
            setup_mode, setup_calibration_id = _setup_calibration_observation(
                setup
            )
            log_event(
                logger,
                "correction.crossover_v2_uncalibrated_capture",
                level=logging.WARNING,
                phase=phase,
                setup_mode=setup_mode,
                setup_calibration_id=setup_calibration_id,
            )
        priors = dataclasses.replace(
            priors,
            mic_tier=mic_tier_for_model(getattr(record, "model", None)),
            mic_calibrated=curve is not None,
        )
        analysis = _pa.analyze_program_capture(
            program,
            samples,
            rate,
            calibration=curve,
            geometry=geometry,
            priors=priors,
            # #2094: the phone's own frame counters, reconciled against the
            # frames just decoded. This seam is the ONLY place both halves of
            # the ledger exist — the page's account arrives on the status
            # event channel, the received count comes out of the WAV — so it is
            # the only place the comparison can be made.
            capture_report=getattr(result, "capture_integrity", None),
        )
        fields = analysis_provenance(program, analysis, record, curve, geometry)
        if meta is not None:
            meta.setdefault("calibration", {})[phase] = fields["capture_calibration"]
            meta.setdefault("capture_provenance", {})[phase] = fields
        return analysis

    return _analyze


def open_v2_evidence_store(topology: Any) -> tuple[Any, str]:
    """Open a fresh v2 commissioning bundle + its exact evidence store (§5.6).

    Every v2 measurement session gets its own retention-bounded bundle under
    ``sessions_dir()`` (the same SC-4 bundle machinery the legacy flow uses),
    and every phase artifact is published through the store's write-once +
    tamper-checked-reopen path. Returns ``(store, bundle_session_id)``.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    info = open_bundle(topology, calibration_id="")
    if not isinstance(info, Mapping) or not info.get("session_id"):
        raise CrossoverV2Refused(
            "could not open a commissioning evidence bundle for this session"
        )
    session_id = str(info["session_id"])
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=session_id
    )
    return store, session_id


def _record_store(store: Any, capture_session_id: str) -> Any:
    """THE durable-write seam for this session's evidence (ADR-0227 §12).

    A frozen dataclass over the same bundle, so the binders that each build one
    are one writer constructed several times and never several authorities.
    """
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore

    return BankedRecordStore(evidence=store, capture_session_id=capture_session_id)


def _bank(
    records: Any, run_async: Any, record: Mapping[str, Any],
) -> tuple[str, Any]:
    """Bank one record; answer its store id and the artifact it wrote.

    The store owns the path, the envelope, the discriminator and the
    reopen-and-compare, and answers with the id that finds the record again;
    the identity every ``refs`` column needs is re-read from it. Driven through ``run_async`` because the publishing seams are
    synchronous and run on a worker thread.
    """
    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    record_id = str(run_async(records.bank(record)))
    return record_id, records.evidence.identify_artifact(
        f"{EVIDENCE_ROOT}/artifacts/{record_id}"
    )


def bind_evidence_publishers(
    store: Any, capture_session_id: str, run_async: Any
) -> tuple[Callable[[Any, Mapping[str, Any]], None], dict[str, Any]]:
    """The real ``publish_check`` seam (§5.6) and the ``refs`` it writes.

    CHECK banks the ambient report + solved gain plan. The artifact fingerprint
    lands in the returned ``refs`` mapping (persisted into the durable state for
    the status surface). Not fail-soft: a CHECK whose evidence did not land has
    nothing for the household to review.
    """
    from jasper.active_speaker.crossover_v2.record_store import CHECK_EVIDENCE_KIND

    records = _record_store(store, capture_session_id)
    refs: dict[str, Any] = {"bundle_session_id": store.session_id}

    def publish_check(gain_plan: Any, ambient_report: Mapping[str, Any]) -> None:
        _, artifact = _bank(records, run_async, {
            "kind": CHECK_EVIDENCE_KIND,
            "gain_plan_db": dict(gain_plan.gain_db),
            "predicted_peak_dbfs": gain_plan.predicted_peak_dbfs,
            "snr_floor_ok": gain_plan.snr_floor_ok,
            # #1825: the per-role derivation behind ``gain_plan_db`` — which
            # limit chose each driver's MEASURE level and the ambient evidence
            # it rests on. Empty for a legacy plan that carries no solves
            # (never a claim that nothing moved).
            "role_solves": {
                role: solve.to_dict()
                for role, solve in (gain_plan.role_solves or {}).items()
            },
            "ambient_report": dict(ambient_report),
        })
        refs["check_artifact"] = artifact.fingerprint

    return publish_check, refs


@dataclass(frozen=True)
class _HeldSession:
    """What one prepared capture hosting holds between ``open`` and the run.

    A named pair rather than the untyped ``holder`` dict this replaces: the
    engine's session and the source walk are two different lifetimes that
    happen to be handed across the same closure boundary, and ``holder["run"]``
    could not say which of them a reader was looking at.
    """

    tuning: Any
    run: Any


@dataclass(frozen=True)
class ProductionPlay:
    graph: Any
    compose: Any


def bind_production_play(
    *,
    camilla_factory: Any,
    evidence_store: Any,
    capture_session_id: str,
    topology: Any,
    preset: Any,
    role_channels: Mapping[str, int],
    playback_device: str,
    safety_profile: Mapping[str, Any],
    role_targets: Mapping[str, str],
    session_volume_db: float,
    protection_sections_by_role: Mapping[str, Sequence[Any]] | None = None,
    declared_sensitivities: Mapping[str, float] | None = None,
    config_dir: str | None = None,
    provenance: CaptureProvenanceRecorder | None = None,
    program_for_phase: Callable[[str], Any],
    program_for_spec: Callable[[Any, Any], Any] | None = None,
    roles: Sequence[Any],
) -> "ProductionPlay":
    """Bind the shared graph and stimulus owners to this session's state."""
    from jasper.active_speaker.crossover_v2.composition import bind_program_composer
    from jasper.active_speaker.crossover_v2.door import bind_measurement_graph
    from jasper.active_speaker.crossover_v2.programs import SUMMED_SWEEP_PHASES
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, measurement_graph_evidence
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR

    resolved_config_dir = config_dir or str(DEFAULT_CAMILLA_CONFIG_DIR)
    session_graph = bind_measurement_graph(
        MeasurementGraphProfile(
            preset=preset, topology=topology, role_channels=role_channels,
            playback_device=playback_device,
            protection_sections_by_role=protection_sections_by_role,
        ), camilla_factory=camilla_factory, config_dir=resolved_config_dir,
    )

    def _program(spec: Any, stimulus_dbfs: Any) -> Any:
        if program_for_spec is not None:
            return program_for_spec(spec, stimulus_dbfs)
        if stimulus_dbfs is not None:
            raise ValueError("The round's program owns its stimulus level.")
        phase = spec.program_phase
        if spec.graph_scope != "drivers" and phase not in SUMMED_SWEEP_PHASES:
            phase = PHASE_CLOUD_MEASURE
        return program_for_phase(phase)

    async def _before_play(spec: Any, program: Any, artifact: Any, phase: str) -> None:
        await v2volume.session_volume_plan().hold_measurement_volume(
            v2volume._session_volume_read(camilla_factory), context=f"capture:{phase}",
        )
        await record_capture_provenance(
            provenance, open_cam=camilla_factory,
            graph_kind="tuning_measurement", program=program,
            phase=phase, artifact=artifact,
            read_volume_plan=v2volume.session_volume_plan,
        )

    compose = bind_program_composer(
        program_for_spec=_program, store=evidence_store,
        capture_session_id=capture_session_id, cam_factory=camilla_factory,
        config_dir=resolved_config_dir, topology=topology,
        safety_profile=safety_profile, role_targets=role_targets,
        declared_sensitivities=declared_sensitivities,
        before_play=_before_play, graph_yaml=session_graph.installed_graph_yaml,
        level_reference_yaml=session_graph.level_reference_yaml,
        roles=roles,
        graph_evidence_for_spec=lambda spec: measurement_graph_evidence(scope=spec.graph_scope, candidate_id=spec.candidate_id),
    )

    return ProductionPlay(graph=session_graph, compose=compose)
