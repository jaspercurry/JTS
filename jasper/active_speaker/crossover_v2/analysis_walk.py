# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Walking :data:`~.analysis_units.ANALYSIS_UNITS` over what a bank carries.

The table says which analyses exist and what each one needs. This says what one
banked record actually holds, runs the layer over it, and reports every unit the
bank could not feed **by name and by reason**. Separate module because the table
is data and this is the code that reads it — a table that grows a walk stops
being a table.

**Isolation is per-unit at the gate and per-walk at the produce half.** The
analysis layer has one entry point, ``analyze_program_capture``, and it computes
every field in one pass. So a gate raising takes out its own unit and nothing
else, while a raise inside the layer fails **all** the produce-halves together —
that is a `failed` walk, not fifteen skips, and the two must not be confused. The
forfeit is recorded at :mod:`.analysis_units`; this module is where it is
enforced.

**Nothing here guesses.** Every input a record does not carry becomes a skip code
naming that input. A program in particular is never invented: where a MEASURE
program can be rebuilt it is rebuilt through
:func:`~.harmonic_evidence.rebuild_measure_program`, which accepts only when the
rebuilt ``program_id`` reproduces the banked one, and where it cannot prove
itself the capture is skipped with the reader's own structured reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jasper.log_event import log_event

from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
    analyze_program_capture,
)
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

from .analysis_units import (
    ABSOLUTE_NO_FC,
    ANALYSIS_UNITS,
    AnalysisInputs,
    AnalysisSkip,
)
from .harmonic_evidence import HarmonicEvidenceRefused, rebuild_measure_program

__all__ = [
    "ANALYSIS_FAILED_EVENT",
    "NO_BANKED_RECORD",
    "NO_CAPTURE_BYTES",
    "NO_CAPTURE_ROOT",
    "NO_DRIVER_BANDS",
    "AnalysisDeclaration",
    "WalkOutcome",
    "capture_inputs",
    "walk_bank",
]

logger = logging.getLogger(__name__)

#: A unit's produce half or its gate raised. A failure is never silent and is
#: never folded into the skips — a skip means the bank lacked an input, and
#: reporting a fault as one would be the same lie in the other direction.
ANALYSIS_FAILED_EVENT = "crossover_v2_analysis_failed"

#: Why a whole capture could not be assembled. Each names the input the bank
#: would have to carry, in the same shape as the table's own gate codes — a
#: reader switching on a skip never has to know whether the input was missing
#: from the record or from the analysis the record points at.
NO_BANKED_RECORD = "no_banked_record"
NO_CAPTURE_ROOT = "no_capture_root"
NO_CAPTURE_BYTES = "no_capture_bytes"
NO_DRIVER_BANDS = "no_driver_bands"


@dataclass(frozen=True)
class AnalysisDeclaration:
    """What a host must tell a session before ``analyze`` can reach a capture.

    One declaration rather than three loose fields, because they are one fact:
    *where this session's evidence is and what it was measured through.* None of
    the three is derivable from a banked record, and every one of them is
    disclosed by name when absent rather than guessed.

    ``driver_bands_hz`` is
    :func:`~.harmonic_evidence.rebuild_measure_program`'s **third** input —
    ``gain_plan_db`` and ``candidate.program_id`` come out of the session state,
    and this does not, so a state carrying only those two rebuilds nothing.

    ``crossover_fc_hz`` is a precondition and not a nicety:
    ``analyze_program_capture`` REFUSES a MEASURE capture without one. It is a
    fact about the round, so a session is told it once rather than a reader
    supplying a corner the captures were never taken through.

    ``capture_root`` resolves a record's bundle-relative ``wav_path``. Only the
    store knows the bundle and its seam hands back records rather than bytes, so
    the host that owns both says it here.
    """

    driver_bands_hz: Mapping[str, tuple[float, float]] = field(
        default_factory=dict
    )
    crossover_fc_hz: float | None = None
    capture_root: Path | None = None


@dataclass(frozen=True)
class WalkOutcome:
    """What one walk over the bank produced, and everything it could not.

    **Every one of the three is keyed by RECORD id first**, and that is the
    shape rather than a convenience. A session banks one record per position
    per ladder rung, so a bank of one is the exception; flattening on unit name
    would let record 2's analysis overwrite record 1's, would repeat a unit name
    in the skips with nothing to tell the two occurrences apart, and would put a
    unit in the results AND the skips at once as soon as one record is reachable
    and another is not.

    The invariant that replaces it is **per record**: for every id,
    ``len(results[id]) + len(skipped[id]) + len(failed[id])`` is the whole
    table. A unit is in exactly one of the three, for exactly one capture.

    ``failed`` is the produce half's own state and is NOT a skip: a skipped
    unit was never asked because the bank lacks its input, while a failed one
    was asked and something raised. Collapsing them would reproduce, one level
    up, the defect the table exists to remove.
    """

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, tuple[AnalysisSkip, ...]] = field(default_factory=dict)
    failed: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _record_wav(record: Mapping[str, Any], root: Path | None) -> bytes | str:
    """The capture's bytes, or the code naming why they are out of reach."""
    relative = str(record.get("wav_path") or "")
    if not relative:
        # `""` is the record saying no bytes were placed — an honest fact
        # about the capture, never a path to guess at.
        return NO_CAPTURE_BYTES
    if root is None:
        return NO_CAPTURE_ROOT
    path = root / relative
    try:
        return path.read_bytes()
    except OSError:
        # An unreadable capture is a fact this verb discloses, on the same
        # terms the store's own `read` returns None rather than raising.
        return NO_CAPTURE_BYTES


