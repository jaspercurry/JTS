# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime arm/disarm of the usbsink FIFO output mode (Stage 4b-iv).

The usbsink ``output_mode`` (``aloop`` | ``fifo``) is fixed at AudioBridge
construction (``DaemonConfig.from_env`` reads ``JASPER_USBSINK_OUTPUT_MODE``
once at start). The lean lane needs to flip it *at runtime* when USB becomes
the sole exclusive source. There is no in-process control surface for it, and
adding one would mean re-opening the bridge's PortAudio streams live — fiddly
and not worth it for a transition that is already an exclusive-source switch.

The JTS-idiomatic move (mirrors ``jasper-aec-reconcile``: the reconciler is the
single env writer, the daemon reads the resolved env on its next start) is to
write ``JASPER_USBSINK_OUTPUT_MODE`` into the unit's wizard-owned
``EnvironmentFile`` (``/var/lib/jasper/usbsink.env``) and restart the daemon
through jasper-control's restart broker. The restart costs a brief audio glitch
on the USB lane — acceptable on an exclusive-source transition, where the user
just selected/started USB and a sub-second re-take is expected.

``output_mode.env`` lives in the unit already
(``EnvironmentFile=-/var/lib/jasper/usbsink.env``), loaded AFTER
``/etc/jasper/jasper.env`` so it wins on conflict — the same last-file-wins
override pattern as every other wizard env file. This module is the single
writer of the ``JASPER_USBSINK_OUTPUT_MODE`` line in it; it preserves any other
operator-set ``JASPER_USBSINK_*`` overrides already in the file.

