"""Auto-load the systemd-equivalent env files into `os.environ` so
CLI tools see the same vars the daemon's systemd unit sees, even
when the user invokes them without sourcing `/etc/jasper/jasper.env`
into their shell first.

Mirrors the systemd unit's ``EnvironmentFile=`` directives:

  1. ``/etc/jasper/jasper.env``                      — operator-managed
  2. ``/var/lib/jasper/voice_provider.env``          — web-wizard-managed
     (overrides 1 on conflict)
  3. ``/var/lib/jasper/google_credentials.env``      — Google wizard-managed
     (CLIENT_ID/SECRET; overrides earlier files on conflict)

Variables already set in the calling shell (``FOO=bar jasper-cues``)
take precedence over both — useful for one-off probes.
"""
from __future__ import annotations

import os
from pathlib import Path


ENV_FILES = (
    "/etc/jasper/jasper.env",
    "/var/lib/jasper/voice_provider.env",
    "/var/lib/jasper/google_credentials.env",
)


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a shell-style KEY=VALUE env file. Strips surrounding
    single or double quotes; ignores blanks and lines starting with
    ``#``. Returns ``{}`` for missing or unreadable files —
    best-effort, never raises."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2 and value[0] == value[-1]
                and value[0] in ('"', "'")):
            value = value[1:-1]
        out[key] = value
    return out


def load_env_files(paths: "tuple[str, ...] | None" = None) -> None:
    """Populate ``os.environ`` from the given paths (default
    ``ENV_FILES``) so ``Config.from_env()`` sees the merged set.

    Later file wins on conflict between files. Calling-shell values
    are preserved (setdefault semantics) so explicit overrides
    still work."""
    files = paths if paths is not None else ENV_FILES
    merged: dict[str, str] = {}
    for path in files:
        merged.update(parse_env_file(path))
    for key, value in merged.items():
        os.environ.setdefault(key, value)
