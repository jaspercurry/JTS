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
from typing import Any, NamedTuple, TypeVar

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
from jasper.capture_relay.spec import (
    LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS,
    CapturePlanEntry,
    CaptureSpec,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

DEFAULT_TTL_S = 900
DEFAULT_POLL_INTERVAL_S = 0.75
DEFAULT_TIMEOUT_S = 120.0

# The longest link the relay Worker grants (``MAX_TTL_S`` in
# relay/src/worker.js, pinned in lockstep by tests/test_capture_relay_session.py).
#
# The Worker CLAMPS an over-large request rather than refusing it, so a caller
# that asks for more is not an error there — it is a silent disagreement here.
# ``open_capture`` publishes the requested ``ttl_s`` to the phone as
# ``time_budget.session_s``, which is the number the household is told the link
# lives for; a request the Worker quietly cut back would make that disclosure a
# lie. So a caller sizing a TTL from its own budget clamps against this, and the
# published number stays the granted one. It is a mirror of a separately
# released artifact, exactly like ``LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS``, not a
# knob to tune from this side.
MAX_TTL_S = 3600

# How long the plan runner keeps polling through CONSECUTIVE transport failures
# on ``client.status`` before it gives up and lets the failure end the session
# (issue #2083). One 10 s HTTP stall on a single status poll used to kill a live
# measurement mid-walk: the call is the first statement of the poll loop, and
# the household — who had done nothing wrong and whose phone was still on the
# step screen — was then shown "Link expired".
#
# The window is anchored at the START of the first failing call, so it bounds
# WHEN THE RUNNER STOPS RETRYING, not total wall time: a single call may itself
# burn the relay client's per-request timeout (10 s, ``_RELAY_REGISTER_TIMEOUT_S``
# — a registration-scoped constant that this poll inherits through the shared
# ``RelayClient``), so two back-to-back stalls can run ~21 s before the second
# one is refused. That is the intended shape — one stall survives, a sustained
# outage still dies promptly — and it is why the bound is wall-clock rather than
# a retry count: with failures that cost anywhere from ~0 s (connection refused)
# to 10 s (timeout), a count buys wildly different amounts of patience.
#
# This is a TRANSPORT tolerance and deliberately not a liveness change. The
# phone-inactivity watchdog (``timeout_s`` / ``first_begin_timeout_s``) remains
# the liveness authority; a tolerated poll skips that deadline check for the
# retry, so an outage defers the watchdog by at most this window PLUS one
# request timeout — the last tolerated call can begin at just under 15 s and
# then burn its own 10 s, so the deadline is next evaluated ~25 s after the
# outage began. Deferring it is the honest behaviour: while the relay is
# unreachable the runner has no evidence about the PHONE at all, and firing a
# "phone never began the next capture" timeout there would blame the household
# for an outage.
STATUS_POLL_TRANSIENT_GRACE_S = 15.0

# How many tolerated status polls a session reports at WARNING before the rest
# drop to DEBUG. Bounds the journal cost of a FLAPPING relay: the grace resets
# on every success, so alternating fail/succeed makes each failure the FIRST of
# a fresh outage — a per-outage dedupe (the shape ``last_deferral`` uses for
# repeated deferrals) would not bound it at all, and at the 0.75 s cadence a
# 15-minute session could emit several hundred WARNING lines. Every line still
# carries the running ``tolerated_total``, so the scale stays visible after the
# level drops, and GIVING UP is always a WARNING regardless of the budget.
STATUS_POLL_WARN_BUDGET = 10

# The same tolerance, for the one call the grace above deliberately did not
# cover: the BLOB PULL (issue #1650). Same number because it answers the same
# policy question — how long is a relay that has stopped answering an outage
# rather than a death — and it is the same object rather than a second literal
# so the two can never drift apart.
#
# What is NOT shared is the ANCHOR, and that is the load-bearing half. The pull
# runs inside the poll loop, one line after a status call that just SUCCEEDED,
# so a run of failing pulls is interleaved with healthy status polls. Sharing
# ``transient_since`` would let each of those successes clear the anchor and
# the retry would never expire — a relay that answers ``/status`` but cannot
# serve ``/blob`` would spin until the phone-inactivity deadline fired and
# blamed the household for an upload they had already completed. A separate
# anchor, cleared only by a pull that actually SUCCEEDS, bounds the retry at
# this grace no matter how healthy the control plane looks.
#
# Why re-pulling is sound at all: ``GET /sessions/:id/blob`` is a pure read of
# stored bytes (``relay/src/worker.js`` — the Worker deletes a blob only on an
# explicit DELETE or TTL expiry), so the capture the phone already uploaded is
# still there. Nothing is re-recorded and the household is not asked for
# anything; the Pi simply asks again for bytes it failed to receive.
#
# And why this has NO counterpart to STATUS_POLL_WARN_BUDGET: the journal cost
# is bounded by construction, so there is no flap to cap. Only a SUCCESSFUL
# pull clears the anchor, and a successful pull is a capture consumed — so the
# retries are bounded by this grace per capture, and the captures by the plan's
# own ``max_attempts``. The status poll needed a budget precisely because it has
# no such ceiling: it runs at the poll cadence for the whole session.
BLOB_PULL_TRANSIENT_GRACE_S = STATUS_POLL_TRANSIENT_GRACE_S

# The deferred-by-design APPLY-hold budget. When the next plan entry's begin
# is gated on an external event (``auto_advance == "on_apply"`` — the
# "applying" hold between MEASURE and VERIFY), the phone is deliberately
# parked and re-posts the SAME ``begin_capture`` on a slow poll. The normal
# ``timeout_s`` phone-inactivity watchdog must NOT collapse the session mid-
# hold, so this rescopes the ``awaiting_begin`` deadline while gated; a
# deferred begin retry rearms it (counts as liveness) and a vanished phone
# still eventually collapses here (teardown intact).
#
# **RETAINED, and unreached by any shipped session** (two-stage commission
# work order D10, PR-T3). The sole consumer is ``begin_budget``'s
# ``auto_advance == on_apply`` arm, and no plan emits that entry any more:
# stage 1 has no VERIFY index at all and stage 2 opens already-applied. Kept
# because D10 keeps the hold itself — a session built without a prior apply
# still gets the honest deferral — but no new design may depend on it.
#
# The history below is why the number is 30 and not 900, and it describes a
# ruling that has since been REVERSED; read it as archaeology, not as current
# behaviour. Owner ruling (2026-07-20): the human mid-flow Apply gate is gone
# — the session auto-applied the candidate itself, so this hold covered only
# the auto-apply TRANSACTION's own latency (a CamillaDSP set-config + confirm
# round trip, typically well under a few seconds), not a human reading a
# candidate and deciding whether to tap Apply. The budget shrank from the prior
# 900 s (sized for a human review) to 30 s. The 2026-07-28 ruling restored the
# human decision and moved it OUT of the session entirely (the untimed review
# interlude on jts.local), which is why nothing holds for an apply now. Anyone
# reviving this budget re-derives it against whatever it would then be holding
# for.
REVIEW_HOLD_BUDGET_S = 30.0

# The ``auto_advance`` policy value (mirrors
# ``jasper.active_speaker.crossover_v2_flow.AUTO_ADVANCE_ON_APPLY``, whose own
# comment records that no plan emits it since PR-T3) that marks a
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
    "This measurement page couldn't be verified. Reopen the link from "
    "your speaker and try again."
)

