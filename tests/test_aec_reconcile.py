from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "systemctl.log"
    fake = tmp_path / "systemctl"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake, log


def _run_reconcile(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    fake_systemctl, systemctl_log = _fake_systemctl(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "JASPER_ENV_FILE": str(tmp_path / "jasper.env"),
            "JASPER_AEC_MODE_FILE": str(tmp_path / "aec_mode.env"),
            "JASPER_ASOUND_ROOT": str(tmp_path / "asound"),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def _write_env(tmp_path: Path, mic_device: str, extra: str = "") -> Path:
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        f"JASPER_MIC_DEVICE={mic_device}\n"
        "JASPER_AEC_UDP_PORT=9876\n"
        f"{extra}"
    )
    return env_file


def _write_mode(tmp_path: Path, mode: str = "auto") -> None:
    (tmp_path / "aec_mode.env").write_text(f"JASPER_AEC_MODE={mode}\n")


def _write_card(tmp_path: Path, card: str = "Array", channels: int = 6) -> None:
    card_dir = tmp_path / "asound" / card
    card_dir.mkdir(parents=True)
    (card_dir / "stream0").write_text(
        f"Playback:\n  Status: Stop\nCapture:\n  Channels: {channels}\n"
    )


def _systemctl_log(tmp_path: Path) -> str:
    log = tmp_path / "systemctl.log"
    return log.read_text() if log.exists() else ""


def test_reconcile_clears_stale_udp_when_array_is_absent(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "stop jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service" in commands
    assert "restart jasper-voice.service" not in commands


def test_reconcile_enables_udp_aec_when_array_is_6_channel(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=udp:9876" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "enable jasper-aec-init.service jasper-aec-bridge.service" in commands
    assert "start jasper-aec-init.service" in commands
    assert "restart jasper-aec-bridge.service" in commands
    assert "restart jasper-voice.service" in commands


def test_reconcile_uses_direct_mic_when_array_is_not_6_channel(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "udp:9876")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=2)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=Array" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "restart jasper-voice.service" in commands


def test_reconcile_respects_custom_mic_device(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path, "UMIK-2")
    _write_mode(tmp_path)

    result = _run_reconcile(tmp_path, "--reason", "test")

    assert result.returncode == 0, result.stderr
    assert "JASPER_MIC_DEVICE=UMIK-2" in env_file.read_text()
    commands = _systemctl_log(tmp_path)
    assert "disable jasper-aec-bridge.service jasper-aec-init.service" in commands
    assert "stop jasper-voice.service" not in commands
    assert "restart jasper-voice.service" not in commands


def test_check_aec_ready_reflects_mode_and_firmware(tmp_path: Path) -> None:
    _write_env(tmp_path, "Array")
    _write_mode(tmp_path)
    _write_card(tmp_path, channels=6)
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 0

    (tmp_path / "aec_mode.env").write_text("JASPER_AEC_MODE=disabled\n")
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 1

    (tmp_path / "aec_mode.env").write_text("JASPER_AEC_MODE=auto\n")
    (tmp_path / "asound" / "Array" / "stream0").write_text("Capture:\n  Channels: 2\n")
    assert _run_reconcile(tmp_path, "--check-aec-ready").returncode == 1
