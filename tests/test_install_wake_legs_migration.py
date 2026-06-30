# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test install.sh migrate_wake_legs_config — the chip-AEC promotion's
hand-set-var migration (and the raw/DTLN translation it extends).

The helper translates legacy hand-set underlying leg env vars in
/etc/jasper/jasper.env into the wizard-owned boolean form in
/var/lib/jasper/aec_mode.env, then strips the underlying vars so the
reconciler is the only writer going forward. Mirrors the harness in
test_install_weather_migration.py (sed-extract the bash function, run it
with ENV_DIR/STATE_DIR pointed at tmp paths).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_MIGRATIONS_LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"


def _run_migrate(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    helper = subprocess.run(
        ["bash", "-c",
         rf"sed -n '/^migrate_wake_legs_config()/,/^}}/p' '{ENV_MIGRATIONS_LIB}'"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "migrate_wake_legs_config()" in helper
    helper = 'ensure_state_dir() { install -d -m 0750 "${STATE_DIR}"; }\n' + helper
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ENV_DIR": str(env_dir),
        "STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{helper}\nmigrate_wake_legs_config"],
        env=env, capture_output=True, text=True,
    )


def test_chip_aec_hand_set_device_migrates_to_boolean(tmp_path):
    """A legacy hand-set JASPER_MIC_DEVICE_CHIP_AEC_* in jasper.env
    translates to JASPER_WAKE_LEG_CHIP_AEC=1 in aec_mode.env, and the
    underlying device + enable vars are stripped (reconciler becomes the
    sole writer). Extra beam opt-ins require custom so the reconciler does
    not collapse them back to the one-detector chip-AEC profile."""
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text(
        "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887\n"
        "JASPER_MIC_DEVICE_CHIP_AEC_210=udp:9888\n"
        "JASPER_AEC_CHIP_AEC_ENABLED=1\n"
    )
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    mode = (tmp_path / "state" / "aec_mode.env").read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=custom" in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=1" in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC_210=1" in mode
    jasper_env = (env_dir / "jasper.env").read_text()
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150" not in jasper_env
    assert "JASPER_MIC_DEVICE_CHIP_AEC_210" not in jasper_env
    assert "JASPER_AEC_CHIP_AEC_ENABLED" not in jasper_env


def test_chip_aec_defaults_off_when_only_other_legs_present(tmp_path):
    """When only raw is hand-set (no chip vars), the migration writes
    JASPER_WAKE_LEG_CHIP_AEC=0 — it never silently turns the chip leg on."""
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text("JASPER_MIC_DEVICE_RAW=udp:9877\n")
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    mode = (tmp_path / "state" / "aec_mode.env").read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=xvf_software_aec3" in mode
    assert "JASPER_WAKE_LEG_RAW=1" in mode        # raw preserved
    assert "JASPER_WAKE_LEG_CHIP_AEC=0" in mode   # chip defaulted off
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=0" in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC_210=0" in mode


def test_migrate_no_op_when_no_underlying_vars(tmp_path):
    """Fresh install (no underlying leg vars at all): the migration is a
    no-op and leaves aec_mode.env for reconcile_aec_state to seed."""
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text("JASPER_VOICE_PROVIDER=gemini\n")
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "state" / "aec_mode.env").exists()


def test_chip_aec_boolean_not_overwritten_when_already_set(tmp_path):
    """Idempotent: an aec_mode.env that already carries the chip boolean
    keeps the operator's wizard choice; the re-run only strips the
    (reconciler-rewritten) underlying vars."""
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text(
        "JASPER_MIC_DEVICE_CHIP_AEC_150=udp:9887\n"
    )
    (state_dir / "aec_mode.env").write_text(
        "JASPER_AEC_MODE=auto\n"
        "JASPER_AUDIO_INPUT_PROFILE=xvf_software_aec3\n"
        "JASPER_WAKE_LEG_RAW=1\n"
        "JASPER_WAKE_LEG_DTLN=0\n"
        "JASPER_WAKE_LEG_CHIP_AEC=0\n"
    )
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    mode = (state_dir / "aec_mode.env").read_text()
    assert "JASPER_AUDIO_INPUT_PROFILE=xvf_software_aec3" in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC=0" in mode      # preserved, not bumped
    assert "JASPER_WAKE_LEG_CHIP_AEC=1" not in mode
    assert "JASPER_WAKE_LEG_CHIP_AEC_150=0" in mode
    assert "JASPER_MIC_DEVICE_CHIP_AEC_150" not in (
        env_dir / "jasper.env"
    ).read_text()
