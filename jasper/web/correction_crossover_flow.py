# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
from http import HTTPStatus
from typing import Any

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs


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
        <a href="http://{html.escape(hostname, quote=True)}/sound/">Remove the active crossover entirely</a>
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

  <section id="crossover-cloud" class="info-card" aria-label="Before and after measurement" hidden>
    <p class="eyebrow">Before and after</p>
    <h2 class="section__title">What the microphone heard</h2>
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
    <div id="crossover-relay" hidden>
      <p id="crossover-relay-status" class="form-hint"></p>
      <a id="crossover-relay-link" class="btn btn--primary" href="#" target="_blank" rel="noopener" hidden>Open measurement page</a>
      <div id="crossover-relay-qr" class="relay-qr"></div>
      <button id="crossover-relay-stop" class="btn btn--danger" type="button" hidden>Stop measurement</button>
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
    *, relay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    from . import correction_crossover_backend as backend

    payload = backend.status_payload()
    payload["relay"] = dict(relay) if relay else None
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


def handle_envelope(
    *, relay: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], HTTPStatus]:
    """GET /crossover/envelope: the server-computed commissioning screen envelope
    the dumb frontend renders each step from (revision plan §3.2), aligned with
    the room flow's envelope-driven pattern. Additive alongside /crossover/status;
    passive speakers get ``active=False`` (Layer A hidden)."""
    from jasper.active_speaker.crossover_envelope import (
        build_crossover_envelope_logged,
    )

    from .correction_crossover_v2 import attach_stage2_preflight

    status, _ = handle_status(relay=relay)
    # Two-stage commission D3 (PR-T2): the review screen's Apply may only be
    # offered on a box that can actually OPEN stage 2, so the same fail-closed
    # predicate the verify re-arm will run is resolved here and rides the
    # status the envelope renders from. A no-op on every phase but `review`;
    # see attach_stage2_preflight for the cost/side-effect disclosure and for
    # why it cannot live inside the (jasper.active_speaker) envelope builder.
    attach_stage2_preflight(status)
    envelope = build_crossover_envelope_logged(status)
    # The "Start over" confirm copy is grouping-aware; carry the (cheap,
    # fail-open) member flag on every polled envelope so the button that is
    # always visible confirms with copy that is true in the current state.
    envelope["grouping_member"] = _active_group_member()
    return envelope, HTTPStatus.OK


def handle_reset(
    *, relay: dict[str, Any] | None = None,
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
    from jasper.active_speaker.crossover_envelope import (
        build_crossover_envelope_logged,
    )

    try:
        reset_result = backend.reset_measurement_journey()
    except backend.MeasurementJourneyResetRefused as exc:
        return {
            "status": "refused",
            "reason": exc.reason,
            "error": str(exc),
        }, HTTPStatus.CONFLICT

    # Reset the durable v2 conductor JOURNEY too (W6.10 fold-in). Without this,
    # Start-over left the stale v2 candidate/verify/failure in place, so the v2
    # envelope re-rendered "Ready to start again" with stale verify-fail actions
    # and no start button instead of the clean microphone_check start screen
    # (round-1 finding #4). Start-over means "restart the measurement" — the
    # applied crossover keeps playing via the legacy applied-crossover contract,
    # so this only resets the guided journey, not what the speaker is emitting.
    # SELECTIVE (gate ruling): while a candidate is applied, the reset preserves
    # `applied` + `pre_apply_profile` so W6.8's Undo (handle_v2_restore) stays
    # reachable — a full clear would strand the household on the applied graph.
    from .correction_crossover_v2 import reset_v2_journey_state

    reset_v2_journey_state()

    status, _ = handle_status(relay=relay)
    envelope = build_crossover_envelope_logged(status)
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

