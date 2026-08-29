# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract for the analysis-unit table.

Four behaviours, and nothing about the table's prose:

  1. **The names are a vocabulary.** Unique, and the derived set is derived.
  2. **The fields partition the produced class.** Every unit field is a real
     ``ProgramAnalysis`` field, no field belongs to two units, and the union is
     the twenty produced ones — which is what makes "fifteen units" a count of
     something rather than an assertion.
  3. **Every gate is total.** Over a generated grid of programs and priors, no
     gate raises and every gate answers with a code or ``""``. A gate that
     raises turns an honest skip into a failure, and those are different facts.
  4. **Unit 14's gate and the analysis layer give the same answer.** The gate
     re-tests conditions ``_verify_absolute_result`` tests again internally;
     rather than lift them out of a layer that ports whole, the two answers are
     pinned equal.
"""
from __future__ import annotations

import dataclasses
import itertools

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import analysis_units
from jasper.active_speaker.crossover_v2.analysis_units import (
    ANALYSIS_NAMES,
    ANALYSIS_UNITS,
    NO_LEVEL_CHECK_CAPTURE,
    NO_SUMMED_SWEEP,
    SKIP_CODES,
    AnalysisInputs,
    AnalysisSkip,
)
from jasper.audio_measurement import program as program_mod
from jasper.audio_measurement import program_analysis as pa
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    AMBIENT_SEGMENT_ID,
    KIND_COURTESY_TONE,
    KIND_PILOT,
    KIND_SILENCE,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    ExcitationProgram,
    ProgramSegment,
)

WOOFER = "woofer"
TWEETER = "tweeter"


def _segment(
    segment_id: str, kind: str, *, role: str | None = None, start: int = 0
) -> ProgramSegment:
    stimulus = kind not in (KIND_SILENCE, KIND_COURTESY_TONE)
    return ProgramSegment(
        segment_id=segment_id,
        kind=kind,
        role=role,
        channel=0 if stimulus else None,
        start_sample=start,
        n_samples=4800,
        f1_hz=100.0 if stimulus else None,
        f2_hz=8000.0 if stimulus else None,
        gain_db=-12.0,
        effective_peak_dbfs=-12.0,
    )


def _program(*segments: ProgramSegment) -> ExcitationProgram:
    """A schedule of exactly the given segments, laid end to end.

    A program must carry at least one segment, so "a bank with no analysable
    content" is a program of pure silence rather than a program of nothing.
    ``program_id`` is minted by ``program.py``'s own hash — the dataclass
    refuses a schedule whose id does not match its content.
    """
    placed = tuple(
        dataclasses.replace(seg, start_sample=index * 4800)
        for index, seg in enumerate(segments or (_segment("lead_in", KIND_SILENCE),))
    )
    total = len(placed) * 4800
    return ExcitationProgram(
        program_id=program_mod._program_id("measure", 48000, 2, placed, total),
        phase="measure",
        sample_rate_hz=48000,
        channels=2,
        segments=placed,
        total_samples=total,
    )


# The content axes a gate can read, one program shape each. Combined below into
# every subset, so the grid covers a bank carrying nothing through a bank
# carrying everything — including the two shapes that are deliberately NOT
# content.
CONTENT_AXES: dict[str, tuple[ProgramSegment, ...]] = {
    "ambient": (_segment(AMBIENT_SEGMENT_ID, KIND_SILENCE),),
    "courtesy": (_segment("courtesy", KIND_COURTESY_TONE),),
    "pilot": (_segment("pilot_w", KIND_PILOT, role=WOOFER),),
    "one_sweep": (_segment("sweep_w", KIND_SWEEP, role=WOOFER),),
    "repeat_sweep": (
        _segment("sweep_w", KIND_SWEEP, role=WOOFER),
        _segment("sweep_w_rep", KIND_SWEEP, role=WOOFER),
    ),
    "both_branches": (
        _segment("sweep_w", KIND_SWEEP, role=WOOFER),
        _segment("sweep_t", KIND_SWEEP, role=TWEETER),
    ),
    "roleless_sweep": (_segment("sweep_x", KIND_SWEEP),),
    "summed": (_segment("sweep_verify", KIND_SUMMED_SWEEP, role="summed"),),
}

PREDICTION = (np.array([100.0, 1000.0]), np.array([0.0, 0.0]))
TRANSFER = {WOOFER: lambda freqs: np.ones_like(freqs, dtype=np.complex128)}

PRIORS_SHAPES: dict[str, pa.MeasurementPriors] = {
    "bare": pa.MeasurementPriors(),
    # 0.0 is "no Fc", not a corner at DC — _analyze_verify normalizes on
    # truthiness before the layer sees it, and the gate must match.
    "fc_zero": pa.MeasurementPriors(crossover_fc_hz=0.0),
    "fc": pa.MeasurementPriors(crossover_fc_hz=2400.0),
    "fc_and_prediction": pa.MeasurementPriors(
        crossover_fc_hz=2400.0, predicted_sum=PREDICTION,
    ),
    "fc_and_target": pa.MeasurementPriors(
        crossover_fc_hz=2400.0, configured_crossover_response_by_role=TRANSFER,
    ),
    # An EMPTY mapping is falsy, so it is "no target" — the layer tests
    # `not transfers`, not `transfers is None`.
    "empty_target": pa.MeasurementPriors(
        crossover_fc_hz=2400.0, configured_crossover_response_by_role={},
    ),
}


def _grid() -> list[AnalysisInputs]:
    """Every subset of the content axes against every priors shape."""
    axes = sorted(CONTENT_AXES)
    out: list[AnalysisInputs] = []
    for size in range(len(axes) + 1):
        for chosen in itertools.combinations(axes, size):
            segments = tuple(
                seg for name in chosen for seg in CONTENT_AXES[name]
            )
            program = _program(*segments)
            for priors in PRIORS_SHAPES.values():
                out.append(AnalysisInputs(program=program, priors=priors))
    return out


GRID = _grid()


def test_the_grid_is_not_empty():
    """Guards the three property tests below against a silent no-op."""
    assert len(GRID) == 2 ** len(CONTENT_AXES) * len(PRIORS_SHAPES)


def test_every_unit_name_is_unique_and_the_name_set_is_derived():
    names = [unit.name for unit in ANALYSIS_UNITS]
    assert len(names) == len(set(names))
    assert ANALYSIS_NAMES == {unit.name for unit in ANALYSIS_UNITS}


# The produced class of the ProgramAnalysis field census, by name. Held here
# rather than counted, so a swap — one produced field traded for another —
# cannot pass a pin that only checked how many there were.
PRODUCED_FIELDS = frozenset(
    {
        "alignment",
        "ambient_report",
        "anchor_ambiguous",
        "candidate",
        "capture_integrity",
        "channel_map_ok",
        "drift",
        "driver_responses",
        "frame_ledger",
        "gain_plan",
        "linearity_ok",
        "locations",
        "pilot_snr_ok",
        "pilots",
        "predicted_sum",
        "summed_response",
        "summed_ripple_db",
        "verify_absolute",
        "verify_tracking",
        "verify_tracking_curve",
    }
)

# The six that belong to no unit: four copy an input parameter, one projects
# another field, one is a predicate over inputs alone.
UNOWNED_FIELDS = frozenset(
    {
        "phase",
        "program_id",
        "mic_tier",
        "mic_calibrated",
        "glitch_detected",
        "configured_path_composed",
    }
)


def test_the_units_partition_the_produced_program_analysis_fields():
    """Fifteen units over twenty fields, asserted by identity, not by count.

    The union is exactly the *produced* class of the ``ProgramAnalysis`` field
    census. The six fields that copy an input, project another field, or are a
    predicate over inputs belong to no unit and must not appear here.
    """
    declared = {field.name for field in dataclasses.fields(pa.ProgramAnalysis)}
    owned: list[str] = [name for unit in ANALYSIS_UNITS for name in unit.fields]

    assert PRODUCED_FIELDS | UNOWNED_FIELDS == declared, (
        "the ProgramAnalysis field set moved; re-run the census before "
        "re-pointing this table"
    )
    assert len(owned) == len(set(owned)), "a field belongs to two units"
    assert set(owned) == PRODUCED_FIELDS
    assert not set(owned) & UNOWNED_FIELDS


def test_the_table_order_sequences_the_units_that_depend_on_each_other():
    """Order is the table's only dependency mechanism, so it is pinned.

    `candidate` consumes `alignment`'s output, and the verify trio narrows left
    to right. A walker runs the table in order; re-sorting it would break that
    silently.
    """
    order = [unit.name for unit in ANALYSIS_UNITS]
    assert order.index("alignment") < order.index("candidate")
    assert (
        order.index("summed_response")
        < order.index("summed_ripple")
        < order.index("verify_tracking")
    )


@pytest.mark.parametrize("unit", ANALYSIS_UNITS, ids=lambda u: u.name)
def test_every_gate_is_total_over_the_grid(unit):
    """No gate raises, and every answer is a string."""
    for inputs in GRID:
        answer = unit.gate(inputs)
        assert isinstance(answer, str)


def test_every_unit_runs_on_some_bank():
    """The wholesale default has something to be wholesale about.

    Without this, "every gate is total" is satisfied by a table whose gates all
    answer "no" — which is indistinguishable at the wire from an empty table.

    It takes TWO banks, not one, and that is the point rather than a weakness:
    the level-check units and the sweep units are mutually exclusive by content,
    because the only phase whose analyzer binds `ambient_report` and `gain_plan`
    is the one that carries no sweep. A single bank running all fifteen would
    mean the separator was not doing its job.
    """
    everything = tuple(
        seg
        for name in ("ambient", "pilot", "both_branches", "repeat_sweep", "summed")
        for seg in CONTENT_AXES[name]
    )
    # `predicted_sum` is the one input that is a prior rather than content.
    sweeps = AnalysisInputs(
        program=_program(*everything),
        priors=dataclasses.replace(
            PRIORS_SHAPES["fc_and_target"], predicted_sum=PREDICTION,
        ),
    )
    level_check = AnalysisInputs(
        program=_program(*CONTENT_AXES["ambient"], *CONTENT_AXES["pilot"]),
        priors=PRIORS_SHAPES["bare"],
    )

    ran: set[str] = set()
    for inputs in (sweeps, level_check):
        ran |= {unit.name for unit in ANALYSIS_UNITS if not unit.gate(inputs)}
    assert ran == ANALYSIS_NAMES

    # And neither bank alone reaches all fifteen — the exclusion is real.
    for inputs in (sweeps, level_check):
        assert any(unit.gate(inputs) for unit in ANALYSIS_UNITS)


def test_an_empty_bank_skips_every_gated_unit_and_names_the_missing_input():
    """A skip says WHICH input was missing, never merely that one was.

    ``AnalysisSkip`` carries no message text: a skip is two structured fields,
    and the wording belongs to ``refusal_copy``.
    """
    inputs = AnalysisInputs(program=_program(), priors=PRIORS_SHAPES["bare"])
    skips = [
        AnalysisSkip(unit.name, unit.gate(inputs))
        for unit in ANALYSIS_UNITS
        if unit.gate(inputs)
    ]
    assert len(skips) == 12  # the three total units still run
    assert all(skip.missing for skip in skips)
    assert tuple(f.name for f in dataclasses.fields(AnalysisSkip)) == (
        "name",
        "missing",
    )


def test_the_courtesy_tone_is_not_content():
    """A gate walking all segments instead of the stimulus set counts it.

    The courtesy tone and silence are both deliberately outside
    ``STIMULUS_KINDS``, and a bank holding only a courtesy tone carries no
    analysable content at all.
    """
    inputs = AnalysisInputs(
        program=_program(*CONTENT_AXES["courtesy"]), priors=PRIORS_SHAPES["fc"],
    )
    gated = [unit.name for unit in ANALYSIS_UNITS if not unit.gate(inputs)]
    assert gated == ["frame_ledger", "anchor", "locations"]


REAL_BANDS = (
    program_mod.RoleBand(role=WOOFER, channel=0, band=FrequencyBand(60.0, 4000.0)),
    program_mod.RoleBand(role=TWEETER, channel=1, band=FrequencyBand(1500.0, 18000.0)),
)


def _real_programs() -> dict[str, ExcitationProgram]:
    """The three shipped programs, composed by their own builders.

    Hand-built schedules cannot show this: all three carry the ambient window
    and a leading pilot pair, because ``_append_pilot_ambient_window`` gives
    those pilots a noise floor on every phase that has them.
    """
    pilots = {"leading_pilot_gains_db": (-30.0, -24.0)}
    return {
        "check": program_mod.build_check_program(REAL_BANDS),
        "measure": program_mod.build_measure_program(
            {WOOFER: 0.0, TWEETER: 0.0},
            REAL_BANDS,
            leading_pilot_role=WOOFER,
            **pilots,
        ),
        "verify": program_mod.build_verify_program(2400.0, **pilots),
    }


def test_all_three_shipped_programs_carry_the_ambient_window():
    """The premise the two pins below rest on, asserted rather than assumed."""
    for program in _real_programs().values():
        ids = {seg.segment_id for seg in program.segments}
        assert AMBIENT_SEGMENT_ID in ids
        assert any(seg.kind == KIND_PILOT for seg in program.stimulus_segments())


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_a_sweep_carrying_bank_skips_the_two_level_check_units(phase: str):
    """`ambient_report` and `gain_plan` are bound by ONE analyzer, `_analyze_check`.

    Their inputs are not: the ambient window and the pilots ride every phase.
    A gate reading only "an ambient window is present" answers RUN on a MEASURE
    or VERIFY bank for two analyses neither phase ever produces — an analysis
    reported as run that nothing asked, which is the defect this table exists
    to remove, inverted.
    """
    inputs = AnalysisInputs(
        program=_real_programs()[phase], priors=PRIORS_SHAPES["fc_and_target"],
    )
    by_name = {unit.name: unit for unit in ANALYSIS_UNITS}
    assert by_name["ambient"].gate(inputs) == NO_LEVEL_CHECK_CAPTURE
    assert by_name["gain_plan"].gate(inputs) == NO_LEVEL_CHECK_CAPTURE


def test_a_level_check_bank_still_runs_the_two_level_check_units():
    """The other half: the separator must not skip the phase that DOES bind them."""
    inputs = AnalysisInputs(
        program=_real_programs()["check"], priors=PRIORS_SHAPES["bare"],
    )
    by_name = {unit.name: unit for unit in ANALYSIS_UNITS}
    assert by_name["ambient"].gate(inputs) == ""
    assert by_name["gain_plan"].gate(inputs) == ""


def test_every_code_a_gate_can_return_is_in_the_exported_set():
    """The module is the skip-code SSOT, so its export surface must be complete.

    Two of the codes are imported from the analysis layer rather than declared
    here; a consumer switching on a skip must not have to know which.
    """
    returnable = {
        answer
        for unit in ANALYSIS_UNITS
        for inputs in GRID
        if (answer := unit.gate(inputs))
    }
    assert returnable == SKIP_CODES
    exported = set(analysis_units.__all__)
    for code_name in ("ABSOLUTE_NO_FC", "ABSOLUTE_NO_TARGET"):
        assert code_name in exported


def _absolute_gate():
    return next(unit for unit in ANALYSIS_UNITS if unit.name == "verify_absolute")


@pytest.mark.parametrize("priors_name", sorted(PRIORS_SHAPES))
def test_unit_14s_gate_agrees_with_the_analysis_layer(priors_name: str):
    """The two answers are pinned equal rather than the duplication lifted.

    Scoped to a bank that HAS a summed sweep, because with none the layer is
    never reached. Both of the layer's remaining refusals are about the trusted
    band, which is derived from the response rather than from the bank, so the
    gate cannot and does not claim them — the layer keeps those, and this
    asserts nothing about them.
    """
    priors = PRIORS_SHAPES[priors_name]
    inputs = AnalysisInputs(
        program=_program(*CONTENT_AXES["summed"]), priors=priors,
    )
    code = _absolute_gate().gate(inputs)
    if not code:
        return

    # Exactly _analyze_verify's own normalization: a zero Fc is "no Fc".
    fc_hz = float(priors.crossover_fc_hz) if priors.crossover_fc_hz else None
    assert pa._verify_absolute_result(None, None, fc_hz, priors) == {
        "not_evaluated": code
    }


def test_unit_14_still_skips_a_bank_with_no_summed_sweep():
    """The half the layer never sees, because it is never called."""
    inputs = AnalysisInputs(
        program=_program(), priors=PRIORS_SHAPES["fc_and_target"],
    )
    assert _absolute_gate().gate(inputs) == NO_SUMMED_SWEEP
