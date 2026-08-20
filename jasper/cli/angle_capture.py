# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""State one angle walk, see exactly what it will run, and leave it for a session.

The operator's door onto
:mod:`jasper.active_speaker.angle_capture` (#2732) -- the seam that resolves
``{per-driver | summed} x {angles} x {arm | human-guided}`` onto the shipped
program, pose and gate machinery, and that shipped with no way for anybody to
state a request.

Two verbs, and deliberately nothing between them:

* ``plan`` reads a request and prints the walk it resolves to -- every stop's
  index, angle, pose, program, advance policy and banking shape. It writes
  nothing and touches no state, so it is the safe thing to run first and the
  thing to paste into a session log.
* ``stage`` runs the SAME resolution and then banks the request where the next
  measurement session will take it
  (:mod:`jasper.active_speaker.angle_capture_spool`).

``plan`` is the dry run of ``stage`` rather than a different question -- the
same constructors, the same refusals, the same resolved walk -- which is the
shape ``jasper-crossover-prescriber``'s ``propose``/``stage`` pair already
establishes for this operator, and for the same reason: a staging verb with a
laxer check would be the second, weaker reader these designs exist to avoid.

**Angles are parsed, never coerced.** ``--angles 0,7,-7,22,-22`` is split and
each field handed to :class:`~jasper.active_speaker.angle_capture.AngleStop`
as an ``int`` **only when it is written as a whole number**; anything else
(``7.5``, ``0.4``, ``+7 deg``) is passed through unchanged so the seam's own
``_validated_angle`` refuses it in its own words. That matters more than it
looks: ``int("0.4")`` raises, but ``int(0.4)`` is ``0``, and an angle silently
truncated to zero is an ON-AXIS capture the operator never asked for. There is
no second validator here -- bounds, whole-degree-ness, the regime vocabulary and
the mover vocabulary are all the seam's.

**Exit codes are part of the contract**, because the caller of this tool is
often a script: ``0`` accepted, ``2`` the request was refused (a bad angle, an
unknown regime or mover, or a measurement session already holding the speaker),
``3`` an accepted request could not be banked. A refusal is not a crash -- it is
the door working -- so it prints the machine-readable reason on stdout as JSON
when asked, and the human sentence on stderr either way. ``3`` is its own code
rather than folded into ``2`` because the two send an operator to different
places: ``2`` means fix the request, ``3`` means fix the speaker's filesystem.

**What this tool does NOT do, stated plainly rather than implied.** It does not
run a capture, open a session, play anything, or move a microphone. ``stage``
places a request; the next measurement session takes it, once. ``plan`` is the
dry run -- the only way to see what a stated walk resolves to, in the units the
session will use -- and ``stage`` gives the request a durable, single-use home
instead of a shell variable. What a taken walk then does, and what it
deliberately does not adjudicate, is
``docs/testing-tooling.md`` ("Angle-walk door").
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Sequence

from ._logging import CLI_LOG_FORMAT

from jasper.active_speaker.angle_capture import (
    MOVER_HUMAN,
    MOVERS,
    REGIME_PER_DRIVER,
    REGIME_SUMMED,
    REGIMES,
    AngleCaptureRequest,
    AngleStop,
    announced_indexes,
    position_angle_deg,
    resolve_request,
)
from jasper.active_speaker.angle_capture_spool import (
    AngleRequestRefused,
    angle_request_spool_path,
    stage_angle_request,
    withdraw_staged_angle_request,
)
from jasper.active_speaker.crossover_v2_flow import CrossoverV2FlowError

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_STAGE_FAILED = 3


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


def _build_request(args: argparse.Namespace) -> AngleCaptureRequest:
    """The request the operator stated, through the seam's own constructors.

    Built with :class:`AngleStop` directly rather than through ``per_driver_at``
    / ``summed_at`` / ``both_at`` for one reason: ``--regime both`` must emit the
    PAIRED order those constructors define (per-driver then summed at each
    angle, so the microphone moves once per angle), and expressing all three
    regimes as one comprehension over ``_REGIME_STOPS`` keeps the pairing rule
    in one place instead of branching to a third constructor. The validation is
    identical either way -- it lives in ``AngleStop``, which every route goes
    through.
    """
    angles = _parse_angles(args.angles)
    regimes = _REGIME_STOPS[args.regime]
    stops = tuple(
        AngleStop(angle, regime) for angle in angles for regime in regimes
    )
    return AngleCaptureRequest(stops=stops, mover=args.mover)


def _walk_payload(request: AngleCaptureRequest) -> dict[str, Any]:
    """The resolved walk, as one JSON-able document.

    Everything here is READ off the seam -- ``resolve_request`` for the stops,
    ``position_angle_deg`` for the round-tripped bearing, ``announced_indexes``
    for the prelude -- so this function states nothing the session would not.

    ``banks_as`` is the receipt shape each stop produces: the fields of
    ``spatial.cloud_position_record`` this walk DETERMINES, and no others. The
    rest of that record (``captured_at``, ``wav_sha256``, the gate disclosure,
    the ripple) is measured, so a planner that printed placeholders for them
    would be inventing evidence. ``position_id`` and ``take_id`` are likewise
    absent: they are minted at consume time against the real attempt number.
    """
    stops = resolve_request(request)
    return {
        "mover": request.mover,
        "externally_positioned": request.externally_positioned,
        "stops": [
            {
                "index": stop.index,
                "angle_deg": stop.angle_deg,
                "regime": stop.regime,
                "program_phase": stop.program_phase,
                "prompt": stop.prompt.text,
                "banks_as": {
                    # The pose, in the cm-primary units every shipped consumer
                    # reads, plus the bearing read BACK off it -- the round trip
                    # the seam asserts, printed so an operator can see it held.
                    "offset_cm": round(float(stop.prompt.offset_cm), 3),
                    "role": stop.prompt.role,
                    "wide": bool(stop.prompt.wide),
                    "position_angle_deg": position_angle_deg(stop.prompt),
                },
                "screen": dict(stop.screen),
            }
            for stop in stops
        ],
        "announced_indexes": list(announced_indexes(request)),
    }


def _print_walk(payload: dict[str, Any]) -> None:
    """The same walk a person reads, one line per stop."""
    mover = payload["mover"]
    print(
        f"{len(payload['stops'])} stops, moved by {mover}"
        f"{' (position gate armed)' if payload['externally_positioned'] else ''}"
    )
    for stop in payload["stops"]:
        banks = stop["banks_as"]
        gate = stop["screen"].get("position_deg")
        # The pose is printed as its cm magnitude AND as the bearing read back
        # off it, because those are two different facts and only the second
        # carries the side: ``offset_cm`` is a distance, so +7 and -7 are the
        # same 12.28 cm, and a line that showed only the distance would look
        # like a duplicated row for a walk that is nothing of the kind.
        print(
            f"  {stop['index']:>2}. {stop['angle_deg']:>+4d} deg  "
            f"{stop['regime']:<10}  plays {stop['program_phase']:<12} "
            f"pose {banks['offset_cm']:>6.2f} cm "
            f"({banks['position_angle_deg']:>+3d} deg back) "
            f"{banks['role']}{' wide' if banks['wide'] else '    '}  "
            f"advance {stop['screen']['auto_advance']}"
            + (f"  gate {gate} deg" if gate is not None else "")
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


def _fs_failure(detail: str, *, as_json: bool) -> int:
    """A filesystem failure, on both channels, as :data:`EXIT_STAGE_FAILED`.

    Shared by the two verbs that touch the slot, so the exit-code contract this
    module states is answered from one place rather than restated per verb.
    """
    if as_json:
        print(
            json.dumps({"ok": False, "reason": "stage_failed", "detail": detail},
                       indent=2)
        )
    print(detail, file=sys.stderr)
    return EXIT_STAGE_FAILED


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
    except CrossoverV2FlowError as exc:
        return _refuse(exc, as_json=args.json)
    payload = _walk_payload(request)
    if args.json:
        print(json.dumps({"ok": True, **payload}, indent=2))
    else:
        _print_walk(payload)
    return EXIT_OK


def _cmd_stage(args: argparse.Namespace) -> int:
    try:
        request = _build_request(args)
    except CrossoverV2FlowError as exc:
        return _refuse(exc, as_json=args.json)
    payload = _walk_payload(request)
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
    parser.add_argument(
        "--angles",
        required=True,
        help=(
            "comma-separated whole degrees off the design axis, negative LEFT "
            "and positive RIGHT facing the speaker (e.g. 0,7,-7,22,-22)"
        ),
    )
    parser.add_argument(
        "--regime",
        default=REGIME_PER_DRIVER,
        choices=sorted(_REGIME_STOPS),
        help=(
            "what to play at each angle: per_driver (the forward model's "
            "input), summed (the system response), or both (paired, so the "
            "microphone moves once per angle)"
        ),
    )
    parser.add_argument(
        "--mover",
        default=MOVER_HUMAN,
        choices=sorted(MOVERS),
        help=(
            "who moves the microphone: human (string and protractor; each stop "
            "waits for a tap) or arm (each stop auto-begins behind the "
            "countdown and declares the angle the position gate waits for)"
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
