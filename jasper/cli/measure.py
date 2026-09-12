# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measure one microphone placement through ``TuningSession`` (ADR-0188 §4).

Run as root: the CamillaDSP socket and session-volume record are root-owned.
Exit 0 when every requested stimulus has a banked take without an incident,
1 on an incomplete run or refusal, 2 on invalid measurement flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import secrets
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.cli._logging import CLI_LOG_FORMAT
from jasper.cli._refusal import (
    EXIT_OK as EXIT_OK,
    answered,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    STATUS_BY_CODE,
    failed,
)
from jasper.cli._stimulus_args import add_stimulus_args, spec_kwargs_from_args
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

#: Authority tier for the generated tool-menu index
#: (docs/tuning-operator-runbook.md's "The tool menu"; ADR-0204).
AUTHORITY_TIER = "measured"

#: A variant axis was set with no ``--candidate-id`` to select the takes by.
REFUSE_CANDIDATE_ID_REQUIRED = "measure_candidate_id_required"
#: The flags do not describe a measurement the engine accepts. The spec's own
#: sentence rides in ``detail``.
REFUSE_SPEC_INVALID = "measure_spec_invalid"
#: The box cannot be measured as it stands — no confirmed safety profile, no
#: 2-way preset, no active output, no resolvable excitation limits.
REFUSE_BOX_NOT_READY = "measure_box_not_ready"
#: No measurement microphone answered, so nothing would record the stimulus.
REFUSE_NO_MIC = "measure_no_wired_mic"
REFUSE_SPL_CEILINGS_MIXED = "measure_spl_ceilings_mixed"
REFUSE_VOLUME_REQUIRES_SPL_WATCH = "measure_volume_requires_spl_watch"
#: ``--level-matched`` on a box whose banked evidence names no trims. Refused
#: at open, where an operator can still act on it.
REFUSE_NO_LEVEL_EVIDENCE = "measure_no_level_match_evidence"
#: More than one ``--position`` in one invocation: this door has no mover seam,
#: so N bearings would bank N ``position_deg`` values nothing moved to (S12).

#: ``--specs`` could not be read, or does not hold a non-empty list of mappings.
REFUSE_SPECS_UNREADABLE = "measure_specs_file_unreadable"
#: A specs file whose entries disagree about the pose. A batch measures ONE
#: microphone placement.
REFUSE_SPECS_MIXED_POSE = "measure_specs_mixed_pose"
#: ``--specs`` given beside the flags that describe one take: two sources of
#: truth for one spec, refused rather than merged behind a precedence rule.
REFUSE_SPECS_WITH_TAKE_FLAGS = "measure_specs_with_take_flags"

#: The running measurement graph could not be re-proven mid-walk, or could not
#: be put back at the door's own exit after an otherwise clean batch.
REFUSE_GRAPH_LOST = "measure_graph_lost"
#: The measurement isolation window was lost mid-walk, so household audio could
#: re-enter the mix. ``play_program`` stops before it does.
REFUSE_ISOLATION_LOST = "measure_isolation_lost"
#: The measurement volume stopped being open, confirmed and fresh mid-walk.
REFUSE_VOLUME_LOST = "measure_volume_lost"
#: The evidence store stopped accepting writes mid-walk. Later specs would play
#: sweeps whose takes nothing keeps, so the batch aborts as a partial result.
REFUSE_STORE_LOST = "measure_evidence_store_lost"
#: The operator interrupted the run. Named like the other three because it ends
#: the batch the same way and needs the ids of what already banked.
REFUSE_CANCELLED = "measure_cancelled"
REFUSE_INCOMPLETE = "measure_incomplete"

#: This door's identity on the mux diagnostic gate. ``mux.FANIN_TEST_OWNERS`` is
#: a CLOSED allowlist, so the name must be registered there; every lease and
#: crash-recovery read files the hold under this name.
DOOR_GATE_OWNER = "jasper-measure"

__all__ = [
    "BoxDeclaration",
    "BoxNotMeasurable",
    "MeasureFlagError",
    "MeasureInterrupted",
    "MeasureRestoreFailed",
    "build_parser",
    "main",
    "read_box_declaration",
    "spec_from_args",
    "specs_from_args",
]


