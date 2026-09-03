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
#: (:func:`_assert_tweeter_crossover_honours_declared_floor`). Named rather than
#: spelled inline because it is the machine-readable half of a hearing-safety
#: refusal: the operator sentence may be reworded, this may not. Sibling of the
#: startup gate's ``tweeter_crossover_below_declared_protection_floor`` blocker
#: code (``path_safety._tweeter_protection_floor_verdict``) — two layers, one
#: condition, so the two slugs are deliberately readable as a pair.
EMIT_GATE_TWEETER_CROSSOVER_BELOW_DECLARED_FLOOR = (
    "blocked_tweeter_crossover_below_declared_floor"
)

if TYPE_CHECKING:
    from jasper.bass_extension.profile import BassExtensionProfile

    # Type-only: the runtime import stays inside the two functions that need
    # it, so a cut-only emit never pulls numpy (see branch_chain's docstring).
    from .branch_chain import CrossoverSection

ACTIVE_STARTUP_CONFIG_NAME = "active_speaker_startup.yml"
# The PARKED graph's on-disk name + internal vocabulary (issue #2135). Kept next
# to the startup config's name because the two are the same class of artifact:
# a generated, topology-derived, all-muted boot graph. See
# emit_active_speaker_parked_config for what parked means and why it exists.
PARKED_CONFIG_NAME = "active_speaker_parked.yml"
PARKED_SILENCE_MIXER = "parked_silence"
# The parked graph's sink. A ``File`` playback, never a DAC — the same
# safe-by-construction argument the camilla#1 program bake rests on ("no DAC, no
# driver to over-drive", see ACTIVE_PROGRAM_BAKE_SOURCE and
# runtime_contract._playback_is_program_bake_pipe). It is also what makes the
# parked graph DAC-agnostic: a board with no active outputd lane at all (the
# InnoMaker HiFi AMP Pro) can still park, where an ALSA-sink parked graph would
# have nothing legal to open.
PARKED_SINK_PATH = "/dev/null"
# The `# Source:` marker the classifier keys on to recognise a parked graph.
# Named here (the emitter owns its own spelling) and re-declared independently
# by the runtime verifier, exactly as ACTIVE_BASELINE_SOURCE is.
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
    # The full-range STEREO ring. An active graph carries POST-crossover
    # per-driver channels; Ring B carries a full-range stereo program that
    # outputd hands to a stereo sink. Pointing an active emitter at it would put
    # per-driver audio on a full-range path (and, read the other way, would let
    # the stereo path's consumers receive a graph they cannot mean). The ACTIVE
    # ring (``jts_ring_active_playback``) is the legal ring target and is
    # deliberately NOT here — and the two names are chosen so this
    # case-insensitive SUBSTRING test separates them: "jts_ring_playback" is not
    # a substring of "jts_ring_active_playback".
    "jts_ring_playback",
)

# The emitters' PARAMETER default for a lab emit that names no queue (Q6).
# Production composes :func:`active_emit_devices`, which passes
# ``jasper.fanin_coupling.RING_CAMILLA_GEOMETRY`` — the certified queue/rate-adjust
# pairing an ACTIVE RING sink needs — whole. Rate adjust itself needs no
# default here: every emitter resolves it from its sink when not told
# (:func:`~jasper.camilla_config_contract.resolve_enable_rate_adjust`, ADR-0218).
DEFAULT_ACTIVE_QUEUELIMIT = 4

# The active-LEADER's camilla#1 program-domain bake (distributed-active Stage B).
# It emits ONLY the program domain (Layer B room correction + Layer C preference
# EQ + program headroom) to a ``File`` sink writing the snapserver pipe — NO
# Layer A (no 2->N split, no per-driver crossover/delay/gain/limiter; those live
# in camilla#2). This distinct ``# Source:`` marker is what the runtime verifier
# keys on to recognise the bake as a DAC-less program graph; the *safety* of the
# exemption keys on ``devices.playback.type == File`` (no DAC attached, so no
# driver can be over-driven), not on this string. Stamped over emit_sound_config's
# own marker so the bake stays distinguishable from the solo /sound + correction
# program graphs that share emit_sound_config's program assembly.
ACTIVE_PROGRAM_BAKE_SOURCE = (
    "jasper.active_speaker.camilla_yaml.emit_active_speaker_program_bake_config"
)

# Driver-domain-only (active follower) emit. A follower picks ONE inter-speaker
# channel of the leader's already-corrected stereo program, so the valid
# program-channel selections are left / right / a clip-safe mono sum. ``stereo``
# is passthrough (not a single-box pick) and ``sub`` is the wireless-sub member
# (gap 5) — both are out of scope for the follower driver-domain emit.
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

    The EMITTER-SIDE twin of ``_outputd_endpoint_width``'s bound. When an active
    graph's sink is the ACTIVE RING, the channel count it declares becomes one of
    the ring's declaring ends, and the ioplug's attach compares that field
    against the on-disk header. An emit whose width the transport cannot
    represent is therefore not a config that fails later — it is a config that
    CRASHES the ring at attach (``RING_ATTACH_FATAL``) rather than being refused.

    Refusing here means the shear is reported by the thing that caused it, at the
    moment it is written, instead of by a daemon that inherited it. Non-ring
    devices are untouched: this is a no-op for every ALSA-lane emit, which is
    every emit on every box today.
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

    THE DEVICE AXIS ALONE, and the one owner of it. :func:`active_emit_devices`
    answers for the whole ``devices:`` block and calls this for its
    ``capture_device`` field — one owner, two arities, so the two cannot drift
    into two lists of the same names. The second arity's caller is
    :func:`jasper.active_speaker.commissioning_admission.issue_protection_evidence`'s
    ``capture_route_current`` check, which derives what a RUNNING graph's
    capture device must be from that same graph's OWN playback device: the live
    counterpart of the end-to-end coupling :func:`active_emit_devices`
    documents, asserted on the read-back rather than on the emit.

    THE DEVICE AXIS IS TOPOLOGY-FREE BY CONSTRUCTION, which is why this
    signature takes no ``topology`` and this body reads no env and no file: the
    ring is the only transport (ADR-0100), so every playback device pairs with
    Ring A and the answer is a module constant. Only the FORMAT axis needs
    :func:`~jasper.fanin_coupling.resolve_ring_wire`, which reads the box's
    declaration and can raise ``ValueError`` — so a caller that needs the
    device name alone pays none of that, and cannot fail for a reason that is
    not about the device. Widening this helper past the device axis would need
    the topology input :func:`active_emit_devices` has and this one
    deliberately does not; a caller that needs more than the device asks that
    function instead.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE

    return RING_CAPTURE_DEVICE


