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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


# Defaults match the outputd topology. Generated correction and
# sound-profile configs must keep Camilla's playback target on the
# post-DSP outputd loopback lane; otherwise applying a profile would
# route music around jasper-outputd while TTS still uses outputd.
DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"
DEFAULT_PLAYBACK_DEVICE = "outputd_content_playback"
ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "outputd_active_content_playback"
DEFAULT_OUTPUTD_CAPTURE_DEVICE = "outputd_content_capture"
ACTIVE_OUTPUTD_CAPTURE_DEVICE = "outputd_active_content_capture"
DEFAULT_CAPTURE_FORMAT = "S32_LE"
DEFAULT_PLAYBACK_FORMAT = "S16_LE"

_OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE = {
    DEFAULT_PLAYBACK_DEVICE: DEFAULT_OUTPUTD_CAPTURE_DEVICE,
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE: ACTIVE_OUTPUTD_CAPTURE_DEVICE,
}


def outputd_capture_device_for_playback(playback_device: object) -> str | None:
    """Return outputd's paired capture endpoint for a Camilla playback PCM.

    This is the single vocabulary boundary for the two halves of the post-DSP
    ALSA transport. Callers resolve one playback device and derive its reader;
    they must not independently choose active/passive lane strings.
    """

    return _OUTPUTD_CAPTURE_BY_PLAYBACK_DEVICE.get(str(playback_device or ""))


DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE_FORMAT = "S32_LE"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048


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
    """Resolve the active output DAC profile's codified CamillaDSP floor field.

    Reads the resolved output-hardware state the audio-hardware reconciler
    writes (``/run/jasper-output-hardware/output_hardware.json``, overridable
    via ``JASPER_OUTPUT_HARDWARE_STATE_PATH``) — the SAME profile resolution
    ``jasper.output_hardware`` / the reconciler use to pick a profile id — and
    returns that profile's ``LatencyFloor.<field>`` (``camilla_chunksize`` or
    ``camilla_target_level``), or ``None`` when the state is unreadable, the
    profile is unknown, or the DAC declares no floor. Best-effort and
    env-independent on purpose: a fresh box reproduces the tuned floor with no
    per-user config, and a box whose state file is not yet written simply keeps
    the global default rather than failing config generation. Import of the
    hardware modules is deferred so this contract module stays import-cheap for
    the socket-activated web surfaces that never call it.
    """
    try:
        from jasper.audio_hardware.dac import latency_floor_for
        from jasper.output_hardware import load_state
    except ImportError:
        return None
    # load_state is itself fail-soft (OSError / JSONDecodeError → None); it does
    # not raise for a missing or malformed state file. No floor when the state is
    # unreadable or the profile is unknown / declares no floor.
    state = load_state()
    if state is None or not state.profile_id:
        return None
    floor = latency_floor_for(state.profile_id)
    if floor is None:
        return None
    return getattr(floor, field, None)


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
        profile_floor = _active_camilla_floor("camilla_chunksize")
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
        profile_floor = _active_camilla_floor("camilla_target_level")
    return _resolve_camilla_int(
        "JASPER_CAMILLA_TARGET_LEVEL", DEFAULT_TARGET_LEVEL,
        os.environ if env is None else env,
        profile_floor,
    )
# CamillaDSP defaults the main fader's maximum to +50 dB when omitted.
# JTS treats 0 dB as the hard software ceiling; source/headroom logic
# should attenuate below this, never boost above full scale.
DEFAULT_VOLUME_LIMIT_DB = 0.0


