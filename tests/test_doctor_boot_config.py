# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor: the saved I2S DAC's overlay line survives a reboot (#2575)."""

from __future__ import annotations

import pytest

from jasper.cli.doctor.boot_config import (
    REASON_BOOT_CONFIG_UNREADABLE,
    REASON_OVERLAY_MISSING,
    REASON_OVERLAY_PRESENT,
    REASON_OVERLAY_PRESENT_SCOPED,
    REASON_SKIPPED,
    check_i2s_dac_overlay_persists,
)
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    save_output_topology,
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
