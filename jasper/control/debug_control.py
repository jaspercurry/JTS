# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Control-side orchestration for the runtime debug-logging toggle.

``jasper-control`` owns the *write* side of the debug toggle (the read
+ pure helpers live in :mod:`jasper.debug_mode`). Responsibilities:

* persist a toggle to ``/var/lib/jasper/debug.env`` (atomic rewrite,
  preserving other keys — same idiom as the AEC-leg toggle);
* apply it — restart the target daemon so its ``apply_for`` runs again,
  defer optional daemons that are not running, or set the level **in
  process** for ``control`` itself (restarting jasper-control would drop
  the request + the expiry timer — a self-restart footgun);
* enforce **auto-expiry** — a single ``threading.Timer`` clears the
  flags when the shared TTL elapses while each daemon self-quiets its
  journal handler in process, so a forgotten toggle self-heals back to
  INFO without a restart. Re-armed on every change and reconciled on
  jasper-control startup.

Threading model: jasper-control is a stdlib threaded HTTP server (no
persistent asyncio loop on the request path), so expiry uses a plain
daemon ``threading.Timer`` rather than ``loop.call_later``.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from jasper.log_event import log_event

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


def _unit_is_active(unit: str) -> bool:
    """Whether systemd reports the unit active. Raises on spawn failure so
    the endpoint can surface a real apply-path problem on the Pi."""
    proc = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
        timeout=3,
    )
    return proc.returncode == 0


def _restart_unit_if_active(subsystem: str, unit: str, enabled: bool) -> None:
    """Apply debug to optional daemons without changing source enablement.

    ``systemctl restart`` starts inactive services, which would make the
    Debug card an accidental source toggle for optional renderers like USB
    input. If the unit is stopped, leave the env flag in place and let the
    daemon pick it up on its next legitimate start.
    """
    if _unit_is_active(unit):
        _restart_unit(unit)
        return
    log_event(
        logger,
        "debug.apply_deferred",
        subsystem=subsystem,
        unit=unit,
        enabled=enabled,
        reason="unit_inactive",
    )


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
    """Timer callback: the shared TTL elapsed. Clear the debug.env SSOT (so
    `/state` reads off and the next daemon start is clean). Every daemon —
    including control — quiets its own journal handler via its per-process
    self-quiet timer (``debug_mode.apply_for``); no restart."""
    with _lock:
        _atomic_write(_clear_all())
        _cancel_timer_locked()
    log_event(logger, "debug.expired")


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
    sub = SUBSYSTEMS[subsystem]
    if sub.apply_policy == "in_process":
        # In-process — no self-restart. apply_for re-reads debug.env (just
        # written), moves control's journal handler, and (re-)arms/cancels
        # control's per-process self-quiet timer — same path as the
        # daemon subsystems.
        # Pass `now` so the expiry check matches the just-written timestamp.
        debug_mode.apply_for("control", now=now)
    elif sub.apply_policy == "restart_if_active":
        _restart_unit_if_active(subsystem, sub.unit, enabled)
    else:
        _restart_unit(sub.unit)
    log_event(
        logger,
        "debug.toggle",
        subsystem=subsystem,
        enabled=enabled,
        remaining_sec=f"{state.remaining_sec:.0f}",
        client="control",
    )
    return state


def snapshot(now: float | None = None) -> dict:
    """State for ``/state.debug`` and the ``GET /debug`` endpoint. The
    per-subsystem ``enabled`` reflects the *effective* (expiry-applied)
    state, so a configured-but-expired toggle reads as off."""
    st = debug_mode.read_debug_state(now=now)
    return {
        "subsystems": [
            {
                "id": s.id,
                "label": s.label,
                "enabled": s.id in st.active,
                "apply_policy": s.apply_policy,
            }
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
