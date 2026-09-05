# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit commissioning-safe CamillaDSP templates for active speakers.

This module is intentionally side-effect-light: it can build or write a
candidate YAML file, but it does not ask CamillaDSP to load it. Hardware
activation belongs behind later channel-identity and path-safety gates.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import yaml

from jasper.atomic_io import atomic_write_text
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_PIPE_SINK_FORMAT,
    RETIRED_ALOOP_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    DRIVER_DOMAIN_PAIR_TRIM_FILTER,
    SHELF_Q,
    SHELF_Q_EMIT_DECIMALS,
    FilterSpec,
    PeqFilter,
    ensure_volume_limit_db,
    resolve_camilla_latency_for_devices,
    resolve_enable_rate_adjust,
    total_positive_boost_db,
)
from jasper.camilla_emit import (
    CHANNEL_SELECT_MIXER,
    emit_butterworth_highpass,
    emit_channel_select_mixer,
    emit_gain_filter,
    emit_linkwitz_riley,
    emit_linkwitz_transform_biquad,
    emit_mixer,
    emit_peaking_biquad,
    fmt,
    mono_sum_sources,
)
from jasper.camilla_stereo_prefix import emit_filter_spec
from jasper.log_event import log_event
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SoundProfile

from .driver_protection import (
    format_protection_hz,
    protection_highpass_floor_satisfied,
)
from .graph_safety import (
    TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    bass_extension_block_valid,
    filter_param_matches,
    output_hard_muted_and_wired,
    output_highpass_protected,
    pipeline_reference_closure_errors,
    tweeter_guard_present,
    unprotected_tweeter_outputs,
    view_from_emitted_text,
)
from .profile import (
    ADJACENT_PAIRS_BY_WAY,
    SUB_CROSSOVER_ORDER,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    lowest_driver_role,
    required_driver_roles,
)
from .test_signal_plan import (
    declared_protection_floor_hz,
    protective_tweeter_highpass_frequency_hz,
    strictest_crossover_highpass_hz,
)

logger = logging.getLogger(__name__)

#: ``result=`` slug of the L0 emit gate's below-declared-floor refusal
#: (:func:`_assert_tweeter_crossover_honours_declared_floor`). The
#: machine-readable half of a hearing-safety refusal: the operator sentence may
#: be reworded, this may not. Sibling of the startup gate's
#: ``tweeter_crossover_below_declared_protection_floor`` blocker code.
EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR = (
    "blocked_tweeter_crossover_below_declared_floor"
)

if TYPE_CHECKING:
    from jasper.bass_extension.profile import BassExtensionProfile

    # Type-only: the runtime import stays inside the two functions that need
    # it, so a cut-only emit never pulls numpy (see branch_chain's docstring).
    from .branch_chain import CrossoverSection

# The PARKED graph's on-disk name + internal vocabulary — a generated,
# topology-derived, all-muted boot graph. See emit_active_speaker_parked_config
# for what parked means.
PARKED_CONFIG_NAME = "active_speaker_parked.yml"
PARKED_SILENCE_MIXER = "parked_silence"
# The parked graph's sink: a ``File`` playback, never a DAC — no DAC attached
# means no driver to over-drive, and it makes parking DAC-agnostic (a board with
# no active outputd lane at all can still park).
PARKED_SINK_PATH = "/dev/null"
# The `# Source:` marker the classifier keys on to recognise a parked graph.
# The emitter owns its own spelling; the runtime verifier re-declares it
# independently, exactly as ACTIVE_BASELINE_SOURCE is.
ACTIVE_PARKED_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_parked_config"
)
STARTUP_HEADROOM_DB = 40.0
COMMISSIONING_HEADROOM_DB = 0.0
STARTUP_MUTE_GAIN_DB = -120.0
STARTUP_LIMITER_CLIP_LIMIT_DB = -12.0
COMMISSIONING_FILTER_MODE = "protected_startup"
APPLIED_RESPONSE_FILTER_MODE = "applied_crossover_response"
BASELINE_HEADROOM_DB = 0.0
BASELINE_LIMITER_CLIP_LIMIT_DB = -1.0
BASS_EXTENSION_LT_FILTER = "bass_ext_lt"
BASS_EXTENSION_SUBSONIC_FILTER = "bass_ext_subsonic"
FORBIDDEN_ACTIVE_PLAYBACK_TOKENS = (
    # The RETIRED snd-aloop stereo lane by its own name: `DEFAULT_PLAYBACK_DEVICE`
    # is Ring B now (ADR-0100) and is already covered by its literal below.
    RETIRED_ALOOP_PLAYBACK_DEVICE,
    "jasper_out",
    # The full-range STEREO ring: pointing an active emitter at it would put
    # POST-crossover per-driver audio on a full-range path. The ACTIVE ring
    # (``jts_ring_active_playback``) is the legal target and is deliberately NOT
    # here — the names are chosen so this case-insensitive SUBSTRING test
    # separates them ("jts_ring_playback" is not a substring of the other).
    "jts_ring_playback",
)

# The emitters' PARAMETER default for a lab emit that names no queue. Production
# composes :func:`active_emit_devices`, which passes
# ``jasper.fanin_coupling.RING_CAMILLA_GEOMETRY`` whole. Rate adjust needs no
# default: every emitter resolves it from its sink when not told (ADR-0218).
DEFAULT_ACTIVE_QUEUELIMIT = 4

# The active-LEADER's camilla#1 program-domain bake: ONLY the program domain
# (Layer B + Layer C + program headroom) to a ``File`` sink writing the
# snapserver pipe; Layer A lives in camilla#2. The runtime verifier keys on this
# marker to recognise a DAC-less program bake, but the exemption's SAFETY keys
# on ``devices.playback.type == File``, never on this string.
ACTIVE_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)

# Driver-domain-only (active follower) emit: a follower picks ONE inter-speaker
# channel of the leader's corrected stereo program, so the valid selections are
# left / right / a clip-safe mono sum. ``stereo`` (passthrough) is out of scope
# here.
DRIVER_DOMAIN_PROGRAM_CHANNELS = ("left", "right", "mono")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")
_PROGRAM_PROTECTION_RE = re.compile(r"^as_(woofer|tweeter)_program_protection_([0-9]+)$")


