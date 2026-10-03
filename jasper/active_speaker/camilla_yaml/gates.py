# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from typing import Any, Collection, Mapping

import yaml

from jasper.platform.log_event import log_event
from jasper.platform.speaker_layout import measurement_target_id

from ..camilla_names import (
    STARTUP_MUTE_GAIN_DB,
    driver_limiter_name,
    output_commission_mute_name,
)
from ..driver_protection import format_protection_hz, protection_highpass_floor_satisfied
from ..graph_safety import (
    GraphView,
    output_hard_muted_and_wired,
    output_highpass_protected,
    pipeline_reference_closure_errors,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
    view_from_yaml_dict,
)
from ..profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from ..test_signal_plan import declared_protection_floor_hz, strictest_crossover_highpass_hz
from .document import logger
from .filters import STARTUP_LIMITER_CLIP_LIMIT_DB
from .topology import _channels_for_role

#: ``result=`` slug of the L0 emit gate's below-declared-floor refusal
#: (:func:`_assert_tweeter_crossover_honours_declared_floor`). The
#: machine-readable half of a hearing-safety refusal: the operator sentence may
#: be reworded, this may not. Sibling of the startup gate's
#: ``tweeter_crossover_below_declared_protection_floor`` blocker code.
EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR = (
    "blocked_tweeter_crossover_below_declared_floor"
)


def _assert_tweeter_crossover_honours_declared_floor(
    preset: ActiveSpeakerPreset,
) -> None:
    """Fail-closed L0 emit gate: refuse a crossover below the tweeter's own floor.

    The *bound* half of tweeter protection;
    :func:`_assert_tweeter_outputs_protected` is the *structural* half. A graph
    can carry a textbook-correct high-pass whose corner the driver's
    manufacturer forbids, which is why both questions are asked.

    Both compared numbers come from their owners and are never re-derived here
    (``strictest_crossover_highpass_hz``, ``declared_protection_floor_hz``, and
    the shared ``protection_highpass_floor_satisfied`` rule), so this gate
    cannot drift from the protective-high-pass clamp, the staged metadata, or
    the load gate.

    Two layers, deliberately: ``path_safety._tweeter_protection_floor_verdict``
    refuses the same condition at commission-load, over a graph already on disk;
    this runs inside the household-facing emitters before any YAML is built, so
    a routine apply cannot write a below-floor crossover at all. Neither
    subsumes the other. The commissioning-flow emitters are deliberately NOT
    gated — refusing there would replace the load gate's actionable refusal with
    a bare emit-time exception, and leave that gate untestable.

    Boundary semantics are the shared predicate's: *at* the floor is legal
    (``>=``), below it is refused, a declared floor with no readable crossover
    corner is refused, and a driver declaring NO floor is honoured unchanged.
    """
    floor_hz = declared_protection_floor_hz(preset, "tweeter")
    crossover_hz = strictest_crossover_highpass_hz(preset, "tweeter")
    if protection_highpass_floor_satisfied(
        highpass_hz=crossover_hz,
        floor_hz=floor_hz,
    ):
        return
    # Unreachable with floor_hz None: the predicate honours an absent floor, so
    # a refusal here always has a real declared number to name.
    assert floor_hz is not None
    floor = format_protection_hz(floor_hz)
    if crossover_hz is None:
        detail = (
            "the preset declares no crossover corner that high-passes the "
            "tweeter, so it cannot be proven to honour that driver's own "
            f"declared protective high-pass floor of {floor}"
        )
    else:
        detail = (
            f"tweeter crossover is {format_protection_hz(crossover_hz)}, below "
            f"that driver's own declared protective high-pass floor of {floor}; "
            f"raise the crossover to at least {floor} (or correct the driver's "
            "declared required_protection_filters)"
        )
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result=EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR,
        preset_id=preset.preset_id,
        tweeter_crossover_highpass_hz=crossover_hz,
        tweeter_protection_floor_hz=floor_hz,
    )
    raise ActiveSpeakerConfigError(
        "refusing to emit an active-speaker graph whose crossover is below the "
        "tweeter's declared protection floor: " + detail
    )


