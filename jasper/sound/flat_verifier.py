# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Proofs for flat CamillaDSP program graphs and their saved output topology."""

from __future__ import annotations

from typing import Any, Literal, Mapping

import yaml

from jasper.active_speaker.graph_types import (
    GRAPH_FLAT_FULL_RANGE,
    GRAPH_PROGRAM_BAKE_PIPE,
    GraphSafety,
)
from jasper.active_speaker.camilla_names import (
    STARTUP_MUTE_GAIN_DB,
    output_commission_mute_name as _commission_mute_name,
)
from jasper.active_speaker.graph_safety import (
    GraphView,
    mixer_output_proved as _mixer_output_proved,
    output_terminally_muted,
    truthy_bool as _truthy_bool,
    view_from_yaml_dict,
)
from jasper.active_speaker.output_contract import (
    CONTRACT_UNCONFIGURED,
    OutputContract,
    classify_output_contract,
    flat_full_range_outputs,
    topology_allows_flat_dac_graph,
)
from jasper.camilla_config_contract import playback_is_pipe
from jasper.camilla_emit import FLAT_PROGRAM_WIDTH, mono_sum_sources
from jasper.json_fields import issue as _issue
from jasper.multiroom.snapfifo import SNAPFIFO
from jasper.output_topology import OutputTopology, OutputTopologyError
from jasper.output_topology_store import load_output_topology_strict
from jasper.sound.camilla_yaml import flat_graph_channel_plan

# Callers may add display text, but must never infer policy from it.
FlatProgramGraphBlockCode = Literal[
    "flat_graph_unconfigured",
    "flat_graph_not_authorized",
    "flat_graph_protected_tweeter",
]
FlatProgramGraphBlock = tuple[FlatProgramGraphBlockCode, str]
FLAT_PROGRAM_GRAPH_UNCONFIGURED: FlatProgramGraphBlockCode = "flat_graph_unconfigured"
FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED: FlatProgramGraphBlockCode = (
    "flat_graph_not_authorized"
)
FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER: FlatProgramGraphBlockCode = (
    "flat_graph_protected_tweeter"
)


def flat_program_graph_block(
    topology: OutputTopology | None = None,
) -> FlatProgramGraphBlock | None:
    """Typed refusal for a flat full-range *program* graph, or ``None``.

    The program lane emits a 2-channel passthrough with no per-driver crossover
    or protection, so it may reach the DAC only for one complete explicit
    passive mono/stereo layout. Unconfigured, invalid, subwoofer and
    roleful/protected layouts are blocked; the last would send full range to a
    compression-driver tweeter (hearing, AGENTS.md #1).

    A TOPOLOGY predicate, not a graph check — verifying a graph that should be
    protective is :func:`classify_camilla_graph`'s job. Policy callers branch on
    the returned code; prose is presentation only. Fail-closed: a corrupt saved
    topology returns a block rather than raising.
    """
    try:
        contract = classify_output_contract(topology or load_output_topology_strict())
    except OutputTopologyError as exc:
        return (
            FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED,
            f"the saved output topology is unavailable or invalid ({exc})",
        )
    if topology_allows_flat_dac_graph(contract):
        return None
    if contract.classification == CONTRACT_UNCONFIGURED:
        return FLAT_PROGRAM_GRAPH_UNCONFIGURED, "no speaker layout is configured"
    if any(item.role == "tweeter" for item in contract.protected_assignments):
        return FLAT_PROGRAM_GRAPH_PROTECTED_TWEETER, _protected_output_detail(contract)
    if not contract.requires_roleful_graph:
        detail = "saved topology is not a complete passive mono or stereo layout"
    else:
        detail = _protected_output_detail(contract)
    return FLAT_PROGRAM_GRAPH_NOT_AUTHORIZED, detail


def flat_program_graph_blocked_reason(
    topology: OutputTopology | None = None,
) -> str | None:
    """Household-readable flat-program refusal detail, or ``None``."""

    block = flat_program_graph_block(topology)
    return block[1] if block is not None else None


