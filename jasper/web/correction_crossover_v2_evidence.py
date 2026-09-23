# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session evidence publication and production bindings."""

from __future__ import annotations

from jasper.json_fields import finite_float

from jasper.active_speaker.crossover_v2 import durable_state as v2durable
from jasper.active_speaker.crossover_v2.capture_provenance import analysis_provenance, enrich_capture_record
from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
from jasper.web import correction_crossover_v2_volume as v2volume


import dataclasses
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, TypeVar

from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE, PHASE_CLOUD_MEASURE
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


def bind_round_receipt(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[Mapping[str, Any]], str]:
    """The conductor's ``publish_round_receipt`` seam (#2291).

    Banks ONE immutable receipt per round, which puts it beside ``check.json``,
    ``candidate.json`` and the retained positions — the artifacts its own
    ``evidence_identities`` name, which is what makes them resolvable at all.
    The store runs the R21 accept-receipt reopen-and-compare at the write
    (``record_store._verify_receipt``).

    Raises rather than swallowing: the fail-soft boundary is the round
    coordinator's own receipt writer
    (:func:`jasper.active_speaker.crossover_v2.coordinator.run_round` catches
    it), the same shape :func:`bind_cloud_publisher` takes.
    """
    records = _record_store(store, capture_session_id)

    def publish_round_receipt(receipt: Mapping[str, Any]) -> str:
        _, artifact = _bank(records, run_async, dict(receipt))
        refs["round_receipt_artifact"] = artifact.fingerprint
        return str(artifact.fingerprint)

    return publish_round_receipt


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
    # No household projection here, deliberately — see
    # :func:`_bank_household_findings`. A carve-out finding's ``household_copy``
    # is COPIED from the carve-out record (``promote_carve_outs`` rule 3) rather
    # than minted, so the copy has an owner already: ``carve_outs_by_band``,
    # whose ``disclosure`` register is the chart callout's plain-language
    # headline (``cloud.js``'s ``buildCallout``) and whose ``expert`` register is
    # the τ/r line ``_carve_out_expert_lines`` folds into ``expert_details``.
    # Both render on both screens this would reach. The store keeps the full
    # record either way.
    log_event(
        logger,
        "correction.crossover_v2_findings_published",
        capture_session_id=capture_session_id,
        phase=phase,
        findings=findings_banked,
    )


