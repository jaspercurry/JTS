# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared tone-artifact vocabulary, timing bounds, and preset loading."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset


TONE_PLAN_KIND = "jts_active_speaker_tone_plan"
DEFAULT_PRESET_RESOURCE = "presets/epique_e150he44_eminence_f110m8_safe_v1.json"
DEFAULT_TONE_DURATION_MS = 300
MIN_TONE_DURATION_MS = 100
MAX_TONE_DURATION_MS = 500
DEFAULT_TONE_RAMP_MS = 20


def load_active_speaker_preset(
    preset_path: str | Path | None = None,
) -> ActiveSpeakerPreset:
    """Load a preset from an explicit path or the bundled worked example."""

    if preset_path:
        try:
            raw = json.loads(Path(preset_path).read_text(encoding="utf-8"))
        except OSError as e:
            raise ActiveSpeakerConfigError(f"could not read active preset: {e}") from e
        except json.JSONDecodeError as e:
            raise ActiveSpeakerConfigError(f"active preset is not valid JSON: {e}") from e
    else:
        raw = json.loads(
            files("jasper.active_speaker")
            .joinpath(DEFAULT_PRESET_RESOURCE)
            .read_text(encoding="utf-8")
        )
    return ActiveSpeakerPreset.from_mapping(raw)
