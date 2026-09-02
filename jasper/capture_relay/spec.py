# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-kind capture-spec builders for the phone-mic relay.

The spec contract itself — :class:`CaptureSpec`, its validation, and its
consent-surface vocabulary — is owned by
:mod:`jasper.active_speaker.crossover_v2.sweep_spec` and re-exported here for
this package's callers. This module adds only the relay's own per-kind
builders: each fills the shared fields and the relay carries the result as
opaque bytes, so a new kind needs no relay change.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jasper.active_speaker.crossover_v2.sweep_spec import (
    CALIBRATION_MODEL_KEYS,
    CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION,
    CAPTURE_PLAN_SCHEMA_VERSIONS,
    CAPTURE_PROTOCOL_VERSION,
    CLEAN_CAPTURE_POLICIES,
    CLOCK_DRIFT_MODES,
    DEFAULT_MAX_UPLOAD_BYTES,
    DEFAULT_SETUP_CALIBRATION_KEYS,
    DEFAULT_SETUP_CALIBRATION_MODES,
    DEFAULT_THEME,
    HARD_MAX_UPLOAD_BYTES,
    MAX_CAPTURE_PLAN_ATTEMPTS,
    MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES,
    OUTPUT_FORMATS,
    REQUIRED_CHANNELS,
    REQUIRED_SAMPLE_RATE_HZ,
    RETURN_URL_SCHEMES,
    SCHEMA_VERSION,
    STIMULUS_PLAYERS,
    THEME_ACCENTS,
    THEME_FONTS,
    TIME_BUDGET_KEYS,
    UI_BUTTON_ACTIONS,
    UI_COMPONENT_TYPES,
    UI_METER_SOURCES,
    CaptureAcknowledgement,
    CaptureConstraints,
    CapturePlan,
    CapturePlanEntry,
    CaptureSpec,
    CaptureSpecError,
    CaptureStimulus,
    CaptureValidity,
    DefaultSetupCalibration,
    build_crossover_sweep_spec,
    build_theme,
    ui_button,
    ui_heading,
    ui_level_meter,
    ui_note,
    ui_steps,
)

__all__ = [
    "BUILDERS",
    "CALIBRATION_MODEL_KEYS",
    "CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION",
    "CAPTURE_PLAN_SCHEMA_VERSIONS",
    "CAPTURE_PROTOCOL_VERSION",
    "CLEAN_CAPTURE_POLICIES",
    "CLOCK_DRIFT_MODES",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_SETUP_CALIBRATION_KEYS",
    "DEFAULT_SETUP_CALIBRATION_MODES",
    "DEFAULT_THEME",
    "HARD_MAX_UPLOAD_BYTES",
    "LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS",
    "MAX_CAPTURE_PLAN_ATTEMPTS",
    "MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES",
    "OUTPUT_FORMATS",
    "REQUIRED_CHANNELS",
    "REQUIRED_SAMPLE_RATE_HZ",
    "RETURN_URL_SCHEMES",
    "SCHEMA_VERSION",
    "SHIPPED_KINDS",
    "STIMULUS_PLAYERS",
    "THEME_ACCENTS",
    "THEME_FONTS",
    "TIME_BUDGET_KEYS",
    "UI_BUTTON_ACTIONS",
    "UI_COMPONENT_TYPES",
    "UI_METER_SOURCES",
    "CaptureAcknowledgement",
    "CaptureConstraints",
    "CapturePlan",
    "CapturePlanEntry",
    "CaptureSpec",
    "CaptureSpecError",
    "CaptureStimulus",
    "CaptureValidity",
    "DefaultSetupCalibration",
    "build_balance_burst_spec",
    "build_crossover_sweep_spec",
    "build_level_ramp_spec",
    "build_room_sweep_spec",
    "build_sync_marker_spec",
    "build_theme",
    "ui_button",
    "ui_heading",
    "ui_level_meter",
    "ui_note",
    "ui_steps",
]


