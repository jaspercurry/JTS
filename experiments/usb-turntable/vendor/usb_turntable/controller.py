"""High-level turntable controller."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Literal

from .commands import COMMANDS, direction_code, format_degrees
from .discovery import select_port
from .protocol import (
    DEFAULT_MOTION_TIMEOUT,
    DEFAULT_RESPONSE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    ExchangeResult,
    ProtocolSession,
    validate_timing_values,
)
from .transport import SerialTransport


@dataclass(frozen=True)
class OperationResult:
    operation: str
    command: str
    acknowledged: bool
    completed: bool
    pause_seen: bool
    frames: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["frames"] = list(self.frames)
        return value


@dataclass(frozen=True)
class ProbeResult:
    connected: bool
    product: str | None
    firmware: str | None
    port: str | None
    frames: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["frames"] = list(self.frames)
        return value


def _first_value(frames: tuple[str, ...], prefix: str) -> str | None:
    for frame in frames:
        if frame.startswith(prefix):
            return frame[len(prefix):]
    return None


def _firmware_tuple(text: str | None) -> tuple[int, int, int] | None:
    match = re.search(r"V(\d+)R(\d+)C(\d+)", text or "", re.IGNORECASE)
    return tuple(int(value) for value in match.groups()) if match else None


class TurntableController:
    def __init__(
        self,
        session: ProtocolSession,
        *,
        port: str | None = None,
        transport: SerialTransport | None = None,
    ) -> None:
        self.session = session
        self.port = port
        self._transport = transport
        self._firmware: str | None = None

    @classmethod
    def open(
        cls,
        port: str | None = None,
        *,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
        motion_timeout: float = DEFAULT_MOTION_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> "TurntableController":
        validate_timing_values(
            response_timeout=response_timeout,
            motion_timeout=motion_timeout,
            startup_timeout=startup_timeout,
        )
        selected = select_port(port)
        transport = SerialTransport(selected).open()
        try:
            session = ProtocolSession(
                transport,
                response_timeout=response_timeout,
                motion_timeout=motion_timeout,
                startup_timeout=startup_timeout,
            )
            session.synchronize()
        except Exception:
            transport.close()
            raise
        return cls(session, port=selected, transport=transport)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def __enter__(self) -> "TurntableController":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @staticmethod
    def _operation(name: str, result: ExchangeResult) -> OperationResult:
        return OperationResult(
            operation=name,
            command=result.command,
            acknowledged=result.acknowledged,
            completed=result.completed,
            pause_seen=result.pause_seen,
            frames=result.frames,
        )

    def probe(self) -> ProbeResult:
        connection = self.session.execute(COMMANDS["connection"])
        firmware = self.session.execute(COMMANDS["firmware"])
        product = self.session.execute(COMMANDS["product"])
        self._firmware = _first_value(firmware.frames, "FWV=")
        return ProbeResult(
            connected=connection.acknowledged,
            product=_first_value(product.frames, "PN="),
            firmware=self._firmware,
            port=self.port,
            frames=connection.frames + firmware.frames + product.frames,
        )

    def _firmware_version(self) -> tuple[int, int, int]:
        if self._firmware is None:
            result = self.session.execute(COMMANDS["firmware"])
            self._firmware = _first_value(result.frames, "FWV=")
        parsed = _firmware_tuple(self._firmware)
        if parsed is None:
            raise ValueError(f"unparseable firmware version: {self._firmware!r}")
        return parsed

    def turn_relative(
        self,
        direction: Literal["cw", "ccw"],
        degrees: int | float,
    ) -> OperationResult:
        code = direction_code(direction)
        formatted = format_degrees(degrees)
        key = "turn_relative" if self._firmware_version() >= (2, 4, 2) else "turn_relative_legacy"
        return self._operation(
            "turn_relative",
            self.session.execute_motion(COMMANDS[key], code, formatted),
        )

    def set_zero(self) -> OperationResult:
        return self._operation("set_zero", self.session.execute(COMMANDS["set_zero"]))

    def return_to_zero(self) -> OperationResult:
        return self._operation(
            "return_to_zero",
            self.session.execute_motion(COMMANDS["return_to_zero"]),
        )

    def stop(self) -> OperationResult:
        return self._operation("stop", self.session.execute(COMMANDS["stop"]))

    # Small compatibility aliases for the original one-file controller.
    def move(self, angle: int | float, direction: str) -> OperationResult:
        return self.turn_relative(direction, angle)  # type: ignore[arg-type]

    def home(self) -> OperationResult:
        return self.return_to_zero()
