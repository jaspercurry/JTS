"""Shared CamillaDSP YAML *emission* primitives — the single home for
the leaf helpers that turn a value into a line of CamillaDSP config.

Why this module exists
----------------------
Four subsystems generate CamillaDSP configs — room correction
(`jasper.correction.camilla_yaml`), sound-preference EQ
(`jasper.sound.camilla_yaml`), active-speaker crossovers
(`jasper.active_speaker.camilla_yaml`), and multi-room channel-split
(`jasper.multiroom.channel_split`). Before this module each had its own
copy of `_fmt` and its own hand-rolled `Gain` / `Biquad` / `Mixer`
emitters, so the CamillaDSP *format* (decimal places, field order,
inline-flow style, the `BiquadCombo` Linkwitz-Riley spelling) was
duplicated 3-4 ways. A CamillaDSP-syntax change meant editing every
copy, and a new DSP feature re-derived the same primitives (which is
how the multi-room crossover was first written as two cascaded `Biquad`
sections instead of the canonical native `BiquadCombo`).

This module is the **format contract**, nothing more. It owns *how* a
gain/biquad/crossover/mixer is spelled in YAML. It does NOT own *what*
config to build — the per-channel chains, gain-staging policy, PEQ
design, crossover regions, and channel routing stay in each subsystem's
emitter, where they belong. Keep that line crisp: leaf primitives here,
config assembly there.

Conventions preserved from the prior hand-rolled emitters (changing
them would re-format every generated config, so they are load-bearing):

  - 4-decimal floats via :func:`fmt` (``f"{v:.4f}"``).
  - 2-space indent per level; filter/mixer bodies sit under a
    ``filters:`` / ``mixers:`` map at column 0.
  - `Gain`/source params on one inline-flow line; `Biquad` /
    `BiquadCombo` params as a nested block.
  - Filter emitters return ``list[str]`` (callers ``extend`` a line
    buffer); :func:`emit_mixer` returns a joined ``str`` block.

Import-cheap on purpose (stdlib only): socket-activated web surfaces
build/inspect CamillaDSP YAML without dragging NumPy/SciPy in.
"""
from __future__ import annotations

import json
from collections.abc import Sequence


def fmt(value: float) -> str:
    """Format a float for CamillaDSP YAML — 4 decimal places.

    The one true formatter. Was copied verbatim into the sound,
    active-speaker, and multi-room emitters; this is the original.
    """
    return f"{value:.4f}"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def emit_gain_filter(
    name: str,
    gain_db: float,
    *,
    inverted: bool = False,
    mute: bool = False,
) -> list[str]:
    """A CamillaDSP ``Gain`` filter (one inline-flow params line).

    Reproduces the prior `sound._emit_gain_filter` (which always emitted
    ``mute: false``) and `active_speaker._emit_gain_filter` (which took a
    ``mute`` flag) byte-for-byte.
    """
    return [
        f"  {name}:",
        "    type: Gain",
        (
            "    parameters: "
            f"{{ gain: {fmt(gain_db)}, inverted: {_bool(inverted)}, "
            f"mute: {_bool(mute)} }}"
        ),
    ]


def emit_peaking_biquad(name: str, *, freq: float, q: float, gain: float) -> list[str]:
    """A CamillaDSP ``Biquad`` / ``Peaking`` filter (parametric EQ band).

    Reproduces `sound._emit_peq_filter` and the room-correction PEQ
    emitter byte-for-byte (both used ``:.4f`` on freq/q/gain).
    """
    return [
        f"  {name}:",
        "    type: Biquad",
        "    parameters:",
        "      type: Peaking",
        f"      freq: {fmt(freq)}",
        f"      q: {fmt(q)}",
        f"      gain: {fmt(gain)}",
    ]


def emit_linkwitz_riley(
    name: str,
    *,
    highpass: bool,
    freq_hz: float,
    order: int,
) -> list[str]:
    """A CamillaDSP ``BiquadCombo`` Linkwitz-Riley crossover section.

    This is CamillaDSP's *native* LR crossover — an order-N
    ``LinkwitzRileyLowpass`` / ``LinkwitzRileyHighpass``. An LR4 (the
    standard sub/woofer slope) is ``order=4``. Reproduces
    `active_speaker._emit_linkwitz_riley_filter` byte-for-byte; it is
    the canonical spelling that multi-room's crossover now uses instead
    of two hand-cascaded ``Biquad`` sections.
    """
    kind = "LinkwitzRileyHighpass" if highpass else "LinkwitzRileyLowpass"
    return [
        f"  {name}:",
        "    type: BiquadCombo",
        "    parameters:",
        f"      type: {kind}",
        f"      freq: {fmt(freq_hz)}",
        f"      order: {order}",
    ]


