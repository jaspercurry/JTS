# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grouping member-config policy — the ONE place that decides what a bonded
member's local CamillaDSP config needs.

CANONICAL MODEL (wired by Increment 5): the LEADER's one CamillaDSP bakes the
shared stereo program and writes it to snapserver's pipe; every member —
including the leader itself — plays the round-tripped stream through outputd's
``dac_content`` lane, picking its channel THERE (outputd ``ChannelPick``), never
in a local CamillaDSP weave. So the ONLY member policy is the leader's: the pipe
sink, plus any leader-owned L/R acoustic delays (room/pair correction state, not
follower-local policy). Every other shape — dumb follower, solo, off, invalid —
needs nothing: its local CamillaDSP keeps producing the normal direct lane,
which is the inv-B fallback feed, byte-for-byte the solo config.

An ACTIVE follower (multi-driver, distributed-active Slice 3) is not this path
at all: it relocates Layer A onto its OWN CamillaDSP IN the bonded path, and
that config comes from :mod:`jasper.multiroom.follower_config`.

``enable_rate_adjust`` is not member policy — see ADR-0218.

This module owns the decision so every config-apply path — ``/sound``,
``/sound/room/``, and the grouping reconciler's bond apply
(:mod:`jasper.multiroom.leader_config`) — applies the SAME transform instead of
threading it per call site (and is what stops a ``/sound`` save while bonded
from silently yanking the leader's CamillaDSP off the pipe). Pure except for the
optional fresh read of ``grouping.env``.
"""
from __future__ import annotations

from typing import Any

from . import config
from .config import GROUPING_ENV_FILE, GroupingConfig

# `load_config` / `is_active_leader` are resolved through the `config` module
# at call time (``config.load_config`` / ``config.is_active_leader``), NOT
# captured via ``from .config import ...``: a from-import binds a private
# reference at import time, so if this module is imported while a test has
# monkeypatched ``jasper.multiroom.config.load_config`` (the grouping-doctor
# tests do), the reference sticks to the stub past the monkeypatch teardown
# (#1270). Call-time resolution honours both the patch and its teardown.


def member_camilla_kwargs(
    cfg: GroupingConfig | None = None, *, path: str = GROUPING_ENV_FILE,
) -> dict[str, Any]:
    """The ``emit_sound_config(**kwargs)`` a member's config needs, derived from
    grouping state.

    ACTIVE LEADER: ``playback_pipe_path`` (the bonded-leader pipe sink), plus
    the leader's L/R delays when it carries any. Every other shape: ``{}`` —
    the emitter's own solo defaults, byte-for-byte unchanged.

    ``cfg`` defaults to a fresh read of ``grouping.env`` (the wizard
    apply paths); the reconciler passes its already-resolved ``cfg``.
    """
    if cfg is None:
        cfg = config.load_config(path)
    if not config.is_active_leader(cfg):
        return {}
    from .reconcile import SNAPFIFO

    out: dict[str, Any] = {"playback_pipe_path": SNAPFIFO}
    if cfg.left_delay_ms > 0.0 or cfg.right_delay_ms > 0.0:
        # Non-zero per-channel delay requires distinct L/R room chains. If a
        # follower room PEQ has not been measured yet, the right room segment is
        # explicitly flat rather than an accidental duplicate of the leader's
        # room PEQ chain.
        out["room_peqs_right"] = []
        out["channel_delays_ms"] = (cfg.left_delay_ms, cfg.right_delay_ms)
    return out
