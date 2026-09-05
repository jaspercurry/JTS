# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-audition`` — listen to this speaker at a reduced DSP layer.

The operator door onto :mod:`jasper.active_speaker.audition`. Its own binary
rather than a tenth ``jasper-active-speaker`` verb: that CLI already carries
startup templates, path audits, re-emit and the whole commissioning ladder, and
this is a different job — one an owner runs while music is playing.

Usage::

    jasper-audition start            # measured correction off, holds until you stop
    jasper-audition stop             # from a second shell; puts the full graph back
    jasper-audition status           # what is playing, and until when

Run it as root (``sudo``): the record lives in ``/run/jasper-active-speaker``,
which the web units own, and the CamillaDSP socket is root-owned. A non-root
run refuses at the write and undoes its own swap rather than half-starting.

``start`` BLOCKS on purpose. The process that swaps the graph is the process
that puts it back — at the deadline, on Ctrl-C, or on any error — so an
abandoned SSH session ends the audition instead of stranding one. Killing it
outright is still safe: nothing durable moved, so ``stop`` or any CamillaDSP
restart reverts.

Exit 0 when the speaker ends on its full graph; 1 on any refusal. Either way
the answer is ONE JSON document on stdout -- the audition's state, or
``_refusal``'s ``{status, reason, detail}`` -- and the human rendering is
stderr's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Any

from jasper.active_speaker.audition import (
    AUDITION_LAYER_BASELINE,
    AUDITION_LAYER_FULL,
    AUDITION_LAYERS,
    END_INTERRUPTED,
    AuditionRefused,
    hold_audition,
    read_audition_state,
    start_audition,
    stop_audition,
)
from jasper.cli._logging import CLI_LOG_FORMAT
from jasper.cli._refusal import EXIT_OK, EXIT_REFUSED, failed
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

#: ``start`` returned with the speaker still off its applied graph -- a second
#: ``start`` took the door, or the restore did not land. Not one of the engine's
#: ``END_*`` words: those say why the hold ENDED, this says what is playing.
NOT_RESTORED = "audition_not_restored"


def _camilla_controller() -> Any:
    """A CamillaController on the live websocket — the same graph the daemons see."""

    from jasper.camilla import primary_controller

    return primary_controller()


def _play_cue(slug: str) -> None:
    """Ask the running daemon to speak one cue.

    Routed through ``jasper-control`` rather than played here for the reason
    ``jasper-cues play`` gives: the daemon owns the TTS gain and routing, and a
    standalone process gets the level wrong. Best-effort by contract — the
    caller swallows whatever this raises.
    """

    from jasper.control import client as control

    control.post("/cue/play", {"slug": slug}, timeout=35)


def _say(line: str = "") -> None:
    """The human rendering, on stderr: stdout carries the answer."""
    print(line, file=sys.stderr)