def _assert_tweeter_outputs_protected(
    yaml_text: str, preset: ActiveSpeakerPreset, *, decorated: bool = False,
) -> None:
    """Fail-closed L0 emit gate: refuse a graph with an unprotected tweeter output.

    Runs on every active-speaker graph this module emits, right before it is
    returned or written, and re-proves against the EMITTED TEXT (not the
    emitter's construction) that every physical output the preset assigns a
    ``tweeter`` role carries a protective high-pass.

    Structure only — whether the corner clears the driver's declared floor is
    :func:`_assert_tweeter_crossover_honours_declared_floor`'s separate concern.
    A compression driver is ~25 dB more sensitive than the woofer, so a graph
    routing full-range program to an unprotected tweeter output is a hot-tweeter
    hazard (hearing, AGENTS.md #1). A preset with no tweeter role has nothing to
    protect and the gate is a no-op. A block emits
    ``event=active_speaker.emit_gate`` before raising.
    """
    # ``decorated`` picks the view, and the choice is itself a check: the text
    # view REFUSES CamillaDSP's re-serialised dialect, which is how it catches
    # emitter drift, so an undecorated graph must still read in the emitter's
    # own spelling. Only a graph a decoration (dynamic bass, the rear
    # calibration stage) has re-serialised is read back parsed.
    if decorated:
        try:
            view = view_from_yaml_dict(yaml.safe_load(yaml_text))
        except yaml.YAMLError as exc:
            raise ActiveSpeakerConfigError(
                f"decorated active-speaker config did not parse as YAML: {exc}"
            ) from exc
    else:
        view = view_from_emitted_text(yaml_text)
    _assert_view_tweeters_protected(view, preset)


def _assert_view_tweeters_protected(view: GraphView, preset: ActiveSpeakerPreset) -> None:
    """:func:`_assert_tweeter_outputs_protected` over a graph already read."""
    tweeter_channels = _channels_for_role(preset, "tweeter")
    if not tweeter_channels:
        return
    unprotected = unprotected_tweeter_outputs(
        view, tweeter_channels=set(tweeter_channels),
    )
    if not unprotected:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_unprotected_tweeter",
        preset_id=preset.preset_id,
        outputs=",".join(str(index + 1) for index in unprotected),
    )
    raise ActiveSpeakerConfigError(
        "refusing to emit an active-speaker graph that sends full-range program "
        "to a tweeter/compression-driver output without a protective high-pass on "
        "DAC output(s) " + ", ".join(str(index + 1) for index in unprotected)
    )


def _assert_parked_outputs_muted(yaml_text: str, output_count: int) -> None:
    """Refuse to emit a parked graph unless EVERY output is a wired hard mute."""

    view = view_from_emitted_text(yaml_text)
    unmuted = [
        index
        for index in range(output_count)
        if not output_hard_muted_and_wired(
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
    ]
    if not unmuted:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        gate="parked_outputs_muted",
        outputs=output_count,
        unmuted=",".join(str(index) for index in unmuted),
        level=logging.ERROR,
    )
    raise ActiveSpeakerConfigError(
        "parked active-speaker graph left outputs unmuted: "
        + ", ".join(str(index) for index in unmuted)
    )


# --- channel-routed program graph ---------------------------------------- #
# The v2 crossover measurement flow plays ONE continuous 2-channel program WAV
# (docs/historical/crossover-measurement-productization-design.md §5.4):
# program capture ch0 carries the woofer stimulus, ch1 the tweeter stimulus,
# sequenced in the WAV so the CamillaDSP graph stays static (no reload
# mid-program). This graph
# maps each program capture channel to its driver's PHYSICAL output path.

# The slope the program graph's tweeter protection high-pass must reach: a
# declared protection below it refuses (ADR-0426).
PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE = 24.0


def preset_target_ids(preset: ActiveSpeakerPreset) -> frozenset[str]:
    """Every physical driver output's measurement target id (ADR-0316)."""
    return frozenset(
        measurement_target_id(output.driver_role, output.output_variant)
        for output in preset.channel_map.outputs
    )


def _validate_program_role_channels(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
    parked_target_ids: Collection[str] = (),
) -> dict[str, int]:
    """Fail-closed check that every driver role owns one distinct program channel.

    A take may PARK a role — every output of it then carries no source at all
    (:func:`_emit_role_routed_mixer`), which is how a front/rear take silences
    the tweeter — but only by NAMING those targets in ``parked_target_ids``. A
    role that is neither channelled nor declared parked refuses: silence must
    be a decision, never an omission.
    """

    if preset.local_subwoofer is not None:
        raise ActiveSpeakerConfigError(
            "program graph does not support a local subwoofer (2-way crossover "
            "measurement is out of scope for bass management)"
        )
    normalized: dict[str, int] = {}
    for role, channel in role_channels.items():
        if type(channel) is not int or channel < 0:
            raise ActiveSpeakerConfigError(
                f"program channel for role {role!r} must be a non-negative integer"
            )
        normalized[role] = channel
    declared: dict[str, set[str]] = {
        role: set() for role in required_driver_roles(preset.way_count)
    }
    for output in preset.channel_map.outputs:
        declared.setdefault(output.driver_role, set()).add(
            measurement_target_id(output.driver_role, output.output_variant)
        )
    every_id = set(declared).union(*declared.values())
    unknown = (set(normalized) | set(parked_target_ids)) - every_id
    if unknown or not normalized:
        raise ActiveSpeakerConfigError(
            "program role_channels names no declared driver: "
            + (", ".join(sorted(unknown)) or "(empty)")
        )
    parked = set(parked_target_ids)
    missing = sorted(
        role for role, target_ids in declared.items()
        if role not in normalized and not (target_ids and target_ids <= parked | set(normalized))
    )
    if missing:
        raise ActiveSpeakerConfigError(
            "program role_channels is missing a channel for role(s) "
            + ", ".join(missing)
        )
    if len(set(normalized.values())) != len(normalized):
        raise ActiveSpeakerConfigError(
            "each driver role must own a distinct program channel"
        )
    channels = sorted(normalized.values())
    if channels != list(range(len(channels))):
        raise ActiveSpeakerConfigError(
            "program channels must be contiguous from 0"
        )
    return normalized


