# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grouping member-config policy — the ONE place that decides what a bonded
member's local CamillaDSP config needs.

CANONICAL MODEL (docs/HANDOFF-multiroom.md §2 "Canonical signal flow",
wired by Increment 5): the LEADER's one CamillaDSP bakes the shared
stereo program and writes it to snapserver's pipe; every member —
including the leader itself — plays the round-tripped stream through
outputd's ``dac_content`` lane, picking its channel THERE (outputd
``ChannelPick``), never in a local CamillaDSP weave. So the policy is:

  - ACTIVE LEADER: ``enable_rate_adjust=False`` (a File/pipe sink has no
    output clock; snapclient's sample-stuffing is the synced chain's ONE
    rate-tracker — §2 invariant 5) + ``playback_pipe_path`` pointed at
    snapserver's FIFO — the pipe carries BOTH channels; each member picks
    its own downstream, in outputd's ``ChannelPick``.
    Optional leader-owned L/R acoustic delays travel here too; they are
    room/pair correction state, not follower-local policy.
  - DUMB FOLLOWER (passive, single-DAC): solo defaults. Its local CamillaDSP
    is OUT of the bonded playback path (the round-trip feeds outputd's
    dac_content lane directly); it keeps producing the normal direct lane —
    which is exactly the inv-B fallback feed — so its config stays
    byte-for-byte the solo config.
  - ACTIVE FOLLOWER (multi-driver, distributed-active Slice 3): NOT this path.
    An active follower relocates Layer A onto its OWN CamillaDSP IN the bonded
    path — it captures the grouping ring
    (:mod:`jasper.multiroom.grouping_ring`) and runs the driver-domain
    crossover so the tweeter is never fed full-range. That config is emitted
    by the active-speaker driver-domain emitter and applied by
    :mod:`jasper.multiroom.follower_config` (the reconciler's active-follower
    arm), not by ``member_camilla_kwargs`` / ``emit_sound_config``. This
    function only governs the leader bake + the dumb-member solo defaults.
  - Solo / off / invalid: solo defaults (byte-for-byte unchanged).

  EVERY branch above emits ``enable_rate_adjust=False``, not just the
  leader's. The non-leader sink used to be the ALSA loopback (a real output
  clock, so ``True`` was a live request there); since ADR-0100 it is Ring B
  (:data:`jasper.fanin_coupling.RING_PLAYBACK_DEVICE`, the ONE CamillaDSP ->
  outputd transport), and a ring PCM is an ioplug — alsa-lib reports card -1
  for every ioplug, so CamillaDSP builds no HCtl and the request cannot be
  actuated (see ``jasper.active_speaker.camilla_yaml``'s
  ``DEFAULT_ACTIVE_ENABLE_RATE_ADJUST`` for the same mechanism on the other
  ring). Requesting ``True`` there is not wrong-but-harmless, it is a
  standing lie: ``capture_status.rate_adjust`` would echo the request on the
  websocket while nothing moves. This function no longer asks.

This module owns the decision so every config-apply path — ``/sound``,
``/correction``, and the grouping reconciler's bond apply
(:mod:`jasper.multiroom.leader_config`) — applies the SAME transform
instead of threading it per call site. Centralising it here is what
keeps "what a member's config needs" in ONE place as callers grow (and
is what stops a ``/sound`` save while bonded from silently yanking the
leader's CamillaDSP off the pipe). Pure except for the optional fresh
read of ``grouping.env``.
"""
from __future__ import annotations

from typing import Any

from . import config
from .config import GROUPING_ENV_FILE, GroupingConfig

# NOTE: `load_config` / `is_active_leader` are resolved through the `config`
# module at call time (``config.load_config`` / ``config.is_active_leader``),
# NOT captured via ``from .config import ...``. A from-import binds a private
# reference at import time; if this module was first imported while a test had
# monkeypatched ``jasper.multiroom.config.load_config`` (the grouping-doctor
# tests do exactly this), that reference stuck to the stub and survived the
# monkeypatch teardown, poisoning later tests — pytest-xdist sharding hid it
# by varying which test imported this module first (#1270). Call-time
# resolution honours both the patch and its teardown.


def member_camilla_kwargs(
    cfg: GroupingConfig | None = None, *, path: str = GROUPING_ENV_FILE,
) -> dict[str, Any]:
    """The ``emit_sound_config(**kwargs)`` a member's config needs, derived from
    grouping state.

    ACTIVE LEADER: ``enable_rate_adjust=False`` + ``playback_pipe_path``
    (the bonded-leader pipe sink). DUMB FOLLOWER and solo / off /
    invalid: the solo-speaker defaults (``enable_rate_adjust=False``,
    ``playback_pipe_path=None``), so those configs are byte-for-byte
    unchanged — the dumb follower's local chain is the inv-B fallback feed,
    not part of the synced stream. ``enable_rate_adjust`` is False there too:
    that fallback feed's sink is Ring B, an ioplug CamillaDSP cannot actuate
    rate_adjust on (see the module docstring). An ACTIVE follower
    (multi-driver) does NOT use this function: its driver-domain crossover
    config comes from :mod:`jasper.multiroom.follower_config`
    (distributed-active Slice 3).

    Members drop channels in outputd's ChannelPick, never in a local
    CamillaDSP weave.

    ``cfg`` defaults to a fresh read of ``grouping.env`` (the wizard
    apply paths); the reconciler passes its already-resolved ``cfg``.
    """
    if cfg is None:
        cfg = config.load_config(path)
    if config.is_active_leader(cfg):
        from .reconcile import SNAPFIFO

        out = {
            "enable_rate_adjust": False,
            "playback_pipe_path": SNAPFIFO,
        }
        if cfg.left_delay_ms > 0.0 or cfg.right_delay_ms > 0.0:
            # Non-zero per-channel delay requires distinct L/R room
            # chains. If a follower room PEQ has not been measured yet,
            # the right room segment is explicitly flat rather than an
            # accidental duplicate of the leader's room PEQ chain.
            out["room_peqs_right"] = []
            out["channel_delays_ms"] = (cfg.left_delay_ms, cfg.right_delay_ms)
        return out
    return {
        "enable_rate_adjust": False,
        "playback_pipe_path": None,
    }
