# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered arm/disarm of the fan-in -> CamillaDSP coupling (loopback <-> fifo).

WHY THIS EXISTS — the two daemons must transition in a specific order.
:mod:`jasper.fanin_coupling` owns the *vocabulary* (the flag, the pipe path, the
emit kwargs); this module owns the *transition*. CamillaDSP's ``RawFile``
capture (the ``fifo`` coupling) reads the named pipe ``jasper-fanin`` writes, and
CamillaDSP **crash-loops on its statefile RawFile config whenever the pipe has no
writer** (verified on jts5 / CamillaDSP 4.1.3, 2026-06-27). So the order is not
optional:

- **ARM** (loopback -> fifo): fan-in MUST be writing the pipe BEFORE CamillaDSP
  loads the RawFile config, because the apply's ``camilladsp --check`` OPENS the
  pipe. Order: write env=fifo -> restart fan-in (writes pipe) -> reconcile
  CamillaDSP (emits + loads RawFile). On a clean reboot the systemd order
  (jasper-fanin ``Before`` jasper-camilla) already gives the same rendezvous, so
  an armed box survives a cold boot without this reconciler running.

- **DISARM** (fifo -> loopback): CamillaDSP must leave the RawFile config (->
  Alsa loopback) BEFORE fan-in stops writing the pipe, else camilla EOFs and
  crash-loops on its statefile RawFile config. Order: write env=loopback ->
  reconcile CamillaDSP (emits + loads Alsa) -> restart fan-in (loopback). A
  sub-second silence spans the camilla-swap -> fan-in-restart window; that is
  acceptable on a deliberate operator transition and it NEVER strands camilla on
  a config it cannot open.

SINGLE WRITER. This module is the sole writer of
``JASPER_FANIN_CAMILLA_COUPLING`` in the reconciler-owned
``/var/lib/jasper/fanin.env`` — the same file ``jasper-fanin`` and ``jasper-mux``
load (fanin.env wins over the unit ``Environment=`` defaults), and which the
adaptive-buffer sibling (:mod:`jasper.fanin.buffer_reconcile`) also owns one key
in. The order-preserving single-key upsert (:mod:`jasper.env_file`) leaves the
buffer key + every operator line intact.

FAIL-SAFE DIRECTION = loopback (the byte-identical-to-today path). Any failure
during ARM rolls the whole transition back to loopback (env + camilla + fan-in)
so a half-applied coupling never strands the realtime path. ``reconcile_camilla``
itself fail-closes on an invalid config (CamillaDSP ``--check`` rejects it; the
apply never loads it), so the worst case is "stayed on / reverted to loopback",
never a bricked DSP. The result carries ``ok`` so a caller's own ladder can
react; daemon-op failures are reported, not raised.

NOT a per-tick hot path. This runs on a deliberate coupling change (a CLI / the
deploy), not in the mux loop — a real transition bounces the SHARED fan-in
daemon (a brief all-source glitch), which is why it is change-gated, not polled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.audio_runtime_plan import RouteMode, fanin_coupling_action
from jasper.env_file import read_value, remove, upsert
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_FIFO,
    COUPLING_LOOPBACK,
    resolve_coupling,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

FANIN_ENV_PATH = "/var/lib/jasper/fanin.env"
FANIN_UNIT = "jasper-fanin.service"
ACTIVE_LEADER_FIFO_BLOCK_REASON = "active_leader_fifo_coupling_unsupported"

# A daemon op (fan-in restart or camilla reconcile) returns (ok, detail).
DaemonOp = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class CouplingResult:
    """Outcome of a coupling reconcile.

    ``ok`` is True only when the env write AND every daemon op the chosen
    direction needs succeeded (or there was nothing to do). ``changed`` is True
    when the persisted env value actually moved. ``direction`` is ``arm`` /
    ``disarm`` / ``confirm`` (env already at desired — camilla re-confirmed, no
    fan-in bounce). ``recovered`` is True when an ARM failure rolled the box back
    to loopback. ``detail`` carries the first failure's reason for the log/CLI.
    """

    ok: bool
    desired: str
    changed: bool
    direction: str
    restarted_fanin: bool = False
    reconciled_camilla: bool = False
    recovered: bool = False
    detail: str = ""


