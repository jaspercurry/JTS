"""Auto-load the systemd-equivalent env files into `os.environ` so
CLI tools see the same vars the daemon's systemd unit sees, even
when the user invokes them without sourcing `/etc/jasper/jasper.env`
into their shell first.

``ENV_FILES`` MUST mirror ``jasper-voice.service``'s ``EnvironmentFile=``
directives, in order (later file wins on conflict — a wizard file overrides a
stale value an operator left in ``jasper.env``). When it drifts, CLI tools
that build ``Config.from_env()`` silently see *less* config than the running
daemon: e.g. ``jasper-doctor`` reported transit / Home Assistant / weather as
"not configured" even when the household had them set, because those wizard
files (``transit.env``, ``home_assistant.env``, ``weather.env``) were sourced
by the daemon's unit but missing here. ``tests/test_env_load_mirrors_unit.py``
asserts this list equals the unit's directives so it can't drift again.

Variables already set in the calling shell (``FOO=bar jasper-cues``)
take precedence over all of these — useful for one-off probes.
"""
from __future__ import annotations

import os
from pathlib import Path


# Mirror of jasper-voice.service's EnvironmentFile= order. Guarded against
# drift by tests/test_env_load_mirrors_unit.py — update BOTH together.
ENV_FILES = (
    "/etc/jasper/jasper.env",
    "/var/lib/jasper/speaker_name.env",
    "/var/lib/jasper/spotify_credentials.env",
    "/var/lib/jasper/voice_provider.env",
    "/var/lib/jasper/google_credentials.env",
    "/var/lib/jasper/wake_model.env",
    "/var/lib/jasper/weather.env",
    "/var/lib/jasper/transit.env",
    "/var/lib/jasper/home_assistant.env",
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


def merged_env_files(paths: "tuple[str, ...] | None" = None) -> dict[str, str]:
    """Return the merged env-file mapping for ``paths``.

    Later files win on conflict, matching systemd's
    ``EnvironmentFile=`` ordering. This is intentionally separate from
    :func:`load_env_files`: some callers need CLI-style "shell wins"
    semantics, while long-lived daemons launching subprocesses need a
    freshly-read view of the wizard-owned SSOT files."""
    files = paths if paths is not None else ENV_FILES
    merged: dict[str, str] = {}
    for path in files:
        merged.update(parse_env_file(path))
    return merged


def load_env_files(paths: "tuple[str, ...] | None" = None) -> None:
    """Populate ``os.environ`` from the given paths (default
    ``ENV_FILES``) so ``Config.from_env()`` sees the merged set.

    Later file wins on conflict between files. Calling-shell values
    are preserved (setdefault semantics) so explicit overrides
    still work."""
    merged = merged_env_files(paths)
    for key, value in merged.items():
        os.environ.setdefault(key, value)


def subprocess_env_with_fresh_files(
    *,
    base: "dict[str, str] | None" = None,
    paths: "tuple[str, ...] | None" = None,
) -> dict[str, str]:
    """Return an environment for subprocesses launched by daemons.

    ``base`` defaults to the current process environment so PATH and
    service-local knobs are preserved. Env-file values are then applied
    with normal systemd file precedence and override any stale value in
    the long-lived daemon process. This is the right shape for
    ``jasper-control`` launching ``jasper-doctor`` from the dashboard:
    the wizard files are the current runtime truth, while
    ``os.environ`` may reflect an older daemon start."""
    env = dict(os.environ if base is None else base)
    env.update(merged_env_files(paths))
    return env
