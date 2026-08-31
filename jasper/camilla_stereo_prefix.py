# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared program-domain (stereo) DSP *prefix* builder.

The program domain (the 1–2-channel music bus) carries room correction
(Layer B) and preference EQ (Layer C): per-channel room PEQs, the shared
preference curve, the worst-case-boost headroom trim, and an optional
preamp. This module owns the single assembly of that pipeline —
``build_stereo_prefix`` — so every emitter that needs it builds from one
implementation instead of a copy:

  - plain stereo (``jasper.sound.camilla_yaml.emit_sound_config``),
  - the bonded-leader bake (via ``emit_sound_config``), and
  - the solo-active pre-split section (PR-3, ``jasper.active_speaker``).

**Layering.** This is a neutral leaf module (alongside
``jasper.camilla_emit`` and ``jasper.camilla_config_contract``). It takes
DATA — already-built preference :class:`FilterSpec` objects and room
:class:`PeqFilter` objects — never a ``SoundProfile``, so it imports
nothing from ``jasper.sound`` (and nothing from ``jasper.active_speaker``).
The caller builds the ``FilterSpec`` list (``build_sound_filters``) and
passes it in. This is what lets both the sound and active emitters reuse
the builder without an active→sound dependency.

This module spells *what* prefix to build (which filters, in what order,
the headroom policy); the per-line CamillaDSP YAML spelling stays in
``jasper.camilla_emit``, the leaf format contract.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from jasper.camilla_config_contract import (
    GAINLESS_BIQUAD_TYPES,
    SHELF_Q,
    SHELF_Q_EMIT_DECIMALS,
    FilterSpec,
    PeqFilter,
    total_positive_boost_db,
)
from jasper.camilla_emit import (
    emit_delay_filter,
    emit_gain_filter,
    emit_peaking_biquad,
    fmt,
)

logger = logging.getLogger(__name__)


def emit_filter_spec(spec: FilterSpec) -> list[str]:
    """Map a preference :class:`FilterSpec` (shelf / gainless / peaking) to a
    CamillaDSP ``Biquad`` block.

    Leaf ``fmt``/Peaking emission is shared (``jasper.camilla_emit``); this
    shelf/gainless dispatch is the preference-EQ assembly's own concern.

    **Every shelf is spelled with CamillaDSP's ``q`` steepness, at the constant
    :data:`~jasper.camilla_config_contract.SHELF_Q`** — the same Butterworth Q
    every evaluator in this codebase draws a shelf at. This is the single
    choke point for that invariant: every shelf JTS emits (taste-EQ curve
    presets, Simple bands, Advanced bands, and the Layer-1a linearization
    shelf / CD-horn backbone / trailing taper) reaches CamillaDSP through here,
    so no caller can express a shelf the model cannot see.

    CamillaDSP's ``ShelfSteepness`` is a ``#[serde(untagged)]`` enum with no
    ``deny_unknown_fields``, so it does NOT reject a shelf carrying both ``q``
    and ``slope`` -- it matches the ``Q`` variant first and silently ignores
    the ``slope`` (the README's "only one of q and slope" is a convention, not
    an enforced one). Do not rely on CamillaDSP to catch a double-specified
    shelf. What this codebase relies on instead is structural: ``FilterSpec``
    has no steepness field at all, so this emitter cannot write one. See
    ``SHELF_Q`` for the ``slope: 6`` defect this replaced (PR-L2).
    """
    lines = [
        f"  {spec.name}:",
        "    type: Biquad",
        "    parameters:",
        f"      type: {spec.biquad_type}",
        f"      freq: {fmt(spec.freq)}",
    ]
    if spec.biquad_type in {"Lowshelf", "Highshelf"}:
        lines.append(f"      q: {SHELF_Q:.{SHELF_Q_EMIT_DECIMALS}f}")
        lines.append(f"      gain: {fmt(spec.gain)}")
    elif spec.biquad_type in GAINLESS_BIQUAD_TYPES:
        # Highpass/Lowpass/Notch shape the response without a gain term.
        lines.append(f"      q: {fmt(spec.q or 1.0)}")
    else:
        lines.append(f"      q: {fmt(spec.q or 1.0)}")
        lines.append(f"      gain: {fmt(spec.gain)}")
    return lines


