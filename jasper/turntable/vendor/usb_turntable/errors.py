"""Controller exceptions."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Sequence


ERROR_MESSAGES = {
    31: "serial receive timeout",
    40: "unsupported function or command",
    41: "command is too short",
    42: "invalid command header",
    43: "invalid command connector",
    45: "invalid parameter opening parenthesis",
    46: "invalid parameter closing parenthesis",
    48: "invalid command terminator",
    51: "command name is too long",
    52: "invalid command name",
    61: "empty parameter",
    62: "parameter is too long",
    63: "too many parameters",
    64: "parameter count mismatch",
}


class TurntableError(RuntimeError):
    """Base error for the controller."""


class PortDiscoveryError(TurntableError):
    """A unique serial device could not be selected."""


class ProtocolError(TurntableError):
    """The device returned a malformed, duplicate, or unexpected frame."""


class StartupSynchronizationError(ProtocolError):
    """The serial stream was not safely synchronized before the first command."""


class CommunicationTimeout(ProtocolError):
    """The device did not acknowledge a command within the deadline."""


class CompletionTimeout(ProtocolError):
    """A motion command was acknowledged but did not report completion."""


class CommandRejected(ProtocolError):
    """The device returned an exact CR+ERR frame."""

    def __init__(self, code: int, command: str, frames: Sequence[str]):
        self.code = code
        self.command = command
        self.frames = tuple(frames)
        if 70 <= code <= 79:
            detail = f"parameter {code - 70} is not numeric"
        elif 80 <= code <= 89:
            detail = f"parameter {code - 80} is outside its valid range"
        else:
            detail = ERROR_MESSAGES.get(code, "unknown device error")
        super().__init__(f"Turntable rejected {command!r}: error {code} ({detail})")
