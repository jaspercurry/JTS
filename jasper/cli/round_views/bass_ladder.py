# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""How loud a bass rung proved it can play, from the steps the round banked.

* ``bass-ladder <round-dir> --target-id T`` — grade the ``bass_candidate``
  takes this round banked for rung ``T``, lowest stimulus first, and publish
  ``bass_ladder/T.json`` beside ``bass_fit.json``: the level the ladder proved
  and the step that ended it. Offline — no audio plays and no device is opened.

The document is what the prescription door requires before a boosted rung may
be adopted, and it is the ONE thing that admits a rung's level. The rule and
its constants live in :mod:`jasper.bass_extension.ladder_evidence`; this
module reads the round.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    HARMONIC_ORDERS,
    calibration_from_text,
    read_program_sweeps,
)
from jasper.active_speaker.crossover_v2.measure_spec import (
    GRAPH_SCOPE_BASS_CANDIDATE,
)
from jasper.active_speaker.crossover_v2.round_inputs import (
    round_inputs,
    round_artifact_dir,
)
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    KIND_SUMMED_SWEEP,
    ExcitationProgram,
)
from jasper.audio_measurement.sweep import read_wav_mono
from jasper.bass_extension.ladder_evidence import (
    LadderStep,
    grade_ladder,
    ladder_document_name,
)
from jasper.bass_extension.refusals import BassExtensionRefusal
from jasper.bass_extension.targets import MARGINS
from jasper.cli._refusal import EXIT_UNREADABLE

from ._common import (
    ARTIFACT_BY_VIEW,
    _ROUND_DIR_HELP,
    _ROUND_DIR_METAVAR,
    _write,
    answer,
    default_out,
    refused_by_name,
)

#: The fit view's artifact is this view's input: it names the margin policy
#: the rung was sized under and the plant its transform moves.
BASS_FIT_FILENAME = ARTIFACT_BY_VIEW["bass-fit"].artifact


def _rung_of(fit: Mapping[str, Any], target_id: str) -> tuple[Mapping[str, Any], float]:
    """One rung of a fitted family, and the plant corner it moves down from."""
    plant = fit.get("effective_plant")
    f0_hz = plant.get("f0_hz") if isinstance(plant, Mapping) else None
    rungs = fit.get("rungs")
    found = next(
        (
            rung for rung in rungs or ()
            if isinstance(rung, Mapping)
            and isinstance(rung.get("target"), Mapping)
            and rung["target"].get("target_id") == target_id
        ),
        None,
    ) if isinstance(rungs, list) else None
    if found is None or not isinstance(f0_hz, (int, float)):
        raise LookupError("no such rung")
    return found["target"], float(f0_hz)


def _ladder_takes(
    positions_dir: Path, *, target_id: str, candidate_id: str,
) -> list[Mapping[str, Any]]:
    """Every banked take that played THIS rung, in banked order.

    ``positions_dir`` is the route ``record_store`` files a position record
    under. A take is selected on what it PLAYED — the scope, the rung and
    (when the caller names one) the candidate — never on its file name.
    """
    takes: list[Mapping[str, Any]] = []
    for path in sorted(positions_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("graph_scope") != GRAPH_SCOPE_BASS_CANDIDATE:
            continue
        if record.get("bass_target_id") != target_id:
            continue
        if candidate_id and record.get("candidate_id") != candidate_id:
            continue
        # The store banks a take only against a PROVEN fader, so a record
        # carrying no number there is not one of this engine's.
        if isinstance(record.get("level_db"), (int, float)):
            takes.append(record)
    return takes


def _programs(artifact_dir: Path) -> dict[str, ExcitationProgram]:
    """The schedules this round banked beside its rendered stimuli, by id."""
    banked: dict[str, ExcitationProgram] = {}
    for path in sorted(artifact_dir.glob("*_program.json")):
        try:
            program = ExcitationProgram.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError, KeyError):
            continue
        banked.setdefault(program.program_id, program)
    return banked


