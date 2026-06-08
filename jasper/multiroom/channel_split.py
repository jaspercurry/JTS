"""Pure CamillaDSP channel-split fragment generator for bonded speakers.

A speaker in a bond plays only its assigned channel of the stereo
program. This module emits the CamillaDSP fragment that performs the
split — a `channel_select` Mixer (and, for a subwoofer, a lowpass
crossover).

The fragment is composable YAML: a later on-device increment (P1.3)
splices it into the live config — after `master_gain`, before the DAC.
This module is PURE: no I/O, no live apply. Building and testing it is
hardware-free; weaving it into the active config and validating sound
is the on-device follow-on.

This is a host-agnostic DSP recipe, not a deployment. The same fragment
is applied either:
  - LOCALLY on a brainy endpoint — a Pi-5 speaker in a stereo pair
    selects its own L / R from the shared stereo stream via its own
    CamillaDSP (alongside its room correction); or
  - on the LEADER, to pre-bake a DUMB endpoint's dedicated stream — a
    Pi Zero sub / satellite runs NO CamillaDSP (design doc §1), so the
    leader applies this fragment and streams the result as that
    endpoint's channel.
Which host applies it is the P1.3 integration decision (design doc §4).
This module only emits the correct fragment for a channel; it does not
choose where it runs.

Design invariants (each has a regression test):

  - NEVER touches `master_gain`. The Ducker (jasper/camilla.py) drives
    CamillaDSP's global `main_volume` fader and relies on `master_gain`
    staying the identity mixer. Channel-split is a SEPARATE mixer
    inserted after it, so ducking, the volume coordinator, and the
    `volume_limit: 0.0` clip ceiling are all unaffected.

  - NEVER emits a positive source gain. The only gains are 0 dB (a
    plain route) or -6.02 dB (a mono/sub sum). No stage can push the
    signal toward the clip ceiling.

  - A mono/sub sum uses -6.0206 dB (= 0.5 linear amplitude) per
    source, so identical L==R content — a mono track, the common case
    on a mono/sub speaker — sums to EXACTLY 0 dBFS. No clipping.
    Uncorrelated content is correspondingly quieter; that is the safe
    trade for a household speaker (loudness is recovered downstream by
    the normal volume fader, not by risking a clip here).

  - `stereo` is passthrough — it emits NOTHING (no mixer, no filter,
    no pipeline step). A solo / unsplit speaker's config is
    byte-for-byte what it would be without grouping.

Subwoofer crossover is a fixed Linkwitz-Riley 4th-order lowpass
(two cascaded Butterworth biquads), default 80 Hz — the de-facto
consumer-AVR sub corner. Main-speaker bass-management highpass (to
unload <80 Hz from the L/R speakers) is a deliberate V1 non-goal: the
mains stay full-range and the sub ADDS low end, so 20–80 Hz is
reproduced by BOTH — expect an audible low-end lift and a phase-overlap
region until bass management lands. Simple and safe; revisit if a
household wants true bass management.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from jasper.multiroom.config import ALLOWED_CHANNELS

# The split mixer's name. Distinct from `master_gain` on purpose — see
# the Ducker invariant in the module docstring.
CHANNEL_SELECT_MIXER = "channel_select"

# Sub crossover corner. 80 Hz is the standard consumer crossover (THX /
# most AV receivers default here). Tunable via build_channel_split(...).
DEFAULT_CROSSOVER_HZ = 80.0

# Butterworth Q for one 2nd-order section. Two cascaded Butterworth
# lowpasses at the same corner == a Linkwitz-Riley 4th-order crossover
# (-6 dB at fc, 24 dB/octave) — the standard sub lowpass slope.
_BUTTERWORTH_Q = 1.0 / math.sqrt(2.0)  # 0.70710678…

# Per-source gain for a 2-channel mono sum. 20*log10(0.5) = -6.0206 dB:
# identical L==R inputs sum to exactly 0 dBFS, so a mono track played
# through a mono/sub speaker cannot clip under volume_limit: 0.0.
MONO_SUM_GAIN_DB = 20.0 * math.log10(0.5)  # -6.020599913…

# Unity route gain (dB). Selecting a channel onto an output is a plain
# copy — no attenuation, no boost.
_ROUTE_GAIN_DB = 0.0


def _fmt(value: float) -> str:
    """Match jasper.sound.camilla_yaml._fmt — 4 decimal places."""
    return f"{value:.4f}"


@dataclass(frozen=True)
class ChannelSplit:
    """The composable CamillaDSP fragment for one channel assignment.

    The fields map 1:1 onto the four splice operations a config-weaver
    (P1.3) performs against a JTS-generated config:

      1. Insert ``mixer_block`` under the top-level ``mixers:`` key.
      2. Insert ``filter_block`` under the top-level ``filters:`` key.
      3. Insert ``pipeline_mixer_step`` into ``pipeline:`` immediately
         after the ``master_gain`` Mixer step.
      4. Append ``filter_chain_names`` to the end of every per-channel
         ``Filter`` step's ``names:`` list, so the crossover runs LAST
         (after room correction / preference EQ), just before the DAC.

    For ``stereo`` every field is empty / passthrough: weaving it is a
    no-op, leaving the config identical to a solo speaker's.
    """

    channel: str
    # YAML block for the top-level `mixers:` map. "" for stereo.
    mixer_block: str
    # Mixer name for the pipeline step, or None for stereo passthrough.
    mixer_name: str | None
    # YAML block for the top-level `filters:` map (sub crossover).
    # "" unless channel == "sub".
    filter_block: str
    # Filter names to append to each per-channel pipeline Filter step.
    # () unless channel == "sub".
    filter_chain_names: tuple[str, ...]
    # Pipeline step inserted after the master_gain Mixer step. "" for stereo.
    pipeline_mixer_step: str

    @property
    def is_passthrough(self) -> bool:
        """True when this assignment changes nothing (stereo / solo)."""
        return self.mixer_name is None


def _source_line(channel_index: int, gain_db: float) -> str:
    """One block-list mixer source (10-space indent, under `sources:`)."""
    return (
        f"          - {{ channel: {channel_index}, "
        f"gain: {_fmt(gain_db)}, inverted: false }}"
    )


def _select_mixer_block(sources_per_dest: list[tuple[int, float]]) -> str:
    """Emit the `channel_select` 2->2 mixer.

    `sources_per_dest` is the (input_channel, gain_db) list mixed onto
    EACH of the two output channels — one source for a left/right route,
    two for a mono/sub sum. Both outputs always carry the same content,
    so the assigned channel reaches the driver regardless of how the
    physical speaker taps the stereo DAC (output 0, output 1, or a
    passive L+R sum). One block-list emission path for every case keeps
    the indentation uniform.
    """
    lines = [
        f"  {CHANNEL_SELECT_MIXER}:",
        "    channels: { in: 2, out: 2 }",
        "    mapping:",
    ]
    for dest in (0, 1):
        lines.append(f"      - dest: {dest}")
        lines.append("        sources:")
        for channel_index, gain_db in sources_per_dest:
            lines.append(_source_line(channel_index, gain_db))
    return "\n".join(lines)


def _crossover_filter_block(crossover_hz: float) -> tuple[str, tuple[str, ...]]:
    """Emit the LR4 (two cascaded Butterworth) lowpass for a subwoofer.

    Returns (yaml_block_for_`filters:`, names_to_append_to_each_channel).
    """
    names = ("sub_lp_1", "sub_lp_2")
    blocks: list[str] = []
    for name in names:
        blocks.extend(
            [
                f"  {name}:",
                "    type: Biquad",
                "    parameters:",
                "      type: Lowpass",
                f"      freq: {_fmt(crossover_hz)}",
                f"      q: {_fmt(_BUTTERWORTH_Q)}",
            ]
        )
    return "\n".join(blocks), names


def _pipeline_mixer_step() -> str:
    """The pipeline step that applies `channel_select` after master_gain."""
    return f"  - type: Mixer\n    name: {CHANNEL_SELECT_MIXER}"


def build_channel_split(
    channel: str,
    *,
    crossover_hz: float = DEFAULT_CROSSOVER_HZ,
) -> ChannelSplit:
    """Build the channel-split fragment for one channel assignment.

    `channel` is one of jasper.multiroom.config.ALLOWED_CHANNELS:
    "stereo" | "left" | "right" | "mono" | "sub". An unknown value
    raises ValueError — this is internal (a resolved GroupingConfig),
    not raw user input, so fail loud rather than silently passthrough.

    Pure and deterministic: same inputs -> identical YAML.
    """
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(
            f"channel {channel!r} is not one of {', '.join(ALLOWED_CHANNELS)}"
        )

    if channel == "stereo":
        # Passthrough: no mixer, no filter, no pipeline step. Weaving
        # this leaves a solo speaker's config untouched.
        return ChannelSplit(
            channel=channel,
            mixer_block="",
            mixer_name=None,
            filter_block="",
            filter_chain_names=(),
            pipeline_mixer_step="",
        )

    if channel == "left":
        mixer_block = _select_mixer_block([(0, _ROUTE_GAIN_DB)])
    elif channel == "right":
        mixer_block = _select_mixer_block([(1, _ROUTE_GAIN_DB)])
    else:  # "mono" or "sub" — both outputs are the clip-safe L+R sum
        mixer_block = _select_mixer_block(
            [(0, MONO_SUM_GAIN_DB), (1, MONO_SUM_GAIN_DB)]
        )

    filter_block = ""
    filter_chain_names: tuple[str, ...] = ()
    if channel == "sub":
        if crossover_hz <= 0:
            raise ValueError(
                f"crossover_hz must be positive, got {crossover_hz!r}"
            )
        filter_block, filter_chain_names = _crossover_filter_block(crossover_hz)

    return ChannelSplit(
        channel=channel,
        mixer_block=mixer_block,
        mixer_name=CHANNEL_SELECT_MIXER,
        filter_block=filter_block,
        filter_chain_names=filter_chain_names,
        pipeline_mixer_step=_pipeline_mixer_step(),
    )
