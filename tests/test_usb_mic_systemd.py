# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-usbmic.service"
APPLY_UNIT = ROOT / "deploy/systemd/jasper-usbmic-apply.service"
APPLY_RESULT = ROOT / "deploy/usbsink/jasper-usbmic-apply-result"
INSTALL = ROOT / "deploy/lib/install/systemd-units.sh"
INSTALL_SH = ROOT / "deploy/install.sh"
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


def test_installer_seeds_usb_mic_enable_and_leg_preferences() -> None:
    text = INSTALL_SH.read_text()
    assert "JASPER_USB_MIC=disabled" in text
    assert "JASPER_USB_MIC_LEG=primary" in text


def test_usb_mic_apply_is_durable_delayed_and_naturally_debounced() -> None:
    text = APPLY_UNIT.read_text()
    assert "Type=oneshot" in text
    assert "ExecStart=/bin/sleep 0.35" in text
    assert "ExecStartPost=/usr/bin/systemctl restart jasper-aec-bridge.service" in text
    assert "event=usb_mic.recompose_applied" in text
    assert "ExecStopPost=/usr/local/sbin/jasper-usbmic-apply-result" in text
    assert "TimeoutStartSec=" in text


def test_usb_mic_apply_reaches_the_gadget_only_through_its_converger() -> None:
    """#3194: a mic toggle must not be a fourth direct owner of the descriptor.

    The converger compares the live ConfigFS composition (the mic is its
    ``usb_mic`` field) against the truth table, so re-applying an unchanged
    shape performs no rebind at all.
    """

    text = APPLY_UNIT.read_text()
    assert "ExecStartPost=/usr/local/sbin/jasper-usbgadget-converge usb_mic" in text
    assert "jasper-usbgadget.service" not in text


def _directive(text: str, key: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip()
    raise AssertionError(f"{key}= not found in {APPLY_UNIT}")


def _seconds(raw: str) -> float:
    """Parse the systemd time spans this unit uses (bare seconds, `s`, `min`)."""
    value = raw.strip()
    for suffix, scale in (("min", 60.0), ("s", 1.0)):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * scale
    return float(value)


def test_usb_mic_apply_retries_accepted_failures_with_a_hard_bound() -> None:
    text = APPLY_UNIT.read_text()
    assert "Restart=on-failure" in text
    assert int(_directive(text, "StartLimitBurst")) == 4


def test_usb_mic_apply_start_limit_window_outlasts_its_own_worst_case_run() -> None:
    """#3194 round 2 (B2): a burst only bounds retries if the window can see it.

    The failure this limit exists for is a HANG, which spends the whole
    TimeoutStartSec before each RestartSec backoff — so the burst-th start lands
    ``(burst - 1)`` such cycles after the first. A StartLimitIntervalSec shorter
    than that span never counts four starts, the limit never trips, and the
    recompose loop runs forever with a gadget re-enumeration per attempt. Pinned
    as the RELATIONSHIP, not as two literals, so raising the timeout again
    cannot silently disarm the bound.
    """

    text = APPLY_UNIT.read_text()
    interval = _seconds(_directive(text, "StartLimitIntervalSec"))
    burst = int(_directive(text, "StartLimitBurst"))
    timeout = _seconds(_directive(text, "TimeoutStartSec"))
    backoff = _seconds(_directive(text, "RestartSec"))

    worst_case_span = (burst - 1) * (timeout + backoff)
    assert interval > worst_case_span, (
        interval,
        burst,
        timeout,
        backoff,
        worst_case_span,
    )


def test_usb_mic_apply_failure_helper_emits_only_for_failure() -> None:
    text = APPLY_RESULT.read_text()
    assert '${SERVICE_RESULT:-unknown}" = "success"' in text
    assert "event=usb_mic.recompose_failed" in text
    assert "phase=apply" in text
    assert "result=${SERVICE_RESULT:-unknown}" in text
    assert "exit_code=${EXIT_CODE:-unknown}" in text
    assert "exit_status=${EXIT_STATUS:-unknown}" in text


def test_installer_creates_dedicated_least_privilege_usb_mic_user() -> None:
    text = SERVICE_USERS.read_text()
    assert "getent passwd jasper-usbmic" in text
    assert "-g jasper -G audio jasper-usbmic" in text
    assert "-G input jasper-usbmic" not in text
