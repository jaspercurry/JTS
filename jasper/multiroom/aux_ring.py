# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Snapcast ring geometry outside the coupling and renderer registries.

Adding these rings to RING_PCM_DEVICES or RING_CONF_PCMS would select the
coupling's graph profile or renderer width policy. See ADR-0261.
"""

from dataclasses import dataclass

from jasper.fanin_coupling import RING_SLOT_FRAMES


@dataclass(frozen=True)
class AuxRing:
    pcm: str
    file: str
    conf_d: str
    # Direct PCMs have no plug conversion: both ends must match Snapcast.
    format: str = "S16_LE"
    channels: int = 2
    period_frames: int = RING_SLOT_FRAMES
    slots: int = 16  # Ioplug ceiling; shallower rings cannot reach the rate target.
