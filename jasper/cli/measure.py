# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-measure`` — one measurement of this speaker, banked and named.

The general operator door onto
:class:`~jasper.active_speaker.crossover_v2.session.TuningSession`. It opens the
speaker once, plays what one :class:`~jasper.active_speaker.crossover_v2.
measure_spec.MeasureSpec` asks for, banks the takes and prints their ids. It
does not grade, does not adopt, and does not restore a profile — reading the
bank is the LLM's job over SSH (ADR-0188 §4, ruling S12).

Usage::

    jasper-measure --kind baseline
    jasper-measure --kind candidate --position -30
    jasper-measure --kind candidate --polarity inverted --inverted-role tweeter \\
        --candidate-id null_a1
    jasper-measure --kind candidate --specs nulls.json   # N configs, one move

**One placement per run — and as many configs against it as you like.** The
engine walks several bearings because the wizard prompts a mover between them;
this door prompts nobody, so a second ``--position`` would play both stimuli
from wherever the microphone already is and bank two poses nothing moved to. A
walk is therefore N runs, one per placement.

Within one placement the opposite is true, and ``--specs`` is how: the physical
cost is the microphone move, so the door holds ONE session and measures every
spec in the file through it. **A preset is a saved**
:class:`~jasper.active_speaker.crossover_v2.measure_spec.MeasureSpec` **and
nothing more**, so the file is a JSON list of exactly that mapping and invents
no vocabulary. The graph's variant emit-cache makes each swap a single
``SetConfig``, so the batch costs one graph load per distinct variant and
nothing else.

Run it as root (``sudo``): the CamillaDSP socket and the session-volume record
are root-owned.

``--candidate-id`` is REQUIRED whenever the spec sets a variant axis — an
inverted polarity, a delayed branch or a level match. A label must SELECT, not
decorate: a bank full of variant takes that no id separates cannot be compared,
and comparing them is the only reason a variant was measured.

Exit 0 when the session opened and closed; 1 on a refusal; 2 on flags that do
not describe a measurement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.cli._logging import CLI_LOG_FORMAT
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INPUT = 2

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
#: at open, where an operator can still act on it, rather than measuring
#: unmatched branches under a record that claims a level match.
REFUSE_NO_LEVEL_EVIDENCE = "measure_no_level_match_evidence"
#: More than one ``--position`` in one invocation. The engine walks bearings
#: because the wizard prompts a mover between them; this door has no mover
#: seam, so N bearings would play back-to-back from ONE placement and bank N
#: different ``position_deg`` values — a silently wrong measurement (S12).
REFUSE_ONE_POSITION_PER_RUN = "measure_one_position_per_run"

#: ``--specs`` could not be read, or does not hold a non-empty list of mappings.
REFUSE_SPECS_UNREADABLE = "measure_specs_file_unreadable"
#: A specs file whose entries name more than one pose. A batch measures ONE
#: placement; mixing bearings is B3 through a file and mixing elevations is the
#: same defect stood on its end.
REFUSE_SPECS_MIXED_POSE = "measure_specs_mixed_pose"
#: ``--specs`` given beside the flags that describe one take. Two sources of
#: truth for one spec, refused rather than merged behind a precedence rule.
REFUSE_SPECS_WITH_TAKE_FLAGS = "measure_specs_with_take_flags"

#: The running measurement graph could not be re-proven or put back mid-walk.
REFUSE_GRAPH_LOST = "measure_graph_lost"
#: The measurement isolation window was lost mid-walk, so household audio could
#: re-enter the mix. ``play_program`` stops before it does.
REFUSE_ISOLATION_LOST = "measure_isolation_lost"
#: The measurement volume stopped being open, confirmed and fresh mid-walk.
REFUSE_VOLUME_LOST = "measure_volume_lost"
#: The operator interrupted the run. Named like the other three because it ends
#: the batch the same way, and because whoever pressed Ctrl-C is exactly who
#: needs the ids of what already banked.
REFUSE_CANCELLED = "measure_cancelled"

