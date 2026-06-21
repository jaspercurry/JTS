"""Shared CamillaDSP YAML *emission* primitives — the single home for
the leaf helpers that turn a value into a line of CamillaDSP config.

Why this module exists
----------------------
Three subsystems generate CamillaDSP configs — sound-preference EQ
(`jasper.sound.camilla_yaml`, which also composes live room correction),
active-speaker crossovers (`jasper.active_speaker.camilla_yaml`), and
multi-room channel-split (`jasper.multiroom.channel_split`). Before this
module each had its own
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
import math
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


def emit_delay_filter(name: str, *, delay_ms: float) -> list[str]:
    """A CamillaDSP ``Delay`` filter in milliseconds.

    This primitive is deliberately gainless: callers own the policy of
    where a delay belongs, while this shared emitter owns only the YAML
    spelling.
    """
    return [
        f"  {name}:",
        "    type: Delay",
        "    parameters:",
        f"      delay: {fmt(delay_ms)}",
        "      unit: ms",
    ]


def emit_peaking_biquad(name: str, *, freq: float, q: float, gain: float) -> list[str]:
    """A CamillaDSP ``Biquad`` / ``Peaking`` filter (parametric EQ band).

    Reproduces the sound room-PEQ/preference-PEQ spelling byte-for-byte
    (``:.4f`` on freq/q/gain).
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


# --- channel-select (inter-speaker pick) vocabulary --------------------------
# The 2->2 mixer that picks WHICH channel of the stereo program a whole speaker
# plays in a bond (left / right / a clip-safe mono+sub sum). This is the
# INTER-speaker axis; it composes BEFORE any intra-speaker driver split. It is a
# pure format/routing primitive (the "how"), so it lives here in the shared leaf
# rather than in either caller — the two consumers are
# ``jasper.multiroom.channel_split`` (weaves it into a 2-ch member config) and
# ``jasper.active_speaker.camilla_yaml`` (prepends it to a follower's
# driver-domain-only crossover graph). Sharing one definition keeps the mixer
# name + the clip-safe mono-sum gain from drifting between them.

# The split mixer's name. Distinct from ``master_gain`` on purpose: the Ducker
# drives CamillaDSP's global ``main_volume`` fader and relies on ``master_gain``
# staying the identity mixer, so channel-select is a SEPARATE mixer inserted
# after it.
CHANNEL_SELECT_MIXER = "channel_select"

# Per-source gain for a 2-channel mono/sub sum. 20*log10(0.5) = -6.0206 dB:
# identical L==R inputs (a mono track, the common case on a mono/sub speaker)
# sum to EXACTLY 0 dBFS, so they cannot clip under ``volume_limit: 0.0``.
# Uncorrelated content is correspondingly quieter; that is the safe trade for a
# household speaker (loudness is recovered downstream by the volume fader, not by
# risking a clip here).
MONO_SUM_GAIN_DB = 20.0 * math.log10(0.5)  # -6.020599913…

# Unity route gain (dB). Selecting a single channel onto an output is a plain
# copy — no attenuation, no boost.
_CHANNEL_ROUTE_GAIN_DB = 0.0


def channel_select_sources(channel: str) -> list[tuple[int, float, bool]]:
    """The ``(input_channel, gain_db, inverted)`` sources mixed onto EACH of the
    two output channels for one inter-speaker channel assignment.

    One unity source for a ``left`` / ``right`` route; two clip-safe -6.02 dB
    sources for a ``mono`` / ``sub`` L+R sum. Both output channels get the SAME
    content, so the assigned channel reaches the driver regardless of how the
    physical speaker taps the stereo bus. ``stereo`` is passthrough and has no
    mixer, so it is not a valid argument here (callers handle it separately).
    ``sub`` shares ``mono``'s sum; its low-pass crossover is a separate filter.

    Raises ``ValueError`` for an unknown / passthrough channel — this is an
    internal (resolved) value, so fail loud rather than emit a silent mis-route.
    """
    if channel == "left":
        return [(0, _CHANNEL_ROUTE_GAIN_DB, False)]
    if channel == "right":
        return [(1, _CHANNEL_ROUTE_GAIN_DB, False)]
    if channel in {"mono", "sub"}:
        return [(0, MONO_SUM_GAIN_DB, False), (1, MONO_SUM_GAIN_DB, False)]
    raise ValueError(
        f"channel {channel!r} has no channel-select mixer "
        "(expected one of left, right, mono, sub)"
    )


def emit_channel_select_mixer(
    channel: str,
    *,
    name: str = CHANNEL_SELECT_MIXER,
) -> str:
    """The 2->2 ``channel_select`` Mixer block for one inter-speaker channel.

    Both output channels carry the same content (see
    :func:`channel_select_sources`). Returns a joined block (callers splice it
    under a top-level ``mixers:`` map). ``stereo`` is rejected via
    :func:`channel_select_sources` — it is passthrough and emits no mixer.
    """
    sources = channel_select_sources(channel)
    return emit_mixer(
        name,
        channels_in=2,
        channels_out=2,
        mapping=[(0, sources), (1, sources)],
    )


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
    deliberately hard-coded — it IS the contract of the configs that
    carry it (the base outputd config and the correction/sound family;
    active-speaker configs have no ``master_gain``): those configs emit
    the mixer as an identity placeholder and preserve it VERBATIM.
    Precisely (do not restate this wrong): the Ducker attenuates
    CamillaDSP's built-in ``main_volume`` fader, **not** this mixer;
    ``master_gain`` is the stable identity anchor that downstream weaves
    position against (multiroom's ``channel_select`` splices immediately
    after it) and the reserved hook for future mixer ops. See the
    sound-emitter tests for the byte-level contract.

    ``right_names=None`` (solo) duplicates ``left_names`` onto channel 1 —
    reproduces the sound emitter's solo pipeline byte-for-byte (the
    solo-impact contract). A distinct ``right_names`` is the
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
