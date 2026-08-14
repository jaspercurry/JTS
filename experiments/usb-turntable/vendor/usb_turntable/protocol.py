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
# At 115200 baud, 20 ms is over 200 byte-times but far below the 1 Hz heartbeat.
HEARTBEAT_INTERBYTE_QUIET_SECONDS = 0.020
SETTLE_SECONDS = 0.300
NORMAL_HEARTBEAT = "#"
REMOTE_LOS_HEARTBEAT = "###"
STARTUP_TIMEOUT_FRAME = b"CR+ERR=31;"


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
        self._heartbeat_run = 0

    def feed(self, payload: bytes) -> tuple[list[str], list[str]]:
        frames: list[str] = []
        heartbeats: list[str] = []
        index = 0
        while index < len(payload):
            if payload[index] == ord("#"):
                if self.buffer:
                    raise ProtocolError("heartbeat byte appeared inside a protocol frame")
                self._heartbeat_run += 1
                if self._heartbeat_run == len(REMOTE_LOS_HEARTBEAT):
                    # Match the vendor receiver: consume complete LOS triples
                    # before treating a one- or two-byte remainder as normal.
                    heartbeats.append(REMOTE_LOS_HEARTBEAT)
                    self._heartbeat_run = 0
                index += 1
                continue
            heartbeats.extend(self.finalize_heartbeat_run())
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

    def finalize_heartbeat_run(self) -> list[str]:
        """Classify a hash run only after a non-hash byte or inter-byte quiet."""
        count, self._heartbeat_run = self._heartbeat_run, 0
        if count == 0:
            return []
        return [NORMAL_HEARTBEAT] * count

    @property
    def heartbeat_pending(self) -> bool:
        return self._heartbeat_run > 0

    @property
    def pending(self) -> str:
        heartbeat = NORMAL_HEARTBEAT * self._heartbeat_run
        frame = bytes(self.buffer).decode("ascii", errors="backslashreplace")
        return heartbeat + frame


