# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-measure`` — one measurement of this speaker, banked and named.

The general operator door onto
:class:`~jasper.active_speaker.crossover_v2.session.TuningSession`: it opens
the speaker once, plays what one ``MeasureSpec`` asks for, banks the takes and
prints their ids. It does not grade, adopt or restore a profile (ADR-0188 §4,
ruling S12). ONE placement per run — this door prompts nobody to move the
microphone, so a walk is N runs — and as many specs against it as ``--specs``
names. Run as root: the CamillaDSP socket and session-volume record are
root-owned. Exit 0 when the session opened and closed, 1 on a refusal, 2 on
flags that do not describe a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.logging_setup import LOG_FORMAT
from jasper.cli._refusal import (
    EXIT_OK as EXIT_OK,
    answered,
    EXIT_REFUSED,
    EXIT_UNREADABLE,
    STATUS_BY_CODE,
    failed,
)
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
#: ``--level-matched`` on a box whose banked evidence names no trims. Refused
#: at open, where an operator can still act on it.
REFUSE_NO_LEVEL_EVIDENCE = "measure_no_level_match_evidence"
#: More than one ``--position`` in one invocation: this door has no mover seam,
#: so N bearings would bank N ``position_deg`` values nothing moved to (S12).
REFUSE_ONE_POSITION_PER_RUN = "measure_one_position_per_run"

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
        self.bundle_dir = str(store.bundle_dir)
        self.session_id = str(session.session_id)
        #: WHICH spec was in flight, and its zero-based place in the batch: the
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
            REFUSE_ONE_POSITION_PER_RUN,
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
            polarity=args.polarity,
            inverted_role=args.inverted_role,
            level_ladder_dbfs=tuple(args.level_dbfs),
            candidate_id=args.candidate_id.strip(),
            delayed_role=args.delayed_role,
            delay_us=args.delay_us,
            level_matched=args.level_matched,
        )
    except ValueError as exc:
        raise MeasureFlagError(REFUSE_SPEC_INVALID, str(exc)) from exc
    _require_candidate_id(spec, where="the flags")
    return spec


#: The flags that describe ONE take, refused beside ``--specs`` rather than
#: merged behind a precedence rule. ``--kind``, ``--axis``, ``--vertical-deg``
#: and ``--regime`` stay off this list: they are the shared defaults a file
#: entry may omit.
_PER_TAKE_FLAGS = ("position", "prompt", "polarity", "inverted_role",
                   "delayed_role", "delay_us", "level_matched", "level_dbfs",
                   "candidate_id")


def specs_from_args(args: argparse.Namespace) -> tuple[Any, ...]:
    """Every spec this invocation measures, against ONE microphone placement.

    Without ``--specs`` that is the single spec the flags describe. With it,
    each file entry IS a :class:`MeasureSpec` mapping and the flags supply the
    defaults an entry does not name. A file whose entries disagree about the
    pose — bearing, prompts, axis or elevation — is refused here.
    """
    if not args.specs:
        return (spec_from_args(args),)
    # Compared against the parser's OWN defaults, not against truthiness:
    # ``--polarity`` defaults to the truthy string ``normal``.
    stated = build_parser()
    named = [
        flag for flag in _PER_TAKE_FLAGS
        if getattr(args, flag) != stated.get_default(flag)
    ]
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


#: The spec fields a file entry must state as JSON strings. Each is trimmed, so
#: a whitespace-only candidate id cannot pass the variant rule.
_STRING_FIELDS = (
    "kind", "position_axis", "regime", "polarity", "inverted_role",
    "delayed_role", "candidate_id",
)


