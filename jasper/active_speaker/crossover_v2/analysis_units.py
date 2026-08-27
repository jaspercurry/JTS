# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The analysis-unit table: what `analyze` can run, and what a bank must carry.

There is no analysis-name vocabulary anywhere else in the tree. This module is
it — a literal table, walked by a function, in the shape :mod:`jasper.tools.packs`
already ships and :mod:`.measure_spec` already uses in this package.

**A unit is defined by its gate.** The definition the count rests on:

    A unit is a maximal set of ``ProgramAnalysis`` fields that are present or
    absent together over every ``(program, capture, priors)`` triple.

A field only joins a unit when it is *produced* — bound from a statement that
reads the capture samples. The three fields that copy an input parameter, the
one that re-reads another field, and the one that is a predicate over inputs
alone belong to no unit, which is why the 25-field dataclass yields 20 fields
across 15 rows. Grouping is therefore forced rather than chosen: ``linearity_ok``,
``channel_map_ok`` and ``pilot_snr_ok`` are total reductions of ``pilots``, so no
capture makes one present and another absent and they are one row, not four. A
gate that can never answer "no" is the ``_crossover_region_null_registry`` defect
wearing a table.

**Gates read CONTENT, never a phase or a measure kind.** Four vocabularies in
this tree claim to say which analysis applies and no two agree —
``PROGRAM_PHASES`` (3), ``journey.PHASE_*`` (11), ``MEASURE_KINDS`` (3) and
``STIMULUS_KINDS`` (3). Only the last describes what a capture actually
*contains*. ``MEASURE_KIND_VERIFY`` and ``PROGRAM_PHASE_VERIFY`` are both
``"verify"`` by spelling alone, there is no ``check`` kind, and
``program_for_phase`` answers eight journey phases with four program objects, so
every map through those collapses. So a gate here reads the program's segments
and the priors, and nothing else.

**A gate returns a reason string, not a bool** — ``""`` runs, anything else is
the code naming the input that was missing. This is the one deviation from
``packs.py``'s ``gate: Callable[[Any], bool]``, and it is deliberate: a bool
plus a parallel reason lookup is two writers of one fact. The precedent is
in-layer — :func:`~jasper.audio_measurement.program_analysis._verify_absolute_result`
already answers ``{"not_evaluated": <code>}`` with three named codes for exactly
this reason, and this table's vocabulary is a generalization of those codes
rather than a second idiom beside them. Unit 14 imports two of the three and
re-spells neither.

The third, ``ABSOLUTE_NO_TRUSTED_BAND``, is deliberately absent from this
module. A trusted band is derived from the *summed response's*
``validity_floor_hz`` — a result, not an input — so no gate reading a bank can
answer it. It stays inside ``_verify_absolute_result``, which owns it, and that
is the honest split: this table answers *can this bank support the analysis*,
the function answers *did the data support it*.

**Segment ids are never spelled here.** ``sweep_w``/``sweep_t``/``sweep_verify``
are f-string literals minted in ``program.py`` with no owner constant, and
``program.segment(id)`` raises rather than returning ``None``. Every gate below
reads structure instead — kinds, roles, and occurrence counts — so this module
adds no third spelling and no gate can raise. The one exception is
``AMBIENT_SEGMENT_ID``, which *is* an owned constant and is imported.

Order is load-bearing and pinned: 9 before 10 (``_build_candidate`` consumes
unit 9's output), and 11 before 12 before 13.

**A gate being total is not a promise that the unit cannot raise.** Every gate
here answers rather than throwing, but a bank a gate admits can still fail
inside the analysis layer — the producers hard-dereference ``sweep_w`` and
``sweep_t``, and a near-field single-driver capture (R-3) is the reachable case.
That is a **failed** unit, not a skipped one, and keeping the two apart is the
walker's contract to apply, not this table's.

**One forfeit, recorded rather than discovered later.** With no per-unit ``run``
callable, per-unit failure isolation is bounded by the layer's single entry
point: one ``analyze_program_capture`` raise fails all fifteen produce-halves at
once. Only *gate* raises are isolated per unit, and gates are the half this
module owns. A walker that wants finer isolation needs the layer to grow
per-unit entry points first; nothing here forecloses that, since a ``run`` field
is additive.

No caller yet. The walker over this table is a separate item.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_PILOT,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    ExcitationProgram,
)
from jasper.audio_measurement.program_analysis import (
    ABSOLUTE_NO_FC,
    ABSOLUTE_NO_TARGET,
    MeasurementPriors,
)

