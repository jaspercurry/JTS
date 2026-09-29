# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Select and persist the graph allowed by the saved output topology."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jasper.platform import paths
from jasper.platform.atomic_io import atomic_write_text
from jasper.platform.json_fields import issue as _issue
from jasper.platform.log_event import log_event
from jasper.audio_routes.output_topology import OutputTopology, OutputTopologyError
from jasper.audio_routes.output_topology_store import load_output_topology_strict, stamp_statefile_topology

from .camilla_yaml import PARKED_CONFIG_NAME
from .environment import (
    CAMILLA_CLASS_ACTIVE_PARKED,
    classify_camilla_config_text,
    read_camilla_statefile_config_path,
)
from .graph.active_verifier import LINEARIZATION_HEADROOM_UNPROVEN_CODE
from .graph_types import (
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    GRAPH_APPROVED_ACTIVE_RUNTIME,
    GRAPH_PARKED_ALL_MUTED,
    GRAPH_PROGRAM_BAKE_PIPE,
    GraphSafety,
)
from .output_contract import (
    CONTRACT_UNCONFIGURED,
    OutputContract,
    classify_output_contract,
    topology_allows_flat_dac_graph,
)
from .playback_route import ActiveLaneCapabilityGap, active_lane_capability_gap
from .profile import ActiveSpeakerConfigError
from .graph.bass_extension import classify_bass_extension_graph
from .runtime_contract import (
    _path_matches,
    _required_output_width,
    _unsafe_boundary,
    classify_camilla_graph,
)
from .state_paths import baseline_profile_state_path

logger = logging.getLogger("jasper.active_speaker.runtime_contract")

# The ONE flat outputd startup graph. It is a RING graph: the ring is the only
# transport (ADR-0100), so there is no sibling to re-seed instead of.
DEFAULT_FLAT_OUTPUTD_CONFIG = Path("/etc/camilladsp/outputd-cutover.yml")

# The third statefile-seeding outcome, alongside "select a flat graph" and
# "select the staged all-muted active startup graph". A parked deploy SUCCEEDS —
# `SafeGraphDecision.ok` is true — because holding a speaker silent until its
# saved intent permits a real output graph is a legal end state, not a failure.
PARKED_MUTED_STATUS = "parked_muted"
PARKED_MUTED_REASON = (
    "roleful/protected topology has no staged startup graph yet; "
    "parked with every output muted"
)
# The two exits out of parked, verbatim, so the CLI transcript, jasper-doctor,
# and /state all name the same two actions.
PARKED_MUTED_EXITS = (
    "finish crossover preview to stage a startup graph, "
    "or reset output setup and choose an explicit passive layout"
)
UNCONFIGURED_PARKED_EXIT = (
    "choose and save a mono or stereo speaker layout before turning audio on"
)
# ...except on a DAC that declares no active outputd lane, where the first exit
# is IMPOSSIBLE: commissioning can never produce a graph that reaches hardware
# there (jasper.active_speaker.playback_route.active_lane_capability_gap owns
# that predicate). Naming an impossible action first sends a household down a
# road with no end, so the capability-aware surfaces use this instead.
PARKED_MUTED_EXITS_NO_ACTIVE_LANE = (
    "reset output setup at /sound/speaker/, then choose an explicit passive "
    "layout (passive sends full-range to every output and requires a built-in "
    "passive crossover), "
    "or attach an active-capable DAC"
)


def parked_muted_exits(topology: OutputTopology | None = None) -> str:
    """The exits out of parked that are actually reachable on this hardware.

    Fail-soft: any unreadable topology falls back to the general pair rather
    than raising inside a reporting surface.
    """

    try:
        resolved = topology or load_output_topology_strict()
        if classify_output_contract(resolved).classification == CONTRACT_UNCONFIGURED:
            return UNCONFIGURED_PARKED_EXIT
        gap = active_lane_capability_gap(resolved)
    except (OutputTopologyError, OSError, ValueError, TypeError, KeyError):
        return PARKED_MUTED_EXITS
    # An unrecognized DAC profile is not proof the active lane is impossible —
    # see active_lane_capability_gap's docstring — so it takes the same exit
    # as a genuinely capable DAC, not the no-active-lane one.
    if not isinstance(gap, ActiveLaneCapabilityGap):
        return PARKED_MUTED_EXITS
    return f"{gap.device_label} cannot drive an active speaker layout — {PARKED_MUTED_EXITS_NO_ACTIVE_LANE}"

