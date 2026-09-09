# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
import shlex
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
from jasper.aec.bridge_config import (
    OUTPUTD_REF_UDP_HOST_ENV,
    OUTPUTD_REF_UDP_PORT_ENV,
    REF_SOURCE_ENV,
)
from jasper.aec.bridge_engines import (
    CORPUS_USB_DTLN_ENABLED_ENV,
    DTLN_ENABLED_ENV,
)
from jasper.aec.bridge_telemetry import BRIDGE_STATS_PATH_ENV
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


@pytest.mark.parametrize("flag", [
    xvf3800.CHIP_AEC_ENABLED_ENV, xvf3800.CORPUS_CHIP_AEC_ENABLED_ENV,
])
@pytest.mark.parametrize("variant", xvf3800.FIRMWARE_VARIANTS)
def test_known_variant_without_beam_plan_cannot_inherit_square_beams(flag, variant):
    env = {
        "JASPER_XVF_VARIANT": variant.variant_id,
        "JASPER_XVF_GEOMETRY": variant.geometry,
        flag: "1",
    }
    assert xvf3800.chip_beam_plan_from_env(env) == xvf3800.chip_beam_plan_for_variant(variant)
    assert xvf3800.chip_beam_plan_from_env({flag: "1"}) == xvf3800.SQUARE_FIXED_150_210_PLAN
    env[flag] = "0"
    assert xvf3800.chip_beam_plan_from_env(env) is None


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


@pytest.fixture(scope="module")
def absent_mic_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """``--env`` output with no XVF card present, parsed the way bash evals it."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jasper.cli.xvf_profile",
            "--asound-root",
            str(tmp_path_factory.mktemp("empty-asound")),
            "--env",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = shlex.split(line)[0].partition("=")
        values[key] = value
    return values


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (
            "JASPER_XVF_SUPPORTED_ALSA_CARDS",
            ",".join(xvf3800.ALSA_CARD_NAMES),
        ),
        (
            "JASPER_XVF_RECOMMENDED_CHANNELS",
            str(xvf3800.RECOMMENDED_CAPTURE_CHANNELS),
        ),
        ("JASPER_XVF_MIXER_CAPTURE_SWITCH", xvf3800.MIXER_CAPTURE_SWITCH),
        ("JASPER_XVF_MIXER_CAPTURE_VOLUME", xvf3800.MIXER_CAPTURE_VOLUME),
        ("JASPER_XVF_MIXER_VOLUME_MAX", str(xvf3800.MIXER_VOLUME_MAX)),
    ],
)
def test_registry_constant_reaches_bash_even_with_no_mic(
    absent_mic_env: dict[str, str], key: str, value: str
) -> None:
    # The reconciler keeps no copy of these (ADR-0235), so they have to be on
    # the emitter's output on every path, including the absent one — that is
    # the pass where it still has to name candidates and a fallback mic.
    assert absent_mic_env[key] == value


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


@pytest.mark.parametrize(
    ("variant", "expected_target_id", "publishes_build_provenance"),
    [
        pytest.param(
            "VARIANT_6CH", "legacy_square_6ch", True, id="legacy_square_flashed",
        ),
        pytest.param(
            "VARIANT_2CH", "legacy_square_6ch", True, id="legacy_square_stock",
        ),
        pytest.param(
            "VARIANT_FLEX_LINEAR_6CH", "flex_linear_6ch", False, id="flex_linear",
        ),
        pytest.param(
            "VARIANT_FLEX_CIRCULAR_6CH",
            "flex_circular_6ch",
            False,
            id="flex_circular",
        ),
    ],
)
def test_recording_provenance_names_the_detected_board_s_firmware(
    monkeypatch, variant, expected_target_id, publishes_build_provenance,
):
    """A Flex recording used to carry the legacy square blob's name, hash date
    and build-repo hash beside a `geometry: linear` that contradicted them
    (#4361). Provenance now comes off the detected board's own registry row,
    and a family that publishes no build provenance omits those keys rather
    than borrowing another family's.
    """
    from jasper.mics import xvf3800
    from jasper.wake_corpus import runtime_probe

    board = getattr(xvf3800, variant)
    monkeypatch.setattr(
        runtime_probe.xvf3800,
        "detect_runtime_profile",
        lambda **_kwargs: xvf3800.RuntimeProfile(
            present=True,
            variant=board,
            alsa_card_name=board.alsa_card_name,
            capture_channels=board.capture_channels,
            chip_beam_plan=xvf3800.chip_beam_plan_for_variant(board),
            reason="test",
        ),
    )

    _probe, identity = runtime_probe.mic_probe_and_identity()

    target = xvf3800.FIRMWARE_UPDATE_TARGETS_BY_ID[expected_target_id]
    to_variant = next(
        v for v in xvf3800.FIRMWARE_VARIANTS if v.variant_id == target.to_variant_id
    )
    firmware = identity["recommended_firmware"]
    assert firmware["blob"] == target.filename
    assert firmware["sha256"] == target.sha256
    assert firmware["capture_channels"] == target.expected_capture_channels
    assert firmware["raw_mic_indices"] == list(to_variant.raw_mic_indices)
    assert identity["geometry"] == target.geometry
    if publishes_build_provenance:
        assert firmware["known_good_as_of"] == target.known_good_as_of
        assert firmware["build_repo_hash"] == target.build_repo_hash
    else:
        assert "known_good_as_of" not in firmware
        assert "build_repo_hash" not in firmware


def test_recommended_firmware_omits_keys_for_unrecognized_board(monkeypatch):
    """A board xvf3800 cannot match to any registry row (unknown BLD_MSG,
    non-standard channel count) has no safe firmware to describe — the
    per-board keys are absent rather than borrowed from the legacy square
    build's (#4361)."""
    from jasper.mics import xvf3800
    from jasper.wake_corpus import runtime_probe

    monkeypatch.setattr(
        runtime_probe.xvf3800,
        "detect_runtime_profile",
        lambda **_kwargs: xvf3800.RuntimeProfile(
            present=True,
            variant=None,
            alsa_card_name=xvf3800.ALSA_CARD_NAME,
            capture_channels=4,
            chip_beam_plan=None,
            reason="test",
        ),
    )

    _probe, identity = runtime_probe.mic_probe_and_identity()

    firmware = identity["recommended_firmware"]
    assert "blob" not in firmware
    assert "sha256" not in firmware
    assert "capture_channels" not in firmware
    assert "raw_mic_indices" not in firmware
    assert "known_good_as_of" not in firmware
    assert "build_repo_hash" not in firmware
