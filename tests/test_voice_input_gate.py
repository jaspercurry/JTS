# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice-input gate for jasper-voice.

Pins the pieces of docs/HANDOFF-hotplug-resilience.md that keep a
no-input box from crash-looping into StartLimitAction=reboot:

1. jasper-voice.service gates ExecStart on the reconciler-written marker
   (ConditionPathExists), and parks (not crash-loops) on the
   mic-unavailable exit code.
2. The marker path agrees across the unit, the bash reconciler default,
   and the Python reader — a drift here silently breaks the gate.
3. The daemon exits VOICE_MIC_UNAVAILABLE_EXIT on a primary mic-open
   failure, and the doctor reports the parked state as expected-idle.
4. The gate is an OR over a local mic and a paired accessory mic
   (issue #2205): the accessory env path agrees across its owner, the unit,
   and env_load, and the bash reconciler carries no copy of it because it
   asks jasper.accessories.mic_env instead.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.accessories.mic_env import DEFAULT_ACCESSORY_MIC_ENV_FILE
from jasper.audio_io import InputDeviceUnavailable
from jasper.env_load import ENV_FILES
from jasper.voice.input_presence import (
    DEFAULT_VOICE_INPUT_ABSENT_MARKER,
    voice_input_absent_marker_path,
    voice_parked_no_mic,
)
from jasper.voice_daemon import VOICE_MIC_UNAVAILABLE_EXIT

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "jasper-voice.service"
RECONCILE = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"


def _unit_text() -> str:
    return UNIT.read_text()


def _directive_values(text: str, key: str) -> list[str]:
    """Tokens of the last `key=` line in a unit file (systemd: last wins)."""
    vals: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            vals = line[len(key) + 1:].split()
    return vals


def test_voice_service_has_mic_presence_condition() -> None:
    text = _unit_text()
    cond = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("ConditionPathExists=")
    ]
    assert cond, "jasper-voice.service must gate on the mic-presence marker"
    # Negated (fail-open): "run UNLESS the marker exists".
    assert any(c == f"ConditionPathExists=!{DEFAULT_VOICE_INPUT_ABSENT_MARKER}"
               for c in cond), cond


def test_voice_service_starts_udp_mic_producer_softly() -> None:
    """The default/reconciler-managed mic is udp:9876, produced by
    jasper-aec-bridge. If voice starts while the bridge is inactive, the UDP
    capture socket binds but receives no frames, and voice watchdog-restarts.

    This must stay a soft dependency: bridge restarts should not cascade-stop an
    otherwise healthy voice daemon, and no-mic/custom-mic boxes should still use
    the reconciler's existing gates.
    """
    text = _unit_text()
    after = _directive_values(text, "After")
    wants = _directive_values(text, "Wants")
    assert "jasper-aec-bridge.service" in after, after
    assert "jasper-aec-bridge.service" in wants, wants
    assert "Requires=jasper-aec-bridge.service" not in text


def test_voice_service_parks_on_mic_unavailable_exit() -> None:
    text = _unit_text()
    success = _directive_values(text, "SuccessExitStatus")
    prevent = _directive_values(text, "RestartPreventExitStatus")
    code = str(VOICE_MIC_UNAVAILABLE_EXIT)
    # Both the mic-unavailable code (66) and the provider-unset code (78)
    # must park cleanly — neither may consume the reboot budget.
    assert code in success, success
    assert code in prevent, prevent
    assert "78" in success and "78" in prevent, (success, prevent)


def test_marker_path_agreement() -> None:
    """The marker path is duplicated in three places (unit literal, bash
    default, Python default). A mismatch silently disables the gate, so
    pin it."""
    unit_text = _unit_text()
    unit_paths = [
        line.strip()[len("ConditionPathExists=!"):]
        for line in unit_text.splitlines()
        if line.strip().startswith("ConditionPathExists=!")
    ]
    assert unit_paths == [DEFAULT_VOICE_INPUT_ABSENT_MARKER], unit_paths

    m = re.search(
        r'VOICE_INPUT_ABSENT_MARKER="\$\{JASPER_VOICE_INPUT_ABSENT_MARKER:-([^}]+)\}"',
        RECONCILE.read_text(),
    )
    assert m, "reconciler must define VOICE_INPUT_ABSENT_MARKER with a default"
    assert m.group(1) == DEFAULT_VOICE_INPUT_ABSENT_MARKER, m.group(1)


