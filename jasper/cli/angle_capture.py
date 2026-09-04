# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State one angle walk, see exactly what it will run, and leave it for a session.

The operator's door onto :mod:`jasper.active_speaker.angle_capture` (#2732).
``plan`` prints the walk a request resolves to and writes nothing; ``stage``
runs the SAME resolution and banks the request where the next measurement
session takes it, once
(:mod:`jasper.active_speaker.angle_capture_spool`). Neither runs a capture,
opens a session, plays anything, or moves a microphone.

``--program`` names a row of :mod:`jasper.active_speaker.measurement_programs`,
which owns the geometry; ``--angles`` is the escape hatch for a bearing no
program names.

**Angles are parsed, never coerced.** A field is handed to
:class:`~jasper.active_speaker.angle_capture.AngleStop` as an ``int`` only when
written as a whole number; anything else passes through unchanged so the seam
refuses it. ``int("0.4")`` raises but ``int(0.4)`` is ``0``, and an angle
truncated to zero is an ON-AXIS capture nobody asked for. There is no second
validator here.

**Exit codes are part of the contract**: ``0`` accepted, ``2`` the request was
refused (fix the request), ``3`` an accepted request could not be banked (fix
the speaker's filesystem). A refusal prints its machine-readable reason on
stdout as JSON when asked, and the human sentence on stderr either way.

What a taken walk then does, and what it deliberately does not publish, is
``docs/testing-tooling.md`` ("Angle-walk door").
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Sequence

from ._logging import CLI_LOG_FORMAT

from jasper.active_speaker import measurement_programs
from jasper.active_speaker.angle_capture import (
    MOVER_HUMAN,
    MOVERS,
    REGIME_PER_DRIVER,
    REGIME_SUMMED,
    REGIMES,
    AngleCaptureRequest,
    AngleStop,
    announced_indexes,
    candidate_measure_axes,
    request_for_program,
    resolve_request,
    walk_price,
)
from jasper.active_speaker.candidate_bank import (
    CandidateBankRefusal,
    find_banked_candidate,
)
from jasper.active_speaker.angle_capture_spool import (
    AngleRequestRefused,
    angle_request_spool_path,
    stage_angle_request,
    withdraw_staged_angle_request,
)
from jasper.active_speaker.crossover_v2.contracts import (
    DRIVER_ROLES,
    POLARITIES,
    POLARITY_NORMAL,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2FlowError
from jasper.active_speaker.measurement_programs import MeasurementProgram
from jasper.active_speaker.seat_level_reference import (
    LevelUnresolved,
    ResolvedLevel,
    resolve_anchor_level,
)
from jasper.audio_measurement.measurement_geometry import (
    DEFAULT_PATH as DECLARED_GEOMETRY_PATH,
    DeclaredGeometry,
    load_declared_geometry,
)
from jasper.identity import CROSSOVER_PAGE_PATH, speaker_url

from ._refusal import EXIT_OK, EXIT_REFUSED, EXIT_WRITE_FAILED

#: The named program does not exist. Not a walk refusal -- no walk was stated,
#: so it does not borrow ``WALK_REFUSAL_REASONS``' vocabulary.
UNKNOWN_PROGRAM = "unknown_program"

#: The program ids this door offers, derived from the registry so a new row
#: reaches the registry's own refusal rather than an argparse "invalid choice".
#: ``spot`` is appended because it carries the caller's own bearing instead of
#: being a registry row.
PROGRAM_IDS = tuple(sorted({
    pid for pid, _size in measurement_programs.available_programs()
})) + ("spot",)
PROGRAM_SIZES = tuple(sorted({
    size for _pid, size in measurement_programs.available_programs()
}))

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). `stage`
#: writes the walk for the next session; `plan` and `withdraw` do not.
AUTHORITY_TIER = "mutating (`stage` writes; `plan`/`withdraw` do not)"


def _size_phrase() -> str:
    """``--size``'s choices with each tier's shape, read off the registry."""
    return ", ".join(
        f"{size} ({row.mic_move_count} spots, {row.capture_count} captures)"
        for size, row in (
            (size, measurement_programs.program("baseline", size))
            for size in PROGRAM_SIZES
        )
    )


def _angle_field(text: str) -> Any:
    """One ``--angles`` field, as the seam should see it.

    A field written as a whole number becomes an ``int``; EVERYTHING else is
    returned as the original string. That asymmetry is the point: the seam
    refuses a non-integral angle by design, and it can only do that if the
    non-integral value actually reaches it. Parsing ``7.5`` to a float here
    would be fine, but parsing it to an ``int`` would not -- and the two are one
    keystroke apart, so this function never converts anything it would have to
    round.
    """
    field = text.strip()
    try:
        return int(field)
    except ValueError:
        return field


def _parse_angles(raw: str) -> list[Any]:
    """``"0,7,-7"`` -> ``[0, 7, -7]``, preserving every field the seam must judge.

    Empty fields are dropped so a trailing comma is not an error; an entirely
    empty list is left empty, and :class:`AngleCaptureRequest` refuses it ("an
    angle capture request needs at least one stop") rather than this function
    inventing a second sentence for the same fact.
    """
    return [_angle_field(field) for field in raw.split(",") if field.strip()]


#: What each ``--regime`` value plays at each angle, in walk order. ``both`` is a
#: pair at one angle rather than two walks -- the property ``both_at``'s
#: docstring states, reproduced here as the data it is rather than as a branch.
#: The two single-regime rows are keyed off :data:`REGIMES` itself, so a regime
#: added to the seam is a ``KeyError`` here rather than a silently missing verb.
_REGIME_STOPS: dict[str, tuple[str, ...]] = {
    **{regime: (regime,) for regime in REGIMES},
    "both": (REGIME_PER_DRIVER, REGIME_SUMMED),
}


def _declared_on_the_box() -> DeclaredGeometry | None:
    """The box's standing declaration (``jasper-declare-geometry set``), if any.

    Shown so the operator sees what the round will bank: the evidence packet
    reads this same file when the session banks, so nothing is carried from
    here. An unreadable file reads as absent rather than refusing the walk --
    ``jasper-declare-geometry show`` is where a damaged declaration is
    diagnosed.
    """
    try:
        return load_declared_geometry(DECLARED_GEOMETRY_PATH)
    except (OSError, ValueError):
        return None


def _graph_flags(args: argparse.Namespace) -> dict[str, Any]:
    """The walk-level graph statement, which both request paths carry alike."""
    return {
        "mover": args.mover,
        "polarity": args.polarity,
        "inverted_role": args.inverted_role,
        "delayed_role": args.delayed_role,
        "delay_us": args.delay_us,
        "level_matched": args.level_matched,
    }


def _resolved_candidates(args: argparse.Namespace) -> tuple[str, ...]:
    """The banked candidates this walk cycles, judged before it is staged.

    Each fingerprint goes through the bank's own door, so one that names
    nothing refuses in the bank's vocabulary rather than a second one; what a
    candidate may VARY is the seam's
    (:func:`~jasper.active_speaker.angle_capture.candidate_measure_axes`),
    asked here so a walk no session could play is refused at staging instead of
    at the open.
    """
    fingerprints = tuple(
        field.strip()
        for field in (args.candidates or "").split(",")
        if field.strip()
    )
    for fingerprint in fingerprints:
        candidate_measure_axes(find_banked_candidate(fingerprint).candidate)
    return fingerprints


def _chosen_program(args: argparse.Namespace) -> MeasurementProgram:
    """The program row ``--program`` names, after the usage rules argparse cannot.

    ``parser.error`` rather than a refusal, because these are malformed
    INVOCATIONS rather than walks the seam judged: ``--regime`` beside a
    program, geometry beside a registry row, or a ``spot`` with no bearing. The
    parser is carried on the namespace so this stays one function instead of
    one per verb.
    """
    parser: argparse.ArgumentParser = args.parser
    if args.regime is not None:
        parser.error("--regime goes with --angles; programs play per_driver at every pose")
    if args.program == "spot":
        if args.azimuth is None:
            parser.error("--program spot needs --azimuth (and --elevation for a raised pose)")
        return measurement_programs.spot_program(args.azimuth, args.elevation or 0)
    if args.azimuth is not None or args.elevation is not None:
        parser.error("--azimuth/--elevation belong to --program spot; a named program owns its poses")
    return measurement_programs.program(args.program, args.size)


def _build_request(args: argparse.Namespace) -> AngleCaptureRequest:
    """The request the operator stated, through the seam's own constructors.

    Two doors, one seam. ``--program`` hands a
    :class:`~jasper.active_speaker.measurement_programs.MeasurementProgram` to
    ``request_for_program``, which owns the pose->stop expansion; ``--angles``
    builds :class:`AngleStop` directly, because ``--regime both`` must emit the
    PAIRED order ``both_at`` defines (per-driver then summed at each angle, so
    the microphone moves once per angle) and one comprehension over
    ``_REGIME_STOPS`` keeps that pairing rule in one place. The validation is
    identical either way -- it lives in ``AngleStop`` and
    ``AngleCaptureRequest``, which every route goes through.
    """
    if args.program:
        return request_for_program(
            _chosen_program(args),
            candidates=_resolved_candidates(args),
            **_graph_flags(args),
        )
    if args.candidates:
        args.parser.error(
            "--candidates goes with --program; a free-form angle list states "
            "no candidate cycle"
        )
    regimes = _REGIME_STOPS[args.regime or REGIME_PER_DRIVER]
    return AngleCaptureRequest(
        stops=tuple(
            AngleStop(angle, regime)
            for angle in _parse_angles(args.angles)
            for regime in regimes
        ),
        **_graph_flags(args),
    )


def _resolved_level() -> ResolvedLevel | LevelUnresolved:
    """This walk's absolute level, or the refusal that stops it being knowable.

    Resolved ONCE per verb and carried as the object it is: ``plan`` prints the
    refusal, ``stage`` hands the SAME exception to :func:`_refuse`, so neither
    verb rebuilds one from the receipt it just flattened.
    """
    try:
        return resolve_anchor_level()
    except LevelUnresolved as exc:
        return exc


def _level_block(level: ResolvedLevel | LevelUnresolved) -> dict[str, Any]:
    """What this walk drives at, or the input that stops it being knowable.

    Never a relative fallback: a receipt that printed ``+0 dB`` with no anchor
    behind it would read as an absolute level nobody measured.
    """
    if isinstance(level, LevelUnresolved):
        return {"resolved": False, "reason": level.reason, "detail": level.detail}
    return {
        "resolved": True,
        "anchor_db_spl": round(level.anchor_db_spl, 2),
        "reference_volume_db": round(level.reference_volume_db, 2),
        "mic_serial": level.mic_serial,
    }


def _walk_payload(
    request: AngleCaptureRequest, level: ResolvedLevel | LevelUnresolved
) -> dict[str, Any]:
    """The resolved walk, as one JSON-able document.

    Everything here is READ off the seam -- ``resolve_request`` for the stops,
    ``announced_indexes`` for the prelude -- so this function states nothing
    the session would not.

    ``program``, ``price``, ``level`` and ``handoff_url`` are the RECEIPT: what
    was asked for, what it drives at, what it costs the household, and where
    they run it. Everything else is the resolved walk.
    """
    stops = resolve_request(request)
    return {
        "program": request.program,
        "candidates": sorted({stop.candidate_id for stop in request.stops} - {""}),
        "price": walk_price(request),
        "level": _level_block(level),
        "handoff_url": speaker_url(CROSSOVER_PAGE_PATH),
        "mover": request.mover,
        "externally_positioned": request.externally_positioned,
        "polarity": request.polarity,
        "inverted_role": request.inverted_role,
        "delayed_role": request.delayed_role,
        "delay_us": request.delay_us,
        "level_matched": request.level_matched,
        "stops": [
            {
                "index": stop.index,
                "angle_deg": stop.angle_deg,
                "elevation_deg": stop.elevation_deg,
                "regime": stop.regime,
                "program_phase": stop.program_phase,
                "prompt": stop.prompt.text,
                "screen": dict(stop.screen),
            }
            for stop in stops
        ],
        "announced_indexes": list(announced_indexes(request)),
    }


def _print_walk(payload: dict[str, Any]) -> None:
    """The same walk a person reads, one line per stop.

    The gate line says WHO arms it, because since #2879 that is two different
    answers. An arm walk can only run in a gated session, and it declares each
    target itself -- so the ``gate N deg`` column below is populated and this
    walk is gated by construction. A person's walk is gated when the SESSION
    holds (a hand-walked round on the wired capture source), and the request
    cannot know that, so it declares no target and the column is empty. Saying
    only the first would tell a human operator their walk has no gate, which
    was true before that change and is not now.
    """
    mover = payload["mover"]
    print(
        f"{len(payload['stops'])} stops, moved by {mover}"
        + (
            " (position gate armed; each stop declares its own target below)"
            if payload["externally_positioned"]
            else " (the SESSION decides the gate: a wired round holds every"
            " begin at the bearing below, a phone round taps)"
        )
    )
    for stop in payload["stops"]:
        gate = stop["screen"].get("position_deg")
        print(
            f"  {stop['index']:>2}. {stop['angle_deg']:>+4d} deg  "
            f"{stop['regime']:<10}  plays {stop['program_phase']:<12} "
            f"advance {stop['screen']['auto_advance']}"
            + (f"  gate {gate} deg" if gate is not None else "")
            # Only when raised: ``offset_cm`` is horizontal, so a vertical pose
            # would otherwise print as an on-axis row it is not.
            + (
                f"  el {stop['elevation_deg']:+d} deg"
                if stop["elevation_deg"] else ""
            )
        )
    if payload["polarity"] != POLARITY_NORMAL:
        # Printed only when it is not the ordinary walk, and printed even when
        # the branch is unnamed: a one-sided pair is refused when the session
        # adopts the walk, and an operator seeing nothing here would read the
        # staging that preceded that refusal as an ordinary success.
        print(
            f"  polarity: {payload['polarity']} on the design-axis MEASURE "
            f"capture, flipping {payload['inverted_role']!r}"
        )
    if payload["delayed_role"]:
        # Printed only when stated, for the same reason the polarity line is:
        # a walk that reaches the graph carrying a confirmation delay must not
        # look identical to one that does not -- previously this was
        # traceable only through measure_spec.measurement_delays_for.
        print(
            f"  delay: {payload['delay_us']:g} us on "
            f"{payload['delayed_role']!r}"
        )
    geometry = _declared_on_the_box()
    if geometry is not None:
        # What the packet will bank, read from the same file it reads. Printed
        # only when the household declared one, which most have not.
        print(
            "  declared geometry (m): "
            + ", ".join(
                f"{key} {value:g}" for key, value in geometry.to_dict().items()
            )
        )
    announced = payload["announced_indexes"]
    print(
        "  prelude: "
        + (
            ", ".join(str(i) for i in announced)
            if announced
            else "none (this walk announces nothing on its own)"
        )
    )
    candidates = payload["candidates"]
    if candidates:
        # Only when stated: an ordinary walk measures the speaker as it stands
        # and has no cycle to name.
        print(f"  candidates: {', '.join(candidates)}")
    price = payload["price"]
    print(
        f"  price: {price['mic_moves']} spots, {price['captures']} captures, "
        f"up to {price['ceiling_min']} min for the session that takes it"
    )
    level = payload["level"]
    print(
        "  level: "
        + (
            f"{level['anchor_db_spl']:.1f} dB SPL at the mic (the banked "
            f"anchor; reference volume "
            f"{level['reference_volume_db']:.1f} dB; "
            f"mic {level['mic_serial']})"
            if level["resolved"]
            else f"unresolved -- {level['reason']}: {level['detail']}"
        )
    )
    print(
        f"  open {payload['handoff_url']} on the household's phone; the page "
        "states the price before Start"
    )
    print(
        "  done: jasper-crossover-prescriber status -- the walk's takes "
        "appear under banked.walk"
    )


def _fs_failure(detail: str, *, as_json: bool) -> int:
    """A filesystem failure, on both channels, as :data:`EXIT_WRITE_FAILED`.

    Shared by the two verbs that touch the slot, so the exit-code contract this
    module states is answered from one place rather than restated per verb.
    """
    if as_json:
        print(
            json.dumps({"ok": False, "reason": "stage_failed", "detail": detail},
                       indent=2)
        )
    print(detail, file=sys.stderr)
    return EXIT_WRITE_FAILED


def _refuse(exc: Exception, *, as_json: bool, reason: str | None = None) -> int:
    """One refusal, on both channels, with the sentence its raiser wrote."""
    detail = getattr(exc, "detail", None) or str(exc)
    slug = reason or getattr(exc, "reason", None) or "angle_request_refused"
    if as_json:
        print(json.dumps({"ok": False, "reason": slug, "detail": detail}, indent=2))
    print(f"refused ({slug}): {detail}", file=sys.stderr)
    return EXIT_REFUSED


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        request = _build_request(args)
    except measurement_programs.UnknownProgramError as exc:
        return _refuse(exc, as_json=args.json, reason=UNKNOWN_PROGRAM)
    except CandidateBankRefusal as exc:
        return _refuse(exc, as_json=args.json, reason=exc.code)
    except CrossoverV2FlowError as exc:
        return _refuse(exc, as_json=args.json)
    # An unresolved level is PRINTED here and refused by ``stage``: the dry run
    # exists to show an operator what is missing before they commit to it.
    payload = _walk_payload(request, _resolved_level())
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2))
    else:
        _print_walk(payload)
    return EXIT_OK


