# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract for the analyze walk and its skip disclosure.

The bar, in the shape `REFACTOR-CUTOVER-2026-08.md` §2 words it after #3169:

  1. A bank missing one input kind produces **N−1** results and one
     ``AnalysisSkip`` **naming** the input.
  2. Isolation is **per-unit at the gate** — that is where per-unit isolation is
     real, so that is where the mutation goes.
  3. Isolation is **per-walk at the produce half** — a raise from the single
     analysis entry marks the walk failed, and takes every admitted unit with
     it rather than one.
  4. ``results`` is a copy.
"""
from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.active_speaker.crossover_v2 import analysis_walk
from jasper.active_speaker.crossover_v2 import session as session_module
from jasper.active_speaker.crossover_v2.analysis_units import (
    ABSOLUTE_NO_FC,
    ANALYSIS_UNITS,
    NO_SUMMED_SWEEP,
    AnalysisSkip,
)
from jasper.active_speaker.crossover_v2.analysis_walk import (
    ANALYSIS_FAILED_EVENT,
    NO_BANKED_RECORD,
    NO_CAPTURE_BYTES,
    NO_CAPTURE_ROOT,
    NO_DRIVER_BANDS,
    AnalysisDeclaration,
    capture_inputs,
    walk_bank,
)
from jasper.active_speaker.crossover_v2.program_transaction import (
    STIMULUS_CAPTURE_NOT_BOUND,
    STIMULUS_NOT_CAPTURED,
)
from jasper.active_speaker.crossover_v2.programs import (
    PILOT_LEVEL_DELTA_DB,
    courtesy_prelude_for_phase,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    PROGRAM_PHASE_MEASURE,
    RoleBand,
    build_measure_program,
    render_program_pcm,
)
from jasper.audio_measurement.wired_capture import encode_wav_s32

from tests.engine_twin import tuning_session

BANDS = {"woofer": (60.0, 4000.0), "tweeter": (1500.0, 18000.0)}
GAINS = {"woofer": -11.0, "tweeter": -13.0}
GLOBAL_OFFSET = 4096
WAV_RELATIVE = "captures/take-0.wav"


def _roles() -> tuple[RoleBand, ...]:
    return (
        RoleBand("woofer", 0, FrequencyBand(*BANDS["woofer"])),
        RoleBand("tweeter", 1, FrequencyBand(*BANDS["tweeter"])),
    )


def _program():
    """Composed exactly as ``rebuild_measure_program``'s grid composes it.

    `downstream_gain_db=0.0` is a point on that grid and the shipped prelude is
    the value it tries first, so the rebuild reproduces this program's id — the
    only condition on which it accepts a reconstruction at all.
    """
    return build_measure_program(
        GAINS,
        _roles(),
        downstream_gain_db=0.0,
        leading_pilot_gains_db=(
            GAINS["woofer"] - PILOT_LEVEL_DELTA_DB, GAINS["woofer"],
        ),
        leading_pilot_role="woofer",
        courtesy_prelude=courtesy_prelude_for_phase(PROGRAM_PHASE_MEASURE),
    )


def _state(program_id: str | None = None) -> dict:
    """The three inputs the rebuild reads, in the reader's own spelling."""
    return {
        "gain_plan_db": dict(GAINS),
        "candidate": {
            "program_id": _program().program_id if program_id is None
            else program_id
        },
    }


def _declared(root=None, *, bands=None, fc_hz=2400.0) -> AnalysisDeclaration:
    return AnalysisDeclaration(
        driver_bands_hz=BANDS if bands is None else bands,
        crossover_fc_hz=fc_hz,
        capture_root=root,
    )


def _capture(program, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    impulse = np.zeros(512)
    impulse[0] = 1.0
    impulse[7] = 0.4
    impulse += rng.normal(0.0, 1e-4, impulse.size)
    pcm = render_program_pcm(program)
    mono = pcm.sum(axis=1) if pcm.ndim > 1 else pcm
    body = fftconvolve(mono, impulse)[: mono.size]
    out = np.zeros(GLOBAL_OFFSET + body.size)
    out[GLOBAL_OFFSET:] = body * 0.4
    return out


@pytest.fixture
def bank(tmp_path):
    """A root holding one real capture, and the record that points at it."""
    program = _program()
    samples = _capture(program)
    as_int32 = np.clip(samples, -1.0, 1.0) * (2**31 - 1)
    wav, _frames = encode_wav_s32(
        as_int32.astype("<i4"), sample_rate_hz=program.sample_rate_hz,
    )
    path = tmp_path / WAV_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav)
    return tmp_path, {"take-0": {"take_id": "take-0", "wav_path": WAV_RELATIVE}}


