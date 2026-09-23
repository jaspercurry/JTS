# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Durable crossover state and advisory VERIFY records."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.json_fields import finite_float as _finite

from .coordinator import ROUND_ORDINAL_EPOCH_STATE_KEY, round_ordinal_epoch_from_state
from .journey import GROUP_PHASES, PHASE_MEASURE
from .topology_prescription import candidate_topology

logger = logging.getLogger(__name__)
MAX_ATTEMPT_HISTORY = 4
PROVENANCE_REALIZED = "realized"


@dataclass(frozen=True)
class AttemptIntegrity:
    comparable: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"comparable": self.comparable, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    metric: str
    provenance: str
    integrity: AttemptIntegrity
    sitting_id: str = ""
    repeats_used: int = 1
    grade_db: float | None = None
    n_graded_bins: int | None = None
    curve_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("AttemptRecord.attempt_id must be non-empty")
        if not self.metric:
            raise ValueError("AttemptRecord.metric must be non-empty")
        if self.provenance not in {"model-graded", PROVENANCE_REALIZED}:
            raise ValueError(f"unknown provenance {self.provenance!r}")
        if self.repeats_used < 1:
            raise ValueError("repeats_used must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "metric": self.metric,
            "provenance": self.provenance,
            "sitting_id": self.sitting_id,
            "integrity": self.integrity.to_dict(),
            "repeats_used": self.repeats_used,
            "grade_db": self.grade_db,
            "n_graded_bins": self.n_graded_bins,
            "curve_refs": list(self.curve_refs),
        }


DEFAULT_V2_STATE_PATH = Path("/var/lib/jasper/active_speaker_crossover_v2_state.json")

__all__ = [
    "DEFAULT_V2_STATE_PATH",
    "FINDING_HOUSEHOLD_REFS_KEY",
    "MAX_PERSISTED_SUM_POINTS",
    "ConductorState",
    "V2ConductorSnapshot",
    "attempt_history_from_state",
    "build_conductor_state",
    "candidate_summary",
]


@dataclass(frozen=True)
class ConductorState:
    state: dict[str, Any]
    durable: bool


@dataclass(frozen=True)
class V2ConductorSnapshot:
    """Durable phase state, bound to the capture session (§5.6).

    Persisted under the session's commissioning run;
    :meth:`CrossoverV2Session.hydrate` keeps the accepted phases only when the
    current session matches, because mic position is unverifiable across
    sessions.
    """

    session_id: str
    accepted_phases: tuple[str, ...] = ()
    applied: bool = False
    gain_plan_db: Mapping[str, float] | None = None
    measure_gain_ceiling_db: Mapping[str, float] | None = None
    measure_sweep_durations_s: Mapping[str, float] | None = None
    candidate_fingerprint: str | None = None
    session_phases: tuple[str, ...] = ()
    attempt_history: tuple[AttemptRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "accepted_phases": list(self.accepted_phases),
            "applied": self.applied,
            "gain_plan_db": dict(self.gain_plan_db) if self.gain_plan_db else None,
            "measure_gain_ceiling_db": dict(self.measure_gain_ceiling_db or {}),
            "measure_sweep_durations_s": (
                dict(self.measure_sweep_durations_s)
                if self.measure_sweep_durations_s
                else None
            ),
            "candidate_fingerprint": self.candidate_fingerprint,
            "session_phases": list(self.session_phases),
            "attempt_history": [item.to_dict() for item in self.attempt_history],
        }


FINDING_HOUSEHOLD_REFS_KEY = "household_findings"
MAX_PERSISTED_SUM_POINTS = 512


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

    from jasper.audio_measurement.spatial_combine import (  # lazy: spatial analysis import cost
        decimate_curve_to_analysis_grid,
    )

    grid, curve_db = decimate_curve_to_analysis_grid(
        np.asarray(freqs, dtype=float),
        np.asarray(mags, dtype=float),
        max_bins=MAX_PERSISTED_SUM_POINTS,
    )
    return {
        "freqs_hz": [float(f) for f in grid],
        "magnitude_db": [float(m) for m in curve_db],
    }


