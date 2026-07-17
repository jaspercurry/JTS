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
    CAPTURE_PROTOCOL_VERSION,
    CaptureConstraints,
    CaptureSpec,
    CaptureSpecError,
    CaptureStimulus,
    CaptureValidity,
    DefaultSetupCalibration,
    build_crossover_sweep_spec,
    build_level_ramp_spec,
    build_room_sweep_spec,
    ui_button,
    ui_heading,
    ui_level_meter,
    ui_steps,
)
from jasper.audio_measurement.calibration import SUPPORTED_MODELS, supported_model_options


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


def test_position_progress_is_omitted_when_the_spec_has_no_room_position():
    payload = build_room_sweep_spec().to_dict()
    assert "position" not in payload
    assert "total_positions" not in payload
    assert "presentation_variant" not in payload


def test_signed_room_repeat_role_round_trips_without_owning_state():
    spec = build_room_sweep_spec(
        position=1,
        total_positions=6,
        presentation_variant="trust_repeat",
    )

    assert spec.to_dict()["presentation_variant"] == "trust_repeat"
    assert CaptureSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()


@pytest.mark.parametrize("variant", ["repeat", "verification", 1])
def test_room_sweep_builder_owns_its_closed_presentation_variants(variant):
    with pytest.raises(CaptureSpecError, match="presentation_variant"):
        build_room_sweep_spec(
            position=1,
            total_positions=6,
            presentation_variant=variant,
        )


def test_non_room_specs_omit_room_placement_and_role_fields():
    for spec in (build_crossover_sweep_spec(), build_level_ramp_spec()):
        payload = spec.to_dict()
        assert "position" not in payload
        assert "total_positions" not in payload
        assert "presentation_variant" not in payload


def test_shared_schema_accepts_a_new_kinds_well_formed_presentation_variant():
    spec = CaptureSpec(
        kind="future_capture_kind",
        duration_ms=1000,
        pre_roll_ms=0,
        post_roll_ms=0,
        presentation_variant="future_variant",
    ).validate()

    assert spec.to_dict()["presentation_variant"] == "future_variant"


@pytest.mark.parametrize("variant", [None, False, 0, [], {}])
def test_shared_schema_rejects_malformed_falsy_presentation_variants(variant):
    payload = build_room_sweep_spec().to_dict()
    payload["presentation_variant"] = variant

    with pytest.raises(CaptureSpecError, match="presentation_variant"):
        CaptureSpec.from_dict(payload)


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
    assert s.to_dict()["position"] == 2
    assert s.to_dict()["total_positions"] == 5


def test_room_sweep_calibration_models_are_registry_driven():
    s = build_room_sweep_spec()

    assert {m["key"] for m in s.calibration_models} == set(SUPPORTED_MODELS)
    assert s.to_dict()["calibration_models"] == list(supported_model_options())


def test_room_sweep_requests_guided_setup_validation():
    s = build_room_sweep_spec()

    assert s.setup_validation is True
    assert s.to_dict()["setup_validation"] is True


def test_room_sweep_default_upload_cap_matches_backend():
    s = build_room_sweep_spec()
    assert s.max_upload_bytes == 32 * 1024 * 1024


# --- round-trip ---------------------------------------------------------------


def test_to_dict_from_dict_round_trip_is_stable():
    s = build_room_sweep_spec(position=3, total_positions=5)
    again = CaptureSpec.from_dict(s.to_dict())
    assert again.to_dict() == s.to_dict()


@pytest.mark.parametrize(
    ("position", "total_positions"),
    [(None, 6), (0, 6), (7, 6), (1, 0), (True, 6)],
)
def test_room_position_progress_is_an_exact_positive_pair(
    position,
    total_positions,
):
    with pytest.raises(CaptureSpecError, match="position"):
        build_room_sweep_spec(
            position=position,
            total_positions=total_positions,
        )


def test_capture_protocol_version_is_explicit_and_strict():
    payload = build_room_sweep_spec().to_dict()
    assert payload["capture_protocol_version"] == CAPTURE_PROTOCOL_VERSION
    payload["capture_protocol_version"] = 99
    with pytest.raises(CaptureSpecError, match="capture_protocol_version"):
        CaptureSpec.from_dict(payload)


