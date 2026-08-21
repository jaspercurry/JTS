# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grouping ingress transport's identity — one name, one wire, one owner.

A bonded JTS endpoint takes the leader's snapcast stream in through one
transport: snapclient writes it, the endpoint's CamillaDSP captures it. This
module owns what that transport IS — its ALSA PCM name, its ring file, the
conf.d block that declares it, and the wire both ends have to agree on.

**Three consumers, one name.** ``jasper.multiroom.reconcile`` gives
snapclient's ``--soundcard`` the WRITE end; ``precheck_active_follower`` and
``precheck_active_leader`` give their CamillaDSP's ``capture_device`` the READ
end. A ring PCM is one device opened in two directions — the ioplug branches its
``hw_params`` on ``io->stream`` (``c/jts-ring-ioplug/pcm_jts_ring.c``) — so all
three name the same string, and the playback/capture pair an snd-aloop round
trip needed collapses into one constant. That is what makes "the writer and the
reader agree" structural instead of a claim someone has to check.

**Here rather than in** :mod:`jasper.multiroom.reconcile`, which is a
2600-line module whose transport constants are already imported piecemeal by
production and test modules that want nothing else from it — for example,
``jasper.cli.doctor.grouping``'s ``check_grouping_leader_pipe`` imports only
``SNAPFIFO`` from the reconciler, not the whole module's surface.

**Deliberately NOT a member of the ring platform's registries.**
:data:`jasper.fanin_coupling.RING_PCM_DEVICES` is what
``jasper.active_speaker.camilla_yaml.active_emit_devices`` tests to decide a
graph is a ring graph and to hand it the ring chunk/target/queuelimit profile;
:data:`jasper.ring_assets.RING_CONF_PCMS` is what
``jasper.ring_assets.render_ring_conf_wire`` walks, and it raises for any member
without a ``per_block`` width entry. The grouping ring is neither the coupling's
wire nor a renderer lane — it is a third axis with its own conf.d file, exactly
as the renderer lanes got theirs. Adding it to either registry would route it
through machinery that is not about it.
"""

from __future__ import annotations

from jasper.ring_assets import ring_writer_lock_path

#: The ALSA PCM name ``deploy/alsa/conf.d/62-jts-ring-grouping.conf`` defines —
#: one string for snapclient's ``--soundcard`` and for CamillaDSP's capture
#: device.
GROUPING_RING_PCM = "jts_ring_grouping"

#: The SHM ring file that PCM's ``path`` names, under the shared
#: ``/dev/shm/jts-ring`` directory the ring platform's tmpfiles entry creates.
#: ONE name for both roles: a box is a leader or a follower, never both, so the
#: role-dependent naming the coupling's content hop uses buys nothing here.
GROUPING_RING_FILE = "/dev/shm/jts-ring/grouping.ring"

#: Where the installer places that conf.d block
#: (``deploy/lib/install/ring-platform.sh``'s ``install_jts_ring_conf_assets``).
#: Sibling of :data:`jasper.ring_assets.RING_CONF_D` and
#: :data:`jasper.renderer_lanes.RENDERER_LANES_CONF_D`.
GROUPING_RING_CONF_D = "/etc/alsa/conf.d/62-jts-ring-grouping.conf"

#: The wire, spelled in the conf.d block rather than inherited from the ioplug's
#: compiled defaults: snapclient decodes to the snapserver-pinned
#: ``sampleformat=48000:16:2``
#: (:func:`jasper.multiroom.reconcile.snapserver_argv`), so this ring carries
#: 16-bit stereo. ``tests/test_grouping_ring_platform.py`` pins both ends of that
#: binding — a widening of either side that left the other alone would be a
#: negotiation failure at open, not a quiet conversion, because this block is
#: opened directly and nothing wraps it: the ioplug's hw_params is single-valued
#: in every dimension but access, and unlike the renderer lanes there is no
#: ``plug`` PCM in front of it to absorb a mismatch.
GROUPING_RING_FORMAT = "S16_LE"
GROUPING_RING_CHANNELS = 2

#: Slot geometry. The slot equals the ring path's CamillaDSP chunk
#: (:data:`jasper.fanin_coupling.RING_CAMILLA_CHUNKSIZE`), so one slot is one
#: chunk. The depth sits at the ioplug's slot CEILING
#: (``JTS_RING_MAX_SLOTS``), which is a constraint the design took knowingly:
#: depth is not tunable upward from a conf.d edit, and the only other depth axis
#: is the period, which is also what bounds how coarse the writer's delay signal
#: can get. What the depth buys and what the ceiling costs: the design's §3.2.
GROUPING_RING_PERIOD_FRAMES = 128
GROUPING_RING_SLOTS = 16

#: The exclusive ``flock`` a C ioplug WRITER holds for the life of its mapping,
#: which is what makes a second writer's open fail loudly with ``-EBUSY``.
#: DERIVED by calling the ring platform's own constructor rather than spelled
#: again here — one suffix rule, one owner, already pinned against the C header
#: by ``tests/test_ring_slot_ceiling_pin.py``.
GROUPING_RING_WRITER_LOCK = ring_writer_lock_path(GROUPING_RING_FILE)
