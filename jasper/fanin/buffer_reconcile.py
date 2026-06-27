# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime resize of the fan-in OUTPUT buffer (adaptive-buffer increment).

WHY THIS EXISTS — the latency lever. jasper-fanin's per-lane INPUT rings are
non-blocking and drained every period; a steady wired USB source keeps them
near-empty, so they are WiFi-burst headroom for AirPlay/Spotify, NOT a latency
source — shrinking the input ring would only cut burst headroom for networked
sources. The OUTPUT buffer is different: fan-in's ``writei()`` blocks until the
DAC-paced CamillaDSP pull opens room, so a steady source keeps the output
buffer near-FULL. That ~64 ms (default 3072 frames @ 48 kHz) is the real,
shrinkable end-to-end latency for an exclusive wired source. This module
shrinks it when USB is the sole active winner and restores the full default
otherwise.

FLOOR. CamillaDSP must always be able to read a full 1024-frame chunk from the
dsnoop capture side, so the output buffer cannot go below chunk + headroom
(``MIN_OUTPUT_BUFFER_FRAMES`` = 1536). fan-in's own ``Config::from_env`` also
hard-rejects anything below ``2 × period_frames`` (512 at the default 256-frame
period). 1536 satisfies both and is the default shrunk target; it is a named
constant and env-overridable (``JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES``) for the
soak sweep. We REJECT a requested value below the floor rather than clamp it —
writing an unstartable buffer into the persisted env would brick the daemon on
its next natural restart (the same no-silent-failure invariant the usbsink FIFO
arm protects).

IDIOM. Mirrors ``jasper.usbsink.output_mode_reconcile``: the reconciler is the
single writer of ``JASPER_FANIN_OUTPUT_BUFFER_FRAMES`` in the unit's
wizard-owned ``EnvironmentFile`` (``/var/lib/jasper/fanin.env``), and the
daemon reads the resolved env on its next start. The file is loaded by the unit
AFTER the hardcoded ``Environment="JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072"``,
so the override wins (systemd applies a later ``EnvironmentFile=`` over an
earlier ``Environment=``).

SF-1 (rollback). On a restart failure we ROLL BACK the env to its prior state
(restore prior contents, or unlink if the file did not exist) so the persisted
buffer size never gets ahead of the running daemon — a later natural restart
(deploy, reboot, watchdog) must never start on a value the daemon isn't
actually running. We return ``ok=False`` so the caller falls back to the full
buffer.

SF-2 (guarded lazy import). The ``jasper.control.restart_broker`` import is
guarded; a missing/broken control package degrades to a reported restart
failure (``ok=False``) instead of raising out of ``set_fanin_output_buffer``
and defeating the caller's fail-safe ladder.

KNOWN LIMITATION (documented, not fixed here). Restarting jasper-fanin is more
disruptive than the usbsink FIFO arm: fanin is the SHARED summing daemon that
CamillaDSP and the AEC bridge both read, so the bounce is a brief ALL-SOURCE
glitch on the transition, not just a single-lane re-take. That is acceptable
for a default-OFF measurement increment where the shrink only fires on an
exclusive-USB edge (the user just started USB and a sub-second re-take is
expected). A runtime PCM resize without a full restart is the Phase-2
follow-up.

CO-READER NOTE. ``/var/lib/jasper/fanin.env`` is a shared, multi-reader file:
``deploy/bin/jasper-apply-airplay-mode`` reads
``JASPER_FANIN_OUTPUT_BUFFER_FRAMES`` from it to fold the fan-in output queue
into the AirPlay A/V sync offset, and ``jasper-mux.service`` loads it as an
``EnvironmentFile``. The order-preserving upsert here touches only the
``JASPER_FANIN_OUTPUT_BUFFER_FRAMES`` line and leaves every other key intact, so
those co-readers are undisturbed. The AirPlay offset is unaffected in practice:
the shrink fires ONLY when USB is the sole source (AirPlay is not playing), and
``jasper-apply-airplay-mode`` re-reads the file at config-apply time, not
per-frame — by the time AirPlay plays again the buffer has been restored to
3072. The doctor's ``check_fanin_service`` reads
``JASPER_FANIN_ADAPTIVE_BUFFER`` and softens its hard "output buffer must be
3072" check to a warn while a floor-valid shrink is active.

