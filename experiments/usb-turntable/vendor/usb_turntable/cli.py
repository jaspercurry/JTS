"""Command-line interface."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import is_dataclass
from typing import Sequence

from .controller import TurntableController
from .discovery import discover_devices
from .errors import TurntableError
from .protocol import (
    DEFAULT_MOTION_TIMEOUT,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    validate_timing_values,
)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control a ComXim USB turntable")
    parser.add_argument("--port", default=None, help="serial device; default is unique auto-discovery")
    parser.add_argument("--response-timeout", type=float, default=DEFAULT_RESPONSE_TIMEOUT)
    parser.add_argument("--motion-timeout", type=float, default=DEFAULT_MOTION_TIMEOUT)
    parser.add_argument("--startup-timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("devices", aliases=["ports"], help="list candidate serial devices")
    commands.add_parser("probe", help="query connection, firmware, and product; never moves")
    turn = commands.add_parser("turn", aliases=["move"], help="turn by a relative angle")
    turn.add_argument("degrees", type=float)
    turn.add_argument("--direction", "-d", required=True, choices=("cw", "ccw"))
    commands.add_parser("set-zero", aliases=["zero"], help="set the current position as logical zero")
    commands.add_parser("return-zero", aliases=["home"], help="return to logical zero")
    commands.add_parser("stop", help="send the vendor stop command")
    return parser


def _payload(value: object) -> dict[str, object]:
    if hasattr(value, "to_dict"):
        return {"ok": True, **value.to_dict()}  # type: ignore[union-attr]
    if isinstance(value, dict):
        return {"ok": True, **value}
    if is_dataclass(value):
        raise TypeError("public result dataclasses must define to_dict")
    return {"ok": True, "result": value}


def _emit(payload: dict[str, object], *, use_json: bool, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if use_json:
        print(json.dumps(payload, sort_keys=True), file=stream)
        return
    if error:
        print(f"Error: {payload['error']['message']}", file=stream)  # type: ignore[index]
        return
    for key, value in payload.items():
        if key != "ok":
            print(f"{key.replace('_', ' ').capitalize()}: {value}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        validate_timing_values(
            response_timeout=args.response_timeout,
            motion_timeout=args.motion_timeout,
            startup_timeout=args.startup_timeout,
        )
        if args.action in ("devices", "ports"):
            result: object = {"devices": discover_devices()}
        else:
            with TurntableController.open(
                args.port,
                response_timeout=args.response_timeout,
                motion_timeout=args.motion_timeout,
                startup_timeout=args.startup_timeout,
            ) as controller:
                if args.action == "probe":
                    result = controller.probe()
                elif args.action in ("turn", "move"):
                    result = controller.turn_relative(args.direction, args.degrees)
                elif args.action in ("set-zero", "zero"):
                    result = controller.set_zero()
                elif args.action in ("return-zero", "home"):
                    result = controller.return_to_zero()
                elif args.action == "stop":
                    result = controller.stop()
                else:  # pragma: no cover - argparse owns this boundary
                    parser.error(f"unknown action: {args.action}")
                    return 2
        _emit(_payload(result), use_json=args.json)
        return 0
    except (TurntableError, ValueError, OSError) as exc:
        payload = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        _emit(payload, use_json=args.json, error=True)
        return 1
