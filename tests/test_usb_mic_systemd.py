# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import os
import re
import subprocess
from pathlib import Path

import pytest

from jasper.usb_mic import GADGET_PATH, INTENT_PATH, usb_mic_enabled
from tests.systemd_unit_helpers import seconds_for, values_for


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-usbmic.service"
APPLY_UNIT = ROOT / "deploy/systemd/jasper-usbmic-apply.service"
APPLY_RESULT = ROOT / "deploy/usbsink/jasper-usbmic-apply-result"
INSTALL = ROOT / "deploy/lib/install/systemd-units.sh"
INSTALL_SH = ROOT / "deploy/install.sh"
SERVICE_USERS = ROOT / "deploy/lib/install/service-users.sh"


def test_usb_mic_service_is_dependency_enabled_and_gadget_scoped() -> None:
    text = UNIT.read_text()
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
    interval = seconds_for(text, "StartLimitIntervalSec")
    burst = int(_directive(text, "StartLimitBurst"))
    timeout = seconds_for(text, "TimeoutStartSec")
    backoff = seconds_for(text, "RestartSec")

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


CHMASK_PATH = f"{GADGET_PATH}/functions/uac2.usb0/p_chmask"
ENABLED = "JASPER_USB_MIC=enabled\n"
OFF = "intent_off_or_invalid"


def _condition_script(tmp_path: Path) -> str:
    """The unit's ExecCondition as /bin/sh receives it, retargeted at tmp_path.

    systemd resolves C escapes, ``%%`` specifiers and ``$$`` before exec, inside
    its single quotes as well; replaying all three rules is what makes this the
    shipped line rather than a copy of it. The two absolute paths are the ones
    Python reads, so a unit that drifts off them fails here.
    """
    argv = values_for(UNIT.read_text(), "ExecCondition")
    assert argv[:2] == ("/bin/sh", "-c")
    script = argv[2].replace("\\\\", "\\").replace("%%", "%").replace("$$", "$")
    for literal, name in ((INTENT_PATH, "usb_mic.env"), (CHMASK_PATH, "p_chmask")):
        assert script.count(literal) == 1, literal
        script = script.replace(literal, str(tmp_path / name))
    return script


def _run_condition(
    tmp_path: Path, intent: str | None, chmask: str | None, bridge: bool
) -> subprocess.CompletedProcess[str]:
    if intent is not None:
        (tmp_path / "usb_mic.env").write_text(intent, encoding="utf-8")
    if chmask is not None:
        (tmp_path / "p_chmask").write_text(chmask, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text(f"#!/bin/sh\nexit {0 if bridge else 3}\n", encoding="utf-8")
    stub.chmod(0o755)
    # macOS ships no `timeout(1)`; the condition script wraps its systemctl
    # call in one, so stand in a passthrough (matches test_control_systemd.py).
    timeout_stub = bin_dir / "timeout"
    timeout_stub.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8")
    timeout_stub.chmod(0o755)
    return subprocess.run(
        ["/bin/sh", "-c", _condition_script(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


@pytest.mark.parametrize(
    ("intent", "chmask", "bridge", "reason"),
    [
        pytest.param(ENABLED, "1\n", True, "", id="ready"),
        pytest.param(
            '\tJASPER_USB_MIC = "enabled" \n', "1\n", True, "", id="quoted"
        ),
        pytest.param("JASPER_USB_MIC=disabled\n", "1\n", True, OFF, id="disabled"),
        pytest.param(
            ENABLED + "JASPER_USB_MIC=disabled\n", "1\n", True, OFF, id="last_wins"
        ),
        pytest.param(None, "1\n", True, OFF, id="intent_missing"),
        pytest.param("JASPER_USB_MIC=maybe\n", "1\n", True, OFF, id="intent_corrupt"),
        pytest.param(ENABLED, None, True, "uac2_missing", id="uac2_missing"),
        pytest.param(ENABLED, "2\n", True, "p_chmask_2", id="chmask_stereo"),
        pytest.param(ENABLED, "1\n", False, "aec_bridge_inactive", id="bridge_inactive"),
    ],
)
def test_usb_mic_start_condition_gates_on_intent_gadget_and_bridge(
    tmp_path: Path,
    intent: str | None,
    chmask: str | None,
    bridge: bool,
    reason: str,
) -> None:
    """#3697: the start gate is inline sh, so RUN it — intent (fail-closed and
    byte-for-byte the intent jasper.usb_mic.read_intent reports), then the UAC2
    capture channel mask, then the AEC bridge. Exit 0 starts the relay; any
    non-zero exit is a clean skip that names its reason.
    """
    result = _run_condition(tmp_path, intent, chmask, bridge)

    assert usb_mic_enabled(tmp_path / "usb_mic.env") is (reason != OFF)
    if reason:
        assert result.returncode != 0
        assert f"event=usb_mic.skip reason={reason}" in result.stderr
    else:
        assert result.returncode == 0
        assert result.stderr == ""


def test_usb_mic_start_condition_doubles_every_percent() -> None:
    """A bare ``%x`` in the ExecCondition is a systemd specifier, not shell.

    ``printf "%s"`` shipped once and expanded to the user shell at unit load,
    so the gate compared a path instead of the intent line and skipped the
    unit on every start. ``systemd-analyze verify`` parses that happily, and
    the runner above replays ``%%`` -> ``%``, so only this pin catches it.
    """
    condition = values_for(UNIT.read_text(), "ExecCondition")[2]

    assert not re.search(r"(?<!%)(?:%%)*%(?!%)", condition)
