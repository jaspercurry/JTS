# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emit CamillaDSP configs for sound curves and preference EQ.

The generated config preserves the base JTS audio path and any existing
room-correction PEQs, then appends preference filters. That ordering is
intentional: room correction fixes the room; preference EQ shapes what
the listener likes after that correction.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.camilla_config_contract import (
    DEFAULT_CAPTURE_DEVICE,
    DEFAULT_CAPTURE_FORMAT,
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_PLAYBACK_FORMAT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_VOLUME_LIMIT_DB,
    PeqFilter,
    ensure_volume_limit_db,
    resolve_camilla_latency_for_devices,
    resolve_enable_rate_adjust,
)
from jasper.camilla_emit import (
    MONO_SUM_GAIN_DB,
    emit_gain_filter,
    emit_master_gain_pipeline,
    fmt,
    mono_sum_sources,
)
from jasper.camilla_stereo_prefix import build_stereo_prefix

from .profile import (
    SoundProfile,
    build_sound_filter_slots,
)

if TYPE_CHECKING:  # `jasper.output_topology` has no jasper imports, but keep
    # the runtime edge out of this hot emitter module all the same.
    from jasper.output_topology import OutputTopology

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = Path("/etc/camilladsp/outputd-cutover.yml")
# The PROGRAM's width: capture channels, the `master_gain` mixer's `in`, and the
# channels `mono_sum_sources()` sums. FIXED — `emit_master_gain_pipeline` is a
# deliberately 2-channel shape (the config contract is stereo-pinned).
FLAT_PROGRAM_WIDTH = 2
# The default OUTPUT width, stated once rather than re-derived by each caller
# that needs to know which physical outputs the emitted graph addresses. A wider
# graph is opt-in per call (`emit_sound_config(width=...)`).
FLAT_GRAPH_WIDTH = FLAT_PROGRAM_WIDTH
SOUND_CONFIG_NAME = "sound_current.yml"
SOUND_AUDITION_CONFIG_NAME = "sound_audition.yml"
_JTS_GENERATED_RE = re.compile(
    r"^(?:correction_[A-Za-z0-9]+_\d+|sound_current|sound_audition"
    r"|sound_snapshot_[A-Za-z0-9]+_\d+|sound_reset_[A-Za-z0-9]+_\d+"
    r"|sound_lean_current"
    r"|correction_measurement_[A-Za-z0-9]+_\d+"
    r"|grouping_leader|grouping_solo_restore|grouping_follower)\.yml$"
)

def _normalize_width(width: int) -> int:
    """Validate ``emit_sound_config(width=...)`` — the graph's OUTPUT width.

    Bounded by the ring's own channel accept-set: playback is Ring B
    (ADR-0100), so a width the transport cannot carry fails the ioplug open
    rather than degrading. Those bounds are imported from their owner and only
    OFF the default, keeping the runtime module out of an ordinary stereo
    emission (see this module's ``TYPE_CHECKING`` note); the default sitting
    inside them is test-pinned instead.
    """

    width = int(width)
    if width == FLAT_GRAPH_WIDTH:
        return width
    from jasper.active_speaker.runtime_contract import (
        MAX_RING_CHANNELS,
        MIN_RING_CHANNELS,
    )

    if not MIN_RING_CHANNELS <= width <= MAX_RING_CHANNELS:
        raise ValueError(
            f"width must be {MIN_RING_CHANNELS}..{MAX_RING_CHANNELS} playback "
            f"channels (the ring layout's accept-set); got {width}"
        )
    return width


def _normalize_program_dest_map(
    program_dest_map: Sequence[int] | None,
    *,
    width: int,
) -> tuple[int, ...] | None:
    """Validate ``emit_sound_config(program_dest_map=...)`` at the API boundary.

    Fail LOUD on the three ways a map would silently drop or double a program
    channel. ``runtime_contract.flat_graph_program_dest_map`` owns WHICH dests
    are right; this only refuses a map no graph could mean.
    """

    if program_dest_map is None:
        return None
    dests = tuple(int(dest) for dest in program_dest_map)
    if len(dests) != FLAT_PROGRAM_WIDTH:
        raise ValueError(
            f"program_dest_map must name one dest per program channel "
            f"({FLAT_PROGRAM_WIDTH}); got {len(dests)}"
        )
    if len(set(dests)) != len(dests):
        raise ValueError(
            f"program_dest_map must not send two program channels to one dest; "
            f"got {dests}"
        )
    out_of_range = sorted(dest for dest in dests if not 0 <= dest < width)
    if out_of_range:
        raise ValueError(
            f"program_dest_map dests must be playback channels in 0..{width - 1}; "
            f"got {', '.join(str(dest) for dest in out_of_range)}"
        )
    return dests


def _normalize_muted_outputs(
    muted_outputs: Iterable[int] | None,
    *,
    width: int,
) -> frozenset[int]:
    """Validate ``emit_sound_config(muted_outputs=...)`` at the API boundary.

    Fail LOUD on the two ways a caller can be wrong, because both would
    otherwise ship a graph nobody meant:

    * an index outside the graph's OUTPUT width — the emitted pipeline has no
      such channel, so the "mute" would be a filter defined and never wired,
      i.e. an output left emitting while the caller believed it silenced;
    * every channel muted — a wholly silent program graph. That is not this
      lane's answer for "the topology claims none of my outputs"; refusing the
      flat graph is (see
      ``jasper.active_speaker.runtime_contract.flat_graph_muted_outputs``,
      which returns empty rather than asking for silence).
    """

    if muted_outputs is None:
        return frozenset()
    channels = frozenset(int(index) for index in muted_outputs)
    if not channels:
        return frozenset()
    out_of_range = sorted(index for index in channels if not 0 <= index < width)
    if out_of_range:
        raise ValueError(
            "muted_outputs indexes must be playback channels in "
            f"0..{width - 1}; got "
            f"{', '.join(str(index) for index in out_of_range)}"
        )
    if len(channels) >= width:
        raise ValueError(
            "muted_outputs would mute every playback channel, emitting a "
            "wholly silent program graph; refuse the flat graph instead"
        )
    return channels


