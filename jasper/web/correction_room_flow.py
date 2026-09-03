# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction page rendering."""

from __future__ import annotations

import html
from typing import Any

from ._common import canonical_header, canonical_page, json_island
from .correction_hub import section_tabs


# ----------------------------------------------------------------------
# Page body (canonical design system).
# ----------------------------------------------------------------------
#
# /correction/ is a restyle-in-place migration onto the canonical look:
# the document shell is canonical_page() (app.css + CSRF meta + icon
# sprite); the chrome is canonical_header() + the shared .btn / card
# vocabulary. The page's mechanism layer — getUserMedia mic capture, the
# AudioWorklet level meter, the measurement-sweep + autolevel + verify
# state machine driven by polling GET /status, the canvas chart, and the
# session-report reader — ships as /assets/correction/js/main.js. The
# server-owned GET /envelope contract controls whole-page membership and
# order; the browser has no parallel screen-to-section policy.
#
# getUserMedia requires a secure context; /correction/ itself is plain HTTP
# and never redirects into the speaker's self-signed origin (issue #2632),
# which is what the local-capture certificate warning is for. The back link
# is an absolute http://<host>/ so the Home affordance lands on the plain-HTTP
# dashboard even when this page was opened over HTTPS by hand. Page-specific
# styling lives in /assets/correction/correction.css.


