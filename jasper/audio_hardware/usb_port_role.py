# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the Pi USB data-port role and reconcile it into config.txt.

The resolver is pure once its observed inputs are supplied.  It deliberately
does not infer an I2S output from a missing USB device: on a Zero-class board,
the shared OTG port stays in host mode through transient DAC removal so the DAC
can reconnect without operator intervention. The role block's own render/parse
lives in ``config_txt.py`` (ADR-0235 PR 6); the CLI lives in
``jasper.cli.usb_port_role``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.usbgadget import DEFAULT_UDC_CLASS_DIR

from .config_txt import (
    DEFAULT_BOOT_CONFIG_PATH,
    BoardUsbTopology,
    UsbDataRole,
    configured_usb_role,
    render_boot_config,
)
from .dac import all_profiles, by_id, is_boot_managed_i2s_profile
from .hat_eeprom import DEFAULT_HAT_DIR
from .i2s_hat import (
    I2sHatCollision,
    configured_i2s_overlays,
    detected_i2s_hat_profile,
    read_i2s_hat_intent,
    render_i2s_hat_boot_config,
)
from .text_property import read_text_property


DEFAULT_MODEL_PATH = "/proc/device-tree/model"


@dataclass(frozen=True)
class UsbPortRoleState:
    """One resolved desired/active USB role and its product availability."""

    board_model: str
    board_topology: BoardUsbTopology
    desired_role: UsbDataRole
    configured_role: UsbDataRole
    active_role: UsbDataRole
    gadget_available: bool
    reboot_required: bool
    reason: str
    decision_reason: str
    management_transport_available: bool
    configured_i2s_overlays: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "UsbPortRoleState | None":
        try:
            raw_board_model = raw["board_model"]
            board_topology = str(raw["board_topology"])
            desired_role = str(raw["desired_role"])
            configured_role = str(raw["configured_role"])
            active_role = str(raw["active_role"])
            reason = str(raw["reason"])
            decision_reason = str(raw["decision_reason"])
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(raw_board_model, str):
            return None
        board_model = raw_board_model.replace("\x00", "").strip()
        if board_topology not in {
            "shared_otg_port",
            "separate_host_ports",
            "unsupported",
        }:
            return None
        if board_usb_topology(board_model) != board_topology:
            return None
        if any(
            role not in {"host", "peripheral", "unknown"}
            for role in (desired_role, configured_role, active_role)
        ):
            return None
        raw_overlays = raw.get("configured_i2s_overlays", [])
        if not isinstance(raw_overlays, list) or not all(
            isinstance(item, str) for item in raw_overlays
        ):
            return None
        overlays = tuple(
            sorted({item.strip().lower() for item in raw_overlays if item.strip()})
        )
        if len(overlays) != len(raw_overlays):
            return None
        registered_i2s_overlays = {
            profile.dtoverlay.lower()
            for profile in all_profiles()
            if is_boot_managed_i2s_profile(profile) and profile.dtoverlay
        }
        if not set(overlays) <= registered_i2s_overlays:
            return None
        if board_topology == "shared_otg_port":
            if desired_role == "host":
                if overlays:
                    return None
                valid_decisions = {
                    "shared_otg_usb_output_requires_host",
                    "shared_otg_defaults_host_without_i2s",
                }
            elif desired_role == "peripheral":
                if not overlays:
                    return None
                valid_decisions = {"registered_i2s_leaves_otg_available"}
            else:
                return None
        elif board_topology == "separate_host_ports":
            if desired_role != "peripheral":
                return None
            valid_decisions = {"dedicated_host_ports_leave_otg_available"}
        else:
            if desired_role != "unknown":
                return None
            valid_decisions = {"unsupported_board"}
        if decision_reason not in valid_decisions:
            return None
        expected_reboot = (
            desired_role != "unknown"
            and (
                configured_role != desired_role
                or (active_role != "unknown" and active_role != desired_role)
            )
        )
        expected_available = (
            desired_role == "peripheral"
            and configured_role == "peripheral"
            and active_role == "peripheral"
            and not expected_reboot
        )
        expected_management_transport = (
            board_topology != "unsupported" and active_role == "peripheral"
        )
        if board_topology == "unsupported":
            expected_reason = "unsupported_board"
        elif expected_reboot:
            expected_reason = "role_change_pending_reboot"
        elif expected_available:
            expected_reason = "available"
        else:
            expected_reason = decision_reason
        if (
            raw.get("gadget_available") is not expected_available
            or raw.get("reboot_required") is not expected_reboot
            or raw.get("management_transport_available")
            is not expected_management_transport
            or reason != expected_reason
        ):
            return None
        return cls(
            board_model=board_model,
            board_topology=board_topology,  # type: ignore[arg-type]
            desired_role=desired_role,  # type: ignore[arg-type]
            configured_role=configured_role,  # type: ignore[arg-type]
            active_role=active_role,  # type: ignore[arg-type]
            gadget_available=expected_available,
            reboot_required=expected_reboot,
            reason=reason,
            decision_reason=decision_reason,
            management_transport_available=expected_management_transport,
            configured_i2s_overlays=overlays,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "board_model": self.board_model,
            "board_topology": self.board_topology,
            "desired_role": self.desired_role,
            "configured_role": self.configured_role,
            "active_role": self.active_role,
            "gadget_available": self.gadget_available,
            "reboot_required": self.reboot_required,
            "reason": self.reason,
            "decision_reason": self.decision_reason,
            "management_transport_available": self.management_transport_available,
            "configured_i2s_overlays": list(self.configured_i2s_overlays),
        }