def _normalize_mono_fold_output(
    mono_fold_output: int | None,
    muted_channels: frozenset[int],
    *,
    width: int,
) -> int | None:
    """Validate ``emit_sound_config(mono_fold_output=...)`` at the API boundary.

    Fail LOUD when the fold's complement is not hard muted: the fold would sum
    the program onto the declared output AND leave raw program on one the
    household never declared, the exact emission #2179's mutes exist to
    prevent. :func:`flat_graph_channel_plan` derives both halves together, so a
    caller reaching this error is misrouted.

    An out-of-width index needs no separate check — it leaves an in-width
    channel unmuted and trips this same one.

    A fold onto a channel PAST the program is refused for a different reason:
    those channels carry no room/preference chain (only their mute), so the
    fold would land uncorrected program on a declared output — and the runtime
    contract, which judges mutes and not chains, would allow it.
    """

    if mono_fold_output is None:
        return None
    index = int(mono_fold_output)
    unmuted_complement = sorted(frozenset(range(width)) - {index} - muted_channels)
    if unmuted_complement:
        raise ValueError(
            f"mono_fold_output={index} requires every other playback channel "
            "to be hard muted, or the fold emits raw program on an output the "
            "topology does not assign; unmuted: "
            f"{', '.join(str(other) for other in unmuted_complement)}"
        )
    # After the complement check, so an out-of-width index keeps reporting the
    # unmuted channel it left behind rather than this narrower reason.
    if not 0 <= index < FLAT_PROGRAM_WIDTH:
        raise ValueError(
            f"mono_fold_output={index} is not a program channel "
            f"(0..{FLAT_PROGRAM_WIDTH - 1}); a wider graph's surplus channels "
            "carry no room/preference chain, so folding onto one would emit "
            "uncorrected program"
        )
    return index


def _program_dests(program_dest_map: Sequence[int] | None) -> tuple[int, ...]:
    """Which dest each program channel drives. ``None`` IS the identity — the
    absence is what keeps every non-composite emit byte-identical."""

    if program_dest_map is None:
        return tuple(range(FLAT_PROGRAM_WIDTH))
    return tuple(program_dest_map)


def _master_gain_mixer_yaml(
    mono_fold_output: int | None,
    *,
    width: int,
    program_dest_map: Sequence[int] | None = None,
) -> str:
    """The ``master_gain`` mixer block, under a caller-supplied ``mixers:`` key.

    This is the seam the graph WIDENS at — the same one the roleful emitter
    widens at (``jasper.active_speaker.camilla_yaml``'s split mixer, ``{in: 2,
    out: output_count}``). Both counts are derived, never spelled, so neither
    can outlive the width it described.

    Identity by default. ``mono_fold_output`` instead folds BOTH program
    channels onto that one output: a mono cabinet declares one full-range
    output on a 2-channel amp, so an identity mixer drops the program's other
    channel into an output it never declared. The feeds come from
    :func:`~jasper.camilla_emit.mono_sum_sources`, which owns the clip-safe
    L+R recipe and the reason for its gain — and is PROGRAM-bounded, so a wider
    graph still sums exactly two. The complement dest keeps its identity route
    and its terminal hard mute, which :func:`_normalize_mono_fold_output`
    refuses a fold without.

    A dest CARRYING NO PROGRAM has no input channel to copy, so it takes the
    parked graph's shape (``emit_active_speaker_parked_config``): one feed from
    program channel 0 at the hard-mute floor, an entry that changes the channel
    count without carrying program. Its terminal pipeline mute is the half the
    runtime contract re-proves. Which dests those are follows
    ``program_dest_map``: on a composite the program lands on one output per
    child (0 and 2 on a dual-Apple stereo box), so the dead dests are 1 and 3
    rather than everything past the program's own width.

    ``master_gain`` stops being identity here. That is safe for the Ducker,
    which claims the volume owner (``jasper.camilla.Ducker``) and never touches
    this mixer.

    The unity route is spelled ``gain: 0``, not ``fmt(0.0)``: that literal is
    the byte contract of every config already in the field (the golden fixtures
    pin it). Only the fold's and the surplus dests' gains go through the shared
    formatter.
    """

    def source(channel: int, gain: str, inverted: bool) -> str:
        return (
            f"{{ channel: {channel}, gain: {gain}, "
            f"inverted: {'true' if inverted else 'false'} }}"
        )

    def surplus_source() -> str:
        from jasper.active_speaker.camilla_yaml import STARTUP_MUTE_GAIN_DB

        return source(0, fmt(STARTUP_MUTE_GAIN_DB), False)

    program_dests = _program_dests(program_dest_map)
    lines = [
        "  master_gain:",
        f"    channels: {{ in: {FLAT_PROGRAM_WIDTH}, out: {width} }}",
        "    mapping:",
    ]
    for dest in range(width):
        if dest == mono_fold_output:
            sources = ", ".join(
                source(channel, fmt(gain_db), inverted)
                for channel, gain_db, inverted in mono_sum_sources()
            )
        elif dest in program_dests:
            sources = source(program_dests.index(dest), "0", False)
        else:
            sources = surplus_source()
        lines.append(f"      - dest: {dest}")
        lines.append(f"        sources: [{sources}]")
    return "\n".join(lines)


