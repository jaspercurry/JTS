# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side capture-session orchestrator (phone-mic relay step 4).

Mints a session, registers it with the relay, renders the tap-link the household
opens on their phone, then polls the relay and — when the phone uploads —
pulls, decrypts, and verifies the WAV. It returns plain WAV bytes; it never
touches CamillaDSP, playback, or the correction daemon directly. The host owns
those (host-mediated indirection, docs/extensibility.md §1):

  - the host plays the stimulus via the injected `on_armed` callback (fired once,
    when the phone's `armed` flag first appears on a poll), and
  - the host feeds the returned, verified WAV into the existing analysis
    (`correction_setup.py`'s pipeline — same 48 kHz / mono / 32 MB contract).

This keeps the transport reusable across room_sweep / balance / sync / crossover
and trivially testable with a fake relay.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from jasper.capture_relay.client import RelayClient
from jasper.capture_relay.cues import classify_failure_cue
from jasper.capture_relay.crypto import (
    content_key_to_b64url,
    decrypt_and_verify,
    generate_content_key,
)
from jasper.capture_relay.spec import CaptureSpec
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 900
DEFAULT_POLL_INTERVAL_S = 0.75
DEFAULT_TIMEOUT_S = 120.0


class CaptureTimeout(RuntimeError):
    """The phone never uploaded a ready blob within the timeout."""


class CaptureFailed(RuntimeError):
    """The relay-pulled blob failed decrypt or integrity (see __cause__)."""


class CaptureAborted(RuntimeError):
    """The phone aborted mid-capture (e.g. backgrounded / screen locked)."""


@dataclass(frozen=True)
class PiCaptureSession:
    """One phone-mic capture, identified to the relay and to the phone link."""

    session_id: str
    content_key: bytes
    upload_token: str
    pull_token: str
    spec: CaptureSpec
    relay_base: str
    capture_origin: str
    ttl_s: int = DEFAULT_TTL_S

    @property
    def tap_link(self) -> str:
        """The single URL the household opens on their phone.

        The content_key rides the FRAGMENT (`#…`) — the one part of a URL
        browsers never transmit — so the relay never receives it. So do the
        session id and upload token, keeping them out of any relay log.
        """
        key_b64 = content_key_to_b64url(self.content_key)
        origin = self.capture_origin.rstrip("/")
        return f"https://{origin}/#s={self.session_id}&u={self.upload_token}&k={key_b64}"

    def capture_spec_json(self) -> str:
        return json.dumps(self.spec.to_dict(), separators=(",", ":"))


def mint_session(
    spec: CaptureSpec,
    *,
    relay_base: str,
    capture_origin: str,
    ttl_s: int = DEFAULT_TTL_S,
) -> PiCaptureSession:
    """Mint a session with CSPRNG ids/key/tokens (plan §11)."""
    return PiCaptureSession(
        session_id="cap_" + secrets.token_urlsafe(16),
        content_key=generate_content_key(),
        upload_token=secrets.token_urlsafe(32),
        pull_token=secrets.token_urlsafe(32),
        spec=spec,
        relay_base=relay_base.rstrip("/"),
        capture_origin=capture_origin,
        ttl_s=ttl_s,
    )


def register_session(client: RelayClient, session: PiCaptureSession) -> dict:
    """Register the session + opaque spec with the relay."""
    result = client.register(
        session_id=session.session_id,
        capture_spec_json=session.capture_spec_json(),
        upload_token=session.upload_token,
        pull_token=session.pull_token,
        ttl_s=session.ttl_s,
        max_upload_bytes=session.spec.max_upload_bytes,
    )
    # session_id is a CSPRNG id, not a secret; tokens/keys are never logged.
    log_event(
        logger,
        "capture_relay.registered",
        session_id=session.session_id,
        kind=session.spec.kind,
        ttl_s=session.ttl_s,
    )
    return result


@dataclass(frozen=True)
class PollState:
    armed: bool
    ready: bool
    integrity: dict | None
    aborted: bool = False
    abort_reason: str = ""


def classify_status(status_payload: dict) -> PollState:
    """Read the relay status into the signals the Pi acts on."""
    event = status_payload.get("event") if isinstance(status_payload, dict) else None
    event = event if isinstance(event, dict) else {}
    armed = bool(event.get("armed"))
    aborted = bool(event.get("aborted"))
    abort_reason = str(event.get("abort_reason") or event.get("reason") or "")
    ready = status_payload.get("state") == "ready"
    integrity = status_payload.get("integrity")
    return PollState(
        armed=armed,
        ready=ready,
        integrity=integrity,
        aborted=aborted,
        abort_reason=abort_reason,
    )


def run_capture(
    client: RelayClient,
    session: PiCaptureSession,
    *,
    on_armed: Callable[[], None],
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    play_cue: Callable[[str], None] | None = None,
) -> bytes:
    """Poll the relay until the phone uploads; pull, decrypt, verify, return WAV.

    `on_armed` fires exactly once, when the phone's `armed` flag is first seen —
    the host plays the stimulus then. Raises loudly — never a silent hang or a
    silently-wrong measurement — on:
      - `CaptureTimeout`: no ready blob within `timeout_s`;
      - `CaptureAborted`: the phone posted an `aborted` event (backgrounded);
      - `CaptureFailed`: the pulled blob failed decrypt/integrity;
      - `RelayError` / `OSError`: the relay died or became unreachable mid-poll.
    `play_cue` (host-injected, no-silent-failure) is called with the matching cue
    slug before ANY of those propagate — so a host that passes `play_cue` gets a
    complete no-silent-failure contract and need not re-cue run_capture failures
    itself (the cue for a connectivity loss is `measurement_relay_unreachable`).
    """
    try:
        return _poll_until_capture(
            client,
            session,
            on_armed=on_armed,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s,
            sleep=sleep,
            monotonic=monotonic,
        )
    except Exception as exc:  # noqa: BLE001 — cue on ANY failure, then re-raise
        slug = classify_failure_cue(exc)
        # Operator-facing half of no-silent-failure: a WARNING with the failure
        # type + cue slug, plus the traceback (so an *unexpected* error — e.g. a
        # bug in the host's on_armed/stimulus playback — is diagnosable even
        # though the household only hears the generic measurement_failed cue).
        log_event(
            logger,
            "capture_relay.failed",
            level=logging.WARNING,
            exc_info=True,
            session_id=session.session_id,
            reason=type(exc).__name__,
            cue=slug,
        )
        if play_cue is not None:
            try:
                play_cue(slug)
            except Exception:  # noqa: BLE001 — the cue is best-effort
                pass
        raise


def _poll_until_capture(
    client: RelayClient,
    session: PiCaptureSession,
    *,
    on_armed: Callable[[], None],
    poll_interval_s: float,
    timeout_s: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> bytes:
    deadline = monotonic() + timeout_s
    armed_fired = False
    while True:
        status = client.status(session.session_id, session.pull_token)
        state = classify_status(status)

        if state.aborted:
            raise CaptureAborted(
                f"phone aborted the capture ({state.abort_reason or 'no reason'})"
            )

        if state.armed and not armed_fired:
            armed_fired = True
            log_event(logger, "capture_relay.armed", session_id=session.session_id)
            on_armed()

        if state.ready:
            log_event(logger, "capture_relay.ready", session_id=session.session_id)
            blob, header_integrity = client.pull_blob(
                session.session_id, session.pull_token
            )
            integrity = state.integrity or header_integrity
            try:
                expected_len = int(integrity["plaintext_len"])
                expected_sha = str(integrity["sha256"])
                wav = decrypt_and_verify(
                    session.content_key, blob, expected_len, expected_sha
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CaptureFailed(
                    "relay-pulled capture failed decrypt/integrity"
                ) from exc
            log_event(
                logger,
                "capture_relay.captured",
                session_id=session.session_id,
                wav_bytes=len(wav),
            )
            return wav

        if monotonic() >= deadline:
            raise CaptureTimeout(
                f"phone never uploaded within {timeout_s:.0f}s (session "
                f"{session.session_id})"
            )
        sleep(poll_interval_s)


def purge(client: RelayClient, session: PiCaptureSession) -> None:
    """Delete the session from the relay after a verified pull (best-effort —
    the short TTL is the backstop)."""
    try:
        client.delete(session.session_id, session.pull_token)
    except Exception:  # noqa: BLE001 — purge is best-effort; TTL self-cleans
        pass