def _bank_household_findings(
    store: Any, *, capture_session_id: str, phase: str, refs: dict[str, Any],
) -> None:
    """Reopen the finding set just published and project what a household reads.

    WO-1's **read** half (first-principles panel lens C, CC1): the flow banks a
    finding with validated household copy and, until this, nothing ever read one
    back — ``read_finding_set`` had zero non-test callers, so #1949's "bank a
    finding and proceed" was, in the household's experience, "proceed".

    **The read happens HERE, at publish, not at render, and that is a
    saturation decision.** The screens that show a finding are polled every
    1.5 s (``crossover/main.js``'s ``POLL_MS``), and a render-time read would
    re-open and re-hash the finding artifact AND its cited ``candidate.json``
    on every one of those polls, forever, on a Pi. It would also fail to reach
    the DONE screen at all: stage 2 opens a **new** bundle under a **new**
    capture session id (a verify-only prepare → ``open_v2_evidence_store``), so
    by the time the household sees the result screen, "this session's bundle"
    no longer holds the set the measuring session banked. Reading once and
    projecting the compact result into the durable state is the same shape
    ``compact_cloud_status`` already uses for the cloud's numbers: the bundle
    artifact stays the record; the state carries what a screen renders.

    **The read-back is itself the honesty check.** Going out through
    the record store and straight back in through ``read_finding_set``
    means only a set that survives the strict reopen — schema, session binding,
    and (``verify_evidence`` defaults True) a re-hash of every bundle citation
    — reaches a household. A finding whose support could not be confirmed
    raises ``FindingEvidenceMissing`` and is logged rather than rendered.

    **Order is the producer's, and nothing is de-duplicated.** The set's own
    order is preserved as persisted (``promote_carve_outs`` sorts by band;
    the level-frame path yields one), because re-ordering here would make this
    a second owner of a decision the producer already made. Two findings whose
    copy happens to read identically both render: dropping one would be this
    function silently deciding a banked finding does not exist, and "must not
    drop a finding" outranks a repeated sentence — a producer emitting the same
    sentence twice is a bug to fix at the producer.

    **Called from the level-frame path only.** ``_publish_findings``' carve-out
    sets are not projected: their ``household_copy`` is the carve-out record's
    own ``reason`` (``promote_carve_outs`` rule 3 copies it rather than minting
    it), so ``carve_outs_by_band`` is already that copy's owner and already
    renders the fact on both these screens — its ``disclosure`` register as the
    chart callout's plain-language headline (``cloud.js``'s ``buildCallout``),
    its ``expert`` register as the τ/r line ``_carve_out_expert_lines`` folds
    into ``expert_details``. Projecting it again would put one fact on one
    screen twice from two owners. When a producer mints copy that no other
    surface owns — as the level-frame path does, and as WO-4's detectors will —
    it calls this.

    Fail-soft, like every other findings path (plan §3.4: "a session with no
    findings behaves exactly as it does today"). A lost projection is a lost
    disclosure, never a lost tune.
    """

    from jasper.attribution.storage import read_finding_set

    try:
        finding_set = read_finding_set(
            store, capture_session_id=capture_session_id, phase=phase,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        log_event(
            logger,
            "correction.crossover_v2_findings_readback_failed",
            level=logging.WARNING,
            capture_session_id=capture_session_id,
            phase=phase,
            exc_info=True,
        )
        return
    if finding_set is None:
        return
    # ONE stamp for the whole set: every finding in it was banked by the same
    # publish, and the household-facing rendering of it is a date (see
    # ``crossover_envelope_v2._record_when_phrase``), so a per-finding clock
    # would be a precision the copy never spends and a second number to keep
    # honest. The store carries no timestamp of its own — this is the
    # finding's own clock, on the same epoch-float footing as
    # ``failure["at"]`` one level up.
    banked_at = time.time()
    projected = refs.setdefault(v2durable.FINDING_HOUSEHOLD_REFS_KEY, [])
    for finding in finding_set.findings:
        projected.append({
            # ``household_copy`` and nothing else. The mechanism id, the
            # evidence scalars, the confidence tier, and the probe lists are
            # INTERNAL taxonomy by ``findings.py``'s own two-vocabularies rule;
            # they stay in the bundle artifact and the journal, where an
            # operator reads them, and never cross onto a household wire.
            "household_copy": finding.household_copy,
            "at": banked_at,
        })
    log_event(
        logger,
        "correction.crossover_v2_findings_readback",
        capture_session_id=capture_session_id,
        phase=phase,
        findings=len(finding_set.findings),
    )


def bind_findings_publisher(
    store: Any, capture_session_id: str, refs: dict[str, Any], run_async: Any
) -> Callable[[Mapping[str, Any]], None]:
    """The real ``publish_findings`` seam — the #1866 frame-gate finding.

    The owner's 2026-07-30 ruling: a level-frame disagreement is BANKED as an
    M7 finding, and the session proceeds, when the realized-level check passes
    on the pair about to ship — a closed-loop read of the OUTCOME, not a
    referee between the two frames (see the flow's gate comment for why the
    distinction matters). The conductor decides and hands over an evidence
    record; this binder is the only thing that knows there is a store.

    **Its own phase, and that is forced rather than chosen.** The finding set
    lands at ``findings_measure.json``, beside the cloud groups'
    ``findings_cloud_measure.json`` / ``findings_cloud_verify.json``. The store
    is write-once and the cloud group's set is published at group CLOSE —
    several seconds and one household tap before the fit this finding comes out
    of even runs — so reusing that phase would be a PATH_CONFLICT, not a merge.
    The per-phase path already exists for exactly this reason (see
    :func:`~jasper.attribution.storage.findings_relative_path`), and the phase
    it takes is the flow phase the finding belongs to.

    **It cites ``candidate.json``**, the artifact
    :func:`bind_evidence_publishers`' ``publish_candidate`` wrote moments
    earlier, for three reasons: it is the thing the finding is ABOUT (the
    trims committed under a frame whose estimators disagreed), it carries the
    candidate's own per-role ``correction_giveback_db`` inside its ``linearization``
    block, and it is guaranteed to exist and to be durable at this point in the
    session — which a citation must be, since
    :func:`~jasper.attribution.storage.read_finding_set` re-hashes it on every
    read and raises when it cannot be confirmed.

    Fail-soft, like :func:`_publish_findings` and for the same §3.4 reason: the
    candidate is already published and the gate has already ruled that this
    session may proceed. A findings failure is a lost diagnosis, never a lost
    tune.
    """

    records = _record_store(store, capture_session_id)

    def publish_findings(record: Mapping[str, Any]) -> None:
        from jasper.active_speaker.commissioning_evidence_store import (
            EVIDENCE_ROOT,
        )
        from jasper.attribution.findings import FindingSet
        from jasper.attribution.promotion import (
            PRODUCED_BY_LEVEL_FRAME,
            promote_level_frame_disagreement,
        )
        from jasper.attribution.storage import bundle_evidence_ref

        def _publish() -> Any:
            identity = v2_session_identity(store, capture_session_id)
            finding = promote_level_frame_disagreement(
                record,
                session=identity,
                cites=(
                    bundle_evidence_ref(
                        store.identify_artifact(
                            f"{EVIDENCE_ROOT}/artifacts/crossover_v2/"
                            f"{capture_session_id}/candidate.json"
                        ),
                        identity,
                    ),
                ),
            )
            # A record the promoter refused is already logged by it, with the
            # reason. Banking an EMPTY set here would be a lie of a different
            # shape — "attribution ran and found nothing" — about a session
            # whose gate found something and said so in the journal.
            if finding is None:
                return None
            return _bank_findings(
                records, run_async, phase=PHASE_MEASURE,
                finding_set=FindingSet(
                    session=identity,
                    produced_by=PRODUCED_BY_LEVEL_FRAME,
                    findings=(finding,),
                ),
            )

        artifact = _fail_soft(
            _publish,
            event="correction.crossover_v2_findings_publish_failed",
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
        )
        if artifact is None:
            return
        refs.setdefault("finding_artifacts", {})[PHASE_MEASURE] = (
            artifact.fingerprint
        )
        log_event(
            logger,
            "correction.crossover_v2_findings_published",
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
            findings=1,
        )
        # The read half (CC1). Deliberately AFTER the publish log and outside
        # the try above: the set is durable at this point, so a read-back
        # failure must be reported as its own event rather than making a
        # successful publish look like a failed one.
        _bank_household_findings(
            store,
            capture_session_id=capture_session_id,
            phase=PHASE_MEASURE,
            refs=refs,
        )

    return publish_findings


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
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR

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