def protected_neutral_program_origin(
    raw: str | Mapping[str, Any],
) -> bool | None:
    """Classify this emitter's namespace: exact, partial, or unrelated."""
    try:
        config = yaml.safe_load(raw) if isinstance(raw, str) else raw
    except yaml.YAMLError:
        return False if "program_protection_" in str(raw) else None
    if not isinstance(config, Mapping):
        return None
    filters, pipeline = config.get("filters"), config.get("pipeline")
    mixers, devices = config.get("mixers"), config.get("devices")
    owns_namespace = isinstance(filters, Mapping) and any(
        _PROGRAM_PROTECTION_RE.fullmatch(str(name)) for name in filters
    )
    if not (
        isinstance(filters, Mapping) and isinstance(pipeline, list)
        and isinstance(mixers, Mapping) and isinstance(devices, Mapping)
    ):
        return False if owns_namespace else None
    if not owns_namespace:
        return None
    capture, playback = devices.get("capture", {}), devices.get("playback", {})
    if not isinstance(capture, Mapping) or not isinstance(playback, Mapping):
        return False
    output_count = playback.get("channels")
    if not isinstance(output_count, int) or output_count < 2 or capture.get("channels") != 2:
        return False
    roles = ("woofer", "tweeter")
    protections: dict[str, list[tuple[int, str]]] = {role: [] for role in roles}
    for name in filters:
        match = _PROGRAM_PROTECTION_RE.fullmatch(str(name))
        if match:
            protections[match.group(1)].append((int(match.group(2)), str(name)))
    for items in protections.values():
        if not items or sorted(index for index, _ in items) != list(range(len(items))):
            return False
    limiters = {role: f"as_{role}_startup_limiter" for role in roles}
    mutes = [output_commission_mute_name(index) for index in range(output_count)]
    protection_names = {name for items in protections.values() for _, name in items}
    if set(filters) != {
        "active_startup_headroom", *protection_names, *limiters.values(), *mutes,
    }:
        return False
    gain = {"type": "Gain", "parameters": {"gain": 0.0, "inverted": False, "mute": False}}
    limiter = {"type": "Limiter", "parameters": {"soft_clip": True, "clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB}}
    if (filters["active_startup_headroom"] != gain
            or any(filters[name] != gain for name in mutes)
            or any(filters[name] != limiter for name in limiters.values())):
        return False
    if len(pipeline) != output_count + 4 or not all(
        isinstance(step, Mapping) for step in pipeline
    ):
        return False
    channel_lists = [pipeline[index].get("channels", ()) for index in (2, 3)]
    role_steps = [
        {"type": "Filter", "channels": channels,
         "names": [*(name for _, name in sorted(protections[role])), limiters[role]]}
        for role, channels in zip(roles, channel_lists, strict=True)
    ]
    expected = [
        {"type": "Filter", "channels": [0, 1], "names": ["active_startup_headroom"]},
        {"type": "Mixer", "name": "split_active_2way"}, *role_steps,
        *({"type": "Filter", "channels": [index], "names": [name]}
          for index, name in enumerate(mutes)),
    ]
    channel_sets = [set(channels) for channels in channel_lists]
    mixer = mixers.get("split_active_2way")
    return (
        pipeline == expected and set(mixers) == {"split_active_2way"}
        and isinstance(mixer, Mapping)
        and mixer.get("channels") == {"in": 2, "out": output_count}
        and all(channel_sets) and not channel_sets[0] & channel_sets[1]
        and set.union(*channel_sets) == set(range(output_count))
    )


def _name_token(value: str) -> str:
    token = _SAFE_NAME_RE.sub("_", value).strip("_").lower()
    return token or "unnamed"


def _yaml_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActiveSpeakerConfigError(f"{field_name} is required")
    out = value.strip()
    if any(ch in out for ch in ('"', "\n", "\r")):
        raise ActiveSpeakerConfigError(f"{field_name} contains unsafe YAML characters")
    return out


def _forbidden_playback_token(playback_device: str) -> str | None:
    lowered = playback_device.lower()
    for token in FORBIDDEN_ACTIVE_PLAYBACK_TOKENS:
        if token.lower() in lowered:
            return token
    return None


def _assert_ring_playback_width(playback_device: str, output_count: int) -> None:
    """Refuse a ring-targeted active emit whose width the ring cannot carry.

    When the sink is the ACTIVE RING the declared channel count is one of the
    ring's declaring ends and the ioplug's attach compares it against the
    on-disk header, so a width the transport cannot represent CRASHES the ring
    at attach (``RING_ATTACH_FATAL``) rather than being refused. A no-op for
    every ALSA-lane emit.
    """
    from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
    from jasper.active_speaker.runtime_contract import (
        MAX_RING_CHANNELS,
        MIN_RING_CHANNELS,
    )

    if playback_device != RING_ACTIVE_PLAYBACK_DEVICE:
        return
    if not (MIN_RING_CHANNELS <= output_count <= MAX_RING_CHANNELS):
        raise ActiveSpeakerConfigError(
            f"active-ring playback requires {MIN_RING_CHANNELS}.."
            f"{MAX_RING_CHANNELS} channels, got {output_count}: the ring layout's "
            "accept-set cannot represent this width, and the ioplug attach "
            "compares the channel count field-by-field — emitting it would crash "
            "the ring rather than refuse it"
        )


def capture_device_for_playback(playback_device: str) -> str:
    """The capture device an active emit against ``playback_device`` must declare.

    THE DEVICE AXIS ALONE, and the one owner of it: :func:`active_emit_devices`
    answers for the whole ``devices:`` block and calls this for its
    ``capture_device`` field, and the live counterpart asserts the same coupling
    on a running graph's read-back.

    The device axis is TOPOLOGY-FREE by construction, so this takes no
    ``topology`` and reads no env and no file: the ring is the only transport
    (ADR-0100), every playback device pairs with Ring A, and the answer is a
    module constant. Only the FORMAT axis needs
    :func:`~jasper.fanin_coupling.resolve_ring_wire`, which can raise.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE

    return RING_CAPTURE_DEVICE


@dataclass(frozen=True)
class ActiveEmitDevices:
    """The whole CamillaDSP ``devices:`` block an active emit against a sink needs.

    BOTH HALVES in one object, because the ring coupling is end-to-end: a graph
    naming the ring on one side and the snd-aloop tap on the other goes silent,
    and a struct carrying only the sink is one a caller can forward half of.
    Every field maps 1:1 onto an ``emit_active_speaker_baseline_config``
    parameter of the same name.

    ``chunksize``/``target_level`` are ``None`` for a sink with no opinion —
    the emitter's "resolve the env/floor value at emit time" contract.
    """

    capture_device: str
    capture_format: str
    playback_format: str
    chunksize: int | None
    target_level: int | None
    queuelimit: int
    enable_rate_adjust: bool


def active_emit_devices(
    playback_device: str, *, topology: Any = None
) -> ActiveEmitDevices:
    """The device block an active emit against ``playback_device`` needs, in ONE
    derivation.

    ONE home for "what does an emit against THIS device have to declare", so a
    caller re-pointing an active graph at the ring cannot forget half of it.
    Every non-ring device gets today's values back byte-identically.

    Ring membership is over ALL THREE ring PCMs
    (:data:`~jasper.fanin_coupling.RING_PCM_DEVICES`), not one ``==`` against the
    active ring, so this is the site that answers for a ring PCM rather than the
    site that happens to know one name. What the ring branch answers:

    - ``capture_device`` — :func:`capture_device_for_playback` (Ring A). The
      coupling is END-TO-END: under ``shm_ring`` fan-in writes Ring A and stops
      feeding the snd-aloop tap, so a graph whose sink is the ring while its
      source is still ``plug:jasper_capture`` captures a device nobody writes —
      digital silence with every daemon healthy, and a QUIET trap (the plan
      compares capture CHANNELS, 2 == 2, and the width gate only holds
      ring-NAMED lanes). Moving both halves together makes it unreachable.
    - ``capture_format`` / ``playback_format`` —
      :func:`~jasper.fanin_coupling.resolve_ring_wire`, ONE format for both
      because the three rings share one wire. Never the box's program-lane
      default, which can be ``S32_LE`` where the resolver answers narrow — a
      sheared attach waiting at the arm.
    - ``chunksize`` / ``target_level`` / ``queuelimit`` /
      ``enable_rate_adjust`` — :data:`~jasper.fanin_coupling.RING_CAMILLA_GEOMETRY`
      whole, the certified pairing passed EXPLICITLY rather than the box floor an
      ordinary stereo graph carries (ADR-0218).

    A helper rather than emitter-internal derivation: the emitters keep taking
    the values as PARAMETERS, because a lab emit setting them is legitimate.

    The CHANNEL axis is deliberately absent: the ACTIVE ring's width is
    structural (the pipeline's output count, from the same saved topology the
    resolver reads), so there is nothing for a device helper to adopt.
    ``jasper.fanin.coupling_reconcile.ring_edge_width_ready`` proves the two ends
    agree per ring at the arm.
    """
    from jasper.fanin_coupling import (
        RING_CAMILLA_GEOMETRY,
        RING_PCM_DEVICES,
        resolve_ring_wire,
    )

    if playback_device not in RING_PCM_DEVICES:
        return ActiveEmitDevices(
            capture_device=capture_device_for_playback(playback_device),
            capture_format=DEFAULT_CAPTURE_FORMAT,
            playback_format=DEFAULT_PLAYBACK_FORMAT,
            chunksize=None,
            target_level=None,
            queuelimit=DEFAULT_ACTIVE_QUEUELIMIT,
            enable_rate_adjust=resolve_enable_rate_adjust(playback_device),
        )
    wire_format = resolve_ring_wire(topology).sample_format
    return ActiveEmitDevices(
        capture_device=capture_device_for_playback(playback_device),
        capture_format=wire_format,
        playback_format=wire_format,
        **RING_CAMILLA_GEOMETRY,
    )


def _finite_float(value: float, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be numeric") from e
    if not math.isfinite(out):
        raise ActiveSpeakerConfigError(f"{field_name} must be finite")
    return out


def _assert_volume_limit(volume_limit_db: float) -> None:
    """Restate the shared 0 dB software ceiling in this module's error type."""
    try:
        ensure_volume_limit_db(volume_limit_db)
    except ValueError as e:
        raise ActiveSpeakerConfigError(str(e)) from e


def _positive_int(value: int, field_name: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as e:
        raise ActiveSpeakerConfigError(f"{field_name} must be an integer") from e
    if out <= 0:
        raise ActiveSpeakerConfigError(f"{field_name} must be positive")
    return out


def _emit_delay_filter(name: str, delay_ms: float = 0.0) -> list[str]:
    return [
        f"  {name}:",
        "    type: Delay",
        "    parameters:",
        f"      delay: {fmt(delay_ms)}",
        "      unit: ms",
    ]


def _emit_limiter_filter(
    name: str,
    *,
    clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    soft_clip: bool = True,
) -> list[str]:
    soft_clip_s = "true" if soft_clip else "false"
    return [
        f"  {name}:",
        "    type: Limiter",
        "    parameters:",
        f"      soft_clip: {soft_clip_s}",
        f"      clip_limit: {fmt(clip_limit_db)}",
    ]


def _ordered_regions(preset: ActiveSpeakerPreset) -> list[CrossoverRegion]:
    by_pair = {
        (region.lower_driver, region.upper_driver): region
        for region in preset.crossover_regions
    }
    return [by_pair[pair] for pair in ADJACENT_PAIRS_BY_WAY[preset.way_count]]


def _role_polarity(preset: ActiveSpeakerPreset) -> dict[str, bool]:
    polarity: dict[str, bool] = {}
    for region in preset.crossover_regions:
        for role, value in (
            (region.lower_driver, region.lower_polarity),
            (region.upper_driver, region.upper_polarity),
        ):
            inverted = value == "inverted"
            previous = polarity.setdefault(role, inverted)
            if previous != inverted:
                raise ActiveSpeakerConfigError(
                    f"driver {role} has inconsistent polarity across crossover regions"
                )
    for role in required_driver_roles(preset.way_count):
        polarity.setdefault(role, False)
    return polarity


# Public spelling; `_role_polarity` survives only for two importers outside
# this PR's ratified file set (retiring it is a follow-up).
role_polarity = _role_polarity


def _channels_for_role(preset: ActiveSpeakerPreset, role: str) -> list[int]:
    return sorted(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == role
    )


def _bass_extension_emission(
    preset: ActiveSpeakerPreset,
    profile: BassExtensionProfile | None,
) -> dict[str, Any] | None:
    """Return the already-evaluated sealed natural block, or no block."""

    if profile is None or profile.status != "accepted":
        return None
    adapter_id = str(profile.enclosure["adapter_id"])
    if adapter_id != "sealed_v1":
        return None
    if any(target.subsonic is None for target in profile.targets):
        raise ActiveSpeakerConfigError(
            "sealed bass-extension profile requires subsonic protection on every target"
        )
    natural = profile.targets[-1]
    if natural.target_id != "natural" or natural.qp is None:
        raise ActiveSpeakerConfigError("sealed bass-extension natural target is invalid")
    owner = profile.bass_owner
    roles = tuple(str(role) for role in owner["roles"])
    channels = tuple(int(channel) for channel in owner["channels"])
    kind = str(owner["kind"])
    if kind == "woofer_way" and len(roles) == 1:
        expected = tuple(_channels_for_role(preset, roles[0]))
    elif kind == "local_sub" and roles == ("subwoofer",):
        sub = preset.local_subwoofer
        expected = () if sub is None else (sub.physical_output_index,)
    else:
        expected = ()
    if not expected or channels != expected:
        raise ActiveSpeakerConfigError(
            "bass-extension owner does not match the emitted active-speaker graph"
        )
    subsonic = dict(natural.subsonic or {})
    if (
        subsonic.get("type") != "ButterworthHighpass"
        or type(subsonic.get("order")) is not int
    ):
        raise ActiveSpeakerConfigError("bass-extension subsonic filter is unsupported")
    return {
        "kind": kind,
        "roles": roles,
        "channels": channels,
        "natural": natural,
        "subsonic": subsonic,
    }


def _bass_extension_profile_summary(
    block: dict[str, Any] | None,
) -> dict[str, Any]:
    if block is None:
        return {"runtime_block_required": False}
    natural = block["natural"]
    return {
        "runtime_block_required": True,
        "bass_owner_channels": list(block["channels"]),
        "natural": {
            "fp_hz": natural.fp_hz,
            "qp": natural.qp,
            "boost_headroom_db": natural.boost_headroom_db,
            "subsonic": dict(block["subsonic"]),
        },
    }


def _emit_bass_extension_definitions(block: dict[str, Any] | None) -> list[str]:
    if block is None:
        return []
    natural = block["natural"]
    subsonic = block["subsonic"]
    return [
        *emit_linkwitz_transform_biquad(
            BASS_EXTENSION_LT_FILTER,
            freq_act=natural.fp_hz,
            q_act=natural.qp,
            freq_target=natural.fp_hz,
            q_target=natural.qp,
        ),
        *emit_butterworth_highpass(
            BASS_EXTENSION_SUBSONIC_FILTER,
            freq=float(subsonic["freq"]),
            order=subsonic["order"],
        ),
    ]


def _bass_extension_chain_names(
    block: dict[str, Any] | None,
    *,
    role: str | None = None,
    local_sub: bool = False,
) -> list[str]:
    if block is None:
        return []
    owns = (
        block["kind"] == "local_sub"
        if local_sub
        else block["kind"] == "woofer_way" and role in block["roles"]
    )
    return (
        [BASS_EXTENSION_LT_FILTER, BASS_EXTENSION_SUBSONIC_FILTER]
        if owns
        else []
    )


def _assert_bass_extension_safe(
    yaml_text: str,
    preset: ActiveSpeakerPreset,
    block: dict[str, Any] | None,
) -> None:
    view = view_from_emitted_text(yaml_text)
    evidence = bass_extension_block_valid(
        view, _bass_extension_profile_summary(block)
    )
    limiter_ok = True
    if block is not None:
        channels = frozenset(block["channels"])
        if block["kind"] == "local_sub":
            limiter_name = _sub_baseline_limiter_name()
        else:
            limiter_name = _driver_baseline_limiter_name(block["roles"][0])
        limiter_ok = filter_param_matches(
            view,
            limiter_name,
            filter_type="Limiter",
            params={
                "clip_limit": BASELINE_LIMITER_CLIP_LIMIT_DB,
                "soft_clip": True,
            },
        )
        owner_steps = [step for step in view.pipeline_steps if step.channels == channels]
        limiter_ok = limiter_ok and len(owner_steps) == 1
        if limiter_ok:
            names = owner_steps[0].names
            required = (
                BASS_EXTENSION_LT_FILTER,
                BASS_EXTENSION_SUBSONIC_FILTER,
                limiter_name,
            )
            limiter_ok = all(name in names for name in required)
            if limiter_ok:
                limiter_ok = (
                    names.index(BASS_EXTENSION_LT_FILTER)
                    < names.index(BASS_EXTENSION_SUBSONIC_FILTER)
                    < names.index(limiter_name)
                )
    if evidence.valid and limiter_ok:
        return
    log_event(
        logger,
        "active_speaker.emit_gate",
        level=logging.ERROR,
        result="blocked_bass_extension",
        preset_id=preset.preset_id,
        reason=evidence.reason or "baseline_limiter_invalid",
    )
    raise ActiveSpeakerConfigError(
        "emitted bass-extension block failed independent safety proof"
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


def _assert_tweeter_outputs_protected(yaml_text: str, preset: ActiveSpeakerPreset) -> None:
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
    tweeter_channels = _channels_for_role(preset, "tweeter")
    if not tweeter_channels:
        return
    view = view_from_emitted_text(yaml_text)
    unprotected = unprotected_tweeter_outputs(
        view, tweeter_channels=set(tweeter_channels)
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


def _mixer_sources(
    side: str,
    layout: str,
    *,
    inverted: bool,
) -> list[tuple[int, float, bool]]:
    if layout == "stereo":
        if side == "left":
            return [(0, 0.0, inverted)]
        if side == "right":
            return [(1, 0.0, inverted)]
        raise ActiveSpeakerConfigError(f"unsupported stereo side {side!r}")
    if layout == "mono":
        # A mono cabinet sums L+R to each driver via the shared clip-safe recipe
        # (the same one the inter-speaker channel-select uses); ``inverted``
        # carries this driver's polarity.
        return mono_sum_sources(inverted=inverted)
    raise ActiveSpeakerConfigError(f"unsupported layout {layout!r}")


def _emit_split_mixer(
    preset: ActiveSpeakerPreset,
    *,
    apply_region_polarity: bool = True,
) -> str:
    # Always run the cross-region polarity reduction — it is also the
    # consistency guard (a role inverted in one region but not another raises).
    # Only its RESULT is optionally suppressed: the baseline/driver-domain
    # emitters carry polarity through ``corrections`` instead, so the mixer must
    # stay a no-op inverter there or the two would cancel out.
    region_polarity = _role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    # The (dest -> L/R-sum sources) map comes from the preset's driver layout
    # plus per-driver polarity; the YAML spelling is the shared emit_mixer.
    mapping: list[tuple[int, list[tuple[int, float, bool]]]] = [
        (
            output.index,
            _mixer_sources(
                output.side,
                preset.channel_map.layout,
                inverted=polarity[output.driver_role],
            ),
        )
        for output in outputs
    ]
    labels = [output.label for output in outputs]
    sub = preset.local_subwoofer
    if sub is not None:
        # The local subwoofer taps the SAME full-range program as the mains,
        # mono-summed with the clip-safe -6.02 dB recipe. Its band-limiting
        # low-pass and excursion limiter live in the per-output pipeline chain.
        mapping.append((sub.physical_output_index, mono_sum_sources(inverted=False)))
        labels.append(sub.label)
    return emit_mixer(
        f"split_active_{preset.way_count}way",
        channels_in=2,
        channels_out=output_count,
        mapping=mapping,
        description=(
            f"{preset.channel_map.layout} source -> "
            f"{output_count} protected active outputs"
        ),
        labels=labels,
    )


def _crossover_filter_name(
    role: str,
    region: CrossoverRegion,
    *,
    highpass: bool,
) -> str:
    suffix = "hp" if highpass else "lp"
    return f"as_{_name_token(role)}_{_name_token(region.id)}_{suffix}"


def _driver_delay_name(role: str) -> str:
    return f"as_{_name_token(role)}_delay"


def _driver_mute_name(role: str) -> str:
    return f"as_{_name_token(role)}_startup_mute"


def _driver_limiter_name(role: str) -> str:
    return f"as_{_name_token(role)}_startup_limiter"


def _driver_baseline_gain_name(role: str) -> str:
    return f"as_{_name_token(role)}_baseline_gain"


def _driver_baseline_limiter_name(role: str) -> str:
    return f"as_{_name_token(role)}_baseline_limiter"


def _room_peq_name(index: int) -> str:
    return f"room_peq_{index}"


def _protective_tweeter_hp_name(role: str) -> str:
    return f"as_{_name_token(role)}_protective_hp"


def _program_protection_name(role: str, index: int) -> str:
    return f"as_{_name_token(role)}_program_protection_{index}"


# --- local-subwoofer + bass-management filter names ---------------------------
# The sub output carries an LR4 low-pass (band-limit) + non-positive baseline
# gain + soft-clip limiter (excursion); the mains' lowest driver carries the
# complementary LR4 high-pass (bass management).


def _sub_lowpass_name() -> str:
    return "as_sub_lowpass"


def _sub_baseline_gain_name() -> str:
    return "as_sub_baseline_gain"


def _sub_baseline_limiter_name() -> str:
    return "as_sub_baseline_limiter"


def _sub_startup_mute_name() -> str:
    return "as_sub_startup_mute"


def _sub_startup_limiter_name() -> str:
    return "as_sub_startup_limiter"


def _bass_management_hp_name(role: str) -> str:
    """The complementary mains bass-management high-pass on the lowest driver."""
    return f"as_{_name_token(role)}_bass_mgmt_hp"


# --- public filter-name vocabulary -------------------------------------------
# The emitter owns the spelling of every filter name it writes; the verification
# side imports THESE aliases rather than a literal, so a rename cannot silently
# desync a safety verifier from the graph it inspects.
driver_mute_name = _driver_mute_name
driver_limiter_name = _driver_limiter_name
driver_delay_name = _driver_delay_name
driver_baseline_gain_name = _driver_baseline_gain_name
driver_baseline_limiter_name = _driver_baseline_limiter_name
protective_tweeter_hp_name = _protective_tweeter_hp_name


def crossover_highpass_for_role(
    preset: ActiveSpeakerPreset, role: str
) -> tuple[str, float, int] | None:
    """Return the applied crossover high-pass protecting ``role``."""

    for region in _ordered_regions(preset):
        if region.upper_driver == role:
            return (
                _crossover_filter_name(role, region, highpass=True),
                region.fc_hz,
                region.order,
            )
    return None


# Local-sub + bass-management aliases (same emitter-owned-spelling contract).
sub_lowpass_name = _sub_lowpass_name
sub_baseline_gain_name = _sub_baseline_gain_name
sub_baseline_limiter_name = _sub_baseline_limiter_name
sub_startup_mute_name = _sub_startup_mute_name
sub_startup_limiter_name = _sub_startup_limiter_name
bass_management_hp_name = _bass_management_hp_name

# The inter-speaker channel-select mixer name, owned by the shared leaf
# (jasper.camilla_emit) and re-exported so the active-speaker verifier has one
# import point.
channel_select_mixer_name = CHANNEL_SELECT_MIXER


def _protective_tweeter_hp_frequency(
    preset: ActiveSpeakerPreset,
    role: str,
) -> float | None:
    return protective_tweeter_highpass_frequency_hz(preset, role)


def _driver_filter_chain(preset: ActiveSpeakerPreset, role: str) -> list[str]:
    names: list[str] = []
    if _bass_management_active(preset, role):
        names.append(_bass_management_hp_name(role))
    protective_freq = _protective_tweeter_hp_frequency(preset, role)
    if protective_freq is not None:
        names.append(_protective_tweeter_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    names.append(_driver_delay_name(role))
    names.append(_driver_mute_name(role))
    names.append(_driver_limiter_name(role))
    return names


def _bass_management_active(preset: ActiveSpeakerPreset, role: str) -> bool:
    """True iff ``role`` is the lowest driver AND a local sub is present — the
    side whose lowest driver carries the complementary bass-management high-pass."""
    return (
        preset.local_subwoofer is not None
        and role == lowest_driver_role(preset.way_count)
    )


def _driver_baseline_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
    bass_extension: dict[str, Any] | None = None,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    names: list[str] = []
    # Bass-management high-pass FIRST: the lowest driver's program is
    # high-passed at the sub crossover corner before its own chain. The sub
    # low-pass at the same corner is the complementary lower half.
    if _bass_management_active(preset, role):
        names.append(_bass_management_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    # Layer-1a driver linearization: immediately after the crossover HP/LP,
    # before bass-extension. Empty linearization is a no-op.
    names.extend(
        _driver_linearization_chain_names(linearization or {}, role)
    )
    names.extend(_bass_extension_chain_names(bass_extension, role=role))
    names.append(_driver_delay_name(role))
    names.append(_driver_baseline_gain_name(role))
    names.append(_driver_baseline_limiter_name(role))
    return names


def _sub_baseline_filter_chain(
    bass_extension: dict[str, Any] | None = None,
) -> list[str]:
    """The local-sub baseline lane: band-limit (LR4 low-pass), then the same
    per-driver protection a main gets (non-positive gain + soft-clip limiter)."""
    return [
        _sub_lowpass_name(),
        *_bass_extension_chain_names(bass_extension, local_sub=True),
        _sub_baseline_gain_name(),
        _sub_baseline_limiter_name(),
    ]


def _emit_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    lines.extend(_emit_bass_management_hp_definition(preset))
    for role in required_driver_roles(preset.way_count):
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                _protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        lines.extend(_emit_delay_filter(_driver_delay_name(role)))
        lines.extend(emit_gain_filter(
            _driver_mute_name(role),
            STARTUP_MUTE_GAIN_DB,
            mute=True,
        ))
        lines.extend(_emit_limiter_filter(
            _driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_startup_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    return "\n".join(lines)


def _correction_value(
    corrections: dict[str, dict[str, float | bool]],
    role: str,
    field: str,
    default: float,
) -> float:
    value = corrections.get(role, {}).get(field)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _correction_bool(
    corrections: dict[str, dict[str, float | bool]],
    role: str,
    field: str,
) -> bool:
    return bool(corrections.get(role, {}).get(field))


def _validated_driver_corrections(
    preset: ActiveSpeakerPreset,
    corrections: dict[str, dict[str, float | bool]] | None,
) -> dict[str, dict[str, float | bool]]:
    """Normalize the final per-driver correction gate shared by both emitters."""

    safe_corrections: dict[str, dict[str, float | bool]] = {}
    for role, values in (corrections or {}).items():
        if role not in required_driver_roles(preset.way_count):
            continue
        if not isinstance(values, dict):
            continue
        gain_db = _correction_value({role: values}, role, "gain_db", 0.0)
        delay_ms = _correction_value({role: values}, role, "delay_ms", 0.0)
        if gain_db > 0:
            raise ActiveSpeakerConfigError(
                f"baseline correction gain for {role} must not be positive"
            )
        if delay_ms < 0 or delay_ms > 20:
            raise ActiveSpeakerConfigError(
                f"baseline delay for {role} must be between 0 and 20 ms"
            )
        safe_corrections[role] = {
            "gain_db": gain_db,
            "delay_ms": delay_ms,
            "inverted": bool(values.get("inverted")),
        }
    return safe_corrections


# --- Layer-1a driver-linearization emission ----------------------------------
#
# Reduced shape only: {role: [{biquad_type, freq, q, gain}, ...]}. The richer
# LinearizationFit.to_dict() is candidate/profile evidence, not emitter input;
# linearization_fit.linearization_filters_by_role() reduces it before any caller
# reaches this module.

# Hard cap on filters per driver (shelf + peaking combined). LOCKSTEP DUPLICATE
# of linearization_fit.MAX_FILTERS_PER_DRIVER — deliberately not imported,
# because the emitter independently re-validates whatever a persisted candidate
# claims rather than inheriting the fit engine's policy. A pinning test asserts
# the two stay numerically equal.
MAX_LINEARIZATION_FILTERS_PER_DRIVER = 8

# Per-filter linearization BOOST ceiling — the lockstep duplicate of
# ``linearization_fit.PER_FILTER_BOOST_CAP_DB``, held here for the reason the
# filter count above is.
#
# It bounds ONE emitted biquad, not the correction. Total boost is uncapped and
# is made safe by ``linearization_headroom_db`` below, which folds the worst
# branch chain's realized peak into ``active_baseline_headroom`` so the boosted
# band lands at or under unity however deep the correction is.
MAX_LINEARIZATION_BOOST_DB = 12.0

# Ceiling on the program-domain attenuation ``active_baseline_headroom`` may
# carry, dB. NOT a cap on the correction — a refusal.
#
# The absorption mechanism turns every dB of uncapped boost into a dB of
# pre-split attenuation, so left unbounded eight filters at the per-filter cap
# would charge 96 dB and emit a graph that is, to a household, simply mute with
# nothing naming why. 40 dB is the same bound
# ``emit_active_speaker_baseline_config`` validates ``baseline_headroom_db``
# against, and far past any correction the fit's realization gates can produce.
MAX_PROGRAM_HEADROOM_DB = 40.0

_LINEARIZATION_BIQUAD_TYPES = frozenset({"Peaking", "Highshelf", "Lowshelf"})

# Public alias: a reader outside this module needs the same set to decide
# whether a persisted linearization record is one this system wrote.
LINEARIZATION_BIQUAD_TYPES = _LINEARIZATION_BIQUAD_TYPES

# A linearization shelf carries NO steepness of its own. Every shelf reaches
# CamillaDSP through ``emit_filter_spec``, which spells the one Butterworth
# ``camilla_config_contract.SHELF_Q`` — the same Q the fit engine designed the
# shelf at and scored its residual with. Both shelf types share it.
#
# CamillaDSP's Butterworth is ``slope: 12`` (S = slope/12, S = 1); at
# ``slope: 6`` the realized Q falls with the shelf's gain (0.476 at -11 dB,
# missing the designed curve by up to 1.7 dB across the tweeter band). See
# ``SHELF_Q`` for the formula and the upstream test that pins it.


def _driver_linearization_shelf_name(role: str) -> str:
    return f"as_{_name_token(role)}_linearization_shelf"


def _driver_linearization_peak_name(role: str, index: int) -> str:
    return f"as_{_name_token(role)}_linearization_peak_{index}"


def _driver_linearization_taper_name(role: str) -> str:
    # The CD-horn stage's optional TRAILING Highshelf taper. A distinct name
    # from the leading shelf so a Lowshelf-led backbone and its taper can
    # coexist in one chain without a duplicate filter name.
    return f"as_{_name_token(role)}_linearization_taper"


def _linearization_slot(
    index: int, count: int, filters: Sequence[Mapping[str, Any]],
) -> str:
    """Classify one filter's role in a linearization chain by POSITION:
    ``"shelf"`` (a leading Highshelf/Lowshelf at index 0), ``"taper"`` (a
    trailing Highshelf after a Lowshelf lead), else ``"peak"``.

    The single source of the shelf-first / taper-last rule, shared by the
    validation gate, the chain namer and the definition emitter. It classifies
    whatever order the input carries; enforcing that order is
    ``_validate_linearization_shelf_structure``'s job.
    """
    biquad_type = filters[index]["biquad_type"]
    leading_is_lowshelf = count > 0 and filters[0]["biquad_type"] == "Lowshelf"
    if index == 0 and biquad_type in ("Highshelf", "Lowshelf"):
        return "shelf"
    if (
        index == count - 1
        and index != 0
        and biquad_type == "Highshelf"
        and leading_is_lowshelf
    ):
        return "taper"
    return "peak"


# Public aliases: the runtime-safety verifier re-proves the linearization stage
# against the EMITTED graph text and must spell these names identically.
driver_linearization_shelf_name = _driver_linearization_shelf_name
driver_linearization_peak_name = _driver_linearization_peak_name
driver_linearization_taper_name = _driver_linearization_taper_name

# Public alias: a gate outside this module that admits a shelf must answer
# "would the emitter accept this list" before it does, so the per-driver
# prescription door reads this and refuses at intake rather than at emission.
linearization_slot = _linearization_slot


def _validated_biquad_entry(
    entry: Any,
    *,
    label: str,
    allowed_types: frozenset[str],
    max_gain_db: float,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> dict[str, Any]:
    """Re-validate ONE persisted biquad record, or raise.

    Shared by every emitter gate that accepts a caller-supplied biquad list;
    each list crosses a JSON round trip before it reaches this module, so this
    is a never-trust-the-caller re-validation. Each caller keeps its own POLICY
    (permitted types, gain ceiling, entry count, order); this owns the
    per-entry field contract, and RAISES rather than clamping — a value out of
    range means the record was not written by the code that claims to own it.

    ``label`` names the owner in the message. The Nyquist refusal
    (``freq >= sample_rate / 2``) is this module's own proof: a biquad at or
    above Nyquist is not a realizable digital filter corner at all and
    CamillaDSP refuses it at ``--check``, so admitting one would stage a config
    guaranteed to fail load.
    """

    if not isinstance(entry, Mapping):
        raise ActiveSpeakerConfigError(f"{label} filter must be a mapping")
    biquad_type = entry.get("biquad_type")
    if biquad_type not in allowed_types:
        raise ActiveSpeakerConfigError(
            f"{label} biquad_type must be one of "
            f"{sorted(allowed_types)}, not {biquad_type!r}"
        )
    freq = _finite_float(entry.get("freq"), f"{label} freq")
    q = _finite_float(entry.get("q"), f"{label} q")
    gain = _finite_float(entry.get("gain"), f"{label} gain")
    if freq <= 0:
        raise ActiveSpeakerConfigError(f"{label} freq must be positive")
    nyquist_hz = sample_rate / 2.0
    if freq >= nyquist_hz:
        raise ActiveSpeakerConfigError(
            f"{label} freq must be below Nyquist ({nyquist_hz} Hz at "
            f"{sample_rate} Hz sample rate)"
        )
    if q <= 0:
        raise ActiveSpeakerConfigError(f"{label} q must be positive")
    if gain > max_gain_db:
        raise ActiveSpeakerConfigError(
            f"{label} gain must not exceed {max_gain_db} dB"
        )
    return {"biquad_type": biquad_type, "freq": freq, "q": q, "gain": gain}


def _validated_linearization(
    preset: ActiveSpeakerPreset,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Normalize + independently re-validate the per-driver linearization list.

    An independent fail-closed gate, not a trust-the-caller pass-through: an
    unknown role is dropped, a known role's list is validated field-by-field and
    RAISES on the first violation. The per-filter boost cap it re-proves is a
    REALIZATION-FIDELITY bound, not a hearing/SPL clamp — past it the emitted
    filter stops being a faithful realization of the requested shape, and the
    SPL budget is charged by headroom accounting instead.
    """

    safe: dict[str, list[dict[str, Any]]] = {}
    for role, filters in (linearization or {}).items():
        if role not in required_driver_roles(preset.way_count):
            continue
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            raise ActiveSpeakerConfigError(
                f"linearization filters for {role} must be a list"
            )
        if len(filters) > MAX_LINEARIZATION_FILTERS_PER_DRIVER:
            raise ActiveSpeakerConfigError(
                f"linearization filter count for {role} exceeds "
                f"{MAX_LINEARIZATION_FILTERS_PER_DRIVER}"
            )
        role_filters: list[dict[str, Any]] = []
        for entry in filters:
            role_filters.append(_validated_biquad_entry(
                entry,
                label=f"linearization {role}",
                allowed_types=_LINEARIZATION_BIQUAD_TYPES,
                max_gain_db=MAX_LINEARIZATION_BOOST_DB,
            ))
        _validate_linearization_shelf_structure(role, role_filters)
        if role_filters:
            safe[role] = role_filters
    return safe


# Ceiling on how many blend-correction cuts a candidate may carry, held here for
# the reason ``MAX_LINEARIZATION_FILTERS_PER_DRIVER`` is. A pinning test asserts
# it stays numerically equal to the solver's own constant.
MAX_BLEND_CORRECTION_FILTERS = 2

# The blend correction is CUTS-ONLY. Two independent places hold that: the
# solver cannot represent a boost, and this gate REFUSES one — between them sits
# a JSON round trip through a persisted candidate, which is where a value the
# solver never produced could appear. A refusal rather than a clamp, because a
# positive gain means the record was not written by its claimed owner.
MAX_BLEND_CORRECTION_GAIN_DB = 0.0

_BLEND_CORRECTION_BIQUAD_TYPES = frozenset({"Peaking"})


def _blend_correction_name(index: int) -> str:
    return f"as_blend_{index}"


# Public alias, matching the linearization name helpers above: the runtime
# safety verifier re-proves this stage against the EMITTED graph text and must
# spell the names identically rather than re-deriving the format.
blend_correction_name = _blend_correction_name


def _validated_blend_correction(
    blend_correction: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Normalize + independently re-validate the pre-split blend correction.

    The crossover blend region's bounded shape correction, solved from the
    summed at-the-mark measurement and emitted on the stereo program bus before
    the split mixer. Per-entry fields go through ``_validated_biquad_entry``;
    the policy this gate adds is stage-specific — Peaking only (a shelf across a
    two-octave blend would re-level the region, which is the trim's job), at
    most :data:`MAX_BLEND_CORRECTION_FILTERS` entries, and every ``gain`` at or
    below :data:`MAX_BLEND_CORRECTION_GAIN_DB`.

    An empty correction returns ``[]`` and emits no stage.
    """

    if blend_correction is None:
        return []
    if (
        not isinstance(blend_correction, Sequence)
        or isinstance(blend_correction, (str, bytes))
    ):
        raise ActiveSpeakerConfigError("blend correction must be a list")
    if len(blend_correction) > MAX_BLEND_CORRECTION_FILTERS:
        raise ActiveSpeakerConfigError(
            f"blend correction filter count exceeds "
            f"{MAX_BLEND_CORRECTION_FILTERS}"
        )
    return [
        _validated_biquad_entry(
            entry,
            label="blend correction",
            allowed_types=_BLEND_CORRECTION_BIQUAD_TYPES,
            max_gain_db=MAX_BLEND_CORRECTION_GAIN_DB,
        )
        for entry in blend_correction
    ]


def _validate_linearization_shelf_structure(
    role: str, role_filters: list[dict[str, Any]],
) -> None:
    """Fail-closed structural gate on shelf placement.

    The fit engine emits shelves in one of two shapes only: a single LEADING
    shelf at position 0, and — only after a Lowshelf lead — a single TRAILING
    Highshelf taper as the last entry. Any other placement means the persisted
    candidate was corrupted or produced by something else, so it raises rather
    than letting a duplicate filter name reach the graph. Peaking is always fine.
    """
    shelf_types = {"Highshelf", "Lowshelf"}
    n = len(role_filters)
    for i, entry in enumerate(role_filters):
        biquad_type = entry["biquad_type"]
        # A shelf-type entry is legal only where it occupies a shelf/taper slot;
        # one that classifies as a "peak" slot (a shelf mid-chain, a second
        # shelf, a taper without a Lowshelf lead, a taper not last) is invalid.
        if (
            biquad_type in shelf_types
            and _linearization_slot(i, n, role_filters) == "peak"
        ):
            raise ActiveSpeakerConfigError(
                f"linearization shelf placement for {role} is invalid: a "
                f"{biquad_type} may only appear as the leading filter, or (a "
                f"Highshelf taper) as the trailing filter after a Lowshelf lead"
            )


def _driver_linearization_chain_names(
    linearization: dict[str, list[dict[str, Any]]],
    role: str,
) -> list[str]:
    """The filter-name list for ``role``'s linearization stage, IN INPUT ORDER.

    Names whatever order the input carries and never reorders it; the
    shelf-first / taper-last order is the fit engine's construction guarantee,
    re-validated at the emitter boundary by
    ``_validate_linearization_shelf_structure``."""

    filters = linearization.get(role) or []
    names: list[str] = []
    peak_index = 0
    count = len(filters)
    for i in range(count):
        slot = _linearization_slot(i, count, filters)
        if slot == "shelf":
            names.append(_driver_linearization_shelf_name(role))
        elif slot == "taper":
            names.append(_driver_linearization_taper_name(role))
        else:
            peak_index += 1
            names.append(_driver_linearization_peak_name(role, peak_index))
    return names


def _emit_driver_linearization_definitions(
    linearization: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Definitions for every role's linearization filters, via the shared
    ``emit_filter_spec`` leaf.

    Shelf-type entries carry NO steepness on their ``FilterSpec``:
    ``emit_filter_spec`` spells every shelf at the one Butterworth ``SHELF_Q``
    the fit engine designed it at, and the entry's own ``q`` is dropped for the
    same reason — honouring a stray value would emit a shelf no evaluator in the
    fit loop can see. Position-aware naming mirrors
    ``_driver_linearization_chain_names`` so definitions and pipeline names
    cannot disagree.
    """

    lines: list[str] = []
    for role, filters in linearization.items():
        peak_index = 0
        count = len(filters)
        for i, entry in enumerate(filters):
            slot = _linearization_slot(i, count, filters)
            if slot == "shelf":
                spec = FilterSpec(
                    name=_driver_linearization_shelf_name(role),
                    biquad_type=entry["biquad_type"],
                    freq=entry["freq"],
                    gain=entry["gain"],
                )
            elif slot == "taper":
                spec = FilterSpec(
                    name=_driver_linearization_taper_name(role),
                    biquad_type="Highshelf",
                    freq=entry["freq"],
                    gain=entry["gain"],
                )
            else:
                peak_index += 1
                spec = FilterSpec(
                    name=_driver_linearization_peak_name(role, peak_index),
                    biquad_type="Peaking",
                    freq=entry["freq"],
                    gain=entry["gain"],
                    q=entry["q"],
                )
            lines.extend(emit_filter_spec(spec))
    return lines


def _emit_baseline_driver_definitions(
    preset: ActiveSpeakerPreset,
    *,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    bass_extension: dict[str, Any] | None = None,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """The driver-domain (Layer A) filter definitions shared by the solo/leader
    baseline and the follower's driver-domain-only graph.

    The per-region Linkwitz-Riley crossover pair, then each driver's [delay,
    non-positive baseline gain, soft-clip limiter] chain. The *intra-speaker*
    half only — no program-domain headroom, no preference EQ — so the follower's
    relocated Layer A is byte-for-byte the chain a solo speaker runs.
    ``linearization`` is threaded only by the solo/leader baseline caller.
    """
    lines: list[str] = []
    for region in _ordered_regions(preset):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    # Layer-1a driver linearization: immediately after the crossover HP/LP
    # definitions, before bass-management/bass-extension. Empty emits nothing.
    lines.extend(_emit_driver_linearization_definitions(linearization or {}))
    # Bass-management high-pass on the lowest driver (the complementary upper half
    # of the single sub crossover). Emitted only when a local sub is present.
    sub = preset.local_subwoofer
    lines.extend(_emit_bass_management_hp_definition(preset))
    lines.extend(_emit_bass_extension_definitions(bass_extension))
    for role in required_driver_roles(preset.way_count):
        delay_ms = _correction_value(corrections, role, "delay_ms", 0.0)
        gain_db = _correction_value(corrections, role, "gain_db", 0.0)
        inverted = _correction_bool(corrections, role, "inverted")
        lines.extend(_emit_delay_filter(_driver_delay_name(role), delay_ms=delay_ms))
        lines.extend(emit_gain_filter(
            _driver_baseline_gain_name(role),
            gain_db,
            inverted=inverted,
        ))
        lines.extend(_emit_limiter_filter(
            _driver_baseline_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    # The local-sub lane definitions: LR4 low-pass (band-limit) + non-positive
    # baseline gain + soft-clip limiter (excursion), same protection a main gets.
    if sub is not None:
        lines.extend(_emit_sub_baseline_definitions(
            sub.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    return lines


def _emit_bass_management_hp_definition(preset: ActiveSpeakerPreset) -> list[str]:
    """The LR4 bass-management high-pass filter def on the lowest driver, or [].

    The complementary upper half of the single sub crossover at the sub corner.
    Shared by every emitter (startup/commissioning/baseline) so the HP corner +
    order have ONE definition that cannot drift between them."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    return emit_linkwitz_riley(
        _bass_management_hp_name(lowest_driver_role(preset.way_count)),
        highpass=True,
        freq_hz=sub.crossover_fc_hz,
        order=SUB_CROSSOVER_ORDER,
    )


def _emit_sub_startup_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub startup/commissioning lane definitions: LR4 low-pass +
    soft-clip limiter + hard mute.

    The sub starts muted for commissioning safety; the band-limit and excursion
    limiter are still present so an un-muting path arms a protected output."""
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            _sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
        *emit_gain_filter(_sub_startup_mute_name(), STARTUP_MUTE_GAIN_DB, mute=True),
    ]


def _sub_startup_filter_chain() -> list[str]:
    """The local-sub startup lane: band-limit, limiter, then the hard mute."""
    return [
        _sub_lowpass_name(),
        _sub_startup_limiter_name(),
        _sub_startup_mute_name(),
    ]


def _emit_sub_commissioning_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub commissioning lane definitions: LR4 low-pass + soft-clip
    limiter only.

    The lane's own startup mute is dropped (the per-output commission mute does
    the muting), so no orphan mute filter is emitted; the band-limit and
    excursion limiter stay so the output is protected when the mute is lifted."""
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *_emit_limiter_filter(
            _sub_startup_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def _sub_commissioning_filter_chain() -> list[str]:
    """The local-sub commissioning lane: band-limit + excursion limiter only.

    The per-output commission mute replaces the lane's own startup mute, so
    exactly one physical output is excited through the real graph; the low-pass
    and limiter stay so the output is protected when that mute is lifted."""
    return [
        _sub_lowpass_name(),
        _sub_startup_limiter_name(),
    ]


def _emit_sub_baseline_definitions(
    crossover_fc_hz: float,
    *,
    limiter_clip_limit_db: float,
) -> list[str]:
    """The local-sub baseline filter definitions: LR4 low-pass + gain + limiter.

    The durable graph's sub protection is this ``gain <= 0`` + soft-clip
    limiter, band-limited by the LR4 low-pass at the bass-management corner. The
    commissioning-tone bounds (50 Hz floor / 300 ms) live in
    ``driver_protection.driver_protection_profile('subwoofer')`` instead.
    """
    return [
        *emit_linkwitz_riley(
            _sub_lowpass_name(),
            highpass=False,
            freq_hz=crossover_fc_hz,
            order=SUB_CROSSOVER_ORDER,
        ),
        *emit_gain_filter(_sub_baseline_gain_name(), 0.0),
        *_emit_limiter_filter(
            _sub_baseline_limiter_name(),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ),
    ]


def linearization_headroom_db(
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    branch_context: Mapping[str, tuple[Sequence["CrossoverSection"], float]],
) -> float:
    """Program-domain attenuation the emitted linearization boost needs, dB.

    The WORST branch's REALIZED PEAK — the largest gain any one branch chain
    (``crossover ⊗ linearization ⊗ trim``) applies to the program, plus
    :data:`jasper.active_speaker.branch_chain.HEADROOM_MARGIN_DB`. Worst branch
    rather than the sum across branches because the driver chains run in
    PARALLEL after the split, so no sample path ever sees two branches' boosts.
    A per-branch SUM of positive gains is a valid but badly loose bound: it once
    charged 22.458 dB against a branch peaking at +4.00 dB, leaving the speaker
    8.3 dB below the household's listening level at maximum volume.

    ``branch_context`` maps role to ``(crossover_sections, trim_db)`` and is
    REQUIRED: omitting it would charge the linearization cascade alone — safe
    for a charge, but wrong for any reader comparing two corrections, and wrong
    in the loud direction for a delta. :func:`_branch_context` builds it from
    the same preset and corrections the graph is emitted from.

    Public because the runtime contract's prover must agree with the emitter
    about this number and the candidate payload discloses it. The evaluation
    lives in :mod:`jasper.active_speaker.branch_chain` — one implementation.
    0.0 for a cut-only linearization.
    """
    # A branch with no positive gain cannot reach unity through a crossover and
    # a non-positive trim, so a cut-only graph is charged 0.0 without evaluating
    # anything — and without importing numpy, kept lazy on a 1 GB Pi.
    if not linearization_has_boost(linearization):
        return 0.0
    from .branch_chain import branch_headroom_db

    worst = 0.0
    for role, filters in (linearization or {}).items():
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            continue
        sections, trim_db = branch_context.get(str(role), ((), 0.0))
        worst = max(worst, branch_headroom_db(
            [entry for entry in filters if isinstance(entry, Mapping)],
            sections=sections,
            trim_db=float(trim_db),
        ))
    return worst


def linearization_has_boost(
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> bool:
    """Does any emitted linearization filter carry positive gain?

    The guard that keeps a cut-only graph off the chain-evaluation path
    entirely, so neither this emitter nor the runtime contract imports numpy for
    it. Sound because a cut cascade, a Linkwitz-Riley section and a non-positive
    trim are each <= 0 dB everywhere.

    Public because the adoption table asks the same question of the APPLIED
    candidate (a boosted intervention whose measured benefit is indeterminate
    fails closed): "does this graph put energy in" has one definition here.
    """
    for filters in (linearization or {}).values():
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            continue
        for entry in filters:
            if not isinstance(entry, Mapping):
                continue
            gain = entry.get("gain")
            if isinstance(gain, (int, float)) and not isinstance(gain, bool) and gain > 0.0:
                return True
    return False


def _branch_context(
    preset: ActiveSpeakerPreset,
    corrections: Mapping[str, Mapping[str, float | bool]],
) -> dict[str, tuple[tuple[CrossoverSection, ...], float]]:
    """Per-role ``(crossover sections, trim_db)`` for the headroom charge.

    Built from the same two sources the graph itself is — the preset's crossover
    regions and ``corrections``' per-driver ``gain_db`` — so the chain this
    charge is computed over IS the chain the next few lines emit. The role ->
    sections half is :func:`jasper.active_speaker.branch_chain.sections_by_role`,
    shared with the session that stamps the disclosed ``headroom_cost_db``.

    Deliberately omits the bass-management and protective tweeter high-passes,
    which attenuate further still: crediting less attenuation over-charges
    rather than under-charges, and keeps this identical to what the runtime
    contract can re-derive without walking optional filters.
    """
    from .branch_chain import sections_by_role

    return {
        role: (
            role_sections,
            float(_correction_value(corrections, role, "gain_db", 0.0)),
        )
        for role, role_sections in sections_by_role(
            _ordered_regions(preset)
        ).items()
    }


def _emit_baseline_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    baseline_headroom_db: float,
    limiter_clip_limit_db: float,
    corrections: dict[str, dict[str, float | bool]],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    bass_extension: dict[str, Any] | None = None,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
    blend_correction: Sequence[Mapping[str, Any]] = (),
) -> str:
    lines: list[str] = []
    room_peqs = tuple(room_peqs)
    for i, peq in enumerate(room_peqs, start=1):
        lines.extend(
            emit_peaking_biquad(
                _room_peq_name(i),
                freq=peq.freq,
                q=peq.q,
                gain=peq.gain,
            )
        )
    # Crossover blend correction — the same Peaking primitive the room PEQs use,
    # wired beside them pre-split.
    #
    # It charges NO headroom because it CANNOT BOOST
    # (``_validated_blend_correction`` refuses a positive gain), not because the
    # common attenuation covers it: a boost posture would have to ADD a term to
    # ``total_headroom_db``, since position above the gain is necessary for
    # absorption and not sufficient for it.
    for i, entry in enumerate(blend_correction, start=1):
        lines.extend(
            emit_peaking_biquad(
                _blend_correction_name(i),
                freq=float(entry["freq"]),
                q=float(entry["q"]),
                gain=float(entry["gain"]),
            )
        )
    # The active graph's single place for explicit common attenuation: baseline
    # headroom, room-correction boost headroom, the Layer-1a linearization
    # boost, plus the household's manual headroom / loudness-match
    # output_trim_db. Preference boosts themselves ride at unity, matching the
    # stereo /sound policy; room-correction and linearization boosts can raise a
    # band above unity, so their worst case is folded in here instead. It rides
    # the PRE-SPLIT gain because every branch sees the same program, so absorbing
    # the worst branch's total covers all of them — the mechanism that lets the
    # fit engine's boost stay uncapped while the 0 dB ceiling stays a hard rail.
    # The linearization term is the SAME quantity the fit discloses as
    # ``LinearizationFit.headroom_cost_db``.
    #
    # A NUMBER, never a gate: `active_baseline_headroom` is always emitted, so
    # folding the trim into its value keeps a flat-window crossing a parameter
    # write rather than stepping the gain by the whole trim, un-ducked, the
    # moment a band crosses ±0.05 dB. Matches the stereo path's `sound_preamp`.
    trim_db = max(0.0, output_trim_db)
    total_headroom_db = (
        baseline_headroom_db
        + total_positive_boost_db(room_peqs)
        + linearization_headroom_db(
            linearization,
            # Built only when there is a boost to charge for: the context
            # itself imports branch_chain, and with it numpy, which the
            # cut-only path must not pay for (see linearization_has_boost).
            branch_context=(
                _branch_context(preset, corrections)
                if linearization_has_boost(linearization)
                else {}
            ),
        )
        + trim_db
    )
    if total_headroom_db > MAX_PROGRAM_HEADROOM_DB:
        raise ActiveSpeakerConfigError(
            f"program-domain headroom {total_headroom_db:.3f} dB exceeds "
            f"{MAX_PROGRAM_HEADROOM_DB} dB — refusing to emit a graph this "
            "attenuated (check the linearization boost and room-correction "
            "boost totals)"
        )
    headroom_gain_db = 0.0 if total_headroom_db == 0 else -total_headroom_db
    lines.extend(
        emit_gain_filter(
            "active_baseline_headroom",
            headroom_gain_db,
        )
    )
    lines.extend(_emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=corrections,
        bass_extension=bass_extension,
        linearization=linearization,
    ))
    # Program-domain preference EQ (Layer C) definitions, via the shared
    # emit_filter_spec leaf so the active and stereo paths spell a preference
    # band identically. Wired pre-split — see _emit_baseline_pipeline.
    for spec in preference_filters:
        lines.extend(emit_filter_spec(spec))
    return "\n".join(lines)


def _emit_pipeline(preset: ActiveSpeakerPreset) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_startup_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(_driver_filter_chain(preset, role))
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    sub = preset.local_subwoofer
    if sub is not None:
        chain = ", ".join(_sub_startup_filter_chain())
        lines.extend([
            "  - type: Filter",
            f"    channels: [{sub.physical_output_index}]",
            f"    names: [{chain}]",
        ])
    return "\n".join(lines)


def _emit_baseline_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    room_peq_names: Sequence[str] = (),
    preference_filter_names: Sequence[str] = (),
    bass_extension: dict[str, Any] | None = None,
    linearization: dict[str, list[dict[str, Any]]] | None = None,
    blend_correction_names: Sequence[str] = (),
) -> str:
    lines: list[str] = []
    # Room PEQs (Layer B) run on the stereo program bus before the common
    # active_baseline_headroom gain. The gain absorbs their positive-boost
    # headroom so the active path stays one-preamp-shaped.
    if room_peq_names:
        names = ", ".join(room_peq_names)
        lines.extend([
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{names}]",
        ])
    # Crossover blend correction — pre-split, and only pre-split, for three
    # properties:
    #
    #  1. ONE summed fact, ONE filter. The correction describes the SUM;
    #     per-role emission would be N copies whose only defence is a test.
    #  2. Common-mode by construction. The same B(f) on every role gives
    #     Σ_r sign_r·B·C_r·D_r = B · Σ_r sign_r·C_r·D_r — the sum scales, the
    #     inter-driver complex ratio is untouched. Asymmetry (which would be
    #     ALIGNMENT work) is unrepresentable here rather than merely tested for.
    #  3. Upstream of protection. In the durable baseline the tweeter's
    #     crossover high-pass IS its protection; a pre-split filter cannot push
    #     energy past it.
    #
    # BEFORE active_baseline_headroom so the stage sits where a boost WOULD be
    # absorbable — necessary but not sufficient, since absorption needs a TERM in
    # ``total_headroom_db`` and this stage deliberately has none.
    if blend_correction_names:
        names = ", ".join(blend_correction_names)
        lines.extend([
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{names}]",
        ])
    lines.extend([
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_baseline_headroom]",
    ])
    # Preference EQ (Layer C) is a PROGRAM-domain transform: it rides the stereo
    # bus on channels [0, 1] strictly BEFORE the split mixer, upstream of every
    # per-driver crossover, limiter and tweeter high-pass. That placement is what
    # makes a preference boost safe — it can neither move a crossover corner nor
    # bypass a driver limiter.
    if preference_filter_names:
        names = ", ".join(preference_filter_names)
        lines.extend([
            "  - type: Filter",
            "    channels: [0, 1]",
            f"    names: [{names}]",
        ])
    lines.extend([
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ])
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _driver_baseline_filter_chain(
                preset, role, bass_extension, linearization,
            )
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset, bass_extension))
    return "\n".join(lines)


def _sub_baseline_pipeline_lines(
    preset: ActiveSpeakerPreset,
    bass_extension: dict[str, Any] | None = None,
) -> list[str]:
    """The sub's baseline pipeline Filter step (its own output channel), or []."""
    sub = preset.local_subwoofer
    if sub is None:
        return []
    chain = ", ".join(_sub_baseline_filter_chain(bass_extension))
    return [
        "  - type: Filter",
        f"    channels: [{sub.physical_output_index}]",
        f"    names: [{chain}]",
    ]


def _emit_driver_domain_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    pair_trim_db: float = 0.0,
    bass_extension: dict[str, Any] | None = None,
) -> str:
    # Driver-domain-only (follower) pipeline, in order: the inter-speaker
    # channel-select (a 2->2 Mixer picking L/R/mono from the leader's corrected
    # program), the optional pair-balance trim, the intra-speaker 2->N split,
    # then each driver's crossover/delay/gain/limiter chain. One helper owns
    # this ordering so the trimmed and untrimmed cases cannot fork.
    lines = [
        "  - type: Mixer",
        f"    name: {CHANNEL_SELECT_MIXER}",
    ]
    lines.extend([
        "  - type: Filter",
        "    channels: [0, 1]",
        f"    names: [{DRIVER_DOMAIN_PAIR_TRIM_FILTER}]",
    ])
    lines.extend([
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ])
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _driver_baseline_filter_chain(preset, role)
            if bass_extension is None
            else _driver_baseline_filter_chain(preset, role, bass_extension)
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    lines.extend(_sub_baseline_pipeline_lines(preset, bass_extension))
    return "\n".join(lines)


def _output_count(preset: ActiveSpeakerPreset) -> int:
    indexes = [output.index for output in preset.channel_map.outputs]
    if preset.local_subwoofer is not None:
        indexes.append(preset.local_subwoofer.physical_output_index)
    return max(indexes) + 1


def emit_active_speaker_startup_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int = DEFAULT_ACTIVE_QUEUELIMIT,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Build a muted/protected active-speaker startup template.

    A candidate for later validation; this does not load or reload CamillaDSP.
    The caller must name an explicit active-hardware playback device so the
    stereo outputd lane is never used by accident.
    """

    preset.validate()
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    # G7 latency knobs; see resolve_camilla_latency_for_devices for why the
    # emitted devices decide the fallback.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    startup_headroom_db = _finite_float(startup_headroom_db, "startup_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    _assert_volume_limit(volume_limit_db)
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    # queuelimit reaches the YAML through an f-string, so an unvalidated value
    # is the one emitter input that can put arbitrary text into a CamillaDSP
    # field; coerce it like every other integer knob here.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_yaml = _emit_filter_definitions(
        preset,
        startup_headroom_db=startup_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_pipeline(preset)
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker startup config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config
{metadata_yaml}
# DO NOT HAND-EDIT or load automatically. This template is for hardware
# bring-up only: all per-driver outputs start muted, tweeter paths include
# an extra protective high-pass, and the software volume ceiling remains
# non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): a startup graph still wires the crossover /
    # protective high-pass on the tweeter channel even though it starts muted, so
    # re-prove that protection before the config can leave the emitter.
    _assert_tweeter_outputs_protected(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_startup_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
        )
    return yaml


def output_commission_mute_name(index: int) -> str:
    """The per-physical-output commission-mute filter name for ``index``.

    Public because the protected-staging software guard references these by
    index to prove a driver's output is muted: the emitter owns the spelling.
    """
    return f"as_out{index}_commission_mute"


def emit_active_speaker_parked_config(
    *,
    output_count: int,
    topology_id: str | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
) -> str:
    """Build the PARKED (all-muted, DAC-less) active-speaker graph.

    The third statefile-seeding outcome, for a box whose saved topology declares
    roleful outputs but has not yet staged an all-muted startup graph: a flat
    full-range graph would put program into a declared tweeter, and refusing
    failed the whole deploy. This graph is silent twice over:

    * **The sink is a ``File``, not a DAC** (:data:`PARKED_SINK_PATH`) — no DAC
      attached, so no driver can be over-driven regardless of the saved
      topology, and parking works on a board with no active outputd lane at all.
      Its ``format`` is ALWAYS ``DEFAULT_PIPE_SINK_FORMAT``; ``playback_format``
      is not applicable to a ``/dev/null`` File sink and passing one EXPLICITLY
      is refused rather than ignored (a presence check on the caller-supplied
      argument, never a comparison against the default).
    * **Every physical output is hard muted** by the repo's one mute idiom — a
      ``Gain`` at :data:`STARTUP_MUTE_GAIN_DB` with ``mute: true`` — and the
      mixer feeds every destination at that same -120 dB floor, so even a
      defeated boolean mute is inaudible.

    It claims NOTHING else: no crossover, no driver roles, no per-driver
    protection, no limiter policy. ``runtime_contract._parked_graph_allowed``
    re-proves both properties before this graph may be selected.
    """

    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    # A bare ``is not None`` presence check on the CALLER-SUPPLIED argument,
    # never a value comparison against DEFAULT_PLAYBACK_FORMAT: a module global
    # read fresh at call time would diverge from this parameter's def-time-bound
    # default the moment the two constants stop being assigned in lockstep. The
    # only production caller never passes playback_format — the box must always
    # be able to park — and the emitted format is always the pinned constant.
    if playback_format is not None:
        raise ActiveSpeakerConfigError(
            "the parked graph's /dev/null File sink is pinned to "
            f"DEFAULT_PIPE_SINK_FORMAT={DEFAULT_PIPE_SINK_FORMAT!r} — "
            "playback_format is not applicable to this sink at all; passing "
            f"one explicitly (got {playback_format!r}) is a caller bug, not "
            "a wire-format request; they are different axes"
        )
    sample_rate = _positive_int(sample_rate, "sample_rate")
    output_count = _positive_int(output_count, "output_count")
    # playback_device=None because this sink is a clockless /dev/null File: it
    # declares no ALSA buffer, so the CAPTURE end is what the floor must fit
    # through. See resolve_camilla_latency_for_devices.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=None,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    _assert_volume_limit(volume_limit_db)

    filter_lines: list[str] = []
    pipeline_lines = [
        "  - type: Mixer",
        f"    name: {PARKED_SILENCE_MIXER}",
    ]
    for index in range(output_count):
        mute_name = output_commission_mute_name(index)
        filter_lines.extend(
            emit_gain_filter(mute_name, STARTUP_MUTE_GAIN_DB, mute=True)
        )
        pipeline_lines.extend([
            "  - type: Filter",
            f"    channels: [{index}]",
            f"    names: [{mute_name}]",
        ])
    filter_yaml = "\n".join(filter_lines)
    pipeline_yaml = "\n".join(pipeline_lines)
    mixer_yaml = emit_mixer(
        PARKED_SILENCE_MIXER,
        channels_in=2,
        channels_out=output_count,
        mapping=[
            # Capture channel 0 only, at the mute floor. The mapping exists to
            # change the channel count, not to carry program: nothing audible
            # may ever reach a declared driver from a parked graph.
            (index, [(0, STARTUP_MUTE_GAIN_DB, False)])
            for index in range(output_count)
        ],
        description="parked: every output muted, no crossover claimed",
    )
    metadata_comments = []
    if topology_id:
        metadata_comments.append(
            f"# topology_id={_yaml_string(topology_id, 'topology_id')}"
        )
    metadata_yaml = "\n".join(metadata_comments)

    yaml = f"""---
# Auto-generated active-speaker PARKED config.
# Source: {ACTIVE_PARKED_SOURCE}
{metadata_yaml}
# DO NOT HAND-EDIT. The saved output topology declares roleful/protected
# outputs but no all-muted active startup graph has been staged yet, so this
# graph parks every physical output hard-muted behind a File sink. It claims no
# crossover and no driver protection; it exists so the speaker holds SILENCE
# instead of running an illegal full-range graph. Finish crossover preview to
# stage a startup graph, or reset output setup and choose an explicit passive
# layout.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: 4
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: false
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: File
    channels: {output_count}
    filename: "{PARKED_SINK_PATH}"
    format: {DEFAULT_PIPE_SINK_FORMAT}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # Fail-closed emit gate: re-prove against the EMITTED TEXT (not the
    # emitter's construction) that every physical output is hard-muted and that
    # mute is wired to its channel.
    _assert_parked_outputs_muted(yaml, output_count)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
    return yaml


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


def audible_outputs_for_role(preset: ActiveSpeakerPreset, role: str) -> frozenset[int]:
    """All physical output indices carrying ``role`` (both sides for stereo).

    A convenience for callers isolating a whole role (e.g. a mono test, or the
    summed check). Single-output isolation is just ``{index}``.
    """
    return frozenset(
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == role
    )


def _commissioning_driver_filter_chain(
    preset: ActiveSpeakerPreset,
    role: str,
    *,
    filter_mode: str,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delay_roles: frozenset[str] = frozenset(),
) -> list[str]:
    """The startup chain minus the per-role mute.

    Commissioning isolates one *physical output* (not a whole role — a stereo
    woofer pair shares a role), so the role-level startup mute is dropped and a
    per-output mute layer is applied in the pipeline instead. Bring-up retains
    the dedicated tweeter high-pass; automatic response measurement removes only
    that extra filter so it measures the applied crossover shoulder.

    ``measurement_delay_roles`` names the roles that carry a ``Delay`` at the
    head of the chain. Position is free (a pure delay is LTI and commutes with
    every stage here); the applied chains place theirs after the crossover.
    """
    if protection_sections_by_role is not None:
        return [
            *([_driver_delay_name(role)] if role in measurement_delay_roles else []),
            *(
                _program_protection_name(role, index)
                for index, _section in enumerate(protection_sections_by_role[role])
            ),
            _driver_limiter_name(role),
        ]
    excluded = {_driver_mute_name(role)}
    if filter_mode == APPLIED_RESPONSE_FILTER_MODE:
        excluded.add(_protective_tweeter_hp_name(role))
    return [name for name in _driver_filter_chain(preset, role) if name not in excluded]


def _emit_commissioning_filter_definitions(
    preset: ActiveSpeakerPreset,
    *,
    startup_headroom_db: float,
    limiter_clip_limit_db: float,
    audible_outputs: frozenset[int],
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delays_us: Mapping[str, float] | None = None,
) -> str:
    lines: list[str] = []
    lines.extend(emit_gain_filter("active_startup_headroom", -startup_headroom_db))
    # The delay lane: definitions only for the roles the caller named.
    #
    # ONE `fmt` pass over the raw microsecond value and no intermediate
    # rounding — `_emit_delay_filter` formats through `jasper.camilla_emit.fmt`,
    # which IS `delay_graph.quantized_delay_ms`'s implementation, so a proof
    # recomputing from the same `delay_us` matches by construction.
    delays = dict(measurement_delays_us or {})
    if delays:
        if protection_sections_by_role is None:
            # The unprotected shape already defines a zero Delay filter per
            # role, so a named delay would emit a duplicate mapping key whose
            # later zero wins on parse — a capture that plays undelayed and
            # banks as a delayed take.
            raise ActiveSpeakerConfigError(
                "a measurement delay needs the protected-neutral program shape; "
                "the unprotected shape carries its own zeroed delay lane"
            )
        known = set(required_driver_roles(preset.way_count))
        unknown = sorted(set(delays) - known)
        if unknown:
            # An unreferenced Delay filter would leave the capture undelayed
            # while its graph fingerprint claimed otherwise.
            raise ActiveSpeakerConfigError(
                f"measurement delays name roles this preset has no branch for: "
                f"{unknown}"
            )
        for role, delay_us in sorted(delays.items()):
            value = float(delay_us)
            if not math.isfinite(value):
                # A non-finite value emits `delay: .nan`, which parses back as a
                # float and would read as a bound question rather than a
                # nonsense one. The RANGE is `_assert_measurement_delays_bound`'s.
                raise ActiveSpeakerConfigError(
                    f"measurement delay for {role!r} is not finite: {delay_us!r}"
                )
            lines.extend(_emit_delay_filter(
                _driver_delay_name(role), delay_ms=value / 1000.0,
            ))
    for region in (() if protection_sections_by_role is not None else _ordered_regions(preset)):
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.lower_driver, region, highpass=False),
            highpass=False,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
        lines.extend(emit_linkwitz_riley(
            _crossover_filter_name(region.upper_driver, region, highpass=True),
            highpass=True,
            freq_hz=region.fc_hz,
            order=region.order,
        ))
    # The bass-management HP is referenced by the lowest driver's commissioning
    # chain (it preserves the running graph's protection), so its definition must
    # be present here too.
    if protection_sections_by_role is None:
        lines.extend(_emit_bass_management_hp_definition(preset))
    for role in required_driver_roles(preset.way_count):
        for index, section in enumerate((protection_sections_by_role or {}).get(role, ())):
            lines.extend(emit_linkwitz_riley(
                _program_protection_name(role, index),
                highpass=section.highpass,
                freq_hz=section.fc_hz,
                order=section.order,
            ))
        protective_freq = _protective_tweeter_hp_frequency(preset, role)
        if filter_mode == COMMISSIONING_FILTER_MODE and protective_freq is not None:
            lines.extend(emit_linkwitz_riley(
                _protective_tweeter_hp_name(role),
                highpass=True,
                freq_hz=protective_freq,
                order=4,
            ))
        if protection_sections_by_role is None:
            lines.extend(_emit_delay_filter(_driver_delay_name(role)))
        lines.extend(_emit_limiter_filter(
            _driver_limiter_name(role),
            clip_limit_db=limiter_clip_limit_db,
            soft_clip=True,
        ))
    # The local-sub lane definitions (LR4 low-pass + soft-clip limiter): the sub
    # output is band-limited AND excursion-limited even in the commissioning
    # graph. Its muting is the per-output commission mask below.
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_commissioning_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    # Per-output commissioning mute: only audible outputs pass, so exactly one
    # physical driver is excited through the real graph; the empty default is
    # fully muted. An audible output carries ``audible_gain_db``, which defaults
    # to the silent mute floor, so an un-ramped commission load arms the target
    # at {gain: -120, mute: off}. Muted outputs stay at -120 dB regardless.
    for index in range(_output_count(preset)):
        is_audible = index in audible_outputs
        lines.extend(emit_gain_filter(
            output_commission_mute_name(index),
            audible_gain_db if is_audible else STARTUP_MUTE_GAIN_DB,
            mute=not is_audible,
        ))
    return "\n".join(lines)


def _emit_commissioning_pipeline(
    preset: ActiveSpeakerPreset,
    *,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    measurement_delay_roles: frozenset[str] = frozenset(),
) -> str:
    lines = [
        "  - type: Filter",
        "    channels: [0, 1]",
        "    names: [active_startup_headroom]",
        "  - type: Mixer",
        f"    name: split_active_{preset.way_count}way",
    ]
    for role in required_driver_roles(preset.way_count):
        channels = _channels_for_role(preset, role)
        chain = ", ".join(
            _commissioning_driver_filter_chain(
                preset,
                role,
                filter_mode=filter_mode,
                protection_sections_by_role=protection_sections_by_role,
                measurement_delay_roles=measurement_delay_roles,
            )
        )
        lines.extend([
            "  - type: Filter",
            f"    channels: [{', '.join(str(ch) for ch in channels)}]",
            f"    names: [{chain}]",
        ])
    # The local sub's protective lane (band-limit + excursion limiter) on its own
    # output channel, BEFORE the per-output commission mute below — so the sub is
    # protected exactly like a driver when its mute is later lifted to ramp it.
    sub = preset.local_subwoofer
    if sub is not None:
        chain = ", ".join(_sub_commissioning_filter_chain())
        lines.extend([
            "  - type: Filter",
            f"    channels: [{sub.physical_output_index}]",
            f"    names: [{chain}]",
        ])
    for index in range(_output_count(preset)):
        lines.extend([
            "  - type: Filter",
            f"    channels: [{index}]",
            f"    names: [{output_commission_mute_name(index)}]",
        ])
    return "\n".join(lines)


def emit_active_speaker_commissioning_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    audible_outputs: frozenset[int] | set[int] | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    startup_headroom_db: float = STARTUP_HEADROOM_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int = DEFAULT_ACTIVE_QUEUELIMIT,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
) -> str:
    """Build the **production** active-speaker graph with a per-output mask.

    The single-audio-path commissioning config: the same protected graph the
    speaker runs (volume ceiling 0 dB, startup headroom, protective tweeter
    high-pass, per-driver limiters) with each physical output individually
    mutable. ``audible_outputs={k}`` excites exactly one driver through its real
    crossover/limiter chain; the empty set is fully muted, the full set is every
    driver live for the summed check. Validation happens on the production path,
    so the commissioned config IS what is frozen as the durable profile.
    """

    preset.validate()
    if filter_mode not in {
        COMMISSIONING_FILTER_MODE,
        APPLIED_RESPONSE_FILTER_MODE,
    }:
        raise ActiveSpeakerConfigError(
            f"unsupported commissioning filter mode: {filter_mode!r}"
        )
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    # G7 latency knobs; see resolve_camilla_latency_for_devices for why the
    # emitted devices decide the fallback.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    startup_headroom_db = _finite_float(startup_headroom_db, "startup_headroom_db")
    limiter_clip_limit_db = _finite_float(limiter_clip_limit_db, "limiter_clip_limit_db")
    audible_gain_db = _finite_float(audible_gain_db, "audible_gain_db")
    _assert_volume_limit(volume_limit_db)
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )
    # Structural bound only: the per-output audible gain is an attenuation, so
    # it never exceeds the 0 dB ceiling nor drops below the -120 dB mute floor.
    # The tighter commissioning level envelope is the ramp gate's.
    if audible_gain_db < STARTUP_MUTE_GAIN_DB or audible_gain_db > 0:
        raise ActiveSpeakerConfigError(
            f"audible_gain_db must be between {STARTUP_MUTE_GAIN_DB:.0f} and 0 dB"
        )

    # queuelimit reaches the YAML through an f-string, so an unvalidated value
    # is the one emitter input that can put arbitrary text into a CamillaDSP
    # field; coerce it like every other integer knob here.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    audible: frozenset[int] = frozenset(audible_outputs or ())
    for index in audible:
        if not isinstance(index, int) or not 0 <= index < output_count:
            raise ActiveSpeakerConfigError(
                f"audible_outputs index {index!r} out of range for "
                f"{output_count} outputs"
            )

    filter_yaml = _emit_commissioning_filter_definitions(
        preset,
        startup_headroom_db=startup_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        audible_outputs=audible,
        audible_gain_db=audible_gain_db,
        filter_mode=filter_mode,
    )
    mixer_yaml = _emit_split_mixer(preset)
    pipeline_yaml = _emit_commissioning_pipeline(
        preset,
        filter_mode=filter_mode,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# audible_outputs={sorted(audible)}",
        f"# audible_gain_db={fmt(audible_gain_db)}",
        f"# filter_mode={filter_mode}",
    ]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker commissioning config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_commissioning_config
{metadata_yaml}
# DO NOT HAND-EDIT. Single-audio-path bring-up: the production graph with a
# per-output mute mask so one driver at a time is tested through its real
# crossover/limiter chain. Bring-up uses the extra protective high-pass;
# automatic response measurement uses the applied crossover high-pass instead.
# The software volume ceiling remains non-positive in both modes.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): every tweeter output keeps its crossover /
    # protective high-pass even while the commission mask mutes it, so a graph
    # that could later be unmuted onto a bare compression driver is refused.
    _assert_tweeter_outputs_protected(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_commissioning_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d audible=%s",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            sorted(audible),
        )
    return yaml