def _restart_fanin(reason: str) -> tuple[bool, str]:
    """Restart jasper-fanin through the broker. (ok, detail).

    Guarded lazy import (mirrors buffer_reconcile SF-2): a missing/broken control
    package degrades to a reported failure, never an exception out of the
    reconcile that would defeat the fail-safe ladder. timeout 8.0 because fan-in
    is the shared summing daemon and its restart is heavier than a leaf
    renderer's.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"
    resp = restart_broker.manage_units(
        FANIN_UNIT, verb="restart", reason=reason, no_block=False, timeout=8.0,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def _reconcile_camilla(coupling: str, *, reason: str) -> tuple[bool, str]:
    """Re-emit + load the CamillaDSP config for ``coupling``. (ok, detail).

    Forces a full reconcile (``force=True``) so the capture flips even on a flat
    profile (the coupling IS the change), and passes ``coupling`` explicitly so
    the emit does not depend on this process's stale ``os.environ`` (the env file
    was just rewritten under us). reconcile_current_dsp validates with
    ``camilladsp --check`` before loading and fail-closes on an invalid config,
    so a failure here leaves the previously-loaded config running.
    """
    import asyncio

    from jasper.sound.runtime import reconcile_current_dsp

    try:
        payload = asyncio.run(reconcile_current_dsp(force=True, coupling=coupling))
    except Exception as e:  # noqa: BLE001 - report, never raise out of the reconcile
        return False, f"camilla reconcile raised: {e}"
    status = payload.get("status")
    if status in ("reconciled", "unchanged", "skipped"):
        return True, str(status)
    return False, str(payload.get("reason") or status or "unknown")


def reconcile_coupling(
    desired_raw: str | None,
    *,
    reason: str,
    env_path: str | Path = FANIN_ENV_PATH,
    apply: bool = True,
    restart_fanin: "DaemonOp | None" = None,
    reconcile_camilla=None,
    active_leader_check: "Callable[[], bool] | None" = None,
) -> CouplingResult:
    """Make the live fan-in->Camilla coupling match ``desired_raw``, in order.

    ``desired_raw`` is normalized by :func:`resolve_coupling` (unknown/typo ->
    loopback, fail-safe). Writes the persisted env, then runs the direction's
    ordered daemon ops:

    - ARM (-> fifo): restart fan-in, then reconcile camilla. On either failure,
      roll the whole box back to loopback (``recovered=True``) and report
      ``ok=False``.
    - DISARM (-> loopback): reconcile camilla, then restart fan-in. A camilla
      failure still proceeds to the fan-in loopback restart (fan-in must leave
      the pipe regardless) and reports ``ok=False``.
    - CONFIRM (env already at desired): re-run only the camilla reconcile to
      self-heal a drifted loaded config, WITHOUT bouncing fan-in.

    ``apply=False`` writes the env only (no daemon ops) — for staging/migration.
    ``restart_fanin`` / ``reconcile_camilla`` / ``active_leader_check`` are
    injectable for tests (default to the real broker + reconcile_current_dsp +
    grouping-state reader); the camilla hook takes the resolved coupling string.
    """
    do_restart = restart_fanin or (lambda: _restart_fanin(reason=reason))

    def do_reconcile(coupling: str) -> tuple[bool, str]:
        if reconcile_camilla is not None:
            return reconcile_camilla(coupling)
        return _reconcile_camilla(coupling, reason=reason)

    path = Path(env_path)
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    current = resolve_coupling(read_value(existing, COUPLING_ENV_VAR))

    route_mode = _route_mode_for_reconcile(active_leader_check)
    action, support = fanin_coupling_action(desired_raw, route_mode)
    desired = support.coupling
    if not support.supported:
        return _block_fifo_for_active_leader(
            do_restart,
            do_reconcile,
            path,
            existing,
            current,
            reason,
            block_detail=support.detail,
            apply=apply,
        )

    if action is None:
        return CouplingResult(
            ok=False,
            desired=desired,
            changed=False,
            direction="error",
            detail=support.detail or "unsupported coupling action",
        )
    new_text, changed = upsert(existing, action.key, action.value)

    # Persist the desired value first (single source of truth for the daemons'
    # next start). A write failure aborts BEFORE any daemon op so we never bounce
    # a daemon into a value the file doesn't carry.
    if changed:
        try:
            atomic_write_text(path, new_text)
        except OSError as e:
            log_event(
                logger, "fanin.coupling_reconcile", result="write_failed",
                desired=desired, reason=reason, error=e, level=logging.ERROR,
            )
            return CouplingResult(
                ok=False, desired=desired, changed=False, direction="error",
                detail=str(e),
            )

    if not apply:
        log_event(
            logger, "fanin.coupling_reconcile", result="written",
            desired=desired, changed=changed, reason=reason,
        )
        return CouplingResult(
            ok=True, desired=desired, changed=changed,
            direction="arm" if desired == COUPLING_FIFO else "disarm",
        )

    if not changed:
        # Env already at desired: re-confirm camilla only (self-heal a drifted
        # loaded config) — no fan-in bounce on a no-op tick.
        ok, detail = do_reconcile(desired)
        log_event(
            logger, "fanin.coupling_reconcile",
            result="confirmed" if ok else "confirm_failed",
            desired=desired, reason=reason, detail=detail or None,
            level=logging.INFO if ok else logging.WARNING,
        )
        return CouplingResult(
            ok=ok, desired=desired, changed=False, direction="confirm",
            reconciled_camilla=ok, detail="" if ok else detail,
        )

    if desired == COUPLING_FIFO:
        return _arm(do_restart, do_reconcile, desired, reason, path, existing)
    return _disarm(do_restart, do_reconcile, desired, reason)


def _route_mode_for_reconcile(check: "Callable[[], bool] | None") -> RouteMode:
    """Return the route shape for the coupling support matrix."""
    if check is not None:
        try:
            return "active_leader" if bool(check()) else "solo"
        except Exception as e:  # noqa: BLE001 - safety check must not crash CLI
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="active_leader_check_failed",
                detail=e,
                level=logging.WARNING,
            )
            return "unknown"
    try:
        from jasper.audio_runtime_plan import route_mode_from_grouping_config
        from jasper.multiroom.config import load_config

        return route_mode_from_grouping_config(load_config())
    except Exception as e:  # noqa: BLE001 - unreadable grouping => not active leader
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="active_leader_check_failed",
            detail=e,
            level=logging.DEBUG,
        )
        return "unknown"


def _block_fifo_for_active_leader(
    do_restart,
    do_reconcile,
    path: Path,
    existing: str,
    current: str,
    reason: str,
    *,
    block_detail: str | None = None,
    apply: bool,
) -> CouplingResult:
    detail = block_detail or (
        "JASPER_FANIN_CAMILLA_COUPLING=fifo is not supported while this box is "
        "an active multiroom leader; keep the fan-in coupling on loopback until "
        "the grouped active-leader FIFO capture path exists"
    )
    if current == COUPLING_FIFO:
        new_text, _ = upsert(existing, COUPLING_ENV_VAR, COUPLING_LOOPBACK)
        try:
            atomic_write_text(path, new_text)
        except OSError as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result=ACTIVE_LEADER_FIFO_BLOCK_REASON,
                action="loopback_write_failed",
                reason=reason,
                detail=e,
                level=logging.ERROR,
            )
            return CouplingResult(
                ok=False,
                desired=COUPLING_FIFO,
                changed=False,
                direction="blocked",
                detail=f"{detail}; failed to write loopback fallback: {e}",
            )
        if apply:
            disarm = _disarm(do_restart, do_reconcile, COUPLING_LOOPBACK, reason)
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result=ACTIVE_LEADER_FIFO_BLOCK_REASON,
                action="recovered_to_loopback",
                reason=reason,
                recovered=disarm.ok,
                detail=disarm.detail or None,
                level=logging.WARNING,
            )
            return CouplingResult(
                ok=False,
                desired=COUPLING_FIFO,
                changed=True,
                direction="blocked",
                restarted_fanin=disarm.restarted_fanin,
                reconciled_camilla=disarm.reconciled_camilla,
                recovered=disarm.ok,
                detail=detail if disarm.ok else f"{detail}; {disarm.detail}",
            )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result=ACTIVE_LEADER_FIFO_BLOCK_REASON,
            action="wrote_loopback_no_apply",
            reason=reason,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False,
            desired=COUPLING_FIFO,
            changed=True,
            direction="blocked",
            detail=detail,
        )

    log_event(
        logger,
        "fanin.coupling_reconcile",
        result=ACTIVE_LEADER_FIFO_BLOCK_REASON,
        action="kept_loopback",
        reason=reason,
        level=logging.WARNING,
    )
    return CouplingResult(
        ok=False,
        desired=COUPLING_FIFO,
        changed=False,
        direction="blocked",
        detail=detail,
    )


def _arm(do_restart, do_reconcile, desired, reason, path, prior_env) -> CouplingResult:
    """fan-in first (write the pipe), then camilla (load RawFile). Roll back to
    loopback on any failure so we never leave fan-in on the pipe with camilla
    unable to read it."""
    fan_ok, fan_detail = do_restart()
    if not fan_ok:
        _rollback_env(path, prior_env)
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_fanin_failed",
            desired=desired, reason=reason, detail=fan_detail or None,
            level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            detail=fan_detail, recovered=True,
        )

    cam_ok, cam_detail = do_reconcile(COUPLING_FIFO)
    if not cam_ok:
        recovered = _recover_to_loopback(do_restart, do_reconcile, path, reason)
        log_event(
            logger, "fanin.coupling_reconcile", result="arm_camilla_failed",
            desired=desired, reason=reason, detail=cam_detail or None,
            recovered=recovered, level=logging.WARNING,
        )
        return CouplingResult(
            ok=False, desired=desired, changed=False, direction="arm",
            restarted_fanin=True, detail=cam_detail, recovered=recovered,
        )

    log_event(
        logger, "fanin.coupling_reconcile", result="armed",
        desired=desired, reason=reason, detail=cam_detail or None,
    )
    return CouplingResult(
        ok=True, desired=desired, changed=True, direction="arm",
        restarted_fanin=True, reconciled_camilla=True,
    )


def _disarm(do_restart, do_reconcile, desired, reason) -> CouplingResult:
    """camilla first (off RawFile -> Alsa), then fan-in (off the pipe). Even if
    the camilla reconcile fails, still restart fan-in to loopback — fan-in must
    leave the pipe regardless, and camilla fail-closed on its prior config."""
    cam_ok, cam_detail = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, fan_detail = do_restart()
    ok = cam_ok and fan_ok
    detail = "; ".join(d for d in (cam_detail if not cam_ok else "",
                                   fan_detail if not fan_ok else "") if d)
    log_event(
        logger, "fanin.coupling_reconcile",
        result="disarmed" if ok else "disarm_partial",
        desired=desired, reason=reason, detail=detail or None,
        level=logging.INFO if ok else logging.WARNING,
    )
    return CouplingResult(
        ok=ok, desired=desired, changed=True, direction="disarm",
        restarted_fanin=fan_ok, reconciled_camilla=cam_ok, detail=detail,
    )


def _recover_to_loopback(do_restart, do_reconcile, path, reason) -> bool:
    """ARM-failure recovery: force the whole box back to loopback (env + camilla
    Alsa + fan-in loopback). Returns True iff the recovery fully succeeded."""
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    new_text, _ = remove(existing, COUPLING_ENV_VAR)
    try:
        if new_text:
            atomic_write_text(path, new_text)
        elif path.exists():
            path.unlink(missing_ok=True)
    except OSError:
        return False
    cam_ok, _ = do_reconcile(COUPLING_LOOPBACK)
    fan_ok, _ = do_restart()
    return cam_ok and fan_ok


def _rollback_env(path: Path, prior_env: str) -> None:
    """Restore the env file to its pre-write contents (or unlink if it did not
    exist). Best-effort: a rollback failure is already on the unhappy path."""
    try:
        if prior_env:
            atomic_write_text(path, prior_env)
        elif path.exists():
            path.unlink(missing_ok=True)
    except OSError:
        pass


def read_persisted_coupling(env_path: str | Path = FANIN_ENV_PATH) -> str:
    """The coupling the daemons will read on their next start (resolved,
    fail-safe to loopback). Doctor + observability use this to compare the
    persisted intent against the live fan-in transport."""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return COUPLING_LOOPBACK
    return resolve_coupling(read_value(text, COUPLING_ENV_VAR))


def main(argv: "list[str] | None" = None) -> int:
    """CLI: ``jasper-fanin-coupling-reconcile <loopback|fifo>`` (the operator +
    install entry). Prints the result and exits non-zero on ``ok=False`` so a
    deploy step or operator sees a failed transition."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="jasper-fanin-coupling-reconcile",
        description="Arm/disarm the fan-in -> CamillaDSP coupling in order.",
    )
    parser.add_argument("coupling", choices=[COUPLING_LOOPBACK, COUPLING_FIFO])
    parser.add_argument("--reason", default="cli")
    parser.add_argument(
        "--no-apply", action="store_true",
        help="write the env only; skip the daemon transition (staging).",
    )
    args = parser.parse_args(argv)
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE reconciling, so the camilla reconcile this triggers emits with
    # the persisted JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} etc. — not their
    # defaults. Without this, arming =fifo from a bare CLI/install shell silently
    # RESET a tuned chunksize back to 1024 (caught on JTS 2026-06-27). setdefault
    # semantics keep an explicit shell override winning. Mirrors jasper.cli.sound.
    from jasper.env_load import load_env_files

    load_env_files()
    result = reconcile_coupling(
        args.coupling, reason=args.reason, apply=not args.no_apply,
    )
    print(
        f"coupling reconcile: desired={result.desired} direction={result.direction} "
        f"ok={result.ok} changed={result.changed} "
        f"fanin={result.restarted_fanin} camilla={result.reconciled_camilla}"
        + (f" recovered={result.recovered}" if result.recovered else "")
        + (f" detail={result.detail}" if result.detail else "")
    )
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
