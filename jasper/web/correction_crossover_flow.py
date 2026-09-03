# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
import logging
from http import HTTPStatus
from typing import Any, Callable, Mapping

from ..log_event import log_event
from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

logger = logging.getLogger(__name__)

AsyncRunner = Callable[..., Any]
CamillaFactory = Callable[[], Any]


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    header = canonical_header(
        "Correction",
        back_href=f"http://{html.escape(hostname, quote=True)}/",
    )
    body = f"""
{header}
<main class="page correction-measurement crossover-page" data-required-sr="48000">
  {section_tabs("crossover")}

  <section class="info-card info-card--accent">
    <p class="eyebrow">Speaker layer</p>
    <h2 class="section__title">Calibrate the active crossover</h2>
    <p id="crossover-verdict" class="form-hint">Checking the speaker…</p>
    <span id="crossover-applied" class="applied-chip" hidden></span>
    <div class="crossover-card__footer">
      <button id="crossover-start-over" class="btn btn--ghost" type="button">Start over</button>
      <p class="form-hint">
        <a href="http://{html.escape(hostname, quote=True)}/sound/setup/">Remove the active crossover entirely</a>
        — this returns the speaker to a plain stereo crossover.
      </p>
    </div>
  </section>

  <section class="info-card" aria-label="Crossover calibration progress">
    <ol id="crossover-steps" class="wizard-steps"></ol>
    <div id="crossover-nudges" aria-live="polite"></div>
  </section>

  <section id="crossover-review" class="info-card" aria-label="Measured crossover details" hidden>
    <p class="eyebrow">What was measured</p>
    <h2 class="section__title">Measured crossover</h2>
    <div id="crossover-review-body"></div>
  </section>

  <!-- ONE section, two truths (issue #2152). Post-VERIFY it shows measured
       before/after curves and the heading below is exactly right. On the
       PRE-APPLY review screen — since R15 removed the pre-apply cloud
       (#2106) — the only curve is the PREDICTION, and "what the microphone
       heard" then contradicts its own "(not measured)" legend. The heading,
       eyebrow, aria-label, and the basis line are therefore set by
       cloud.js's `updateSectionFraming` from what is actually on the canvas;
       the markup below is the measured wording it falls back to. -->
  <section id="crossover-cloud" class="info-card" aria-label="Before and after measurement" hidden>
    <p id="crossover-cloud-eyebrow" class="eyebrow">Before and after</p>
    <h2 id="crossover-cloud-title" class="section__title">What the microphone heard</h2>
    <p id="crossover-cloud-basis" class="form-hint" hidden></p>
    <p id="crossover-cloud-provenance" class="form-hint"></p>
    <div class="crossover-chart-wrap">
      <canvas id="crossover-cloud-chart" aria-label="Frequency response before and after correction"></canvas>
    </div>
    <ul class="crossover-chart-legend">
      <li id="crossover-chart-legend-measure"><span class="crossover-chart-legend__swatch crossover-chart-legend__swatch--measure"></span>Before correction</li>
      <li id="crossover-chart-legend-verify"><span class="crossover-chart-legend__swatch crossover-chart-legend__swatch--verify"></span>After correction</li>
      <li id="crossover-chart-legend-predicted" hidden><span class="crossover-chart-legend__swatch crossover-chart-legend__swatch--predicted"></span>Expected after correction (not measured)</li>
      <li id="crossover-chart-legend-corridor"><span class="crossover-chart-legend__swatch crossover-chart-legend__swatch--corridor"></span>Spec tolerance</li>
      <li id="crossover-chart-legend-excluded"><span class="crossover-chart-legend__swatch crossover-chart-legend__swatch--excluded"></span>Excluded (interference)</li>
    </ul>
    <p id="crossover-cloud-pending" class="form-hint" hidden></p>
    <p id="crossover-cloud-geometry" class="form-hint" hidden></p>
    <div id="crossover-cloud-callouts"></div>
  </section>

  <section class="info-card" aria-live="polite">
    <div id="crossover-action" class="measurement-row__actions"></div>
    <div id="crossover-capture" hidden>
      <!-- The walkthrough: one prompted spot at a time, for a session whose
           operator is at THIS browser rather than on a capture page (#2881).
           Eyebrow (which spot) / title (the move) / hint (the supporting
           clause) is the same three-slot grammar the capture page's step
           screen uses, and the words in all three come from the capture
           plan, so the two surfaces read alike without sharing markup. -->
      <div id="crossover-walk" class="capture-walk" hidden>
        <p id="crossover-walk-progress" class="eyebrow"></p>
        <!-- A paragraph, not a heading: this block appears and disappears
             inside the section's own `aria-live="polite"`, which announces
             the instruction already, and a transient h3 under a section with
             no h2 would put a hole in the page's heading outline. Same class,
             same weight, as `.measurement-row__title`'s paragraph. -->
        <p id="crossover-walk-headline" class="section__title"></p>
        <p id="crossover-walk-detail" class="form-hint"></p>
        <div id="crossover-walk-action" class="measurement-row__actions"></div>
      </div>
      <p id="crossover-capture-status" class="form-hint"></p>
      <button id="crossover-capture-stop" class="btn btn--danger" type="button" hidden>Stop measurement</button>
    </div>
    <p id="capture-status" class="capture-status" role="status" aria-live="polite"></p>
  </section>
</main>
<script type="module" src="/assets/correction/js/crossover/main.js"></script>
"""
    return canonical_page(
        # User-facing browser-tab title only (#1670 rename) — the route,
        # slug, section_tabs key, and every internal identifier stay
        # "crossover".
        "Active speaker measurement — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/crossover.css",
    )


