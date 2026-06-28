# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Privileged delegation of the lean-lane CamillaDSP apply/restore (Stage 4b-iv).

WHY THIS EXISTS — the WS1 privilege boundary. The lean-lane enter/leave ladder
swaps CamillaDSP's CAPTURE config, which means writing
``/var/lib/camilladsp/configs`` (the generated-config dir, the shared
``.dsp_apply.lock``, and the carrier-preserved lean YAML). ``jasper-mux`` runs
as the non-root ``jasper-mux`` user under ``ProtectSystem=strict`` with
``ReadWritePaths=/var/lib/jasper /var/lib/jasper-intsecrets`` — it does NOT own
``camilladsp/configs`` (only ``jasper-web`` does, per WS1 Phase 3b-3). So the
in-process apply (``apply_lean_capture_config`` -> ``dsp_writer_lock`` ->
``.dsp_apply.lock``) fails ``[Errno 30] EROFS`` on a privilege-separated box.
The lean lane was built when the mux was still root; the sandbox came after and
the hardware-free tests don't exercise it, so this slipped past CI.

Widening the mux's ``ReadWritePaths`` to include ``camilladsp/configs`` was
rejected: it would re-open the mux's write surface against the WS1 "S2 single
StateDirectory owner" design (the mux deliberately holds the narrowest writable
set of any Tier-A daemon). Instead the mux DELEGATES the privileged apply to a
root oneshot — exactly mirroring how it already delegates the usbsink restart
through jasper-control's restart broker (:mod:`jasper.usbsink.output_mode_reconcile`).

THE DELEGATION SHAPE (host-mediated indirection):

1. This module (running in the mux) writes the intent (``enter`` | ``leave``)
   into the reconciler-owned ``/var/lib/jasper/lean.env`` — a path the mux CAN
   write (it is under the mux's ``ReadWritePaths``).
2. It then asks jasper-control's restart broker to ``start
   jasper-lean-apply.service`` with ``no_block=False`` (BLOCKING). The broker
   runs ``systemctl start`` as the privileged broker host; for a ``Type=oneshot``
   unit that blocks until ``ExecStart`` exits and the systemctl rc reflects the
   oneshot's exit status. So the mux gets a SYNCHRONOUS success/failure verdict
   — exactly what the enter/leave ladder needs to decide fail-loud -> buffered.
3. The oneshot's ``ExecStart`` is ``jasper-lean-apply`` (this module's
   :func:`main`), which runs the real
   :func:`jasper.sound.runtime.apply_lean_capture_config` /
   :func:`restore_buffered_config` at full privilege (it CAN write
   ``camilladsp/configs``), and exits non-zero on any failure so the broker rc
   carries it back.

The mux's only NEW privilege is the broker ``start`` of one more unit
(``jasper-lean-apply.service`` is a ``START_ONLY_UNITS`` entry — non-root
clients may only START it, never restart/stop). The actual config write stays
with the privileged owner. No mux ``ReadWritePaths`` widening.

FAIL-LOUD. The :class:`ApplyResult` carries ``ok`` (never raises out of
:func:`delegate`), so the caller's ladder falls back to buffered on ``ok=False``
just as it does on the usbsink ``ArmResult``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from jasper.atomic_io import atomic_write_text
from jasper.env_file import read_value, upsert
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Reconciler-owned intent file under the mux's ReadWritePaths (/var/lib/jasper).
# The oneshot reads this to know which leg to run. Mirrors the
# usbsink.env / fanin.env single-key-upsert pattern.
LEAN_ENV_PATH = "/var/lib/jasper/lean.env"
ACTION_KEY = "JASPER_LEAN_ACTION"
LEAN_APPLY_UNIT = "jasper-lean-apply.service"

# The two intents the oneshot's CLI understands. "enter" -> apply lean capture;
# "leave" -> restore the buffered config.
ACTION_ENTER = "enter"
ACTION_LEAVE = "leave"
_VALID_ACTIONS = (ACTION_ENTER, ACTION_LEAVE)

