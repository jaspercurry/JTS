# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Replay the S3 attempts loop over banked captures. Offline, no hardware.

Feeds recorded tuning attempts to
:mod:`jasper.active_speaker.attempts_loop` and writes down what the loop
decided at every step. It opens no device, plays nothing, contacts no Pi, and
reads only JSON that some earlier session already analysed.

Two banks, either or both in one run:

``--repeat-floor <dir>``
    The 2026-07-31 repeat-floor study (``captures/repeat-floor-20260731/``):
    sixteen back-to-back VERIFY measurements of one **unchanged** profile with
    the mic bolted down. Nothing was tuned between them, so every consecutive
    change is instrument noise and none of it may reach the claim floor. That
    is the stop rule under test, on real noise.

``--sessions <dir>``
    Recorded commissioning sessions (``captures/r11-loop-proof-corpus/
    sessions/``), one fitted candidate each, replayed oldest-first as an
    improvement arc — graded **per driver role**, because that is the grain the
    banked numbers exist at and pooling them would be a metric this repo does
    not ship.

**Exit codes are three-state, because the evidence is.**

* ``0`` — every run reached decisions, and no run contradicted what its bank
  asserts.
* ``1`` — the finding: on the unchanged-profile bank a consecutive change
  reached the claim floor, so the loop read a change on a speaker nobody
  touched. Only ``--repeat-floor`` can produce this, because it is the only
  bank carrying a known-unchanged control to contradict; a session arc has no
  such control and is reported, not asserted against.