def build_stereo_prefix(
    sound_filters: Sequence[FilterSpec],
    room_peqs: Iterable[PeqFilter],
    *,
    room_peqs_right: Iterable[PeqFilter] | None = None,
    output_trim_db: float = 0.0,
    channel_delays_ms: tuple[float, float] | None = None,
) -> tuple[str, list[str], list[str] | None, float]:
    """Build the program-domain prefix: room PEQs → headroom → preamp →
    preference filters.

    Returns ``(filters_yaml, chain_names, chain_names_right, trim_db)`` —
    filter DEFINITIONS plus the per-channel chain NAME lists; the caller
    wires the names into its pipeline (``emit_master_gain_pipeline`` for the
    stereo emitter, the pre-split section for the active emitter). It does
    NOT emit the mixer/pipeline, so there is no master_gain-vs-split
    coupling here.

    ``sound_filters`` is the already-built preference filter list
    (``build_sound_filter_slots(profile)`` — a slot per declared band, neutral
    specs); it is normalized to a tuple at the boundary, so a generator is
    safe. Whether any spec is ``.active()`` — not whether the list is empty —
    gates the optional preamp, so a profile whose bands are all flat still
    plays at unity.

    ``chain_names_right`` is ``None`` when ``room_peqs_right`` is ``None``
    (solo — channel 1 duplicates channel 0, byte-identical to before this
    axis existed). When given, only the ROOM-correction segment differs
    per channel (``room_peq_r*`` — the per-seat part); the preference
    filters (taste, shared household EQ) and the optional preamp are the
    SAME named filters referenced by both chains — defined once.
    """
    # Normalize at the boundary: this is a shared builder (the stereo emitter
    # today, the active pre-split section next), so the boost scan and the
    # iteration below stay correct even if a caller hands a generator.
    sound_filters = tuple(sound_filters)
    lines: list[str] = []
    room_names: list[str] = []
    room_names_right: list[str] | None = None
    left_delay_ms, right_delay_ms = (
        (0.0, 0.0) if channel_delays_ms is None else channel_delays_ms
    )

    lines.extend(emit_gain_filter("flat", 0.0))

    if left_delay_ms > 0.0:
        lines.extend(emit_delay_filter("room_delay_l", delay_ms=left_delay_ms))
        room_names.append("room_delay_l")

    room_list = list(room_peqs)
    for i, peq in enumerate(room_list, start=1):
        name = f"room_peq_{i}"
        lines.extend(emit_peaking_biquad(name, freq=peq.freq, q=peq.q, gain=peq.gain))
        room_names.append(name)

    if room_peqs_right is not None:
        room_names_right = []
        if right_delay_ms > 0.0:
            lines.extend(emit_delay_filter("room_delay_r", delay_ms=right_delay_ms))
            room_names_right.append("room_delay_r")
        for i, peq in enumerate(room_peqs_right, start=1):
            name = f"room_peq_r{i}"
            lines.extend(
                emit_peaking_biquad(name, freq=peq.freq, q=peq.q, gain=peq.gain)
            )
            room_names_right.append(name)

    tail_names: list[str] = []

    # Audio-safety: room-correction BOOSTS (the assertive strategy runs
    # cuts_only=False, up to +3 dB total) raise specific bands with no
    # compensating attenuation, so a hot note in a boosted band can clip
    # above full scale. The master `volume_limit` caps the output FADER, not
    # a per-band filter boost upstream of it. Pull the whole signal down by
    # the worst-case additive room boost so the corrected response cannot
    # exceed unity. Cuts-only correction (the default safe/balanced path) has
    # zero boost, so this emits nothing and the solo config stays
    # byte-identical. The trim is SHARED across both room chains; for an
    # asymmetric leader-bake (different per-seat boosts per channel) we trim
    # by the louder channel so neither can clip.
    room_headroom_db = max(
        total_positive_boost_db(room_list),
        total_positive_boost_db(list(room_peqs_right or [])),
    )
    if room_headroom_db > 0.0:
        lines.extend(emit_gain_filter("room_headroom", -room_headroom_db))
        tail_names.append("room_headroom")
        # debug, not info: this emitter is re-run on every /sound/live-draft
        # slider interaction with the active room correction preserved, so an
        # info line here would spam the journal during EQ editing whenever an
        # assertive (boosted) correction is applied. The headroom is also
        # visible in the emitted YAML and the "wrote sound config" summary.
        logger.debug(
            "room-correction boost headroom: -%.2f dB preamp "
            "(worst-case additive room boost)",
            room_headroom_db,
        )

    # Preference boosts apply at unity: a +N dB band raises only that band
    # and leaves the rest of the spectrum untouched, like a consumer EQ. The
    # one optional global attenuation is the caller-supplied output trim
    # (manual headroom and/or loudness matching, both opt-in, both default 0).
    # With trim 0 there is no preamp at all -- boosts boost. The master
    # volume_limit ceiling stays the hard clip guard regardless. The trim
    # applies only when some band actually boosts -- a property of the specs'
    # VALUES, not of the list being non-empty, because the list carries a slot
    # per declared band so the graph's shape can stay still while one is
    # dragged through 0 dB. A flat profile can't clip from EQ, so it plays at
    # unity even if a headroom trim is configured.
    boosts = any(spec.active() for spec in sound_filters)
    trim_db = max(0.0, float(output_trim_db)) if boosts else 0.0
    if trim_db > 0.0:
        lines.extend(emit_gain_filter("sound_preamp", -trim_db))
        tail_names.append("sound_preamp")
    for spec in sound_filters:
        lines.extend(emit_filter_spec(spec))
        tail_names.append(spec.name)
    tail_names.append("flat")

    chain_names = room_names + tail_names
    chain_names_right = (
        None if room_names_right is None else room_names_right + tail_names
    )
    return "\n".join(lines), chain_names, chain_names_right, round(trim_db, 3)
