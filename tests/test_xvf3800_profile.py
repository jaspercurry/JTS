# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from jasper.aec_sweep import (
    AGC1_ENABLED_ENV,
    AGC1_MAX_GAIN_DB_ENV,
    AGC1_TARGET_DBFS_ENV,
    NS_ENABLED_ENV,
    NS_LEVEL_ENV,
)
from jasper.audio_profile_state import AEC_MODE_ENV, AEC_MODE_FILE_ENV
from jasper.cli.aec_bridge_config import (
    OUTPUTD_REF_UDP_HOST_ENV,
    OUTPUTD_REF_UDP_PORT_ENV,
    REF_SOURCE_ENV,
)
from jasper.cli.aec_bridge_engines import (
    CORPUS_USB_DTLN_ENABLED_ENV,
    DTLN_ENABLED_ENV,
)
from jasper.cli.aec_bridge_telemetry import BRIDGE_STATS_PATH_ENV
from jasper.mics import xvf3800

RECONCILER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "jasper-aec-reconcile"


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
    assert profile.recommended_profile == "xvf_chip_aec"
    assert "no validated production chip beam plan" in profile.reason


def test_fixed_profile_and_native_reference_are_single_source_of_truth() -> None:
    assert xvf3800.CHIP_AEC_SYS_DELAY_DEFAULT == -37
    profile = xvf3800.chip_aec_profile_commands(
        xvf3800.SQUARE_FIXED_150_210_PLAN,
        sys_delay=-38,
    )

    assert profile[0] == ("SHF_BYPASS", [1])
    assert profile[-1] == ("SHF_BYPASS", [0])
    assert dict(profile[1:-1])["AUDIO_MGR_SYS_DELAY"] == [-38]
    assert dict(profile[1:-1])["AUDIO_MGR_OP_L"] == [7, 0]
    assert dict(profile[1:-1])["AUDIO_MGR_OP_R"] == [7, 1]
    assert (
        xvf3800.CHIP_AEC_REFERENCE_PCM_ACCESS,
        xvf3800.CHIP_AEC_REFERENCE_SAMPLE_RATE_HZ,
        xvf3800.CHIP_AEC_REFERENCE_CHANNELS,
        xvf3800.CHIP_AEC_REFERENCE_SAMPLE_FORMAT,
        xvf3800.CHIP_AEC_REFERENCE_PERIOD_FRAMES,
        xvf3800.CHIP_AEC_REFERENCE_BUFFER_FRAMES,
    ) == ("hw", 16_000, 2, "S16_LE", 128, 256)


def test_2ch_square_firmware_has_safe_update_manifest(tmp_path: Path) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "Array", 2)

    profile = xvf3800.detect_runtime_profile(asound_root=asound)
    status = xvf3800.firmware_update_status(profile)

    assert profile.variant_id == "xvf3800_legacy_square_2ch"
    assert status["state"] == "update_required"
    assert status["required"] is True
    assert status["action"]["enabled"] is True
    assert status["target"]["id"] == "legacy_square_6ch"
    assert status["target"]["dfu_alt_setting"] == 1
    assert status["target"]["sha256"] == xvf3800.FIRMWARE_KNOWN_GOOD_SHA256
    assert status["target"]["expected_size_bytes"] == (
        xvf3800.FIRMWARE_KNOWN_GOOD_SIZE_BYTES
    )


def test_current_6ch_firmware_does_not_offer_update(tmp_path: Path) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "Array", 6)

    status = xvf3800.firmware_update_status(
        xvf3800.detect_runtime_profile(asound_root=asound),
    )

    assert status["state"] == "current"
    assert status["required"] is False
    assert status["action"]["enabled"] is False


def test_failed_safe_update_stays_visible_and_retryable(tmp_path: Path) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "Array", 2)

    status = xvf3800.firmware_update_status(
        xvf3800.detect_runtime_profile(asound_root=asound),
        last_update={"state": "failed", "error": "hash mismatch"},
    )

    assert status["state"] == "failed"
    assert status["required"] is True
    assert status["detail"] == "hash mismatch"
    assert status["action"]["enabled"] is True


def test_flex_circular_geometry_has_its_own_safe_update_manifest(
    tmp_path: Path,
) -> None:
    asound = tmp_path / "asound"
    _write_card(asound, "C16K2Ch", 2)

    profile = xvf3800.detect_runtime_profile(asound_root=asound)
    status = xvf3800.firmware_update_status(profile)

    assert profile.variant_id == "xvf3800_flex_circular_2ch"
    assert profile.geometry == "circular"
    assert status["state"] == "update_required"
    assert status["action"]["enabled"] is True
    assert status["target"]["id"] == "flex_circular_6ch"
    assert status["target"]["sha256"] == (
        xvf3800.FIRMWARE_FLEX_CIRCULAR_KNOWN_GOOD_SHA256
    )
    assert status["target"]["expected_size_bytes"] == (
        xvf3800.FIRMWARE_FLEX_KNOWN_GOOD_SIZE_BYTES
    )


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


def _reconciler_written_aec_keys() -> frozenset[str]:
    return frozenset(
        re.findall(
            r'^\s*set_env_var "\$ENV_FILE" (JASPER_AEC_[A-Z0-9_]+)',
            RECONCILER.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )


@pytest.mark.parametrize(
    "constant",
    [
        xvf3800.AEC_MIC_DEVICE_ENV,
        xvf3800.CHIP_AEC_ENABLED_ENV,
        DTLN_ENABLED_ENV,
        REF_SOURCE_ENV,
        OUTPUTD_REF_UDP_HOST_ENV,
        OUTPUTD_REF_UDP_PORT_ENV,
    ],
)
def test_env_key_constant_is_a_key_the_reconciler_writes(constant: str) -> None:
    # A rename on either side of the bash/Python edge fails here.
    assert constant in _reconciler_written_aec_keys()


@pytest.mark.parametrize(
    ("constant", "literal"),
    [
        (xvf3800.CORPUS_CHIP_AEC_ENABLED_ENV, "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED"),
        (xvf3800.CHIP_AEC_PRIMARY_LEG_ENV, "JASPER_AEC_CHIP_AEC_PRIMARY_LEG"),
        (BRIDGE_STATS_PATH_ENV, "JASPER_AEC_BRIDGE_STATS_PATH"),
        (CORPUS_USB_DTLN_ENABLED_ENV, "JASPER_AEC_CORPUS_USB_DTLN_ENABLED"),
        (NS_ENABLED_ENV, "JASPER_AEC_NS_ENABLED"),
        (NS_LEVEL_ENV, "JASPER_AEC_NS_LEVEL"),
        (AGC1_ENABLED_ENV, "JASPER_AEC_AGC1_ENABLED"),
        (AGC1_TARGET_DBFS_ENV, "JASPER_AEC_AGC1_TARGET_DBFS"),
        (AGC1_MAX_GAIN_DB_ENV, "JASPER_AEC_AGC1_MAX_GAIN_DB"),
        (AEC_MODE_ENV, "JASPER_AEC_MODE"),
        (AEC_MODE_FILE_ENV, "JASPER_AEC_MODE_FILE"),
    ],
)
def test_env_key_constant_not_written_by_reconciler_keeps_its_name(
    constant: str, literal: str,
) -> None:
    # These keys are set by callers other than the reconciler (wake_corpus,
    # the /aec wizard, engine tuning). Not in _reconciler_written_aec_keys(),
    # so pinned directly: a rename here is still observable behavior for
    # every reader of /etc/jasper/jasper.env or /var/lib/jasper/aec_mode.env.
    assert constant == literal
    assert constant not in _reconciler_written_aec_keys()
