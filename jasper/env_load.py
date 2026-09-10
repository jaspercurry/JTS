# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import logging
import os
from dataclasses import dataclass
from typing import Literal

from jasper.env_file import parse_env_mapping, read_env_file_text


#: The operator-owned base layer every daemon unit loads first.
BASE_ENV_PATH = "/etc/jasper/jasper.env"
#: Single-writer env files, declared here for every reader (the writing
#: daemon/wizard imports the name from this module).
FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
GROUPING_ENV_FILE = "/var/lib/jasper/grouping.env"
OUTPUTD_ENV_PATH = "/var/lib/jasper/outputd.env"
#: PERSISTENT (never /run) so a bonded speaker boots with the content lane
#: already configured, without a second jasper-outputd restart.
OUTPUTD_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-outputd.env"
SOURCE_INTENT_ENV = "/var/lib/jasper/source_intent.env"
SPEAKER_NAME_ENV_PATH = "/var/lib/jasper/speaker_name.env"
#: CLIENT_ID + OAUTH_MODE. Separate from ``jasper.env`` so jasper-web can
#: write it without /etc being RW (systemd ``ProtectSystem=full``).
SPOTIFY_CREDENTIALS_ENV_PATH = "/var/lib/jasper-intsecrets/spotify_credentials.env"
#: The TTS socket key is OMITTED, never written empty: an empty value is
#: read as a real, invalid path.
VOICE_GROUPING_ENV_FILE = "/var/lib/jasper/grouping-voice.env"


def env_file_path() -> str:
    """``BASE_ENV_PATH``, overridable via ``JASPER_ENV_FILE`` (test/probe seam)."""
    return os.environ.get("JASPER_ENV_FILE") or BASE_ENV_PATH


