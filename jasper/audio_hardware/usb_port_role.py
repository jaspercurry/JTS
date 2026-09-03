# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the Pi USB data-port role from board topology and DAC declarations.

The resolver is pure once its observed inputs are supplied.  It deliberately
does not infer an I2S output from a missing USB device: on a Zero-class board,
the shared OTG port stays in host mode through transient DAC removal so the DAC
can reconnect without operator intervention.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from jasper.atomic_io import atomic_write_text, read_regular_bytes_nofollow
from jasper.env_file import parse_env_lines

from .dac import INNOMAKER_HIFI_AMP_PRO_ID, DacProfile, all_profiles, by_id


DEFAULT_MODEL_PATH = "/proc/device-tree/model"
DEFAULT_BOOT_CONFIG_PATH = "/boot/firmware/config.txt"
DEFAULT_UDC_CLASS_DIR = "/sys/class/udc"
DEFAULT_I2S_HAT_INTENT_PATH = "/var/lib/jasper/i2s_hat.env"
I2S_HAT_INTENT_KEY = "JASPER_I2S_HAT_PROFILE"

MANAGED_BLOCK_BEGIN = "# BEGIN JTS USB DATA ROLE"
MANAGED_BLOCK_END = "# END JTS USB DATA ROLE"
I2S_HAT_BLOCK_BEGIN = "# BEGIN JTS I2S AUDIO HAT"
I2S_HAT_BLOCK_END = "# END JTS I2S AUDIO HAT"

BoardUsbTopology = Literal[
    "shared_otg_port",
    "separate_host_ports",
    "unsupported",
]
UsbDataRole = Literal["host", "peripheral", "unknown"]

_ROLE_LINE_RE = re.compile(
    r"^\s*dtoverlay\s*=\s*dwc2\s*,\s*dr_mode\s*=\s*(host|peripheral)\s*(?:#.*)?$",
    re.IGNORECASE,
)
_OVERLAY_LINE_RE = re.compile(
    r"^\s*dtoverlay\s*=\s*([^,\s#]+)",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
_LEGACY_COMMENT_START = "# JTS install — required for the composite USB gadget"


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
            if profile.connection == "i2s" and profile.dtoverlay
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


def _global_or_all_lines(content: str) -> tuple[str, ...]:
    """Return directives that apply globally or under the final ``[all]``.

    JTS owns its role and registered DAC overlays in that portable scope.  A
    carrier-specific block such as ``[cm5]`` must not affect a Zero merely
    because it appears in the same config file.
    """

    section = "global"
    out: list[str] = []
    for line in content.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip().lower()
            continue
        if section in {"global", "all"}:
            out.append(line)
    return tuple(out)


def configured_usb_role(content: str) -> UsbDataRole:
    role: UsbDataRole = "unknown"
    for line in _global_or_all_lines(content):
        match = _ROLE_LINE_RE.match(line)
        if match:
            role = match.group(1).lower()  # type: ignore[assignment]
    return role


def _overlay_values(lines: Iterable[str]) -> set[str]:
    overlays: set[str] = set()
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _OVERLAY_LINE_RE.match(line)
        if match:
            overlays.add(match.group(1).lower())
    return overlays


def configured_i2s_overlays(
    content: str,
    *,
    profiles: tuple[DacProfile, ...] | None = None,
) -> tuple[str, ...]:
    overlays = _overlay_values(_global_or_all_lines(content))
    candidates = profiles if profiles is not None else all_profiles()
    registered = {
        profile.dtoverlay.lower()
        for profile in candidates
        if profile.connection == "i2s" and profile.dtoverlay
    }
    return tuple(sorted(overlays & registered))


def overlay_declared_anywhere(content: str, overlay: str) -> bool:
    """Whether ``dtoverlay=<overlay>`` appears under ANY section, unlike
    :func:`configured_i2s_overlays`'s global/``[all]``-only view.

    The boot-config doctor check's fallback: a model-scoped line (say
    ``[pi5]``) must not false-FAIL a check whose only job is catching the
    line vanishing from the file entirely (#2575).
    """
    return overlay.lower() in _overlay_values(content.splitlines())


def read_i2s_hat_intent(
    path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
) -> str | None:
    try:
        text = read_regular_bytes_nofollow(path, max_bytes=1024).decode("utf-8")
    except FileNotFoundError:
        return None
    values = {
        key: (value or "").strip().strip("'\"")
        for key, value in parse_env_lines(text)
    }
    if set(values) - {I2S_HAT_INTENT_KEY}:
        raise ValueError("I2S HAT intent contains an unsupported key")
    choice = values.get(I2S_HAT_INTENT_KEY)
    if not choice:
        return None
    if choice != INNOMAKER_HIFI_AMP_PRO_ID:
        raise ValueError("I2S HAT intent names an unsupported profile")
    return INNOMAKER_HIFI_AMP_PRO_ID


def write_i2s_hat_intent(
    enabled: bool,
    path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
) -> None:
    target = Path(path)
    if not enabled:
        target.unlink(missing_ok=True)
        return
    atomic_write_text(
        target,
        f"{I2S_HAT_INTENT_KEY}={INNOMAKER_HIFI_AMP_PRO_ID}\n",
        mode=0o660,
        group_from_parent=True,
    )


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


def _read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").replace("\x00", "").strip()
    except OSError:
        return ""


def boot_config_path() -> Path:
    """``config.txt``'s resolved path, honoring ``JTS_BOOT_CONFIG_FILE``."""

    return Path(os.environ.get("JTS_BOOT_CONFIG_FILE", DEFAULT_BOOT_CONFIG_PATH))


def read_boot_config_or_none(path: str | Path | None = None) -> str | None:
    """``config.txt``'s content, or ``None`` if it could not be read.

    Unlike :func:`_read_text` (whose callers treat a transient USB-port-role
    read as equivalent to "not configured"), a caller confirming a saved
    line is genuinely GONE — not merely unreadable right now — needs that
    distinction kept.
    """
    target = Path(path) if path is not None else boot_config_path()
    try:
        return target.read_text(encoding="utf-8").replace("\x00", "")
    except OSError:
        return None


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
        board_model=_read_text(model_path),
        boot_config=_read_text(boot_config_path),
        active_role=observed_active_role(udc_class_dir),
        observed_output_profile_id=observed_output_profile_id,
    )