# --- Session-spanning capture plans (SPEC W2.3) -------------------------------
# Event vocabulary. The phone requests each capture of the set with an
# authenticated `begin_capture {index, attempt}` field (`index` = 1-based
# measurement slot of `capture_target`; `attempt` = 1-based Pi-admitted attempt
# of `max_attempts`); every later event of that capture carries the same
# context. The Pi answers over the host-event channel with the phases below.
# The blob for an admitted attempt rides relay `capture_index = attempt - 1`
# (attempt 1 uses the legacy un-indexed key).
BEGIN_CAPTURE_EVENT_KEY = "begin_capture"
# The household's explicit "I am done measuring" signal (two-stage commission
# work order D1, issue #1806). A plan whose host HOLDS the set open past its
# capture target — because closing the set is a household decision, not an
# arithmetic one — ends on this event instead of on the target being met. The
# literal ``true`` only, for the same reason ``begin_capture.retake`` demands
# it: one wire shape per meaning on an authenticated control field. A page that
# never sends it cannot reach any of it, and a host that does not hold the set
# never looks at it.
COMPLETE_CAPTURE_SET_EVENT_KEY = "complete_capture_set"
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


# The two clocks a capture session can run out of, named so every surface that
# discloses an expiry uses the same word (work order D8, issue #1807).
#
#   TIME_BUDGET_STEP — the per-step phone-inactivity budget (``DEFAULT_TIMEOUT_S``
#     and the wider first-begin / hold variants). Refreshed on every tap, so it
#     is the one a household spends by walking away mid-walk.
#   TIME_BUDGET_LINK — the relay session's own TTL (``DEFAULT_TTL_S`` unless the
#     caller mints a longer link — the v2 REMOTE preparers size theirs from the
#     stage's own wall-clock ceiling), counted from the moment the link was
#     minted and refreshed by nothing.
TIME_BUDGET_STEP = "step"
TIME_BUDGET_LINK = "link"
#: The THIRD answer, and the reason it is a value rather than an absence
#: (issue #2083). A session that died of a transport outage ran out of neither
#: clock, and the Pi used to say so by OMITTING the field — which the phone
#: renders as "the speaker reached its measurement attempt limit", a claim
#: about a third thing that did not happen either. Sending the negative
#: explicitly lets the page distinguish "no clock ran out" from "this Pi is too
#: old to say", which an omission cannot. An older page treats it as an unknown
#: budget name and falls back to exactly today's copy.
TIME_BUDGET_NONE = "none"


def expired_time_budget(exc: BaseException) -> str:
    """Which budget an ended session ran out of, or ``""`` if neither.

    Every one of these deaths persists as the same failure code, so without
    this classifier the only place the difference survived was an exception
    message — and the household was shown "the speaker reached its measurement
    attempt limit", which describes a third thing entirely.

    Deliberately conservative: an unclassifiable death answers ``""`` and the
    surfaces fall back to their existing generic copy. Naming the wrong clock
    would be worse than naming none, because "you have two minutes between
    taps" is advice, and advice about the wrong budget sends a household back
    to fail the same way.
    """
    if isinstance(exc, CaptureTimeout):
        return TIME_BUDGET_STEP
    # A purged or expired relay session answers 401/403/404 to the Pi exactly
    # as it does to the phone (see the page's own isDeadSessionError). A 5xx or
    # a transport OSError is an outage, not a budget.
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status in (401, 403, 404, 410):
        return TIME_BUDGET_LINK
    return ""


def is_transient_relay_failure(exc: BaseException) -> bool:
    """Whether ``exc`` is a relay OUTAGE worth re-polling, not a verdict.

    The complement of :func:`expired_time_budget`'s dead-session statuses, and
    the same classification ``jasper.web.correction_setup._post_relay_host_event``
    already retries on: a socket-level ``OSError`` (``TimeoutError`` and
    ``urllib``'s ``URLError`` are both subclasses), or a relay answer of 429 /
    5xx. Everything else — notably the 401/403/404/410 of a purged or expired
    session — is the relay telling us something true that repeating cannot
    change, so it must stay fatal.

    Note that an HTTP status never arrives here as an exception from the
    transport: ``_urllib_transport`` converts ``HTTPError`` into a response and
    ``RelayClient._require_ok`` re-raises it as ``RelayError``, so the status
    check below is the ONLY path by which a status is classified.
    """
    if isinstance(exc, RelayError):
        return exc.status == 429 or exc.status >= 500
    return isinstance(exc, OSError)


