"""Auto-load the systemd-equivalent env files into `os.environ` so
CLI tools see the same vars the daemons see, even when the user invokes
them without sourcing `/etc/jasper/jasper.env` into their shell first.

``ENV_FILES`` MUST be a SUPERSET of every ``deploy/systemd/*.service``'s
persistent ``EnvironmentFile=`` directives — NOT just one daemon's. A
``Config.from_env()`` built by a *cross-cutting* CLI (chiefly
``jasper-doctor``, which checks subsystems owned by many daemons) has to see
the union, or that CLI silently sees *less* config than the running system:
``jasper-doctor`` reported transit / Home Assistant / weather — and, before
this list became the union, peering / grouping / usbsink — as "not configured"
even when set, because those wizard files were sourced by some daemon's unit
but missing here. ``tests/test_env_load_mirrors_unit.py`` asserts every unit's
persistent ``EnvironmentFile=`` path is in this list, so a new wizard env file
(a future DAC/mic registry's, say) can't silently reintroduce the bug.

Ordering: ``jasper.env`` first (operator base), then the wizard-owned
``/var/lib/jasper/*.env`` files (later wins on conflict — a wizard file
overrides a stale value an operator left in ``jasper.env``). The wizard files
own disjoint keys, so order among them doesn't matter for resolution.
``/run/*`` runtime-IPC env files are intentionally excluded (generated at
runtime, absent at CLI time, never config the doctor reads).

CAVEAT: a few runtime-only vars are NOT in any persistent file — e.g.
``JASPER_MIC_DEVICE`` is resolved and injected into the daemon's env by
``jasper-aec-reconcile`` (via systemd, not a file), so a CLI can't see it
this way. Doctor checks that need such a value read it another way (or gate on
the daemon being active); ``ENV_FILES`` only covers persistent config.

Variables already set in the calling shell (``FOO=bar jasper-cues``)
take precedence over all of these — useful for one-off probes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


# UNION of every unit's persistent EnvironmentFile= (not one daemon's).
# Guarded by tests/test_env_load_mirrors_unit.py: add a wizard env file to ANY
# deploy/systemd/*.service and the test fails until it's added here too.
ENV_FILES = (
    "/etc/jasper/jasper.env",
    # jasper-voice.service order (the most config-consuming daemon):
    "/var/lib/jasper/speaker_name.env",
    "/var/lib/jasper/spotify_credentials.env",
    "/var/lib/jasper/voice_provider.env",
    "/var/lib/jasper/google_credentials.env",
    "/var/lib/jasper/wake_model.env",
    "/var/lib/jasper/weather.env",
    "/var/lib/jasper/transit.env",
    "/var/lib/jasper/home_assistant.env",
    # ...plus persistent files sourced by OTHER units (control / aec / etc.):
    "/var/lib/jasper/aec_mode.env",
    "/var/lib/jasper/fanin.env",
    "/var/lib/jasper/grouping.env",
    "/var/lib/jasper/grouping-outputd.env",
    "/var/lib/jasper/grouping-voice.env",
    "/var/lib/jasper/outputd.env",
    "/var/lib/jasper/peering.env",
    "/var/lib/jasper/usbsink.env",
    "/var/lib/jasper/wake_corpus_bridge.env",
)

EnvFileReadStatus = Literal["loaded", "missing", "unreadable"]


@dataclass(frozen=True)
class EnvFileState:
    """Status-bearing read of a shell-style env file.

    ``parse_env_file`` stays fail-soft for legacy callers that only need
    the values. Consumers that render diagnostics should use this shape
    so missing and unreadable files do not collapse into the same empty
    mapping.
    """

    path: str
    values: dict[str, str]
    status: EnvFileReadStatus
    error: str = ""

    @property
    def loaded(self) -> bool:
        return self.status == "loaded"


def parse_env_text(text: str) -> dict[str, str]:
    """Parse shell-style KEY=VALUE env file text.

    Strips surrounding single or double quotes; ignores blanks and
    lines starting with ``#``.
    """
    out: dict[str, str] = {}
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


def read_env_file_state(path: str) -> EnvFileState:
    """Read and parse an env file while preserving read status."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return EnvFileState(path, {}, "missing")
    except (OSError, UnicodeError) as e:
        return EnvFileState(
            path,
            {},
            "unreadable",
            error=f"{type(e).__name__}: {e}",
        )
    return EnvFileState(path, parse_env_text(text), "loaded")


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a shell-style KEY=VALUE env file. Strips surrounding
    single or double quotes; ignores blanks and lines starting with
    ``#``. Returns ``{}`` for missing or unreadable files —
    best-effort, never raises."""
    return read_env_file_state(path).values


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