def gadget_unavailable_detail(state: UsbPortRoleState) -> str:
    """Return the shared operator-facing explanation for gadget availability."""

    if state.gadget_available:
        return ""
    if state.reason == "role_change_pending_reboot":
        return (
            "A reboot is required to apply the detected USB data-port role "
            f"({state.desired_role})."
        )
    if state.decision_reason == "registered_i2s_leaves_otg_available":
        return (
            "The supported I2S output leaves the USB data port available, but "
            "peripheral mode is not active. Re-run the installer and reboot."
        )
    if state.board_topology == "shared_otg_port":
        return (
            "This Zero-class speaker reserves its single USB data port for the "
            "output DAC. USB Audio Input and USB management are unavailable; "
            "a configured supported I2S DAC leaves the port available instead."
        )
    if state.reason == "unsupported_board":
        return (
            "USB gadget support is unavailable because this board's USB port "
            "topology is not recognized."
        )
    return "USB gadget support is not currently available on this speaker."


def board_usb_topology(model: str) -> BoardUsbTopology:
    normalized = model.replace("\x00", "").strip()
    if "Raspberry Pi Zero" in normalized:
        return "shared_otg_port"
    if (
        "Raspberry Pi 4 Model B" in normalized
        or "Raspberry Pi 5 Model B" in normalized
    ):
        return "separate_host_ports"
    return "unsupported"


def resolve_usb_port_role(
    *,
    board_model: str,
    boot_config: str,
    active_role: UsbDataRole,
    observed_output_profile_id: str = "unknown",
) -> UsbPortRoleState:
    topology = board_usb_topology(board_model)
    configured_role = configured_usb_role(boot_config)
    i2s_overlays = configured_i2s_overlays(boot_config)
    observed_profile = by_id(observed_output_profile_id)

    if topology == "shared_otg_port":
        if i2s_overlays:
            desired_role: UsbDataRole = "peripheral"
            decision_reason = "registered_i2s_leaves_otg_available"
        else:
            desired_role = "host"
            if observed_profile is not None and observed_profile.connection == "usb":
                decision_reason = "shared_otg_usb_output_requires_host"
            else:
                decision_reason = "shared_otg_defaults_host_without_i2s"
    elif topology == "separate_host_ports":
        desired_role = "peripheral"
        decision_reason = "dedicated_host_ports_leave_otg_available"
    else:
        desired_role = "unknown"
        decision_reason = "unsupported_board"

    reboot_required = (
        desired_role != "unknown"
        and (
            configured_role != desired_role
            or (active_role != "unknown" and active_role != desired_role)
        )
    )
    gadget_available = (
        desired_role == "peripheral"
        and active_role == "peripheral"
        and configured_role == "peripheral"
        and not reboot_required
    )
    management_transport_available = (
        topology != "unsupported" and active_role == "peripheral"
    )
    if topology == "unsupported":
        reason = "unsupported_board"
    elif reboot_required:
        reason = "role_change_pending_reboot"
    elif gadget_available:
        reason = "available"
    else:
        reason = decision_reason

    return UsbPortRoleState(
        board_model=board_model.replace("\x00", "").strip(),
        board_topology=topology,
        desired_role=desired_role,
        configured_role=configured_role,
        active_role=active_role,
        gadget_available=gadget_available,
        reboot_required=reboot_required,
        reason=reason,
        decision_reason=decision_reason,
        management_transport_available=management_transport_available,
        configured_i2s_overlays=i2s_overlays,
    )


