# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight CamillaDSP config contract shared by DSP config emitters.

Keep this module import-cheap. Socket-activated web surfaces use these
defaults to build and inspect CamillaDSP YAML without pulling NumPy/SciPy
into the combined ``jasper-web`` process.

Vocabulary only. Resolution that reads hardware, the environment or the lab
override artifact lives above, in :mod:`jasper.camilla_latency`.
"""

from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Any, Mapping

from jasper.biquad import RESPONSE_SAMPLE_RATE_HZ
from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_PCM_DEVICES,
    RING_PLAYBACK_DEVICE,
)


# Capture is Ring A, aliased rather than respelled so the emitters' no-kwargs
# answer and the ring's own device name cannot drift apart. The ring is the only
# fan-in -> CamillaDSP transport (ADR-0100), so an emit that receives no coupling
# kwargs must still name a lane fan-in actually writes.
DEFAULT_CAPTURE_DEVICE = RING_CAPTURE_DEVICE
# Playback is Ring B, aliased for the same reason capture is aliased to Ring A:
# the ring is the only CamillaDSP -> outputd transport (ADR-0100), so a
# generated correction or sound-profile config must name the lane outputd
# actually reads. Routing a profile anywhere else would take music around
# jasper-outputd while TTS still went through it.
DEFAULT_PLAYBACK_DEVICE = RING_PLAYBACK_DEVICE
ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "outputd_active_content_playback"
DEFAULT_CAPTURE_FORMAT = "S32_LE"
# The bonded-leader pipe sink (jasper.sound.camilla_yaml's playback_pipe_path
# axis) and the active-speaker parked graph's /dev/null File sink are pinned
# to THIS format, independently of
# :data:`~jasper.fanin_coupling.DEFAULT_PLAYBACK_FORMAT`: snapserver's pipe
# source is a fixed-format wire contract —
# jasper.multiroom.reconcile_plan.snapserver_argv hardcodes `sampleformat=
# 48000:16:2` — so a future DEFAULT_PLAYBACK_FORMAT widening (the
# wide-output-path program) must not also widen the bytes snapserver reads
# off the FIFO. Pipe/File sinks are a different axis from the ALSA loopback
# lane's format.
DEFAULT_PIPE_SINK_FORMAT = "S16_LE"
# Canonical live pair-balance Gain identity for the active driver-domain graph.
# The emitter and runtime patcher share this lightweight vocabulary; the safety
# verifier deliberately retains an independent private literal and re-proves
# compatibility through the driver-domain round-trip tests.
DRIVER_DOMAIN_PAIR_TRIM_FILTER = "pair_balance_trim"

# Every endpoint a post-DSP CamillaDSP graph can name. NONE of them has an
# outputd capture PCM: outputd reads a ring FILE, #2534 deleted the snd-aloop
# ACTIVE lane's PCM definitions, and ADR-0262 retired the snd-aloop pair
# outright. Membership is not a disposition — the two rings get opposite ones
# from the same absent capture, and ``transport_coherence_report`` owns that
# split.
POST_DSP_PLAYBACK_DEVICES = frozenset(
    (
        ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
)


DEFAULT_SAMPLE_RATE = RESPONSE_SAMPLE_RATE_HZ
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048
#: CamillaDSP frames queued ahead of an ordinary (non-ring) ALSA sink. A
#: ring-ended graph takes ``fanin_coupling.RING_CAMILLA_QUEUELIMIT`` instead.
DEFAULT_QUEUELIMIT = 4
#: camilla#1's websocket port with nothing overriding it.
DEFAULT_CAMILLA_PORT = 1234


def resolve_enable_rate_adjust(playback_device: str | None) -> bool:
    """Whether CamillaDSP's rate adjuster can steer THIS graph's sink.

    A property of the SINK, never of the graph's role. False for ``None``, the
    clockless ``File`` sink
    :func:`~jasper.camilla_latency.resolve_camilla_latency_for_devices` reads
    the same way, because it has no output clock to follow. False for a ring
    PCM (:data:`~jasper.fanin_coupling.RING_PCM_DEVICES`) because it is an
    ioplug: alsa-lib reports card -1 for every ioplug, so CamillaDSP builds no
    HCtl and has no mixer element to actuate, and a requested ``true`` would
    only echo back on ``capture_status.rate_adjust`` while nothing moved. True
    for an ordinary ALSA sink, whose own clock the adjuster can track. See
    ADR-0218.
    """

    return playback_device is not None and playback_device not in RING_PCM_DEVICES


# CamillaDSP defaults the main fader's maximum to +50 dB when omitted.
# JTS treats 0 dB as the hard software ceiling; source/headroom logic
# should attenuate below this, never boost above full scale.
DEFAULT_VOLUME_LIMIT_DB = 0.0


def ensure_volume_limit_db(value: float) -> float:
    """Validate a ``devices.volume_limit`` value against the JTS safety
    ceiling and return it as a float.

    0 dB is the project-wide hard software ceiling (AGENTS.md
    non-negotiable 1): generated configs must never let the main fader
    boost above full scale. Both JTS emitter families (``jasper.sound``
    and ``jasper.active_speaker``) route their build-time refusal through
    here, so the threshold has one home. Raises ``ValueError`` — config
    generation is a programming/caller error surface, not a runtime
    degrade-gracefully path.
    """
    try:
        out = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError("volume_limit_db must be numeric") from e
    if not math.isfinite(out):
        raise ValueError("volume_limit_db must be finite")
    if out > 0:
        raise ValueError("volume_limit_db must not exceed 0 dB")
    return out


class VolumeLimitViolation(ValueError):
    """A CamillaDSP graph write that breaks the JTS hearing ceiling.

    ``code`` is the stable machine name for the refusal:
    ``volume_limit_missing``, ``volume_limit_positive``, or
    ``patch_touches_devices`` (a partial-config patch may write running-filter
    parameters, never the ``devices`` block the ceiling lives in).
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def check_volume_limit(text: str) -> None:
    """Raise :class:`VolumeLimitViolation` unless a CamillaDSP graph text
    carries ``devices.volume_limit`` at or below the 0 dB ceiling.

    The one rule behind AGENTS.md non-negotiable 1, asked by every writer
    that installs a graph. A missing key is as unsafe as a positive one:
    CamillaDSP defaults the main fader's maximum to +50 dB when it is
    omitted, and its own ``--check`` accepts both. Ambiguous configs fail
    closed through :func:`parse_camilla_devices_config`, which omits the
    limit when duplicate keys make it unreadable. See ADR-0313.
    """

    limit = parse_camilla_devices_config(text).get("volume_limit")
    if limit is None:
        raise VolumeLimitViolation(
            "config omits devices.volume_limit; CamillaDSP would default "
            "the main fader ceiling above 0 dB",
            code="volume_limit_missing",
        )
    try:
        ensure_volume_limit_db(limit)
    except ValueError as e:
        raise VolumeLimitViolation(
            f"devices.volume_limit={limit:.1f} dB exceeds the 0 dB JTS "
            "safety ceiling",
            code="volume_limit_positive",
        ) from e