# UNION of every unit's persistent EnvironmentFile= (not one daemon's).
# Guarded by tests/test_env_load_mirrors_unit.py: add a wizard env file to ANY
# deploy/systemd/*.service and the test fails until it's added here too.
ENV_FILES = (
    BASE_ENV_PATH,
    # jasper-voice.service order (the most config-consuming daemon):
    SPEAKER_NAME_ENV_PATH,
    SPOTIFY_CREDENTIALS_ENV_PATH,
    "/var/lib/jasper/voice_provider.env",
    # High-value provider/Google secrets live in jasper-secrets (voice+web), while HA +
    # Spotify integration secrets live in jasper-intsecrets (voice+control+mux+web). A
    # non-member CLI/daemon that runs env_load simply reads {} for an unreadable
    # compartment file (parse_env_file is fail-soft on EACCES); the root jasper-doctor
    # reads them fine.
    "/var/lib/jasper-secrets/voice_keys.env",
    "/var/lib/jasper-secrets/google_credentials.env",
    "/var/lib/jasper-secrets/google_routes.env",
    "/var/lib/jasper/wake_model.env",
    "/var/lib/jasper/weather.env",
    "/var/lib/jasper/transit.env",
    "/var/lib/jasper-intsecrets/home_assistant.env",
    "/var/lib/jasper/tool_state.env",
    "/var/lib/jasper/conversation_history.env",
    # ...plus persistent files sourced by OTHER units (control / aec / etc.):
    "/var/lib/jasper/aec_mode.env",
    FANIN_ENV_PATH,
    GROUPING_ENV_FILE,
    OUTPUTD_GROUPING_ENV_FILE,
    VOICE_GROUPING_ENV_FILE,
    OUTPUTD_ENV_PATH,
    "/var/lib/jasper/peering.env",
    "/var/lib/jasper/accessory-mics.env",
    "/var/lib/jasper/usb_mic.env",
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


def bounded_env_float(
    name: str,
    default: float,
    *,
    lo: float,
    hi: float,
) -> float:
    """Read an inclusive-range float knob, falling back on invalid input."""

    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


def bounded_env_int(
    name: str,
    default: int,
    *,
    lo: int,
    hi: int,
) -> int:
    """Read an inclusive-range integer knob, falling back on invalid input."""

    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


def read_env_file_state(path: str) -> EnvFileState:
    """Read and parse an env file while preserving read status."""
    text, err = read_env_file_text(path)
    if text is None:
        if err is None:
            return EnvFileState(path, {}, "missing")
        return EnvFileState(
            path,
            {},
            "unreadable",
            error=f"{type(err).__name__}: {err}",
        )
    return EnvFileState(path, parse_env_mapping(text), "loaded")


def read_env_file_or_warn(path: str, *, logger: logging.Logger) -> dict[str, str]:
    """Read an EnvironmentFile, warning via ``logger`` when it exists but
    can't be read. Missing files resolve silently to ``{}``."""
    state = read_env_file_state(path)
    if state.status == "unreadable":
        logger.warning("could not read %s: %s", path, state.error)
    return state.values


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a shell-style KEY=VALUE env file. Strips surrounding
    single or double quotes; ignores blanks and lines starting with
    ``#``. Returns ``{}`` for missing or unreadable files —
    best-effort, never raises."""
    return read_env_file_state(path).values


def merged_env_files(
    paths: "tuple[str, ...] | None" = None, *, require_readable: bool = False,
) -> dict[str, str]:
    """Return the merged env-file mapping for ``paths``.

    Later files win on conflict, matching systemd's
    ``EnvironmentFile=`` ordering. This is intentionally separate from
    :func:`load_env_files`: some callers need CLI-style "shell wins"
    semantics, while long-lived daemons launching subprocesses need a
    freshly-read view of the wizard-owned SSOT files.

    ``require_readable`` raises on unreadable files so diagnostic callers do
    not report partial settings as complete. Missing files remain optional.

    The base layer resolves through :func:`env_file_path`, so the
    ``JASPER_ENV_FILE`` seam reaches every reader that goes through this
    function — not readers that open :data:`BASE_ENV_PATH` themselves, nor the
    separate ``JASPER_SYSTEM_ENV_FILE`` seam in
    ``wake_corpus/runtime_probe.py``."""
    files = paths if paths is not None else ENV_FILES
    merged: dict[str, str] = {}
    for path in files:
        if path == BASE_ENV_PATH:
            path = env_file_path()
        state = read_env_file_state(path)
        if require_readable and state.status == "unreadable":
            raise OSError(f"unreadable env file {path}: {state.error}")
        merged.update(state.values)
    return merged


def outputd_reconciled_env(
    outputd_env_path: str | None = None, *, require_readable: bool = False,
) -> dict[str, str]:
    """jasper-outputd's persistent env, read fresh through its own layering.

    THE ONE MERGE every surface that reports what outputd is RUNNING consumes —
    parks, the doctor, ``/state``, the runtime plan, the health sampler — in the
    unit's own ``EnvironmentFile=`` order
    (``deploy/systemd/jasper-outputd.service``): ``jasper.env``, then
    ``outputd.env``, then ``grouping-outputd.env``. LATER WINS, exactly as it
    does for the daemon. All three, not the last two: keys an operator pins in
    ``jasper.env`` — the period above all — are ones outputd loads, so a reader
    that skipped that layer would answer for an env the daemon is not running.

    NOT ``ENV_FILES``: that tuple is jasper-voice's order, and it lists
    ``grouping-outputd.env`` BEFORE ``outputd.env`` — the opposite of what
    outputd loads, which would silently invert every bonded pin.

    ``outputd_env_path`` overrides the outputd.env layer only (the
    ``JASPER_OUTPUTD_ENV_FILE`` operator seam); ``grouping-outputd.env`` keeps
    its own path. The base ``jasper.env`` layer has its own seam,
    ``JASPER_ENV_FILE`` (:func:`env_file_path`), applied inside
    :func:`merged_env_files`.
    """
    return merged_env_files(
        (
            BASE_ENV_PATH,
            outputd_env_path or OUTPUTD_ENV_PATH,
            OUTPUTD_GROUPING_ENV_FILE,
        ),
        require_readable=require_readable,
    )


def load_env_files(paths: "tuple[str, ...] | None" = None) -> None:
    """Populate ``os.environ`` from the given paths (default
    ``ENV_FILES``) so ``Config.from_env()`` sees the merged set.

    Later file wins on conflict between files. Calling-shell values
    are preserved (setdefault semantics) so explicit overrides
    still work."""
    merged = merged_env_files(paths)
    for key, value in merged.items():
        os.environ.setdefault(key, value)

