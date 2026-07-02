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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from jasper.route_latency.pairing import TapEvent


DEFAULT_TAP_HOST = "127.0.0.1"
DEFAULT_TAP_PORT = 8781
DEFAULT_TAP_PATH = "/run/jasper-usbsink/impulse-tap.jsonl"
DEFAULT_HTTP_TIMEOUT_SECONDS = 5.0


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
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_TAP_HOST",
    "DEFAULT_TAP_PATH",
    "DEFAULT_TAP_PORT",
    "TapArmParams",
    "TapClient",
    "TapClientError",
    "TapStatus",
    "read_tap_events",
]