Fail-soft on the I/O write (a bad byte must not crash the caller's tick), but
the RESULT carries ``ok`` so the caller's fail-safe ladder can keep/restore the
FULL buffer if the arm did not take.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
OUTPUT_BUFFER_KEY = "JASPER_FANIN_OUTPUT_BUFFER_FRAMES"
FANIN_UNIT = "jasper-fanin.service"

# The full, default output buffer (~64 ms @ 48 kHz). Mirrors the value
# hardcoded in deploy/systemd/jasper-fanin.service's Environment= line and
# jasper_fanin::config's documented default. A "restore" writes nothing and
# unlinks the override line so the unit's own default reasserts.
DEFAULT_OUTPUT_BUFFER_FRAMES = 3072

# Hard floor: CamillaDSP reads a full 1024-frame chunk from the dsnoop capture
# side, so the output buffer needs chunk + headroom. 1536 = 1024 + 512; it also
# clears fan-in's own 2×period_frames hard min (512 at the 256-frame default).
# A request below this is REJECTED, never clamped — persisting an unstartable
# buffer would brick the daemon on its next natural restart.
MIN_OUTPUT_BUFFER_FRAMES = 1536

# Default shrunk target for the exclusive-wired-USB path. Env-overridable so the
# on-device soak can sweep 3072 -> 2048 -> 1536 -> (1024, expected to fail the
# floor) without a redeploy.
_SHRUNK_FRAMES_ENV = "JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES"


def shrunk_target_frames() -> int:
    """The frame count the adaptive path shrinks the output buffer to.

    ``MIN_OUTPUT_BUFFER_FRAMES`` (1536) by default; overridable via
    ``JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES`` for the soak sweep. A malformed or
    below-floor override falls back to the floor with a warning rather than
    risking an unstartable value (``set_fanin_output_buffer`` would reject it
    anyway, but resolving to a safe value here keeps the caller's edge action
    from no-opping every tick on a typo'd env)."""
    raw = os.environ.get(_SHRUNK_FRAMES_ENV, "").strip()
    if not raw:
        return MIN_OUTPUT_BUFFER_FRAMES
    try:
        value = int(raw)
    except ValueError:
        log_event(
            logger,
            "fanin.adaptive_shrunk_frames_invalid",
            value=raw,
            fallback=MIN_OUTPUT_BUFFER_FRAMES,
            level=logging.WARNING,
        )
        return MIN_OUTPUT_BUFFER_FRAMES
    if value < MIN_OUTPUT_BUFFER_FRAMES:
        log_event(
            logger,
            "fanin.adaptive_shrunk_frames_below_floor",
            value=value,
            floor=MIN_OUTPUT_BUFFER_FRAMES,
            fallback=MIN_OUTPUT_BUFFER_FRAMES,
            level=logging.WARNING,
        )
        return MIN_OUTPUT_BUFFER_FRAMES
    return value


@dataclass(frozen=True)
class BufferResult:
    """Outcome of a set/restore call.

    ``ok`` is True only when BOTH the env write and the daemon restart
    succeeded (or there was nothing to do). ``changed`` is True when the env
    file's value actually moved (so the caller can suppress a redundant
    restart). ``restarted`` records whether the broker reported a successful
    restart. ``frames`` is the resolved target (the restore default when
    restoring)."""

    ok: bool
    changed: bool
    restarted: bool
    frames: int
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


def read_output_buffer(path: str | os.PathLike = FANIN_ENV_PATH) -> int | None:
    """The currently-written ``JASPER_FANIN_OUTPUT_BUFFER_FRAMES``, or None if
    the file/key is absent or unparseable as an int. None means "no override —
    the unit's default 3072 is live"."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    for key, value in _parse_env(text):
        if value is not None and key == OUTPUT_BUFFER_KEY:
            try:
                return int(value.strip().strip("'\""))
            except ValueError:
                return None
    return None


def _render_set(text: str, frames: int) -> tuple[str, bool]:
    """Upsert ``OUTPUT_BUFFER_KEY=frames`` into ``text``, preserving every
    other line. Returns (new_text, changed)."""
    lines = _parse_env(text)
    new_lines: list[str] = []
    found = False
    changed = False
    for key, value in lines:
        if value is not None and key == OUTPUT_BUFFER_KEY:
            found = True
            if value.strip().strip("'\"") != str(frames):
                changed = True
            new_lines.append(f"{OUTPUT_BUFFER_KEY}={frames}")
        else:
            new_lines.append(f"{key}={value}" if value is not None else key)
    if not found:
        new_lines.append(f"{OUTPUT_BUFFER_KEY}={frames}")
        changed = True
    return "\n".join(new_lines) + "\n", changed


def _render_restore(text: str) -> tuple[str, bool]:
    """Strip the ``OUTPUT_BUFFER_KEY`` override from ``text``, preserving every
    other line. Returns (new_text, changed). Removing the line (rather than
    writing 3072) lets the unit's own ``Environment=`` default reassert as the
    single source of truth for the full value."""
    lines = _parse_env(text)
    new_lines: list[str] = []
    changed = False
    for key, value in lines:
        if value is not None and key == OUTPUT_BUFFER_KEY:
            changed = True
            continue  # drop the override line
        new_lines.append(f"{key}={value}" if value is not None else key)
    body = "\n".join(new_lines)
    return (body + "\n" if body else ""), changed


def _restart_fanin(reason: str) -> tuple[bool, str]:
    """Restart the fanin daemon through the broker. Returns (ok, detail).

    SF-2: the lazy import is guarded so a missing/broken control package
    degrades to a reported restart failure (ok=False) instead of raising out of
    ``set_fanin_output_buffer`` and defeating the caller's fail-safe ladder
    (which relies on the BufferResult, not an exception, to keep/restore the
    full buffer)."""
    try:
        from jasper.control import restart_broker
    except ImportError as e:
        return False, f"restart_broker unavailable: {e}"

    resp = restart_broker.manage_units(
        FANIN_UNIT, verb="restart", reason=reason, no_block=False, timeout=8.0,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def _apply(
    *,
    frames: int | None,
    reason: str,
    env_path: str | os.PathLike,
    restart: bool,
) -> BufferResult:
    """Shared write+restart+rollback core for set and restore.

    ``frames is None`` means RESTORE (strip the override; the daemon falls back
    to its unit default); an int means SET that value. ``frames`` is the value
    reported in the result (the default when restoring)."""
    reported = DEFAULT_OUTPUT_BUFFER_FRAMES if frames is None else frames

    path = Path(env_path)
    file_existed = path.exists()
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""

    if frames is None:
        new_text, changed = _render_restore(existing)
    else:
        new_text, changed = _render_set(existing, frames)

    if not changed:
        # Already at the requested state. Nothing to write, nothing to restart.
        log_event(
            logger, "fanin.output_buffer_arm", result="unchanged",
            frames=reported, reason=reason,
        )
        return BufferResult(
            ok=True, changed=False, restarted=False, frames=reported,
        )

    # RESTORE that empties the file: unlink rather than leave a 0-byte file, so
    # the override truly disappears and the unit default is the only source.
    write_is_unlink = frames is None and new_text == ""
    try:
        if write_is_unlink:
            if file_existed:
                path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, new_text)
    except OSError as e:
        log_event(
            logger, "fanin.output_buffer_arm", result="write_failed",
            frames=reported, reason=reason, error=e, level=logging.ERROR,
        )
        return BufferResult(
            ok=False, changed=False, restarted=False, frames=reported,
            detail=str(e),
        )

    if not restart:
        log_event(
            logger, "fanin.output_buffer_arm", result="written",
            frames=reported, reason=reason,
        )
        return BufferResult(
            ok=True, changed=True, restarted=False, frames=reported,
        )

    restarted, detail = _restart_fanin(reason=reason)
    if not restarted:
        # SF-1: the env now carries the new value but the daemon did NOT restart
        # into it, so the persisted file is AHEAD of the running daemon. A later
        # NATURAL restart (deploy, reboot, watchdog) would then start on a buffer
        # the box wasn't cleanly running. Roll the env back to its prior state so
        # the file never gets ahead of the daemon; the caller keeps/restores the
        # full buffer on ok=False.
        rollback_detail = ""
        try:
            if file_existed:
                atomic_write_text(path, existing)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            rollback_detail = f"; rollback_failed={e}"
        log_event(
            logger, "fanin.output_buffer_arm",
            result="restart_failed_rolled_back", frames=reported,
            reason=reason, detail=(detail + rollback_detail) or None,
            level=logging.WARNING,
        )
        return BufferResult(
            ok=False, changed=False, restarted=False, frames=reported,
            detail=detail + rollback_detail,
        )

    log_event(
        logger, "fanin.output_buffer_arm", result="armed", frames=reported,
        reason=reason, detail=detail or None, level=logging.INFO,
    )
    return BufferResult(
        ok=True, changed=True, restarted=True, frames=reported, detail=detail,
    )


def set_fanin_output_buffer(
    frames: int,
    *,
    reason: str,
    env_path: str | os.PathLike = FANIN_ENV_PATH,
    restart: bool = True,
) -> BufferResult:
    """Write ``JASPER_FANIN_OUTPUT_BUFFER_FRAMES=<frames>`` and restart the
    daemon.

    ``frames`` MUST be >= ``MIN_OUTPUT_BUFFER_FRAMES`` (1536). A below-floor
    value returns ``ok=False`` WITHOUT touching the file — never write an
    unstartable buffer (CamillaDSP could not read a full 1024-frame chunk, and
    the persisted value would brick the daemon on its next natural restart).

    No-op fast path: when the env file already carries ``frames``, skips the
    restart and returns ``ok=True, changed=False`` so a re-armed tick does not
    glitch the shared audio path.

    Fail-soft on the write (OSError -> ``ok=False``). SF-1 rolls the env back if
    the restart fails so the persisted value never leads the running daemon.
    """
    if frames < MIN_OUTPUT_BUFFER_FRAMES:
        log_event(
            logger, "fanin.output_buffer_arm", result="below_floor",
            frames=frames, floor=MIN_OUTPUT_BUFFER_FRAMES, level=logging.ERROR,
        )
        return BufferResult(
            ok=False, changed=False, restarted=False, frames=frames,
            detail=f"{frames} below floor {MIN_OUTPUT_BUFFER_FRAMES}",
        )
    return _apply(
        frames=frames, reason=reason, env_path=env_path, restart=restart,
    )


def restore_fanin_output_buffer(
    *,
    reason: str,
    env_path: str | os.PathLike = FANIN_ENV_PATH,
    restart: bool = True,
) -> BufferResult:
    """Restore the FULL default output buffer by stripping the override line.

    The unit's hardcoded ``Environment="JASPER_FANIN_OUTPUT_BUFFER_FRAMES=3072"``
    then reasserts as the single source of truth for the full value. NO-OP
    (``ok=True, changed=False``) when no override is present — the common
    steady-state path, so a default-OFF / already-full tick never restarts the
    shared daemon.

    Same SF-1 rollback + fail-soft contract as ``set_fanin_output_buffer``.
    """
    return _apply(
        frames=None, reason=reason, env_path=env_path, restart=restart,
    )