__all__ = [
    "ABSOLUTE_NO_FC",
    "ABSOLUTE_NO_TARGET",
    "ANALYSIS_NAMES",
    "ANALYSIS_UNITS",
    "NO_AMBIENT_WINDOW",
    "NO_BRANCH_SWEEP_PAIR",
    "NO_LEVEL_CHECK_CAPTURE",
    "NO_PILOT_SEGMENT",
    "NO_PREDICTED_SUM",
    "NO_REPEATED_SWEEP",
    "NO_ROLE_SWEEP",
    "NO_SUMMED_SWEEP",
    "SKIP_CODES",
    "AnalysisInputs",
    "AnalysisSkip",
    "AnalysisUnit",
]

#: Skip codes for the inputs no existing constant already names. Each says
#: WHICH input a bank lacks, never merely that one is missing — a skip that
#: cannot be told apart from another skip is the defect this table removes.
NO_AMBIENT_WINDOW = "no_ambient_window"
NO_PILOT_SEGMENT = "no_pilot_segment"
NO_LEVEL_CHECK_CAPTURE = "no_level_check_capture"
NO_REPEATED_SWEEP = "no_repeated_sweep"
NO_ROLE_SWEEP = "no_role_sweep"
NO_BRANCH_SWEEP_PAIR = "no_branch_sweep_pair"
NO_SUMMED_SWEEP = "no_summed_sweep"
NO_PREDICTED_SUM = "no_predicted_sum"

#: Every code a gate in this table can return, this module's own and the two
#: it borrows from the analysis layer. Composed from the constants rather than
#: re-spelling their values, so the borrowed half stays visibly borrowed — a
#: consumer switching on a skip needs one enumerable set, not two imports and
#: a guess. Pinned equal to the codes the gates actually produce.
SKIP_CODES = frozenset(
    {
        NO_AMBIENT_WINDOW,
        NO_PILOT_SEGMENT,
        NO_LEVEL_CHECK_CAPTURE,
        NO_REPEATED_SWEEP,
        NO_ROLE_SWEEP,
        NO_BRANCH_SWEEP_PAIR,
        NO_SUMMED_SWEEP,
        NO_PREDICTED_SUM,
        ABSOLUTE_NO_FC,
        ABSOLUTE_NO_TARGET,
    }
)


@dataclass(frozen=True)
class AnalysisInputs:
    """Everything a gate may read.

    Deliberately two fields. A gate that could reach the capture samples would
    be able to answer questions about the *result*, and a unit that skips on its
    own output has no meaning; a gate that could reach the phase would key on a
    vocabulary that collapses (see the module docstring).
    """

    program: ExcitationProgram
    priors: MeasurementPriors


@dataclass(frozen=True)
class AnalysisSkip:
    """One analysis that did not run, and the input that was missing.

    Two fields, and no sentence. ``name`` is the unit; ``missing`` is the input's
    code. Household wording belongs to :mod:`.refusal_copy`, which is the repo's
    one owner of it — a second sentence-composer here is the shape this refactor
    exists to remove.

    Not a :class:`~.measure_spec.CapabilityStub`, and the two must not be
    merged: a stub says *the engine never built this analysis*, a skip says
    *this bank lacks the input this analysis needs*. The stub's message is
    hard-wired to "not implemented", its ``aborted()`` raises on any code
    outside its four-row table, and its merge rule upgrades a disclosure when
    later evidence arrives. None of those is true of a skip.
    """

    name: str
    missing: str


@dataclass(frozen=True)
class AnalysisUnit:
    """One named analysis, the fields it owns, and the gate that skips it.

    ``fields`` is the unit's identity, not decoration: the unit definition IS a
    set of ``ProgramAnalysis`` fields that travel together, and it is the wire
    shape a walker projects — ``results`` keyed by unit name must not hold
    fifteen references to one whole analysis object.

    ``gate`` returns ``""`` to run and a skip code otherwise. It must be total:
    a gate that raises turns an honest skip into a failure, and the two are
    different facts.

    There is deliberately no ``requires`` field. A unit that depends on another
    repeats that unit's gate, and the table's order is what sequences them —
    the same property ``TOOL_PACKS`` already makes load-bearing.
    """

    name: str
    fields: tuple[str, ...]
    gate: Callable[[AnalysisInputs], str]