def capture_inputs(
    record: Mapping[str, Any] | None,
    state: Mapping[str, Any],
    declared: AnalysisDeclaration,
) -> tuple[AnalysisInputs, Any, int] | str:
    """One banked record as analysis inputs, or the code naming what is missing.

    ``state`` is the session's own persisted state — two of the rebuild's three
    inputs live there, and the third is declared.

    Every prior beyond the declared corner stays at its default. That is
    deliberate rather than a shortcut: the layer already runs on defaults for
    the one shipped offline caller, and a default that makes a gate answer "no"
    is a disclosed skip, where a guessed prior would be a silent wrong answer.
    """
    if record is None:
        return NO_BANKED_RECORD
    if not declared.driver_bands_hz:
        return NO_DRIVER_BANDS
    if not declared.crossover_fc_hz:
        # The layer refuses a MEASURE capture without one, so an absent corner
        # is a missing INPUT for the whole capture rather than a fault — and it
        # is named with the code the layer's own refusals already use.
        return ABSOLUTE_NO_FC
    wav = _record_wav(record, declared.capture_root)
    if isinstance(wav, str):
        return wav
    try:
        program, _downstream_db, _prelude = rebuild_measure_program(
            state, declared.driver_bands_hz,
        )
    except HarmonicEvidenceRefused as refused:
        # Its refusals are already {"missing": <input>} shaped, so the reason
        # travels verbatim rather than being re-worded here. Falling back to
        # the refusal constant keeps a code in the skip either way.
        missing = refused.evidence.get("missing")
        return str(missing) if missing else refused.reason
    samples, rate = decode_wav_to_mono(wav)
    return (
        AnalysisInputs(
            program=program,
            priors=MeasurementPriors(crossover_fc_hz=declared.crossover_fc_hz),
        ),
        samples,
        rate,
    )


def _analysis(inputs: AnalysisInputs, samples: Any, rate: int) -> ProgramAnalysis:
    """The single analysis entry, called ONCE per capture.

    Not once per unit: the layer has no per-unit entry point, and fifteen calls
    would be fifteen full analyses of one capture for one dataclass each.
    """
    return analyze_program_capture(
        inputs.program,
        samples,
        rate,
        geometry=MeasurementGeometry(),
        priors=inputs.priors,
    )


def walk_bank(
    records: Mapping[str, Mapping[str, Any] | None],
    state: Mapping[str, Any],
    declared: AnalysisDeclaration,
) -> WalkOutcome:
    """Run every unit the bank can feed; name every unit it cannot.

    Answered **per record**: a unit's outcome is a fact about one capture, and
    two captures in one bank routinely disagree about it. Within a record,
    ``results`` is keyed by unit name and holds only the fields that unit owns —
    fifteen keys must not be fifteen references to one whole analysis.
    """
    results: dict[str, dict[str, Any]] = {}
    skipped: dict[str, tuple[AnalysisSkip, ...]] = {}
    failed: dict[str, tuple[str, ...]] = {}

    for record_id, record in records.items():
        produced: dict[str, Any] = {}
        missed: list[AnalysisSkip] = []
        faulted: list[str] = []
        results[record_id] = produced
        skipped[record_id] = ()
        failed[record_id] = ()

        assembled = capture_inputs(record, state, declared)
        if isinstance(assembled, str):
            # One code for the whole capture: the first assembly check that
            # fires answers for every unit, because none of them can run
            # without the capture itself.
            skipped[record_id] = tuple(
                AnalysisSkip(unit.name, assembled) for unit in ANALYSIS_UNITS
            )
            continue
        inputs, samples, rate = assembled

        admitted = []
        for unit in ANALYSIS_UNITS:
            # Per-unit, because the gate half IS per-unit: one gate raising
            # must not decide anything about the other fourteen.
            try:
                reason = unit.gate(inputs)
            except Exception as error:  # noqa: BLE001 - this unit's fault alone
                log_event(
                    logger, ANALYSIS_FAILED_EVENT, level=logging.WARNING,
                    half="gate", units=(unit.name,), record_id=record_id,
                    error=type(error).__name__,
                )
                faulted.append(unit.name)
                continue
            if reason:
                missed.append(AnalysisSkip(unit.name, reason))
            else:
                admitted.append(unit)

        if admitted:
            # Per-walk, because the produce half IS per-walk: one entry point
            # computes every field, so one raise takes all the admitted units.
            try:
                analysis = _analysis(inputs, samples, rate)
            except Exception as error:  # noqa: BLE001 - the layer's raise fails it
                log_event(
                    logger, ANALYSIS_FAILED_EVENT, level=logging.WARNING,
                    half="produce", units=tuple(u.name for u in admitted),
                    record_id=record_id, error=type(error).__name__,
                )
                faulted.extend(unit.name for unit in admitted)
            else:
                for unit in admitted:
                    produced[unit.name] = {
                        name: getattr(analysis, name) for name in unit.fields
                    }
        skipped[record_id] = tuple(missed)
        failed[record_id] = tuple(faulted)

    return WalkOutcome(results=results, skipped=skipped, failed=failed)