def _dwc2_parameters(line: str) -> tuple[str, ...] | None:
    directive = line.split("#", 1)[0].strip()
    key, separator, value = directive.partition("=")
    if not separator or key.strip().lower() != "dtoverlay":
        return None
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or parts[0].lower() != "dwc2":
        return None
    return parts[1:]


def _removable_dwc2_line(line: str) -> bool:
    parameters = _dwc2_parameters(line)
    if parameters is None:
        return False
    if not parameters:
        return True
    if len(parameters) == 1:
        key, separator, value = parameters[0].partition("=")
        if (
            separator
            and key.strip().lower() == "dr_mode"
            and value.strip().lower() in {"host", "peripheral"}
        ):
            return True
    raise ValueError(
        "ambiguous global/[all] dwc2 overlay; remove unsupported parameters "
        "before JTS reconciles the USB data role"
    )


def _without_legacy_comment(content: str) -> str:
    """Remove only the contiguous comment paragraph from the old installer.

    The old role directive itself is removed by the ordinary DWC2 pass.  This
    deliberately refuses a DOTALL pattern: intervening hardware directives
    can never become part of a migration match.
    """

    lines = content.splitlines(keepends=True)
    remove: set[int] = set()
    for index, line in enumerate(lines):
        if not line.strip().startswith(_LEGACY_COMMENT_START):
            continue
        cursor = index
        while cursor < len(lines) and lines[cursor].lstrip().startswith("#"):
            cursor += 1
        if cursor >= len(lines) or _SECTION_RE.match(lines[cursor]) is None:
            continue
        section = _SECTION_RE.match(lines[cursor])
        if section is None or section.group(1).strip().lower() != "all":
            continue
        role_index = cursor + 1
        while role_index < len(lines) and not lines[role_index].strip():
            role_index += 1
        if role_index < len(lines) and _ROLE_LINE_RE.match(lines[role_index]):
            remove.update(range(index, cursor))
    return "".join(line for index, line in enumerate(lines) if index not in remove)


