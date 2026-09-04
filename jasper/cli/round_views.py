# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator entry point for the round-grading comparison views (issue #2769).

One console script, twelve subcommands — each a thin argparse wrapper over
:mod:`jasper.active_speaker.crossover_v2.round_views`, which owns every
number this tool prints. A round directory is EITHER a banked round tree or
a live session bundle still on the speaker
(``/var/lib/jasper/active_speaker/sessions/<id>``) — the shape is
:func:`~jasper.active_speaker.crossover_v2.round_inputs.round_inputs`'
answer, so an operator can grade the round they just ran without banking it
first (#3498). This module calls the product view and writes the result as
JSON — into a BANKED round's own directory by default, so the artifact travels
with the evidence it was computed from, and beside the CALLER for a live
session bundle, which belongs to the daemon (:func:`_default_out`). Per the
same "who prints the sentence" boundary :mod:`jasper.cli.crossover_prescriber`
already keeps for its own JSON output.

Subcommands:

* ``entry <round-dir>`` — grade the state the round ENTERED on, from the
  entry-baseline take it banked, through the shipped flat-spec evaluator.
  The one table nothing else prints: a fresh box's declarations-derived
  config is the first round's entry state, and until this door it could only
  be graded by hand. Writes ``entry_state_grade.json``. A round that banked
  no gradeable take says so with a named reason and still exits ``0`` — that
  is an answer, not an unreadable round.
* ``frozen <baseline-dir> <target-dir>`` — grade ``target`` shipped AND
  frozen to ``baseline``'s per-position reference levels. Writes
  ``frozen_reference.json`` for the TARGET round.
* ``per-seat <round-dir>`` — every banked position plus the VERIFY pose
  (when its dump-ring capture is banked), normalised onto one comparable
  basis. Writes ``per_seat.json``.
* ``repeat <round-dir> [<round-dir> ...]`` — session-to-session spread of
  the pooled honest figures (the stop criterion). Writes
  ``repeatability.json`` for the FIRST round.
* ``repeat-floor <round-dir> <round-dir> [...] (--install | --out PATH)`` —
  the same spread, banked as the durable record the evidence packet's
  ``in_capture_repeat_floor`` reads and derives the stopping plateau/benefit
  margin from. The rounds must be touched-nothing fixed-pose repeats.
  ``--install`` publishes it at the on-speaker path, from which
  ``bank-crossover-round.sh`` pulls it beside every later round as
  ``repeat-floor.json``; ``--out`` writes the same record somewhere else
  (beside a banked round, say). At least one is required: running on a laptop
  over banked directories, the speaker's path is a destination to ask for,
  never one to assume.
* ``agreement <round-dir>`` — per-position sign/magnitude testimony for
  every feature in the trusted sweep, built from the same per-seat curves
  ``per-seat`` computes. Writes ``agreement.json``.
* ``spec-sweep <round-dir>`` — the round's own graded spec verdict, with
  "is this band's worst bin the room or the speaker" answered AT that bin:
  the gate ladder's ``sigma_growth_ratio`` (growth with window length is what
  says room), the window's null-model-corrected contribution in dB, how many
  rungs were resolution-valid, and the frame all three are stated in.
  Disclosure only — no grade moves, and every field is a re-reading of the
  spec report the round already banked. Writes
  ``spec_gate_sensitivity.json``. Reach for ``jasper-gate-sweep --at-hz``
  only for a bin this verdict did NOT flag.
* ``frequency <source-a> [<source-b>]`` — the renderer-neutral frequency view
  shared with the JTS web page. A source may be a banked round, a session
  bundle, or a JSON measurement/analysis document.
* ``co-metrics <round-dir>`` — NBD + SM (Olive 2004, ADR-0202) on the
  on-axis curve and the pooled horizontal window. Co-metrics only: they
  inform, they never gate or veto — ``entry``/``frozen``/``per-seat`` etc.
  above stay the acceptance path. Writes ``audibility_co_metrics.json``.
* ``directivity <round-dir>`` — every cloud seat as its departure from the
  on-axis reference, split per graded band into a level offset (that band's
  directivity index, which a trim can remove) and the shape residual it
  cannot. Observed only: no grade moves. Writes ``directivity.json``. A round
  banked before the seat bearings were written still answers, with
  ``angles_recorded`` false — read it as role-labelled, never as 0°.
