# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker preset loading."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset


DEFAULT_PRESET_RESOURCE = "presets/epique_e150he44_eminence_f110m8_safe_v1.json"


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
