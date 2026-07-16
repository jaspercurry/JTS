# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-usbmic.service"
APPLY_UNIT = ROOT / "deploy/systemd/jasper-usbmic-apply.service"
APPLY_RESULT = ROOT / "deploy/usbsink/jasper-usbmic-apply-result"
INSTALL = ROOT / "deploy/lib/install/systemd-units.sh"
SERVICE_USERS = ROOT / "deploy/lib/install/service-users.sh"


def test_usb_mic_service_is_dependency_enabled_and_gadget_scoped() -> None:
    text = UNIT.read_text()
    assert "ExecCondition=/opt/jasper/.venv/bin/jasper-usbmic --check-ready" in text
    assert "ExecStart=/opt/jasper/.venv/bin/jasper-usbmic" in text
    assert "After=jasper-usbgadget.service jasper-aec-bridge.service" in text
    assert (
        "PartOf=jasper-usbgadget.service jasper-aec-bridge.service" in text
    )
    assert "WantedBy=jasper-usbgadget.service jasper-aec-bridge.service" in text
    assert "User=jasper-usbmic" in text
    assert "OOMScoreAdjust=-300" in text
    assert "MemoryMax=" in text


def test_installer_stages_and_enables_usb_mic_service() -> None:
    text = INSTALL.read_text()
    assert "deploy/systemd/jasper-usbmic.service" in text
    assert "deploy/systemd/jasper-usbmic-apply.service" in text
    assert "deploy/usbsink/jasper-usbmic-apply-result" in text
    assert "systemctl enable jasper-usbmic.service" in text


def test_usb_mic_apply_is_durable_delayed_and_naturally_debounced() -> None:
    text = APPLY_UNIT.read_text()
    assert "Type=oneshot" in text
    assert "ExecStart=/bin/sleep 0.35" in text
    assert (
        "ExecStartPost=/usr/bin/systemctl restart "
        "jasper-aec-bridge.service jasper-usbgadget.service"
    ) in text
    assert "event=usb_mic.recompose_applied" in text
    assert "ExecStopPost=/usr/local/sbin/jasper-usbmic-apply-result" in text
    assert "TimeoutStartSec=" in text
    assert "Restart=" not in text


def test_usb_mic_apply_failure_helper_emits_only_for_failure() -> None:
    text = APPLY_RESULT.read_text()
    assert '${SERVICE_RESULT:-unknown}" = "success"' in text
    assert "event=usb_mic.recompose_failed" in text
    assert "result=${SERVICE_RESULT:-unknown}" in text
    assert "exit_code=${EXIT_CODE:-unknown}" in text
    assert "exit_status=${EXIT_STATUS:-unknown}" in text


def test_installer_creates_dedicated_least_privilege_usb_mic_user() -> None:
    text = SERVICE_USERS.read_text()
    assert "getent passwd jasper-usbmic" in text
    assert "-g jasper -G audio jasper-usbmic" in text
    assert "-G input jasper-usbmic" not in text