# The privileged apply can re-emit + validate (camilladsp --check) + live-reload
# CamillaDSP, which is heavier than a leaf restart. Give the broker room.
_BROKER_TIMEOUT_SEC = 20.0


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of a delegated lean apply/restore.

    ``ok`` is True only when BOTH the intent write and the privileged oneshot
    succeeded. ``detail`` carries the first failure's reason for the caller's
    log line.
    """

    ok: bool
    action: str
    detail: str = ""


def _start_oneshot(reason: str) -> tuple[bool, str]:
    """Start the privileged lean-apply oneshot through the broker, BLOCKING.

    Returns (ok, detail). ``no_block=False`` so ``systemctl start`` waits for the
    ``Type=oneshot`` ExecStart to finish and the rc reflects the oneshot's exit
    status — the synchronous verdict the caller's ladder needs.

    Guarded lazy import (mirrors output_mode_reconcile SF-2): a missing/broken
    control package degrades to a reported failure (ok=False), never an exception
    out of :func:`delegate` that would defeat the caller's fail-soft ladder.
    """
    try:
        from jasper.control import restart_broker
    except ImportError as e:  # pragma: no cover - control pkg always present in prod
        return False, f"restart_broker unavailable: {e}"

    resp = restart_broker.manage_units(
        LEAN_APPLY_UNIT,
        verb="start",
        reason=reason,
        no_block=False,
        timeout=_BROKER_TIMEOUT_SEC,
    )
    if resp.get("ok"):
        return True, ""
    return False, str(resp.get("error") or f"rc={resp.get('rc')}")


def delegate(
    action: str,
    *,
    reason: str,
    env_path: str | os.PathLike = LEAN_ENV_PATH,
) -> ApplyResult:
    """Write the lean intent and run the privileged apply oneshot.

    ``action`` must be ``"enter"`` or ``"leave"``. Writes
    ``JASPER_LEAN_ACTION=<action>`` into ``lean.env`` (a path the mux owns),
    then BLOCKING-starts ``jasper-lean-apply.service`` via the broker. The
    oneshot performs the actual ``camilladsp/configs`` write at full privilege.

    Fail-soft on the intent write (OSError reported via ``ok=False``); the
    caller's enter/leave ladder treats ``ok=False`` as "fall back to buffered".
    Never raises.
    """
    if action not in _VALID_ACTIONS:
        log_event(
            logger,
            "sound.lean_delegate",
            result="invalid_action",
            action=action,
            level=logging.ERROR,
        )
        return ApplyResult(ok=False, action=action, detail=f"invalid action {action!r}")

    path = Path(env_path)
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""

    # Always (re)write the intent before the oneshot runs — the oneshot reads it
    # fresh, and a previous run's value must never win a later episode. upsert is
    # the order-preserving single-key writer (leaves any operator lines intact).
    new_text, _ = upsert(existing, ACTION_KEY, action)
    try:
        atomic_write_text(path, new_text)
    except OSError as e:
        log_event(
            logger,
            "sound.lean_delegate",
            result="write_failed",
            action=action,
            error=e,
            level=logging.ERROR,
        )
        return ApplyResult(ok=False, action=action, detail=str(e))

    ok, detail = _start_oneshot(reason=reason)
    if not ok:
        log_event(
            logger,
            "sound.lean_delegate",
            result="oneshot_failed",
            action=action,
            reason=reason,
            detail=detail or None,
            level=logging.WARNING,
        )
        return ApplyResult(ok=False, action=action, detail=detail)

    log_event(
        logger,
        "sound.lean_delegate",
        result="applied",
        action=action,
        reason=reason,
    )
    return ApplyResult(ok=True, action=action, detail=detail)


def read_action(env_path: str | os.PathLike = LEAN_ENV_PATH) -> str | None:
    """The intent the oneshot will act on (the last value :func:`delegate`
    wrote), or None if the file/key is absent. The oneshot CLI reads this."""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except OSError:
        return None
    value = read_value(text, ACTION_KEY)
    return value.strip() if value is not None else None


def main(argv: "list[str] | None" = None) -> int:
    """``jasper-lean-apply`` — the privileged oneshot's entry point.

    Reads the delegated intent from ``lean.env`` (or ``--action`` for an
    operator/test override) and runs the matching CamillaDSP leg at full
    privilege. Exits 0 on success, non-zero on any failure so the broker's
    blocking ``systemctl start`` carries the verdict back to the mux's ladder.

    NOTE: this runs in the root oneshot, NOT in the mux. The
    ``camilladsp/configs`` write that EROFS-fails under the mux sandbox succeeds
    here.
    """
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="jasper-lean-apply",
        description="Privileged CamillaDSP lean-lane apply/restore (delegated by jasper-mux).",
    )
    parser.add_argument(
        "--action",
        choices=list(_VALID_ACTIONS),
        default=None,
        help="override the lean.env intent (operator/test); default reads lean.env.",
    )
    args = parser.parse_args(argv)

    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load) BEFORE applying, so the emit reads the persisted tuning (camilla
    # chunksize/target level, fan-in coupling) — not bare defaults. Mirrors
    # jasper.fanin.coupling_reconcile.main. setdefault semantics keep an explicit
    # shell override winning.
    from jasper.env_load import load_env_files

    load_env_files()

    action = args.action or read_action(LEAN_ENV_PATH)
    if action not in _VALID_ACTIONS:
        log_event(
            logger,
            "sound.lean_apply",
            result="no_action",
            action=action,
            level=logging.ERROR,
        )
        print(f"jasper-lean-apply: no valid action (got {action!r})")
        return 2

    from jasper.sound.runtime import (
        apply_lean_capture_config,
        restore_buffered_config,
    )

    try:
        if action == ACTION_ENTER:
            asyncio.run(apply_lean_capture_config())
        else:
            asyncio.run(restore_buffered_config())
    except Exception as e:  # noqa: BLE001 - report via exit code, never traceback-crash the unit
        log_event(
            logger,
            "sound.lean_apply",
            result="failed",
            action=action,
            detail=str(e),
            level=logging.WARNING,
        )
        print(f"jasper-lean-apply: {action} failed: {e}")
        return 1

    log_event(logger, "sound.lean_apply", result="ok", action=action)
    print(f"jasper-lean-apply: {action} ok")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
