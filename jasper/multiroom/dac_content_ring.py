# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The DAC-content return transport's identity — one name, one wire, one owner.

A DUMB bonded member plays the bond's program by taking its own copy back
out of the sync engine, in either role: its snapclient writes the bond's
shared stereo — localhost for a leader, pointed at the leader for a
follower, purely a ``--host`` difference and not which ring gets carried —
and ``jasper-outputd`` reads it one DAC period at a time so the member is
sample-locked to the bond. This module owns what that transport IS — its
ALSA PCM name, its ring file, the conf.d block that declares it, and the
wire both ends have to agree on.

**Both ends are live.** ``jasper.multiroom.reconcile.outputd_grouping_env``
arms :data:`DAC_CONTENT_LANE_ENV` on every dumb bonded member and points that
member's snapclient at :data:`DAC_CONTENT_RING_PCM`; outputd resolves the
marker to ``ContentBridgeMode::DacContentRing`` and reads this ring as the
box's SOLE content source, attaching no central Ring B. The lane's old
transport was a raw-PCM FIFO, which the one-transport ruling (ADR-0100) left
with no route to pin — that spelling survives only as the
``grouped_dac_content_lane`` park (:doc:`ADR-0178
<../docs/adr/0178-every-shape-the-ring-cannot-serve-parks-under-its-own-name>`,
#3118) until its own deletion PR, and no writer emits it.

**A sibling of** :mod:`jasper.multiroom.grouping_ring`, **not a reuse of it.**
The two rings carry the same wire between the same two processes' languages,
which is exactly why sharing one would be wrong: a ring's slot is its READER's
period, and these have different readers. The grouping ring's reader is
CamillaDSP, which sips 128 frames; this ring's reader is outputd, which gulps a
whole 1024-frame DAC period. One ring cannot be both without one end reading
partial slots. They are also opposite directions of the same bond — ingress vs
return — and a box can hold both at once.

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

from jasper.ring_assets import ring_writer_lock_path

#: The ALSA PCM name ``deploy/alsa/conf.d/63-jts-ring-dac-content.conf``
#: defines — one string for a dumb bonded member's snapclient
#: ``--soundcard`` (either role) and for outputd's reader.
DAC_CONTENT_RING_PCM = "jts_ring_dac_content"

#: The SHM ring file that PCM's ``path`` names, under the shared
#: ``/dev/shm/jts-ring`` directory the ring platform's tmpfiles entry creates.
#: Every DUMB bonded member carries this lane, in either role, and all of
#: them name the same single file — unlike the coupling's content hop, there
#: is no role-dependent second spelling.
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

#: Slot geometry. The slot is the READER's period: outputd's
#: ``DEFAULT_PERIOD_FRAMES`` (``rust/jasper-outputd/src/config.rs``), so one
#: slot is one DAC period and the reader never holds a partial slot. 1024
#: frames x 2 ch x 2 B = 4096 B per slot, inside the ioplug's
#: ``JTS_RING_MAX_SLOT_BYTES`` (65536).
DAC_CONTENT_RING_PERIOD_FRAMES = 1024

#: Depth at the ioplug's slot CEILING (``JTS_RING_MAX_SLOTS``), the same
#: constraint the grouping ring took knowingly: depth is not tunable upward from
#: a conf.d edit. 16 x 1024 frames = 16384 frames = 341 ms at 48 kHz, against
#: the grouping ring's 43 ms — the depth follows the slot, and the slot follows
#: the reader. outputd gulps a whole period per DAC period where CamillaDSP sips
#: 128 frames, so the same 16 slots buy 8x the wall-clock cushion here.
DAC_CONTENT_RING_SLOTS = 16

#: The exclusive ``flock`` a C ioplug WRITER holds for the life of its mapping,
#: which is what makes a second writer's open fail loudly with ``-EBUSY``.
#: DERIVED by calling the ring platform's own constructor rather than spelled
#: again here — one suffix rule, one owner, already pinned against the C header
#: by ``tests/test_ring_slot_ceiling_pin.py``.
DAC_CONTENT_RING_WRITER_LOCK = ring_writer_lock_path(DAC_CONTENT_RING_FILE)