def attempt_history_from_state(raw: Any) -> tuple[AttemptRecord, ...]:

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
                sitting_id=str(row.get("sitting_id") or ""),
                integrity=AttemptIntegrity(
                    comparable=integrity.get("comparable") is True,
                    reasons=tuple(
                        str(reason)
                        for reason in integrity.get("reasons", ())
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
                n_graded_bins=(
                    _attempt_optional_positive_int(row.get("n_graded_bins"))
                ),
                curve_refs=tuple(
                    str(ref)
                    for ref in row.get("curve_refs", ())
                    if isinstance(ref, str) and ref
                ),
            )
        except (TypeError, ValueError, OverflowError):
            continue
        restored.append(record)
    return tuple(restored[-MAX_ATTEMPT_HISTORY:])


def _attempt_optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _attempt_optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _entry_baseline_prior(conductor: Any) -> dict[str, Any] | None:
    baseline = getattr(conductor, "measure_entry_baseline", None)
    to_dict = getattr(baseline, "to_dict", None)
    if not callable(to_dict):
        return None
    record = to_dict()
    return dict(record) if isinstance(record, Mapping) else None


def _candidate_headroom_cost_db(linearization: Any) -> float:
    """The applied correction's disclosed max-level cost, dB.

    Thin adapter over the fit module's own reducer, so this payload and the
    conductor's cannot disagree about a household-facing number.
    """
    from jasper.active_speaker.linearization_fit import (
        worst_headroom_cost_db,
    )  # lazy: fitting stack import cost

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
    linearization: Any,
    octaves: Mapping[str, Mapping[str, float]],
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
            str(hz): code
            for hz, code in reasons.items()
            if isinstance(code, str) and code
        }
        if role_reasons:
            out[str(role)] = role_reasons
    return out


def _candidate_octave_driver_classes(
    linearization: Any,
    octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, str]:
    """Declared driver classes for the roles with octave evidence."""
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

    Read off the candidate's ``trim_pinned`` and ``displaced_trim_db``, rather
    than asked of the session.

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


def candidate_summary(
    candidate: Any,
    *,
    topology_pinned: bool = False,
    headroom_cost_basis: str | None = None,
) -> dict[str, Any] | None:
    from jasper.active_speaker.linearization_fit import (  # lazy: fitting stack import cost
        HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN,
    )

    stamped_basis = headroom_cost_basis or HEADROOM_COST_BASIS_REALIZED_PEAK_FULL_DOMAIN

    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    octaves = _candidate_octave_summary(candidate.linearization)
    return {
        "fingerprint": candidate.fingerprint,
        "program_id": candidate.program_id,
        "trims_db": dict(candidate.role_attenuations_db),
        "trims_pinned": _candidate_pinned_trims(candidate),
        "crossover": candidate_topology(candidate),
        "crossover_pinned": bool(topology_pinned),
        "alignment": candidate.alignment.to_dict(),
        "alignment_confidence": analysis.get("alignment_confidence"),
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        "alignment_objective": analysis.get("alignment_objective"),
        **{
            key: analysis.get(key)
            for key in (
                "timing_verdict",
                "timing_saved",
                "timing_verification",
                "repeat_count",
            )
        },
        "polarity_pinned": bool(analysis.get("polarity_pinned")),
        "left_anchor_lobe": analysis.get("left_anchor_lobe"),
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        "linearization_octaves": octaves,
        "linearization_octave_reasons": _candidate_octave_reasons(
            candidate.linearization, octaves
        ),
        "linearization_driver_class": _candidate_octave_driver_classes(
            candidate.linearization, octaves
        ),
        "headroom_cost_db": _candidate_headroom_cost_db(candidate.linearization),
        "headroom_cost_basis": stamped_basis,
    }


