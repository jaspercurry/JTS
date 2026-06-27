# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Graft the relay transport onto the existing /correction/ measurement flow.

This is the host-mediated seam between the phone-mic capture relay and the
correction daemon (`jasper/web/correction_setup.py` + `jasper/correction/
session.py`). A relay-pulled, decrypted, verified WAV is fed into the SAME
analysis as today's same-origin browser upload — written to the session's
per-position capture path, then the host awaits
`MeasurementSession.on_capture_uploaded(path)` (the identical 48 kHz / mono /
32 MB contract).

Host-mediated indirection (docs/extensibility.md §1): this module NEVER plays
audio, touches CamillaDSP, or imports the correction daemon. The caller injects:
  - `on_armed` — fired once when the phone is recording; the host plays the
    stimulus through the existing `measurement_window()` + `prepare_and_play_
    sweep` machinery (so the loud-output safety + renderer/voice pause still
    apply), and
  - `play_cue` — optional; jasper-web has no cue channel today, so the daemon
    currently passes None (failures surface visibly on the capture page + the
    jts.local status page + `event=capture_relay.*` logs). Wiring an audible cue
    needs a jasper-web → jasper-voice cue bridge (a documented follow-up; the
    registry cues already exist).

Config (deploy-time, both required for the relay path; otherwise the on-Pi
same-origin capture is used):
  - JASPER_CAPTURE_RELAY_BASE — the relay origin the Pi pulls from.
  - JASPER_CAPTURE_ORIGIN — the trusted capture-page origin the tap-link points
    the phone at.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.capture_relay.client import RelayClient
from jasper.capture_relay.health import relay_base_from_env
from jasper.capture_relay.session import (
    PiCaptureSession,
    mint_session,
    purge,
    register_session,
    run_capture,
)
from jasper.capture_relay.spec import build_room_sweep_spec

ENV_CAPTURE_ORIGIN = "JASPER_CAPTURE_ORIGIN"
DEFAULT_CAPTURE_ORIGIN = "capture.jasper.tech"


def capture_origin_from_env(env: dict[str, str] | None = None) -> str:
    """The capture-page origin as a BARE host (no scheme).

    `PiCaptureSession.tap_link` always prepends `https://`, so we strip a scheme
    an operator may have pasted — `https://cap.example` must not become the dead
    link `https://https://cap.example`. (The sibling JASPER_CAPTURE_RELAY_BASE is
    scheme-bearing because the Pi-side client uses it as a full base URL; this one
    is a host the tap-link composes.)
    """
    source = env if env is not None else os.environ
    raw = (source.get(ENV_CAPTURE_ORIGIN) or DEFAULT_CAPTURE_ORIGIN).strip()
    raw = re.sub(r"^https?://", "", raw).rstrip("/")
    return raw or DEFAULT_CAPTURE_ORIGIN


def relay_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the relay capture path is available.

    It requires an operator-configured relay origin; absent it, /correction/
    keeps using today's on-Pi same-origin capture and the relay endpoint is inert
    (the existing flow is byte-identical). The capture origin always has a
    fleet-wide default, so only the relay base gates availability.
    """
    return relay_base_from_env(env) is not None


@dataclass(frozen=True)
class RelayCapture:
    """A registered relay capture: the Pi session + the phone tap-link."""

    pi_session: PiCaptureSession
    tap_link: str


def open_room_sweep_capture(
    client: RelayClient,
    *,
    position: int,
    total_positions: int,
    relay_base: str,
    capture_origin: str,
    ttl_s: int = 900,
) -> RelayCapture:
    """Mint + register a `room_sweep` relay capture for one measurement position.

    `position` is 1-based for display (matches the spec builder); the caller
    passes `measurement_session.current_position + 1`.
    """
    spec = build_room_sweep_spec(position=position, total_positions=total_positions)
    pi_session = mint_session(
        spec, relay_base=relay_base, capture_origin=capture_origin, ttl_s=ttl_s
    )
    register_session(client, pi_session)
    return RelayCapture(pi_session=pi_session, tap_link=pi_session.tap_link)


def run_and_store(
    client: RelayClient,
    pi_session: PiCaptureSession,
    capture_path: str | Path,
    *,
    on_armed: Callable[[], None],
    play_cue: Callable[[str], None] | None = None,
    **run_kwargs: Any,  # poll_interval_s / timeout_s / sleep / monotonic — run_capture validates
) -> Path:
    """Run the relay capture, write the verified WAV to `capture_path`, purge the
    relay session, and return the path. The caller then feeds it to the existing
    analysis: ``await measurement_session.on_capture_uploaded(path)`` — the same
    seam a same-origin ``/upload-capture`` POST uses.

    Raises loudly (CaptureTimeout / CaptureAborted / CaptureFailed / RelayError)
    exactly as `run_capture` does — the caller surfaces the failure on the page
    and (when wired) cues it.
    """
    wav = run_capture(
        client, pi_session, on_armed=on_armed, play_cue=play_cue, **run_kwargs
    )
    path = Path(capture_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav)
    # Best-effort delete now that we have the verified bytes (TTL is the backstop).
    purge(client, pi_session)
    return path
