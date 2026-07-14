# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Client for the fan-in DIRECT-capture ingress tap's arm/disarm surface + its
JSONL event log.

The USB ingress tap lives in ``jasper-fanin`` (Rust; ``rust/jasper-fanin/src/
impulse_tap.rs``): fan-in DIRECT-captures ``hw:UAC2Gadget`` and taps the impulse
there, armed over its control UDS with plaintext line verbs. This module never
imports Rust code — it only speaks the pinned control-socket + JSONL contract.

The historical usbsink-bridge HTTP tap (``127.0.0.1:8781``) was removed with the
aloop solo capture path; the bridge process itself has now been retired.

Pinned contract (matches ``TapConfig::from_arm_body`` / the tap verb serializers
in the Rust fan-in crate):

* ``TAP_ARM {json}`` — body ``{"threshold":..,"hysteresis":..,
  "refractory_ms":..,"max_events":..,"auto_disarm_min":..,"path":..}``
  (all optional). Truncates the JSONL, resets counters, arms the detector.
  Reply ``OK armed path=<path>`` or ``ERR <reason>``.
* ``TAP_DISARM`` — clears armed state. Reply
  ``OK disarmed events_written=N events_dropped=M``.
* JSONL event schema (one object per line):
  ``{"monotonic_ns":..,"frame_index":..,"ring_fill_frames":..,"peak":..}``.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from jasper.route_latency.pairing import TapEvent


# The fan-in DIRECT-capture tap (the only USB ingress tap). fan-in exposes it over
# its control UDS with plaintext verbs. These MUST match the Rust side: the control
# socket + default JSONL path in rust/jasper-fanin/src/impulse_tap.rs + config.rs.
# FANIN_CONTROL_SOCKET is the SAME socket jasper.route_latency.status_socket reads
# STATUS from (FANIN_STATUS_SOCKET); pinned equal by the tap-transport contract test.
FANIN_CONTROL_SOCKET = "/run/jasper-fanin/control.sock"
FANIN_DEFAULT_TAP_PATH = "/run/jasper-fanin/impulse-tap.jsonl"
DEFAULT_FANIN_TAP_TIMEOUT_SECONDS = 5.0


class TapClientError(RuntimeError):
    """Raised when the tap HTTP surface is unreachable or returns an error.

    Callers must treat this as a hard failure of the arm/disarm step — a
    measurement run with a tap that never armed produces zero ingress
    evidence, which is exactly the "refuse to certify" case this harness
    exists to enforce.
    """


@dataclass(frozen=True)
class TapArmParams:
    """Optional arm-request parameters; unset fields use the Rust side's
    documented defaults (threshold=0.2, hysteresis=0.05, refractory_ms=250,
    max_events=4000, auto_disarm_min=45)."""

    threshold: float | None = None
    hysteresis: float | None = None
    refractory_ms: float | None = None
    max_events: int | None = None
    auto_disarm_min: float | None = None
    path: str | None = None

    def to_body(self) -> dict[str, object]:
        body: dict[str, object] = {}
        if self.threshold is not None:
            body["threshold"] = self.threshold
        if self.hysteresis is not None:
            body["hysteresis"] = self.hysteresis
        # The three integer-valued knobs are coerced to int so a CLI arg typed
        # with `type=float` (e.g. --tap-refractory-ms 300 → 300.0) serializes as
        # `300`, not `300.0`. The Rust side parses these with `as_u64()`, which
        # returns None for a JSON float — so a float here 400s the arm request
        # for the entire measurement window. `round()` (not `int()`) so an
        # operator's `250.7` lands on the nearest ms rather than truncating; the
        # Rust side ALSO accepts integral floats now (defense on both sides), but
        # emitting a clean int keeps the wire honest and the Rust parser strict.
        if self.refractory_ms is not None:
            body["refractory_ms"] = round(self.refractory_ms)
        if self.max_events is not None:
            body["max_events"] = round(self.max_events)
        if self.auto_disarm_min is not None:
            body["auto_disarm_min"] = round(self.auto_disarm_min)
        if self.path is not None:
            body["path"] = self.path
        return body


@runtime_checkable
class TapArmer(Protocol):
    """The arm/disarm surface the tap client satisfies.

    :class:`FaninTapClient` (fan-in DIRECT-capture tap, control UDS) is the sole
    implementation since the usbsink-bridge HTTP tap was removed. ``arm`` raises
    :class:`TapClientError` on any failure (a run with a tap that never armed
    produces zero ingress evidence — the "refuse to certify" case)."""

    def arm(self, params: TapArmParams | None = None) -> dict[str, object]: ...

    def disarm(self) -> dict[str, object]: ...


def _coerce_int_or_str(value: str) -> object:
    """Coerce a control-socket reply token's value to int when it parses as one,
    else leave it a string (a path stays a path, the counters become numbers)."""

    try:
        return int(value)
    except ValueError:
        return value


