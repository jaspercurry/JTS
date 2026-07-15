# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Install migration coverage for retired USB/grouping generated state."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "deploy/lib/install/env-migrations.sh"


def _run(state_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{MIGRATIONS}"; migrate_retired_source_state',
        ],
        env={"STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_retired_generated_files_are_removed(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "usbsink.env").write_text(
        "# generated route\n"
        "JASPER_USBSINK_AUDIO_STANDBY=1\n"
        "JASPER_USBSINK_BLOCK_FRAMES=256\n"
        "JASPER_USBSINK_RING_PERIODS=3\n"
        "JASPER_USBSINK_LATENCY=low\n"
        "JASPER_USBSINK_OUTPUT_MODE=aloop\n",
        encoding="utf-8",
    )
    (state_dir / "grouping-follower-status.json").write_text(
        '{"status":"legacy"}\n', encoding="utf-8"
    )

    result = _run(state_dir)

    assert result.returncode == 0, result.stderr
    assert not (state_dir / "usbsink.env").exists()
    assert not (state_dir / "grouping-follower-status.json").exists()


def test_retired_symlinks_are_unlinked_without_touching_targets(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    target = tmp_path / "operator-data"
    target.write_text("do not touch\n", encoding="utf-8")
    (state_dir / "usbsink.env").symlink_to(target)
    (state_dir / "grouping-follower-status.json").symlink_to(target)

    result = _run(state_dir)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "do not touch\n"
    assert not (state_dir / "usbsink.env").exists()
    assert not (state_dir / "grouping-follower-status.json").exists()


def test_retired_directories_are_refused(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "usbsink.env").mkdir()

    result = _run(state_dir)

    assert result.returncode != 0
    assert "refusing retired-state directory" in result.stderr
    assert (state_dir / "usbsink.env").is_dir()