def test_accessory_mic_env_path_agreement() -> None:
    """The accessory half of the gate is one file with one writer. Its path is
    duplicated in the unit's EnvironmentFile= and env_load's list; pin both
    against the owning module, the same treatment the marker path gets."""
    unit_text = _unit_text()
    assert (
        f"EnvironmentFile=-{DEFAULT_ACCESSORY_MIC_ENV_FILE}" in unit_text
    ), DEFAULT_ACCESSORY_MIC_ENV_FILE
    assert DEFAULT_ACCESSORY_MIC_ENV_FILE in ENV_FILES, ENV_FILES


def test_reconciler_asks_python_for_the_accessory_half() -> None:
    """The bash reconciler must NOT carry a copy of the accessory env path or
    re-derive the entry format in shell — a second parser is how the gate and
    the daemon come to disagree about whether a source is published. It shells
    to the owning module instead (same posture as the mic-profile resolver)."""
    script = RECONCILE.read_text()
    assert "jasper.accessories.mic_env" in script
    # Prose may name the path and the key; executable shell may not.
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert DEFAULT_ACCESSORY_MIC_ENV_FILE not in code
    assert "JASPER_MANUAL_MIC_SOURCES" not in code


def test_gate_owner_can_actually_report_enabled() -> None:
    """``refresh_voice_input`` starts the gate owner ONLY when systemd reports
    ``UnitFileState=enabled``. That permission is unreachable for a unit with no
    ``[Install]`` section — systemd reports such a unit ``static`` — and it is
    only ever granted because ``install.sh`` enables it.

    Drop either and the accessory half of the gate goes dead on every full
    speaker with **no other test failing**: the reconciler would classify a
    perfectly healthy owner as ``parked``, fall through to ``try-restart``, and
    a freshly-paired remote would never re-derive the marker. Observed
    ``LoadState=loaded UnitFileState=enabled`` on jts3 (2026-08-07)."""
    owner_unit = ROOT / "deploy" / "systemd" / "jasper-aec-reconcile.service"
    assert "[Install]" in owner_unit.read_text(), owner_unit
    installer = (ROOT / "deploy" / "install.sh").read_text()
    enable_lines = [
        line.strip() for line in installer.splitlines()
        if "systemctl enable" in line and "jasper-aec-reconcile.service" in line
        and not line.lstrip().startswith("#")
    ]
    assert enable_lines, "install.sh must enable the voice-input gate owner"


def test_voice_parked_no_mic_reads_marker(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "voice-input-absent"
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))
    assert voice_input_absent_marker_path() == str(marker)
    assert voice_parked_no_mic() is False
    marker.write_text("reason=test\n")
    assert voice_parked_no_mic() is True
    marker.unlink()
    assert voice_parked_no_mic() is False


def test_input_device_unavailable_carries_device() -> None:
    cause = ValueError("No input device matching 'Array'")
    exc = InputDeviceUnavailable("Array", cause)
    assert exc.device == "Array"
    assert "Array" in str(exc)
    # The original cause is preserved for the forensic log.
    assert "No input device matching" in str(exc)


def test_main_exits_mic_unavailable_code(monkeypatch) -> None:
    """main() must translate a primary mic-open failure into a clean
    VOICE_MIC_UNAVAILABLE_EXIT, not let it crash with a traceback (exit 1
    → systemd Restart=on-failure → crash-loop)."""
    from jasper.voice import daemon_main

    async def _boom() -> None:
        raise InputDeviceUnavailable("Array", ValueError("absent"))

    monkeypatch.setattr(daemon_main, "run", _boom)
    with pytest.raises(SystemExit) as exc:
        daemon_main.main()
    assert exc.value.code == VOICE_MIC_UNAVAILABLE_EXIT


def test_check_mic_capture_reports_expected_idle_when_marked(
    tmp_path, monkeypatch,
) -> None:
    from jasper.cli.doctor import audio

    monkeypatch.setattr(audio, "_parked_as_bonded_follower", lambda: False)
    marker = tmp_path / "voice-input-absent"
    marker.write_text("reason=test\n")
    monkeypatch.setenv("JASPER_VOICE_INPUT_ABSENT_MARKER", str(marker))

    # Marker present → early ok return, before any device access, so a
    # bare stand-in cfg is enough.
    result = audio.check_mic_capture(SimpleNamespace())
    assert result.status == "ok"
    assert "no usable microphone input" in result.detail.lower()
