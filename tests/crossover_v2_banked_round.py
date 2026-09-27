# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One banked round, as the FLOW banks it — the shared real-shape fixture.

A fixture library that IS a fixture library, on
``tests/crossover_v2_round_harness.py``'s precedent and for its reason: a
shared builder living in a collected test module makes that module
undeletable. This one is imported by the round-views and forward-model
suites.

**The two shapes are DISJOINT, and that is the finding they exist to hold.**
``jasper.web.correction_crossover_v2``'s own words: *"stage 2 opens a new
bundle under a new capture session id"*. So one ``bank-crossover-round.sh`` run
banks ONE stage, and:

* :func:`bank_measure_round` — stage 1. CHECK, the design-axis MEASURE take
  carrying both per-driver solos, the lateral walk pose(s), and the ENTRY BASELINE.
  Its flow state carries no VERIFY curve because stage 1 measures no VERIFY.
* :func:`bank_verify_round` — stage 2. The VERIFY take, and a flow state
  carrying ``verify_priors.verify_measured``. No per-driver solos: a verify
  stage walks none.
* :func:`bank_seat_round` — the ``seat/cube`` walk (ADR-0260): one
  ungated summed take per pose of the shipped program's own resolved walk, so
  a reader of categorized poses gets seven takes that differ only in where the
  microphone was. No solos and no VERIFY curve — a seat walk measures neither.

No round carries both a prediction basis and a measured VERIFY sum, which is
issue #3482's root fact; no round carries both an entry baseline and a graded
spec, which is #3478's.

**The cloud group is deliberately absent from BOTH.** No current run writes
one, so the readers that need a ``cloud_verify.json`` keep the payload builder
that already lives with them.

Measure and verify fixtures carry no WAVs. Seat fixtures retain captures for
the room analyzer; room statistics tests supply documents at its output.
"""

from __future__ import annotations

from tests.run_manifest_fixture import write_manifest

import asyncio
import json
import math
from dataclasses import dataclass, replace
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.audio_measurement.bundles import record_artifact
from jasper.audio_measurement.calibration import store_calibration
from jasper.audio_measurement.wired_capture import WiredCaptureAnswer
from jasper.active_speaker.capture_provenance import CaptureProvenance, CaptureProvenanceRecorder
from jasper.active_speaker.crossover_v2.wired_stimulus import CapturedRecordStore, place_wired_answer
from jasper.active_speaker.run_manifest import RunManifest
from jasper.active_speaker.plan_run import PlanCapture, run_plan
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
from jasper.active_speaker.crossover_v2.session import MeasureOutcome, StimulusOutcome
from jasper.web.correction_crossover_v2_evidence import bind_production_analyze
from jasper.web.correction_run_host import bind_plan_analysis
from tests.crossover_v2_fixtures import FakeSeams, _conductor, _measure_analysis, _verify_analysis
from tests.engine_twin import open_session
from jasper.audio_measurement.program import ExcitationProgram, build_verify_program, render_program_pcm
from jasper.audio_measurement.wired_capture import encode_wav_s32
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import (
    EVIDENCE_ROOT,
    CommissioningEvidenceStore,
)
from jasper.active_speaker import angle_capture, measurement_programs
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.capture_plan import position_geometry
from jasper.active_speaker.crossover_v2.contracts import (
    DESIGN_AXIS_DEG,
    DRIVER_ROLE_TWEETER,
    DRIVER_ROLE_WOOFER,
    REFERENCE_MARK_DESIGN_AXIS,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.durable_state import MAX_PERSISTED_SUM_POINTS
from jasper.active_speaker.crossover_v2.journey import (
    LATERAL_CONSUMER_FC_SELECTOR,
    LATERAL_CONSUMER_FORWARD_MODEL,
    PHASE_CHECK,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.record_store import (
    BankedRecordStore,
)
from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    SessionIdentity,
)

from tests.active_speaker_fixtures import mono_output_topology


#: The two SHAPES ``bank_measure_round`` can bank, spelled as the topology
#: fixture's own group modes so a round's bundle and its solos cannot disagree
#: about how many branches the speaker has.
__all__ = [
    "ENTRY_GRID_HZ",
    "MODE_TWO_WAY",
    "MODE_WAY1",
    "SEAT_BAND_HZ",
    "SEAT_GRID_HZ",
    "SOLO_BAND_HZ",
    "SOLO_GRID_HZ",
    "VERIFY_GRID_HZ",
    "bank_measure_round",
    "bank_seat_round",
    "bank_verify_round",
]


MODE_TWO_WAY = "active_2_way"
MODE_WAY1 = "full_range_passive"

#: The per-driver solos' grid and each driver's own swept band. One band for
#: both roles keeps ``BranchPair.sum_band_hz`` equal to it, so a pin can state
#: the compared span without re-deriving a union.
SOLO_BAND_HZ = (200.0, 12000.0)
SOLO_GRID_HZ = np.linspace(SOLO_BAND_HZ[0], SOLO_BAND_HZ[1], 256)

#: The entry baseline's own reduced grid — log-spaced across all three
#: ``flat_spec.SPEC_BANDS`` rows so a grade has bins in every band.
ENTRY_GRID_HZ = np.geomspace(280.0, 16000.0, 90)

#: The grid the VERIFY capture's own banked curve sits on. Deliberately NOT
#: the solos' grid: the persisted VERIFY pair is on the capture's own
#: frequencies, and a reader that handed one back for the other would pass a
#: same-grid fixture.
VERIFY_GRID_HZ = np.geomspace(SOLO_BAND_HZ[0], SOLO_BAND_HZ[1], 301)

#: A seat take is the room's own measurement, full-band on the shared basis
#: every retained pose curve is sampled onto.
SEAT_BAND_HZ = spatial.LATERAL_EVIDENCE_BAND_HZ
SEAT_GRID_HZ = spatial.lateral_evidence_grid_hz()

#: Where the fixture's two synthetic branches cross. A SHAPE knob, not a
#: measured corner: it only has to sit inside :data:`SOLO_BAND_HZ` so the
#: summed prediction has a crossover region inside the compared span.
_CROSSOVER_HZ = 1800.0
_CAPTURE_SESSION_ID = "capture-1"


def _lr4(freqs_hz: np.ndarray, *, highpass: bool) -> np.ndarray:
    """One LR4 branch: an in-phase aligned pair sums flat through Fc."""
    s = 1j * (np.asarray(freqs_hz, dtype=float) / _CROSSOVER_HZ)
    butter2 = (s**2 if highpass else 1.0) / (s**2 + math.sqrt(2.0) * s + 1.0)
    return butter2**2


def _pose_curves(mode: str) -> tuple[spatial.LateralPoseCurve, ...]:
    """This shape's solos as the curve value both banked shapes carry.

    A 1-way main walks ONE routed solo and declares no corner, so its branch is
    unity across the band rather than half of an LR4 pair.
    """
    branches = (
        (("full_range", np.ones_like(SOLO_GRID_HZ, dtype=complex)),)
        if mode == MODE_WAY1 else (
            (DRIVER_ROLE_WOOFER, _lr4(SOLO_GRID_HZ, highpass=False)),
            (DRIVER_ROLE_TWEETER, _lr4(SOLO_GRID_HZ, highpass=True)),
        )
    )
    return tuple(
        spatial.LateralPoseCurve(
            role=role, freqs_hz=SOLO_GRID_HZ, complex_tf=tf, band_hz=SOLO_BAND_HZ,
        )
        for role, tf in branches
    )


def _solo_curves(mode: str) -> list[dict[str, Any]]:
    """This shape's per-driver solos, through the ONE banked-curve serializer."""
    return [spatial.pose_curve_record(curve) for curve in _pose_curves(mode)]