def handle_status(
    *, capture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = backend.status_payload()
    payload["capture"] = dict(capture) if capture else None
    return payload, HTTPStatus.OK


def _active_group_member() -> bool:
    """True when this speaker is an active multi-room group member.

    Read fresh from ``grouping.env`` via the pure declared-config predicates
    (``is_active_leader`` / ``is_bonded_follower``) — no cross-origin HTTP,
    so it is cheap to compute on the correction daemon. Fail-open to ``False``
    (a read failure must never over-warn a solo household). The "Start over"
    confirm copy uses this: a bonded speaker's group crossover is rebuilt from
    the CLEARED measurement evidence, so it needs re-measurement after a scoped
    reset (fail-safe to solo) — see ``jasper.active_speaker.reset`` and
    ``jasper.web.correction_crossover_backend.reset_measurement_journey``.
    """
    try:
        from jasper.multiroom.config import (
            is_active_leader,
            is_bonded_follower,
            load_config,
        )
    except ImportError:
        # Fail-open to the solo copy if the multiroom module is unavailable.
        return False
    # load_config is total (documented never-raises: a missing/unreadable
    # grouping.env resolves to the all-off config), and the two predicates are
    # pure, so no broad catch is warranted here.
    cfg = load_config()
    return is_active_leader(cfg) or is_bonded_follower(cfg)


def _build_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    """Serve the v2 session crossover envelope for ``status`` and log the serve."""

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    envelope = build_crossover_envelope_v2(status)
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        screen=envelope["screen"],
        active=envelope["active"],
        step_count=len(envelope["steps"]),
        nudge_count=len(envelope["nudges"]),
        action=(envelope.get("next_action") or {}).get("id"),
        alternate_action_count=len(envelope.get("alternate_actions") or []),
        applied=(envelope.get("applied") or {}).get("state"),
    )
    return envelope