* ``2`` — no verdict: bad inputs, a bank that does not parse, or a run with
  nothing gradeable in it. Absence of evidence must not read as a pass (see
  :mod:`jasper.active_speaker.delta_probe`'s ``unavailable`` doctrine).
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.active_speaker.attempts_loop import (
    CONTINUE,
    FLOOR_SCOPE_ACROSS_SITTINGS,
    PROVENANCE_MODEL_GRADED,
    PROVENANCE_REALIZED,
    AttemptBudget,
    AttemptIntegrity,
    AttemptRecord,
    FloorStats,
    LoopDecision,
    first_stop_index,
    material_improvement_db,
    percentile,
    replay,
    summarize,
)
from jasper.active_speaker.model_error_store import adopt_floor, load_state

REPORT_KIND = "jts_attempts_loop_replay"
SCHEMA_VERSION = 1

#: The shipped VERIFY grade: worst deviation from the predicted summed
#: response over the tracking band, with detected nulls excluded.
METRIC_VERIFY_MAX_NOTCH_EXCLUDED = "max_db_notch_excluded"
#: One driver's linearization fit residual, RMS over its own fit band.
METRIC_LINEARIZATION_RESIDUAL_RMS = "linearization_residual_rms_db"

#: The repeat bank is a single uninterrupted sitting — one applied profile,
#: re-measured with the microphone bolted in place — so every attempt replayed
#: out of it carries one sitting id (#2081). Its value is opaque; only the fact
#: that all the records share it is load-bearing.
_REPEAT_BANK_SITTING_ID = "repeat-floor-20260731"


class ReplayError(Exception):
    """A bank could not be read as the shape this driver replays."""


# --------------------------------------------------------------------------
# A replayed run
# --------------------------------------------------------------------------


class Run:
    """One sequence of attempts, its floor, and what the loop decided."""

    def __init__(
        self,
        *,
        run_id: str,
        label: str,
        attempts: list[AttemptRecord],
        floor: FloorStats,
        provenance: Mapping[str, Any],
        asserts_no_detectable_change: bool,
        budget: AttemptBudget,
    ) -> None:
        self.run_id = run_id
        self.label = label
        self.attempts = attempts
        self.floor = floor
        self.provenance = dict(provenance)
        self.asserts_no_detectable_change = asserts_no_detectable_change
        self.decisions: list[LoopDecision] = replay(attempts, floor, budget=budget)
        self.store_path: Path | None = None

    @property
    def gradeable(self) -> bool:
        """Did anything in this run reach a comparison at all?

        A run of one attempt, or one where the instrument's gate rejected
        enough captures that no two comparable ones ever landed side by side,
        produced no verdict — and "no verdict" is not a pass.

        **A magnitude is the only evidence a comparison happened.** A two-id
        basis is not: the kernel attaches the pair to its *refusals* too
        (``predecessor_not_comparable``, ``provenance_mismatch``,
        ``no_deviation_available``, ``direction_unknown_above_floor``) so a
        reader can see which two attempts it declined to compare. Reading that
        as evidence let a bank whose gate rejected alternating captures — zero
        magnitudes anywhere — exit 0 "consistent", which is exactly the
        absence-of-evidence-as-pass this module's own contract forbids.
        """

        return any(
            decision.magnitude_db is not None for decision in self.decisions
        )

    @property
    def improvement_claims(self) -> list[LoopDecision]:
        """Decisions that claimed the tune got better."""

        return [
            decision
            for decision in self.decisions
            if decision.decision == CONTINUE and decision.improved is True
        ]

    @property
    def contradictions(self) -> list[LoopDecision]:
        """Decisions a known-unchanged bank says should not exist.

        On a bank where nothing was tuned between attempts, **any** change
        reaching the claim floor contradicts the bank — whether the loop went
        on to call it an improvement or merely could not tell which way it
        went. Asserting on improvement claims alone would be unfalsifiable
        here: the shipped comparator reports an unsigned magnitude and no
        absolute per-capture grade, so ``improved`` is structurally never
        ``True`` on this bank and a test of it would pass for the wrong
        reason. Both are checked; the floor crossing is the one with teeth.
        """

        if not self.asserts_no_detectable_change:
            return []
        offending = [
            decision
            for decision in self.decisions
            if decision.magnitude_db is not None
            and decision.magnitude_db >= self.floor.claim_floor_db
        ]
        return offending + [
            claim for claim in self.improvement_claims if claim not in offending
        ]

    @property
    def contradicted(self) -> bool:
        return bool(self.contradictions)

    def to_dict(self) -> dict[str, Any]:
        stop_at = first_stop_index(self.decisions)
        return {
            "run_id": self.run_id,
            "label": self.label,
            "floor": self.floor.to_dict(),
            "provenance": self.provenance,
            "asserts_no_detectable_change": self.asserts_no_detectable_change,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "decisions": [decision.to_dict() for decision in self.decisions],
            "first_stop_index": stop_at,
            "first_stop_attempt_id": (
                self.attempts[stop_at].attempt_id if stop_at is not None else None
            ),
            "decision_counts": dict(summarize(self.decisions)),
            "improvement_claims": len(self.improvement_claims),
            "contradictions": [
                decision.to_dict() for decision in self.contradictions
            ],
            "contradicted": self.contradicted,
            "gradeable": self.gradeable,
            "model_error_store": (
                str(self.store_path) if self.store_path is not None else None
            ),
        }


# --------------------------------------------------------------------------
# Mode A — the unchanged-profile repeat-floor bank
# --------------------------------------------------------------------------

_REFINED_RELATIVE = Path("analysis/refined_A_product_level.json")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayError(f"{path} does not exist") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayError(f"{path} could not be read as JSON: {exc}") from exc


def build_repeat_floor_run(bank: Path, *, budget: AttemptBudget) -> Run:
    """Sixteen repeats of one unchanged profile, as sixteen tuning attempts.

    The bank's ``refined_*`` analysis is the source, not the raw ``floor_*``
    one, because ``refined`` is where the shipped acceptance gate has already
    been applied per capture. That gate's verdict becomes each attempt's
    comparability: a capture the product would have thrown away has no grade
    here either, rather than a bad one.

    The floor is derived from **this same bank's** consecutive pairs, which
    makes this run a self-consistency demonstration and not an independent
    validation of the threshold. It shows the rule does the right thing on real
    noise; it cannot show the threshold is right, because the threshold came
    from the noise. The generated report says so in the same words.
    """

    refined = _read_json(bank / _REFINED_RELATIVE)
    if not isinstance(refined, Mapping):
        raise ReplayError(f"{bank / _REFINED_RELATIVE} is not a JSON object")

    verdicts = refined.get("verdicts")
    pairs = (
        refined.get("shipped_comparator_accepted_pairs", {})
        .get("vs_predecessor", {})
    )
    fixed = (
        refined.get("shipped_comparator_accepted_pairs", {})
        .get("vs_repeat_1", {})
    )
    rows = pairs.get("rows") if isinstance(pairs, Mapping) else None
    fixed_rows = fixed.get("rows") if isinstance(fixed, Mapping) else None
    if not isinstance(verdicts, list) or not verdicts:
        raise ReplayError("bank carries no per-capture verdicts")
    if not isinstance(rows, list) or not rows:
        raise ReplayError("bank carries no accepted consecutive pairs")

    deviation_by_repeat: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        repeat = row.get("repeat")
        reference = row.get("reference_repeat")
        value = row.get("max_db_notch_excluded")
        if not isinstance(repeat, int) or not isinstance(reference, int):
            continue
        if not isinstance(value, (int, float)):
            continue
        if reference != repeat - 1:
            # The kernel grades against the immediate predecessor only. A row
            # spanning a gap is not a consecutive pair and is not replayed as
            # one.
            continue
        deviation_by_repeat[repeat] = float(value)

    if not deviation_by_repeat:
        raise ReplayError("no immediate-predecessor pairs found in the bank")

    consecutive = sorted(deviation_by_repeat.values())
    floor = FloorStats.from_repeat_study(
        metric=METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
        median_db=percentile(consecutive, 50.0),
        p95_db=percentile(consecutive, 95.0),
        source=(
            f"{bank.name}: {len(consecutive)} accepted consecutive pairs of an "
            "unchanged applied profile, jts3, mic fixed"
        ),
        measured_at="2026-07-31",
    )

    attempts: list[AttemptRecord] = []
    for verdict in verdicts:
        if not isinstance(verdict, Mapping):
            continue
        repeat = verdict.get("repeat")
        if not isinstance(repeat, int):
            continue
        reasons = verdict.get("reasons")
        attempts.append(AttemptRecord(
            attempt_id=f"r{repeat:02d}",
            metric=METRIC_VERIFY_MAX_NOTCH_EXCLUDED,
            # Realized: these are captures of a profile already applied to the
            # hardware, graded through the shipped comparator. Nothing here is
            # a prediction.
            provenance=PROVENANCE_REALIZED,
            # ONE sitting for the whole bank, and the same fact the floor is
            # derived from: these repeats were taken back-to-back with the mic
            # bolted in place. So the within-sitting floor licenses every pair
            # here, which is exactly the comparison it was measured for — this
            # replay is the one corpus in the repo where that is true (#2081).
            sitting_id=_REPEAT_BANK_SITTING_ID,
            integrity=AttemptIntegrity(
                comparable=bool(verdict.get("accepted")),
                reasons=tuple(str(item) for item in reasons)
                if isinstance(reasons, list) else (),
            ),
            repeats_used=1,
            deviation_from_predecessor_db=deviation_by_repeat.get(repeat),
        ))

    provenance: dict[str, Any] = {
        "bank": str(bank),
        "analysis_file": str(bank / _REFINED_RELATIVE),
        "what_changed_between_attempts": "nothing — one applied profile, re-measured",
        "n_captures": refined.get("n_captures"),
        "n_accepted_by_shipped_gate": refined.get("n_accepted_by_shipped_gate"),
        "consecutive_pairs": {
            "n": len(consecutive),
            "median_db": percentile(consecutive, 50.0),
            "p95_db": percentile(consecutive, 95.0),
            "max_db": consecutive[-1],
        },
        "floor_is_derived_from_this_bank": True,
    }
    fixed_values = _fixed_baseline_values(fixed_rows)
    if fixed_values:
        provenance["fixed_baseline_pairs"] = {
            "n": len(fixed_values),
            "median_db": percentile(fixed_values, 50.0),
            "p95_db": percentile(fixed_values, 95.0),
            "max_db": max(fixed_values),
        }

    return Run(
        run_id="repeat-floor-unchanged-profile",
        label="16 VERIFY repeats of one unchanged profile (jts3, mic fixed)",
        attempts=attempts,
        floor=floor,
        provenance=provenance,
        asserts_no_detectable_change=True,
        budget=budget,
    )


def _fixed_baseline_values(rows: Any) -> list[float]:
    """The same captures graded against repeat 1 — the mode the kernel refuses.

    Reported only as evidence for why it refuses: on this bank the fixed
    baseline's deviations are measurably larger than the consecutive ones, and
    they trend upward while the consecutive ones do not.
    """

    if not isinstance(rows, list):
        return []
    values: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("max_db_notch_excluded")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


# --------------------------------------------------------------------------
# Mode B — the recorded session arc
# --------------------------------------------------------------------------

_ROLES = ("woofer", "tweeter")


def _load_sessions(sessions_dir: Path) -> list[dict[str, Any]]:
    """Every session bundle under ``sessions_dir``, oldest first.

    **Bundles** are ordered by the bundle's own ``started_at``, never by
    directory name — the ids are opaque and sorting them would be sorting
    nothing.

    **Candidates within one bundle cannot be ordered at all**, so a bundle
    carrying more than one is refused rather than picked from. Nothing in the
    bundle timestamps a candidate: ``candidate.json`` has no ``created_at`` or
    equivalent, and ``artifact_manifest.json`` — which does carry a
    ``recorded_at`` per artifact — lists only ``info.json`` (checked against
    both corpus bundles, 2026-08-02). With no ordering key, taking the
    lexicographically first capture id would be choosing which tune this
    attempt represents by coin flip, and grading the wrong tune is worse than
    grading none. If a multi-candidate bundle ever needs replaying, give the
    manifest a candidate entry and order by that; do not restore a silent pick.
    """

    loaded: list[dict[str, Any]] = []
    for info_path in sorted(sessions_dir.glob("*/info.json")):
        info = _read_json(info_path)
        if not isinstance(info, Mapping):
            continue
        candidates = sorted(
            info_path.parent.glob(
                "evidence/v1/artifacts/crossover_v2/*/candidate.json",
            ),
        )
        if not candidates:
            continue
        if len(candidates) > 1:
            raise ReplayError(
                f"{info_path.parent.name} carries {len(candidates)} candidates "
                "and nothing in the bundle orders them, so which tune this "
                "attempt represents is undecidable: "
                + ", ".join(path.parent.name for path in candidates),
            )
        candidate = _read_json(candidates[0])
        if not isinstance(candidate, Mapping):
            continue
        started_at = info.get("started_at")
        loaded.append({
            "session_id": str(info.get("session_id") or info_path.parent.name),
            "started_at": float(started_at)
            if isinstance(started_at, (int, float)) else 0.0,
            "build_sha": str(
                (info.get("fingerprints") or {}).get("build_sha") or "",
            ),
            "topology_fingerprint": str(
                (info.get("fingerprints") or {}).get("topology_fingerprint") or "",
            ),
            "candidate_path": str(candidates[0]),
            "candidate": candidate,
        })
    loaded.sort(key=lambda item: item["started_at"])
    return loaded


def build_session_runs(
    sessions_dir: Path, *, budget: AttemptBudget,
) -> list[Run]:
    """One run per driver role over the recorded sessions, oldest first.

    **Which number is the grade, and why not the obvious ones.**
    ``linearization.<role>.residual_rms_db`` — how far the fitted correction
    still sits from its target across that role's fit band. Lower is better and
    it is derived from the session's own captures.

    Two nearby numbers are deliberately not used.
    ``analysis.predicted_ripple_db`` is measured on the independently aligned
    branch sum and asks a *capture-quality* question ("how coherently can these
    two branches sum at all"), per ``CrossoverCandidate``'s own docstring — it
    would grade the microphone, not the tune. ``flatness_improvement_db`` is
    ``seed_ripple − committed_ripple``, what the (polarity, delay) objective
    bought over the correlation seed on a decision already taken, so it is
    backward-looking and belongs nowhere near a "what is left to win" input.

    Every grade here is **model-graded**: a fit residual is what the model
    predicts about itself. No VERIFY analysis survived in either bundle, so
    there is no realized grade to pair with it — which is exactly the rung-P4
    gap :mod:`jasper.active_speaker.model_error_store` exists to close.
    """

    sessions = _load_sessions(sessions_dir)
    if len(sessions) < 2:
        raise ReplayError(
            f"{sessions_dir} holds {len(sessions)} replayable session(s); an arc "
            "needs at least two",
        )

    floor = FloorStats.from_policy_bar(
        metric=METRIC_LINEARIZATION_RESIDUAL_RMS,
        claim_floor_db=material_improvement_db(),
        source=(
            "crossover_v2_flow.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB — the "
            "shipped bar for a model-graded improvement worth applying, set to "
            "the gap between what the correction model predicts and what the "
            "hardware realizes on JTS3. No repeat study has measured a floor "
            "for this metric."
        ),
        # ACROSS sittings, and stated rather than defaulted (#2081). The bar
        # bounds the MODEL's own tracking error, not an instrument's wander
        # between two captures, so nothing about it is scoped to a microphone
        # placement — which is just as well, since these attempts are recorded
        # commissioning sessions and are cross-sitting by construction. The
        # confound this replay already discloses is a build difference, not a
        # mic one; grading these against a fixed-mic repeat floor is what
        # would be dishonest, and that floor is not what this is.
        scope=FLOOR_SCOPE_ACROSS_SITTINGS,
    )

    runs: list[Run] = []
    for role in _ROLES:
        attempts: list[AttemptRecord] = []
        bands: list[tuple[float, float] | None] = []
        for session in sessions:
            candidate = session["candidate"]
            fit = (candidate.get("linearization") or {}).get(role)
            band = _fit_band(fit)
            bands.append(band)
            attempts.append(AttemptRecord(
                attempt_id=session["session_id"],
                metric=METRIC_LINEARIZATION_RESIDUAL_RMS,
                provenance=PROVENANCE_MODEL_GRADED,
                # Recorded honestly even though this run's floor is
                # across-sittings and will not read it: one commissioning
                # session is one sitting, and a record that knows where it came
                # from stays gradeable if a narrower floor is ever adopted for
                # this metric (#2081).
                sitting_id=session["session_id"],
                integrity=_session_integrity(candidate, fit, band, bands),
                repeats_used=_int_or(fit, "n_repeats", 1),
                grade_db=_float_or_none(fit, "residual_rms_db"),
                curve_refs=(session["candidate_path"],),
            ))
        runs.append(Run(
            run_id=f"session-arc-{role}",
            label=(
                f"{len(sessions)} recorded commissioning sessions, {role} "
                "linearization residual"
            ),
            attempts=attempts,
            floor=floor,
            provenance={
                "sessions_dir": str(sessions_dir),
                "role": role,
                "ordered_by": "bundle info.json started_at",
                "sessions": [
                    {
                        "session_id": session["session_id"],
                        "started_at": session["started_at"],
                        "build_sha": session["build_sha"],
                        "topology_fingerprint": session["topology_fingerprint"],
                        "candidate": session["candidate_path"],
                        "fit_band_hz": list(band) if band else None,
                    }
                    for session, band in zip(sessions, bands)
                ],
                "confound": (
                    "the two sessions ran under different product builds, so "
                    "an improvement here is attempt-over-attempt and its CAUSE "
                    "is not isolated. The loop grades change, not cause."
                ),
                "no_realized_grade": (
                    "neither bundle kept a VERIFY analysis, so every grade is "
                    "model-graded and no model error can be banked from this "
                    "corpus"
                ),
            },
            asserts_no_detectable_change=False,
            budget=budget,
        ))
    return runs


def _fit_band(fit: Any) -> tuple[float, float] | None:
    if not isinstance(fit, Mapping):
        return None
    band = fit.get("fit_band_hz")
    if not isinstance(band, list) or len(band) != 2:
        return None
    if not all(isinstance(edge, (int, float)) for edge in band):
        return None
    return (float(band[0]), float(band[1]))


def _session_integrity(
    candidate: Mapping[str, Any],
    fit: Any,
    band: tuple[float, float] | None,
    bands_so_far: Sequence[tuple[float, float] | None],
) -> AttemptIntegrity:
    """Whether this session's grade may be compared to the previous one's.

    Three ways it may not be: the fit refused, the role is absent, or the fit
    band moved. A residual measured over a different span is a different
    number wearing the same name — the same denominator hazard
    :func:`~jasper.active_speaker.flat_spec.spec_convergence_residual` names,
    caught here at the band rather than the bin count because the band is what
    the bundles record.
    """

    reasons: list[str] = []
    outcome = candidate.get("linearization_outcome")
    if outcome != "fitted":
        reasons.append(f"linearization_outcome={outcome!r}")
    if not isinstance(fit, Mapping):
        reasons.append("role_absent_from_linearization")
    elif not isinstance(fit.get("residual_rms_db"), (int, float)):
        reasons.append("residual_rms_db_missing")
    if band is None:
        reasons.append("fit_band_unreadable")
    else:
        earlier = [item for item in bands_so_far[:-1] if item is not None]
        if earlier and earlier[-1] != band:
            reasons.append(
                f"fit_band_changed {earlier[-1]} -> {band}",
            )
    return AttemptIntegrity(comparable=not reasons, reasons=tuple(reasons))


def _float_or_none(fit: Any, key: str) -> float | None:
    if not isinstance(fit, Mapping):
        return None
    value = fit.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or(fit: Any, key: str, default: int) -> int:
    if not isinstance(fit, Mapping):
        return default
    value = fit.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _fmt(value: float | None, places: int = 4) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _attempt_table(run: Run) -> list[str]:
    """Attempt N, its grade, its change from the predecessor, the decision."""

    header = (
        "| # | attempt | grade (dB) | Δ vs predecessor (dB) | floor (dB) | "
        "decision | reason |"
    )
    lines = [header, "|---|---|---|---|---|---|---|"]
    for index, (attempt, decision) in enumerate(
        zip(run.attempts, run.decisions), start=1,
    ):
        note = ""
        if not attempt.integrity.comparable and attempt.integrity.reasons:
            note = f" ({'; '.join(attempt.integrity.reasons)})"
        lines.append(
            f"| {index} | `{attempt.attempt_id}` | {_fmt(attempt.grade_db)} | "
            f"{_fmt(decision.magnitude_db)} | "
            f"{_fmt(run.floor.claim_floor_db, 5)} | "
            f"**{decision.decision}** | {decision.reason}{note} |",
        )
    return lines


def _provenance_line(run: Run) -> str:
    """State the grade provenance of every delta in this run, in words.

    The household wire will require deltas labelled model-vs-model, and a
    label that only exists in the JSON is a label the reader never sees. This
    is derived from the decisions rather than restated, so it cannot drift
    from what the kernel actually compared.
    """

    labels = {
        decision.provenance
        for decision in run.decisions
        if len(decision.basis_attempt_ids) == 2 and decision.provenance
    }
    grades = {attempt.provenance for attempt in run.attempts}
    if not labels:
        return (
            f"Grades in this run are **{'/'.join(sorted(grades))}**. No delta "
            "was graded, so nothing here is a comparison."
        )
    if len(labels) == 1:
        label = next(iter(labels))
        detail = (
            "measured from captures of a profile already applied to the hardware"
            if label == PROVENANCE_REALIZED
            else "predicted by a fit and never re-measured after applying it"
        )
        return (
            f"Every delta below is **{label} vs {label}** — {detail}. The "
            "kernel refuses to compare across the two."
        )
    return (
        "Deltas in this run carry mixed provenance "
        f"({', '.join(sorted(labels))}), which the kernel refuses to compare."
    )


def _constraint_one_evidence(run: Run) -> list[str]:
    """The measured case against a fixed baseline, from this bank's own rows.

    Only emitted when the bank carried both pairings. Prose, because the
    numbers are already in the provenance block and restating them in two
    places is how two numbers start disagreeing.
    """

    consecutive = run.provenance.get("consecutive_pairs")
    fixed = run.provenance.get("fixed_baseline_pairs")
    if not isinstance(consecutive, Mapping) or not isinstance(fixed, Mapping):
        return []
    near = float(consecutive["median_db"])
    far = float(fixed["median_db"])
    if near <= 0.0:
        return []
    return [
        "",
        "### Why consecutive, not a fixed baseline",
        "",
        f"The same captures graded against repeat 1 instead of against the "
        f"predecessor give a median deviation of {far:.5f} dB against "
        f"{near:.5f} dB — **{far / near:.2f}x larger**, on identical data. "
        f"That gap is drift the fixed baseline accumulates and the consecutive "
        f"comparison does not. The kernel therefore has no fixed-baseline "
        f"mode at all; it reads the last two attempts and nothing else.",
    ]


def _run_prose(run: Run) -> list[str]:
    stop_at = first_stop_index(run.decisions)
    counts = summarize(run.decisions)
    lines = [
        f"## {run.label}",
        "",
        f"Run id: `{run.run_id}`",
        "",
        _provenance_line(run),
        "",
    ]

    if stop_at is None:
        lines.append(
            f"A live loop would not have stopped inside this bank — all "
            f"{len(run.attempts)} attempt(s) read as `continue`, so the bank "
            f"ran out before the loop did.",
        )
    else:
        stop = run.decisions[stop_at]
        lines.append(
            f"**A live loop stops at attempt {stop_at + 1} "
            f"(`{run.attempts[stop_at].attempt_id}`) with `{stop.decision}` — "
            f"{stop.reason}.**",
        )
        lines.append("")
        lines.append(_why_that_reason(run, stop))
    lines.append("")
    lines.append(
        "The table below is the whole walk: what the loop *would* have decided "
        "at each attempt had it kept going. A live loop stops at the first "
        "non-`continue` row; the rest is there so you can see the stop holds "
        "for the same reason every time, not just once.",
    )
    lines.append("")
    lines.extend(_attempt_table(run))
    lines.append("")
    lines.append(
        "Decision counts across the walk: "
        + ", ".join(f"`{name}` {count}" for name, count in counts.items() if count)
        + ".",
    )
    lines.append("")
    lines.append("### Floor")
    lines.append("")
    lines.append(
        f"`{run.floor.metric}`, claim floor **{run.floor.claim_floor_db:.5f} dB**, "
        f"basis `{run.floor.basis}`.",
    )
    if run.floor.p95_db is not None and run.floor.median_db is not None:
        lines.append("")
        lines.append(
            f"Measured: median {run.floor.median_db:.5f} dB, p95 "
            f"{run.floor.p95_db:.5f} dB across consecutive pairs. The claim "
            f"floor is 2 x p95 — computed, not transcribed. The study's README "
            f"states the same rule as \"at least 0.2 dB\"; that is this number "
            f"rounded up for a household-facing sentence, not a different "
            f"threshold.",
        )
    lines.append("")
    lines.append(f"Provenance: {run.floor.source}")
    lines.extend(_constraint_one_evidence(run))
    lines.append("")
    lines.append("### Where these numbers came from")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(run.provenance, indent=2, sort_keys=True))
    lines.append("```")
    if run.store_path is not None:
        lines.append("")
        lines.append(
            f"Model-error store for this run: `{run.store_path.name}` "
            "(floor adopted; see the run notes above for whether any model "
            "error could be banked).",
        )
    return lines


