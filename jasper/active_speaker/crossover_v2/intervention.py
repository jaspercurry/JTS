# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The deterministic prescription planner, as pure functions (#2291 Phase 2).

**What this module is.** :func:`plan_linearization` is the prescription
planner, as side-effect-free assembly around the repository's existing pure
DSP primitives
(:func:`~jasper.active_speaker.linearization_fit.fit_driver_linearization`,
:func:`~jasper.audio_measurement.program_analysis.solve_ripple_optimal_trim`,
:func:`~jasper.audio_measurement.program_analysis.realized_branch_level_match`,
:func:`~jasper.active_speaker.linearization_envelope.compose_envelope`, …).
**No fitter, solver, or estimator is reimplemented here** — #2291 forbids that
explicitly, and the DSP those primitives do is the part of the old conductor
that was never broken. What is reimplemented is the *assembly*: which corner
each primitive is called at, which trim pair is committed, and how the result
leaves the planner.

**Reimplemented rather than moved, on purpose.** Moving the 814-line method
would have carried its hidden dependencies with it: seven ``self._last_*``
writes acting as an implicit return channel, and seven reads of the session's
``self._fc_hz`` inside a function whose caller had already chosen a *different*
candidate corner. Both are structural, and neither survives a rewrite that has
to name its inputs.

**The two defects this shape makes impossible.**

1. *One Fc owner.* Every corner-driven call below reads
   :attr:`~.contracts.CandidateAcousticContext.fc_hz` from the context passed
   in — the overlap band, the straddle decision, the ripple solve, the realized
   level match, and both journal fields that name a corner. There is no second
   corner in scope to read, so the 2026-08-10 jts3 shape (sections cornered at
   1,648.7 Hz, trim arithmetic at 2,000 Hz) cannot be expressed. The context
   also owns the *sections*, so the corner and the shapes it is realized with
   travel together.

2. *A rejected trim is not committed.* See :func:`decide_trim` — beyond the
   sanity margin the planner falls back to the anchor, and the strategy it
   returns names what it committed.

**Purity, stated as the contract callers may rely on.** Given equal inputs this
module returns equal outputs; it writes nothing, logs nothing, reads no clock,
and mutates none of its arguments. Journal lines are *returned as data*
(:class:`JournalRecord`) for the host to emit with its own session identity —
the planner has no session and no logger. That is
``docs/extensibility.md``'s host-mediated indirection applied to a migration:
the domain function cannot reach back into the host that called it.

The one concession is an optional ``journal`` **port**: a host may pass a
callable that receives each record as it is produced, so a fit that raises
part-way still discloses the lines it had already reached. Without it those
lines would exist only on the returned plan, and a plan that never returns
carries nothing — the ``fit_band`` line naming the corner a raising fit ran at
is exactly the one an operator wants.