class MeasureFlagError(ValueError):
    """The flags do not describe a measurement. Carries a code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class BoxNotMeasurable(RuntimeError):
    """This speaker cannot be measured as it stands. Carries a code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class MeasureInterrupted(RuntimeError):
    """A walk stopped part-way, with takes already banked.

    Carries the ids of the records that DID land; without them the takes are on
    disk under names only a directory scan could recover.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        session: Any,
        store: Any,
        *,
        spec: Any,
        spec_index: int,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.record_ids = list(session.banked_record_ids)
        self.playback = session.last_playback.as_dict()
        self.bundle_dir = str(store.bundle_dir)
        self.session_id = str(session.session_id)
        #: WHICH spec was in flight, and its 1-based place in the batch: the
        #: ids alone cannot say where a batch stopped.
        self.spec = spec
        self.spec_index = int(spec_index)
        super().__init__(f"{reason}: {detail}")


class MeasureRestoreFailed(RuntimeError):
    """The batch measured cleanly; only the door's own exit could not restore.

    Distinct from :class:`MeasureInterrupted`: no spec stopped in flight, so
    every spec the batch asked for is already in ``report``.
    """

    def __init__(self, reason: str, detail: str, report: dict[str, Any]) -> None:
        self.reason = reason
        self.detail = detail
        self.report = report
        super().__init__(f"{reason}: {detail}")




@dataclass(frozen=True)
class BoxDeclaration:
    """Everything one session needs that the SPEAKER answers, not the operator.

    Read on the box at open, from the same owners the wizard reads: nothing
    here is a flag, because a measurement graph carrying protection sections,
    caps or a level match somebody typed has a safety argument nobody checked.
    """

    topology: Any
    preset: Any
    safety_profile: Mapping[str, Any]
    role_targets: Mapping[str, str]
    declared_sensitivities: Mapping[str, float]
    playback_device: str
    protection_sections_by_role: Mapping[str, Any]
    roles_bands: tuple[Any, ...]
    caps_dbfs: Mapping[str, float]
    sweep_duration_limits_s: Mapping[str, float]
    fc_hz: float
    session_volume_db: float


def read_box_declaration() -> BoxDeclaration:
    """The speaker's own declarations, or a typed refusal naming what is missing.

    ONE owner: ``resolve_conductor_context`` resolves the preset, the per-role
    bands/caps/duration limits, the targets, the session volume and the
    playback device, and refuses fail-closed naming what to finish first. This
    door adds only what a wired session needs on top — the confirmed per-role
    protection — and the 2-way scope its measurement graph is built for.

    It measures the box as DECLARED and never repairs it, so a preview that is
    not already staged refuses here rather than reaching
    ``ensure_crossover_preview_ready``'s regenerate branch: repairing the
    design inputs would be setup under a measurement's name.
    """
    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.crossover_v2.conductor_context import (
        conductor_status,
        resolve_conductor_context,
    )
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.output_topology import (
        load_output_topology,
        topology_is_subless_passive_mains,
    )

    if topology_is_subless_passive_mains(load_output_topology()):
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "this box has no active crossover to measure",
        )
    preview = load_crossover_preview(current_design_draft=load_design_draft())
    if preview.get("status") != "ready_for_protected_staging":
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the crossover preview is not staged for the current design; "
            "finish speaker setup at http://jts.local/sound/",
        )
    try:
        context = resolve_conductor_context(conductor_status())
    except CrossoverV2Refused as exc:
        raise BoxNotMeasurable(REFUSE_BOX_NOT_READY, str(exc)) from exc
    if context.preset.way_count != 2:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the measurement graph is scoped to 2-way presets; this box "
            f"declares {context.preset.way_count}",
        )
    if context.fc_hz is None:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "this box declares no crossover corner to measure around",
        )
    try:
        protection = confirmed_protection_sections(
            context.safety_profile, context.role_targets
        )
    except ValueError as exc:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the confirmed per-role protection could not be resolved",
        ) from exc
    return BoxDeclaration(
        topology=context.topology,
        preset=context.preset,
        safety_profile=context.safety_profile,
        role_targets=context.role_targets,
        declared_sensitivities=context.declared_sensitivities,
        playback_device=context.playback_device,
        protection_sections_by_role=protection,
        roles_bands=context.roles_bands,
        caps_dbfs=context.driver_caps_dbfs,
        sweep_duration_limits_s=context.driver_sweep_duration_limits_s,
        fc_hz=context.fc_hz,
        session_volume_db=context.session_volume_db,
    )


def _variant_axes(spec: Any) -> tuple[str, ...]:
    """The axes that make a take a VARIANT rather than the plain measurement.

    Read off the built spec rather than off flags, so one rule serves both the
    flag layer and the ``--specs`` file.
    """
    from jasper.active_speaker.crossover_v2.contracts import POLARITY_INVERTED

    return tuple(
        axis
        for axis, chosen in (
            ("polarity=inverted", spec.polarity == POLARITY_INVERTED),
            ("delayed_role", bool(spec.delayed_role)),
            ("level_matched", bool(spec.level_matched)),
        )
        if chosen
    )


def _require_candidate_id(spec: Any, *, where: str) -> None:
    """A variant spec must name the candidate id that selects its takes.

    The rule lives in the door, not on the spec: a wizard-built spec already
    carries a candidate id, and an engine-side refusal would be a new gate on a
    shipped shape.
    """
    axes = _variant_axes(spec)
    if axes and not spec.candidate_id:
        raise MeasureFlagError(
            REFUSE_CANDIDATE_ID_REQUIRED,
            f"a variant take needs a candidate id to select it from its "
            f"siblings; {where} sets {', '.join(axes)} and names no candidate",
        )


def spec_from_args(args: argparse.Namespace) -> Any:
    """One :class:`MeasureSpec` from the flags, candidate-id rule included."""
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    if len(args.position) > 1:
        # This door has no mover seam: N bearings would play back-to-back from
        # ONE placement and bank N ``position_deg`` values nothing moved to,
        # the silent wrong measurement ruling S12 refuses.
        raise MeasureFlagError(
            REFUSE_SPEC_INVALID,
            "this door prompts nobody to move the microphone, so it measures "
            f"one bearing per run; got {len(args.position)} --position values "
            f"({', '.join(str(deg) for deg in args.position)}). Run it once "
            "per placement",
        )
    try:
        spec = MeasureSpec(
            kind=args.kind,
            positions=tuple(args.position),
            pose_prompts=tuple(args.prompt),
            position_axis=args.axis,
            vertical_deg=args.vertical_deg,
            regime=args.regime,
            graph_scope=args.graph_scope,
            candidate_id=args.candidate_id.strip(),
            **spec_kwargs_from_args(args),
        )
    except ValueError as exc:
        raise MeasureFlagError(REFUSE_SPEC_INVALID, str(exc)) from exc
    _require_candidate_id(spec, where="the flags")
    return spec


#: The flags that describe the RUN rather than a take, so a document naming
#: every take still sits beside them: the level it plays at, which microphone
#: records.
_RUN_STATED_FLAGS = ("volume_db", "mic_serial")

#: The batch-wide defaults a ``--specs`` entry may omit, so they are not a
#: second source of truth beside the file.
_SPECS_FILE_DEFAULTS = ("specs", "kind", "graph_scope", "axis", "vertical_deg", "regime")


def _flags_a_document_states(
    args: argparse.Namespace, *, coexist: Sequence[str] = (),
) -> list[str]:
    """The flags this invocation TYPED that a stated document already names.

    The COMPLEMENT, read off :func:`build_parser` itself rather than an
    allowlist: a flag added to the parser is refused beside a document that
    states every take, without a second list learning its name. Compared
    against the parser's OWN defaults, not against truthiness: ``--polarity``
    defaults to the truthy string ``normal``.
    """
    stated = build_parser()
    # ``func`` is the subcommand handler, not a flag anybody types.
    skip = {"func", *_RUN_STATED_FLAGS, *coexist}
    return [
        dest for dest in vars(stated.parse_args([]))
        if dest not in skip and getattr(args, dest) != stated.get_default(dest)
    ]


def specs_from_args(args: argparse.Namespace) -> tuple[Any, ...]:
    """Every spec this invocation measures, against ONE microphone placement.

    Without ``--specs`` that is the single spec the flags describe. With it,
    each file entry IS a :class:`MeasureSpec` mapping and the flags supply the
    defaults an entry does not name. A file whose entries disagree about the
    pose — bearing, prompts, axis or elevation — is refused here.
    """
    if not args.kind:
        raise MeasureFlagError(
            REFUSE_SPEC_INVALID,
            "--kind names what this run measures; give it or use --specs",
        )
    if not args.specs:
        return (spec_from_args(args),)
    named = _flags_a_document_states(args, coexist=_SPECS_FILE_DEFAULTS)
    if named:
        raise MeasureFlagError(
            REFUSE_SPECS_WITH_TAKE_FLAGS,
            "--specs names every take in the batch, so the per-take flags have "
            f"nothing left to describe; drop {', '.join('--' + flag.replace('_', '-') for flag in named)} "
            "or drop --specs",
        )
    specs = _specs_from_file(args)
    poses = {
        # ``positions=()`` and ``positions=(0,)`` name the same pose
        # (:class:`MeasureSpec`'s contract). The prompt is part of the
        # placement too: it is what the mover was told.
        (spec.positions or (0,), spec.pose_prompts, spec.position_axis,
         spec.vertical_deg)
        for spec in specs
    }
    if len(poses) > 1:
        raise MeasureFlagError(
            REFUSE_SPECS_MIXED_POSE,
            "a batch measures ONE microphone placement, and nothing here moves "
            f"the microphone between specs; this file names {len(poses)} poses "
            "(bearing, prompts, axis, elevation). Split it into one file per "
            "placement",
        )
    return specs


def _specs_from_file(args: argparse.Namespace) -> tuple[Any, ...]:
    """The file's entries as specs, each one held to the same rules as a flag run."""
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    try:
        document = json.loads(Path(args.specs).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MeasureFlagError(REFUSE_SPECS_UNREADABLE, str(exc)) from exc
    if not isinstance(document, list) or not document:
        raise MeasureFlagError(
            REFUSE_SPECS_UNREADABLE,
            "a specs file is a non-empty JSON list of MeasureSpec mappings",
        )
    defaults = {
        "kind": args.kind,
        "position_axis": args.axis,
        "vertical_deg": args.vertical_deg,
        "regime": args.regime,
        "graph_scope": args.graph_scope,
        "spl_ceiling_db_spl": args.spl_ceiling_db_spl,
    }
    specs = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise MeasureFlagError(
                REFUSE_SPECS_UNREADABLE,
                f"spec {index} is not a mapping",
            )
        try:
            # ``MeasureSpec`` owns its own JSON shape, so a raw entry gets
            # argparse's typing from the class the flags build too -- one
            # vocabulary for "this is not a spec", whichever door said it.
            spec = MeasureSpec.from_mapping({**defaults, **entry})
        except ValueError as exc:
            raise MeasureFlagError(
                REFUSE_SPEC_INVALID, f"spec {index}: {exc}",
            ) from exc
        if len(spec.positions) > 1:
            # The same rule a second ``--position`` meets: nothing here moves
            # the microphone, so one entry states at most one placement.
            raise MeasureFlagError(
                REFUSE_SPEC_INVALID,
                f"spec {index} names {len(spec.positions)} bearings "
                f"({', '.join(str(deg) for deg in spec.positions)}), and "
                "nothing here moves the microphone between them; one entry "
                "measures one placement",
            )
        _require_candidate_id(spec, where=f"spec {index}")
        specs.append(spec)
    if len({spec.spl_ceiling_db_spl for spec in specs}) > 1:
        raise MeasureFlagError(REFUSE_SPL_CEILINGS_MIXED, "one batch must use one SPL ceiling")
    return tuple(specs)




def _level_match_trims(box: BoxDeclaration) -> dict[str, float]:
    """This box's own per-driver level offsets, from its banked evidence.

    Asked of :func:`~jasper.active_speaker.baseline_profile.measured_level_trims`,
    the one owner of which evidence source wins. Empty means the box has
    nothing to level by and the caller refuses.
    """
    from jasper.active_speaker.baseline_profile import measured_level_trims
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.measurement import load_measurement_state

    trims, _meta = measured_level_trims(
        box.preset,
        load_measurement_state(box.topology) or {},
        load_crossover_preview() or {},
    )
    return {str(role): float(db) for role, db in trims.items()}


def _bind_compose(
    *, box: BoxDeclaration, store: Any, session_id: str, cam_factory: Any,
    config_dir: str, graph: Any, measurement_profile: Any = None,
) -> Any:
    from jasper.active_speaker.crossover_v2.composition import bind_program_composer
    from jasper.active_speaker.crossover_v2.measure_spec import CANDIDATE_SCOPES, GRAPH_SCOPE_DRIVERS
    from jasper.active_speaker.candidate_bank import find_banked_candidate
    from jasper.active_speaker.measurement_emit import measurement_bass_extension
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS
    from jasper.active_speaker.program_playback import ProgramPlaybackError
    from jasper.active_speaker.volume_latch import MeasurementFaderDrift, hold_fader_at

    excitation = SessionExcitation(
        roles=box.roles_bands, caps_dbfs=box.caps_dbfs,
        session_volume_db=box.session_volume_db, fc_hz=box.fc_hz,
        sweep_duration_limits_s=box.sweep_duration_limits_s,
    )

    def program_for_spec(spec: Any, stimulus_dbfs: float | None) -> Any:
        peak = BASE_STIMULUS_PEAK_DBFS if stimulus_dbfs is None else stimulus_dbfs
        if spec.graph_scope == GRAPH_SCOPE_DRIVERS:
            return excitation.measure_program({role.role: peak for role in box.roles_bands})
        summed = replace(excitation, summed_sweep_band_hz=spec.sweep_band_hz or None)
        return summed.verify_program(extra_backoff_db=BASE_STIMULUS_PEAK_DBFS - peak, sweep_s=spec.sweep_s)

    async def before_play(spec: Any, program: Any, artifact: Any, phase: str) -> None:
        cam = cam_factory()
        try:
            await hold_fader_at(
                box.session_volume_db, lambda: cam.get_volume_db(best_effort=False),
                context=f"cli_measure:{phase}",
            )
        except MeasurementFaderDrift as exc:
            raise ProgramPlaybackError(str(exc)) from exc

    def bass_for_spec(spec: Any) -> Mapping[str, Any]:
        if measurement_profile is None:
            return {}
        candidate = (
            find_banked_candidate(spec.candidate_id).candidate
            if spec.graph_scope in CANDIDATE_SCOPES else None
        )
        return measurement_bass_extension(
            scope=spec.graph_scope, candidate=candidate,
        )

    return bind_program_composer(
        program_for_spec=program_for_spec, store=store,
        capture_session_id=session_id, cam_factory=cam_factory,
        config_dir=config_dir, topology=box.topology,
        safety_profile=box.safety_profile, role_targets=box.role_targets,
        declared_sensitivities=box.declared_sensitivities,
        before_play=before_play, graph_yaml=graph.installed_graph_yaml,
        bass_extension_for_spec=bass_for_spec,
    )


def _spl_monitor(
    stated: float | None,
    *,
    box: BoxDeclaration,
    device: Any,
    mic_serial: str | None,
    volume_db: float | None,
) -> tuple[Any, str]:
    from jasper.active_speaker.angle_capture import LateralWalkRefused  # lazy: measurement stack import cost
    from jasper.cli.measurement_watch import measurement_spl_watch  # lazy: measurement stack import cost

    try:
        monitor, note = measurement_spl_watch(
            stated, topology=box.topology, preset=box.preset, device=device, mic_serial=mic_serial,
        )
    except LateralWalkRefused as exc:
        raise BoxNotMeasurable(exc.reason, exc.detail) from exc
    if volume_db is not None and monitor is None:
        raise BoxNotMeasurable(
            REFUSE_VOLUME_REQUIRES_SPL_WATCH,
            "--volume-db requires a resolvable microphone sensitivity for the live SPL watch",
        )
    return monitor, note


def _wired_setup_reference() -> Mapping[str, Any] | None:
    from jasper.active_speaker.crossover_v2.sweep_spec import DefaultSetupCalibration
    from jasper.audio_measurement.household_mic import resolved_household_mic
    from jasper.audio_measurement.wired_capture import setup_from_hint

    found = resolved_household_mic()
    if found is None:
        return None
    household, calibration = found
    return setup_from_hint(DefaultSetupCalibration(
        mode="upload" if household.provider == "manual_upload" else "serial",
        model=household.model_key, calibration_id=calibration.calibration_id,
        resolvable=True,
    ))


async def _measure(
    specs: tuple[Any, ...],
    box: BoxDeclaration,
    *,
    mic_serial: str | None = None,
    volume_db: float | None = None,
) -> dict[str, Any]:
    """Open the door once, run the plan through it, close, and report.

    The spec batch is the plan at one placement. Its loop is
    :mod:`~jasper.active_speaker.plan_run`'s, so what ends a run and what a take
    reports have one owner.

    One session hold for the whole batch: the physical cost is the microphone
    move, and the graph's variant emit-cache makes each swap a single
    ``SetConfig``. The engine's give-back runs inside the door's, so the door's
    ``finally`` finds idempotent no-ops and lands the durable snapshot last.

    The bundle is opened INSIDE the door, and that ordering is a safety
    property: ``open_bundle`` marks every prior ``open`` bundle ``abandoned``,
    stripping retention protection off a wizard session's evidence — doing that
    before the interlock would hit a LIVE session and then be refused.
    """
    from jasper.active_speaker.run_manifest import RunManifest, incumbent_fingerprints  # lazy: measurement stack
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
    from jasper.active_speaker.bundles import mark_state, open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore, CommissioningEvidenceStoreError,
    )
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams
    from jasper.active_speaker.crossover_v2.door import measurement_door
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from jasper.active_speaker.crossover_v2.session import TuningSession
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.wired_capture import (
        WiredMicMissing,
        require_wired_mic,
    )
    from jasper.camilla import primary_controller
    from jasper.active_speaker.crossover_v2.wired_stimulus import (
        CapturedRecordStore, WiredStimulusCapture,
    )

    # Resolved ONCE for the batch and asked for by ANY spec in it: the trims
    # are a property of the speaker, not of a take. Refused before the door
    # opens, where an operator can still act on it.
    wants_level_match = any(spec.level_matched for spec in specs)
    trims = _level_match_trims(box) if wants_level_match else {}
    if wants_level_match and not trims:
        raise BoxNotMeasurable(
            REFUSE_NO_LEVEL_EVIDENCE,
            "this box has banked no per-driver level evidence, so a "
            "level-matched take would measure unmatched branches",
        )
    try:
        device = require_wired_mic()
    except WiredMicMissing as exc:
        # The kernel owns the sentence; this door owns only its exit code.
        raise BoxNotMeasurable(REFUSE_NO_MIC, str(exc)) from exc
    spl_monitor, spl_note = _spl_monitor(
        specs[0].spl_ceiling_db_spl,
        box=box, device=device, mic_serial=mic_serial, volume_db=volume_db,
    )
    if volume_db is not None:
        box = replace(box, session_volume_db=volume_db)

    session_id = f"measure-{secrets.token_hex(4)}"
    config_dir = str(DEFAULT_CAMILLA_CONFIG_DIR)
    cam_factory = primary_controller

    # Set before the door opens, so a restore failure below always has
    # something to report against.
    outcomes: tuple[tuple[Any, str], ...] = ()
    store: Any = None
    bundle_dir: Path | None = None
    try:
        measurement_profile = MeasurementGraphProfile(
            preset=box.preset,
            topology=box.topology,
            role_channels={"woofer": 0, "tweeter": 1},
            playback_device=box.playback_device,
            protection_sections_by_role=box.protection_sections_by_role,
        )
        async with measurement_door(
            profile=measurement_profile,
            spl_monitor=spl_monitor,
            measurement_volume_db=box.session_volume_db,
            camilla_factory=cam_factory,
            action="measuring",
            config_dir=config_dir,
            gate_owner=DOOR_GATE_OWNER,
        ) as door:
            info = open_bundle(box.topology, calibration_id="")
            if not isinstance(info, Mapping) or not info.get("session_id"):
                raise BoxNotMeasurable(
                    REFUSE_BOX_NOT_READY,
                    "could not open a commissioning evidence bundle for this "
                    "session",
                )
            bundle_dir = Path(str(info["bundle_dir"]))
            store = CommissioningEvidenceStore.open(
                bundle_dir,
                expected_session_id=str(info["session_id"]),
            )
            manifest = RunManifest(session_id, BankedRecordStore(store, session_id),
                                   incumbent=incumbent_fingerprints(load_applied_baseline_profile_state()))
            capture = WiredStimulusCapture(
                device=device, bundle_dir=Path(store.bundle_dir),
                setup_reference=_wired_setup_reference,
                spl_monitor=door.spl_monitor,
                read_loudness_volume_db=lambda: door.measurement_loudness_volume_db,
            )
            seams = bind_engine_seams(
                session_graph=door.graph,
                records=CapturedRecordStore(
                    inner=manifest,
                    capture=capture,
                ),
                volume_claim=door.claim,
                session_volume_plan=door.plan,
                compose_stimulus=_bind_compose(
                    box=box,
                    store=store,
                    session_id=session_id,
                    cam_factory=cam_factory,
                    config_dir=config_dir,
                    graph=door.graph,
                    measurement_profile=measurement_profile,
                ),
                capture_stimulus=capture,
            )
            async with TuningSession(
                session_id=session_id,
                allocate_take_id=manifest.allocate_take_id,
                seams=seams,
                measurement_level_db=box.session_volume_db,
                level_match_trims_db=trims,
            ) as session:
                try:
                    result = await _ran(
                        session, specs, manifest=manifest,
                        analyze=partial(_analyze_take, Path(store.bundle_dir), manifest, box.fc_hz),
                        gain_ceiling_db=box.caps_dbfs,
                        spl_monitor=spl_note,
                    )
                except CommissioningEvidenceStoreError as exc:
                    index = (manifest.stopped_at or {}).get("index", 1)
                    raise MeasureInterrupted(
                        REFUSE_STORE_LOST, str(exc), session, store,
                        spec=manifest.specs.get(index, specs[0]), spec_index=index,
                    ) from exc
                outcomes = tuple(result.outcomes)
                if result.stopped_at is not None:
                    # A cancellation is CONVERTED rather than re-raised: the
                    # operator interrupting a long run most needs the ids of
                    # what banked, and the door's give-back still runs shielded
                    # on the way out.
                    stopped_at = result.stopped_at["index"]
                    raise MeasureInterrupted(
                        result.reason, result.detail, session, store,
                        spec=result.specs[stopped_at], spec_index=stopped_at,
                    )
                if result.reason and not result.attempts:
                    raise BoxNotMeasurable(result.reason, result.detail)
    except SessionGraphError as exc:
        if store is None:
            raise
        # ``TuningSession.close`` and the door's own `finally` both restore this
        # SAME graph handle on a clean exit; either can raise OUTSIDE
        # the run's per-take catch. `outcomes` already holds every spec this
        # batch earned.
        raise MeasureRestoreFailed(
            REFUSE_GRAPH_LOST, str(exc),
            _report(outcomes, store=store, session_id=session_id),
        ) from exc
    finally:
        bundle_closed = bundle_dir is None or mark_state(bundle_dir, "closed") is not None
    report = _report(outcomes, store=store, session_id=session_id)
    # The package's own path and its counts; the take ROWS stay in the package,
    # which is where an unbounded list belongs (ADR-0237).
    report["run_manifest"] = result.path
    if result.status != "complete":
        report["status"] = "incomplete"
    report["spl_monitor"] = result.spl_monitor
    report["measurement_volume_db"] = box.session_volume_db
    report["measurement_loudness_volume_db"] = door.measurement_loudness_volume_db
    if not bundle_closed:
        report["status"] = "incomplete"
        report["bundle_failure"] = REFUSE_STORE_LOST
        report.pop("next", None)
    return report