# The ceiling every relay Worker deployed BEFORE the capacity raise enforces.
#
# This is a FROZEN historical constant describing a deployed artifact, not a
# tunable: it must never be bumped alongside `MAX_CAPTURE_PLAN_ATTEMPTS` (its
# whole job is to describe what the OLD Worker does). A pre-capacity Worker has
# no `GET /capabilities` endpoint, and the relay carried no version surface
# before that endpoint existed — so the endpoint's ABSENCE is the only honest
# version signal available, and the Pi reads it as exactly this ceiling.
#
# `register_session` refuses a larger plan at session setup rather than letting
# the skew surface mid-session: a pre-capacity Worker rejects blob index >= 8 on
# BOTH the phone's `PUT /blob` and the Pi's `GET /blob`, so attempt 9 would
# 400 `bad_capture_index` after the operator had already walked eight prompted
# positions. Plans at or below this size never probe, so every flow a
# pre-capacity Pi could already emit keeps its exact request sequence.
LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS = 8

# --- Per-kind builders --------------------------------------------------------


def build_room_sweep_spec(
    *,
    stimulus_duration_ms: int = 10000,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 700,
    hard_timeout_ms: int = 30000,
    position: int | None = None,
    total_positions: int | None = None,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    calibration_models: Sequence[Mapping[str, Any]] | None = None,
    guided_setup: bool = True,
    setup_binding_id: str = "",
    presentation_variant: str = "",
) -> CaptureSpec:
    """Build the `kind="room_sweep"` capture spec (plan §6, build step 1).

    ``duration_ms`` is the hard recording timeout, not the usual stop condition:
    the phone records until the Pi reports ``sweep_complete`` through the relay,
    then keeps ``post_roll_ms`` of room tail. ``pre_roll_ms`` remains part of the
    spec for compatibility/documentation, but the race is now prevented more
    directly: the phone starts recording before it posts ``armed`` and the Pi only
    plays after seeing that event. Magnitude frequency response is drift-insensitive, so
    ``clock_drift="ignore"``; clean capture is mandatory (EC/AGC/NS flatten the
    response we measure) so ``clean_capture="refuse"`` — paired with a labeled
    device-capability fallback so a strict iPhone is never dead-ended.

    The ``ui`` is server-driven: the heading, steps, and button label all ship
    from here, so copy/choreography changes ride a Pi update with no web deploy.
    ``position`` / ``total_positions`` tailor the copy for the multi-position
    correction flow.
    """
    if stimulus_duration_ms <= 0:
        raise CaptureSpecError("stimulus_duration_ms must be positive")
    if pre_roll_ms < 0 or post_roll_ms < 0:
        raise CaptureSpecError("pre_roll_ms / post_roll_ms must be >= 0")
    if presentation_variant not in {"", "trust_repeat"}:
        raise CaptureSpecError(
            "room_sweep presentation_variant must be empty or trust_repeat"
        )
    duration_ms = max(
        pre_roll_ms + stimulus_duration_ms + post_roll_ms,
        int(hard_timeout_ms),
    )

    seconds = round(stimulus_duration_ms / 1000)
    if calibration_models is None and guided_setup:
        from jasper.audio_measurement.calibration import supported_model_options

        calibration_models = supported_model_options()
    elif calibration_models is None:
        calibration_models = ()

    screen: tuple[Mapping[str, Any], ...]
    if presentation_variant == "trust_repeat":
        screen = (
            ui_heading("Ready to repeat the main seat"),
            ui_note(
                "Keep the same microphone selected and return it to the main "
                "listening position. This extra capture checks that the result "
                "is trustworthy."
            ),
            ui_button("Start measurement", action="begin_capture"),
        )
    elif not guided_setup:
        position_label = (
            f"position {position} of {total_positions}"
            if position is not None and total_positions
            else "this room position"
        )
        screen = (
            ui_heading(f"Ready for {position_label}"),
            ui_note(
                "The speaker has set this position. Keep the same microphone "
                "selected and place it where the speaker shows you."
            ),
            ui_button("Start measurement", action="begin_capture"),
        )
    else:
        heading_text = (
            f"Room measurement — position {position} of {total_positions}"
            if position is not None and total_positions
            else "Room measurement"
        )
        screen = (
            ui_heading(heading_text),
            ui_steps(
                [
                    "Stand at your listening position",
                    "Hold the microphone up at ear height",
                    f"Tap Start, then stay quiet for about {seconds} seconds",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        )

    spec = CaptureSpec(
        kind="room_sweep",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),  # all false → measurement-clean
        stimulus=CaptureStimulus(played_by="pi", label="log sweep 20 Hz – 20 kHz"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            # Room alignment is observation-only while fleet evidence is
            # collected; the relay adapter emits capture_relay.alignment from
            # the persisted direct-arrival proxy. Do not advertise a hard gate
            # until its threshold is calibrated on representative speakers.
            require_alignment=False,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=screen,
        calibration_models=tuple(calibration_models),
        max_upload_bytes=max_upload_bytes,
        # Mic choice + calibration are session setup, not per-position work.
        # The first level-check link validates and freezes them on the Pi; later
        # position links are intentionally capture-only and report the realized
        # device for the Pi's identity check before playback and after upload.
        setup_validation=guided_setup,
        setup_binding_id=setup_binding_id,
        position=position,
        total_positions=total_positions,
        presentation_variant=presentation_variant,
    )
    return spec.validate()


# The sibling builders below are the plan §14 step-8 generalization. Each is a
# new measurement KIND added with **zero relay change** (the relay is opaque) and
# **zero page-renderer change** (every screen reuses the closed component
# vocabulary — `ui_heading` / `ui_steps` / `ui_level_meter` / `ui_button` /
# `ui_note`). The only per-kind differences are copy (server-driven) and the
# validity policy, both carried as DATA in the spec. Pinned by
# tests/test_capture_relay_kinds.py.


def build_balance_burst_spec(
    *,
    stimulus_duration_ms: int = 2400,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 600,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """`kind="balance_burst"` — left/right level balance.

    Clean capture is mandatory: auto-gain would normalize away the very L/R level
    difference being measured (`clean_capture="refuse"`). It is a level
    comparison, not an arrival-timing one, so alignment is not required and clock
    drift is irrelevant (`require_alignment=False`, `clock_drift="ignore"`).
    """
    duration_ms = pre_roll_ms + stimulus_duration_ms + post_roll_ms
    return CaptureSpec(
        kind="balance_burst",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),
        stimulus=CaptureStimulus(played_by="pi", label="left then right level bursts"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=False,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading("Speaker balance"),
            ui_steps(
                [
                    "Sit centred between the two speakers",
                    "Hold the microphone up at ear height",
                    "Tap Start and stay still while each side plays",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
    ).validate()


def build_sync_marker_spec(
    *,
    stimulus_duration_ms: int = 2000,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 600,
    hard_timeout_ms: int = 30000,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """`kind="sync_marker"` — left/right arrival-time delta.

    Both L and R markers land inside ONE recording so the independent mic/playback
    clock drift is common-mode and cancels (`clock_drift="single_window"`, §9) —
    the timing answer comes from comparing the two markers within the single
    capture, never across separate captures. Arrival alignment is the signal, so
    `require_alignment=True`. The window must contain both markers (the Pi plays
    them at ~0.5 s and ~1.5 s); `stimulus_duration_ms` spans that.

    ``duration_ms`` is the phone's HARD recording deadline whose clock starts at
    ``armed`` (its ``waitForSweepComplete`` throws when it expires), so — like
    ``room_sweep`` and ``crossover_sweep`` — the acoustic window is floored by
    ``hard_timeout_ms``. The pre-floor value (3 400 ms) left ~1.4 s for the Pi's
    entire armed-poll → playback → ``sweep_complete``-post round trip, which
    killed every sync relay capture; the normal stop is the Pi's
    ``sweep_complete`` relay event (published by ``sync_flow.
    relay_run_and_consume``), the deadline is only the backstop.
    """
    duration_ms = max(
        pre_roll_ms + stimulus_duration_ms + post_roll_ms,
        int(hard_timeout_ms),
    )
    return CaptureSpec(
        kind="sync_marker",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),
        stimulus=CaptureStimulus(played_by="pi", label="left/right sync markers"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=True,
            clock_drift="single_window",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading("Speaker sync"),
            ui_steps(
                [
                    "Sit at your listening position",
                    "Hold the microphone up at ear height",
                    "Tap Start and stay quiet for the two clicks",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
    ).validate()

def build_level_ramp_spec(
    *,
    geometry_label: str = "listening position",
    placement_instruction: str = "",
    tone_frequency_hz: float = 1000.0,
    hard_timeout_ms: int = 75000,
    pre_roll_ms: int = 400,
    post_roll_ms: int = 400,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    run_token: str = "",
    calibration_models: Sequence[Mapping[str, Any]] | None = None,
    setup_binding_id: str = "",
    setup_collect_positions: bool = False,
    default_setup_calibration: DefaultSetupCalibration | None = None,
) -> CaptureSpec:
    """`kind="level_ramp"` — the relay-closed level-match ramp (§3.1, P2).

    Unlike the sweep kinds this capture does NOT upload a WAV to analyze: the Pi
    plays a quiet-start staircase of band-limited noise while the phone streams
    **batched, client-timestamped mic-level samples** over the relay ``event``
    channel, and the Pi's :class:`~jasper.audio_measurement.ramp.RampController`
    settles into the safe window and locks. ``duration_ms`` is therefore a hard
    phone-side *timeout* sized ABOVE the Pi's own derived safety timeout
    (``MeasurementRamp.safety_timeout``, ≈56 s at defaults) so the Pi's stop is
    always the real one; the phone otherwise stops streaming when the Pi posts a
    terminal ``ramp`` host event (re-posted until the relay echoes it back —
    the ``event`` slot is a read-modify-write race).

    ``run_token`` is the per-run nonce (mint one per ramp run; pass the same
    value to ``LevelMatchSession.run_for_geometry``): the phone echoes it in
    every level batch so a previous run's persisted relay slot can never
    insta-cancel or mis-feed a retry.

    Clean capture is mandatory — auto-gain would flatten the very level the ramp
    maps. The page additionally requires explicit realized
    ``autoGainControl=false`` and refuses before the tone when a browser cannot
    prove it; ``allow_capability_fallback`` does not authorize a degraded
    automatic level result. It is a
    level comparison, not a timing one, so alignment is not required and clock
    drift is irrelevant (``require_alignment=False``, ``clock_drift="ignore"``).
    ``geometry_label`` tailors the heading for the near-field (baffle) vs
    listening-position step. ``placement_instruction`` optionally supplies the
    exact Pi-owned geometry copy; the page renders that same instruction after
    microphone setup instead of inventing a second placement description.

    ``default_setup_calibration`` is the OPTIONAL household-mic prefill hint
    (Wave-2 persistence, ``jasper.correction.household_mic``) — see
    ``DefaultSetupCalibration``. Omitted by default so existing callers are
    unaffected; the room and crossover level-match handlers in
    ``jasper/web/correction_setup.py`` pass one when a household record
    exists.
    """
    duration_ms = max(pre_roll_ms + post_roll_ms + 1000, int(hard_timeout_ms))
    if calibration_models is None:
        from jasper.audio_measurement.calibration import supported_model_options

        calibration_models = supported_model_options()
    return CaptureSpec(
        kind="level_ramp",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),  # all false → measurement-clean
        stimulus=CaptureStimulus(
            played_by="pi",
            label=f"{float(tone_frequency_hz):g} Hz level-match tone",
        ),
        run_token=run_token,
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=False,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading(f"Level match — {geometry_label}"),
            ui_steps(
                [
                    placement_instruction
                    or f"Place the microphone at the {geometry_label}",
                    "Tap Start — the speaker rises slowly from quiet",
                    "Stay still; it locks the level automatically",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start level check", action="begin_capture"),
            ui_button("Stop", action="stop"),
            ui_note("Keep the screen on — leaving this page stops the level match."),
        ),
        calibration_models=tuple(calibration_models),
        setup_validation=True,
        setup_binding_id=(setup_binding_id or (f"level-{run_token}" if run_token else "")),
        setup_collect_positions=setup_collect_positions,
        default_setup_calibration=default_setup_calibration,
        max_upload_bytes=max_upload_bytes,
    ).validate()


# The kinds JTS ships a builder for today. The relay never sees this list — it is
# Pi-side only. Adding a kind appends one builder above; the relay and page need
# no change.
SHIPPED_KINDS = (
    "room_sweep",
    "balance_burst",
    "sync_marker",
    "crossover_sweep",
    "level_ramp",
)

BUILDERS = {
    "room_sweep": build_room_sweep_spec,
    "balance_burst": build_balance_burst_spec,
    "sync_marker": build_sync_marker_spec,
    "crossover_sweep": build_crossover_sweep_spec,
    "level_ramp": build_level_ramp_spec,
}