_PAGE_BODY = """
__HEADER__
<main class="page correction-stack" data-required-sr="__REQUIRED_SR__" data-level-trust-margin-db="__LEVEL_TRUST_MARGIN_DB__">
__TABS__
<p class="page-sub">Measure your room with a microphone and apply the result to the speaker.</p>

<!-- Stepped-wizard chrome (P3b). Server-computed screen envelope (GET
     /envelope) drives everything here: which step you're on, the one
     plain-language verdict, homeowner nudges (a sentence + severity, never
     a block), the single primary action (always live — nudges never
     disable it), and the step indicator. The workflow sections below stay;
     the router shows the ones the current step needs. -->
<section id="wizard-chrome" class="wizard-chrome" aria-live="polite">
  <ol id="wizard-steps" class="wizard-steps" aria-label="Room correction steps"></ol>
  <p id="wizard-verdict" class="wizard-verdict"></p>
  <div id="wizard-nudges" class="wizard-nudges"></div>
  <button id="wizard-next" type="button" class="btn btn--primary hidden"></button>
  <button id="cancel-measurement" type="button" class="btn btn--danger hidden">Cancel measurement</button>
</section>

<div id="envelope-sections" class="correction-sections">
<section id="current-correction" data-envelope-section="current-correction" class="flat hidden" aria-live="polite">
  <span class="label" id="current-correction-label">Checking current correction…</span>
  <button id="current-correction-reset" type="button" class="btn btn--danger hidden">Reset correction</button>
</section>

<!-- P6 tuning assistant. The envelope's sections list owns top-level
     visibility; tuning_llm fills the nudge/actions inside it. The paid call
     happens ONLY on a tap. -->
<section id="tuning-panel" data-envelope-section="tuning" class="tuning-panel hidden" aria-live="polite">
  <h2 class="tuning-title">Tuning assistant</h2>
  <p id="tuning-nudge" class="tuning-nudge hidden"></p>
  <div id="tuning-actions" class="tuning-actions hidden">
    <button id="tuning-interpret" type="button" class="btn">Explain my room</button>
    <button id="tuning-propose" type="button" class="btn">Suggest a tweak</button>
  </div>
  <p id="tuning-status" class="tuning-status hidden"></p>
  <div id="tuning-explanation" class="tuning-explanation hidden"></div>
  <p id="tuning-provenance" class="tuning-provenance hidden"></p>
  <div id="tuning-proposals" class="tuning-proposals"></div>
</section>

<section id="readiness-blocker" data-envelope-section="readiness-blocker" class="info-card hidden" role="alert">
  <p id="readiness-blocker-message"></p>
  <a id="readiness-blocker-action" class="btn hidden" href=""></a>
</section>

<section id="capture-handoff" data-envelope-section="capture-handoff" class="info-card hidden" aria-live="polite">
  <p id="capture-handoff-copy" class="hint"></p>
  <div id="relay-link-row" class="relay-link-row hidden">
    <a id="relay-tap-link" class="btn btn--primary" href="#" target="_blank" rel="noopener">Open measurement page</a>
    <div id="relay-qr" class="relay-qr"></div>
  </div>
  <p id="relay-status" class="relay-status"></p>
</section>

<section id="placement" data-envelope-section="placement" class="info-card hidden">
  <h2 class="section__title">Place the microphone</h2>
  <p id="placement-instruction">Put the microphone at head height where you normally listen. If it's a phone, lay it flat screen up, point the bottom edge toward the speakers, and remove its case. Keep the room quiet.</p>
  <div id="position-prompt" class="note-box hidden">
    <p style="margin:0; font-weight:600">Move to position <span id="position-current">2</span> of <span id="position-total">__DEFAULT_ROOM_POSITION_COUNT__</span>.</p>
    <p class="hint" style="margin-top:0.3em">Move about 30 cm from the previous position, keep the microphone at ear height, then continue.</p>
  </div>
</section>

<section id="local-certificate-warning" data-envelope-section="local-certificate-warning" class="info-card hidden" role="note">
  Your browser will warn about the speaker's local certificate — continue past it.
</section>

<section id="capture-setup" data-envelope-section="capture-setup" class="mic-panel hidden">
  <h2 style="margin-top:0">Microphone</h2>
  <div class="mic-grid">
    <div id="local-input-row" class="mic-row local-capture-only">
      <label for="input-device-select">Input device
        <select id="input-device-select">
          <option value="" disabled selected>Detecting microphones…</option>
        </select>
      </label>
      <button id="refresh-inputs" type="button" class="btn btn--ghost">Refresh microphones</button>
    </div>
    <p id="local-input-hint" class="hint local-capture-only" style="margin:0">Your USB measurement mic should appear automatically. Tap <strong>Refresh microphones</strong> if it doesn’t, then select it before <strong>Allow microphone</strong>.</p>

    <label for="mic-model-select">Calibration
      <select id="mic-model-select">
        <option value="">None / built-in mic</option>
        __MIC_MODEL_OPTIONS__
        <option value="other">Other calibrated mic</option>
      </select>
    </label>

    <p id="household-mic-banner" class="mic-status hidden" role="status">
      <span id="household-mic-banner-text"></span>
      <button id="household-mic-change" type="button" class="btn btn--ghost">Change</button>
    </p>

    <div id="serial-row" class="mic-row hidden">
      <label for="mic-serial">Serial number
        <input id="mic-serial" type="text" inputmode="text" autocomplete="off"
               placeholder="e.g. 700-1234">
      </label>
      <button id="fetch-calibration" type="button" class="btn btn--ghost">Fetch calibration</button>
    </div>

    <div id="upload-row" class="mic-row hidden">
      <label for="calibration-file">Calibration file
        <input id="calibration-file" type="file" accept=".txt,.cal,.frd,.csv,.omm,text/plain">
      </label>
      <label for="mic-orientation">Orientation
        <select id="mic-orientation">
          <option value="0deg">0° / pointed at speaker</option>
          <option value="90deg">90° / upright</option>
          <option value="unknown">Unknown</option>
        </select>
      </label>
      <label for="calibration-sign">File values are
        <select id="calibration-sign">
          <option value="response" selected>The microphone’s response (usual)</option>
          <option value="correction">A correction to add (rare)</option>
        </select>
      </label>
      <button id="upload-calibration" type="button" class="btn btn--ghost">Upload calibration</button>
      <p class="hint" style="margin:0">Calibration files from miniDSP, Dayton Audio, Cross-Spectrum and the rest of the REW ecosystem describe what the microphone <em>hears</em>, and JTS inverts them. Choose “a correction to add” only if the file’s own documentation says its numbers are already the correction.</p>
    </div>
    <p id="calibration-status" class="mic-status">No calibration loaded. This is okay for a quick check; use a calibrated microphone before relying on the final result.</p>
    <p id="calibration-preview" class="cal-preview hidden"></p>
  </div>

<div id="constraints" class="hidden" aria-live="polite">
  <h2>Capture settings</h2>
  <p class="hint">JTS checks that this browser can record a clean measurement. Continue when every row reads <span class="ok">✓ ok</span>.</p>
  <table class="constraint-table">
    <thead><tr><th>Setting</th><th>Requested</th><th>Actual</th><th>Status</th></tr></thead>
    <tbody id="constraint-rows"></tbody>
  </table>
  <div id="err-banner" class="err-banner hidden"></div>
  <div id="browser-audio-report" class="browser-audio-card hidden"></div>

  <h2>Live mic level</h2>
  <p class="hint">Speak near the microphone. The meter should move with your voice.</p>
  <div class="level-bar-track" aria-label="microphone level">
    <div id="level-bar-fill" class="level-bar-fill"></div>
  </div>
</div>
</section>

<section id="run-defaults" data-envelope-section="run-defaults" class="info-card hidden">
  <div class="run-defaults-line">
    <p id="run-defaults-summary">__RUN_DEFAULTS_SUMMARY__</p>
    <span aria-hidden="true">—</span>
    <button id="change-run-defaults" type="button" class="btn btn--ghost" aria-controls="measurement-options" aria-expanded="false">Change</button>
  </div>
  <p id="repeat-main-position-disclosure" class="hint">__REPEAT_MAIN_POSITION_DISCLOSURE__</p>
  <div id="measurement-options" class="hidden">
    <label for="positions-select">Positions to measure</label>
    <select id="positions-select" form="dummy">
      __ROOM_POSITION_OPTIONS__
    </select>
    <p class="hint" style="margin-top:0.3em">More positions describe more of the listening area. We'll guide you through each one.</p>

    <label for="target-select" style="margin-top:0.6em">Target curve</label>
    <select id="target-select" form="dummy">
      __TARGET_PROFILE_OPTIONS__
    </select>

    <label for="strategy-select" style="margin-top:0.6em">Correction strategy</label>
    <select id="strategy-select" form="dummy">
      __CORRECTION_STRATEGY_OPTIONS__
    </select>
    <p class="hint" style="margin-top:0.3em">Balanced is the recommended household setting. Safe makes fewer, gentler adjustments.</p>
  </div>
</section>

<section id="level-check" data-envelope-section="level-check" class="info-card hidden">
  <h2 class="section__title">Check measurement level</h2>
  <p style="display:flex; gap:0.6em; flex-wrap:wrap">
    <button id="autolevel-lock" type="button" class="btn btn--primary hidden">Lock now</button>
    <button id="autolevel-cancel" type="button" class="btn btn--danger hidden">Cancel</button>
  </p>
  <p id="autolevel-hint" class="hint" style="margin-top:0.4em">The speaker slowly raises a short test tone until the microphone hears a clear measurement level, then stops automatically. If it sounds comfortably loud first, choose <strong>Lock now</strong>. This takes only a few seconds.</p>
  <p class="hint" style="margin-top:0.4em">JTS temporarily pauses your current sound settings so it can measure the room clearly. They return unless you apply the new correction.</p>
  <div id="autolevel-status" class="note-box hidden">
    <p style="margin:0; font-weight:600" id="autolevel-line">Auto-leveling…</p>
    <p class="hint" style="margin-top:0.3em" id="autolevel-detail"></p>
  </div>
</section>

<section id="position-capture" data-envelope-section="position-capture" class="info-card hidden">
  <h2 class="section__title">Measure this position</h2>
  <p>Music pauses automatically while the speaker plays the test sweep.</p>
  <div id="quality-banner" class="quality-banner hidden"></div>
</section>

<section id="measurement-review" data-envelope-section="measurement-review" class="hidden">
  <div id="result-section" class="hidden">
    <h3>Frequency response</h3>
    <div class="chart-controls">
      <label><input id="chart-show-filter" type="checkbox" checked> filter effect</label>
    </div>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <p class="hint">
      <span style="color:#d44">red</span> = measured (averaged across positions),
      <span style="color:#888">gray dashed</span> = target,
      <span style="color:#1db954">green</span> = predicted post-correction.
      <span style="color:#2b7bb9">blue dashed</span> = filter effect.
      After Verify: <span style="color:#a050d0">purple dashed</span> = post-correction measurement,
      with the measured before→after gap shaded
      <span style="color:#1db954">green</span> where it moved toward target and
      <span style="color:#d68200">amber</span> where it moved away.
    </p>
    <button id="reset-correction" type="button" class="btn btn--danger hidden">Reset correction</button>
  </div>
</section>

<section id="apply-status" data-envelope-section="apply-status" class="info-card hidden">
  <p>Room correction is applied. The next measurement checks whether it helped.</p>
</section>

<section id="verification" data-envelope-section="verification" class="info-card hidden">
  <p>Return the microphone to the main seat for a fresh comparison.</p>
</section>

<section id="result-proof" data-envelope-section="result-proof" class="hidden"></section>

<section id="reports" data-envelope-section="reports" class="report-panel hidden">
  <h2>Measurement reports</h2>
  <p class="hint">Read-only evidence from previous sessions. Raw measurement recordings are private and stay on the speaker unless you delete the bundle.</p>
  <button id="load-sessions" type="button" class="btn btn--ghost">Load recent reports</button>
  <div id="session-history" class="session-list"></div>
  <div id="session-report" class="session-report hidden"></div>
</section>
</div>
</main>
__HOUSEHOLD_MIC_ISLAND__
<script type="module" src="/assets/correction/js/main.js"></script>
"""