def _swept_band_hz(program: ExcitationProgram) -> tuple[float, float]:
    """The band this program's sweeps actually excite, from the schedule."""
    swept = [
        (float(segment.f1_hz), float(segment.f2_hz))
        for segment in program.stimulus_segments()
        if segment.kind == KIND_SUMMED_SWEEP
        and segment.f1_hz is not None and segment.f2_hz is not None
    ]
    if not swept:
        return (0.0, 0.0)
    return min(lo for lo, _ in swept), max(hi for _, hi in swept)


def _declared_program_id(sidecar: Path) -> str:
    """The schedule a placed capture declares it was played through."""
    try:
        doc = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return ""
    stimulus = (doc.get("provenance") or {}).get("stimulus") if isinstance(doc, Mapping) else None
    declared = stimulus.get("program_id") if isinstance(stimulus, Mapping) else None
    return declared if isinstance(declared, str) else ""


def _banked_calibration_id(records: Sequence[Mapping[str, Any]]) -> str:
    """The mic identity this round's takes were captured against, or ``""``."""
    for record in records:
        setup = record.get("capture_setup")
        block = setup.get("calibration") if isinstance(setup, Mapping) else None
        named = block.get("calibration_id") if isinstance(block, Mapping) else None
        if isinstance(named, str) and named:
            return named
    return ""


def _band_metrics(
    readings: Sequence[Any], band_hz: tuple[float, float], orders: Sequence[int],
) -> tuple[float | None, float | None, tuple[int, ...], tuple[int, ...]]:
    """``(fundamental_db, thd_ratio, clean_orders, floor_limited_orders)``.

    Pooled across the capture's own sweeps by grid index, the rule
    ``harmonic_evidence._role_block`` pools by, and reduced over the rung's
    extension band alone: what the rung does above its own transform is not
    what the ladder is bounding.
    """
    first = readings[0]
    if not all(np.array_equal(r.freqs_hz, first.freqs_hz) for r in readings):
        return None, None, (), ()
    band = (first.freqs_hz >= band_hz[0]) & (first.freqs_hz <= band_hz[1])
    if not band.any():
        return None, None, (), ()
    fundamental = np.median(
        np.stack([r.fundamental_db for r in readings]), axis=0
    )[band]
    fundamental = fundamental[np.isfinite(fundamental)]

    amplitudes: list[np.ndarray] = []
    clean: list[int] = []
    limited: list[int] = []
    for order in orders:
        relative = np.median(
            np.stack([r.relative_db[order] for r in readings]), axis=0
        )[band]
        # Majority vote across the capture's sweeps, so one sweep's noise
        # spike cannot flag a point the others read as clear.
        on_floor = np.stack(
            [r.floor_limited(order) for r in readings]
        ).sum(axis=0)[band] > len(readings) / 2
        usable = ~on_floor & np.isfinite(relative)
        if not usable.any():
            limited.append(int(order))
            continue
        clean.append(int(order))
        amplitudes.append(np.where(usable, 10.0 ** (relative / 20.0), 0.0))
    if fundamental.size == 0 or not clean:
        return None, None, tuple(clean), tuple(limited)
    rss = np.sqrt(np.sum(np.stack(amplitudes) ** 2, axis=0))
    thd = float(np.median(rss[rss > 0.0])) if np.any(rss > 0.0) else 0.0
    return (
        float(np.median(fundamental)),
        thd if math.isfinite(thd) else None,
        tuple(clean),
        tuple(limited),
    )


