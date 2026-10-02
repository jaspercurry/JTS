# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS active-crossover microphone measurement flow."""

from __future__ import annotations

import html
import logging
from http import HTTPStatus
from typing import Any, Mapping

from jasper.platform.log_event import log_event
from jasper.active_speaker.capture_status import SESSION_ENDED_STATUSES
from .chrome import canonical_header, canonical_page

logger = logging.getLogger(__name__)


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    header = canonical_header(
        "Active speaker",
        back_href=f"http://{html.escape(hostname, quote=True)}/sound/speaker/",
        back_label="Speaker setup",
    )
    body = f"""
{header}
<main class="page correction-measurement" data-required-sr="48000">
  <section class="info-card info-card--accent">
    <p class="eyebrow">Speaker layer</p>
    <h2 class="section__title">Calibrate the active crossover</h2>
    <p id="crossover-verdict" class="form-hint">Checking the speaker…</p>
    <span id="crossover-applied" class="badge badge--idle" hidden></span>
    <div class="crossover-card__footer">
      <p class="form-hint">
        <a href="http://{html.escape(hostname, quote=True)}/sound/speaker/">Back to the speaker page</a>
      </p>
    </div>
  </section>

  <section class="info-card" aria-label="Crossover calibration progress">
    <ol id="crossover-steps" class="wizard-steps"></ol>
    <div id="crossover-nudges" aria-live="polite"></div>
  </section>

  <section class="info-card" aria-live="polite">
    <dl class="deflist"><dt>Round</dt><dd id="crossover-round-lines"></dd></dl>
    <div id="crossover-round-choice" hidden>
      <details><summary>Measurement options</summary><div class="field"><label for="crossover-round-select">Measurement plan</label><select id="crossover-round-select"></select></div></details>
      <div id="crossover-round-summary"></div>
      <div id="crossover-round-start" class="form-actions"></div>
    </div>
    <div id="crossover-walk" class="capture-walk" hidden>
      <!-- Page-local metric/imperial preference (#3629, #1941). Every
           prompt below already carries both units in one string
           (capture_plan.py's format_position_distance); the toggle only
           reorders which one leads -- see units.js. -->
      <div class="segmented" role="group" aria-label="Distance units">
        <button type="button" class="segmented__btn" id="crossover-units-imperial" aria-pressed="true">in</button>
        <button type="button" class="segmented__btn" id="crossover-units-metric" aria-pressed="false">cm</button>
      </div>
      <p id="crossover-walk-progress" class="eyebrow"></p>
      <!-- The per-position picture (#3629, #1941): speaker, the mark,
           and an arrow to this prompt's spot -- see position-diagram.js.
           Hidden whenever the prompt carries no bearing to draw. -->
      <div id="crossover-walk-diagram" class="position-diagram-wrap" hidden></div>
      <p id="crossover-walk-caption" class="form-hint position-diagram-caption" hidden></p>
      <!-- A paragraph, not a heading: this block appears and disappears
           inside the section's own `aria-live="polite"`, which announces
           the instruction already, and a transient h3 under a section with
           no h2 would put a hole in the page's heading outline. Same class,
           same weight, as `.measurement-row__title`'s paragraph. -->
      <p id="crossover-walk-headline" class="section__title"></p>
      <p id="crossover-walk-detail" class="form-hint"></p>
      <div id="crossover-walk-action" class="measurement-row__actions"></div>
    </div>
    <div id="crossover-action" class="measurement-row__actions"></div>
    <div id="crossover-capture" hidden>
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
        # slug, and every internal identifier stay "crossover".
        "Active speaker",
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


def _build_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    """Serve the v2 session crossover envelope for ``status`` and log the serve."""

    from jasper.active_speaker.crossover_envelope_v2 import (
        build_crossover_envelope_v2,
    )

    envelope = build_crossover_envelope_v2(status)
    envelope["snapshot_at"] = status.get("snapshot_at")
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        level=logging.DEBUG,
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
    *, capture: dict[str, Any] | None = None, selected_program: str = "",
) -> tuple[dict[str, Any], HTTPStatus]:
    """GET /crossover/envelope: the server-computed commissioning screen envelope
    the dumb frontend renders each step from (revision plan §3.2), aligned with
    the room flow's envelope-driven pattern. Additive alongside /crossover/status;
    passive speakers get ``active=False`` (Layer A hidden)."""
    status, _ = handle_status(capture=capture)
    envelope = _build_envelope_logged(status)
    live = status.get("capture") or {}
    if envelope["screen"] in {"awaiting_plan", "finished"} and (not live or live.get("status") in SESSION_ENDED_STATUSES):
        from jasper.active_speaker.measurement_view import round_choices  # lazy: planning reads measurement

        envelope["round_choices"] = round_choices(status, selected_program)
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

    The caller (``correction_handlers._handle_crossover_reset``) has already
    requested a stop of any crossover-owned capture before this runs; ``capture``
    here is only the freshest capture snapshot for the response, matching
    :func:`handle_status`/:func:`handle_envelope`.
    """
    from . import correction_crossover_backend as backend

    reset_result = backend.reset_measurement_journey()

    # The stored failure goes too, or the page would keep its failure screen.
    # The applied crossover keeps playing: Start Over resets the run, not the tune.
    from .correction_crossover_v2_state import reset_v2_journey_state

    reset_v2_journey_state()

    status, _ = handle_status(capture=capture)
    envelope = _build_envelope_logged(status)
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