def _cmd_stage(args: argparse.Namespace) -> int:
    try:
        request = _build_request(args)
    except measurement_programs.UnknownProgramError as exc:
        return _refuse(exc, as_json=args.json, reason=UNKNOWN_PROGRAM)
    except CandidateBankRefusal as exc:
        return _refuse(exc, as_json=args.json, reason=exc.code)
    except CrossoverV2FlowError as exc:
        return _refuse(exc, as_json=args.json)
    # The methodology levels the seat before anything measures, so a level this
    # door cannot resolve names the step the operator skipped rather than
    # staging a walk whose captures nobody could read absolutely. It is not
    # written to the spool -- nothing downstream reads a level yet.
    level = _resolved_level()
    if isinstance(level, LevelUnresolved):
        return _refuse(level, as_json=args.json)
    payload = _walk_payload(request, level)
    try:
        path = stage_angle_request(request)
    except AngleRequestRefused as exc:
        return _refuse(exc, as_json=args.json)
    except OSError as exc:
        return _fs_failure(
            f"the request could not be written to "
            f"{angle_request_spool_path()}: {exc}",
            as_json=args.json,
        )
    if args.json:
        print(json.dumps({"ok": True, "staged_at_path": str(path), **payload}, indent=2))
    else:
        _print_walk(payload)
        print(f"staged at {path}")
    return EXIT_OK


