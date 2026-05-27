"""ALSA sample-rate-converter quality preference.

The global ALSA ``defaults.pcm.rate_converter`` setting controls the
quality/cost tradeoff for plug-layer conversions such as AirPlay's
44.1 kHz -> JTS's 48 kHz fan-in lane. The persisted user preference
lives in a tiny systemd-style env file so installs, web UI, and manual
operator edits all converge on the same source of truth.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONVERTER = "samplerate_medium"
STATE_ENV_KEY = "JASPER_ALSA_RATE_CONVERTER"
VALID_CONVERTERS = ("samplerate_medium", "samplerate_best")

_OPTION_META: dict[str, dict[str, str]] = {
    "samplerate_medium": {
        "label": "Medium",
        "summary": "Lower CPU, still clean for AirPlay, wake, and AEC.",
    },
    "samplerate_best": {
        "label": "Best",
        "summary": "Maximum ultrasonic-band fidelity, higher CPU.",
    },
}


def _state_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(
        path
        or os.environ.get(
            "JASPER_AUDIO_QUALITY_FILE",
            "/var/lib/jasper/audio_quality.env",
        ),
    )


def _asound_path(path: str | os.PathLike[str] | None = None) -> Path:
    return Path(
        path
        or os.environ.get("JASPER_ASOUND_CONF", "/etc/asound.conf"),
    )


def _render_command(command: str | None = None) -> str:
    return command or os.environ.get(
        "JASPER_ASOUND_RENDER_COMMAND",
        "/usr/local/sbin/jasper-render-asound-conf",
    )


def normalize_converter(raw: str | None) -> str:
    """Return a canonical ALSA converter plugin name.

    Accepts the human aliases used by the web UI plus the exact plugin
    names used by ALSA. Raises ``ValueError`` for anything else.
    """
    value = (raw or "").strip().strip('"').strip("'").lower()
    aliases = {
        "medium": "samplerate_medium",
        "balanced": "samplerate_medium",
        "samplerate_medium": "samplerate_medium",
        "best": "samplerate_best",
        "max": "samplerate_best",
        "maximum": "samplerate_best",
        "samplerate_best": "samplerate_best",
    }
    try:
        return aliases[value]
    except KeyError as e:
        raise ValueError(
            f"unsupported ALSA rate converter {raw!r}; expected "
            f"{', '.join(VALID_CONVERTERS)}"
        ) from e


def _read_env_value(path: Path) -> str | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == STATE_ENV_KEY:
            return value.strip()
    return None


def read_requested_converter(
    path: str | os.PathLike[str] | None = None,
) -> str:
    """Read the persisted requested converter, defaulting to Medium."""
    raw = _read_env_value(_state_path(path))
    if raw is None:
        return DEFAULT_CONVERTER
    return normalize_converter(raw)


def _write_converter_env(converter: str, path: Path) -> None:
    path.write_text(
        "# Written by JTS /system audio quality control.\n"
        f"{STATE_ENV_KEY}={converter}\n",
    )
    os.chmod(path, 0o644)


def read_active_converter(
    path: str | os.PathLike[str] | None = None,
) -> str | None:
    """Parse the active rendered ALSA config, if it is readable."""
    try:
        text = _asound_path(path).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or not stripped.startswith("defaults.pcm.rate_converter")
        ):
            continue
        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            try:
                return normalize_converter(parts[1])
            except ValueError:
                return parts[1].strip().strip('"').strip("'")
    return None


def converter_options() -> list[dict[str, str]]:
    return [
        {"converter": value, **_OPTION_META[value]}
        for value in VALID_CONVERTERS
    ]


def read_state(
    *,
    state_path: str | os.PathLike[str] | None = None,
    asound_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    requested = read_requested_converter(state_path)
    active = read_active_converter(asound_path)
    meta = _OPTION_META[requested]
    return {
        "converter": requested,
        "active_converter": active,
        "label": meta["label"],
        "summary": meta["summary"],
        "options": converter_options(),
    }


def write_requested_converter(
    converter: str,
    path: str | os.PathLike[str] | None = None,
) -> str:
    canonical = normalize_converter(converter)
    dst = _state_path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    _write_converter_env(canonical, tmp)
    os.replace(tmp, dst)
    return canonical


def apply_requested_converter(
    converter: str,
    *,
    state_path: str | os.PathLike[str] | None = None,
    asound_path: str | os.PathLike[str] | None = None,
    render_command: str | None = None,
) -> dict[str, Any]:
    """Persist and render the requested converter.

    The renderer script writes the actual ALSA config. It reads a
    temporary env file first; the durable preference is updated only
    after rendering succeeds, so a failed render cannot leave a future
    deploy pointed at an unapplied setting.
    """
    canonical = normalize_converter(converter)
    dst = _state_path(state_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=dst.parent,
            prefix=dst.name + ".render.",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(
                "# Written by JTS /system audio quality control.\n"
                f"{STATE_ENV_KEY}={canonical}\n",
            )
        os.chmod(tmp_name, 0o644)
        env = os.environ.copy()
        env["JASPER_AUDIO_QUALITY_FILE"] = tmp_name
        subprocess.run(
            [_render_command(render_command)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        write_requested_converter(canonical, dst)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass
    state = read_state(state_path=state_path, asound_path=asound_path)
    state["converter"] = canonical
    return state