def test_the_fixture_bank_assembles(bank):
    """Guards every test below against passing on an unassembled bank."""
    root, records = bank
    assembled = capture_inputs(records["take-0"], _state(), _declared(root))
    assert not isinstance(assembled, str), assembled


def test_a_real_bank_produces_results_for_every_unit_it_can_feed(bank):
    """The wholesale default, over a capture that actually exists."""
    root, records = bank
    walk = walk_bank(records, _state(), _declared(root))

    produced = walk.results["take-0"]
    assert produced, "a real MEASURE bank produced nothing"
    assert walk.failed == {"take-0": ()}
    ran = set(produced)
    skipped = {skip.name for skip in walk.skipped["take-0"]}
    assert ran | skipped == {unit.name for unit in ANALYSIS_UNITS}
    assert not ran & skipped


def test_results_hold_only_the_fields_each_unit_owns(bank):
    """Sixteen keys must not be sixteen references to one whole analysis."""
    root, records = bank
    walk = walk_bank(records, _state(), _declared(root))
    by_name = {unit.name: unit for unit in ANALYSIS_UNITS}
    for name, fields in walk.results["take-0"].items():
        assert set(fields) == set(by_name[name].fields)


def test_a_missing_input_kind_costs_exactly_its_units(bank):
    """The bar: N−1 results, and the skip NAMES the input.

    A MEASURE bank carries no summed sweep, so the units gated on one are
    exactly the units absent from the results — each saying so by code.
    """
    root, records = bank
    walk = walk_bank(records, _state(), _declared(root))

    summed_units = {
        unit.name
        for unit in ANALYSIS_UNITS
        if unit.name in {
            "summed_response", "summed_ripple", "verify_tracking",
            "capture_integrity",
        }
    }
    for skip in walk.skipped["take-0"]:
        if skip.name in summed_units:
            assert skip.missing == NO_SUMMED_SWEEP
    assert summed_units.isdisjoint(walk.results["take-0"])
    assert len(walk.results["take-0"]) == (
        len(ANALYSIS_UNITS) - len(walk.skipped["take-0"])
    )


@pytest.mark.parametrize(
    ("record", "state", "bands", "fc_hz", "use_root", "expected"),
    [
        (None, True, True, 2400.0, True, NO_BANKED_RECORD),
        ({"wav_path": WAV_RELATIVE}, True, False, 2400.0, True, NO_DRIVER_BANDS),
        ({"wav_path": WAV_RELATIVE}, True, True, None, True, ABSOLUTE_NO_FC),
        ({"wav_path": WAV_RELATIVE}, True, True, 0.0, True, ABSOLUTE_NO_FC),
        ({"wav_path": ""}, True, True, 2400.0, True, NO_CAPTURE_BYTES),
        (
            {"wav_path": "", "incident": STIMULUS_NOT_CAPTURED},
            True, True, 2400.0, True, STIMULUS_NOT_CAPTURED,
        ),
        (
            {"wav_path": "", "incident": STIMULUS_CAPTURE_NOT_BOUND},
            True, True, 2400.0, True, STIMULUS_CAPTURE_NOT_BOUND,
        ),
        (
            {"wav_path": "captures/absent.wav"},
            True, True, 2400.0, True, NO_CAPTURE_BYTES,
        ),
        ({"wav_path": WAV_RELATIVE}, True, True, 2400.0, False, NO_CAPTURE_ROOT),
        (
            {"wav_path": WAV_RELATIVE},
            False, True, 2400.0, True, "candidate.program_id",
        ),
    ],
)
def test_every_unreachable_input_is_named_not_raised(
    bank, record, state, bands, fc_hz, use_root, expected,
):
    """A capture the walk cannot assemble is disclosed, never an exception.

    A zero corner is "no corner", not a crossover at DC — the same truthiness
    the analysis layer's own normalization uses.

    Two rows are about ATTRIBUTION rather than about a missing input: a record
    whose stimulus played and left no bytes carries the play transaction's own
    reason, and the skip cites that instead of this module's generic
    ``no_capture_bytes``. ``stimulus_not_captured`` and ``capture_not_bound``
    send a reader to two different places; ``no_capture_bytes`` sends them to
    neither, and it is what the walk falls back to when the record says
    nothing.
    """
    root, _records = bank
    answer = capture_inputs(
        record,
        _state() if state else {},
        _declared(
            root if use_root else None,
            bands=None if bands else {},
            fc_hz=fc_hz,
        ),
    )
    assert answer == expected