def _cmd_withdraw(args: argparse.Namespace) -> int:
    try:
        removed = withdraw_staged_angle_request()
    except OSError as exc:
        # The same exit code and the same channel split as ``stage``'s write
        # failure, because it is the same class of problem: an unwritable slot
        # directory. Without this the module's own exit-code contract is false
        # for one verb -- an unlink that cannot proceed would exit ``1`` with a
        # traceback, which tells a script neither "fix the request" nor "fix the
        # filesystem".
        return _fs_failure(
            f"the staged walk could not be withdrawn from "
            f"{angle_request_spool_path()}: {exc}",
            as_json=args.json,
        )
    if args.json:
        print(json.dumps({"ok": True, "withdrawn": removed}, indent=2))
    else:
        print("withdrew the staged walk" if removed else "no walk was staged")
    return EXIT_OK


def _add_request_args(parser: argparse.ArgumentParser) -> None:
    # Carried so ``_chosen_program`` can raise a USAGE error (exit 2, argparse's
    # own wording) from the one place the request is built, rather than each
    # verb repeating the cross-argument rules.
    parser.set_defaults(parser=parser)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--program",
        choices=PROGRAM_IDS,
        help=(
            "the named measurement program to walk: baseline (the standard "
            "pose table), tournament (the candidate cycle's few poses, "
            "multiplied by --candidates), both sized by --size, or spot (one "
            "pose, at --azimuth and --elevation). The program owns the geometry"
        ),
    )
    source.add_argument(
        "--angles",
        help=(
            "operator escape hatch: a free-form angle list; LLM drivers stage "
            "a named program with --program. Comma-separated whole degrees off "
            "the design axis, negative LEFT and positive RIGHT facing the "
            "speaker (e.g. 0,7,-7,22,-22)"
        ),
    )
    parser.add_argument(
        "--size",
        default="express",
        # No argparse ``choices``: the registry owns the valid set, so an
        # unknown size refuses in its own words and names the real pairs.
        help=(
            f"which tier of a named program, one of {_size_phrase()} for "
            "baseline. Ignored by --program spot, which is one pose either way"
        ),
    )
    parser.add_argument(
        "--azimuth",
        type=int,
        help=(
            "--program spot only: the signed whole-degree bearing off the "
            "design axis, negative LEFT"
        ),
    )
    parser.add_argument(
        "--elevation",
        type=int,
        help=(
            "--program spot only: the signed whole-degree bearing above mark "
            "height, negative BELOW. Defaults to mark height"
        ),
    )
    parser.add_argument(
        "--candidates",
        help=(
            "--program only: comma-separated banked candidate fingerprints to "
            "cycle at every pose, adjacent so the microphone moves once per "
            "pose. Each is played as the ALIGNMENT it was minted with; a "
            "candidate carrying linearization EQ, or minted against another "
            "crossover corner, is refused. Omit to measure the speaker as it "
            "stands"
        ),
    )
    parser.add_argument(
        "--regime",
        default=None,
        choices=sorted(_REGIME_STOPS),
        help=(
            "--angles only: what to play at each angle -- per_driver (the "
            "forward model's input, the default), summed (the system "
            "response), or both (paired, so the microphone moves once per "
            "angle). A program plays per_driver at every pose"
        ),
    )
    parser.add_argument(
        "--mover",
        default=MOVER_HUMAN,
        choices=sorted(MOVERS),
        help=(
            "who moves the microphone: human (string and protractor; each stop "
            "waits for a tap, and the session holds it at that bearing when it "
            "is a wired round) or arm (each stop auto-begins behind the "
            "countdown and declares the angle the position gate waits for)"
        ),
    )
    parser.add_argument(
        "--polarity",
        default=POLARITY_NORMAL,
        choices=sorted(POLARITIES),
        help=(
            "how the session's design-axis MEASURE capture rides: normal, or "
            "inverted with --inverted-role naming the branch flipped (the "
            "reverse-null; one act at one place, so it is not per angle). "
            "Needs a WIRED session: only that source plays MEASURE through "
            "the engine leg, so an inverted walk refuses any other"
        ),
    )
    parser.add_argument(
        "--inverted-role",
        default="",
        help=(
            "which driver branch --polarity inverted flips, one of "
            f"{', '.join(DRIVER_ROLES)}. Left unchecked here on purpose: the "
            "measurement spec judges the pair when the session adopts the walk"
        ),
    )
    parser.add_argument(
        "--delayed-role",
        default="",
        help=(
            "which driver branch carries the confirmation delay, one of "
            f"{', '.join(DRIVER_ROLES)}. Pair with --delay-us. Unchecked here "
            "for the reason --inverted-role is: the spec judges the pair"
        ),
    )
    parser.add_argument(
        "--delay-us",
        type=float,
        default=0.0,
        help=(
            "the confirmation coordinate in microseconds, non-negative. Pair "
            "with --delayed-role; the sign frame lives in the walk coordinate, "
            "which names the branch"
        ),
    )
    parser.add_argument(
        "--level-matched",
        action="store_true",
        help=(
            "play the MEASURE capture through a graph carrying this speaker's "
            "own per-driver level match, so branches of unequal sensitivity "
            "meet the crossover at comparable level and a reverse null can "
            "form. A flag and not a number: the trims are resolved on the box "
            "from its banked evidence when the session adopts the walk, and a "
            "box with none refuses the walk rather than measuring unmatched"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the result (or refusal) as JSON"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-angle-capture",
        description=(
            "State one angle walk, see what it resolves to, and leave it for "
            "the next measurement session."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WHEN NOT TO USE\n"
            "  - to run a walk -- this only DECLARES one; jasper-arm-walk\n"
            "    (lab arm) or the guided web flow (human mover) is what\n"
            "    actually moves the microphone\n"
            "  - stage, when a walk is already staged -- check first with\n"
            "    jasper-crossover-prescriber status, or withdraw the\n"
            "    pending one\n"
            "\n"
            "EXAMPLES\n"
            "  jasper-angle-capture plan --program baseline --size express\n"
            "  jasper-angle-capture stage --program baseline --size express\n"
            "  jasper-angle-capture stage --program spot --azimuth 22\n"
            "  jasper-angle-capture stage --angles 0,7,-7 (operator escape\n"
            "    hatch: a free-form list no program names)\n"
            "  jasper-angle-capture withdraw\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- resolved (plan), staged (stage), or\n"
            "     withdrew/no-op (withdraw)\n"
            "  1  EXIT_REFUSED -- the request reached a door and was\n"
            "     refused; \"refused (<reason>): <detail>\" on stderr names\n"
            "     why, and as JSON with --json. An invocation argparse\n"
            "     itself rejects exits 2 with a usage line instead\n"
            "  3  EXIT_WRITE_FAILED -- stage or withdraw could not write or\n"
            "     unlink the spool file -- a filesystem problem, not a\n"
            "     request problem"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser(
        "plan",
        help="resolve a request and print the walk; writes nothing",
    )
    _add_request_args(plan)
    plan.set_defaults(func=_cmd_plan)

    stage = sub.add_parser(
        "stage",
        help="resolve a request and bank it for the next session to take",
    )
    _add_request_args(stage)
    stage.set_defaults(func=_cmd_stage)

    withdraw = sub.add_parser(
        "withdraw",
        help="remove a staged walk without running it",
    )
    withdraw.add_argument(
        "--json", action="store_true", help="emit the result as JSON"
    )
    withdraw.set_defaults(func=_cmd_withdraw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # The same reasoning ``jasper-crossover-prescriber``'s ``main`` records
    # (#2728): without this, ``event=angle_capture.request_staged`` — written by
    # ``stage_angle_request`` at INFO, right after the atomic write — reaches no
    # handler at all, because ``logging.lastResort`` starts at WARNING. This CLI
    # is the only supported staging path, so that would make the one state
    # transition it performs unobservable: a walk could be banked, or silently
    # REPLACE another, with nothing anywhere saying so. In ``main`` rather than
    # at import, because a module that configures the root logger on import
    # imposes its choice on every importer, the test suite included.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