class _StartupStreamParser:
    """Recognize the one approved open-time byte-stream shape."""

    def __init__(self) -> None:
        self.state = "idle"
        self.prefix_length = 0
        self.prefix_has_ff = False
        self.frame_index = 0

    def _finish_prefix(self) -> None:
        if not 1 <= self.prefix_length <= STARTUP_NOISE_MAX_BYTES:
            raise ProtocolError("startup noise prefix length is outside 1..64 bytes")
        if not self.prefix_has_ff:
            raise ProtocolError("startup noise prefix did not contain 0xff")
        self.state = "expect_timeout_frame"

    def allow_heartbeat(self) -> None:
        if self.state == "prefix":
            self._finish_prefix()
        elif self.state == "timeout_frame":
            raise ProtocolError("heartbeat appeared inside CR+ERR=31 startup frame")

    def feed_nonheartbeat(self, byte: int) -> None:
        if byte == ord("#"):
            raise ValueError("heartbeat bytes must use allow_heartbeat")
        if self.state == "idle":
            if byte not in (0xFE, 0xFF):
                raise ProtocolError(f"startup returned unapproved byte 0x{byte:02x}")
            self.state = "prefix"
            self._append_prefix_byte(byte)
            return
        if self.state == "prefix":
            if byte in (0xFE, 0xFF):
                self._append_prefix_byte(byte)
                return
            self._finish_prefix()
        if self.state == "expect_timeout_frame":
            if byte != STARTUP_TIMEOUT_FRAME[0]:
                raise ProtocolError("startup noise prefix was not followed by exact CR+ERR=31;")
            self.state = "timeout_frame"
            self.frame_index = 1
            return
        if self.state == "timeout_frame":
            if byte != STARTUP_TIMEOUT_FRAME[self.frame_index]:
                raise ProtocolError("malformed CR+ERR=31 startup frame")
            self.frame_index += 1
            if self.frame_index == len(STARTUP_TIMEOUT_FRAME):
                self.state = "complete"
            return
        raise ProtocolError("unexpected or duplicate data after CR+ERR=31 startup frame")

    def _append_prefix_byte(self, byte: int) -> None:
        self.prefix_length += 1
        if self.prefix_length > STARTUP_NOISE_MAX_BYTES:
            raise ProtocolError("startup noise prefix exceeded 64 bytes")
        self.prefix_has_ff = self.prefix_has_ff or byte == 0xFF

    @property
    def shape_complete(self) -> bool:
        return self.state in ("idle", "complete")

    @property
    def pending_detail(self) -> str:
        return {
            "prefix": "startup noise prefix was not followed by CR+ERR=31;",
            "expect_timeout_frame": "startup noise prefix was not followed by CR+ERR=31;",
            "timeout_frame": "CR+ERR=31 startup frame was incomplete",
        }.get(self.state, "startup stream did not complete")


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
        self._last_heartbeat_byte_at: float | None = None
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
            now = self.monotonic()
            self._last_rx_heartbeat = now
            if heartbeat == REMOTE_LOS_HEARTBEAT:
                self._heartbeat_state = "remote_fail"
                if self.heartbeat:
                    # The vendor FSM sends a normal heartbeat while the remote
                    # endpoint reports LOS. Receiving it lets that endpoint
                    # leave LOS and resume its normal heartbeat.
                    self.transport.write(b"#")
                    self._next_heartbeat_at = now + HEARTBEAT_INTERVAL
            else:
                self._heartbeat_state = "normal"

    def _feed_protocol(self, chunk: bytes) -> list[str]:
        frames, heartbeats = self.parser.feed(chunk)
        if self.parser.heartbeat_pending:
            self._last_heartbeat_byte_at = self.monotonic()
        else:
            self._last_heartbeat_byte_at = None
        self._accept_heartbeats(heartbeats)
        return frames

    def _finalize_heartbeat_run(self) -> None:
        heartbeats = self.parser.finalize_heartbeat_run()
        self._last_heartbeat_byte_at = None
        self._accept_heartbeats(heartbeats)

    def _heartbeat_quiet_deadline(self) -> float | None:
        if not self.parser.heartbeat_pending or self._last_heartbeat_byte_at is None:
            return None
        return self._last_heartbeat_byte_at + HEARTBEAT_INTERBYTE_QUIET_SECONDS

    def _link_is_normal(self) -> bool:
        if not self.heartbeat:
            return True
        now = self.monotonic()
        if self._last_rx_heartbeat is None or now - self._last_rx_heartbeat > HEARTBEAT_LOST_AFTER:
            self._heartbeat_state = "local_fail"
        return self._heartbeat_state == "normal"

    def synchronize(self) -> None:
        """Observe a bounded, quiescent startup stream before the first CT byte."""
        started = self.monotonic()
        deadline = started + self.startup_timeout
        last_nonheartbeat = started
        link_normal = False
        startup = _StartupStreamParser()
        while self.monotonic() < deadline:
            self._send_heartbeat_if_due()
            remaining = deadline - self.monotonic()
            wait = min(0.1, max(0.0, remaining))
            quiet_deadline = self._heartbeat_quiet_deadline()
            if quiet_deadline is not None:
                wait = min(wait, max(0.0, quiet_deadline - self.monotonic()))
            chunk = self.transport.read_chunk(wait)
            if chunk:
                try:
                    for byte in chunk:
                        if byte == ord("#"):
                            startup.allow_heartbeat()
                            self._feed_protocol(b"#")
                        else:
                            self._finalize_heartbeat_run()
                            startup.feed_nonheartbeat(byte)
                            last_nonheartbeat = self.monotonic()
                except ProtocolError as exc:
                    raise StartupSynchronizationError(str(exc)) from exc
            quiet_deadline = self._heartbeat_quiet_deadline()
            if (
                not chunk
                and wait == 0
                and quiet_deadline is not None
                and self.monotonic() >= quiet_deadline
            ):
                try:
                    self._finalize_heartbeat_run()
                except ProtocolError as exc:
                    raise StartupSynchronizationError(str(exc)) from exc
            link_normal = self._heartbeat_state == "normal"
            now = self.monotonic()
            if (
                now - started >= STARTUP_OBSERVE_SECONDS
                and now - last_nonheartbeat >= STARTUP_QUIET_SECONDS
                and link_normal
                and not self.parser.heartbeat_pending
                and startup.shape_complete
            ):
                self.synchronized = True
                return
        detail = ""
        if self.parser.heartbeat_pending:
            detail = ": heartbeat run did not reach an inter-byte quiet boundary"
        elif not startup.shape_complete:
            detail = f": {startup.pending_detail}"
        elif not link_normal:
            detail = ": no normal heartbeat observed"
        raise StartupSynchronizationError(f"startup did not become synchronized{detail}")

    def _read_runtime(self, timeout: float) -> list[str]:
        frames: list[str] = []
        deadline = self.monotonic() + max(0.0, timeout)
        attempted_read = False
        while True:
            self._send_heartbeat_if_due()
            now = self.monotonic()
            quiet_deadline = self._heartbeat_quiet_deadline()
            if quiet_deadline is None and attempted_read:
                return frames
            wait_until = quiet_deadline if quiet_deadline is not None else deadline
            wait = max(0.0, wait_until - now)
            chunk = self.transport.read_chunk(wait)
            attempted_read = True
            if chunk:
                frames.extend(self._feed_protocol(chunk))
                if not self.parser.heartbeat_pending:
                    return frames
                continue
            quiet_deadline = self._heartbeat_quiet_deadline()
            if quiet_deadline is not None:
                if wait == 0 and self.monotonic() >= quiet_deadline:
                    self._finalize_heartbeat_run()
                    return frames
                continue
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

    def _recover_runtime_link(self) -> None:
        if self._link_is_normal():
            return
        deadline = self.monotonic() + self.response_timeout
        while self.monotonic() < deadline:
            frames = self._read_runtime(min(0.1, deadline - self.monotonic()))
            if frames or self.parser.pending:
                raise ProtocolError(
                    "link recovery received unexpected protocol data: "
                    f"frames={frames!r}, pending={self.parser.pending!r}"
                )
            if self._link_is_normal():
                return
        raise CommunicationTimeout(
            f"link did not return to a normal heartbeat within {self.response_timeout:.1f}s"
        )

    def _prepare_command(self) -> None:
        self._pace()
        frames = self._read_runtime(0.0)
        if frames or self.parser.pending:
            raise ProtocolError(
                "pre-command receive was not quiescent: "
                f"frames={frames!r}, pending={self.parser.pending!r}"
            )
        self._recover_runtime_link()

    def _finish_command(self, command: str) -> None:
        self._require_quiet_until(self.monotonic() + SETTLE_SECONDS, f"{command} settle")
        self._recover_runtime_link()

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
        self._prepare_command()
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
        self._finish_command(command)
        return ExchangeResult(command, tuple(frames), True, True, False)

    def execute_motion(self, spec: CommandSpec, *parameters: object) -> ExchangeResult:
        if not spec.motion:
            raise ValueError("execute_motion requires a motion CommandSpec")
        if not self.synchronized:
            raise ProtocolError("session must be synchronized before sending a command")
        command = build_command(spec, *parameters)
        self._prepare_command()
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
        self._finish_command(command)
        return ExchangeResult(command, tuple(frames), True, True, pause_seen)