* ``cloud-binding <round-dir>`` — re-fit this round's linearization with the
  cloud's null evidence CUT, and say whether that evidence actually BOUND the
  fit: by how much, and in which octave bands. It refuses rather than
  reporting when the all-inputs-wired refit does not reproduce the fit the
  round banked, and names a round whose MEASURE take predates the per-
  occurrence fit inputs as unevaluable instead of refitting it into an empty
  answer. Observed only: no grade moves, and it says nothing about whether
  the wired answer was RIGHT — nothing banked is ground truth for that.
  Writes ``cloud_binding.json``.
* ``inventory <round-dir>`` — which of the named artifacts above this round
  already has, and the subcommand that produces each one it is missing, so
  "was this round ever analysed for X" is read rather than re-run. The round
  is resolved, never graded, and each ``produced_by`` places it in the slot
  that writes the artifact beside it. Presence is read at the path each view
  writes with no ``--out``, so a LIVE session bundle's artifacts are looked
  for beside the caller, not in the bundle (:func:`_default_out`). Writes
  ``inventory.json``.

Every subcommand accepts ``--out PATH`` to write somewhere else instead
(``-`` for stdout, except ``repeat-floor``, whose record is published by
its owning module and so requires a real path), and prints a
one-line human summary to stderr either way. On failure the exit code names
the STAGE that failed and it publishes the shared failure record;
``--help``'s EXIT CODES block and docs/tuning-operator-runbook.md's "Exit
codes" state the numbers and the record's shape, so neither is repeated here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence, TypeVar

from jasper.active_speaker.crossover_v2.round_views import (
    AGREEMENT_TESTIFY_MIN,
    BankedRound,
    RoundViewsError,
    agreement_table,
    audibility_co_metrics,
    cloud_binding_view,
    default_agreement_lo_hz,
    directivity_view,
    entry_state_grade,
    frozen_reference_grade,
    load_banked_round,
    per_seat_curves,
    repeat_floor_provenance,
    repeatability_spread,
    spec_with_gate_sensitivity,
    verify_pose_curve,
)
from jasper.active_speaker.frequency_view import build_frequency_view
from jasper.active_speaker.measurement_archive import (
    ArchivedMeasurement,
    load_measurement,
)
from jasper.active_speaker.measurement_document import frequency_run_from_documents
from jasper.active_speaker.repeat_floor import (
    DEFAULT_STATE_PATH as _REPEAT_FLOOR_DEFAULT_PATH,
    SHIPPED_POOL_METRIC,
    derive_repeat_floor,
    stopping_thresholds,
    write_repeat_floor,
)
from jasper.active_speaker.crossover_v2.frequency_view import frequency_run
from jasper.active_speaker.crossover_v2.round_inputs import RoundInputs, round_inputs
from jasper.cli._refusal import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    EXIT_WRITE_FAILED,
    failed,
)
from jasper.cli._report import write_report
from jasper.cli.gate_sweep import add_rungs_ms_argument

_T = TypeVar("_T")

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "advisory"

#: What every round-directory positional takes, said once. Both shapes, named
#: in the order an operator meets them: the live one is what a round leaves on
#: the speaker, the banked one is what ``bank-crossover-round.sh`` made of it.
_ROUND_DIR_HELP = "a banked round directory, or a live session bundle"

PROG = "jasper-round-views"

#: The positionals a view's subcommand takes, with the inventoried round in
#: the SLOT that writes the artifact beside it: ``frozen`` grades — and writes
#: beside — the TARGET, ``repeat`` writes beside the FIRST directory, so their
#: second round sits on opposite sides. A hint carrying ``<other-round>`` is
#: one this inventory cannot fill, and running it without that round is an
#: invocation argparse rejects.
TAKES_THIS_ROUND = "<this-round>"
TAKES_AFTER_ANOTHER = "<other-round> <this-round>"
TAKES_BEFORE_ANOTHER = "<this-round> <other-round>"


class ViewArtifact(NamedTuple):
    """One view's artifact, and what its subcommand takes to produce it."""

    artifact: str
    takes: str = TAKES_THIS_ROUND


#: The artifact each view writes beside the round, declared once: the
#: subcommands take their default output path from this table and
#: ``inventory`` names each missing artifact's producer from the same one, so
#: there is no second list to drift. ``repeat-floor`` is absent because it
#: publishes to ``--install`` or ``--out`` instead of beside the round.
ARTIFACT_BY_VIEW: dict[str, ViewArtifact] = {
    "entry": ViewArtifact("entry_state_grade.json"),
    "frozen": ViewArtifact("frozen_reference.json", TAKES_AFTER_ANOTHER),
    "per-seat": ViewArtifact("per_seat.json"),
    "repeat": ViewArtifact("repeatability.json", TAKES_BEFORE_ANOTHER),
    "agreement": ViewArtifact("agreement.json"),
    "co-metrics": ViewArtifact("audibility_co_metrics.json"),
    "directivity": ViewArtifact("directivity.json"),
    "cloud-binding": ViewArtifact("cloud_binding.json"),
    "spec-sweep": ViewArtifact("spec_gate_sensitivity.json"),
    "frequency": ViewArtifact("frequency_view.json"),
}