def _tilted(
    curves: tuple[spatial.LateralPoseCurve, ...], db_per_octave: float,
) -> tuple[spatial.LateralPoseCurve, ...]:
    """The same solos through a per-octave tilt — one LADDER rung's shape.

    A tilt rather than a gain: two candidates that differ only in level are
    indistinguishable to any reader that normalises level away, so a fixture
    built from those would pass a comparison that computed nothing.
    """
    if not db_per_octave:
        return curves
    return tuple(
        spatial.LateralPoseCurve(
            role=curve.role, freqs_hz=curve.freqs_hz,
            complex_tf=curve.complex_tf * 10.0 ** (
                db_per_octave
                * np.log2(curve.freqs_hz / curve.band_hz[0]) / 20.0
            ),
            band_hz=curve.band_hz,
        )
        for curve in curves
    )


def _open_round(
    root: Path, name: str, mode: str,
) -> tuple[Path, BankedRecordStore, str]:
    """``<round-dir>/bundle/<session-id>/`` with a real evidence store on it.

    The directory layout is ``bank-crossover-round.sh``'s: the whole session
    bundle untarred under ``bundle/``, with the flow state written beside it
    by the callers below.
    """
    round_dir = Path(root) / name
    info = open_bundle(
        mono_output_topology(mode=mode),
        calibration_id="calibration-test",
        sessions_dir=round_dir / "bundle",
    )
    assert info is not None, "open_bundle refused to open a fixture bundle"
    store = CommissioningEvidenceStore.open(
        Path(str(info["bundle_dir"])), expected_session_id=str(info["session_id"]),
    )
    return round_dir, BankedRecordStore(
        evidence=store, capture_session_id=_CAPTURE_SESSION_ID,
    ), str(info["session_id"])


def _bank(store: BankedRecordStore, *records: Mapping[str, Any]) -> None:
    """Every record through the store that writes it, in bank order."""

    async def _run() -> None:
        for record in records:
            await store.bank(record)

    asyncio.run(_run())


def _receipt(round_id: str) -> dict[str, Any]:
    return {
        "kind": ROUND_RECEIPT_KIND,
        "schema_version": 2,
        "round_id": round_id,
        "entry_graph_fingerprint": "fp-entry-graph",
    }


def _state(
    *,
    round_ordinal: int,
    verify_measured: Any,
) -> dict[str, Any]:
    """The flow state ``bank-crossover-round.sh`` drops beside the bundle.

    Narrowed to the keys the readers under test open — the round's place in
    the series, and ``verify_priors.verify_measured``, whose value comes from
    the persist-time writer rather than a hand-typed dict. ``verify_priors``
    is rebuilt from the conductor on every persist, so a ``None`` here is what
    a stage that measured no VERIFY actually banks.
    """
    return {
        "session_id": _CAPTURE_SESSION_ID,
        "round_receipt": {"round_ordinal": round_ordinal},
        "round_ordinal_epoch": 1,
        "verify_priors": {"verify_measured": verify_measured},
    }


