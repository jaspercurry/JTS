# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Snapclient return read by outputd on passive bond members (ADR-0220/0261).

A box may hold both ingress and return rings, so their identities differ.
"""

from jasper.multiroom.aux_ring import AuxRing
from jasper.ring_assets import RING_SHM_DIR

DAC_CONTENT_RING = AuxRing(
    pcm="jts_ring_dac_content",
    file=f"{RING_SHM_DIR}/dac-content.ring",
    conf_d="/etc/alsa/conf.d/63-jts-ring-dac-content.conf",
)
DAC_CONTENT_RING_PCM = DAC_CONTENT_RING.pcm
DAC_CONTENT_RING_FILE = DAC_CONTENT_RING.file
DAC_CONTENT_RING_CONF_D = DAC_CONTENT_RING.conf_d
DAC_CONTENT_RING_FORMAT = DAC_CONTENT_RING.format
DAC_CONTENT_RING_CHANNELS = DAC_CONTENT_RING.channels
DAC_CONTENT_RING_PERIOD_FRAMES = DAC_CONTENT_RING.period_frames
DAC_CONTENT_RING_SLOTS = DAC_CONTENT_RING.slots

# Both keys are cleared when the lane is disarmed; outputd reads empty as unset.
OUTPUTD_DAC_CONTENT_CHANNEL_ENV = "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL"
OUTPUTD_DAC_CONTENT_TRIM_ENV = "JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB"


def dac_content_ring_servable(outputd_period_frames: int | None) -> bool:
    """A mismatched or unresolved period cannot arm: outputd would park at 78."""
    return outputd_period_frames == DAC_CONTENT_RING_PERIOD_FRAMES