def _protected_output_detail(contract: OutputContract) -> str:
    targets = contract.protected_assignments or contract.roleful_assignments
    labels = [
        f"{item.output_label} ({item.role}{'/protected' if item.protected else ''})"
        for item in targets
    ]
    return ", ".join(labels) or "a roleful/protected output"


def _playback_is_program_bake_pipe(text: str) -> bool:
    """True iff a flat graph's ``devices.playback`` is the snapserver File pipe
    the active-leader's camilla#1 program bake writes.

    This is the load-bearing key for the program-bake exemption: a ``File`` sink
    has no DAC, so no driver can be over-driven — safe regardless of topology.
    The leader-pipe liveness check reads the same predicate and ``SNAPFIFO``, so
    the two cannot disagree about what "pipe-shaped" means."""
    return playback_is_pipe(text, SNAPFIFO)


def _flat_output_terminally_muted(
    payload: Mapping[str, Any],
    view: GraphView,
    index: int,
) -> bool:
    """This module's binding of :func:`output_terminally_muted` for a flat graph.

    The three-fact proof itself was PROMOTED to ``graph_safety`` when a second
    caller appeared — the ring arm's anchor acceptance
    (``jasper.fanin.ring_readiness._anchor_is_all_muted``) needs the same
    three facts about the same shape of graph, and a mirrored copy would be a
    drift site on a hearing-safety path. What stays here is the binding this
    module owns: the flat graph's commission-mute NAME for ``index`` and the
    startup mute floor. Behaviour is unchanged.
    """

    return output_terminally_muted(
        payload,
        view,
        index,
        mute_name=_commission_mute_name(index),
        mute_gain_db=STARTUP_MUTE_GAIN_DB,
    )


def _flat_hard_muted_outputs(text: str, playback_channels: Any) -> frozenset[int]:
    """The flat graph's terminally-muted playback channels, parsed from ``text``.

    Derived at ``classify_camilla_graph``'s scope, where the config text lives,
    so :func:`_flat_graph_allowed` stays text-free — the same split the
    ``program_bake_pipe`` fact already uses. Fails closed (empty set) on an
    unparseable graph, which leaves every channel counted as emitting.
    """

    if not isinstance(playback_channels, int) or isinstance(playback_channels, bool):
        return frozenset()
    if playback_channels <= 0:
        return frozenset()
    try:
        payload = yaml.safe_load(text)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    view = view_from_yaml_dict(payload)
    return frozenset(
        index
        for index in range(playback_channels)
        if _flat_output_terminally_muted(payload, view, index)
    )


# The flat family's one Mixer step. Its emitter of record,
# ``jasper.camilla_emit.emit_master_gain_pipeline``, hard-codes the same literal
# for the same reason: the name IS the byte contract of every flat config in the
# field, so there is nothing to parameterise.
_MASTER_GAIN_MIXER = "master_gain"


def _required_mono_fold_output(
    topology: OutputTopology, *, playback_channels: Any
) -> int | None:
    """The playback channel a flat graph on ``topology`` MUST fold onto, if any.

    Delegated WHOLE to ``jasper.sound.camilla_yaml.flat_graph_channel_plan``, so
    the checker cannot demand a fold the renderer would not emit nor accept a
    box the renderer would have folded.

    ``None`` when the graph's own width is unreadable or non-positive — a plan
    derived at width 0 is degenerate (its mute set and the complement of the
    assigned output are both empty, which reads as "fold").
    """

    if not isinstance(playback_channels, int) or isinstance(playback_channels, bool):
        return None
    if playback_channels <= 0:
        return None
    return flat_graph_channel_plan(topology, width=playback_channels).mono_fold_output