# --------------------------------------------------------------------------- #
# the flow-banked take shape (moved from spatial/records.py: no product path
# calls these any more — session.TuningSession._record and
# run_manifest.RunManifest.allocate_take_id are the engine's own producers)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LateralPose:
    """One accepted pose in the lateral walk.

    Carries NO trim, delay, polarity or fit, structurally: re-solving any of
    them per pose is forbidden, and there is no field here to write one to.

    ``pose_id`` is the canonical key for a POSE on every surface.
    ``position_id`` / ``position_index`` answer a different question — which
    slot of a walk — and joining takes on ``position_id`` mixes poses into the
    seat table.
    """

    pose_id: str
    index: int
    attempt: int
    prompt: str
    role: str
    offset_cm: float
    at_mark: bool
    curves: tuple[spatial.LateralPoseCurve, ...]

    def curve(self, role: str) -> spatial.LateralPoseCurve | None:
        for curve in self.curves:
            if curve.role == role:
                return curve
        return None


def pose_kind_fields(
    geometry: spatial.PositionGeometry, *, gating_applied: bool | None = None,
) -> dict[str, Any]:
    """Measured geometry and analysis facts shared by retained takes."""
    return {
        "mark_distance_m": geometry.mark_distance_m,
        **({"gating_applied": gating_applied} if gating_applied is not None else {}),
        **({
            "pose_kind": geometry.kind,
            "seat_offset_m": list(geometry.seat_offset_m) if geometry.seat_offset_m is not None else None,
        } if geometry.kind != measurement_programs.POSE_KIND_BEARING else {}),
    }


def take_id_for(position_id: str, attempt: int) -> str:
    """One take's id, as every builder that mints one spells it.

    A geometry retake reuses the position id, so the position id alone does not
    identify a take. Zero-padded so a lexical sort of the bundle is also a
    chronological one.
    """
    return f"{position_id}_a{int(attempt):02d}"


@dataclass(frozen=True)
class TakeClaim:
    """What the SESSION claimed around one take, on every record it banks.

    Carried at the builders so a flow-banked take and an engine-banked take are
    one record shape. Every field defaults empty because an unstated field is an
    honest fact about the capture, never a refusal to bank it.

    ``level_db`` is the PROVEN fader level and ``stimulus_dbfs`` is the ladder
    rung the stimulus played at — two quantities on purpose, since a ladder
    moves the stimulus and never the claim. ``level_db`` is optional here where
    an engine-banked record's is not: the flow's retention sites hold no volume
    claim, so ``None`` says exactly that rather than inviting an invented
    number. ``stimulus_dbfs`` is ``None`` when no ladder was asked for.

    ``wav_path`` is the record → capture pointer, bundle-relative, and is NOT
    derivable from ``take_id`` (``bundles.capture_artifact_relpath`` appends a
    ``uuid4`` hex).
    """

    measure_kind: str = ""
    baseline_record_id: str = ""
    candidate_id: str = ""
    polarity: str = ""
    #: Whether the graph this take played through carried the box's own
    #: per-driver level match, and by how much: a reverse-null pair is only
    #: comparable to a reader who knows whether the branches were levelled
    #: before they were summed. ``False``/``None`` on a take that declared none.
    level_matched: bool = False
    level_match_trims_db: Mapping[str, float] | None = None
    level_db: float | None = None
    stimulus_dbfs: float | None = None
    incident: str = ""
    wav_path: str = ""
    #: Which of :func:`~jasper.active_speaker.crossover_v2.spatial.phase_composition`'s
    #: two words the banked curves carry. ``""`` where it says neither, and
    #: ABSENT from the record there: an unstated composition must not read as
    #: either one.
    phase_composition: str = ""


