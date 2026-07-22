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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

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
from jasper.capture_relay.spec import CapturePlanEntry, CaptureSpec
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 900
DEFAULT_POLL_INTERVAL_S = 0.75
DEFAULT_TIMEOUT_S = 120.0

# The deferred-by-design APPLY-hold budget. When the next plan entry's begin
# is gated on an external event (``auto_advance == "on_apply"`` — the
# "applying" hold between MEASURE and VERIFY), the phone is deliberately
# parked and re-posts the SAME ``begin_capture`` on a slow poll. The normal
# ``timeout_s`` phone-inactivity watchdog must NOT collapse the session mid-
# hold, so this rescopes the ``awaiting_begin`` deadline while gated; a
# deferred begin retry rearms it (counts as liveness) and a vanished phone
# still eventually collapses here (teardown intact).
#
# Owner ruling (2026-07-20): the human mid-flow Apply gate is gone — the
# conductor auto-applies the candidate itself, so this hold now covers only
# the auto-apply TRANSACTION's own latency (a CamillaDSP set-config +
# confirm round trip, typically well under a few seconds), not a human
# reading a candidate and deciding whether to tap Apply. The budget shrank
# from the prior 900 s (sized for a human review) to 30 s — generous
# multi-second margin over typical apply latency without holding the phone
# through anything resembling a human-scale wait. A genuinely stuck apply
# (well past 30 s) is itself an anomaly worth surfacing as a session
# collapse rather than holding indefinitely. Re-derive from W6 bench
# observation of real auto-apply latency if this proves too tight.
REVIEW_HOLD_BUDGET_S = 30.0

# The ``auto_advance`` policy value (mirrors
# ``jasper.active_speaker.crossover_v2_flow.AUTO_ADVANCE_ON_APPLY``) that marks a
# capture-plan entry whose begin is gated on the apply-complete host event. Kept
# as a local literal so this generic runner does not import the v2 flow upward;
# the two are pinned equal by tests/test_capture_relay_plan.py.
AUTO_ADVANCE_ON_APPLY = "on_apply"

# Household-facing copy for a page-identity / spec-integrity failure (W6.10
# blocker #4e). The raw "capture control integrity check failed" is developer
# jargon that surfaces on the phone; the internal ``CaptureFailed`` keeps the
# technical wording for logs, but the phone-facing ``capture_incompatible``
# host event names a friendly, actionable message instead. This is the single
# Pi-side origin of the copy — the phone renders whatever ``error`` it carries.
CAPTURE_INCOMPATIBLE_USER_MESSAGE = (
    "This phone's measurement page couldn't be verified. Reopen the link from "
    "your speaker and try again."
)

# --- Protocol v3 (session-spanning capture plans, SPEC W2.3) -------------------
# Event vocabulary. The phone requests each capture of the set with an
# authenticated `begin_capture {index, attempt}` field (`index` = 1-based
# measurement slot of `capture_target`; `attempt` = 1-based Pi-admitted attempt
# of `max_attempts`); every later event of that capture carries the same
# context. The Pi answers over the host-event channel with the phases below.
# The blob for an admitted attempt rides relay `capture_index = attempt - 1`
# (attempt 1 uses the legacy un-indexed key).
BEGIN_CAPTURE_EVENT_KEY = "begin_capture"
HOST_PHASE_CAPTURE_AUTHORIZED = "capture_authorized"
HOST_PHASE_CAPTURE_RESULT = "capture_result"
HOST_PHASE_CAPTURE_REFUSED = "capture_refused"
HOST_PHASE_CAPTURE_DEFERRED = "capture_deferred"
HOST_PHASE_CAPTURE_SET_COMPLETE = "capture_set_complete"
HOST_PHASE_CAPTURE_SET_EXHAUSTED = "capture_set_exhausted"


class CaptureTimeout(RuntimeError):
    """The phone failed one bounded relay phase before capture completed."""

    def __init__(self, message: str, *, phase: str | None = None) -> None:
        super().__init__(message)
        self.phase = phase


class CaptureFailed(RuntimeError):
    """The relay-pulled blob failed decrypt or integrity (see __cause__)."""


class CaptureAborted(RuntimeError):
    """The phone aborted mid-capture (e.g. backgrounded / screen locked).

    ``reason`` preserves the phone's own ``abort_reason`` (e.g. ``"stopped"``
    for a deliberate tap of the Stop button, ``"backgrounded"`` for a
    screen-off/background abort) as a structured attribute — not just baked
    into the formatted message string — so a caller can classify a deliberate
    Stop distinctly from a genuine relay/transport death (see
    ``jasper.web.correction_crossover_v2``'s ``_run_and_consume``, which
    splits ``reason == "stopped"`` into its own honest reason code instead of
    the generic ``relay_timeout``/"link timed out" bucket).
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = str(reason or "")


class CaptureStopped(RuntimeError):
    """The host explicitly stopped this capture."""


class CapturePageIncompatible(RuntimeError):
    """The public capture page does not implement this Pi's protocol."""


