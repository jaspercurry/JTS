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
buffer near-FULL. The old 3072-frame default was therefore a real latency
source. The production default is now 1024 frames (~21 ms at 48 kHz), validated
on JTS2 with the low-latency Apple-DAC Camilla path (chunksize=256,
target_level=1536). This module remains as the single safe writer for lab
overrides and for older deployments that still carry a larger persisted value.

FLOOR. 1024 frames is the production floor. fan-in's own ``Config::from_env``
also hard-rejects anything below ``2 × period_frames`` (512 at the default
256-frame period), but values below 1024 are lab-only until a real hardware
soak proves they do not cause AirPlay tears or Camilla underruns. We REJECT a
requested value below the floor rather than clamp it — writing an unvalidated
buffer into the persisted env could break the daemon on its next natural
restart (the same no-silent-failure invariant every JTS reconciler protects).

IDIOM. Mirrors ``jasper-aec-reconcile``: the reconciler is the
single writer of ``JASPER_FANIN_OUTPUT_BUFFER_FRAMES`` in the unit's
wizard-owned ``EnvironmentFile`` (``/var/lib/jasper/fanin.env``), and the
daemon reads the resolved env on its next start. The file is loaded by the unit
AFTER the hardcoded ``Environment="JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024"``,
so the override wins (systemd applies a later ``EnvironmentFile=`` over an
earlier ``Environment=``).

SF-1 (rollback). On a restart failure we ROLL BACK the env to its prior state
(restore prior contents, or unlink if the file did not exist) so the persisted
buffer size never gets ahead of the running daemon — a later natural restart
(deploy, reboot, watchdog) must never start on a value the daemon isn't
actually running. We return ``ok=False`` so the caller falls back to the full
buffer.

SF-2 (guarded lazy import). The restart routes through
:func:`jasper.fanin.coupling_reconcile.coordinated_fanin_restart`, whose broker
access keeps the guarded lazy ``jasper.control.restart_broker`` import; a
missing/broken control package degrades to a reported restart failure
(``ok=False``) instead of raising out of ``set_fanin_output_buffer`` and
defeating the caller's fail-safe ladder.

COORDINATION. On a live ring/pipe coupling (``shm_ring`` / ``transport_pipe``)
a bare fan-in restart detaches the ring writer under CamillaDSP and
RTTIME-SIGKILLs it (see ``_restart_fanin_coordinated`` in
:mod:`jasper.fanin.coupling_reconcile`), so the restart here is
CamillaDSP-coordinated: stop camilla -> restart fan-in -> start camilla, read
fresh from the SAME env file this module writes. Loopback keeps its single
plain fan-in restart.

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
per-frame. The doctor's ``check_fanin_service`` accepts the 1024-frame
production floor and fails values below it.

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
from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES,
    FANIN_ADAPTIVE_SHRUNK_FRAMES_ENV,
    FANIN_OUTPUT_BUFFER_KEY,
    MIN_FANIN_OUTPUT_BUFFER_FRAMES,
    RuntimeEnvAction,
    fanin_output_buffer_action,
    resolve_fanin_output_buffer_target,
)
from jasper.audio_runtime_overrides import load_runtime_overrides, runtime_overrides_path
from jasper.env_file import read_value, remove, upsert
from jasper.fanin.coupling_reconcile import coordinated_fanin_restart
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
OUTPUT_BUFFER_KEY = FANIN_OUTPUT_BUFFER_KEY
FANIN_UNIT = "jasper-fanin.service"

# The default output buffer (~21 ms @ 48 kHz). Mirrors the value
# hardcoded in deploy/systemd/jasper-fanin.service's Environment= line and
# jasper_fanin::config's documented default. A "restore" writes nothing and
# unlinks the override line so the unit's own default reasserts.
DEFAULT_OUTPUT_BUFFER_FRAMES = DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES

# Production floor. Sub-1024 is deliberately kept out of defaults until a
# hardware soak proves it is clean across the active DAC/Camilla paths.
MIN_OUTPUT_BUFFER_FRAMES = MIN_FANIN_OUTPUT_BUFFER_FRAMES

# Default shrunk target for the exclusive-wired-USB path. Env-overridable so the
# on-device soak can sweep candidate values without a redeploy.
_SHRUNK_FRAMES_ENV = FANIN_ADAPTIVE_SHRUNK_FRAMES_ENV


