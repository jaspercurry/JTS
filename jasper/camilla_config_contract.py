"""Lightweight CamillaDSP config contract shared by DSP config emitters.

Keep this module import-cheap. Socket-activated web surfaces use these
defaults to build and inspect CamillaDSP YAML without pulling NumPy/SciPy
into the combined ``jasper-web`` process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True)
class PeqFilter:
    """Import-cheap representation of a CamillaDSP peaking EQ."""

    freq: float
    q: float
    gain: float


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
