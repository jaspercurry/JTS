# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — active-speaker domain.

Split from audio.py verbatim (ADR-0235 PR 8); no check logic changed."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ._evidence import evidence
from ._registry import doctor_check
from ._shared import REASON_TOPOLOGY_UNREADABLE, CheckResult
from .correction import (
    REASON_CAMILLA_CONFIG_MISSING,
    REASON_CAMILLA_STATEFILE_UNREADABLE,
    _active_camilla_config_path,
)

# Closed vocabulary for this module's `CheckResult.reason`: one snake_case
# constant per distinct outcome branch below. Every `warn`/`fail` carries one;
# an `ok` carries one only where the ok itself is a fact a consumer branches on
# (not-applicable, skipped, an informational sub-state). `detail` stays the
# human sentence and is free to reword; tests pin `status` and `reason`
# (ADR-0233 rule 3).

REASON_GRAPH_PASSIVE_LAYOUT = "runtime_graph_passive_layout"
REASON_GRAPH_PARKED_SILENT = "runtime_graph_parked_silent"
REASON_GRAPH_LAYOUT_INCOMPLETE = "runtime_graph_layout_incomplete"
REASON_GRAPH_UNCONFIGURED_NOT_PARKED = "runtime_graph_unconfigured_not_parked"
REASON_GRAPH_UNSAFE = "runtime_graph_unsafe"

REASON_SOUND_PROFILE_DEFAULT = "sound_profile_default"
REASON_SOUND_PROFILE_UNREADABLE = "sound_profile_unreadable"
REASON_SOUND_PROFILE_NOT_ACTIVE = "sound_profile_not_active"

REASON_BASS_EXTENSION_NOT_COMMISSIONED = "bass_extension_not_commissioned"
REASON_BASS_EXTENSION_MALFORMED = "bass_extension_malformed"
REASON_BASS_EXTENSION_STALE = "bass_extension_stale"
REASON_BASS_EXTENSION_BYPASSED = "bass_extension_bypassed"

REASON_DSP_APPLY_NONE = "dsp_apply_none"
REASON_DSP_APPLY_ROLLBACK_FAILED = "dsp_apply_rollback_failed"
REASON_DSP_APPLY_UNSUCCESSFUL = "dsp_apply_unsuccessful"

REASON_BASELINE_CANONICAL_NOT_APPLICABLE = "baseline_canonical_not_applicable"
REASON_BASELINE_CANONICAL_MISSING = "baseline_canonical_missing"
REASON_BASELINE_CANONICAL_LIVE_MISSING = "baseline_canonical_live_missing"
REASON_BASELINE_CANONICAL_UNCOMPARABLE = "baseline_canonical_uncomparable"
REASON_BASELINE_CANONICAL_STALE = "baseline_canonical_stale"

REASON_SPEAKER_SETUP_UNREADABLE = "speaker_setup_unreadable"
REASON_APPLIED_GRAPH_NO_PROFILE = "applied_graph_no_profile"
REASON_APPLIED_GRAPH_STAGED_ANCHOR = "applied_graph_staged_anchor"
REASON_APPLIED_GRAPH_NOT_EVALUATED = "applied_graph_not_evaluated"
REASON_APPLIED_GRAPH_MISMATCH = "applied_graph_mismatch"

REASON_STARTUP_HOLD_NONE = "startup_hold_none"
REASON_STARTUP_HOLD_IN_FLIGHT = "startup_hold_in_flight"
REASON_STARTUP_HOLD_STALE = "startup_hold_stale"

REASON_ROOM_AUTHORITY_NO_DECISION = "room_authority_no_decision"
REASON_ROOM_AUTHORITY_NOT_REQUIRED = "room_authority_not_required"
REASON_ROOM_AUTHORITY_UNBANKED = "room_authority_unbanked"
REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE = "room_authority_receipt_unreadable"
REASON_ROOM_AUTHORITY_UNPROVEN = "room_authority_unproven"
REASON_ROOM_AUTHORITY_BLOCKED = "room_authority_blocked"

REASON_SETUP_NOTICES_NONE = "setup_notices_none"
REASON_SETUP_NOTICES_STANDING = "setup_notices_standing"