def _assert_pipeline_references_closed(
    yaml_text: str, preset: ActiveSpeakerPreset
) -> None:
    """Fail-closed L0 emit gate: refuse a graph whose pipeline points at an
    undefined mixer or filter name.

    Runs on every active-speaker graph this module emits, right before it is
    returned or written — the same prove-it-against-the-emitted-text shape as
    :func:`_assert_tweeter_outputs_protected`, but structural: it reasons about
    no channels and no filter parameters, only whether every
    ``Mixer.name``/``Filter.names`` entry resolves against the graph's own
    ``mixers:``/``filters:`` sections.

    Every emitter here composes its definitions, mixer and pipeline from
    independent helper calls, and nothing upstream of this gate proves the three
    agree; CamillaDSP catches a dangling reference only at LOAD time, and only
    the first one.
    """
    try:
        payload = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ActiveSpeakerConfigError(
            f"emitted active-speaker config did not parse as YAML: {e}"
        ) from e
    _assert_graph_references_closed(payload, preset)


def _assert_graph_references_closed(
    payload: Mapping[str, Any], preset: ActiveSpeakerPreset
) -> None:
    """:func:`_assert_pipeline_references_closed` over a graph already read."""
    errors = pipeline_reference_closure_errors(payload)
    if not errors:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_dangling_pipeline_reference",
        preset_id=preset.preset_id,
        detail="; ".join(errors),
    )
    raise ActiveSpeakerConfigError(
        "refusing to emit an active-speaker config whose pipeline references "
        "undefined mixer/filter name(s): " + "; ".join(errors)
    )


def _assert_program_graph_proven(
    yaml_text: str,
    preset: ActiveSpeakerPreset,
    *,
    min_corner_hz: float,
    tweeter_hp_name: str | None,
) -> None:
    """Build-and-prove the emitted program graph against graph_safety (fail-closed).

    The reference-closure gate plus the three L0 tweeter proofs, run on the
    EMITTED text — the same evidence a later readback would inspect. The program
    builder cannot return a graph whose pipeline points at an undefined
    mixer/filter name, nor one whose tweeter output is not high-pass protected
    against the declared floor AND wrapped by ``tweeter_hp_name`` + soft-clip
    limiter in one post-mixer step. That pairing is what rejects a
    pre-split per-channel high-pass, which ``output_highpass_protected`` alone
    could false-PASS on the 2-way preset (program ch1 numerically coincides with
    tweeter output 1).
    """
    _assert_pipeline_references_closed(yaml_text, preset)
    tweeter_channels = _channels_for_role(preset, "tweeter")
    if not tweeter_channels:
        return
    view = view_from_emitted_text(yaml_text)
    tweeter_set = set(tweeter_channels)
    unprotected = unprotected_tweeter_outputs(
        view, tweeter_channels=tweeter_set, min_corner_hz=min_corner_hz
    )
    highpass_ok = all(
        output_highpass_protected(
            view,
            channel=channel,
            allowed_channels=tweeter_set,
            min_corner_hz=min_corner_hz,
        )
        for channel in tweeter_channels
    )
    guard_ok = tweeter_hp_name is not None and tweeter_guard_present(
        view,
        channels=tweeter_set,
        hp_name=tweeter_hp_name,
        limiter_name=driver_limiter_name("tweeter"),
        limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
    )
    if unprotected or not highpass_ok or not guard_ok:
        log_event(
            logger,
            "active_speaker.program_emit_gate",
            level=logging.ERROR,
            result="blocked_unproven_program_graph",
            preset_id=preset.preset_id,
            unprotected=",".join(str(index + 1) for index in unprotected),
            highpass_ok=highpass_ok,
            guard_ok=guard_ok,
        )
        raise ActiveSpeakerConfigError(
            "refusing to emit a program graph whose tweeter output(s) are not "
            "provably high-pass protected and limiter-wrapped on the physical "
            "output channels"
        )