def _program_pipeline_yaml(
    left_names: Sequence[str],
    right_names: Sequence[str] | None,
    *,
    program_dests: tuple[int, ...],
) -> str:
    """The mixer step plus one ``Filter`` step per PROGRAM-CARRYING dest.

    The pipeline's Mixer runs FIRST, so these chains are downstream of the
    2->width widening and belong to DESTS, not to program channels. An indexed
    sink's two coincide, and delegating there keeps it byte-identical. On a
    composite the chain must FOLLOW the program to child B's output: left on
    dest 1 it would correct a dead output and leave the live one raw.
    """

    if program_dests == tuple(range(FLAT_PROGRAM_WIDTH)):
        return emit_master_gain_pipeline(left_names, right_names)
    chains = (
        list(left_names),
        list(left_names if right_names is None else right_names),
    )
    return "\n".join(
        ["  - type: Mixer", "    name: master_gain"]
        + [
            line
            for dest, names in zip(program_dests, chains)
            for line in (
                "  - type: Filter",
                f"    channels: [{dest}]",
                f"    names: [{', '.join(names)}]",
            )
        ]
    )


def emit_sound_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    room_peqs_right: list[PeqFilter] | None = None,
    channel_delays_ms: tuple[float, float] | None = None,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
    playback_device: str = DEFAULT_PLAYBACK_DEVICE,
    capture_format: str = DEFAULT_CAPTURE_FORMAT,
    playback_format: str | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    chunksize: int | None = None,
    queuelimit: int = 4,
    target_level: int | None = None,
    volume_limit_db: float = DEFAULT_VOLUME_LIMIT_DB,
    out_path: str | Path | None = None,
    profile_id: str | None = None,
    output_trim_db: float = 0.0,
    enable_rate_adjust: bool | None = None,
    playback_pipe_path: str | None = None,
    muted_outputs: Iterable[int] | None = None,
    mono_fold_output: int | None = None,
    width: int = FLAT_GRAPH_WIDTH,
    program_dest_map: Sequence[int] | None = None,
) -> str:
    """Build a CamillaDSP YAML config for the preference profile.

    ``room_peqs_right`` is the multi-room leader-bake axis: a DIFFERENT
    room correction per channel in ONE config — channel 0 gets
    ``room_peqs`` (the leader's seat), channel 1 gets ``room_peqs_right``
    (the follower's seat); preference EQ stays shared (taste, not seat).
    ``None`` (default — solo) duplicates ``room_peqs`` onto channel 1,
    **byte-identical** to before this parameter existed (the solo-impact
    contract). ``[]`` bakes a FLAT right room segment (an uncalibrated
    follower ships flat, never the wrong-room curve). Deliberately a
    2-channel axis, matching the stereo-pinned config contract.

    ``channel_delays_ms`` is the room/pair time-of-arrival axis that
    belongs with measured correction, not Snapcast transport sync. It is
    stereo-pinned (``(left_ms, right_ms)``), positive-only, and emitted as
    CamillaDSP ``Delay`` filters inside the per-room chain. ``None``
    (default — solo) and ``(0, 0)`` emit no delay filters, preserving the
    solo byte contract. Delays are for static acoustic alignment at the
    listening seat; Snapcast still owns distributed clock/transport sync.

    ``playback_pipe_path`` is the BONDED-LEADER playback axis: when set,
    the playback device becomes a CamillaDSP ``File`` sink writing the corrected
    stereo program to that FIFO (snapserver's pipe source) instead of
    the ALSA loopback. ``None`` (default — solo) is **byte-identical**
    to before this parameter existed (the solo-impact contract). The
    pipe sink's emitted ``format`` is ALWAYS ``DEFAULT_PIPE_SINK_FORMAT``
    (``jasper.camilla_config_contract``), a DIFFERENT axis from
    ``playback_format`` — snapserver's pipe source is a fixed-format wire
    contract (``sampleformat=48000:16:2``,
    ``jasper.multiroom.reconcile.snapserver_argv``), so the ALSA loopback
    lane's format can widen independently of the pipe. Three fail-loud
    guards: ``playback_format`` (default ``None`` — the loopback lane's
    ``DEFAULT_PLAYBACK_FORMAT``) is meaningless on a pipe sink, so passing
    it EXPLICITLY (a caller-supplied value, ``is not None``) alongside a
    pipe sink is refused — the two axes must not be conflated, and this is
    a pure presence check, never a value comparison against a mutable
    default (a caller who omits the argument entirely never trips it, no
    matter what ``DEFAULT_PLAYBACK_FORMAT`` currently resolves to); and a pipe
    sink refuses an explicit ``enable_rate_adjust=True`` — as does any sink
    the resolver below answers ``False`` for.

    ``enable_rate_adjust`` defaults to
    :func:`~jasper.camilla_config_contract.resolve_enable_rate_adjust`'s answer
    for the sink this call emits — see it for why the sink decides. It stays a
    parameter only as the lab/explicit seam, so a lab emit can set it; no live
    caller passes one.

    ``muted_outputs`` hard-mutes the named playback channels: each gets the
    repo's one mute idiom (a ``Gain`` at ``-120 dB`` with ``mute: true``)
    appended as the LAST filter in its chain, so it is terminal — nothing in
    this pipeline runs after it. It exists so a graph rendered for a topology
    that assigns fewer physical outputs than the stereo-pinned width cannot
    send full-range program to an output the household never declared.
    ``jasper.active_speaker.runtime_contract.flat_graph_muted_outputs`` owns
    WHICH channels those are; this emitter owns only how the mute is spelled.
    ``None`` / empty is **byte-identical** to before this parameter existed
    (the solo-impact contract). Muting EVERY channel is refused: a wholly
    silent program graph is never the right answer from here, and a caller
    that asked for one is misrouted (see ``flat_graph_muted_outputs``, which
    returns empty rather than requesting it).

    ``mono_fold_output`` folds BOTH program channels onto the named playback
    channel in the ``master_gain`` mixer (see :func:`_master_gain_mixer_yaml`
    for the gain rule). It PAIRS with ``muted_outputs`` and is refused without
    it; :func:`flat_graph_channel_plan` owns which channel folds where, this
    emitter only how the fold is spelled. ``None`` is **byte-identical** to
    before this parameter existed (the solo-impact contract).

    ``program_dest_map`` names which playback channel each PROGRAM channel
    drives, routing both the mixer's feeds and the per-dest filter chains.
    ``None`` (default) is the identity and is **byte-identical** to before this
    parameter existed (the solo-impact contract); a composite sink is the one
    shape needing another answer, since outputd deinterleaves the period across
    its child DACs.
    ``jasper.active_speaker.runtime_contract.flat_graph_program_dest_map`` owns
    WHICH dests those are. Mutually exclusive with ``mono_fold_output``, which
    collapses the program this spreads.

    ``width`` is the graph's OUTPUT width, and the bound the other channel
    axes here are checked against. The PROGRAM does not widen with it: capture
    and the ``master_gain`` mixer's ``in`` stay :data:`FLAT_PROGRAM_WIDTH`,
    because widening belongs in the mixer. Channels past the program carry no
    program chain, only their mute, so a wider graph is meaningful only with
    the matching ``muted_outputs``. Bounded by :func:`_normalize_width`, and
    refused alongside ``playback_pipe_path``. The default is
    **byte-identical** to before this parameter existed (the solo-impact
    contract)."""

    # Loud-output safety: refuse to emit a config whose master fader
    # could boost above full scale. Mirrors the active_speaker emitter.
    volume_limit_db = ensure_volume_limit_db(volume_limit_db)
    width = _normalize_width(width)
    # The sink this call actually emits: `None` is the clockless File sink, the
    # vocabulary both resolvers below read. `is not None`, the same predicate
    # the File-sink branch below decides on.
    sink_device = None if playback_pipe_path is not None else playback_device
    # G7 latency knobs; see resolve_camilla_latency_for_devices for why the
    # emitted devices decide the fallback.
    chunksize, target_level = resolve_camilla_latency_for_devices(
        capture_device=capture_device,
        playback_device=sink_device,
        chunksize=chunksize,
        target_level=target_level,
    )
    if channel_delays_ms is not None:
        if len(channel_delays_ms) != 2:
            raise ValueError("channel_delays_ms must be a (left_ms, right_ms) pair")
        left_delay, right_delay = channel_delays_ms
        for label, value in (("left", left_delay), ("right", right_delay)):
            if not math.isfinite(float(value)):
                raise ValueError(f"{label} channel delay must be finite")
            if float(value) < 0.0:
                raise ValueError(f"{label} channel delay must be positive-only")
        channel_delays_ms = (float(left_delay), float(right_delay))
        if channel_delays_ms != (0.0, 0.0) and room_peqs_right is None:
            raise ValueError(
                "channel_delays_ms requires room_peqs_right so the two "
                "speaker channels have distinct room chains"
            )

    # Bonded-leader pipe-sink guards (fail LOUD at the API boundary).
    if playback_pipe_path is not None:
        # D4 (wide-output-path program): the pipe sink's format is pinned to
        # DEFAULT_PIPE_SINK_FORMAT below, independently of playback_format —
        # snapserver's pipe wire is a fixed-format contract, so the axis is
        # not applicable to a pipe sink at all. GENUINELY explicit: a bare
        # ``is not None`` presence check on the CALLER-SUPPLIED argument, not
        # a value comparison against DEFAULT_PLAYBACK_FORMAT (a module
        # global read fresh at call time would silently diverge from this
        # parameter's def-time-bound default the moment the two constants
        # are no longer assigned in lockstep — SF1 from the PR-1 gate
        # review). A caller that omits the argument entirely (playback_format
        # stays None) never trips this, regardless of what
        # DEFAULT_PLAYBACK_FORMAT currently resolves to.
        if playback_format is not None:
            raise ValueError(
                "playback_pipe_path (bonded-leader pipe sink) is pinned to "
                f"DEFAULT_PIPE_SINK_FORMAT={DEFAULT_PIPE_SINK_FORMAT!r} — "
                "playback_format is not applicable to a pipe sink at all; "
                f"passing one explicitly (got {playback_format!r}) alongside "
                "playback_pipe_path is a caller bug, not a wire-format "
                "request; they are different axes"
            )
        if width != FLAT_PROGRAM_WIDTH:
            # `sampleformat=48000:16:2` (jasper.multiroom.reconcile.
            # snapserver_argv): a wider pipe is not a wider sink, it is a
            # frame-misaligned stream the reader cannot resynchronise.
            raise ValueError(
                "playback_pipe_path (bonded-leader pipe sink) is a fixed "
                f"{FLAT_PROGRAM_WIDTH}-channel wire; width={width} is not "
                "applicable to it"
            )
    # Resolve the sentinel AFTER the explicitness guard above (mirrors the
    # chunksize/target_level None-or-default pattern a few lines up): the
    # ALSA loopback branch below still needs a concrete playback_format even
    # though a pipe sink never reads this resolved value.
    if playback_format is None:
        playback_format = DEFAULT_PLAYBACK_FORMAT
    if enable_rate_adjust is None:
        enable_rate_adjust = resolve_enable_rate_adjust(sink_device)
    elif enable_rate_adjust and not resolve_enable_rate_adjust(sink_device):
        raise ValueError(
            "enable_rate_adjust=True on a sink CamillaDSP cannot rate-adjust "
            f"({'a File sink' if sink_device is None else sink_device}); pass "
            "enable_rate_adjust=False or omit it"
        )
    muted_channels = _normalize_muted_outputs(muted_outputs, width=width)
    mono_fold_output = _normalize_mono_fold_output(
        mono_fold_output, muted_channels, width=width
    )
    dest_map = _normalize_program_dest_map(program_dest_map, width=width)
    if dest_map is not None and mono_fold_output is not None:
        # A fold sums BOTH program channels onto one output; a non-identity map
        # exists precisely because they belong on two. The plan derives them
        # together and never asks for both, so a caller here is misrouted.
        raise ValueError(
            "mono_fold_output and program_dest_map are mutually exclusive: a "
            "fold collapses the program the map is spreading"
        )
    program_dests = _program_dests(dest_map)
    # The shared stereo-prefix builder (jasper.camilla_stereo_prefix) owns the
    # room-PEQ -> headroom -> preamp -> preference assembly. Build the list once
    # and reuse it for the summary log below.
    sound_filters = build_sound_filter_slots(profile)
    filter_yaml, chain_names, chain_names_right, trim_db = build_stereo_prefix(
        sound_filters,
        room_peqs or [],
        room_peqs_right=room_peqs_right,
        output_trim_db=output_trim_db,
        channel_delays_ms=channel_delays_ms,
    )
    # Structure is the shared primitive; this module owns only which
    # names go in each chain (room L/R segments + the shared tail).
    if muted_channels:
        # The repo's ONE hard-mute idiom, imported from its owner rather than
        # respelled here — the runtime contract re-proves the emitted graph with
        # the same name and gain, so a drifted copy would silently stop proving.
        # Lazy because jasper.active_speaker.camilla_yaml imports THIS module at
        # module scope (emit_sound_config); a top-level edge back would be
        # circular. Same lazy-import idiom runtime_contract uses for its own
        # reverse edges.
        from jasper.active_speaker.camilla_yaml import (
            STARTUP_MUTE_GAIN_DB,
            output_commission_mute_name,
        )

        # Append each mute LAST in its channel's chain. CamillaDSP applies a
        # step's filters in order and these per-channel steps are the pipeline's
        # last, so the mute is terminal — nothing downstream can re-amplify it.
        # `chain_names_right is None` normally means "duplicate left onto
        # channel 1"; muting only one channel needs the two chains spelled out,
        # so materialise the duplicate here rather than teaching the shared
        # pipeline primitive about mutes.
        left_names = list(chain_names)
        right_names = list(
            chain_names if chain_names_right is None else chain_names_right
        )
        if program_dests[0] in muted_channels:
            left_names.append(output_commission_mute_name(program_dests[0]))
        if program_dests[1] in muted_channels:
            right_names.append(output_commission_mute_name(program_dests[1]))
        filter_yaml = "\n".join(
            [filter_yaml]
            + [
                line
                for index in sorted(muted_channels)
                for line in emit_gain_filter(
                    output_commission_mute_name(index),
                    STARTUP_MUTE_GAIN_DB,
                    mute=True,
                )
            ]
        )
        pipeline_yaml = _program_pipeline_yaml(
            left_names, right_names, program_dests=program_dests
        )
        # A channel CARRYING NO PROGRAM has no chain to append to, so its mute is
        # its whole Filter step. Appended here rather than by widening
        # `emit_master_gain_pipeline`, a deliberately 2-channel shape (see its
        # docstring); empty at the default width, and on a composite it is the
        # dead dest BETWEEN the program's two (dest 1) as well as the trailing
        # one, which is why the test is membership rather than `>= width`.
        pipeline_yaml = "\n".join(
            [pipeline_yaml]
            + [
                line
                for index in sorted(muted_channels)
                if index not in program_dests
                for line in (
                    "  - type: Filter",
                    f"    channels: [{index}]",
                    f"    names: [{output_commission_mute_name(index)}]",
                )
            ]
        )
    else:
        pipeline_yaml = _program_pipeline_yaml(
            chain_names, chain_names_right, program_dests=program_dests
        )
    rate_adjust_literal = "true" if enable_rate_adjust else "false"
    header_id = f" (id={profile_id})" if profile_id else ""
    # Playback sink: ALSA loopback (solo — the default, byte-identical)
    # or the bonded-leader File/pipe sink feeding snapserver. Identical
    # indentation so the surrounding template is sink-agnostic.
    if playback_pipe_path is not None:
        # D4: pinned to DEFAULT_PIPE_SINK_FORMAT, NOT playback_format — see
        # the guard above and the constant's own comment
        # (jasper.camilla_config_contract).
        playback_yaml = f"""  playback:
    type: File
    channels: {width}
    filename: "{playback_pipe_path}"
    format: {DEFAULT_PIPE_SINK_FORMAT}"""
    else:
        playback_yaml = f"""  playback:
    type: Alsa
    channels: {width}
    device: "{playback_device}"
    format: {playback_format}"""
    capture_yaml = f"""  capture:
    type: Alsa
    channels: {FLAT_PROGRAM_WIDTH}
    device: "{capture_device}"
    format: {capture_format}"""
    # The header's mixer sentence tracks the mixer it describes. Byte-identical
    # when nothing folds; a folded graph must not carry the identity claim.
    mixer_note = (
        "# preference-EQ filters. The `master_gain` mixer remains identity so\n"
        "# the Ducker contract holds."
        if mono_fold_output is None
        else (
            "# preference-EQ filters. The `master_gain` mixer folds both program\n"
            f"# channels onto output {mono_fold_output} at "
            f"{fmt(MONO_SUM_GAIN_DB)} dB each (clip-safe mono sum);\n"
            "# the Ducker drives CamillaDSP's main_volume fader, not this mixer."
        )
    )
    yaml = f"""---
# Auto-generated JTS DSP config{header_id}.
# Source: jasper.sound.camilla_yaml.emit_sound_config
# DO NOT HAND-EDIT — update http://jts.local/sound/room/ or
# http://jts.local/sound/eq/ instead.
#
# Structure mirrors deploy/camilladsp/outputd-cutover.yml.
# Room-correction PEQs, when present, run before sound-curve /
{mixer_note}
# output_trim_db={trim_db:.3f}

devices:
  samplerate: {sample_rate}
  chunksize: {chunksize}
  queuelimit: {queuelimit}
  target_level: {target_level}
  volume_limit: {volume_limit_db:.1f}
  enable_rate_adjust: {rate_adjust_literal}
{capture_yaml}
{playback_yaml}

filters:
{filter_yaml}

mixers:
{_master_gain_mixer_yaml(mono_fold_output, width=width, program_dest_map=dest_map)}

pipeline:
{pipeline_yaml}
"""

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        atomic_write_text(out_path, yaml, mode=CONFIG_FILE_MODE)
        right_note = (
            f" room_peqs_right={len(room_peqs_right)}"
            if room_peqs_right is not None
            else ""
        )
        logger.info(
            "wrote sound config: %s (room_peqs=%d%s sound_filters=%d output_trim=%.3f)",
            out_path,
            len(room_peqs or []),
            right_note,
            len(sound_filters),
            trim_db,
        )
    return yaml


