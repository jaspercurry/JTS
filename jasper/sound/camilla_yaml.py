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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.atomic_io import atomic_write_text
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
    resolve_camilla_chunksize,
    resolve_camilla_target_level,
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
    build_sound_filters,
)

if TYPE_CHECKING:  # `jasper.output_topology` has no jasper imports, but keep
    # the runtime edge out of this hot emitter module all the same.
    from jasper.output_topology import OutputTopology

logger = logging.getLogger(__name__)

BASE_CONFIG_PATH = Path("/etc/camilladsp/outputd-cutover.yml")
# The program lane's fixed playback width. `emit_master_gain_pipeline` is a
# deliberately 2-channel shape (the config contract is stereo-pinned; 2.1's
# 3-channel stream generalises it WITH that contract, not alone), so the width
# is stated once here rather than re-derived by each caller that needs to know
# which physical outputs the emitted graph reaches.
FLAT_GRAPH_WIDTH = 2
SOUND_CONFIG_NAME = "sound_current.yml"
SOUND_AUDITION_CONFIG_NAME = "sound_audition.yml"
_JTS_GENERATED_RE = re.compile(
    r"^(?:correction_[A-Za-z0-9]+_\d+|sound_current|sound_audition"
    r"|sound_snapshot_[A-Za-z0-9]+_\d+|sound_reset_[A-Za-z0-9]+_\d+"
    r"|sound_lean_current"
    r"|correction_measurement_[A-Za-z0-9]+_\d+"
    r"|grouping_leader|grouping_solo_restore|grouping_follower)\.yml$"
)