def _has_segment_id(inputs: AnalysisInputs, segment_id: str) -> bool:
    """Scans ALL segments: the ambient window is a silence segment."""
    return any(seg.segment_id == segment_id for seg in inputs.program.segments)


def _sweeps_by_role(inputs: AnalysisInputs) -> dict[str, int]:
    """Sweep-segment count per role, over stimulus segments only.

    ``stimulus_segments()`` is the courtesy-tone-safe enumerator:
    ``KIND_COURTESY_TONE`` and ``KIND_SILENCE`` are both deliberately outside
    ``STIMULUS_KINDS``, and a gate that walked ``program.segments`` instead
    would count a courtesy tone as content.
    """
    counts: dict[str, int] = {}
    for seg in inputs.program.stimulus_segments():
        if seg.kind == KIND_SWEEP and seg.role is not None:
            counts[seg.role] = counts.get(seg.role, 0) + 1
    return counts


def _has_stimulus_kind(inputs: AnalysisInputs, kind: str) -> bool:
    return any(seg.kind == kind for seg in inputs.program.stimulus_segments())


def _always(_inputs: AnalysisInputs) -> str:
    """Runs on every capture: these units are properties of the timeline.

    A missing phone report is not a missing input — it grades as
    not-evaluated inside the unit, which is a different fact from a skip.
    """
    return ""


def _needs_level_check_bank(inputs: AnalysisInputs) -> str:
    """The CHECK program's content signature: ambient and pilots, no sweeps.

    **Content alone does not identify these two units' bank, and this conjunct
    is why.** `ambient_report` and `gain_plan` are bound at exactly one
    construction site — `_analyze_check` — because the layer's analyzer is
    dispatched by phase. But the ambient window and the leading pilot pair are
    NOT unique to CHECK: `_append_pilot_ambient_window` puts both into the
    MEASURE and VERIFY programs as well, so those pilots have a noise floor to
    be judged against. A gate reading only "an ambient window is present" would
    therefore answer RUN on all three phases, for two analyses that two of the
    three never produce — reporting an analysis as run that nothing asked, which
    is the null-registry defect with its sign flipped.

    Absence of sweep content is what separates them, and it is a property of the
    bank rather than of a label: the CHECK program is the only one of the three
    carrying no per-role sweep and no summed sweep. So this stays a content
    gate, and the table still keys on nothing a phase vocabulary owns.
    """
    if _sweeps_by_role(inputs) or _has_stimulus_kind(inputs, KIND_SUMMED_SWEEP):
        return NO_LEVEL_CHECK_CAPTURE
    return ""


def _needs_ambient(inputs: AnalysisInputs) -> str:
    return _needs_level_check_bank(inputs) or (
        "" if _has_segment_id(inputs, AMBIENT_SEGMENT_ID) else NO_AMBIENT_WINDOW
    )


def _needs_pilots(inputs: AnalysisInputs) -> str:
    return "" if _has_stimulus_kind(inputs, KIND_PILOT) else NO_PILOT_SEGMENT


def _needs_ambient_and_pilots(inputs: AnalysisInputs) -> str:
    """A conjunction neither input's own unit carries.

    `_solve_gain_plan` never refuses today — with no pilots it returns an empty
    solve and says nothing, which is why this row exists: the gate turns that
    silence into a named skip. The level-check conjunct rides in through
    `_needs_ambient`, so it is stated in one place.
    """
    return _needs_ambient(inputs) or _needs_pilots(inputs)


def _needs_repeated_sweep(inputs: AnalysisInputs) -> str:
    """Drift is a first-vs-last comparison, so one occurrence is not enough."""
    counts = _sweeps_by_role(inputs)
    return "" if any(n >= 2 for n in counts.values()) else NO_REPEATED_SWEEP


def _needs_role_sweep(inputs: AnalysisInputs) -> str:
    return "" if _sweeps_by_role(inputs) else NO_ROLE_SWEEP


