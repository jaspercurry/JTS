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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from jasper.fanin_coupling import (
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_PCM_DEVICES,
    RING_PLAYBACK_DEVICE,
    ring_capacity_frames,
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
# jasper.multiroom.reconcile.snapserver_argv hardcodes `sampleformat=
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


DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048
#: camilla#1's websocket port with nothing overriding it.
DEFAULT_CAMILLA_PORT = 1234


@dataclass(frozen=True)
class CamillaFloor:
    """The lowest CamillaDSP ``(chunksize, target_level)`` a box runs xrun-free.

    Declared per DacProfile because a box is identified by its DAC, but the
    numbers are CamillaDSP's own buffering, not the DAC's: since ADR-0100 the
    chunk crosses the SHM ring, whose capacity is a transport constant, so a
    chunk the ring cannot open is refused at declaration rather than clamped at
    emit time. ``target_level`` is the resampler's steady-state fill: it must be
    >= 4x ``chunksize`` so the adjuster has headroom, and the ring's capacity
    does not bound it.
    """

    chunksize: int
    target_level: int

    def __post_init__(self) -> None:
        for name, value in (
            ("chunksize", self.chunksize),
            ("target_level", self.target_level),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        if self.target_level < 4 * self.chunksize:
            raise ValueError(
                f"target_level must be >= 4 x chunksize ({4 * self.chunksize}), "
                f"got {self.target_level}"
            )
        capacity = ring_capacity_frames()
        if self.chunksize > capacity:
            raise ValueError(
                f"chunksize {self.chunksize} exceeds the ring's {capacity}-frame "
                "capacity; CamillaDSP could not open the ring with it"
            )


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


@dataclass(frozen=True)
class PeqFilter:
    """Import-cheap representation of a CamillaDSP peaking EQ."""

    freq: float
    q: float
    gain: float


def total_positive_boost_db(filters: Iterable[PeqFilter]) -> float:
    """Worst-case additive boost (dB) across a set of peaking filters.

    The sum of positive gains is an upper bound on the combined response
    peak (overlapping boosts at one frequency add), so attenuating a signal
    by this much guarantees the corrected response cannot exceed unity. This
    is the one canonical definition of "how much can these boosts clip",
    shared by the room-correction headroom trim
    (``jasper.sound.camilla_yaml``) and the PEQ boost-cap check
    (``jasper.audio_measurement.peq.total_max_boost_db``). Any object exposing a
    numeric ``.gain`` is accepted — the designer's ``PEQ`` is structurally
    compatible with ``PeqFilter`` here.
    """
    return max(0.0, sum(f.gain for f in filters if f.gain > 0.0))


# Below the simplest |gain| a preference filter is considered "active" — a
# tiny shelf/peaking gain rounds to a no-op and is dropped before emission.
FILTER_EPSILON_DB = 0.05

# Cut/notch biquads shape the response without a user gain term. They are
# "active" by virtue of being enabled, not by a non-zero gain — see
# FilterSpec.active(). Highpass/Lowpass protect against rumble / tame top
# end; Notch is a surgical gain-less cut.
GAINLESS_BIQUAD_TYPES = frozenset({"Highpass", "Lowpass", "Notch"})

# The ONE steepness every Lowshelf/Highshelf in this codebase is both MODELLED
# at and EMITTED at: the Butterworth (non-resonant, no-overshoot) shelf Q.
#
# It is a single constant on purpose. Every evaluator that draws or scores a
# shelf hardcodes this Q -- jasper.sound.profile._biquad_coeffs (the /sound/
# preview), deploy/assets/sound-profile/js/eq-math.js (its browser twin), and
# jasper.active_speaker.linearization_fit (the fit engine's residual/realization
# gate). None of them reads a per-band steepness, so a per-band steepness is not
# expressible: a shelf emitted at any other Q would be a filter no evaluator in
# this system can see, which is exactly the PR-L2 defect (2026-07-27).
#
# CamillaDSP's ``slope: 6.0`` is NOT Butterworth, despite reading like the
# familiar 6 dB/octave figure. CamillaDSP's advanced shelf takes S = slope/12
# and derives
#     Q = 1 / sqrt((A + 1/A) * (1/S - 1) + 2),   A = 10**(gain/40)
# (RBJ Audio EQ Cookbook; CamillaDSP src/filters/biquad.rs). Butterworth is
# S = 1, i.e. ``slope: 12`` -- pinned by CamillaDSP's own ``lowshelf_slope_vs_q``
# test, which asserts ``slope: 12.0`` and ``q: FRAC_1_SQRT_2`` produce the same
# coefficients. At ``slope: 6`` the realized Q collapses with gain (0.476 at
# -11 dB) and the realized curve missed the modelled one by up to 1.7 dB.
#
# Emitting ``q`` rather than ``slope: 12`` is deliberate: the number in the
# emitted YAML is then literally the number the evaluators use, and unlike
# ``slope`` its meaning does not depend on the band's gain.
#
# If a per-band shelf steepness is ever genuinely wanted, the MODEL must gain
# the parameter in the SAME change. A steepness the evaluators do not read is
# the bug this constant exists to prevent.
SHELF_Q: float = 1.0 / math.sqrt(2.0)

# Decimals used when spelling SHELF_Q into CamillaDSP YAML. The shared 4-decimal
# ``camilla_emit.fmt`` is right for Hz / dB / ms but leaves 0.7071 -- a 1e-5
# relative Q error, worth ~5e-5 dB of realized-vs-modelled mismatch. Seven
# decimals put the emitted filter within ~1.3e-7 dB of the model, i.e. inside
# the PEQ parity suite's 1e-6 dB tolerance, so "emitted == modelled" can be
# asserted as an equality rather than an approximation.
SHELF_Q_EMIT_DECIMALS = 7


@dataclass(frozen=True)
class FilterSpec:
    """A bounded CamillaDSP-friendly filter definition (preference EQ band).

    The program-domain (stereo) DSP contract type, sibling to
    :class:`PeqFilter`. The sound model (``jasper.sound.profile``) builds
    these from a ``SoundProfile``; the shared stereo-prefix builder
    (``jasper.camilla_stereo_prefix``) emits them — so this lives in the
    neutral contract layer, importable by both the sound and active-speaker
    emitters without a cross-dependency.

    ``q`` carries the Q-parameterised types only (Peaking / Highpass / Lowpass /
    Notch). Shelves carry NO steepness field: every shelf is emitted and
    modelled at :data:`SHELF_Q` -- see that constant for why a per-band shelf
    steepness is deliberately not expressible here.
    """

    name: str
    biquad_type: str
    freq: float
    gain: float
    q: float | None = None

    def active(self) -> bool:
        if self.biquad_type in GAINLESS_BIQUAD_TYPES:
            return True
        return abs(self.gain) >= FILTER_EPSILON_DB


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