def _clean_yaml_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _yaml_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_camilla_devices_config(text: str) -> dict[str, Any]:
    """Return the small ``devices:`` subset JTS needs for observability.

    Generated Camilla configs in this repo use a stable, simple YAML
    shape. Keeping this parser dependency-free preserves the existing
    no-PyYAML runtime contract while still giving dashboards and health
    checks one shared way to inspect samplerate/chunksize/target level
    and ALSA endpoints. Ambiguous duplicate ``devices`` or direct
    ``volume_limit`` keys omit the limit so safety callers fail closed.

    ``queuelimit`` and ``enable_rate_adjust`` join the direct subset because they
    are half the RING's CamillaDSP-side contract (queue 1 / rate_adjust off — a
    blocking slot handshake gives the rate controller nothing to adjust to), and
    a drift pin that read only chunk/target would have called a seed correct with
    either of them moved. ``enable_rate_adjust`` is the one BOOL here; anything
    that is not ``true``/``false`` omits the key rather than guessing, like every
    other field.

    ``*_format``, ``*_type`` and ``*_filename`` join ``*_device`` /
    ``*_channels`` because the callers that judge a lane judge several of its
    fields at once — the ring's width gate
    (``jasper.fanin.ring_readiness.ring_edge_width_ready``) and the doctor's
    coupling and playback-format checks — and one file read per field lets
    those answers come from different revisions of it. A key is omitted when
    the block declares no such field, exactly like the others, so every
    existing caller is unaffected.
    """

    text = textwrap.dedent(text)
    top_level_devices = 0
    for raw_line in text.splitlines():
        if raw_line.startswith((" ", "\t")):
            continue
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        raw_key = stripped.split(":", 1)[0].strip()
        if (
            len(raw_key) >= 2
            and raw_key[0] == raw_key[-1]
            and raw_key[0] in {"'", '"'}
        ):
            raw_key = raw_key[1:-1]
        if raw_key == "devices":
            top_level_devices += 1
    if top_level_devices != 1:
        return {}

    result: dict[str, Any] = {}
    in_devices = False
    devices_indent = 0
    direct_indent: int | None = None
    nested: str | None = None
    nested_indent = 0
    volume_limit_count = 0

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _yaml_indent(raw_line)

        if not in_devices:
            if stripped == "devices:":
                in_devices = True
                devices_indent = indent
            continue

        if indent <= devices_indent and raw_line.lstrip() == raw_line:
            break

        if indent <= devices_indent:
            break

        if direct_indent is None:
            direct_indent = indent
        is_direct = indent == direct_indent

        if nested is not None and indent <= nested_indent:
            nested = None

        if stripped.endswith(":"):
            key = stripped[:-1].strip()
            if is_direct and key in {"capture", "playback"}:
                nested = key
                nested_indent = indent
            continue

        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _clean_yaml_scalar(raw_value)

        if is_direct and key in {
            "samplerate",
            "chunksize",
            "target_level",
            "queuelimit",
        }:
            try:
                result[key] = int(value)
            except ValueError:
                continue
            continue

        if is_direct and key == "enable_rate_adjust":
            lowered = value.strip().lower()
            if lowered in {"true", "false"}:
                result[key] = lowered == "true"
            continue

        if is_direct and key == "volume_limit":
            volume_limit_count += 1
            if volume_limit_count > 1:
                result.pop("volume_limit", None)
                continue
            try:
                parsed_limit = float(value)
            except ValueError:
                continue
            if math.isfinite(parsed_limit):
                result[key] = parsed_limit
            continue

        if nested in {"capture", "playback"} and indent > nested_indent:
            if key == "device":
                result[f"{nested}_device"] = value
                continue
            if key in {"format", "type", "filename"}:
                if value:
                    result[f"{nested}_{key}"] = value
                continue
            if key == "channels":
                try:
                    result[f"{nested}_channels"] = int(value)
                except ValueError:
                    continue

    return result


def devices_playback_is_pipe(devices: Mapping[str, Any], fifo: str) -> bool:
    """True when a parsed ``devices`` subset's playback lane is a ``File``
    sink writing ``fifo`` — the bonded-leader pipe.

    The FILENAME is compared exactly (the parser has already stripped its
    quotes), not just the type: any other ``File`` sink — the parked graph's
    ``/dev/null``, a stale local pipe — is not the bond.
    """

    return (
        devices.get("playback_type") == "File"
        and devices.get("playback_filename") == fifo
    )


def playback_is_pipe(text: str, fifo: str) -> bool:
    """:func:`devices_playback_is_pipe` for config text rather than a parsed
    ``devices`` block."""
    return devices_playback_is_pipe(parse_camilla_devices_config(text), fifo)


def read_camilla_devices_config(path: str | Path | None) -> dict[str, Any] | None:
    """Best-effort file reader for :func:`parse_camilla_devices_config`."""

    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_camilla_devices_config(text)
    return parsed or None