def _normalize_muted_outputs(muted_outputs: Iterable[int] | None) -> frozenset[int]:
    """Validate ``emit_sound_config(muted_outputs=...)`` at the API boundary.

    Fail LOUD on the two ways a caller can be wrong, because both would
    otherwise ship a graph nobody meant:

    * an index outside the stereo-pinned width — the emitted pipeline has no
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
    out_of_range = sorted(index for index in channels if not 0 <= index < FLAT_GRAPH_WIDTH)
    if out_of_range:
        raise ValueError(
            "muted_outputs indexes must be playback channels in "
            f"0..{FLAT_GRAPH_WIDTH - 1}; got "
            f"{', '.join(str(index) for index in out_of_range)}"
        )
    if len(channels) >= FLAT_GRAPH_WIDTH:
        raise ValueError(
            "muted_outputs would mute every playback channel, emitting a "
            "wholly silent program graph; refuse the flat graph instead"
        )
    return channels


def _normalize_mono_fold_output(
    mono_fold_output: int | None,
    muted_channels: frozenset[int],
) -> int | None:
    """Validate ``emit_sound_config(mono_fold_output=...)`` at the API boundary.

    Fail LOUD when the fold's complement is not hard muted: the fold would sum
    the program onto the declared output AND leave raw program on one the
    household never declared, the exact emission #2179's mutes exist to
    prevent. :func:`flat_graph_channel_plan` derives both halves together, so a
    caller reaching this error is misrouted.

    An out-of-width index needs no separate check — it leaves an in-width
    channel unmuted and trips this same one.
    """

    if mono_fold_output is None:
        return None
    index = int(mono_fold_output)
    unmuted_complement = sorted(
        frozenset(range(FLAT_GRAPH_WIDTH)) - {index} - muted_channels
    )
    if unmuted_complement:
        raise ValueError(
            f"mono_fold_output={index} requires every other playback channel "
            "to be hard muted, or the fold emits raw program on an output the "
            "topology does not assign; unmuted: "
            f"{', '.join(str(other) for other in unmuted_complement)}"
        )
    return index


def _master_gain_mixer_yaml(mono_fold_output: int | None) -> str:
    """The ``master_gain`` mixer block, under a caller-supplied ``mixers:`` key.

    Identity by default. ``mono_fold_output`` instead folds BOTH program
    channels onto that one output: a mono cabinet declares one full-range
    output on a 2-channel amp, so an identity mixer drops the program's other
    channel into an output it never declared. The feeds come from
    :func:`~jasper.camilla_emit.mono_sum_sources`, which owns the clip-safe
    L+R recipe and the reason for its gain. The complement dest keeps its
    identity route and its terminal hard mute, which
    :func:`_normalize_mono_fold_output` refuses a fold without.

    ``master_gain`` stops being identity here. That is safe for the Ducker,
    which claims the volume owner (``jasper.camilla.Ducker``) and never touches
    this mixer.

    The unity route is spelled ``gain: 0``, not ``fmt(0.0)``: that literal is
    the byte contract of every config already in the field (the golden fixtures
    pin it). Only the fold's gains go through the shared formatter.
    """

    def source(channel: int, gain: str, inverted: bool) -> str:
        return (
            f"{{ channel: {channel}, gain: {gain}, "
            f"inverted: {'true' if inverted else 'false'} }}"
        )

    lines = [
        "  master_gain:",
        "    channels: { in: 2, out: 2 }",
        "    mapping:",
    ]
    for dest in range(FLAT_GRAPH_WIDTH):
        if dest == mono_fold_output:
            sources = ", ".join(
                source(channel, fmt(gain_db), inverted)
                for channel, gain_db, inverted in mono_sum_sources()
            )
        else:
            sources = source(dest, "0", False)
        lines.append(f"      - dest: {dest}")
        lines.append(f"        sources: [{sources}]")
    return "\n".join(lines)


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
    enable_rate_adjust: bool = True,
    playback_pipe_path: str | None = None,
    muted_outputs: Iterable[int] | None = None,
    mono_fold_output: int | None = None,
) -> str:
    """Build a CamillaDSP YAML config for the preference profile.

    ``room_peqs_right`` is the multi-room leader-bake axis
    (docs/HANDOFF-multiroom.md §2 "Canonical signal flow"): a DIFFERENT
    room correction per channel in ONE config — channel 0 gets
    ``room_peqs`` (the leader's seat), channel 1 gets ``room_peqs_right``
    (the follower's seat); preference EQ stays shared (taste, not seat).
    ``None`` (default — solo) duplicates ``room_peqs`` onto channel 1,
    **byte-identical** to before this parameter existed (the solo-impact
    contract). ``[]`` bakes a FLAT right room segment (an uncalibrated
    follower ships flat, never the wrong-room curve). Deliberately a
    2-channel axis — 2.1's 3-channel stream generalises it together with
    the stereo-pinned config contract (HANDOFF-multiroom.md §2); do not
    pre-generalise it alone.

    ``channel_delays_ms`` is the room/pair time-of-arrival axis that
    belongs with measured correction, not Snapcast transport sync. It is
    stereo-pinned (``(left_ms, right_ms)``), positive-only, and emitted as
    CamillaDSP ``Delay`` filters inside the per-room chain. ``None``
    (default — solo) and ``(0, 0)`` emit no delay filters, preserving the
    solo byte contract. Delays are for static acoustic alignment at the
    listening seat; Snapcast still owns distributed clock/transport sync.

    ``playback_pipe_path`` is the BONDED-LEADER playback axis
    (docs/HANDOFF-multiroom.md §2, Increment 5): when set, the playback
    device becomes a CamillaDSP ``File`` sink writing the corrected
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
    sink REQUIRES ``enable_rate_adjust=False`` (a ``File`` backend has no
    output clock for rate_adjust to steer — Snapcast's sample-stuffing is
    the one rate-tracker on the synced chain, §2 invariant 5).

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
    before this parameter existed (the solo-impact contract)."""

    # Loud-output safety: refuse to emit a config whose master fader
    # could boost above full scale. Mirrors the active_speaker emitter.
    volume_limit_db = ensure_volume_limit_db(volume_limit_db)
    # CamillaDSP latency knobs (G7): None → env-or-default, resolved at call
    # time so a JASPER_CAMILLA_{CHUNKSIZE,TARGET_LEVEL} systemd override applies
    # on the next regeneration. Unset env → the literal defaults (1024/2048), so
    # the emitted YAML is byte-identical absent an opt-in. An explicit caller
    # value still wins.
    if chunksize is None:
        chunksize = resolve_camilla_chunksize()
    if target_level is None:
        target_level = resolve_camilla_target_level()
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

    # Bonded-leader pipe-sink guards (fail LOUD at the API boundary,
    # at the playback-pipe API boundary). A File sink has no output clock, so
    # rate_adjust has nothing to steer — and the synced chain's one
    # rate-tracker must be snapclient's sample-stuffing (§2 invariant
    # 5); silently emitting `enable_rate_adjust: true` on a pipe config
    # would hide a wiring bug in the caller.
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
        if enable_rate_adjust:
            raise ValueError(
                "playback_pipe_path (bonded-leader pipe sink) requires "
                "enable_rate_adjust=False — snapclient is the sole "
                "rate-tracker on the synced chain; see "
                "HANDOFF-multiroom.md §2 invariant 5"
            )
    # Resolve the sentinel AFTER the explicitness guard above (mirrors the
    # chunksize/target_level None-or-default pattern a few lines up): the
    # ALSA loopback branch below still needs a concrete playback_format even
    # though a pipe sink never reads this resolved value.
    if playback_format is None:
        playback_format = DEFAULT_PLAYBACK_FORMAT
    muted_channels = _normalize_muted_outputs(muted_outputs)
    mono_fold_output = _normalize_mono_fold_output(mono_fold_output, muted_channels)
    # The shared stereo-prefix builder (jasper.camilla_stereo_prefix) owns the
    # room-PEQ -> headroom -> preamp -> preference assembly. Build the active
    # preference filters once and pass them in (it drops inactive specs);
    # reuse the same list for the summary log below.
    sound_filters = build_sound_filters(profile)
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
        if 0 in muted_channels:
            left_names.append(output_commission_mute_name(0))
        if 1 in muted_channels:
            right_names.append(output_commission_mute_name(1))
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
        pipeline_yaml = emit_master_gain_pipeline(left_names, right_names)
    else:
        pipeline_yaml = emit_master_gain_pipeline(chain_names, chain_names_right)
    # inv-5: an active bond member runs rate_adjust off (snapclient is the sole
    # rate-tracker); default True keeps the solo path unchanged.
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
    channels: 2
    filename: "{playback_pipe_path}"
    format: {DEFAULT_PIPE_SINK_FORMAT}"""
    else:
        playback_yaml = f"""  playback:
    type: Alsa
    channels: 2
    device: "{playback_device}"
    format: {playback_format}"""
    capture_yaml = f"""  capture:
    type: Alsa
    channels: 2
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
# DO NOT HAND-EDIT — update http://jts.local/correction/ or
# http://jts.local/eq/ instead.
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
{_master_gain_mixer_yaml(mono_fold_output)}

pipeline:
{pipeline_yaml}
"""

    if out_path is not None:
        out_path = Path(out_path)
        if not out_path.parent.exists():
            raise FileNotFoundError(
                f"parent directory does not exist: {out_path.parent}"
            )
        _atomic_write_text(out_path, yaml)
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

    Two answers that are only correct together, so they are derived together
    (:func:`flat_graph_channel_plan`). The empty plan is the golden stereo graph.
    """

    muted_outputs: frozenset[int] = frozenset()
    mono_fold_output: int | None = None


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

    The fold is offered only for an explicit 1-channel full-range layout
    (``CONTRACT_NORMAL_MONO_FULL_RANGE``) that assigns exactly one output, and
    only when the SSOT's mute set is that output's exact complement. That last
    equality is the load-bearing one: it is how every case the SSOT withholds
    muting for — unconfigured, roleful/protected, corrupt, or a composite sink
    whose channel *i* is not physical output *i* — withholds the fold too,
    without this function re-deriving (or drifting from) any of those rules. A
    composite mono box in particular must NOT fold here: outputd owns the
    program's fan-out across its child DACs.

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
    if (
        contract.classification == CONTRACT_NORMAL_MONO_FULL_RANGE
        and len(assigned) == 1
        and muted == frozenset(range(width)) - assigned
    ):
        return FlatChannelPlan(
            muted_outputs=muted, mono_fold_output=next(iter(assigned))
        )
    return FlatChannelPlan(muted_outputs=muted)


def emit_flat_outputd_cutover_config(
    *,
    out_path: str | Path | None = None,
    topology: OutputTopology | None = None,
) -> str:
    """Emit the flat outputd startup graph through the production generator.

    Fresh plain-flat installs boot through this graph. Keeping it on the same
    emitter as ordinary sound configs means the active DAC profile's latency
    floor reaches first boot without adding a second Camilla/outputd path.

    WIDTH-MATCHED to the saved output topology. The emitted pipeline is always
    the stereo-pinned two channels (outputd negotiates the DAC's own width —
    the InnoMaker's two — so narrowing the ALSA device is not on the table),
    but every channel the topology does not assign to a ``full_range`` output
    is hard muted. A mono topology therefore boots a graph that CANNOT put
    program audio on the output it never declared, instead of one that does so
    and is refused at the statefile guard. That refusal is real and stays:
    ``jasper.active_speaker.runtime_contract`` re-proves the mutes off this
    YAML, so an unmuted surplus channel is still blocked.

    A mono topology also FOLDS: muting alone would leave a mono cabinet playing
    only the program's left channel, so both channels are summed onto its one
    declared output. Mute and fold come from one :func:`flat_graph_channel_plan`
    read of the topology.

    ``topology`` (any ``OutputTopology``) is a test seam; production reads the
    saved topology.
    """

    from jasper.fanin_coupling import (
        RING_CAMILLA_CHUNKSIZE,
        RING_CAMILLA_ENABLE_RATE_ADJUST,
        RING_CAMILLA_QUEUELIMIT,
        RING_CAMILLA_TARGET_LEVEL,
        resolve_ring_wire,
    )

    # BOTH HALVES ARE THE RING (ADR-0100) — capture is Ring A and playback is
    # Ring B, both off the module defaults — so the geometry is the ring's own
    # hardware-validated low-latency set. The ioplug pins the ring's period bytes
    # min==max, so a 1024-frame chunk cannot negotiate either ring.
    #
    # The wire comes from the same resolver `capture_kwargs_for_coupling` reads,
    # so the seeded startup graph and a live `/sound/` re-emit cannot declare
    # different widths for the same box. Resolved with NO topology: this is the
    # SOLO-STEREO flat graph by construction (a roleful box is seeded from the
    # driver-domain emitters instead), so it has no per-topology width to ask for.
    wire = resolve_ring_wire()
    plan = flat_graph_channel_plan(topology, width=FLAT_GRAPH_WIDTH)

    return emit_sound_config(
        SoundProfile(enabled=False),
        capture_format=wire.sample_format,
        playback_format=wire.sample_format,
        chunksize=RING_CAMILLA_CHUNKSIZE,
        target_level=RING_CAMILLA_TARGET_LEVEL,
        queuelimit=RING_CAMILLA_QUEUELIMIT,
        enable_rate_adjust=RING_CAMILLA_ENABLE_RATE_ADJUST,
        muted_outputs=plan.muted_outputs,
        mono_fold_output=plan.mono_fold_output,
        out_path=out_path,
    )


# The flat cutover config is read by whoever loads it (CamillaDSP, the runtime
# contract's classifier, the camillagui config browser), not only by a
# group-jasper daemon, so it is world-readable — wider than the 0640 the
# ordinary sound configs get from `_atomic_write_text`.
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


def _atomic_write_text(path: Path, text: str) -> None:
    # Sound configs (including the bonded-leader pipe config grouping_leader.yml)
    # are read off-disk by the non-root jasper-control /state leader-pipe health
    # check (active_leader_pipe_path scans the active config for the snapserver
    # pipe sink). Keep them group-readable (0640, group jasper via the setgid
    # configs dir) or that check goes blind under the WS1 non-root drop and the
    # leader falsely reports the bond "degraded — stream is silent" while audio
    # flows. Mirrors jasper.active_speaker.camilla_yaml._atomic_write_text (the
    # active-speaker emitter already widens for the same non-root reason); this
    # is the sibling writer that was missed. The canonical atomic_write_text also
    # replaces the hand-rolled tempfile+rename (no wider-permission window).
    atomic_write_text(path, text, mode=0o640)


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
    per-speaker profiles, never by re-extracting a woven config (see
    docs/HANDOFF-multiroom.md §2, Increment 5).
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
    # stored profiles (HANDOFF-multiroom.md §2, Increment 5).
    if any(
        re.fullmatch(r"(?:room_)?peq_r\d+", name) for name, _ in blocks
    ):
        logger.warning(
            "event=sound.extract_room_peqs result=right_channel_ignored "
            "detail=leader-bake right-channel (*_r*) filters present and "
            "NOT extracted; re-emitting from this extraction alone would "
            "drop the follower's correction — compose from stored "
            "profiles (HANDOFF-multiroom.md §2, Increment 5)"
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
