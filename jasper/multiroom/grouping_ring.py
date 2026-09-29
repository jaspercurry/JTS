# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Snapclient ingress read by the bonded endpoint's CamillaDSP (ADR-0261)."""

from jasper.dsp_control.fanin_coupling import RING_CAMILLA_CHUNKSIZE
from jasper.multiroom.aux_ring import AuxRing
from jasper.audio_control.ring_assets import RING_SHM_DIR

GROUPING_RING = AuxRing(
    pcm="jts_ring_grouping",
    file=f"{RING_SHM_DIR}/grouping.ring",
    conf_d="/etc/alsa/conf.d/62-jts-ring-grouping.conf",
    period_frames=RING_CAMILLA_CHUNKSIZE,
)
GROUPING_RING_PCM = GROUPING_RING.pcm
GROUPING_RING_FILE = GROUPING_RING.file
GROUPING_RING_CONF_D = GROUPING_RING.conf_d
GROUPING_RING_FORMAT = GROUPING_RING.format
GROUPING_RING_CHANNELS = GROUPING_RING.channels
GROUPING_RING_PERIOD_FRAMES = GROUPING_RING.period_frames
GROUPING_RING_SLOTS = GROUPING_RING.slots