def test_an_unprovable_program_skips_every_unit_with_the_readers_own_reason(bank):
    """`rebuild_measure_program` accepts only on proof, and its reason travels.

    A state whose banked ``program_id`` no grid point reproduces must not
    become a guessed program — every unit skips, carrying the refusal the
    reader itself minted.
    """
    root, records = bank
    walk = walk_bank(records, _state("not-a-real-program-id"), _declared(root))

    assert walk.results == {"take-0": {}}
    assert walk.failed == {"take-0": ()}
    assert {skip.name for skip in walk.skipped["take-0"]} == {
        unit.name for unit in ANALYSIS_UNITS
    }
    assert len({skip.missing for skip in walk.skipped["take-0"]}) == 1


def test_a_raising_gate_fails_only_its_own_unit(bank, monkeypatch):
    """Per-unit isolation is real at the gate, so it is pinned at the gate."""
    root, records = bank
    clean = walk_bank(records, _state(), _declared(root))

    def explode(_inputs):
        raise RuntimeError("gate probe")

    victim = ANALYSIS_UNITS[7]
    patched = tuple(
        dataclasses.replace(unit, gate=explode) if unit is victim else unit
        for unit in ANALYSIS_UNITS
    )
    monkeypatch.setattr(analysis_walk, "ANALYSIS_UNITS", patched)
    walk = walk_bank(records, _state(), _declared(root))

    assert walk.failed == {"take-0": (victim.name,)}
    assert victim.name not in walk.results["take-0"]
    # Nothing else moved: every other unit's outcome is what it was.
    assert set(walk.results["take-0"]) == set(clean.results["take-0"]) - {
        victim.name
    }
    assert set(walk.skipped["take-0"]) == set(clean.skipped["take-0"])


def test_a_raising_analysis_entry_fails_the_WALK_not_one_unit(bank, monkeypatch):
    """The forfeit, enforced: one entry point means one blast radius.

    With no per-unit ``run``, a raise inside ``analyze_program_capture`` takes
    every admitted unit together — and they are **failed**, never folded into
    the skips, which mean something else entirely.
    """
    root, records = bank
    clean = walk_bank(records, _state(), _declared(root))

    def explode(*_args, **_kwargs):
        raise RuntimeError("layer probe")

    monkeypatch.setattr(analysis_walk, "analyze_program_capture", explode)
    walk = walk_bank(records, _state(), _declared(root))

    assert walk.results == {"take-0": {}}
    assert set(walk.failed["take-0"]) == set(clean.results["take-0"])
    assert len(walk.failed["take-0"]) > 1, "the point is it is not one unit"
    assert set(walk.skipped["take-0"]) == set(clean.skipped["take-0"])
    assert not set(walk.failed["take-0"]) & {
        skip.name for skip in walk.skipped["take-0"]
    }


def test_a_failure_is_never_silent(bank, monkeypatch, caplog):
    """A raise the walk absorbs still says so — no silent failure."""
    root, records = bank

    def explode(*_args, **_kwargs):
        raise RuntimeError("layer probe")

    monkeypatch.setattr(analysis_walk, "analyze_program_capture", explode)
    with caplog.at_level("WARNING"):
        walk_bank(records, _state(), _declared(root))

    emitted = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
        and ANALYSIS_FAILED_EVENT in record.getMessage()
    ]
    assert emitted, "an absorbed raise emitted no failure event"
    # Structured fields, never prose: which half raised, and for which record.
    assert "half=produce" in emitted[0]
    assert "record_id=take-0" in emitted[0]


async def _analyze_over(bank, **session_kwargs):
    """One session whose bank is the fixture's, analyzed."""
    root, records = bank
    session, fakes = tuning_session(
        gain_plan_db=dict(GAINS),
        candidate_program_id=_program().program_id,
        analysis_declaration=_declared(root),
        **session_kwargs,
    )
    for record in records.values():
        session._banked.append(await fakes.records.bank(record))
    return await session.analyze()


@pytest.mark.asyncio
async def test_analyze_surfaces_results_and_skips_from_one_walk(bank):
    """The verb's own answer, not just the walk's."""
    outcome = await _analyze_over(bank)
    assert len(outcome.results) == 1
    record_id = next(iter(outcome.results))
    ran = set(outcome.results[record_id])
    skipped = {skip.name for skip in outcome.skipped[record_id]}
    assert ran and skipped
    assert ran | skipped == {unit.name for unit in ANALYSIS_UNITS}


