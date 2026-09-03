# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The durable crossover-v2 state document, in both directions:
:func:`build_conductor_state` assembles it and the ``*_from_state`` readers take
it apart, so one document has one owner.

Reading and writing the FILE is not here — the host keeps the schema version,
the atomic write and the fsync decision; only :data:`DEFAULT_V2_STATE_PATH`
lives here. :func:`build_conductor_state` returns the document and the
durability verdict together rather than performing the write.

The conductor argument is duck-typed on purpose: callers persist stand-ins, so
every optional field is read through ``getattr`` with a default and an absent
attribute means what the key's own absence means downstream.

The carry-forward rules are the interesting half. A key is either written fresh
by this session or inherited from the state being replaced, in one of three
shapes named at each rule: **unconditional** (the host-owned apply keys, which a
session-scoped guard would drop on the first post-apply write because the
deferred VERIFY auto-arms under a new relay session id —
``test_every_host_owned_apply_key_survives_persist_conductor_state`` derives
that set mechanically), **session-scoped** (a previous session's answer says
nothing about this one), and **phase-gated** (keyed on the phase that PRODUCES
the value, so a session that never had the chance inherits rather than erases).

No ``jasper.web`` import, and nothing from
``jasper.active_speaker.crossover_v2_flow``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.active_speaker.attempts_loop import (
    PROVENANCE_REALIZED,
    AttemptBudget,
    AttemptIntegrity,
    AttemptRecord,
)
from jasper.json_fields import finite_float as _finite
from jasper.log_event import log_event

from .contracts import ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED
from .topology_prescription import candidate_topology

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jasper.audio_measurement.program_analysis import ProgramAnalysis

logger = logging.getLogger(__name__)

#: Where this document lives on a speaker. Re-exported by
#: ``jasper.web.correction_crossover_v2`` under the same name, which still owns
#: the write.
DEFAULT_V2_STATE_PATH = Path("/var/lib/jasper/active_speaker_crossover_v2_state.json")

__all__ = [
    "DEFAULT_V2_STATE_PATH",
    "FINDING_HOUSEHOLD_REFS_KEY",
    "MAX_PERSISTED_SUM_POINTS",
    "ConductorState",
    "V2ConductorSnapshot",
    "alignment_prescription_prior_from_state",
    "attempt_history_from_state",
    "attempt_record_from_verify",
    "blend_prescription_prior_from_state",
    "blend_prescription_sha256_from_state",
    "build_conductor_state",
    "commanded_delta_prior_from_state",
    "declared_transfer_prior_from_state",
    "entry_baseline_prior_from_state",
    "pilot_transfer_prior_from_state",
    "topology_prescription_prior_from_state",
    "verify_measured_curve_from_state",
]


@dataclass(frozen=True)
class ConductorState:
    """One persist's answer: the document, and whether it must be fsynced.

    ``durable`` is true exactly when this write records a NEW round-receipt
    identity: a power cut that lost one would leave a receipt in the bundle that
    nothing points at. Every other persist stays cheap, and there is one per
    capture. The verdict travels beside the document because the fact that
    decides it is only visible while the document is being built.
    """

    state: dict[str, Any]
    durable: bool


@dataclass(frozen=True)
class V2ConductorSnapshot:
    """Durable phase state, bound to the relay session (§5.6).

    Persisted under the session's commissioning run;
    :meth:`CrossoverV2Session.hydrate` keeps the accepted phases only when the
    current session matches, because mic position is unverifiable across
    sessions.
    """

    session_id: str
    accepted_phases: tuple[str, ...] = ()
    applied: bool = False
    gain_plan_db: Mapping[str, float] | None = None
    # MEASURE's ACTUAL per-role sweep duration, read off the composed program
    # — a continuous float no offline search grid can reach, banked so
    # ``harmonic_evidence.rebuild_measure_program`` can REPLAY a fitted round's
    # sweep. Purely derived and never restored by ``hydrate``, since the live
    # conductor recomposes it from ``gain_plan_db``. ``None`` before MEASURE is
    # composed (#2923).
    measure_sweep_durations_s: Mapping[str, float] | None = None
    candidate_fingerprint: str | None = None
    # The ordered phases THIS session actually runs — the subset of
    # ``CAPTURE_PHASES`` its ``index_phase_map`` addresses, which the
    # module-global tuple cannot express. Empty on older state; readers fall
    # back to ``CAPTURE_PHASES``.
    session_phases: tuple[str, ...] = ()
    # WHICH INSTRUMENT produced this session. Empty string means UNKNOWN and
    # readers must render it as unknown rather than assuming full: guessing
    # would attach a post-apply cross-position claim to a result that never
    # measured across positions.
    tier: str = ""
    # WHERE the pre-apply cloud's close has got to: one of
    # :data:`CLOUD_CLOSE_NONE` / :data:`CLOUD_CLOSE_AWAITING_CONFIRM` /
    # :data:`CLOUD_CLOSE_RUNNING`. Persisted because the wizard renders from
    # durable state alone, where "every stage-1 phase accepted and no candidate"
    # otherwise reads identically at the confirm screen, during the fit, and
    # after a session that produced nothing.
    cloud_close: str = ""
    # Attempt history is journey-scoped, not relay-session-scoped: a second
    # apply→VERIFY runs under a fresh relay session, so these records survive
    # ``hydrate``'s session rebind while CHECK/MEASURE evidence does not.
    #
    # Surviving is not the same as being COMPARABLE (#2081): they also survive
    # ``reset_v2_journey_state``, across which the mic was re-placed, so each
    # record carries the sitting that produced it and the kernel refuses a
    # cross-sitting pair rather than reporting an improvement no study licenses.
    attempt_history: tuple[AttemptRecord, ...] = ()
    last_attempt_decision: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "accepted_phases": list(self.accepted_phases),
            "applied": self.applied,
            "gain_plan_db": dict(self.gain_plan_db) if self.gain_plan_db else None,
            "measure_sweep_durations_s": (
                dict(self.measure_sweep_durations_s)
                if self.measure_sweep_durations_s else None
            ),
            "candidate_fingerprint": self.candidate_fingerprint,
            "session_phases": list(self.session_phases),
            "tier": self.tier,
            "cloud_close": self.cloud_close,
            "attempt_history": [item.to_dict() for item in self.attempt_history],
            "last_attempt_decision": (
                dict(self.last_attempt_decision)
                if self.last_attempt_decision is not None else None
            ),
        }


# Where the household-readable projection of a banked finding set rides inside
# the durable state's ``evidence`` refs: a list of ``{household_copy, at}``
# rows. Named here rather than beside its writer because three places spell it.
FINDING_HOUSEHOLD_REFS_KEY = "household_findings"

# Downsample ceiling for the persisted predicted-sum verify prior — enough
# resolution for the ±1.5 dB [Fc/2, 2Fc] comparison at 1/6-octave smoothing
# while keeping the state file small. Reduction to it must be a block average,
# never a raw stride: a stride aliases below ~600 Hz, where 46.875 Hz spacing
# leaves fewer than 3 samples in a 1/3-octave band (#1858).
MAX_PERSISTED_SUM_POINTS = 512


# --------------------------------------------------------------------------- #
# conductor persistence
# --------------------------------------------------------------------------- #