def _take_identity(
    *,
    position_id: str,
    phase: str,
    index: int,
    attempt: int,
    session_id: str,
    wav_sha256: str | None,
    graph_fingerprint: str = "",
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """The identity block every retained take carries, whatever kind it is.

    The common core; each builder adds its own role-tagged extension rather than
    sharing one shape with half its columns null. Deliberately NOT emitted here:
    the id key itself — a cloud position calls it ``position_id`` and a pose
    calls it ``pose_id``, which are two questions.

    ``wav_sha256`` is the capture's content digest: the VERIFIER for a replay,
    never the index. Recorded whether or not any store retained the bytes.
    ``claim.wav_path`` is its pointer sibling.
    """
    return {
        "phase": phase,
        "index": index,
        "attempt": attempt,
        "take_id": take_id_for(position_id, attempt),
        "session_id": session_id,
        "wav_sha256": wav_sha256,
        "measure_kind": claim.measure_kind,
        "graph_fingerprint": graph_fingerprint,
        "baseline_record_id": claim.baseline_record_id,
        "candidate_id": claim.candidate_id,
        "polarity": claim.polarity,
        "level_matched": claim.level_matched,
        # The numbers only when there ARE numbers: an absent key reads as an
        # un-matched take, so no schema version moves.
        **(
            {"level_match_trims_db": dict(claim.level_match_trims_db)}
            if claim.level_matched and claim.level_match_trims_db
            else {}
        ),
        # Stated or absent, never a guessed default: see TakeClaim.
        **(
            {"phase_composition": claim.phase_composition}
            if claim.phase_composition
            else {}
        ),
        "level_db": claim.level_db,
        "stimulus_dbfs": claim.stimulus_dbfs,
        "incident": claim.incident,
        "wav_path": claim.wav_path,
    }


def cloud_position_record(
    *,
    position_id: str,
    phase: str,
    index: int,
    attempt: int,
    prompt: str,
    wide: bool,
    role: str,
    geometry: spatial.PositionGeometry,
    captured_at: float,
    session_id: str,
    gate_window_ms: float | None,
    gate_floor_source: str | None,
    gate_disclosure: str | None,
    gate_moved_rms_db: float | None,
    gate_reflection_delay_ms: float | None,
    gate_entanglement_floor_hz: float | None,
    gate_entanglement_floor_source: str,
    validity_floor_hz: float | None,
    gating_applied: bool,
    summed_ripple_db: float | None,
    glitch_detected: bool,
    wav_sha256: str | None,
    graph_fingerprint: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One retained cloud position, as a banked record.

    ``take_id`` is minted here so the session's evidence and the bundle's
    sidecar path name the same take.

    ``gate_floor_source`` records WHY the gate window is what it is (#1966);
    ``gating_applied`` alone cannot distinguish a window that stops at a found
    reflection from one capped at the search bound. ``gate_disclosure`` is the
    same fact as a sentence.

    ``gate_moved_rms_db`` and ``gate_reflection_delay_ms`` are the two numbers
    that sentence narrates, from the same
    :mod:`~jasper.audio_measurement.gate_disclosure` record, so digits and
    prose share a derivation. Both are ``None`` on an ungateable capture, and
    the delay is ``None`` — never 0.0 — on a window capped at the search
    ceiling. The delay is RELATIVE to the direct arrival, not the gating block's
    absolute ``first_reflection_ms``.

    ``gate_entanglement_floor_hz`` is the ROOM's floor at THIS seat and
    ``gate_entanglement_floor_source`` says which of
    :data:`~jasper.audio_measurement.gating.ENTANGLEMENT_SOURCES` timed it —
    never one without the other (#3502). Banked per SEAT because it is derived
    at the seat's own ``mark_distance_m``. ``unknown`` with a null floor is
    ordinary on a rig whose first bounce lands while the direct sound is still
    decaying.

    ``regime`` is WHAT PLAYED, in the walk seam's vocabulary
    (:data:`LATERAL_POSE_REGIME` is the other word in it), ``""`` until a caller
    states it. That vocabulary is NOT :data:`~.contracts.MEASURE_REGIMES`',
    which the engine's record spells under the same key — two vocabularies, one
    key name.

    ``geometry`` is WHERE the microphone was, as fields rather than English:
    ``position_deg`` (``None`` where no bearing was commanded),
    ``position_axis``, ``vertical_deg`` and ``mark_distance_m``, stamped from
    the pose the operator was given, with ``prompt`` beside them as the human
    instruction rather than the source of truth. ``vertical_deg`` is absent from
    older records and a reader takes that absence as 0. See
    :class:`~jasper.active_speaker.crossover_v2.spatial.PositionGeometry` for
    the frame.

    ``curves`` is WHAT WAS MEASURED, in
    :func:`~jasper.active_speaker.crossover_v2.spatial.pose_curve_record`'s shape.
    """
    return {
        "position_id": position_id,
        **_take_identity(
            position_id=position_id, phase=phase, index=index, attempt=attempt,
            session_id=session_id, wav_sha256=wav_sha256,
            graph_fingerprint=graph_fingerprint, claim=claim,
        ),
        "prompt": prompt,
        "regime": regime,
        "wide": wide,
        # The position's named question: the prompt string alone cannot be
        # parsed back into a role, so the label rides the record explicitly.
        "role": role,
        "position_deg": geometry.degrees,
        "position_axis": geometry.axis,
        "vertical_deg": geometry.vertical_deg,
        "captured_at": captured_at,
        "gate_window_ms": gate_window_ms,
        "gate_floor_source": gate_floor_source,
        "gate_disclosure": gate_disclosure,
        "gate_moved_rms_db": gate_moved_rms_db,
        "gate_reflection_delay_ms": gate_reflection_delay_ms,
        "gate_entanglement_floor_hz": gate_entanglement_floor_hz,
        "gate_entanglement_floor_source": gate_entanglement_floor_source,
        "validity_floor_hz": validity_floor_hz,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
        **pose_kind_fields(geometry, gating_applied=gating_applied),
    }


#: What every :data:`~jasper.active_speaker.crossover_v2.journey.PHASE_LATERAL`
#: pose plays: the anchor's interleaved per-driver MEASURE object. A literal
#: copy of :data:`jasper.active_speaker.angle_capture.REGIME_PER_DRIVER`,
#: pinned equal by test.
LATERAL_POSE_REGIME = "per_driver"


def lateral_pose_record(
    pose: LateralPose,
    *,
    geometry: spatial.PositionGeometry,
    lateral_consumer: str,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    claim: TakeClaim = TakeClaim(),
    gating_applied: bool | None = None,
) -> dict[str, Any]:
    """One lateral capture with its actual pose, purpose and analyzed curves."""
    if geometry.degrees is None:
        raise ValueError("a lateral pose commands a horizontal bearing; this geometry declares none")
    return {
        "pose_id": pose.pose_id,
        **_take_identity(
            position_id=pose.pose_id, phase=PHASE_LATERAL, index=pose.index,
            attempt=pose.attempt, session_id=session_id, wav_sha256=wav_sha256,
            graph_fingerprint=graph_fingerprint, claim=claim,
        ),
        "prompt": pose.prompt,
        "role": pose.role,
        "position_deg": int(geometry.degrees),
        "position_axis": spatial.POSITION_AXIS_HORIZONTAL,
        "vertical_deg": int(geometry.vertical_deg),
        "offset_cm": float(pose.offset_cm),
        "at_mark": bool(pose.at_mark),
        "regime": LATERAL_POSE_REGIME,
        "lateral_consumer": lateral_consumer,
        "captured_at": captured_at,
        "curves": [spatial.pose_curve_record(curve) for curve in pose.curves],
        **pose_kind_fields(geometry, gating_applied=gating_applied),
    }


def phase_capture_record(
    *,
    phase: str,
    index: int,
    attempt: int,
    session_id: str,
    graph_fingerprint: str,
    captured_at: str,
    wav_sha256: str | None,
    prompt: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """One banked take for a phase that prompts no spot: CHECK, MEASURE, VERIFY.

    These play from wherever the microphone already is, so a take records the
    CAPTURE: its digest, the identity that finds it again, and ``curves``.

    The curves are the only part of the analysis this record keeps: a round's
    verdicts are rewritten inside the round, but the complex responses they were
    drawn from land in no file unless they land here. CHECK banks an empty list
    because it computes no transfer function; an empty list is "no curve banked"
    and never "this capture was clean".

    The take id follows the entry baseline's convention — the position id is
    minted from phase and index, so it IS the take id once :func:`take_id_for`
    qualifies it by attempt.

    The pose is
    :data:`~jasper.active_speaker.crossover_v2.contracts.DESIGN_AXIS_DEG` on
    the horizontal axis, which is the reading ``session.TuningSession._bearings``
    gives a spec naming no position, so one pose is one record on both sides.
    ``prompt`` is ``""`` because no instruction was issued, a different fact
    from an unknown one; ``regime`` is the caller's to state and is never
    guessed from the phase.
    """
    identity = _take_identity(
        position_id=f"{phase}_{index:02d}",
        phase=phase, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # No prompted spot of its own, so the position id IS the take id.
        "position_id": identity["take_id"],
        **identity,
        "captured_at": captured_at,
        "prompt": prompt,
        "regime": regime,
        "position_deg": DESIGN_AXIS_DEG,
        "position_axis": spatial.POSITION_AXIS_HORIZONTAL,
        "vertical_deg": 0,
        "curves": [dict(curve) for curve in curves],
    }


def entry_baseline_record(
    *,
    index: int,
    attempt: int,
    session_id: str,
    program_id: str,
    reference_mark: str,
    graph_fingerprint: str,
    captured_at: str,
    freqs_hz: Sequence[float],
    magnitude_db: Sequence[float],
    excluded: Sequence[bool],
    validity_floor_hz: float | None,
    gate_window_ms: float | None,
    summed_ripple_db: float | None,
    glitch_detected: bool,
    wav_sha256: str | None,
    prompt: str = "",
    regime: str = "",
    curves: Sequence[Mapping[str, Any]] = (),
    claim: TakeClaim = TakeClaim(),
) -> dict[str, Any]:
    """The entry baseline's retained record — a cloud position's shape, minus
    the group, plus the curve.

    Three fields a cloud position has no use for make THIS capture comparable to
    the post-apply one, and are why it is a separate builder: WHAT was played
    (``program_id``), WHERE from (``reference_mark``), and WHICH graph it went
    through (``graph_fingerprint``).

    The reduced curve rides here, which is what makes this the DURABLE copy:
    a retained take is write-once and keyed by ``take_id``, while the flow state
    file holding the same arrays is rewritten on every persist. Bounded at
    ``round_evidence.BENEFIT_CURVE_MAX_BINS`` upstream. Same three arrays and
    names as ``round_evidence.EntryBaseline.to_dict``, so one reader covers both.

    ``curves`` is a SECOND curve on a second basis, not a copy: the three arrays
    are the GRADED side (decimated, magnitude only, carrying the ``excluded``
    mask), ``curves`` is the MEASURED side on the shared log basis with phase.
    Neither is derivable from the other.

    The pose is
    :data:`~jasper.active_speaker.crossover_v2.contracts.DESIGN_AXIS_DEG` on
    the horizontal axis, as for every capture with no prompted move.
    ``reference_mark`` says where that axis was measured from; ``prompt`` is
    ``""`` because no instruction was issued.
    """
    identity = _take_identity(
        position_id=f"{PHASE_ENTRY_BASELINE}_{index:02d}",
        phase=PHASE_ENTRY_BASELINE, index=index, attempt=attempt,
        session_id=session_id, wav_sha256=wav_sha256,
        graph_fingerprint=graph_fingerprint, claim=claim,
    )
    return {
        # No prompted spot of its own, so the position id IS the take id.
        "position_id": identity["take_id"],
        **identity,
        "program_id": program_id,
        "reference_mark": reference_mark,
        "prompt": prompt,
        "position_deg": DESIGN_AXIS_DEG,
        "position_axis": spatial.POSITION_AXIS_HORIZONTAL,
        "vertical_deg": 0,
        "regime": regime,
        "captured_at": captured_at,
        "freqs_hz": [float(hz) for hz in freqs_hz],
        "magnitude_db": [float(db) for db in magnitude_db],
        "excluded": [bool(flag) for flag in excluded],
        "validity_floor_hz": validity_floor_hz,
        "gate_window_ms": gate_window_ms,
        "summed_ripple_db": summed_ripple_db,
        "glitch_detected": glitch_detected,
        "curves": [dict(curve) for curve in curves],
    }


#: The pose a capture with no prompted move of its own was taken at.
_DESIGN_AXIS_GEOMETRY = spatial.PositionGeometry(
    axis=spatial.POSITION_AXIS_HORIZONTAL,
    degrees=0,
    mark_distance_m=spatial.MARK_DISTANCE_M,
)


def bank_measure_round(
    root: Path,
    *,
    name: str = "r1-measure",
    entry_baseline_db: np.ndarray | None = None,
    entry_excluded: Sequence[bool] | None = None,
    round_ordinal: int = 1,
    mode: str = MODE_TWO_WAY,
    candidates: Sequence[str] = (),
) -> Path:
    """One STAGE-1 round directory, as the flow banks it.

    CHECK, the design-axis MEASURE take carrying both per-driver solos, the
    lateral walk pose(s), and the entry baseline — plus the round receipt and
    the flow state. No cloud group and therefore no graded ``spec``: this is the
    only round shape that produces an entry baseline, and it is the shape the
    forward model's own worked example points at.

    ``entry_baseline_db`` defaults to a flat -20 dB curve on
    :data:`ENTRY_GRID_HZ`. ``mode`` picks the SHAPE: :data:`MODE_TWO_WAY` walks
    both solos, :data:`MODE_WAY1` the one a subless passive main has.

    ``candidates`` makes the walk a LADDER: one lateral pose per named
    candidate at the SAME bearing, each a rung further tilted, which is the
    shape the candidate cycle banks — one pose held while the graph swaps
    under it. Empty walks the single unattributed pose a round with no ladder
    banks.
    """
    round_dir, store, session_id = _open_round(root, name, mode)
    magnitude_db = (
        np.full(ENTRY_GRID_HZ.shape, -20.0)
        if entry_baseline_db is None else np.asarray(entry_baseline_db, dtype=float)
    )
    excluded = (
        [False] * int(ENTRY_GRID_HZ.size)
        if entry_excluded is None else [bool(flag) for flag in entry_excluded]
    )
    stamp = {
        "session_id": session_id,
        "graph_fingerprint": "fp-entry-graph",
        "captured_at": "2026-08-31T22:00:00Z",
        "wav_sha256": "a" * 64,
    }
    poses = [
        lateral_pose_record(
            LateralPose(
                pose_id=f"lateral_{3 + rung:02d}", index=3 + rung, attempt=1,
                prompt="", role="", offset_cm=0.0, at_mark=True,
                curves=_tilted(_pose_curves(mode), float(rung)),
            ),
            geometry=spatial.PositionGeometry(
                spatial.POSITION_AXIS_HORIZONTAL, 7, spatial.MARK_DISTANCE_M,
            ),
            lateral_consumer=LATERAL_CONSUMER_FC_SELECTOR,
            claim=TakeClaim(candidate_id=candidate_id), **stamp,
        )
        for rung, candidate_id in enumerate(candidates or ("",))
    ]
    _bank(
        store,
        # CHECK computes no transfer function, so it banks an empty curve list
        # — the writer's own shape, not an omission here.
        phase_capture_record(
            phase=PHASE_CHECK, index=1, attempt=1, curves=(), **stamp,
        ),
        phase_capture_record(
            phase=PHASE_MEASURE, index=2, attempt=1, curves=_solo_curves(mode),
            **stamp,
        ),
        # At least one lateral pose, so a reader selecting the MEASURE take
        # has a sibling take of another phase to pass over rather than a clear
        # field.
        *poses,
        entry_baseline_record(
            index=3 + len(poses), attempt=1,
            program_id="prog-entry", reference_mark=REFERENCE_MARK_DESIGN_AXIS,
            freqs_hz=ENTRY_GRID_HZ, magnitude_db=magnitude_db, excluded=excluded,
            # Capture SCALARS the record carries and every reader in these
            # suites drops (read_entry_baseline_take narrows to the identity
            # and the three arrays). Plausible values so the record is whole,
            # never a number any pin reads.
            validity_floor_hz=200.0, gate_window_ms=7.0, summed_ripple_db=1.0,
            glitch_detected=False, **stamp,
        ),
        _receipt("r1"),
    )
    (round_dir / "state.json").write_text(
        json.dumps(_state(round_ordinal=round_ordinal, verify_measured=None))
    )
    write_manifest(round_dir, program="room" if name == "r3-seat" else "speaker")
    return round_dir


def bank_verify_round(
    root: Path,
    *,
    name: str = "r2-verify",
    measured_db: np.ndarray | None = None,
    round_ordinal: int = 2,
) -> Path:
    """One STAGE-2 round directory, as the flow banks it.

    The VERIFY take, plus a flow state carrying the VERIFY capture's own
    measured curve. It banks NO per-driver solos, so it can supply a
    forward-model comparison's measured half and never its prediction basis.

    ``measured_db`` defaults to a flat -30 dB curve on :data:`VERIFY_GRID_HZ`.
    """
    round_dir, store, session_id = _open_round(root, name, MODE_TWO_WAY)
    measured = (
        np.full(VERIFY_GRID_HZ.shape, -30.0)
        if measured_db is None else np.asarray(measured_db, dtype=float)
    )
    stamp = {
        "session_id": session_id,
        "graph_fingerprint": "fp-applied-graph",
        "captured_at": "2026-08-31T23:00:00Z",
        "wav_sha256": "c" * 64,
    }
    _bank(
        store,
        phase_capture_record(
            phase=PHASE_VERIFY, index=1, attempt=1,
            curves=[
                spatial.pose_curve_record(
                    spatial.LateralPoseCurve(
                        role="summed", freqs_hz=VERIFY_GRID_HZ,
                        complex_tf=10.0 ** (measured / 20.0),
                        band_hz=SOLO_BAND_HZ,
                    )
                )
            ],
            **stamp,
        ),
        _receipt("r2"),
    )
    (round_dir / "state.json").write_text(json.dumps(_state(
        round_ordinal=round_ordinal,
        verify_measured=_decimate_verify_measured(
            (VERIFY_GRID_HZ, measured, np.zeros_like(measured))
        ),
    )))
    write_manifest(round_dir, program="room" if name == "r3-seat" else "speaker")
    return round_dir


def _seat_capture(
    store: BankedRecordStore, program: ExcitationProgram, index: int, magnitude: np.ndarray,
) -> dict[str, Any]:
    stimulus = render_program_pcm(program)[:, 0]
    spectrum = np.fft.rfft(stimulus)
    freqs = np.fft.rfftfreq(len(stimulus), 1 / program.sample_rate_hz)
    signal = np.fft.irfft(spectrum * 10 ** (np.interp(freqs, SEAT_GRID_HZ, magnitude) / 20), n=len(stimulus))
    signal = np.concatenate((np.zeros(800), signal, np.zeros(5000)))
    wav, _ = encode_wav_s32((signal * (2**31 - 1)).astype(np.int32), sample_rate_hz=program.sample_rate_hz)
    artifact = store.evidence.publish_raw_artifact(f"seat-{index}.wav", wav)
    record_artifact(store.evidence.bundle_dir, artifact.relative_path, kind="jts_measurement_capture",
                    sensitivity="raw_audio", recomputable=False, generated_by=__name__)
    return {"wav_path": artifact.relative_path, "wav_sha256": artifact.sha256,
            "program": program.to_dict(), "measurement_status": "captured",
            "graph_scope": "candidate", "candidate_id": "fixture-speaker"}


def bank_seat_round(
    root: Path,
    *,
    name: str = "r3-seat",
    magnitudes_db: Sequence[np.ndarray] | None = None,
    round_ordinal: int = 3,
) -> Path:
    """One SEAT round directory: the ``seat/cube`` walk, as the flow banks it.

    One lateral take per pose of the shipped program's OWN resolved walk, each
    ungated (a seat take keeps its reflections) and stated from the head
    rather than the mark, plus the round receipt and the flow state.

    ``magnitudes_db`` defaults to seven flat -30 dB curves on
    :data:`SEAT_GRID_HZ`, in the program's own pose order.
    """
    round_dir, store, session_id = _open_round(root, name, MODE_TWO_WAY)
    stops = angle_capture.resolve_request(
        angle_capture.request_for_program(measurement_programs.run_program("room", "seat_cube"))
    )
    magnitudes = (
        [np.full(SEAT_GRID_HZ.shape, -30.0)] * len(stops)
        if magnitudes_db is None
        else [np.asarray(magnitude, dtype=float) for magnitude in magnitudes_db]
    )
    stamp = {
        "session_id": session_id,
        "graph_fingerprint": "fp-applied-graph",
        "captured_at": "2026-09-01T00:00:00Z",
        "wav_sha256": "d" * 64,
    }
    program = build_verify_program(2500, sweep_band_hz=SEAT_BAND_HZ, sweep_s=0.5)
    _bank(
        store,
        *(
            {**lateral_pose_record(
                LateralPose(
                    pose_id=f"lateral_{stop.index:02d}", index=stop.index, attempt=1,
                    prompt=stop.prompt.text, role=stop.prompt.role,
                    offset_cm=0.0, at_mark=False,
                    curves=(
                        spatial.LateralPoseCurve(
                            role="summed", freqs_hz=SEAT_GRID_HZ,
                            complex_tf=10.0 ** (magnitude / 20.0),
                            band_hz=SEAT_BAND_HZ,
                        ),
                    ),
                ),
                geometry=position_geometry(stop.prompt),
                lateral_consumer=LATERAL_CONSUMER_FORWARD_MODEL,
                gating_applied=False,
                **stamp,
            ), **_seat_capture(store, program, stop.index, magnitude)}
            for stop, magnitude in zip(stops, magnitudes)
        ),
        _receipt("r3"),
    )
    (round_dir / "state.json").write_text(
        json.dumps(_state(round_ordinal=round_ordinal, verify_measured=None))
    )
    write_manifest(round_dir, program="room" if name == "r3-seat" else "speaker")
    return round_dir


def _reopen(round_dir: Path) -> tuple[BankedRecordStore, SessionIdentity]:
    """The store a banked round was written through, and its two-namespace id."""
    bundle_dir, = (Path(round_dir) / "bundle").iterdir()
    session_id = str(json.loads((bundle_dir / "info.json").read_text())["session_id"])
    return (
        BankedRecordStore(
            evidence=CommissioningEvidenceStore.open(
                bundle_dir, expected_session_id=session_id,
            ),
            capture_session_id=_CAPTURE_SESSION_ID,
        ),
        SessionIdentity(
            session_id=session_id,
            aliases={ALIAS_CAPTURE_SESSION_ID: _CAPTURE_SESSION_ID},
        ),
    )


def bank_executor_take(root, monkeypatch, *, program=None, raw_record=None, analysis_error=None, pose=None,
                       analysis_fields=None, recording=None):
    """One take through the capture host. ``recording`` is int32 samples the
    host analyses for real at the stop's own lateral pose, as a walk plays it;
    without one the take records 32 zeros under a stand-in analysis."""
    program = program or build_verify_program(2500, sweep_s=1.5, gain_db=-30, leading_pilot_gains_db=(-24, -14))
    raw_record = raw_record or {}
    calibration_root = root / "calibration"
    calibration = store_calibration(text="20 -1\n1000 1\n20000 0\n", provider="minidsp",
        model="minidsp_umik2", label="miniDSP UMIK-2", source="fixture", root=calibration_root)
    with monkeypatch.context() as patch:
        patch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(calibration_root))
        if recording is None:
            analysis = replace(_measure_analysis(program) if program.phase == "measure" else _verify_analysis(program),
                               **(analysis_fields or {}))
            def analyzed(*args, **kwargs):
                if analysis_error is not None:
                    raise analysis_error
                return analysis
            patch.setattr("jasper.audio_measurement.program_analysis.analyze_program_capture", analyzed)
        info = open_bundle(mono_output_topology(), calibration_id="", sessions_dir=root / "sessions")
        store = CommissioningEvidenceStore.open(Path(info["bundle_dir"]), expected_session_id=info["session_id"])
        manifest = RunManifest("executor", BankedRecordStore(store, "executor"))
        stop = angle_capture.AngleStop(0, angle_capture.REGIME_SUMMED,
                                      candidate_id="speaker-candidate", **(pose or {}))
        request = angle_capture.AngleCaptureRequest(stops=(stop,), candidates=(stop.candidate_id,))
        spec = MeasureSpec(kind="candidate", graph_scope="candidate", candidate_id=stop.candidate_id,
                           program_phase=program.phase)
        wav, _ = encode_wav_s32(np.zeros(32, dtype=np.int32) if recording is None else recording, sample_rate_hz=48000)
        answer = WiredCaptureAnswer(wav=wav, program=program.to_dict(),
            device={"card": "UMIK2", "usb_id": "2752:002b", "model_key": "minidsp_umik2",
                    "pcm": "hw:CARD=UMIK2,DEV=0", "channel_selected": 0},
            setup={"calibration": {"mode": "stored", "calibration_id": calibration.calibration_id,
                                    "model": calibration.model}})
        answer = place_wired_answer(store.bundle_dir, answer, phase=program.phase, group=program.phase)
        capture = SimpleNamespace(take_answer=lambda: answer, bundle_dir=store.bundle_dir)
        records = CapturedRecordStore(manifest, capture)
        conductor = (_conductor(FakeSeams(), index_phase_map={1: program.phase}) if recording is None else
                     _conductor(FakeSeams(), index_phase_map={1: PHASE_LATERAL},
                                lateral_consumer=LATERAL_CONSUMER_FORWARD_MODEL,
                                lateral_prompts=(angle_capture.resolve_request(request)[0].prompt,)))
        refs: dict[str, Any] = {}
        conductor._seams = replace(conductor._seams, analyze=bind_production_analyze(meta=refs))
        provenance = CaptureProvenanceRecorder()
        provenance.record(CaptureProvenance(graph_kind="tuning_measurement", graph_fingerprint="played",
            session_volume_db=-20.0, stimulus_wav_sha256="a" * 64, stimulus_peak_dbfs=-20.0))
        analyze, _ = bind_plan_analysis(conductor, records, manifest=manifest, evidence=refs, provenance=provenance)
        async def measure(session, spec):
            record_id = await records.bank({"take_id": "executor-take", "kind": "candidate",
                "graph_scope": "candidate", "candidate_id": "speaker-candidate", "graph_fingerprint": "submitted",
                "program_phase": program.phase, "position_axis": "horizontal", "position_deg": 0,
                "level_db": -20.0, "stimulus_dbfs": None, **raw_record})
            return MeasureOutcome(spec, (StimulusOutcome(0, None, -20.0, record_id),))
        async def bank():
            async with open_session() as (session, _):
                await run_plan(request, session=session, manifest=manifest, analyze=analyze,
                    captures=(PlanCapture(stop, spec),), measure=measure,
                    assessor=lambda *_a, **_k: TakeVerdict(True), aborts={Exception: "internal_error"})
            assert manifest.status == ("partial" if analysis_error is not None else "complete")
            _, record_id = manifest.pending_records[0]
            return json.loads((store.bundle_dir / EVIDENCE_ROOT / "artifacts" / record_id).read_text())
        return asyncio.run(bank())


def _decimate_verify_measured(tracking_curve: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the VERIFY capture's graded curve pair — the
    ``(freqs_hz, measured_db, predicted_db)`` the delta probe graded (#2522).

    Averaged in dB, which here is what makes the record RE-GRADABLE: block
    averaging in dB is linear, so the difference of the two decimated curves is
    exactly the decimated difference, and ``measured − predicted`` is what
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe` grades. A
    power mean would bias each side differently.

    ``None`` for an absent curve, one that is not a triple, an empty grid, or
    arrays whose lengths disagree — all of which mean "not re-gradable offline".
    """
    if tracking_curve is None:
        return None
    import numpy as np

    try:
        freqs, measured, predicted = tracking_curve
    except (TypeError, ValueError):
        return None
    grid = np.asarray(freqs, dtype=float)
    measured_db = np.asarray(measured, dtype=float)
    predicted_db = np.asarray(predicted, dtype=float)
    n = int(grid.size)
    if n == 0 or int(measured_db.size) != n or int(predicted_db.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block

        def _blocks(values):
            return values[:kept].reshape(blocks, block).mean(axis=1)

        grid, measured_db, predicted_db = (
            _blocks(grid),
            _blocks(measured_db),
            _blocks(predicted_db),
        )
    return {
        "freqs_hz": [float(f) for f in grid],
        "measured_db": [float(v) for v in measured_db],
        "predicted_db": [float(v) for v in predicted_db],
    }
