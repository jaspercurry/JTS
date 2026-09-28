# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Snapcast ring geometry outside the coupling and renderer registries.

Adding these rings to RING_PCM_DEVICES or RING_CONF_PCMS would select the
coupling's graph profile or renderer width policy. See ADR-0261.
"""

from dataclasses import dataclass

from jasper.dsp_control.fanin_coupling import RING_SLOT_FRAMES


@dataclass(frozen=True)
class AuxRing:
    pcm: str
    file: str
    conf_d: str
    # Direct PCMs have no plug conversion: both ends must match Snapcast.
    format: str = "S16_LE"
    channels: int = 2
    # The reader's period, so it never holds a partial slot (#3656). A slot of
    # period_frames x channels x 2 B must fit JTS_RING_MAX_SLOT_BYTES (65536).
    period_frames: int = RING_SLOT_FRAMES
    slots: int = 16  # Ioplug ceiling; shallower rings cannot reach the rate target.