def handle_envelope(
    *, capture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """GET /crossover/envelope: the server-computed commissioning screen envelope
    the dumb frontend renders each step from (revision plan §3.2), aligned with
    the room flow's envelope-driven pattern. Additive alongside /crossover/status;
    passive speakers get ``active=False`` (Layer A hidden)."""
    from .correction_crossover_v2 import attach_stage2_preflight

    status, _ = handle_status(capture=capture)
    # Two-stage commission D3: a stage-2 openability refusal is knowable now,
    # so it is resolved here and rides the status the envelope renders from
    # as a DISCLOSURE (the apply transaction owns the refusal). A no-op on
    # every phase but `review`; see attach_stage2_preflight for the
    # cost/side-effect disclosure and for why it cannot live inside the
    # (jasper.active_speaker) envelope builder.
    attach_stage2_preflight(status)
    envelope = _build_envelope_logged(status)
    # The "Start over" confirm copy is grouping-aware; carry the (cheap,
    # fail-open) member flag on every polled envelope so the button that is
    # always visible confirms with copy that is true in the current state.
    envelope["grouping_member"] = _active_group_member()
    return envelope, HTTPStatus.OK


def handle_reset(
    *, capture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/reset: scoped "start over" for the measurement journey.

    Clears comparison-set/level-lock state and the driver/summed/staged
    measurement evidence, then returns the same envelope shape
    :func:`handle_envelope` does so the page can re-render from a clean
    start screen in one round trip. Driver research and whatever crossover
    is currently applied/loaded are untouched — see
    ``jasper.web.correction_crossover_backend.reset_measurement_journey``.

    The caller (``correction_setup._handle_crossover_reset``) has already
    requested a stop of any crossover-owned relay before this runs; ``relay``
    here is only the freshest relay snapshot for the response, matching
    :func:`handle_status`/:func:`handle_envelope`.
    """
    from . import correction_crossover_backend as backend

    try:
        reset_result = backend.reset_measurement_journey()
    except backend.MeasurementJourneyResetRefused as exc:
        return {
            "status": "refused",
            "reason": exc.reason,
            "error": str(exc),
        }, HTTPStatus.CONFLICT

    # Reset the durable v2 session JOURNEY too (W6.10 fold-in). Without this,
    # Start-over left the stale v2 candidate/verify/failure in place, so the v2
    # envelope re-rendered "Ready to start again" with stale verify-fail actions
    # and no start button instead of the clean microphone_check start screen
    # (round-1 finding #4). Start-over means "restart the measurement" — the
    # applied crossover keeps playing via the legacy applied-crossover contract,
    # so this only resets the guided journey, not what the speaker is emitting.
    # SELECTIVE (gate ruling): while a candidate is applied, the reset preserves
    # `applied` + `pre_apply_profile` — the stash carrying the way back's
    # pointer — a full clear would strand the household on the applied graph.
    from .correction_crossover_v2 import reset_v2_journey_state

    reset_v2_journey_state()

    status, _ = handle_status(capture=capture)
    envelope = _build_envelope_logged(status)
    envelope["grouping_member"] = _active_group_member()
    # Surface the honest outcome, not the static intent: ``status`` is
    # ``partial`` when any file failed to unlink, and ``errors`` names them —
    # the page branches its message on this rather than always painting green.
    envelope["reset"] = {
        "status": reset_result.get("status"),
        "cleared": reset_result.get("cleared_ids"),
        "missing": reset_result.get("missing_ids"),
        "errors": reset_result.get("error_ids"),
        "kept": reset_result.get("kept_ids"),
    }
    return envelope, HTTPStatus.OK


def handle_v2_decline(
    body: Mapping[str, Any] | None = None,
    *,
    capture: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/v2/decline: "Keep current sound", as a real action.

    **What #2641 measured.** The review screen's decline was minted with an
    ``href`` and no endpoint, so the click reloaded the page and landed back on
    the SAME decision screen — a household that had decided was asked again,
    indefinitely — and the round record could not tell "the household declined"
    from "the household never looked". The second half is the one the machinery
    needs: a series that offers another bite has to know when the answer was no.

    **What this does NOT do**, deliberately, because the review screen's own
    docstring is the contract: it does not touch the speaker (declining changes
    nothing), and it does not delete the candidate (an accidental tap would
    otherwise cost ten captures to undo). The proposal stays reviewable until a
    newer measurement replaces it. What is recorded is the DECISION.

    ``expected_candidate_fingerprint`` is the same guard ``/v2/apply`` carries
    and refuses on the same way: a decline recorded against a candidate that
    has since been replaced would close a review the household never saw. An
    empty expectation is accepted — the review screen mints one when there is
    no candidate at all, and "there is nothing to propose, keep what you have"
    is a real decline.

    Returns the freshly-built envelope, the shape :func:`handle_reset` returns,
    so the page re-renders from its resting screen in one round trip rather
    than polling for it. This route reads state to CHECK the guard and hands
    the write to ``correction_crossover_v2``'s own locked writer, which is
    where every durable v2 write lives.
    """
    from .correction_crossover_v2 import load_v2_state, observe_review_decline

    payload = body if isinstance(body, Mapping) else {}
    expected = str(payload.get("expected_candidate_fingerprint") or "")

    state = load_v2_state()
    candidate = (state or {}).get("candidate")
    current = (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping) else ""
    )
    if expected and expected != current:
        log_event(
            logger, "correction.crossover_v2_review_decline_superseded",
            level=logging.WARNING,
            expected_candidate_fingerprint=expected,
        )
        return {
            "ok": False,
            "error": (
                "The crossover candidate changed while this screen was open. "
                "Review the current one."
            ),
            "issues": [{
                "code": "baseline_candidate_fingerprint_mismatch",
                "message": "the reviewed candidate is no longer the current one",
            }],
        }, HTTPStatus.CONFLICT

    observe_review_decline(current)
    return handle_envelope(capture=capture)


def playback_issue_text(payload: Any, fallback: str) -> str:
    """The one operator-facing reason a capture-sweep payload failed.

    Mirrors the same-origin JS ``issueMessage`` (crossover/main.js) — the
    canonical reader of these payloads: first ``issues[].message/label/code``,
    then the nested ``playback.issues``, then ``next_step``, then ``reason``,
    else the fallback. So a refusal ("Finish the other measurement…") or a
    rollback failure surfaces its real reason instead of a generic "did not
    play"."""
    if not isinstance(payload, dict):
        return fallback
    for source in (payload, payload.get("playback")):
        if not isinstance(source, dict):
            continue
        for issue in source.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            text = issue.get("message") or issue.get("label") or issue.get("code")
            if text:
                return str(text)
    for key in ("next_step", "reason"):
        value = payload.get(key)
        if value:
            return str(value)
    return fallback


def ensure_automatic_measurement_profile(
    status: dict[str, Any],
    run_async: AsyncRunner,
    camilla_factory: CamillaFactory,
    *,
    status_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Migrate a safe legacy manual graph before automatic measurement.

    A pre-snapshot manual profile is a valid playback anchor, but it cannot be
    the source of truth for per-role automatic excitation. ``Tune
    automatically`` is one sequential intent, so this boundary performs the
    existing manual-preservation transaction when—and only when—the setup
    contract proves the current source is an exact match. Unsafe preservation
    fails before relay registration or audio.
    """
    from . import correction_crossover_backend as backend

    setup = status.get("setup")
    setup = setup if isinstance(setup, dict) else {}
    applied = setup.get("applied_crossover")
    applied = applied if isinstance(applied, dict) else {}
    if applied.get("valid") is True:
        return status

    reason = str(applied.get("reason") or "")
    preservation = setup.get("manual_preservation")
    preservation = preservation if isinstance(preservation, dict) else {}
    baseline_profile = setup.get("baseline_profile")
    baseline_profile = baseline_profile if isinstance(baseline_profile, dict) else {}
    if reason == "active_applied_profile_snapshot_missing":
        if preservation.get("ready") is not True:
            detail = str(
                preservation.get("detail")
                or applied.get("detail")
                or "the current manual crossover cannot be preserved safely"
            )
            log_event(
                logger,
                "correction.crossover_legacy_profile_autopreserve",
                status="refused",
                reason=preservation.get("reason") or reason,
            )
            raise ValueError(detail)
        payload = run_async(
            backend.apply_profile(
                tuning_owner="manual",
                expected_candidate_fingerprint=str(
                    baseline_profile.get("candidate_fingerprint") or ""
                ),
                camilla_factory=camilla_factory,
            ),
            timeout=30.0,
        )
        if payload.get("status") != "applied":
            detail = playback_issue_text(
                payload,
                "the current manual crossover could not be preserved safely",
            )
            log_event(
                logger,
                "correction.crossover_legacy_profile_autopreserve",
                status="failed",
                reason=detail,
            )
            raise ValueError(detail)
        loader = status_loader or backend.status_payload
        status = loader()
        setup = status.get("setup")
        setup = setup if isinstance(setup, dict) else {}
        applied = setup.get("applied_crossover")
        applied = applied if isinstance(applied, dict) else {}
        log_event(
            logger,
            "correction.crossover_legacy_profile_autopreserve",
            status="applied" if applied.get("valid") is True else "failed",
            owner=applied.get("owner"),
        )

    # in-sequence-anchor-exempt: this gate is unreachable mid-sequence — a
    # running sequence has applied_crossover.valid True and early-returns
    # above before the legacy migration runs. It fires only immediately
    # after the legacy apply_profile this function just performed, which
    # repoints production itself (superseding any capture-entry stash), so
    # full "ready" is the correct proof that the fresh apply produced a
    # ready production graph. Do not admit the staged anchor here.
    if setup.get("status") != "ready":
        raise ValueError(
            str(
                setup.get("detail")
                or "the protected crossover setup is no longer ready"
            )
        )
    if applied.get("valid") is not True:
        raise ValueError(
            str(
                applied.get("detail")
                or "apply a protected crossover profile before measuring it"
            )
        )
    return status


def validate_current_level_target_context(
    status: Mapping[str, Any],
    *,
    current_topology_id: str,
    expected_topology_id: str,
    expected_profile_context_id: str,
    speaker_group_id: str,
    role: str,
    expected_target_fingerprint: str,
) -> None:
    """Reject a stale driver-level link before its isolated tone can load."""

    if current_topology_id != expected_topology_id:
        raise ValueError(
            "the speaker topology changed after this link was created; "
            "start driver level matching again"
        )
    setup = status.get("setup")
    setup = setup if isinstance(setup, Mapping) else {}
    protected = setup.get("protected_profile")
    protected = protected if isinstance(protected, Mapping) else {}
    applied = setup.get("applied_crossover")
    applied = applied if isinstance(applied, Mapping) else {}
    # Tone-prep for lock 2..N runs while the persisted config is the
    # all-muted staged anchor (PR #1523), so a raw ready requirement here
    # wedged the tweeter tone (run 11). Admit exactly the in-sequence state;
    # every remaining trigger of this error is a genuine changed-underneath
    # case (fingerprint/applied/other-blocked regression after the link was
    # created), so the copy stays accurate.
    from jasper.active_speaker.setup_status import (
        setup_blocked_only_by_in_sequence_anchor,
    )

    if (
        (
            setup.get("status") != "ready"
            and not setup_blocked_only_by_in_sequence_anchor(status)
        )
        or applied.get("valid") is not True
        or str(protected.get("candidate_fingerprint") or "")
        != expected_profile_context_id
    ):
        raise ValueError(
            "the protected crossover setup changed after this link was created; "
            "start driver level matching again"
        )
    targets = status.get("targets")
    targets = targets if isinstance(targets, Mapping) else {}
    rows = targets.get("drivers")
    rows = rows if isinstance(rows, list) else []
    target = next(
        (
            item
            for item in rows
            if isinstance(item, Mapping)
            and str(item.get("speaker_group_id") or "") == speaker_group_id
            and str(item.get("role") or "").lower() == role.lower()
        ),
        None,
    )
    if (
        not expected_target_fingerprint
        or not isinstance(target, Mapping)
        or str(target.get("target_fingerprint") or "")
        != expected_target_fingerprint
    ):
        raise ValueError(
            "the driver level target changed after this link was created; "
            "create a new level-check link"
        )
