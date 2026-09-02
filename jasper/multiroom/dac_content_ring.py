# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The DAC-content return transport's identity — one name, one wire, one owner.

A grouping LEADER plays what its followers play by taking its own program back
out of the sync engine: the leader's localhost snapclient writes the bond's
shared stereo, and ``jasper-outputd`` reads it one DAC period at a time so the
leader is sample-locked to its members. This module owns what that transport
IS — its ALSA PCM name, its ring file, the conf.d block that declares it, and
the wire both ends have to agree on.

**Nothing opens it yet.** The lane is parked (:doc:`ADR-0178
<../docs/adr/0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name>`
``grouped_dac_content_lane``, #3118): its old transport was a raw-PCM FIFO,
which the one-transport ruling (ADR-0100) leaves with no route to pin —
``jasper.multiroom.reconcile.outputd_grouping_env`` says so in prose. This
module is the identity the lane moves ONTO, shipped ahead of its consumers
exactly as ``60-jts-ring.conf``, ``61-jts-renderer-lanes.conf`` and the
grouping ring all shipped ahead of theirs, so a geometry that fails on metal
costs one file rather than a transport already flipped onto it.

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
#: defines — one string for the leader's localhost snapclient ``--soundcard``
#: and for outputd's reader.
DAC_CONTENT_RING_PCM = "jts_ring_dac_content"

#: The SHM ring file that PCM's ``path`` names, under the shared
#: ``/dev/shm/jts-ring`` directory the ring platform's tmpfiles entry creates.
#: Only a LEADER has a return path to carry, so unlike the coupling's content
#: hop there is no role-dependent second spelling.
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