def _typed_entry(index: int, entry: Mapping[str, Any]) -> dict[str, Any]:
    """argparse's typing, for a mapping that never went through argparse.

    Raw JSON can hand :class:`MeasureSpec` what no flag could — a truthy string
    for ``level_matched``, a bare string where a tuple field expects an array
    (``tuple("030")`` is three bearings), a non-finite delay. Each is refused
    here with the entry's index.
    """

    def refuse(what: str) -> MeasureFlagError:
        return MeasureFlagError(REFUSE_SPEC_INVALID, f"spec {index}: {what}")

    fields = dict(entry)
    for key in ("positions", "pose_prompts", "level_ladder_dbfs"):
        if key in fields:
            if not isinstance(fields[key], list):
                raise refuse(f"{key} must be a JSON array")
            fields[key] = tuple(fields[key])
    for key in _STRING_FIELDS:
        if key in fields:
            if not isinstance(fields[key], str):
                raise refuse(f"{key} must be a string")
            fields[key] = fields[key].strip()
    if not all(isinstance(p, str) for p in fields.get("pose_prompts", ())):
        raise refuse("pose_prompts entries must be strings")
    if not isinstance(fields.get("level_matched", False), bool):
        raise refuse("level_matched must be true or false")
    for name, values in (
        ("delay_us", (fields.get("delay_us", 0.0),)),
        ("level_ladder_dbfs", fields.get("level_ladder_dbfs", ())),
    ):
        for value in values:
            # ``bool`` is an ``int``; JSON itself can carry NaN/Infinity.
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise refuse(f"{name} must be a finite number, got {value!r}")
    return fields


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
    }
    specs = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise MeasureFlagError(
                REFUSE_SPECS_UNREADABLE,
                f"spec {index} is not a mapping",
            )
        fields = {**defaults, **_typed_entry(index, entry)}
        try:
            spec = MeasureSpec(**fields)
        except (TypeError, ValueError) as exc:
            raise MeasureFlagError(
                REFUSE_SPEC_INVALID, f"spec {index}: {exc}",
            ) from exc
        if len(spec.positions) > 1:
            # The same rule a second ``--position`` meets: nothing here moves
            # the microphone, so one entry states at most one placement.
            raise MeasureFlagError(
                REFUSE_ONE_POSITION_PER_RUN,
                f"spec {index} names {len(spec.positions)} bearings "
                f"({', '.join(str(deg) for deg in spec.positions)}), and "
                "nothing here moves the microphone between them; one entry "
                "measures one placement",
            )
        _require_candidate_id(spec, where=f"spec {index}")
        specs.append(spec)
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
    *,
    box: BoxDeclaration,
    store: Any,
    session_id: str,
    cam_factory: Any,
    config_dir: str,
) -> Any:
    """The host's ``compose``: one routed per-driver program per stimulus.

    One program shape for all three kinds — ``contracts.MEASURE_KINDS`` says a
    baseline, a candidate check and a re-measure differ by that word and by
    nothing else. ``kind`` is what the banked record SAYS the take is, not a
    second stimulus selector.

    The level is the ladder's rung in dBFS folded into the per-role gain plan,
    or the composer's reference base with no ladder. Both are clamped per role
    by :func:`~jasper.active_speaker.crossover_v2.programs.back_off_gain`
    against that driver's declared cap, and admission re-judges the rendered
    bytes against the same caps at play time.
    """
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
    )
    from jasper.active_speaker.crossover_v2.program_transaction import (
        ProgramForStimulus,
    )
    from jasper.active_speaker.crossover_v2.programs import SessionExcitation
    from jasper.audio_measurement.program import (
        BASE_STIMULUS_PEAK_DBFS,
        write_program_wav,
    )

    excitation = SessionExcitation(
        roles=box.roles_bands,
        caps_dbfs=box.caps_dbfs,
        session_volume_db=box.session_volume_db,
        fc_hz=box.fc_hz,
        sweep_duration_limits_s=box.sweep_duration_limits_s,
    )
    bundle_dir = Path(store.bundle_dir)
    minted: list[int] = []

    async def _compose(
        *,
        spec: Any,
        position_deg: int | None = None,
        prompt: str = "",
        level_db: float = 0.0,
        stimulus_dbfs: float | None = None,
    ) -> Any:
        peak = (
            BASE_STIMULUS_PEAK_DBFS if stimulus_dbfs is None else float(stimulus_dbfs)
        )
        program = excitation.measure_program(
            {band.role: peak for band in box.roles_bands}
        )
        # One WAV per stimulus, never one per phase: a ladder plays a different
        # program at every rung, and a shared name would let admission re-read
        # bytes some other rung wrote.
        ordinal = len(minted)
        minted.append(ordinal)
        wav_rel = f"crossover_v2/{session_id}/{program.phase}_program_{ordinal:02d}.wav"
        wav_path = bundle_dir / wav_rel

        def _render() -> Any:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            write_program_wav(str(wav_path), program)
            return store.identify_artifact(wav_rel)

        artifact = await asyncio.to_thread(_render)
        seams = bind_program_playback_seams(
            cam_factory(),
            bundle_dir=str(bundle_dir),
            artifact=artifact,
            config_dir=config_dir,
            program=program,
            wav_path=str(wav_path),
            topology=box.topology,
            safety_profile=box.safety_profile,
            role_targets=box.role_targets,
            session_volume_db=box.session_volume_db,
            declared_sensitivities=box.declared_sensitivities,
        )
        return ProgramForStimulus(program=program, seams=seams)

    return _compose


