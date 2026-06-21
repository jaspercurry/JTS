# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from jasper.mics import xvf3800


def _write_card(root: Path, card: str, channels: int) -> None:
    card_dir = root / card
    card_dir.mkdir(parents=True)
    (card_dir / "stream0").write_text(
        f"Playback:\n  Channels: 2\nCapture:\n  Channels: {channels}\n"
    )


def test_square_6ch_variant_has_legacy_150_210_beam_plan(tmp_path: Path) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "Array", 6)

    profile = xvf3800.detect_runtime_profile(asound_root=asound)

    assert profile.variant_id == "xvf3800_legacy_square_6ch"
    assert profile.geometry == "square"
    assert profile.chip_beam_plan_id == "xvf_square_fixed_150_210"
    assert profile.chip_aec_supported is True
    assert [leg.token for leg in profile.chip_beam_plan.legs] == [
        "chip_aec_150",
        "chip_aec_210",
    ]


def test_flex_linear_6ch_variant_has_no_production_beam_plan(
    tmp_path: Path,
) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "L16K6Ch", 6)

    profile = xvf3800.detect_runtime_profile(asound_root=asound)

    assert profile.variant_id == "xvf3800_flex_linear_6ch"
    assert profile.geometry == "linear"
    assert profile.chip_beam_plan is None
    assert profile.chip_aec_supported is False
    assert profile.recommended_profile == "xvf_software_aec3"
    assert "no validated production chip beam plan" in profile.reason


def test_cli_env_and_state_share_resolved_profile(tmp_path: Path) -> None:
    asound = tmp_path / "asound"
    state_path = tmp_path / "state" / "xvf3800.json"
    _write_card(asound, "L16K6Ch", 6)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jasper.cli.xvf_profile",
            "--asound-root",
            str(asound),
            "--state-path",
            str(state_path),
            "--write-state",
            "--env",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert "JASPER_XVF_VARIANT=xvf3800_flex_linear_6ch" in result.stdout
    assert "JASPER_XVF_GEOMETRY=linear" in result.stdout
    assert "JASPER_XVF_CHIP_AEC_SUPPORTED=0" in result.stdout
    state = json.loads(state_path.read_text())
    assert state["variant_id"] == "xvf3800_flex_linear_6ch"
    assert state["chip_beam_plan"] is None
