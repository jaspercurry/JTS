# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the kind-agnostic capture-spec contract (phone-mic relay step 1).

Two boundaries are the point of this suite and map to plan §15 acceptance
criteria:

  - **Kind-agnostic schema** — a brand-new ``kind`` that fills the same fields
    validates with zero schema changes (proves the relay never needs to learn a
    kind).
  - **UI is an allowlisted token vocabulary, not markup** — themes are tokens,
    component types are enumerated, and the validator refuses anything outside
    the vocabulary so the Pi can never *emit* an executable-looking payload.
"""
from __future__ import annotations

import pytest

from jasper.capture_relay import spec as spec_mod
from jasper.capture_relay.spec import (
    CaptureConstraints,
    CaptureSpec,
    CaptureSpecError,
    CaptureStimulus,
    CaptureValidity,
    build_room_sweep_spec,
    ui_button,
    ui_heading,
    ui_level_meter,
    ui_steps,
)


# --- room_sweep builder -------------------------------------------------------


def test_room_sweep_builder_is_measurement_clean_48k_mono():
    s = build_room_sweep_spec()
    assert s.kind == "room_sweep"
    assert s.sample_rate_hz == 48000
    assert s.channels == 1
    # EC/AGC/NS/voice-isolation all off — anything on flattens the response.
    assert s.constraints == CaptureConstraints(False, False, False, False)
    d = s.to_dict()
    assert d["constraints"] == {
        "echoCancellation": False,
        "autoGainControl": False,
        "noiseSuppression": False,
        "voiceIsolation": False,
    }


def test_room_sweep_window_contains_stimulus():
    s = build_room_sweep_spec(
        stimulus_duration_ms=10000, pre_roll_ms=800, post_roll_ms=700
    )
    # The phone normally stops from Pi-reported sweep_complete; duration_ms is a
    # hard timeout that still fully contains the documented pre/sweep/post span.
    assert s.duration_ms == 30000
    assert s.duration_ms >= 800 + 10000 + 700


def test_room_sweep_stimulus_played_by_pi():
    s = build_room_sweep_spec()
    assert s.stimulus is not None
    assert s.stimulus.played_by == "pi"
    assert s.to_dict()["stimulus"]["played_by"] == "pi"


def test_room_sweep_validity_refuses_unclean_with_fallback():
    s = build_room_sweep_spec()
    assert s.validity.clean_capture == "refuse"
    # …but never dead-end an iPhone that cannot do a clean capture.
    assert s.validity.allow_capability_fallback is True
    assert s.validity.require_alignment is True
    # Magnitude FR is drift-insensitive.
    assert s.validity.clock_drift == "ignore"


def test_room_sweep_ui_is_server_driven_copy():
    # Copy/choreography ship from the Pi (no web deploy): position tailoring is
    # reflected in the heading text the page will render.
    s = build_room_sweep_spec(position=2, total_positions=5)
    headings = [c for c in s.screen if c["type"] == "heading"]
    assert headings and "position 2 of 5" in headings[0]["text"]
    buttons = [c for c in s.screen if c["type"] == "button"]
    assert buttons and buttons[0]["action"] == "begin_capture"


def test_room_sweep_default_upload_cap_matches_backend():
    s = build_room_sweep_spec()
    assert s.max_upload_bytes == 32 * 1024 * 1024


# --- round-trip ---------------------------------------------------------------


def test_to_dict_from_dict_round_trip_is_stable():
    s = build_room_sweep_spec(position=3, total_positions=5)
    again = CaptureSpec.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()


def test_from_dict_validates_and_reconstructs_sub_records():
    payload = build_room_sweep_spec().to_dict()
    s = CaptureSpec.from_dict(payload)
    assert isinstance(s.constraints, CaptureConstraints)
    assert isinstance(s.stimulus, CaptureStimulus)
    assert isinstance(s.validity, CaptureValidity)


def test_passive_capture_has_null_stimulus():
    s = CaptureSpec(
        kind="noise_floor",
        duration_ms=3000,
        pre_roll_ms=0,
        post_roll_ms=0,
        stimulus=None,
        screen=(ui_heading("Stay quiet"),),
    ).validate()
    assert s.to_dict()["stimulus"] is None
    assert CaptureSpec.from_dict(s.to_dict()).stimulus is None


# --- kind-agnostic boundary (plan §15) ----------------------------------------


def test_brand_new_kind_validates_with_no_schema_change():
    # A future kind the schema has never heard of must validate purely on the
    # shape of its fields — this is what lets the relay stay kind-blind.
    payload = build_room_sweep_spec().to_dict()
    payload["kind"] = "totally_new_kind_42"
    s = CaptureSpec.from_dict(payload)
    assert s.kind == "totally_new_kind_42"


def test_schema_never_enumerates_kinds():
    # Defensive: the validator source must not branch on specific kind values,
    # or "zero relay/schema change for a new kind" would silently erode.
    import inspect

    source = inspect.getsource(CaptureSpec.validate)
    for forbidden in ("room_sweep", "balance_burst", "sync_marker", "crossover"):
        assert forbidden not in source


# --- strict, loud validation --------------------------------------------------


def test_rejects_wrong_sample_rate():
    with pytest.raises(CaptureSpecError, match="sample_rate_hz"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            sample_rate_hz=44100,
        ).validate()


def test_rejects_stereo():
    with pytest.raises(CaptureSpecError, match="channels"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            channels=2,
        ).validate()


def test_rejects_empty_kind():
    with pytest.raises(CaptureSpecError, match="kind"):
        CaptureSpec(
            kind="", duration_ms=1000, pre_roll_ms=0, post_roll_ms=0
        ).validate()


def test_rejects_non_wav_output():
    with pytest.raises(CaptureSpecError, match="format"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            output_format="opus",
        ).validate()


def test_rejects_window_smaller_than_rolls():
    with pytest.raises(CaptureSpecError, match="duration_ms must be"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=100,
            pre_roll_ms=800,
            post_roll_ms=700,
        ).validate()


def test_rejects_oversize_upload_cap():
    with pytest.raises(CaptureSpecError, match="max_upload_bytes"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            max_upload_bytes=1024 * 1024 * 1024,
        ).validate()


def test_rejects_unknown_stimulus_player():
    with pytest.raises(CaptureSpecError, match="played_by"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            stimulus=CaptureStimulus(played_by="phone"),
        ).validate()


# --- UI-is-data boundary ------------------------------------------------------


def test_rejects_non_allowlisted_theme_accent():
    with pytest.raises(CaptureSpecError, match="theme.accent"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            theme={"accent": "red; } body{}", "font": "figtree"},
        ).validate()


def test_rejects_unknown_theme_key():
    with pytest.raises(CaptureSpecError, match="unknown keys"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            theme={"accent": "sage", "font": "figtree", "style": "x"},
        ).validate()


def test_rejects_unknown_ui_component_type():
    with pytest.raises(CaptureSpecError, match="type must be one of"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            screen=({"type": "iframe", "src": "javascript:alert(1)"},),
        ).validate()


def test_rejects_unknown_button_action():
    with pytest.raises(CaptureSpecError, match="action must be one of"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            screen=(ui_button("Go", action="exfiltrate"),),
        ).validate()


def test_rejects_steps_with_non_string_items():
    with pytest.raises(CaptureSpecError, match="items must be a list of strings"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            screen=({"type": "steps", "items": ["ok", {"x": 1}]},),
        ).validate()


def test_html_like_text_is_allowed_but_carried_as_data():
    # The renderer escapes; the Pi may legitimately include punctuation. The
    # point is the *type* vocabulary is closed, not that text is censored.
    s = CaptureSpec(
        kind="room_sweep",
        duration_ms=1000,
        pre_roll_ms=0,
        post_roll_ms=0,
        screen=(ui_heading("<script>alert(1)</script>"),),
    ).validate()
    # Carried verbatim as DATA — the page renderer is responsible for escaping
    # it into inert text (asserted in the page renderer harness, step 3).
    assert s.to_dict()["ui"]["screen"][0]["text"] == "<script>alert(1)</script>"


def test_validity_vocabulary_is_enforced():
    with pytest.raises(CaptureSpecError, match="clean_capture"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            validity=CaptureValidity(clean_capture="maybe"),
        ).validate()
    with pytest.raises(CaptureSpecError, match="clock_drift"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            validity=CaptureValidity(clock_drift="whenever"),
        ).validate()


# --- UI builder shapes --------------------------------------------------------


def test_ui_builders_emit_expected_shapes():
    assert ui_heading("Hi") == {"type": "heading", "text": "Hi"}
    assert ui_steps(["a", "b"]) == {"type": "steps", "items": ["a", "b"]}
    assert ui_level_meter() == {"type": "level_meter", "source": "mic"}
    assert ui_button("Go") == {"type": "button", "label": "Go", "action": "begin_capture"}


def test_contract_constants_are_self_consistent():
    # The default theme must itself satisfy the allowlist.
    assert spec_mod.DEFAULT_THEME["accent"] in spec_mod.THEME_ACCENTS
    assert spec_mod.DEFAULT_THEME["font"] in spec_mod.THEME_FONTS
    assert spec_mod.REQUIRED_SAMPLE_RATE_HZ == 48000
    assert spec_mod.DEFAULT_MAX_UPLOAD_BYTES <= spec_mod.HARD_MAX_UPLOAD_BYTES