def _decimate_sum(predicted_sum: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the full-resolution predicted-sum curve to at
    most :data:`MAX_PERSISTED_SUM_POINTS`.

    Routed through
    :func:`~jasper.audio_measurement.spatial_combine.decimate_curve_to_analysis_grid`,
    the same block-average owner that grades this curve, so every persisted
    point is a genuine local mean in linear power rather than one raw bin
    (#1858). A verify-only re-arm feeds an already-persisted curve back through
    here, so such a curve is block-averaged again and comes out coarser; the
    household's next MEASURE replaces it.
    """
    if predicted_sum is None:
        return None
    freqs, mags = predicted_sum
    n = len(freqs)
    if n == 0:
        return None
    import numpy as np

    from jasper.audio_measurement.spatial_combine import (
        decimate_curve_to_analysis_grid,
    )

    grid, curve_db = decimate_curve_to_analysis_grid(
        np.asarray(freqs, dtype=float), np.asarray(mags, dtype=float),
        max_bins=MAX_PERSISTED_SUM_POINTS,
    )
    return {
        "freqs_hz": [float(f) for f in grid],
        "magnitude_db": [float(m) for m in curve_db],
    }


def _decimate_delta(commanded_delta: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the COMMANDED delta — the change the applied
    graph asks the speaker for relative to the graph it replaces, covering
    filters, role gains, polarity and delay (#2611).

    Bounded at the same :data:`MAX_PERSISTED_SUM_POINTS` ceiling over the same
    fixed-width blocks as :func:`_decimate_sum`, so the two curves land on one
    grid for the same input frequencies (pinned by
    ``test_the_commanded_delta_persists_on_the_same_grid_as_the_predicted_sum``).

    Averaged in dB, not in linear power — the one place this parts company with
    :func:`_decimate_sum`, because the arithmetic mean is the unbiased estimator
    of a DIFFERENCE of dB curves where the power mean is biased upward by
    Jensen's inequality, most where the delta is steepest. Measured over 200
    realistic cascades: worst single-block disagreement 1.60 dB, and 5 of
    100,762 persisted bins change side of the 0.5 dB
    :data:`~jasper.active_speaker.delta_probe.DELTA_PROBE_MIN_COMMANDED_DB`
    floor, so the choice reaches band membership rather than a third decimal.

    It does not move the graded error: the conductor reconstructs
    ``realized = (measured - predicted) + commanded``, so ``realized -
    commanded`` cancels this curve exactly.
    """
    if commanded_delta is None:
        return None
    import numpy as np

    freqs, delta = commanded_delta
    grid = np.asarray(freqs, dtype=float)
    values = np.asarray(delta, dtype=float)
    n = int(grid.size)
    # A length disagreement is not reachable from ``_commanded_delta``, but a
    # raise here would lose the WHOLE snapshot rather than this key. Absent
    # means "nothing commanded", the honest reading downstream.
    if n == 0 or int(values.size) != n:
        return None
    if n > MAX_PERSISTED_SUM_POINTS:
        block = -(-n // MAX_PERSISTED_SUM_POINTS)  # ceil division
        blocks = n // block
        kept = blocks * block
        grid = grid[:kept].reshape(blocks, block).mean(axis=1)
        values = values[:kept].reshape(blocks, block).mean(axis=1)
    return {
        "freqs_hz": [float(f) for f in grid],
        "delta_db": [float(d) for d in values],
    }


def _decimate_verify_measured(tracking_curve: Any) -> dict[str, Any] | None:
    """Persist-time reduction of the VERIFY capture's graded curve pair — the
    ``(freqs_hz, measured_db, predicted_db)`` the delta probe graded (#2522).

    Bounded over the same fixed-width blocks as :func:`_decimate_delta`, but on
    the VERIFY capture's grid rather than the MEASURE prediction's, so the two
    are not expected to land on the same frequencies; a re-grade interpolates
    the commanded axis onto this one, exactly as the live probe does.

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
            _blocks(grid), _blocks(measured_db), _blocks(predicted_db)
        )
    return {
        "freqs_hz": [float(f) for f in grid],
        "measured_db": [float(v) for v in measured_db],
        "predicted_db": [float(v) for v in predicted_db],
    }


def verify_measured_curve_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any, Any] | None:
    """The persisted VERIFY curve pair, ready to re-grade (#2522).

    The read side of ``verify_priors.verify_measured``. With the persisted
    ``commanded_delta`` and the ``delta_probe`` record's own
    ``requested_band_hz`` / ``expected_offset_db``, that is everything a
    laptop-side re-grade needs.

    ``None`` for every case that means "no measured curve to grade", length
    disagreement included: three arrays that are not one curve would otherwise
    reach the classifier as a grid mismatch instead of an absence.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get("verify_measured") if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs = record.get("freqs_hz")
    measured = record.get("measured_db")
    predicted = record.get("predicted_db")
    if not freqs or not measured or not predicted:
        return None
    if not (len(freqs) == len(measured) == len(predicted)):
        log_event(
            logger, "correction.crossover_v2_verify_measured_malformed",
            level=logging.WARNING,
            n_freqs=len(freqs), n_measured=len(measured), n_predicted=len(predicted),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(measured, dtype=float),
        np.asarray(predicted, dtype=float),
    )


def attempt_history_from_state(raw: Any) -> tuple[AttemptRecord, ...]:
    """Restore the session-owned attempt history from durable journey state.

    Invalid rows are dropped as unavailable history, never partially trusted.
    The floor is intentionally absent from this shape: it has one owner in
    :mod:`jasper.active_speaker.model_error_store` and is read afresh by the
    host when it constructs the session.
    """

    loop = raw.get("attempts_loop") if isinstance(raw, Mapping) else None
    rows = loop.get("history") if isinstance(loop, Mapping) else None
    if not isinstance(rows, list):
        return ()
    restored: list[AttemptRecord] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        integrity = row.get("integrity")
        if not isinstance(integrity, Mapping):
            continue
        try:
            record = AttemptRecord(
                attempt_id=str(row.get("attempt_id") or ""),
                metric=str(row.get("metric") or ""),
                provenance=str(row.get("provenance") or ""),
                # #2081. Absent on every row written before it, and ``""`` is
                # exactly what the kernel refuses on — so an upgraded speaker
                # stops claiming improvement against its pre-upgrade attempt
                # instead of claiming one whose sitting nothing recorded.
                sitting_id=str(row.get("sitting_id") or ""),
                integrity=AttemptIntegrity(
                    comparable=integrity.get("comparable") is True,
                    reasons=tuple(
                        str(reason) for reason in integrity.get("reasons", ())
                        if isinstance(reason, str) and reason
                    ),
                ),
                repeats_used=(
                    int(row["repeats_used"])
                    if isinstance(row.get("repeats_used"), int)
                    and not isinstance(row.get("repeats_used"), bool)
                    else 1
                ),
                grade_db=_attempt_optional_float(row.get("grade_db")),
                deviation_from_predecessor_db=_attempt_optional_float(
                    row.get("deviation_from_predecessor_db")
                ),
                n_graded_bins=(
                    _attempt_optional_positive_int(row.get("n_graded_bins"))
                ),
                predicted_remaining_improvement_db=_attempt_optional_float(
                    row.get("predicted_remaining_improvement_db")
                ),
                in_spec=(
                    row.get("in_spec")
                    if isinstance(row.get("in_spec"), bool) else None
                ),
                curve_refs=tuple(
                    str(ref) for ref in row.get("curve_refs", ())
                    if isinstance(ref, str) and ref
                ),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        restored.append(record)
    # The kernel's hard cap is the only live attempt budget; older rows carry
    # no decision value and would grow Pi state for no payoff.
    return tuple(restored[-AttemptBudget().hard_cap_attempts:])


def _attempt_optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _attempt_optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def attempt_record_from_verify(
    analysis: ProgramAnalysis, *, attempt_id: str, sitting_id: str,
) -> AttemptRecord:
    """Map one VERIFY analysis into the pure kernel's realized record (#2033).

    VERIFY leaves repeat-only checks ``not_evaluated`` because it contains one
    summed sweep; their names ride as reasons but do not make an otherwise clean
    capture incomparable. Any evaluated failure does, and carries both the
    failed and not-evaluated names.

    ``sitting_id`` is the relay session that captured this sweep, and is
    REQUIRED rather than defaulted because the available default — ``""`` — is
    what the kernel reads as "unrecorded" and refuses on (#2081). The relay
    session is the right proxy for one continuous microphone sitting for the
    reason ``hydrate`` invalidates CHECK and MEASURE across a rebind.
    """

    # Function-local for the module's standing reason: ``verification`` pulls
    # numpy at module scope and this module is on the socket-activated web
    # host's import path.
    from .verification import CAPTURE_INTEGRITY_UNAVAILABLE

    integrity = analysis.capture_integrity
    if integrity is None:
        attempt_integrity = AttemptIntegrity(
            comparable=False,
            reasons=(CAPTURE_INTEGRITY_UNAVAILABLE,),
        )
    else:
        reasons = tuple(dict.fromkeys((*integrity.failed, *integrity.not_evaluated)))
        attempt_integrity = AttemptIntegrity(
            comparable=not integrity.failed,
            reasons=reasons,
        )
    tracking = analysis.verify_tracking or {}
    frame = tracking.get("frame")
    frame = frame if isinstance(frame, Mapping) else {}
    return AttemptRecord(
        attempt_id=str(attempt_id),
        metric=ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        provenance=PROVENANCE_REALIZED,
        sitting_id=str(sitting_id),
        integrity=attempt_integrity,
        grade_db=_attempt_optional_float(
            tracking.get(ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED)
        ),
        # ``frame.n_bins`` comes from the exact validity-clamped,
        # notch-excluded mask VERIFY graded, and carrying it activates the
        # kernel's denominator-shrink refusal.
        n_graded_bins=_attempt_optional_positive_int(frame.get("n_bins")),
    )


def _predicted_spec_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's stored prediction verdict, as durable state carries it.

    ``getattr`` because a conductor double may not carry the property, and a
    missing one must read as "no verdict" rather than raise mid-persist and lose
    the whole snapshot.
    """
    report = getattr(conductor, "measure_predicted_spec_report", None)
    return dict(report) if isinstance(report, Mapping) else None


def _entry_baseline_prior(conductor: Any) -> dict[str, Any] | None:
    """The conductor's entry baseline as durable state carries it (#2291).

    ``getattr`` plus a duck-typed ``to_dict`` for
    :func:`_predicted_spec_prior`'s reason.
    """
    baseline = getattr(conductor, "measure_entry_baseline", None)
    to_dict = getattr(baseline, "to_dict", None)
    if not callable(to_dict):
        return None
    record = to_dict()
    return dict(record) if isinstance(record, Mapping) else None


def _round_receipt_identity(conductor: Any) -> dict[str, Any] | None:
    """The conductor's round-receipt identity, or ``None``.

    ``getattr`` for :func:`_predicted_spec_prior`'s reason.
    """
    record = getattr(conductor, "round_receipt_identity", None)
    return dict(record) if isinstance(record, Mapping) else None


def entry_baseline_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 entry baseline, as the conductor's ctor takes it (#2291).

    The read side of ``verify_priors.entry_baseline``: durable state in, the
    ``measure_entry_baseline`` argument out.

    ``None`` is
    :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_BASELINE_UNAVAILABLE`,
    which is INDETERMINATE and not a pass. It covers every case that means
    "there is no comparable before", and which one is not recoverable from the
    file. Shape validation belongs to ``EntryBaseline.from_dict``.
    """
    from jasper.active_speaker.crossover_v2.round_evidence import EntryBaseline

    priors = (state or {}).get("verify_priors")
    record = priors.get("entry_baseline") if isinstance(priors, Mapping) else None
    return EntryBaseline.from_dict(record)


def alignment_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 delay prescription, as the conductor's ctor takes it (#2662).

    The read side of ``verify_priors.alignment_prescription``. ``None`` is "this
    round prescribed no delay" and also covers an older or truncated record. The
    BOUND is deliberately not re-applied here: it has one owner, the request
    boundary.
    """
    from jasper.active_speaker.crossover_v2.alignment_prescription import (
        alignment_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("alignment_prescription") if isinstance(priors, Mapping) else None
    )
    return alignment_prescription_from_mapping(record)


def topology_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """Durable state in, the ``topology_prescription`` argument out.

    The grading stage re-opens its session AT this topology, so a pin that
    failed to rehydrate would silently grade a pinned round's VERIFY against the
    crossover the speaker used to run. The read-back is shape-only: re-applying
    the bounds at grading time could only throw away the evidence of a round
    that really ran.
    """
    from jasper.active_speaker.crossover_v2.topology_prescription import (
        topology_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("topology_prescription") if isinstance(priors, Mapping) else None
    )
    return topology_prescription_from_mapping(record)


def blend_prescription_prior_from_state(state: Mapping[str, Any] | None) -> Any:
    """The stage-1 blend prescription, as the conductor's ctor takes it.

    The read side of ``verify_priors.blend_prescription``. Without this arm the
    feature loses what it exists to bank: ``verify_priors`` is rebuilt from the
    conductor on EVERY persist and a stage-2 conductor holds no prescription, so
    stage 2 would write ``None`` over stage 1's record before the round receipt
    is written.

    ``None`` is "this round prescribed no blend correction" and also covers an
    older or truncated record. The BOUND is deliberately not re-applied: it has
    one owner, the boundary that accepted the document.
    """
    from jasper.active_speaker.crossover_v2.blend_prescription import (
        blend_prescription_from_mapping,
    )

    priors = (state or {}).get("verify_priors")
    record = (
        priors.get("blend_prescription") if isinstance(priors, Mapping) else None
    )
    return blend_prescription_from_mapping(record)


def blend_prescription_sha256_from_state(state: Mapping[str, Any] | None) -> str:
    """The digest beside the record above, or ``""``.

    Read separately because it is banked separately — see the persist's own
    comment for why the digest cannot live inside the record it describes.
    """
    priors = (state or {}).get("verify_priors")
    digest = (
        priors.get("blend_prescription_sha256") if isinstance(priors, Mapping)
        else None
    )
    return str(digest or "") if isinstance(digest, str) else ""


def pilot_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """The PREVIOUS session's G3 reference, as durable state carries it (#1927).

    The read side of ``verify_priors.pilot_transfer_reference``, and the whole
    of what a verify-only re-arm seeds a fresh conductor's history with. Shape
    checking beyond "is it a mapping" belongs to the conductor, which owns the
    "values plus a date, or nothing" rule.
    """
    priors = (state or {}).get("verify_priors")
    prior = priors.get("pilot_transfer_reference") if isinstance(priors, Mapping) else None
    return prior if isinstance(prior, Mapping) else None


def commanded_delta_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 commanded delta, as the conductor's ctor takes it (#2291).

    The read side of ``verify_priors.commanded_delta``. ``None`` is the probe's
    :data:`~jasper.active_speaker.delta_probe.VERDICT_UNAVAILABLE`, which is not
    a pass, and covers every case meaning "there is no commanded axis to grade
    against" — a trims-only candidate included.

    A length disagreement is one of those and is checked here (#2316): the two
    arrays are read separately, so a truncated record yields two valid arrays
    that are not a curve, and returning them would make a capability line report
    the delta PRESENT while the probe reports it unavailable a moment later.
    """
    return _delta_prior_from_state(state, "commanded_delta")


def declared_transfer_prior_from_state(
    state: Mapping[str, Any] | None,
) -> tuple[Any, Any] | None:
    """The stage-1 STATE axis, as the conductor's ctor takes it (#2614).

    The applied graph's own transfer against the uncorrected crossover — the
    axis the delta probe's two directional safety rules mask on. ``None`` means
    the probe falls back to the CHANGE axis alone for those two rules, which is
    an identity on a first-ever apply.
    """
    return _delta_prior_from_state(state, "declared_transfer")


def _delta_prior_from_state(
    state: Mapping[str, Any] | None, key: str,
) -> tuple[Any, Any] | None:
    """One ``verify_priors`` curve record, rehydrated — the shared reader.

    Both delta axes persist through :func:`_decimate_delta` and rehydrate
    through here, so the length check the docstring above argues for cannot end
    up applied to one axis and not the other.
    """
    import numpy as np

    priors = (state or {}).get("verify_priors")
    record = priors.get(key) if isinstance(priors, Mapping) else None
    if not isinstance(record, Mapping):
        return None
    freqs, delta = record.get("freqs_hz"), record.get("delta_db")
    if not freqs or not delta:
        return None
    if len(freqs) != len(delta):
        log_event(
            logger, "correction.crossover_v2_commanded_delta_malformed",
            level=logging.WARNING, prior=key,
            n_freqs=len(freqs), n_delta=len(delta),
        )
        return None
    return (
        np.asarray(freqs, dtype=float),
        np.asarray(delta, dtype=float),
    )


def _candidate_headroom_cost_db(linearization: Any) -> float:
    """The applied correction's disclosed max-level cost, dB.

    Thin adapter over the fit module's own reducer, so this payload and the
    conductor's cannot disagree about a household-facing number.
    """
    from jasper.active_speaker.linearization_fit import worst_headroom_cost_db

    if not isinstance(linearization, Mapping):
        return 0.0
    return worst_headroom_cost_db(linearization)


def _candidate_octave_summary(linearization: Any) -> dict[str, dict[str, float]]:
    """Per-role OBSERVE-layer octave deficits
    (``LinearizationFit.observe_octave_summary``, achieved-minus-target dB at
    each octave center), read off the candidate's ``linearization`` dict.

    A pure projection, never a derived curve. Empty for a role whose fit never
    ran. These are per-driver fit diagnostics from the design-axis capture, not
    the spec measurement.
    """
    out: dict[str, dict[str, float]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping):
            continue
        octaves = fit.get("observe_octave_summary")
        if not isinstance(octaves, Mapping) or not octaves:
            continue
        role_octaves: dict[str, float] = {}
        for hz, value in octaves.items():
            db = _finite(value)
            if db is not None:
                role_octaves[str(hz)] = db
        if role_octaves:
            out[str(role)] = role_octaves
    return out


def _candidate_octave_reasons(
    linearization: Any, octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, str]]:
    """Per-role octave-band reason codes (``LinearizationFit.reason_summary``),
    the sibling of :func:`_candidate_octave_summary`'s numbers.

    Keyed off the already-projected ``octaves`` rather than recomputed, because
    ``linearization_fit._empty_fit`` returns an EMPTY
    ``observe_octave_summary`` beside a fully populated ``reason_summary``: a
    role can honestly have verdicts and no numbers, and keying off the numbers
    makes the reason set a subset of the octave set by construction.

    The numbers need this (#2638): ``observe_octave_summary`` is
    ``working_db - frame_target_db`` across the WHOLE grid, so above a driver's
    radiating band the crossover target dives at 24 dB/oct while the measurement
    floor stays put and the difference explodes positive — stopband arithmetic,
    not performance. The fit engine labels those octaves
    ``envelope_out_of_band``; this carries the label to the surface showing the
    number.

    A SEPARATE key rather than a compound value, so an older candidate carrying
    numbers with no reasons renders as it always did.
    """
    out: dict[str, dict[str, str]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        reasons = fit.get("reason_summary")
        if not isinstance(reasons, Mapping) or not reasons:
            continue
        role_reasons = {
            str(hz): code for hz, code in reasons.items()
            if isinstance(code, str) and code
        }
        if role_reasons:
            out[str(role)] = role_reasons
    return out


def _candidate_octave_driver_classes(
    linearization: Any, octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, str]:
    """Per-role declared ``driver_class`` (``LinearizationFit.driver_class``),
    the third sibling of the octave numbers and their verdicts.

    Gated on the same ``octaves`` membership as
    :func:`_candidate_octave_reasons`. A separate key because ``driver_class``
    is a per-FIT scalar rather than a per-band verdict, and because
    ``LIMITED_BY_CLASS_PRIOR`` fires for every declared class: the remedy
    reading this must tell an already-declared class from an undeclared one,
    since only the second has an action left to take.
    """
    out: dict[str, str] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        driver_class = fit.get("driver_class")
        if isinstance(driver_class, str) and driver_class:
            out[str(role)] = driver_class
    return out


def _candidate_pinned_trims(
    candidate: Any,
) -> dict[str, dict[str, float | None]]:
    """Each pinned role's shipped trim, the value it displaced, and the gap.

    Read off the candidate, where ``build_candidate`` already stamped
    ``trim_pinned`` and ``displaced_trim_db``, rather than asked of the session.

    The program-analysis ``trim_db`` is deliberately NOT read: on the fitted
    lane it is the pre-commit number, a different value from the
    giveback-and-normalized trim the pin displaced, so a delta against it would
    misstate what the pin changed. ``None`` means the candidate carries no
    displaced value, never a substituted zero.
    """
    out: dict[str, dict[str, float | None]] = {}
    for role, entry in (candidate.linearization or {}).items():
        if not isinstance(entry, Mapping) or entry.get("trim_pinned") is not True:
            continue
        shipped = candidate.role_attenuations_db.get(str(role))
        if shipped is None:
            continue
        raw = entry.get("displaced_trim_db")
        displaced = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        out[str(role)] = {
            "pinned_db": float(shipped),
            "displaced_db": displaced,
            "delta_db": None if displaced is None else float(shipped) - displaced,
        }
    return out


def _candidate_summary(
    candidate: Any, *, topology_pinned: bool = False,
    headroom_cost_basis: str | None = None,
) -> dict[str, Any] | None:
    # Lazy: this module has no module-level numpy and the fit module does, so
    # the socket-activated wizard only pays for it on a path with a candidate.
    from jasper.active_speaker.linearization_fit import (
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )

    # WHICH era stamped the per-branch charges below, supplied by the CALLER
    # because only the caller knows. The default is this build's era, true on
    # the minting path; republish passes UNKNOWN rather than letting an
    # off-disk candidate wear a current label over older numbers.
    stamped_basis = headroom_cost_basis or HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN

    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    octaves = _candidate_octave_summary(candidate.linearization)
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        # …and which of those trims the round did NOT solve: the household
        # copy must never word a pinned number as a measured result. The
        # DISPLACED value rides beside it, so a reader judging a pin sees the
        # answer it overrode. Discloses rather than blocks.
        "trims_pinned": _candidate_pinned_trims(candidate),
        # WHERE this candidate crosses, and whether the round was PINNED there.
        # The corner comes off the candidate; the bit comes from the session,
        # because a corner cannot say who chose it.
        "crossover": candidate_topology(candidate),
        "crossover_pinned": bool(topology_pinned),
        "alignment": candidate.alignment.to_dict(),
        # For the conductor's trust gate
        # (``ALIGNMENT_CONFIDENCE_TRUST_FLOOR``) and the result screen's
        # collapsed expert disclosure.
        "alignment_confidence": analysis.get("alignment_confidence"),
        # Result-screen expert disclosure only.
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        # WHICH objective committed this candidate's (polarity, delay) pair,
        # and whether the committed delay left the comb lobe its physical
        # anchor owns (#2598). The review screen must not word a
        # declared-design commitment as a measured one; the lobe flag is a
        # receipt line, because that mode is magnitude-flat and an on-axis
        # VERIFY cannot contradict it.
        "alignment_objective": analysis.get("alignment_objective"),
        # …and whether the polarity above was MEASURED or held by the request.
        # Its own key because the objective cannot say: a pinned round commits
        # the same ``explicit_prescription_committed`` an unpinned one does.
        "polarity_pinned": bool(analysis.get("polarity_pinned")),
        "left_anchor_lobe": analysis.get("left_anchor_lobe"),
        # WHY driver linearization did or did not run this attempt — "" /
        # "fitted" / "trim_rejected" / "ineligible_mic_tier" /
        # "ineligible_repeats" / "fit_failed".
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        # Per-role top-octave deficits.
        "linearization_octaves": octaves,
        # WHY each of those octaves reads the way it does (#2638): the number
        # alone cannot distinguish a real deficit from an octave past the
        # driver's own band, where the difference is the crossover's rolloff.
        "linearization_octave_reasons": _candidate_octave_reasons(
            candidate.linearization, octaves
        ),
        # Which declared driver_class produced each role's octave verdicts
        # above, so the remedy attached downstream never tells a household to
        # redeclare a class it already named.
        "linearization_driver_class": _candidate_octave_driver_classes(
            candidate.linearization, octaves
        ),
        # "This correction costs N dB of maximum level": headroom spend is
        # DISCLOSED, never silently limited.
        #
        # The WORST branch's charge, matching the emitter's own worst-branch
        # rule (``camilla_yaml.linearization_headroom_db``): the driver chains
        # run in parallel after the split, so the graph gives up the largest
        # branch's charge, not the sum. 0.0 for a cut-only correction — present
        # and zero rather than absent, so a surface never has to guess whether
        # the field is missing or the cost is nothing.
        "headroom_cost_db": _candidate_headroom_cost_db(candidate.linearization),
        # WHICH derivation the number above was stamped under (#1808 /
        # two-stage commission D3) — see ``stamped_basis`` above for why it
        # comes from the caller, and ``linearization_fit.HEADROOM_COST_BASIS_*``
        # for why an era is recorded rather than sniffed.
        "headroom_cost_basis": stamped_basis,
    }


def _cloud_summary(conductor: Any) -> dict[str, Any] | None:
    """Per-group geometry verdict + position ids, or ``None`` when no group ran.

    Reads the conductor's public group surfaces only, and tolerates a conductor
    double that has none (the persistence helper is called from test seams too).
    """
    from jasper.active_speaker.crossover_v2.journey import GROUP_PHASES

    try:
        session_phases = tuple(conductor.session_phases)
    except (AttributeError, TypeError):
        return None
    out: dict[str, Any] = {}
    for phase in session_phases:
        if phase not in GROUP_PHASES:
            continue
        geometry = conductor.group_geometry(phase)
        if geometry is None:
            continue
        out[phase] = {
            "geometry": geometry,
            # The SURVIVING take per position (id + attempt). A bare id list
            # is ambiguous after a geometry retake, where two takes share an id
            # and only one is in the cloud.
            "positions": list(conductor.group_position_takes(phase)),
            # The honest-instrument pipeline result for this group, in
            # ``assemble_cloud_group_result``'s own JSON shape — verbatim what
            # the bundle artifact carries. ``None`` only if the conductor double
            # has no such method, never "the pipeline was fine".
            "pipeline": (
                conductor.group_cloud_result(phase)
                if hasattr(conductor, "group_cloud_result")
                else None
            ),
            # The PRODUCING session's id, stamped once here so
            # ``_compact_cloud_status`` can tell "measured in the active
            # session" from "carried forward". The carry-forward branch below
            # copies this whole per-phase dict verbatim, so the stamp survives
            # every re-arm without a second write site. A missing stamp reads as
            # unknown provenance, never a fabricated one.
            "session_id": (
                str(conductor.session_id) if hasattr(conductor, "session_id") else None
            ),
        }
    return out or None


def _delta_probe_summary(probe: Any) -> dict[str, Any]:
    """The delta probe's verdict, small enough to live in durable state (#1811).

    The durable summary, not a second copy of the record: the full map (per-bin
    errors, exceedance width, gain factor, spatial arm, both bands) stays on
    ``event=correction.crossover_v2_delta_probe``.

    Each qualifying term rides beside the verdict it qualifies, because a
    verdict is a claim ABOUT those numbers. The frame terms are ``None`` when no
    frame was fitted — never 0.0, which would read as "measured, and flat".
    ``frame_n_bins`` / ``frame_band_hz`` bound how much weight the two frame
    terms can carry: two scalars fitted over a narrow quiet span can be large
    and mean nothing. ``entry_anchor_offset_db`` says what standing offset was
    subtracted to make ``residual_offset_db`` a level CHANGE, and the ``quiet_*``
    terms bound
    ``uncommanded_level_shift_outside_probe_band``'s coverage claim.
    ``quiet_core_band_hz`` is the interquartile span, not a second copy of
    ``frame_band_hz``'s min/max.

    ``getattr`` throughout, including into ``frame``: an absent field is
    "unknown", never a raise that loses the whole snapshot.
    """
    frame = getattr(probe, "frame", None)
    return {
        "verdict": str(getattr(probe, "verdict", "") or ""),
        "reason": str(getattr(probe, "reason", "") or ""),
        # Whether the realized-energy half of the safety axis ran: a
        # first-ever round takes the ``state_axis_only`` branch, so its axis
        # reports SAFE with that half unrun. A forensic key with no renderer
        # today, kept here because the round receipt is write-once and this
        # record is the live one every surface reads.
        "safety_anchored": bool(getattr(probe, "safety_anchored", False)),
        "expected_offset_db": getattr(probe, "expected_offset_db", 0.0),
        "residual_offset_db": getattr(probe, "residual_offset_db", None),
        "entry_anchor_offset_db": getattr(probe, "entry_anchor_offset_db", None),
        "quiet_n_bins": getattr(probe, "quiet_n_bins", None),
        "quiet_core_band_hz": (
            list(core) if isinstance(
                core := getattr(probe, "quiet_core_band_hz", None), tuple
            ) else None
        ),
        "quiet_probe_coverage": getattr(probe, "quiet_probe_coverage", None),
        "frame_offset_db": getattr(frame, "offset_db", None),
        "frame_tilt_db_per_octave": getattr(frame, "tilt_db_per_octave", None),
        "frame_n_bins": getattr(frame, "n_bins", None),
        "frame_band_hz": (
            list(band) if isinstance(band := getattr(frame, "band_hz", None), tuple)
            else None
        ),
    }


def build_conductor_state(
    conductor: Any,
    prior: Mapping[str, Any],
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
) -> ConductorState:
    """The whole document one persist writes, over the one it is replacing.

    ``prior`` is the state currently on disk (``{}`` when there is none); the
    carry-forward rules below read it, which is why this is a document builder
    rather than a projection of the conductor. This function touches no file.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure — FORENSICS, never household copy: the envelope renders
    ``failure["code"]`` through the reason registry and ignores this key.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_MEASURE

    snap = conductor.snapshot()
    verify_outcome = conductor.verify_outcome
    # Every optional read below goes through ``getattr`` because this function
    # accepts DUCK-TYPED conductors: an absent property means "nothing
    # reserved", which is what the key's own absence means downstream.
    ripple_reservation = getattr(conductor, "measure_ripple_reservation", None)
    alignment_reservation = getattr(
        conductor, "measure_alignment_reservation", None
    )
    calibration_reservation = getattr(
        conductor, "measure_calibration_reservation", None
    )
    # On ``snap`` rather than ``conductor``, since this one lives on
    # ``V2ConductorSnapshot`` itself.
    measure_sweep_durations_s = getattr(snap, "measure_sweep_durations_s", None)
    # GATED ON THE CODE BEING PERSISTED, not on the conductor's own: several
    # terminal arms supply a ``failure_code`` the capture loop never produced
    # (the relay-death arm persists ``capture_timeout`` over whatever the last
    # capture failed on), and ungated this would pair one failure's code with
    # another's evidence.
    failure_pilot_heard = (
        getattr(conductor, "last_failure_pilot_heard", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    # Which arm of ``correction_rollback_failed`` this is. Gated on the SAME
    # code-agreement check, so a terminal arm passing a different
    # ``failure_code`` cannot render a sentence about a restore it never
    # attempted.
    failure_rollback_anchor = (
        getattr(conductor, "last_failure_rollback_anchor", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
    # Let the journey learn about a restore it could not see (#2616): a
    # durable-state writer that clears ``applied`` holds no conductor, so a live
    # session whose speaker was restored out from under it keeps ``applied``
    # True in memory and the write below would put that stale True back. The
    # durable state is the authority on whether a restore HAPPENED and the
    # journey owns the flag, so this tells the journey and writes what it says.
    # Scoped to the SAME session, because a prior session's restore says nothing
    # about this one.
    if (
        prior.get("applied") is False
        and prior.get("session_id") == snap.session_id
        and snap.applied
    ):
        conductor.note_restore_observed()
        snap = conductor.snapshot()
    if hasattr(snap, "attempt_history"):
        attempts_loop_state: dict[str, Any] | None = {
            "history": [
                item.to_dict()
                for item in (getattr(snap, "attempt_history", ()) or ())
            ],
            "last_decision": (
                dict(getattr(snap, "last_attempt_decision"))
                if getattr(snap, "last_attempt_decision", None) is not None
                else None
            ),
        }
    else:
        prior_attempts = prior.get("attempts_loop")
        attempts_loop_state = (
            dict(prior_attempts) if isinstance(prior_attempts, Mapping) else None
        )
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        # The phases THIS session runs — read by ``_phase_from_state`` so a
        # verify-only re-arm reaches "done" rather than waiting on a position
        # group it never had.
        "session_phases": list(snap.session_phases),
        # WHICH INSTRUMENT produced this state. Empty string means unknown and
        # readers must render it as unknown rather than assuming "full":
        # express makes no cross-position post-apply claim, so guessing would
        # attach a claim the measurement never made.
        "tier": snap.tier,
        # WHERE the pre-apply cloud's close has got to. The wizard renders
        # from this file alone, and "every stage-1 phase accepted, no
        # candidate" is true at the confirm screen, during the fit, and after a
        # session that produced nothing.
        "cloud_close": snap.cloud_close,
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        # MEASURE's realized per-role sweep duration, banked so
        # ``harmonic_evidence.rebuild_measure_program`` can replay a fitted
        # round's sweep instead of refusing PROGRAM_NOT_REPRODUCIBLE (#2923).
        "measure_sweep_durations_s": (
            dict(measure_sweep_durations_s) if measure_sweep_durations_s else None
        ),
        # Journey state. The conductor is the sole lifecycle owner and the host
        # serializes its snapshot verbatim; `/state` projects only the last
        # decision, never the full history.
        "attempts_loop": attempts_loop_state,
        "candidate": _candidate_summary(
            conductor.candidate,
            # A stand-in without the property means "not pinned", which is
            # what an ordinary round is.
            topology_pinned=(
                getattr(conductor, "topology_prescription_record", None) is not None
            ),
        ),
        "sound_design_revision": (
            getattr(conductor, "sound_design_revision", None)
            if getattr(conductor, "sound_design_revision", None) is not None
            else prior.get("sound_design_revision")
        ),
        # What MEASURE accepted WITH A RESERVATION (#2087). Absent means the
        # accepted capture had nothing to reserve about, never "we did not
        # check", because a session that runs MEASURE writes this key on every
        # persist. Its own block rather than a key on ``candidate``: that
        # summary projects the ARTIFACT's fields, and a reservation is a
        # verdict-time judgement about the capture it was built from.
        "measure": (
            {
                **(
                    {"ripple_reservation": dict(ripple_reservation)}
                    if ripple_reservation
                    else {}
                ),
                **(
                    {"alignment_reservation": dict(alignment_reservation)}
                    if alignment_reservation
                    else {}
                ),
                **(
                    {"calibration_reservation": True}
                    if calibration_reservation
                    else {}
                ),
            }
            if ripple_reservation or alignment_reservation or calibration_reservation
            else None
        ),
        "verify": (
            {
                "outcome": verify_outcome,
                # WHICH VERDICT produced that outcome (#1974): "inconclusive"
                # is reached by two verdicts sharing no mechanism, and the done
                # screen must name the right one. NOT read from ``failure.code``
                # below, which is the most recent rejection of ANY phase and is
                # nulled by a later persist while this outcome still stands.
                **(
                    {"code": conductor.verify_code}
                    if conductor.verify_code
                    else {}
                ),
                # WHAT THE GATE DID, on EVERY outcome. The sentence is
                # ``gate_disclosure.describe_gate``'s, composed once at verdict
                # time and rendered verbatim: a bare window length reads as
                # "reflections removed" when it often means "no reflection
                # found; window capped". ``reflection_measured`` beside it is
                # the fact the household copy branches on (#1966).
                **(
                    {"gate": dict(conductor.verify_gate)}
                    if conductor.verify_gate
                    else {}
                ),
                # The verify_fail expert-disclosure numbers, persisted only for
                # a NON-pass outcome: a pass shows the candidate_review card
                # instead and keeps its lean shape.
                **(
                    {"evidence": dict(conductor.verify_evidence)}
                    if (verify_outcome != "pass" and conductor.verify_evidence)
                    else {}
                ),
                # WHAT SPAN was graded, on EVERY outcome including a pass
                # (#1868), so the screen that says "Verified." says over what.
                # The band is not the nominal Fc±1 octave: two clamps move its
                # lower edge up, far enough on a real corpus to sit above the
                # defect under investigation.
                **(
                    {"graded_band_hz": list(conductor.verify_graded_band_hz)}
                    if conductor.verify_graded_band_hz
                    else {}
                ),
                # WHAT FRAME the comparison spanned, on EVERY outcome. VERIFY
                # differences an on-axis MODEL against an in-room MEASUREMENT,
                # and a single tilt between those frames accounted for 84% of
                # one corpus's apparent prediction error. This says how much of
                # the raw numbers was the instrument.
                **(
                    {"frame": dict(conductor.verify_frame)}
                    if conductor.verify_frame
                    else {}
                ),
                # WHICH CLAIMS WERE PROVED, on EVERY outcome including a pass:
                # two of the four are structurally not-evaluated because VERIFY
                # plays one summed sweep, and "Verified." over an unstated claim
                # set reads as all four (#1868).
                **(
                    {"claims": dict(conductor.verify_claims)}
                    if conductor.verify_claims
                    else {}
                ),
                # The level-reference reset this session performed, when the
                # previous session's reference differed enough to be worth
                # saying (#1927). Absent means nothing to disclose, never "we
                # did not reset" — the reset is unconditional.
                **(
                    {"level_reference": dict(conductor.verify_level_reference_reset)}
                    if conductor.verify_level_reference_reset
                    else {}
                ),
                # The delta probe's verdict, on EVERY outcome including a pass
                # (#1811): a non-rollback non-matched verdict reaches no other
                # surface, because the refusal path ignores it by design. A
                # summary, not the whole map — the full record stays in the
                # journal.
                **(
                    {"delta_probe": _delta_probe_summary(conductor.delta_probe)}
                    if getattr(conductor, "delta_probe", None) is not None
                    else {}
                ),
            }
            if verify_outcome is not None else None
        ),
        "failure": (
            {
                "code": failure_code,
                # WHEN this failure happened (#1942), so the envelope can tell
                # a live failure from one a previous day's session left behind.
                # The file-level ``updated_at`` cannot answer it — that is
                # last-write-of-anything. Epoch float, the same type and clock
                # as ``updated_at``, so an age is a subtraction. Stamped at
                # write and never carried forward: every writer is an
                # in-session capture-loop event and no read path persists.
                "at": time.time(),
                **(
                    {"refusals": [str(slug) for slug in failure_refusals]}
                    if failure_refusals else {}
                ),
                # WHAT THE CAPTURE MEASURED about the speaker being audible:
                # ``locate_failed``'s household copy branches on it (#2085), so
                # unlike ``refusals`` above this is not forensics. Present only
                # when established — absent is "no pilot evidence" and ``False``
                # is "measured, and it did not clear the room", so a bare
                # ``False`` would turn a missing measurement into a claim about
                # the room.
                **(
                    {"pilot_heard": bool(failure_pilot_heard)}
                    if failure_pilot_heard is not None else {}
                ),
                # On exactly the key above's terms: absent is "the question
                # does not apply to this code" and the copy owner reads it as
                # the Undo arm, so a bare ``False`` would tell a household with
                # a good anchor that they have none.
                **(
                    {"rollback_anchor_available": bool(failure_rollback_anchor)}
                    if failure_rollback_anchor is not None else {}
                ),
            }
            if failure_code else None
        ),
        # Position-group outcome, per group. Present only for groups that have
        # CLOSED — an absent key means "still walking", never "geometry was
        # fine".
        "cloud": _cloud_summary(conductor),
        # No ``fc_selection`` key: the corner selector that produced one is
        # retired, and the field is absent rather than written as a null. No
        # product path reads a banked one back; the offline scripts still do.
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            # The spec verdict for the curve above, graded ONCE against the
            # full-resolution tuple — a copy of that report, never a re-grade of
            # the decimation the line above wrote. ``None`` means ungradeable,
            # which is not a pass. Rehydrated by the verify-only re-arm, which
            # builds a fresh conductor that never runs a fit.
            "predicted_spec": _predicted_spec_prior(conductor),
            # The delta probe's COMMANDED axis. Produced by the stage-1 fit
            # and consumed by the stage-2 probe, which runs in a different
            # process against a conductor that never ran a fit, so this durable
            # state is the only channel it has.
            "commanded_delta": _decimate_delta(
                getattr(conductor, "measure_commanded_delta", None)
            ),
            # The delta probe's STATE axis beside its CHANGE axis (#2614):
            # what the applied graph declares it does against the uncorrected
            # crossover. Absent degrades downstream to the change axis alone for
            # the two directional safety rules.
            "declared_transfer": _decimate_delta(
                getattr(conductor, "measure_declared_transfer", None)
            ),
            # The MEASURED side of the same comparison (#2522), beside what
            # the correction PREDICTED and COMMANDED. Without it a disputed
            # probe verdict could only be re-examined by measuring again.
            "verify_measured": _decimate_verify_measured(
                getattr(conductor, "verify_tracking_curve", None)
            ),
            # Produced by the stage that MEASURES the candidate and banked by
            # the stage that GRADES it — different sessions in different
            # processes — so durable state is its only channel (#2662).
            "alignment_prescription": getattr(
                conductor, "alignment_prescription_record", None
            ),
            # The crossover pin, on the identical route. Stage 2 re-opens at
            # the topology this names, so without it a pinned round's VERIFY
            # would be graded against the incumbent corner's design target.
            "topology_prescription": getattr(
                conductor, "topology_prescription_record", None
            ),
            # Crosses for the line above's reason: stage 1 TAKES a blend
            # prescription and stage 2 banks the receipt. ``None`` means the
            # blend correction came from the solver, which is what an automatic
            # round banks, so a series read back later can attribute an outcome
            # to the class that produced it.
            "blend_prescription": getattr(
                conductor, "blend_prescription_record", None
            ),
            # …and WHICH document asked, so a later reader can find the
            # evidence packet behind the numbers. Its own key because the record
            # must round-trip through ``blend_prescription_from_mapping``, which
            # REFUSES an unknown field: a digest nested inside would make the
            # whole record unreadable.
            "blend_prescription_sha256": str(
                getattr(conductor, "blend_prescription_sha256", "") or ""
            ),
            # …and WHICH commitment the fit reached. Its own key because the
            # prescription is the REQUEST and this is the OUTCOME: nesting one
            # in the other would leave a round that prescribed nothing nowhere
            # to record an objective it still has.
            "alignment_objective": getattr(
                conductor, "measure_alignment_objective", "",
            ),
            # The measured "before": the summed capture stage 1 takes at the
            # mark immediately before apply, which stage 2's benefit verdict
            # differences its own capture against (#2291).
            #
            # Already bounded at ``round_evidence.BENEFIT_CURVE_MAX_BINS`` at
            # capture time, on BOTH sides of the comparison, so no decimation
            # belongs here: re-gridding one side after the fact is how a grid
            # mismatch gets manufactured.
            "entry_baseline": _entry_baseline_prior(conductor),
            # What stage 1 PROPOSED, as an identity (#2392). The FINGERPRINT
            # travels, never the proposal: reassembling one at VERIFY out of the
            # decimated priors around it would digest to a different value, and
            # a receipt naming a proposal that never existed is worse than one
            # naming the candidate honestly.
            "proposal_fingerprint": str(
                getattr(conductor, "measure_proposal_fingerprint", "") or ""
            ),
            "gate_window_ms": conductor.measure_gate_window_ms,
            # Measurement-honesty gate G3's reference, DATED — history, not a
            # comparator (#1927). The verify-only re-arm hands it to the next
            # conductor as ``verify_pilot_transfer_prior``, which may only
            # disclose it. Carried forward below when this session set no
            # reference of its own, so a re-arm that dies before its first
            # usable VERIFY attempt does not erase the history.
            "pilot_transfer_reference": conductor.verify_pilot_transfer_reference,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    # A conductor that declares no tier of its own — the verify-only re-arm —
    # must not erase which instrument produced the applied result. Carried
    # forward UNCONDITIONALLY, because the re-arm runs under a brand-new relay
    # session id and a session-scoped guard would drop it on "Try again".
    if not state["tier"] and prior.get("tier"):
        state["tier"] = str(prior["tier"])
    # G3's dated reference (#1927) carries forward across the writes of a
    # VERIFY-ONLY session and is dropped by any session that MEASURES.
    #
    # Carried, because a verify session's first writes run BEFORE any usable
    # VERIFY attempt has set its own reference, and a session-id guard is the
    # wrong one for a re-arm under a new relay session id.
    #
    # Dropped by a measuring session, because a pilot transfer is captured
    # THROUGH the applied graph: across an apply the two numbers answer
    # different questions, and a disclosure spanning that boundary would report
    # a graph change as a level-reference move.
    #
    # The predicate is COARSER than "the graph changed": a session that measures
    # and never applies drops the history even though nothing moved. That is the
    # fail-silent direction, and the cost is a disclosure that goes unsaid
    # rather than one that says something untrue.
    if PHASE_MEASURE in snap.session_phases:
        state["verify_priors"]["pilot_transfer_reference"] = None
    elif state["verify_priors"]["pilot_transfer_reference"] is None:
        prior_reference = (prior.get("verify_priors") or {}).get(
            "pilot_transfer_reference"
        )
        if isinstance(prior_reference, Mapping):
            state["verify_priors"]["pilot_transfer_reference"] = dict(prior_reference)
    # The entry baseline needs NO carry-forward, unlike its neighbour above:
    # it is seeded into the SAME field its own capture writes
    # (``measure_entry_baseline``), so a stage-2 persist re-writes the record
    # its conductor was constructed with.
    #
    # The applied flag is host-durable (set by the apply endpoint) — never
    # regressed by a conductor snapshot that predates it.
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        # A verify-only re-arm mints a new relay session around the
        # already-applied candidate and has no candidate object of its own, but
        # the fingerprint is the attempts loop's stable write identity, so
        # erasing it turns recovery into a second record. A measuring session
        # keeps the session-scoped rule, so a new journey cannot inherit a stale
        # candidate before it builds its own.
        if (
            prior.get("session_id") == snap.session_id
            or (
                prior.get("applied") is True
                and PHASE_MEASURE not in snap.session_phases
            )
        ):
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])
    # ``cloud`` carries forward UNCONDITIONALLY whenever THIS conductor's own
    # session has no group phase to report on, not on
    # ``candidate``/``evidence``'s session-scoped guard: a verify-only re-arm
    # has no group phase, so ``_cloud_summary`` returns ``None`` for it because
    # there is nothing to close, and a session-id gate would blank the cloud
    # verdict on the first "Try again".
    #
    # A conductor that DOES have a group phase is left alone: ``None`` there
    # honestly means "this session's group has not closed yet" and must not be
    # papered over with a stale prior verdict.
    from jasper.active_speaker.crossover_v2.journey import GROUP_PHASES, PHASE_MEASURE

    conductor_session_phases = set(getattr(conductor, "session_phases", ()) or ())
    if not (conductor_session_phases & GROUP_PHASES):
        if state["cloud"] is None and isinstance(prior.get("cloud"), Mapping):
            state["cloud"] = dict(prior["cloud"])
        # The cloud bundle-artifact fingerprints ride inside ``evidence``: a
        # group-phase-less session never wires its ``publish_cloud`` seam, so
        # this key has to be restored from ``prior``.
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and "cloud_artifacts" in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                "cloud_artifacts", prior_evidence["cloud_artifacts"]
            )
            state["evidence"] = merged_evidence
    # The household-readable findings projection, carried forward on its OWN
    # predicate rather than the group-phase one above: a finding is banked by
    # the fit, which runs in MEASURE, so a session that does not run MEASURE
    # never had the chance to produce one. The converse matters as much — a
    # session that DOES run MEASURE writes its own projection, empty included,
    # so a fresh measurement that banks nothing clears a previous finding.
    if PHASE_MEASURE not in conductor_session_phases:
        prior_evidence = prior.get("evidence")
        if (
            isinstance(prior_evidence, Mapping)
            and FINDING_HOUSEHOLD_REFS_KEY in prior_evidence
        ):
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                FINDING_HOUSEHOLD_REFS_KEY,
                prior_evidence[FINDING_HOUSEHOLD_REFS_KEY],
            )
            state["evidence"] = merged_evidence
        # The MEASURE reservation (#2087) carries forward on the SAME
        # predicate and for the same reason as the findings projection above,
        # so the household is not shown a reservation on the screen where they
        # DECIDE and then not on the screen that says the speaker is tuned. The
        # converse holds too: a measuring session writes its own value, ``None``
        # included.
        if isinstance(prior.get("measure"), Mapping) and state["measure"] is None:
            state["measure"] = dict(prior["measure"])
    # The HOST-OWNED apply keys below are not conductor-owned — the conductor
    # neither produces nor reads them — so they are absent from the ``state``
    # literal above and every persist would otherwise erase them.
    # ``test_every_host_owned_apply_key_survives_persist_conductor_state``
    # derives that set mechanically and fails on the next one added without a
    # carry-forward line here; a key that genuinely wants session scoping
    # belongs in that test's exception set instead, with its reason.
    #
    # They carry forward with OPPOSITE session-scoping, by design:
    #
    #   * ``previous_candidate_fingerprint`` UNCONDITIONALLY, because the
    #     deferred VERIFY that auto-arms after every successful apply runs
    #     under a BRAND-NEW relay session id, so a session-id gate would lose
    #     the pointer on that first post-apply snapshot.
    #
    #   * ``apply_blocked`` session-scoped (#1605), because it is only set on a
    #     BLOCKED auto-apply, which refuses the deferred VERIFY outright and so
    #     never has to survive a re-arm's rebind. Gating it drops a stale nudge
    #     rather than leaking one session's blocker onto the next.
    state["previous_candidate_fingerprint"] = prior.get(
        "previous_candidate_fingerprint"
    )
    # The pointer's PAIRING (which apply recorded it) is host-owned on
    # identical terms and takes the same unconditional carry: session-scoping it
    # would unpair the pointer on the first post-apply re-arm and silently
    # disarm the round's automatic revert.
    state["previous_candidate_displaced_by"] = prior.get(
        "previous_candidate_displaced_by"
    )
    # ``expected_post_apply_offset_db`` (#1811) takes the pointer's
    # unconditional shape: the CLOUD_VERIFY probe carries rollback authority
    # and re-classifies after the group closes, several persists later, so
    # losing this would let it grade the apply's own headroom charge blind and
    # roll a healthy correction back.
    state["expected_post_apply_offset_db"] = prior.get(
        "expected_post_apply_offset_db"
    )
    state["accepted_sound_revision"] = (prior.get("accepted_sound_revision")
        if PHASE_MEASURE not in snap.session_phases else None)
    # Session-gated to exactly that token: it is the inverse of a save the
    # apply has not yet committed to a graph, readable only while the review
    # that saved Sound is still current. A record that outlived it would be an
    # inverse nothing can apply.
    state["accepted_sound_declaration_change"] = (
        prior.get("accepted_sound_declaration_change")
        if PHASE_MEASURE not in snap.session_phases else None)
    state["apply_blocked"] = (
        prior.get("apply_blocked")
        if prior.get("session_id") == snap.session_id
        else None
    )
    # WHERE this round's receipt landed — round id plus the bundle artifact's
    # fingerprint, so the next round resolves the previous one by identity
    # instead of scanning bundles. Carried forward rather than session-scoped:
    # the receipt describes the graph currently on the speaker, which outlives
    # the session that wrote it.
    receipt_identity = _round_receipt_identity(conductor)
    state["round_receipt"] = (
        receipt_identity
        if receipt_identity is not None
        else prior.get("round_receipt")
    )
    # How many times the ordinal sequence has been RESET, carried forward
    # unconditionally: only the two reset doors increment it, and it has to
    # outlive the session those doors create — session-scoping would erase the
    # disclosure on the first persist after a reset, the round it labels.
    #
    # Imported here rather than at module scope: ``coordinator`` pulls
    # ``program_analysis`` and the numpy stack, and this module is on the
    # socket-activated web host's import path.
    from .coordinator import (
        ROUND_ORDINAL_EPOCH_STATE_KEY,
        round_ordinal_epoch_from_state,
    )

    state[ROUND_ORDINAL_EPOCH_STATE_KEY] = round_ordinal_epoch_from_state(prior)
    return ConductorState(state, receipt_identity is not None)