@dataclass(frozen=True)
class ActiveEmitDevices:
    """The whole CamillaDSP ``devices:`` block an active emit against a sink needs.

    BOTH HALVES, in one object, because the ring coupling is end-to-end: a graph
    that names the ring on one side and the snd-aloop tap on the other is a graph
    that goes silent, and a struct carrying only the sink is a struct a caller can
    forward half of. Every field maps 1:1 onto an
    ``emit_active_speaker_baseline_config`` parameter of the same name, and
    ``tests/test_ring_active_endpoint.py`` walks
    ``dataclasses.fields(ActiveEmitDevices)`` at every forwarding site so a field
    added here cannot be silently dropped by one of them.

    ``chunksize``/``target_level`` are ``None`` for a sink with no opinion, which
    is the emitter's own "resolve the env/floor value at emit time" contract.
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
    caller that re-points an active graph at the ring cannot forget any of it —
    which is exactly the shape of forgetting that produces a graph naming the
    right device and behaving like the wrong one. Every other device (the ALSA
    active lane, and every lab/CI override) gets today's values back, so a
    caller that routes through this helper is byte-identical on every box that
    is not armed.

    RING MEMBERSHIP IS OVER ALL THREE RING PCMs
    (:data:`~jasper.fanin_coupling.RING_PCM_DEVICES`), not one ``==`` against
    the active ring. Two of the three are refused earlier by other guards — the
    stereo ring is a forbidden active playback token, and the capture ring is
    not a sink at all — so today only the active ring reaches the ring branch.
    Keying on the SET anyway is what makes this the site that answers for a ring
    PCM rather than the site that happens to know one name: a caller emitting
    any ring device reads the same resolution here instead of adding a second.

    WHAT THE RING BRANCH ANSWERS, and who owns each value:

    - ``capture_device`` — :func:`capture_device_for_playback`, which answers
      :data:`~jasper.fanin_coupling.RING_CAPTURE_DEVICE` (Ring A) here and owns
      that axis for its second caller too.
      THE COUPLING IS END-TO-END AND SO IS THIS: under ``shm_ring``
      fan-in writes Ring A and stops feeding the snd-aloop tap, so a graph whose
      sink is the ring while its source is still ``plug:jasper_capture`` captures
      a device nobody writes — digital silence with every daemon healthy. That
      trap is QUIET: the plan compares capture CHANNELS (2 == 2 passes) and the
      arm's width gate only holds ring-NAMED lanes to the wire, so a tap capture
      is not-inspected rather than refused. Moving both halves together is what
      makes it unreachable, and once the capture names a ring PCM the width gate
      holds it to the wire for free. There is no reverse: the ring is the one
      legal ACTIVE endpoint, so no derivation restores the tap.
    - ``capture_format`` / ``playback_format`` —
      :func:`~jasper.fanin_coupling.resolve_ring_wire`, the one per-box
      resolution EVERY declaring end reads. ONE format for both, because the
      three rings share one wire. Never the box's program-lane default: on a box
      whose live post-DSP path is wide that default is ``S32_LE`` while the
      resolver may answer narrow, and a graph carrying the box default is a
      sheared attach waiting at the arm (observed on jts3 2026-08-11,
      ``captures/r7b-jts3-arm2-20260811T132227Z``). This adopts whatever the
      resolver answers, in both directions.
    - ``chunksize`` / ``target_level`` / ``queuelimit`` /
      ``enable_rate_adjust`` — :data:`~jasper.fanin_coupling.RING_CAMILLA_GEOMETRY`,
      whole. This graph is built end-to-end on the ring and passes the certified
      pairing EXPLICITLY. That is not the fallback an ordinary stereo graph
      takes: those carry the box's own floor, clamped to the ring's capacity by
      ``resolve_camilla_latency_for_devices``, and read their
      ``enable_rate_adjust`` from
      :func:`~jasper.camilla_config_contract.resolve_enable_rate_adjust` — the
      sink decides it on either branch (ADR-0218).

    A helper rather than emitter-internal derivation: the emitters keep taking
    the values as PARAMETERS (Q6), because a lab emit deliberately setting them
    is a legitimate call, and burying the choice inside the emitter would remove
    that seam. This is the default a production caller composes with.

    The CHANNEL axis is deliberately not here, on either half. Ring A always
    carries the 2-channel stereo program fan-in mixes and the emitters already
    declare exactly that; the ACTIVE ring's width is structural — the pipeline's
    output count, derived from the same saved topology the resolver reads — so
    there is nothing for a device helper to "adopt". What matters is that the
    two agree per ring, and
    ``jasper.fanin.coupling_reconcile.ring_edge_width_ready`` proves that at the
    arm, over the emitted graph, holding each lane to its OWN ring's width
    (``_wire_channels_for_ring``) rather than to one number.
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

    The *bound* half of tweeter protection, where
    :func:`_assert_tweeter_outputs_protected` is the *structural* half.
    Structure answers "is there **a** high-pass on this output"; this answers
    "is its corner at or above what this driver's own declaration requires". A
    searched or prescribed crossover frequency makes those two different
    questions: a graph can carry a textbook-correct high-pass whose corner the
    driver's manufacturer forbids.

    **Both compared numbers come from their owners, never re-derived here.**
    ``test_signal_plan.strictest_crossover_highpass_hz`` is the one derivation
    of "the crossover corner this driver is protected at";
    ``test_signal_plan.declared_protection_floor_hz`` is the thin read of the
    preset-carried floor that
    ``driver_protection.declared_protection_highpass_floor_hz`` parsed at the
    single compile point (``staging._driver_spec_from_preview``); and
    ``driver_protection.protection_highpass_floor_satisfied`` is the shared
    comparison rule. So this gate cannot drift from the protective-high-pass
    clamp, the staged metadata, or the load gate — all four compare the same
    numbers with the same rule.

    **Two layers, deliberately, and they are not redundant.**
    ``path_safety._tweeter_protection_floor_verdict`` refuses the same
    condition at **startup / commission-load**, reading the two facts
    ``staging`` published onto a *staged config's metadata*. That gate can only
    ever judge a graph already written to disk, and only on the load path. This
    one runs first thing inside the household-facing emitters, before a single
    line of YAML is built — so the routine apply transaction
    (``baseline_profile.build_baseline_profile_candidate`` →
    ``apply_baseline_profile`` → ``dsp_apply.apply_dsp_config``) cannot put a
    below-floor crossover on disk in the first place. Neither subsumes the
    other: remove this and a routine apply ships the graph the load gate would
    later refuse; remove that and a config staged by any other producer loads
    unchecked.

    **Why only the household-facing emitters, and not every emitter.** The
    commissioning-flow emitters (startup / commissioning / program) must stay
    able to emit a below-floor graph, because refusing one is precisely what
    the layer above them does *with a better message*: ``staging`` emits the
    startup config, publishes both facts onto its metadata, and the load gate
    then refuses it by name with an actionable "raise the crossover or correct
    the declaration". Gating the emitter would replace that operator-facing
    refusal with a bare emit-time exception thrown from underneath the
    commissioning flow, and would leave the load gate untestable against the
    very artifact it exists to catch (issue #2491's own contract pins stage a
    below-floor draft on purpose). The scope is therefore the graphs that carry
    household program: the baseline apply and the multiroom driver-domain bake.

    **Boundary semantics are the shared predicate's, deliberately identical to
    the startup gate's:** *at* the floor is legal (``>=``, the 2026-08-17 owner
    ruling — a driver rated to cross at 5000 Hz may cross at 5000 Hz), below it
    is refused, and a declared floor with no readable crossover corner is
    refused rather than waved through. A driver that declares **no** floor is
    honoured unchanged: ``None`` means the operator declared nothing, and
    inventing a floor there is the nanny behaviour the 2026-08-14 ruling
    excludes — the same present-and-``None`` case the startup gate calls "the
    honest undeclared case".
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

    Runs on every active-speaker graph THIS module emits, right before it is
    returned or written, so an unprotected-tweeter graph can never leave the
    emitter (let alone be loaded). It re-proves — against the emitted text, not
    the emitter's own construction — that every physical output the preset
    assigns a ``tweeter`` (compression-driver) role carries a protective
    high-pass: its crossover high-pass and/or a dedicated protective high-pass.

    Structure only: this answers "is there **a** high-pass on this output", not
    "is its corner at or above what this driver declared". That bound is the
    separate concern of
    :func:`_assert_tweeter_crossover_honours_declared_floor`, which the
    household-facing emitters run *before* they build anything — see its
    docstring for why the two are deliberately not merged, and why it does not
    run on the commissioning-flow emitters this one also guards.

    This closes the L0 hearing-safety hole: a compression driver is ~25 dB more
    sensitive than the woofer, so a graph
    that routes full-range program to a tweeter output with no high-pass is a
    shrill / hot-tweeter hazard. The active emitters wire that protection by
    construction; this gate makes the guarantee ENFORCED (a future refactor that
    dropped the tweeter high-pass would fail loudly here rather than ship a
    dangerous graph). A preset with no tweeter role (a passive full-range or
    woofer-only shape) has nothing to protect, so the gate is a no-op — it never
    over-blocks. Observability: a block emits ``event=active_speaker.emit_gate``
    before raising, so the refusal is never silent.
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
    # Only its result is optionally suppressed as the mixer's inversion source:
    # the baseline/driver-domain emitters carry polarity through ``corrections``
    # (a per-driver Gain filter, see ``_emit_baseline_driver_definitions``)
    # instead, so the mixer must stay a no-op inverter there or the two would
    # cancel each other out (double inversion == net non-inverted).
    region_polarity = _role_polarity(preset)
    polarity = (
        region_polarity
        if apply_region_polarity
        else {role: False for role in region_polarity}
    )
    outputs = sorted(preset.channel_map.outputs, key=lambda item: item.index)
    output_count = _output_count(preset)
    # Active-speaker policy: build the (dest -> L/R-sum sources) map from
    # the preset's driver layout + per-driver polarity. The YAML spelling
    # is the shared emit_mixer; this routing is the assembly concern.
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
        # The local subwoofer taps the SAME full-range program as the mains: it
        # mono-sums L+R with the clip-safe -6.02 dB recipe. Its band-limiting
        # low-pass and excursion limiter live in the per-output pipeline chain,
        # NOT here — the mixer is pure routing.
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
# The single home for the local-sub lane spellings. The sub output carries an LR4
# low-pass (band-limit) + non-positive baseline gain + soft-clip limiter
# (excursion); the mains' lowest driver carries the complementary LR4 high-pass
# (bass management). Mirrors the per-driver name helpers above so the verifier
# imports one alias per filter rather than re-deriving the format.


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
# The emitter owns the spelling of every filter name it writes. The
# verification side (runtime_contract / staging / commission_ramp, via
# graph_evidence) imports THESE aliases rather than hardcoding a literal like
# "as_tweeter_startup_limiter" or re-deriving the format, so a name change here
# can never silently desync a safety verifier from the graph it inspects (the
# verifier would otherwise look for a filter that no longer exists and fail
# closed, spuriously blocking commissioning). Aliases (not renames) keep the
# emitter's own call sites — and its emission behaviour — completely untouched.
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

# The inter-speaker channel-select mixer name — emitter-owned graph vocabulary
# the verification side re-imports (via graph_evidence) rather than hardcoding,
# so a rename can never silently desync the runtime_contract driver-domain arm
# from the graph it inspects. The name is owned by the shared leaf
# (jasper.camilla_emit) and re-exported here so the active-speaker verifier has
# one import point.
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
    # Bass-management high-pass FIRST: the lowest driver's program is high-passed
    # at the sub crossover corner before its own crossover/delay/gain/limiter. The
    # sub low-pass at the same corner is the complementary lower half (see the sub
    # lane below) — together they are one crossover.
    if _bass_management_active(preset, role):
        names.append(_bass_management_hp_name(role))
    for region in _ordered_regions(preset):
        if region.lower_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=False))
        if region.upper_driver == role:
            names.append(_crossover_filter_name(role, region, highpass=True))
    # Layer-1a driver linearization (#1668 PR-D): immediately after the
    # crossover HP/LP, before bass-extension — mirrors the bass-extension
    # addon's own slot exactly (design doc "Layer 1a concretely"). Empty
    # linearization is a no-op, so an un-linearized/legacy baseline stays
    # byte-identical to before this stage existed.
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


# --- Layer-1a driver-linearization emission (#1668 PR-D) ---------------------
#
# Reduced shape only: {role: [{biquad_type, freq, q, gain}, ...]}. The richer
# LinearizationFit.to_dict() (fit_band_hz, residuals, reason_summary, mic_tier,
# driver_class, n_repeats, the honesty-ladder fields) is candidate/profile
# evidence, not emitter input -- jasper.active_speaker.linearization_fit's
# shared linearization_filters_by_role() reduces the richer shape down to
# this one before any caller reaches this module, so the emitter itself never
# needs to know the fit engine's own artifact shape.

# Hard cap on filters per driver (shelf + peaking combined). LOCKSTEP
# DUPLICATE of jasper.active_speaker.linearization_fit.MAX_FILTERS_PER_DRIVER
# -- not imported, because the emitter is an independent re-validation of
# whatever a persisted candidate claims, not a trust-the-caller pass-through,
# so it must not import the fit engine's own policy constant and inherit a
# future change to it silently. A pinning test asserts the two constants
# stay numerically equal.
MAX_LINEARIZATION_FILTERS_PER_DRIVER = 8

# Per-filter linearization BOOST ceiling (PR-L5) — the lockstep duplicate of
# ``linearization_fit.PER_FILTER_BOOST_CAP_DB``, held here for the same reason
# the filter count above is: the emitter re-validates what a persisted
# candidate claims rather than importing the fit engine's policy. A pinning
# test asserts the two stay numerically equal.
#
# This bounds ONE emitted biquad, not the correction. Total boost is uncapped
# by the owner's 2026-07-27 ruling, and is made safe not by a number here but
# by ``linearization_headroom_db`` below, which folds the worst branch chain's
# realized peak into ``active_baseline_headroom`` — so the boosted band
# lands at or under unity no matter how deep the correction is, and CamillaDSP's
# 0 dB ceiling, the per-driver limiters, and tweeter protection are untouched.
MAX_LINEARIZATION_BOOST_DB = 12.0

# Ceiling on the program-domain attenuation ``active_baseline_headroom`` may
# carry, dB. NOT a cap on the correction — it is a refusal.
#
# Total boost is uncapped by the owner's ruling, and the absorption mechanism
# turns every dB of it into a dB of pre-split attenuation. Left unbounded that
# is a silent failure mode rather than a safety one: eight filters at the
# per-filter cap would charge 96 dB and emit a graph that is, to a household,
# simply mute — with nothing in the journal naming why. So the emitter REFUSES
# past this line instead of quietly muting the speaker (adversarial review N7).
#
# 40 dB is deliberately generous — it is the same bound
# ``emit_active_speaker_baseline_config`` already validates
# ``baseline_headroom_db`` against, it is far past any correction the fit's own
# realization gates can produce, and a config asking for more has a defect
# upstream of this file. Fail-safe stays fail-safe; it just says so.
MAX_PROGRAM_HEADROOM_DB = 40.0

_LINEARIZATION_BIQUAD_TYPES = frozenset({"Peaking", "Highshelf", "Lowshelf"})

# Public alias, on ``blend_correction_name``'s rule below: a reader outside
# this module needs the same set to decide whether a persisted linearization
# record is one this system wrote. The evidence packet's
# ``packet_incumbent_linearization`` reads the applied profile's copy back out
# and asks exactly that; a second literal there is how the emitter's policy and
# that reader drift.
LINEARIZATION_BIQUAD_TYPES = _LINEARIZATION_BIQUAD_TYPES

# A linearization shelf carries NO steepness of its own. Every shelf reaches
# CamillaDSP through ``emit_filter_spec``, which spells the one Butterworth
# ``camilla_config_contract.SHELF_Q`` -- the same Q the fit engine designed the
# shelf at (``linearization_fit._HIGHSHELF_Q``) and scored its residual with.
# BOTH shelf types the fit engine emits (the rising-slope Highshelf and the
# CD-horn Lowshelf backbone / trailing Highshelf taper, #1668) share it.
#
# This block used to define ``_LINEARIZATION_SHELF_SLOPE = 6.0`` on the belief
# that CamillaDSP's ``slope: 6`` realized Butterworth. It does not: CamillaDSP's
# Butterworth is ``slope: 12`` (S = slope/12, S = 1), and at ``slope: 6`` the
# realized Q falls with the shelf's gain -- 0.476 at the -11 dB shelf the
# 2026-07-27 JTS3 profile carried, missing the designed curve by up to 1.7 dB
# across the tweeter band. The fit's realization gate, residual, and VERIFY
# prediction all evaluated the Butterworth shelf, so nothing in the loop could
# see it. See ``SHELF_Q`` for the formula and the upstream test that pins it.


def _driver_linearization_shelf_name(role: str) -> str:
    return f"as_{_name_token(role)}_linearization_shelf"


def _driver_linearization_peak_name(role: str, index: int) -> str:
    return f"as_{_name_token(role)}_linearization_peak_{index}"


def _driver_linearization_taper_name(role: str) -> str:
    # The CD-horn stage's optional TRAILING Highshelf taper (#1668). A distinct
    # name from the leading shelf so both a Lowshelf-led backbone and its taper
    # can coexist in one driver's chain without a duplicate filter name (which
    # CamillaDSP would reject / silently collapse).
    return f"as_{_name_token(role)}_linearization_taper"


def _linearization_slot(
    index: int, count: int, filters: Sequence[Mapping[str, Any]],
) -> str:
    """Classify one filter's role in a linearization chain by POSITION:
    ``"shelf"`` (a leading Highshelf/Lowshelf at index 0), ``"taper"`` (a
    trailing Highshelf after a Lowshelf lead, #1668), else ``"peak"``.

    The single source of the shelf-first / taper-last structural rule, shared
    by the validation gate, the chain namer, and the definition emitter so the
    three can never disagree about which slot an entry occupies. It classifies
    whatever order the input carries; enforcing that order is
    ``_validate_linearization_shelf_structure``'s job (a shelf-type entry that
    lands in a ``"peak"`` slot is what that gate rejects).
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


