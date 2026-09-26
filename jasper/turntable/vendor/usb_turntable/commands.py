"""Canonical CT command catalog and wire formatting."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class CommandSpec:
    wire_name: str
    value_prefix: str | None = None
    motion: bool = False


_COMMANDS = {
    "connection": CommandSpec("CT"),
    "product": CommandSpec("GETPN", "PN="),
    "firmware": CommandSpec("GETFWV", "FWV="),
    "offset_angle": CommandSpec("GETOFFSETANGLE"),
    "turn_relative": CommandSpec("TURNSINGLE", motion=True),
    "turn_relative_legacy": CommandSpec("TRUNSINGLE", motion=True),
    "set_zero": CommandSpec("SETZERO"),
    "return_to_zero": CommandSpec("TOZERO", motion=True),
    "stop": CommandSpec("SETSTOP"),
}
COMMANDS = MappingProxyType(_COMMANDS)


def format_degrees(value: int | float) -> str:
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 360:
        raise ValueError("degrees must be a finite number greater than 0 and at most 360")
    return f"{number:.6f}".rstrip("0").rstrip(".")


def direction_code(direction: str) -> int:
    normalized = direction.lower()
    if normalized in ("cw", "clockwise"):
        return 0
    if normalized in ("ccw", "counterclockwise"):
        return 1
    raise ValueError("direction must be 'cw' or 'ccw'")


def build_command(spec: CommandSpec, *parameters: object) -> str:
    if spec.wire_name == "CT":
        if parameters:
            raise ValueError("connection test does not take parameters")
        return "CT();"
    joined = ",".join(str(parameter) for parameter in parameters)
    return f"CT+{spec.wire_name}({joined});"
