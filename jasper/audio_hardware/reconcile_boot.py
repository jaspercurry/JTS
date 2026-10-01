# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The reconcile pass's boot-config step: the USB data role and the I2S HAT
block in the boot config, and the marker that says a reboot is still owed.

Each step takes the pass; ``Pass.execute`` decides the order.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.audio_hardware.reconcile_common import _Abort, _ensure_dir
from jasper.platform.log_event import log_event

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass


def reconcile_i2s_hat_boot(run: Pass, logger: logging.Logger) -> None:
    """``logger`` is the pass module's: every journal line names its logger
    (``jasper.platform.logging_setup.LOG_FORMAT``), and under ``python -m`` that is
    ``__main__``, so the boot role events are logged through it."""
    # lazy: patch target — the tests replace it on the source module, which
    # only a per-call import sees.
    from jasper.audio_hardware.usb_port_role import (
        reconcile_boot_config, boot_role_events,
    )

    try:
        (
            state,
            boot_changed,
            hat_changed,
            desired_profile,
            durability_failed,
            hat_collision,
        ) = reconcile_boot_config(
            model_path=run.model_path,
            boot_config_path=run.boot_config_path,
            udc_class_dir=run.udc_class_dir,
            i2s_hat_intent_path=run.i2s_hat_intent_file,
        )
    # noqa reason: any failure here means the boot config was NOT applied, and
    # 66 (rather than a traceback) is what says the config was preserved.
    except Exception:  # noqa: BLE001
        run.log("i2s_hat_apply", result="error", action="preserve_boot_config")
        raise _Abort(66) from None
    for name, fields in boot_role_events(
        state,
        boot_config_changed=boot_changed,
        hat_profile=desired_profile or "",
        hat_changed=hat_changed,
        hat_collision=hat_collision,
    ):
        log_event(logger, name, fields=fields)
    if durability_failed:
        run.i2s_hat_apply_error = True
    run.i2s_hat_blocked_by_collision = (
        hat_collision is not None
        and hat_collision.managed_overlay not in hat_collision.colliding_overlays
    )
    run.i2s_hat_desired_profile = desired_profile or ""
    if state.board_topology == "unsupported":
        run.log(
            "i2s_hat_apply", result="unavailable", board_topology="unsupported"
        )
        return
    run.i2s_hat_boot_changed = bool(hat_changed)
    if run.i2s_hat_apply_error:
        run.log(
            "i2s_hat_apply",
            result="error",
            error="boot_config_published_not_durable",
        )
        return
    run.log(
        "i2s_hat_apply",
        result=run.i2s_hat_boot_changed,
        profile=run.i2s_hat_desired_profile or "none",
    )


def sync_i2s_hat_reboot_marker(run: Pass) -> None:
    """State, not edge: an install-time pass can write the detected HAT's
    boot line before this service ever runs, so "changed this pass" is
    false while the kernel still runs the old overlay. Any registered I2S
    profile, not just InnoMaker: a HAT can be the composite's child device
    rather than the top-level profile_id."""
    desired = run.i2s_hat_desired_profile
    if run.i2s_hat_boot_changed is None or not run.observed.valid:
        return
    observed = run.observed.profile_id
    children = run.observed.child_device_ids
    if desired and desired in children:
        observed = desired
    marker = Path(run.i2s_hat_reboot_required_path)
    # A hand-written line naming another overlay kept the managed line out of
    # the boot config, so a reboot boots that overlay again: the conflict
    # event names the next step, not a restart.
    if observed == desired or run.i2s_hat_blocked_by_collision:
        marker.unlink(missing_ok=True)
        return
    # No HAT desired: whatever DAC is attached is not a pending boot
    # change, so only this pass having cleared the managed block pends one.
    if not desired and not run.i2s_hat_boot_changed:
        return
    _ensure_dir(marker.parent, 0o755)
    marker.write_text("", encoding="utf-8")
    os.chmod(marker, 0o644)