def _why_that_reason(run: Run, stop: LoopDecision) -> str:
    """One plain sentence on why the stop reason is the right one."""

    if stop.decision == "stop_floor":
        return (
            f"That is the right reason: the change from the predecessor is "
            f"{_fmt(stop.magnitude_db)} dB against a claim floor of "
            f"{run.floor.claim_floor_db:.5f} dB. Nothing was tuned between "
            f"these two measurements, so the only honest reading of a "
            f"sub-floor change is that the instrument cannot tell them apart "
            f"— and the loop says so instead of banking it as progress."
            if run.asserts_no_detectable_change else
            f"The change from the predecessor is {_fmt(stop.magnitude_db)} dB, "
            f"below the {run.floor.claim_floor_db:.5f} dB claim floor, so it "
            f"cannot be claimed either way."
        )
    if stop.decision == "stop_evidence":
        detail = "; ".join(stop.notes) if stop.notes else stop.reason
        return (
            f"That is the right reason: the attempt could not be graded "
            f"({detail}), and an ungradeable attempt has no grade rather than "
            f"a bad one. The loop refuses rather than guessing."
        )
    if stop.decision == "stop_converged":
        return (
            "That is the right reason: there was no material improvement left "
            "to win, so further attempts would spend the household's time on "
            "nothing."
        )
    return (
        "That is the right reason: the attempt budget was spent, and the loop "
        "stops rather than running the speaker indefinitely."
    )


