# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free contracts for installed audio prerequisites."""

from __future__ import annotations

import subprocess
from pathlib import Path

from jasper.cli.doctor import audio


def test_check_loopback_reports_present_card(monkeypatch) -> None:
    def fake_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        assert timeout == 5.0
        assert cmd == ["aplay", "-L"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="null\nhw:CARD=Loopback,DEV=0\n",
            stderr="",
        )

    monkeypatch.setattr(audio, "_run", fake_run)

    result = audio.check_loopback()

    assert result.name == "snd-aloop"
    assert result.status == "ok"
    assert result.detail == "CARD=Loopback present"


def test_check_loopback_reports_missing_card_with_remediation(monkeypatch) -> None:
    def fake_run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
        assert timeout == 5.0
        assert cmd == ["aplay", "-L"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="null\ndefault\n",
            stderr="",
        )

    monkeypatch.setattr(audio, "_run", fake_run)

    result = audio.check_loopback()

    assert result.name == "snd-aloop"
    assert result.status == "fail"
    assert result.detail == (
        "Loopback device missing. `sudo modprobe snd-aloop` or check "
        "/etc/modules-load.d/snd-aloop.conf"
    )


def test_check_fanin_binary_installed_reports_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    monkeypatch.setattr(audio, "Path", lambda _path: binary)

    result = audio.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "fail"
    assert result.detail == (
        f"{binary} missing. Re-run install.sh; check cargo build "
        "output for compilation errors."
    )


def test_check_fanin_binary_installed_reports_nonexecutable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    binary.write_bytes(b"fan-in")
    binary.chmod(0o644)
    monkeypatch.setattr(audio, "Path", lambda _path: binary)

    result = audio.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "fail"
    assert result.detail == (
        f"{binary} present but not executable. Run: sudo chmod +x {binary}"
    )


def test_check_fanin_binary_installed_reports_executable_size(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "jasper-fanin"
    binary.write_bytes(b"x" * 2500)
    binary.chmod(0o755)
    monkeypatch.setattr(audio, "Path", lambda _path: binary)

    result = audio.check_fanin_binary_installed()

    assert result.name == "jasper-fanin binary"
    assert result.status == "ok"
    assert result.detail == f"{binary} (2 KB)"