def _session_scoped_aborts() -> dict[type[BaseException], str]:
    """The failures that end the RUN, by TYPE, and the reason each reports.

    An operator's Ctrl-C is HERE as ``CancelledError`` and under no second name:
    ``asyncio.run`` cancels the main task on SIGINT, so a suspended frame is
    never handed a ``KeyboardInterrupt``.

    The scope split is drawn by exception type at this one site and nowhere
    else — no string matching, no runtime judgement. Everything here is a
    property of what the whole batch stands on. A failure scoped to one
    stimulus never appears: the play transaction turns it into a typed
    ``incident``, which is what lets the batch carry on and disclose it.
    """
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStoreError,
    )
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.measurement_window import MeasurementWindowError

    return {
        SessionGraphError: REFUSE_GRAPH_LOST,
        SessionVolumePlanError: REFUSE_VOLUME_LOST,
        MeasurementWindowError: REFUSE_ISOLATION_LOST,
        CommissioningEvidenceStoreError: REFUSE_STORE_LOST,
        asyncio.CancelledError: REFUSE_CANCELLED,
    }


async def _ran(
    session: Any,
    specs: tuple[Any, ...],
    *,
    spl_monitor: str,
    manifest: Any,
    analyze: Any,
    gain_ceiling_db: Mapping[str, float],
) -> Any:
    from jasper.active_speaker import plan_run  # lazy: executor import cost

    return await plan_run.run_specs(
        specs, session=session, manifest=manifest, analyze=analyze,
        aborts=_session_scoped_aborts(), spl_monitor=spl_monitor,
        gain_ceiling_db=gain_ceiling_db,
    )