def _answer(payload: dict[str, Any]) -> int:
    """The one JSON document a verb that succeeded puts on stdout."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return EXIT_OK


def _print_status(payload: dict[str, Any]) -> None:
    layer = payload["layer"]
    _say(f"audition: {payload.get('status')}")
    _say(f"  layer: {layer}")
    if payload.get("deadline_at"):
        remaining = float(payload["deadline_at"]) - time.time()
        if remaining > 0:
            _say(f"  auto-restores in: {remaining / 60.0:.0f} min")
        else:
            # Past the deadline with the record still here means the owner died
            # without restoring. Nothing will now: say so instead of counting
            # down to a zero that never arrives.
            _say("  STALE: the owner is gone; run `jasper-audition stop`")
    if payload.get("louder_than_full_db"):
        _say(
            "  NOT level-matched: dropping the measured correction hands back "
            f"up to {float(payload['louder_than_full_db']):.1f} dB where it "
            "was cutting, so this layer plays louder in those bands"
        )
    if payload.get("entry_config_path"):
        _say(f"  durable graph (untouched): {payload['entry_config_path']}")


def _cmd_start(args: argparse.Namespace) -> int:
    cam = _camilla_controller()

    async def _run() -> dict[str, Any]:
        state = await start_audition(cam=cam, layer=args.layer, play_cue=_play_cue)
        if state.get("status") != "auditioning":
            return state
        _print_status(state)
        _say("Listening. Ctrl-C, or `jasper-audition stop`, to go back.")
        reason = await hold_audition(state, cam=cam, play_cue=_play_cue)
        return _ended(reason)

    try:
        payload = asyncio.run(_run())
    except AuditionRefused as exc:
        return _refused(exc)
    except KeyboardInterrupt:
        payload = _ended(END_INTERRUPTED)
        _say()
    _print_status(payload)
    if payload["layer"] != AUDITION_LAYER_FULL:
        return failed(
            EXIT_REFUSED, NOT_RESTORED, {**payload, "next": "jasper-audition stop"}
        )
    return _answer(payload)


def _ended(reason: str) -> dict[str, Any]:
    """What the speaker is actually on now, read rather than assumed.

    ``hold_audition``'s reason alone cannot answer this: ``superseded`` covers
    both an explicit stop (the applied graph IS back) and a takeover by a
    second ``start`` (it is NOT). The record is the oracle for which happened,
    and the exit code follows it.
    """

    live = read_audition_state()
    if live is None:
        return {"status": "restored", "layer": AUDITION_LAYER_FULL, "ended": reason}
    return {"status": "auditioning", "ended": reason, **live}


def _cmd_stop(args: argparse.Namespace) -> int:
    try:
        payload = asyncio.run(
            stop_audition(cam=_camilla_controller(), play_cue=_play_cue)
        )
    except AuditionRefused as exc:
        return _refused(exc)
    _print_status(payload)
    return _answer(payload)


def _cmd_status(args: argparse.Namespace) -> int:
    state = read_audition_state()
    payload = (
        {"status": "auditioning", **state}
        if state is not None
        else {"status": "not_auditioning", "layer": AUDITION_LAYER_FULL}
    )
    _print_status(payload)
    return _answer(payload)


def _refused(exc: AuditionRefused) -> int:
    log_event(
        logger,
        "active_speaker.audition",
        level=logging.WARNING,
        action="refused",
        reason=exc.reason,
        detail=exc.detail,
    )
    return failed(EXIT_REFUSED, exc.reason, exc.detail)


#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204). Runtime
#: only -- the durable graph is untouched (ADR-0193).
AUTHORITY_TIER = "mutating (runtime only; durable graph untouched -- ADR-0193)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-audition",
        description="Play this speaker at a reduced DSP layer, then put it back",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  Listen with the measured driver correction removed (start),\n"
            "  then put it back (stop), for an A/B against the applied\n"
            "  graph. Runtime only -- the durable graph on disk is never\n"
            "  touched (ADR-0193), so killing the process outright is safe:\n"
            "  nothing durable moved, and any CamillaDSP restart reverts.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - unattended -- start BLOCKS, holding the reduced layer\n"
            "    until you stop it, Ctrl-C it, or it times out; a caller\n"
            "    that forks it and walks away needs its own timeout\n"
            "    budget, this tool will not impose one for you\n"
            "  - to change what the speaker plays durably -- that is a\n"
            "    prescription through the doors, applied and re-measured;\n"
            "    this only swaps the RUNTIME graph back and forth\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-audition start --layer baseline   # correction off\n"
            "  jasper-audition stop                     # from a 2nd shell\n"
            "\n"
            "EXIT CODES\n"
            "  0  the speaker ends this call on its full (applied) graph --\n"
            "     start after a clean stop, or stop/status themselves\n"
            "  1  AuditionRefused (start already running elsewhere,\n"
            "     CamillaDSP unreachable, ...) under its own reason, or\n"
            "     start ended WITHOUT the full graph restored (a second\n"
            "     start superseded this one) under audition_not_restored,\n"
            "     which carries the live state. {status, reason, detail} on\n"
            "     stdout, one sentence on stderr. status/stop always exit\n"
            "     0 -- read the printed layer, not the code, for their\n"
            "     outcome"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="swap to a reduced layer and hold it until you stop or time out",
    )
    start.add_argument(
        "--layer",
        choices=AUDITION_LAYERS,
        default=AUDITION_LAYER_BASELINE,
        help=(
            "baseline = crossover, trims, delays and protection with the "
            "measured driver correction removed; full = the applied graph "
            "(asking for it is the restore)"
        ),
    )
    start.set_defaults(func=_cmd_start)

    stop = sub.add_parser("stop", help="put the applied graph back now")
    stop.set_defaults(func=_cmd_stop)

    status = sub.add_parser("status", help="which layer is playing, and until when")
    status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    # INFO floor, not configure_verbose_logging's WARNING one: the audition's
    # own event= lines are the record of which graph the speaker was on.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    from jasper.env_load import load_env_files
    from jasper.volume_coordinator import install_env_canonical_target_provider

    load_env_files()
    # Every swap here rides `set_active_config_raw`'s fader duck, and releasing
    # that duck reads the canonical target. Without this the release lands on a
    # stale entry snapshot — the household's level, restored wrong, in a process
    # whose whole job is to leave the speaker exactly as it found it.
    install_env_canonical_target_provider()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
