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

import inspect
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jasper.capture_relay.client import RelayClient, RelayError
from jasper.capture_relay.cues import classify_failure_cue
from jasper.capture_relay.crypto import (
    content_key_to_b64url,
    decrypt_and_verify,
    generate_content_key,
)
from jasper.capture_relay.integrity import (
    CaptureIntegrityError,
    capture_spec_mac,
    verify_authenticated_phone_event,
)
from jasper.capture_relay.spec import CaptureSpec
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 900
DEFAULT_POLL_INTERVAL_S = 0.75
DEFAULT_TIMEOUT_S = 120.0


class CaptureTimeout(RuntimeError):
    """The phone failed one bounded relay phase before capture completed."""

    def __init__(self, message: str, *, phase: str | None = None) -> None:
        super().__init__(message)
        self.phase = phase


class CaptureFailed(RuntimeError):
    """The relay-pulled blob failed decrypt or integrity (see __cause__)."""


class CaptureAborted(RuntimeError):
    """The phone aborted mid-capture (e.g. backgrounded / screen locked)."""


class CapturePageIncompatible(RuntimeError):
    """The public capture page does not implement this Pi's protocol."""


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
        spec_mac = capture_spec_mac(
            self.content_key,
            self.session_id,
            self.capture_spec_json(),
        )
        origin = self.capture_origin.rstrip("/")
        return (
            f"https://{origin}/#s={self.session_id}&u={self.upload_token}"
            f"&k={key_b64}&a={spec_mac}"
        )

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
    device: dict | None = None
    noise_floor: dict | None = None
    setup: dict | None = None
    setup_identity: dict | None = None
    setup_validate: bool = False
    setup_token: str = ""
    capture_page: dict | None = None
    acknowledgement: dict | None = None


@dataclass(frozen=True)
class CaptureResult:
    """A verified relay capture: the WAV plus the phone-reported capture device.

    `device` is the small, NON-secret metadata the phone posts in its `armed`
    event (e.g. `{"label": "UMIK-1", "device_id": "..."}`) — it rides the opaque
    event channel (the relay passes it through without parsing), NOT the E2E WAV
    blob. The Pi uses it to decide whether a loaded mic calibration applies (a
    phone built-in mic ⇒ refuse a vendor curve; the matching USB measurement mic ⇒
    apply it). `None` when the phone reports no device (older capture page)."""

    wav: bytes
    device: dict | None = None
    noise_floor: dict | None = None
    setup: dict | None = None


class PhoneEventVerifier:
    """Verify the relay's mutable phone-event slot before host code reads it."""

    def __init__(self, session: PiCaptureSession) -> None:
        self._session = session
        self._sequence = 0
        self._event: dict[str, Any] | None = None

    def verify(self, relay_event: Any) -> dict[str, Any] | None:
        if relay_event is None:
            return None
        if self._session.spec.capture_protocol_version < 2:
            return dict(relay_event) if isinstance(relay_event, dict) else None
        verified, sequence = verify_authenticated_phone_event(
            self._session.content_key,
            self._session.session_id,
            relay_event,
        )
        if sequence < self._sequence or (
            sequence == self._sequence
            and self._event is not None
            and verified != self._event
        ):
            raise CaptureIntegrityError(
                "authenticated phone event sequence moved backwards"
            )
        self._sequence = sequence
        self._event = verified
        return verified


