"""Test install.sh voice-provider id manifest generation."""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from jasper.voice.catalog import provider_ids_manifest_text


ROOT = Path(__file__).resolve().parents[1]
ENV_MIGRATIONS_LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"


def _extract_render_helper() -> str:
    helper = subprocess.run(
        [
            "bash",
            "-c",
            rf"sed -n '/^render_voice_provider_ids_manifest()/,/^}}/p' '{ENV_MIGRATIONS_LIB}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "render_voice_provider_ids_manifest()" in helper
    return helper


def _run_render(
    tmp_path: Path,
    *,
    python_bin: str = sys.executable,
) -> subprocess.CompletedProcess[str]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "STATE_DIR": str(state_dir),
        "INSTALL_DIR": str(tmp_path / "opt" / "jasper"),
        "JASPER_INSTALL_PYTHON": python_bin,
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{_extract_render_helper()}\nrender_voice_provider_ids_manifest"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_install_renders_voice_provider_ids_manifest(tmp_path: Path) -> None:
    proc = _run_render(tmp_path)

    assert proc.returncode == 0, proc.stderr
    manifest = tmp_path / "state" / "voice_provider_ids"
    assert manifest.read_text() == provider_ids_manifest_text()
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o644
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o750


def test_install_manifest_generation_failure_fails_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "state" / "voice_provider_ids"
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text("stale-provider\n")

    proc = _run_render(tmp_path, python_bin="/bin/false")

    assert proc.returncode == 0, proc.stderr
    assert "could not generate" in proc.stdout
    assert not manifest.exists()
