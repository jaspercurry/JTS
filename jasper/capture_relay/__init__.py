# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Phone-mic capture relay — Pi-side transport for browser microphone capture.

This package is the Pi-side half of the phone-mic capture relay described in
[docs/phone-mic-relay-plan.md](../../docs/phone-mic-relay-plan.md). It moves the
browser microphone-capture page off the Pi (where a self-signed cert blocks
Android Chrome) and onto a trusted cloud origin, routing the recorded WAV back
to the Pi through a small, stateless, end-to-end-encrypted relay the Pi *pulls*
from. The downstream analysis is unchanged — a relay-pulled, decrypted, verified
WAV is fed into the same `jasper/web/correction_setup.py` pipeline as today's
same-origin upload, on the same 48 kHz / mono / 32 MB contract.

This module (`spec`) is build-order step 1: the **kind-agnostic capture-spec
contract**. The Pi builds a `CaptureSpec` for the active measurement kind; the
relay stores it as opaque bytes (it never parses it); the static page renders it
as DATA (never as code). Adding a new measurement kind therefore requires **zero
relay changes** — only a new Pi-side builder plus, occasionally, a new page
renderer component.

Public surface:
  - `CaptureSpec` — the frozen, kind-agnostic spec dataclass.
  - `build_room_sweep_spec(...)` — the step-1 builder for `kind="room_sweep"`.
  - `CaptureSpecError` — raised by strict, loud validation at the boundary.
  - `crypto` — Pi-side E2E decrypt + plaintext integrity (step 5): mint the
    content key, decrypt the relay-pulled blob, verify before analysis.
"""
from __future__ import annotations

from jasper.capture_relay import crypto
from jasper.capture_relay.client import RelayClient, RelayError
from jasper.capture_relay.crypto import (
    DecryptError,
    IntegrityError,
    decrypt_and_verify,
    generate_content_key,
)
from jasper.capture_relay.session import (
    CaptureFailed,
    CaptureTimeout,
    PiCaptureSession,
    mint_session,
    register_session,
    run_capture,
)
from jasper.capture_relay.spec import (
    BUILDERS,
    SHIPPED_KINDS,
    CaptureConstraints,
    CaptureSpec,
    CaptureSpecError,
    CaptureStimulus,
    CaptureValidity,
    build_balance_burst_spec,
    build_crossover_sweep_spec,
    build_room_sweep_spec,
    build_sync_marker_spec,
)

__all__ = [
    "BUILDERS",
    "SHIPPED_KINDS",
    "CaptureConstraints",
    "CaptureFailed",
    "CaptureSpec",
    "CaptureSpecError",
    "CaptureStimulus",
    "CaptureTimeout",
    "CaptureValidity",
    "DecryptError",
    "IntegrityError",
    "PiCaptureSession",
    "RelayClient",
    "RelayError",
    "build_balance_burst_spec",
    "build_crossover_sweep_spec",
    "build_room_sweep_spec",
    "build_sync_marker_spec",
    "crypto",
    "decrypt_and_verify",
    "generate_content_key",
    "mint_session",
    "register_session",
    "run_capture",
]
