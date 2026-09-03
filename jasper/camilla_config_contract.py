# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lightweight CamillaDSP config contract shared by DSP config emitters.

Keep this module import-cheap. Socket-activated web surfaces use these
defaults to build and inspect CamillaDSP YAML without pulling NumPy/SciPy
into the combined ``jasper-web`` process.
"""

from __future__ import annotations

import math
import os
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
# The snd-aloop tap ADR-0100 retired. Its ALSA definition is gone, so this name
# no longer resolves on a box; it survives to RECOGNIZE the retired route in a
# graph an unreconciled box still carries, never to emit it.
RETIRED_ALOOP_CAPTURE_DEVICE = "plug:jasper_capture"
# Playback is Ring B, aliased for the same reason capture is aliased to Ring A:
# the ring is the only CamillaDSP -> outputd transport (ADR-0100), so a
# generated correction or sound-profile config must name the lane outputd
# actually reads. Routing a profile anywhere else would take music around
# jasper-outputd while TTS still went through it.
DEFAULT_PLAYBACK_DEVICE = RING_PLAYBACK_DEVICE
# The snd-aloop playback half ADR-0100 retired — the twin of
# RETIRED_ALOOP_CAPTURE_DEVICE, and the key the outputd-capture pairing below is
# still written against. Its ALSA definition is gone too, so the pairing below
# resolves a name that no box can open: the lookup exists to CLASSIFY a graph
# carrying the retired route, never to hand a caller a lane to write.
RETIRED_ALOOP_PLAYBACK_DEVICE = "outputd_content_playback"
ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "outputd_active_content_playback"
DEFAULT_OUTPUTD_CAPTURE_DEVICE = "outputd_content_capture"
ACTIVE_OUTPUTD_CAPTURE_DEVICE = "outputd_active_content_capture"
DEFAULT_CAPTURE_FORMAT = "S32_LE"
# The CamillaDSP→outputd content hop's width on the snd-aloop lanes. S32_LE
# since the wide-output-path program's flip (PR-6,
# captures/PLAN-wide-output-path-2026-08-07.md): CamillaDSP's float math stays
# wide all the way to outputd's i32 program spine, so the ONE deliberate output
# quantization happens at the DAC edge, at the DAC's own declared width. At a
# ≥24-bit edge that floor sits below the DAC's analog noise, so it stops being
# audible at all.
#
# What changed at an S16 edge is WHERE that single narrowing happens, not how
# many there are: before the flip there was already exactly one lossy narrowing
# (CamillaDSP's S16 playback write), and outputd's widen→narrow round trip around
# it was proven bit-exact. The flip MOVES that narrowing downstream of outputd's
# mixing, ducking, and trim, which now do their arithmetic on full-resolution
# content instead of on samples already quantized to 16 bits — which is what makes
# a −18 dB tweeter trim stop costing three bits of program resolution.
#
# Two things must move with this value, and both are derived rather than
# restated: ``deploy/camilladsp/outputd-cutover.yml`` carries it on BOTH ring
# halves (since ADR-0100 the flat startup graph names ``jts_ring_capture`` and
# ``jts_ring_playback``, and the ioplug pins the ring's own geometry), and the
# audio-hardware reconciler emits outputd's matching
# ``JASPER_OUTPUTD_CONTENT_FORMAT`` through
# ``jasper.fanin_coupling.content_lane_format_for_coupling``.
DEFAULT_PLAYBACK_FORMAT = "S32_LE"
# The bonded-leader pipe sink (jasper.sound.camilla_yaml's playback_pipe_path
# axis) and the active-speaker parked graph's /dev/null File sink are pinned
# to THIS format, independently of DEFAULT_PLAYBACK_FORMAT: snapserver's pipe
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

# The post-DSP ALSA transport's two halves, paired. Every ABSENCE is a decision:
# a ring has no outputd capture PCM at all (outputd reads the ring FILE), and
# #2534 deleted the snd-aloop ACTIVE lane's PCM definitions. Adding an entry
# would invent a lane nothing opens; `transport_coherence_report` reads the
# absence as meaningful and owns what each missing pairing MEANS, through
# UNPAIRED_POST_DSP_PLAYBACK_DEVICES below.
#
# The one entry is NOT trimmable on its own: the audio-hardware reconciler
# resolves this map once per pass against RETIRED_ALOOP_PLAYBACK_DEVICE and
# hard-exits 66 when the lookup misses, so deleting it while that gate stands
# parks EVERY box on EVERY reconcile. ADR-0186 rules the gate and this entry
# stay; the gate goes first, or the two go together.
_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE = {
    RETIRED_ALOOP_PLAYBACK_DEVICE: DEFAULT_OUTPUTD_CAPTURE_DEVICE,
}

# Every endpoint a post-DSP CamillaDSP graph can name, paired or not.
POST_DSP_PLAYBACK_DEVICES = frozenset(
    (
        RETIRED_ALOOP_PLAYBACK_DEVICE,
        ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        RING_ACTIVE_PLAYBACK_DEVICE,
    )
)
# PAIRING only — "is there a registered outputd capture for this playback
# device" — never disposition: the two rings get opposite dispositions from the
# same absent pairing, and ``transport_coherence_report`` owns that split.
UNPAIRED_POST_DSP_PLAYBACK_DEVICES = (
    POST_DSP_PLAYBACK_DEVICES - _OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE.keys()
)


def outputd_capture_device_for_playback(playback_device: object) -> str | None:
    """Return outputd's paired capture endpoint for a Camilla playback PCM.

    This is the single vocabulary boundary for the two halves of the post-DSP
    ALSA transport. Callers resolve one playback device and derive its reader;
    they must not independently choose active/passive lane strings.
    """

    return _OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE.get(str(playback_device or ""))


DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048


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


# Sentinel distinguishing "caller did not pass profile_floor → auto-resolve the
# active DAC's codified floor" from an explicit ``profile_floor=None`` ("no
# floor, keep the global default" — the byte-identical contract path the
# emitters' own None-sentinel relies on). Auto-resolution reads the DacProfile
# registry directly, so the floor reaches EVERY live generation path (install.sh
# runtime-safe-graph, the ExecStartPre statefile guards, and jasper-control's
# sound / active-speaker generation) regardless of whether that path happens to
# have outputd.env in its environment — the #27 keystone fix.
class _Unset:
    __slots__ = ()


_UNSET = _Unset()


def _active_camilla_floor(field: str) -> int | None:
    """The active output DAC's declared ``CamillaFloor.<field>``, or None.

    None when the reconciler has resolved no DAC, the profile is unknown, or
    the DAC declares no floor — the caller then keeps the global default, so
    a box whose record is not yet written still generates a config. The
    hardware modules are imported lazily so this contract module stays
    import-cheap for the socket-activated web surfaces that never call it.
    """
    try:
        from jasper.audio_hardware.dac import camilla_floor_for
        from jasper.output_hardware import active_dac_profile_id
    except ImportError:
        return None
    profile_id = active_dac_profile_id()
    if profile_id is None:
        return None
    floor = camilla_floor_for(profile_id)
    if floor is None:
        return None
    return int(getattr(floor, field))


def _lab_override_allows_below_floor(
    env_var: str,
    value: int,
    env: Mapping[str, str],
) -> bool:
    """Return whether an explicit audio-runtime lab override owns ``value``.

    The DacProfile latency floor is the production safety/stability floor. Lab
    tuning may intentionally probe below it, but only when the dedicated
    ``audio_runtime_overrides.json`` artifact carries the same active value.
    This keeps ordinary stale ``outputd.env`` values clamped while allowing the
    generated CamillaDSP config to match the route plan during visible lab work.
    """

    try:
        from jasper.audio_runtime_overrides import (
            load_runtime_overrides,
            runtime_overrides_path,
        )
    except ImportError:
        return False
    overrides = load_runtime_overrides(runtime_overrides_path(env))
    raw = overrides.values().get(env_var)
    try:
        override_value = int(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return override_value == value


def _resolve_camilla_int(
    env_var: str,
    default: int,
    env: Mapping[str, str],
    profile_floor: int | None,
) -> int:
    """Resolve a positive-int CamillaDSP latency knob with floor precedence.

    Precedence: max(explicit operator env, active DacProfile floor) > global
    default. ``profile_floor`` is the active DAC's codified floor value (None
    when the DAC declares no floor — the non-breaking path that keeps the
    global default). An explicit operator override can raise latency above the
    profile floor for testing, but a stale or over-aggressive value below the
    measured floor is clamped back up. That makes the DacProfile value a true
    safety/stability floor, not only a fresh-box default.

    Returns ``default`` (or ``profile_floor`` when given) when the var is unset
    OR malformed (non-int, zero, negative) — a bad override must never produce a
    config that won't load, so it degrades rather than raising. With the env var
    unset and no profile floor the result is byte-identical to the literal
    default, so threading these through the emitters does not change any emitted
    YAML unless an operator opts in or the active DAC declares a floor. Read at
    emitter-call time so a systemd EnvironmentFile change takes effect on the
    next config regeneration without a code edit.
    """
    fallback = default if profile_floor is None else profile_floor
    raw = str(env.get(env_var, "")).strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    if value <= 0:
        return fallback
    if profile_floor is not None and value < profile_floor:
        if _lab_override_allows_below_floor(env_var, value, env):
            return value
        return profile_floor
    return value


def resolve_camilla_chunksize(
    env: Mapping[str, str] | None = None,
    profile_floor: int | None | _Unset = _UNSET,
) -> int:
    """CamillaDSP ``chunksize`` — ``JASPER_CAMILLA_CHUNKSIZE`` or the active
    DAC's profile floor or ``DEFAULT_CHUNKSIZE`` (1024).

    ``profile_floor`` left unset (the live-emitter default) auto-resolves the
    active output DAC profile's codified floor from the registry, so every live
    generation path gets the floor with max(operator-env, profile-floor) >
    global precedence. Pass ``profile_floor=None`` explicitly to force the
    no-floor (global-default) path — the byte-identical contract used by tests
    and by the pre-#27 explicit-literal call. See :func:`_resolve_camilla_int`.
    """
    if isinstance(profile_floor, _Unset):
        profile_floor = _active_camilla_floor("chunksize")
    return _resolve_camilla_int(
        "JASPER_CAMILLA_CHUNKSIZE", DEFAULT_CHUNKSIZE,
        os.environ if env is None else env,
        profile_floor,
    )


def resolve_camilla_target_level(
    env: Mapping[str, str] | None = None,
    profile_floor: int | None | _Unset = _UNSET,
) -> int:
    """CamillaDSP ``target_level`` — ``JASPER_CAMILLA_TARGET_LEVEL`` or the
    active DAC's profile floor or ``DEFAULT_TARGET_LEVEL`` (2048).

    ``profile_floor`` left unset (the live-emitter default) auto-resolves the
    active output DAC profile's codified floor from the registry. Pass
    ``profile_floor=None`` explicitly to force the no-floor (global-default)
    path. See :func:`resolve_camilla_chunksize` and :func:`_resolve_camilla_int`.
    """
    if isinstance(profile_floor, _Unset):
        profile_floor = _active_camilla_floor("target_level")
    return _resolve_camilla_int(
        "JASPER_CAMILLA_TARGET_LEVEL", DEFAULT_TARGET_LEVEL,
        os.environ if env is None else env,
        profile_floor,
    )


def resolve_camilla_latency_for_devices(
    *,
    capture_device: str,
    playback_device: str | None,
    chunksize: int | None = None,
    target_level: int | None = None,
) -> tuple[int, int]:
    """The ``(chunksize, target_level)`` a graph between these devices needs.

    A caller value passed here is returned untouched — the lab seam every
    emitter already offers, and the half a caller leaves ``None`` is the only
    half resolved. Filling both halves here rather than at each emitter keeps
    "an explicit value wins" spelled once.

    WHY A DEVICE DECIDES THIS. The DacProfile ``LatencyFloor`` sizes the DAC's
    OWN buffer, and jasper-outputd is what feeds that buffer. CamillaDSP does
    not: since ADR-0100 its chunk crosses THE RING, whose capacity is
    ``RING_SLOT_FRAMES x DEFAULT_FANIN_RING_SLOTS`` frames — a compile-time
    constant of the fan-in writer and the ioplug, identical on every box and
    unrelated to which DAC is fitted. A chunk larger than that cannot be
    negotiated at all: CamillaDSP exits with "Trying to set avail_min to N, must
    be smaller than or equal to device buffer size of 256" and systemd
    restart-loops it, which is silent deafness (AGENTS.md #6).

    So a ring end CLAMPS the resolved chunk to what the ring can carry. It does
    not replace the box's floor with the ring's certified geometry: jts.local
    runs the Apple floor's 256 across this same ring healthily, so a floor that
    already fits is the box's own tuning and is passed through untouched. Only a
    floor the transport physically cannot serve is brought down — the InnoMaker
    floor's 1024, which is what crash-looped jts4. Whether every ring graph
    should instead run the certified ``RING_CAMILLA_*`` pair is a deliberate
    retune of healthy boxes, not this clamp; the armed active path and the
    fresh-install boot graph already pass that pair explicitly.

    ``target_level`` is not bounded by the ring's capacity — jts.local carries
    1536 over a 256-frame ring with no complaint — but it IS bounded by
    CamillaDSP relative to the chunk, so a clamped chunk drags its ceiling down
    with it and the pair scales together. See the comment at the clamp.

    ``playback_device=None`` is a CLOCKLESS sink (a ``File`` — the bonded
    leader's snapserver FIFO, the parked graph's ``/dev/null``). It declares no
    ALSA buffer, so a ring capture is then the only ALSA end and it governs.

    A non-ring ALSA playback device keeps the box's floor whole even when
    capture is Ring A, because that sink's own hardware buffer is what the
    process must feed (pinned by the ALSA-lane control in
    ``test_ring_reemit_carries_the_certified_ring_chunk_and_target``).
    """

    resolved_target = target_level is None
    if target_level is None:
        target_level = resolve_camilla_target_level()
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
        governing_device = (
            capture_device if playback_device is None else playback_device
        )
        if governing_device in RING_PCM_DEVICES:
            capacity = ring_capacity_frames()
            if chunksize > capacity:
                # THE PAIR SCALES TOGETHER. CamillaDSP bounds target_level at
                # `chunksize x (queuelimit + 4)` — measured against 4.1.3 on
                # jts4, exact across chunk 128/256/512 and queuelimit 1/2/4 —
                # so the ceiling falls with the chunk. Clamping the chunk alone
                # pushed jts4's floor-declared target of 4096 over the new
                # 2048 ceiling and swapped one fatal config for another
                # ("target_level cannot be larger than 2048", same crash loop).
                #
                # Scaling by the same ratio keeps the pair valid without
                # encoding CamillaDSP's formula here: the bound is proportional
                # to chunksize, so a pair that fit before fits after. It also
                # preserves the RELATIONSHIP the DacProfile declared rather
                # than substituting a number of our own.
                if resolved_target:
                    target_level = max(1, target_level * capacity // chunksize)
                chunksize = capacity
    return chunksize, target_level


def resolve_enable_rate_adjust(playback_device: str | None) -> bool:
    """Whether CamillaDSP's rate adjuster can steer THIS graph's sink.

    A property of the SINK, never of the graph's role. False for ``None``, the
    clockless ``File`` sink :func:`resolve_camilla_latency_for_devices` reads
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

    0 dB is the project-wide hard software ceiling (see AGENTS.md
    "Renderer architecture"): generated configs must never let the main
    fader boost above full scale. Mirrors the
    guard in ``jasper.active_speaker.camilla_yaml`` so every JTS config
    emitter rejects a positive limit at build time instead of shipping a
    loud-output hazard to CamillaDSP. Raises ``ValueError`` — config
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
    (``jasper.correction.peq.total_max_boost_db``). Any object exposing a
    numeric ``.gain`` is accepted — the correction ``PEQ`` is structurally
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

    ``capture_format`` / ``playback_format`` join ``*_device`` / ``*_channels``
    in the per-lane subset because the ring's width gate
    (``jasper.fanin.coupling_reconcile.ring_edge_width_ready``) has to judge
    device, channels and format off ONE snapshot of the loaded graph — reading
    the same file three times through the single-field reader would let the
    three answers come from three different revisions of it. Keys are omitted
    entirely when the block declares no such field, exactly like the others, so
    every existing caller is unaffected.
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
            if key == "format":
                if value:
                    result[f"{nested}_format"] = value
                continue
            if key == "channels":
                try:
                    result[f"{nested}_channels"] = int(value)
                except ValueError:
                    continue

    return result


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


def read_camilla_device_field(
    config_path: str | Path | None, block: str, field: str
) -> str | None:
    """One field from ``devices.<block>`` of a CamillaDSP config file, or None.

    Tiny indent-aware scan (no YAML dep): find the 2-space device block, return
    its first 4-space ``field:`` value with quotes stripped. Deliberately
    narrower than :func:`parse_camilla_devices_config`, which returns a fixed
    observability subset — this reads an arbitrary named field (``type``,
    ``filename``) that no fixed subset has to grow a key for. One field per
    call is one FILE READ per call, so a caller that needs several fields of
    one graph revision wants the subset parser over a single snapshot instead
    (which is why ``format`` moved into that subset).

    The SSOT for that scan: ``jasper.cli.doctor.audio_runtime._loaded_device_field``
    delegates here, and the wiring test that pins the shipped flat-cutover seed
    to :data:`DEFAULT_PLAYBACK_FORMAT` reads it the same way.
    """

    if not config_path:
        return None
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError:
        return None
    target_block = f"{block}:"
    target_field = f"{field}:"
    in_block = False
    for raw in text.splitlines():
        is_2space = raw.startswith("  ") and not raw.startswith("   ")
        if is_2space and raw.strip() == target_block:
            in_block = True
            continue
        if in_block:
            if raw.startswith("    ") and raw.strip().startswith(target_field):
                return raw.split(":", 1)[1].strip().strip("\"'")
            # A sibling 2-space key (playback:/resampler:/...) or any dedent ends
            # the block — never read a sibling block's field.
            if is_2space or (raw[:1] not in (" ", "") and raw.strip()):
                in_block = False
    return None