def _flat_mono_fold_proved(text: str, fold_output: int) -> bool:
    """True iff ``text``'s ``master_gain`` mixer really folds L+R onto
    ``fold_output``, at the clip-safe gain, and really runs.

    Derived at ``classify_camilla_graph``'s scope like the mute set, so
    :func:`_flat_graph_allowed` stays text-free. Three required facts:

    * the pipeline's Mixer steps are exactly one un-bypassed ``master_gain`` — a
      mixer the pipeline never runs folds nothing, a second could re-route what
      this one summed, and CamillaDSP skips a ``bypassed:`` step entirely;
    * that mixer feeds ``fold_output`` from BOTH program channels, neither
      source muted, order-free (a mixer is a sum);
    * each feed carries :data:`~jasper.camilla_emit.MONO_SUM_GAIN_DB` and its
      polarity: two unity feeds sum 6 dB hotter and a mono track then clips
      against ``volume_limit: 0.0``.

    Fails closed on anything unparseable or unexpected.
    """

    try:
        payload = yaml.safe_load(text)
    except (RecursionError, UnicodeError, ValueError, yaml.YAMLError):
        return False
    if not isinstance(payload, dict):
        return False
    pipeline = payload.get("pipeline")
    steps = [
        step
        for step in (pipeline if isinstance(pipeline, list) else [])
        if isinstance(step, dict) and step.get("type") == "Mixer"
    ]
    if len(steps) != 1 or steps[0].get("name") != _MASTER_GAIN_MIXER:
        return False
    if _truthy_bool(steps[0].get("bypassed")):
        return False
    return _mixer_output_proved(payload, _MASTER_GAIN_MIXER, fold_output, mono_sum_sources())