The port is genuinely write-only, and both halves of that are enforced rather
than asserted (#2313 panel should-fixes 2 and 3). A consumer that **raises**
cannot abort the plan: the call is guarded, and the refused records are
reported on :attr:`LinearizationPlan.journal_dropped` so the loss is visible
instead of swallowed. A consumer that **mutates** what it is handed cannot
reach the plan either: :class:`JournalRecord` detaches its fields at
construction. Determinism therefore holds whatever the host's logger does.

**A port must raise stdlib exception types, or wrap its own.** The guard
enumerates them in :data:`_PORT_ERRORS` rather than catching a bare
``Exception``, because the repository's lint contract freezes its broad-except
budget and an enumeration is the shape that leaves. A consumer raising a
*custom* exception class therefore escapes the guard and does abort the plan —
by design, not by oversight: a consumer with its own exception hierarchy knows
what it is doing and can raise a stdlib type or wrap.

**Dependency direction.** This module imports the DSP primitives and
:mod:`.contracts`. It must never import
:mod:`jasper.active_speaker.crossover_v2_flow` (the flow imports *this*) or
anything under :mod:`jasper.web`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

import numpy as np

from ..branch_chain import (
    HEADROOM_MARGIN_DB,
    CrossoverSection,
    branch_chain_peak_db,
    headroom_charge_db,
    radiating_band_hz,
)
from ..branch_target import branch_target
from ..linearization_envelope import (
    DEFAULT_ENVELOPE_GRID_HZ,
    compose_envelope,
    compute_sigma_curve,
)
from ..linearization_fit import (
    FitVocabulary,
    complex_correction_response,
    core_level_band_hz,
    driver_core_level_db,
    fit_driver_linearization,
    solve_shared_level_frame,
)
from jasper.audio_measurement.program_analysis import (
    ALIGNMENT_OK,
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    RealizedLevelMatch,
    overlap_band_hz,
    predicted_branch_sum,
    realized_branch_level_match,
    solve_ripple_optimal_trim,
    summed_model_residual_delay_us,
)

from .contracts import (
    CandidateAcousticContext,
    CrossoverV2ContractError,
    TrimStrategy,
    detached_json,
)

__all__ = [
    "CloudFitTerms",
    "DriverEvidence",
    "JournalRecord",
    "LINEARIZATION_MIN_PAIRED_OCCURRENCES",
    "LINEARIZATION_TRIM_SANITY_MARGIN_DB",
    "LinearizationPlan",
    "LinearizationRequest",
    "MIN_TRIM_SANITY_MARGIN_RATIO",
    "PlannerError",
    "PlannerInputError",
    "SIGMA_TOLERABLE_DB",
    "TrimDecision",
    "compose_sigma_db",
    "decide_trim",
    "driver_response_by_role",
    "measure_validity_floor_hz",
    "plan_linearization",
    "realized_level_match",
    "request_from_analysis",
    "rounded_band_hz",
]


class PlannerError(CrossoverV2ContractError):
    """The planner cannot plan from these inputs — a refusal, not a crash.

    A subclass of :class:`~.contracts.CrossoverV2ContractError` so a host that
    already classifies contract failures needs no second ``except`` arm, and so
    it inherits that base's :attr:`refusal_reason` — the facade maps a raise to
    a typed :class:`~.contracts.PlanRefusal` by reading that attribute, never
    the message text (#2307 gate note N5).
    """



class PlannerInputError(PlannerError):
    """A required planner input is missing or malformed."""

    refusal_reason = "contract_invalid"


# What a misbehaving disclosure port may raise without taking the plan down.
# Enumerated rather than a blind ``except Exception`` (ruff BLE, and the
# repository's frozen broad-except budget). ``OSError`` is in the set because
# the likeliest real consumer is a logging handler, and a handler writing to a
# closed stream or a full disk raises exactly that. The set was originally
# derived by mirroring the Phase-1 planner facade's own assembly-error tuple,
# which #2291 Phase 5c-iii deleted along with the facade; it stands on its own
# reasoning now, not on a second copy elsewhere.
_PORT_ERRORS = (
    ArithmeticError,
    AttributeError,
    IndexError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


# --------------------------------------------------------------------------- #
# policy constants
# --------------------------------------------------------------------------- #
#
# These live here rather than in the flow because the planner is now their only
# reader, and a constant whose reader cannot import it is how a second copy
# starts. ``crossover_v2_flow`` re-exports both under their historical names,
# so every existing import keeps resolving to this one definition.

# Minimum paired in-capture occurrences (primary + repeats) per driver before
# the linearization path is trusted at all. Coincides with the MEASURE
# program's own default repeat count
# (jasper.audio_measurement.program.MEASURE_REPEAT_COUNT) — not imported,
# since this is a POLICY floor (what linearization requires), not a
# statement about what the program composes; the two happen to agree today.
LINEARIZATION_MIN_PAIRED_OCCURRENCES = 3

# How far the ripple-optimal tweeter trim may move from its ANCHORED trim
# (raw trim + that branch's measured `correction_giveback_db`, normalized)
# before the scan's result is treated as implausible. ANCHOR-anchored since
# the 2026-07-24 JTS3 runs (#1668): the anchor is measured give-back, not a
# solver prediction, so it is trusted by construction and only the SCAN can
# drift. What the guard catches is the scan wandering into the "attenuate the
# tweeter toward silence is always flatter against a flat woofer" degenerate
# its own +/-window allows.
#
# Magnitude protection against a garbage correction lives upstream in the fit
# engine's structural caps (per-filter <=12 dB cut, total normalization budget,
# realization tolerance) plus the downstream VERIFY gate — not this trim guard.
#
# NOTE the margin's MEANING changed with the anchor: it is "how far the
# summed-flatness optimum may disagree with the measured level anchor," not
# "how far a re-solve may drift from another level estimate." The 6.0 value is
# retained deliberately and is judged from live guard telemetry, not
# re-derived. What #2291 Phase 2 changed is not the number but its
# CONSEQUENCE — see :func:`decide_trim`.
#
# **It is COUPLED to REALIZED_LEVEL_MATCH_TOLERANCE_DB and the coupling is
# load-bearing**, which is why :data:`MIN_TRIM_SANITY_MARGIN_RATIO` exists
# below rather than the relation living only in prose. Since #2291 Phase 2 a
# beyond-margin scan falls back to the anchor, and the claim that a badly
# levelled anchor then produces a refusal rather than a hot speaker holds only
# while the margin is at least twice the realized-level tolerance. 6.0 against
# 2 × 3.0 is exactly on that edge, in two different modules, and this PR made
# the margin a per-request field — so the relation is now checked at
# construction (hearing-safety lens, #2313 panel should-fix 1).
LINEARIZATION_TRIM_SANITY_MARGIN_DB = 6.0

#: The margin must be at least this multiple of
#: :data:`~jasper.audio_measurement.program_analysis.REALIZED_LEVEL_MATCH_TOLERANCE_DB`.
#:
#: **Why two, derived rather than chosen.** The fallback commits the anchor
#: when the scan drifts past the margin ``M``. The accountability seam then
#: refuses unless the committed pair's realized level error is within the
#: tolerance ``T``. For the refusal to be able to catch every fallback that
#: leaves the speaker hotter than the scan would have, the drift the fallback
#: can introduce (up to ``M``) has to exceed what the gate tolerates on each
#: side of the crossover (``T`` on the anchor's side and ``T`` on the scan's) —
#: i.e. ``M >= 2T``. Below that there is a band of drifts where the anchor is
#: committed, is louder than the scan by more than the gate can see, and passes:
#: at ``M = 4.0`` against ``T = 3.0`` the panel measured a tweeter landing
#: +4.05 dB hotter than legacy would have shipped, *through* a matched gate.
MIN_TRIM_SANITY_MARGIN_RATIO = 2.0

# Mirrors jasper.active_speaker.linearization_envelope._SIGMA_TOLERABLE_DB
# (module-private there — see that module's top docstring for the "no
# cross-module private imports" convention this repo follows). LOCKSTEP
# REQUIREMENT: any change to that table must be mirrored here, or this
# planner's sigma floor and the envelope module's own repeatability_limit()
# disagree about what "tolerable" means per tier.
SIGMA_TOLERABLE_DB: Mapping[str, float] = {
    "reference": 0.5,
    "consumer": 1.0,
    "phone": 1.5,
}


# --------------------------------------------------------------------------- #
# small pure helpers, relocated from the flow so the planner can reach them
# --------------------------------------------------------------------------- #


def driver_response_by_role(analysis: Any, role: str) -> Any | None:
    for resp in analysis.driver_responses:
        if resp.role == role:
            return resp
    return None


def rounded_band_hz(
    band_hz: tuple[float, float] | None,
) -> tuple[float, float | None] | None:
    """A ``(lo, hi)`` band rounded for the journal, with a non-finite upper
    edge rendered as ``None`` rather than ``inf``.

    A high-pass branch radiates to infinity, and ``inf`` is not JSON-safe;
    ``None`` reads as "no upper bound" and survives every consumer. Shared by
    every place that logs a band so they cannot render the same number two
    ways.
    """
    if band_hz is None:
        return None
    lo_hz, hi_hz = band_hz
    return (
        round(float(lo_hz), 1),
        round(float(hi_hz), 1) if math.isfinite(hi_hz) else None,
    )


def measure_validity_floor_hz(analysis: Any) -> float | None:
    """The worse (higher) of the two driver responses' own reflection-gate floor."""

    floors = [
        r.validity_floor_hz
        for r in analysis.driver_responses
        if r.validity_floor_hz is not None
    ]
    return max(floors) if floors else None


def compose_sigma_db(
    own: Any,
    sibling: Any,
    *,
    tier: str,
    valid_band_hz: tuple[float, float],
    grid_hz: np.ndarray = DEFAULT_ENVELOPE_GRID_HZ,
) -> np.ndarray | None:
    """PR-C's σ-composition policy: the paired-N gate + the per-tier floor.

    ``own``/``sibling`` are the two
    :class:`~jasper.audio_measurement.program_analysis.DriverResponse` of a
    crossover pair. Returns ``None`` (no evidence, no permission — the same
    contract :func:`~jasper.active_speaker.linearization_envelope.compute_sigma_curve`
    itself uses) when EITHER driver has fewer than
    :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES` occurrences (primary +
    repeats) — an under-repeated sibling voids the pair's trust even if ``own``
    alone has plenty. This gate is deliberately redundant with the host's own
    outer eligibility gate; belt-and-suspenders, so this function stays
    independently correct when called from a different context.

    Otherwise computes ``own``'s live σ(f) and floors it at the tier's own
    tolerable value: ``sigma_eff = max(sigma_tolerable(tier), live)``.

    **This floor is BEHAVIORALLY INERT today.** ``repeatability_limit``'s own
    formula is ``D_cap * min(1, sigma_tolerable / max(sigma, eps))`` — for ANY
    ``live <= sigma_tolerable`` that expression already saturates at
    ``D_cap * 1``, identically whether ``live`` is floored up to
    ``sigma_tolerable`` or left alone. Flooring at EXACTLY the tier's own
    tolerable value therefore changes nothing about the resulting envelope; it
    exists as a SEAM for a future policy that sets the floor HIGHER than
    ``sigma_tolerable``. Do not assume this floor currently does more than the
    paired-N gate above. (N2, 2026-07-24 adversarial review: flagged the same
    inertness; the ruling was to KEEP it, honestly documented and pinned by
    ``test_compose_sigma_db_floor_is_behaviorally_inert_on_repeatability_limit``.)
    """
    own_n = 1 + len(own.repeat_responses)
    sibling_n = 1 + len(sibling.repeat_responses)
    if (
        own_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
        or sibling_n < LINEARIZATION_MIN_PAIRED_OCCURRENCES
    ):
        return None
    live = compute_sigma_curve(own, valid_band_hz=valid_band_hz, grid_hz=grid_hz)
    if live is None:
        return None
    try:
        floor_db = SIGMA_TOLERABLE_DB[tier]
    except KeyError as exc:
        # A tier outside the closed set is a caller error, not a measurement
        # outcome — but a bare ``KeyError`` reaching the host is indistinguishable
        # from malformed planner output and would be classified as a generic
        # ``contract_invalid``. Name it instead.
        raise PlannerInputError(
            f"unknown mic tier {tier!r}; expected one of "
            f"{sorted(SIGMA_TOLERABLE_DB)}"
        ) from exc
    return np.maximum(floor_db, live)


def realized_level_match(
    freqs: np.ndarray,
    w_tf: np.ndarray,
    t_tf: np.ndarray,
    fc_hz: float,
    trims_db: Mapping[str, float],
    woofer_role: str,
    tweeter_role: str,
    *,
    woofer_span_hz: tuple[float, float],
    tweeter_span_hz: tuple[float, float],
) -> RealizedLevelMatch:
    """One candidate trim pair's realized inter-driver level (PR-L4 item 1).

    A thin role-ordering adapter over
    :func:`~jasper.audio_measurement.program_analysis.realized_branch_level_match`
    — this layer speaks roles, that estimator speaks woofer/tweeter branches,
    and nothing else belongs in between. A free function rather than a method
    so :func:`decide_trim` can grade BOTH candidate pairs with one obviously-
    identical call, and so ``fc_hz`` has to be *passed* rather than read off a
    session (the 2026-08-10 defect's fifth site).
    """
    return realized_branch_level_match(
        freqs,
        w_tf,
        t_tf,
        float(fc_hz),
        trim_w_db=float(trims_db[woofer_role]),
        trim_t_db=float(trims_db[tweeter_role]),
        woofer_span_hz=woofer_span_hz,
        tweeter_span_hz=tweeter_span_hz,
    )


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, init=False)
class JournalRecord:
    """One log line the planner would have emitted, as data.

    The planner owns *what happened*; the host owns *how it is said* — it adds
    its own ``session_id`` and writes through its own logger. Returned in
    emission order, so a host that iterates and logs produces the same journal
    a logging planner would have.

    **A consumer cannot reach the plan through a record** (#2313 panel
    should-fix 3). Two things are needed for that, and one alone is not enough:

    * the payload is **detached at construction** by the same
      :func:`~.contracts.detached_json` the proposal contracts use, so the
      containers the planner built it from are not shared; and
    * :attr:`fields` is a **property returning a fresh detached copy on every
      read**, so a consumer that pops a key, clears the mapping, or edits a
      nested value — all ordinary things for a log formatter to do — edits a
      throwaway.

    The detach alone leaves the record's own dict mutable, which is exactly the
    hole the panel found. A ``MappingProxyType`` would close the top level, and
    the reason it is not used is narrower than it first appears — measured
    rather than assumed:

    * ``render_logfmt`` is **unaffected**. It formats values through ``str``,
      and a proxy's ``str`` delegates to the underlying dict, so the logfmt
      line is byte-identical either way.
    * ``render_json`` (``JASPER_LOG_JSON=1``) is where it breaks. That sink is
      ``json.dumps(..., default=str)``, and a proxy is not JSON-serializable —
      so a nested mapping that renders as a real object ``{"woofer": [...]}``
      degrades to a quoted Python repr ``"{'woofer': [...]}"``. Machine
      consumers of the journal would silently start receiving strings where
      they had objects.

    Copy-on-read has neither problem. Payloads are a handful of scalars and
    small mappings; the copy is not a cost worth trading correctness for.
    """

    event: str
    level: int
    _fields: Mapping[str, Any] = field(repr=False)

    def __init__(
        self, event: str, fields: Mapping[str, Any], level: int = logging.INFO
    ) -> None:
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "_fields", detached_json(dict(fields)))

    @property
    def fields(self) -> Mapping[str, Any]:
        """This record's payload — a fresh copy each call; see the class doc."""

        return detached_json(self._fields)