def test_return_url_round_trips_for_phone_done_cta():
    s = build_room_sweep_spec().with_return_url("http://jts5.local/correction/")
    payload = s.to_dict()

    assert payload["return_url"] == "http://jts5.local/correction/"
    assert (
        CaptureSpec.from_dict(payload).return_url
        == "http://jts5.local/correction/"
    )


def test_from_dict_validates_and_reconstructs_sub_records():
    payload = build_room_sweep_spec().to_dict()
    s = CaptureSpec.from_dict(payload)
    assert isinstance(s.constraints, CaptureConstraints)
    assert isinstance(s.stimulus, CaptureStimulus)
    assert isinstance(s.validity, CaptureValidity)


# --- default_setup.calibration — the optional household-mic prefill hint ------
# (Wave-2 persistence, jasper/correction/household_mic.py. Never binding; the
# current capture page ignores unknown spec fields, so the block is inert
# until the one-tap-confirm follow-up page PR reads it.)


def _household_hint(**overrides) -> DefaultSetupCalibration:
    kwargs = dict(
        mode="serial",
        model="minidsp_umik2",
        serial_display="8494",
        calibration_id="minidsp-minidsp_umik2-abc123456789",
    )
    kwargs.update(overrides)
    return DefaultSetupCalibration(**kwargs)


def test_default_setup_calibration_round_trips_and_is_omitted_when_absent():
    populated = build_level_ramp_spec(default_setup_calibration=_household_hint())
    payload = populated.to_dict()
    assert payload["default_setup"] == {
        "calibration": {
            "mode": "serial",
            "model": "minidsp_umik2",
            "serial_display": "8494",
            "calibration_id": "minidsp-minidsp_umik2-abc123456789",
        }
    }
    again = CaptureSpec.from_dict(payload)
    assert again.default_setup_calibration == populated.default_setup_calibration
    assert again.to_dict() == payload  # stable round-trip

    # Absent by default: existing callers/specs emit no default_setup key at
    # all, so older pages and the relay see byte-identical payload shapes.
    assert "default_setup" not in build_level_ramp_spec().to_dict()
    assert CaptureSpec.from_dict(
        build_level_ramp_spec().to_dict()
    ).default_setup_calibration is None


def test_default_setup_calibration_from_dict_is_strict():
    good = _household_hint().to_dict()
    assert DefaultSetupCalibration.from_dict(good) == _household_hint()
    with pytest.raises(CaptureSpecError, match="unknown keys"):
        DefaultSetupCalibration.from_dict({**good, "serial": "700-1234"})
    with pytest.raises(CaptureSpecError, match="must be an object"):
        DefaultSetupCalibration.from_dict(["not", "a", "mapping"])


def test_default_setup_calibration_vocabulary_is_enforced():
    with pytest.raises(CaptureSpecError, match="default_setup.calibration.mode"):
        build_level_ramp_spec(
            default_setup_calibration=_household_hint(mode="telepathy"),
        )
    # "none" deliberately absent from the vocabulary: a record only exists
    # after a calibration succeeded, so the hint is present-and-actionable
    # or omitted entirely.
    with pytest.raises(CaptureSpecError, match="default_setup.calibration.mode"):
        build_level_ramp_spec(
            default_setup_calibration=_household_hint(mode="none"),
        )
    with pytest.raises(CaptureSpecError, match="calibration_id"):
        build_level_ramp_spec(
            default_setup_calibration=_household_hint(calibration_id=""),
        )


