# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Client for the Rust ingress tap's arm/disarm/status HTTP surface + its
JSONL event log.

The tap lives inside ``jasper-usbsink-audio`` (Rust; out of this package's
scope) on its existing ``127.0.0.1:8781`` listener, extending the same
hand-rolled HTTP handler that already serves ``GET /status`` and the
preempt endpoints. This module is the harness's *only* interface to that
process: it never imports Rust code or touches the crate directly, only
speaks the pinned HTTP + JSONL contract.

Pinned contract (see the Stage 0 architecture brief for the authoritative
version):

* ``POST /tap/arm`` — body ``{"threshold":..,"hysteresis":..,
  "refractory_ms":..,"max_events":..,"auto_disarm_min":..,"path":..}``
  (all optional). Truncates the JSONL, resets counters, arms the detector.
* ``POST /tap/disarm`` — clears armed state.
* ``GET /tap`` — current armed state + counters + path.
* JSONL event schema (one object per line):
  ``{"monotonic_ns":..,"frame_index":..,"ring_fill_frames":..,"peak":..}``.

Uses stdlib ``urllib.request`` rather than ``requests`` — this is a single
localhost JSON round-trip, not a general HTTP client need, and keeping this
module import-cheap (no third-party import at module load) matters for a
CLI that most operators invoke for a quick `arm`/`disarm` one-liner.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from jasper.route_latency.pairing import TapEvent


DEFAULT_TAP_HOST = "127.0.0.1"
DEFAULT_TAP_PORT = 8781
DEFAULT_TAP_PATH = "/run/jasper-usbsink/impulse-tap.jsonl"
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0

# The fan-in DIRECT-capture tap (the combo-box ingress). On a USB combo box
# (JASPER_FANIN_USB_DIRECT=enabled) the certified route's ingress is fan-in's
# hw:UAC2Gadget DIRECT capture, so the impulse tap lives in jasper-fanin, NOT the
# usbsink bridge — the bridge stands down and opens no capture, so its :8781 tap
# never fires. fan-in exposes the tap over its control UDS with plaintext verbs
# (not HTTP). These MUST match the Rust side: DEFAULT_TAP_PATH / the control
# socket in rust/jasper-fanin/src/impulse_tap.rs + config.rs. FANIN_CONTROL_SOCKET
# is the SAME socket jasper.route_latency.status_socket reads STATUS from
# (FANIN_STATUS_SOCKET); pinned equal by the tap-transport contract test.
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


@dataclass(frozen=True)
class TapStatus:
    """Parsed ``GET /tap`` response."""

    armed: bool
    events_written: int
    events_dropped: int
    path: str
    raw: dict[str, object]


def _coerce_int(value: object) -> int:
    """Best-effort int coercion for a JSON-decoded value of unknown shape.

    A malformed/missing counter becomes 0 rather than raising — a status
    read is diagnostics, not a contract the harness should crash on.
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


@runtime_checkable
class TapArmer(Protocol):
    """The arm/disarm surface both tap transports satisfy.

    Two implementations speak two different wires to two different daemons —
    :class:`TapClient` (usbsink bridge, HTTP :8781) and :class:`FaninTapClient`
    (fan-in DIRECT-capture tap, control UDS) — so the harness can drive whichever
    tap is actually live on the box behind one interface. ``arm`` raises
    :class:`TapClientError` on any failure (a run with a tap that never armed
    produces zero ingress evidence — the "refuse to certify" case)."""

    def arm(self, params: TapArmParams | None = None) -> dict[str, object]: ...

    def disarm(self) -> dict[str, object]: ...


class TapClient:
    """Thin HTTP client for the Rust tap's arm/disarm/status endpoints."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_TAP_HOST,
        port: int = DEFAULT_TAP_PORT,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._timeout_seconds = timeout_seconds

    def _request(self, method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TapClientError(
                f"{method} {path} failed against {self._base_url} — is "
                "jasper-usbsink-audio running with the 8781 listener? "
                f"({e})"
            ) from e
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise TapClientError(f"{method} {path} returned invalid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise TapClientError(f"{method} {path} response root is not an object")
        return parsed

    def arm(self, params: TapArmParams | None = None) -> dict[str, object]:
        return self._request("POST", "/tap/arm", (params or TapArmParams()).to_body())

    def disarm(self) -> dict[str, object]:
        return self._request("POST", "/tap/disarm")

    def status(self) -> TapStatus:
        raw = self._request("GET", "/tap")
        return TapStatus(
            armed=bool(raw.get("armed", False)),
            events_written=_coerce_int(raw.get("events_written")),
            events_dropped=_coerce_int(raw.get("events_dropped")),
            path=str(raw.get("path", "")),
            raw=raw,
        )


def _coerce_int_or_str(value: str) -> object:
    """Coerce a control-socket reply token's value to int when it parses as one,
    else leave it a string (a path stays a path, the counters become numbers)."""

    try:
        return int(value)
    except ValueError:
        return value


def parse_tap_socket_reply(reply: str) -> dict[str, object]:
    """Parse a fan-in tap control-socket reply line into a dict.

    The fan-in tap replies plaintext (unlike the usbsink HTTP tap's JSON), so
    this normalizes both replies into a dict the caller can treat like the HTTP
    one:

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

    The combo-box counterpart to :class:`TapClient`. On a USB combo box the
    certified route's ingress is fan-in's ``hw:UAC2Gadget`` DIRECT capture, and
    the impulse tap lives in ``jasper-fanin`` (``rust/jasper-fanin/src/
    impulse_tap.rs``), NOT the usbsink bridge (which stands down and opens no
    capture, so its :8781 HTTP tap never fires — arming it against known-good
    combo audio records zero detections, the reported bug). fan-in exposes the
    tap over its existing control socket as plaintext line verbs, not HTTP:

    * ``TAP_ARM {json}\\n`` — the SAME JSON body / validation / ceilings /
      ``/run/jasper-fanin/`` path-constraint as the usbsink HTTP ``/tap/arm``
      (both parse ``TapConfig::from_arm_body``); reply is a plaintext line
      ``OK armed path=<path>`` or ``ERR <reason>``.
    * ``TAP_DISARM\\n`` — reply ``OK disarmed events_written=N events_dropped=M``.

    Speaks the same ``AF_UNIX`` mechanic :mod:`jasper.route_latency.status_socket`
    uses for ``STATUS`` — connect, send one line, read the reply to EOF (fan-in
    writes the reply then drops the stream) — but with the tap verbs and a
    plaintext reply it parses via :func:`parse_tap_socket_reply`. Satisfies
    :class:`TapArmer` so the harness drives it exactly like the HTTP client.
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
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_TAP_HOST",
    "DEFAULT_TAP_PATH",
    "DEFAULT_TAP_PORT",
    "FANIN_CONTROL_SOCKET",
    "FANIN_DEFAULT_TAP_PATH",
    "FaninTapClient",
    "TapArmParams",
    "TapArmer",
    "TapClient",
    "TapClientError",
    "TapStatus",
    "parse_tap_socket_reply",
    "read_tap_events",
]