def render_readme(
    runs: Sequence[Run], *, command: str, out_dir: Path,
) -> str:
    lines = [
        "# Attempts-loop replay — S3 improve/stop policy on banked captures",
        "",
        "## What ran",
        "",
        "The S3 tuning loop's decision kernel "
        "(`jasper/active_speaker/attempts_loop.py`) replayed over recorded "
        "measurements. No hardware, no playback, no Pi: the driver reads JSON "
        "some earlier session already analysed and writes down what the loop "
        "would have decided at every step.",
        "",
        f"{len(runs)} run(s):",
        "",
    ]
    for run in runs:
        stop_at = first_stop_index(run.decisions)
        verdict = (
            f"stops at attempt {stop_at + 1} with "
            f"`{run.decisions[stop_at].decision}`"
            if stop_at is not None else "never stops inside the bank"
        )
        claims = len(run.improvement_claims)
        claim_text = (
            f"{claims} improvement claim{'' if claims == 1 else 's'}" if claims
            else "no improvement claimed at any step"
        )
        lines.append(f"* **{run.label}** — {verdict}; {claim_text}.")
    lines.extend([
        "",
        "## Reproduce",
        "",
        "```sh",
        command,
        "```",
        "",
        f"Machine-readable output: `{out_dir.name}/report.json`.",
        "",
        "## The rule under test",
        "",
        "A tuning loop that cannot tell a real improvement from its own "
        "measurement noise will happily run forever, banking noise as "
        "progress. The kernel therefore compares each attempt to the one "
        "immediately before it, and refuses to call a change an improvement "
        "unless it clears the instrument's own repeat floor.",
        "",
        "Two constraints come from the 2026-07-31 fixed-mic repeat study "
        "(`captures/repeat-floor-20260731/`) and are load-bearing here:",
        "",
        "1. **Consecutive attempts only.** Graded against a fixed early "
        "baseline the floor walks (+0.0046 dB/repeat, r = +0.81) because the "
        "box drifts; graded against the immediate predecessor it is flat. The "
        "kernel offers no fixed-baseline mode, and a test fails if one "
        "appears.",
        "2. **The claim floor is 2 x the consecutive-pair p95** — computed "
        "from the study's own pairs, never transcribed.",
        "",
    ])
    for run in runs:
        lines.extend(_run_prose(run))
        lines.append("")
    lines.extend([
        "## What this does and does not prove",
        "",
        "**Does:** the stop rule, run over real consecutive measurements of a "
        "speaker nobody touched, never claims an improvement. The decision at "
        "every step is reproducible from the JSON in this directory.",
        "",
        "**Does not:** validate the threshold. The repeat-floor run derives "
        "its floor from the same pairs it then grades, and on a bank this "
        "small that is close to unfalsifiable by construction: with 13 pairs "
        "the p95 lands at the 11.4th of 12 gaps, within a hair of the "
        "maximum, so twice it is always above every pair. This is a "
        "self-consistency demonstration. It shows the rule behaves; it cannot "
        "show the number is right, because the number came from that noise. "
        "An independent check needs a second unchanged-profile bank the floor "
        "was not derived from.",
        "",
        "**Also does not:** produce a realized improvement arc. Every session "
        "grade here is model-graded — a fit's own residual — because neither "
        "recorded bundle kept a VERIFY analysis. The realized arc, and the "
        "first real `realized - predicted` model-error record, wait on a live "
        "fixed-mic run.",
        "",
    ])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_command(argv: Sequence[str]) -> str:
    return "jasper-active-speaker-attempts-replay " + " ".join(
        shlex.quote(arg) for arg in argv
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repeat-floor",
        type=Path,
        default=None,
        help="repeat-floor bank directory (unchanged-profile control)",
    )
    parser.add_argument(
        "--sessions",
        type=Path,
        default=None,
        help="directory of recorded commissioning session bundles",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="directory for report.json, README.md, and the per-run stores",
    )
    parser.add_argument(
        "--target-attempts", type=int, default=AttemptBudget().target_attempts,
    )
    parser.add_argument(
        "--hard-cap-attempts", type=int, default=AttemptBudget().hard_cap_attempts,
    )
    args = parser.parse_args(argv)

    if args.repeat_floor is None and args.sessions is None:
        print(
            "REFUSED — give --repeat-floor, --sessions, or both",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # A refusing run must not leave a previous run's outputs beside this run's
    # fresh ones, where the next reader would pair them. That includes the
    # per-run stores: a `*.model_error.json` asserting an adopted claim floor,
    # sitting in a directory with no report to explain it, is a threshold
    # someone could pick up and believe.
    report_path = out_dir / "report.json"
    readme_path = out_dir / "README.md"
    report_path.unlink(missing_ok=True)
    readme_path.unlink(missing_ok=True)
    for stale_store in out_dir.glob("*.model_error.json"):
        stale_store.unlink(missing_ok=True)

    try:
        budget = AttemptBudget(
            target_attempts=int(args.target_attempts),
            hard_cap_attempts=int(args.hard_cap_attempts),
        )
    except ValueError as exc:
        print(f"REFUSED — budget: {exc}", file=sys.stderr)
        return 2

    runs: list[Run] = []
    try:
        if args.repeat_floor is not None:
            runs.append(build_repeat_floor_run(
                Path(args.repeat_floor), budget=budget,
            ))
        if args.sessions is not None:
            runs.extend(build_session_runs(Path(args.sessions), budget=budget))
    except (ReplayError, ValueError) as exc:
        print(f"REFUSED — {exc}", file=sys.stderr)
        return 2

    if not runs:
        print("REFUSED — nothing to replay", file=sys.stderr)
        return 2

    # Each run adopts its own floor into its own store. One store holds one
    # speaker's one floor; these runs are neither the same metric nor provably
    # the same speaker, so folding them into one file would invent a fact.
    for run in runs:
        run.store_path = out_dir / f"{run.run_id}.model_error.json"
        adopt_floor(run.floor, path=run.store_path)

    ungradeable = [run.run_id for run in runs if not run.gradeable]
    contradicted = [run.run_id for run in runs if run.contradicted]
    outcome = (
        "no_verdict" if ungradeable
        else "contradicted" if contradicted
        else "consistent"
    )
    report = {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "command": _build_command(argv),
        "budget": budget.to_dict(),
        "outcome": outcome,
        "ungradeable_runs": ungradeable,
        "contradicted_runs": contradicted,
        "runs": [run.to_dict() for run in runs],
        "model_error_stores": {
            run.run_id: load_state(run.store_path) for run in runs
        },
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    readme_path.write_text(
        render_readme(runs, command=_build_command(argv), out_dir=out_dir),
        encoding="utf-8",
    )

    for run in runs:
        stop_at = first_stop_index(run.decisions)
        where = (
            f"stops at attempt {stop_at + 1} ({run.attempts[stop_at].attempt_id}) "
            f"-> {run.decisions[stop_at].decision}"
            if stop_at is not None else "no stop inside the bank"
        )
        print(f"{run.run_id:<32} {where}")
        claims = len(run.improvement_claims)
        print(
            f"{'':<32} floor {run.floor.claim_floor_db:.5f} dB "
            f"({run.floor.basis}), {len(run.attempts)} attempts, "
            f"{claims} improvement claim{'' if claims == 1 else 's'}",
        )
    print(f"\nreport: {report_path}")
    print(f"readme: {readme_path}")

    if ungradeable:
        print(
            "no verdict — nothing in "
            f"{', '.join(ungradeable)} reached a comparison",
            file=sys.stderr,
        )
        return 2
    if contradicted:
        print(
            "FINDING — a consecutive change reached the claim floor on a "
            f"profile nobody touched: {', '.join(contradicted)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