def test_from_dict_rejects_malformed_default_setup_block():
    base = build_level_ramp_spec(
        default_setup_calibration=_household_hint()
    ).to_dict()

    non_mapping = dict(base)
    non_mapping["default_setup"] = "not-an-object"
    with pytest.raises(CaptureSpecError, match="default_setup must be an object"):
        CaptureSpec.from_dict(non_mapping)

    unknown_sub_key = dict(base)
    unknown_sub_key["default_setup"] = {
        "calibration": _household_hint().to_dict(),
        "device": {"label": "smuggled"},
    }
    with pytest.raises(CaptureSpecError, match="default_setup has unknown keys"):
        CaptureSpec.from_dict(unknown_sub_key)

    unknown_calibration_key = dict(base)
    unknown_calibration_key["default_setup"] = {
        "calibration": {**_household_hint().to_dict(), "serial": "700-1234"},
    }
    with pytest.raises(CaptureSpecError, match="unknown keys"):
        CaptureSpec.from_dict(unknown_calibration_key)


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


def test_rejects_unsafe_return_url():
    for url in (
        "javascript:alert(1)",
        "/correction/",
        "http://user:pass@jts.local/correction/",
        "http://jts.local/correction/#frag",
        "http://bad\nhost/correction/",
    ):
        with pytest.raises(CaptureSpecError, match="return_url"):
            build_room_sweep_spec().with_return_url(url)


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


def test_rejects_invalid_calibration_model_shape():
    with pytest.raises(CaptureSpecError, match="calibration_models"):
        CaptureSpec(
            kind="room_sweep",
            duration_ms=1000,
            pre_roll_ms=0,
            post_roll_ms=0,
            calibration_models=({"key": "mic", "label": "Mic", "aliases": "mic"},),
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


# --- capture_plan (session-spanning protocol v3, SPEC W2.3) --------------------


def _plan_spec(**overrides):
    from jasper.capture_relay.spec import CapturePlan

    kwargs = dict(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        stimulus_duration_ms=4000,
        capture_plan=CapturePlan(capture_target=3, max_attempts=4),
    )
    kwargs.update(overrides)
    return build_crossover_sweep_spec(**kwargs)


def test_capture_plan_marker_is_dormant_for_every_shipped_builder():
    # PR-1 dormancy: NO shipped builder emits the v3 marker or a plan. The
    # follow-up capture-page PR flips it on; until then every emitted spec is
    # byte-identical to the pre-plan contract.
    from jasper.capture_relay.spec import BUILDERS

    for kind, builder in BUILDERS.items():
        spec = (
            builder(acknowledgement_binding="placement_abcdefghijklmnopqrstuv")
            if kind == "crossover_sweep"
            else builder()
        )
        assert spec.capture_plan is None, kind
        assert "capture_plan" not in spec.to_dict(), kind
        assert spec.capture_protocol_version < 3, kind


def test_capture_plan_opts_the_crossover_spec_into_protocol_three():
    spec = _plan_spec()
    assert spec.capture_protocol_version == 3
    d = spec.to_dict()
    assert d["capture_protocol_version"] == 3
    assert d["capture_plan"] == {
        "schema_version": 1,
        "capture_target": 3,
        "max_attempts": 4,
    }
    # Round-trips through the inbound validation path.
    rebuilt = CaptureSpec.from_dict(d)
    assert rebuilt.capture_plan == spec.capture_plan
    assert rebuilt.capture_protocol_version == 3


def test_capture_plan_requires_an_acknowledgement_binding():
    with pytest.raises(CaptureSpecError, match="acknowledgement_binding"):
        _plan_spec(acknowledgement_binding="")


def test_capture_plan_requires_protocol_three_and_vice_versa():
    from dataclasses import replace

    from jasper.capture_relay.spec import CapturePlan

    base = _plan_spec()
    with pytest.raises(CaptureSpecError, match="capture protocol 3"):
        replace(base, capture_protocol_version=2).validate()
    with pytest.raises(CaptureSpecError, match="requires a capture_plan"):
        replace(base, capture_plan=None).validate()
    # A plan-free spec at protocol 2 stays valid (the v2 path).
    replace(
        base, capture_plan=None, capture_protocol_version=2
    ).validate()
    assert CapturePlan(capture_target=3, max_attempts=4).schema_version == 1


@pytest.mark.parametrize(
    ("target", "attempts", "match"),
    [
        (0, 4, "1..max_attempts"),
        (5, 4, "1..max_attempts"),
        (3, 9, "<= 8"),
        (True, 4, "integer"),
        (3, None, "integer"),
    ],
    ids=["zero-target", "target-over-budget", "over-ceiling", "bool", "none"],
)
def test_capture_plan_bounds_are_strict(target, attempts, match):
    from dataclasses import replace

    from jasper.capture_relay.spec import CapturePlan

    base = _plan_spec()
    plan = CapturePlan(capture_target=target, max_attempts=attempts)
    with pytest.raises(CaptureSpecError, match=match):
        replace(base, capture_plan=plan).validate()


def test_capture_plan_from_dict_is_strict():
    from jasper.capture_relay.spec import CapturePlan

    with pytest.raises(CaptureSpecError, match="unknown keys"):
        CapturePlan.from_dict(
            {"schema_version": 1, "capture_target": 3, "max_attempts": 4, "x": 1}
        )
    with pytest.raises(CaptureSpecError, match="capture_target"):
        CapturePlan.from_dict({"schema_version": 1, "max_attempts": 4})
    with pytest.raises(CaptureSpecError, match="must be an object"):
        CaptureSpec.from_dict({**_plan_spec().to_dict(), "capture_plan": "3"})
    with pytest.raises(CaptureSpecError, match="schema_version"):
        CaptureSpec.from_dict(
            {
                **_plan_spec().to_dict(),
                "capture_plan": {
                    "schema_version": 2,
                    "capture_target": 3,
                    "max_attempts": 4,
                },
            }
        )


def test_plan_attempt_ceiling_stays_in_lockstep_with_the_worker():
    # Each admitted attempt's blob rides relay capture_index = attempt - 1
    # (attempt in 1..MAX_CAPTURE_PLAN_ATTEMPTS), so the valid blob indexes are
    # EXACTLY 0..MAX_CAPTURE_PLAN_ATTEMPTS-1. The Worker must carry the SAME
    # attempt cap and apply it to indexes with a strict inequality — a bare
    # equal-constant check would happily pin an off-by-one storable-but-never-
    # authorized slot.
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent / "relay" / "src" / "worker.js"
    ).read_text(encoding="utf-8")
    assert (
        f"const MAX_CAPTURE_PLAN_ATTEMPTS = {spec_mod.MAX_CAPTURE_PLAN_ATTEMPTS};"
        in worker_src
    ), "worker attempt cap drifted from the Pi-side plan attempt cap"
    assert "index >= MAX_CAPTURE_PLAN_ATTEMPTS ? null : index" in worker_src, (
        "worker must reject index >= the attempt cap (valid indexes are "
        "exactly 0..cap-1, one per admitted attempt)"
    )
    assert spec_mod.SUPPORTED_CAPTURE_PROTOCOL_VERSIONS == (1, 2, 3)