# --- channel-routed program graph (crossover measurement session, W2) --------
# The v2 crossover measurement flow plays ONE continuous 2-channel program WAV
# (docs/historical/crossover-measurement-productization-design.md §5.4):
# program capture ch0 carries the woofer stimulus, ch1 the tweeter stimulus,
# sequenced in the WAV so the CamillaDSP graph stays static (no reload
# mid-program). This graph
# maps each program capture channel to its driver's PHYSICAL output path.

# The slope this build COMMISSIONS a tweeter crossover high-pass at. A code
# figure, not a declaration, so it may disclose a shallower crossover and may
# prove a protective filter this build itself derived — it may NOT refuse a
# crossover order a household pinned. See
# ``_assert_tweeter_crossover_hp_satisfies_floor`` for which half of that gate
# refuses and which logs.
PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE = 24.0


def _validated_inverted_roles(
    preset: ActiveSpeakerPreset, inverted_roles: Sequence[str],
) -> frozenset[str]:
    """The reverse-null's named branches, refused unless this cabinet has them.

    Fail-closed for the same reason :func:`_validate_program_role_channels` is:
    a role no output declares would flip nothing, the graph would emit
    byte-identical to its non-inverted twin, and the banked record would claim
    a reverse-null nobody measured.
    """
    flipped = frozenset(inverted_roles)
    declared = {output.driver_role for output in preset.channel_map.outputs}
    unknown = flipped - declared
    if unknown:
        raise ActiveSpeakerConfigError(
            "cannot invert driver role(s) this preset declares no output for: "
            + ", ".join(sorted(unknown))
        )
    return flipped


