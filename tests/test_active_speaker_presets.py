# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from jasper.active_speaker import ActiveSpeakerPreset
from jasper.cli.active_speaker import main as active_speaker_main


def _worked_example_path() -> Path:
    return Path(
        str(
            files("jasper.active_speaker").joinpath(
                "presets/bc_de250_dayton_e150he44_v1.json"
            )
        )
    )


def _epique_f110m_path() -> Path:
    return Path(
        str(
            files("jasper.active_speaker").joinpath(
                "presets/epique_e150he44_eminence_f110m8_safe_v1.json"
            )
        )
    )


def test_de250_e150he44_worked_example_preset_is_valid():
    preset_path = _worked_example_path()
    preset = ActiveSpeakerPreset.from_mapping(
        json.loads(preset_path.read_text(encoding="utf-8"))
    )

    assert preset.preset_id == "bc-de250-dayton-e150he44-v1"
    assert preset.way_count == 2
    assert preset.channel_map.layout == "mono"
    assert preset.crossover_regions[0].fc_hz == 1600
    assert preset.crossover_regions[0].delay_range_ms == (0.05, 0.3)


def test_epique_f110m_safe_bringup_preset_is_valid():
    preset_path = _epique_f110m_path()
    preset = ActiveSpeakerPreset.from_mapping(
        json.loads(preset_path.read_text(encoding="utf-8"))
    )

    assert preset.preset_id == "epique-e150he44-eminence-f110m8-safe-v1"
    assert preset.way_count == 2
    assert preset.channel_map.layout == "mono"
    assert preset.drivers["tweeter"].manufacturer == "Eminence"
    assert preset.drivers["tweeter"].model == "F110M-8"
    assert preset.crossover_regions[0].fc_hz == 2500
    assert preset.safety.initial_sweep_level_db_spl == 60
    assert preset.safety.max_commissioning_level_db_spl == 80


def test_worked_example_preset_generates_startup_template(tmp_path: Path):
    out = tmp_path / "active_startup.yml"

    code = active_speaker_main([
        "startup-template",
        str(_worked_example_path()),
        "--playback-device",
        "hw:ActiveDAC",
        "--output",
        str(out),
        "--no-check",
        "--json",
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "preset_id=bc-de250-dayton-e150he44-v1" in text
    assert "as_tweeter_protective_hp:" in text
    assert "freq: 3200.0000" in text
    assert "clip_limit: -12.0000" in text