def _step(
    record: Mapping[str, Any],
    program: ExcitationProgram | None,
    *,
    session_dir: Path,
    band_hz: tuple[float, float],
    calibration: Any,
) -> LadderStep:
    """One banked take as a ladder step, read or left unproven.

    A take that carries an incident is never read: it names the reason the
    ladder ends there, and reading a capture the engine already disowned would
    invent a step out of it.
    """
    take_id = str(record.get("take_id") or "")
    # An empty ladder is the ONE stimulus the program declares, read here at
    # the peak the composer gives it — the same reading the door's own seat-SPL
    # stop makes of a rung requested without --level-dbfs.
    stimulus = record.get("stimulus_dbfs")
    stimulus_dbfs = (
        float(stimulus) if isinstance(stimulus, (int, float))
        else BASE_STIMULUS_PEAK_DBFS
    )
    level_db = float(record["level_db"])
    incident = str(record.get("incident") or "")
    unread = LadderStep(
        take_id=take_id, stimulus_dbfs=stimulus_dbfs, level_db=level_db,
        fundamental_db=None, thd_ratio=None, clean_orders=(), incident=incident,
    )
    wav_path = str(record.get("wav_path") or "")
    if incident or not wav_path or program is None:
        return unread
    wav = session_dir / wav_path
    try:
        samples, rate = read_wav_mono(wav)
    except (OSError, ValueError, EOFError):
        return unread
    if int(rate) != int(program.sample_rate_hz):
        return unread
    try:
        readings = read_program_sweeps(
            program, samples, orders=HARMONIC_ORDERS, calibration=calibration,
            level_notes={"take_id": take_id, "phase": program.phase},
            # A rung plays the SUMMED sweep: the whole system is what the
            # ladder bounds, so the sum is what to read, not a driver solo.
            kinds=(KIND_SUMMED_SWEEP,),
        )
        if not readings:
            return unread
        fundamental_db, thd_ratio, clean, limited = _band_metrics(
            readings, band_hz, HARMONIC_ORDERS
        )
    except (ValueError, IndexError, KeyError):
        # A capture this round cannot be read through is an UNPROVEN step, and
        # the ladder ends there — never a refusal of the whole run, which would
        # discard the steps below it that did read.
        return unread
    return LadderStep(
        take_id=take_id, stimulus_dbfs=stimulus_dbfs, level_db=level_db,
        fundamental_db=fundamental_db, thd_ratio=thd_ratio,
        clean_orders=clean, floor_limited_orders=limited, incident="",
    )


