"""Lightweight CamillaDSP config contract shared by DSP config emitters.

Keep this module import-cheap. Socket-activated web surfaces use these
defaults to build and inspect CamillaDSP YAML without pulling NumPy/SciPy
into the combined ``jasper-web`` process.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# Defaults match the outputd topology. Generated correction and
# sound-profile configs must keep Camilla's playback target on the
# post-DSP outputd loopback lane; otherwise applying a profile would
# route music around jasper-outputd while TTS still uses outputd.
DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"
DEFAULT_PLAYBACK_DEVICE = "outputd_content_playback"
ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "outputd_active_content_playback"
DEFAULT_CAPTURE_FORMAT = "S32_LE"
DEFAULT_PLAYBACK_FORMAT = "S16_LE"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHUNKSIZE = 1024
DEFAULT_TARGET_LEVEL = 2048
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
