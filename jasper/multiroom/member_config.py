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
    snapserver's FIFO. No channel_split: the pipe carries BOTH channels.
    Optional leader-owned L/R acoustic delays travel here too; they are
    room/pair correction state, not follower-local policy.
  - DUMB FOLLOWER (passive, single-DAC): solo defaults. Its local CamillaDSP
    is OUT of the bonded playback path (the round-trip feeds outputd's
    dac_content lane directly); it keeps producing the normal direct lane —
    which is exactly the inv-B fallback feed — so its config stays
    byte-for-byte the solo config, rate_adjust=True and all (its sink is the
    ALSA loopback, which HAS a clock to track).
  - ACTIVE FOLLOWER (multi-driver, distributed-active Slice 3): NOT this path.
    An active follower relocates Layer A onto its OWN CamillaDSP IN the bonded
    path — it captures the round-trip snd-aloop loopback and runs the
    driver-domain crossover so the tweeter is never fed full-range. That config
    is emitted by the active-speaker driver-domain emitter and applied by
    :mod:`jasper.multiroom.follower_config` (the reconciler's active-follower
    arm), not by ``member_camilla_kwargs`` / ``emit_sound_config``. This
    function only governs the leader bake + the dumb-member solo defaults.
  - Solo / off / invalid: solo defaults (byte-for-byte unchanged).

MIGRATION NOTE: before Increment 5 this policy applied the
``channel_split`` weave to every active member's local config — the
superseded self-correct model where each member's own CamillaDSP
selected its channel. The canonical round-trip made that weave
obsolete (members drop channels in outputd, downstream of the stream);
``emit_sound_config``'s mutual-exclusion guards police the boundary.
``build_channel_split`` itself remains for the channel vocabulary and
lab paths.

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

from .config import GROUPING_ENV_FILE, GroupingConfig, is_active_member, load_config


def member_camilla_kwargs(
    cfg: GroupingConfig | None = None, *, path: str = GROUPING_ENV_FILE,
) -> dict[str, Any]:
    """The ``emit_sound_config(**kwargs)`` a member's config needs, derived from
    grouping state.

    ACTIVE LEADER: ``enable_rate_adjust=False`` + ``playback_pipe_path``
    (the bonded-leader pipe sink). DUMB FOLLOWER and solo / off /
    invalid: the solo-speaker defaults (``enable_rate_adjust=True``,
    ``playback_pipe_path=None``), so those configs are byte-for-byte
    unchanged — the dumb follower's local chain is the inv-B fallback feed,
    not part of the synced stream. An ACTIVE follower (multi-driver) does NOT
    use this function: its driver-domain crossover config comes from
    :mod:`jasper.multiroom.follower_config` (distributed-active Slice 3).

    ``channel_split`` is always ``None`` here (canonical members drop
    channels in outputd's ChannelPick, never in a local CamillaDSP
    weave — see the migration note in the module docstring).

    ``cfg`` defaults to a fresh read of ``grouping.env`` (the wizard
    apply paths); the reconciler passes its already-resolved ``cfg``.
    """
    if cfg is None:
        cfg = load_config(path)
    if is_active_member(cfg) and cfg.role == "leader":
        from .reconcile import SNAPFIFO

        out = {
            "enable_rate_adjust": False,
            "channel_split": None,
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
        "enable_rate_adjust": True,
        "channel_split": None,
        "playback_pipe_path": None,
    }