class CaptureActivityProbe:
    """Fail closed when a long host stimulus outlives the phone recorder.

    ``run_capture`` normally polls phone state itself, but its synchronous
    ``on_armed`` callback may run a host stimulus for several seconds.  A
    caller with a long callback can poll through this probe while that callback
    is active.  The mutable event slot is authenticated and sequence-checked
    exactly like the main capture loop before an abort can affect playback.
    """

    def __init__(self, client: RelayClient, session: PiCaptureSession) -> None:
        if session.spec.capture_protocol_version < 2:
            raise CaptureFailed(
                "abort-aware host playback requires authenticated capture protocol v2"
            )
        self._client = client
        self._session = session
        self._verifier = PhoneEventVerifier(session)
        self._lock = threading.Lock()

    def assert_active(self) -> None:
        with self._lock:
            status = self._client.status(
                self._session.session_id, self._session.pull_token
            )
            relay_event = status.get("event") if isinstance(status, dict) else None
            verified_event = self._verifier.verify(relay_event)
            status = {**status, "event": verified_event}
            state = classify_status(status)
            if state.aborted:
                raise CaptureAborted(
                    f"phone aborted the capture ({state.abort_reason or 'no reason'})"
                )
            if state.ready:
                raise CaptureAborted(
                    "phone capture ended before host playback completed"
                )
            if not state.armed:
                raise CaptureFailed("phone recorder is no longer armed")


def classify_status(status_payload: dict) -> PollState:
    """Read the relay status into the signals the Pi acts on."""
    event = status_payload.get("event") if isinstance(status_payload, dict) else None
    event = event if isinstance(event, dict) else {}
    armed = bool(event.get("armed"))
    aborted = bool(event.get("aborted"))
    abort_reason = str(event.get("abort_reason") or event.get("reason") or "")
    device = event.get("device") if isinstance(event.get("device"), dict) else None
    noise_floor = (
        event.get("noise_floor")
        if isinstance(event.get("noise_floor"), dict)
        else None
    )
    setup = event.get("setup") if isinstance(event.get("setup"), dict) else None
    setup_identity = (
        event.get("setup_identity")
        if isinstance(event.get("setup_identity"), dict)
        else None
    )
    setup_validate = bool(event.get("setup_validate"))
    setup_token = str(event.get("setup_token") or "")
    capture_page = (
        event.get("capture_page")
        if isinstance(event.get("capture_page"), dict)
        else None
    )
    acknowledgement = (
        event.get("acknowledgement")
        if isinstance(event.get("acknowledgement"), dict)
        else None
    )
    ready = status_payload.get("state") == "ready"
    integrity = status_payload.get("integrity")
    return PollState(
        armed=armed,
        ready=ready,
        integrity=integrity,
        aborted=aborted,
        abort_reason=abort_reason,
        device=device,
        noise_floor=noise_floor,
        setup=setup,
        setup_identity=setup_identity,
        setup_validate=setup_validate,
        setup_token=setup_token,
        capture_page=capture_page,
        acknowledgement=acknowledgement,
    )


def validate_capture_page(
    identity: dict | None,
    spec: CaptureSpec,
) -> dict:
    """Validate the phone page identity before any host callback can play audio."""
    observed = identity if isinstance(identity, dict) else {}
    protocol = observed.get("capture_protocol_version")
    supported = observed.get("supported_capture_protocol_versions")
    build = observed.get("capture_page_build")
    if (
        observed.get("schema_version") != 1
        or isinstance(protocol, bool)
        or not isinstance(protocol, int)
        or not isinstance(supported, list)
        or spec.capture_protocol_version not in supported
        or protocol not in supported
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in supported
        )
        or not isinstance(build, str)
        or not re.fullmatch(r"[0-9]{8}\.[0-9]+", build)
    ):
        raise CapturePageIncompatible(
            "capture page is incompatible with this speaker "
            f"(expected protocol {spec.capture_protocol_version}, "
            f"observed {protocol!r}, build {build!r})"
        )
    return observed


def validate_capture_acknowledgement(
    state: PollState,
    spec: CaptureSpec,
) -> dict | None:
    """Verify a spec-bound operator acknowledgement before host playback."""

    required = spec.acknowledgement
    if required is None:
        return None
    observed = state.acknowledgement or {}
    valid = (
        observed.get("schema_version") == required.schema_version
        and observed.get("accepted") is True
        and isinstance(observed.get("id"), str)
        and isinstance(observed.get("binding_id"), str)
        and secrets.compare_digest(observed.get("id", ""), required.id)
        and secrets.compare_digest(
            observed.get("binding_id", ""),
            required.binding_id,
        )
    )
    if not valid:
        raise CaptureFailed(
            "required microphone-placement acknowledgement is missing or stale"
        )
    return {
        "schema_version": required.schema_version,
        "id": required.id,
        "accepted": True,
    }