def _collapse_empty_all_sections(content: str) -> str:
    """Collapse adjacent bare ``[all]`` headers separated only by blank lines.

    ``render_boot_config`` and ``render_i2s_hat_boot_config`` each append a
    fresh ``[all]`` header when the file doesn't already end in one, then
    strip only their own block's directives on the next pass -- leaving the
    now-empty header behind. Two (or more) adjacent ``[all]`` headers with
    nothing but blank lines between them are equivalent to one, so this
    heals both new growth and an already-bloated file in one pass.

    Only a header with no trailing comment is treated as droppable: a line
    like ``[all]  # keep me`` is never our own emission (the writer always
    emits a bare ``[all]``), so it and any comment it carries are left
    untouched rather than silently merged away.
    """
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().lower() == "[all]":
            cursor = index + 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor < len(lines) and lines[cursor].strip().lower() == "[all]":
                index = cursor
                continue
        output.append(line)
        index += 1
    return "".join(output)


def _without_managed_role_lines(content: str) -> str:
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    section = "global"
    in_managed_block = False
    for line in lines:
        if line.strip() == MANAGED_BLOCK_BEGIN:
            if in_managed_block:
                raise ValueError("nested JTS USB data-role block")
            in_managed_block = True
            continue
        if in_managed_block:
            if line.strip() == MANAGED_BLOCK_END:
                in_managed_block = False
            elif (
                line.strip()
                and not line.lstrip().startswith("#")
                and not _removable_dwc2_line(line)
            ):
                raise ValueError(
                    "unexpected directive inside JTS USB data-role block"
                )
            continue
        if line.strip() == MANAGED_BLOCK_END:
            raise ValueError("JTS USB data-role block ends without a beginning")
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip().lower()
            output.append(line)
            continue
        if section in {"global", "all"} and _removable_dwc2_line(line):
            continue
        output.append(line)
    if in_managed_block:
        raise ValueError("JTS USB data-role block is missing its end marker")
    return _collapse_empty_all_sections("".join(output))


def _without_managed_i2s_hat(content: str, *, overlay: str) -> str:
    output: list[str] = []
    section = "global"
    in_managed_block = False
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == I2S_HAT_BLOCK_BEGIN:
            if in_managed_block:
                raise ValueError("nested JTS I2S audio-HAT block")
            in_managed_block = True
            continue
        if stripped == I2S_HAT_BLOCK_END:
            if not in_managed_block:
                raise ValueError("JTS I2S audio-HAT block ends without a beginning")
            in_managed_block = False
            continue
        if in_managed_block:
            if stripped and not stripped.startswith("#"):
                match = _OVERLAY_LINE_RE.match(line)
                if (
                    match is None
                    or match.group(1).lower() != overlay.lower()
                    or "," in line.split("#", 1)[0]
                ):
                    raise ValueError("unexpected directive in JTS I2S HAT block")
            continue
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1).strip().lower()
            output.append(line)
            continue
        overlay_match = _OVERLAY_LINE_RE.match(line)
        if (
            section in {"global", "all"}
            and overlay_match is not None
            and overlay_match.group(1).lower() == overlay.lower()
        ):
            if "," in line.split("#", 1)[0]:
                raise ValueError(f"ambiguous {overlay} overlay parameters")
            continue
        output.append(line)
    if in_managed_block:
        raise ValueError("JTS I2S audio-HAT block is missing its end marker")
    return _collapse_empty_all_sections("".join(output))


def render_i2s_hat_boot_config(content: str, profile_id: str | None) -> str:
    if profile_id not in {None, INNOMAKER_HIFI_AMP_PRO_ID}:
        raise ValueError("unsupported I2S audio-HAT profile")
    profile = by_id(INNOMAKER_HIFI_AMP_PRO_ID)
    if profile is None or profile.connection != "i2s" or not profile.dtoverlay:
        raise ValueError("InnoMaker HAT profile is missing its I2S overlay metadata")
    cleaned = _without_managed_i2s_hat(content, overlay=profile.dtoverlay).rstrip()
    if profile_id is None:
        return cleaned + ("\n" if cleaned else "")
    last_line = cleaned.splitlines()[-1].strip().lower() if cleaned else ""
    section_prefix = "" if last_line == "[all]" else "[all]\n"
    separator = "\n" if last_line == "[all]" else "\n\n"
    block = section_prefix + (
        f"{I2S_HAT_BLOCK_BEGIN}\n"
        f"# JTS hardware reconciliation: enable {profile.label}.\n"
        f"dtoverlay={profile.dtoverlay}\n"
        f"{I2S_HAT_BLOCK_END}\n"
    )
    return f"{cleaned}{separator}{block}" if cleaned else block