@dataclass(frozen=True)
class FlatChannelPlan:
    """How a ``width``-wide flat graph addresses the topology's outputs.

    Three answers that are only correct together, so they are derived together
    (:func:`flat_graph_channel_plan`). The empty plan is the golden stereo graph.
    """

    muted_outputs: frozenset[int] = frozenset()
    mono_fold_output: int | None = None
    # None means the identity: program channel i drives dest i. Only a composite
    # sink resolves anything else today.
    program_dest_map: tuple[int, ...] | None = None


def flat_graph_channel_plan(
    topology: OutputTopology | None = None,
    *,
    width: int,
) -> FlatChannelPlan:
    """The mute set and the mono fold for a ``width``-wide flat graph.

    ``jasper.active_speaker.runtime_contract.flat_graph_muted_outputs`` stays
    the SSOT for which channels are unclaimed; this function adds the second
    half of the mono answer — which channel the program folds ONTO — and reads
    the topology once so the two cannot describe different boxes.

    The third answer is WHERE THE PROGRAM LANDS, delegated whole to
    ``jasper.active_speaker.runtime_contract.flat_graph_program_dest_map``: the
    identity on an indexed sink, and one output per child on a composite whose
    children declare exactly one ``full_range`` output each. It is reported as
    ``None`` for the identity so every non-composite emit stays byte-identical.

    The fold is offered only for an explicit 1-channel full-range layout
    (``CONTRACT_NORMAL_MONO_FULL_RANGE``) that assigns exactly one output, and
    only when the SSOT's mute set is that output's exact complement. That last
    equality is the load-bearing one: it is how every case the SSOT withholds
    muting for — unconfigured, roleful/protected, corrupt, or a sink whose
    program-to-output mapping does not resolve — withholds the fold too, without
    this function re-deriving (or drifting from) any of those rules. A composite
    mono box in particular must NOT fold here: outputd owns the program's
    fan-out across its child DACs, and the mapping resolves one output per
    child, which a single-output mono layout is not.

    One case the mute set cannot speak for, so it is withheld explicitly: a
    single full-range output sitting PAST the program's channels (a mono box on
    output 2 of an 8-output DAC). The complement equality holds there, but only
    a program channel can carry a fold — :func:`_normalize_mono_fold_output`
    refuses the rest — so offering one would turn a render into an exception
    (``render_flat_cutover_configs`` has no guard behind it; install would
    abort) rather than a typed refusal. Withheld the same way the composite case
    is, and for the same reason: this lane cannot say which output that program
    belongs on. Deciding that is the deferred mapping work.

    Fails SOFT — the empty plan — on a corrupt topology, matching the SSOT.
    The graph is checked either way: ``classify_camilla_graph`` and
    ``safe_graph_for_current_topology`` both fail closed on that topology.
    """

    # Lazy: the active-speaker package imports THIS module at module scope, so
    # a top-level edge back would be circular.
    from jasper.active_speaker.runtime_contract import (
        CONTRACT_NORMAL_MONO_FULL_RANGE,
        classify_output_contract,
        flat_full_range_outputs,
        flat_graph_muted_outputs,
        flat_graph_program_dest_map,
    )
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        if topology is None:
            topology = load_output_topology_strict()
        contract = classify_output_contract(topology)
    except OutputTopologyError:
        return FlatChannelPlan()
    muted = flat_graph_muted_outputs(topology, width=width)
    assigned = flat_full_range_outputs(contract)
    dest_map = flat_graph_program_dest_map(topology, contract, width=width)
    if (
        contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE
        and len(assigned) == 1
        and assigned <= frozenset(range(FLAT_PROGRAM_WIDTH))
        and muted == frozenset(range(width)) - assigned
    ):
        return FlatChannelPlan(
            muted_outputs=muted, mono_fold_output=next(iter(assigned))
        )
    # Reported only when it is NOT the identity: the emitter reads absence as
    # "program channel i drives dest i", so passing the identity explicitly
    # would be a second spelling of the same graph.
    if dest_map == tuple(range(FLAT_PROGRAM_WIDTH)):
        dest_map = None
    return FlatChannelPlan(muted_outputs=muted, program_dest_map=dest_map)


