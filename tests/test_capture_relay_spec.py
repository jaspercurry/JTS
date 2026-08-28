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

import re

import pytest

import jasper.capture_relay as capture_relay
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


def test_package_exports_every_shipped_capture_builder() -> None:
    for builder in capture_relay.BUILDERS.values():
        assert getattr(capture_relay, builder.__name__) is builder
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

    assert spec.screen == (
        {"type": "heading", "text": "Ready to repeat the main seat"},
        {
            "type": "note",
            "text": (
                "Keep the same microphone selected and return it to the main "
                "listening position. This extra capture checks that the result "
                "is trustworthy."
            ),
        },
        {
            "type": "button",
            "label": "Start measurement",
            "action": "begin_capture",
        },
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
    # Room reports direct-arrival alignment evidence but does not advertise a
    # hard gate until its fleet threshold has been calibrated.
    assert s.validity.require_alignment is False
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


def test_capture_only_room_screen_is_owned_by_the_pi_spec():
    s = build_room_sweep_spec(
        position=2,
        total_positions=5,
        guided_setup=False,
    )

    assert s.screen == (
        {"type": "heading", "text": "Ready for position 2 of 5"},
        {
            "type": "note",
            "text": (
                "The speaker has set this position. Keep the same microphone "
                "selected and place it where the speaker shows you."
            ),
        },
        {
            "type": "button",
            "label": "Start measurement",
            "action": "begin_capture",
        },
    )


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


def test_from_dict_refuses_a_spec_that_states_no_protocol():
    """A version-less spec is INCOMPATIBLE, not legacy — the same rule the
    capture page applies (capture-page/js/capture-protocol.js returns null,
    which fails the handshake).

    Defaulting on the parse boundary would let inbound bytes the page refuses
    through the Pi silently; that asymmetry is exactly how the deleted
    "no version means protocol 1" rule used to work. The dataclass field keeps
    its default so builders stay ergonomic — strictness belongs here."""
    payload = build_room_sweep_spec().to_dict()
    del payload["capture_protocol_version"]
    with pytest.raises(CaptureSpecError, match="capture_protocol_version is required"):
        CaptureSpec.from_dict(payload)
    # Explicit null is the same refusal, not a fall-through to the default.
    payload["capture_protocol_version"] = None
    with pytest.raises(CaptureSpecError, match="capture_protocol_version is required"):
        CaptureSpec.from_dict(payload)


def test_return_url_round_trips_for_phone_done_cta():
    s = build_room_sweep_spec().with_return_url("http://jts5.local/correction/")
    payload = s.to_dict()

    assert payload["return_url"] == "http://jts5.local/correction/"
    assert (
        CaptureSpec.from_dict(payload).return_url
        == "http://jts5.local/correction/"
    )


def test_time_budget_round_trips_and_is_omitted_when_absent():
    """The two clocks a household can run out of (work order D8, issue #1807).

    ABSENCE IS THE DEFAULT AND STAYS MEANINGFUL: every builder omits the key,
    so a page that finds nothing says nothing about time rather than inventing
    a number. That is also what makes a new capture page safe against a Pi that
    predates the field — see capture-page/README.md's "additive field that
    degrades on its own" class.
    """
    bare = build_room_sweep_spec()
    assert bare.time_budget is None
    assert "time_budget" not in bare.to_dict()
    assert CaptureSpec.from_dict(bare.to_dict()).time_budget is None

    published = bare.with_time_budget(step_s=120, session_s=900)
    assert published.to_dict()["time_budget"] == {"step_s": 120, "session_s": 900}
    assert CaptureSpec.from_dict(published.to_dict()).time_budget == {
        "step_s": 120,
        "session_s": 900,
    }


@pytest.mark.parametrize(
    "budget",
    [
        {"step_s": 120},                       # a half-stated pair
        {"session_s": 900},
        {"step_s": 0, "session_s": 900},       # "0 minutes" is not a budget
        {"step_s": 120, "session_s": -1},
        {"step_s": "two", "session_s": 900},   # not a number at all
        {"step_s": 120, "session_s": 900, "wall_clock_s": 3360},  # unknown clock
    ],
)
def test_time_budget_is_a_closed_two_key_vocabulary(budget):
    """A CLOSED set, like every other spec vocabulary: the page renders exactly
    these two clocks, so an unknown or half-stated one is drift to catch here
    rather than a sentence nobody wrote."""
    from dataclasses import replace

    with pytest.raises(CaptureSpecError):
        replace(build_room_sweep_spec(), time_budget=budget).validate()


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
    # `resolvable` (bool) alongside the rest (str) makes `**kwargs` unpacking
    # against DefaultSetupCalibration's mixed signature un-typeable without a
    # TypedDict; not worth it for a test helper, and tests/ is outside the
    # CI mypy gate's `files = ["jasper"]` scope.
    return DefaultSetupCalibration(**kwargs)  # type: ignore[arg-type]


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


# --- resolvable — gates the phone page's one-tap "stored" confirm (W2 addendum) -


def test_resolvable_true_round_trips_and_is_present_on_the_wire():
    populated = build_level_ramp_spec(
        default_setup_calibration=_household_hint(resolvable=True),
    )
    payload = populated.to_dict()
    assert payload["default_setup"]["calibration"]["resolvable"] is True
    again = CaptureSpec.from_dict(payload)
    assert again.default_setup_calibration.resolvable is True
    assert again.to_dict() == payload  # stable round-trip


def test_resolvable_false_is_omitted_from_the_wire_payload():
    # Default False (unset) — byte-identical to the pre-`resolvable` 4-key
    # shape so existing callers/pages are unaffected.
    populated = build_level_ramp_spec(default_setup_calibration=_household_hint())
    payload = populated.to_dict()
    assert "resolvable" not in payload["default_setup"]["calibration"]
    assert payload["default_setup"]["calibration"] == {
        "mode": "serial",
        "model": "minidsp_umik2",
        "serial_display": "8494",
        "calibration_id": "minidsp-minidsp_umik2-abc123456789",
    }

    explicit_false = build_level_ramp_spec(
        default_setup_calibration=_household_hint(resolvable=False),
    )
    assert "resolvable" not in explicit_false.to_dict()["default_setup"]["calibration"]

    # Absent on the wire round-trips back to False, not an error.
    again = CaptureSpec.from_dict(payload)
    assert again.default_setup_calibration.resolvable is False


def test_resolvable_must_be_a_boolean():
    good = _household_hint().to_dict()
    with pytest.raises(CaptureSpecError, match="resolvable must be a boolean"):
        DefaultSetupCalibration.from_dict({**good, "resolvable": "yes"})


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


def test_no_crossover_consent_screen_ships_a_dead_level_meter():
    """Flow-simplification §2.3: ``updateLevelMeters`` on the capture page is
    fed ONLY by the level-ramp protocol, so the meter this builder used to emit
    never moved on any crossover consent screen — and a meter that never moves
    reads as a broken mic. Removed from every crossover shape (per-driver,
    stationary summed, and the guided cloud alike), which is deliberately wide.

    The BUILDER stays: the level-ramp flow genuinely uses it, and
    ``test_ui_builders_emit_expected_shapes`` above pins its shape.
    """
    binding = "placement_abcdefghijklmnopqrstuv"
    shapes = {
        "per-driver": dict(driver_label="Woofer driver", driver_role="woofer"),
        "summed-stationary": dict(driver_label="crossover", driver_role="summed"),
        "guided-cloud": dict(
            driver_label="crossover", driver_role="summed", guided_captures=16,
            announced_captures=(1, 16),
        ),
    }
    for label, kwargs in shapes.items():
        spec = build_crossover_sweep_spec(acknowledgement_binding=binding, **kwargs)
        types = [component["type"] for component in spec.screen]
        assert "level_meter" not in types, label
    # The level-ramp consent screen still has one, so this is not a blanket
    # deletion of the component vocabulary.
    assert any(
        component["type"] == "level_meter" for component in build_level_ramp_spec().screen
    )


def test_a_summed_consent_heading_does_not_repeat_its_own_driver_label():
    """§2.3: the v2 cloud passes ``driver_label="crossover"`` into a heading
    template written for per-driver captures, which the household read as
    "Crossover — crossover". A summed capture measures the speaker."""
    binding = "placement_abcdefghijklmnopqrstuv"
    summed = build_crossover_sweep_spec(
        driver_label="crossover", driver_role="summed",
        acknowledgement_binding=binding,
    )
    heading = next(c for c in summed.screen if c["type"] == "heading")
    assert heading["text"] == "Tune your speaker"
    # A per-driver capture keeps its label — there it is genuinely informative.
    driver = build_crossover_sweep_spec(
        driver_label="Woofer driver", driver_role="woofer",
        acknowledgement_binding=binding,
    )
    driver_heading = next(c for c in driver.screen if c["type"] == "heading")
    assert driver_heading["text"] == "Crossover — Woofer driver"


def test_the_displayed_duration_estimate_matches_the_capture_pages_own():
    """§1.1: server-side consent copy and the phone's wake-lock hint must never
    quote different durations for one session, so the server derives from the
    SAME per-capture allowance the page uses. Read the page's constant out of
    its source rather than restating it here — the page is where the household
    sees the number.
    """
    import re
    from pathlib import Path

    from jasper.capture_protocol import (
        CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS,
        CapturePlan,
        CapturePlanEntry,
    )

    page = Path(__file__).resolve().parents[1] / "capture-page" / "js" / "main.js"
    match = re.search(
        r"const WAKE_LOCK_PER_CAPTURE_OVERHEAD_MS = (\d+);",
        page.read_text(encoding="utf-8"),
    )
    assert match is not None, "the capture page no longer declares the allowance"
    assert CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS == int(match.group(1))

    # …and the arithmetic itself: sum(duration + allowance), ceil to minutes.
    plan = CapturePlan(
        capture_target=2,
        max_attempts=3,
        schema_version=2,
        entries=(
            CapturePlanEntry(index=0, kind_label="a", duration_ms=40_000),
            CapturePlanEntry(index=1, kind_label="b", duration_ms=20_000),
        ),
    )
    assert plan.estimated_minutes() == 2  # (40+20+40) s → 1.67 min → ceil 2
    # A plan with no entry table has nothing to estimate from — never a fake 0
    # minutes presented as a duration; callers treat 0 as "say nothing".
    assert CapturePlan(capture_target=3, max_attempts=4).estimated_minutes() == 0


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


def test_every_shipped_builder_emits_the_one_capture_protocol():
    # There is exactly ONE capture protocol; a builder may not opt into a
    # different one. This replaces the old per-builder 1/2/3 split, where
    # room_sweep/sync_marker emitted 1 and level_ramp emitted 2 — a page
    # advertising only the current protocol could not have served them.
    from jasper.capture_relay.spec import BUILDERS

    for kind, builder in BUILDERS.items():
        spec = (
            builder(acknowledgement_binding="placement_abcdefghijklmnopqrstuv")
            if kind == "crossover_sweep"
            else builder()
        )
        assert spec.capture_protocol_version == CAPTURE_PROTOCOL_VERSION, kind
        # A plan is opt-in per call, never implied by the protocol.
        assert spec.capture_plan is None, kind
        assert "capture_plan" not in spec.to_dict(), kind


def test_capture_plan_serializes_without_changing_the_protocol():
    spec = _plan_spec()
    # Carrying a plan does NOT move the protocol — plan-ness is the plan's
    # presence alone (the capture page branches on exactly this).
    assert spec.capture_protocol_version == CAPTURE_PROTOCOL_VERSION
    d = spec.to_dict()
    assert d["capture_protocol_version"] == CAPTURE_PROTOCOL_VERSION
    assert d["capture_plan"] == {
        "schema_version": 1,
        "capture_target": 3,
        "max_attempts": 4,
    }
    # Round-trips through the inbound validation path.
    rebuilt = CaptureSpec.from_dict(d)
    assert rebuilt.capture_plan == spec.capture_plan
    assert rebuilt.capture_protocol_version == CAPTURE_PROTOCOL_VERSION


def test_capture_plan_requires_an_acknowledgement_binding():
    with pytest.raises(CaptureSpecError, match="acknowledgement_binding"):
        _plan_spec(acknowledgement_binding="")


def test_capture_plan_presence_is_decoupled_from_the_protocol():
    # The old contract was a biconditional: a plan REQUIRED protocol 3 and
    # protocol 3 REQUIRED a plan. With one protocol both halves are gone —
    # keeping the "protocol 3 requires a plan" half would have rejected every
    # plan-free flow (room_sweep, level_ramp, sync_marker), which all now
    # emit this same protocol.
    from dataclasses import replace

    from jasper.capture_relay.spec import CapturePlan

    base = _plan_spec()
    # A plan-free spec at the one protocol is valid...
    replace(base, capture_plan=None).validate()
    # ...and so is a plan-carrying one.
    base.validate()
    # Any other protocol number is refused outright — no negotiation.
    for bogus in (1, 2, 4):
        with pytest.raises(CaptureSpecError, match="capture_protocol_version"):
            replace(base, capture_protocol_version=bogus).validate()
    assert CapturePlan(capture_target=3, max_attempts=4).schema_version == 1


@pytest.mark.parametrize(
    ("target", "attempts", "match"),
    [
        (0, 4, "1..max_attempts"),
        (5, 4, "1..max_attempts"),
        # Derived, never a literal: a hardcoded bound goes silently stale the
        # next time the ceiling moves.
        (3, spec_mod.MAX_CAPTURE_PLAN_ATTEMPTS + 1, "<= "),
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


def test_capture_plan_accepts_the_multi_position_capacity_the_choreography_needs():
    """A plan larger than the pre-raise ceiling of 8 validates up to the new cap.

    The regime pinned here is the ENTRY count PR-3b's choreography needs
    (docs/historical/linearization-campaign-2026-07.md § PR-3b: 21 entries at the
    documented maxima) plus the retake budget that shares `max_attempts` — not
    an arbitrary large number.
    """
    from dataclasses import replace

    from jasper.capture_relay.spec import (
        LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS,
        MAX_CAPTURE_PLAN_ATTEMPTS,
        CapturePlan,
        CapturePlanEntry,
    )

    worst_case_entries = 21
    assert worst_case_entries > LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS
    assert worst_case_entries <= MAX_CAPTURE_PLAN_ATTEMPTS
    plan = CapturePlan(
        capture_target=worst_case_entries,
        max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(index=i, kind_label="cloud_measure", duration_ms=20_000)
            for i in range(worst_case_entries)
        ),
    )
    replace(_plan_spec(), capture_plan=plan).validate()

    # Exactly at the cap is legal; one past it is refused.
    at_cap = CapturePlan(
        capture_target=1, max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS
    )
    replace(_plan_spec(), capture_plan=at_cap).validate()
    over = CapturePlan(
        capture_target=1, max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS + 1
    )
    with pytest.raises(CaptureSpecError, match="max_attempts must be <="):
        replace(_plan_spec(), capture_plan=over).validate()


def test_legacy_plan_ceiling_is_frozen_at_the_pre_capacity_worker_value():
    """`LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS` describes a DEPLOYED artifact.

    It is the ceiling a relay Worker published before `GET /capabilities`
    existed, so it can never be bumped alongside the live cap — doing so would
    make the Pi assume an un-updated relay could store indexes it will reject.
    """
    assert spec_mod.LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS == 8
    assert spec_mod.LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS <= (
        spec_mod.MAX_CAPTURE_PLAN_ATTEMPTS
    )


def test_max_capacity_plan_with_product_sized_prompt_copy_fits_the_worker_spec_cap():
    """A full-capacity plan with PR-3b-sized prompt copy fits `MAX_SPEC_BYTES`.

    The relay caps the OPAQUE spec at 64 KiB, which at 32 entries leaves ~2 KiB
    of spec budget per entry — BELOW the per-entry
    `MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES` ceiling of 4 KiB. So the cap raise
    makes a `capture_spec_too_large` registration refusal newly reachable, and
    the regime that must stay comfortable is the product one: a title + body +
    auto-advance policy per entry, the shape `build_v2_capture_plan` already
    emits. This pins that budget rather than the pathological one.
    """
    import json
    from dataclasses import replace
    from pathlib import Path

    from jasper.capture_relay.spec import (
        MAX_CAPTURE_PLAN_ATTEMPTS,
        CapturePlan,
        CapturePlanEntry,
    )

    worker_src = (
        Path(__file__).resolve().parent.parent / "relay" / "src" / "worker.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"const MAX_SPEC_BYTES = ([0-9 *]+);", worker_src)
    assert match is not None, "worker MAX_SPEC_BYTES not found"
    max_spec_bytes = eval(match.group(1), {"__builtins__": {}})  # noqa: S307

    # Copy at the upper end of what the shipped v2 entries carry (the longest
    # live `body` is ~150 chars; allow generous headroom for a position prompt).
    #
    # RE-DERIVED for PR-T4 (#1805/#1806): the representative body is now a
    # numeric ABSOLUTE pose with "the microphone" as the actor, matching the
    # register the shipped prompts moved to. Keeping the withdrawn hand-width
    # phrasing here would have left the size check measuring copy the flow no
    # longer emits — and the new register is LONGER, which is exactly why the
    # sample has to track it rather than stay frozen.
    body = (
        "Move the microphone 16 in (40 cm) to the LEFT of the mark, at mark "
        "height, then tap Start. Step a little toward the speaker as you go "
        "out, and stay quiet while JTS measures — about twenty seconds."
    )
    entries = tuple(
        CapturePlanEntry(
            index=i,
            kind_label="cloud_measure",
            duration_ms=20_000,
            screen={
                "title": f"Position {i + 1} of {MAX_CAPTURE_PLAN_ATTEMPTS}",
                "body": body,
                "auto_advance": "tap",
            },
        )
        for i in range(MAX_CAPTURE_PLAN_ATTEMPTS)
    )
    plan = CapturePlan(
        capture_target=MAX_CAPTURE_PLAN_ATTEMPTS,
        max_attempts=MAX_CAPTURE_PLAN_ATTEMPTS,
        schema_version=2,
        entries=entries,
    )
    spec = replace(_plan_spec(), capture_plan=plan)
    spec.validate()
    encoded = json.dumps(spec.to_dict(), separators=(",", ":")).encode("utf-8")
    assert len(encoded) < max_spec_bytes, (
        f"a full-capacity plan with product-sized prompt copy is "
        f"{len(encoded)} B, over the relay's {max_spec_bytes} B opaque-spec cap"
    )


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
    assert spec_mod.CAPTURE_PROTOCOL_VERSION == 3
    # One protocol, one constant: the multi-version surface is deleted, not
    # merely emptied. A reintroduced list is how a legacy branch creeps back.
    assert not hasattr(spec_mod, "SUPPORTED_CAPTURE_PROTOCOL_VERSIONS")
    assert not hasattr(spec_mod, "SESSION_SPANNING_CAPTURE_PROTOCOL_VERSION")


def test_worker_stays_opaque_to_capture_plan_entries():
    # Wave 3 (crossover-measurement-productization-design.md §5.7): entries /
    # kind_label / duration_ms / screen are Pi-side and page-side ONLY. The
    # relay never parses capture_spec at all (see its own module docstring);
    # pin that none of the new field names leaked into the Worker source,
    # alongside the attempt-ceiling lockstep test above.
    from pathlib import Path

    worker_src = (
        Path(__file__).resolve().parent.parent / "relay" / "src" / "worker.js"
    ).read_text(encoding="utf-8")
    for token in ("entries", "kind_label", "CapturePlanEntry"):
        assert token not in worker_src, (
            f"worker.js must stay opaque to capture_plan.{token} — the relay "
            "never parses capture_spec"
        )


# --- CapturePlanEntry (per-capture heterogeneity, schema_version 2, SPEC ------
# --- crossover-measurement-productization-design.md §5.7) --------------------


def _entry(index, *, kind_label="check", duration_ms=5000, screen=None):
    from jasper.capture_relay.spec import CapturePlanEntry

    return CapturePlanEntry(
        index=index, kind_label=kind_label, duration_ms=duration_ms, screen=screen
    )


def _entries_plan(**overrides):
    from jasper.capture_relay.spec import CapturePlan

    entries = overrides.pop("entries", None)
    if entries is None:
        entries = (
            _entry(0, kind_label="check", duration_ms=25000),
            _entry(1, kind_label="measure", duration_ms=20000),
            _entry(2, kind_label="verify", duration_ms=15000, screen={"title": "Verify"}),
        )
    kwargs = dict(
        capture_target=3, max_attempts=3, schema_version=2, entries=entries
    )
    kwargs.update(overrides)
    return CapturePlan(**kwargs)


def test_capture_plan_entries_round_trip_through_to_dict_from_dict():
    from jasper.capture_relay.spec import CapturePlan

    plan = _entries_plan()
    d = plan.to_dict()
    assert d["schema_version"] == 2
    assert d["entries"] == [
        {"index": 0, "kind_label": "check", "duration_ms": 25000},
        {"index": 1, "kind_label": "measure", "duration_ms": 20000},
        {
            "index": 2,
            "kind_label": "verify",
            "duration_ms": 15000,
            "screen": {"title": "Verify"},
        },
    ]
    rebuilt = CapturePlan.from_dict(d)
    assert rebuilt == plan


def test_capture_plan_entries_round_trip_via_a_full_spec():
    spec = _plan_spec(capture_plan=_entries_plan())
    rebuilt = CaptureSpec.from_dict(spec.to_dict())
    assert rebuilt.capture_plan == spec.capture_plan
    assert rebuilt.to_dict() == spec.to_dict()


def test_capture_plan_entry_for_index_maps_one_based_wire_index_to_zero_based_entry():
    from jasper.capture_relay.spec import CapturePlan

    plan = _entries_plan()
    assert plan.entry_for_index(1) == plan.entries[0]
    assert plan.entry_for_index(2) == plan.entries[1]
    assert plan.entry_for_index(3) == plan.entries[2]
    assert plan.entry_for_index(4) is None  # out of range, never reachable post-validate
    # A v1 plan (no entry table) always resolves to None — dormant.
    v1_plan = CapturePlan(capture_target=3, max_attempts=4)
    assert v1_plan.entry_for_index(1) is None


def test_capture_plan_entries_require_schema_version_two_and_vice_versa():
    from dataclasses import replace

    from jasper.capture_relay.spec import CapturePlan

    # entries present but schema_version left at 1 -> rejected.
    with pytest.raises(CaptureSpecError, match="schema_version"):
        _plan_spec(capture_plan=_entries_plan(schema_version=1))
    # schema_version 2 with NO entries -> rejected (the reciprocal contract).
    with pytest.raises(CaptureSpecError, match="requires entries"):
        _plan_spec(capture_plan=replace(_entries_plan(), entries=None))
    # v1 payload without entries stays exactly as it was — the whole point of
    # the additive design.
    _plan_spec(capture_plan=CapturePlan(capture_target=3, max_attempts=4))


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        (  # gap: missing index 1
            (_entry(0), _entry(2)),
            "0..capture_target-1",
        ),
        (  # duplicate index
            (_entry(0), _entry(0), _entry(1)),
            "duplicate",
        ),
        (  # out-of-range index (only 0..1 valid for capture_target=2... but
           # here capture_target stays 3 with a 3rd entry indexed 5)
            (_entry(0), _entry(1), _entry(5)),
            "0..capture_target-1",
        ),
    ],
    ids=["gap", "duplicate", "out-of-range"],
)
def test_capture_plan_entries_must_cover_indexes_exactly(entries, match):
    with pytest.raises(CaptureSpecError, match=match):
        _plan_spec(capture_plan=_entries_plan(entries=entries))


@pytest.mark.parametrize(
    ("bad_entry", "match"),
    [
        (_entry(0, duration_ms=0), "duration_ms must be positive"),
        (_entry(0, duration_ms=-100), "duration_ms must be positive"),
        (_entry(0, kind_label=""), "short lowercase slug"),
        (_entry(0, kind_label="Check"), "short lowercase slug"),
        (_entry(0, kind_label="check one"), "short lowercase slug"),
    ],
)
def test_capture_plan_entry_field_bounds_are_strict(bad_entry, match):
    entries = (bad_entry, _entry(1), _entry(2))
    with pytest.raises(CaptureSpecError, match=match):
        _plan_spec(capture_plan=_entries_plan(entries=entries))


def test_capture_plan_entry_screen_must_map_strings_to_strings():
    entries = (
        _entry(0, screen={"title": 5}),
        _entry(1),
        _entry(2),
    )
    with pytest.raises(CaptureSpecError, match="strings to strings"):
        _plan_spec(capture_plan=_entries_plan(entries=entries))


def test_capture_plan_entry_screen_is_size_bounded():
    oversized = {"body": "x" * spec_mod.MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES}
    entries = (_entry(0, screen=oversized), _entry(1), _entry(2))
    with pytest.raises(CaptureSpecError, match="exceeds"):
        _plan_spec(capture_plan=_entries_plan(entries=entries))
    # Comfortably under the ceiling is fine.
    fine = {"title": "Verify", "body": "Stand back and stay quiet."}
    _plan_spec(
        capture_plan=_entries_plan(
            entries=(_entry(0, screen=fine), _entry(1), _entry(2))
        )
    )


def test_capture_plan_entries_from_dict_rejects_unknown_keys_and_bad_shapes():
    from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry

    with pytest.raises(CaptureSpecError, match="unknown keys"):
        CapturePlanEntry.from_dict(
            {"index": 0, "kind_label": "check", "duration_ms": 1000, "x": 1}
        )
    with pytest.raises(CaptureSpecError, match="must be an object or null"):
        CapturePlanEntry.from_dict(
            {"index": 0, "kind_label": "check", "duration_ms": 1000, "screen": "nope"}
        )
    with pytest.raises(CaptureSpecError, match="must be a list"):
        CapturePlan.from_dict(
            {
                "schema_version": 2,
                "capture_target": 1,
                "max_attempts": 1,
                "entries": "not-a-list",
            }
        )


def test_compat_matrix_a_page_without_the_current_protocol_is_refused():
    # The page/Pi handshake is the surviving compatibility mechanism, and it
    # still fails closed BEFORE any tone: a stale page that advertises only
    # the deleted protocols cannot run today's choreography.
    from jasper.capture_relay.session import (
        CapturePageIncompatible,
        validate_capture_page,
    )

    spec = _plan_spec()
    stale_page = {
        "schema_version": 1,
        "capture_protocol_version": 2,
        "supported_capture_protocol_versions": [1, 2],
        "capture_page_build": "20260716.1",
    }
    with pytest.raises(CapturePageIncompatible, match="expected protocol 3"):
        validate_capture_page(stale_page, spec)


def test_compat_matrix_current_page_serves_plan_free_and_plan_specs_alike():
    # One protocol covers BOTH shapes, so the same published page serves a
    # plan-free capture and a session-spanning one. Before the deletion the
    # plan-free crossover spec was protocol 2 and this page would have had to
    # advertise two versions to serve both.
    from jasper.capture_relay.session import validate_capture_page

    plan_free = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
        stimulus_duration_ms=4000,
    )
    assert plan_free.capture_plan is None
    assert plan_free.capture_protocol_version == CAPTURE_PROTOCOL_VERSION
    current_page = {
        "schema_version": 1,
        "capture_protocol_version": 3,
        "supported_capture_protocol_versions": [3],
        "capture_page_build": "20260801.1",
    }
    validate_capture_page(current_page, plan_free)  # no raise
    validate_capture_page(current_page, _plan_spec())  # no raise


def test_compat_matrix_current_spec_is_served_by_the_deployed_page():
    """THE claim this release's deploy order rests on: a Pi carrying the
    protocol deletion still works against the page that is deployed RIGHT NOW,
    which advertises [1, 2, 3].

    That is why this change ships Pi-first, page-second — the reverse order
    would break every un-upgraded Pi the moment the [3] page went live. If
    this test ever fails, the deploy sequencing documented in
    capture-page/README.md and phone-mic-relay-plan.md is wrong."""
    from jasper.capture_relay.session import validate_capture_page

    deployed_page_today = {
        "schema_version": 1,
        "capture_protocol_version": 3,
        "supported_capture_protocol_versions": [1, 2, 3],
        "capture_page_build": "20260727.1",
    }
    validate_capture_page(deployed_page_today, build_room_sweep_spec())  # no raise
    validate_capture_page(deployed_page_today, _plan_spec())  # no raise