@dataclass(frozen=True)
class CloudFitTerms:
    """What a closed spatial cloud contributes to the correction envelope.

    The three optional arguments of
    :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`,
    travelling together as one value so the fit cannot be handed a
    half-supplied pair, plus the boost-only bound (#1967) the fit *vocabulary*
    takes.

    ``boost_excluded_bands_hz`` does NOT go to the envelope. Empty is the
    ordinary case and means "nothing contradicted a boost", never "no
    evidence".

    Structurally identical to the flow's ``_CloudFitEvidence``, and
    :meth:`from_evidence` adapts one without this module importing the flow.
    """

    excluded_bands_hz: tuple[tuple[float, float], ...] = ()
    band_spread: tuple[Any, ...] = ()
    n_positions: int = 0
    boost_excluded_bands_hz: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_evidence(cls, evidence: Any) -> "CloudFitTerms | None":
        """Adapt the host's cloud-evidence value; ``None`` passes through."""

        if evidence is None:
            return None
        if isinstance(evidence, CloudFitTerms):
            return evidence
        # Every field is read directly, with no ``getattr`` default. A default
        # on ``boost_excluded_bands_hz`` would fail OPEN: an evidence object
        # missing it would silently become "nothing contradicted a boost" and
        # grant the lift stage the full band, which is the one direction this
        # bound must never fail in. Legacy's ``_CloudFitEvidence`` always
        # carries the field, so an object without it is a programming error and
        # should raise like one.
        return cls(
            excluded_bands_hz=tuple(
                (float(lo), float(hi)) for lo, hi in evidence.excluded_bands_hz
            ),
            band_spread=tuple(evidence.band_spread),
            n_positions=int(evidence.n_positions),
            boost_excluded_bands_hz=tuple(
                (float(lo), float(hi)) for lo, hi in evidence.boost_excluded_bands_hz
            ),
        )


@dataclass(frozen=True)
class DriverEvidence:
    """One driver's measured response and the two bands that bound its fit.

    ``excited_band_hz`` is the declared sweep span for this role — the band its
    stimulus actually radiated in, which bounds σ-composition and the
    envelope's validity. ``driver_class`` is the envelope's per-class depth
    table key.
    """

    role: str
    response: Any
    excited_band_hz: tuple[float, float]
    driver_class: str = "unknown"


@dataclass(frozen=True)
class LinearizationRequest:
    """Everything :func:`plan_linearization` reads. Nothing else is in scope.

    Naming the inputs *is* the fix: the old method read five conductor fields
    that its caller had no way to see, and one of them (the session corner) was
    wrong for every non-configured candidate. A request holds one corner —
    ``context.fc_hz`` — and nothing that could disagree with it.
    """

    context: CandidateAcousticContext
    """The one Fc owner: this candidate's corner and the sections realizing it."""

    woofer: DriverEvidence
    tweeter: DriverEvidence
    raw_trim_db: Mapping[str, float]
    """``candidate.trim_db`` — the raw solve's applied per-role attenuation."""

    trim_band_average_db: Mapping[str, float]
    """``candidate.trim_band_average_db`` — the level-match term the frame uses."""

    predicted_ripple_db: float
    polarity_sign: int
    delay_us: float
    anchor_delay_us: float | None
    """``None`` when the alignment is not :data:`ALIGNMENT_OK` — load-bearing."""

    mic_tier: str
    branch_floor_hz: float | None
    """The shared reflection-gate floor; ``None`` when no response reported one."""

    post_apply_verifies: bool
    """Does the JOURNEY measure what the speaker did with a boost? Boost's ONE
    necessary condition."""

    cloud_phase_planned: bool
    """Did this session PLAN a spatial cloud? Distinguishes "absent by design"
    (R15's driver-only path, boost permitted) from "planned and lost" (boost
    withheld) — the two share a ``cloud is None`` signature and are different
    evidence states."""

    cloud: CloudFitTerms | None = None
    trim_sanity_margin_db: float = LINEARIZATION_TRIM_SANITY_MARGIN_DB

    def __post_init__(self) -> None:
        if not isinstance(self.context, CandidateAcousticContext):
            raise PlannerInputError("context must be a CandidateAcousticContext")
        for name, driver in (("woofer", self.woofer), ("tweeter", self.tweeter)):
            if not isinstance(driver, DriverEvidence):
                raise PlannerInputError(f"{name} must be a DriverEvidence")
            if driver.response is None:
                raise PlannerInputError(f"{name} evidence carries no response")
        if self.woofer.role == self.tweeter.role:
            raise PlannerInputError(
                f"woofer and tweeter share the role {self.woofer.role!r}"
            )
        if int(self.polarity_sign) not in (-1, 1):
            raise PlannerInputError("polarity_sign must be -1 or +1")
        # The hearing-safety coupling, checked rather than trusted. NaN is
        # tested first and separately because it is the silent case: every
        # comparison against NaN is False, so a NaN margin makes
        # ``drift > margin`` false for every drift — the guard does not
        # misfire, it stops existing, and every scan commits as if the sanity
        # check were not there.
        margin = float(self.trim_sanity_margin_db)
        if not math.isfinite(margin):
            raise PlannerInputError(
                "trim_sanity_margin_db must be finite; a non-finite margin "
                "silently disables the sanity guard rather than widening it"
            )
        floor = MIN_TRIM_SANITY_MARGIN_RATIO * REALIZED_LEVEL_MATCH_TOLERANCE_DB
        if margin < floor:
            raise PlannerInputError(
                f"trim_sanity_margin_db {margin} dB is below "
                f"{MIN_TRIM_SANITY_MARGIN_RATIO}x the realized-level tolerance "
                f"({REALIZED_LEVEL_MATCH_TOLERANCE_DB} dB): the anchor fallback "
                "could then commit a pair louder than the scan by more than the "
                "accountability gate can detect"
            )
        # Snapshot the caller's trim mappings. A frozen dataclass holding a
        # live dict is frozen in name only, and these two are read at four
        # points spread across the plan — a caller mutating one mid-plan would
        # produce a result that matches no single input.
        object.__setattr__(self, "raw_trim_db", dict(self.raw_trim_db))
        object.__setattr__(
            self, "trim_band_average_db", dict(self.trim_band_average_db)
        )

    @property
    def roles(self) -> tuple[str, str]:
        return (self.woofer.role, self.tweeter.role)

    def sections_for(self, role: str) -> tuple[CrossoverSection, ...]:
        """This candidate's sections for ``role`` — empty when it has none.

        Empty is *representable* and is handled without inventing a section:
        the emitter builds its crossover filters from the same regions, so such
        a role runs full range in the emitted graph, and guessing a section
        here would make the planner's stamped disclosure smaller than the
        emitter's charge.

        Representable is not the same as correct. **On a 2-way session a role
        with no crossover region is a defect upstream**, which is why
        :func:`plan_linearization` names it in the journal rather than passing
        over it silently — the same disposition, and the same event, the
        retired ``CrossoverV2Session._branch_crossover_sections`` gave the
        condition. That method was legacy's seventh reader of the session
        corner — the planner has none, which is this module's central
        invariant — and #2291 Phase 2b deleted it with the rest of the
        fitter; emitting the disclosure here keeps it at the detection site,
        and the host inherits it with the rest of the journal.

        The *context* separately guarantees that every section which does exist
        names this candidate's corner.
        """
        return self.context.sections_by_role.get(role, ())


# --------------------------------------------------------------------------- #
# the trim decision — the half whose name used to lie
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TrimDecision:
    """Which trim pair was committed, and the complete evidence for why.

    Every field the old code left in a log line or a ``_last_*`` field is here
    as a value, because "which pair won" was exactly the fact the 2026-08-10
    artifact could not answer.
    """

    committed_db: Mapping[str, float]
    strategy: TrimStrategy
    rationale: str
    anchored_db: Mapping[str, float]
    resolved_db: Mapping[str, float]
    anchor_drift_db: float
    sanity_margin_db: float
    beyond_sanity_margin: bool
    anchored_match: RealizedLevelMatch
    resolved_match: RealizedLevelMatch
    committed_match: RealizedLevelMatch
    ripple_db: float | None
    """The scan's own ripple at its optimum; ``None`` when the scan was skipped."""

    @property
    def committed_side(self) -> str:
        """``"anchored"`` or ``"resolved"`` — derived from the strategy.

        The journal's own ``committed`` field reads this rather than a literal,
        so there is one owner of "which pair won" and no second copy to drift
        from it.
        """
        return (
            "resolved"
            if self.strategy
            in (
                TrimStrategy.RESOLVED_COMMITTED,
                TrimStrategy.RESOLVED_COMMITTED_AFTER_SANITY_DRIFT,
            )
            else "anchored"
        )

    @property
    def outcome(self) -> str:
        """The persisted ``linearization_outcome`` string.

        ``"trim_rejected"`` is retained deliberately and is now *true*: since
        #2291 Phase 2 a beyond-margin scan IS rejected — the anchor is what
        ships. The string stopped lying because the behaviour changed to match
        it, not because the string was renamed.
        """
        return "trim_rejected" if self.beyond_sanity_margin else "fitted"