class RelayCapacityUnavailable(ValueError):
    """The DEPLOYED relay cannot store every blob index this plan would use.

    The sibling of :class:`CapturePageIncompatible` in WHAT it catches — the
    other half of the cross-deployment protocol: the page's skew is caught from
    its identity event, the relay Worker's from its ``GET /capabilities``
    document. Both fail before any tone plays; this one fails before the spec
    is even uploaded.

    It is deliberately NOT a sibling in base class, because the two are raised
    at different points in the lifecycle and the base class is what routes them
    to the right HTTP shape. ``CapturePageIncompatible`` is raised from the
    BACKGROUND runner once a session is live, so it lands on
    ``/status.relay = failed`` with translated copy. This one is raised in the
    FOREGROUND, inside ``register_session``, before a session exists at all —
    the same position every other start-time refusal occupies
    (``_require_relay_base``, the already-in-progress guard,
    ``CrossoverV2Refused``, ``ServerOwnedNextStepMismatch``), all of which are
    ``ValueError`` so ``correction_setup``'s dispatcher answers a clean 400
    carrying the message rather than a 500 with a traceback. The message names
    the cause AND the operator action, so the 400 body is the whole answer.

    PR-3b is the first release that can actually reach this path (it is the
    first to emit a plan larger than ``LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS``); the
    shape was chosen here rather than left to be discovered on a stale relay."""


class CaptureBeginRefused(RuntimeError):
    """The Pi refused a phone ``begin_capture`` request (capture plans).

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
    """A NON-terminal soft-hold on a phone's ``begin_capture`` (capture plans).

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


def advertised_plan_ceiling(document: Mapping[str, Any] | None) -> int | None:
    """The ceiling a capability document advertises, or ``None`` if none usable.

    Judges ONLY the document's contents. Whether the endpoint answered at all
    is :attr:`RelayCapabilities.served`'s job — keeping the two separate is what
    lets the refusal below name the right operator action instead of collapsing
    every failure into "your relay is old"."""
    if not isinstance(document, Mapping):
        return None
    value = document.get("max_capture_plan_attempts")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _assert_relay_plan_capacity(
    client: RelayClient, session: PiCaptureSession
) -> None:
    """Refuse a plan the deployed relay could not carry — BEFORE registering it.

    The cross-deployment skew this closes: `MAX_CAPTURE_PLAN_ATTEMPTS` is the
    Worker's blob-index space as well as the Pi's plan ceiling, and a relay
    deployed before the capacity raise rejects index >= 8 on both the phone's
    upload and the Pi's pull. Without this gate an oversized plan would fail on
    the ninth capture — after the operator had already walked eight prompted
    positions — which is exactly the mid-session failure the design contract
    forbids. Refusing here, ahead of `client.register`, also means a
    pre-capacity relay never even receives the oversized spec.

    Only plans larger than ``LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS`` probe. Every
    plan a pre-capacity Pi could already emit — the 3-entry driver set, the
    1-entry re-verify — issues the identical request sequence it always has.

    Dormant as shipped: no builder emits a plan this large yet
    (``crossover_v2_flow.CAPTURE_PLAN_MAX_ATTEMPTS`` is 8), so today this
    returns on its first line for every real session. It exists so that the
    multi-position choreography can land as a Pi-side change against an
    already-deployed relay."""
    plan = session.spec.capture_plan
    if plan is None or plan.max_attempts <= LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS:
        return
    probe = client.capabilities()
    advertised = advertised_plan_ceiling(probe.document)
    if advertised is not None and advertised >= plan.max_attempts:
        return
    # Four outcomes, four operator actions — never one message for all of them.
    if advertised is not None:
        cause = f"advertises a capture-plan ceiling of {advertised}"
        remedy = "Deploy the current relay Worker (cd relay && npx wrangler deploy)"
    elif not probe.served:
        cause = (
            "predates the capture-plan capacity release (it serves no "
            f"/capabilities endpoint, so its ceiling is "
            f"{LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS})"
        )
        remedy = "Deploy the current relay Worker (cd relay && npx wrangler deploy)"
    elif probe.document is None:
        # Answered 2xx with something that is not a capability document at all.
        # Deploying the Worker fixes nothing — the network path is intercepting.
        cause = (
            "answered /capabilities with a body that is not a capability "
            "document (a proxy, captive portal, or WAF is likely intercepting)"
        )
        remedy = "Check the network path to the relay"
    else:
        cause = "serves a /capabilities document carrying no usable ceiling"
        remedy = "Redeploy the relay Worker (cd relay && npx wrangler deploy)"
    log_event(
        logger,
        "capture_relay.plan_capacity_refused",
        session_id=session.session_id,
        kind=session.spec.kind,
        required=plan.max_attempts,
        # `None` renders as `null`, distinct from any real ceiling.
        advertised=advertised,
        served=probe.served,
    )
    raise RelayCapacityUnavailable(
        f"the relay at {client.base_url} {cause}, but this measurement needs "
        f"{plan.max_attempts}. {remedy} before running this measurement."
    )


def register_session(client: RelayClient, session: PiCaptureSession) -> dict:
    """Register the session + opaque spec with the relay."""
    _assert_relay_plan_capacity(client, session)
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
    # The phone's own account of the capture it just recorded (issue #2151):
    # whether the page lost the foreground during the recording window, and the
    # render-graph block counters. Diagnostic ONLY — nothing in the runner
    # branches on it; it exists so splice forensics can separate a capture-side
    # discontinuity from a host-side one without re-analysing the WAV. `None`
    # from any page that does not report it.
    capture_integrity: dict | None = None
    setup_identity: dict | None = None
    setup_validate: bool = False
    setup_token: str = ""
    capture_page: dict | None = None
    acknowledgement: dict | None = None
    # Session-spanning plans: the phone's current
    # `begin_capture {index, attempt}` request context, and the relay's
    # per-index blob summary (`{"<capture_index>": {size, integrity}}`).
    # Both `None` on a plan-free session — single-capture readers never
    # touch them.
    begin_capture: dict | None = None
    blobs: dict | None = None
    # The household's explicit set-completion signal
    # (``COMPLETE_CAPTURE_SET_EVENT_KEY``). Only ever read by a plan whose host
    # holds the set open past its capture target; ``False`` everywhere else.
    complete_capture_set: bool = False


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
    # The phone's per-take capture-integrity report (issue #2151). Diagnostic
    # only; carried so a retained capture can be self-describing about the
    # conditions it was recorded under. Threaded by the capture-PLAN runner,
    # which is the path the page that reports it actually uses — the
    # single-capture runner would only ever set it to `None`.
    capture_integrity: dict | None = None