Fail-soft on the I/O write (a bad byte must not crash the caller's tick), but
the RESULT carries ``ok`` so the caller's enter-lean ladder can fall back to
buffered if the arm did not take.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

USBSINK_ENV_PATH = "/var/lib/jasper/usbsink.env"
OUTPUT_MODE_KEY = "JASPER_USBSINK_OUTPUT_MODE"
USBSINK_UNIT = "jasper-usbsink.service"

_VALID_MODES = ("aloop", "fifo")


@dataclass(frozen=True)
class ArmResult:
    """Outcome of an arm/disarm call.

    ``ok`` is True only when BOTH the env write and the daemon restart
    succeeded. ``changed`` is True when the env file's value actually moved
    (so the caller can suppress a redundant restart). ``restarted`` records
    whether the broker reported a successful restart.
    """

    ok: bool
    changed: bool
    restarted: bool
    mode: str
    detail: str = ""


def _parse_env(text: str) -> "list[tuple[str, str | None]]":
    """Parse env-file lines into (key, value) for assignments, or
    (raw_line, None) for comments/blanks/malformed lines we preserve
    verbatim. Order-preserving so an operator's file isn't reshuffled."""
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append((raw, None))
            continue
        key, _, value = stripped.partition("=")
        out.append((key.strip(), value))
    return out


def read_output_mode(path: str | os.PathLike = USBSINK_ENV_PATH) -> str | None:
    """The currently-written ``JASPER_USBSINK_OUTPUT_MODE``, or None if the
    file/key is absent. Does NOT validate — a malformed value is returned
    as-is so the caller can decide (the daemon's own ``_parse_output_mode``
    is the validator of record)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for key, value in _parse_env(text):
        if value is not None and key == OUTPUT_MODE_KEY:
            return value.strip().strip("'\"")
    return None


def _render_env(text: str, mode: str) -> tuple[str, bool]:
    """Upsert ``OUTPUT_MODE_KEY=mode`` into ``text``, preserving every other
    line. Returns (new_text, changed)."""
    lines = _parse_env(text)
    new_lines: list[str] = []
    found = False
    changed = False
    for key, value in lines:
        if value is not None and key == OUTPUT_MODE_KEY:
            found = True
            if value.strip().strip("'\"") != mode:
                changed = True
            new_lines.append(f"{OUTPUT_MODE_KEY}={mode}")
        else:
            # Preserve assignments and comment/blank lines verbatim.
            new_lines.append(f"{key}={value}" if value is not None else key)
    if not found:
        new_lines.append(f"{OUTPUT_MODE_KEY}={mode}")
        changed = True
    return "\n".join(new_lines) + "\n", changed


def _restart_usbsink(reason: str) -> tuple[bool, str]:
    """Restart the usbsink daemon through the broker. Returns (ok, detail).
    Lazy import keeps jasper-mux's dep graph free of the control package
    on Pis where the gadget feature is off. SF-2: the import is guarded so a
    missing/broken control package degrades to a reported restart failure
    (ok=False) instead of raising out of set_output_mode and defeating the
    caller's fail-soft enter-lean ladder (which relies on the ArmResult, not an
    exception, to fall back to buffered + arm the per-episode block)."""
    try:
        from jasper.control import restart_broker
    except ImportError as e:
        return False, f"restart_broker unavailable: {e}"

    resp = restart_broker.manage_units(
        USBSINK_UNIT, verb="restart", reason=reason, no_block=False, timeout=8.0,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def set_output_mode(
    mode: str,
    *,
    reason: str,
    env_path: str | os.PathLike = USBSINK_ENV_PATH,
    restart: bool = True,
) -> ArmResult:
    """Write ``JASPER_USBSINK_OUTPUT_MODE=<mode>`` and restart the daemon.

    ``mode`` must be ``"aloop"`` or ``"fifo"`` (the only two the daemon's own
    parser accepts). An invalid mode returns ``ok=False`` without touching the
    file — never silently coerce, since the wrong mode would silently route
    audio.

    No-op fast path: when the env file already carries ``mode`` AND
    ``restart`` is requested, we STILL restart only if the value changed.
    A redundant arm (same value) skips the restart and returns
    ``ok=True, changed=False`` so a re-armed tick does not glitch audio.

    Fail-soft on the write (OSError is caught and reported via ``ok=False``);
    the caller's enter-lean ladder treats ``ok=False`` as "fall back to
    buffered".
    """
    if mode not in _VALID_MODES:
        log_event(
            logger,
            "usbsink.output_mode_arm",
            result="invalid_mode",
            mode=mode,
            level=logging.ERROR,
        )
        return ArmResult(ok=False, changed=False, restarted=False, mode=mode,
                         detail=f"invalid mode {mode!r}")

    path = Path(env_path)
    file_existed = path.exists()
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    new_text, changed = _render_env(existing, mode)

    if not changed:
        # Already at the requested mode. Nothing to write, nothing to
        # restart — the running daemon is already in this mode (or will be on
        # its next natural restart; we don't force one for a no-op).
        log_event(logger, "usbsink.output_mode_arm", result="unchanged", mode=mode)
        return ArmResult(ok=True, changed=False, restarted=False, mode=mode)

    try:
        atomic_write_text(path, new_text)
    except OSError as e:
        log_event(
            logger,
            "usbsink.output_mode_arm",
            result="write_failed",
            mode=mode,
            error=e,
            level=logging.ERROR,
        )
        return ArmResult(ok=False, changed=False, restarted=False, mode=mode,
                         detail=str(e))

    if not restart:
        log_event(logger, "usbsink.output_mode_arm", result="written", mode=mode)
        return ArmResult(ok=True, changed=True, restarted=False, mode=mode)

    restarted, detail = _restart_usbsink(reason=reason)
    if not restarted:
        # SF-1: the env now carries `mode` but the daemon did NOT restart into
        # it, so the persisted file is AHEAD of the running daemon. A later
        # NATURAL restart (deploy, reboot, watchdog) would then start in `mode`
        # — e.g. fifo writing a pipe nobody reads -> SILENT USB audio with no
        # cue, the exact no-silent-failure invariant JTS treats as inviolable.
        # Roll the env back to its prior state so the file never gets ahead of
        # the running daemon; the caller falls back to buffered on ok=False.
        rollback_detail = ""
        try:
            if file_existed:
                atomic_write_text(path, existing)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            rollback_detail = f"; rollback_failed={e}"
        log_event(
            logger,
            "usbsink.output_mode_arm",
            result="restart_failed_rolled_back",
            mode=mode,
            reason=reason,
            detail=(detail + rollback_detail) or None,
            level=logging.WARNING,
        )
        return ArmResult(ok=False, changed=False, restarted=False, mode=mode,
                         detail=detail + rollback_detail)

    log_event(
        logger,
        "usbsink.output_mode_arm",
        result="armed",
        mode=mode,
        reason=reason,
        detail=detail or None,
        level=logging.INFO,
    )
    return ArmResult(ok=True, changed=True, restarted=True, mode=mode, detail=detail)