def parse_tap_socket_reply(reply: str) -> dict[str, object]:
    """Parse a fan-in tap control-socket reply line into a dict.

    The fan-in tap replies plaintext, so this normalizes the reply into a dict:

    * ``OK armed path=<p>`` -> ``{"ok": True, "status": "armed", "path": <p>}``
    * ``OK disarmed events_written=N events_dropped=M`` ->
      ``{"ok": True, "status": "disarmed", "events_written": N,
      "events_dropped": M}``
    * ``ERR <reason>`` / empty / anything else -> ``{"ok": False, ...}``

    ``key=value`` tokens split on the first ``=`` (a ``/run`` path carries no
    spaces, so it stays one token); a value that parses as an int is coerced so
    counters come back as numbers. ``reply`` keeps the raw line for the error
    path. The reply shapes are pinned to the Rust ``tap_arm_command`` /
    ``tap_disarm_command`` serializers by the tap-transport contract test.
    """

    text = reply.strip()
    tokens = text.split()
    out: dict[str, object] = {"ok": bool(tokens) and tokens[0] == "OK", "reply": text}
    for token in tokens[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            out[key] = _coerce_int_or_str(value)
        else:
            out.setdefault("status", token)
    return out


class FaninTapClient:
    """Arm/disarm the fan-in DIRECT-capture impulse tap over its control UDS.

    The sole USB ingress tap. fan-in DIRECT-captures ``hw:UAC2Gadget`` and the
    impulse tap lives in ``jasper-fanin`` (``rust/jasper-fanin/src/
    impulse_tap.rs``); the old usbsink-bridge HTTP tap (:8781) was removed with the
    aloop solo path. fan-in exposes the tap over its existing control socket as
    plaintext line verbs, not HTTP:

    * ``TAP_ARM {json}\\n`` — JSON body / validation / ceilings /
      ``/run/jasper-fanin/`` path-constraint parsed by ``TapConfig::from_arm_body``;
      reply is a plaintext line ``OK armed path=<path>`` or ``ERR <reason>``.
    * ``TAP_DISARM\\n`` — reply ``OK disarmed events_written=N events_dropped=M``.

    Speaks the same ``AF_UNIX`` mechanic :mod:`jasper.route_latency.status_socket`
    uses for ``STATUS`` — connect, send one line, read the reply to EOF (fan-in
    writes the reply then drops the stream) — with the tap verbs and a plaintext
    reply it parses via :func:`parse_tap_socket_reply`. Satisfies :class:`TapArmer`.
    """

    def __init__(
        self,
        *,
        socket_path: str = FANIN_CONTROL_SOCKET,
        timeout_seconds: float = DEFAULT_FANIN_TAP_TIMEOUT_SECONDS,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    def _command(self, line: str, *, verb: str) -> dict[str, object]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout_seconds)
                sock.connect(self._socket_path)
                sock.sendall(line.encode("utf-8"))
                # Half-close the write side so the daemon sees end-of-request
                # even if it ever reads to EOF; harmless with its line reader.
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                chunks: list[bytes] = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except (OSError, TimeoutError) as e:
            raise TapClientError(
                f"{verb} failed against {self._socket_path} — is jasper-fanin "
                f"running with its control socket? ({e})"
            ) from e
        reply = b"".join(chunks).decode("utf-8", errors="replace")
        parsed = parse_tap_socket_reply(reply)
        if not parsed.get("ok"):
            raise TapClientError(
                f"{verb} on {self._socket_path} returned: "
                f"{parsed.get('reply') or '<empty reply>'}"
            )
        return parsed

    def arm(self, params: TapArmParams | None = None) -> dict[str, object]:
        body = json.dumps((params or TapArmParams()).to_body())
        return self._command(f"TAP_ARM {body}\n", verb="TAP_ARM")

    def disarm(self) -> dict[str, object]:
        return self._command("TAP_DISARM\n", verb="TAP_DISARM")


def read_tap_events(path: Path) -> list[TapEvent]:
    """Parse the tap's JSONL event log into :class:`TapEvent` objects.

    Malformed trailing lines (a JSONL file read mid-write by the publisher
    thread can have a partial final line) are skipped rather than raising —
    every earlier complete line is still valid evidence.
    """

    events: list[TapEvent] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        try:
            events.append(
                TapEvent(
                    monotonic_ns=int(obj["monotonic_ns"]),
                    frame_index=int(obj["frame_index"]),
                    ring_fill_frames=int(obj["ring_fill_frames"]),
                    peak=float(obj["peak"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return events


__all__ = [
    "DEFAULT_FANIN_TAP_TIMEOUT_SECONDS",
    "FANIN_CONTROL_SOCKET",
    "FANIN_DEFAULT_TAP_PATH",
    "FaninTapClient",
    "TapArmParams",
    "TapArmer",
    "TapClientError",
    "parse_tap_socket_reply",
    "read_tap_events",
]