def decide_trim(
    *,
    anchored_db: Mapping[str, float],
    resolved_db: Mapping[str, float],
    tweeter_role: str,
    anchored_match: RealizedLevelMatch,
    resolved_match: RealizedLevelMatch,
    ripple_db: float | None,
    sanity_margin_db: float = LINEARIZATION_TRIM_SANITY_MARGIN_DB,
) -> TrimDecision:
    """Commit one trim pair, and say which one and why.

    **Two regimes, and the boundary between them is the sanity margin.**

    *Within the margin* the scan is trusted to have polished, so both pairs are
    graded by the one direct measurement of what a trim is FOR — their realized
    inter-driver level — and the better-levelled pair is committed. Ties go to
    the anchor, which is level-preserving by construction. This is PR-L4 item
    9's rule and it is unchanged: grading BOTH pairs (rather than only inside a
    rejection) is what removed the non-monotonicity its review B2 measured,
    where a 2.0-5.9 dB drift committed ungraded and then hard-stopped the
    session at item 1's 3.0 dB tolerance while a LARGER drift fell back and
    applied.

    *Beyond the margin* the scan is not trusted at all, and the anchor is
    committed — :attr:`~.contracts.TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT`.

    **This second regime is a deliberate behaviour change (#2291, sanctioned).**
    The old code graded unconditionally, so a scan that had drifted past the
    margin was still committed whenever it levelled better — while the
    candidate it rode on recorded ``linearization_outcome="trim_rejected"``.
    On 2026-08-10 that shipped a −13.013 dB tweeter trim under the word
    "rejected"; the level-preserving anchor was −6.713 dB, and the extra
    6.300 dB of cut is the largest single term in the dark upper half the
    household then measured. A guard whose rejection is telemetry is not a
    guard, and #2291's acceptance criterion is explicit: *a ripple trim beyond
    the sanity policy cannot be recorded as rejected and still silently
    committed.*

    **What the change can and cannot do to loudness.** Falling back raises the
    committed level, because the anchor is the *less* attenuated pair whenever
    the scan drifted downward (the degenerate direction the margin exists to
    catch). It is bounded on both sides: the anchor is level-*preserving* by
    construction — each branch's own measured give-back, normalized so no trim
    is ever positive — so the committed pair can never exceed the branch's own
    pre-correction system level, and the emitted trims stay cut-only. And the
    committed pair is not final: it still faces the realized-level assertion at
    the host's accountability seam, which refuses the session rather than
    shipping a pair that does not level. Falling back to a badly-levelled
    anchor therefore produces a loud refusal, not a loud speaker.

    **That last sentence is CONDITIONAL, and the condition is enforced rather
    than assumed.** It holds only while

        ``sanity_margin_db >= MIN_TRIM_SANITY_MARGIN_RATIO *
        REALIZED_LEVEL_MATCH_TOLERANCE_DB``

    — today 6.0 against 2 × 3.0, exactly on the edge. Below it there is a band
    of drifts where the anchor is committed, is louder than the scan by more
    than the accountability gate can see, and passes: at a 4.0 dB margin the
    #2313 hearing-safety lens measured a tweeter landing +4.05 dB hotter than
    legacy would have shipped, *through* a matched gate. Because this function
    takes the margin as an argument, that relation is validated in
    :meth:`LinearizationRequest.__post_init__`, where the margin enters — the
    constant's own comment carries the derivation.

    **What the change gives up, named.** A beyond-margin scan that genuinely
    levelled better than the anchor is no longer committed. PR-L4's review
    measured one such case (a 6.5 dB drift committing at −1.6 dB of level
    error). That case now falls back and — if the anchor misses item 1's
    tolerance — refuses. That is the intended trade: an honest refusal beats an
    unaccountable application, and the margin is judged from live guard
    telemetry rather than widened here to avoid the refusal.
    """
    drift_db = abs(
        float(resolved_db[tweeter_role]) - float(anchored_db[tweeter_role])
    )
    beyond = drift_db > float(sanity_margin_db)
    anchor_levels_better = abs(anchored_match.difference_db) <= abs(
        resolved_match.difference_db
    )
    if beyond:
        committed_db, committed_match = anchored_db, anchored_match
        strategy = TrimStrategy.ANCHORED_COMMITTED_AFTER_SANITY_DRIFT
        rationale = (
            f"the ripple scan drifted {drift_db:.3f} dB from the anchor, past "
            f"the {float(sanity_margin_db):.1f} dB sanity margin, so it was "
            "rejected and the level-preserving anchored pair was committed."
        )
    elif anchor_levels_better:
        committed_db, committed_match = anchored_db, anchored_match
        strategy = TrimStrategy.ANCHORED_COMMITTED
        rationale = (
            "the anchored pair realized the better inter-driver level "
            f"({anchored_match.difference_db:+.3f} dB against the scan's "
            f"{resolved_match.difference_db:+.3f} dB); the scan stayed within "
            "the sanity margin."
        )
    else:
        committed_db, committed_match = resolved_db, resolved_match
        strategy = TrimStrategy.RESOLVED_COMMITTED
        rationale = (
            "the ripple scan's pair realized the better inter-driver level "
            f"({resolved_match.difference_db:+.3f} dB against the anchor's "
            f"{anchored_match.difference_db:+.3f} dB) and stayed within the "
            "sanity margin."
        )
    return TrimDecision(
        committed_db=dict(committed_db),
        strategy=strategy,
        rationale=rationale,
        anchored_db=dict(anchored_db),
        resolved_db=dict(resolved_db),
        anchor_drift_db=drift_db,
        sanity_margin_db=float(sanity_margin_db),
        beyond_sanity_margin=beyond,
        anchored_match=anchored_match,
        resolved_match=resolved_match,
        committed_match=committed_match,
        ripple_db=None if ripple_db is None else float(ripple_db),
    )


# --------------------------------------------------------------------------- #
# the plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LinearizationPlan:
    """One candidate's complete prescription, as a value.

    Everything the old method left on ``self`` — the level frame and its three
    diagnostic companions, the outcome string, the realized match, the
    linearized VERIFY prediction — is a field here. That is the whole point of
    the extraction: the sweep used to have to *save and restore seven conductor
    fields* around each candidate because the planner's outputs had nowhere
    else to live.
    """

    fc_hz: float
    role_attenuations_db: Mapping[str, float]
    linearization: Mapping[str, Any]
    trim: TrimDecision
    level_frame: Any | None
    level_frame_disagreement_db: float
    level_frame_cores: Mapping[str, Mapping[str, Any]]
    level_frame_trims: Mapping[str, float]
    linearized_predicted_sum: tuple[np.ndarray, np.ndarray]
    headroom_charge_db: Mapping[str, float]
    chain_peak_db: Mapping[str, float]
    radiating_band_hz: Mapping[str, tuple[float, float]]
    journal: tuple[JournalRecord, ...] = field(default=())
    journal_dropped: tuple[str, ...] = field(default=())
    """Records the host's disclosure port refused, as ``event: Error: detail``.

    Empty is the ordinary case. Non-empty means the plan is sound but the
    host's own logging lost lines — a fact that must not be swallowed just
    because it could not be logged. It is a plan FIELD rather than a final
    journal record on purpose: a record announcing that the port is failing
    would be emitted through the failing port.
    """

    @property
    def outcome(self) -> str:
        return self.trim.outcome

    @property
    def realized_level_match(self) -> RealizedLevelMatch:
        return self.trim.committed_match