def _analyze_take(bundle_dir: Path, manifest: Any, fc_hz: float, record: Mapping[str, Any], record_id: str) -> Any:
    from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT  # lazy: measurement stack
    from jasper.active_speaker.crossover_v2.record_index import reopen_measurement_capture  # lazy: measurement stack
    from jasper.audio_measurement.evidence_identity import json_fingerprint  # lazy: measurement stack
    from jasper.audio_measurement.gating import SEAT_EXEMPT  # lazy: numpy
    from jasper.audio_measurement.household_mic import resolve_setup_calibration  # lazy: measurement stack
    from jasper.audio_measurement.program import ExcitationProgram  # lazy: numpy
    from jasper.audio_measurement.program_analysis import (  # lazy: numpy
        MeasurementGeometry, MeasurementPriors, analyze_program_capture,
    )
    from jasper.audio_measurement.wired_capture import decode_wav_to_mono  # lazy: numpy

    _, wav = reopen_measurement_capture(bundle_dir, f"{EVIDENCE_ROOT}/artifacts/{record_id}")
    program = ExcitationProgram.from_dict(record["program"])
    calibration = resolve_setup_calibration(record.get("capture_setup"), device=record.get("capture_device"))
    manifest.calibration = {"id": calibration.calibration_id if calibration else None,
                            "curve_fingerprint": json_fingerprint(calibration.curve.to_dict()) if calibration else None}
    if wav is None:
        raise ValueError("capture WAV missing")
    samples, rate = decode_wav_to_mono(wav)
    return analyze_program_capture(
        program, samples, rate, calibration=calibration.curve if calibration else None,
        priors=MeasurementPriors(crossover_fc_hz=fc_hz, mic_calibrated=calibration is not None),
        geometry=MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT) if program.phase == "verify" else None,
        capture_report=record.get("capture_integrity"),
    )


