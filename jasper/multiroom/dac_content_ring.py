# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The DAC-content return transport's identity — one name, one wire, one owner.

A bonded DUMB member plays the bond's program by taking it back out of the sync
engine: that box's snapclient writes the bond's shared stereo, and
``jasper-outputd`` reads it one DAC period at a time as its SOLE content source,
so the member is sample-locked to the rest of the bond. EITHER ROLE — a passive
leader takes its own program back the same way a follower takes the leader's;
what decides it is the box being a passive single-DAC member, not which end of
the bond it is. This module owns what that transport IS — its ALSA PCM name, its
ring file, the conf.d block that declares it, and the wire both ends have to
agree on.

**Its writer is** ``jasper.multiroom.reconcile._assemble_args`` (the member's
snapclient ``--soundcard``) **and its arm is**
``jasper.multiroom.reconcile.outputd_grouping_env`` (the bare
:data:`DAC_CONTENT_LANE_ENV` marker). Why the marker is served, why the legacy
FIFO spelling still parks, and what the two env layers have to agree on:
:doc:`ADR-0219 <../docs/adr/0219-the-dac-content-marker-is-served-and-its-contradiction-parks>`.

**A sibling of** :mod:`jasper.multiroom.grouping_ring`, **not a reuse of it.**
The two carry the same wire at the same geometry, but they are opposite
directions of the same bond — ingress vs return — and a box can hold both at
once, so one ring cannot be both.

**Deliberately NOT a member of the ring platform's registries**, for the reason
:mod:`jasper.multiroom.grouping_ring` states at length:
:data:`jasper.fanin_coupling.RING_PCM_DEVICES` decides a graph is a ring graph
and hands it the coupling's chunk/target/queuelimit profile, and
:data:`jasper.ring_assets.RING_CONF_PCMS` is what
``jasper.ring_assets.render_ring_conf_wire`` walks. This ring is neither the
coupling's wire nor a renderer lane, so it carries its own conf.d file and
joins neither registry.
"""

from __future__ import annotations

from jasper.fanin_coupling import RING_SLOT_FRAMES
from jasper.ring_assets import ring_writer_lock_path

#: The ALSA PCM name ``deploy/alsa/conf.d/63-jts-ring-dac-content.conf``
#: defines — one string for the member's snapclient ``--soundcard`` and for
#: outputd's reader.
DAC_CONTENT_RING_PCM = "jts_ring_dac_content"

#: The SHM ring file that PCM's ``path`` names, under the shared
#: ``/dev/shm/jts-ring`` directory the ring platform's tmpfiles entry creates.
#: One spelling for both roles — unlike the coupling's content hop, this ring
#: does not vary with which end of the bond the box is.
DAC_CONTENT_RING_FILE = "/dev/shm/jts-ring/dac-content.ring"

#: Where the installer places that conf.d block
#: (``deploy/lib/install/ring-platform.sh``'s ``install_jts_ring_conf_assets``).
#: Sibling of :data:`jasper.multiroom.grouping_ring.GROUPING_RING_CONF_D`.
DAC_CONTENT_RING_CONF_D = "/etc/alsa/conf.d/63-jts-ring-dac-content.conf"

#: The outputd env key that arms this ring as a box's dac-content source. A
#: BARE marker, never a path: outputd derives the file from its own
#: ``DEFAULT_DAC_CONTENT_RING_PATH``, pinned equal to
#: :data:`DAC_CONTENT_RING_FILE`, so no env can name this ring and the two ends
#: have no second spelling to disagree on. Truthiness is outputd's ``env_bool``
#: accept-set (:data:`jasper.fanin_coupling.OUTPUTD_ENV_BOOL_TRUE`) — a reader
#: that tests mere PRESENCE would call ``=0`` armed.
DAC_CONTENT_LANE_ENV = "JASPER_OUTPUTD_DAC_CONTENT_LANE"

#: The wire, spelled in the conf.d block rather than inherited from the ioplug's
#: compiled defaults. Both ends already pin it independently: snapclient decodes
#: to the snapserver-pinned ``sampleformat=48000:16:2``
#: (:func:`jasper.multiroom.reconcile.snapserver_argv`), and outputd's
#: dac-content lane is "S16 by contract" — its ``period_bytes`` is
#: ``period_frames * 2 channels * 2 bytes`` (``rust/jasper-outputd/src/
#: dac_content.rs``). This block is opened directly with no ``plug`` PCM in
#: front of it, and the ioplug's hw_params is single-valued in every dimension
#: but access, so a widening of one side alone is a negotiation failure at open
#: rather than a quiet conversion.
DAC_CONTENT_RING_FORMAT = "S16_LE"
DAC_CONTENT_RING_CHANNELS = 2

#: Slot geometry. The slot is the READER's period, and every DAC profile that
#: declares a latency floor runs outputd at
#: :data:`~jasper.fanin_coupling.RING_SLOT_FRAMES`
#: (:mod:`jasper.audio_hardware.dac`), so one slot is one DAC period and the
#: reader never holds a partial slot (#3656). The floorless HiFiBerry DAC8x
#: Studio runs outputd's packaged 1024 and is the one profile that cannot arm
#: the lane; outputd's config guard names that at startup. 128 frames x 2 ch
#: x 2 B = 512 B per slot, inside the ioplug's ``JTS_RING_MAX_SLOT_BYTES``
#: (65536).
DAC_CONTENT_RING_PERIOD_FRAMES = RING_SLOT_FRAMES

#: Depth at the ioplug's slot CEILING (``JTS_RING_MAX_SLOTS``), the same
#: constraint the grouping ring took knowingly: depth is not tunable upward from
#: a conf.d edit. 16 x 128 frames = 2048 frames = 43 ms at 48 kHz.
DAC_CONTENT_RING_SLOTS = 16

#: The exclusive ``flock`` a C ioplug WRITER holds for the life of its mapping,
#: which is what makes a second writer's open fail loudly with ``-EBUSY``.
#: DERIVED by calling the ring platform's own constructor rather than spelled
#: again here — one suffix rule, one owner, already pinned against the C header
#: by ``tests/test_ring_slot_ceiling_pin.py``.
DAC_CONTENT_RING_WRITER_LOCK = ring_writer_lock_path(DAC_CONTENT_RING_FILE)


def dac_content_ring_servable(outputd_period_frames: int | None) -> bool:
    """Can a box running ``outputd_period_frames`` read this ring? PURE.

    One slot is one outputd PERIOD, and outputd bails EX_CONFIG on the
    mismatched pair (``rust/jasper-outputd/src/config.rs``) under the unit's
    ``RestartPreventExitStatus=78`` — a parked daemon and a silent speaker. So
    the writer must not arm the lane unless the two already agree.

    ``None`` (an unresolved period) is not servable: an indeterminate period
    must never be assumed to match.
    """
    return outputd_period_frames == DAC_CONTENT_RING_PERIOD_FRAMES