def _validated_measurement_trims(
    preset: ActiveSpeakerPreset, trims_db: Mapping[str, float] | None,
) -> dict[str, float]:
    """The measurement's per-role level match, refused unless it can be honoured.

    Fail-closed for :func:`_validated_inverted_roles`'s reason: a trim naming a
    role no output declares would attenuate nothing while the banked record
    claimed a level match nobody played.

    **Attenuation only** — a positive value is refused rather than clamped,
    because this is the one seam that could raise a measurement's drive above
    the level the session was admitted at. Every hearing clamp is untouched:
    ``volume_limit``, the per-driver limiter and the tweeter protection
    high-pass are downstream of this mixer and unreachable from here.
    """
    if not trims_db:
        return {}
    declared = {output.driver_role for output in preset.channel_map.outputs}
    validated: dict[str, float] = {}
    for role, value in trims_db.items():
        if role not in declared:
            raise ActiveSpeakerConfigError(
                "cannot level-match a driver role this preset declares no "
                f"output for: {role}"
            )
        trim_db = _finite_float(value, f"measurement level trim for {role}")
        if trim_db > 0.0:
            raise ActiveSpeakerConfigError(
                "a measurement level trim is attenuation only; "
                f"{role} asked for {trim_db:g} dB"
            )
        validated[role] = trim_db
    return validated


