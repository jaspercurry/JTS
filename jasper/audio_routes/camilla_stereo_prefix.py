# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared program-domain (stereo) DSP *prefix* builder.

The program domain (the 1–2-channel music bus) carries room correction
(Layer B) and preference EQ (Layer C): per-channel room PEQs, the shared
preference curve, the room headroom trim, and an optional
preamp. This module owns the single assembly of that pipeline —
``build_stereo_prefix`` — so every emitter that needs it builds from one
implementation instead of a copy:

  - plain stereo (``jasper.sound.camilla_yaml.emit_sound_config``),
  - the bonded-leader bake (via ``emit_sound_config``), and
  - the solo-active pre-split section (PR-3, ``jasper.active_speaker``).

**Layering.** This is a neutral leaf module (alongside
``jasper.audio_routes.camilla_emit`` and ``jasper.dsp_control.camilla_config_contract``). It takes
DATA — already-built preference :class:`FilterSpec` objects and room
:class:`PeqFilter` objects — never a ``SoundProfile``, so it imports
nothing from ``jasper.sound`` (and nothing from ``jasper.active_speaker``).
The caller builds the ``FilterSpec`` list (``build_sound_filter_slots``) and
passes it in. This is what lets both the sound and active emitters reuse
the builder without an active→sound dependency.

This module spells *what* prefix to build (which filters, in what order,
the headroom policy); the per-line CamillaDSP YAML spelling stays in
``jasper.audio_routes.camilla_emit``, the leaf format contract.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from jasper.platform.biquad import (
    GAINLESS_BIQUAD_TYPES,
    SHELF_BIQUAD_TYPES,
    SHELF_Q,
    SHELF_Q_EMIT_DECIMALS,
    FilterSpec,
    PeqFilter,
    headroom_charge_db,
    peaking_cascade_peak_db,
)
from jasper.audio_routes.camilla_emit import (
    emit_delay_filter,
    emit_gain_filter,
    emit_peaking_biquad,
    fmt,
)

logger = logging.getLogger("jasper.camilla_stereo_prefix")


def emit_filter_spec(spec: FilterSpec) -> list[str]:
    """Map a preference :class:`FilterSpec` (shelf / gainless / peaking) to a
    CamillaDSP ``Biquad`` block.

    Leaf ``fmt``/Peaking emission is shared (``jasper.audio_routes.camilla_emit``); this
    shelf/gainless dispatch is the preference-EQ assembly's own concern.

    **Every shelf is spelled with CamillaDSP's ``q`` steepness, at the constant
    :data:`~jasper.platform.biquad.SHELF_Q`** — the same Butterworth Q
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
    if spec.biquad_type in SHELF_BIQUAD_TYPES:
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

    ``sound_filters`` is the already-built preference filter list: EVERY caller
    passes ``build_sound_filter_slots(profile)``, a slot per declared band with
    the neutral ones kept, so the graph's shape follows the profile's
    declaration and never its values. It is normalized to a tuple at the
    boundary, so a generator is safe. The preamp is emitted unconditionally and
    its gain carries the trim — see the comment at the emit for why, and for
    the promise that change deliberately drops.

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

    room_list_right = None if room_peqs_right is None else list(room_peqs_right)
    if room_list_right is not None:
        room_names_right = []
        if right_delay_ms > 0.0:
            lines.extend(emit_delay_filter("room_delay_r", delay_ms=right_delay_ms))
            room_names_right.append("room_delay_r")
        for i, peq in enumerate(room_list_right, start=1):
            name = f"room_peq_r{i}"
            lines.extend(
                emit_peaking_biquad(name, freq=peq.freq, q=peq.q, gain=peq.gain)
            )
            room_names_right.append(name)

    tail_names: list[str] = []

    # Audio-safety: a room-correction boost raises its band with no
    # compensating attenuation, and `volume_limit` caps the fader, not a filter
    # upstream of it. So the whole signal is pulled down by the room chain's
    # netted peak plus the margin, and cuts net against boosts (ADR-0399). A
    # chain that never leaves unity (cuts only) emits nothing, so the solo
    # config stays byte-identical. The trim is SHARED by both room chains, so
    # it pays for the louder one.
    room_headroom_db = headroom_charge_db(max(
        peaking_cascade_peak_db(room_list),
        peaking_cascade_peak_db(room_list_right or []),
    ))
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
            "(netted room peak plus margin)",
            room_headroom_db,
        )

    # Preference boosts apply at unity: a +N dB band raises only that band and
    # leaves the rest of the spectrum untouched, like a consumer EQ. The one
    # global attenuation is the caller-supplied output trim (manual headroom
    # and/or loudness matching, both opt-in, both default 0).
    #
    # Always emit sound_preamp, including at 0 dB. Adding or removing a filter
    # makes CamillaDSP rebuild its group and reset filter state; changing only
    # the gain preserves that state. A configured trim also attenuates a flat
    # profile; devices.volume_limit remains independent of this trim.
    trim_db = max(0.0, float(output_trim_db))
    # -0.0 formats as "-0.0000", which would make two graphs that are the same
    # number look like different bytes to every comparison downstream.
    lines.extend(emit_gain_filter("sound_preamp", -trim_db if trim_db else 0.0))
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
