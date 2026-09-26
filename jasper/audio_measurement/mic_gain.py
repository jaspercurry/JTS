# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A measurement mic's capture controls, set to full scale and read back (#4865).

The calibration's Sens Factor is quoted at the maximum capture volume, so a
dBFS reading is absolute SPL only there: below it the commissioning stop reads
low and fires late.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from contextlib import closing
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


def set_full_scale_gain(card_id: str) -> dict[str, Any]:
    """Every capture volume on the card at its maximum, its capture switch on.

    ``verified`` holds only when the card has a capture volume and each one
    reads back at its maximum with its switch on. The read-back opens a fresh
    handle: pyalsaaudio drops a failed write's error, and the writing handle's
    cache keeps the value it asked for.
    """
    device = f"hw:CARD={card_id}"
    controls: list[dict[str, Any]] = []
    try:
        import alsaaudio  # lazy: ALSA-only dependency, capture path only
    except ImportError as exc:
        return _unverified(card_id, controls, exc)
    capture, raw = alsaaudio.PCM_CAPTURE, alsaaudio.VOLUME_UNITS_RAW
    try:
        seen: Counter[str] = Counter()
        for name in alsaaudio.mixers(device=device):
            index = seen[name]
            seen[name] += 1
            with closing(alsaaudio.Mixer(control=name, id=index, device=device)) as mixer:
                if "Capture Volume" not in mixer.volumecap():
                    continue
                switched = "Capture Mute" in mixer.switchcap()
                top = mixer.getrange(pcmtype=capture, units=raw)[1]
                found = mixer.getvolume(pcmtype=capture, units=raw)
                if any(value != top for value in found) or (switched and not all(mixer.getrec())):
                    mixer.setvolume(top, pcmtype=capture, units=raw)
                    if switched:
                        mixer.setrec(1)
                    log_event(logger, "audio_measurement.mic_capture_gain_raised",
                              card=card_id, control=name, index=index, found=found, max=top)
            with closing(alsaaudio.Mixer(control=name, id=index, device=device)) as fresh:
                controls.append({
                    "control": name, "index": index,
                    "value": fresh.getvolume(pcmtype=capture, units=raw),
                    "max": fresh.getrange(pcmtype=capture, units=raw)[1],
                    # ALSA reports dB in hundredths.
                    "db": [value / 100 for value in fresh.getvolume(pcmtype=capture, units=alsaaudio.VOLUME_UNITS_DB)],
                    "switch": [bool(value) for value in fresh.getrec()] if switched else [],
                })
    except (OSError, alsaaudio.ALSAAudioError) as exc:
        return _unverified(card_id, controls, exc)
    if controls and all(
        control["value"] and all(value == control["max"] for value in control["value"])
        and all(control["switch"]) for control in controls
    ):
        return {"verified": True, "controls": controls}
    return _unverified(card_id, controls, None)


def _unverified(card_id: str, controls: list[dict[str, Any]], error: BaseException | None) -> dict[str, Any]:
    log_event(logger, "audio_measurement.mic_capture_gain_unverified", level=logging.WARNING,
              card=card_id, controls=json.dumps(controls, separators=(",", ":")), error=error)
    return {"verified": False, "controls": controls}