def observed_active_role(udc_class_dir: str | Path) -> UsbDataRole:
    root = Path(udc_class_dir)
    if not root.is_dir():
        return "unknown"
    try:
        return "peripheral" if next(root.iterdir(), None) is not None else "host"
    except OSError:
        return "unknown"


def resolve_system_usb_port_role(
    *,
    observed_output_profile_id: str = "unknown",
    model_path: str | Path | None = None,
    boot_config_path: str | Path | None = None,
    udc_class_dir: str | Path | None = None,
) -> UsbPortRoleState:
    model_path = model_path or os.environ.get("JASPER_PI_MODEL_FILE", DEFAULT_MODEL_PATH)
    boot_config_path = boot_config_path or os.environ.get(
        "JTS_BOOT_CONFIG_FILE", DEFAULT_BOOT_CONFIG_PATH
    )
    udc_class_dir = udc_class_dir or os.environ.get(
        "JASPER_UDC_CLASS_DIR", DEFAULT_UDC_CLASS_DIR
    )
    return resolve_usb_port_role(
        board_model=read_text_property(model_path),
        boot_config=read_text_property(boot_config_path),
        active_role=observed_active_role(udc_class_dir),
        observed_output_profile_id=observed_output_profile_id,
    )


def reconcile_boot_config(
    *,
    model_path: str | Path,
    boot_config_path: str | Path,
    udc_class_dir: str | Path,
    i2s_hat_intent_path: str | Path | None = None,
    hat_dir: str | Path = DEFAULT_HAT_DIR,
) -> tuple[UsbPortRoleState, bool, bool, str | None, bool, I2sHatCollision | None]:
    # Resolution order (ADR-0234): a HAT that names itself in its ID EEPROM is
    # applied with no operator step; the intent file is the toggle for the HATs
    # that carry no EEPROM to read. With neither -- and the intent FILE must
    # exist, not just the path argument -- NOTHING is touched, managed block
    # included (see the jts3 incident this guards).
    detected = detected_i2s_hat_profile(hat_dir)
    detected_id = detected.id if detected is not None else None
    intent_declared = i2s_hat_intent_path is not None and Path(
        i2s_hat_intent_path
    ).is_file()
    desired_profile = detected_id
    if desired_profile is None and intent_declared:
        assert i2s_hat_intent_path is not None
        desired_profile = read_i2s_hat_intent(i2s_hat_intent_path)
    manage_hat = detected_id is not None or intent_declared
    config_path = Path(boot_config_path)
    if not config_path.is_file():
        state = resolve_system_usb_port_role(
            model_path=model_path,
            boot_config_path=boot_config_path,
            udc_class_dir=udc_class_dir,
        )
        if manage_hat and state.board_topology != "unsupported":
            raise FileNotFoundError(f"boot config does not exist: {config_path}")
        return state, False, False, desired_profile, False, None
    original = config_path.read_text(encoding="utf-8")
    initial = resolve_usb_port_role(
        board_model=read_text_property(model_path),
        boot_config=original,
        active_role=observed_active_role(udc_class_dir),
    )
    if initial.board_topology == "unsupported":
        return initial, False, False, desired_profile, False, None
    hat_changed = False
    hat_collision: I2sHatCollision | None = None
    with_hat = original
    if manage_hat:
        with_hat, hat_changed, hat_collision = render_i2s_hat_boot_config(
            original, desired_profile
        )
    desired_role = resolve_usb_port_role(
        board_model=initial.board_model,
        boot_config=with_hat,
        active_role=initial.active_role,
    ).desired_role
    rendered = render_boot_config(with_hat, desired_role)
    changed = rendered != original
    durability_failed = False
    if changed:
        try:
            atomic_write_text(
                config_path,
                rendered,
                mode=stat.S_IMODE(config_path.stat().st_mode),
                durable=True,
            )
        except OSError:
            if config_path.read_text(encoding="utf-8") != rendered:
                raise
            durability_failed = True
    state = resolve_usb_port_role(
        board_model=initial.board_model,
        boot_config=rendered,
        active_role=initial.active_role,
    )
    return state, changed, hat_changed, desired_profile, durability_failed, hat_collision


if __name__ == "__main__":
    from jasper.cli.usb_port_role import main

    raise SystemExit(main())
