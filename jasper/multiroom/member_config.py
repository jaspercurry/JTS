"""Grouping member-config policy — the ONE place that decides what a bonded
member's local CamillaDSP config needs.

A speaker that is an ACTIVE member of a bond (enabled + valid) needs two
transforms applied to its local CamillaDSP config, and BOTH are grouping
concerns, not EQ concerns:

  - inv-5: ``enable_rate_adjust: false`` — snapclient is the sole rate-tracker
    for the synced chain (a second rate-adjuster oscillates).
  - the channel-split — the ``channel_select`` mixer (+ sub crossover) so the
    member plays only its assigned channel of the stereo program.

This module owns that decision so every config-apply path applies the SAME
transform instead of threading it per call site. Today two paths call it — the
``/sound`` apply and the ``/correction`` session — and a third will: the inv-2
reconciler, which (re)builds the post-snapclient member config (CamillaDSP-B)
and should reuse this exact policy rather than re-deriving it. Centralising it
here is what keeps "what a member's config needs" in ONE place as those callers
grow (the layering the EQ wizards must NOT own).

The transforms themselves live in :mod:`jasper.multiroom.channel_split`
(``build_channel_split`` / ``weave_channel_split``, consumed by
``emit_sound_config``); this module only decides WHICH to apply, from grouping
state. Pure except for the optional fresh read of ``grouping.env``.
"""
from __future__ import annotations

from typing import Any

from .channel_split import build_channel_split
from .config import GROUPING_ENV_FILE, GroupingConfig, is_active_member, load_config


def member_camilla_kwargs(
    cfg: GroupingConfig | None = None, *, path: str = GROUPING_ENV_FILE,
) -> dict[str, Any]:
    """The ``emit_sound_config(**kwargs)`` a member's config needs, derived from
    grouping state.

    For an ACTIVE bond member: ``enable_rate_adjust=False`` (inv-5) plus a
    ``channel_split`` built for its channel. Solo / off / invalid: the
    solo-speaker defaults (``enable_rate_adjust=True``, ``channel_split=None``),
    so a non-member's config is byte-for-byte unchanged.

    ``cfg`` defaults to a fresh read of ``grouping.env`` (the wizard apply
    paths); the inv-2 reconciler passes its already-resolved ``cfg`` so it does
    not re-read. ``stereo`` is a passthrough split (``build_channel_split`` weaves
    nothing), so an active member with no channel assignment is also unchanged.
    """
    if cfg is None:
        cfg = load_config(path)
    active = is_active_member(cfg)
    return {
        "enable_rate_adjust": not active,
        "channel_split": build_channel_split(cfg.channel) if active else None,
    }