# Public aliases (matching the driver_baseline_gain_name convention above):
# the runtime-safety verifier (graph_evidence -> runtime_contract) re-proves
# the linearization stage against the EMITTED graph text and must spell these
# names identically rather than re-deriving the format.
driver_linearization_shelf_name = _driver_linearization_shelf_name
driver_linearization_peak_name = _driver_linearization_peak_name
driver_linearization_taper_name = _driver_linearization_taper_name

# Public alias, on ``LINEARIZATION_BIQUAD_TYPES``'s rule: a gate outside this
# module that admits a shelf must answer "would the emitter accept this list"
# before it accepts one, and that question has one owner. The per-driver
# prescription door (``crossover_v2.driver_prescription._parse_filters``) reads
# it so a shelf placed where ``_validate_linearization_shelf_structure`` would
# raise is refused at intake instead of at emission.
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

    Shared by every emitter gate that accepts a caller-supplied biquad list —
    Layer-1a linearization and the crossover blend correction today. Extracted
    rather than copied: both lists cross a JSON round trip before they reach
    this module, both are re-validated here on the never-trust-the-caller rule,
    and two hand-written copies of "is this a legal biquad" is exactly the
    second-source-of-truth shape that lets one of them silently fall behind a
    tightening applied to the other.

    What stays with each caller is its own POLICY: which biquad types it
    permits, what its gain ceiling is, how many entries it allows, and any
    structural rule about their order. This function owns only the per-entry
    field contract, and it raises rather than clamping — a value out of range
    means the persisted record was not written by the code that claims to own
    it, which is not a condition a clamp can repair.

    ``label`` names the owner in the message, so a household-visible refusal
    still says which stage refused.

    The Nyquist refusal (``freq >= sample_rate / 2``) is this module's OWN
    proof, independent of whatever produced the record: a fit-engine placement
    bound (``linearization_fit._HF_TAPER_NYQUIST_HZ``) is a design-time
    choice about what that ONE stage should emit, not a guarantee about every
    biquad this function will ever see — "the emitter would never write this"
    is not a proof (the doctrine `branch_chain.py` already states for exactly
    this class of gap). A biquad at or above Nyquist is not a policy choice to
    reject, it is not a realizable digital filter corner at all: CamillaDSP
    itself refuses it at ``--check``, so admitting one here would let an
    apply transaction stage a config guaranteed to fail load, rather than
    refusing it at the boundary that can still say why.
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
    """Normalize + independently re-validate the per-driver linearization
    filter list, mirroring ``_validated_driver_corrections``'s shape/spirit.

    ``linearization`` crosses a JSON round-trip before it ever reaches this
    emitter (a persisted ``MeasuredCrossoverCandidate``/applied-profile
    snapshot, not the fit engine's own dataclass), so this is an independent
    fail-closed gate -- not a trust-the-caller pass-through -- exactly like
    every other correction/gain field this module re-validates. An unknown
    role is dropped (mirrors ``_validated_driver_corrections``); a known
    role's filter list is validated field-by-field and RAISES
    ``ActiveSpeakerConfigError`` on the first violation (never silently
    dropped or clamped). The per-filter boost cap it re-proves is a
    REALIZATION-FIDELITY bound, not a hearing/SPL clamp -- past it the
    emitted filter stops being a faithful realization of the requested shape
    (``linearization_fit.PER_FILTER_BOOST_CAP_DB``'s own derivation); the SPL
    budget is charged by headroom accounting, not here. Pinned by
    tests/test_active_speaker_linearization_emission.py::test_linearization_rejects_boost_above_the_per_filter_cap
    and ::test_linearization_boost_is_accepted_and_absorbed_by_baseline_headroom.
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


# Ceiling on how many blend-correction cuts a candidate may carry, held here
# for the same reason ``MAX_LINEARIZATION_FILTERS_PER_DRIVER`` is: the emitter
# independently re-validates whatever a persisted candidate claims rather than
# importing the solver's own policy constant and inheriting a future change to
# it silently. A pinning test asserts the two stay numerically equal.
MAX_BLEND_CORRECTION_FILTERS = 2

# The blend correction is CUTS-ONLY, and this is the emitter's own half of that
# invariant (the solver's ``−max(deviation, 0)`` construction is the other).
# Two independent places, deliberately: the solver cannot represent a boost,
# and this gate refuses one — because between them sits a JSON round trip
# through a persisted candidate, which is precisely where a value the solver
# never produced could appear.
#
# A REFUSAL rather than a clamp, matching ``_validated_linearization``'s choice
# for the same reason: a positive gain here means the record was not written by
# the solver that claims to own it, and silently clamping it would emit a graph
# from a document nobody can vouch for.
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

    The crossover blend region's bounded shape correction (design doc decision
    10) — ``[{biquad_type, freq, q, gain}, ...]``, solved by
    ``crossover_v2.blend_correction`` from the summed at-the-mark measurement
    and emitted on the stereo program bus, before the split mixer.

    Fail-closed, like every other correction field this module re-validates.
    Per-entry fields go through the shared ``_validated_biquad_entry``; the
    policy this gate adds on top is the part that is specific to the stage:
    Peaking only (a shelf across a two-octave blend would re-level the region,
    which is the trim's job), at most
    :data:`MAX_BLEND_CORRECTION_FILTERS` entries, and every ``gain`` at or
    below :data:`MAX_BLEND_CORRECTION_GAIN_DB` — i.e. cuts only.

    An empty/absent correction returns ``[]``, so a candidate that carries none
    emits no stage and its graph is byte-identical to one written before this
    stage existed.
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
    """Fail-closed structural gate on shelf placement (#1668). The fit engine
    only ever emits shelves in ONE of two shapes: a single LEADING shelf
    (rising-slope Highshelf OR CD-horn Lowshelf backbone) at position 0, and —
    only after a Lowshelf lead — a single TRAILING Highshelf taper as the last
    entry. Everything else is Peaking. Any other shelf placement (a shelf mid-
    chain, two leading shelves, a taper without a Lowshelf lead, a taper that
    isn't last, a second Lowshelf) means the persisted candidate was corrupted
    or produced by something other than the fit engine — raise, so a duplicate
    filter name can never reach the graph. Peaking anywhere is always fine.
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
    """The filter-name list for ``role``'s linearization stage, one name per
    entry in ``filters`` IN INPUT ORDER: a leading shelf-type entry (Highshelf
    OR CD-horn Lowshelf, #1668) at position 0 gets the shelf name; a TRAILING
    Highshelf after a Lowshelf lead gets the taper name; everything else gets
    the next peak name. This function does not reorder or otherwise enforce the
    "shelf first / taper last" order -- it names whatever order the input list
    already carries. That order is a construction guarantee of the fit engine
    (``linearization_fit.fit_driver_linearization``) and is fail-closed re-
    validated at the emitter boundary (``_validate_linearization_shelf_
    structure``), not enforced here."""

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
    ``emit_filter_spec`` leaf (the same primitive preference-EQ bands use) so
    a Peaking/Highshelf/Lowshelf band is spelled identically everywhere in
    this codebase. Shelf-type entries (a leading Highshelf/Lowshelf, or a
    trailing Highshelf taper after a Lowshelf lead, #1668) carry NO steepness
    on their ``FilterSpec``: ``emit_filter_spec`` spells every shelf at the one
    Butterworth ``SHELF_Q`` the fit engine designed it at (see the shelf-Q note
    above this function's neighbours). The entry's own ``q`` is deliberately
    dropped for the same reason -- the fit only ever produces ``_HIGHSHELF_Q``
    there, and honouring a stray value would emit a shelf no evaluator in the
    fit loop can see. Position-aware naming mirrors
    ``_driver_linearization_chain_names`` exactly so the emitted definitions
    and pipeline names cannot disagree.
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

    Emits the per-region Linkwitz-Riley crossover pair, then each driver's
    [delay, non-positive baseline gain, soft-clip limiter] chain. This is the
    *intra-speaker* half — it has no program-domain headroom and no preference
    EQ (those are program-domain, wired only by the baseline caller). The
    follower's driver-domain emit reuses this verbatim so the relocated Layer A
    is byte-for-byte the same protective chain a solo speaker runs. ``linearization``
    is threaded only by the solo/leader baseline caller today — the follower's
    driver-domain emit never passes it, so its graph stays exactly the pre-PR-D
    protective chain (Layer 1a linearization ships to a follower in a later slice).
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
    # Layer-1a driver linearization (#1668 PR-D): immediately after the
    # crossover HP/LP definitions, before bass-management/bass-extension —
    # mirrors the bass-extension addon's own slot. Empty linearization emits
    # nothing.
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

    The sub starts muted for commissioning safety (mirrors a driver's startup
    mute). The band-limit (low-pass) and excursion limiter are still present so an
    un-muting Phase-B path arms an already-protected sub output."""
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

    Mirrors a driver's commissioning definitions — the lane's own startup mute is
    dropped (the per-output commission mute does the muting), so no orphan mute
    filter is emitted. The band-limit + excursion limiter are still present so the
    sub output stays protected even when its per-output mute is later lifted."""
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

    Mirrors a driver's commissioning chain — the per-output commission mute
    (appended in the pipeline) replaces the lane's own startup mute, so exactly
    one physical output is excited through the real graph. The LR4 low-pass and
    soft-clip limiter are preserved so the sub output stays band-limited AND
    excursion-limited even when its per-output mute is later lifted to ramp it."""
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

    The sub protection mirrors the mains' per-driver chain (non-positive gain +
    soft-clip limiter); the band-limit is the LR4 low-pass at the bass-management
    corner. The subwoofer commissioning-tone bounds (50 Hz floor / 300 ms) live in
    ``driver_protection.driver_protection_profile('subwoofer')`` and gate the
    later audible ramp; the durable graph's protection is this gain<=0 + limiter.
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
    :data:`jasper.active_speaker.branch_chain.HEADROOM_MARGIN_DB`. Worst
    branch and not the sum across branches because the driver chains run in
    PARALLEL after the split, so no single sample path ever sees two branches'
    boosts and the graph gives up the largest one.

    Until 2026-07-28 this was the per-branch SUM of positive filter gains — a
    valid upper bound on the same quantity (overlapping boosts at ONE
    frequency do add, which is the case a headroom charge has to survive), but
    a badly loose one: on the JTS3 profile of that morning it charged
    22.458 dB against a branch that peaked at +4.00 dB, most of the difference
    paying for two boosts the woofer's own crossover then attenuated by 13.3
    and 7.8 dB. The speaker ended 8.3 dB below the household's listening level
    at maximum volume. The owner's ruling (#1808): corrections must never
    stack invisible headroom — the speaker plays as loud as possible while
    accomplishing its leveling goals, subject to ``volume_limit`` 0 and
    protection. A bin-by-bin cascade evaluation contains the overlapping-boost
    case the sum was protecting, and no other.

    ``branch_context`` maps role to ``(crossover_sections, trim_db)`` — what
    else that branch's chain carries — and is **required**, deliberately.
    Omitting it would charge the linearization cascade alone: a strict
    over-estimate, so safe for a CHARGE, but silently wrong for any reader
    asking what a correction costs or comparing two of them, and wrong in the
    loud direction for a delta. There is no signature that lets a caller skip
    it by accident; :func:`_branch_context` builds it from the same preset and
    corrections the graph is emitted from. A role absent from the mapping
    still falls back to "no crossover, no trim" for the same over-estimating
    reason, but that is a per-role gap in a supplied context, not a whole
    missing one.

    Public because the runtime contract's prover has to agree with the emitter
    about this number, and the candidate payload and ``/correction/`` browser
    summary disclose it (via ``linearization_fit.worst_headroom_cost_db``
    reading the per-branch numbers the session stamps). The evaluation
    itself lives in :mod:`jasper.active_speaker.branch_chain` — one
    implementation, read by the fit's disclosure, this charge, and that proof.
    0.0 for a cut-only linearization, which is every one before PR-L5.
    """
    # A branch with no positive gain cannot reach unity through a crossover
    # and a non-positive trim, so the ordinary cut-only graph is charged 0.0
    # without evaluating anything — and without this module importing numpy,
    # which it otherwise does not (see branch_chain's module docstring on why
    # that dependency is kept lazy on a 1 GB Pi).
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

    The guard that keeps a cut-only graph — every graph before PR-L5, and
    every one a follower runs — off the chain-evaluation path entirely, so
    neither this emitter nor the runtime contract imports numpy for it (see
    :mod:`jasper.active_speaker.branch_chain`'s module docstring). Sound
    because a cut cascade, a Linkwitz-Riley section, and a non-positive trim
    are each <= 0 dB everywhere: that chain cannot reach unity, so its charge
    is 0.0 without evaluating anything.

    **Public because #2291's adoption table asks the same question of the
    APPLIED candidate.** A boosted intervention whose measured benefit is
    indeterminate fails closed and comes back off
    (:func:`~jasper.active_speaker.crossover_v2.verification.decide_adoption`'s
    ``boosted`` modifier). "Does this graph put energy in" has one definition
    on this speaker; a second copy of the rule in the host is precisely the
    drift that would let the emitter and the adoption decision disagree about
    the same graph.
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

    Built from the same two sources the graph itself is: the preset's
    crossover regions (which :func:`_emit_baseline_driver_definitions` turns
    into the emitted Linkwitz-Riley filters, ``lower_driver`` low-pass /
    ``upper_driver`` high-pass) and ``corrections``' per-driver ``gain_db``
    (the emitted baseline Gain). So the chain this charge is computed over is
    the chain the next few lines emit — not a description of it.

    The role -> sections half is :func:`jasper.active_speaker.branch_chain.
    sections_by_role`, shared with the session that stamps the disclosed
    ``headroom_cost_db`` — one derivation, because two of them had already
    drifted on the no-region case in a way that would have made the disclosure
    smaller than this charge.

    Deliberately omits the bass-management high-pass and the protective
    tweeter high-pass, which attenuate further still: crediting less
    attenuation over-estimates the peak, which over-charges rather than
    under-charges, and it keeps this identical to what the runtime contract
    can re-derive from the graph without walking optional filters.
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
    # Crossover blend correction (decision 10) — same Peaking primitive the
    # room PEQs above use, and wired beside them pre-split.
    #
    # It charges NO headroom below, and the reason is that it CANNOT BOOST —
    # ``_validated_blend_correction`` refuses a positive gain — not that the
    # common attenuation covers it. ``blend_correction`` is deliberately absent
    # from ``total_headroom_db``'s expression, and a boost posture would have
    # to ADD a term there: position above the gain is necessary for absorption
    # and is not sufficient for it. A boosting stage left un-termed would
    # silently spend the room layer's allocation instead of charging its own.
    # Pinned by ``test_the_blend_stage_charges_no_headroom_and_is_not_a_term``.
    for i, entry in enumerate(blend_correction, start=1):
        lines.extend(
            emit_peaking_biquad(
                _blend_correction_name(i),
                freq=float(entry["freq"]),
                q=float(entry["q"]),
                gain=float(entry["gain"]),
            )
        )
    # Program-domain headroom for the pre-split room PEQ (Layer B) and
    # preference EQ (Layer C). This gain is the active graph's single place for
    # explicit common attenuation: baseline headroom, room-correction boost
    # headroom, plus the household's manual headroom / loudness-match
    # output_trim_db when a profile has EQ. Preference boosts themselves ride at
    # unity, matching the stereo /sound policy: boosts boost. Room-correction
    # boosts are different: correction can raise a known room band above unity,
    # so its worst-case positive boost is folded into this headroom gain rather
    # than emitted as a separate room_headroom filter.
    # Layer-1a linearization boost (PR-L5) joins the same fold for the same
    # reason room-correction boost does: a per-driver correction can now raise
    # a band above unity, so its worst-case positive boost is absorbed by this
    # one common attenuation rather than left to clip. It rides the PRE-SPLIT
    # gain because every branch sees the same program, so absorbing the worst
    # branch's total covers all of them. This is the mechanism that lets the
    # fit engine's boost stay uncapped while the 0 dB ceiling stays a hard
    # rail. It is the SAME quantity the fit discloses as
    # ``LinearizationFit.headroom_cost_db`` — both are that branch chain's
    # realized peak (#1808; it was the per-branch sum of positive gains until
    # 2026-07-28) — so what a household is told the correction costs is what
    # the speaker actually gives up. Pinned by a test.
    # A NUMBER, never a gate. `active_baseline_headroom` is always emitted, so
    # folding the trim into its value keeps a flat-window crossing a parameter
    # write; gating it on the profile doing something would step this gain by
    # the whole trim, un-ducked, the moment a band crossed +-0.05 dB. Matches
    # the stereo path's `sound_preamp`, which is emitted unconditionally for
    # the same reason. The user-visible consequence is the same there: a
    # configured trim attenuates even while the profile is flat.
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
    # Program-domain preference EQ (Layer C) definitions. Emitted via the shared
    # leaf emit_filter_spec (the same one emit_sound_config uses), so the active
    # and stereo paths spell a preference band identically. They are wired
    # pre-split — see _emit_baseline_pipeline.
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
    # Crossover blend correction (decision 10) — the summed measurement's
    # bounded, cuts-first shape correction across the crossover region. Three
    # properties come from this placement and no other, which is why it is here
    # and not in a per-role chain:
    #
    #  1. ONE summed fact, ONE filter. The correction describes the SUM.
    #     Pre-split is the only place where "one filter = one summed fact" is
    #     true by construction; per-role emission would be N copies of one fact
    #     whose only defence against drift is a test.
    #  2. Common-mode by construction. Applying the same B(f) to every role
    #     gives Σ_r sign_r·B·C_r·D_r = B · Σ_r sign_r·C_r·D_r — the sum scales,
    #     the inter-driver complex ratio is untouched. An asymmetric
    #     application would change the interference pattern, which is ALIGNMENT
    #     work; alignment keeps its own tools (contract clause (c)). Here,
    #     asymmetry is unrepresentable rather than merely tested against.
    #  3. Upstream of protection. In the durable baseline the crossover
    #     high-pass at the tweeter's own chain IS that driver's protection
    #     (re-proved by _assert_tweeter_outputs_protected). A pre-split filter
    #     sits above it and cannot push energy past it, whatever a future boost
    #     posture decides.
    #
    # BEFORE active_baseline_headroom, beside the room PEQs, so that the stage
    # sits where a boost WOULD be absorbable. That placement is necessary and
    # not sufficient: absorption happens because a layer is a TERM in
    # ``total_headroom_db``, and this one deliberately is not (it cannot boost).
    # A future boost posture needs both — the position it already has, and a
    # term it does not. Emitted only when present, so a candidate carrying none
    # stays byte-identical to a pre-decision-10 graph.
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
    # Preference EQ (Layer C) is a PROGRAM-domain transform: it rides the
    # stereo bus on channels [0, 1] strictly BEFORE the split mixer, so it is
    # upstream of every per-driver crossover, limiter, and tweeter high-pass.
    # That placement is what makes a preference boost safe — it can neither
    # move a crossover corner nor bypass a driver limiter. Emitted only when
    # present, so a flat profile is byte-identical to the pre-PR-3 baseline.
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
    # Driver-domain-only (follower) pipeline. The inter-speaker channel-select
    # runs FIRST (a 2->2 Mixer that picks L/R/mono from the leader's corrected
    # stereo program), THEN the optional pair-balance trim on the selected stereo
    # bus, THEN the intra-speaker 2->N split, THEN each driver's
    # crossover/delay/gain/limiter chain. Exactly one helper owns this ordering so
    # an edit to the safety-critical Layer-A pipeline cannot fork the trimmed and
    # untrimmed cases.
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

    The returned YAML is a candidate for later validation and manual
    inspection. This function deliberately does not load or reload
    CamillaDSP. The caller must also provide an explicit active-hardware
    playback device so the current stereo outputd lane is never used by
    accident.
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    # queuelimit rides the same coercion as every other integer knob here.
    # It reaches the YAML through an f-string, so an unvalidated value is the
    # one emitter input that can put arbitrary text into a CamillaDSP field;
    # its siblings were coerced and it was not. Hardening, not a fixed escape:
    # the reachable injection was chased to the volume_limit ceiling and does
    # not escalate past it.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear
    # here rather than let the ioplug attach crash on it. No-op for every
    # ALSA-lane emit (see _assert_ring_playback_width).
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

    Public because the protected-staging software guard
    (``jasper.active_speaker.staging``) references these by index to prove a
    driver's output is muted — the name is a cross-module contract, so the
    emitter owns it and the guard reads it rather than re-deriving the spelling.
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

    The third statefile-seeding outcome (issue #2135). A box whose saved output
    topology declares roleful/protected outputs but has NOT yet staged an
    all-muted active startup graph — commissioning paused between "topology
    declared" and "crossover preview staged" — has no legal graph to run: a flat
    full-range graph would put program into a declared tweeter, and there is no
    staged graph to select. Refusing was the only option before, and it failed
    the whole deploy.

    This graph is the safe third option, and it is silent twice over:

    * **The sink is a ``File``, not a DAC** (:data:`PARKED_SINK_PATH`). That is
      the same safe-by-construction argument the camilla#1 program bake rests
      on — no DAC attached, so no driver can be over-driven regardless of the
      saved topology. It also makes parking DAC-agnostic: a board that declares
      no active outputd lane at all can still park. Its emitted ``format`` is
      ALWAYS ``DEFAULT_PIPE_SINK_FORMAT`` (D4, wide-output-path program),
      decoupled from ``playback_format``/``DEFAULT_PLAYBACK_FORMAT`` the same
      way the bonded-leader pipe sink is in
      :func:`jasper.sound.camilla_yaml.emit_sound_config`. ``playback_format``
      (default ``None``) is not applicable to this ``/dev/null`` File sink at
      all — passing it EXPLICITLY (a caller-supplied value, ``is not None``)
      is refused rather than silently ignored; this is a pure presence check
      on the argument, never a value comparison against a mutable default, so
      the only production caller (which never passes it) can never trip it.
    * **Every physical output is hard muted** by the repo's one mute idiom — a
      ``Gain`` filter at :data:`STARTUP_MUTE_GAIN_DB` with ``mute: true``, the
      same primitive the staged all-muted startup graph's crash-recovery
      invariant rests on — and the mixer feeds every destination at that same
      -120 dB floor, so even a defeated boolean mute is inaudible.

    It deliberately claims NOTHING else: no crossover, no driver roles, no
    per-driver protection, no limiter policy. It is silence with a legal shape,
    not a tuning. The runtime contract re-proves both properties independently
    before this graph may be selected
    (``jasper.active_speaker.runtime_contract._parked_graph_allowed``); this
    emitter's own gate below is the first of those proofs.
    """

    capture_device = _yaml_string(capture_device, "capture_device")
    capture_format = _yaml_string(capture_format, "capture_format")
    # D4 (wide-output-path program): the parked graph's /dev/null File sink is
    # pinned to DEFAULT_PIPE_SINK_FORMAT below, independently of
    # playback_format — mirrors the bonded-leader pipe-sink guard in
    # jasper.sound.camilla_yaml.emit_sound_config. GENUINELY explicit: a bare
    # ``is not None`` presence check on the CALLER-SUPPLIED argument, not a
    # value comparison against DEFAULT_PLAYBACK_FORMAT (a module global read
    # fresh at call time would silently diverge from this parameter's
    # def-time-bound default the moment the two constants are no longer
    # assigned in lockstep — SF1 from the PR-1 gate review). This function's
    # only production caller never passes playback_format, so it can never
    # trip this — the box must always be able to park. playback_format is
    # not otherwise used (no ``_yaml_string`` validation either): the emitted
    # format is always the pinned constant below, never this parameter.
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")

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

    # Fail-closed emit gate, mirroring _assert_tweeter_outputs_protected: re-prove
    # against the EMITTED TEXT (not the emitter's own construction) that every
    # physical output is hard-muted and that mute is wired to its channel. A
    # refactor that dropped one output's mute fails loudly here instead of
    # shipping a graph that could drive a declared tweeter.
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
    the dedicated tweeter high-pass; automatic response measurement removes
    only that extra filter so it measures the applied crossover shoulder.

    ``measurement_delay_roles`` is the reverse-null delay sweep's lane (R-1):
    the named roles carry a ``Delay`` filter at the head of the chain. Position
    is free — a pure delay is LTI and commutes with every other stage here, so
    it changes no magnitude wherever it sits — and the head is chosen because
    this chain has nothing upstream of the protection worth preceding. (The
    applied chains place their own delay AFTER the crossover; the two orderings
    are equivalent, not a match.) Empty for every other caller, which keeps
    their chains byte-identical.
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
    # R-1's delay lane. Definitions only for the roles the caller named, so an
    # ordinary program emits exactly the filters it always did.
    #
    # ONE `fmt` pass over the raw microsecond value and no intermediate
    # rounding: `_emit_delay_filter` formats through `jasper.camilla_emit.fmt`,
    # which IS `delay_graph.quantized_delay_ms`'s implementation, so the value
    # a proof recomputes from the same `delay_us` matches by construction.
    # Pre-quantizing here would be the second pass that module forbids.
    delays = dict(measurement_delays_us or {})
    if delays:
        if protection_sections_by_role is None:
            # The unprotected shape already defines a zero Delay filter for
            # every role below, so a named delay would emit a duplicate mapping
            # key whose later zero wins on parse -- a capture that plays with no
            # delay and banks as a delayed take. Refuse rather than emit a
            # graph whose YAML disagrees with the request.
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
                # Caught here because a non-finite value emits `delay: .nan`,
                # which parses back as a float and would read as a bound
                # question rather than a nonsense one. The RANGE is the shared
                # proof's (`_assert_measurement_delays_bound`), not a second
                # copy here.
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
    # output is band-limited AND excursion-limited even in the commissioning graph,
    # exactly like the mains. Its muting is the per-output commission mask below
    # (the sub's own startup mute is not wired into the commissioning chain).
    if preset.local_subwoofer is not None:
        lines.extend(_emit_sub_commissioning_definitions(
            preset.local_subwoofer.crossover_fc_hz,
            limiter_clip_limit_db=limiter_clip_limit_db,
        ))
    # Per-output commissioning mute: only audible outputs pass; the rest are
    # hard-muted so exactly one physical driver is excited through the real
    # graph. Default (empty set) is fully muted — the safe initial load.
    # Audible outputs carry ``audible_gain_db`` as their per-output gain — the
    # Stage-5 ramp variable. It defaults to the silent mute floor
    # (``STARTUP_MUTE_GAIN_DB``), so an un-ramped commission load arms the target
    # at {gain: -120, mute: off} (silent); the Stage-5 gate raises it within the
    # commissioning level envelope. Muted outputs always stay at the -120 dB
    # mute floor regardless of ``audible_gain_db``.
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

    This is the single-audio-path commissioning config: the same protected
    graph the speaker runs (volume ceiling 0 dB, startup headroom, protective
    tweeter high-pass, per-driver limiters), but with each physical output
    individually mutable. Loading it with ``audible_outputs={k}`` excites
    exactly one driver through its real crossover/limiter chain; an empty set
    is fully muted (the safe initial load); the full set is every driver live
    for the summed check.

    It replaces the old direct-DAC diagnostic bypass: validation now happens on
    the production path, so the commissioned config *is* what gets frozen as the
    durable profile. Like the other emitters it does not load or reload
    CamillaDSP, and it refuses the existing stereo outputd lane as a playback
    device.
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if startup_headroom_db < 0 or startup_headroom_db > 80:
        raise ActiveSpeakerConfigError("startup_headroom_db must be between 0 and 80")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )
    # Structural bound only: the per-output audible gain is an attenuation, so it
    # never exceeds the 0 dB ceiling and never drops below the -120 dB mute floor.
    # The tighter commissioning *level envelope* (the audible test range and the
    # per-step ramp limit) is owned by the Stage-5 ramp gate, not this primitive.
    if audible_gain_db < STARTUP_MUTE_GAIN_DB or audible_gain_db > 0:
        raise ActiveSpeakerConfigError(
            f"audible_gain_db must be between {STARTUP_MUTE_GAIN_DB:.0f} and 0 dB"
        )

    # queuelimit rides the same coercion as every other integer knob here.
    # It reaches the YAML through an f-string, so an unvalidated value is the
    # one emitter input that can put arbitrary text into a CamillaDSP field;
    # its siblings were coerced and it was not. Hardening, not a fixed escape:
    # the reachable injection was chased to the volume_limit ceiling and does
    # not escalate past it.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear
    # here rather than let the ioplug attach crash on it. No-op for every
    # ALSA-lane emit (see _assert_ring_playback_width).
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
    # protective high-pass even while the per-output commission mask mutes it, so
    # a graph that could later be unmuted onto a bare compression driver is
    # refused here rather than shipped.
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

# LR4 is the shipped crossover slope, and 24 dB/oct is the slope this build
# COMMISSIONS a tweeter crossover high-pass at (design §5.4). It is a code
# figure, not a declaration, so since the 2026-08-23 owner ruling it may
# disclose a shallower crossover and may prove a protective filter this build
# itself derived — it may NOT refuse a crossover order a household pinned. See
# ``_assert_tweeter_crossover_hp_satisfies_floor`` for which half of that gate
# is a refusal and which is a log line, and
# ``driver_protection.PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE`` for the same
# distinction on the declaration side.
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

    Fail-closed on the same question :func:`_validated_inverted_roles` asks,
    and for its reason: a trim naming a role
    no output declares would attenuate nothing, the graph would emit
    byte-identical to its unmatched twin, and the banked record would claim a
    level match nobody played.

    **Attenuation only.** A positive value is refused rather than clamped: this
    is the one seam that could raise a measurement's drive above the level the
    session was admitted at, and the honest answer to "make the tweeter louder"
    is that the measurement graph does not do that. It leaves every hearing
    clamp untouched — ``volume_limit``, the per-driver limiter and the tweeter
    protection high-pass are all downstream of this mixer and unreachable from
    here.
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

    ``inverted_roles`` is the MEASUREMENT's reverse-null flip (R-1): each named
    role's branch has its sign reversed **relative to whatever polarity this
    graph would otherwise carry**, which is why it XORs onto the region
    polarity rather than replacing it. It is level-neutral by construction —
    every ``dest`` of this mixer has exactly ONE source (a program channel is
    copied to its role's outputs, never summed), so flipping ``inverted``
    negates each sample and leaves its magnitude, and therefore every peak the
    limiter and the volume ceiling answer for, bit-identical.

    ``level_trims_db`` is the measurement's declared per-driver level match,
    and it is the ONE thing that moves a ``gain``: each named role's single
    source is attenuated by its value, so the branches meet the crossover at
    comparable level and a reverse null can actually form. Attenuation only
    (:func:`_validated_measurement_trims`), so every peak this mixer emits can
    only fall — the limiter and the volume ceiling answer for strictly less
    than they did. An unnamed role keeps the ``0.0`` it always carried, so an
    emit that asks for no level match is byte-identical to what it was.

    Unlike :func:`_emit_split_mixer` (which routes a stereo program bus by output
    *side*), this routes by driver *role*: every physical output of role ``r``
    takes its single source from ``role_channels[r]`` — the program-WAV channel
    carrying that driver's stimulus (ch0 → woofer, ch1 → tweeter, per design
    §5.4). ``channels_in`` is the program channel count (max mapped channel + 1).

    The emitted mixer is named ``split_active_{way_count}way`` — the SAME name
    :func:`_emit_split_mixer` uses — for two independent reasons that both land
    on the identical spelling: (1) :func:`_emit_commissioning_pipeline`, reused
    verbatim by the program graph, hardcodes a ``Mixer`` pipeline step under
    that exact name (CamillaDSP's ``SetConfig`` refuses to load a pipeline step
    referencing an undefined mixer — W6 hardware run 4 finding I: the program
    graph shipped as ``program_route_{way}way`` here while the pipeline pointed
    at ``split_active_{way}way``, and every program-graph load was rejected);
    (2) ``jasper.active_speaker.environment``'s ``_ACTIVE_SPLIT_RE`` — the
    runtime ecosystem's active-config classifier — recognizes an active-speaker
    config by the presence of a ``split_active_Nway`` mixer name. It is ecosystem
    vocabulary, not a routing claim: this mixer's ROUTING stays role-routed
    (see above), never side-routed like the commissioning/baseline/startup
    split — only the NAME is shared.
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
    high-pass alone (the extra bring-up protective HP is dropped so the measured
    branch is the applied crossover shoulder — design §5.4), so this build-time
    gate reads that crossover from the preset BEFORE any YAML is emitted.

    **The corner REFUSES; the slope only DISCLOSES**, and the split is the
    2026-08-23 owner ruling ("if it was in the safe overall envelope, it's safe
    to test"), not a weakening. ``min_corner_hz`` reaches here as
    :data:`~jasper.active_speaker.graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ`
    (400 Hz) — an absolute code floor, not this driver's declaration, which is
    enforced at the pin (``declared_floor_hz``) and at apply
    (:func:`_assert_tweeter_crossover_honours_declared_floor`). It stays a
    refusal because a crossover below it puts the low-frequency excursion hazard
    band on a compression driver — a named damage mechanism, which is what the
    doctrine's section 4 lets a code figure refuse on.
    ``min_slope_db_per_octave`` reaches here
    as :data:`PROGRAM_PROTECTIVE_HP_MIN_SLOPE_DB_PER_OCTAVE`, a hardcoded 24 that
    no datasheet contains; refusing an order-2 crossover against it is the
    same nanny the ruling struck at
    :func:`~jasper.active_speaker.crossover_v2.topology_prescription.read_topology_prescription`,
    one stage later and after the round had already been measured.

    Why that was not caught with the rest of it: this branch runs only when the
    caller omits ``protection_sections_by_role``, which the MEASURE call site
    supplies and the **VERIFY** one does not — so an order-2 pin was admitted,
    measured, applied, and then refused at VERIFY's emit. The manufacturer's
    published condition is enforced at the pin, where the declaration is
    readable; here there is no profile to read one from, so the shortfall is
    logged (``result=tweeter_hp_slope_below_commissioning_floor``) and the graph
    is emitted.
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
            # rather than ERROR because nothing is blocked: the household chose
            # a shallower crossover than this build commissions at, the corner
            # above already cleared the declared floor, and the manufacturer's
            # published condition (if any) was applied at the pin.
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

    Runs on every active-speaker graph THIS module emits, right before it is
    returned or written — the same "prove it against the emitted text" shape as
    :func:`_assert_tweeter_outputs_protected`, but a structural check rather
    than a protection one: it does not reason about channels or filter
    parameters, only whether every ``Mixer.name``/``Filter.names`` entry the
    pipeline references resolves against the graph's own ``mixers:``/
    ``filters:`` sections.

    This closes the W6 hardware run 4 finding I hole: the program graph's
    pipeline (reused verbatim from ``_emit_commissioning_pipeline``) named its
    routing mixer ``split_active_2way`` while the graph's own ``mixers:``
    section defined ``program_route_2way`` — a mismatch CamillaDSP's
    ``SetConfig`` only caught at LOAD time on real hardware ("Use of missing
    mixer 'split_active_2way'"), and even then reported only the first
    dangling reference. Every emitter in this module composes its filter
    definitions, mixer, and pipeline from independent helper calls
    (see ``emit_active_speaker_program_config`` and
    ``emit_active_speaker_baseline_config``); nothing upstream of this gate
    proves the three pieces agree.
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
    the tree's one answer to "does this graph carry that delay": it checks the
    value through the same quantizer a later proof would, that the filter
    occurs in EXACTLY ONE pipeline step wired to exactly the role's channels,
    the 20 ms DSP bound, and ``devices.volume_limit``. Consuming it here rather
    than re-checking those things by hand is what keeps one answer to the
    question — and it is structural, so it catches an orphan filter or a
    duplicate definition that a value check alone would miss.
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

    Runs the reference-closure gate (:func:`_assert_pipeline_references_closed`)
    plus the three L0 tweeter proofs on the EMITTED text — the same evidence a
    later readback would inspect — and refuses to return a graph that does not
    prove all four. This is the program builder's return contract: it cannot
    emit a graph whose pipeline points at an undefined mixer/filter name, nor
    one whose tweeter output is not high-pass protected (against the declared
    floor) AND wrapped by its crossover high-pass + soft-clip limiter in one
    post-mixer step. A pre-split per-channel high-pass (which
    ``output_highpass_protected`` alone could false-PASS on the 2-way preset,
    where program ch1 numerically coincides with tweeter output 1) is rejected
    here because ``tweeter_guard_present`` requires the high-pass AND the limiter
    together on exactly the tweeter output channels.
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

    The v2 crossover session plays one 2-channel program WAV
    (``jasper.audio_measurement.program``) once through ``correction_substream``;
    ``role_channels`` maps each driver role to the program-WAV channel carrying
    its stimulus (ch0 → woofer, ch1 → tweeter — design §5.4). This graph:

    * routes each program channel to its driver's PHYSICAL output path via a
      role-routed mixer (:func:`_emit_role_routed_mixer`);
    * carries either the legacy target crossover or caller-supplied confirmed
      role protection plus the per-driver limiter. The protected-neutral shape
      omits configured crossover, delay, linearization, bass, Room, and
      preference filters, and carries no per-driver level trim unless the
      measurement declares one (``measurement_level_trims_db`` below);
    * keeps the software volume ceiling non-positive and stays static (no reload
      mid-program).

    ``inverted_roles`` is the reverse-null measurement's flip (R-1): the named
    driver branches ride the mixer with their sign reversed. **This emitter
    only** — the applied/baseline graphs a household plays through never take
    it. Level-neutral: see :func:`_emit_role_routed_mixer` for why no peak
    moves, and note that the tweeter protection high-pass, the per-driver
    limiter and the ``volume_limit`` ceiling below are unreachable from it.

    ``measurement_delays_us`` is R-1's other half — the delay sweep's candidate
    coordinate, one non-negative microsecond value per named role. **Scoped
    exactly like** ``inverted_roles``, and for the same reason: it is a
    parameter of this measurement emitter and of nothing else. The applied and
    baseline emitters take their per-driver delay from the profile's
    ``corrections`` (:func:`_emit_baseline_driver_definitions`) and cannot
    reach this argument, so a swept coordinate can never leak into a graph a
    household plays. ``None`` or empty emits no ``Delay`` filter and no chain
    entry, which keeps every existing program byte-identical. Values reach the
    YAML through a single :func:`~jasper.camilla_emit.fmt` pass — the same
    formatter :func:`~jasper.audio_measurement.delay_graph.quantized_delay_ms`
    is implemented as — so a proof that recomputes the expected value from the
    same ``delay_us`` agrees exactly, with no intermediate rounding to disagree
    about.
    Delays ride ahead of the protection sections. A pure delay commutes with
    every stage in this chain, so the position changes no magnitude; it is a
    readability choice, not an equivalence claim about the applied chain, which
    places its own delay after the crossover.

    ``measurement_level_trims_db`` is the measurement's declared per-driver
    LEVEL MATCH — the box's own banked per-driver trim, resolved on-box by the
    session that opened this graph. **Scoped exactly like**
    ``measurement_delays_us``, and for its reason: on a cabinet whose drivers
    differ by ~10 dB of sensitivity the two branches never sum to a deep null
    however well aligned they are, so the reverse-null confirmation needs the
    branches level — and a household's applied graph takes its per-driver gain
    from the profile's ``corrections``, cannot reach this argument, and is
    therefore untouched by whatever a measurement declares. It lands on ONE
    seam, the role-routed mixer's per-source gain
    (:func:`_emit_role_routed_mixer`); the per-output commissioning gain is
    deliberately NOT also touched, because two seams carrying one decision
    would apply it twice. Attenuation only, refused otherwise. ``None`` or
    empty moves no gain and keeps every existing program byte-identical.

    Two fail-closed gates run before the graph can leave this function: a
    build-time proof that the selected tweeter HP satisfies the declared floor, and
    a build-and-prove of the emitted text against ``graph_safety``'s three L0
    tweeter proofs (:func:`_assert_program_graph_proven`). Like the sibling
    emitters it does not load or reload CamillaDSP and refuses the stereo outputd
    lane as a playback device.
    """

    preset.validate()
    # Scope gate: this emitter routes ONE program channel per declared driver
    # role — a 1-way passive main (one routed solo of the whole speaker) or a
    # 2-way. A 3-way needs a designed reshape (mid-band MESM schedule,
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
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

    # queuelimit rides the same coercion as every other integer knob here.
    # It reaches the YAML through an f-string, so an unvalidated value is the
    # one emitter input that can put arbitrary text into a CamillaDSP field;
    # its siblings were coerced and it was not. Hardening, not a fixed escape:
    # the reachable injection was chased to the volume_limit ceiling and does
    # not escalate past it.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear
    # here rather than let the ioplug attach crash on it. No-op for every
    # ALSA-lane emit (see _assert_ring_playback_width).
    _assert_ring_playback_width(playback_device, output_count)
    program_channels = 1 + max(role_channels.values())
    # Every output is audible: a program never mutes a driver (the WAV silences
    # it by channel), so the per-output commission mask is all-unmuted at 0 dB.
    # Program headroom is the commissioning headroom (0 dB) so the effective-peak
    # ledger the session-volume plan / admission share is main_volume + program
    # peak with no hidden graph attenuation.
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
        # Beside `inverted_roles` below and for its reason: the graph SAYS which
        # coordinate it carries, so a record naming it by fingerprint can be
        # read without reconstructing the Delay filter body.
        #
        # Emitted ONLY when there is a coordinate. An unconditional line would
        # change the bytes -- and so the fingerprint -- of every CHECK and
        # MEASURE graph in the tree, which is exactly the byte-identity this
        # parameter is scoped to protect. Its absence is unambiguous.
        *(
            [
                "# measurement_delays_us="
                + repr(dict(sorted(measurement_delays_us.items())))
            ]
            if measurement_delays_us
            else []
        ),
        # Beside the delay line, on its terms: emitted ONLY when the
        # measurement declares a level match, so every graph that declares
        # none keeps the bytes — and so the fingerprint — it always had. The
        # numbers reach the comment from the SAME validated mapping the mixer
        # gains came from, so the graph states the trims it actually carries
        # and the take's ``graph_fingerprint`` moves with them for free.
        *(
            ["# measurement_level_trims_db=" + repr(dict(sorted(level_trims.items())))]
            if level_trims
            else []
        ),
    ]
    if inverted_roles:
        # Emitted only when a branch is actually flipped, so every non-inverted
        # emit stays byte-identical to what it was. The graph then SAYS which
        # branch carries the reverse-null, beside the fingerprint a record
        # names it by.
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

    Unlike the startup template, this YAML is not muted. It still preserves
    the JTS 0 dB volume ceiling, keeps per-driver limiters, and refuses
    positive per-driver correction gain.
    Callers own the acceptance evidence and explicit CamillaDSP apply step.

    ``room_peqs`` (Layer B) is the preserved room-correction PEQ set. Each
    filter is emitted on program channels [0, 1] before the split mixer, and any
    positive room-correction boost is folded into ``active_baseline_headroom``.
    That keeps the active path one-headroom-shaped while preserving the stereo
    correction safety policy.

    ``preference_filters`` (Layer C) is the program-domain preference EQ band
    list — the same ``FilterSpec`` objects the stereo emitter takes. Each band
    is emitted VERBATIM on the program channels [0, 1] strictly *before* the
    split mixer; dropping neutral ones is the caller's job, because the live
    editing draft needs its idle slots kept. Preference
    boosts ride at unity, matching ``emit_sound_config``: the active graph keeps
    them safe by placing them upstream of every crossover/limiter/tweeter HP and
    preserving the 0 dB volume ceiling. The empty default keeps every existing
    caller byte-identical.

    ``output_trim_db`` is the household's manual headroom + loudness-match
    attenuation (``jasper.sound.settings.output_trim_db``), folded into the same
    ``active_baseline_headroom`` gain so the active path honours it exactly like
    ``emit_sound_config``. It is applied only when some band actually boosts (a
    flat profile can't clip from EQ and plays at unity), so the default keeps
    the no-EQ baseline byte-identical.

    ``linearization`` (Layer 1a, #1668 PR-D) is the per-driver EQ/shelf stage
    the driver-linearization fit engine
    (``jasper.active_speaker.linearization_fit``) designs — cut-preferred but
    NOT cut-only since PR-L5: a positive ``gain`` is legal here, bounded by
    the per-filter cap re-proved below — the REDUCED shape
    ``{role: [{biquad_type, freq, q, gain}, ...]}``, produced from a
    ``LinearizationFit``/candidate by
    ``linearization_fit.linearization_filters_by_role``. Each role's filters
    are emitted immediately after that driver's crossover HP/LP and before
    bass-extension (mirrors the bass-extension addon's own slot exactly), via
    the shared ``emit_filter_spec`` primitive. Independently re-validated here
    (``_validated_linearization``): ``biquad_type`` in {Peaking, Highshelf,
    Lowshelf}, finite positive ``freq``/``q``, ``gain`` capped at
    ``MAX_LINEARIZATION_BOOST_DB``, plus the
    fail-closed shelf-placement structure (one leading shelf, one optional
    trailing Highshelf taper after a Lowshelf lead — #1668) — a hardware-bound
    safety invariant re-proved at the emitter boundary, not assumed from the
    caller. The empty default keeps every existing caller byte-identical.
    Pinned by
    tests/test_active_speaker_linearization_emission.py::test_linearization_rejects_boost_above_the_per_filter_cap
    and ::test_linearization_boost_is_accepted_and_absorbed_by_baseline_headroom.

    ``blend_correction`` (decision 10) is the crossover blend region's
    summed-response-owned shape correction — the flat list
    ``[{biquad_type, freq, q, gain}, ...]``
    ``crossover_v2.blend_correction.solve_blend_correction`` designs from the
    post-apply spatial cloud. Flat rather than per-role on purpose: it
    describes the SUM, and it is emitted ONCE on program channels [0, 1]
    before the split mixer, which is what makes it common-mode by construction
    (see ``_emit_baseline_pipeline`` for the three properties that placement
    buys). Independently re-validated here (``_validated_blend_correction``):
    Peaking only, finite positive ``freq``/``q``, at most
    ``MAX_BLEND_CORRECTION_FILTERS`` entries, and NON-POSITIVE ``gain`` — the
    cuts-only invariant, refused rather than clamped. The empty default keeps
    every existing caller byte-identical.
    """

    preset.validate()
    # L0 emit gate (fail-closed), BEFORE any YAML is built: this is the graph
    # the routine apply transaction ships to a household, so a crossover below
    # the tweeter's own declared protection floor is refused here rather than
    # emitted and left for the startup-load gate to catch on the next boot.
    _assert_tweeter_crossover_honours_declared_floor(preset)
    # N3 (#1668 PR-D review): normalize the optional linearization mapping
    # here rather than defaulting the parameter to a mutable ``{}`` literal
    # -- matches how every other optional dict/sequence param on this
    # function is handled.
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
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

    # Emit what the caller handed over, verbatim. Every caller now hands a slot
    # per declared band with the neutral ones kept, because a filter appearing
    # or disappearing is what forces a ducked pipeline replace. Re-filtering
    # here would quietly undo that.
    emitted_preference_filters = tuple(preference_filters)
    room_peqs = tuple(room_peqs)

    # queuelimit rides the same coercion as every other integer knob here.
    # It reaches the YAML through an f-string, so an unvalidated value is the
    # one emitter input that can put arbitrary text into a CamillaDSP field;
    # its siblings were coerced and it was not. Hardening, not a fixed escape:
    # the reachable injection was chased to the volume_limit ceiling and does
    # not escalate past it.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear
    # here rather than let the ioplug attach crash on it. No-op for every
    # ALSA-lane emit (see _assert_ring_playback_width).
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
    # household actually plays through, so re-prove every tweeter output carries
    # its crossover / protective high-pass before it can leave the emitter — a
    # flat/unprotected tweeter baseline is the shrill hot-tweeter hazard.
    _assert_tweeter_outputs_protected(yaml, preset)
    _assert_bass_extension_safe(yaml, preset, bass_extension)
    # Reference-closure gate (fail-closed): the durable baseline shares the
    # same "filters/mixer/pipeline assembled from independent helper calls"
    # shape the program graph does (see the W6 finding I comment on
    # _assert_pipeline_references_closed) — cheap enough to run on every
    # baseline build, and it is what would have caught a dangling mixer/filter
    # name here before it ever reached a live CamillaDSP load.
    _assert_pipeline_references_closed(yaml, preset)

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
        # linearization_shelves / shelf_q make the PR-L2 emission change legible
        # in the journal: a graph written by a build BEFORE 2026-07-27 realized
        # its Layer-1a shelves at CamillaDSP's gain-dependent ``slope: 6`` Q
        # (0.476 at -11 dB), not the Butterworth Q the fit designed and scored
        # them at. Re-emitting the SAME persisted design now realizes the
        # designed curve, so the speaker's treble audibly changes on the first
        # write after upgrading. These two fields date that transition per
        # speaker; ``grep event=active_speaker_baseline_config_written``.
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

    This is the active-crossover analogue of the dumb follower's channel-pick: an
    *endpoint-crossover* graph that runs only **Layer A** (the ``2->N`` split plus
    each driver's crossover / delay / non-positive gain / soft-clip limiter, with
    the tweeter band-limited by its crossover high-pass) on a stereo program the
    **leader already corrected** (Layer B room PEQ + Layer C preference EQ baked
    into the streamed program). It therefore emits **no** program-domain prefix —
    no ``active_baseline_headroom`` gain and no preference-EQ band — because that
    domain belongs to the leader's bake instance.

    The pipeline is ``channel_select (2->2 pick L/R/mono) -> optional
    pair_balance_trim -> split_active_<way>way (2->N) -> per-driver chain``:
    the inter-speaker channel-select runs FIRST (which channel of the pair this
    box plays), THEN the attenuate-only pair trim for this physical member,
    THEN the intra-speaker driver split.
    ``program_channel`` is one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``
    (``left`` / ``right`` / ``mono``); the channel-select mixer is the shared
    ``emit_channel_select_mixer`` primitive, so a follower and a bonded member
    spell the pick identically.

    Like the baseline emitter it keeps the JTS 0 dB volume ceiling, per-driver
    soft-clip limiters, and non-positive per-driver correction gain, and it
    refuses the existing stereo outputd lane as a playback device. It does NOT
    load or reload CamillaDSP — the reconciler (gap 3, a later slice) owns
    pointing the capture at the grouping ring and the apply. ``corrections``
    carries the same commissioned per-driver delay/gain/polarity as the solo
    baseline (hardware truth, role-independent — gap 1), so the relocated Layer A
    is the same protective chain the speaker runs solo.
    """

    preset.validate()
    # Same L0 emit gate as the solo baseline above, for the same reason: a
    # bonded leader's driver domain runs the identical protective chain on the
    # identical drivers, so it inherits the identical declared-floor bound.
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
    if volume_limit_db > 0:
        raise ActiveSpeakerConfigError("volume_limit_db must not exceed 0 dB")
    if limiter_clip_limit_db < -120 or limiter_clip_limit_db > 0:
        raise ActiveSpeakerConfigError(
            "limiter_clip_limit_db must be between -120 and 0 dB"
        )

    safe_corrections = _validated_driver_corrections(preset, corrections)
    bass_extension = _bass_extension_emission(preset, bass_extension_profile)

    # queuelimit rides the same coercion as every other integer knob here.
    # It reaches the YAML through an f-string, so an unvalidated value is the
    # one emitter input that can put arbitrary text into a CamillaDSP field;
    # its siblings were coerced and it was not. Hardening, not a fixed escape:
    # the reachable injection was chased to the volume_limit ceiling and does
    # not escalate past it.
    queuelimit = _positive_int(queuelimit, "queuelimit")
    output_count = _output_count(preset)
    # The ring's width is one of its declaring ends — refuse a shear
    # here rather than let the ioplug attach crash on it. No-op for every
    # ALSA-lane emit (see _assert_ring_playback_width).
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
    # ``safe_corrections`` (a per-driver Gain filter above), so the mixer must
    # stay a no-op inverter — see the docstring on _emit_split_mixer.
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

    # L0 emit gate (fail-closed): the follower runs Layer A (the split + per-driver
    # crossover chain) on the leader's corrected program, so its tweeter output
    # must still carry the crossover / protective high-pass — re-prove it before
    # the graph leaves the emitter.
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

    Stage B splits an active *leader*'s DSP across two CamillaDSP instances. This emits
    the **program** half (camilla#1): Layer B room correction + Layer C
    preference EQ + program headroom, written to a ``File`` sink feeding the
    snapserver pipe (``SNAPFIFO``) so the follower(s) receive a corrected stereo
    wire. The **driver** half — the ``2->N`` split and every per-driver
    crossover / delay / gain / soft-clip limiter (Layer A) — lives in camilla#2
    and is **deliberately absent here**.

    It is a *separate* emit that **bypasses the graph carrier** (exactly like the
    follower's :func:`emit_active_speaker_driver_domain_config`): the carrier
    fence ``eq_on_active_bonded_member`` guards the interactive ``/sound`` EQ
    apply and is untouched by this path. The program assembly is
    :func:`jasper.sound.camilla_yaml.emit_sound_config`'s — reused verbatim with
    a ``File``/pipe sink — so the baked correction is byte-for-byte the
    program graph the speaker already ships. Only the ``# Source:`` provenance
    marker differs: this config carries :data:`ACTIVE_PROGRAM_BAKE_SOURCE` so the
    runtime verifier recognises it as a DAC-less program bake.

    Safety is *by construction*: the playback is a pipe, not a DAC, so no driver
    can be over-driven and the full-range-to-tweeter invariant cannot be
    violated regardless of the saved speaker topology. The runtime verifier's
    matching exemption (:func:`jasper.active_speaker.runtime_contract.classify_camilla_graph`)
    keys on ``devices.playback.type == File`` — never on this marker — so an
    ALSA-sink program graph reaching the DAC stays blocked under a roleful
    topology.

    This emit does NOT load or reload CamillaDSP, and it does NOT wire camilla#1
    into the reconciler — that is a later Stage-B slice. ``out_path`` writes the
    YAML group-readably (0640) for callers that stage it; the default returns the
    text only.
    """

    # Import the snapserver pipe target lazily: it is the canonical camilla#1
    # sink and lives in the grouping reconciler (jasper.multiroom.reconcile),
    # whose module-load chain this read-heavy emitter must not pull eagerly. The
    # sibling jasper.multiroom.leader_config uses the same lazy-import idiom for
    # exactly this constant.
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
    # loud if the upstream marker ever changes shape (a silent miss would ship a
    # bake the verifier can't route to the flat program path).
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