def emit_flat_outputd_cutover_config(
    *,
    out_path: str | Path | None = None,
    topology: OutputTopology | None = None,
    width: int = FLAT_GRAPH_WIDTH,
) -> str:
    """Emit the flat outputd startup graph through the production generator.

    Fresh plain-flat installs boot through this graph. Keeping it on the same
    emitter as ordinary sound configs means the active DAC profile's latency
    floor reaches first boot without adding a second Camilla/outputd path.

    WIDTH-MATCHED to the saved output topology. The emitted pipeline is
    ``width`` channels wide — the stereo default for every production caller
    (outputd negotiates the DAC's own width — the InnoMaker's two — so
    narrowing the ALSA device is not on the table) — and every channel the
    topology does not assign to a ``full_range`` output is hard muted, at any
    width. A mono topology therefore boots a graph that CANNOT put
    program audio on the output it never declared, instead of one that does so
    and is refused at the statefile guard. That refusal is real and stays:
    ``jasper.active_speaker.runtime_contract`` re-proves the mutes off this
    YAML, so an unmuted surplus channel is still blocked.

    A mono topology also FOLDS: muting alone would leave a mono cabinet playing
    only the program's left channel, so both channels are summed onto its one
    declared output. Mute and fold come from one :func:`flat_graph_channel_plan`
    read of the topology.

    ``topology`` (any ``OutputTopology``) is a test seam; production reads the
    saved topology. ``width`` has no production caller off its default yet — the
    reconciler that will pass one for a passive composite is a later change —
    but a wide graph now MEANS something on a composite sink: the mixer's
    dest-to-output mapping is decided (:func:`flat_graph_channel_plan` resolves
    one output per child), so a 4-wide dual-Apple stereo graph puts the program
    on outputs 0 and 2 and terminally mutes 1 and 3. A sink whose mapping does
    NOT resolve still gets no mute and no fold, and the runtime contract refuses
    a wide graph from it rather than counting live channels it cannot place.
    """

    from jasper.fanin_coupling import RING_CAMILLA_GEOMETRY, resolve_ring_wire

    # BOTH HALVES ARE THE RING (ADR-0100) — capture is Ring A and playback is
    # Ring B, both off the module defaults — so this graph passes the certified
    # ring geometry explicitly rather than resolving a box floor. The ioplug pins
    # the ring's period bytes min==max, so a 1024-frame chunk cannot negotiate
    # either ring.
    #
    # The wire comes from the same resolver `capture_kwargs_for_coupling` reads,
    # so the seeded startup graph and a live `/sound/` re-emit cannot declare
    # different widths for the same box. Resolved with NO topology: every
    # production path here is the SOLO-STEREO flat graph (a roleful box is
    # seeded from the driver-domain emitters instead), so there is no
    # per-topology width to ask for. An off-default `width` would need a ring
    # wide enough to carry it as well; establishing that pairing belongs to
    # whoever first passes one.
    wire = resolve_ring_wire()
    plan = flat_graph_channel_plan(topology, width=width)

    return emit_sound_config(
        SoundProfile(enabled=False),
        capture_format=wire.sample_format,
        playback_format=wire.sample_format,
        **RING_CAMILLA_GEOMETRY,
        muted_outputs=plan.muted_outputs,
        mono_fold_output=plan.mono_fold_output,
        program_dest_map=plan.program_dest_map,
        width=width,
        out_path=out_path,
    )


