# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session evidence publication and production bindings."""

from __future__ import annotations

from jasper.json_fields import finite_float

from jasper.active_speaker.crossover_v2.capture_provenance import analysis_provenance, enrich_capture_record
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.web import correction_crossover_v2_volume as v2volume


import dataclasses
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, TypeVar

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


class CaptureEvidenceCarry:
    """The analyze seam's one-capture handoff of the blocks a take banks.

    Same single-shot discipline, and for the same reason, as
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`
    — read that class for why ``take`` consumes. A separate slot rather than a
    second field on that one because the two hops answer different questions
    and are fed by different seams: the play seam observes the graph and the
    fader, and only the analyze seam has ever held the analysis.

    ``record`` overwrites unconditionally, so nothing has to be drained first:
    every analyze produces a block set (``diagnostic`` at minimum), so a
    refused capture's blocks are always replaced by the next analyze rather
    than stranded for the next accepted take to pick up.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Mapping[str, Any] | None = None

    def record(self, blocks: Mapping[str, Any]) -> None:
        with self._lock:
            self._pending = blocks

    def take(self) -> Mapping[str, Any] | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending


def _bankable(value: Any) -> Any:
    """One JSON document with unbankable floats nulled, recursively.

    NOT decoration. ``CommissioningEvidenceStore`` canonicalises with
    ``allow_nan=False``, so a single ``NaN`` anywhere in a banked record is a
    ``MALFORMED`` refusal — and the retention seam fail-softs, which would
    lose the WHOLE take record over one unmeasurable diagnostic. Since the
    point of carrying these blocks is to stop losing data, an unmeasurable
    number becomes ``null``.

    **Keys are never dropped, only their values nulled**, and that is the whole
    difference between a scrub and a lie. ``analysis_diagnostic_summary``
    spends tri-states deliberately — ``polarity_agrees_with_sum`` is ``None``
    for "nobody cross-checked" against an absent key for "no alignment at all",
    and the ``frame_*`` block is "present with ``None`` terms when the
    comparison ran but no frame could be fitted; absent only when no
    comparison happened" — so a pass that removed empty keys would flatten
    those two answers into one, permanently, on a write-once record.

    Floats only. An unbounded JSON integer serializes exactly, so ``int`` is
    left alone and only the type that can BE ``NaN``/``inf`` is screened —
    through ``json_fields.finite_float``. A non-native number (a
    ``numpy`` scalar, an array) is NOT screened here and would cost the record
    at the store's own ``TypeError``; no field on today's three blocks is one,
    and :func:`_capture_evidence_blocks` names that contract.
    """
    if isinstance(value, float):
        return finite_float(value)
    if isinstance(value, Mapping):
        return {key: _bankable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bankable(item) for item in value]
    return value


def _add_capture_block(
    blocks: dict[str, Any], name: str, build: Callable[[], Any],
) -> None:
    """Add one evidence block, or lose that block and nothing else.

    The belt the deleted ring writer carried in as many words — *"ANY failure
    here must never affect the measurement itself"* — kept rather than dropped
    with it. This runs inside the analyze seam, so a raise costs the CAPTURE:
    the sweep played, the operator is standing at the mark, and a diagnostic
    that could not be summarised would take the measurement with it.

    Per block, not around all three, so a raise while summarising the analysis
    still leaves the frame ledger banked. The caught tuple is concrete rather
    than blind for the reason the shapes below are real: ``AttributeError`` and
    ``TypeError`` are what a half-populated or foreign analysis produces, and
    ``ValueError`` is what a hostile mapping produces. A genuinely unexpected
    type still propagates to the analyze seam's own callers.
    """
    try:
        blocks[name] = _bankable(build())
    except (AttributeError, TypeError, ValueError):
        log_event(
            logger, "correction.crossover_v2_capture_evidence_block_failed",
            level=logging.WARNING, block=name, exc_info=True,
        )


def _capture_evidence_blocks(result: Any, analysis: Any) -> dict[str, Any]:
    """Retain recorder counters separately from the analysis verdict.

    A malformed optional block must not discard an otherwise bankable take.
    """
    from jasper.audio_measurement import program_analysis as _pa

    blocks: dict[str, Any] = {}
    _add_capture_block(
        blocks, "diagnostic", lambda: _pa.analysis_diagnostic_summary(analysis),
    )
    report = getattr(result, "capture_integrity", None)
    if isinstance(report, Mapping) and report:
        _add_capture_block(blocks, "capture_integrity", lambda: dict(report))
    ledger = getattr(analysis, "frame_ledger", None)
    if ledger is not None:
        # A lambda and not ``ledger.to_dict``: the bound-method LOOKUP is
        # itself an attribute read, and passing it would raise while building
        # the argument — outside the guard that exists to catch exactly that.
        _add_capture_block(blocks, "frame_ledger", lambda: ledger.to_dict())
    return blocks


def bind_production_analyze(
    *,
    resolve_calibration: Callable[[Any, Any], Any] | None = resolve_setup_calibration,
    meta: dict[str, Any] | None = None,
    provenance: CaptureProvenanceRecorder | None = None,
    carry: CaptureProvenanceRecorder | None = None,
    evidence: CaptureEvidenceCarry | None = None,
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

    ``provenance`` (optional) is the session's
    :class:`~jasper.active_speaker.capture_provenance.CaptureProvenanceRecorder`
    — the same object ``bind_production_play`` records into, and the only way a
    banked take can name the graph it went through.

    ``carry`` (optional) is the SECOND recorder, the one the banking seam
    drains. The shot stays single and stays here, because this is the only
    place in a capture's life that runs exactly once between the play that
    observed the graph and the arm that decides whether to bank. Re-recorded
    rather than re-observed: ``CaptureProvenance`` is a snapshot the play seam
    already took, so the second hop moves bytes, never readings. See
    ``bind_position_retention`` for the drain.

    ``evidence`` (optional) is the analyze seam's OWN handoff to that same
    banking seam: the ``diagnostic``/``capture_integrity``/``frame_ledger``
    blocks, which exist nowhere else in a capture's life. This is the only
    moment they can be taken — the analysis is rewritten inside the round and
    the capture bytes are gone by the time anything reads the bundle — so a
    binding without one computes them and drops them, which is the data-loss
    window the dump ring's death opened. See :func:`_capture_evidence_blocks`.
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
        # THIS capture's stimulus, consumed ONCE: a second analyze with no
        # play between gets ``None``, never the last capture's context. The
        # banking seam is its one consumer, reached through ``carry``.
        taken = provenance.take() if provenance is not None else None
        if carry is not None:
            # DRAINED FIRST, unconditionally, and that is not tidiness. Banking
            # is accepted-only, so a REFUSED capture leaves whatever this
            # analyze put in the carry with nobody to take it out. The next
            # accepted capture whose own observation missed would then drain a
            # value belonging to a capture that never became evidence, and
            # write it into a write-once forensic record naming the wrong
            # graph and the wrong fader. ``record`` cannot clear that by
            # itself: the case that strands a value is exactly the case where
            # there is no new value to overwrite it with.
            carry.take()
            if taken is not None:
                carry.record(taken)
        if evidence is not None:
            # No drain-first, unlike the carry above: this block set is never
            # empty, so a refused capture's blocks are overwritten here rather
            # than stranded for the next accepted take to drain.
            evidence.record({
                **_capture_evidence_blocks(result, analysis),
                **fields,
            })
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


_T = TypeVar("_T")


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
    the identity every ``refs`` column and every citation needs is re-read from
    it. Driven through ``run_async`` because the publishing seams are
    synchronous and run on a worker thread.
    """
    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

    record_id = str(run_async(records.bank(record)))
    return record_id, records.evidence.identify_artifact(
        f"{EVIDENCE_ROOT}/artifacts/{record_id}"
    )