#: ``inventory``'s own report, named apart from the views it reports on.
INVENTORY_ARTIFACT = "inventory.json"

#: A round directory is operator-pulled evidence, not a validated
#: input — the documented failure shapes it can hand back are broader than
#: the product module's own typed :class:`RoundViewsError`. A malformed
#: evidence document can be missing a key (``KeyError``), hold the wrong type
#: at one (``TypeError``), or not parse at all (``ValueError``, which
#: ``json.JSONDecodeError`` subclasses); and any of the files this tool reads
#: — or the one it WRITES, where an operator can name an ``--out`` they may
#: not create — can simply not exist or not be permitted (``OSError``, which
#: ``PermissionError`` subclasses). The LOAD stage claims this whole tuple;
#: :func:`main` takes what no stage claimed, so no subcommand can grow a
#: traceback of its own.
#:
#: ``struct.error`` was here for one reader that no longer exists: a
#: header-truncated dump-ring WAV raised it out of ``scipy.io.wavfile.read``
#: while ``verify_pose_curve`` still deconvolved raw ring bytes. That view
#: reads the round's banked curve now, no code on this path opens a WAV, and
#: catching an exception nothing can raise is not how it is caught.
_ROUND_TOOL_ERRORS: tuple[type[Exception], ...] = (
    RoundViewsError, OSError, EOFError, ValueError, KeyError, TypeError,
)

#: The named ``reason`` each failing stage publishes. The bucket is the STAGE,
#: never the exception type — one ``RoundViewsError`` is raised both for a
#: round that could not be read and for a view that declined one, so a
#: type-based split answers the operator's "where do I go" wrong.
REASON_REFUSED = "round_views_refused"
REASON_UNREADABLE = "round_views_unreadable_round"
REASON_UNWRITABLE = "round_views_unwritable_out"

_REASON_BY_CODE = {
    EXIT_REFUSED: REASON_REFUSED,
    EXIT_UNREADABLE: REASON_UNREADABLE,
    EXIT_WRITE_FAILED: REASON_UNWRITABLE,
}


