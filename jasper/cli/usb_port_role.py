# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The CLI the shell evals: resolve/reconcile the USB data-port role and emit
its ``--env`` contract (ADR-0235).
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from jasper.audio_hardware.config_txt import DEFAULT_BOOT_CONFIG_PATH
from jasper.audio_hardware.hat_eeprom import DEFAULT_HAT_DIR
from jasper.audio_hardware.i2s_hat import I2sHatCollision
from jasper.audio_hardware.usb_port_role import (
    DEFAULT_MODEL_PATH,
    UsbPortRoleState,
    reconcile_boot_config,
    resolve_system_usb_port_role,
)
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR


def _flag(value: object) -> str:
    return "true" if value else "false"


def _env_lines(
    state: UsbPortRoleState,
    *,
    boot_config_changed: bool,
    hat_profile: str,
    hat_changed: bool,
    durability_failed: bool,
    hat_collision: I2sHatCollision | None,
) -> str:
    """The boot-config CLI's whole shell contract (ADR-0235 R2).

    Emitted whether or not ``--reconcile-boot`` ran, so a caller evaling this
    never hits an unset variable: the HAT-only keys read as empty/false when
    there was nothing to reconcile.
    """
    values = {
        "JASPER_BOOT_BOARD_TOPOLOGY": state.board_topology,
        "JASPER_BOOT_USB_DESIRED_ROLE": state.desired_role,
        "JASPER_BOOT_USB_ACTIVE_ROLE": state.active_role,
        "JASPER_BOOT_REBOOT_REQUIRED": _flag(state.reboot_required),
        "JASPER_BOOT_CONFIG_CHANGED": _flag(boot_config_changed),
        "JASPER_BOOT_I2S_HAT_PROFILE": hat_profile,
        "JASPER_BOOT_I2S_HAT_CHANGED": _flag(hat_changed),
        "JASPER_BOOT_CONFIG_PUBLISHED_NOT_DURABLE": _flag(durability_failed),
        "JASPER_BOOT_I2S_HAT_COLLISION_MANAGED_OVERLAY": (
            hat_collision.managed_overlay if hat_collision is not None else ""
        ),
        "JASPER_BOOT_I2S_HAT_COLLISION_COLLIDING_OVERLAYS": (
            ",".join(hat_collision.colliding_overlays)
            if hat_collision is not None
            else ""
        ),
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile-boot", action="store_true")
    parser.add_argument("--i2s-hat-intent-file")
    parser.add_argument("--hat-dir", default=DEFAULT_HAT_DIR)
    parser.add_argument("--require-management-transport", action="store_true")
    parser.add_argument(
        "--model-file",
        default=os.environ.get("JASPER_PI_MODEL_FILE", DEFAULT_MODEL_PATH),
    )
    parser.add_argument(
        "--boot-config",
        default=os.environ.get("JTS_BOOT_CONFIG_FILE", DEFAULT_BOOT_CONFIG_PATH),
    )
    parser.add_argument(
        "--udc-class-dir",
        default=os.environ.get("JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR),
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print the shell contract as shell-safe KEY=value assignments",
    )
    args = parser.parse_args(argv)
    hat_changed = False
    desired_hat_profile: str | None = None
    durability_failed = False
    hat_collision: I2sHatCollision | None = None
    if args.reconcile_boot:
        result = reconcile_boot_config(
            model_path=args.model_file,
            boot_config_path=args.boot_config,
            udc_class_dir=args.udc_class_dir,
            i2s_hat_intent_path=args.i2s_hat_intent_file,
            hat_dir=args.hat_dir,
        )
        (
            state,
            changed,
            hat_changed,
            desired_hat_profile,
            durability_failed,
            hat_collision,
        ) = result
    else:
        state = resolve_system_usb_port_role(
            model_path=args.model_file,
            boot_config_path=args.boot_config,
            udc_class_dir=args.udc_class_dir,
        )
        changed = False
    if args.require_management_transport:
        print(
            "event=hardware.usb_management_transport "
            f"available={str(state.management_transport_available).lower()} "
            f"desired={state.desired_role} active={state.active_role} "
            f"reason={state.reason}",
            file=sys.stderr,
        )
        return 0 if state.management_transport_available else 1
    if args.env:
        print(
            _env_lines(
                state,
                boot_config_changed=changed,
                hat_profile=desired_hat_profile or "",
                hat_changed=hat_changed,
                durability_failed=durability_failed,
                hat_collision=hat_collision,
            ),
            end="",
        )
    # Every event= line goes to stderr (ADR-0235 R4): stdout carries the
    # `--env` payload, stderr reaches the journal on every invocation.
    print(
        "event=hardware.usb_role_resolved "
        f"topology={state.board_topology} desired={state.desired_role} "
        f"active={state.active_role} "
        f"gadget_available={str(state.gadget_available).lower()} "
        "management_transport_available="
        f"{str(state.management_transport_available).lower()} "
        f"reason={state.reason}",
        file=sys.stderr,
    )
    if changed:
        print(
            "event=hardware.boot_config_changed "
            f"reboot_required={int(state.reboot_required)}",
            file=sys.stderr,
        )
    if args.reconcile_boot and hat_changed:
        # No `reboot_required` here: whether the running kernel already
        # carries this overlay is desired-vs-observed, which only the
        # reconciler's I2S reboot marker can decide (ADR-0233 one owner).
        print(
            "event=hardware.i2s_hat_boot_config_changed "
            f"profile={desired_hat_profile or 'none'}",
            file=sys.stderr,
        )
    if hat_collision is not None:
        print(
            "event=hardware.i2s_hat_boot_config_conflict "
            f"managed_overlay={hat_collision.managed_overlay} "
            f"colliding_overlays={','.join(hat_collision.colliding_overlays)}",
            file=sys.stderr,
        )
    return os.EX_IOERR if durability_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
