"""Single source of truth for the runtime **debug-logging toggle**.

The ``/system`` Debug card lets an operator raise one subsystem's
logging to ``DEBUG`` on demand — for an *occasional* debug session,
never as a default. State is persisted by ``jasper-control`` to
``/var/lib/jasper/debug.env`` (the same wizard-owned ``*.env`` SSOT
convention as ``aec_mode.env`` / ``voice_provider.env``).

Schema (all keys optional; absence ⇒ that subsystem is at normal
INFO logging)::

    JASPER_DEBUG_VOICE=1            # one per subsystem id (see SUBSYSTEMS)
    JASPER_DEBUG_AEC=1
    JASPER_DEBUG_CONTROL=1
    JASPER_DEBUG_EXPIRES_AT=1717000000   # unix epoch; shared auto-expiry

**Design invariant — additive only.** This module can only *raise*
verbosity (INFO → DEBUG). It never lowers a logger and never touches
WARNING/ERROR or the structured ``event=`` lines the resilience layer
depends on. There is no "quiet mode" here. See
``docs/HANDOFF-observability.md``.

**Auto-expiry.** A single shared ``JASPER_DEBUG_EXPIRES_AT`` bounds the
whole debug session (default 2 h, re-armed on each toggle change). Once
it passes, :func:`resolve_debug_state` reports everything inactive even
if the per-subsystem flags are still ``1`` — so a daemon that reads the
file after expiry comes up at normal INFO. ``jasper-control`` owns the
timer that actually rewrites the file + restarts the affected daemons
at expiry; this module just makes the *read* honest in the meantime.

**Why a fresh file read, not ``os.environ``.** Mirrors
:mod:`jasper.voice.provider_state`: long-lived daemons freeze their
env at process start, so the apply path reads the file directly. A
daemon's logging level is set once at startup (``apply_for`` is called
right after ``logging.basicConfig``), so a toggle takes effect when
``jasper-control`` restarts that daemon — the restart-to-apply MVP.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .env_load import parse_env_file

# Wizard-owned SSOT. Path (not contents) overridable for tests / headless
# imaging via JASPER_DEBUG_FILE — a static deploy constant, so reading it
# once is fine; only the file's *contents* are read fresh on every call.
DEBUG_FILE = "/var/lib/jasper/debug.env"

# Default debug-session lifetime. Verbose-in-production is a session,
# never a default — so the toggle auto-expires. 2 h is long enough to
# reproduce an issue, short enough that a forgotten toggle self-heals.
DEFAULT_TTL_SEC = 2 * 60 * 60

EXPIRES_KEY = "JASPER_DEBUG_EXPIRES_AT"


@dataclass(frozen=True)
class Subsystem:
    """One togglable subsystem. ``unit`` is the systemd unit
    ``jasper-control`` restarts to apply a change; ``loggers`` are the
    logger names raised to DEBUG inside that daemon's process (each
    daemon is one process, so the daemon's whole ``jasper`` tree is the
    natural unit)."""

    id: str
    unit: str
    label: str
    loggers: tuple[str, ...]


# The subsystems the Debug card exposes. Extensible: add a row here, wire
# apply_for() into that daemon's startup, and it appears on /system.
# (mux uses a --log-level CLI arg and shairport uses its own config-file
# log_verbosity — both a different mechanism than basicConfig, deferred.)
SUBSYSTEMS: dict[str, Subsystem] = {
    "voice": Subsystem(
        "voice", "jasper-voice.service", "Voice", ("jasper",),
    ),
    "aec": Subsystem(
        "aec", "jasper-aec-bridge.service", "AEC bridge", ("jasper",),
    ),
    "control": Subsystem(
        "control", "jasper-control.service", "Control", ("jasper",),
    ),
}


def env_key(subsystem_id: str) -> str:
    """The per-subsystem env key, e.g. ``voice`` → ``JASPER_DEBUG_VOICE``."""
    return f"JASPER_DEBUG_{subsystem_id.upper()}"


@dataclass(frozen=True)
class DebugState:
    """Resolved debug state at a point in time. ``configured`` is the raw
    set of subsystems flagged ``=1`` in the file; ``active`` applies the
    shared expiry (empty once expired). Consumers gating behaviour use
    ``active``; the UI shows ``configured`` + ``remaining_sec``."""

    configured: frozenset[str]
    expires_at: float | None
    now: float

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.now >= self.expires_at

    @property
    def active(self) -> frozenset[str]:
        return frozenset() if self.expired else self.configured

    @property
    def remaining_sec(self) -> float:
        if self.expires_at is None:
            return 0.0
        return max(0.0, self.expires_at - self.now)


def resolve_debug_state(env: dict[str, str], now: float) -> DebugState:
    """Pure resolver from an already-parsed env mapping. No IO, so the
    file readers below and ``jasper-control``'s endpoint share one rule.
    Unknown/malformed values fail toward *off* (normal INFO)."""
    configured = frozenset(
        sid for sid in SUBSYSTEMS if (env.get(env_key(sid)) or "").strip() == "1"
    )
    raw = (env.get(EXPIRES_KEY) or "").strip()
    try:
        expires_at: float | None = float(raw) if raw else None
    except ValueError:
        expires_at = None
    # An expiry with nothing configured is meaningless; normalise to None.
    if not configured:
        expires_at = None
    return DebugState(configured=configured, expires_at=expires_at, now=now)


def read_debug_state(path: str | None = None, now: float | None = None) -> DebugState:
    """Read debug state fresh from the SSOT file. Best-effort: a missing
    or unreadable file resolves to "nothing in debug" rather than
    raising (``parse_env_file`` returns ``{}`` on any OSError)."""
    return resolve_debug_state(
        parse_env_file(path or DEBUG_FILE),
        time.time() if now is None else now,
    )


def compute_env_update(
    current: dict[str, str],
    subsystem_id: str,
    enabled: bool,
    *,
    now: float,
    ttl: float = DEFAULT_TTL_SEC,
) -> dict[str, str]:
    """Pure computation of the env-file updates for toggling one
    subsystem. Caller persists via ``_atomic_rewrite_env``. Re-arms the
    shared expiry to ``now + ttl`` whenever anything is (or stays)
    enabled; clears it when the last subsystem goes off."""
    if subsystem_id not in SUBSYSTEMS:
        raise ValueError(f"unknown debug subsystem: {subsystem_id!r}")
    after = {
        sid for sid in SUBSYSTEMS
        if (current.get(env_key(sid)) or "").strip() == "1"
    }
    after.add(subsystem_id) if enabled else after.discard(subsystem_id)
    return {
        env_key(subsystem_id): "1" if enabled else "0",
        EXPIRES_KEY: str(int(now + ttl)) if after else "",
    }


def apply_for(
    subsystem_id: str,
    *,
    now: float | None = None,
    path: str | None = None,
) -> bool:
    """Called by a daemon right after ``logging.basicConfig`` to raise
    its loggers to DEBUG iff that subsystem is actively toggled on.
    Returns whether debug was applied. Best-effort — never raises, so a
    malformed ``debug.env`` can't break daemon startup (fails toward
    normal INFO)."""
    sub = SUBSYSTEMS.get(subsystem_id)
    if sub is None:
        return False
    try:
        state = read_debug_state(path=path, now=now)
        if subsystem_id not in state.active:
            return False
        for name in sub.loggers:
            logging.getLogger(name).setLevel(logging.DEBUG)
    except Exception:  # pragma: no cover - defensive; startup must survive
        return False
    logging.getLogger(__name__).info(
        "debug mode ON for %s — verbose logging raised to DEBUG "
        "(auto-expires in ~%.0f min)",
        subsystem_id, state.remaining_sec / 60.0,
    )
    return True