def _needs_branch_pair_and_fc(inputs: AnalysisInputs) -> str:
    """Both branches, plus the corner they are being aligned about."""
    if len(_sweeps_by_role(inputs)) < 2:
        return NO_BRANCH_SWEEP_PAIR
    return "" if inputs.priors.crossover_fc_hz else ABSOLUTE_NO_FC


def _needs_summed_sweep(inputs: AnalysisInputs) -> str:
    return "" if _has_stimulus_kind(inputs, KIND_SUMMED_SWEEP) else NO_SUMMED_SWEEP


def _needs_summed_and_fc(inputs: AnalysisInputs) -> str:
    return _needs_summed_sweep(inputs) or (
        "" if inputs.priors.crossover_fc_hz else ABSOLUTE_NO_FC
    )


def _needs_prediction(inputs: AnalysisInputs) -> str:
    return _needs_summed_and_fc(inputs) or (
        "" if inputs.priors.predicted_sum is not None else NO_PREDICTED_SUM
    )


def _needs_crossover_target(inputs: AnalysisInputs) -> str:
    """Reproduces ``_verify_absolute_result``'s own first two refusals.

    Fc before target, and Fc by TRUTHINESS — ``_analyze_verify`` normalizes a
    zero Fc to ``None`` before the function sees it, so ``0.0`` is "no Fc", not
    a corner at DC. The two remaining refusals in that function are about the
    trusted band, which no gate can see; see the module docstring.

    The duplication this leaves is accepted rather than lifted: removing it
    would mean editing the analysis layer, and the layer ports whole. The two
    answers are pinned equal instead.
    """
    unavailable = _needs_summed_sweep(inputs)
    if unavailable:
        return unavailable
    if not inputs.priors.crossover_fc_hz:
        return ABSOLUTE_NO_FC
    if not inputs.priors.configured_crossover_response_by_role:
        return ABSOLUTE_NO_TARGET
    return ""


#: The fifteen units, in walk order. Order is load-bearing — `candidate`
#: consumes `alignment`'s output, and the verify trio narrows left to right —
#: and it is pinned, the way ``TOOL_PACKS``'s is.
ANALYSIS_UNITS: tuple[AnalysisUnit, ...] = (
    AnalysisUnit("frame_ledger", ("frame_ledger",), _always),
    AnalysisUnit("anchor", ("anchor_ambiguous",), _always),
    AnalysisUnit("locations", ("locations",), _always),
    AnalysisUnit("ambient", ("ambient_report",), _needs_ambient),
    AnalysisUnit(
        "pilots",
        ("pilots", "linearity_ok", "channel_map_ok", "pilot_snr_ok"),
        _needs_pilots,
    ),
    AnalysisUnit("gain_plan", ("gain_plan",), _needs_ambient_and_pilots),
    AnalysisUnit("drift", ("drift",), _needs_repeated_sweep),
    AnalysisUnit("driver_responses", ("driver_responses",), _needs_role_sweep),
    AnalysisUnit("alignment", ("alignment",), _needs_branch_pair_and_fc),
    AnalysisUnit(
        "candidate", ("candidate", "predicted_sum"), _needs_branch_pair_and_fc,
    ),
    AnalysisUnit("summed_response", ("summed_response",), _needs_summed_sweep),
    AnalysisUnit("summed_ripple", ("summed_ripple_db",), _needs_summed_and_fc),
    AnalysisUnit(
        "verify_tracking",
        ("verify_tracking", "verify_tracking_curve"),
        _needs_prediction,
    ),
    AnalysisUnit("verify_absolute", ("verify_absolute",), _needs_crossover_target),
    # Shares `summed_response`'s gate object, and stays a separate row for the
    # same reason `candidate` does: a dependency edge, not a gate difference.
    # `_verify_capture_integrity` consumes the summed unit's result together
    # with unit 1's frame ledger, so no bank makes one present and the other
    # absent — merging them would hide an ordering the walker has to honour.
    AnalysisUnit("capture_integrity", ("capture_integrity",), _needs_summed_sweep),
)

#: Every name a walker's ``results`` may be keyed by. Derived from the table
#: rather than re-listed, so a sixteenth unit joins it by existing — the
#: ``STUB_CODES`` discipline.
ANALYSIS_NAMES = frozenset(unit.name for unit in ANALYSIS_UNITS)
