# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTPS bass-management display flow (revision plan §3.3 / P5).

DISPLAY-ONLY. This page does NOT own the crossover corner — the SPEAKER layer
does (active-speaker LocalSubwoofer / the wireless-sub bond). It reads the live
bass-management state (:func:`jasper.bass_management.resolve_bass_management`)
and shows the household: what corner is bass-managing this speaker, who owns it
(active-speaker vs wireless sub), whether a sub is present, and whether the
mains high-pass is armed — plus a pointer to the Room tab, where the bass-region
measurement/correction already lives. There is no control surface here (no
corner picker, no apply) by design: the corner is set where the speaker is
commissioned, not here.
"""

from __future__ import annotations

import html
import logging
from http import HTTPStatus
from typing import Any

from ._common import canonical_header, canonical_page
from .correction_hub import section_tabs

logger = logging.getLogger(__name__)


def render_page(hostname: str, csrf_token: str = "") -> bytes:
    header = canonical_header(
        "Correction",
        back_href=f"http://{html.escape(hostname, quote=True)}/",
    )
    body = f"""
{header}
<main class="page correction-measurement bass-page">
  {section_tabs("bass")}

  <section class="info-card info-card--accent">
    <h2 class="section__title">Bass management</h2>
    <p class="form-hint">
      Where your subwoofer and speakers hand off — the crossover corner, who
      owns it, and whether the mains high-pass is on. This page is read-only:
      the corner is set when your speaker is set up, not here.
    </p>
  </section>

  <section id="bass-state" class="info-card" aria-live="polite">
    <h2 class="section__title">Current bass management</h2>
    <p id="bass-state-message" class="form-hint">Loading…</p>
    <dl id="bass-state-list" class="deflist" hidden></dl>
  </section>

  <section id="bass-extension-state" class="info-card" aria-live="polite" hidden>
    <h2 class="section__title">Bass extension</h2>
    <p id="bass-extension-message" class="form-hint">Loading…</p>
    <dl id="bass-extension-list" class="deflist" hidden></dl>
    <p id="bass-extension-recovery" class="wizard-nudge warn" hidden>
      A previous apply was interrupted — recovery pending.
    </p>
  </section>

  <section class="info-card">
    <h2 class="section__title">Bass-region correction</h2>
    <p class="form-hint">
      Your room's low-frequency response — and any correction — is measured on
      the Room tab. A dip right at the crossover is your speakers handing off to
      the subwoofer, not a room mode, so it is left alone there.
    </p>
    <a class="btn btn--ghost" href="/correction/room/">Go to Room measurement</a>
  </section>
</main>
<script type="module" src="/assets/correction/js/bass/main.js"></script>
"""
    return canonical_page(
        "Bass management — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/crossover.css",
    )


# Homeowner-facing labels for the corner owner. Stable strings, provider-
# agnostic. The wizard shows these verbatim — the JS never re-derives ownership.
_OWNER_LABELS = {
    "active_speaker_local": "This speaker's own subwoofer output",
    "wireless_sub": "A wireless subwoofer in this speaker's group",
}


def status_payload() -> dict[str, Any]:
    """The read-only bass-management display payload. Fail-soft.

    Reads the resolved bass-management state and adds a homeowner-facing owner
    label. Never raises — a read failure resolves to "no bass management," which
    the page shows as "not configured."
    """
    from jasper.bass_management import resolve_bass_management

    state = resolve_bass_management()
    payload: dict[str, Any] = state.to_dict()
    payload["owner_label"] = (
        _OWNER_LABELS.get(state.owner) if state.owner else None
    )
    payload["configured"] = state.corner_hz is not None

    # Bass Extension (docs/HANDOFF-bass-extension-plan.md §9) is a separate,
    # not-yet-household-launched feature that also lives on this tab. Its own
    # summary is already fail-soft (never raises), but guard the import + call
    # here too so a future change to that module can never take this
    # long-shipped bass-management page down with it — a broken read just
    # means "not shown," not "the whole tab 500s." Same narrow exception
    # tuple as the other bass_extension_state_summary() caller
    # (jasper.control.state_aggregate) rather than a blind except, so this
    # doesn't add to the frozen BLE001 suppression ratchet.
    try:
        from jasper.bass_extension.profile import bass_extension_state_summary

        payload["bass_extension"] = bass_extension_state_summary()
    except (
        ImportError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ):
        logger.exception("bass extension profile state read failed")
        payload["bass_extension"] = None
    return payload


def handle_status() -> tuple[dict[str, Any], HTTPStatus]:
    return status_payload(), HTTPStatus.OK