class CaptureBeginRefused(RuntimeError):
    """The Pi refused a phone ``begin_capture`` request (protocol v3).

    Admission stays Pi-owned (SPEC W2.3): the host's injected
    ``authorize_begin`` raises this to refuse — most importantly when
    ``repeat_admission`` refuses the attempt budget. ``code`` is the stable
    machine reason; ``user_message`` is the phone/operator-facing copy
    (refusal-naming pattern, #1534). Both ride the refusal host event so the
    phone can show why nothing started."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


class CaptureBeginDeferred(RuntimeError):
    """A NON-terminal soft-hold on a phone's ``begin_capture`` (protocol v3).

    Distinct from :class:`CaptureBeginRefused` (terminal — ends the whole
    plan): the host's injected ``authorize_begin`` raises this when the Pi
    is not YET ready to admit this capture but the plan should stay alive —
    e.g. a heterogeneous plan (crossover-measurement-productization-design.md
    §5.7) parked between MEASURE and VERIFY awaiting the household's Apply
    tap. The Pi replies with a ``capture_deferred`` host event
    (index/attempt/code/reason) and keeps polling in the SAME
    ``awaiting_begin`` phase, so the phone may retry the IDENTICAL
    ``begin_capture {index, attempt}`` — the attempt budget is not spent and
    the session does not end. ``code`` is the stable machine reason;
    ``user_message`` is the phone-facing copy (mirrors
    ``CaptureBeginRefused``). Because the phone retries throughout a hold,
    the runner dedupes on the (index, code) transition: one INFO log + one
    ``capture_deferred`` host event per state change, DEBUG for identical
    repeats."""

    def __init__(self, code: str, user_message: str = "") -> None:
        super().__init__(user_message or code)
        self.code = str(code)
        self.user_message = str(user_message or code)


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
    # Protocol v3 (session-spanning plans): the phone's current
    # `begin_capture {index, attempt}` request context, and the relay's
    # per-index blob summary (`{"<capture_index>": {size, integrity}}`).
    # Both `None` on v2 sessions — v2 readers never touch them.
    begin_capture: dict | None = None
    blobs: dict | None = None


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

    @property
    def sequence(self) -> int:
        """The last verified event sequence (0 before any verified event).

        The relay `event` slot persists between polls, so a plan runner needs
        the sequence to tell "the same event, still sitting there" from "the
        phone posted a new event" (protocol-v3 begin dedup vs replay)."""
        return self._sequence


class CaptureActivityProbe:
    """Fail closed when a long host stimulus outlives the phone recorder.

    ``run_capture`` normally polls phone state itself, but its synchronous
    ``on_armed`` callback may run a host stimulus for several seconds.  A
    caller with a long callback can poll through this probe while that callback
    is active.  The mutable event slot is authenticated and sequence-checked
    exactly like the main capture loop before an abort can affect playback.
    """

    def __init__(
        self,
        client: RelayClient,
        session: PiCaptureSession,
        *,
        capture_index: int | None = None,
    ) -> None:
        if session.spec.capture_protocol_version < 2:
            raise CaptureFailed(
                "abort-aware host playback requires authenticated capture protocol v2"
            )
        self._client = client
        self._session = session
        self._verifier = PhoneEventVerifier(session)
        self._lock = threading.Lock()
        # Protocol v3: "the recorder finished" is per-capture, not per-session.
        # A prior attempt's blob leaves the legacy `state == ready` set for the
        # whole session, so a plan-aware probe checks THIS capture's index in
        # the per-index blob map instead.
        self._capture_index = capture_index

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
                    f"phone aborted the capture ({state.abort_reason or 'no reason'})",
                    reason=state.abort_reason,
                )
            capture_ended = (
                state.ready
                if self._capture_index is None
                else _plan_blob_ready(state, self._capture_index)
            )
            if capture_ended:
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
    begin_capture = (
        event.get(BEGIN_CAPTURE_EVENT_KEY)
        if isinstance(event.get(BEGIN_CAPTURE_EVENT_KEY), dict)
        else None
    )
    ready = status_payload.get("state") == "ready"
    integrity = status_payload.get("integrity")
    blobs = (
        status_payload.get("blobs")
        if isinstance(status_payload.get("blobs"), dict)
        else None
    )
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
        begin_capture=begin_capture,
        blobs=blobs,
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


