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
# Room label lives in the same identity home as the display name. Empty
# string means "unset" (callers fall back — e.g. identity.read_identity
# defers to the legacy peering room). Unlike the display name there is no
# non-empty default: an unset room is a meaningful state, not "JTS".
ENV_VAR_ROOM = "JASPER_SPEAKER_ROOM"
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
    # `name` stays first so positional construction (SpeakerNameState(name))
    # keeps working for any caller. `room` joins the identity home (this
    # state is now name + room); it defaults to "" (unset). `source` records
    # which layer the name came from ("state"/"default").
    name: str
    room: str = ""
    source: str = ""


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


def validate_room(raw: str) -> str:
    """Return a normalized room label, or "" for an unset room.

    A room is optional, so the empty string (after normalization) is a
    valid "unset" answer rather than an error. A non-empty room reuses the
    exact same normalize/validate rules as the display name — the name
    validator's allowed punctuation comment already anticipates room names
    like ``Living Room #2``, so there is no second character policy to keep
    in sync.
    """
    if not normalize_name(raw):
        return ""
    return validate_name(raw)


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
    """Read the persisted display name + room, defaulting to ``JTS``/no room.

    Parses BOTH ``JASPER_SPEAKER_NAME`` and ``JASPER_SPEAKER_ROOM`` from the
    file (in any order). A present-but-invalid name falls back to the
    default; a present-but-invalid room falls back to "" (unset), each
    independently — one bad line never blanks the other field.
    """
    found_name: str | None = None
    room = ""
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
                    try:
                        found_name = validate_name(value)
                    except SpeakerNameError:
                        pass
                elif key == ENV_VAR_ROOM:
                    try:
                        room = validate_room(value)
                    except SpeakerNameError:
                        room = ""
    except FileNotFoundError:
        pass
    except OSError:
        pass
    if found_name is not None:
        return SpeakerNameState(found_name, room, "state")
    return SpeakerNameState(DEFAULT_SPEAKER_NAME, room, "default")


def runtime_name(*, environ: dict[str, str] | None = None, path: str = STATE_FILE) -> str:
    """Resolve the name for runtime code.

    Systemd services source ``speaker_name.env`` into the environment on
    start. Dev/test processes may not, so fall back to reading the file.
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_VAR):
        return validate_name(env[ENV_VAR])
    return read_state(path).name


def runtime_room(*, environ: dict[str, str] | None = None, path: str = STATE_FILE) -> str:
    """Resolve the room label for runtime code, or "" when unset.

    Same precedence shape as ``runtime_name``: the env var
    (``JASPER_SPEAKER_ROOM``, sourced by systemd) wins, then the state
    file, then "". An empty/whitespace env value is treated as unset and
    falls through to the file rather than masking it.
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_VAR_ROOM, "").strip():
        return validate_room(env[ENV_VAR_ROOM])
    return read_state(path).room


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


# Sentinel so callers can pass room=None to mean "preserve whatever is on
# disk" without colliding with room="" which explicitly clears the room.
_PRESERVE_ROOM = object()


def write_state(
    name: str,
    room: object = _PRESERVE_ROOM,
    path: str = STATE_FILE,
    *,
    mode: int = 0o644,
) -> str:
    """Validate and atomically write ``speaker_name.env`` (name + room).

    Back-compat: existing callers ``write_state(name)`` keep working — the
    room defaults to the currently-stored room (or "" if none), so renaming
    the speaker never silently drops its room. Pass ``room=""`` to clear it,
    or a non-empty string to set it. Both keys are written in one atomic
    replace so the file is never observed half-updated.
    """
    cleaned = validate_name(name)
    if room is _PRESERVE_ROOM:
        cleaned_room = read_state(path).room
    else:
        cleaned_room = validate_room(room)  # type: ignore[arg-type]

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{ENV_VAR}={quote_env_value(cleaned)}\n")
            f.write(f"{ENV_VAR_ROOM}={quote_env_value(cleaned_room)}\n")
        os.replace(tmp, target)
        os.chmod(target, mode)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return cleaned
