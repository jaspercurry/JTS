"""Bounded synchronization and strict CT request/response handling."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .commands import CommandSpec, build_command
from .errors import (
    CommandRejected,
    CommunicationTimeout,
    CompletionTimeout,
    ProtocolError,
    StartupSynchronizationError,
)


DEFAULT_RESPONSE_TIMEOUT = 3.0
DEFAULT_MOTION_TIMEOUT = 30.0
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_MIN_COMMAND_INTERVAL = 0.300
STARTUP_OBSERVE_SECONDS = 1.0
STARTUP_QUIET_SECONDS = 0.5
STARTUP_NOISE_MAX_BYTES = 64
HEARTBEAT_INTERVAL = 1.0
HEARTBEAT_LOST_AFTER = 5.0
SETTLE_SECONDS = 0.300


class Transport(Protocol):
    def write(self, payload: bytes, timeout: float = 2.0) -> None: ...
    def read_chunk(self, timeout: float) -> bytes: ...


@dataclass(frozen=True)
class ExchangeResult:
    command: str
    frames: tuple[str, ...]
    acknowledged: bool
    completed: bool
    pause_seen: bool


class FrameParser:
    """Parse semicolon frames while separating the vendor heartbeat bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, payload: bytes) -> tuple[list[str], list[str]]:
        frames: list[str] = []
        heartbeats: list[str] = []
        index = 0
        while index < len(payload):
            if payload[index] == ord("#"):
                if self.buffer:
                    raise ProtocolError("heartbeat byte appeared inside a protocol frame")
                end = index + 1
                while end < len(payload) and payload[end] == ord("#"):
                    end += 1
                heartbeat = bytes(payload[index:end])
                if len(heartbeat) >= 3:
                    raise ProtocolError(f"malformed heartbeat bytes: {heartbeat!r}")
                heartbeats.extend("#" for _ in heartbeat)
                index = end
                continue
            if payload[index] in (0, 10, 13):
                raise ProtocolError(f"forbidden control byte 0x{payload[index]:02x} in protocol stream")
            if payload[index] == ord(";"):
                raw = bytes(self.buffer)
                self.buffer.clear()
                if not raw:
                    raise ProtocolError("empty semicolon-terminated frame")
                try:
                    text = raw.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise ProtocolError(f"non-ASCII protocol frame: {raw.hex()}") from exc
                frames.append(text)
            else:
                self.buffer.append(payload[index])
            index += 1
        return frames, heartbeats

    @property
    def pending(self) -> str:
        return bytes(self.buffer).decode("ascii", errors="backslashreplace")


def _error_code(frame: str) -> int | None:
    match = re.fullmatch(r"CR\+ERR=(\d+)", frame)
    return int(match.group(1)) if match else None


def validate_timing_values(
    *,
    response_timeout: float,
    motion_timeout: float,
    startup_timeout: float,
    min_command_interval: float = DEFAULT_MIN_COMMAND_INTERVAL,
) -> None:
    values = {
        "response_timeout": response_timeout,
        "motion_timeout": motion_timeout,
        "startup_timeout": startup_timeout,
        "min_command_interval": min_command_interval,
    }
    for name, value in values.items():
        try:
            finite = math.isfinite(value)
        except TypeError as exc:
            raise ValueError(f"{name} must be a finite number") from exc
        if not finite:
            raise ValueError(f"{name} must be a finite number")
    if response_timeout <= 0 or motion_timeout <= 0:
        raise ValueError("response_timeout and motion_timeout must be greater than zero")
    if startup_timeout < STARTUP_OBSERVE_SECONDS:
        raise ValueError("startup_timeout must be at least 1 second")
    if min_command_interval < 0:
        raise ValueError("min_command_interval must be at least zero")


