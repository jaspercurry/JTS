"""Canonical speaker display-name state.

This is the user-facing name shown by renderer surfaces such as
Spotify Connect, AirPlay, Bluetooth, and USB Audio. It is intentionally
separate from ``JASPER_HOSTNAME``: the address stays ``jts.local`` while
the display name can be something like ``Kitchen``.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SPEAKER_NAME = "JTS"
ENV_VAR = "JASPER_SPEAKER_NAME"
STATE_FILE = "/var/lib/jasper/speaker_name.env"

# AirPlay's documented ceiling is 50 characters. JTS uses a shorter
# cross-renderer limit so Bluetooth / USB / app pickers stay tidy.
MAX_SPEAKER_NAME_CHARS = 32

# Conservative printable-ASCII subset for names that travel through
# mDNS, BlueZ, ConfigFS USB descriptors, shell-sourced env files, and
# libconfig strings. Includes the punctuation people naturally use in
# room/device names without inviting quoting or path-like surprises.
ALLOWED_PUNCTUATION = " .,'&()+-_#"
_ALLOWED_RE = re.compile(rf"^[A-Za-z0-9{re.escape(ALLOWED_PUNCTUATION)}]+$")


class SpeakerNameError(ValueError):
    """Raised when a submitted speaker name is not safe to persist."""


@dataclass(frozen=True)
class SpeakerNameState:
    name: str
    source: str


def normalize_name(raw: str) -> str:
    """Trim and collapse whitespace before validation/persistence."""
    return " ".join((raw or "").strip().split())


def validate_name(raw: str) -> str:
    """Return a normalized name or raise ``SpeakerNameError``."""
    name = normalize_name(raw)
    if not name:
        raise SpeakerNameError("Enter a speaker name.")
    if len(name) > MAX_SPEAKER_NAME_CHARS:
        raise SpeakerNameError(
            f"Use {MAX_SPEAKER_NAME_CHARS} characters or fewer.",
        )
    if not name[0].isalnum() or not name[-1].isalnum():
        raise SpeakerNameError("Start and end the name with a letter or number.")
    if not name.isascii() or not _ALLOWED_RE.fullmatch(name):
        raise SpeakerNameError(
            "Use letters, numbers, spaces, apostrophes, dashes, and simple punctuation.",
        )
    return name


def _parse_env_line(line: str) -> tuple[str, str] | None:
    try:
        parts = shlex.split(line, comments=True, posix=True)
    except ValueError:
        return None
    if not parts or "=" not in parts[0]:
        return None
    key, _, value = parts[0].partition("=")
    return key, value


def read_state(path: str = STATE_FILE) -> SpeakerNameState:
    """Read the persisted display name, defaulting to ``JTS``."""
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parsed = _parse_env_line(line)
                if parsed is None:
                    continue
                key, value = parsed
                if key == ENV_VAR:
                    return SpeakerNameState(validate_name(value), "state")
    except FileNotFoundError:
        pass
    except OSError:
        pass
    except SpeakerNameError:
        pass
    return SpeakerNameState(DEFAULT_SPEAKER_NAME, "default")


def runtime_name(*, environ: dict[str, str] | None = None, path: str = STATE_FILE) -> str:
    """Resolve the name for runtime code.

    Systemd services source ``speaker_name.env`` into the environment on
    start. Dev/test processes may not, so fall back to reading the file.
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_VAR):
        return validate_name(env[ENV_VAR])
    return read_state(path).name


def quote_env_value(value: str) -> str:
    """Quote a value for both systemd EnvironmentFile and shell source."""
    escaped = (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    return f'"{escaped}"'


def write_state(name: str, path: str = STATE_FILE, *, mode: int = 0o644) -> str:
    """Validate and atomically write ``speaker_name.env``."""
    cleaned = validate_name(name)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{ENV_VAR}={quote_env_value(cleaned)}\n")
        os.replace(tmp, target)
        os.chmod(target, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return cleaned