def build_conductor_state(
    conductor: Any,
    prior: Mapping[str, Any],
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
    failure_detail: str = "",
) -> ConductorState:
    """The whole document one persist writes, over the one it is replacing.

    ``prior`` is the state currently on disk (``{}`` when there is none); the
    carry-forward rules below read it, which is why this is a document builder
    rather than a projection of the conductor. This function touches no file.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure — FORENSICS, never household copy: the envelope renders
    ``failure["code"]`` through the reason registry and ignores this key.
    """

    snap = conductor.snapshot()
    measure_sweep_durations_s = getattr(snap, "measure_sweep_durations_s", None)
    failure_pilot_heard = (
        getattr(conductor, "last_failure_pilot_heard", None)
        if failure_code == getattr(conductor, "last_failure_code", None)
        else None
    )
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
                item.to_dict() for item in (getattr(snap, "attempt_history", ()) or ())
            ],
        }
    else:
        prior_attempts = prior.get("attempts_loop")
        attempts_loop_state = (
            dict(prior_attempts) if isinstance(prior_attempts, Mapping) else None
        )
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "accepted_phases": list(snap.accepted_phases),
        "session_phases": list(snap.session_phases),
        "applied": snap.applied,
        "gain_plan_db": dict(snap.gain_plan_db) if snap.gain_plan_db else None,
        "measure_gain_ceiling_db": dict(
            getattr(snap, "measure_gain_ceiling_db", None) or {}
        ),
        "measure_sweep_durations_s": (
            dict(measure_sweep_durations_s) if measure_sweep_durations_s else None
        ),
        "attempts_loop": attempts_loop_state,
        "candidate": None,
        "sound_design_revision": (
            getattr(conductor, "sound_design_revision", None)
            if getattr(conductor, "sound_design_revision", None) is not None
            else prior.get("sound_design_revision")
        ),
        "measure": None,
        "verify": None,
        "failure": (
            {
                "code": failure_code,
                **({"detail": failure_detail} if failure_detail else {}),
                "at": time.time(),
                **(
                    {"refusals": [str(slug) for slug in failure_refusals]}
                    if failure_refusals
                    else {}
                ),
                **(
                    {"pilot_heard": bool(failure_pilot_heard)}
                    if failure_pilot_heard is not None
                    else {}
                ),
            }
            if failure_code
            else None
        ),
        "cloud": None,
        "verify_priors": {
            "predicted_sum": _decimate_sum(conductor.measure_predicted_sum),
            "predicted_spec": None,
            "commanded_delta": None,
            "declared_transfer": None,
            "verify_measured": None,
            "alignment_objective": "",
            "entry_baseline": _entry_baseline_prior(conductor),
            "proposal_fingerprint": "",
            "gate_window_ms": None,
            "pilot_transfer_reference": None,
        },
        "evidence": dict(evidence) if evidence else None,
    }
    if PHASE_MEASURE in snap.session_phases:
        state["verify_priors"]["pilot_transfer_reference"] = None
    elif state["verify_priors"]["pilot_transfer_reference"] is None:
        prior_reference = (prior.get("verify_priors") or {}).get(
            "pilot_transfer_reference"
        )
        if isinstance(prior_reference, Mapping):
            state["verify_priors"]["pilot_transfer_reference"] = dict(prior_reference)
    if prior.get("applied") is True and prior.get("session_id") == snap.session_id:
        state["applied"] = True
    if state["candidate"] is None and isinstance(prior.get("candidate"), Mapping):
        if prior.get("session_id") == snap.session_id or (
            prior.get("applied") is True and PHASE_MEASURE not in snap.session_phases
        ):
            state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping):
        if prior.get("session_id") == snap.session_id:
            state["evidence"] = dict(prior["evidence"])

    conductor_session_phases = set(getattr(conductor, "session_phases", ()) or ())
    if not (conductor_session_phases & GROUP_PHASES):
        if state["cloud"] is None and isinstance(prior.get("cloud"), Mapping):
            state["cloud"] = dict(prior["cloud"])
        prior_evidence = prior.get("evidence")
        if isinstance(prior_evidence, Mapping) and "cloud_artifacts" in prior_evidence:
            merged_evidence = dict(state["evidence"] or {})
            merged_evidence.setdefault(
                "cloud_artifacts", prior_evidence["cloud_artifacts"]
            )
            state["evidence"] = merged_evidence
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
        if isinstance(prior.get("measure"), Mapping) and state["measure"] is None:
            state["measure"] = dict(prior["measure"])
    for key in ("previous_applied_profile", "accepted_sound_candidate_fingerprint"):
        if key in prior:
            state[key] = prior[key]
    state["previous_candidate_fingerprint"] = prior.get(
        "previous_candidate_fingerprint"
    )
    state["previous_candidate_displaced_by"] = prior.get(
        "previous_candidate_displaced_by"
    )
    state["expected_post_apply_offset_db"] = prior.get("expected_post_apply_offset_db")
    for key in ("accepted_sound_revision", "accepted_sound_declaration_change"):
        state[key] = (
            prior.get(key)
            if prior.get("accepted_sound_candidate_fingerprint")
            or PHASE_MEASURE not in snap.session_phases
            else None
        )
    state["round_receipt"] = prior.get("round_receipt")
    state[ROUND_ORDINAL_EPOCH_STATE_KEY] = round_ordinal_epoch_from_state(prior)
    return ConductorState(state, False)