def _emit_role_routed_mixer(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
    *,
    apply_region_polarity: bool = True,
    inverted_roles: Sequence[str] = (),
    level_trims_db: Mapping[str, float] | None = None,
) -> str:
    """Emit the program graph's role-routed split mixer.

    ``inverted_roles`` is the measurement's reverse-null flip: each named role's
    sign is reversed RELATIVE to whatever polarity this graph would otherwise
    carry, so it XORs onto the region polarity rather than replacing it. It is
    level-neutral by construction — every ``dest`` here has exactly ONE source,
    so flipping ``inverted`` negates each sample and leaves every peak the
    limiter and the volume ceiling answer for bit-identical.

    ``level_trims_db`` is the ONE thing that moves a ``gain``: each named role's
    single source is attenuated so the branches meet the crossover at comparable
    level and a reverse null can form. Attenuation only
    (:func:`_validated_measurement_trims`), so every peak can only fall.

    Unlike :func:`_emit_split_mixer` (which routes a stereo bus by output
    *side*), this routes by driver *role*: every output of role ``r`` takes its
    single source from ``role_channels[r]``. ``channels_in`` is the program
    channel count (max mapped channel + 1).

    The mixer is named ``split_active_{way_count}way`` — the SAME name
    :func:`_emit_split_mixer` uses — for two reasons landing on one spelling:
    :func:`_emit_commissioning_pipeline`, reused verbatim here, hardcodes a
    ``Mixer`` step under that name (CamillaDSP refuses a pipeline referencing an
    undefined mixer), and ``environment``'s active-config classifier recognises
    a ``split_active_Nway`` name. Ecosystem vocabulary, not a routing claim: the
    ROUTING stays role-routed.
    """
    region_polarity = _role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    flipped = _validated_inverted_roles(preset, inverted_roles)
    trims = _validated_measurement_trims(preset, level_trims_db)
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    channels_in = 1 + max(role_channels.values())
    mapping: list[tuple[int, list[tuple[int, float, bool]]]] = [
        (
            output.index,
            [(
                role_channels[output.driver_role],
                trims.get(output.driver_role, 0.0),
                polarity[output.driver_role] != (output.driver_role in flipped),
            )],
        )
        for output in outputs
    ]
    labels = [output.label for output in outputs]
    return emit_mixer(
        f"split_active_{preset.way_count}way",
        channels_in=channels_in,
        channels_out=output_count,
        mapping=mapping,
        description=(
            f"program channels -> {output_count} role-routed active outputs"
        ),
        labels=labels,
    )


