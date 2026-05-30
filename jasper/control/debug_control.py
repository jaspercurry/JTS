"""Control-side orchestration for the runtime debug-logging toggle.

``jasper-control`` owns the *write* side of the debug toggle (the read
+ pure helpers live in :mod:`jasper.debug_mode`). Responsibilities:

* persist a toggle to ``/var/lib/jasper/debug.env`` (atomic rewrite,
  preserving other keys — same idiom as the AEC-leg toggle);
* apply it — restart the target daemon so its ``apply_for`` runs again
  (``voice`` / ``aec``), or set the level **in process** for
  ``control`` itself (restarting jasper-control would drop the request
  + the expiry timer — a self-restart footgun, so it's special-cased);
* enforce **auto-expiry** — a single ``threading.Timer`` clears the
  flags + restarts the affected daemons when the shared TTL elapses, so
  a forgotten toggle self-heals back to INFO. Re-armed on every change
  and reconciled on jasper-control startup.

Threading model: jasper-control is a stdlib threaded HTTP server (no
persistent asyncio loop on the request path), so expiry uses a plain
daemon ``threading.Timer`` rather than ``loop.call_later``.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from .. import debug_mode
from ..debug_mode import EXPIRES_KEY, SUBSYSTEMS, env_key
from ..web._common import read_env_file, write_env_file

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_timer: "threading.Timer | None" = None


# ----------------------------------------------------------- file write


def _read_env() -> dict[str, str]:
    try:
        return read_env_file(debug_mode.DEBUG_FILE)
    except OSError:
        return {}  # first-ever toggle: file not seeded yet


def _atomic_write(updates: dict[str, str]) -> None:
    """Read-modify-write of debug.env preserving unrelated keys. Mirrors
    server._atomic_rewrite_env; duplicated (not imported) to avoid a
    circular import with the server module."""
    state = _read_env()
    state.update(updates)
    write_env_file(debug_mode.DEBUG_FILE, state, mode=0o644)


def _clear_all() -> dict[str, str]:
    return {**{env_key(sid): "0" for sid in SUBSYSTEMS}, EXPIRES_KEY: ""}


# ------------------------------------------------------------ apply side


def _restart_unit(unit: str) -> None:
    """Best-effort non-blocking restart. Raises on spawn failure so the
    endpoint can surface it; the file write has already landed, so the
    change still applies on the daemon's next start."""
    subprocess.Popen(["systemctl", "restart", "--no-block", unit])


def _apply_control_level(enabled: bool) -> None:
    """``control`` debug is applied in-process — no self-restart. The flight
    recorder (Tier C) holds the logger at DEBUG, so the toggle moves the
    journal handler (DEBUG on, INFO off); raising the logger too covers the
    recorder-disabled case."""
    if enabled:
        for name in SUBSYSTEMS["control"].loggers:
            logging.getLogger(name).setLevel(logging.DEBUG)
    debug_mode.set_console_debug(enabled)


# Seam for tests: swap out the timer factory so unit tests don't spawn
# real background threads. Returns an object with .cancel().
def _schedule(delay: float, fn) -> "threading.Timer":
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


def _cancel_timer_locked() -> None:
    global _timer
    if _timer is not None:
        _timer.cancel()
        _timer = None


def _arm_expiry_locked(state: debug_mode.DebugState, now: float) -> None:
    global _timer
    _cancel_timer_locked()
    if state.active and state.expires_at is not None:
        _timer = _schedule(max(0.0, state.expires_at - now), _on_expiry)


# --------------------------------------------------------------- expiry


def _on_expiry() -> None:
    """Timer callback: the shared TTL elapsed. Clear every flag and drop
    the affected daemons back to INFO so the speaker quiets itself."""
    now = time.time()
    with _lock:
        state = debug_mode.resolve_debug_state(_read_env(), now)
        external = [
            SUBSYSTEMS[sid].unit
            for sid in state.configured
            if sid != "control"
        ]
        control_was_on = "control" in state.configured
        _atomic_write(_clear_all())
        _cancel_timer_locked()
    for unit in external:
        try:
            _restart_unit(unit)
        except (OSError, subprocess.SubprocessError):
            logger.exception("event=debug.expire_restart_failed unit=%s", unit)
    if control_was_on:
        _apply_control_level(False)
    logger.info(
        "event=debug.expired restarted=%s", ",".join(external) or "none",
    )


# ------------------------------------------------------------- public API


def set_debug(
    subsystem: str, enabled: bool, *, now: float | None = None,
) -> debug_mode.DebugState:
    """Toggle one subsystem's debug logging. Persists + applies + arms
    expiry. Returns the resulting state. Raises ``ValueError`` on an
    unknown subsystem; lets a restart spawn failure propagate (the file
    write has already landed)."""
    if subsystem not in SUBSYSTEMS:
        raise ValueError(f"unknown debug subsystem: {subsystem!r}")
    now = time.time() if now is None else now
    with _lock:
        updates = debug_mode.compute_env_update(
            _read_env(), subsystem, enabled, now=now,
        )
        _atomic_write(updates)
        state = debug_mode.read_debug_state(now=now)
        _arm_expiry_locked(state, now)
    if subsystem == "control":
        _apply_control_level(subsystem in state.active)
    else:
        _restart_unit(SUBSYSTEMS[subsystem].unit)
    logger.info(
        "event=debug.toggle subsystem=%s enabled=%s remaining_sec=%.0f client=control",
        subsystem, enabled, state.remaining_sec,
    )
    return state


def snapshot(now: float | None = None) -> dict:
    """State for ``/state.debug`` and the ``GET /debug`` endpoint. The
    per-subsystem ``enabled`` reflects the *effective* (expiry-applied)
    state, so a configured-but-expired toggle reads as off."""
    st = debug_mode.read_debug_state(now=now)
    return {
        "subsystems": [
            {"id": s.id, "label": s.label, "enabled": s.id in st.active}
            for s in SUBSYSTEMS.values()
        ],
        "any_active": bool(st.active),
        "expires_at": st.expires_at,
        "remaining_sec": round(st.remaining_sec),
        "ttl_sec": debug_mode.DEFAULT_TTL_SEC,
    }


def reconcile_on_startup(now: float | None = None) -> None:
    """Called once from jasper-control ``main``. If the persisted state
    is expired (or empty with a stray expiry), clean the file — the
    daemons already came up at INFO because ``apply_for`` treats expired
    as off. If still active, re-arm the expiry timer for the remaining
    window so the auto-quiet still fires after a control restart."""
    now = time.time() if now is None else now
    with _lock:
        cur = _read_env()
        state = debug_mode.resolve_debug_state(cur, now)
        if not state.active:
            has_residue = cur.get(EXPIRES_KEY) or any(
                (cur.get(env_key(sid)) or "").strip() == "1" for sid in SUBSYSTEMS
            )
            if has_residue:
                _atomic_write(_clear_all())
            _cancel_timer_locked()
        else:
            _arm_expiry_locked(state, now)