@pytest.mark.asyncio
async def test_two_records_each_get_their_own_answer(bank):
    """B1: the outcome carries the RECORD dimension, and must.

    A session banks one record per position per ladder rung, so this — one
    reachable capture beside one unreachable one — is the ordinary bank, not
    the edge case. Flattened on unit name, the second record's analysis
    overwrote the first's, the skips repeated a name with nothing to tell the
    occurrences apart, and eight units landed in BOTH results and skipped.
    """
    root, records = bank
    two = {
        "take-0": records["take-0"],
        "take-1": {"take_id": "take-1", "wav_path": ""},
    }
    session, fakes = tuning_session(
        gain_plan_db=dict(GAINS),
        candidate_program_id=_program().program_id,
        analysis_declaration=_declared(root),
    )
    for record in two.values():
        session._banked.append(await fakes.records.bank(record))
    outcome = await session.analyze()

    ids = tuple(outcome.results)
    assert len(ids) == 2
    assert set(ids) == set(outcome.skipped)

    # Which id is which is decided by content, not by iteration order.
    produced = [rid for rid in ids if outcome.results[rid]]
    barren = [rid for rid in ids if not outcome.results[rid]]
    assert len(produced) == 1, "the reachable capture produced nothing"
    assert len(barren) == 1
    assert {skip.name for skip in outcome.skipped[barren[0]]} == {
        unit.name for unit in ANALYSIS_UNITS
    }

    # THE per-record partition invariant, and the collision it forbids.
    for record_id in ids:
        ran = set(outcome.results[record_id])
        skipped = {skip.name for skip in outcome.skipped[record_id]}
        assert not ran & skipped, "a unit reported as both run and skipped"
        assert len(ran) == len(ANALYSIS_UNITS) - len(
            outcome.skipped[record_id]
        )
        assert len(skipped) == len(outcome.skipped[record_id]), "repeated name"


@pytest.mark.asyncio
async def test_analyze_hands_out_a_copy_of_the_walkers_results(bank, monkeypatch):
    """`AnalyzeOutcome`'s own contract line: a copy, not the dict it filled.

    Asserted by IDENTITY against the walk's own mapping, because equality
    cannot tell a copy from the original and two separate walks cannot either —
    each would build its own dict whether or not the copy is made.
    """
    seen: dict[str, Any] = {}
    real = analysis_walk.walk_bank

    def spy(*args, **kwargs):
        outcome = real(*args, **kwargs)
        seen["walk"] = outcome
        return outcome

    monkeypatch.setattr(session_module, "walk_bank", spy)
    outcome = await _analyze_over(bank)

    assert outcome.results == seen["walk"].results
    assert outcome.results is not seen["walk"].results
    outcome.results["injected"] = {}  # type: ignore[index]
    assert "injected" not in seen["walk"].results

    # The per-record mappings are copies too — a nested alias would leak
    # exactly as far, one level down.
    record_id = next(iter(seen["walk"].results))
    assert outcome.results[record_id] is not seen["walk"].results[record_id]
    outcome.results[record_id]["injected"] = "probe"  # type: ignore[index]
    assert "injected" not in seen["walk"].results[record_id]


@pytest.mark.asyncio
async def test_an_empty_bank_says_every_gate_said_no_rather_than_nothing(bank):
    """The semantic flip, asserted as behaviour.

    A session with records it cannot reach returns empty ``results`` AND a full
    ``skipped`` — which is what makes an empty ``results`` readable as "every
    gate said no" instead of "nothing is wired yet".
    """
    _root, records = bank
    session, fakes = tuning_session()  # told nothing
    for record in records.values():
        session._banked.append(await fakes.records.bank(record))

    outcome = await session.analyze()
    assert list(outcome.results.values()) == [{}]
    only = next(iter(outcome.skipped.values()))
    assert {skip.name for skip in only} == {
        unit.name for unit in ANALYSIS_UNITS
    }
    assert all(skip.missing for skip in only)


@pytest.mark.asyncio
async def test_a_session_with_no_records_skips_nothing_at_all():
    """The other empty: no bank means no units to report on, not sixteen."""
    session, _fakes = tuning_session()
    outcome = await session.analyze()
    assert outcome.results == {}
    assert outcome.skipped == {}


@pytest.mark.asyncio
async def test_save_writes_the_rebuilds_third_input(bank):
    """A state carrying only two of the three rebuilds nothing."""
    root, _records = bank
    session, fakes = tuning_session(analysis_declaration=_declared(root))
    await session.save()
    assert fakes.records.persisted[-1]["bands_hz"] == {
        role: list(band) for role, band in sorted(BANDS.items())
    }


def test_the_skip_carries_two_structured_fields_and_no_prose(bank):
    """Household wording belongs to `refusal_copy`, not to a skip."""
    root, records = bank
    walk = walk_bank(records, _state(), _declared(root))
    assert walk.skipped["take-0"]
    for skip in walk.skipped["take-0"]:
        assert isinstance(skip, AnalysisSkip)
        assert skip.missing and " " not in skip.missing
    assert tuple(f.name for f in dataclasses.fields(AnalysisSkip)) == (
        "name", "missing",
    )