# The flat cutover config is read by whoever loads it (CamillaDSP, the runtime
# contract's classifier, the camillagui config browser), not only by a
# group-jasper daemon, so it is world-readable — wider than the 0640 the
# ordinary sound configs get from `CONFIG_FILE_MODE`.
FLAT_CUTOVER_MODE = 0o644


@dataclass(frozen=True)
class RenderedFlatConfig:
    """One flat startup config as written (or found already correct)."""

    path: Path
    text: str
    changed: bool


@dataclass(frozen=True)
class FlatCutoverRender:
    """The result of one :func:`render_flat_cutover_configs` pass."""

    config_dir: Path
    rendered: tuple[RenderedFlatConfig, ...]

    @property
    def changed(self) -> bool:
        return any(item.changed for item in self.rendered)


def render_flat_cutover_configs(
    *,
    config_dir: str | Path | None = None,
    topology: OutputTopology | None = None,
) -> FlatCutoverRender:
    """Write the flat startup config, WRITE-ON-CHANGE. The single writer.

    ``outputd-cutover.yml`` — the flat startup graph, width-matched to the saved
    topology and coupled to the rings at both halves (ADR-0100). There is one
    file: the ``shm_ring`` sibling this used to write beside it collapsed into
    it when the ring became the only transport, so a deploy or CamillaDSP
    restart can no longer re-seed a box onto a graph its transport cannot serve.

    THREE callers must produce a byte-identical file or the box's graph depends
    on which one ran last: ``deploy/install.sh`` at deploy time,
    ``jasper-audio-hardware-reconcile`` at boot / udev / topology-save, and
    ``jasper-output-topology-reset``. They all reach this one function through
    ``jasper-sound render-flat-cutover``, so there is no second writer and no
    second spelling of the file mode.

    Write-on-change because the reconciler runs on every boot and every sound-card
    event: an unconditional write would churn mtimes and make "did the graph
    change?" unanswerable from the filesystem. Same discipline as the reconciler's
    own ``set_env_var_if_changed``. The mode is asserted on every pass even when
    the bytes match, so a file left narrow by an older writer is repaired.

    Never raises for an unreadable existing CONFIG file — that is simply
    "different". Write failures DO raise: the caller decides whether a failed
    render is fatal (install) or best-effort (the reset's convergence step).

    **A corrupt saved TOPOLOGY raises ``OutputTopologyError`` and writes
    nothing.** This function owns that distinction because it is the one that
    loads the topology, so missing / corrupt / ok is answered in a single place:

    * **missing** — nothing is declared, so nothing is undeclared. Renders the
      golden unmuted graph, which is correct for a fresh box.
    * **corrupt** — refuse. ``flat_graph_channel_plan`` fails SOFT (mutes
      nothing) because its other callers all have a guard behind them, but the
      reconciler has none: it renders on boot / udev / topology-save, and
      CamillaDSP loads the cutover from its statefile on its next start with no
      ordering to this. A soft failure there would overwrite a healthy
      width-matched graph with an unmuted one and log success — silently, in the
      hazard direction. Keeping the previous file is the fail-closed answer.
    """

    if topology is None:
        # Explicitly, so a corrupt artifact raises HERE rather than being
        # swallowed downstream into "mute nothing". A missing file returns an
        # empty draft (the golden case) and does not raise.
        from jasper.output_topology import load_output_topology_strict

        topology = load_output_topology_strict()
    directory = Path(config_dir) if config_dir is not None else BASE_CONFIG_PATH.parent
    entries: list[tuple[str, str]] = [
        (BASE_CONFIG_PATH.name, emit_flat_outputd_cutover_config(topology=topology)),
    ]
    rendered: list[RenderedFlatConfig] = []
    for name, text in entries:
        path = directory / name
        try:
            unchanged = path.read_text(encoding="utf-8") == text
        except OSError:
            unchanged = False
        if not unchanged:
            atomic_write_text(path, text, mode=FLAT_CUTOVER_MODE)
        else:
            path.chmod(FLAT_CUTOVER_MODE)
        rendered.append(
            RenderedFlatConfig(path=path, text=text, changed=not unchanged)
        )
    return FlatCutoverRender(config_dir=directory, rendered=tuple(rendered))