__all__ = [
    "BoxDeclaration",
    "BoxNotMeasurable",
    "MeasureFlagError",
    "MeasureInterrupted",
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

    Carries what the operator needs and an exception alone cannot: the ids of
    the records that DID land. Without them the takes are on disk under names
    only a directory scan could recover, and the whole point of this door is
    that it prints the names.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        session: Any,
        store: Any,
        *,
        spec: Any,
        done: tuple[Any, ...] = (),
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.record_ids = list(session.banked_record_ids)
        self.graph_fingerprint = str(session.graph_fingerprint)
        self.bundle_dir = str(store.bundle_dir)
        self.session_id = str(session.session_id)
        #: WHICH spec was in flight. In a batch the ids alone cannot say where
        #: the run stopped, and the next attempt needs to know what to re-take.
        self.spec = spec
        #: The specs that completed before it, so a partial batch renders the
        #: same per-spec shape a whole one does.
        self.done = done
        super().__init__(f"{reason}: {detail}")


# --------------------------------------------------------------------------- #
# what the box itself declares
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoxDeclaration:
    """Everything one session needs that the SPEAKER answers, not the operator.

    Read on the box at open, from the same owners the wizard reads: nothing
    here is a flag, and that is the point. A measurement graph carrying
    protection sections, caps or a level match somebody typed is a measurement
    graph whose safety argument nobody checked.
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

    The same readers and the same gates ``resolve_conductor_context`` runs, in
    the same order — this is a second FRONT END, never a second opinion about
    whether a box may be measured. What it deliberately does not do is the
    wizard's ``ensure_crossover_preview_ready()``: that regenerates and WRITES a
    preview, and a measurement door that repaired the box's design inputs on its
    way past them would be doing setup under a measurement's name.

    **DISCLOSED: this is the SECOND derivation of the box's declarations**, and
    the repo's rule is converge-or-open-an-issue. Converging means lifting the
    shared half of ``jasper.web.correction_crossover_v2.resolve_conductor_context``
    out of the web host — its other half (driver class, radiating diameter, the
    tweeter measurement band, the design revision) is wizard-only, so the lift is
    its own reviewable change and not this door's. Until then, the two are held
    together by reading the SAME owners in the SAME order, and by
    ``resolve_driver_excitation_ceilings`` being the one cap resolver both ask.
    """
    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.driver_safety import evaluate_driver_safety_profile
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        effective_sweep_duration_limit_s,
        resolve_driver_excitation_ceilings,
    )
    from jasper.active_speaker.measurement import active_driver_targets
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )
    from jasper.audio_measurement.program import RoleBand
    from jasper.output_topology import load_output_topology

    topology = load_output_topology()
    preset = resolve_capture_preset(topology)
    if preset.way_count != 2:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the measurement graph is scoped to 2-way presets; this box "
            f"declares {preset.way_count}",
        )
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current or not isinstance(
        safety_profile, Mapping
    ):
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the driver safety profile is not confirmed and current "
            f"({evaluation.status}); confirm it at http://jts.local/sound/",
        )
    role_targets = {
        str(target.get("role") or "").lower(): str(
            target.get("target_fingerprint") or ""
        )
        for target in active_driver_targets(topology)
        if isinstance(target, Mapping)
    }
    role_targets = {role: fp for role, fp in role_targets.items() if role and fp}
    if set(role_targets) != {"woofer", "tweeter"}:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the woofer and tweeter measurement targets are not both active",
        )
    declared_sensitivities = declared_effective_driver_sensitivities(draft)

    roles_bands: list[Any] = []
    caps: dict[str, float] = {}
    limits: dict[str, float] = {}
    for channel, role in enumerate(("woofer", "tweeter")):
        try:
            # ``program_admission=True`` for the reason the wizard's own
            # resolution gives: these caps clamp every composed level, and the
            # routed graph carries each driver's protective filter by
            # construction, so the derived HF ceiling must be the one in force.
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile,
                role_targets[role],
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
            limits[role] = effective_sweep_duration_limit_s(
                safety_profile, role_targets[role],
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise BoxNotMeasurable(
                REFUSE_BOX_NOT_READY,
                f"the {role}'s safe excitation limits could not be resolved",
            ) from exc
        roles_bands.append(RoleBand(role, channel, band))
        caps[role] = float(cap)

    playback_device, _source = resolve_active_playback_device(topology)
    if not playback_device:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the active output device is not declared; finish speaker setup",
        )
    try:
        protection = confirmed_protection_sections(safety_profile, role_targets)
    except ValueError as exc:
        raise BoxNotMeasurable(
            REFUSE_BOX_NOT_READY,
            "the confirmed per-role protection could not be resolved",
        ) from exc
    return BoxDeclaration(
        topology=topology,
        preset=preset,
        safety_profile=safety_profile,
        role_targets=role_targets,
        declared_sensitivities=declared_sensitivities,
        playback_device=str(playback_device),
        protection_sections_by_role=protection,
        roles_bands=tuple(roles_bands),
        caps_dbfs=caps,
        sweep_duration_limits_s=limits,
        fc_hz=float(preset.crossover_regions[0].fc_hz),
        session_volume_db=session_measurement_volume_db(
            safety_profile,
            [role_targets["woofer"], role_targets["tweeter"]],
            declared_sensitivities=declared_sensitivities,
        ),
    )