class _StageFailed(Exception):
    """A failure a stage claimed, carrying that stage's exit code."""

    def __init__(self, code: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.code = code


def _stage(
    code: int,
    errors: tuple[type[Exception], ...],
    fn: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    """Run one stage; what it raises from ``errors`` gets that stage's code."""

    try:
        return fn(*args, **kwargs)
    except errors as exc:
        raise _StageFailed(code, exc) from exc


def _load_round(round_dir: str | Path) -> BankedRound:
    """Read one round directory. A failure here is the ROUND, not the view."""

    return _stage(
        EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, load_banked_round, Path(round_dir)
    )


def _write(payload: Any, out: str | None, default_path: Path) -> Path | None:
    """Publish one view. ``OSError`` only, and that is the whole rule.

    A ``ValueError`` out of the strict writer is a payload this run should not
    have built — co-metrics over partial bearing coverage yields ``NaN``, which
    ``allow_nan=False`` rejects — and sending that operator to fix the
    filesystem sends them to the wrong place. It falls to :func:`main`.
    """

    return _stage(EXIT_WRITE_FAILED, (OSError,), write_report, payload, out, default_path)


def _default_out(inputs: RoundInputs, round_dir: Path, name: str) -> Path:
    """Where a view lands when the operator named no ``--out``.

    A BANKED round tree is the operator's own directory, so its views stay
    beside the evidence they were computed from. A LIVE session bundle is the
    daemon's (``/var/lib/jasper/active_speaker/sessions/<id>``, written by the
    web host as its own user): defaulting inside it made the ordinary
    invocation — grade the round I just ran — raise ``PermissionError`` for
    the operator this door was added for (#3498). So a live round's view lands
    beside the caller instead, named by the session it came from so two
    sessions graded in one directory do not overwrite each other.
    """
    if inputs.banked:
        return round_dir / name
    return Path.cwd() / f"{inputs.session_dir.name}-{name}"


def _view_out(args: argparse.Namespace, round_: BankedRound) -> Path:
    """This subcommand's own artifact path, from :data:`ARTIFACT_BY_VIEW`."""
    return _default_out(
        round_.inputs, round_.round_dir, ARTIFACT_BY_VIEW[args.command].artifact
    )


def _frequency_default_out(source: Path) -> Path:
    """:func:`_cmd_frequency`'s own default: it takes sources the round
    resolver does not (a JSON document, a bundle that banked no round), so it
    reads the live shape directly — under the same rule as
    :func:`_default_out`, never inside a daemon-owned session bundle.
    """
    name = ARTIFACT_BY_VIEW["frequency"].artifact
    if not source.is_dir():
        return source.parent / name
    if (source / "info.json").is_file():
        return Path.cwd() / f"{source.name}-{name}"
    return source / name


def _cmd_entry(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    # A packet missing `entry_baseline` is a corrupt packet, which this grade's
    # own docstring puts in the unreadable arm — not a view declining a round.
    grade = _stage(EXIT_UNREADABLE, (KeyError, TypeError), entry_state_grade, banked)
    written = _write(grade.to_dict(), args.out, _view_out(args, banked))
    report = grade.report
    # ``report is None`` IS ``not available`` — the two move together on
    # ``EntryStateGrade`` — and testing the report narrows it for the summary
    # below without a second, unfalsifiable assertion that they agree.
    if report is None:
        # Exit 0, not 1: "this round banked no gradeable entry baseline" is an
        # ANSWER — the one this door exists to give instead of an operator's
        # hand-rolled evaluation — not a failure to read the round, which is
        # what the unreadable exit is for.
        print(f"entry-state: NOT GRADED — {grade.reason}", file=sys.stderr)
        return EXIT_OK
    # `is False` / `is None`, never a bare truthiness test, for exactly the
    # reason `_cmd_agreement` states below: an UNEVALUABLE band (no
    # non-excluded bin survived) is not a failing one, and collapsing them
    # would report a band nobody could measure as one that measured badly.
    n_failed = sum(1 for band in report.bands if band.passed is False)
    n_unevaluable = sum(1 for band in report.bands if band.passed is None)
    ordinal = "?" if grade.round_ordinal is None else grade.round_ordinal
    epoch = "?" if grade.round_ordinal_epoch is None else grade.round_ordinal_epoch
    print(
        f"entry-state: {len(report.bands)} band(s), {n_failed} outside target, "
        f"{n_unevaluable} unevaluable; "
        f"overall_passed={report.overall_passed} "
        f"round={ordinal} epoch={epoch} "
        f"graph={grade.graph_fingerprint or '(not recorded)'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_frozen(args: argparse.Namespace) -> int:
    baseline = _load_round(args.baseline_dir)
    target = _load_round(args.target_dir)
    result = frozen_reference_grade(baseline, target)
    written = _write(result.to_dict(), args.out, _view_out(args, target))
    print(
        f"frozen-reference: shipped={result.shipped} frozen={result.frozen}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_per_seat(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "curve_grid_hz": banked.curve_grid_hz.tolist(),
        "norm_band_hz": [args.norm_lo, args.norm_hi],
        "verify_pose": {
            "included": verify.curve is not None,
            "reason": verify.reason,
        },
        "seats": [
            {
                "position_id": seat.position_id,
                "role": seat.role,
                "normalized_db": seat.normalized_db.tolist(),
            }
            for seat in seats
        ],
    }
    written = _write(payload, args.out, _view_out(args, banked))
    print(
        f"per-seat: {len(seats)} seat(s) ({', '.join(s.position_id for s in seats)}); "
        f"verify pose {'included' if verify.curve is not None else f'ABSENT ({verify.reason})'}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _load_rounds(round_dirs: Sequence[str]) -> list[tuple[str, BankedRound]]:
    """The (label, round) pairs both repeat verbs grade, labelled by the
    directory the operator named."""
    return [(round_dir, _load_round(round_dir)) for round_dir in round_dirs]


def _cmd_repeat(args: argparse.Namespace) -> int:
    rounds = _load_rounds(args.round_dirs)
    result = repeatability_spread(rounds)
    written = _write(result.to_dict(), args.out, _view_out(args, rounds[0][1]))
    shipped = next((m for m in result.metrics if m.name == SHIPPED_POOL_METRIC), None)
    spread = shipped.spread() if shipped else None
    print(
        f"repeatability: {len(result.round_labels)} round(s); "
        f"{SHIPPED_POOL_METRIC} spread={spread}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _record_path(value: str) -> Path:
    """``--out`` for a verb that publishes a file: ``-`` is not a path."""
    if value == "-":
        raise argparse.ArgumentTypeError("repeat-floor publishes a file; '-' is not a path")
    return Path(value)


#: ``--install``'s destination is a 0770 StateDirectory owned by the daemon's
#: user, so the login account cannot write it unaided.
_INSTALL_SUDO_HINT = f" — run it with sudo -n /opt/jasper/.venv/bin/{PROG} ..."


def _cmd_repeat_floor(args: argparse.Namespace) -> int:
    destinations: list[tuple[Path, str]] = []
    if args.out is not None:
        destinations.append((args.out, ""))
    if args.install:
        destinations.append((_REPEAT_FLOOR_DEFAULT_PATH, _INSTALL_SUDO_HINT))
    if not destinations:
        args.parser.error("nowhere to publish: pass --install, --out PATH, or both")
    rounds = _load_rounds(args.round_dirs)
    payload = derive_repeat_floor(
        repeatability_spread(rounds),
        rounds=[repeat_floor_provenance(round_dir, banked) for round_dir, banked in rounds],
    )
    for path, hint in destinations:
        try:
            write_repeat_floor(payload, state_path=path)
        except OSError as exc:  # an unwritable destination is the WRITE exit
            detail = f"{path}: {exc}"
            raise _StageFailed(
                EXIT_WRITE_FAILED,
                OSError(detail + hint if isinstance(exc, PermissionError) else detail),
            ) from exc
    thresholds = stopping_thresholds(payload)
    aggregate = payload["metrics"][SHIPPED_POOL_METRIC]
    print(
        f"repeat-floor: {payload['n_repeats']} round(s); {SHIPPED_POOL_METRIC} "
        f"sd={aggregate['sd_db']:.4g} dB; thresholds={thresholds} -> "
        + ", ".join(str(path) for path, _ in destinations),
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_agreement(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    lo_hz = args.lo if args.lo is not None else default_agreement_lo_hz(banked)
    verify = verify_pose_curve(banked)
    seats = per_seat_curves(
        banked, verify.curve, norm_band_hz=(args.norm_lo, args.norm_hi)
    )
    features = agreement_table(
        seats,
        banked.curve_grid_hz,
        lo_hz=lo_hz,
        hi_hz=args.hi,
        feature_db=args.feature_db,
        testify_db=args.testify_db,
    )
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "seats": [seat.position_id for seat in seats],
        "swept_band_hz": [lo_hz, args.hi],
        "feature_db": args.feature_db,
        "testify_db": args.testify_db,
        "features": [feature.to_dict() for feature in features],
    }
    written = _write(payload, args.out, _view_out(args, banked))
    # `common_mode is True`, never a bare truthiness test: `None` (not
    # evaluable, below AGREEMENT_TESTIFY_MIN seats) must not be silently
    # counted alongside `False` (evaluated and failed the bar).
    n_common = sum(1 for f in features if f.common_mode is True)
    n_not_evaluable = sum(1 for f in features if f.common_mode is None)
    print(
        f"agreement: {len(features)} feature(s), {n_common} common-mode, "
        f"{n_not_evaluable} not-evaluable (< {AGREEMENT_TESTIFY_MIN} seats)"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_co_metrics(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    result = audibility_co_metrics(banked)
    written = _write(result.to_dict(), args.out, _view_out(args, banked))
    on_axis = (
        f"NBD={result.on_axis.nbd_db:.3f} dB SM={result.on_axis.sm_r2:.3f}"
        if result.on_axis is not None else f"NOT AVAILABLE ({result.on_axis_reason})"
    )
    pooled = (
        f"NBD={result.pooled_window.nbd_db:.3f} dB SM={result.pooled_window.sm_r2:.3f} "
        f"({len(result.pooled_window_bearings_deg)} bearing(s))"
        if result.pooled_window is not None
        else f"NOT AVAILABLE ({result.pooled_window_reason})"
    )
    print(
        f"co-metrics [informational only, never a grade input]: "
        f"on-axis {on_axis}; pooled-window {pooled}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_directivity(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    table = directivity_view(banked)
    payload = {
        "round_dir": str(banked.round_dir),
        "banked": banked.inputs.banked,
        "directivity": table.to_dict(),
    }
    written = _write(payload, args.out, _view_out(args, banked))
    # Both clauses only inside the evaluable arm: an absent reference forces
    # `angles_recorded` false whatever the round banked, so reading it out
    # there would tell an operator their bearings are missing when they are not.
    if table.evaluable:
        n_not_evaluable = sum(1 for row in table.rows if not row.evaluable)
        summary = (
            f"{len(table.rows)} seat(s), {n_not_evaluable} not-evaluable, against "
            f"{len(table.reference_position_ids)} {table.reference_role} seat(s); "
            + (
                "angles recorded" if table.angles_recorded
                else "angles NOT recorded (role-labelled only)"
            )
        )
    else:
        summary = f"NOT AVAILABLE ({table.not_evaluated_reason})"
    print(
        f"directivity [observed only, no grade moves]: {summary}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_cloud_binding(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    view = cloud_binding_view(banked)
    written = _write(view.to_dict(), args.out, _view_out(args, banked))
    if not view.evaluable:
        summary = f"NOT EVALUATED ({view.not_evaluated_reason})"
    else:
        moved = [
            f"{role.role} {role.max_delta_db:.2f} dB"
            for role in view.roles if role.bound
        ]
        summary = (
            f"BOUND: {', '.join(moved)}" if moved else "NOT BOUND: no branch moved"
        ) + f"; refit vs banked {view.refit_vs_banked_db:.3f} dB"
    print(
        f"cloud-binding [observed only, no grade moves]: {summary}"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _band_sweep_line(band: Any) -> str:
    """One band's gate read as the operator reads it: which band, then whether
    that band's own worst bin is the room or the speaker.

    ``gate_window_verdict`` is ``None`` only when the ladder never ran on this
    band; a ladder that ran and still could not call it stamps
    ``"unresolved"`` rather than nothing, and that is told apart from "never
    swept" here too.
    """
    label = f"{band.f_lo_hz:g}-{band.f_hi_hz:g} Hz"
    verdict = band.gate_window_verdict
    if verdict is None:
        return f"{label} NOT SWEPT ({band.gate_sensitivity_note})"
    if band.sigma_growth_ratio is None:
        return f"{label} {verdict.upper()} ({band.gate_sensitivity_note})"
    return (
        f"{label} @{band.max_deviation_hz:.1f} Hz {verdict.upper()} "
        f"sigma x{band.sigma_growth_ratio:.2f} over {band.n_valid_rungs} rung(s), "
        f"window {band.gate_sensitivity_db:+.2f} dB"
    )


def _cmd_spec_sweep(args: argparse.Namespace) -> int:
    banked = _load_round(args.round_dir)
    report = spec_with_gate_sensitivity(banked, rungs_ms=args.rungs_ms)
    payload = {"round_dir": str(banked.round_dir), "spec": report.to_dict()}
    written = _write(payload, args.out, _view_out(args, banked))
    print(
        "spec-sweep [disclosure only, no grade moves]: "
        + "; ".join(_band_sweep_line(band) for band in report.bands)
        + (f" -> {written}" if written else ""),
        file=sys.stderr,
    )
    return EXIT_OK


def _frequency_source(path: Path):
    """One round, bundle, or JSON document as a neutral frequency run."""

    if path.is_file():
        document = json.loads(path.read_text())
        if not isinstance(document, dict):
            raise ValueError(f"{path}: expected one JSON object")
        run = frequency_run_from_documents(
            run_id=path.stem, documents=(document,),
        )
    elif (path / "info.json").is_file():
        info = json.loads((path / "info.json").read_text())
        if not isinstance(info, dict):
            raise ValueError(f"{path / 'info.json'}: expected one JSON object")
        run = load_measurement(ArchivedMeasurement(
            id=str(info.get("session_id") or path.name),
            bundle_dir=path,
            started_at=info.get("started_at"),
            state=str(info.get("state") or "") or None,
        ))
    else:
        run = frequency_run(_load_round(path).packet)
    if not run.series:
        raise ValueError(f"{path}: no usable frequency-response curves")
    return run


def _cmd_frequency(args: argparse.Namespace) -> int:
    source_a = Path(args.source_a)
    # Resolving a source IS this verb's load stage, "that document holds no
    # curves" included: the fix is to name a different source.
    run_a = _stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, source_a)
    run_b = (
        _stage(
            EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, _frequency_source, Path(args.source_b)
        )
        if args.source_b
        else None
    )
    payload = build_frequency_view(run_a, run_b)
    written = _write(payload, args.out, _frequency_default_out(source_a))
    print(
        f"frequency: {len(payload['runs'])} run(s)"
        f"{f' -> {written}' if written else ''}",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_inventory(args: argparse.Namespace) -> int:
    # The round is RESOLVED, never graded: which artifacts sit beside a round
    # is a directory question, and building the evidence packet to answer it
    # would make the cheapest verb here cost the most (415 MB target, ADR-0226).
    round_dir = Path(args.round_dir)
    inputs = _stage(EXIT_UNREADABLE, _ROUND_TOOL_ERRORS, round_inputs, round_dir)
    artifacts: list[dict[str, Any]] = []
    for view, spec in ARTIFACT_BY_VIEW.items():
        path = _default_out(inputs, round_dir, spec.artifact)
        artifacts.append({
            "artifact": spec.artifact,
            "path": str(path),
            "present": path.is_file(),
            "produced_by": f"{PROG} {view} {spec.takes}",
            "producer_needs_another_round": spec.takes != TAKES_THIS_ROUND,
        })
    payload = {
        "round_dir": str(round_dir),
        "banked": inputs.banked,
        "artifacts": artifacts,
    }
    written = _write(
        payload, args.out, _default_out(inputs, round_dir, INVENTORY_ARTIFACT)
    )
    missing = [row["produced_by"] for row in artifacts if not row["present"]]
    print(
        f"inventory: {len(artifacts) - len(missing)}/{len(artifacts)} "
        f"artifact(s) present"
        + (f"; missing: {', '.join(missing)}" if missing else "")
        + (f" -> {written}" if written else ""),
        file=sys.stderr,
    )
    return EXIT_OK


def _add_norm_band_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--norm-lo", type=float, default=400.0, help="normalisation band low edge, Hz (default 400)")
    parser.add_argument("--norm-hi", type=float, default=8000.0, help="normalisation band high edge, Hz (default 8000)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "The round-grading comparison views: entry-state grading, "
            "frozen-reference grading, per-seat curves, session-to-session "
            "repeatability and the banked repeat floor, per-seat agreement, "
            "audibility co-metrics, measured per-angle directivity, whether "
            "the cloud's null evidence bound the linearization fit, the gate "
            "sweep read onto the spec verdict, the shared frequency view, and "
            "an inventory of which of those a round already carries — over "
            "banked rounds and live sessions."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - frozen/repeat/repeat-floor need MULTIPLE round directories\n"
            "    (a baseline plus a target, or two-or-more rounds);\n"
            "    entry/per-seat/agreement grade a single round\n"
            "  - to classify a feature's likely CAUSE -- that is\n"
            "    jasper-classify-features; this tool grades curves, not\n"
            "    defects\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-round-views frequency captures/.../session-1/round-3\n"
            "  jasper-round-views frozen captures/.../baseline captures/.../round-3\n"
            "  jasper-round-views spec-sweep captures/.../session-1/round-3\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- graded; printed, or written to --out. entry can\n"
            "     print \"entry-state: NOT GRADED — <reason>\" on stderr and\n"
            "     still exit 0 -- \"not gradeable yet\" is a valid verdict,\n"
            "     not a failure, so check the printed line rather than only\n"
            "     the code if that distinction matters to your caller\n"
            "  1  EXIT_REFUSED -- the round read, and the view itself\n"
            "     declined to grade it (a round with no cloud group, a\n"
            "     repeat floor from a single round)\n"
            "  2  EXIT_UNREADABLE -- the round or source could not be\n"
            "     read into a comparable view\n"
            "  3  EXIT_WRITE_FAILED -- graded, but the destination could\n"
            "     not be written\n"
            "  1-3 print \"<status> (<reason>): <detail>\" on stderr and the\n"
            "     same record as JSON on stdout"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    entry = sub.add_parser("entry", help="grade the state this round entered on, before it applied anything")
    entry.add_argument("round_dir", help=_ROUND_DIR_HELP)
    entry.add_argument("--out", default=None, help="write the result here (- for stdout)")
    entry.set_defaults(func=_cmd_entry)

    frozen = sub.add_parser("frozen", help="grade a round shipped and frozen to a baseline's reference")
    frozen.add_argument("baseline_dir", help=f"{_ROUND_DIR_HELP} to freeze the reference from")
    frozen.add_argument("target_dir", help=f"{_ROUND_DIR_HELP} to grade")
    frozen.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frozen.set_defaults(func=_cmd_frozen)

    per_seat = sub.add_parser("per-seat", help="every banked position plus the VERIFY pose, normalised")
    per_seat.add_argument("round_dir", help=_ROUND_DIR_HELP)
    _add_norm_band_args(per_seat)
    per_seat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    per_seat.set_defaults(func=_cmd_per_seat)

    repeat = sub.add_parser("repeat", help="session-to-session spread of the pooled honest figures")
    repeat.add_argument("round_dirs", nargs="+", help=f"two or more of: {_ROUND_DIR_HELP}")
    repeat.add_argument("--out", default=None, help="write the result here (- for stdout)")
    repeat.set_defaults(func=_cmd_repeat)

    repeat_floor = sub.add_parser(
        "repeat-floor", help="bank the repeat spread as the floor the evidence packet reads",
    )
    repeat_floor.add_argument(
        "round_dirs", nargs="+",
        help="two or more TOUCHED-NOTHING fixed-pose repeat round directories",
    )
    repeat_floor.add_argument(
        "--install", action="store_true",
        help=(
            f"publish it on the speaker at {_REPEAT_FLOOR_DEFAULT_PATH}, from "
            "which bank-crossover-round.sh pulls it beside every later round; "
            "needs sudo"
        ),
    )
    repeat_floor.add_argument(
        "--out", default=None, type=_record_path,
        help=(
            "also write the record here, e.g. beside a banked round as "
            "repeat-floor.json; at least one of --install/--out is required"
        ),
    )
    repeat_floor.set_defaults(func=_cmd_repeat_floor, parser=repeat_floor)

    agreement = sub.add_parser("agreement", help="per-seat sign/magnitude testimony for every feature")
    agreement.add_argument("round_dir", help=_ROUND_DIR_HELP)
    _add_norm_band_args(agreement)
    agreement.add_argument(
        "--lo", type=float, default=None,
        help="trusted sweep low edge, Hz (default: this round's own trusted_floor_hz)",
    )
    agreement.add_argument("--hi", type=float, default=16000.0, help="trusted sweep high edge, Hz")
    agreement.add_argument("--feature-db", type=float, default=0.4, help="minimum |pooled dB| to count as a feature")
    agreement.add_argument("--testify-db", type=float, default=0.4, help="minimum |seat dB| to testify or dissent")
    agreement.add_argument("--out", default=None, help="write the result here (- for stdout)")
    agreement.set_defaults(func=_cmd_agreement)

    co_metrics = sub.add_parser(
        "co-metrics", help="NBD + SM (Olive 2004) on the on-axis and pooled-window curves — informational only",
    )
    co_metrics.add_argument("round_dir", help=_ROUND_DIR_HELP)
    co_metrics.add_argument("--out", default=None, help="write the result here (- for stdout)")
    co_metrics.set_defaults(func=_cmd_co_metrics)

    directivity = sub.add_parser(
        "directivity",
        help="every cloud seat's departure from on-axis, split per band into level and shape — observed only",
    )
    directivity.add_argument("round_dir", help=_ROUND_DIR_HELP)
    directivity.add_argument("--out", default=None, help="write the result here (- for stdout)")
    directivity.set_defaults(func=_cmd_directivity)

    cloud_binding = sub.add_parser(
        "cloud-binding",
        help="re-fit with the cloud's null evidence cut, and say whether it bound the fit — observed only",
    )
    cloud_binding.add_argument("round_dir", help=_ROUND_DIR_HELP)
    cloud_binding.add_argument("--out", default=None, help="write the result here (- for stdout)")
    cloud_binding.set_defaults(func=_cmd_cloud_binding)

    spec_sweep = sub.add_parser(
        "spec-sweep",
        help="the round's spec verdict with room-or-speaker answered at each band's worst bin",
    )
    spec_sweep.add_argument("round_dir", help=_ROUND_DIR_HELP)
    add_rungs_ms_argument(spec_sweep)
    spec_sweep.add_argument("--out", default=None, help="write the result here (- for stdout)")
    spec_sweep.set_defaults(func=_cmd_spec_sweep)

    frequency = sub.add_parser("frequency", help="build the shared frequency-response view")
    frequency.add_argument(
        "source_a", help="banked round, session bundle, or JSON document for A",
    )
    frequency.add_argument(
        "source_b", nargs="?",
        help="optional banked round, session bundle, or JSON document for B",
    )
    frequency.add_argument("--out", default=None, help="write the result here (- for stdout)")
    frequency.set_defaults(func=_cmd_frequency)

    inventory = sub.add_parser(
        "inventory",
        help="which view artifacts this round has, and what produces each missing one",
    )
    inventory.add_argument("round_dir", help=_ROUND_DIR_HELP)
    inventory.add_argument("--out", default=None, help="write the result here (- for stdout)")
    inventory.set_defaults(func=_cmd_inventory)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except _StageFailed as staged:
        return failed(staged.code, _REASON_BY_CODE[staged.code], str(staged))
    except _ROUND_TOOL_ERRORS as exc:
        # What no stage claimed: the round READ, and the view then declined to
        # grade it. That is the refusal exit, not an unreadable one.
        return failed(EXIT_REFUSED, REASON_REFUSED, str(exc))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
