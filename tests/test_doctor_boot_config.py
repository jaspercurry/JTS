# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor: the saved I2S DAC's overlay line survives a reboot (#2575),
and a managed I2S HAT block does not outlive the HAT it was written for
(#4027 R3)."""

from __future__ import annotations

import pytest

from jasper.audio_hardware.i2s_hat import I2S_HAT_BLOCK_BEGIN, I2S_HAT_BLOCK_END
from jasper.cli.doctor.boot_config import (
    REASON_BOOT_CONFIG_UNREADABLE,
    REASON_I2S_HAT_BLOCK_MALFORMED,
    REASON_ORPHAN_MANAGED_I2S_BLOCK,
    REASON_OVERLAY_MISSING,
    REASON_OVERLAY_PRESENT,
    REASON_OVERLAY_PRESENT_SCOPED,
    REASON_SKIPPED,
    check_i2s_dac_overlay_persists,
    check_i2s_hat_block_orphaned,
)
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    save_output_topology,
)

_MANAGED_I2S_BLOCK = (
    f"[all]\n{I2S_HAT_BLOCK_BEGIN}\n"
    "dtoverlay=hifiberry-dac8x\n"
    f"{I2S_HAT_BLOCK_END}\n"
)


def _isolate(monkeypatch, tmp_path):
    """No saved topology unless a test writes one."""
    monkeypatch.setenv(
        "JASPER_OUTPUT_TOPOLOGY_PATH", str(tmp_path / "output_topology.json")
    )


def _save_topology(tmp_path, *, device_id: str, physical_output_count: int) -> None:
    topology = OutputTopology.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "bench",
            "name": "Bench speaker",
            "status": "draft",
            "hardware": {
                "device_id": device_id,
                "physical_output_count": physical_output_count,
            },
            "speaker_groups": [],
        }
    )
    save_output_topology(topology, path=tmp_path / "output_topology.json")


@pytest.mark.parametrize(
    "boot_config_text,expected_status,expected_reason",
    [
        pytest.param(
            "dtoverlay=hifiberry-dac8x\n", "ok", REASON_OVERLAY_PRESENT,
            id="overlay_present",
        ),
        pytest.param(
            "[pi5]\ndtoverlay=hifiberry-dac8x\n",
            "ok",
            REASON_OVERLAY_PRESENT_SCOPED,
            id="overlay_present_scoped",
        ),
        pytest.param("", "fail", REASON_OVERLAY_MISSING, id="overlay_missing"),
    ],
)
def test_i2s_dac_checks_the_boot_config_overlay(
    monkeypatch, tmp_path, boot_config_text, expected_status, expected_reason
):
    _isolate(monkeypatch, tmp_path)
    _save_topology(tmp_path, device_id="hifiberry_dac8x", physical_output_count=8)
    boot_config = tmp_path / "config.txt"
    boot_config.write_text(boot_config_text, encoding="utf-8")
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(boot_config))

    result = check_i2s_dac_overlay_persists()

    assert result.status == expected_status
    assert result.reason == expected_reason


def test_i2s_dac_check_warns_when_boot_config_is_unreadable(monkeypatch, tmp_path):
    """A missing/unreadable config.txt is an evidence gap, not confirmation the
    overlay line is gone — must not FAIL with "add the line" (#2575 review)."""
    _isolate(monkeypatch, tmp_path)
    _save_topology(tmp_path, device_id="hifiberry_dac8x", physical_output_count=8)
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(tmp_path / "absent-config.txt"))

    result = check_i2s_dac_overlay_persists()

    assert result.status == "warn"
    assert result.reason == REASON_BOOT_CONFIG_UNREADABLE


def test_i2s_dac_check_skips_a_usb_profile(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _save_topology(tmp_path, device_id="apple_usb_c_dongle", physical_output_count=2)
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(tmp_path / "config.txt"))

    result = check_i2s_dac_overlay_persists()

    assert result.status == "ok"
    assert result.reason == REASON_SKIPPED


def test_i2s_dac_check_skips_an_unregistered_device_id(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _save_topology(tmp_path, device_id="made_up_dac", physical_output_count=2)
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(tmp_path / "config.txt"))

    result = check_i2s_dac_overlay_persists()

    assert result.status == "ok"
    assert result.reason == REASON_SKIPPED


def test_i2s_dac_check_skips_with_no_saved_topology(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(tmp_path / "config.txt"))

    result = check_i2s_dac_overlay_persists()

    assert result.status == "ok"
    assert result.reason == REASON_SKIPPED


# ---------------------------------- orphaned managed I2S HAT block ----------


@pytest.mark.parametrize(
    "intent_file_present,expected_status,expected_reason",
    [
        pytest.param(
            False, "warn", REASON_ORPHAN_MANAGED_I2S_BLOCK, id="not_managed",
        ),
        pytest.param(True, "ok", None, id="managed_via_intent_file"),
    ],
)
def test_i2s_hat_block_orphan_check_reflects_whether_the_hat_is_managed(
    monkeypatch, tmp_path, intent_file_present, expected_status, expected_reason
):
    """A managed block with no HAT to justify it WARNs (i2s_hat_apply never
    touches an unmanaged block, #4027 R3); an intent file at
    ``JASPER_I2S_HAT_INTENT_FILE`` -- the real ``i2s_hat_managed``, no
    monkeypatch -- is what clears it, same env var the reconciler shell
    resolves and hands to its own CLI."""
    boot_config = tmp_path / "config.txt"
    boot_config.write_text(_MANAGED_I2S_BLOCK, encoding="utf-8")
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(boot_config))
    intent_file = tmp_path / "i2s_hat.env"
    if intent_file_present:
        intent_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("JASPER_I2S_HAT_INTENT_FILE", str(intent_file))

    result = check_i2s_hat_block_orphaned()

    assert result.status == expected_status
    if expected_reason is None:
        assert result.reason != REASON_ORPHAN_MANAGED_I2S_BLOCK
    else:
        assert result.reason == expected_reason


def test_i2s_hat_block_orphan_check_warns_on_a_malformed_block(monkeypatch, tmp_path):
    """A mangled BEGIN marker with a live ``dtoverlay=`` line still inside it
    is neither a clean absence nor a parseable block -- the canonical parser
    raises, and this must surface as a warn, not a false "ok" (#4027 R3)."""
    boot_config = tmp_path / "config.txt"
    boot_config.write_text(
        f"## {I2S_HAT_BLOCK_BEGIN[2:]}\n"
        "dtoverlay=hifiberry-dac8x\n"
        f"{I2S_HAT_BLOCK_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(boot_config))

    result = check_i2s_hat_block_orphaned()

    assert result.status == "warn"
    assert result.reason == REASON_I2S_HAT_BLOCK_MALFORMED


def test_i2s_hat_block_orphan_check_skips_the_hat_read_with_no_block(
    monkeypatch, tmp_path
):
    boot_config = tmp_path / "config.txt"
    boot_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(boot_config))
    monkeypatch.setattr(
        "jasper.cli.doctor.boot_config.i2s_hat_managed",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe the HAT")),
    )

    result = check_i2s_hat_block_orphaned()

    assert result.status == "ok"
    assert result.reason != REASON_ORPHAN_MANAGED_I2S_BLOCK


def test_i2s_hat_block_orphan_check_warns_when_boot_config_is_unreadable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JTS_BOOT_CONFIG_FILE", str(tmp_path / "absent-config.txt"))

    result = check_i2s_hat_block_orphaned()

    assert result.status == "warn"
    assert result.reason == REASON_BOOT_CONFIG_UNREADABLE