def _spec_report(outcome: Any, graph_fingerprint: str) -> dict[str, Any]:
    """A spec's graph, banked takes, and any stimulus incidents."""
    from jasper.active_speaker.crossover_v2.measure_spec import stubbed_capabilities

    return {
        "candidate_id": outcome.spec.candidate_id,
        "kind": outcome.spec.kind,
        "graph_fingerprint": graph_fingerprint,
        "n_takes": len(outcome.record_ids),
        "incidents": [s.incident for s in outcome.stimuli if s.incident],
        "playback": [s.playback.as_dict() for s in outcome.stimuli],
        "stubs": [stub.code for stub in stubbed_capabilities(outcome.spec)],
    }


def _report(
    outcomes: tuple[tuple[Any, str], ...], *, store: Any, session_id: str,
) -> dict[str, Any]:
    record_ids = [
        record_id
        for outcome, _fingerprint in outcomes
        for record_id in outcome.record_ids
    ]
    complete = bool(outcomes) and all(
        outcome.complete for outcome, _fingerprint in outcomes
    )
    return {
        "status": "measured" if complete else "incomplete",
        "session_id": session_id,
        "bundle_dir": str(store.bundle_dir),
        "n_takes": len(record_ids),
        "record_ids": record_ids,
        "specs": [
            _spec_report(outcome, fingerprint)
            for outcome, fingerprint in outcomes
        ],
        "next": f"jasper-round-views inventory {store.bundle_dir}",
    }