def test_compat_matrix_v3_spec_refuses_todays_v2_page():
    # v3 Pi session + v2-only page → fail closed BEFORE any tone (the page
    # cannot run the session-spanning choreography it never implemented).
    from jasper.capture_relay.session import (
        CapturePageIncompatible,
        validate_capture_page,
    )

    spec = _plan_spec()
    todays_page = {
        "schema_version": 1,
        "capture_protocol_version": 2,
        "supported_capture_protocol_versions": [1, 2],
        "capture_page_build": "20260716.1",
    }
    with pytest.raises(CapturePageIncompatible, match="expected protocol 3"):
        validate_capture_page(todays_page, spec)


def test_compat_matrix_v3_page_serves_v2_spec():
    # A future page that ALSO supports protocol 3 keeps serving today's v2
    # specs — the marker, not the page build, selects the choreography.
    from jasper.capture_relay.session import validate_capture_page

    v2_spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        stimulus_duration_ms=4000,
    )
    assert v2_spec.capture_protocol_version == 2
    v3_page = {
        "schema_version": 1,
        "capture_protocol_version": 3,
        "supported_capture_protocol_versions": [1, 2, 3],
        "capture_page_build": "20260801.1",
    }
    validate_capture_page(v3_page, v2_spec)  # no raise
