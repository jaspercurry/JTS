# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

Two "channel" vocabularies — keep them straight. This module's
``channel`` (left/right/sub/mono/stereo) is the INTER-speaker axis:
which channel of the stereo PROGRAM a whole speaker plays in a bond.
It is distinct from ``output_topology.SpeakerChannel`` (``role`` =
woofer/tweeter/…), the INTRA-speaker axis: which DRIVER a physical DAC
output feeds. They compose, they do not compete — on a multi-way active
speaker that is also a bond member, this channel-select runs FIRST
(pick the L/R/mono program), then the active-speaker crossover splits
that program across the drivers. Neither layer needs to know about the
other because channel-select is INTERFACE-PRESERVING: a 2→2 transform
that changes only WHAT is on the two channels. Everything downstream —
per-channel room correction on channels ``[0]``/``[1]``, the
active-speaker 2→N driver split — still consumes two channels, so it
composes unchanged. (Live weaving into an active-speaker config is P1.3.)

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
(CamillaDSP's native ``BiquadCombo`` ``LinkwitzRileyLowpass`` at
``order: 4``, via the shared ``emit_linkwitz_riley``), default 80 Hz —
the de-facto consumer-AVR sub corner. Main-speaker bass-management highpass (to
unload <80 Hz from the L/R speakers) is a deliberate V1 non-goal: the
mains stay full-range and the sub ADDS low end, so 20–80 Hz is
reproduced by BOTH — expect an audible low-end lift and a phase-overlap
region until bass management lands. Simple and safe; revisit if a
household wants true bass management.
"""
from __future__ import annotations

from dataclasses import dataclass

from jasper.camilla_emit import (
    CHANNEL_SELECT_MIXER,
    emit_channel_select_mixer,
    emit_linkwitz_riley,
)
from jasper.camilla_emit import (
    MONO_SUM_GAIN_DB as MONO_SUM_GAIN_DB,  # re-export for this module's importers
)
from jasper.multiroom.config import ALLOWED_CHANNELS

# ``CHANNEL_SELECT_MIXER`` (the mixer name) and ``MONO_SUM_GAIN_DB`` (the
# clip-safe -6.02 dB mono-sum gain) are owned by the shared leaf
# ``jasper.camilla_emit`` and re-exported here so this module's existing
# importers/tests keep working; the channel-select *recipe* (sources + the 2->2
# mixer) is now the shared ``emit_channel_select_mixer`` so the active-speaker
# follower path and this member-config path can never drift apart.

# Sub crossover corner. 80 Hz is the standard consumer crossover (THX /
# most AV receivers default here). Tunable via build_channel_split(...).
DEFAULT_CROSSOVER_HZ = 80.0

# A 4th-order Linkwitz-Riley lowpass (-6 dB at fc, 24 dB/octave) is the
# standard sub slope. Emitted as CamillaDSP's NATIVE BiquadCombo
# LinkwitzRileyLowpass (jasper.camilla_emit.emit_linkwitz_riley) — the
# same primitive the active-speaker crossovers use, not a hand-cascaded
# pair of Biquad Lowpass sections.
_CROSSOVER_ORDER = 4
SUB_CROSSOVER_FILTER = "sub_crossover"


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

    # Both output channels carry the same content (see channel_select_sources).
    mixer_block = emit_channel_select_mixer(channel)

    filter_block = ""
    filter_chain_names: tuple[str, ...] = ()
    if channel == "sub":
        if crossover_hz <= 0:
            raise ValueError(
                f"crossover_hz must be positive, got {crossover_hz!r}"
            )
        filter_block = "\n".join(
            emit_linkwitz_riley(
                SUB_CROSSOVER_FILTER,
                highpass=False,
                freq_hz=crossover_hz,
                order=_CROSSOVER_ORDER,
            )
        )
        filter_chain_names = (SUB_CROSSOVER_FILTER,)

    return ChannelSplit(
        channel=channel,
        mixer_block=mixer_block,
        mixer_name=CHANNEL_SELECT_MIXER,
        filter_block=filter_block,
        filter_chain_names=filter_chain_names,
        pipeline_mixer_step=_pipeline_mixer_step(),
    )


def _augment_names_line(line: str, extra: tuple[str, ...]) -> str:
    """Append `extra` filter names to a pipeline ``names: [...]`` line,
    preserving indent. ``names: []`` -> ``names: [extra]``; ``names: [a, b]``
    -> ``names: [a, b, extra]``."""
    indent = line[: len(line) - len(line.lstrip())]
    lb, rb = line.index("["), line.rindex("]")
    existing = [n.strip() for n in line[lb + 1 : rb].split(",") if n.strip()]
    return f"{indent}names: [{', '.join(existing + list(extra))}]"


# The pipeline Mixer step the channel_select must run immediately after — the
# Ducker's identity fader. Owned by the generators (not this module), so it is
# a string anchor, not a constant import.
_MASTER_GAIN_STEP = "master_gain"


def _validate_woven(woven: str, split: ChannelSplit) -> None:
    """Parse the woven config and assert the splice landed CORRECTLY. A
    mis-spliced DSP config would silence or mis-route the speaker, so fail LOUD
    here rather than hand CamillaDSP a broken config.

    Checks: (1) 2-channel only — the splice appends the crossover to EACH
    per-channel Filter and routes a 2→2 ``channel_select``; a multi-channel
    (e.g. active-speaker) config would mis-apply, so refuse it (active-speaker
    weave is separate future work); (2) ``channel_select`` is in ``mixers:``;
    (3) it runs in the pipeline IMMEDIATELY after ``master_gain`` (position, not
    just presence — it must apply after the Ducker fader, before per-channel
    correction); (4) the sub crossover is in ``filters:`` for a sub."""
    import yaml

    try:
        doc = yaml.safe_load(woven)
    except yaml.YAMLError as e:  # pragma: no cover - defensive
        raise ValueError(f"channel-split weave produced invalid YAML: {e}") from e
    doc = doc or {}

    # (1) 2-channel guard. Require channels == 2 on BOTH sides — a missing
    # `channels` (None) is rejected too, not waved through: the splice routes a
    # 2→2 mixer and appends to per-channel [0]/[1] Filters, so anything but an
    # explicit 2-channel config could mis-apply (active-speaker / multi-driver
    # weave is future work, not this path).
    devices = doc.get("devices") or {}
    for side in ("capture", "playback"):
        ch = (devices.get(side) or {}).get("channels")
        if ch != 2:
            raise ValueError(
                "channel-split weave supports 2-channel configs only; "
                f"devices.{side}.channels={ch!r} (expected 2)"
            )

    # (2) channel_select in mixers.
    if CHANNEL_SELECT_MIXER not in (doc.get("mixers") or {}):
        raise ValueError("channel-split weave: channel_select missing from mixers")

    # (3) channel_select runs immediately after master_gain in the pipeline.
    pipeline = doc.get("pipeline") or []
    mixer_idx = {
        s.get("name"): i
        for i, s in enumerate(pipeline)
        if isinstance(s, dict) and s.get("type") == "Mixer"
    }
    if CHANNEL_SELECT_MIXER not in mixer_idx:
        raise ValueError("channel-split weave: channel_select step missing from pipeline")
    mg, cs = mixer_idx.get(_MASTER_GAIN_STEP), mixer_idx[CHANNEL_SELECT_MIXER]
    if mg is None or cs != mg + 1:
        raise ValueError(
            "channel-split weave: channel_select must run immediately after "
            f"{_MASTER_GAIN_STEP} in the pipeline "
            f"({_MASTER_GAIN_STEP}@{mg}, channel_select@{cs})"
        )

    # (4) sub crossover present for a sub.
    if split.channel == "sub" and SUB_CROSSOVER_FILTER not in (doc.get("filters") or {}):
        raise ValueError("channel-split weave: sub_crossover missing from filters")


def weave_channel_split(config_yaml: str, split: ChannelSplit) -> str:
    """Splice a :class:`ChannelSplit` fragment into a JTS-generated CamillaDSP
    config and return the woven YAML.

    Performs the four operations documented on :class:`ChannelSplit`: insert
    the ``channel_select`` mixer under ``mixers:``; insert the sub crossover
    under ``filters:``; insert the ``channel_select`` Mixer step into the
    pipeline immediately AFTER the ``master_gain`` step (so it runs after the
    Ducker's fader, before the per-channel correction/EQ); and append the
    crossover to each per-channel ``Filter`` step's ``names:`` list (so the
    sub lowpass runs LAST, just before the DAC).

    PASSTHROUGH (``stereo`` / solo) returns ``config_yaml`` BYTE-FOR-BYTE — a
    solo speaker's config is untouched.

    Only ever runs on a JTS-generated config (auto-generated, so the
    ``mixers:`` / ``filters:`` / ``pipeline:`` section keys and the
    ``name: master_gain`` pipeline step are stable anchors). The result is
    parsed + structurally validated; a config missing the expected anchors
    raises ValueError rather than emitting a broken DSP config."""
    if split.is_passthrough:
        return config_yaml

    out: list[str] = []
    in_pipeline = False
    inserted_mixer = inserted_pipeline_step = False
    inserted_filter = not split.filter_block  # nothing to insert when no filter

    for line in config_yaml.split("\n"):
        is_top_level = bool(line) and not line[0].isspace()
        if is_top_level:
            in_pipeline = line.rstrip() == "pipeline:"

        out.append(line)
        rstripped = line.rstrip()
        stripped = line.strip()

        if is_top_level and rstripped == "mixers:":
            out.append(split.mixer_block)
            inserted_mixer = True
        elif is_top_level and split.filter_block and rstripped == "filters:":
            out.append(split.filter_block)
            inserted_filter = True
        elif in_pipeline and stripped == "name: master_gain":
            out.append(split.pipeline_mixer_step)
            inserted_pipeline_step = True
        elif in_pipeline and split.filter_chain_names and stripped.startswith("names:"):
            out[-1] = _augment_names_line(line, split.filter_chain_names)

    if not (inserted_mixer and inserted_pipeline_step and inserted_filter):
        raise ValueError(
            "channel-split weave failed: config missing expected anchors "
            f"(mixers={inserted_mixer} pipeline_master_gain={inserted_pipeline_step} "
            f"filters={inserted_filter})"
        )
    woven = "\n".join(out)
    _validate_woven(woven, split)
    return woven