def render_follower_page(
    hostname: str,
    csrf_token: str = "",
    *,
    leader_url: str | None,
) -> bytes:
    leader_link = (
        '<a class="btn btn--primary" href="'
        + html.escape(leader_url)
        + '">Open leader correction</a>'
        if leader_url
        else ""
    )
    header = canonical_header(
        "Room correction",
        back_href="http://{host}/".format(host=hostname),
    )
    body = f"""
{header}
<main class="page">
  <section class="info-card info-card--accent" role="note">
    <h2 class="section__title">Room correction is controlled by the pair leader</h2>
    <p class="form-hint">This speaker is an active follower. Room correction,
    balance, and sync measurements are content calibration for the paired
    playback image, so run them from the leader while the pair is active.</p>
    <div class="actions">
      {leader_link}
      <a class="btn" href="/rooms/">Manage pair</a>
    </div>
  </section>
</main>
"""
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
    )


def render_page(
    hostname: str,
    csrf_token: str = "",
    *,
    required_sample_rate: int,
    household_mic_prefill_payload: dict[str, Any] | None,
) -> bytes:
    from jasper.audio_measurement.calibration import (
        SUPPORTED_MODELS,
        model_label_aliases,
    )
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        household_correction_strategy_options,
        target_profile_options,
    )
    from jasper.correction.envelope import room_position_label
    from jasper.correction.session import (
        DEFAULT_ROOM_POSITION_COUNT,
        ROOM_POSITION_COUNT_CHOICES,
    )
    from jasper.audio_measurement.ramp import MeasurementRamp

    # data-aliases carries the registry's label tokens to the wizard so it can
    # infer the model from a device label without a hardcoded client-side map.
    mic_model_options = "\n        ".join(
        '<option value="{key}" data-aliases="{aliases}">{label}</option>'.format(
            key=html.escape(key, quote=True),
            aliases=html.escape(",".join(model_label_aliases(key)), quote=True),
            label=html.escape(spec["label"]),
        )
        for key, spec in SUPPORTED_MODELS.items()
    )
    target_profile_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label}</option>'.format(
            key=html.escape(str(spec["target_id"]), quote=True),
            selected=(
                " selected" if spec["target_id"] == DEFAULT_TARGET_PROFILE_ID else ""
            ),
            label=html.escape(str(spec["label"])),
        )
        for spec in target_profile_options()
    )
    correction_strategy_options_html = "\n      ".join(
        '<option value="{key}"{selected}>{label}</option>'.format(
            key=html.escape(str(spec["strategy_id"]), quote=True),
            selected=(
                " selected"
                if spec["strategy_id"] == DEFAULT_CORRECTION_STRATEGY_ID
                else ""
            ),
            label=html.escape(str(spec["label"])),
        )
        for spec in household_correction_strategy_options()
    )
    room_position_options_html = "\n      ".join(
        (
            '<option value="{count}" data-summary-label="{summary_label}"'
            "{selected}>{label}</option>"
        ).format(
            count=count,
            summary_label=html.escape(room_position_label(count), quote=True),
            selected=(" selected" if count == DEFAULT_ROOM_POSITION_COUNT else ""),
            label=html.escape(
                (
                    "1 position — quick check"
                    if count == 1
                    else f"{count} positions"
                    + (" — recommended" if count == DEFAULT_ROOM_POSITION_COUNT else "")
                )
            ),
        )
        for count in ROOM_POSITION_COUNT_CHOICES
    )
    # The server-owned envelope fills both after the first presentation read.
    run_defaults_summary = ""
    repeat_main_position_disclosure = ""
    household_mic_island = json_island(
        "household-mic-data", household_mic_prefill_payload
    )
    # Absolute http:// back link: /correction/ is HTTPS but the dashboard at /
    # is plain HTTP, so a relative "/" would try HTTPS on the root and fail.
    header = canonical_header(
        "Correction",
        back_href="http://{host}/".format(host=hostname),
    )
    body = (
        _PAGE_BODY.replace("__HEADER__", header)
        .replace("__TABS__", section_tabs("room"))
        .replace("__REQUIRED_SR__", str(required_sample_rate))
        .replace(
            "__DEFAULT_ROOM_POSITION_COUNT__",
            str(DEFAULT_ROOM_POSITION_COUNT),
        )
        .replace("__RUN_DEFAULTS_SUMMARY__", run_defaults_summary)
        .replace("__ROOM_POSITION_OPTIONS__", room_position_options_html)
        .replace(
            "__REPEAT_MAIN_POSITION_DISCLOSURE__",
            repeat_main_position_disclosure,
        )
        .replace(
            "__LEVEL_TRUST_MARGIN_DB__",
            format(MeasurementRamp.from_env().trust_margin_db, ".6g"),
        )
        .replace("__MIC_MODEL_OPTIONS__", mic_model_options)
        .replace("__TARGET_PROFILE_OPTIONS__", target_profile_options_html)
        .replace("__CORRECTION_STRATEGY_OPTIONS__", correction_strategy_options_html)
        .replace("__HOUSEHOLD_MIC_ISLAND__", household_mic_island)
    )
    return canonical_page(
        "Room correction — JTS speaker",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/correction/correction.css",
    )