def plan_linearization(
    request: LinearizationRequest,
    *,
    journal: Callable[[JournalRecord], None] | None = None,
) -> LinearizationPlan:
    """Fit both drivers, correct in the linear domain, and commit one trim pair.

    The ordering is the design doc's and is load-bearing: fit each branch, apply
    the correction as a COMPLEX (minimum-phase) response, and re-solve the trim
    from the LINEARIZED branch pair — which is what structurally defuses #1667's
    band-average trim bias.

    Assumes eligibility (reference-tier mic, both drivers paired
    N ≥ :data:`LINEARIZATION_MIN_PAIRED_OCCURRENCES`) and does not re-check —
    the host's own gate owns that question, and a request that fails it will
    raise inside the fit engine rather than quietly returning a worse plan.

    Its branch inputs must already carry the crossover shoulders; on the
    protected-neutral path that is the §4.2 composition's job, and the host
    refuses an uncomposed capture before reaching here.

    ``journal`` is the optional write-only disclosure port described in the
    module docstring: every record is handed to it as it is produced, and every
    record is also on the returned plan. A host that passes one sees the lines
    a raising fit had reached; a host that does not loses nothing on the
    success path. A port that raises is contained — the record still reaches
    the plan and the refusal is listed in
    :attr:`LinearizationPlan.journal_dropped`.
    """
    woofer_role, tweeter_role = request.roles
    context = request.context
    fc_hz = context.fc_hz
    responses = {
        woofer_role: request.woofer.response,
        tweeter_role: request.tweeter.response,
    }
    siblings = {
        woofer_role: request.tweeter.response,
        tweeter_role: request.woofer.response,
    }
    excited_band_hz = {
        woofer_role: request.woofer.excited_band_hz,
        tweeter_role: request.tweeter.excited_band_hz,
    }
    driver_class = {
        woofer_role: request.woofer.driver_class,
        tweeter_role: request.tweeter.driver_class,
    }
    records: list[JournalRecord] = []
    dropped: list[str] = []

    def emit(event: str, fields: Mapping[str, Any], level: int = logging.INFO) -> None:
        record = JournalRecord(event, fields, level)
        records.append(record)
        if journal is None:
            return
        try:
            journal(record)
        except _PORT_ERRORS as exc:
            # A disclosure port that can abort the plan is worse than no port.
            # Legacy called ``log_event`` inline and inherited logging's own
            # never-raise posture; a host formatter that throws on one field
            # would, without this, cost a household an entire candidate — and
            # in a six-candidate sweep, silently narrow the comparison. The
            # record is still on the returned plan, and the failure is
            # disclosed rather than swallowed: see ``journal_dropped``.
            dropped.append(f"{record.event}: {type(exc).__name__}: {exc}")

    # --- each branch's own crossover, and the band it radiates in ----------
    #
    # (#1809.) The fit's LIFT stage is bounded to it: a driver measured THROUGH
    # its crossover carries that crossover's rolloff in its curve, and a fit
    # flattening it against a flat target reads the rolloff as a driver deficit.
    # With PR-L5's boost vocabulary that read became emittable — the 2026-07-28
    # JTS3 woofer carried +11.6155 dB (Q 8) at 2747 Hz inside its own 2 kHz LR4
    # stopband, where the lowpass then removed 13.3 dB of it. The band also
    # bounds the knee, where a branch is 6 dB down BY DESIGN and a fit reaching
    # it boosts that back.
    #
    # LIFT is bounded at the band ITSELF. The SOLVE — cuts included — is bounded
    # at the band WIDENED by half an octave (#2523,
    # ``linearization_fit._solve_band_mask``), which is a looser bound and not
    # this one. #1809's asymmetry stands where it was measured: leakage past the
    # handoff still reaches the summed response and removing it spends no
    # headroom, so a shoulder cut is kept. What #1809 never had to answer is how
    # far past, and against R10a's ideal-crossover target the answer used to be
    # "to the fit band's own edge", which put the branch's deep stopband — where
    # a real driver's breakup and the capture's noise floor sit tens of dB above
    # any ideal rolloff — into the objective as demand no cascade can realize.
    #
    # The band ALSO bounds one LEVEL question, and only one (#1929): the
    # core-level median the frame is built from, below. The 2026-07-30 JTS3
    # session (#1870) is what closed it: a woofer declared to 4000 Hz against
    # that session's 2000 Hz LR4 put ~28% of its core bins an octave inside its
    # own stopband, read 3.395 dB away from the trim solve's estimate of the
    # same physical level, and refused a healthy speaker at the accountability
    # gate. It stops at the median — the give-back is a power-domain average
    # that quiet stopband bins barely reach, and the fit target is #1817's
    # crossover-shaped-target question, not a band.
    #
    # These sections are the CANDIDATE's, from the request's context, and every
    # one of them names ``fc_hz``: that is checked at construction, not here.
    # Each Fc candidate must be fitted against ITS OWN crossover (R17), or every
    # one of them is corrected toward some other corner's shape and the
    # comparison measures the fit's mismatch instead of the crossover's.
    sections = {
        role: request.sections_for(role) for role in (woofer_role, tweeter_role)
    }
    # The named defect, disclosed at the site that detects it. The retired
    # ``_branch_crossover_sections`` raised this same event — it was the
    # SEVENTH read of the session corner, and the one an audit of the fitter
    # alone could not see.
    # See :meth:`LinearizationRequest.sections_for` for why the condition is
    # representable and still a defect.
    for role in (woofer_role, tweeter_role):
        if not sections[role]:
            emit(
                "correction.crossover_v2_linearization_no_crossover",
                {"role": role, "fc_hz": round(float(fc_hz), 3)},
                logging.WARNING,
            )
    radiating_bands = {
        role: radiating_band_hz(sections[role]) for role in (woofer_role, tweeter_role)
    }
    # **At the CANDIDATE's corner** — defect site 1 of seven, and the one an
    # operator reads first: legacy named the session's corner on this line while
    # the shapes beside it were the candidate's.
    emit(
        "correction.crossover_v2_linearization_fit_band",
        {
            "fc_hz": round(float(fc_hz), 3),
            "radiating_band_hz": {
                role: rounded_band_hz(band) for role, band in radiating_bands.items()
            },
            "crossover_order": {
                role: tuple(s.order for s in sections[role])
                for role in (woofer_role, tweeter_role)
            },
        },
    )

    # --- the session's ONE shared level frame (PR-L5) ----------------------
    #
    # Compose every envelope FIRST, read each driver's own core-passband level
    # off it, and reconcile the set into a single frame before any driver is
    # fitted. Before this, each driver was fitted to a flat target at its own
    # median and the two numbers had no stated relationship at all — 13.9 dB
    # apart on the 2026-07-27 JTS3 profile, reconciled only by a trim that was
    # itself carrying ~11 dB of frame error. PR-L4 asserted afterwards that the
    # realized branch levels agree; this makes them agree by construction.
    envelopes: dict[str, Any] = {}
    for role in (woofer_role, tweeter_role):
        resp = responses[role]
        sigma_db = compose_sigma_db(
            resp,
            siblings[role],
            tier=request.mic_tier,
            valid_band_hz=excited_band_hz[role],
        )
        # The cloud seam (plan PR-6a's two optional terms). All three arguments
        # are ``None`` when no cloud verdict was available, and
        # ``compose_envelope`` documents that case as byte-identical to an
        # envelope composed before the terms existed. They can only ever NARROW
        # allowed depth: they enter the same ``np.min`` as every other term, so
        # no cloud can buy the fit permission it did not already have.
        cloud = request.cloud
        envelopes[role] = compose_envelope(
            role,
            resp,
            excited_band_hz=excited_band_hz[role],
            mic_tier=request.mic_tier,
            driver_class=driver_class[role],
            sigma_db=sigma_db,
            excluded_bands_hz=cloud.excluded_bands_hz if cloud else None,
            band_spread=cloud.band_spread if cloud else None,
            n_positions=cloud.n_positions if cloud else None,
        )
    core_levels_db = {
        role: level
        for role in (woofer_role, tweeter_role)
        if (
            level := driver_core_level_db(
                responses[role],
                envelopes[role],
                radiating_band_hz=radiating_bands[role],
            )
        )
        is not None
    }
    # The frame reconciles two LEVEL-MATCH estimates, so its trim term is the
    # trim solve's own level-match result (``trim_band_average_db``) — not
    # ``trim_db``, which is that result AFTER the ripple-optimal polish moved it
    # for summed flatness. Reading the applied trim made the gate sensitive to a
    # refinement it is not measuring: the polish is bounded by the sanity margin
    # (6.0), DOUBLE the gate's tolerance, so an ordinary 3-6 dB polish hard-
    # stopped an otherwise healthy session (adversarial review S5). Falls back
    # to ``trim_db`` only for a legacy candidate constructed before the field
    # existed.
    frame_trims_db = dict(request.trim_band_average_db or request.raw_trim_db)
    level_frame = (
        solve_shared_level_frame(core_levels_db, frame_trims_db)
        if core_levels_db
        else None
    )
    # The frame's own honesty evidence, and the reason it is not just applied.
    #
    # ``offset_db[role]`` is how far that role's own proposal
    # (``core_level + trim``) sits below the loudest — which is exactly the
    # DISAGREEMENT between two measured estimates of the same physical
    # relationship: the trim solve's power-band average on each side of Fc, and
    # the fit's median over each driver's own RADIATING band (#1929). A small
    # offset is an honest reconciliation. A LARGE one means the two instruments
    # are measuring different things again — so it is returned here and
    # adjudicated at the host's accountability seam rather than silently
    # applied.
    #
    # How close "close" actually is, measured rather than asserted: on the
    # archived run-5 capture the two land 0.510 dB apart (1.076 before #1929),
    # and on a pair that is identical by construction they still land 0.910 dB
    # apart. The residual is real and #1929 did not close it.
    level_frame_disagreement_db = (
        max((abs(v) for v in level_frame.offset_db.values()), default=0.0)
        if level_frame is not None
        else 0.0
    )
    # The frame's own INPUTS, for the refusal's journal line (#1929). A refusal
    # that names only the disagreement asks whoever reads it to re-derive which
    # driver read what, and over which band.
    #
    # BOTH bands are reported, and that is the point. ``radiating_band_hz`` is
    # the bound this gate asked for; ``band_hz`` is the span the median was
    # actually taken over. They differ exactly when the bound was refused for
    # leaving too little band — the case where a reader diagnosing a refusal
    # would otherwise be shown a band the number in front of them was never
    # computed over.
    level_frame_cores = {
        role: {
            "level_db": round(float(level), 3),
            "band_hz": rounded_band_hz(
                core_level_band_hz(
                    envelopes[role], radiating_band_hz=radiating_bands[role]
                )
            ),
            "radiating_band_hz": rounded_band_hz(radiating_bands[role]),
        }
        for role, level in core_levels_db.items()
    }
    # The OTHER estimator, for the #1866 finding. Restricted to the roles the
    # frame was actually solved over, so the banked pair is the pair that
    # disagreed rather than every role the trim solve happened to return.
    level_frame_trims = {
        role: float(frame_trims_db[role])
        for role in core_levels_db
        if role in frame_trims_db
    }

    # Boost permission is EVIDENCE-gated, and this is the gate. ONE necessary
    # condition, plus a clause that distinguishes two different ways a cloud can
    # be missing.
    #
    # 1. NECESSARY: the JOURNEY will MEASURE what the speaker did with the boost
    #    (``post_apply_verifies``). Every plan shape declares a post-apply check
    #    today, so this reads as always-on — but it is the correct condition
    #    rather than a constant, so a future tier that drops the post-apply
    #    sweep drops boost with it instead of shipping an unverified one.
    #
    # 2. The cloud clause, and the OWNER RULING (#2106, 2026-08-05) that rewrote
    #    it. R15 removed the pre-apply cloud from stage 1, so on the shipped
    #    driver-only path there is no cloud to wait for and the old
    #    ``cloud is not None`` demand would have demoted every R15 correction to
    #    cut-only for want of evidence the plan never collects. The ruling
    #    permits boost there, on the named and accepted risk that a boost can
    #    land on a position-specific artifact an at-mark verification cannot
    #    detect, adjudicated by post-apply VERIFY, household listening, and
    #    retained Undo. See the "Boost ruling" block in
    #    ``docs/crossover-linearization-80-20-plan.md`` §4.2.
    #
    #    What did NOT change is the case the retired condition was written for:
    #    a session that PLANNED a cloud and LOST it. There
    #    ``compose_envelope`` receives ``excluded_bands_hz=None``, so
    #    ``allowed_depth_db`` is NOT zeroed in the registry's interference
    #    nulls, and granting boost would let the fit EQ a null the session's own
    #    instrument was supposed to have found. Absent by DESIGN and absent by
    #    FAILURE share the ``cloud is None`` signature and are different
    #    evidence states; ``cloud_phase_planned`` is what tells them apart.
    #
    # What bounds a boost once permitted, on EITHER path: the envelope's own
    # depth limits, the realized-cascade stopband-gain guard, the headroom
    # charge, post-apply VERIFY, and Undo. The gate grants a vocabulary, never a
    # filter.
    vocabulary = FitVocabulary(
        allow_boost=request.post_apply_verifies
        and (request.cloud is not None or not request.cloud_phase_planned),
        boost_excluded_bands_hz=(
            request.cloud.boost_excluded_bands_hz if request.cloud is not None else ()
        ),
    )

    fits: dict[str, Any] = {}
    corrections: dict[str, np.ndarray] = {}
    for role in (woofer_role, tweeter_role):
        resp = responses[role]
        fit = fit_driver_linearization(
            resp,
            envelopes[role],
            vocabulary=vocabulary,
            level_frame=level_frame,
            radiating_band_hz=radiating_bands[role],
            # The branch's own committed crossover as the fit's target SHAPE
            # (#1817, R10a) — built from the SAME ``sections`` the radiating
            # band above and the emitter's own filters come from, so the shape
            # the fit aims at and the filter the graph runs cannot disagree.
            # ``None`` for a role with no committed region, which is the
            # pre-R10a flat target exactly.
            target=branch_target(sections[role], envelopes[role].freqs_hz),
        )
        fits[role] = fit
        # #1967's per-filter verdicts, disclosed as a journal record rather than
        # from inside the fit: ``linearization_fit`` is pure computation and
        # owns no logger. Emitted only when the bound actually decided
        # something, so a session with no cloud exclusions adds no noise.
        if fit.lift_boost_excluded_drops or fit.lift_boost_excluded_residual:
            emit(
                "correction.crossover_v2_boost_excluded_verdicts",
                {
                    "role": role,
                    # Dropped: the boost was AIMED at a contradicted band.
                    "dropped": [d.to_dict() for d in fit.lift_boost_excluded_drops],
                    # Kept: skirt tail from filters working elsewhere. Accepted
                    # and measured by the post-apply sweep, not refused here.
                    "residual": [r.to_dict() for r in fit.lift_boost_excluded_residual],
                    "lift_suppressed_reason": fit.lift_suppressed_reason,
                },
                logging.WARNING if fit.lift_boost_excluded_drops else logging.INFO,
            )
        # COMPLEX (minimum-phase) correction, not a zero-phase magnitude scale
        # (#1667). The emitted biquads rotate phase near their corners and the
        # two-branch summation below is phase-dominated, so a magnitude-only
        # model mispredicts it — measured on JTS3, the zero-phase model
        # mistracked the VERIFY summation by ~2.0 dB (WORSE than the ~1.7 dB of
        # no correction at all) where this complex model tracks to ~0.5 dB. This
        # is the single seam: the complex-corrected branches below feed all
        # three consumers (the trim re-solve, the ripple-optimal scan, and the
        # persisted VERIFY prediction).
        corrections[role] = complex_correction_response(fit.filters, resp.freqs_hz)

    freqs = responses[woofer_role].freqs_hz
    w_lin = responses[woofer_role].complex_tf * corrections[woofer_role]
    t_lin = responses[tweeter_role].complex_tf * corrections[tweeter_role]

    # Same gating-consistent overlap band the raw trim solve used, so the
    # comparison below is apples to apples: same band, linearized vs raw branch
    # content. **At the CANDIDATE's corner** — defect site 2 of seven.
    lo, hi = overlap_band_hz(
        fc_hz,
        tweeter_sweep_lo_hz=excited_band_hz[tweeter_role][0],
        woofer_sweep_hi_hz=excited_band_hz[woofer_role][1],
    )
    branch_floor_hz = request.branch_floor_hz
    lo_clamped = (
        max(lo, branch_floor_hz)
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz)
        else lo
    )

    # Each branch's OWN excited-and-gated validity span, the shape
    # ``branch_level_bands_hz`` takes (PR-L3) — the declared sweep band, floored
    # by the shared reflection floor — so the realized-level check reads the
    # same frame the raw trim solve read, one layer up on the linearized
    # branches.
    def _span(role: str) -> tuple[float, float]:
        span_lo, span_hi = excited_band_hz[role]
        if branch_floor_hz is not None and math.isfinite(branch_floor_hz):
            span_lo = max(span_lo, branch_floor_hz)
        return float(span_lo), float(span_hi)

    woofer_span = _span(woofer_role)
    tweeter_span = _span(tweeter_role)

    # ANCHORED give-back (#1668, replaces the overlap-band solve seed after the
    # 2026-07-24 JTS3 runs). Each branch's linearized trim is its own COMMITTED
    # raw trim plus ``LinearizationFit.correction_giveback_db`` — the fit
    # engine's SSOT, the MEASURED before-vs-after level delta of that branch's
    # own reference (core) band. Because the quantity added back IS the measured
    # level change of the band being restored, this restores each branch's
    # audible band to the pre-correction system level the raw candidate already
    # accepted — with no flat-core assumption, no solver prediction, and no
    # cross-branch coupling.
    #
    # Why not the old ``solve_branch_trims(W_lin, T_lin)`` band-average seed: it
    # averaged over the CROSSOVER OVERLAP band, which is the wrong reference for
    # a top-octave correction on two counts — the tweeter's LR4 high-pass skirt
    # lives there, and a power-domain mean weights the loudest (least-cut) bins
    # hardest. Measured live 2026-07-24: it returned only 5.81 dB of a 9.27 dB
    # spend, leaving the whole tweeter band ~3 dB low. PR-L3 later fixed the
    # overlap-band frame itself, but the anchor stays: measured give-back beats
    # any solver prediction for restoring a corrected branch's own level.
    #
    # PR-L5 adds ONE term: ``level_frame_offset_db``, how far this branch must
    # move to reach the session's shared level frame. The anchor already returns
    # a branch to its OWN pre-correction system level; the offset then places
    # that level where the frame says it belongs. The two together mean
    # ``target + trim`` is the same number for every branch by construction. It
    # is 0.0 for the frame's own reference role and for any session with no
    # frame, so a fit that predates the frame anchors byte-identically.
    raw_trim = dict(request.raw_trim_db)
    # The anchor's base trim is the SAME term the frame was solved on, so the
    # two cannot disagree about which trim they are anchoring to. With a frame
    # this is ``trim_band_average_db``; without one it is the applied trim,
    # byte-identical to the pre-PR-L5 anchor.
    #
    # Using the applied ``trim_db`` here while the frame used the band average
    # would let the raw candidate's ripple polish survive into the anchor —
    # which it never did before, because the trim term cancels out of
    # ``raw_trim + giveback + offset`` (the offset subtracts the same trim the
    # base adds, leaving ``giveback + system − core``). Precisely: the RELATIVE
    # level between branches is exact either way, and the emitted pair is
    # identical whenever ``normalize_shift_db`` clamps (the ordinary case). When
    # it does not — which needs a net-BOOSTED core band — the pair shifts in
    # COMMON MODE by the change in ``system_level_db``; every emitted trim is
    # still ≤ 0, and a common shift is a volume-knob difference, not a tonal
    # one.
    anchor_base_db = frame_trims_db if level_frame is not None else raw_trim
    anchored_unnormalized = {
        role: float(
            anchor_base_db.get(role, 0.0)
            + fits[role].correction_giveback_db
            + fits[role].level_frame_offset_db
        )
        for role in (woofer_role, tweeter_role)
    }
    # Normalize to non-positive: a branch whose own cuts give back more than its
    # raw attenuation would otherwise land POSITIVE (a boost), which the emitter
    # refuses and the hardware must never see. Subtracting the same shift from
    # every role preserves the relative leveling exactly and is honest extra
    # ledger (a little more max-SPL spent, disclosed below).
    normalize_shift_db = max(0.0, max(anchored_unnormalized.values()))
    anchored = {
        role: value - normalize_shift_db
        for role, value in anchored_unnormalized.items()
    }

    # Ripple fine-tune around the anchor: the anchor sets the LEVEL, the scan
    # only polishes summed flatness near it.
    #
    # ...and only where that band straddles Fc (PR-L3). On a tweeter swept from
    # Fc the band is ``[Fc, 2*Fc]``, where the woofer sits 20+ dB down its
    # skirt: the summed ripple is the tweeter's own and barely responds to the
    # tweeter's gain, so the scan is not measuring the handoff. A selector that
    # cannot see the woofer does not set the woofer's handoff level.
    #
    # **At the CANDIDATE's corner** — defect sites 3 (the straddle test), 4 (the
    # solve) and 5 (this event's ``fc_hz`` field) of seven.
    ripple_lin: float | None = None
    if lo_clamped < fc_hz < hi:
        trim_t_lin, ripple_lin, _seed_lin = solve_ripple_optimal_trim(
            freqs,
            w_lin,
            t_lin,
            fc_hz,
            lo_hz=lo_clamped,
            hi_hz=hi,
            seed_trim_db=anchored[tweeter_role],
            trim_w_db=anchored[woofer_role],
            sign=int(request.polarity_sign),
        )
    else:
        emit(
            "correction.crossover_v2_linearization_ripple_trim_skipped",
            {
                "reason": "ripple_band_one_sided",
                "fc_hz": round(float(fc_hz), 3),
                "ripple_band_hz": (round(float(lo_clamped), 1), round(float(hi), 1)),
                "anchored_trim_db": {k: round(v, 3) for k, v in anchored.items()},
            },
        )
        trim_t_lin = anchored[tweeter_role]
    resolved = {
        woofer_role: anchored[woofer_role],
        tweeter_role: float(trim_t_lin),
    }

    emit(
        "correction.crossover_v2_linearization_giveback",
        {
            "giveback_db": {
                role: round(float(fits[role].correction_giveback_db), 3)
                for role in (woofer_role, tweeter_role)
            },
            "raw_trim_db": {k: round(v, 3) for k, v in raw_trim.items()},
            "anchored_trim_db": {k: round(v, 3) for k, v in anchored.items()},
            "normalize_shift_db": round(float(normalize_shift_db), 3),
            # The FIT frame's own per-role level, beside the TRIM frame this
            # line already carries (PR-L3 forensics). ``raw_trim_db`` should
            # track the negated difference of these two; a large disagreement
            # means the level match and the fit are measuring different things,
            # which is exactly what shipped the 10 dB-dark tweeter.
            "target_level_db": {
                role: round(float(fits[role].target_level_db), 3)
                for role in (woofer_role, tweeter_role)
            },
            # PR-L5: the shared frame the two targets were reconciled into, and
            # the per-role move it asked for. A large offset is the 10 dB-dark
            # shape being CORRECTED, not a new problem.
            "level_frame_system_db": (
                round(float(level_frame.system_level_db), 3)
                if level_frame is not None
                else None
            ),
            "level_frame_reference_role": (
                level_frame.reference_role if level_frame is not None else None
            ),
            "level_frame_offset_db": {
                role: round(float(fits[role].level_frame_offset_db), 3)
                for role in (woofer_role, tweeter_role)
            },
        },
    )

    # --- grade both pairs, then commit one ---------------------------------
    #
    # **At the CANDIDATE's corner** — defect site 6 of seven, on BOTH pairs.
    # Two of legacy's seven reads lived one method away — its
    # ``_realized_level_match`` (this one) and ``_branch_crossover_sections``
    # (site 7, disclosed above) — which is why an audit of the fitter alone
    # counted five.
    anchored_match = realized_level_match(
        freqs,
        w_lin,
        t_lin,
        fc_hz,
        anchored,
        woofer_role,
        tweeter_role,
        woofer_span_hz=woofer_span,
        tweeter_span_hz=tweeter_span,
    )
    resolved_match = realized_level_match(
        freqs,
        w_lin,
        t_lin,
        fc_hz,
        resolved,
        woofer_role,
        tweeter_role,
        woofer_span_hz=woofer_span,
        tweeter_span_hz=tweeter_span,
    )
    trim = decide_trim(
        anchored_db=anchored,
        resolved_db=resolved,
        tweeter_role=tweeter_role,
        anchored_match=anchored_match,
        resolved_match=resolved_match,
        ripple_db=ripple_lin,
        sanity_margin_db=request.trim_sanity_margin_db,
    )
    role_attenuations_db = dict(trim.committed_db)

    if trim.beyond_sanity_margin:
        emit(
        "correction.crossover_v2_linearization_trim_rejected",
        {
            "raw_trim_db": {k: round(v, 3) for k, v in raw_trim.items()},
            "resolved_trim_db": {
                k: round(v, 3) for k, v in trim.resolved_db.items()
            },
            # #1668 anchored give-back: the anchor the guard is measured
            # against, and — since #2291 Phase 2 — the pair that ships.
            "anchored_trim_db": {
                k: round(v, 3) for k, v in trim.anchored_db.items()
            },
            "fallback_trim_db": {
                k: round(v, 3) for k, v in role_attenuations_db.items()
            },
            "anchored_level_error_db": round(
                float(anchored_match.difference_db), 3
            ),
            "resolved_level_error_db": round(
                float(resolved_match.difference_db), 3
            ),
            # Derived, not restated: a literal here would be a second place
            # recording which pair won, free to drift from the one that
            # decided it. Mutation exposed exactly that — flipping the
            # committed pair left this field reading "anchored", with no
            # test looking at it.
            "committed": trim.committed_side,
            "strategy": trim.strategy.value,
            "drift_db": round(float(trim.anchor_drift_db), 3),
            "margin_db": trim.sanity_margin_db,
            # P4 telemetry (2026-07-24 review): the ripple at each trim
            # lets live evidence distinguish "legitimate flatter optimum
            # rejected" from "garbage correctly caught" before anyone
            # widens the guard. ``None`` is unreachable here — a skipped
            # scan leaves the trim AT the anchor, so the drift is 0 by
            # construction — but the field stays honest rather than
            # reporting a fabricated 0.0.
            "resolved_ripple_db": (
                round(float(trim.ripple_db), 3)
                if trim.ripple_db is not None
                else None
            ),
            "raw_predicted_ripple_db": round(
                float(request.predicted_ripple_db), 3
            ),
        },
        level=logging.WARNING,
        )

    # PR-L4 item 1: the inter-driver realized-level ledger, on every fitted
    # candidate whatever the guard decided. Recorded here (where the linearized
    # branches live) and ASSERTED at the host's accountability seam —
    # deliberately outside the host's degrade-to-trims-only catch, because an
    # accountability refusal must stop the session rather than quietly become
    # the unlinearized path.
    committed_match = trim.committed_match
    emit(
    "correction.crossover_v2_realized_level_match",
    {
        "matched": committed_match.matched,
        "level_w_db": round(float(committed_match.level_w_db), 3),
        "level_t_db": round(float(committed_match.level_t_db), 3),
        "difference_db": round(float(committed_match.difference_db), 3),
        "tolerance_db": committed_match.tolerance_db,
        "woofer_band_hz": tuple(
            round(v, 1) for v in committed_match.woofer_band_hz
        ),
        "tweeter_band_hz": tuple(
            round(v, 1) for v in committed_match.tweeter_band_hz
        ),
        "trim_db": {k: round(v, 3) for k, v in role_attenuations_db.items()},
    },
    level=logging.WARNING if not committed_match.matched else logging.INFO,
    )

    # VERIFY-prediction coherence (hardware-validation-caught live finding,
    # #1668 PR-D): the emitted graph carries these SAME W_lin/T_lin correction
    # filters whichever trim pair was committed — the sanity guard only ever
    # changes the TRIM, never whether the filters are emitted — so the persisted
    # VERIFY prediction must be rebuilt from them too, at whichever trim
    # ``role_attenuations_db`` actually ended up holding. Without this, VERIFY
    # compared the correctly-linearized measured summation against a prediction
    # still built from the raw branches — a deterministic mismatch equal to the
    # filters' own in-band response (measured live on JTS3: 1.688-1.699 dB
    # against the 1.5 dB tolerance).
    #
    # The delay term is rung P3 / R10b ("make the summed model carry the
    # committed delay and trim"). ``w_lin``/``t_lin`` are the branches'
    # ``DriverResponse.complex_tf`` times a correction, windowed on each
    # branch's OWN argmax exactly as ``_aligned_branch_tf`` does, so this pair
    # sits in the SAME argmax-referenced frame as the raw pair and takes the
    # SAME residual — never the applied delay itself, which would double-count
    # the measured peak gap.
    #
    # Both numbers come off the alignment the host read, the same pair
    # ``alignment_to_candidate_fields`` uses to build the emitted
    # ``MeasuredCrossoverAlignment`` — so "the committed delay" here is
    # literally the delay that reaches the graph. Site 1 deriving its residual
    # from the same two values is what leaves the raw and linearized predictions
    # differing by the correction filters and nothing else.
    predicted_lin = predicted_branch_sum(
        w_lin,
        t_lin,
        role_attenuations_db[woofer_role],
        role_attenuations_db[tweeter_role],
        int(request.polarity_sign),
        freqs_hz=freqs,
        residual_delay_us=summed_model_residual_delay_us(
            request.anchor_delay_us, request.delay_us
        ),
    )
    linearized_predicted_sum = (
        freqs,
        20.0 * np.log10(np.maximum(np.abs(predicted_lin), 1e-12)),
    )

    # The headroom charge, computed now that the trim is committed (#1808).
    #
    # A correction's cost is a property of the CHAIN it is emitted into — the
    # crossover that follows it and the trim that follows that — so the
    # topology-agnostic fit core cannot compute it and deliberately does not
    # try. This is the same ``branch_headroom_db`` the emitter charges
    # ``active_baseline_headroom`` with, over the same three terms, so the
    # number the household is told and the number the speaker gives up are one
    # number rather than two that agree by inspection.
    #
    # ``role_attenuations_db`` and not ``anchored``/``resolved``: whichever pair
    # the decision above committed is the trim the graph will run, and the
    # charge follows the emitted graph.
    #
    # ``normalize_shift_db`` is NET-NEUTRAL under this rule, which is why it is
    # left exactly as it was. Subtracting a common S from every branch's trim
    # lowers every branch's chain peak by S, so it lowers this charge by S too,
    # and the pre-split attenuation the emitter applies falls by the same S the
    # branches gained: identical output, no lost loudness. The one case where it
    # is not neutral is a branch whose whole chain already sits below unity —
    # the charge floors at 0 and cannot fall further — which is honest ledger,
    # still disclosed by the ``normalize_shift_db`` field of the giveback record
    # above.
    charge_db: dict[str, float] = {}
    peak_db: dict[str, float] = {}
    linearization: dict[str, Any] = {}
    for role, fit in fits.items():
        emitted = [f.to_dict() for f in fit.filters]
        trim_db = float(role_attenuations_db.get(role, 0.0))
        peak_db[role] = branch_chain_peak_db(
            emitted, sections=sections[role], trim_db=trim_db
        )
        charge_db[role] = headroom_charge_db(peak_db[role])
        linearization[role] = replace(fit, headroom_cost_db=charge_db[role]).to_dict()
    emit(
    "correction.crossover_v2_linearization_headroom",
    {
        # What the branch actually puts above unity at its loudest
        # bin...
        "chain_peak_db": {r: round(v, 3) for r, v in peak_db.items()},
        # ...what that costs the speaker's maximum level (peak + margin,
        # 0.0 for a chain that never exceeds unity)...
        "headroom_cost_db": {r: round(v, 3) for r, v in charge_db.items()},
        # ...and what the retired sum-of-positives rule would have
        # charged, kept so the reclaimed loudness is visible in the
        # journal rather than only in a design doc.
        "sum_of_positives_db": {
            role: round(
                math.fsum(f.gain for f in fit.filters if f.gain > 0.0), 3
            )
            for role, fit in fits.items()
        },
        "trim_db": {
            role: round(float(role_attenuations_db.get(role, 0.0)), 3)
            for role in fits
        },
        "margin_db": HEADROOM_MARGIN_DB,
    },
    )

    return LinearizationPlan(
        fc_hz=fc_hz,
        role_attenuations_db=role_attenuations_db,
        linearization=linearization,
        trim=trim,
        level_frame=level_frame,
        level_frame_disagreement_db=level_frame_disagreement_db,
        level_frame_cores=level_frame_cores,
        level_frame_trims=level_frame_trims,
        linearized_predicted_sum=linearized_predicted_sum,
        headroom_charge_db=charge_db,
        chain_peak_db=peak_db,
        radiating_band_hz=radiating_bands,
        journal=tuple(records),
        journal_dropped=tuple(dropped),
    )