def shrunk_target_frames() -> int:
    """The frame count the adaptive path shrinks the output buffer to.

    ``MIN_OUTPUT_BUFFER_FRAMES`` (1024) by default; overridable via
    ``JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES`` for the soak sweep. A malformed or
    below-floor override falls back to the floor with a warning rather than
    risking an unstartable value (``set_fanin_output_buffer`` would reject it
    anyway, but resolving to a safe value here keeps the caller's edge action
    from no-opping every tick on a typo'd env)."""
    override_path = runtime_overrides_path(os.environ)
    overrides = load_runtime_overrides(
        override_path,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    target = resolve_fanin_output_buffer_target(os.environ, overrides=overrides.values())
    if target.warning_event == "fanin.adaptive_shrunk_frames_invalid":
        log_event(
            logger,
            target.warning_event,
            value=target.raw_value,
            fallback=MIN_OUTPUT_BUFFER_FRAMES,
            level=logging.WARNING,
        )
    elif target.warning_event == "fanin.adaptive_shrunk_frames_below_floor":
        log_event(
            logger,
            target.warning_event,
            value=target.raw_value,
            floor=MIN_OUTPUT_BUFFER_FRAMES,
            fallback=MIN_OUTPUT_BUFFER_FRAMES,
            level=logging.WARNING,
        )
    return target.frames


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


def read_output_buffer(path: str | os.PathLike = FANIN_ENV_PATH) -> int | None:
    """The currently-written ``JASPER_FANIN_OUTPUT_BUFFER_FRAMES``, or None if
    the file/key is absent or unparseable as an int. None means "no override —
    the unit's default 1024 is live"."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    raw = read_value(text, OUTPUT_BUFFER_KEY)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _restart_fanin(reason: str, *, env_path: str | os.PathLike) -> tuple[bool, str]:
    """Restart the fanin daemon, CamillaDSP-coordinated. Returns (ok, detail).

    Routes through ``coordinated_fanin_restart`` so an adaptive-buffer restart
    on a live ring/pipe coupling pauses camilla around the fan-in bounce
    (stop -> restart -> start) instead of RTTIME-SIGKILLing it; loopback stays a
    plain broker restart. ``env_path`` is the same fanin.env this module just
    wrote — the coupling line lives in that file and the helper re-reads it
    fresh. (A restore that UNLINKED the file is safe: the unlink only happens
    when the buffer line was the file's sole key, so the coupling key was
    already absent and the missing-file loopback fail-safe is the same answer
    it would have read before.) ``ok`` is "fan-in restarted", exactly what
    SF-1's rollback keys off:
    a camilla-resume failure after a successful fan-in restart does NOT roll the
    env back (the daemon IS running the new value; OnFailure recovery owns the
    resume).

    SF-2: the broker's lazy import stays guarded inside the coordinated helper,
    so a missing/broken control package degrades to a reported restart failure
    (ok=False) instead of raising out of ``set_fanin_output_buffer`` and
    defeating the caller's fail-safe ladder (which relies on the BufferResult,
    not an exception, to keep/restore the full buffer)."""
    return coordinated_fanin_restart(
        reason, phase="adaptive_buffer", env_path=env_path,
    )


def _apply(
    *,
    action: RuntimeEnvAction,
    reported: int,
    reason: str,
    env_path: str | os.PathLike,
    restart: bool,
) -> BufferResult:
    """Shared write+restart+rollback core for set and restore.

    ``action`` carries the plan-owned set/unset decision; ``reported`` is the
    frame count reported to logs/callers (the default when restoring)."""

    path = Path(env_path)
    file_existed = path.exists()
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""

    if action.action == "unset":
        new_text, changed = remove(existing, action.key)
    else:
        new_text, changed = upsert(existing, action.key, action.value)

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
    write_is_unlink = action.action == "unset" and new_text == ""
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

    restarted, detail = _restart_fanin(reason=reason, env_path=path)
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

    ``frames`` MUST be >= ``MIN_OUTPUT_BUFFER_FRAMES`` (1024). A below-floor
    value returns ``ok=False`` WITHOUT touching the file — never write an
    unvalidated buffer that would take effect on the next natural restart.

    No-op fast path: when the env file already carries ``frames``, skips the
    restart and returns ``ok=True, changed=False`` so a re-armed tick does not
    glitch the shared audio path.

    Fail-soft on the write (OSError -> ``ok=False``). SF-1 rolls the env back if
    the restart fails so the persisted value never leads the running daemon.
    """
    try:
        action = fanin_output_buffer_action(frames)
    except ValueError as e:
        log_event(
            logger, "fanin.output_buffer_arm", result="below_floor",
            frames=frames, floor=MIN_OUTPUT_BUFFER_FRAMES, level=logging.ERROR,
        )
        return BufferResult(
            ok=False, changed=False, restarted=False, frames=frames,
            detail=str(e),
        )
    return _apply(
        action=action, reported=frames, reason=reason,
        env_path=env_path, restart=restart,
    )


def restore_fanin_output_buffer(
    *,
    reason: str,
    env_path: str | os.PathLike = FANIN_ENV_PATH,
    restart: bool = True,
) -> BufferResult:
    """Restore the FULL default output buffer by stripping the override line.

    The unit's hardcoded ``Environment="JASPER_FANIN_OUTPUT_BUFFER_FRAMES=1024"``
    then reasserts as the single source of truth for the full value. NO-OP
    (``ok=True, changed=False``) when no override is present — the common
    steady-state path, so a default-OFF / already-full tick never restarts the
    shared daemon.

    Same SF-1 rollback + fail-soft contract as ``set_fanin_output_buffer``.
    """
    action = fanin_output_buffer_action(None)
    return _apply(
        action=action, reported=DEFAULT_OUTPUT_BUFFER_FRAMES,
        reason=reason, env_path=env_path, restart=restart,
    )