class ProtocolSession:
    def __init__(
        self,
        transport: Transport,
        *,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
        motion_timeout: float = DEFAULT_MOTION_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        heartbeat: bool = True,
        min_command_interval: float = DEFAULT_MIN_COMMAND_INTERVAL,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_timing_values(
            response_timeout=response_timeout,
            motion_timeout=motion_timeout,
            startup_timeout=startup_timeout,
            min_command_interval=min_command_interval,
        )
        self.transport = transport
        self.response_timeout = response_timeout
        self.motion_timeout = motion_timeout
        self.startup_timeout = startup_timeout
        self.heartbeat = heartbeat
        self.min_command_interval = min_command_interval
        self.monotonic = monotonic
        self.parser = FrameParser()
        now = self.monotonic()
        self._next_heartbeat_at = now + HEARTBEAT_INTERVAL
        self._last_rx_heartbeat: float | None = None
        self._heartbeat_state = "local_fail"
        self._last_command_at: float | None = None
        self.synchronized = False

    def _send_heartbeat_if_due(self) -> None:
        if not self.heartbeat:
            return
        now = self.monotonic()
        if self._last_rx_heartbeat is None or now - self._last_rx_heartbeat > HEARTBEAT_LOST_AFTER:
            self._heartbeat_state = "local_fail"
        if now >= self._next_heartbeat_at:
            self.transport.write(b"###" if self._heartbeat_state == "local_fail" else b"#")
            self._next_heartbeat_at = now + HEARTBEAT_INTERVAL

    def _accept_heartbeats(self, heartbeats: list[str]) -> None:
        for heartbeat in heartbeats:
            self._last_rx_heartbeat = self.monotonic()
            self._heartbeat_state = "normal"

    def synchronize(self) -> None:
        """Observe a bounded, quiescent startup stream before the first CT byte."""
        started = self.monotonic()
        deadline = started + self.startup_timeout
        last_nonheartbeat = started
        normal_heartbeat_seen = False
        prefix_seen = False
        timeout_frame_seen = False
        while self.monotonic() < deadline:
            self._send_heartbeat_if_due()
            remaining = deadline - self.monotonic()
            chunk = self.transport.read_chunk(min(0.1, max(0.0, remaining)))
            if chunk:
                if all(byte == ord("#") for byte in chunk):
                    try:
                        frames, heartbeats = FrameParser().feed(chunk)
                    except ProtocolError as exc:
                        raise StartupSynchronizationError(str(exc)) from exc
                    if frames or not heartbeats or any(item != "#" for item in heartbeats):
                        raise StartupSynchronizationError("startup returned a non-normal heartbeat")
                    self._accept_heartbeats(heartbeats)
                    normal_heartbeat_seen = True
                elif (
                    not prefix_seen
                    and not timeout_frame_seen
                    and 1 <= len(chunk) <= STARTUP_NOISE_MAX_BYTES
                    and all(byte in (0xFE, 0xFF) for byte in chunk)
                    and 0xFF in chunk
                ):
                    prefix_seen = True
                    last_nonheartbeat = self.monotonic()
                elif prefix_seen and not timeout_frame_seen and chunk == b"CR+ERR=31;":
                    timeout_frame_seen = True
                    last_nonheartbeat = self.monotonic()
                else:
                    raise StartupSynchronizationError(
                        f"startup returned unapproved bytes: {chunk.hex()}"
                    )
            now = self.monotonic()
            shape_complete = (not prefix_seen and not timeout_frame_seen) or (
                prefix_seen and timeout_frame_seen
            )
            if (
                now - started >= STARTUP_OBSERVE_SECONDS
                and now - last_nonheartbeat >= STARTUP_QUIET_SECONDS
                and normal_heartbeat_seen
                and shape_complete
            ):
                self.synchronized = True
                return
        detail = ""
        if not normal_heartbeat_seen:
            detail = ": no normal heartbeat observed"
        elif prefix_seen != timeout_frame_seen:
            detail = ": incomplete startup noise/CR+ERR=31 pair"
        raise StartupSynchronizationError(f"startup did not become synchronized{detail}")

    def _read_runtime(self, timeout: float) -> list[str]:
        self._send_heartbeat_if_due()
        chunk = self.transport.read_chunk(max(0.0, timeout))
        if not chunk:
            return []
        frames, heartbeats = self.parser.feed(chunk)
        self._accept_heartbeats(heartbeats)
        return frames

    def _reject_device_error(self, command: str, frames: list[str]) -> None:
        for frame in frames:
            code = _error_code(frame)
            if code is not None:
                raise CommandRejected(code, command, frames)

    def _require_quiet_until(self, deadline: float, phase: str) -> None:
        while self.monotonic() < deadline:
            frames = self._read_runtime(min(0.1, deadline - self.monotonic()))
            if frames or self.parser.pending:
                raise ProtocolError(f"{phase} was not quiescent: frames={frames!r}, pending={self.parser.pending!r}")

    def _pace(self) -> None:
        if self._last_command_at is None:
            return
        due = self._last_command_at + self.min_command_interval
        if self.monotonic() < due:
            self._require_quiet_until(due, "between-command receive")

    @staticmethod
    def _encode(command: str) -> bytes:
        try:
            return command.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("CT commands must be ASCII") from exc

    def execute(self, spec: CommandSpec, *parameters: object) -> ExchangeResult:
        if not self.synchronized:
            raise ProtocolError("session must be synchronized before sending a command")
        command = build_command(spec, *parameters)
        self._pace()
        self.transport.write(self._encode(command))
        self._last_command_at = self.monotonic()
        deadline = self.monotonic() + self.response_timeout
        frames: list[str] = []
        value_seen = False
        acknowledged = False
        while self.monotonic() < deadline and not acknowledged:
            new_frames = self._read_runtime(min(0.1, deadline - self.monotonic()))
            for frame in new_frames:
                frames.append(frame)
                self._reject_device_error(command, frames)
                if frame == "CR+OK":
                    if acknowledged:
                        raise ProtocolError(f"duplicate CR+OK for {command}")
                    if spec.value_prefix and not value_seen:
                        raise ProtocolError(f"missing {spec.value_prefix} value before CR+OK")
                    acknowledged = True
                elif spec.value_prefix and frame.startswith(spec.value_prefix) and not acknowledged:
                    if value_seen or not frame[len(spec.value_prefix):]:
                        raise ProtocolError(f"invalid or duplicate {spec.value_prefix} value")
                    value_seen = True
                else:
                    raise ProtocolError(f"unexpected frame for {command}: {frame!r}")
        if not acknowledged:
            raise CommunicationTimeout(f"no exact CR+OK for {command} within {self.response_timeout:.1f}s")
        self._require_quiet_until(self.monotonic() + SETTLE_SECONDS, f"{command} settle")
        return ExchangeResult(command, tuple(frames), True, True, False)

    def execute_motion(self, spec: CommandSpec, *parameters: object) -> ExchangeResult:
        if not spec.motion:
            raise ValueError("execute_motion requires a motion CommandSpec")
        if not self.synchronized:
            raise ProtocolError("session must be synchronized before sending a command")
        command = build_command(spec, *parameters)
        self._pace()
        self.transport.write(self._encode(command))
        self._last_command_at = self.monotonic()
        ack_deadline = self.monotonic() + self.response_timeout
        motion_deadline = self.monotonic() + self.motion_timeout
        frames: list[str] = []
        acknowledged = False
        pause_seen = False
        completed = False
        while self.monotonic() < motion_deadline and not completed:
            if not acknowledged and self.monotonic() >= ack_deadline:
                break
            deadline = motion_deadline if acknowledged else ack_deadline
            new_frames = self._read_runtime(min(0.1, deadline - self.monotonic()))
            for frame in new_frames:
                frames.append(frame)
                self._reject_device_error(command, frames)
                if not acknowledged:
                    if frame != "CR+OK":
                        raise ProtocolError(f"motion returned pre-ack frame {frame!r}")
                    acknowledged = True
                elif frame == "CR+EVENT=TB_PAUSE" and not pause_seen:
                    pause_seen = True
                elif frame == "CR+EVENT=TB_END" and not completed:
                    completed = True
                else:
                    raise ProtocolError(f"unexpected or duplicate motion frame {frame!r}")
        if not acknowledged:
            raise CommunicationTimeout(f"no exact CR+OK for {command} within {self.response_timeout:.1f}s")
        if not completed:
            raise CompletionTimeout(f"{command} was acknowledged but no exact TB_END arrived within {self.motion_timeout:.1f}s")
        self._require_quiet_until(self.monotonic() + SETTLE_SECONDS, f"{command} settle")
        return ExchangeResult(command, tuple(frames), True, True, pause_seen)