# One mixer source: (input_channel, gain_db, inverted).
MixerSource = tuple[int, float, bool]
# One output: (dest_channel_index, sources mixed onto it).
MixerDest = tuple[int, Sequence[MixerSource]]


def emit_mixer(
    name: str,
    *,
    channels_in: int,
    channels_out: int,
    mapping: Sequence[MixerDest],
    description: str | None = None,
    labels: Sequence[str] | None = None,
) -> str:
    """A CamillaDSP ``Mixer`` block (channel routing / matrix).

    ``mapping`` is ``[(dest_index, [(src_channel, gain_db, inverted), …]), …]``.
    Optional ``description`` / ``labels`` lines (used by the
    active-speaker split mixer) are emitted only when provided, so the
    multi-room channel-select mixer — which omits them — is unchanged.

    Reproduces `active_speaker._emit_split_mixer` and
    `multiroom._select_mixer_block` byte-for-byte. Returns a joined
    block (callers splice it under a top-level ``mixers:`` map).
    """
    lines = [f"  {name}:"]
    if description is not None:
        lines.append(f'    description: "{description}"')
    if labels is not None:
        lines.append(f"    labels: {json.dumps(list(labels))}")
    lines.append(f"    channels: {{ in: {channels_in}, out: {channels_out} }}")
    lines.append("    mapping:")
    for dest, sources in mapping:
        lines.append(f"      - dest: {dest}")
        lines.append("        sources:")
        for channel, gain_db, inverted in sources:
            lines.append(
                f"          - {{ channel: {channel}, gain: {fmt(gain_db)}, "
                f"inverted: {_bool(inverted)} }}"
            )
    return "\n".join(lines)


def emit_master_gain_pipeline(
    left_names: Sequence[str],
    right_names: Sequence[str] | None = None,
) -> str:
    """The standard JTS 2-channel ``pipeline:`` block — ``master_gain``
    Mixer step, then one ``Filter`` step per channel.

    Like :func:`emit_mixer`, this spells STRUCTURE with caller data: the
    filter-name lists are policy and stay in each subsystem's emitter
    (``peq_*`` vs ``room_peq_*`` naming, chain order); this function owns
    only how the pipeline is written. The ``master_gain`` step name is
    deliberately hard-coded — it IS the cross-subsystem contract: every
    JTS config preserves this mixer VERBATIM as an identity
    placeholder/anchor. Precisely (do not restate this wrong): the Ducker
    attenuates CamillaDSP's built-in ``main_volume`` fader, **not** this
    mixer; ``master_gain`` is the stable identity anchor that downstream
    weaves position against (multiroom's ``channel_select`` splices
    immediately after it) and the reserved hook for future mixer ops.
    See ``test_master_gain_mixer_unchanged_with_peqs`` in
    tests/test_correction_camilla_yaml.py — the byte-level contract.

    ``right_names=None`` (solo) duplicates ``left_names`` onto channel 1 —
    reproduces `correction._emit_pipeline` and `sound._emit_pipeline`
    byte-for-byte (the solo-impact contract; both consumers carry exact
    pipeline-bytes regression tests). A distinct ``right_names`` is the
    multi-room leader-bake (per-seat correction per channel,
    docs/HANDOFF-multiroom.md §2). Deliberately a 2-channel shape: the
    config contract is stereo-pinned today; 2.1's 3-channel stream
    generalises this WITH that contract, not alone. Returns a joined
    block (callers splice it under a top-level ``pipeline:`` map).
    """
    left = "[" + ", ".join(left_names) + "]"
    right = left if right_names is None else "[" + ", ".join(right_names) + "]"
    return (
        "  - type: Mixer\n"
        "    name: master_gain\n"
        "  - type: Filter\n"
        "    channels: [0]\n"
        f"    names: {left}\n"
        "  - type: Filter\n"
        "    channels: [1]\n"
        f"    names: {right}"
    )