class PhoneEventVerifier:
    """Verify the relay's mutable phone-event slot before host code reads it."""

    def __init__(self, session: PiCaptureSession) -> None:
        self._session = session
        self._sequence = 0
        self._event: dict[str, Any] | None = None
        self._stale_logged = False

    def verify(self, relay_event: Any) -> dict[str, Any] | None:
        if relay_event is None:
            return None
        # Every event is authenticated. Protocol 1 used to pass the relay's
        # slot through unverified; it is deleted, and no lab Pi emits it, so
        # there is no unauthenticated path left to fall into.
        verified, sequence = verify_authenticated_phone_event(
            self._session.content_key,
            self._session.session_id,
            relay_event,
        )
        if sequence < self._sequence:
            # An authenticated older last-write-wins slot carries no new state.
            # Log once per session while the stale slot persists.
            if not self._stale_logged:
                self._stale_logged = True
                log_event(
                    logger,
                    "capture_relay.capture_event_stale_ignored",
                    session_id=self._session.session_id,
                    kind=self._session.spec.kind,
                    stale_sequence=sequence,
                    accepted_sequence=self._sequence,
                )
            return None
        if (
            sequence == self._sequence
            and self._event is not None
            and verified != self._event
        ):
            raise CaptureIntegrityError(
                "authenticated capture event sequence conflicts with accepted payload"
            )
        if sequence > self._sequence:
            log_event(
                logger,
                "capture_relay.capture_event_sequence_accepted",
                session_id=self._session.session_id,
                kind=self._session.spec.kind,
                previous_sequence=self._sequence,
                current_sequence=sequence,
            )
        self._sequence = sequence
        self._event = verified
        return verified

    @property
    def sequence(self) -> int:
        """The last verified event sequence (0 before any verified event).

        The relay `event` slot persists between polls, so a plan runner needs
        the sequence to tell "the same event, still sitting there" from "the
        phone posted a new event" (begin dedup vs replay)."""
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
        self._client = client
        self._session = session
        self._verifier = PhoneEventVerifier(session)
        self._lock = threading.Lock()
        # In a capture-plan session "the recorder finished" is per-capture, not
        # per-session. A prior attempt's blob leaves the session-wide
        # `state == ready` set, so a plan-aware probe checks THIS capture's
        # index in the per-index blob map instead.
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
    capture_integrity = (
        event.get("capture_integrity")
        if isinstance(event.get("capture_integrity"), dict)
        else None
    )
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
    # Literal ``true`` only — a falsey or truthy-but-not-True value is not the
    # household's confirmation, and there is exactly one wire shape per meaning.
    complete_capture_set = event.get(COMPLETE_CAPTURE_SET_EVENT_KEY) is True
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
        capture_integrity=capture_integrity,
        setup_identity=setup_identity,
        setup_validate=setup_validate,
        setup_token=setup_token,
        capture_page=capture_page,
        acknowledgement=acknowledgement,
        begin_capture=begin_capture,
        blobs=blobs,
        complete_capture_set=complete_capture_set,
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
    poll loop and the capture-plan runner need."""
    relay_event = status.get("event") if isinstance(status, dict) else None
    if relay_event is None:
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


# --- Session-spanning plan runner (SPEC W2.3) ---------------------------------


class BeginCapture(NamedTuple):
    """One parsed ``begin_capture`` request.

    ``retake`` is the VOLUNTARY-retake marker (flow-simplification §2.6): the
    household asking to re-measure the capture that JUST completed, because
    they sneezed, a truck passed, or they were not where they meant to be. It
    is a shape the ordering contract admits, never permission to skip it — see
    ``_poll_capture_plan``. A page that never sends the key parses as ``False``
    and behaves exactly as it did before the key existed.
    """

    # ``index`` shadows ``tuple.index`` — legal at runtime (NamedTuple fields
    # win over the inherited method) and the right domain name here, since the
    # wire field this parses IS ``begin_capture.index``. mypy reports the
    # shadowing as an assignment error, so it is silenced at the one line that
    # causes it rather than by renaming the field away from its own protocol.
    index: int  # type: ignore[assignment]
    attempt: int
    retake: bool = False


BEGIN_CAPTURE_RETAKE_KEY = "retake"

# A voluntary retake that arrives after its window shut. Not a protocol fault:
# the page rendered the control, the household pressed it, and the plan moved on
# in between (issue #2090 — the owner's 2026-08-03 verify). It is refused like
# any other begin the ordering contract cannot admit — accepting it would land
# evidence on a slot the plan has already moved past — but under its OWN code
# and copy, for two reasons the generic ordering refusal cannot serve:
#
#   * The phone renders a refusal's ``error`` string VERBATIM (it does not
#     branch on ``code``), so the alternative is a household reading
#     "expected capture 2 attempt 2" off a run-day screen.
#   * ``code`` is what a journal is greppable by. #2090 was diagnosed by mining
#     a session for ``retake=true`` authorizations, finding none, and being
#     unable to tell "the press arrived and lost the race" from "the press never
#     arrived at all". This code makes the first one a positive fact.
BEGIN_REFUSED_RETAKE_TOO_LATE = "retake_too_late"
# Deliberately the same sentence the page shows when its OWN mirror of the
# window catches the press first (``RETAKE_TOO_LATE_MESSAGE`` in
# capture-page/js/main.js): whichever side answers, the household reads the same
# thing. Names the state they can see, not the protocol fact behind it.
RETAKE_TOO_LATE_MESSAGE = (
    "Too late to redo that spot — the next measurement already started."
)


def parse_begin_capture(
    payload: Any,
    *,
    capture_target: int,
    max_attempts: int,
) -> BeginCapture:
    """Strictly parse a phone ``begin_capture`` request.

    Shape validation only — ordering and the Pi-owned attempt budget are
    enforced by the plan runner plus the host's injected admission
    (``repeat_admission``). ``index`` is the 1-based measurement slot
    (``"Measurement N of {capture_target}"``); ``attempt`` is the 1-based
    admission attempt whose blob rides relay ``capture_index = attempt - 1``;
    the optional ``retake`` flag (see :class:`BeginCapture`) must be literally
    ``true`` when present. Raises ``CaptureBeginRefused`` (code
    ``begin_malformed``) on any drift — no Postel-style liberality on an
    authenticated control field."""
    if not isinstance(payload, Mapping):
        raise CaptureBeginRefused(
            "begin_malformed", "begin_capture must be an object"
        )
    keys = set(payload)
    if keys not in ({"index", "attempt"},
                    {"index", "attempt", BEGIN_CAPTURE_RETAKE_KEY}):
        raise CaptureBeginRefused(
            "begin_malformed",
            "begin_capture must carry index and attempt, and at most retake",
        )
    retake = payload.get(BEGIN_CAPTURE_RETAKE_KEY, False)
    if BEGIN_CAPTURE_RETAKE_KEY in keys and retake is not True:
        # Only the literal ``true`` — a page that means "not a retake" omits
        # the key rather than sending a falsey value, so there is exactly one
        # wire shape per meaning.
        raise CaptureBeginRefused(
            "begin_malformed", "begin_capture.retake must be true when present"
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
    return BeginCapture(index, attempt, bool(retake))


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
    # Whether this attempt was a VOLUNTARY retake of an already-accepted slot
    # (flow-simplification §2.6). Reporting only — the runner has already
    # applied the one behavioural consequence (an accepted retake replaces
    # rather than counts). Defaulted so every existing constructor call and
    # every reader that ignores it is unaffected.
    retake: bool = False


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
    completion_signal_required: Callable[[], bool] | None = None,
    on_completion_signal: Callable[[], None] | None = None,
) -> list[PlanCaptureOutcome]:
    """Run one session-spanning capture SET (SPEC W2.3).

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

    A verdict may additionally carry ``terminal: true`` when the host can
    decide at consume time that no later capture can make the set usable (for
    example, a per-position retry bound spent below a required position
    floor). The runner publishes that exact ``capture_result`` and returns
    immediately; it never waits for a next begin whose only possible answer is
    refusal. The host owns the accompanying ``reason`` and
    ``terminal_outcome`` copy/state.

    The phone NEVER decides admission; out-of-order, replayed, or malformed
    begins are refused loudly (named host event + ``CaptureFailed``). Failure
    semantics — timeout phases, abort, stop, cues, logging — mirror
    ``run_capture``. Accepted captures the host already committed durably
    (ledger writes inside ``consume_capture``) persist across a later abort or
    Stop; this runner never rolls them back.

    **Voluntary retakes (flow-simplification §2.6).** The ordering contract
    above admits EXACTLY one extra shape: a begin for ``accepted_count`` — the
    slot that just completed — carrying ``retake: true``
    (:class:`BeginCapture`), on the next attempt number, and only while the
    begin for the next entry has not been seen yet. It exists so a household
    can re-measure a spot simply because they want to (they sneezed, a truck
    passed) rather than only when the Pi rejected it. What it deliberately
    does NOT do:

    * ``accepted_count`` never rewinds, and an accepted retake never advances
      it — the slot was counted once and stays counted, so the completion
      check and every index→phase lookup are untouched.
    * The capture flows through the normal authorize → arm → upload → verdict
      path for that slot. REPLACING the retained evidence on acceptance is the
      host's job (the v2 session's position retention is already per-index
      idempotent), which is also why a rejected or abandoned retake leaves the
      original take standing: nothing was dropped on its behalf.
    * The attempt budget is the plan's own ``max_attempts`` — a retake spends
      an attempt like any other admission, so the feature is bounded by
      construction with no second budget to reason about.

    A page that never sends the marker cannot reach any of it, and the relay
    Worker stays byte-opaque throughout (the marker is payload).

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
    a NON-terminal "not yet" — e.g. the v2 crossover session's heterogeneous
    plan parked between MEASURE and VERIFY while its own auto-apply is in
    flight (jasper.active_speaker.crossover_v2_flow). Read that example as
    archaeology: the 2026-07-20 ruling it rested on was superseded by two-stage
    T3, which moved the apply out of the session entirely — see
    :data:`REVIEW_HOLD_BUDGET_S`'s comment above for why no shipped plan
    reaches it. The Pi posts a
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

    **A HELD set: ending the set is the household's decision (two-stage
    commission work order D1).** ``completion_signal_required`` and
    ``on_completion_signal`` are wired as a PAIR, or not at all. When the target
    is met the runner asks ``completion_signal_required()``; a ``True`` answer
    means the host is not finished with this set and the runner neither posts
    ``capture_set_complete`` nor returns. It stays in ``awaiting_begin`` — so
    the just-accepted slot is still retakeable on exactly the terms above — and
    waits for the phone's authenticated
    :data:`COMPLETE_CAPTURE_SET_EVENT_KEY`, whose arrival calls
    ``on_completion_signal()`` (which may raise
    :class:`CaptureBeginRefused` and is published to the phone identically to
    an admission refusal) and only then completes the set. The wait spends the
    ordinary between-capture ``awaiting_begin`` budget; NO new time budget is
    introduced. The crossover-v2 host uses this so the pre-apply cloud's group
    close — the fit — runs on the household's confirmation rather than on the
    arithmetic of a counter reaching a target.
    """
    plan = session.spec.capture_plan
    if plan is None:
        raise CaptureFailed("run_capture_plan requires a capture_plan spec")
    if (completion_signal_required is None) != (on_completion_signal is None):
        # Half-wired is the one shape that fails silently and expensively: a
        # held set with no handler parks a household on the confirm screen
        # until the inactivity budget kills the session.
        raise CaptureFailed(
            "completion_signal_required and on_completion_signal must be "
            "wired together"
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
            completion_signal_required=completion_signal_required,
            on_completion_signal=on_completion_signal,
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
    completion_signal_required: Callable[[], bool] | None = None,
    on_completion_signal: Callable[[], None] | None = None,
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

    def retakes_the_just_accepted_slot(index: int, wants_retake: bool) -> bool:
        """Whether a begin is a VOLUNTARY retake of the slot that just
        completed — the one extra shape §2.6 recognises.

        Read off the MARKER rather than inferred from the index alone: an
        unmarked begin for an already-accepted index is an ordinary
        out-of-order fault and must keep reading as one, which is what lets an
        OLD page (one that never sends the key) behave exactly as it always
        did. Says nothing about whether the retake is still ADMISSIBLE — the
        window (``next_begin_seen``) is a separate question, and the two
        callers that ask this while refusing are asking precisely because the
        window is shut.
        """
        return wants_retake and index == accepted_count

    def lost_the_retake_race(index: int, wants_retake: bool) -> bool:
        """Whether a begin about to be REFUSED is a voluntary retake that lost
        its race with the plan's own forward motion (issue #2090).

        ``next_begin_seen`` is the whole test, and it is exactly what the named
        copy asserts — the next measurement has started. It is what makes the
        sentence true rather than merely plausible:

        * while the in-flight capture is itself a retake of this same slot it
          is False (a retake never sets it), so a second press is not told the
          plan moved on when a redo of this very spot is running; and
        * a marked retake carrying the wrong attempt number is refused with the
          window still OPEN, so it is not told it arrived too late either.

        Both keep the generic ordering refusal, which is true of them.
        """
        return retakes_the_just_accepted_slot(index, wants_retake) and next_begin_seen

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

    def post_set_complete(
        accepted: int, final_verdict: Mapping[str, Any] | None = None,
    ) -> None:
        unresolved = (
            final_verdict.get("unresolved")
            if isinstance(final_verdict, Mapping)
            and isinstance(final_verdict.get("unresolved"), Mapping)
            else None
        )
        client.post_host_event(
            session.session_id,
            session.pull_token,
            {
                "phase": HOST_PHASE_CAPTURE_SET_COMPLETE,
                "accepted": accepted,
                "capture_target": plan_target,
                # The relay host slot is last-write-wins. The final
                # capture_result can be replaced by this event before the
                # phone polls, so carry the one final-position disclosure the
                # completion screen must not lose.
                **({"unresolved": dict(unresolved)} if unresolved else {}),
            },
        )

    outcomes: list[PlanCaptureOutcome] = []
    accepted_count = 0
    attempts_used = 0
    # Whether the target is met but the HOST is holding the set open for the
    # household's explicit completion signal (work order D1). Set at the
    # target-met branch and re-evaluated there after every later upload, so a
    # voluntary retake of the final slot re-enters the hold rather than ending
    # the set behind the household's back.
    completion_pending = False
    current: tuple[int, int] | None = None
    # Whether the in-flight attempt is a VOLUNTARY retake of the just-accepted
    # slot (§2.6). It changes exactly one thing downstream: an accepted retake
    # must NOT advance ``accepted_count``, because the slot it re-measures was
    # already counted — double-counting would finish the set one capture early.
    current_retake = False
    # Whether the begin for the NEXT entry has already been seen this slot
    # (admitted, or parked on a deferral). Closes the retake window; cleared
    # each time ``accepted_count`` advances, which opens the next one.
    next_begin_seen = False
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
    capture_integrity: dict | None = None
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
    # When the current run of CONSECUTIVE status-poll transport failures began
    # (``None`` while the relay is answering). Anchored at the start of the
    # first failing call, and cleared by any success — see
    # STATUS_POLL_TRANSIENT_GRACE_S for why the bound is wall-clock.
    transient_since: float | None = None
    # Every tolerated poll this SESSION, across outages — never reset, because
    # bounding a flap is the whole point (see STATUS_POLL_WARN_BUDGET).
    transient_total = 0
    # The blob pull's OWN outage anchor (``None`` while pulls are succeeding),
    # cleared only by a successful pull. Deliberately not the one above — see
    # BLOB_PULL_TRANSIENT_GRACE_S for why sharing it would never expire.
    blob_pull_stalled_since: float | None = None
    while True:
        raise_if_stopped()
        attempt_started = monotonic()
        try:
            status = client.status(session.session_id, session.pull_token)
        except (OSError, RelayError) as exc:
            if not is_transient_relay_failure(exc):
                raise
            if transient_since is None:
                transient_since = attempt_started
            # Deliberately NOT named waited_s: the timeout path further down
            # owns that name for a different quantity (which phone-inactivity
            # budget was in force), and this is how long the RELAY has been
            # unreachable.
            stalled_for_s = monotonic() - transient_since
            if stalled_for_s > STATUS_POLL_TRANSIENT_GRACE_S:
                # Not an outage any more — a dead relay. Re-raise the ORIGINAL
                # exception so the caller classifies this exactly as it always
                # has (``expired_time_budget`` reads its ``status``).
                log_event(
                    logger,
                    "capture_relay.status_poll_gave_up",
                    level=logging.WARNING,
                    session_id=session.session_id,
                    phase=phase,
                    stalled_for_s=round(stalled_for_s, 1),
                    tolerated_total=transient_total,
                    error=type(exc).__name__,
                )
                raise
            transient_total += 1
            log_event(
                logger,
                "capture_relay.status_poll_transient",
                level=(
                    logging.WARNING
                    if transient_total <= STATUS_POLL_WARN_BUDGET
                    else logging.DEBUG
                ),
                session_id=session.session_id,
                phase=phase,
                stalled_for_s=round(stalled_for_s, 1),
                grace_s=STATUS_POLL_TRANSIENT_GRACE_S,
                tolerated_total=transient_total,
                error=type(exc).__name__,
            )
            sleep(poll_interval_s)
            continue
        transient_since = None
        raise_if_stopped()
        status = _verify_phone_event(client, session, event_verifier, status)
        state = classify_status(status)
        if state.device is not None:
            capture_device = state.device
        if state.noise_floor is not None:
            capture_noise_floor = state.noise_floor
        if state.setup is not None:
            capture_setup = state.setup
        # The page posts this strictly BEFORE its blob, and both are read from
        # the same status document — so any poll that finds the blob ready has
        # already carried the report past this line. Nothing waits for it: a
        # page that never sends one simply leaves it `None`.
        if state.capture_integrity is not None:
            capture_integrity = state.capture_integrity

        if state.aborted:
            raise CaptureAborted(
                f"phone aborted the capture ({state.abort_reason or 'no reason'})",
                reason=state.abort_reason,
            )

        # The household's explicit set-completion signal (work order D1). Only
        # honoured while the set is actually held AND nothing is in flight —
        # a signal that landed while a voluntary retake was recording must not
        # close the group out from under the capture that is still running.
        if (
            completion_pending
            and phase == "awaiting_begin"
            and state.complete_capture_set
        ):
            raise_if_stopped()
            log_event(
                logger,
                "capture_relay.plan_completion_signalled",
                session_id=session.session_id,
                accepted=accepted_count,
            )
            assert on_completion_signal is not None  # paired by run_capture_plan
            try:
                on_completion_signal()
            except CaptureBeginRefused as refusal:
                # The host refused ON the confirmation (the v2 session's
                # pre-apply accountability veto is the shipped case). Same
                # publication an admission refusal gets, so the phone renders
                # the named reason rather than waiting out a budget.
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
                raise
            post_set_complete(accepted_count)
            log_event(
                logger,
                "capture_relay.plan_complete",
                session_id=session.session_id,
                accepted=accepted_count,
                attempts=attempts_used,
                completion_signalled=True,
            )
            return outcomes

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
                    index, attempt, wants_retake = parse_begin_capture(
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
                    # A capture is already authorized. When it is the FORWARD
                    # one, a retake press here lost its race by a whole capture.
                    refuse_code, refuse_message = (
                        (BEGIN_REFUSED_RETAKE_TOO_LATE, RETAKE_TOO_LATE_MESSAGE)
                        if lost_the_retake_race(index, wants_retake)
                        else (
                            "begin_out_of_order",
                            "a capture attempt is already in progress",
                        )
                    )
                    refuse_begin_order(index, attempt, refuse_code, refuse_message)
                elif pair != (accepted_count + 1, attempts_used + 1) and not (
                    # The ONE additional admitted shape (flow-simplification
                    # §2.6): a VOLUNTARY retake of the capture that just
                    # completed. It is a begin for ``accepted_count`` — the
                    # just-accepted slot, never a rewind of the counter — on
                    # the next attempt number, marked as a retake so an
                    # ordinary out-of-order begin stays refused, and admitted
                    # ONLY while the runner is still waiting for the next
                    # entry's begin. Once that next begin has been seen
                    # (admitted OR parked on a deferral, e.g. the apply hold
                    # the household's "continue" tap starts), the window is
                    # shut: work has moved on, and a late retake would land
                    # evidence after the group it belongs to was consumed.
                    wants_retake
                    and accepted_count > 0
                    and not next_begin_seen
                    and pair == (accepted_count, attempts_used + 1)
                ):
                    # Nothing is in flight here, so a retake that lost its race
                    # lost it to a begin parked on a DEFERRAL — the v2 apply
                    # hold the household's "continue" tap starts.
                    refuse_code, refuse_message = (
                        (BEGIN_REFUSED_RETAKE_TOO_LATE, RETAKE_TOO_LATE_MESSAGE)
                        if lost_the_retake_race(index, wants_retake)
                        else (
                            "begin_out_of_order",
                            (
                                f"expected capture {accepted_count + 1} attempt "
                                f"{attempts_used + 1}"
                            ),
                        )
                    )
                    refuse_begin_order(index, attempt, refuse_code, refuse_message)
                else:
                    raise_if_stopped()
                    # A retake never advances ``accepted_count``, so the slot
                    # it re-measures stays accepted throughout — the
                    # completion check, the index→phase lookups, and the
                    # host's own bookkeeping are all untouched by it.
                    #
                    # Read off the MARKER rather than inferred from the index:
                    # the two are equivalent here (only the retake branch above
                    # can admit ``index == accepted_count``), but an inference
                    # that is only true because of a condition twenty lines up
                    # is the kind that stops being true after an edit. The same
                    # predicate the two refusal sites grade a LOST race with, so
                    # "what counts as a retake" has one definition.
                    is_retake = retakes_the_just_accepted_slot(index, wants_retake)
                    if not is_retake:
                        next_begin_seen = True
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
                        # A new attempt reports its OWN condition or none at
                        # all. The page's report is best-effort (a diagnostic
                        # never costs it a good capture), so without this clear
                        # a dropped report would let the previous attempt's
                        # verdict describe this one's WAV — the exact
                        # mis-attribution the field exists to prevent.
                        capture_integrity = None
                        current_retake = is_retake
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
                            # An admitted retake and an ordinary begin look
                            # identical by (index, attempt) alone once you are
                            # reading a journal after the fact — say which it
                            # was at ADMISSION time, not only in plan_result.
                            retake=is_retake,
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
                    parsed = parse_begin_capture(
                        context,
                        capture_target=plan_target,
                        max_attempts=plan_max_attempts,
                    )
                except CaptureBeginRefused:
                    context_pair = None
                else:
                    # Compare the SLOT identity only: the page re-sends the
                    # begin payload verbatim on ``armed``, so a retake's
                    # context legitimately carries the retake marker too, and
                    # the marker is an admission input, not part of "which
                    # capture is armed".
                    context_pair = (parsed.index, parsed.attempt)
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
                pull_started = monotonic()
                try:
                    blob, header_integrity = client.pull_blob(
                        session.session_id,
                        session.pull_token,
                        capture_index=capture_index,
                    )
                except (OSError, RelayError) as exc:
                    # The capture is UPLOADED and safe on the relay; only our
                    # fetch of it failed. Giving up here is the one failure
                    # that voids work the household already did right, so a
                    # transient stall is re-pulled rather than fatal
                    # (issue #1650). See BLOB_PULL_TRANSIENT_GRACE_S.
                    if not is_transient_relay_failure(exc):
                        raise
                    if blob_pull_stalled_since is None:
                        blob_pull_stalled_since = pull_started
                    stalled_for_s = monotonic() - blob_pull_stalled_since
                    if stalled_for_s > BLOB_PULL_TRANSIENT_GRACE_S:
                        # Re-raise the ORIGINAL exception, so every caller
                        # classifies this exactly as it did before the
                        # tolerance existed.
                        log_event(
                            logger,
                            "capture_relay.blob_pull_gave_up",
                            level=logging.WARNING,
                            session_id=session.session_id,
                            index=index,
                            attempt=attempt,
                            capture_index=capture_index,
                            stalled_for_s=round(stalled_for_s, 1),
                            error=type(exc).__name__,
                        )
                        raise
                    log_event(
                        logger,
                        "capture_relay.blob_pull_transient",
                        level=logging.WARNING,
                        session_id=session.session_id,
                        index=index,
                        attempt=attempt,
                        capture_index=capture_index,
                        stalled_for_s=round(stalled_for_s, 1),
                        grace_s=BLOB_PULL_TRANSIENT_GRACE_S,
                        error=type(exc).__name__,
                    )
                    sleep(poll_interval_s)
                    continue
                blob_pull_stalled_since = None
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
                    capture_integrity=capture_integrity,
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
                        retake=current_retake,
                    )
                )
                if accepted and not current_retake:
                    # A retake re-measures a slot that is ALREADY counted, so
                    # its acceptance replaces evidence rather than adding a
                    # capture (the swap is the host's — see the v2 session's
                    # per-index position retention). Counting it here would
                    # complete the set one capture short of the plan.
                    accepted_count += 1
                    next_begin_seen = False
                log_event(
                    logger,
                    "capture_relay.plan_result",
                    session_id=session.session_id,
                    index=index,
                    attempt=attempt,
                    accepted=accepted,
                    retake=current_retake,
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
                if verdict.get("terminal") is True:
                    log_event(
                        logger,
                        "capture_relay.plan_terminal_result",
                        level=logging.WARNING,
                        session_id=session.session_id,
                        index=index,
                        attempt=attempt,
                        code=str(verdict.get("code") or ""),
                        outcome=str(verdict.get("terminal_outcome") or ""),
                    )
                    return outcomes
                if accepted_count >= plan_target:
                    completion_pending = bool(
                        completion_signal_required is not None
                        and completion_signal_required()
                    )
                    if completion_pending:
                        # HELD (work order D1): the target is arithmetic, the
                        # end of the set is a decision. Post nothing — the
                        # phone's last verdict is the one it renders the
                        # confirm screen from, and a ``capture_set_complete``
                        # here would win the relay's last-write-wins host slot
                        # and tell the household the session was over.
                        log_event(
                            logger,
                            "capture_relay.plan_awaiting_completion",
                            session_id=session.session_id,
                            accepted=accepted_count,
                            attempts=attempts_used,
                        )
                        phase = "awaiting_begin"
                        # **This budget WIDENS, and the consequence is
                        # recorded rather than discovered.** There is no entry
                        # past a stage-1 plan's target, so ``begin_budget``
                        # finds no ``screen`` and falls back to ``timeout_s``
                        # — 120 s. The single-session plan this replaced had
                        # VERIFY sitting at that index carrying
                        # ``AUTO_ADVANCE_ON_APPLY``, so the same screen used
                        # to get ``REVIEW_HOLD_BUDGET_S`` (30 s).
                        #
                        # That is 4× longer for the household to sit on the
                        # confirm screen, and the window in which a voluntary
                        # or geometry retake of the final position can re-close
                        # the group widens with it — which was issue #1872's
                        # duplicate-close path. Widening the window ships
                        # deliberately: closing it would also close the
                        # legitimate voluntary-retake case (the whole point of
                        # this hold), so #1872's fix lives on the SESSION
                        # side instead of here, and does not shut this window
                        # at all. ``_close_cloud_group`` still re-enters and
                        # fully RECOMPUTES on every retake of the final index —
                        # the geometry verdict, ``_group_cloud_result`` (what
                        # the FIT and the candidate's fingerprinted
                        # ``exclusion_evidence`` read), and the
                        # ``cloud_group_complete`` / ``cloud_spec`` journal
                        # lines all describe whichever cloud is CURRENTLY
                        # retained, every time. The one thing that is a
                        # per-phase singleton is the durable EVIDENCE ARTIFACT
                        # write — see the ``publish_cloud`` guard inside
                        # :meth:`CrossoverV2Session._run_cloud_pipeline`,
                        # keyed by ``self._group_cloud_published`` and marked
                        # only on a SUCCESSFUL write. The write-once evidence
                        # store refuses a write only when its bytes DIFFER from
                        # what is already there (an identical retry is accepted
                        # idempotently), so a re-close's recomputed — and
                        # normally different — bytes would be refused if
                        # re-attempted; the guard skips that attempt outright
                        # rather than spend it on a call that cannot succeed.
                        # The FIT still cannot double-fire either, because
                        # ``self._candidate`` guards it fire-once.
                        deadline = monotonic() + begin_budget(accepted_count + 1)
                        continue
                    post_set_complete(accepted_count, verdict)
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
                # A HELD set has no next capture to begin — the target is met
                # and the runner is waiting on the household's own signal — so
                # naming one would send a support read looking for a capture
                # that was never expected. Say what actually ran out.
                detail = (
                    "the household never confirmed the finished measurement "
                    f"within {waited_s:.0f}s (their phone ended the walk, or "
                    "the page went stale at the confirm screen)"
                    if completion_pending
                    else f"phone never began the next capture within {waited_s:.0f}s"
                )
            raise CaptureTimeout(
                f"{detail} (session {session.session_id})",
                phase=phase,
            )
        sleep(poll_interval_s)


def purge(client: RelayClient, session: PiCaptureSession) -> bool:
    """Delete the session from the relay after a verified pull (best-effort —
    the short TTL is the backstop). Return whether deletion completed so an
    owning cleanup path can report the otherwise-swallowed transport fault."""
    try:
        client.delete(session.session_id, session.pull_token)
    except (OSError, RelayError):
        return False
    return True
