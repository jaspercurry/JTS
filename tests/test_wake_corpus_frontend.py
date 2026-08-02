# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


"""Wake-corpus page behavior-module and stylesheet contract tests."""

from __future__ import annotations

import json
import subprocess

import pytest

from jasper.web import wake_corpus_setup

from tests.wake_corpus_setup_fixtures import (
    _NODE,
    _controls_js,
    _controls_js_path,
    _module_js,
    _page_css,
)

# ---------------------------------------------------------------------------
# HTML — new UI affordances for ambient + mic-level + trash icon
# ---------------------------------------------------------------------------


def test_html_has_ambient_radio_button() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'value="ambient"' in html_text


def test_html_renders_ambient_in_counts_matrix() -> None:
    """The per-cell counts table includes an ambient column so the
    operator sees their progress in the third condition. The counts
    matrix is built by the behaviour module's renderCounts(), so the
    column label + per-row key live there."""
    js = _module_js()
    # The renderCounts JS literal — header row + per-row keys
    assert '">ambient<' in js
    assert '`${d}-ambient`' in js


def test_html_has_mic_level_bar_elements() -> None:
    """The Record-a-clip card includes a visible mic-level meter
    so the operator knows the mic is alive before they speak."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="mic-level"' in html_text
    assert 'id="mic-level-fill"' in html_text
    assert 'id="mic-level-readout"' in html_text


def test_html_subscribes_to_level_sse() -> None:
    """The behaviour module opens an EventSource to the level endpoint on load."""
    assert "EventSource('api/recording/level')" in _module_js()


def test_html_count_guidance_matches_two_session_protocol() -> None:
    """The UI copy should match Phase 0b's Session A/B targets."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert "Session A: ~7-9 per cell" in html_text
    assert "Session B: ~2-3 per cell" in html_text
    assert "~13-14 utterances per cell" not in html_text


def test_html_delete_button_uses_trash_icon() -> None:
    """Delete button is small + uses a trash icon (was previously
    wide text 'delete', which overlapped the audio player). The clip rows
    are rendered by the behaviour module, so assert against it."""
    js = _module_js()
    # The icon character + the icon class
    assert "🗑" in js
    assert '"danger icon"' in js


def test_clip_row_audio_cell_does_not_block_trash_button() -> None:
    """Regression: with a naked `audio` element in a fixed-pixel
    grid column, the audio's intrinsic min-content (browser-default
    300px+) blows past the column width and pushes the trash button
    off the right edge of the card. Fix is twofold and both legs
    must remain in the CSS:

      1. `minmax(0, …)` on the audio column overrides grid's default
         min-width:auto so the column can shrink below content min-content.
      2. Explicit `width: 100%; min-width: 0` on .clip audio so the
         element itself shrinks to fit instead of forcing the cell
         to grow.

    A future CSS edit dropping either leg would silently re-introduce
    the "I see the audio but can't find the delete button" bug.
    """
    css = _page_css()
    # Leg 1: minmax(0, …) in the .clip grid template
    assert "minmax(0," in css, (
        "the .clip row's audio column needs minmax(0, …) so the "
        "audio's intrinsic min-content doesn't force the grid to grow"
    )
    # Leg 2: explicit width constraints on the audio element
    assert ".clip audio" in css
    assert "min-width: 0" in css


# ---------------------------------------------------------------------------
# /api/recording/level SSE endpoint — read-only, no CSRF, streams
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# HTML — Sessions card + raw-mic-0 toggle wiring
# ---------------------------------------------------------------------------