def _flat_graph_allowed(
    contract: OutputContract,
    *,
    config_path: str | None,
    summary: dict[str, Any],
    program_bake_pipe: bool = False,
    hard_muted_outputs: frozenset[int] = frozenset(),
    program_dest_map: tuple[int, ...] | None = None,
    required_mono_fold: int | None = None,
    mono_fold_proved: bool = False,
) -> GraphSafety:
    # Program-bake exemption: a flat program graph whose playback is a File/pipe
    # sink, not a DAC, is safe regardless of the saved topology — no driver can
    # be over-driven, so the full-range-to-tweeter invariant cannot fire. It
    # keys strictly on the File-pipe playback, so an ALSA-sink flat graph takes
    # the roleful-topology block below unchanged.
    if program_bake_pipe:
        return GraphSafety(
            classification=GRAPH_PROGRAM_BAKE_PIPE,
            allowed=True,
            config_path=config_path,
            camilla_classification=str(summary.get("classification") or "unknown"),
            playback_device=summary.get("playback_device"),
            playback_channels=summary.get("playback_channels"),
            issues=(),
            details={
                "contract_requires_roleful_graph": contract.requires_roleful_graph,
                "program_bake_pipe": True,
                "volume_limit_ok": bool(summary.get("volume_limit_ok")),
            },
        )
    issues: list[dict[str, str]] = []
    allowed = topology_allows_flat_dac_graph(contract)
    playback_channels = summary.get("playback_channels")
    full_range_outputs = flat_full_range_outputs(contract)
    # The invariant is "no emission on an output the topology does not claim".
    # A channel proved hard muted emits nothing; everything else is LIVE.
    # `hard_muted_outputs` is proved structurally off the graph, never taken
    # from the renderer's intent.
    #
    # How the live set is judged depends on what is known about the sink:
    #
    # * a RESOLVED `program_dest_map` — dest index IS physical output index, so
    #   the exact question can be asked. This is what makes muting the WRONG
    #   channel useless, and (since the map also resolves a composite pairing)
    #   what catches a wide composite graph whose program landed on the child-A
    #   pair instead of one output per child.
    # * UNDECIDED mapping on a graph WIDER than the program — refuse. Counting
    #   cannot speak: the surplus dests are hard muted, so a graph feeding the
    #   wrong outputs has exactly as many live channels as the right one.
    # * UNDECIDED mapping at the program's own width — count: more live channels
    #   than assigned outputs means at least one lands somewhere undeclared
    #   under ANY injective mapping. This is why a 2-wide dual-Apple stereo box
    #   on outputs 0 and 2 is not refused.
    if isinstance(playback_channels, int) and not isinstance(playback_channels, bool):
        live_outputs = frozenset(range(playback_channels)) - hard_muted_outputs
    else:
        live_outputs = None
    if allowed and contract.topology_configured and live_outputs is not None:
        code = "flat_full_range_graph_wider_than_topology"
        if program_dest_map is not None:
            undeclared = sorted(live_outputs - full_range_outputs)
            # The RECIPROCAL of "no emission on an undeclared output": a
            # declared output that nothing feeds. Only the program's dests (and
            # the fold, which sums onto one of them) carry program, so a declared
            # output outside that set gets the mixer's mute-floor feed and the
            # speaker is silent while every mute reads correct.
            carries_program = frozenset(program_dest_map) | (
                frozenset() if required_mono_fold is None
                else frozenset({required_mono_fold})
            )
            silent = sorted(full_range_outputs - carries_program)
            if undeclared:
                detail = (
                    f"flat full-range graph emits on physical output(s) "
                    f"{', '.join(str(index) for index in undeclared)}, which the "
                    f"saved full-range topology does not assign"
                )
            elif silent:
                code = "flat_full_range_graph_declared_output_unfed"
                detail = (
                    f"flat full-range graph routes no program to declared "
                    f"physical output(s) "
                    f"{', '.join(str(index) for index in silent)}; the program "
                    f"reaches {', '.join(str(index) for index in sorted(carries_program))}"
                )
            else:
                detail = ""
        elif playback_channels > FLAT_PROGRAM_WIDTH:
            code = "flat_full_range_graph_mapping_undecided"
            detail = (
                f"flat full-range graph is {playback_channels} channels wide on "
                f"a sink whose program-to-output mapping is undecided, so no "
                f"live channel can be traced to a declared physical output"
            )
        else:
            over_wide = len(live_outputs) > len(full_range_outputs)
            detail = (
                f"flat full-range graph exposes {len(live_outputs)} output "
                f"channels, but saved full-range topology assigns only "
                f"{len(full_range_outputs)} physical output(s)"
            ) if over_wide else ""
        if detail:
            allowed = False
            issues.append(_issue("blocker", code, detail))
    if not allowed:
        if contract.classification == CONTRACT_UNCONFIGURED:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_illegal_for_unconfigured_topology",
                "No speaker layout is configured; keep audio parked until a "
                "passive or active layout is saved.",
            ))
        elif contract.requires_roleful_graph:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_illegal_for_roleful_topology",
                (
                    "Active speaker topology assigns "
                    f"{_protected_output_detail(contract)} to a roleful/protected role, "
                    "but Camilla is running a flat full-range graph. Normal playback "
                    "can send full-range signal to the protected driver. Load protected "
                    "active startup or disconnect/clear the topology."
                ),
            ))
        else:
            issues.append(_issue(
                "blocker",
                "flat_full_range_graph_requires_explicit_passive_layout",
                "A flat full-range graph requires a complete saved passive "
                "mono or stereo layout.",
            ))
    # The fold, re-proved off the emitted YAML. Muting the complement satisfies
    # "no emission on an undeclared output" but leaves a mono cabinet playing
    # the program's LEFT channel only — quietly wrong rather than loudly wrong.
    #
    # BELOW the layout ladder above, deliberately: a box refused only for a
    # missing fold has a perfectly good layout, so refusing here keeps the
    # operator from re-saving a topology that is already right.
    # `required_mono_fold` is the RENDERER's own plan, so a topology the
    # renderer would not fold is never asked to.
    if required_mono_fold is not None and not mono_fold_proved:
        allowed = False
        issues.append(_issue(
            "blocker",
            "flat_full_range_graph_mono_fold_missing",
            (
                "Mono full-range topology assigns one physical output "
                f"({required_mono_fold}), but the graph's {_MASTER_GAIN_MIXER} "
                "mixer does not fold both program channels onto it at the "
                "clip-safe mono-sum gain; the speaker would play only the "
                "program's left channel."
            ),
        ))
    return GraphSafety(
        classification=GRAPH_FLAT_FULL_RANGE,
        allowed=allowed,
        config_path=config_path,
        camilla_classification=str(summary.get("classification") or "unknown"),
        playback_device=summary.get("playback_device"),
        playback_channels=summary.get("playback_channels"),
        issues=tuple(issues),
        details={
            "contract_requires_roleful_graph": contract.requires_roleful_graph,
            "volume_limit_ok": bool(summary.get("volume_limit_ok")),
            "hard_muted_outputs": sorted(hard_muted_outputs),
            "mono_fold_output": required_mono_fold,
        },
    )