@dataclass(frozen=True)
class SafeGraphDecision:
    """The graph the runtime contract selects for the saved topology.

    ``selected_config_path`` normally names a config that ALREADY EXISTS on
    disk. The one exception is ``status == PARKED_MUTED_STATUS`` (#2135): the
    parked graph is *generated*, so the path names where
    ``apply_safe_graph_decision_to_statefile`` will materialise it. A read-only
    caller (one that does not write the statefile) must not assume the file is
    there yet.
    """

    status: str
    selected_config_path: str | None
    reason: str
    topology_contract: OutputContract
    current_graph: GraphSafety | None = None
    preferred_graph: GraphSafety | None = None
    fallback_graph: GraphSafety | None = None
    issues: tuple[dict[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "blocked" and self.selected_config_path is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_config_path": self.selected_config_path,
            "reason": self.reason,
            "ok": self.ok,
            "topology_contract": self.topology_contract.to_dict(),
            "current_graph": (
                self.current_graph.to_dict() if self.current_graph else None
            ),
            "preferred_graph": (
                self.preferred_graph.to_dict() if self.preferred_graph else None
            ),
            "fallback_graph": (
                self.fallback_graph.to_dict() if self.fallback_graph else None
            ),
            "issues": list(self.issues),
        }



def parked_muted_config_path(path: str | Path | None = None) -> Path:
    """The deterministic on-disk location of the PARKED graph.

    Lives beside the staged startup config in the generated-config dir (staging
    owns that directory constant, so there is one spelling of it).
    """

    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR  # lazy: test_runtime_convergence redirects staging.DEFAULT_CAMILLA_CONFIG_DIR

    return Path(path) if path else Path(DEFAULT_CAMILLA_CONFIG_DIR) / PARKED_CONFIG_NAME


def active_graph_is_parked(config_path: str | Path | None) -> bool:
    """True when ``config_path`` holds the parked graph.

    Content-keyed on the emitted ``# Source:`` provenance marker, not on the
    filename — a renamed or hand-copied file must not be able to claim (or
    disclaim) parked status. Fail-soft: False on any read or parse problem, so a
    reporting surface degrades to "not parked" rather than raising. Callers that
    need SAFETY, not reporting, use ``classify_camilla_graph`` — this predicate
    proves nothing about the graph's contents.
    """

    if not config_path:
        return False
    try:
        text = Path(config_path).read_text(encoding="utf-8")
        summary = classify_camilla_config_text(text)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        return False
    return summary.get("classification") == CAMILLA_CLASS_ACTIVE_PARKED


def build_parked_muted_graph(
    topology: OutputTopology,
    *,
    config_path: str | Path | None = None,
) -> tuple[str | None, GraphSafety]:
    """Build + independently verify the PARKED graph for ``topology``.

    Pure: derives the graph from the saved topology alone (no disk write, no
    hardware probe, no output-route resolution — the sink is a File, so there is
    no DAC lane to resolve) and returns it only alongside the verifier's verdict,
    so no caller can persist parked bytes that were not proved safe.
    """

    from jasper.active_speaker.camilla_yaml import emit_active_speaker_parked_config  # lazy: test_active_speaker_runtime_contract patches camilla_yaml

    contract = classify_output_contract(topology)
    # A stereo capture feeds the mixer, so park at least 2 channels: a 1-output
    # topology (a lone subwoofer) would otherwise emit a 2->1 graph whose extra
    # capture channel is silently dropped rather than explicitly muted. The extra
    # muted output costs nothing.
    width = max(_required_output_width(contract), 2)
    try:
        text = emit_active_speaker_parked_config(
            output_count=width,
            topology_id=topology.topology_id,
        )
    except (ActiveSpeakerConfigError, ValueError) as exc:
        return None, _unsafe_boundary(
            "parked_graph_emit_failed",
            f"could not build a parked active-speaker graph: {type(exc).__name__}",
        )
    return text, classify_camilla_graph(
        topology=topology,
        text=text,
        config_path=str(parked_muted_config_path(config_path)),
    )


def parked_safe_graph_decision(
    topology: OutputTopology,
    *,
    config_path: str | Path | None = None,
    reason: str = "temporarily parked before changing saved speaker layout",
) -> SafeGraphDecision:
    """Return the independently-proved all-muted holding graph for topology.

    This is the only temporary graph topology replacement may load before it
    writes new speaker intent.  It has a File sink and every output terminally
    muted, so it is legal for both the old and proposed topology.
    """

    contract = classify_output_contract(topology)
    text, graph = build_parked_muted_graph(topology, config_path=config_path)
    if text is not None and graph.allowed:
        return SafeGraphDecision(
            status=PARKED_MUTED_STATUS,
            selected_config_path=str(parked_muted_config_path(config_path)),
            reason=reason,
            topology_contract=contract,
            fallback_graph=graph,
        )
    return SafeGraphDecision(
        status="blocked",
        selected_config_path=None,
        reason="could not prove the parked all-muted graph",
        topology_contract=contract,
        fallback_graph=graph,
        issues=graph.issues,
    )


def _linearization_headroom_regression(
    *graphs: GraphSafety | None,
) -> tuple[dict[str, str], ...]:
    """The numeric headroom refusals carried by any of ``graphs``.

    Empty whenever none of them failed THAT way, so a caller can ask "did this
    box's own active graph regress on the headroom arithmetic?" without
    re-deriving the condition, and a new refusal reason cannot silently start
    firing a migration guard written for this one.

    DE-DUPLICATED, order-preserving: on a commissioned box the two graphs asked
    here are usually the SAME FILE. Keyed on the whole issue rather than the
    code, so two branches that genuinely both regressed still report one line
    each with their own role and numbers.
    """
    seen: list[dict[str, str]] = []
    for graph in graphs:
        if graph is None:
            continue
        for issue in graph.issues:
            if issue.get("code") != LINEARIZATION_HEADROOM_UNPROVEN_CODE:
                continue
            if issue not in seen:
                seen.append(issue)
    return tuple(seen)


def safe_graph_for_current_topology(
    topology: OutputTopology | None = None,
    *,
    statefile_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    preferred_config_path: str | Path | None = None,
    flat_config_path: str | Path = DEFAULT_FLAT_OUTPUTD_CONFIG,
    parked_config_path: str | Path | None = None,
    applied_baseline_path: str | Path | None = None,
    staged_metadata_path: str | Path | None = None,
    consider_applied_baseline: bool = True,
) -> SafeGraphDecision:
    """Select the only safe persisted CamillaDSP graph for this topology.

    The flat branch selects one PRE-RENDERED file, because a flat graph is the
    same on every box. A roleful box's graph is per-speaker, so there is nothing
    to pick between: the applied baseline and the staged all-muted startup are
    emitted by the commissioning path and the roleful branches below preserve or
    select whatever the box's own graphs declare.

    An armed roleful box's residual is therefore a STALE ARTIFACT, not a wrong
    selection — an on-disk baseline still naming the pre-arm ALSA lane is the
    graph the box has, and ``jasper-active-speaker baseline-reemit --endpoint
    ring`` is what closes it (step ONE of the arm ladder). This function
    deliberately adds no refusal of its own: blocking here would turn a
    recoverable stale artifact into a box that cannot seed a graph at all.

    The PARKED shape needs nothing either way — its ``File`` sink is DAC- and
    transport-agnostic by construction."""

    from jasper.active_speaker.staging import staged_metadata_path as default_staged_path  # lazy: test_ring_active_endpoint patches staging.staged_metadata_path

    topology = topology or load_output_topology_strict()
    contract = classify_output_contract(topology)
    # Empty is a deliberate runtime state, not implicit stereo.  Decide it
    # before looking at the current graph so a previously-loaded flat graph
    # cannot be preserved after reset or on a fresh install.
    if contract.classification == CONTRACT_UNCONFIGURED:
        return parked_safe_graph_decision(
            topology,
            config_path=parked_config_path,
            reason="no speaker layout is configured; parked with every output muted",
        )
    statefile = paths.camilla_statefile(statefile_path)
    applied_path = Path(applied_baseline_path or baseline_profile_state_path())
    staged_path_authority = Path(staged_metadata_path or default_staged_path())

    authority = {
        "applied_baseline_path": applied_path,
        "staged_metadata_path": staged_path_authority,
    }
    if current_config_path:
        current_path = str(current_config_path)
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="explicit",
            candidate_path=Path(current_config_path),
            **authority,
        )
    else:
        current_graph = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_boot",
            statefile_path=statefile,
            **authority,
        )
        current_path = current_graph.config_path
    preferred_graph = (
        classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="applied_baseline",
            **authority,
        )
        if consider_applied_baseline
        else None
    )
    preferred_path = preferred_graph.config_path if preferred_graph else None
    if preferred_config_path and preferred_path and not _path_matches(
        preferred_config_path, preferred_path
    ):
        preferred_graph = _unsafe_boundary(
            "applied_baseline_locator_mismatch",
            "preferred graph does not match the applied-baseline authority",
        )
    if (
        current_graph
        and current_graph.allowed
        and topology_allows_flat_dac_graph(contract)
        # A program-bake pipe is allowed by the verifier but is NOT a selectable
        # solo graph: its File sink feeds the snapserver FIFO, so preserving it
        # on a solo speaker would leave the DAC silent.
        and current_graph.classification != GRAPH_PROGRAM_BAKE_PIPE
        # The PARKED graph is the same shape of trap: legal for ANY topology, so
        # without this it would be "preserved" forever after a reset. The
        # exclusion lets the selector fall through to `select_flat` once the
        # household saves an explicit passive layout.
        and current_graph.classification != GRAPH_PARKED_ALL_MUTED
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current CamillaDSP graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current approved active-speaker runtime graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        preferred_graph
        and preferred_graph.allowed
        and preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME
    ):
        return SafeGraphDecision(
            status="select_active_baseline",
            selected_config_path=preferred_path,
            reason="saved applied active-speaker baseline is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )
    if (
        current_graph
        and current_graph.allowed
        and current_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="preserve_current",
            selected_config_path=current_path,
            reason="current all-muted active startup graph is legal for saved topology",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
        )

    if topology_allows_flat_dac_graph(contract):
        # ONE flat graph. It used to choose between a loopback flat config and a
        # ring sibling by the persisted coupling; under one audio transport
        # (ADR-0100) the flat graph IS the ring graph, so there is nothing to
        # choose and no way to re-seed a box off its transport.
        fallback = classify_bass_extension_graph(
            topology,
            evidence_source="persisted_candidate",
            candidate_kind="explicit",
            candidate_path=Path(flat_config_path),
            **authority,
        )
        if fallback.allowed:
            return SafeGraphDecision(
                status="select_flat",
                selected_config_path=str(flat_config_path),
                reason="saved topology is an explicit valid passive layout",
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=fallback,
            )
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason="flat outputd fallback is unavailable or invalid",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=fallback,
            issues=fallback.issues,
        )

    staged_graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_candidate",
        candidate_kind="staged_all_muted",
        **authority,
    )
    staged_path = staged_graph.config_path
    # A commissioned box whose OWN boot graph stopped proving on the headroom
    # arithmetic must not be quietly re-pointed at the all-muted startup graph.
    # That fall is legal, which is what makes it dangerous: the deploy stays
    # GREEN, the speaker goes SILENT, and it is sticky, because the next deploy
    # preserves the all-muted graph it just selected. Refuse instead, carrying
    # the numbers: the boot writer re-emits the baseline on this refusal, and a
    # human is summoned to `baseline-reemit` only if that fails (#2847).
    #
    # Narrow on purpose: only the NUMERIC refusal fires it. A shape refusal is a
    # different defect with a different remedy, and every other reason a box
    # lands on the staged anchor is a state this ladder is SUPPOSED to resolve
    # silently and green.
    regressed = _linearization_headroom_regression(current_graph, preferred_graph)
    if regressed:
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason=(
                "the active-speaker graph this box boots no longer proves its "
                "own headroom charge; selecting the all-muted startup graph "
                "would silence the speaker on a green deploy. Re-emit the "
                "baseline (`jasper-active-speaker baseline-reemit`) and re-run"
            ),
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=staged_graph,
            issues=tuple(regressed),
        )
    if (
        staged_graph
        and staged_graph.allowed
        and staged_graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return SafeGraphDecision(
            status="select_active_startup",
            selected_config_path=staged_path,
            reason="roleful/protected topology requires the all-muted active startup graph",
            topology_contract=contract,
            current_graph=current_graph,
            preferred_graph=preferred_graph,
            fallback_graph=staged_graph,
        )

    issues: list[dict[str, str]] = []
    if current_graph and current_graph.issues:
        issues.extend(current_graph.issues)
    elif current_graph and current_graph.allowed:
        issues.append(_issue(
            "blocker",
            "current_graph_not_persistable",
            (
                f"{current_graph.classification} is legal only for an active "
                "session, not as a deploy/restart fallback"
            ),
        ))
    if preferred_graph and preferred_graph.issues:
        issues.extend(preferred_graph.issues)
    if staged_graph and staged_graph.issues:
        issues.extend(staged_graph.issues)
    if not staged_path:
        # Third outcome: NO staged graph at all — a roleful topology declared and
        # paused before crossover preview. Park the speaker silent rather than
        # refuse, so the box can still take deploys in that limbo.
        #
        # Gated on "no staged LOCATOR", not "no usable staged graph": a staged
        # graph that exists but fails its safety proof keeps blocking below,
        # because that is a commissioning bug rather than a paused household.
        #
        # LAST on purpose — every real graph above has already been considered,
        # so a parked file can never shadow one carrying driver protection.
        parked_text, parked_graph = build_parked_muted_graph(
            topology, config_path=parked_config_path
        )
        if parked_text is not None and parked_graph.allowed:
            # No `event=` line here: this function is a pure decision reached
            # also by read-only callers. `apply_safe_graph_decision_to_statefile`
            # emits the stable `decision=parked_muted` line instead.
            selected = str(parked_muted_config_path(parked_config_path))
            return SafeGraphDecision(
                status=PARKED_MUTED_STATUS,
                selected_config_path=selected,
                reason=PARKED_MUTED_REASON,
                topology_contract=contract,
                current_graph=current_graph,
                preferred_graph=preferred_graph,
                fallback_graph=parked_graph,
                # Proceeding is not the same as being clean: parking is
                # reachable for a topology that still carries blockers, so the
                # decision REPORTS them while `ok` stays True (derived from
                # `status`, never from `issues`). Deliberately `contract.issues`
                # alone — the set the parked verdict declined to refuse on —
                # rather than the wider `issues` list, which also collects "no
                # candidate at this path" noise a parked box is expected to have.
                issues=tuple(contract.issues),
            )
        issues.append(_issue(
            "blocker",
            "active_startup_graph_missing",
            (
                "saved topology has roleful/protected outputs but no staged "
                "all-muted active startup graph is available"
            ),
        ))
        # Deduped: the parked verifier re-runs `classify_camilla_graph`, which
        # prepends the SAME `contract.issues` the current/preferred/staged
        # classifications already contributed above. Appending them verbatim
        # printed each topology-level blocker twice in the install transcript.
        seen = {(issue["code"], issue["message"]) for issue in issues}
        issues.extend(
            issue
            for issue in parked_graph.issues
            if (issue["code"], issue["message"]) not in seen
        )
    return SafeGraphDecision(
        status="blocked",
        selected_config_path=None,
        reason=(
            "roleful/protected topology has no legal all-muted active startup graph"
        ),
        topology_contract=contract,
        current_graph=current_graph,
        preferred_graph=preferred_graph,
        fallback_graph=staged_graph,
        issues=tuple(issues),
    )


def write_camilla_statefile(
    statefile_path: str | Path,
    config_path: str | Path,
    *,
    channel_slots: int = 5,
) -> None:
    """Write CamillaDSP's persisted config path with muted volume slots.

    Published atomically: a reader sees the old statefile or the complete new
    one, never a partial write. A truncated ``outputd-statefile.yml`` is a
    CamillaDSP that cannot start — the same class of dead box #2664 is about —
    and since that issue this writer also runs during install, on the deploy
    path, which is exactly when a power cut or an OOM kill is most likely.
    """

    target = Path(statefile_path)
    slots = max(1, int(channel_slots))
    payload: dict[str, Any] = {}
    try:
        existing = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        existing = None
    if isinstance(existing, dict):
        payload.update(existing)
    payload["config_path"] = str(config_path)
    if "mute" not in payload:
        payload["mute"] = [False] * slots
    if "volume" not in payload:
        payload["volume"] = [0.0] * slots
    ordered = {"config_path": payload.pop("config_path")}
    ordered.update(payload)
    atomic_write_text(target, yaml.safe_dump(ordered, sort_keys=False), mode=0o644)


def apply_safe_graph_decision_to_statefile(
    decision: SafeGraphDecision,
    *,
    statefile_path: str | Path,
    topology: OutputTopology | None = None,
) -> bool:
    """Persist the selected graph if the statefile is absent or needs repair.

    ``topology`` is used only by the PARKED branch: unlike every other selectable
    graph, the parked graph is generated rather than found, so this writer
    materialises it from the saved topology and RE-PROVES it all-muted before the
    bytes reach disk. Re-deriving (instead of carrying decision-time bytes) means
    the write-time proof is a real second check, not a replay of the first.
    """

    if not decision.ok or not decision.selected_config_path:
        return False
    materialise_safe_graph_decision(decision, topology=topology)
    if decision.status == PARKED_MUTED_STATUS:
        # Logged HERE, not at decision time: the decision function is also
        # reached by read-only callers, and a `decision=parked_muted` line from
        # those would read as "the box was just parked" when nothing was written.
        # It DOES fire on a statefile no-op, deliberately: the parked graph has
        # already been RE-PROVED all-muted by this point, and the line means
        # "this apply resolved to parked", not "the statefile changed".
        log_event(
            logger,
            "active_speaker.runtime_graph",
            decision=PARKED_MUTED_STATUS,
            reason=decision.reason,
            topology_mode=decision.topology_contract.classification,
            statefile=str(statefile_path),
            config_path=decision.selected_config_path,
        )
    # The proof stamp goes down only AFTER the pointer it certifies is on disk.
    # Stamping first would leave a failed `write_camilla_statefile` claiming the
    # OLD statefile was proved against the NEW topology — the exact pair the
    # boot gate reads as "no mismatch", which is the one answer that must not
    # come out of a write that did not happen.
    current = read_camilla_statefile_config_path(statefile_path)
    if _path_matches(current, decision.selected_config_path):
        stamp_statefile_topology(statefile_path, topology)
        return False
    write_camilla_statefile(statefile_path, decision.selected_config_path)
    stamp_statefile_topology(statefile_path, topology)
    return True


def materialise_safe_graph_decision(
    decision: SafeGraphDecision,
    *,
    topology: OutputTopology | None = None,
) -> None:
    """Materialise any generated graph without taking statefile ownership.

    Park-before-save runs in jasper-web, where the generated-config directory
    is writable but CamillaDSP's root-owned statefile is not.  The websocket
    load persists that pointer on behalf of the daemon.  Root boot/reconcile
    callers compose this helper with :func:`write_camilla_statefile` through
    :func:`apply_safe_graph_decision_to_statefile`.
    """

    if (
        decision.ok
        and decision.selected_config_path
        and decision.status == PARKED_MUTED_STATUS
    ):
        _materialise_parked_muted_config(
            decision.selected_config_path,
            topology=topology,
        )


def _materialise_parked_muted_config(
    config_path: str | Path,
    *,
    topology: OutputTopology | None,
) -> None:
    """Write the parked graph to disk, refusing anything not proved all-muted.

    Runs on every apply, not only when the statefile changes: the statefile may
    already point here while the config itself is missing — a deleted or
    never-written generated-config dir — and a statefile pointing at a missing
    config is how CamillaDSP fails to start. It is a NO-OP when the on-disk bytes
    already match, so the steady state costs one read instead of a new inode plus
    a ``camilladsp --check`` subprocess on every deploy (twice: outputd's
    statefile and camilla#2's).
    """

    import tempfile

    from jasper.dsp_control.dsp_apply import validate_camilla_config  # lazy: test_active_speaker_runtime_contract patches dsp_apply.validate_camilla_config

    topology = topology or load_output_topology_strict()
    text, graph = build_parked_muted_graph(topology, config_path=config_path)
    if text is None or not graph.allowed:
        raise ActiveSpeakerConfigError(
            "refusing to write a parked active-speaker graph that is not "
            "proved all-muted: "
            + "; ".join(issue["code"] for issue in graph.issues)
        )
    target = Path(config_path)
    try:
        if target.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeError):
        pass  # absent, unreadable, or not text — fall through and rewrite
    target.parent.mkdir(parents=True, exist_ok=True)
    # CamillaDSP preflight before these bytes become the box's boot graph: an
    # unloadable parked config would crash-loop jasper-camilla, a worse outcome
    # than the blocked deploy this path replaces, so a rejected graph degrades
    # back to blocked. Checked on a per-invocation-unique temp sibling (mkstemp,
    # not a fixed dotfile) so two concurrent writers in this shared dir cannot
    # unlink each other's probe mid-validation. A missing camilladsp binary
    # passes through, the same `ok_to_apply` contract protected staging uses.
    handle, probe_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.check-", suffix=".yml"
    )
    os.close(handle)
    probe = Path(probe_name)
    try:
        atomic_write_text(probe, text, mode=0o640)
        validation = validate_camilla_config(probe)
    finally:
        probe.unlink(missing_ok=True)
    if not validation.ok_to_apply:
        raise ActiveSpeakerConfigError(
            "generated parked active-speaker graph failed CamillaDSP "
            f"validation ({validation.status.value}): {validation.error}"
        )
    atomic_write_text(target, text, mode=0o640)