_SPEAKER_SETUP_URL = "http://<speaker>/sound/setup/"


def _blocker_summary(contract) -> str:
    """``blockers=<codes>: <messages>`` for a contract, empty when it is clean."""

    if not contract.issues:
        return ""
    codes = ",".join(str(issue.get("code") or "") for issue in contract.issues)
    messages = "; ".join(
        str(issue.get("message") or "")
        for issue in contract.issues
        if issue.get("message")
    )
    return f"blockers={codes}" + (f": {messages}" if messages else "")


def _incomplete_layout_detail(contract) -> str:
    """Why the saved layout is not a complete passive one, and where to fix it."""

    blocker = (
        contract.issues[0]["message"]
        if contract.issues
        else "saved layout is not a complete passive mono or stereo layout"
    )
    return f"{blocker}. Fix the layout at {_SPEAKER_SETUP_URL}"


@doctor_check()
def check_active_speaker_runtime_graph() -> CheckResult:
    """Report the graph selected for saved speaker intent, fail closed if unsafe.

    "Is the speaker parked" is answered by ``active_graph_is_parked`` and the
    way out by ``parked_muted_exits`` — the readers ``/state`` and
    ``jasper.control.audio_health`` consume (ADR-0233 rule 1). Asked of the
    file the safety proof classified, not of a second statefile resolution, so
    one row never mixes two views of the disk. Deliberately narrower than those
    two reporting surfaces in one direction: bytes carrying the parked
    provenance marker that FAIL the structural all-muted proof are reported
    here as unsafe, never as a healthy park.

    The saved layout's unresolved blockers ride on this row rather than a
    second one: a blocker is already a refusal of the runtime graph, and on a
    parked box it needs saying that clearing them does not by itself restore
    sound — parking is gated on the absence of a staged startup graph.

    Parked is WARN, never FAIL (#2145): a parked speaker is silent, not broken,
    and a mid-commission box must stay deployable.
    """
    from jasper.active_speaker.runtime_contract import (
        CONTRACT_UNCONFIGURED,
        active_graph_is_parked,
        classify_bass_extension_graph,
        classify_output_contract,
        parked_muted_exits,
        topology_allows_flat_dac_graph,
    )
    from jasper.output_topology import OutputTopologyError

    name = "active speaker runtime graph"
    try:
        topology = evidence.output_topology_strict()
    except OutputTopologyError as exc:
        return CheckResult(
            name, "fail",
            f"saved output topology is unavailable or invalid: {exc}",
            reason=REASON_TOPOLOGY_UNREADABLE,
        )
    contract = classify_output_contract(topology)
    # The SSOT that authorizes a flat DAC graph is deliberately narrower than
    # "not roleful": unconfigured and incomplete/invalid non-roleful layouts
    # are not passive playback contracts.
    if topology_allows_flat_dac_graph(contract):
        return CheckResult(
            name, "ok",
            f"{contract.classification}: explicit passive layout is valid",
            reason=REASON_GRAPH_PASSIVE_LAYOUT,
        )

    statefile, config_path = evidence.get("camilla_config", _active_camilla_config_path)
    if config_path is None:
        return CheckResult(
            name, "fail",
            (
                f"could not read config_path from {statefile}; saved topology "
                "does not permit an unchecked flat fallback"
            ),
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    if not Path(config_path).exists():
        return CheckResult(
            name, "fail",
            f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    from ...active_speaker.state_paths import baseline_profile_state_path
    from ...active_speaker.staging import staged_metadata_path
    from ...bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from ...bass_extension.profile import DEFAULT_PROFILE_PATH

    graph = classify_bass_extension_graph(
        topology,
        evidence_source="persisted_boot",
        statefile_path=Path(statefile),
        applied_baseline_path=baseline_profile_state_path(),
        profile_path=DEFAULT_PROFILE_PATH,
        intent_path=BASS_EXTENSION_APPLY_INTENT_PATH,
        staged_metadata_path=staged_metadata_path(),
    )
    if graph.allowed and active_graph_is_parked(graph.config_path):
        # A parked graph is intentional silence, not a broken runtime — both a
        # zero-group topology (the household must choose a layout) and an
        # incomplete roleful layout. Either way the proof above establishes that
        # every output is muted, so both arms below carry `speaker_silent`.
        if contract.classification == CONTRACT_UNCONFIGURED or (
            contract.requires_roleful_graph
        ):
            blockers = _blocker_summary(contract)
            return CheckResult(
                name, "warn",
                f"parked silent for {contract.classification}."
                + (f" Clear {blockers} at {_SPEAKER_SETUP_URL}." if blockers else "")
                + f" Next: {parked_muted_exits(topology)}",
                speaker_silent=True,
                reason=REASON_GRAPH_PARKED_SILENT,
            )
        return CheckResult(
            name, "fail", _incomplete_layout_detail(contract),
            speaker_silent=True,
            reason=REASON_GRAPH_LAYOUT_INCOMPLETE,
        )
    if graph.allowed:
        if contract.classification == CONTRACT_UNCONFIGURED:
            return CheckResult(
                name, "fail",
                "unconfigured topology must use the proved parked graph",
                reason=REASON_GRAPH_UNCONFIGURED_NOT_PARKED,
            )
        if not contract.requires_roleful_graph:
            return CheckResult(
                name, "fail", _incomplete_layout_detail(contract),
                reason=REASON_GRAPH_LAYOUT_INCOMPLETE,
            )
        return CheckResult(
            name, "ok",
            f"{graph.classification} is legal for {contract.classification}",
        )

    detail = (
        graph.issues[0]["message"]
        if graph.issues
        else "Camilla graph is unsafe for saved active speaker topology"
    )
    return CheckResult(name, "fail", detail, reason=REASON_GRAPH_UNSAFE)


def _sound_profile_path() -> Path:
    return Path(
        os.environ.get(
            "JASPER_SOUND_PROFILE_PATH",
            "/var/lib/jasper/sound_profile.json",
        )
    )

@doctor_check()
def check_sound_profile() -> CheckResult:
    from jasper.sound.camilla_yaml import is_jts_generated_config
    from jasper.sound.profile import (
        SoundProfile,
        build_sound_filters,
        estimate_headroom_db,
    )
    from jasper.sound.settings import load_sound_settings, output_trim_db

    path = _sound_profile_path()
    if not path.exists():
        return CheckResult(
            "sound profile",
            "ok",
            "default Flat profile (no saved preference EQ)",
            reason=REASON_SOUND_PROFILE_DEFAULT,
        )
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return CheckResult(
            "sound profile", "fail", f"could not read {path}: {e}",
            reason=REASON_SOUND_PROFILE_UNREADABLE,
        )

    profile = SoundProfile.from_mapping(raw)
    filter_count = len(build_sound_filters(profile))
    headroom_db = estimate_headroom_db(profile)
    settings = load_sound_settings()
    trim = output_trim_db(profile, settings)

    active_path = evidence.camilla_config_path()
    # "Is this a JTS-generated config?" has ONE owner
    # (:func:`jasper.sound.camilla_yaml.is_jts_generated_config`) — never a
    # local copy of the name set: since #2572 the reconcile legitimately leaves
    # a content-identical graph running under whatever it is named instead of
    # rewriting it to `sound_current.yml`, so a stale copy here would
    # permanently tell a household its saved profile is missing from a graph
    # that carries it. `config_dir` is the active config's own parent, the same
    # way `check_correction_current_config` asks, so the only question left to
    # the canonical owner is the name.
    active_generated = is_jts_generated_config(
        active_path,
        config_dir=Path(active_path).parent,
    ) if active_path else False
    drifted = bool(profile.enabled and filter_count and not active_generated)

    detail = (
        f"enabled={profile.enabled} curve={profile.curve_id} "
        f"filters={filter_count} headroom={headroom_db:.1f}dB "
        f"match_loudness={'on' if settings.match_loudness else 'off'} "
        f"output_trim={trim:.1f}dB"
        + (" (saved profile not reflected in active generated config)"
           if drifted else "")
    )
    if drifted:
        return CheckResult(
            "sound profile", "warn", detail,
            reason=REASON_SOUND_PROFILE_NOT_ACTIVE,
        )
    return CheckResult("sound profile", "ok", detail)

@doctor_check()
def check_bass_extension_profile() -> CheckResult:
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.bass_extension.profile import evaluate_bass_extension_profile

    evaluation = evaluate_bass_extension_profile(
        topology=evidence.output_topology(),
        applied_baseline_state=load_applied_baseline_profile_state(),
    )
    if evaluation.status == "missing":
        return CheckResult(
            "bass extension profile", "ok", "bass extension: not commissioned",
            reason=REASON_BASS_EXTENSION_NOT_COMMISSIONED,
        )
    if evaluation.status == "malformed":
        return CheckResult(
            "bass extension profile",
            "fail",
            f"bass extension profile is malformed: {evaluation.detail}",
            reason=REASON_BASS_EXTENSION_MALFORMED,
        )
    if evaluation.status == "stale":
        refusals = ",".join(refusal.value for refusal in evaluation.refusals)
        return CheckResult(
            "bass extension profile",
            "warn",
            f"bass extension profile is stale [{refusals}]: {evaluation.detail}",
            reason=REASON_BASS_EXTENSION_STALE,
        )
    if evaluation.status == "bypassed":
        return CheckResult(
            "bass extension profile", "ok", "bass extension profile is bypassed",
            reason=REASON_BASS_EXTENSION_BYPASSED,
        )
    assert evaluation.profile is not None
    return CheckResult(
        "bass extension profile",
        "ok",
        f"accepted; deepest={evaluation.profile.targets[0].fp_hz:g}Hz "
        f"natural={evaluation.profile.targets[-1].fp_hz:g}Hz",
    )

@doctor_check()
def check_dsp_apply_state() -> CheckResult:
    from jasper.dsp_apply import last_dsp_apply_state

    state = last_dsp_apply_state()
    if state is None:
        return CheckResult(
            "DSP apply state",
            "ok",
            "no DSP apply attempts recorded yet",
            reason=REASON_DSP_APPLY_NONE,
        )

    result = str(state.get("result") or "unknown")
    phase = str(state.get("phase") or "unknown")
    source = str(state.get("source") or "unknown")
    candidate = state.get("candidate_config_path")
    op_id = str(state.get("op_id") or "")[:8]

    detail = f"source={source} result={result} phase={phase} op={op_id}"
    if candidate:
        detail += f" config={candidate}"
    if state.get("rollback_attempted") and state.get("rollback_succeeded") is False:
        return CheckResult(
            "DSP apply state", "fail", detail,
            reason=REASON_DSP_APPLY_ROLLBACK_FAILED,
        )
    if result != "success":
        return CheckResult(
            "DSP apply state", "warn", detail,
            reason=REASON_DSP_APPLY_UNSUCCESSFUL,
        )
    return CheckResult("DSP apply state", "ok", detail)

def _is_baseline_candidate_sibling(live_path: Path, canonical: Path) -> bool:
    """True if ``live_path`` is a source-fingerprinted sibling of ``canonical``.

    ``build_baseline_profile_candidate`` names every candidate
    ``<canonical stem>_candidate_<fingerprint12><canonical suffix>`` beside
    the canonical file (issue #1666). Used to gate the comparison below to
    speakers that actually have an active-speaker baseline applied live —
    a plain stereo/flat topology's live config (e.g. ``outputd-cutover.yml``)
    never matches this shape, so it stays "not applicable" rather than a
    false warning.
    """
    return (
        live_path.parent == canonical.parent
        and live_path.suffix == canonical.suffix
        and live_path.name.startswith(f"{canonical.stem}_candidate_")
    )

@doctor_check()
def check_active_speaker_baseline_canonical() -> CheckResult:
    """Canonical ``active_speaker_baseline.yml`` durability (issue #1666).

    ``build_baseline_profile_candidate`` never writes the canonical
    ``baseline_config_path()`` name directly; every apply/restore promotes the
    applied candidate's bytes onto it fail-soft, after CamillaDSP confirmed the
    candidate live. A failed promote leaves that copy stale without affecting
    the audible graph, which the other readers of the canonical name (the
    multiroom follower fallback, operators, this doctor) trust. Disclosed as
    `ok`: the live graph is the audible truth until the next follower teardown
    restores the canonical over it.
    """
    from jasper.active_speaker.baseline_profile import (
        active_layer_a_fingerprint,
        baseline_config_path,
    )
    from jasper.active_speaker.profile import ActiveSpeakerConfigError

    label = "active speaker baseline canonical"
    statefile, live_path_raw = evidence.get("camilla_config", _active_camilla_config_path)
    if live_path_raw is None:
        # A missing/unreadable outputd statefile is already a real failure at
        # the checks that own it (check_active_speaker_runtime_graph fails when
        # a roleful topology needs it). This check's scope is only "does
        # canonical mirror the live baseline", which cannot be evaluated here —
        # not applicable, not a warning.
        return CheckResult(
            label, "skipped",
            f"could not read config_path from {statefile}",
            reason=REASON_BASELINE_CANONICAL_NOT_APPLICABLE,
        )
    live_path = Path(live_path_raw)
    canonical = baseline_config_path()
    if live_path == canonical:
        return CheckResult(
            label, "ok", f"live config is the canonical file ({canonical})",
        )
    if not _is_baseline_candidate_sibling(live_path, canonical):
        return CheckResult(
            label, "skipped",
            f"live config ({live_path}) is not an active-speaker baseline "
            "candidate",
            reason=REASON_BASELINE_CANONICAL_NOT_APPLICABLE,
        )
    if not canonical.exists():
        return CheckResult(
            label, "ok",
            f"canonical baseline file is missing ({canonical}) while the live "
            f"config is an applied baseline candidate ({live_path}); the next "
            "apply or restore re-promotes it",
            reason=REASON_BASELINE_CANONICAL_MISSING,
        )
    if not live_path.exists():
        return CheckResult(
            label, "ok",
            f"live baseline candidate file is missing on disk ({live_path}); "
            f"cannot compare it against canonical ({canonical})",
            reason=REASON_BASELINE_CANONICAL_LIVE_MISSING,
        )
    try:
        live_fingerprint = active_layer_a_fingerprint(
            live_path.read_text(encoding="utf-8")
        )
        canonical_fingerprint = active_layer_a_fingerprint(
            canonical.read_text(encoding="utf-8")
        )
    except (OSError, ActiveSpeakerConfigError) as exc:
        return CheckResult(
            label, "warn", f"could not compare {live_path} to {canonical}: {exc}",
            reason=REASON_BASELINE_CANONICAL_UNCOMPARABLE,
        )
    if live_fingerprint == canonical_fingerprint:
        return CheckResult(
            label, "ok",
            f"canonical file ({canonical}) matches the live applied baseline "
            f"({live_path})",
        )
    return CheckResult(
        label, "ok",
        f"canonical baseline file ({canonical}) does not match the live "
        f"applied config ({live_path}); the running graph is correct, but the "
        "canonical file is stale for other readers (multiroom follower "
        "fallback, operators)",
        reason=REASON_BASELINE_CANONICAL_STALE,
    )


@doctor_check(label="active speaker applied graph")
def check_active_speaker_applied_graph() -> CheckResult:
    """Is the durable graph the one the applied profile names?

    A crossover-v2 round that ends on a verify rejection banks no adoption but
    has already repointed CamillaDSP's persisted ``config_file_path`` at the
    rejected candidate, leaving the anchor's per-driver values disagreeing with
    the applied profile's. ``setup_status`` binds the two; this reads the
    binding out.

    Compared at the DURABLE anchor, never at the running graph: runtime-only
    swaps (audition, ADR-0193; the measurement session graph; the per-driver
    commissioning load) install through ``set_active_config_raw`` and leave the
    statefile alone, so none can read as drift. A staged/commissioning anchor
    IS a durable repoint and is excluded by name instead.

    WARN, never FAIL: the anchor is the audible truth either way.
    """

    from ...active_speaker.setup_status import IN_SEQUENCE_CAPTURE_ANCHOR_REASON

    label = "active speaker applied graph"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    protected = status.get("protected_profile")
    binding = protected.get("layer_a_binding") if isinstance(protected, dict) else None
    if not isinstance(binding, dict):
        return CheckResult(
            label, "skipped", "no applied active-speaker profile to bind",
            reason=REASON_APPLIED_GRAPH_NO_PROFILE,
        )
    issues = status.get("issues")
    if any(
        isinstance(issue, dict)
        and issue.get("code") == IN_SEQUENCE_CAPTURE_ANCHOR_REASON
        for issue in (issues if isinstance(issues, list) else [])
    ):
        return CheckResult(
            label, "skipped",
            "a commissioning/staged graph is the durable anchor by design",
            reason=REASON_APPLIED_GRAPH_STAGED_ANCHOR,
        )
    if binding.get("matches") is True:
        return CheckResult(
            label, "ok",
            "the durable graph is the one the applied profile names "
            f"(layer_a={binding.get('loaded_fingerprint')})",
        )
    if binding.get("status") != "mismatch":
        return CheckResult(
            label, "skipped",
            "applied-profile graph binding not evaluated "
            f"({binding.get('status') or 'absent'})",
            reason=REASON_APPLIED_GRAPH_NOT_EVALUATED,
        )
    fields = "; ".join(
        f"{item.get('field')} profile={item.get('expected')} "
        f"graph={item.get('loaded')}"
        for item in (binding.get("differences") or [])
        if isinstance(item, dict)
    )
    return CheckResult(
        label, "warn",
        f"the durable graph at {status.get('active_config_path')} is not the one "
        "the applied profile names: layer_a profile="
        f"{binding.get('expected_fingerprint')} graph="
        f"{binding.get('loaded_fingerprint')}"
        + (f" [{fields}]" if fields else "")
        + " — apply that crossover again, or republish the banked candidate "
        "and apply it, to make the two agree",
        reason=REASON_APPLIED_GRAPH_MISMATCH,
    )


@doctor_check(label="active speaker startup hold")
def check_active_speaker_startup_hold() -> CheckResult:
    """A staged-startup hold marker with no startup load behind it is stale.

    ``load_protected_startup_config`` takes an ephemeral ``/run`` marker before
    applying the all-muted staged anchor; while it is present
    ``safe_graph_for_current_topology`` preserves that anchor instead of the
    saved baseline, which the household-facing
    ``staged_startup_hold_unavailable`` copy points at. That rung ALSO requires
    the live graph to BE the anchor, so a marker outliving its load silences
    only a box still on the anchor path (`fail`); over any other live graph it
    holds nothing and ``/run`` empties before the next boot reads it (`warn`,
    not silent).
    """

    from ...active_speaker.startup_hold import (
        staged_startup_hold_active,
        startup_hold_marker_path,
    )
    from ...active_speaker.startup_load import load_startup_load_state

    label = "active speaker startup hold"
    marker = startup_hold_marker_path()
    if not staged_startup_hold_active():
        return CheckResult(
            label, "ok", f"no staged-startup hold in flight ({marker})",
            reason=REASON_STARTUP_HOLD_NONE,
        )
    state = load_startup_load_state()
    status = str(state.get("status") or "unknown")
    if status == "loaded":
        return CheckResult(
            label, "ok",
            f"staged-startup hold held by an in-flight protected load ({marker})",
            reason=REASON_STARTUP_HOLD_IN_FLIGHT,
        )
    anchor = str(state.get("candidate_config_path") or "")
    live = evidence.camilla_config_path() or ""
    on_anchor = bool(anchor and live and Path(anchor) == Path(live))
    return CheckResult(
        label, "fail" if on_anchor else "warn",
        f"stale staged-startup hold at {marker}: the startup load is "
        f"'{status}', not 'loaded', so no commission is in flight, and the live "
        f"graph ({live or 'unknown'}) is "
        + ("still the anchor it staged, so the selector keeps preserving that "
           "silent graph instead of the saved baseline."
           if on_anchor else
           f"not the anchor it staged ({anchor or 'unknown'}), so the hold "
           "silences nothing and /run empties at the next boot.")
        + " Roll the startup load back from http://jts.local/sound/.",
        speaker_silent=on_anchor,
        reason=REASON_STARTUP_HOLD_STALE,
    )


@doctor_check(label="room correction authority")
def check_room_correction_authority() -> CheckResult:
    """Room correction runs unproven — this is the line that says so.

    Ruling S10 and ADR-0019: only a RECEIPT denial lets the run proceed and
    bank nothing (`ok`, the disclosure this line exists for); every other
    denial stops room correction outright, so it warns. Never FAIL, and the
    denials do not share one line because they do not share a remedy (ADR-0196).
    """

    from ...active_speaker._common import (
        ROOM_AUTHORITY_RECEIPT_ABSENT,
        ROOM_AUTHORITY_RECEIPT_MALFORMED,
        ROOM_AUTHORITY_RECEIPT_STALE,
        ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
        ROOM_AUTHORITY_RECEIPT_UNREADABLE,
    )
    label = "room correction authority"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    acoustic = status.get("acoustic_commissioning")
    if not isinstance(acoustic, dict):
        return CheckResult(
            label, "warn", "speaker setup published no room decision",
            reason=REASON_ROOM_AUTHORITY_NO_DECISION,
        )
    if acoustic.get("required") is not True:
        return CheckResult(
            label, "skipped", "room correction needs no speaker authority",
            reason=REASON_ROOM_AUTHORITY_NOT_REQUIRED,
        )
    if acoustic.get("allowed") is True:
        return CheckResult(
            label, "ok",
            f"room correction is banked under {acoustic.get('authority')}",
        )
    denial = str(acoustic.get("reason") or "")
    detail = str(acoustic.get("detail") or "")
    cause = str(acoustic.get("cause") or "")
    if denial == ROOM_AUTHORITY_RECEIPT_ABSENT:
        # The state every uncommissioned speaker is in, hence `ok`. ABSENT is
        # also the module's catch-all default, so it covers a receipt that
        # VANISHED under a verified lifecycle; forwarding `cause` keeps that
        # sub-state visible without turning it into a nag.
        return CheckResult(
            label, "ok",
            f"room correction runs unbanked ({denial})"
            + (f": {cause}" if cause else ""),
            reason=REASON_ROOM_AUTHORITY_UNBANKED,
        )
    if denial == ROOM_AUTHORITY_RECEIPT_UNREADABLE:
        # A machine fault, not a verdict on the record: the file and errno are
        # the sentence that ends the incident. Without them an operator reads
        # "unproven" and goes looking for a mint that was never the problem.
        return CheckResult(
            label, "warn",
            "room correction cannot read its commissioning record "
            f"({cause or denial}): {detail}",
            reason=REASON_ROOM_AUTHORITY_RECEIPT_UNREADABLE,
        )
    if denial in {
        ROOM_AUTHORITY_RECEIPT_STALE,
        ROOM_AUTHORITY_RECEIPT_MALFORMED,
        ROOM_AUTHORITY_RECEIPT_SUPERSEDED,
    }:
        return CheckResult(
            label, "ok", f"room correction runs unproven ({denial}): {detail}",
            reason=REASON_ROOM_AUTHORITY_UNPROVEN,
        )
    return CheckResult(
        label, "warn",
        f"room correction is blocked, not merely unbanked ({denial}): {detail}",
        reason=REASON_ROOM_AUTHORITY_BLOCKED,
    )


@doctor_check(label="active speaker setup notices")
def check_active_speaker_setup_notices() -> CheckResult:
    """The standing home for setup facts that no longer stop anything.

    Ruling S10 and ADR-0019 turn staleness and unproven-ness into disclosures
    rather than blocks; nothing else renders a non-blocker setup issue.
    Blockers keep their own surfaces (`/state`, the landing page, the volume
    and grouping refusals) and are not repeated here.
    """

    label = "active speaker setup notices"
    try:
        status = evidence.active_speaker_setup_status()
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        return CheckResult(
            label, "warn", f"could not read speaker setup: {exc}",
            reason=REASON_SPEAKER_SETUP_UNREADABLE,
        )
    issues = status.get("issues")
    notices = [
        issue for issue in (issues if isinstance(issues, list) else [])
        if isinstance(issue, dict) and issue.get("severity") != "blocker"
    ]
    if not notices:
        return CheckResult(
            label, "ok", "no standing speaker setup notices",
            reason=REASON_SETUP_NOTICES_NONE,
        )
    return CheckResult(
        label, "ok",
        "; ".join(
            f"{issue.get('code')}: {issue.get('message')}" for issue in notices
        ),
        reason=REASON_SETUP_NOTICES_STANDING,
    )