def test_html_has_sessions_card() -> None:
    """The collapsible Sessions card sits below the new-session form."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert html_text.index('id="session-card"') < html_text.index(
        'id="sessions-card"',
    )
    assert "<details" in html_text
    assert 'id="sessions-card"' in html_text
    assert 'id="sessions-list"' in html_text


def test_html_labels_speaker_as_name_not_member() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    assert '<label for="member">Name:</label>' in html_text
    assert '<label for="member">Member:</label>' not in html_text


def test_html_has_include_raw_mic_0_toggle() -> None:
    """Begin-a-new-session form has the raw-mic-0 toggle."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-raw-mic-0"' in html_text
    assert 'class="toggle"' in html_text
    assert 'Raw mic 0' in html_text


def test_html_has_include_usb_mic_toggle() -> None:
    """Begin-a-new-session form has the corpus USB/ref toggle. The switch
    + label live in the page body; the include_usb_mic payload key lives in
    the capture option module."""
    html_text = wake_corpus_setup._render_index_html("t")
    controls_js = _controls_js()
    assert 'id="include-usb-mic"' in html_text
    assert 'USB mic + reference' in html_text
    assert 'Raw USB, AEC3, reference.' in html_text
    assert 'id="usb-mic-note"' not in html_text
    assert 'include_usb_mic' in controls_js
    assert "elements.usbMic.disabled = sessionLoaded;" in controls_js
    assert "|| corpusProfile === 'chip_aec_comparison_v1'" not in controls_js


