"""Tests for migrate_voice_provider in deploy/lib/install/env-migrations.sh."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_MIGRATIONS_LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"


def _extract_helper() -> str:
    helper = subprocess.run(
        [
            "bash",
            "-c",
            rf"sed -n '/^migrate_voice_provider()/,/^}}/p' '{ENV_MIGRATIONS_LIB}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "migrate_voice_provider()" in helper
    return helper


def _run_migrate(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ENV_DIR": str(env_dir),
        "STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{_extract_helper()}\nmigrate_voice_provider"],
        env=env,
        capture_output=True,
        text=True,
    )


def _read_keys(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_migrates_legacy_provider_to_wizard_file(tmp_path: Path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    wizard_env = state_dir / "voice_provider.env"
    jasper_env.write_text(
        "GEMINI_API_KEY=op-key\n"
        "JASPER_VOICE_PROVIDER=openai\n"
        "JASPER_HOSTNAME=jts.local\n"
    )

    proc = _run_migrate(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert _read_keys(jasper_env) == {
        "GEMINI_API_KEY": "op-key",
        "JASPER_HOSTNAME": "jts.local",
    }
    assert _read_keys(wizard_env)["JASPER_VOICE_PROVIDER"] == "openai"
    assert stat.S_IMODE(wizard_env.stat().st_mode) == 0o600


def test_existing_wizard_provider_wins_over_legacy_provider(tmp_path: Path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    wizard_env = state_dir / "voice_provider.env"
    jasper_env.write_text(
        "JASPER_VOICE_PROVIDER=gemini\n"
        "JASPER_HOSTNAME=jts.local\n"
    )
    wizard_env.write_text("JASPER_VOICE_PROVIDER=openai\n")
    wizard_env.chmod(0o600)

    proc = _run_migrate(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "JASPER_VOICE_PROVIDER" not in _read_keys(jasper_env)
    assert _read_keys(wizard_env)["JASPER_VOICE_PROVIDER"] == "openai"


def test_empty_legacy_provider_is_removed_without_creating_wizard_file(
    tmp_path: Path,
):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    jasper_env = env_dir / "jasper.env"
    wizard_env = state_dir / "voice_provider.env"
    jasper_env.write_text(
        "JASPER_VOICE_PROVIDER=\n"
        "JASPER_HOSTNAME=jts.local\n"
    )

    proc = _run_migrate(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert "JASPER_VOICE_PROVIDER" not in _read_keys(jasper_env)
    assert not wizard_env.exists()