def _refused(
    reason: str, detail: Any, *, code: int,
    refusal_code: str | None = None, next_action: Mapping[str, Any] | None = None,
) -> int:
    """One failing stage, under the word its code owns (``_refusal.py``)."""

    log_event(
        logger,
        "active_speaker.measure",
        level=logging.WARNING,
        action=STATUS_BY_CODE[code],
        reason=reason,
        detail=detail,
    )
    return failed(code, reason, detail, code=refusal_code, next_action=next_action)


def _interrupted(exc: MeasureInterrupted) -> int:
    """A run that stopped part-way — a refusal carrying what it banked.

    The ids are the only handle anybody has on takes already on disk, and
    ``stopped_at`` names the spec in flight by 1-based ``index`` as well as by
    its fields, since repeated or unlabelled entries cannot be told apart by
    fields alone.
    """
    log_event(
        logger,
        "active_speaker.measure",
        level=logging.ERROR,
        action="interrupted",
        reason=exc.reason,
        detail=exc.detail,
        banked=str(len(exc.record_ids)),
    )
    return failed(EXIT_REFUSED, "interrupted", {
        "reason": exc.reason,
        "detail": exc.detail,
        "session_id": exc.session_id,
        "bundle_dir": exc.bundle_dir,
        "record_ids": exc.record_ids,
        "playback": exc.playback,
        "stopped_at": {
            "index": exc.spec_index,
            "candidate_id": exc.spec.candidate_id,
            "kind": exc.spec.kind,
        },
    })


