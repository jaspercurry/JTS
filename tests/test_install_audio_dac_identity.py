"""Test install.sh audio DAC identity seeding."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "deploy" / "install.sh"


def _run_seed(tmp_path: Path, *, aplay_output: str) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    env_dir.mkdir(exist_ok=True)
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    aplay = fakebin / "aplay"
    aplay.write_text(f"#!/bin/sh\ncat <<'EOF'\n{aplay_output}EOF\n")
    aplay.chmod(0o755)
    env = {
        "PATH": f"{fakebin}:{os.environ.get('PATH', '')}",
        "ENV_DIR": str(env_dir),
    }
    script = (
        f"source '{INSTALL_SH}' >/dev/null && "
        f"ENV_DIR='{env_dir}' && "
        "seed_audio_dac_identity"
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_seed_audio_dac_identity_adds_apple_dongle_id(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text("JASPER_HOSTNAME=jts.local\n")
    aplay_output = (
        "default:CARD=A\n"
        "    Apple USB-C to 3.5mm Headphone Jack Adapter, USB Audio\n"
    )

    proc = _run_seed(tmp_path, aplay_output=aplay_output)

    assert proc.returncode == 0, proc.stderr
    text = (env_dir / "jasper.env").read_text()
    assert "JASPER_AUDIO_DAC_ID=apple_usb_c_dongle\n" in text
    assert "audio DAC identity: apple_usb_c_dongle" in proc.stdout


def test_seed_audio_dac_identity_preserves_operator_value(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text("JASPER_AUDIO_DAC_ID=custom_dac\n")
    aplay_output = (
        "default:CARD=A\n"
        "    Apple USB-C to 3.5mm Headphone Jack Adapter, USB Audio\n"
    )

    proc = _run_seed(tmp_path, aplay_output=aplay_output)

    assert proc.returncode == 0, proc.stderr
    assert (env_dir / "jasper.env").read_text() == "JASPER_AUDIO_DAC_ID=custom_dac\n"
    assert proc.stdout == ""


def test_seed_audio_dac_identity_leaves_unknown_dac_unset(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text("JASPER_HOSTNAME=jts.local\n")

    proc = _run_seed(tmp_path, aplay_output="")

    assert proc.returncode == 0, proc.stderr
    text = (env_dir / "jasper.env").read_text()
    assert "JASPER_AUDIO_DAC_ID" not in text
    assert "audio DAC identity: unknown" in proc.stdout