def render_boot_config(content: str, desired_role: UsbDataRole) -> str:
    if desired_role == "unknown":
        return content
    cleaned = _without_legacy_comment(content)
    cleaned = _without_managed_role_lines(cleaned).rstrip()
    purpose = (
        "reserve the shared OTG port for output-DAC host mode"
        if desired_role == "host"
        else "enable the composite USB gadget on the OTG-capable port"
    )
    last_line = cleaned.splitlines()[-1].strip().lower() if cleaned else ""
    section_prefix = "" if last_line == "[all]" else "[all]\n"
    separator = "\n" if last_line == "[all]" else "\n\n"
    block = section_prefix + (
        f"{MANAGED_BLOCK_BEGIN}\n"
        f"# JTS hardware reconciliation: {purpose}.\n"
        "# Generated from board topology + registered DAC overlay; do not edit.\n"
        f"dtoverlay=dwc2,dr_mode={desired_role}\n"
        f"{MANAGED_BLOCK_END}\n"
    )
    return f"{cleaned}{separator}{block}" if cleaned else block


def reconcile_boot_config(
    *,
    model_path: str | Path,
    boot_config_path: str | Path,
    udc_class_dir: str | Path,
    i2s_hat_intent_path: str | Path | None = None,
) -> tuple[UsbPortRoleState, bool, bool, str | None, bool]:
    desired_profile = None
    if i2s_hat_intent_path is not None:
        desired_profile = read_i2s_hat_intent(i2s_hat_intent_path)
    config_path = Path(boot_config_path)
    if not config_path.is_file():
        state = resolve_system_usb_port_role(
            model_path=model_path,
            boot_config_path=boot_config_path,
            udc_class_dir=udc_class_dir,
        )
        if i2s_hat_intent_path is not None and state.board_topology != "unsupported":
            raise FileNotFoundError(f"boot config does not exist: {config_path}")
        return state, False, False, desired_profile, False
    original = config_path.read_text(encoding="utf-8")
    initial = resolve_usb_port_role(
        board_model=_read_text(model_path),
        boot_config=original,
        active_role=observed_active_role(udc_class_dir),
    )
    if initial.board_topology == "unsupported":
        return initial, False, False, desired_profile, False
    hat_changed = False
    with_hat = original
    if i2s_hat_intent_path is not None:
        profile = by_id(INNOMAKER_HIFI_AMP_PRO_ID)
        if profile is None or not profile.dtoverlay:
            raise ValueError("InnoMaker HAT profile has no boot overlay")
        hat_changed = (
            profile.dtoverlay in configured_i2s_overlays(original)
        ) != (desired_profile is not None)
        with_hat = render_i2s_hat_boot_config(original, desired_profile)
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
    return state, changed, hat_changed, desired_profile, durability_failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile-boot", action="store_true")
    parser.add_argument("--i2s-hat-intent-file")
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
    if args.reconcile_boot:
        result = reconcile_boot_config(
            model_path=args.model_file,
            boot_config_path=args.boot_config,
            udc_class_dir=args.udc_class_dir,
            i2s_hat_intent_path=args.i2s_hat_intent_file,
        )
        state, changed, hat_changed, desired_hat_profile, durability_failed = result
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
            f"reason={state.reason}"
        )
        return 0 if state.management_transport_available else 1
    fields = state.to_dict()
    fields["boot_config_changed"] = changed
    if args.i2s_hat_intent_file:
        fields["i2s_hat_profile"] = desired_hat_profile or ""
        fields["i2s_hat_boot_config_changed"] = hat_changed
        fields["boot_config_published_not_durable"] = durability_failed
    print(json.dumps(fields, sort_keys=True))
    print(
        "event=hardware.usb_role_resolved "
        f"topology={state.board_topology} desired={state.desired_role} "
        f"active={state.active_role} "
        f"gadget_available={str(state.gadget_available).lower()} "
        "management_transport_available="
        f"{str(state.management_transport_available).lower()} "
        f"reason={state.reason}"
    )
    if changed:
        print(
            "event=hardware.boot_config_changed "
            f"reboot_required={int(state.reboot_required)}"
        )
    return os.EX_IOERR if durability_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