def _validate_program_role_channels(
    preset: ActiveSpeakerPreset,
    role_channels: dict[str, int],
) -> dict[str, int]:
    """Fail-closed check that every output's role owns one distinct program channel."""

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
    required = set(required_driver_roles(preset.way_count))
    output_roles = {output.driver_role for output in preset.channel_map.outputs}
    missing = (output_roles | required) - set(normalized)
    if missing:
        raise ActiveSpeakerConfigError(
            "program role_channels is missing a channel for role(s) "
            + ", ".join(sorted(missing))
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


def _assert_tweeter_crossover_hp_satisfies_floor(
    preset: ActiveSpeakerPreset,
    *,
    min_corner_hz: float,
    min_slope_db_per_octave: float,
) -> None:
    """Refuse a preset whose tweeter crossover HP crosses BELOW the declared floor.

    In the program graph the tweeter is protected by its TARGET crossover
    high-pass alone (the bring-up protective HP is dropped so the measured
    branch is the applied crossover shoulder), so this build-time gate reads
    that crossover from the preset before any YAML is emitted.

    **The corner REFUSES; the slope only DISCLOSES.** ``min_corner_hz`` arrives
    as :data:`~jasper.active_speaker.graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ`
    — an absolute code floor, not this driver's declaration — and stays a
    refusal because a crossover below it puts the low-frequency excursion hazard
    band on a compression driver, a named damage mechanism
    (docs/measurement-loop-doctrine.md §5). ``min_slope_db_per_octave`` arrives
    as :data:`PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE`, a code figure no
    datasheet contains, so refusing a household's pinned order against it would
    be a nanny; the manufacturer's published condition is enforced at the pin,
    where the declaration is readable. Here there is none to read, so the
    shortfall is logged (``result=tweeter_hp_slope_below_commissioning_floor``)
    and the graph is emitted.
    """
    for role in required_driver_roles(preset.way_count):
        if role != "tweeter":
            continue
        crossover = crossover_highpass_for_role(preset, role)
        if crossover is None:
            raise ActiveSpeakerConfigError(
                "program graph requires a tweeter crossover high-pass; the "
                f"preset declares none for role {role!r}"
            )
        _name, fc_hz, order = crossover
        if fc_hz < min_corner_hz:
            log_event(
                logger,
                "active_speaker.program_emit_gate",
                level=logging.ERROR,
                result="blocked_tweeter_hp_below_floor",
                preset_id=preset.preset_id,
                fc_hz=f"{fc_hz:g}",
                min_corner_hz=f"{min_corner_hz:g}",
            )
            raise ActiveSpeakerConfigError(
                f"tweeter crossover high-pass corner {fc_hz:g} Hz is below the "
                f"declared protective floor {min_corner_hz:g} Hz"
            )
        if order * 6.0 < min_slope_db_per_octave:
            # Disclosed, never refused — see this function's docstring. WARNING
            # rather than ERROR because nothing is blocked: the corner already
            # cleared the declared floor and the manufacturer's published
            # condition, if any, was applied at the pin.
            log_event(
                logger,
                "active_speaker.program_emit_gate",
                level=logging.WARNING,
                result="tweeter_hp_slope_below_commissioning_floor",
                preset_id=preset.preset_id,
                order=order,
                slope_db_per_octave=f"{order * 6.0:g}",
                commissioning_floor_db_per_octave=f"{min_slope_db_per_octave:g}",
            )


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


def _assert_measurement_delays_bound(
    yaml_text: str,
    measurement_delays_us: Mapping[str, float] | None,
    *,
    role_channels: Mapping[str, int],
    preset: ActiveSpeakerPreset,
) -> None:
    """Prove each requested delay actually landed, through the shared proof.

    :func:`~jasper.audio_measurement.delay_graph.prove_static_delay_binding` is
    the tree's one answer to "does this graph carry that delay": the value
    through the same quantizer a later proof would use, the filter in EXACTLY
    ONE pipeline step wired to exactly the role's channels, the 20 ms DSP bound,
    and ``devices.volume_limit``. Structural, so it catches an orphan filter or
    a duplicate definition a value check alone would miss.
    """
    if not measurement_delays_us:
        return
    import yaml as yaml_lib

    from jasper.audio_measurement.delay_graph import prove_static_delay_binding
    from jasper.audio_measurement.null_walk import NullWalkError

    parsed = yaml_lib.safe_load(yaml_text)
    if not isinstance(parsed, dict):
        raise ActiveSpeakerConfigError("emitted program graph did not parse")
    for role, delay_us in sorted(measurement_delays_us.items()):
        channels = tuple(sorted(_channels_for_role(preset, role) or ()))
        if not channels:
            channels = (int(role_channels[role]),)
        try:
            prove_static_delay_binding(
                parsed,
                delay_filter_name=_driver_delay_name(role),
                channels=channels,
                delay_us=float(delay_us),
            )
        except NullWalkError as exc:
            # The proof's whole error family: `DelayGraphProofError` carries the
            # typed failure code and subclasses this.
            raise ActiveSpeakerConfigError(
                f"the emitted program graph does not carry the requested "
                f"{role!r} measurement delay: {exc}"
            ) from exc


def _assert_program_graph_proven(
    yaml_text: str,
    preset: ActiveSpeakerPreset,
    *,
    min_corner_hz: float,
    tweeter_hp_name: str | None = None,
) -> None:
    """Build-and-prove the emitted program graph against graph_safety (fail-closed).

    The reference-closure gate plus the three L0 tweeter proofs, run on the
    EMITTED text — the same evidence a later readback would inspect. The program
    builder cannot return a graph whose pipeline points at an undefined
    mixer/filter name, nor one whose tweeter output is not high-pass protected
    against the declared floor AND wrapped by its crossover high-pass +
    soft-clip limiter in one post-mixer step. That pairing is what rejects a
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
    if tweeter_hp_name is None:
        crossover = crossover_highpass_for_role(preset, "tweeter")
        tweeter_hp_name = crossover[0] if crossover is not None else None
    guard_ok = tweeter_hp_name is not None and tweeter_guard_present(
        view,
        channels=tweeter_set,
        hp_name=tweeter_hp_name,
        limiter_name=_driver_limiter_name("tweeter"),
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


def emit_active_speaker_program_config(
    preset: ActiveSpeakerPreset,
    *,
    role_channels: dict[str, int],
    playback_device: str,
    protection_sections_by_role: Mapping[str, Sequence[CrossoverSection]] | None = None,
    protective_hp_min_corner_hz: float = TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    protective_hp_min_slope_db_per_octave: float = (
        PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE
    ),
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    limiter_clip_limit_db: float = STARTUP_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int = DEFAULT_ACTIVE_QUEUELIMIT,
    enable_rate_adjust: bool | None = None,
    inverted_roles: Sequence[str] = (),
    measurement_delays_us: Mapping[str, float] | None = None,
    measurement_level_trims_db: Mapping[str, float] | None = None,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
) -> str:
    """Emit the static channel-routed program graph for CHECK/MEASURE playback.

    ``role_channels`` maps each driver role to the program-WAV channel carrying
    its stimulus (ch0 → woofer, ch1 → tweeter). The graph routes each program
    channel to that driver's PHYSICAL output path through a role-routed mixer,
    carries either the legacy target crossover or caller-supplied confirmed role
    protection plus the per-driver limiter, keeps the software volume ceiling
    non-positive, and stays static (no reload mid-program). The
    protected-neutral shape omits configured crossover, delay, linearization,
    bass, Room and preference filters.

    ``inverted_roles``, ``measurement_delays_us`` and
    ``measurement_level_trims_db`` are parameters of THIS measurement emitter
    and of nothing else: the applied and baseline emitters take their per-driver
    delay and gain from the profile's ``corrections`` and cannot reach these, so
    a swept coordinate can never leak into a graph a household plays. Empty or
    ``None`` keeps every existing program byte-identical.

    * ``inverted_roles`` is level-neutral — see :func:`_emit_role_routed_mixer`.
    * ``measurement_delays_us`` reaches the YAML through a single
      :func:`~jasper.camilla_emit.fmt` pass, the same formatter
      :func:`~jasper.audio_measurement.delay_graph.quantized_delay_ms` is
      implemented as, so a proof recomputing from the same ``delay_us`` agrees
      exactly. Delays ride ahead of the protection sections; a pure delay
      commutes, so the position changes no magnitude.
    * ``measurement_level_trims_db`` lands on ONE seam, the role-routed mixer's
      per-source gain — the per-output commissioning gain is deliberately not
      also touched, or one decision would be applied twice. Attenuation only.

    Two fail-closed gates run before the graph can leave: a build-time proof
    that the selected tweeter HP satisfies the declared floor, and
    :func:`_assert_program_graph_proven` over the emitted text.
    """

    preset.validate()
    # Scope gate: ONE program channel per declared driver role — a 1-way passive
    # main or a 2-way. A 3-way needs a designed reshape (mid-band MESM schedule,
    # per-region alignment), not a silent generalization of this emitter.
    if preset.way_count not in (1, 2):
        raise ActiveSpeakerConfigError(
            "the crossover-measurement program graph is scoped to 1- and 2-way "
            f"presets; way_count={preset.way_count} requires a designed "
            "program reshape"
        )
    role_channels = _validate_program_role_channels(preset, role_channels)
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker templates require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    limiter_clip_limit_db = _finite_float(limiter_clip_limit_db, "limiter_clip_limit_db")
    protective_hp_min_corner_hz = _finite_float(
        protective_hp_min_corner_hz, "protective_hp_min_corner_hz"
    )
    protective_hp_min_slope_db_per_octave = _finite_float(
        protective_hp_min_slope_db_per_octave,
        "protective_hp_min_slope_db_per_octave",
    )
    _assert_volume_limit(volume_limit_db)
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    tweeter_hp_name = None
    if protection_sections_by_role is None:
        _assert_tweeter_crossover_hp_satisfies_floor(
            preset,
            min_corner_hz=protective_hp_min_corner_hz,
            min_slope_db_per_octave=protective_hp_min_slope_db_per_octave,
        )
    else:
        required_roles = set(required_driver_roles(preset.way_count))
        if set(protection_sections_by_role) != required_roles:
            raise ActiveSpeakerConfigError("program protection must cover every driver role")
        # Asked of the role that DECLARES one: a 1-way main has no tweeter for
        # a high-pass to protect, so the gate is absent, not waived
        # (``_assert_program_graph_proven`` agrees from the emitted text).
        if "tweeter" in required_roles:
            tweeter_hps = [
                (index, section)
                for index, section in enumerate(protection_sections_by_role["tweeter"])
                if section.highpass
            ]
            if len(tweeter_hps) != 1:
                raise ActiveSpeakerConfigError("program graph requires one tweeter protection high-pass")
            hp_index, hp_section = tweeter_hps[0]
            if hp_section.fc_hz < protective_hp_min_corner_hz or (
                hp_section.order * 6.0 < protective_hp_min_slope_db_per_octave
            ):
                # SOLE slope-floor enforcement on this path: it reaches the
                # journal exactly as its predecessor's refusal does.
                log_event(
                    logger, "active_speaker.program_emit_gate", level=logging.ERROR,
                    result="blocked_tweeter_protection_below_floor",
                    preset_id=preset.preset_id, fc_hz=f"{hp_section.fc_hz:g}",
                    order=hp_section.order)
                raise ActiveSpeakerConfigError("tweeter protection does not satisfy the program floor")
            tweeter_hp_name = _program_protection_name("tweeter", hp_index)

    # queuelimit reaches the YAML through an f-string, so an unvalidated value
    # is the one emitter input that can put arbitrary text into a CamillaDSP
    # field; coerce it like every other integer knob here.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    program_channels = 1 + max(role_channels.values())
    # Every output is audible: a program never mutes a driver (the WAV silences
    # it by channel). Program headroom is the commissioning headroom (0 dB), so
    # the effective-peak ledger the session-volume plan and admission share is
    # main_volume + program peak with no hidden graph attenuation.
    audible = frozenset(range(output_count))
    filter_mode = (
        APPLIED_RESPONSE_FILTER_MODE
        if protection_sections_by_role is None else "protected_neutral"
    )
    filter_yaml = _emit_commissioning_filter_definitions(
        preset,
        startup_headroom_db=COMMISSIONING_HEADROOM_DB,
        limiter_clip_limit_db=limiter_clip_limit_db,
        audible_outputs=audible,
        audible_gain_db=0.0,
        filter_mode=filter_mode,
        protection_sections_by_role=protection_sections_by_role,
        measurement_delays_us=measurement_delays_us,
    )
    level_trims = _validated_measurement_trims(preset, measurement_level_trims_db)
    mixer_yaml = _emit_role_routed_mixer(
        preset, role_channels,
        apply_region_polarity=protection_sections_by_role is None,
        inverted_roles=inverted_roles,
        level_trims_db=level_trims,
    )
    pipeline_yaml = _emit_commissioning_pipeline(
        preset,
        filter_mode=filter_mode,
        protection_sections_by_role=protection_sections_by_role,
        measurement_delay_roles=frozenset(measurement_delays_us or ()),
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# role_channels={dict(sorted(role_channels.items()))}",
        f"# program_channels={program_channels}",
        f"# filter_mode={filter_mode}",
        # The graph SAYS which coordinate it carries, so a record naming it by
        # fingerprint reads without reconstructing the Delay filter body.
        # Emitted ONLY when there is one: an unconditional line would change the
        # bytes — and so the fingerprint — of every CHECK and MEASURE graph.
        *(
            [
                "# measurement_delays_us="
                + repr(dict(sorted(measurement_delays_us.items())))
            ]
            if measurement_delays_us
            else []
        ),
        # On the delay line's terms: emitted ONLY when a level match is
        # declared. The numbers come from the SAME validated mapping the mixer
        # gains did, so the graph states the trims it actually carries.
        *(
            ["# measurement_level_trims_db=" + repr(dict(sorted(level_trims.items())))]
            if level_trims
            else []
        ),
    ]
    if inverted_roles:
        # Emitted only when a branch is actually flipped, so a non-inverted emit
        # stays byte-identical; the graph then SAYS which branch carries the
        # reverse-null, beside the fingerprint a record names it by.
        metadata_comments.append(f"# inverted_roles={sorted(set(inverted_roles))}")
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker crossover-measurement program config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config
{metadata_yaml}
# DO NOT HAND-EDIT. Static channel-routed program graph: program capture channel
# c is routed to every physical output of role role_channels^-1(c), each carrying
# its declared protection filter + soft-clip limiter. Played once (no reload
# mid-program) while a 2-channel program WAV sequences the driver stimuli by
# channel. The software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: {program_channels}
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): the shared per-output tweeter-protection re-proof.
    _assert_tweeter_outputs_protected(yaml, preset)
    # Build-and-prove the program graph's return contract against graph_safety.
    _assert_program_graph_proven(
        yaml, preset, min_corner_hz=protective_hp_min_corner_hz,
        tweeter_hp_name=tweeter_hp_name,
    )
    _assert_measurement_delays_bound(
        yaml, measurement_delays_us, role_channels=role_channels, preset=preset,
    )

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_program_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d channels=%d",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            program_channels,
        )
    return yaml


def emit_active_speaker_baseline_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    baseline_headroom_db: float = BASELINE_HEADROOM_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    queuelimit: int = DEFAULT_ACTIVE_QUEUELIMIT,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    bass_extension_profile: BassExtensionProfile | None = None,
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    blend_correction: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Build an accepted active-speaker baseline candidate.

    Unlike the startup template this YAML is not muted. It still preserves the
    JTS 0 dB volume ceiling, keeps per-driver limiters, and refuses positive
    per-driver correction gain; callers own the acceptance evidence and the
    explicit CamillaDSP apply step.

    Every program-domain layer is emitted on channels [0, 1] strictly BEFORE the
    split mixer, upstream of every crossover, limiter and tweeter high-pass:

    * ``room_peqs`` (Layer B) — the preserved room-correction PEQ set; any
      positive boost is folded into ``active_baseline_headroom``.
    * ``preference_filters`` (Layer C) — the same ``FilterSpec`` objects the
      stereo emitter takes, emitted VERBATIM (dropping neutral bands is the
      caller's job, because the live editing draft needs its idle slots).
      Preference boosts ride at unity, matching ``emit_sound_config``.
    * ``output_trim_db`` — the household's manual headroom + loudness-match
      attenuation, folded into the same headroom gain, applied only when some
      band actually boosts.
    * ``blend_correction`` — the crossover blend region's summed-response-owned
      shape correction, flat rather than per-role because it describes the SUM;
      see ``_emit_baseline_pipeline`` for what that placement buys.

    ``linearization`` (Layer 1a) is the per-driver stage the fit engine designs,
    in the REDUCED shape ``{role: [{biquad_type, freq, q, gain}, ...]}``. Each
    role's filters are emitted immediately after that driver's crossover HP/LP
    and before bass-extension.

    ``linearization`` and ``blend_correction`` are both independently
    re-validated here (``_validated_linearization`` /
    ``_validated_blend_correction``) rather than trusted from the caller — the
    per-filter boost cap and shelf-placement structure for one, Peaking-only and
    NON-POSITIVE gain for the other. Every empty default keeps an existing
    caller byte-identical.
    """

    preset.validate()
    # L0 emit gate (fail-closed), BEFORE any YAML is built: this is the graph
    # the routine apply transaction ships to a household, so a crossover below
    # the tweeter's declared protection floor is refused here rather than left
    # for the startup-load gate to catch on the next boot.
    _assert_tweeter_crossover_honours_declared_floor(preset)
    linearization = linearization or {}
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker baselines require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    # G7 latency knobs; see resolve_camilla_latency_for_devices for why the
    # emitted devices decide the fallback.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    baseline_headroom_db = _finite_float(baseline_headroom_db, "baseline_headroom_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    output_trim_db = _finite_float(output_trim_db, "output_trim_db")
    _assert_volume_limit(volume_limit_db)
    if baseline_headroom_db < 0 or baseline_headroom_db > 40:
        raise ActiveSpeakerConfigError("baseline_headroom_db must be between 0 and 40")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections = _validated_driver_corrections(preset, corrections)
    bass_extension = _bass_extension_emission(preset, bass_extension_profile)
    safe_linearization = _validated_linearization(preset, linearization)
    safe_blend_correction = _validated_blend_correction(blend_correction)

    # Verbatim: every caller hands a slot per declared band with the neutral
    # ones kept, because a filter appearing or disappearing is what forces a
    # ducked pipeline replace.
    emitted_preference_filters = tuple(preference_filters)
    room_peqs = tuple(room_peqs)

    # queuelimit reaches the YAML through an f-string, so an unvalidated value
    # is the one emitter input that can put arbitrary text into a CamillaDSP
    # field; coerce it like every other integer knob here.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_yaml = _emit_baseline_filter_definitions(
        preset,
        baseline_headroom_db=baseline_headroom_db,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
        room_peqs=room_peqs,
        preference_filters=emitted_preference_filters,
        output_trim_db=output_trim_db,
        bass_extension=bass_extension,
        linearization=safe_linearization,
        blend_correction=safe_blend_correction,
    )
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections`` (a per-driver Gain filter below), so the mixer must
    # stay a no-op inverter — see the docstring on _emit_split_mixer.
    mixer_yaml = _emit_split_mixer(preset, apply_region_polarity=False)
    pipeline_yaml = _emit_baseline_pipeline(
        preset,
        room_peq_names=[_room_peq_name(i) for i in range(1, len(room_peqs) + 1)],
        preference_filter_names=[spec.name for spec in emitted_preference_filters],
        bass_extension=bass_extension,
        linearization=safe_linearization,
        blend_correction_names=[
            _blend_correction_name(i)
            for i in range(1, len(safe_blend_correction) + 1)
        ],
    )
    metadata_comments = [f"# preset_id={preset.preset_id}"]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)
    capture_yaml = f"""  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}"""

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker baseline config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config
{metadata_yaml}
# This is a candidate speaker baseline: crossover filters are active, outputs
# are not startup-muted, per-driver correction gain is non-positive, and the
# software volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
{capture_yaml}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): the durable (unmuted) baseline is the graph a
    # household plays through, so re-prove every tweeter output carries its
    # crossover / protective high-pass before it can leave the emitter.
    _assert_tweeter_outputs_protected(yaml, preset)
    _assert_bass_extension_safe(yaml, preset, bass_extension)
    # Reference-closure gate (fail-closed): the baseline assembles its
    # filters/mixer/pipeline from independent helper calls, exactly as the
    # program graph does.
    _assert_pipeline_references_closed(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        # linearization_shelves / shelf_q date, per speaker, the write after
        # which a persisted Layer-1a design realizes the Butterworth shelf Q the
        # fit designed it at rather than CamillaDSP's gain-dependent
        # ``slope: 6`` Q (0.476 at -11 dB) — an audible treble change.
        shelf_count = sum(
            1
            for filters in safe_linearization.values()
            for index in range(len(filters))
            if _linearization_slot(index, len(filters), filters) in ("shelf", "taper")
        )
        logger.info(
            "event=active_speaker_baseline_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d "
            "linearization_shelves=%d shelf_q=%.*f",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            shelf_count,
            SHELF_Q_EMIT_DECIMALS,
            SHELF_Q,
        )
    return yaml


def emit_active_speaker_driver_domain_config(
    preset: ActiveSpeakerPreset,
    *,
    playback_device: str,
    program_channel: str,
    pair_trim_db: float = 0.0,
    corrections: dict[str, dict[str, float | bool]] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str = DEFAULT_PLAYBACK_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    limiter_clip_limit_db: float = BASELINE_LIMITER_CLIP_LIMIT_DB,
    queuelimit: int = DEFAULT_ACTIVE_QUEUELIMIT,
    enable_rate_adjust: bool | None = None,
    out_path: str | Path | None = None,
    baseline_id: str | None = None,
    bass_extension_profile: BassExtensionProfile | None = None,
) -> str:
    """Build a **driver-domain-only** active-speaker graph for a wireless follower.

    An *endpoint-crossover* graph running only **Layer A** — the ``2->N`` split
    plus each driver's crossover / delay / non-positive gain / soft-clip
    limiter, tweeter band-limited by its crossover high-pass — on a stereo
    program the **leader already corrected**. It emits NO program-domain prefix
    (no ``active_baseline_headroom``, no preference EQ): that domain belongs to
    the leader's bake instance.

    The pipeline is ``channel_select (2->2 pick L/R/mono) -> optional
    pair_balance_trim -> split_active_<way>way (2->N) -> per-driver chain``.
    ``program_channel`` is one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``; the
    channel-select mixer is the shared ``emit_channel_select_mixer`` primitive,
    so a follower and a bonded member spell the pick identically.

    Like the baseline emitter it keeps the 0 dB volume ceiling, per-driver
    limiters and non-positive correction gain, and refuses the stereo outputd
    lane. It does NOT load or reload CamillaDSP. ``corrections`` carries the same
    commissioned per-driver delay/gain/polarity as the solo baseline, so the
    relocated Layer A is the chain the speaker runs solo.
    """

    preset.validate()
    # Same L0 emit gate as the solo baseline: a bonded member's driver domain
    # runs the identical protective chain on the identical drivers.
    _assert_tweeter_crossover_honours_declared_floor(preset)
    playback_device = _yaml_string(playback_device, "playback_device")
    forbidden_token = _forbidden_playback_token(playback_device)
    if forbidden_token:
        raise ActiveSpeakerConfigError(
            "active-speaker baselines require an explicit active playback "
            f"device, not the existing {forbidden_token} lane"
        )
    if program_channel not in DRIVER_DOMAIN_PROGRAM_CHANNELS:
        raise ActiveSpeakerConfigError(
            f"program_channel must be one of {DRIVER_DOMAIN_PROGRAM_CHANNELS}, "
            f"not {program_channel!r}"
        )
    pair_trim_db = _finite_float(pair_trim_db, "pair_trim_db")
    if pair_trim_db < 0.0 or pair_trim_db > 120.0:
        raise ActiveSpeakerConfigError("pair_trim_db must be between 0 and 120 dB")
    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    playback_format = _yaml_string(playback_format, "playback_format")
    sample_rate = _positive_int(sample_rate, "sample_rate")
    # G7 latency knobs; see resolve_camilla_latency_for_devices for why the
    # emitted devices decide the fallback.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    chunksize = _positive_int(chunksize, "chunksize")
    target_level = _positive_int(target_level, "target_level")
    volume_limit_db = _finite_float(volume_limit_db, "volume_limit_db")
    limiter_clip_limit_db = _finite_float(
        limiter_clip_limit_db,
        "limiter_clip_limit_db",
    )
    _assert_volume_limit(volume_limit_db)
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections = _validated_driver_corrections(preset, corrections)
    bass_extension = _bass_extension_emission(preset, bass_extension_profile)

    # queuelimit reaches the YAML through an f-string, so an unvalidated value
    # is the one emitter input that can put arbitrary text into a CamillaDSP
    # field; coerce it like every other integer knob here.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear here
    # rather than let the ioplug attach crash on it (see
    # _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    filter_lines = _emit_baseline_driver_definitions(
        preset,
        limiter_clip_limit_db=limiter_clip_limit_db,
        corrections=safe_corrections,
        bass_extension=bass_extension,
    )
    filter_lines.extend(
        emit_gain_filter(DRIVER_DOMAIN_PAIR_TRIM_FILTER, -pair_trim_db)
    )
    filter_yaml = "\n".join(filter_lines)
    # channel_select FIRST (inter-speaker pick), then the intra-speaker split.
    # apply_region_polarity=False: this graph carries polarity through
    # ``safe_corrections``, so the mixer must stay a no-op inverter.
    mixer_yaml = "\n".join((
        emit_channel_select_mixer(program_channel),
        _emit_split_mixer(preset, apply_region_polarity=False),
    ))
    pipeline_yaml = _emit_driver_domain_pipeline(
        preset,
        pair_trim_db=pair_trim_db,
        bass_extension=bass_extension,
    )
    metadata_comments = [
        f"# preset_id={preset.preset_id}",
        f"# program_channel={program_channel}",
        f"# pair_trim_db={pair_trim_db:.3f}",
    ]
    if baseline_id:
        baseline_id = _yaml_string(baseline_id, "baseline_id")
        metadata_comments.append(f"# baseline_id={baseline_id}")
    metadata_yaml = "\n".join(metadata_comments)

    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(playback_device)
    # CamillaDSP YAML booleans are lowercase; Python's repr is not.
    enable_rate_adjust_yaml = 'true' if enable_rate_adjust else 'false'
    yaml = f"""---
# Auto-generated active-speaker driver-domain config.
# Source: jasper.active_speaker.camilla_yaml.emit_active_speaker_driver_domain_config
{metadata_yaml}
# This is a wireless follower's driver-domain-only Layer-A graph: it picks one
# inter-speaker channel of the leader's already-corrected stereo program, then
# runs the per-driver crossover/limiter chain. There is no program-domain
# headroom or preference EQ (the leader baked Layer B/C); outputs are not
# startup-muted, per-driver correction gain is non-positive, and the software
# volume ceiling remains non-positive.

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db!r}
  enable_rate_adjust: {enable_rate_adjust_yaml}
  capture:
    type: Alsa
    channels: 2
    device: "{capture_device}"
    format: {capture_format}
  playback:
    type: Alsa
    channels: {output_count}
    device: "{playback_device}"
    format: {playback_format}

filters:
{filter_yaml}

mixers:
{mixer_yaml}

pipeline:
{pipeline_yaml}
"""

    # L0 emit gate (fail-closed): the follower runs Layer A on the leader's
    # corrected program, so its tweeter output must still carry the crossover /
    # protective high-pass.
    _assert_tweeter_outputs_protected(yaml, preset)
    _assert_bass_extension_safe(yaml, preset, bass_extension)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_driver_domain_config_written "
            "path=%s preset_id=%s way_count=%d outputs=%d program_channel=%s",
            out_path,
            preset.preset_id,
            preset.way_count,
            output_count,
            program_channel,
        )
    return yaml