def _call_with_optional_entry(
    callback: Callable[..., Any],
    *args: Any,
    entry: CapturePlanEntry | None,
) -> Any:
    """Call a plan callback, appending ``entry`` only if it accepts one more arg.

    ``authorize_begin(index, attempt)`` and ``consume_capture(index, attempt,
    result)`` predate per-capture entries (§5.7); every existing caller (e.g.
    ``jasper/web/correction_crossover_flow.py``) declares exactly ``len(args)``
    positional parameters and keeps working completely unchanged. A NEW
    caller that wants the active ``CapturePlanEntry`` (``None`` on a plan
    with no entry table) declares one more positional parameter and receives
    it. Mirrors ``_call_state_callback``'s inspect-based polymorphism for
    ``on_setup``/``on_armed``.
    """
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(*args)
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
    ]
    accepts_entry = any(p.kind is p.VAR_POSITIONAL for p in positional) or (
        len(positional) > len(args)
    )
    return callback(*args, entry) if accepts_entry else callback(*args)


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
    stop_requested: Callable[[], bool] | None = None,
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
      - `CaptureStopped`: the host's cooperative Stop signal was observed;
      - `CaptureFailed`: the pulled blob failed decrypt/integrity;
      - `RelayError` / `OSError`: the relay died or became unreachable mid-poll.
    `play_cue` (host-injected, no-silent-failure) is called with the matching cue
    slug before failures propagate, so a host need not re-cue them itself (the
    cue for connectivity loss is `measurement_relay_unreachable`). Explicit
    `CaptureStopped` is expected control flow: it is logged without a failure
    cue.
    """
    return _run_with_failure_cues(
        session,
        play_cue,
        lambda: _poll_until_capture(
            client,
            session,
            on_armed=on_armed,
            on_setup=on_setup,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s,
            sleep=sleep,
            monotonic=monotonic,
            stop_requested=stop_requested,
        ),
    )


_T = TypeVar("_T")


def _run_with_failure_cues(
    session: PiCaptureSession,
    play_cue: Callable[[str], None] | None,
    runner: Callable[[], _T],
) -> _T:
    """The no-silent-failure tail shared by every capture runner.

    Explicit Stop is expected control flow — logged, never cued. Any other
    failure gets both halves: the operator-facing WARNING with the failure
    type + cue slug plus the traceback (so an *unexpected* error — e.g. a bug
    in the host's on_armed/stimulus playback — is diagnosable even though the
    household only hears the generic measurement_failed cue), and the
    household-facing cue, best-effort."""
    try:
        return runner()
    except CaptureStopped:
        log_event(
            logger,
            "capture_relay.stopped",
            session_id=session.session_id,
            kind=session.spec.kind,
        )
        raise
    except Exception as exc:  # noqa: BLE001 — cue on ANY failure, then re-raise
        slug = classify_failure_cue(exc)
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


def _verify_phone_event(
    client: RelayClient,
    session: PiCaptureSession,
    verifier: PhoneEventVerifier,
    status: dict,
) -> dict:
    """Authenticate the relay's mutable event slot before any field is read.

    On an integrity failure this logs, tells the phone (best-effort), and
    raises ``CaptureFailed`` — the identical handling both the single-capture
    poll loop and the protocol-v3 plan runner need."""
    relay_event = status.get("event") if isinstance(status, dict) else None
    if session.spec.capture_protocol_version < 2 or relay_event is None:
        return status
    try:
        verified_event = verifier.verify(relay_event)
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
                    "error": CAPTURE_INCOMPATIBLE_USER_MESSAGE,
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
    return {**status, "event": verified_event}


def _ensure_page_compatible(
    client: RelayClient,
    session: PiCaptureSession,
    state: PollState,
) -> None:
    """Establish the independently-deployed page's protocol contract.

    Runs before setup callbacks or ``on_armed`` can reach an audio player, so
    a stale page fails visibly and cannot start a tone."""
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
    log_event(
        logger,
        "capture_relay.page_compatible",
        session_id=session.session_id,
        protocol=session.spec.capture_protocol_version,
        page_build=(state.capture_page or {}).get("capture_page_build"),
    )


def _verify_acknowledgement_or_refuse(
    client: RelayClient,
    session: PiCaptureSession,
    state: PollState,
) -> None:
    """Verify the spec-bound operator acknowledgement; refuse loudly if stale."""
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
    stop_requested: Callable[[], bool] | None,
) -> CaptureResult:
    def raise_if_stopped() -> None:
        if stop_requested is not None and stop_requested():
            raise CaptureStopped("capture stopped")

    deadline = monotonic() + timeout_s
    armed_fired = False
    capture_device: dict | None = None
    capture_noise_floor: dict | None = None
    capture_setup: dict | None = None
    setup_tokens_seen: set[str] = set()
    page_compatible = False
    event_verifier = PhoneEventVerifier(session)
    while True:
        raise_if_stopped()
        status = client.status(session.session_id, session.pull_token)
        raise_if_stopped()
        status = _verify_phone_event(client, session, event_verifier, status)
        state = classify_status(status)
        if state.device is not None:
            capture_device = state.device  # phone-reported mic; persists to ready
        if state.noise_floor is not None:
            capture_noise_floor = state.noise_floor
        if state.setup is not None:
            capture_setup = state.setup

        if state.aborted:
            raise CaptureAborted(
                f"phone aborted the capture ({state.abort_reason or 'no reason'})",
                reason=state.abort_reason,
            )

        # The phone page is independently deployed. Establish this contract
        # before setup callbacks or `on_armed` can reach an audio player. A
        # stale page therefore fails visibly and cannot start a tone.
        if not page_compatible and (
            state.setup_validate or state.armed or state.ready
        ):
            _ensure_page_compatible(client, session, state)
            page_compatible = True

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
            raise_if_stopped()
            _verify_acknowledgement_or_refuse(client, session, state)
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
            raise_if_stopped()

        if state.ready:
            raise_if_stopped()
            log_event(logger, "capture_relay.ready", session_id=session.session_id)
            blob, header_integrity = client.pull_blob(
                session.session_id, session.pull_token
            )
            raise_if_stopped()
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
            raise_if_stopped()
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


# --- Session-spanning plan runner (protocol v3, SPEC W2.3) ---------------------


def parse_begin_capture(
    payload: Any,
    *,
    capture_target: int,
    max_attempts: int,
) -> tuple[int, int]:
    """Strictly parse a phone ``begin_capture`` request into (index, attempt).

    Shape validation only — ordering and the Pi-owned attempt budget are
    enforced by the plan runner plus the host's injected admission
    (``repeat_admission``). ``index`` is the 1-based measurement slot
    (``"Measurement N of {capture_target}"``); ``attempt`` is the 1-based
    admission attempt whose blob rides relay ``capture_index = attempt - 1``.
    Raises ``CaptureBeginRefused`` (code ``begin_malformed``) on any drift —
    no Postel-style liberality on an authenticated control field."""
    if not isinstance(payload, Mapping):
        raise CaptureBeginRefused(
            "begin_malformed", "begin_capture must be an object"
        )
    if set(payload) != {"index", "attempt"}:
        raise CaptureBeginRefused(
            "begin_malformed",
            "begin_capture must carry exactly index and attempt",
        )
    index = payload.get("index")
    attempt = payload.get("attempt")
    for name, value, bound in (
        ("index", index, capture_target),
        ("attempt", attempt, max_attempts),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= bound
        ):
            raise CaptureBeginRefused(
                "begin_malformed",
                f"begin_capture.{name} must be an integer in 1..{bound}",
            )
    assert isinstance(index, int) and isinstance(attempt, int)
    if index > attempt:
        # Slot N cannot be reached before its Nth admission attempt.
        raise CaptureBeginRefused(
            "begin_malformed", "begin_capture.index cannot exceed attempt"
        )
    return index, attempt


def _plan_blob_ready(state: PollState, capture_index: int) -> bool:
    """Whether the blob for one admitted attempt is uploaded.

    Per-index readiness comes from the Worker's ``blobs`` summary; the legacy
    session-wide ``state == "ready"`` remains an accepted signal for index 0
    (the un-indexed key it aliases)."""
    entry = (state.blobs or {}).get(str(capture_index))
    if isinstance(entry, dict):
        return True
    return capture_index == 0 and state.ready


def _plan_blob_integrity(state: PollState, capture_index: int) -> dict | None:
    entry = (state.blobs or {}).get(str(capture_index))
    if isinstance(entry, dict) and isinstance(entry.get("integrity"), dict):
        return entry["integrity"]
    if capture_index == 0:
        return state.integrity
    return None


@dataclass(frozen=True)
class PlanCaptureOutcome:
    """One admitted, uploaded, and host-consumed attempt of a capture plan."""

    index: int
    attempt: int
    accepted: bool
    verdict: dict[str, Any]
    result: CaptureResult


def run_capture_plan(
    client: RelayClient,
    session: PiCaptureSession,
    *,
    authorize_begin: Callable[[int, int], None],
    on_armed: Callable[..., None],
    consume_capture: Callable[[int, int, CaptureResult], Mapping[str, Any]],
    on_setup: Callable[..., None] | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    first_begin_timeout_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    play_cue: Callable[[str], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> list[PlanCaptureOutcome]:
    """Run one session-spanning capture SET (protocol v3, SPEC W2.3).

    One relay session covers ``capture_plan.capture_target`` accepted captures
    within ``capture_plan.max_attempts`` admitted attempts. Per capture N the
    choreography is: the phone posts an authenticated ``begin_capture
    {index, attempt}`` → the host's injected ``authorize_begin(index, attempt)``
    admits it (budget stays PI-OWNED — wire ``repeat_admission`` there; raise
    ``CaptureBeginRefused`` to refuse, which is published to the phone as a
    named ``capture_refused`` host event) → the Pi ACKs ``capture_authorized``
    → the phone records and posts ``armed`` carrying the same begin context
    (placement acknowledgement and page identity are validated per capture,
    exactly as the single-capture runner does) → ``on_armed`` plays the host
    stimulus → the phone uploads its blob at ``capture_index = attempt - 1`` →
    the Pi pulls/decrypts/verifies and hands the ``CaptureResult`` to
    ``consume_capture(index, attempt, result)``, whose returned mapping (with
    at least ``accepted: bool``; every other field is relayed to the phone)
    becomes the ``capture_result`` host event. The set ends with
    ``capture_set_complete`` (target met) or ``capture_set_exhausted``
    (attempt budget spent) and returns every outcome in order.

    The phone NEVER decides admission; out-of-order, replayed, or malformed
    begins are refused loudly (named host event + ``CaptureFailed``). Failure
    semantics — timeout phases, abort, stop, cues, logging — mirror
    ``run_capture``. Accepted captures the host already committed durably
    (ledger writes inside ``consume_capture``) persist across a later abort or
    Stop; this runner never rolls them back.

    **Per-capture entries (schema_version 2, additive — crossover-
    measurement-productization-design.md §5.7).** When
    ``session.spec.capture_plan.entries`` is set, this runner exposes the
    active :class:`~jasper.capture_relay.spec.CapturePlanEntry` (or ``None``
    on a plan with no entry table) to ``authorize_begin`` and
    ``consume_capture`` — declare one extra positional parameter to receive
    it (existing 2-/3-arg callables are unaffected; see
    ``_call_with_optional_entry``). An entry's ``duration_ms`` is the
    capture's DECLARED acoustic length — phone-side presentation and
    analysis-side locator-window data, never a deadline: the
    recording+upload backstop stays this function's ``timeout_s`` for every
    plan, entries or not.

    **Deferred admission.** ``authorize_begin`` may raise
    :class:`CaptureBeginDeferred` instead of :class:`CaptureBeginRefused` for
    a NON-terminal "not yet" — e.g. the v2 crossover conductor's heterogeneous
    plan parked between MEASURE and VERIFY while its own auto-apply is in
    flight (jasper.active_speaker.crossover_v2_flow — no household tap
    involved since the 2026-07-20 owner ruling). The Pi posts a
    ``capture_deferred`` host event and stays in ``awaiting_begin`` with the
    attempt budget untouched, so the phone may retry the IDENTICAL
    ``begin_capture {index, attempt}``; unlike a refusal this never ends the
    session. Repeated identical deferrals during one hold are deduped on the
    (index, code) transition — one INFO ``capture_relay.plan_deferred`` +
    one ``capture_deferred`` host event per state change, DEBUG otherwise.

    **Deferred-by-design apply budget (W6.10 blocker #1).** While the entry the
    phone is waiting to begin declares ``screen.auto_advance == "on_apply"`` (the
    "applying" hold between MEASURE and VERIFY), the ``awaiting_begin``
    inactivity deadline is rescoped to :data:`REVIEW_HOLD_BUDGET_S` instead of the
    tight ``timeout_s`` — the phone is legitimately parked while the host's own
    auto-apply transaction runs, and a deferred begin retry rearms the clock as
    liveness. The deadline rearms to the normal ``timeout_s`` the moment the
    begin is admitted (apply released the hold, or failed and refused it).
    ``first_begin_timeout_s`` (when set) widens ONLY the very first
    ``awaiting_begin`` before any capture — reading the placement instructions
    legitimately outlasts the general 120 s budget.
    """
    plan = session.spec.capture_plan
    if plan is None or session.spec.capture_protocol_version < 3:
        raise CaptureFailed(
            "run_capture_plan requires a capture_plan spec (capture protocol 3)"
        )
    return _run_with_failure_cues(
        session,
        play_cue,
        lambda: _poll_capture_plan(
            client,
            session,
            plan_target=plan.capture_target,
            plan_max_attempts=plan.max_attempts,
            authorize_begin=authorize_begin,
            on_armed=on_armed,
            consume_capture=consume_capture,
            on_setup=on_setup,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s,
            first_begin_timeout_s=first_begin_timeout_s,
            sleep=sleep,
            monotonic=monotonic,
            stop_requested=stop_requested,
        ),
    )


def _poll_capture_plan(
    client: RelayClient,
    session: PiCaptureSession,
    *,
    plan_target: int,
    plan_max_attempts: int,
    authorize_begin: Callable[[int, int], None],
    on_armed: Callable[..., None],
    consume_capture: Callable[[int, int, CaptureResult], Mapping[str, Any]],
    on_setup: Callable[..., None] | None,
    poll_interval_s: float,
    timeout_s: float,
    first_begin_timeout_s: float | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    stop_requested: Callable[[], bool] | None,
) -> list[PlanCaptureOutcome]:
    def raise_if_stopped() -> None:
        if stop_requested is not None and stop_requested():
            raise CaptureStopped("capture stopped")

    def post_refusal_best_effort(
        index: int | None, attempt: int | None, code: str, message: str
    ) -> None:
        payload: dict[str, Any] = {
            "phase": HOST_PHASE_CAPTURE_REFUSED,
            "code": code,
            "error": message,
        }
        if index is not None:
            payload["index"] = index
        if attempt is not None:
            payload["attempt"] = attempt
        try:
            client.post_host_event(
                session.session_id, session.pull_token, payload
            )
        except (OSError, RelayError):
            logger.warning(
                "could not publish capture-begin refusal", exc_info=True
            )

    def refuse_begin_order(
        index: int, attempt: int, code: str, message: str
    ) -> None:
        log_event(
            logger,
            "capture_relay.plan_refused",
            level=logging.WARNING,
            session_id=session.session_id,
            index=index,
            attempt=attempt,
            code=code,
        )
        post_refusal_best_effort(index, attempt, code, message)
        raise CaptureFailed(message)

    # Guaranteed non-None: run_capture_plan checks `plan is None` before ever
    # calling this function. Per-capture entries (§5.7) live on it; a plan
    # with no entry table (`plan.entries is None`, the pre-Wave-3 shape)
    # makes every entry-aware branch below a no-op.
    plan = session.spec.capture_plan
    assert plan is not None

    def begin_budget(next_index: int) -> float:
        """The ``awaiting_begin`` inactivity budget for the entry the phone is
        waiting to begin (W6.10 blocker #1). An ``on_apply``-gated entry (the
        "applying" hold) gets the wider apply-latency budget; every other
        entry keeps the tight ``timeout_s`` arm/upload backstop."""
        entry = plan.entry_for_index(next_index)
        screen = entry.screen if entry is not None else None
        if screen and str(screen.get("auto_advance") or "") == AUTO_ADVANCE_ON_APPLY:
            return REVIEW_HOLD_BUDGET_S
        return timeout_s

    outcomes: list[PlanCaptureOutcome] = []
    accepted_count = 0
    attempts_used = 0
    current: tuple[int, int] | None = None
    processed: set[tuple[int, int]] = set()
    # S2 dedupe: the last (index, code) deferral already logged + posted to
    # the phone. The phone re-posts the same begin every ~1.5s during a hold,
    # so without this a long "waiting for apply" hold would spam one INFO
    # log line + one host-event POST per retry for zero new information.
    last_deferral: tuple[int, str] | None = None
    phase = "awaiting_begin"
    armed_fired = False
    page_compatible = False
    begin_handled_sequence = 0
    capture_device: dict | None = None
    capture_noise_floor: dict | None = None
    capture_setup: dict | None = None
    setup_tokens_seen: set[str] = set()
    event_verifier = PhoneEventVerifier(session)
    # The FIRST begin (before any capture) may legitimately outlast the general
    # phone-inactivity budget — reading the placement instructions on the v2 spec
    # takes >120 s (Chrome round 1 died here). Widen only this first window when
    # the caller supplied a first-begin budget; every later awaiting_begin uses
    # begin_budget().
    deadline = monotonic() + (
        first_begin_timeout_s if first_begin_timeout_s is not None else timeout_s
    )
    while True:
        raise_if_stopped()
        status = client.status(session.session_id, session.pull_token)
        raise_if_stopped()
        status = _verify_phone_event(client, session, event_verifier, status)
        state = classify_status(status)
        if state.device is not None:
            capture_device = state.device
        if state.noise_floor is not None:
            capture_noise_floor = state.noise_floor
        if state.setup is not None:
            capture_setup = state.setup

        if state.aborted:
            raise CaptureAborted(
                f"phone aborted the capture ({state.abort_reason or 'no reason'})",
                reason=state.abort_reason,
            )

        if not page_compatible and (
            state.setup_validate
            or state.armed
            or state.ready
            or state.begin_capture is not None
        ):
            _ensure_page_compatible(client, session, state)
            page_compatible = True

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

        # --- begin_capture: act only on NEW events (the relay event slot
        # persists between polls; the verifier's sequence tells them apart) ---
        sequence = event_verifier.sequence
        if sequence > begin_handled_sequence:
            begin_handled_sequence = sequence
            if state.begin_capture is not None:
                try:
                    index, attempt = parse_begin_capture(
                        state.begin_capture,
                        capture_target=plan_target,
                        max_attempts=plan_max_attempts,
                    )
                except CaptureBeginRefused as refusal:
                    log_event(
                        logger,
                        "capture_relay.plan_refused",
                        level=logging.WARNING,
                        session_id=session.session_id,
                        code=refusal.code,
                    )
                    post_refusal_best_effort(
                        None, None, refusal.code, refusal.user_message
                    )
                    raise CaptureFailed(refusal.user_message) from refusal
                pair = (index, attempt)
                if pair in processed:
                    if phase == "awaiting_begin" or pair != current:
                        refuse_begin_order(
                            index,
                            attempt,
                            "begin_replayed",
                            "this capture attempt was already processed",
                        )
                    # else: the current capture's context riding a newer
                    # event (e.g. its own armed post) — nothing to do.
                elif phase != "awaiting_begin":
                    refuse_begin_order(
                        index,
                        attempt,
                        "begin_out_of_order",
                        "a capture attempt is already in progress",
                    )
                elif pair != (accepted_count + 1, attempts_used + 1):
                    refuse_begin_order(
                        index,
                        attempt,
                        "begin_out_of_order",
                        (
                            f"expected capture {accepted_count + 1} attempt "
                            f"{attempts_used + 1}"
                        ),
                    )
                else:
                    raise_if_stopped()
                    entry = plan.entry_for_index(index)
                    try:
                        _call_with_optional_entry(
                            authorize_begin, index, attempt, entry=entry
                        )
                    except CaptureBeginDeferred as deferral:
                        # Dedupe on the (index, code) transition: the phone
                        # re-posts the same begin throughout a hold, so only
                        # the FIRST deferral of a hold (or a changed code /
                        # index) is INFO-logged and posted to the phone;
                        # identical repeats stay at DEBUG with no host POST
                        # (the relay's host_event slot still holds the first
                        # one, so the phone keeps rendering the wait screen).
                        deferral_key = (index, deferral.code)
                        if deferral_key != last_deferral:
                            last_deferral = deferral_key
                            log_event(
                                logger,
                                "capture_relay.plan_deferred",
                                session_id=session.session_id,
                                index=index,
                                attempt=attempt,
                                code=deferral.code,
                            )
                            try:
                                client.post_host_event(
                                    session.session_id,
                                    session.pull_token,
                                    {
                                        "phase": HOST_PHASE_CAPTURE_DEFERRED,
                                        "index": index,
                                        "attempt": attempt,
                                        "code": deferral.code,
                                        "error": deferral.user_message,
                                    },
                                )
                            except (OSError, RelayError):
                                logger.warning(
                                    "could not publish capture-begin deferral",
                                    exc_info=True,
                                )
                        else:
                            log_event(
                                logger,
                                "capture_relay.plan_deferred",
                                level=logging.DEBUG,
                                session_id=session.session_id,
                                index=index,
                                attempt=attempt,
                                code=deferral.code,
                                repeated=True,
                            )
                        # NON-terminal soft-hold: `pair` is deliberately NOT
                        # marked processed and `phase` stays "awaiting_begin",
                        # so the phone's identical retry of THIS SAME
                        # (index, attempt) is admitted cleanly next time —
                        # unlike a refusal, the attempt budget is not spent
                        # and the session does not end. A deferred retry counts
                        # as liveness and rearms the inactivity clock; for an
                        # ``on_apply``-gated entry that is the apply-latency
                        # budget, not the tight ``timeout_s`` (W6.10 blocker #1).
                        deadline = monotonic() + begin_budget(index)
                    except CaptureBeginRefused as refusal:
                        log_event(
                            logger,
                            "capture_relay.plan_refused",
                            level=logging.WARNING,
                            session_id=session.session_id,
                            index=index,
                            attempt=attempt,
                            code=refusal.code,
                        )
                        post_refusal_best_effort(
                            index, attempt, refusal.code, refusal.user_message
                        )
                        raise
                    except (OSError, RuntimeError, ValueError):
                        # Admission crashed (not a policy refusal) — the named
                        # family the admission ledger raises. Still tell the
                        # phone why nothing will start, then fail loud.
                        post_refusal_best_effort(
                            index,
                            attempt,
                            "authorize_failed",
                            "the speaker could not admit this capture",
                        )
                        raise
                    else:
                        # The hold (if any) ended — the next deferral, even
                        # for the same (index, code), is a NEW hold and gets
                        # its own INFO log + host event.
                        last_deferral = None
                        processed.add(pair)
                        current = pair
                        attempts_used = attempt
                        armed_fired = False
                        phase = "awaiting_arm"
                        # Between-capture operator time (tapping Next / Retry)
                        # must not consume the acoustic/upload window.
                        deadline = monotonic() + timeout_s
                        log_event(
                            logger,
                            "capture_relay.plan_authorized",
                            session_id=session.session_id,
                            index=index,
                            attempt=attempt,
                        )
                        client.post_host_event(
                            session.session_id,
                            session.pull_token,
                            {
                                "phase": HOST_PHASE_CAPTURE_AUTHORIZED,
                                "index": index,
                                "attempt": attempt,
                            },
                        )

        if phase == "awaiting_arm" and state.armed and not armed_fired:
            raise_if_stopped()
            context = state.begin_capture
            context_pair: tuple[int, int] | None = None
            if context is not None:
                try:
                    context_pair = parse_begin_capture(
                        context,
                        capture_target=plan_target,
                        max_attempts=plan_max_attempts,
                    )
                except CaptureBeginRefused:
                    context_pair = None
            if context_pair != current:
                raise CaptureFailed(
                    "armed event does not carry the authorized capture context"
                )
            _verify_acknowledgement_or_refuse(client, session, state)
            armed_fired = True
            deadline = monotonic() + timeout_s
            if session.spec.acknowledgement is not None:
                log_event(
                    logger,
                    "capture_relay.acknowledgement_verified",
                    session_id=session.session_id,
                    kind=session.spec.kind,
                    policy=session.spec.acknowledgement.id,
                )
            log_event(
                logger,
                "capture_relay.armed",
                session_id=session.session_id,
                index=current[0] if current else None,
                attempt=current[1] if current else None,
            )
            _call_state_callback(on_armed, state)
            raise_if_stopped()
            phase = "awaiting_upload"

        if phase == "awaiting_upload" and current is not None:
            index, attempt = current
            capture_index = attempt - 1
            if _plan_blob_ready(state, capture_index):
                raise_if_stopped()
                log_event(
                    logger,
                    "capture_relay.ready",
                    session_id=session.session_id,
                    capture_index=capture_index,
                )
                blob, header_integrity = client.pull_blob(
                    session.session_id,
                    session.pull_token,
                    capture_index=capture_index,
                )
                raise_if_stopped()
                integrity = (
                    _plan_blob_integrity(state, capture_index)
                    or header_integrity
                )
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
                raise_if_stopped()
                result = CaptureResult(
                    wav=wav,
                    device=capture_device,
                    noise_floor=capture_noise_floor,
                    setup=capture_setup,
                )
                log_event(
                    logger,
                    "capture_relay.captured",
                    session_id=session.session_id,
                    wav_bytes=len(wav),
                    device=(capture_device or {}).get("label") or "",
                    index=index,
                    attempt=attempt,
                )
                verdict = dict(
                    _call_with_optional_entry(
                        consume_capture,
                        index,
                        attempt,
                        result,
                        entry=plan.entry_for_index(index),
                    )
                    or {}
                )
                accepted = verdict.get("accepted") is True
                outcomes.append(
                    PlanCaptureOutcome(
                        index=index,
                        attempt=attempt,
                        accepted=accepted,
                        verdict=verdict,
                        result=result,
                    )
                )
                if accepted:
                    accepted_count += 1
                log_event(
                    logger,
                    "capture_relay.plan_result",
                    session_id=session.session_id,
                    index=index,
                    attempt=attempt,
                    accepted=accepted,
                )
                client.post_host_event(
                    session.session_id,
                    session.pull_token,
                    {
                        "phase": HOST_PHASE_CAPTURE_RESULT,
                        "index": index,
                        "attempt": attempt,
                        "accepted": accepted,
                        **{
                            k: v for k, v in verdict.items() if k != "accepted"
                        },
                    },
                )
                if accepted_count >= plan_target:
                    client.post_host_event(
                        session.session_id,
                        session.pull_token,
                        {
                            "phase": HOST_PHASE_CAPTURE_SET_COMPLETE,
                            "accepted": accepted_count,
                            "capture_target": plan_target,
                        },
                    )
                    log_event(
                        logger,
                        "capture_relay.plan_complete",
                        session_id=session.session_id,
                        accepted=accepted_count,
                        attempts=attempts_used,
                    )
                    return outcomes
                if attempts_used >= plan_max_attempts:
                    client.post_host_event(
                        session.session_id,
                        session.pull_token,
                        {
                            "phase": HOST_PHASE_CAPTURE_SET_EXHAUSTED,
                            "accepted": accepted_count,
                            "capture_target": plan_target,
                            "attempts": attempts_used,
                        },
                    )
                    log_event(
                        logger,
                        "capture_relay.plan_exhausted",
                        level=logging.WARNING,
                        session_id=session.session_id,
                        accepted=accepted_count,
                        attempts=attempts_used,
                    )
                    return outcomes
                phase = "awaiting_begin"
                # Between-capture window budget: the next entry drives it. A
                # MEASURE→VERIFY transition parks on the ``on_apply`` apply
                # hold (wider budget) even before the phone posts its first
                # deferred begin, so a slow/backgrounded phone during the
                # host's own auto-apply does not trip the 120 s watchdog
                # before apply completes (W6.10 #1).
                deadline = monotonic() + begin_budget(accepted_count + 1)

        if monotonic() >= deadline:
            if phase == "awaiting_upload":
                detail = (
                    f"phone never uploaded within {timeout_s:.0f}s after arming"
                )
            elif phase == "awaiting_arm":
                detail = f"phone never armed within {timeout_s:.0f}s"
            else:
                # Report the budget actually in force — the first begin uses
                # first_begin_timeout_s, and the deferred-by-design apply hold
                # rescopes to REVIEW_HOLD_BUDGET_S, so a collapse there names
                # that budget, not the tight 120 s one (W6.10 #1).
                first_begin = not processed and first_begin_timeout_s is not None
                waited_s = (
                    first_begin_timeout_s if first_begin
                    else begin_budget(accepted_count + 1)
                )
                detail = (
                    f"phone never began the next capture within {waited_s:.0f}s"
                )
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
