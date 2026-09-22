# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from jasper.camilla_config_contract import DEFAULT_CAPTURE_FORMAT, resolve_enable_rate_adjust
from jasper.camilla_latency import resolve_camilla_latency_for_devices
from jasper.fanin_coupling import (
    DEFAULT_PLAYBACK_FORMAT,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAMILLA_GEOMETRY,
    RING_CAPTURE_DEVICE,
    RING_PCM_DEVICES,
    resolve_ring_wire,
)

from ..profile import ActiveSpeakerConfigError

# The ring layout's channel accept-set (``jasper_ring::Geometry::validate_self``
# and the C ioplug's ``JTS_RING_MIN_CHANNELS`` / ``MAX_RING_CHANNELS``). Spelled
# here so the topology side refuses a width the transport could not carry
# instead of deferring it to an attach failure.
MIN_RING_CHANNELS = 2
MAX_RING_CHANNELS = 8

FORBIDDEN_ACTIVE_PLAYBACK_TOKENS = (
    "jasper_out",
    # The retired snd-aloop stereo lane by its own name: an old asound.conf can
    # still resolve these on a box that has not reconciled since the retirement.
    "outputd_content_playback",
    "outputd_content_capture",
    # The full-range STEREO ring: pointing an active emitter at it would put
    # POST-crossover per-driver audio on a full-range path. The ACTIVE ring
    # (``jts_ring_active_playback``) is the legal target and is deliberately NOT
    # here — the names are chosen so this case-insensitive SUBSTRING test
    # separates them ("jts_ring_playback" is not a substring of the other).
    "jts_ring_playback",
)


def _yaml_string(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActiveSpeakerConfigError(f"{field_name} is required")
    out = value.strip()
    if any(ch in out for ch in ('"', "\n", "\r")):
        raise ActiveSpeakerConfigError(f"{field_name} contains unsafe YAML characters")
    return out


def forbidden_playback_token(playback_device: str | None) -> str | None:
    """The FORBIDDEN_ACTIVE_PLAYBACK_TOKENS entry ``playback_device`` names, or
    None. Case-insensitive substring, and ``None``/empty is not forbidden."""
    if not playback_device:
        return None
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

    return RING_CAPTURE_DEVICE


@dataclass(frozen=True)
class ActiveEmitDevices:
    """The whole CamillaDSP ``devices:`` block an active emit against a sink needs.

    BOTH HALVES in one object, because the ring coupling is end-to-end: a graph
    naming the ring on one side and the snd-aloop tap on the other goes silent,
    and a struct carrying only the sink is one a caller can forward half of.
    Every field maps 1:1 onto an ``emit_active_speaker_baseline_config``
    parameter of the same name.

    ``chunksize``/``target_level``/``queuelimit`` are ``None`` for a sink with
    no opinion — the emitter's "resolve it at emit time" contract.
    """

    capture_device: str
    capture_format: str
    playback_format: str
    chunksize: int | None
    target_level: int | None
    queuelimit: int | None
    enable_rate_adjust: bool


def active_emit_devices(
    playback_device: str, *, topology: Any = None
) -> ActiveEmitDevices:
    """The device block an active emit against ``playback_device`` needs, in ONE
    derivation.

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
    ``jasper.fanin.ring_readiness.ring_edge_width_ready`` proves the two ends
    agree per ring at the arm.
    """

    if playback_device not in RING_PCM_DEVICES:
        return ActiveEmitDevices(
            capture_device=capture_device_for_playback(playback_device),
            capture_format=DEFAULT_CAPTURE_FORMAT,
            playback_format=DEFAULT_PLAYBACK_FORMAT,
            chunksize=None,
            target_level=None,
            queuelimit=None,
            enable_rate_adjust=resolve_enable_rate_adjust(playback_device),
        )
    try:
        wire_format = resolve_ring_wire(topology).sample_format
    except ValueError as exc:
        raise ActiveSpeakerConfigError(str(exc)) from exc
    return ActiveEmitDevices(
        capture_device=capture_device_for_playback(playback_device),
        capture_format=wire_format,
        playback_format=wire_format,
        **RING_CAMILLA_GEOMETRY,
    )


def _finite_float(value: Any, field_name: str) -> float:
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


def _camilla_latency(
    capture_device: str,
    playback_device: str | None,
    chunksize: int | None,
    target_level: int | None,
    queuelimit: int | None,
) -> tuple[int, int, int]:
    """Resolve the three latency knobs, then coerce them like every other
    integer knob: each reaches the YAML through an f-string."""
    chunksize, target_level, queuelimit = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=playback_device,
        chunksize=chunksize,
        target_level=target_level,
        queuelimit=queuelimit,
    )
    return (
        _positive_int(chunksize, "chunksize"),
        _positive_int(target_level, "target_level"),
        _positive_int(queuelimit, "queuelimit"),
    )