def _call_state_callback(callback: Callable[..., None], state: PollState) -> None:
    """Call old zero-arg or new state-aware callbacks.

    Existing tests and sibling relay kinds used ``on_armed()``. Room correction's
    guided relay flow now also has state-aware ``on_setup(state)`` and
    ``on_armed(state)`` callbacks. Supporting both keeps the seam additive.
    """
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        callback(state)
        return
    required = [
        p for p in sig.parameters.values()
        if p.default is inspect.Signature.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if required:
        callback(state)
    else:
        callback()


def run_capture(
    client: RelayClient,
    session: PiCaptureSession,
    *,
    on_armed: Callable[..., None],
    on_setup: Callable[..., None] | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    play_cue: Callable[[str], None] | None = None,
) -> CaptureResult:
    """Poll the relay until the phone uploads; pull, decrypt, verify, return it.

    Returns a `CaptureResult` (the verified WAV + the phone-reported capture
    device). The device rides the opaque `armed` event, not the E2E WAV blob.

    `on_armed` fires exactly once, when the phone's `armed` flag is first seen —
    the host plays the stimulus then. Raises loudly — never a silent hang or a
    silently-wrong measurement — on:
      - `CaptureTimeout`: no arm within the initial `timeout_s`, or no ready
        blob within one refreshed `timeout_s` window after arming;
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
            on_setup=on_setup,
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
        failure_fields: dict[str, Any] = {}
        if isinstance(exc, CaptureTimeout) and exc.phase is not None:
            failure_fields["phase"] = exc.phase
        log_event(
            logger,
            "capture_relay.failed",
            level=logging.WARNING,
            exc_info=True,
            session_id=session.session_id,
            reason=type(exc).__name__,
            cue=slug,
            **failure_fields,
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
    on_armed: Callable[..., None],
    on_setup: Callable[..., None] | None,
    poll_interval_s: float,
    timeout_s: float,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> CaptureResult:
    deadline = monotonic() + timeout_s
    armed_fired = False
    capture_device: dict | None = None
    capture_noise_floor: dict | None = None
    capture_setup: dict | None = None
    setup_tokens_seen: set[str] = set()
    page_compatible = False
    event_verifier = PhoneEventVerifier(session)
    while True:
        status = client.status(session.session_id, session.pull_token)
        relay_event = status.get("event") if isinstance(status, dict) else None
        if session.spec.capture_protocol_version >= 2 and relay_event is not None:
            try:
                verified_event = event_verifier.verify(relay_event)
            except CaptureIntegrityError as exc:
                log_event(
                    logger,
                    "capture_relay.phone_event_integrity_failed",
                    level=logging.WARNING,
                    session_id=session.session_id,
                    kind=session.spec.kind,
                    reason=str(exc),
                )
                try:
                    client.post_host_event(
                        session.session_id,
                        session.pull_token,
                        {
                            "phase": "capture_incompatible",
                            "error": "capture control integrity check failed",
                        },
                    )
                except (OSError, RelayError):
                    logger.warning(
                        "could not publish capture integrity failure",
                        exc_info=True,
                    )
                raise CaptureFailed(
                    "capture control integrity check failed"
                ) from exc
            status = {**status, "event": verified_event}
        state = classify_status(status)
        if state.device is not None:
            capture_device = state.device  # phone-reported mic; persists to ready
        if state.noise_floor is not None:
            capture_noise_floor = state.noise_floor
        if state.setup is not None:
            capture_setup = state.setup

        if state.aborted:
            raise CaptureAborted(
                f"phone aborted the capture ({state.abort_reason or 'no reason'})"
            )

        # The phone page is independently deployed. Establish this contract
        # before setup callbacks or `on_armed` can reach an audio player. A
        # stale page therefore fails visibly and cannot start a tone.
        if not page_compatible and (
            state.setup_validate or state.armed or state.ready
        ):
            try:
                validate_capture_page(state.capture_page, session.spec)
            except CapturePageIncompatible as exc:
                log_event(
                    logger,
                    "capture_relay.page_incompatible",
                    level=logging.WARNING,
                    session_id=session.session_id,
                    expected_protocol=session.spec.capture_protocol_version,
                    observed_protocol=(state.capture_page or {}).get(
                        "capture_protocol_version"
                    ),
                    observed_build=(state.capture_page or {}).get(
                        "capture_page_build"
                    ),
                )
                try:
                    client.post_host_event(
                        session.session_id,
                        session.pull_token,
                        {
                            "phase": "capture_incompatible",
                            "error": str(exc),
                            "expected_protocol": session.spec.capture_protocol_version,
                        },
                    )
                except (OSError, RelayError):
                    logger.warning(
                        "could not publish capture-page incompatibility",
                        exc_info=True,
                    )
                raise
            page_compatible = True
            log_event(
                logger,
                "capture_relay.page_compatible",
                session_id=session.session_id,
                protocol=session.spec.capture_protocol_version,
                page_build=(state.capture_page or {}).get("capture_page_build"),
            )

        if (
            on_setup is not None
            and state.setup_validate
            and state.setup is not None
            and state.setup_token
            and state.setup_token not in setup_tokens_seen
        ):
            setup_tokens_seen.add(state.setup_token)
            log_event(
                logger,
                "capture_relay.setup_validate",
                session_id=session.session_id,
            )
            _call_state_callback(on_setup, state)

        if state.armed and not armed_fired:
            try:
                validate_capture_acknowledgement(state, session.spec)
            except CaptureFailed:
                log_event(
                    logger,
                    "capture_relay.acknowledgement_refused",
                    level=logging.WARNING,
                    session_id=session.session_id,
                    kind=session.spec.kind,
                    policy=(
                        session.spec.acknowledgement.id
                        if session.spec.acknowledgement
                        else None
                    ),
                )
                try:
                    client.post_host_event(
                        session.session_id,
                        session.pull_token,
                        {
                            "phase": "sweep_failed",
                            "error": (
                                "Confirm the microphone placement before "
                                "starting the sweep."
                            ),
                        },
                    )
                except (OSError, RelayError):
                    logger.warning(
                        "could not publish acknowledgement refusal",
                        exc_info=True,
                    )
                raise
            armed_fired = True
            # The pre-arm wait is operator time: opening the trusted page,
            # selecting the microphone, and confirming placement.  Do not let
            # that consume the bounded acoustic/upload window.  Refresh the
            # deadline exactly once after the validated, acknowledged arm event;
            # ``armed_fired`` prevents a phone from extending it by
            # replaying the armed state on every poll.
            deadline = monotonic() + timeout_s
            if session.spec.acknowledgement is not None:
                log_event(
                    logger,
                    "capture_relay.acknowledgement_verified",
                    session_id=session.session_id,
                    kind=session.spec.kind,
                    policy=session.spec.acknowledgement.id,
                )
            log_event(logger, "capture_relay.armed", session_id=session.session_id)
            _call_state_callback(on_armed, state)

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
                device=(capture_device or {}).get("label") or "",
            )
            return CaptureResult(
                wav=wav,
                device=capture_device,
                noise_floor=capture_noise_floor,
                setup=capture_setup,
            )

        if monotonic() >= deadline:
            if armed_fired:
                detail = (
                    f"phone never uploaded within {timeout_s:.0f}s after arming"
                )
                phase = "awaiting_upload"
            else:
                detail = f"phone never armed within {timeout_s:.0f}s"
                phase = "awaiting_arm"
            raise CaptureTimeout(
                f"{detail} (session {session.session_id})",
                phase=phase,
            )
        sleep(poll_interval_s)


def purge(client: RelayClient, session: PiCaptureSession) -> None:
    """Delete the session from the relay after a verified pull (best-effort —
    the short TTL is the backstop)."""
    try:
        client.delete(session.session_id, session.pull_token)
    except (OSError, RelayError):
        pass