# --------------------------------------------------------------------------- #
# flags → spec
# --------------------------------------------------------------------------- #


def _variant_axes(spec: Any) -> tuple[str, ...]:
    """The axes that make a take a VARIANT rather than the plain measurement.

    Read off the built spec rather than off flags, so the one rule serves both
    the flag layer and the ``--specs`` file. A second copy keyed on argparse
    names would be free to disagree with this one about what a variant is.
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
    """C3, for one spec however it was written down.

    **The rule lives HERE, in the door's flag layer, and not on the spec.** A
    spec constructed by the wizard already carries a candidate id from the round
    it belongs to; an engine-side refusal would be a new gate on a shipped shape
    rather than a door telling its own operator that the take they are about to
    bank would be unfindable.
    """
    axes = _variant_axes(spec)
    if axes and not spec.candidate_id:
        raise MeasureFlagError(
            REFUSE_CANDIDATE_ID_REQUIRED,
            f"a variant take needs a candidate id to select it from its "
            f"siblings; {where} sets {', '.join(axes)} and names no candidate",
        )


def spec_from_args(args: argparse.Namespace) -> Any:
    """One :class:`MeasureSpec` from the flags, with the C3 rule behind it."""
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    if len(args.position) > 1:
        # The ENGINE walks bearings because the wizard prompts a mover between
        # them. This door has no mover seam: N bearings here would play N
        # stimuli back-to-back from ONE microphone placement and bank N
        # different ``position_deg`` values — takes that name a pose nothing
        # moved to, which is the silent wrong measurement ruling S12 refuses.
        # A walk is N runs of this command, one per placement.
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


#: The flags that describe ONE take. ``--specs`` describes several, so the two
#: together would be two sources of truth for one spec — refused rather than
#: merged, which is the shape a silent precedence rule hides.
_PER_TAKE_FLAGS = ("polarity", "delayed_role", "level_matched", "candidate_id",
                   "position")


def specs_from_args(args: argparse.Namespace) -> tuple[Any, ...]:
    """Every spec this invocation measures, against ONE microphone placement.

    Without ``--specs`` that is the single spec the flags describe. With it, the
    file is the batch: **a preset is a saved** :class:`MeasureSpec` **and
    nothing more** (``measure_spec``'s own words), so the file needs no
    vocabulary of its own — each entry is that mapping, and the flags supply the
    defaults an entry does not name.

    The batch measures one PLACEMENT, which is the whole reason it exists: the
    physical cost is the mic move, so N configs against one move is the win. A
    file whose entries disagree about the pose is therefore refused here rather
    than measured — mixing bearings is B3 arriving through a file, and mixing
    elevations is the same defect stood on its end.
    """
    if not args.specs:
        return (spec_from_args(args),)
    # Compared against the parser's OWN defaults, not against truthiness:
    # ``--polarity`` defaults to ``normal``, which is a perfectly truthy string,
    # so a truthiness test would refuse every batch ever written.
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
    poses = {(spec.positions, spec.position_axis, spec.vertical_deg) for spec in specs}
    if len(poses) > 1:
        raise MeasureFlagError(
            REFUSE_SPECS_MIXED_POSE,
            "a batch measures ONE microphone placement, and nothing here moves "
            f"the microphone between specs; this file names {len(poses)} poses "
            "(bearing, axis, elevation). Split it into one file per placement",
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
    }
    specs = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise MeasureFlagError(
                REFUSE_SPECS_UNREADABLE,
                f"spec {index} is not a mapping",
            )
        fields = {**defaults, **entry}
        for key in ("positions", "pose_prompts", "level_ladder_dbfs"):
            if key in fields:
                fields[key] = tuple(fields[key])
        try:
            spec = MeasureSpec(**fields)
        except (TypeError, ValueError) as exc:
            raise MeasureFlagError(
                REFUSE_SPEC_INVALID, f"spec {index}: {exc}",
            ) from exc
        _require_candidate_id(spec, where=f"spec {index}")
        specs.append(spec)
    return tuple(specs)


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #


def _level_match_trims(box: BoxDeclaration) -> dict[str, float]:
    """This box's own per-driver level offsets, from its banked evidence.

    Asked of :func:`~jasper.active_speaker.baseline_profile.measured_level_trims`
    — the one owner of *banked base trim before guided captures* — so the graph
    a measurement plays through is levelled by the same evidence the speaker
    itself would be. Empty means the box has nothing to level by, and the caller
    refuses rather than measuring unmatched branches under a matched label.
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
    played: _PlayedProgram,
) -> Any:
    """The host's ``compose``: one routed per-driver program per stimulus.

    **One program shape for all three kinds, and the engine's own contract says
    so**: *"a baseline, a candidate check and a re-measure differ by this word
    and by nothing else in the code that runs them"*
    (``contracts.MEASURE_KINDS``). ``kind`` is what the banked record SAYS the
    take is; it is not a second stimulus selector. The wizard's four held
    programs and its phase map are the wizard's own vocabulary, and a door that
    borrowed them would play a summed sweep for ``--kind verify`` while
    installing a routed measurement graph nothing summed measures through.

    **The level is the ladder's, and never the claim's.** A rung is a stimulus
    level in dBFS folded into the per-role gain plan; with no ladder the plan is
    the composer's own reference base. Both are then clamped per role by
    :func:`~jasper.active_speaker.crossover_v2.programs.back_off_gain` against
    that driver's declared cap, and admission re-judges the rendered bytes
    against the same caps at play time.
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

        played.program = program
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


def _crunch(
    program: Any, wav_path: Path, integrity: Any, fc_hz: float,
) -> list[dict[str, Any]]:
    """The take's complex responses, through the analysis that already owns them.

    Three shipped owners in a row and no arithmetic of its own:
    :func:`~jasper.active_speaker.crossover_v2.harmonic_evidence.read_banked_capture_mono`
    for the bytes (the one place that reads a banked capture's width off its own
    ``fmt`` chunk rather than assuming it — a 16-bit read of a 32-bit wired take
    is a 96 dB level error),
    :func:`~jasper.audio_measurement.program_analysis.analyze_program_capture`
    for the analysis, and
    :func:`~jasper.active_speaker.crossover_v2.spatial.analysis_curve_records`
    for the banked shape. Uncalibrated, and the record says so: the calibration
    reference rides beside this as ``capture_setup``, and applying a curve is
    the analysis stage's job, not the capture's.

    **Fail-soft, and not decoratively** — the wizard's own ``_banked_curves``
    guards the same call for the same reason: the sweep already played and the
    take is already on disk, so a serialization fault must cost the CURVES, not
    the capture. An empty list therefore reads as "no curve was banked", which
    the journal line is the discriminator for.
    """
    from jasper.active_speaker.crossover_v2 import spatial
    from jasper.active_speaker.crossover_v2.harmonic_evidence import (
        read_banked_capture_mono,
    )
    from jasper.audio_measurement.program_analysis import (
        MeasurementPriors,
        analyze_program_capture,
    )

    if program is None:
        return []
    try:
        analysis = analyze_program_capture(
            program,
            read_banked_capture_mono(wav_path),
            program.sample_rate_hz,
            # The corner is a DECLARED property of this speaker, and a MEASURE
            # analysis refuses without it — it is what the per-driver read
            # splits its bands around. Taken from the box's own declaration,
            # never from a flag.
            priors=MeasurementPriors(crossover_fc_hz=fc_hz),
            capture_report=integrity,
        )
        return spatial.analysis_curve_records(analysis, program)
    except (AttributeError, IndexError, OSError, TypeError, ValueError):
        log_event(
            logger,
            "active_speaker.measure",
            level=logging.WARNING,
            action="curves_failed",
            wav_path=str(wav_path),
            exc_info=True,
        )
        return []


@dataclass
class _PlayedProgram:
    """The program the last composed stimulus played, for the crunch to read.

    One slot, set at compose and read at bank, which are the two ends of one
    stimulus. The crunch needs the program the capture answers — its schedule is
    what locates the segments and names each role's band — and neither the
    engine's record nor the capture answer carries it.
    """

    program: Any = None


@dataclass(frozen=True)
class _CaptureAnnotatedStore:
    """The banked store, plus what the microphone said about the take.

    :class:`~jasper.active_speaker.crossover_v2.session.TuningSession` builds a
    record from what the ENGINE knows, and the engine deliberately knows nothing
    about a microphone. The capture half meanwhile mints a whole
    :class:`~jasper.active_speaker.crossover_v2.capture_source.CaptureAnswer`
    and hands the transaction only the path. Without this seam the two never
    meet and a CLI take banks structurally poorer than a wizard one — no xrun
    counters, no mic identity, no calibration reference — so a reader could not
    tell a clean take from a spliced one.

    Draining is take-and-CLEAR by the capture half's own contract, and it
    happens here because banking is the moment the two facts belong to the same
    take. A stimulus that played but did not bank leaves its answer for the next
    ``around`` to clear, which is that contract working, not a leak.

    **It also CRUNCHES.** Capture and analysis are one act: there is no point
    taking a measurement nobody turns into numbers, and a take banked as a WAV
    reference alone makes every later reader re-derive what the box already
    knew. So the complex responses land ON the record, in the same
    ``curves`` shape and under the same key the wizard's own takes use — ruling
    S3's *"just save the information"*, which is what makes a bank re-analysable
    by an analysis that did not exist when it was captured.

    Deeper readings stay explicit tools: classification, distortion and packets
    are things somebody ASKS for, and folding them in here would make every
    take pay for readings most takes never need.
    """

    inner: Any
    capture: Any
    played: _PlayedProgram
    fc_hz: float

    async def bank(self, record: Mapping[str, Any]) -> str:
        answer = self.capture.take_answer()
        if answer is None:
            return await self.inner.bank(record)
        # Prefixed, because a record is a different namespace from an answer:
        # ``capture_integrity`` keeps the spelling every existing reader
        # already knows, and the other two say whose ears and which curve.
        annotated = {
            **record,
            **({"capture_integrity": answer.capture_integrity}
               if answer.capture_integrity else {}),
            **({"capture_device": answer.device} if answer.device else {}),
            **({"capture_setup": answer.setup} if answer.setup else {}),
            "curves": await asyncio.to_thread(
                _crunch,
                self.played.program,
                Path(self.inner.evidence.bundle_dir) / str(record["wav_path"]),
                answer.capture_integrity,
                self.fc_hz,
            ),
        }
        return await self.inner.bank(annotated)

    async def read(self, record_id: str) -> Mapping[str, Any] | None:
        return await self.inner.read(record_id)

    async def persist(self, state: Mapping[str, Any]) -> str:
        return await self.inner.persist(state)

    async def read_state(self, state_id: str) -> Mapping[str, Any] | None:
        return await self.inner.read_state(state_id)


async def _measure(specs: tuple[Any, ...], box: BoxDeclaration) -> dict[str, Any]:
    """Open the door once, measure every spec through it, close, and report.

    **One session hold for the whole batch**, which is the point: the physical
    cost is the microphone move, so N configs against one placement is the win,
    and the graph's variant emit-cache makes each swap a single ``SetConfig``.

    The engine's own give-back runs inside the door's: ``TuningSession.close``
    puts the graph back and drops the claim, and the door's ``finally`` then
    finds three idempotent no-ops and lands the plan's durable snapshot last.

    **The bundle is opened INSIDE the door**, and that ordering is a safety
    property rather than tidiness: ``open_bundle``'s first act is to mark every
    prior ``open`` bundle ``abandoned``, which strips the retention protection
    off a wizard session's evidence. Opening it before the interlock would do
    that to a LIVE session and then get refused — destructive on the path that
    was supposed to change nothing.
    """
    from jasper.active_speaker.bundles import open_bundle
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )
    from jasper.active_speaker.crossover_v2.composition import bind_engine_seams
    from jasper.active_speaker.crossover_v2.door import measurement_door
    from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
    from jasper.active_speaker.crossover_v2.session import TuningSession
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile
    from jasper.active_speaker.staging import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.wired_capture import resolve_wired_mic
    from jasper.camilla import primary_controller
    from jasper.web.correction_crossover_v2_wired import WiredStimulusCapture

    # Resolved ONCE for the batch, and asked for by ANY spec in it: the trims
    # are a property of the speaker, not of a take, and the session applies the
    # one answer to whichever specs asked. Refused before the door opens, where
    # an operator can still act on it.
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

    played = _PlayedProgram()
    session_id = f"measure-{secrets.token_hex(4)}"
    config_dir = str(DEFAULT_CAMILLA_CONFIG_DIR)
    cam_factory = primary_controller

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
    ) as door:
        info = open_bundle(box.topology, calibration_id="")
        if not isinstance(info, Mapping) or not info.get("session_id"):
            raise BoxNotMeasurable(
                REFUSE_BOX_NOT_READY,
                "could not open a commissioning evidence bundle for this session",
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
                    relay_session_id=session_id,
                    # The door banks and exits (S12), so it holds no durable
                    # session state — and no engine verb reads these two
                    # (ADR-0198).
                    load_state=lambda: None,
                    save_state=lambda state: None,
                ),
                capture=capture,
                played=played,
                fc_hz=box.fc_hz,
            ),
            volume_claim=door.claim,
            session_volume_plan=door.plan,
            compose_stimulus=_bind_compose(
                box=box,
                store=store,
                session_id=session_id,
                cam_factory=cam_factory,
                config_dir=config_dir,
                played=played,
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
            return _report(
                session, outcomes, store=store, session_id=session_id,
            )


def _session_scoped_aborts() -> tuple[tuple[type[BaseException], ...], dict[type, str]]:
    """The failures that end the BATCH, by TYPE, and the reason each reports.

    **The scope split is drawn by exception type at this one site, and nowhere
    else** — no string matching and no runtime judgement about how bad a failure
    was. The discriminator is structural: everything here is a property of the
    SESSION (the graph nobody can re-prove, the volume that stopped being open,
    the isolation that was lost, the operator's own cancel), so the next spec
    would measure through a speaker this process no longer holds. Every other
    failure is a property of ONE STIMULUS — an admission refusal, a dead aplay,
    a capture that could not be placed — and those never raise at all: the play
    transaction turns each into a typed ``incident`` on its own stimulus, which
    is what lets the batch carry on and disclose them per spec.
    """
    from jasper.active_speaker.crossover_v2.session_graph import SessionGraphError
    from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
    from jasper.correction.coordinator import MeasurementWindowError

    reasons: dict[type, str] = {
        SessionGraphError: REFUSE_GRAPH_LOST,
        SessionVolumePlanError: REFUSE_VOLUME_LOST,
        MeasurementWindowError: REFUSE_ISOLATION_LOST,
        asyncio.CancelledError: REFUSE_CANCELLED,
    }
    return tuple(reasons), reasons


async def _measured(
    session: Any, specs: tuple[Any, ...], *, store: Any,
) -> tuple[Any, ...]:
    """Every spec against one open session, and what a mid-batch failure costs.

    A session-scoped failure ABORTS: the speaker is no longer held the way the
    remaining specs would be measured through, so continuing would bank takes
    whose provenance is a guess. It leaves through :class:`MeasureInterrupted`
    naming the spec that was in flight, carrying every id banked so far — B4's
    partial payload, generalized from one spec to N.

    A spec-scoped failure does not reach here at all: the play transaction
    reports it as a typed ``incident`` on the stimulus, so that spec's entry in
    the result says what happened and the batch measures the next one. The
    physical cost is the mic move, and one refused admission is no reason to
    give the placement back.
    """
    aborting, reasons = _session_scoped_aborts()
    outcomes: list[Any] = []
    for spec in specs:
        try:
            outcomes.append(await session.measure(spec))
        except aborting as exc:
            # ``isinstance`` and not ``reasons[type(exc)]``: a subclass of any
            # of these is still that failure, and a KeyError here would replace
            # the operator's answer with a lookup error.
            reason = next(
                code for cls, code in reasons.items() if isinstance(exc, cls)
            )
            # A cancellation is CONVERTED rather than re-raised, deliberately:
            # the operator interrupting a long batch is exactly who most needs
            # the ids of what already banked, and the batch has stopped either
            # way. The door's own give-back still runs shielded on the way out,
            # so the speaker is handed back before this is rendered.
            raise MeasureInterrupted(
                reason, str(exc) or type(exc).__name__,
                session, store, spec=spec, done=tuple(outcomes),
            ) from exc
    return tuple(outcomes)


def _spec_report(outcome: Any) -> dict[str, Any]:
    """One spec's own answer, in the caller's words.

    Every stimulus is reported, not only the banked ones: a rung that played and
    could not be banked is the fact a reader most needs, and a bare list of ids
    would hide it behind a shorter list. ``incident`` is where a SPEC-scoped
    failure lands — a refused admission, a dead emission, a lost capture — each
    already carrying the play transaction's own typed reason, which is what lets
    the batch carry on past one and still say what happened.

    ``stubs`` names every capability this spec asked for that the engine has not
    built, so a reader knows which banked evidence is still owed an analysis
    rather than discovering it later by its absence.
    """
    from jasper.active_speaker.crossover_v2.measure_spec import stubbed_capabilities

    return {
        "candidate_id": outcome.spec.candidate_id,
        "kind": outcome.spec.kind,
        "record_ids": list(outcome.record_ids),
        "stimuli": [
            {
                "position_deg": stimulus.position_deg,
                "stimulus_dbfs": stimulus.stimulus_dbfs,
                "level_db": stimulus.level_db,
                "record_id": stimulus.record_id,
                "incident": stimulus.incident,
            }
            for stimulus in outcome.stimuli
        ],
        "stubs": [
            {
                "code": stub.code,
                "instrument": stub.instrument,
                "captured": stub.captured,
            }
            for stub in stubbed_capabilities(outcome.spec)
        ],
    }


def _report(
    session: Any, outcomes: tuple[Any, ...], *, store: Any, session_id: str,
) -> dict[str, Any]:
    """What this run measured — ONE shape whether it ran one spec or ten.

    A single-spec run reports a one-entry ``specs`` list rather than a flatter
    payload of its own: two shapes would make every reader branch on a count,
    and the batch is the general case the door now has.
    """
    return {
        "status": "measured",
        "session_id": session_id,
        "bundle_dir": str(store.bundle_dir),
        "graph_fingerprint": session.graph_fingerprint,
        "record_ids": [
            record_id
            for outcome in outcomes
            for record_id in outcome.record_ids
        ],
        "specs": [_spec_report(outcome) for outcome in outcomes],
    }


# --------------------------------------------------------------------------- #
# the door itself
# --------------------------------------------------------------------------- #


def _refused(reason: str, detail: str, *, json_output: bool, code: int) -> int:
    log_event(
        logger,
        "active_speaker.measure",
        level=logging.WARNING,
        action="refused",
        reason=reason,
        detail=detail,
    )
    if json_output:
        print(
            json.dumps(
                {"status": "refused", "reason": reason, "detail": detail},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"refused ({reason}): {detail}", file=sys.stderr)
    return code


def _interrupted(exc: MeasureInterrupted) -> int:
    """A run that stopped part-way, printed as a PARTIAL result.

    Always JSON, and always on stdout beside the ordinary result, because the
    ids in it are the only handle anybody has on takes that are already on
    disk. A refusal line on stderr would be honest about the failure and lose
    the evidence.

    ``specs`` carries the same per-spec shape a whole run reports, and
    ``stopped_at`` names the one that was in flight — in a batch the ids alone
    cannot say where the run stopped, and the next attempt needs to know what
    is still owed.
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
    print(json.dumps(
        {
            "status": "partial",
            "reason": exc.reason,
            "detail": exc.detail,
            "session_id": exc.session_id,
            "bundle_dir": exc.bundle_dir,
            "graph_fingerprint": exc.graph_fingerprint,
            "record_ids": exc.record_ids,
            "specs": [_spec_report(outcome) for outcome in exc.done],
            "stopped_at": {
                "candidate_id": exc.spec.candidate_id,
                "kind": exc.spec.kind,
            },
        },
        indent=2,
        sort_keys=True,
        default=str,
    ))
    return EXIT_REFUSED


def _cmd_measure(args: argparse.Namespace) -> int:
    from jasper.active_speaker.crossover_v2.door import MeasurementDoorRefused

    try:
        specs = specs_from_args(args)
    except MeasureFlagError as exc:
        return _refused(
            exc.reason, exc.detail, json_output=args.json, code=EXIT_INPUT
        )
    try:
        payload = asyncio.run(_measure(specs, read_box_declaration()))
    except MeasureInterrupted as exc:
        return _interrupted(exc)
    except (BoxNotMeasurable, MeasurementDoorRefused) as exc:
        return _refused(
            exc.reason, exc.detail, json_output=args.json, code=EXIT_REFUSED
        )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return EXIT_OK


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
    )
    parser.add_argument("--kind", choices=MEASURE_KINDS, required=True)
    parser.add_argument(
        # ``append`` rather than a plain value so a SECOND one is visible here
        # and can be refused by name (``spec_from_args``). Taking the last one
        # silently would measure one bearing and let the operator believe two
        # were walked.
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
            "file needs no vocabulary of its own. The flags above supply the "
            "defaults an entry does not name; the per-take flags "
            "(--position/--polarity/--delayed-role/--level-matched/"
            "--candidate-id) are refused beside it, and every entry needs its "
            "own candidate id once it sets a variant axis"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="render a refusal as JSON too (the measurement always is)",
    )
    parser.set_defaults(func=_cmd_measure)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # INFO floor, like the audition door: this door's own `event=` lines are
    # the record of which graph the speaker was measured through.
    logging.basicConfig(level=logging.INFO, format=CLI_LOG_FORMAT)
    from jasper.env_load import load_env_files
    from jasper.volume_coordinator import install_env_canonical_target_provider

    load_env_files()
    # Installs this process's VolumeOwner AND the canonical target the duck
    # release reads. Without the owner there is no SESSION_MEASUREMENT rank to
    # claim the fader through, and the door refuses rather than minting a
    # second authority over it.
    install_env_canonical_target_provider()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