# The exact ``# Source:`` line emit_sound_config stamps. We rewrite it to the
# bake's own marker, so the substitution is a 1:1 swap; assert it fired rather
# than silently shipping the wrong provenance if that emitter's header changes.
_SOUND_SOURCE_LINE = "# Source: jasper.sound.camilla_yaml.emit_sound_config"
_PROGRAM_BAKE_SOURCE_LINE = f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"


def emit_active_speaker_program_bake_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    output_trim_db: float = 0.0,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
) -> str:
    """Build the active-LEADER's **program-domain-only** camilla#1 bake.

    The **program** half of a leader's split DSP: Layer B room correction +
    Layer C preference EQ + program headroom, written to a ``File`` sink feeding
    the snapserver pipe so the followers receive a corrected stereo wire. The
    **driver** half (Layer A) lives in camilla#2 and is deliberately absent.

    A separate emit that bypasses the graph carrier, like the follower's
    :func:`emit_active_speaker_driver_domain_config`. The program assembly is
    :func:`jasper.sound.camilla_yaml.emit_sound_config`'s, reused verbatim with a
    ``File``/pipe sink, so the baked correction is byte-for-byte the program
    graph the speaker already ships; only the ``# Source:`` marker differs.

    Safety is BY CONSTRUCTION: the playback is a pipe, not a DAC, so no driver
    can be over-driven regardless of the saved topology — and the runtime
    verifier's matching exemption keys on ``devices.playback.type == File``,
    never on the marker, so an ALSA-sink program graph reaching the DAC stays
    blocked under a roleful topology.

    This does NOT load or reload CamillaDSP and does NOT wire camilla#1 into the
    reconciler. ``out_path`` writes the YAML group-readably (0640).
    """

    # Lazy: the snapserver pipe target lives in the grouping reconciler, whose
    # module-load chain this read-heavy emitter must not pull eagerly.
    from jasper.multiroom.reconcile import SNAPFIFO

    program_yaml = emit_sound_config(
        profile,
        room_peqs=room_peqs,
        capture_device=capture_device,
        capture_format=capture_format,
        sample_rate=sample_rate,
        chunksize=chunksize,
        target_level=target_level,
        volume_limit_db=volume_limit_db,
        profile_id=profile_id,
        output_trim_db=output_trim_db,
        playback_pipe_path=SNAPFIFO,
    )

    # Re-stamp provenance so the bake is distinguishable from the solo /sound +
    # correction program graphs that share emit_sound_config's assembly. Fail
    # loud if the upstream marker changes shape: a silent miss would ship a bake
    # the verifier cannot route to the flat program path.
    if _SOUND_SOURCE_LINE not in program_yaml:
        raise ActiveSpeakerConfigError(
            "program bake could not re-stamp the source marker: "
            "emit_sound_config no longer emits the expected '# Source:' line"
        )
    yaml = program_yaml.replace(_SOUND_SOURCE_LINE, _PROGRAM_BAKE_SOURCE_LINE, 1)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        logger.info(
            "event=active_speaker_program_bake_config_written path=%s pipe=%s "
            "room_peqs=%d output_trim=%.3f",
            out_path,
            SNAPFIFO,
            len(room_peqs or []),
            output_trim_db,
        )
    return yaml


def _atomic_write_text(path: Path, text: str) -> None:
    # Active-speaker configs are read by both root-owned CamillaDSP helpers and
    # the non-root jasper-web commissioning route. Keep them group-readable.
    atomic_write_text(path, text, mode=0o640)