def _bank_findings(
    records: Any, run_async: Any, *, phase: str, finding_set: Any,
) -> Any:
    """Bank one phase's finding set; answer the artifact it wrote.

    ``phase`` rides the record to ROUTE it — per phase and not per session,
    because the two groups close at different times and the store is write-once
    — and the route takes it back off: the file is ``FindingSet.to_dict()``.
    """
    _, artifact = _bank(
        records, run_async, {**finding_set.to_dict(), "phase": phase},
    )
    return artifact


def _fail_soft(work: Callable[[], _T], *, event: str, **fields: Any) -> _T | None:
    """Run one durable write; log ``event`` and answer ``None`` if it refused.

    The fail-soft boundary, at the caller and never in the store (ADR-0227
    §12): the store stays strict — ``publish_json_artifact`` raises rather than
    dropping an artifact — so every OTHER caller keeps the strictness it was
    built for. Each caller passes its own shipped event name and fields.
    """
    try:
        return work()
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(logger, event, level=logging.WARNING, exc_info=True, **fields)
        return None


def bind_evidence_publishers(
    store: Any, capture_session_id: str, run_async: Any
) -> tuple[Callable[[Any, Mapping[str, Any]], None], Callable[[Any], None], dict[str, Any]]:
    """Real ``publish_check`` / ``publish_candidate`` seams (§5.6).

    CHECK banks the ambient report + solved gain plan; MEASURE banks the full
    candidate dict, which the store re-opens through
    ``MeasuredCrossoverCandidate.from_mapping`` — the same tamper check the
    apply path runs, so a candidate that cannot survive exact reopen never
    becomes reviewable. Artifact fingerprints land in the returned ``refs``
    mapping (persisted into the durable state for the status surface).

    Neither is fail-soft, and that is the shipped behaviour: a CHECK or MEASURE
    whose evidence did not land has nothing for the household to review.
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

    def publish_candidate(candidate: Any) -> None:
        _, artifact = _bank(records, run_async, candidate.to_dict())
        refs["candidate_artifact"] = artifact.fingerprint
        log_event(
            logger,
            "correction.crossover_v2_candidate_published",
            capture_session_id=capture_session_id,
            candidate_fingerprint=candidate.fingerprint,
            artifact_fingerprint=artifact.fingerprint,
        )

    return publish_check, publish_candidate, refs


@dataclass
class _TakeRetention:
    store: Any
    refs: dict[str, Any]
    provenance: CaptureProvenanceRecorder | None = None
    evidence: CaptureEvidenceCarry | None = None
    layout: str | None = None
    pending: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __call__(self, result: Any, metadata: Mapping[str, Any]) -> str:
        self.pending.update(metadata)
        return ""

    def enrich(self, _answer: Any, _record: Mapping[str, Any]) -> Mapping[str, Any]:
        record = dict(self.pending)
        self.pending.clear()
        carried = self.provenance.take() if self.provenance else None
        if carried is not None:
            record["provenance"] = carried.to_dict()
            record["stimulus_wav_sha256"] = carried.stimulus_wav_sha256
        blocks = self.evidence.take() if self.evidence else None
        if blocks:
            record = {**blocks, **record}
        return enrich_capture_record({**_record, **record}, layout=self.layout)

    def after_bank(self, record: Mapping[str, Any], record_id: str) -> None:
        from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT

        artifact = self.store.identify_artifact(f"{EVIDENCE_ROOT}/artifacts/{record_id}")
        self.refs.setdefault("position_artifacts", []).append({
            "position_id": str(record.get("position_id") or record.get("pose_id") or ""),
            "attempt": int(record.get("attempt") or 0),
            "take_id": str(record.get("take_id") or ""),
            "artifact": artifact.fingerprint,
            "wav_path": str(record.get("wav_path") or ""),
            "wav_sha256": str(record.get("wav_sha256") or ""),
        })


def bind_position_retention(
    store: Any, refs: dict[str, Any], *,
    provenance: CaptureProvenanceRecorder | None = None,
    evidence: CaptureEvidenceCarry | None = None,
    layout: str | None = None,
) -> _TakeRetention:
    return _TakeRetention(store, refs, provenance, evidence, layout)


def v2_session_identity(store: Any, capture_session_id: str) -> Any:
    """This v2 session's cross-store identity (attribution plan §6).

    The **bundle** session id is canonical, because Q-C's bundle-lifetime
    ruling makes the bundle the retention unit: identity and lifetime then
    name the same thing, which is what keeps a finding from outliving its
    evidence. The capture-session id is real and is minted *after* the
    bundle — it is not derivable from it — so it rides as an alias rather
    than as a second identity. Before this, the only join between the two
    namespaces was one key in the durable state file, and the capture ring
    carried neither.
    """

    from jasper.attribution.session_identity import (
        ALIAS_CAPTURE_SESSION_ID,
        SessionIdentity,
    )

    return SessionIdentity(
        session_id=str(store.session_id),
        aliases={ALIAS_CAPTURE_SESSION_ID: str(capture_session_id)},
    )


def _publish_findings(
    records: Any,
    run_async: Any,
    phase: str,
    result: Mapping[str, Any],
    cloud_artifact: Any,
    refs: dict[str, Any],
) -> None:
    """Promote this group's excluded-band records to findings and persist them.

    WO-1's write half. The findings cite the cloud artifact **that was just
    banked** — the exact bytes the carve-out records were read from — so
    the citation is verifiable and, being a bundle artifact, is bound to the
    same lifetime the finding is (Q-C).

    **Fail-soft, like ``bank_take`` and unlike ``publish_cloud``**, which
    deliberately lets the strict store's refusals surface so the conductor's
    own boundary handles them. Findings are different:
    plan §3.4 makes them *optional evidence artifacts* — "a session with no
    findings behaves exactly as it does today" — so a findings failure must
    not turn a successfully-banked cloud group into a logged failure. The
    cloud artifact above is already durable by the time this runs.
    """

    from jasper.attribution.findings import FindingSet
    from jasper.attribution.promotion import PRODUCED_BY, promote_carve_outs
    from jasper.attribution.storage import bundle_evidence_ref

    capture_session_id = records.capture_session_id

    def _publish() -> tuple[Any, int]:
        identity = v2_session_identity(records.evidence, capture_session_id)
        findings = promote_carve_outs(
            result.get("carve_outs"),
            session=identity,
            cites=(bundle_evidence_ref(cloud_artifact, identity),),
        )
        return _bank_findings(
            records, run_async, phase=phase,
            finding_set=FindingSet(
                session=identity,
                produced_by=PRODUCED_BY,
                findings=findings,
            ),
        ), len(findings)

    published = _fail_soft(
        _publish,
        event="correction.crossover_v2_findings_publish_failed",
        capture_session_id=capture_session_id,
        phase=phase,
    )
    if published is None:
        return
    artifact, findings_banked = published
    refs.setdefault("finding_artifacts", {})[phase] = artifact.fingerprint
    log_event(
        logger,
        "correction.crossover_v2_findings_published",
        capture_session_id=capture_session_id,
        phase=phase,
        findings=findings_banked,
    )


def bind_cloud_publisher(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[str, Mapping[str, Any]], None]:
    """The real ``publish_cloud`` seam (flat-linearization plan PR-4).

    One JSON artifact PER CLOSED GROUP — ``crossover_v2/<session>/<phase>.json``
    (``cloud_measure.json`` / ``cloud_verify.json``), never a single shared
    ``cloud.json`` across both groups: the store is write-once and the
    pre-apply and post-apply groups close at genuinely different times in the
    SAME session, so a shared path would collide on the second group's write.
    This is a mechanism deviation from the work order's literal
    ``crossover_v2/<session>/cloud.json`` path, recorded here rather than
    silently matched — the per-group content (mask/registry/spec/geometry) is
    exactly what was asked for either way.

    Fail-soft at the CALLER: a full disk or a write-once conflict must surface
    as an exception here so the caller's own boundary can log and continue.
    """
    from jasper.active_speaker.crossover_v2.record_store import CLOUD_EVIDENCE_KIND

    records = _record_store(store, capture_session_id)

    def publish_cloud(phase: str, result: Mapping[str, Any]) -> None:
        _, artifact = _bank(records, run_async, {
            "kind": CLOUD_EVIDENCE_KIND, "phase": phase, **dict(result),
        })
        cloud_artifacts = refs.setdefault("cloud_artifacts", {})
        cloud_artifacts[phase] = artifact.fingerprint
        _publish_findings(records, run_async, phase, result, artifact, refs)

    return publish_cloud


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