def ensure_volume_limit_db(value: float) -> float:
    """Validate a ``devices.volume_limit`` value against the JTS safety
    ceiling and return it as a float.

    0 dB is the project-wide hard software ceiling (see AGENTS.md
    "Renderer architecture" / docs/HANDOFF-volume.md): generated configs
    must never let the main fader boost above full scale. Mirrors the
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


# --- Lean-lane File-capture resampler (CamillaDSP v4 object schema) ---
# A File (named-pipe) capture has no hardware clock, so enable_rate_adjust can
# only discipline it by steering an ASYNC resampler's ratio (rate-adjust
# "method 2"). The deployed CamillaDSP is v4.x, whose resampler is an OBJECT
# under "devices": a "resampler" mapping carrying an AsyncSinc kind and a
# Balanced profile (see file_capture_resampler_yaml below for the emitted YAML).
# The pre-v2 scalar form (`resampler_type: BalancedAsync`) is rejected by the
# v4 parser, so emitters MUST use the object form. AsyncSinc / Balanced is
# CamillaDSP's recommended speed/quality point for adaptive rate adjustment on
# a 1 GB Pi 5. Shared here so both the stereo and active-speaker emitters use
# one definition (no copy-paste twin, no cross-package private import).
DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE = "AsyncSinc"
DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE = "Balanced"
# Local low-latency content pipe: CamillaDSP's File playback writes the post-DSP
# stereo program here and jasper-outputd reads it once per DAC period before the
# blocking DAC write. This is distinct from the multiroom SnapFIFO.
DEFAULT_LOCAL_OUTPUTD_CONTENT_PIPE = "/run/jasper-outputd/content.pipe"
# CamillaDSP v4 async (ratio-adjustable) resampler types — the ONLY ones that
# can carry enable_rate_adjust on a clockless File capture. Synchronous cannot.
ASYNC_RESAMPLER_TYPES = frozenset({"AsyncSinc", "AsyncPoly"})


def is_async_resampler(resampler_type: str | None) -> bool:
    """True iff ``resampler_type`` is a CamillaDSP v4 async (ratio-adjustable)
    resampler — the only kind that can carry enable_rate_adjust on a clockless
    File capture. Exact-set membership: an unknown or ``Synchronous`` type
    returns False so the File-capture guard fails loud rather than emitting a
    config that would free-run.
    """
    return resampler_type in ASYNC_RESAMPLER_TYPES


# snd-aloop ALSA captures whose name a JTS config taps for the summed program.
# A `plug:`-wrapped form resolves to the same device, so match the substring.
_SND_ALOOP_CAPTURE_TOKENS = ("jasper_capture", "Loopback")


def snd_aloop_rate_adjust_oscillation_reason(text: str) -> str | None:
    """Return why an emitted CamillaDSP config would re-introduce the
    rate_adjust + async-resampler oscillation on a snd-aloop capture, else None.

    This is a TEST-TIME contract predicate, NOT a runtime emit-time guard: it has
    no callers in the emit path, so it does not fail-loud at config generation.
    The regression test in test_camilla_config_contract.py feeds it every
    JTS-generated snd-aloop capture config to pin that the emitters never produce
    the oscillation-prone shape; a genuinely NEW emitter path is only covered once
    it is added to that test's fixtures. (Contrast the lean-lane File-capture
    case, whose safe shape is the inverse — a clockless File named-pipe capture
    REQUIRES enable_rate_adjust + an async resampler.)

    A snd-aloop ALSA capture (``plug:jasper_capture`` / ``hw:Loopback,...``) at
    capture-rate == playback-rate already rate-tracks via the loopback, so
    ``enable_rate_adjust: true`` WITH an async resampler makes CamillaDSP's
    adjuster and the resampler fight, producing the metastable AirPlay-dropout
    oscillation documented in docs/HANDOFF (CamillaDSP rate_adjust + AsyncSinc).
    The safe shape is enable_rate_adjust true AND NO async resampler block.

    Returns a one-clause reason string when the config is a JTS-generated
    snd-aloop capture config (single samplerate ⇒ capture == playback by
    construction) that carries an async resampler — the dangerous both-on
    combination. Returns None when the config is not a snd-aloop capture (e.g. a
    File-capture lean config, which has its own guard) or is safe. NOTE the
    bonded-leader pipe-sink config legitimately sets ``enable_rate_adjust:
    false`` on its snd-aloop capture (snapclient is the sole rate-tracker on the
    synced chain) — that is NOT this oscillation and is intentionally not
    flagged; only the async-resampler-on-loopback case is. Parser is
    deliberately lightweight — these are JTS-generated configs with a stable,
    simple ``devices:`` shape, not arbitrary YAML.
    """
    capture_device: str | None = None
    has_resampler = False
    resampler_type: str | None = None
    in_devices = False
    devices_indent = 0
    nested: str | None = None
    nested_indent = 0

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
        # Left the devices: block.
        if indent <= devices_indent and raw_line.lstrip() == raw_line:
            break
        if stripped.endswith(":"):
            key = stripped[:-1].strip()
            if key in {"capture", "playback", "resampler"}:
                nested = key
                nested_indent = indent
                if key == "resampler":
                    has_resampler = True
            continue
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _clean_yaml_scalar(raw_value)
        if nested == "capture" and key == "device" and indent > nested_indent:
            capture_device = value
        elif nested == "resampler" and key == "type" and indent > nested_indent:
            resampler_type = value

    if capture_device is None:
        return None
    if not any(tok in capture_device for tok in _SND_ALOOP_CAPTURE_TOKENS):
        return None  # not a snd-aloop capture (e.g. File lean lane)

    if has_resampler and is_async_resampler(resampler_type):
        return (
            f"snd-aloop capture {capture_device!r} carries an async resampler "
            f"(type={resampler_type!r}) — combined with enable_rate_adjust this "
            "fights the loopback's own rate tracking (the AirPlay-dropout "
            "oscillation)"
        )
    return None


def file_capture_resampler_yaml(
    resampler_type: str,
    profile: str | None = DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
) -> str:
    """Emit the CamillaDSP v4 ``resampler:`` object block for a File-capture
    lean-lane config.

    Newline-prefixed and indented to nest under ``devices:`` (two spaces for
    ``resampler:``, four for its keys). ``profile`` applies to ``AsyncSinc``;
    pass ``None`` to omit it (e.g. ``AsyncPoly``, which takes ``interpolation``
    rather than ``profile``).
    """
    block = f"\n  resampler:\n    type: {resampler_type}"
    if profile:
        block += f"\n    profile: {profile}"
    return block


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


@dataclass(frozen=True)
class FilterSpec:
    """A bounded CamillaDSP-friendly filter definition (preference EQ band).

    The program-domain (stereo) DSP contract type, sibling to
    :class:`PeqFilter`. The sound model (``jasper.sound.profile``) builds
    these from a ``SoundProfile``; the shared stereo-prefix builder
    (``jasper.camilla_stereo_prefix``) emits them — so this lives in the
    neutral contract layer, importable by both the sound and active-speaker
    emitters without a cross-dependency.
    """

    name: str
    biquad_type: str
    freq: float
    gain: float
    q: float | None = None
    slope: float | None = None

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
    and ALSA endpoints.
    """

    result: dict[str, Any] = {}
    in_devices = False
    devices_indent = 0
    nested: str | None = None
    nested_indent = 0

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

        if stripped.endswith(":"):
            key = stripped[:-1].strip()
            if key in {"capture", "playback"}:
                nested = key
                nested_indent = indent
            continue

        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _clean_yaml_scalar(raw_value)

        if key in {"samplerate", "chunksize", "target_level"}:
            try:
                result[key] = int(value)
            except ValueError:
                continue
            continue

        if key == "volume_limit":
            try:
                result[key] = float(value)
            except ValueError:
                continue
            continue

        if nested in {"capture", "playback"} and indent > nested_indent:
            if key == "device":
                result[f"{nested}_device"] = value
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
