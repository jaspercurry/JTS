# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve or reconcile the USB data-port role (ADR-0235).

Every `event=` line goes to stderr (ADR-0235 R4), which is what reaches the
journal on every invocation; the shell callers read only the exit status.
"""

from __future__ import annotations

import argparse
import os
import sys

from jasper.audio_hardware.config_txt import DEFAULT_BOOT_CONFIG_PATH
from jasper.audio_hardware.hat_eeprom import DEFAULT_HAT_DIR
from jasper.audio_hardware.i2s_hat import I2sHatCollision
from jasper.audio_hardware.usb_port_role import (
    DEFAULT_MODEL_PATH,
    boot_role_events,
    reconcile_boot_config,
    resolve_system_usb_port_role,
)
from jasper.log_event import render_logfmt
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR




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
    for name, fields in boot_role_events(
        state,
        boot_config_changed=changed,
        hat_profile=desired_hat_profile or "",
        hat_changed=args.reconcile_boot and hat_changed,
        hat_collision=hat_collision,
    ):
        print(render_logfmt(name, fields), file=sys.stderr)
    return os.EX_IOERR if durability_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