@dataclass(frozen=True)
class _CaptureAnnotatedStore:
    """The banked store, plus what the microphone said about the take.

    ``TuningSession`` builds a record from what the ENGINE knows, and the engine
    knows nothing about a microphone; the capture half mints a whole
    ``CaptureAnswer`` and hands the transaction only the path. Without this seam
    a CLI take banks poorer than a wizard one — no xrun counters, no mic
    identity, no calibration reference. Draining is take-and-CLEAR by the
    capture half's contract, and happens at banking because that is when the
    two facts belong to the same take.
    """

    inner: Any
    capture: Any

    async def bank(self, record: Mapping[str, Any]) -> str:
        answer = self.capture.take_answer()
        if answer is None:
            return await self.inner.bank(record)
        # Prefixed, because a record is a different namespace from an answer;
        # ``capture_integrity`` keeps the spelling existing readers know.
        annotated = {
            **record,
            **({"capture_integrity": answer.capture_integrity}
               if answer.capture_integrity else {}),
            **({"capture_device": answer.device} if answer.device else {}),
            **({"capture_setup": answer.setup} if answer.setup else {}),
        }
        return await self.inner.bank(annotated)


async def _measure(specs: tuple[Any, ...], box: BoxDeclaration) -> dict[str, Any]:
    """Open the door once, measure every spec through it, close, and report.

    One session hold for the whole batch: the physical cost is the microphone
    move, and the graph's variant emit-cache makes each swap a single
    ``SetConfig``. The engine's give-back runs inside the door's, so the door's
    ``finally`` finds idempotent no-ops and lands the durable snapshot last.

    The bundle is opened INSIDE the door, and that ordering is a safety
    property: ``open_bundle`` marks every prior ``open`` bundle ``abandoned``,
    stripping retention protection off a wizard session's evidence — doing that
    before the interlock would hit a LIVE session and then be refused.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams
    from jasper.active_speaker.crossover_v2.door import measurement_door
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from jasper.active_speaker.crossover_v2.session import TuningSession
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.wired_capture import resolve_wired_mic
    from jasper.camilla import primary_controller
    from jasper.web.correction_crossover_v2_wired import WiredStimulusCapture

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
    device = resolve_wired_mic()
    if device is None:
        raise BoxNotMeasurable(
            REFUSE_NO_MIC,
            "no measurement microphone answered; connect the UMIK and re-run",
        )

    session_id = f"measure-{secrets.token_hex(4)}"
    config_dir = str(DEFAULT_CAMILLA_CONFIG_DIR)
    cam_factory = primary_controller

    # Set before the door opens, so a restore failure below always has
    # something to report against.
    outcomes: tuple[tuple[Any, str], ...] = ()
    store: Any = None
    try:
        async with measurement_door(
            profile=MeasurementGraphProfile(
                preset=box.preset,
                topology=box.topology,
                role_channels={"woofer": 0, "tweeter": 1},
                playback_device=box.playback_device,
                protection_sections_by_role=box.protection_sections_by_role,
            ),
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
            store = CommissioningEvidenceStore.open(
                Path(str(info["bundle_dir"])),
                expected_session_id=str(info["session_id"]),
            )
            capture = WiredStimulusCapture(
                device=device, bundle_dir=Path(store.bundle_dir),
            )
            seams = bind_engine_seams(
                session_graph=door.graph,
                records=_CaptureAnnotatedStore(
                    inner=BankedRecordStore(
                        evidence=store,
                        capture_session_id=session_id,
                    ),
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
                ),
                capture_stimulus=capture,
            )
            async with TuningSession(
                session_id=session_id,
                seams=seams,
                measurement_level_db=box.session_volume_db,
                level_match_trims_db=trims,
            ) as session:
                outcomes = await _measured(session, specs, store=store)
                return _report(outcomes, store=store, session_id=session_id)
    except SessionGraphError as exc:
        if store is None:
            raise
        # ``TuningSession.close`` and the door's own `finally` both restore this
        # SAME graph handle on a clean exit; either can raise OUTSIDE
        # `_session_scoped_aborts`'s per-spec catch. `outcomes` already holds
        # every spec this batch earned.
        raise MeasureRestoreFailed(
            REFUSE_GRAPH_LOST, str(exc),
            _report(outcomes, store=store, session_id=session_id),
        ) from exc


def _session_scoped_aborts() -> tuple[tuple[type[BaseException], ...], dict[type, str]]:
    """The failures that end the BATCH, by TYPE, and the reason each reports.

    The scope split is drawn by exception type at this one site and nowhere
    else — no string matching, no runtime judgement. Everything here is a
    property of what the whole batch stands on. A failure scoped to one
    stimulus never raises here: the play transaction turns it into a typed
    ``incident``, which is what lets the batch carry on and disclose it.
    """
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStoreError,
    )
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.measurement_window import MeasurementWindowError

    reasons: dict[type, str] = {
        SessionGraphError: REFUSE_GRAPH_LOST,
        SessionVolumePlanError: REFUSE_VOLUME_LOST,
        MeasurementWindowError: REFUSE_ISOLATION_LOST,
        CommissioningEvidenceStoreError: REFUSE_STORE_LOST,
        asyncio.CancelledError: REFUSE_CANCELLED,
    }
    return tuple(reasons), reasons


async def _measured(
    session: Any, specs: tuple[Any, ...], *, store: Any,
) -> tuple[tuple[Any, str], ...]:
    """Every spec against one open session, as ``(outcome, graph fingerprint)``.

    A session-scoped failure ABORTS through :class:`MeasureInterrupted`, naming
    the spec in flight and carrying every id banked so far: the speaker is no
    longer held the way the remaining specs would be measured. A spec-scoped
    failure never reaches here — the play transaction reports it as a typed
    ``incident`` and the batch measures the next spec.
    """
    aborting, reasons = _session_scoped_aborts()
    done: list[tuple[Any, str]] = []
    for index, spec in enumerate(specs):
        try:
            outcome = await session.measure(spec)
        except aborting as exc:
            # ``isinstance`` and not ``reasons[type(exc)]``: a subclass is
            # still that failure, and a KeyError would replace the answer.
            reason = next(
                code for cls, code in reasons.items() if isinstance(exc, cls)
            )
            # A cancellation is CONVERTED rather than re-raised: the operator
            # interrupting a long batch most needs the ids of what banked, and
            # the door's give-back still runs shielded on the way out.
            raise MeasureInterrupted(
                reason, str(exc) or type(exc).__name__,
                session, store, spec=spec, spec_index=index,
            ) from exc
        # Read per spec, before the next spec swaps the install: the session
        # re-proves the graph per stimulus, so this fingerprint names the
        # variant graph THIS spec measured through.
        done.append((outcome, str(session.graph_fingerprint)))
    return tuple(done)


def _spec_report(outcome: Any, graph_fingerprint: str) -> dict[str, Any]:
    """One spec's own answer: scalars, and the incidents nothing else records.

    ``graph_fingerprint`` rides per spec, never once per run: each spec may
    install a different variant graph. A stimulus with an ``incident`` banked
    NO record, so its sentence exists nowhere but here; the banked takes carry
    their own levels and ids and are read from the bundle, not from stdout.
    """
    from jasper.active_speaker.crossover_v2.measure_spec import stubbed_capabilities

    return {
        "candidate_id": outcome.spec.candidate_id,
        "kind": outcome.spec.kind,
        "graph_fingerprint": graph_fingerprint,
        "n_takes": len(outcome.record_ids),
        "incidents": [s.incident for s in outcome.stimuli if s.incident],
        "stubs": [stub.code for stub in stubbed_capabilities(outcome.spec)],
    }


def _report(
    outcomes: tuple[tuple[Any, str], ...], *, store: Any, session_id: str,
) -> dict[str, Any]:
    """What this run measured — ONE shape whether it ran one spec or ten.

    A single-spec run reports a one-entry ``specs`` list, so no reader has to
    branch on a count. The graph fingerprint lives on each entry, never at the
    top: one value could not name the several variant graphs a batch installs.
    """
    record_ids = [
        record_id
        for outcome, _fingerprint in outcomes
        for record_id in outcome.record_ids
    ]
    return {
        "status": "measured",
        "session_id": session_id,
        "bundle_dir": str(store.bundle_dir),
        "n_takes": len(record_ids),
        "record_ids": record_ids,
        "specs": [
            _spec_report(outcome, fingerprint)
            for outcome, fingerprint in outcomes
        ],
        "next": f"jasper-round bank {store.bundle_dir}",
    }


def _refused(reason: str, detail: str, *, code: int) -> int:
    """One failing stage, under the word its code owns (``_refusal.py``)."""

    log_event(
        logger,
        "active_speaker.measure",
        level=logging.WARNING,
        action=STATUS_BY_CODE[code],
        reason=reason,
        detail=detail,
    )
    return failed(code, reason, detail)


def _interrupted(exc: MeasureInterrupted) -> int:
    """A run that stopped part-way — a refusal carrying what it banked.

    The ids are the only handle anybody has on takes already on disk, and
    ``stopped_at`` names the spec in flight by zero-based ``index`` as well as
    by its fields, since repeated or unlabelled entries cannot be told apart by
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

    try:
        specs = specs_from_args(args)
    except MeasureFlagError as exc:
        return _refused(exc.reason, exc.detail, code=EXIT_UNREADABLE)
    try:
        payload = asyncio.run(_measure(specs, read_box_declaration()))
    except MeasureInterrupted as exc:
        return _interrupted(exc)
    except MeasureRestoreFailed as exc:
        return _restore_failed(exc)
    except (BoxNotMeasurable, MeasurementDoorRefused) as exc:
        return _refused(exc.reason, exc.detail, code=EXIT_REFUSED)
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

    parser = argparse.ArgumentParser(
        prog="jasper-measure",
        description="Measure this speaker once, bank the takes, print their ids",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "PURPOSE\n"
            "  One on-box measurement through a temporary protected graph,\n"
            "  banked as a standard take jasper-round-views frequency can\n"
            "  read directly. For raw-driver plants or ad-hoc work outside a\n"
            "  wizard round -- scripts/run-crossover-round.py is the\n"
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
            "  1  EXIT_REFUSED -- the door refused the measurement itself\n"
            "     (box not measurable, an interrupt, a restore failure)\n"
            "  2  EXIT_UNREADABLE -- the request could not even be built: a\n"
            "     second --position, a variant axis with no --candidate-id,\n"
            "     a malformed --specs file"
        ),
    )
    parser.add_argument("--kind", choices=MEASURE_KINDS, required=True)
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
    parser.add_argument(
        "--level-dbfs",
        type=float,
        action="append",
        default=[],
        metavar="DBFS",
        help="one stimulus level per ladder rung, repeatable",
    )
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
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
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