def sound_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / SOUND_CONFIG_NAME


def sound_audition_config_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / SOUND_AUDITION_CONFIG_NAME


def is_base_config(path: str | Path | None) -> bool:
    return Path(path) == BASE_CONFIG_PATH if path else False


def is_jts_generated_config(
    path: str | Path | None,
    *,
    config_dir: str | Path,
) -> bool:
    if not path:
        return False
    cfg_path = Path(path)
    return cfg_path.parent == Path(config_dir) and bool(
        _JTS_GENERATED_RE.match(cfg_path.name)
    )


def extract_room_peqs_from_config_text(text: str) -> list[PeqFilter]:
    """Extract generated room-correction PEQs from a CamillaDSP YAML.

    We intentionally avoid a YAML runtime dependency here. The parser is
    scoped to historical correction configs and the deterministic shapes
    emitted by this module.

    SCOPE: extracts the SOLO/left chain only (``peq_*`` / ``room_peq_*``);
    right-channel leader-bake filters (``peq_r*`` / ``room_peq_r*``) are
    deliberately NOT matched — this extractor serves the solo re-emit
    path. The multi-room leader apply path must compose from STORED
    per-speaker profiles, never by re-extracting a woven config.
    """

    try:
        filters_text = text.split("\nfilters:\n", 1)[1].split("\nmixers:\n", 1)[0]
    except IndexError:
        return []

    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    for line in filters_text.splitlines():
        match = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if match:
            if current_name is not None:
                blocks.append((current_name, "\n".join(current_lines)))
            current_name = match.group(1)
            current_lines = []
            continue
        if current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current_lines)))

    # No silent failure paths: if this config carries leader-bake
    # right-channel filters, extraction alone CANNOT reproduce it — a
    # re-emit from just this result would silently DROP the follower's
    # correction. Warn loudly; the leader apply path must compose from
    # stored profiles.
    if any(
        re.fullmatch(r"(?:room_)?peq_r\d+", name) for name, _ in blocks
    ):
        logger.warning(
            "event=sound.extract_room_peqs result=right_channel_ignored "
            "detail=leader-bake right-channel (*_r*) filters present and "
            "NOT extracted; re-emitting from this extraction alone would "
            "drop the follower's correction — compose from stored profiles"
        )

    peqs: list[PeqFilter] = []
    for name, block in blocks:
        if not (re.fullmatch(r"peq_\d+", name) or re.fullmatch(r"room_peq_\d+", name)):
            continue
        if "type: Biquad" not in block or not re.search(
            r"^\s+type:\s+Peaking\s*$", block, re.M
        ):
            continue
        values: dict[str, float] = {}
        for key in ("freq", "q", "gain"):
            match = re.search(rf"^\s+{key}:\s+([-+]?\d+(?:\.\d+)?)\s*$", block, re.M)
            if not match:
                break
            values[key] = float(match.group(1))
        else:
            peqs.append(
                PeqFilter(freq=values["freq"], q=values["q"], gain=values["gain"])
            )
    return peqs


def extract_room_peqs_from_config(path: str | Path | None) -> list[PeqFilter]:
    if not path:
        return []
    cfg_path = Path(path)
    try:
        return extract_room_peqs_from_config_text(cfg_path.read_text())
    except FileNotFoundError:
        logger.info("active CamillaDSP config path not readable: %s", cfg_path)
    except OSError as e:
        logger.warning("could not inspect CamillaDSP config %s: %s", cfg_path, e)
    return []