def _restore_failed(exc: MeasureRestoreFailed) -> int:
    """The batch's own report, with the give-back failure named beside it.

    Every id in ``exc.report`` is real evidence already on disk, so the whole
    report rides under ``detail`` rather than being dropped for the failure.
    """
    log_event(
        logger,
        "active_speaker.measure",
        level=logging.ERROR,
        action="restore_failed",
        reason=exc.reason,
        detail=exc.detail,
    )
    report = {k: v for k, v in exc.report.items() if k != "status"}
    return failed(EXIT_REFUSED, "restore_failed", {
        **report, "reason": exc.reason, "detail": exc.detail,
    })


def _cmd_measure(args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.door import MeasurementDoorRefused
    from jasper.active_speaker.measurement_emit import MeasurementGraphRefused  # lazy: graph import cost

    try:
        specs = specs_from_args(args)
    except MeasureFlagError as exc:
        return _refused(exc.reason, exc.detail, code=EXIT_UNREADABLE)
    try:
        box = read_box_declaration()
        if args.volume_db is not None:
            if not math.isfinite(args.volume_db) or not -100 <= args.volume_db <= 0:
                raise BoxNotMeasurable("measurement_volume_invalid", "volume must be within -100..0 dB")
        payload = asyncio.run(_measure(
            specs, box, mic_serial=args.mic_serial, volume_db=args.volume_db,
        ))

    except MeasureInterrupted as exc:
        return _interrupted(exc)
    except MeasureRestoreFailed as exc:
        return _restore_failed(exc)
    except MeasurementGraphRefused as exc:
        from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for  # lazy: numpy import cost

        return _refused(
            exc.reason, exc.detail, code=EXIT_REFUSED, refusal_code=exc.code,
            next_action=refusal_copy_for(exc.code)[1],
        )
    except (BoxNotMeasurable, MeasurementDoorRefused) as exc:
        return _refused(exc.reason, exc.detail, code=EXIT_REFUSED)
    if payload["status"] != "measured":
        return failed(EXIT_REFUSED, REFUSE_INCOMPLETE, {
            k: v for k, v in payload.items() if k != "status"
        })
    return answered(
        payload,
        f"measured {payload['n_takes']} take(s) into {payload['bundle_dir']}",
    )


def build_parser() -> argparse.ArgumentParser:
    from jasper.active_speaker.crossover_v2.contracts import (
        DRIVER_ROLES,
        MEASURE_KINDS,
        MEASURE_REGIMES,
        POLARITIES,
        POLARITY_NORMAL,
        POSITION_AXES,
        POSITION_AXIS_HORIZONTAL,
        REGIME_REFERENCE_AXIS,
    )

    from jasper.active_speaker.crossover_v2.measure_spec import GRAPH_SCOPES, GRAPH_SCOPE_DRIVERS

    parser = argparse.ArgumentParser(
        prog="jasper-measure",
        description="Measure this speaker once, bank the takes, print their ids",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  One on-box measurement through a temporary protected graph,\n"
            "  banked as a standard take jasper-round-views frequency can\n"
            "  read directly. For raw-driver plants or ad-hoc work outside a\n"
            "  wizard round -- jasper-round run is the\n"
            "  ordinary path through a full session.\n"
            "\n"
            "WHEN NOT TO USE\n"
            "  - to measure several PLACEMENTS in one call -- one placement\n"
            "    per run; --specs measures several MeasureSpecs at ONE\n"
            "    placement, not several placements\n"
            "\n"
            "EXAMPLE\n"
            "  jasper-measure --kind baseline --position 0\n"
            "\n"
            "EXIT CODES\n"
            "  0  EXIT_OK -- every spec measured; ids printed\n"
            "  1  EXIT_REFUSED -- incomplete takes, a refusal, an interrupt,\n"
            "     or a restore failure; any banked ids remain in detail\n"
            "  2  EXIT_UNREADABLE -- the request could not even be built: a\n"
            "     second --position, a variant axis with no --candidate-id,\n"
            "     a malformed --specs file"
        ),
    )
    parser.add_argument("--kind", choices=MEASURE_KINDS, default="")
    parser.add_argument("--volume-db", type=float, help="temporary Main and bass reference level; defaults to the saved measurement level")
    parser.add_argument("--graph-scope", choices=[scope for scope in GRAPH_SCOPES if scope != "candidate_branches"], default=GRAPH_SCOPE_DRIVERS)
    parser.add_argument(
        # ``append`` rather than a plain value so a SECOND one is visible here
        # and can be refused by name; taking the last one silently would let an
        # operator believe two bearings were walked.
        "--position",
        type=int,
        action="append",
        default=[],
        metavar="DEG",
        help=(
            "signed whole-degree bearing, ONE per run; negative is LEFT of the "
            "design axis seen from the microphone. Omitted means the design axis"
        ),
    )
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        help="what the mover was told, for the bearing this run measures",
    )
    parser.add_argument("--axis", choices=POSITION_AXES, default=POSITION_AXIS_HORIZONTAL)
    parser.add_argument(
        "--vertical-deg",
        type=int,
        default=0,
        help="signed whole-degree elevation above mark height",
    )
    parser.add_argument("--regime", choices=MEASURE_REGIMES, default=REGIME_REFERENCE_AXIS)
    parser.add_argument("--polarity", choices=POLARITIES, default=POLARITY_NORMAL)
    parser.add_argument(
        "--inverted-role",
        choices=DRIVER_ROLES,
        default="",
        help="which branch an inverted-polarity take flips",
    )
    parser.add_argument(
        "--delayed-role",
        choices=DRIVER_ROLES,
        default="",
        help="which branch carries --delay-us",
    )
    parser.add_argument("--delay-us", type=float, default=0.0)
    parser.add_argument(
        "--level-matched",
        action="store_true",
        help="carry this box's own banked per-driver level trims in the graph",
    )
    add_stimulus_args(parser)
    parser.add_argument("--mic-serial", default=None)
    parser.add_argument(
        "--candidate-id",
        default="",
        help="required whenever a variant axis is set",
    )
    parser.add_argument(
        "--specs",
        metavar="FILE",
        default="",
        help=(
            "a JSON list of MeasureSpec mappings to measure against ONE "
            "microphone placement — a preset IS a saved MeasureSpec, so the "
            "file needs no vocabulary of its own. --kind/--axis/--vertical-deg/"
            "--regime supply the defaults an entry does not name; every other "
            "per-take flag above is refused beside it, and every entry needs "
            "its own candidate id once it sets a variant axis"
        ),
    )
    parser.set_defaults(func=_cmd_measure)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # INFO floor: this door's `event=` lines are the record of which graph the
    # speaker was measured through.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    from jasper.env_load import load_env_files
    from jasper.volume_coordinator import install_env_canonical_target_provider

    load_env_files()
    # Installs this process's VolumeOwner AND the canonical target the duck
    # release reads. Without the owner there is no SESSION_MEASUREMENT rank to
    # claim the fader through.
    install_env_canonical_target_provider()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