def _cmd_bass_ladder(args: argparse.Namespace) -> int:
    round_dir = Path(args.round_dir)
    inputs = round_inputs(round_dir)
    target_id = str(args.target_id).strip()
    fit_path = (
        Path(args.bass_fit) if args.bass_fit
        else default_out(inputs, round_dir, BASS_FIT_FILENAME)
    )
    try:
        document = json.loads(fit_path.read_bytes())
        fit = document["bass_fit"]
        margin = MARGINS[str(fit["margin"])]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return refused_by_name(
            BassExtensionRefusal.FIELD_MALFORMED,
            {"bass_fit": str(fit_path), "error": str(exc)},
            code=EXIT_UNREADABLE,
        )
    try:
        target, f0_hz = _rung_of(fit, target_id)
        fp_hz = float(target["fp_hz"])
        out_name = ladder_document_name(target_id)
    except (LookupError, TypeError, ValueError):
        return refused_by_name(BassExtensionRefusal.TARGET_INVALID, {
            "bass_fit": str(fit_path), "target_id": target_id,
            "problem": "the fitted family carries no such nameable rung",
        })
    if not (0.0 < fp_hz < f0_hz):
        # The band the ladder grades is the one the transform moves the corner
        # across; a rung that moves it nowhere spends no boost and needs no
        # ladder to be adopted.
        return refused_by_name(BassExtensionRefusal.TARGET_INVALID, {
            "target_id": target_id, "fp_hz": fp_hz, "plant_f0_hz": f0_hz,
            "problem": "the rung extends nothing, so no band carries its boost",
        })

    artifact_dir, why = round_artifact_dir(inputs.session_dir)
    if artifact_dir is None:
        return refused_by_name(
            BassExtensionRefusal.LADDER_INCOMPLETE,
            {"round_dir": str(round_dir), "problem": why},
            code=EXIT_UNREADABLE,
        )
    records = _ladder_takes(
        artifact_dir / "positions", target_id=target_id,
        candidate_id=str(args.candidate_id or "").strip(),
    )
    if not records:
        return refused_by_name(BassExtensionRefusal.LADDER_INCOMPLETE, {
            "round_dir": str(round_dir), "target_id": target_id,
            "candidate_id": args.candidate_id or None,
            "problem": f"no {GRAPH_SCOPE_BASS_CANDIDATE} take banked this rung",
        })

    banked = _programs(artifact_dir)
    played = [
        banked.get(_declared_program_id(
            (inputs.session_dir / str(record.get("wav_path") or "")).with_suffix(".json")
        ))
        for record in records
    ]
    # The band the ladder can grade is where the rung's boost and the ladder's
    # own stimulus MEET. They need not: the summed sweep a bass rung plays
    # today starts at the crossover's own low bound (program.VERIFY_F_LO_HZ, or
    # fc/2), which on a two-way sits well above the corner an extension rung
    # moves. Nothing below that bound was excited, so nothing below it was
    # measured, and a "fail" published over an unexcited band would read as a
    # driver that failed.
    stimulus_band = [
        _swept_band_hz(program) for program in played if program is not None
    ]
    band_hz = (
        max(fp_hz, min(band[0] for band in stimulus_band)),
        min(f0_hz, max(band[1] for band in stimulus_band)),
    ) if stimulus_band else (fp_hz, f0_hz)
    if band_hz[0] >= band_hz[1]:
        return refused_by_name(BassExtensionRefusal.LADDER_INCOMPLETE, {
            "target_id": target_id,
            "rung_band_hz": [fp_hz, f0_hz],
            "stimulus_band_hz": [
                min((band[0] for band in stimulus_band), default=None),
                max((band[1] for band in stimulus_band), default=None),
            ],
            "problem": "no step's stimulus reaches the band this rung boosts",
        })

    # The mic the SESSION recorded through, so the file is read under its own
    # vendor's sign convention rather than the parser's bare default.
    calibration, calibration_note = calibration_from_text(
        Path(args.calibration).read_text() if args.calibration else None,
        _banked_calibration_id(records),
    )
    steps = [
        _step(
            record, program, session_dir=inputs.session_dir,
            band_hz=band_hz, calibration=calibration,
        )
        for record, program in zip(records, played)
    ]
    evidence = grade_ladder(steps, target_id=target_id, margin=margin, basis={
        "candidate_fingerprint": str(records[0].get("candidate_id") or ""),
        "round_id": artifact_dir.name,
        "bass_fit": str(fit_path),
        "band_hz": list(band_hz),
        "rung_band_hz": [fp_hz, f0_hz],
        "orders": list(HARMONIC_ORDERS),
        "calibration": calibration_note,
    })
    written = _write(
        evidence, args.out, default_out(inputs, round_dir, out_name),
        make_parents=True,
    )
    rows = evidence["steps"]
    return answer(
        args.command, out=written, target_id=target_id,
        verdict=evidence["verdict"], max_level_db=evidence.get("max_level_db"),
        margin=margin.name, n_steps=len(rows), n_takes=len(records),
        band_hz=list(band_hz),
        ended_by=next(
            (row["reason"] for row in rows if row.get("reason")), None,
        ),
        line=(
            f"bass-ladder {target_id}: {evidence['verdict']} over "
            f"{len(rows)}/{len(records)} step(s)"
            + (
                f", max_level_db={evidence['max_level_db']:+.1f}"
                if "max_level_db" in evidence else ""
            )
            + (f" -> {written}" if written else "")
        ),
    )


def add_parser(sub: argparse._SubParsersAction) -> None:
    ladder = sub.add_parser(
        "bass-ladder",
        help="grade this round's stepped bass-rung takes and publish the level the rung proved",
    )
    ladder.add_argument("round_dir", metavar=_ROUND_DIR_METAVAR, help=_ROUND_DIR_HELP)
    ladder.add_argument(
        "--target-id", required=True, metavar="T",
        help="which rung of the fitted bass family this ladder measured",
    )
    ladder.add_argument(
        "--candidate-id", default="", metavar="FINGERPRINT",
        help="grade only the takes that played this candidate's rung "
             "(default: every bass_candidate take banked for the target)",
    )
    ladder.add_argument(
        "--bass-fit", default=None, metavar="PATH",
        help=f"the fitted family (default: <round-dir>/{BASS_FIT_FILENAME})",
    )
    ladder.add_argument(
        "--calibration", default=None, metavar="PATH",
        help="microphone calibration file; without one every harmonic-to-"
             "fundamental ratio carries the mic's own response across an octave",
    )
    ladder.add_argument("--out", default=None, help="write the result here (- for stdout)")
    ladder.set_defaults(func=_cmd_bass_ladder, parser=ladder)