def test_html_has_dtln_session_toggles() -> None:
    """Begin-a-new-session form exposes XVF and USB DTLN toggles. Switches
    in the body, payload keys in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-dtln"' in html_text
    assert 'id="include-usb-dtln"' in html_text
    assert 'USB DTLN' in html_text
    js = _controls_js()
    assert 'include_dtln' in js
    assert 'include_usb_dtln' in js


def test_html_has_aec3_sweep_toggle() -> None:
    """Begin-a-new-session form exposes the bounded AEC3 sweep mode. The
    toggle is in the body; the sweep variant legs + labels ride in the JSON
    config island (so they're in the rendered page) and the
    include_aec3_sweep payload key is in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'id="include-aec3-sweep"' in html_text
    assert "AEC3 sweep" in html_text
    assert "include_aec3_sweep" in _controls_js()
    for variant in wake_corpus_setup.AEC3_SWEEP_VARIANTS:
        # Both leg + label are serialized into the wake-corpus-config island.
        assert variant.leg in html_text
        assert variant.label in html_text


def test_html_has_capture_plan_preview() -> None:
    html_text = wake_corpus_setup._render_index_html("t")
    js = _module_js()
    css = _page_css()

    assert 'id="capture-plan-preview"' in html_text
    assert "api/capture-plan" in js
    assert "renderCapturePlan" in js
    assert ".capture-plan-preview" in css


def test_html_test_mode_button_follows_capture_leg_choices() -> None:
    """The operator chooses capture legs before entering test mode. The leg
    toggles + Begin button live in the body; the corpus-test-mode call
    lives in the module."""
    html_text = wake_corpus_setup._render_index_html("t")
    button_idx = html_text.index('id="session-begin"')
    assert html_text.index('id="include-raw-mic-0"') < button_idx
    assert html_text.index('id="include-dtln"') < button_idx
    assert html_text.index('id="include-aec3-sweep"') < button_idx
    assert html_text.index('id="include-usb-mic"') < button_idx
    assert html_text.index('id="include-usb-dtln"') < button_idx
    assert "voice-toggle" not in html_text
    assert "bridge-output-disable" not in html_text
    assert "api/corpus-test-mode" in _module_js()


def test_capture_option_controls_enforce_chip_profile_rules() -> None:
    if _NODE is None:
        pytest.skip("node is required for the wake-corpus controls harness")

    harness = f"""
        import {{
          currentSessionPayload,
          syncCorpusProfileControls,
        }} from {json.dumps(_controls_js_path().as_uri())};

        function input({{ checked = false, value = '', row = null }} = {{}}) {{
          return {{
            checked,
            value,
            disabled: false,
            closest(selector) {{
              if (selector !== '.capture-option') throw new Error(selector);
              return row;
            }},
          }};
        }}
        function row() {{
          return {{ hidden: false }};
        }}

        const chipRows = {{
          raw: row(),
          dtln: row(),
          sweep: row(),
        }};
        const chip = {{
          member: input({{ value: ' jasper ' }}),
          chipProfile: input({{ checked: true }}),
          rawMic0: input({{ checked: false, row: chipRows.raw }}),
          dtln: input({{ checked: true, row: chipRows.dtln }}),
          xvfRaw0Dtln: input({{ checked: true }}),
          aec3Sweep: input({{ checked: true, row: chipRows.sweep }}),
          usbMic: input({{ checked: false }}),
          usbDtln: input({{ checked: false }}),
        }};
        syncCorpusProfileControls(chip, false);
        const chipPayload = currentSessionPayload(chip);

        const standardRows = {{
          raw: row(),
          dtln: row(),
          sweep: row(),
        }};
        const standard = {{
          member: input({{ value: ' test ' }}),
          chipProfile: input({{ checked: false }}),
          rawMic0: input({{ checked: false, row: standardRows.raw }}),
          dtln: input({{ checked: true, row: standardRows.dtln }}),
          xvfRaw0Dtln: input({{ checked: false }}),
          aec3Sweep: input({{ checked: true, row: standardRows.sweep }}),
          usbMic: input({{ checked: false }}),
          usbDtln: input({{ checked: true }}),
        }};
        syncCorpusProfileControls(standard, false);
        const standardPayload = currentSessionPayload(standard);

        console.log(JSON.stringify({{
          chip: {{
            rawChecked: chip.rawMic0.checked,
            dtlnChecked: chip.dtln.checked,
            sweepChecked: chip.aec3Sweep.checked,
            rawHidden: chipRows.raw.hidden,
            dtlnHidden: chipRows.dtln.hidden,
            sweepHidden: chipRows.sweep.hidden,
            rawDisabled: chip.rawMic0.disabled,
            dtlnDisabled: chip.dtln.disabled,
            sweepDisabled: chip.aec3Sweep.disabled,
            usbDisabled: chip.usbMic.disabled,
            payload: chipPayload,
          }},
          standard: {{
            rawHidden: standardRows.raw.hidden,
            dtlnHidden: standardRows.dtln.hidden,
            sweepHidden: standardRows.sweep.hidden,
            rawDisabled: standard.rawMic0.disabled,
            dtlnDisabled: standard.dtln.disabled,
            sweepDisabled: standard.aec3Sweep.disabled,
            payload: standardPayload,
          }},
        }}));
    """
    proc = subprocess.run(
        [_NODE, "--input-type=module"],
        input=harness,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["chip"]["rawChecked"] is True
    assert out["chip"]["dtlnChecked"] is False
    assert out["chip"]["sweepChecked"] is False
    assert out["chip"]["rawHidden"] is True
    assert out["chip"]["dtlnHidden"] is True
    assert out["chip"]["sweepHidden"] is True
    assert out["chip"]["rawDisabled"] is True
    assert out["chip"]["dtlnDisabled"] is True
    assert out["chip"]["sweepDisabled"] is True
    assert out["chip"]["usbDisabled"] is False
    assert out["chip"]["payload"] == {
        "member": "jasper",
        "corpus_profile": "chip_aec_comparison_v1",
        "include_raw_mic_0": True,
        "include_dtln": False,
        "include_usb_mic": False,
        "include_usb_dtln": False,
        "include_xvf_raw0_dtln": True,
        "include_aec3_sweep": False,
        "aec3_sweep_source": "xvf",
    }

    assert out["standard"]["rawHidden"] is False
    assert out["standard"]["dtlnHidden"] is False
    assert out["standard"]["sweepHidden"] is False
    assert out["standard"]["rawDisabled"] is False
    assert out["standard"]["dtlnDisabled"] is False
    assert out["standard"]["sweepDisabled"] is False
    assert out["standard"]["payload"] == {
        "member": "test",
        "corpus_profile": "standard",
        "include_raw_mic_0": False,
        "include_dtln": True,
        "include_usb_mic": True,
        "include_usb_dtln": True,
        "include_xvf_raw0_dtln": False,
        "include_aec3_sweep": True,
        "aec3_sweep_source": "usb",
    }


def test_html_loaded_session_enters_test_mode_without_new_session() -> None:
    """Loaded sessions should not be labeled as newly-started sessions.

    The button enters corpus test mode using the loaded session's saved
    legs instead of beginning a second session. This dynamic button-text
    + flow logic lives in the behaviour module; the unload control + its
    'Loaded session' empty state are guarded there too.
    """
    js = _module_js()
    assert "Enter corpus test mode" in js
    assert "Stop voice & resume recording" in js
    assert "Stop voice & apply outputs" in js
    assert "Apply bridge outputs" in js
    assert "Ready to record" in js
    assert "Enter corpus test mode for loaded session" not in js
    assert "api/session/unload" in js
    assert "'Loaded session'" in js
    assert "sessionBridgeReady" in js
    assert "latestStatus?.session_id" in js
    assert "latestStatus.include_dtln" in js
    assert "latestStatus.include_aec3_sweep" in js
    assert "Session active" not in js
    # The unload button element itself is server-rendered in the body.
    assert 'id="session-unload"' in wake_corpus_setup._render_index_html("t")


def test_html_confirm_enables_missing_bridge_outputs() -> None:
    """The Begin flow offers a deliberate bridge enable/restart retry
    instead of silently starting a session with missing WAV legs. This
    retry flow lives in the behaviour module."""
    js = _module_js()
    assert "can_enable_bridge_outputs" in js
    assert "enable_bridge_outputs: true" in js
    assert "restart the affected audio daemons" in js


def test_html_playback_uses_leg_selector() -> None:
    """Clip rows let the operator choose any recorded leg for playback. The
    clip rendering + leg-ordering live in the behaviour module; the
    AEC3-sweep + USB labels ride in the JSON config island."""
    js = _module_js()
    html_text = wake_corpus_setup._render_index_html("t")
    assert 'data-audio-leg' in js
    assert 'legLabel(leg)' in js
    assert 'orderedLegs(c.files || {})' in js
    assert "'usb_dtln', 'ref'" in js
    assert 'encodeURIComponent(ev.target.value)' in js
    assert 'import { createLegLabels } from "./labels.js";' in js
    assert "on: 'XVF WebRTC AEC3'" not in js
    # All base + sweep/legacy leg labels are injected via the config island.
    for label in wake_corpus_setup.LEG_LABELS.values():
        assert label in html_text
    assert "aec3_variant_1" in html_text
    assert "aec3_variant_2" in html_text
    assert "aec3_variant_3" in html_text
    assert "aec3_hf_slow_only" in html_text
    assert "aec3_hf_relaxed" in html_text
    assert "aec3_edge_combo" in html_text
    assert "aec3_gentle_dnd" in html_text
    assert "aec3_nearend_fast" in html_text
    assert "aec3_slow_attack" in html_text
    assert "USB AEC3 edge combo 80 ms" in html_text  # usb_webrtc corpus label
    assert "createLegLabels(_config)" in js
    assert "usb_dtln: 'USB DTLN'" not in js


def test_html_js_calls_sessions_endpoints() -> None:
    """JS must call the right relative API paths (not absolute —
    nginx prefix-strip would 502 those). The calls live in the module."""
    js = _module_js()
    assert "'api/sessions'" in js or '"api/sessions"' in js
    assert "'api/session/load'" in js or '"api/session/load"' in js
    assert "'api/session/unload'" in js or '"api/session/unload"' in js
    assert "api/session/${" in js  # DELETE template literal