def request_from_analysis(
    analysis: Any,
    candidate: Any,
    *,
    context: CandidateAcousticContext,
    woofer_role: str,
    tweeter_role: str,
    excited_band_hz: Mapping[str, tuple[float, float]],
    driver_class_by_role: Mapping[str, str],
    post_apply_verifies: bool,
    cloud_phase_planned: bool,
    cloud: Any = None,
) -> LinearizationRequest:
    """Assemble a request from a ``ProgramAnalysis`` and its raw candidate.

    The one place the host's measurement objects are unpacked into the
    planner's explicit inputs. It is a *derivation*, not a policy: every value
    is read straight off the analysis, and the two facts the analysis cannot
    know (``post_apply_verifies``, ``cloud_phase_planned``) are the host's to
    pass.

    Raises :class:`PlannerInputError` when the analysis lacks a driver response
    for either role or carries no alignment — both of which the host's own
    eligibility gate is supposed to have excluded, so reaching them means the
    gate and this derivation disagree and the plan must not be guessed at.
    """
    if analysis.alignment is None:
        raise PlannerInputError("a MEASURE analysis must carry an alignment")
    drivers: dict[str, DriverEvidence] = {}
    for role in (woofer_role, tweeter_role):
        response = driver_response_by_role(analysis, role)
        if response is None:
            raise PlannerInputError(f"no measured response for role {role!r}")
        band = excited_band_hz.get(role)
        if band is None:
            raise PlannerInputError(f"no excited band for role {role!r}")
        drivers[role] = DriverEvidence(
            role=role,
            response=response,
            excited_band_hz=(float(band[0]), float(band[1])),
            driver_class=str(driver_class_by_role.get(role, "unknown")),
        )
    alignment = analysis.alignment
    return LinearizationRequest(
        context=context,
        woofer=drivers[woofer_role],
        tweeter=drivers[tweeter_role],
        raw_trim_db=dict(candidate.trim_db),
        trim_band_average_db=dict(candidate.trim_band_average_db or {}),
        predicted_ripple_db=float(candidate.predicted_ripple_db),
        polarity_sign=int(alignment.polarity_sign),
        delay_us=float(alignment.delay_us),
        # ``None`` when the alignment is not OK is load-bearing, not a default:
        # ``summed_model_residual_delay_us`` treats an absent anchor differently
        # from a zero one, and the candidate's own ``anchor_delay_us`` is NOT
        # used because it is ``None`` on the no-declared-bounds path where the
        # alignment's is not, and the two models must not disagree.
        anchor_delay_us=(
            alignment.anchor_delay_us if alignment.status == ALIGNMENT_OK else None
        ),
        mic_tier=str(analysis.mic_tier),
        branch_floor_hz=measure_validity_floor_hz(analysis),
        post_apply_verifies=bool(post_apply_verifies),
        cloud_phase_planned=bool(cloud_phase_planned),
        cloud=CloudFitTerms.from_evidence(cloud),
    )
