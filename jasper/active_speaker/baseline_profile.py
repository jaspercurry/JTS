# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and apply accepted active-speaker baseline profiles."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Sequence

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.audio_measurement.peq import bell_half_width_oct
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.dsp_apply import (
    DspApplyError,
    DspApplyState,
    apply_dsp_config,
    dsp_writer_lock,
    same_config_file,
)
from jasper.json_fields import utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    canonical_fingerprint as _fingerprint,
    topology_config_fingerprint,
)
from jasper.output_topology_store import load_output_topology

from ._common import coerce_finite_float, issue as _issue
from .camilla_yaml import (
    _branch_context,
    linearization_headroom_db,
)
from .candidate_bank import BankedCandidate, CandidateBankRefusal, bank_candidate, load_applied_candidate
from .measurement_emit import MeasurementGraphProfile
from .crossover_contract import (
    measured_level_match_applied,
)
from .crossover_preview import build_crossover_preview, crossover_preview_fingerprint
from .driver_base_trim import (
    BANK_CLEAR_FAILED,
    BANK_CORRECTION_ENTRY_UNREADABLE,
    BANK_CORRECTIONS_UNREADABLE,
    BANK_PARTLY_MEASURED,
    BANK_READINESS_UNREADABLE,
    BANK_UNMEASURED,
    BANK_WRITE_FAILED,
    BANK_WRITE_REFUSED,
    REFUSE_NO_FRAME,
    DriverBaseTrimError,
    banked_base_trims,
    clear_base_trim,
    load_base_trim,
    write_base_trim,
)
from .measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    candidate_on_declaration, driver_corrections, effective_preset,
)
from .measurement import empty_driver_check_summary
from .measurement_programs import PROGRAM_DOCUMENT_ORDER, PURPOSE_SPEAKER
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .profile import snapshot_declares_single_branch
from .rear_calibration import rear_operating_facts
from . import passive_profile as _passive
from .state_paths import (
    baseline_candidate_config_path, baseline_config_path, baseline_profile_state_path, config_text_sha256,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"
REAR_CALIBRATION_WALL_GAP_MISMATCH = "rear_calibration_wall_gap_differs"
REAR_CALIBRATION_FRONT_DELAY_SHIFTS_TIMING = "rear_calibration_front_delay_shifts_timing"
REAR_CALIBRATION_ROOM_BAND_OVERLAP = "rear_calibration_room_band_overlap"
# The wizard declares the wall gap in millimetres while a document carries an
# inch-derived value (0.2032 m), so only a millimetre-scale difference is real.
REAR_CALIBRATION_WALL_GAP_TOLERANCE_M = 0.001

# Canonical per-parameter provenance vocabulary (SC-3).
PROVENANCE_MANUAL = "manual"
PROVENANCE_MEASURED = "measured"
PROVENANCE_AUTHORED_BY_MODEL = "authored_by_model"
PROVENANCE_SET_BY_USER = "set_by_user"

#: ``corrections_source`` values an operator set by hand. Every other source
#: that is not ``measured`` fell back to weaker evidence (the datasheet, an
#: estimate, a preserved manual crossover).
_PINNED_GAIN_SOURCES = frozenset({"operator_pinned", "explicit"})


def applied_bass_extension(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = load_applied_baseline_profile_state() if profile is None else profile
    snapshot = (source or {}).get("recomposition_snapshot") or {}
    raw = snapshot.get("bass_extension") or {}
    return validate_dynamic_bass_descriptor(raw) if raw else {}


@asynccontextmanager
async def load_composed_graph(
    text: str | Callable[[], Awaitable[str]], *, source: str,
    profile: Mapping[str, Any],
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]],
    persist: Callable[[], Any] | None = None,
    config_dir: str | Path | None = None,
    audition: bool = False,
    record: Literal["apply", "sound"] | None = "apply",
    sound_filter_count: int | None = None,
) -> AsyncIterator[tuple[DspApplyState, dict[str, Any]]]:
    from jasper.sound.camilla_yaml import extract_room_peqs_from_config_text, sound_audition_config_path  # lazy: sound graph metadata

    directory = Path(config_dir) if config_dir is not None else baseline_config_path().parent
    target = sound_audition_config_path(directory) if audition else directory / baseline_config_path().name
    applied = dict(profile)
    async with dsp_writer_lock(directory, source=source):
        async def write_graph() -> dict[str, Any]:
            nonlocal target, applied
            rendered = await text() if callable(text) else text
            applied = dict(profile)
            if record == "sound":
                saved = load_baseline_profile_state() or {}
                anchor = _applied_profile_anchor(saved) or {}
                if not anchor.get("recomposition_snapshot") or not anchor.get("source"):
                    raise ValueError("Applied speaker record is incomplete")
                protection = _protection_projection((profile.get("recomposition_snapshot") or {}).get("driver_protection"))
                applied = {**anchor, "source": {**anchor.get("source", {}),
                           "driver_protection_fingerprint": _fingerprint(protection)},
                           "recomposition_snapshot": {**anchor.get("recomposition_snapshot", {}), "driver_protection": protection}}
                applied["source"]["fingerprint"] = _fingerprint({key: value for key, value in applied["source"].items() if key != "fingerprint"})
            if not audition:
                target = baseline_candidate_config_path(rendered, directory / baseline_config_path().name)
            sha256 = config_text_sha256(rendered)
            applied["config"] = {**profile.get("config", {}), "path": str(target), "basename": target.name,
                                 "sha256": sha256, "exists": True}
            atomic_write_text(target, rendered, mode=CONFIG_FILE_MODE)
            return {"candidate_path": target, "expected_candidate_sha256": sha256,
                    "room_peq_count": len(extract_room_peqs_from_config_text(rendered))}

        state = await apply_dsp_config(
            source=source, candidate_path=target,
            prepare=write_graph,
            load_config=load_config, get_current_config_path=get_current_config_path,
            persist=persist, sound_filter_count=sound_filter_count,
        )
        if record == "apply":
            applied = persist_applied_baseline_profile(applied, apply_state=state.to_dict())
        elif record == "sound":
            saved = load_baseline_profile_state() or {}
            if saved.get("status") == "applied":
                saved = applied
            else:
                saved["applied_recomposition_profile"] = applied
            atomic_write_text(baseline_profile_state_path(), json.dumps(saved, indent=2, sort_keys=True) + "\n",
                              mode=CONFIG_FILE_MODE, durable=True)
        if record:
            promote_applied_baseline_candidate(applied)
        yield state, applied


def baseline_candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Identify the exact immutable Layer-A candidate, not its cache source."""

    source = candidate.get("source")
    snapshot = candidate.get("recomposition_snapshot")
    hashed_snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else None
    if hashed_snapshot is not None and isinstance(
        hashed_snapshot.get("level_match"), Mapping
    ):
        # Capture recency changes on recompose; graph identity must not.
        # Candidates without this field must keep their stored fingerprint.
        hashed_snapshot["level_match"] = {
            key: value
            for key, value in hashed_snapshot["level_match"].items()
            if key != "newest_capture_at"
        }
    return _fingerprint({
        "artifact_schema_version": candidate.get("artifact_schema_version"),
        "kind": candidate.get("kind"),
        "source_fingerprint": (
            source.get("fingerprint") if isinstance(source, Mapping) else None
        ),
        "recomposition_snapshot": hashed_snapshot,
    })


def reviewed_candidate_refusal(
    candidate: Mapping[str, Any], expected_candidate_fingerprint: str,
) -> dict[str, Any] | None:
    if expected_candidate_fingerprint and candidate.get("candidate_fingerprint") == expected_candidate_fingerprint:
        return None
    refused = dict(candidate)
    refused["permissions"] = {**(refused.get("permissions") or {}), "may_apply": False}
    refused["issues"] = [*(refused.get("issues") or []), _issue(
        "blocker", "baseline_candidate_fingerprint_mismatch",
        "the crossover candidate changed after review; refresh and review the current candidate before applying",
    )]
    return {"status": "blocked", "profile": refused, "apply": None, "issues": refused["issues"]}


def _commissioning_refusal(profile: dict[str, Any], exc: Exception) -> None:
    profile.update(status="blocked", permissions={"may_apply": False, "may_compile": False})
    profile["issues"] = getattr(exc, "issues", None) or [_issue(
        "blocker", getattr(exc, "code", None) or getattr(exc, "reason", None) or "compose_refused", str(exc),
    )]


def rear_calibration_issues(candidate: MeasuredCrossoverCandidate) -> list[dict[str, Any]]:
    """Disclose geometry, timing and room-band interactions (ADR-0101, ADR-0322)."""
    from jasper.audio_measurement.measurement_geometry import load_declared_geometry  # lazy: geometry pulls NumPy in

    document = candidate.rear_calibration
    if not document:
        return []
    issues: list[dict[str, Any]] = []
    fitted_m = (document.get("geometry") or {}).get("cabinet_back_wall_m")
    geometry = load_declared_geometry()
    declared_m = None if geometry is None else geometry.cabinet_back_wall_m
    if fitted_m is not None and declared_m is not None and (
        abs(fitted_m - declared_m) > REAR_CALIBRATION_WALL_GAP_TOLERANCE_M
    ):
        issues.append(_issue(
            "warning", REAR_CALIBRATION_WALL_GAP_MISMATCH,
            f"the rear calibration was fitted {fitted_m:g} m from the wall behind the cabinet, "
            f"but the declared rig geometry says {declared_m:g} m",
        ))
    front_delay_ms = (document.get("front") or {}).get("delay_ms")
    if front_delay_ms and (candidate.analysis.get("resolution") or {}).get("alignment") == "measured":
        issues.append(_issue(
            "warning", REAR_CALIBRATION_FRONT_DELAY_SHIFTS_TIMING,
            f"the rear calibration delays the front woofer by {front_delay_ms:g} ms, which moves it "
            "away from the measured woofer/tweeter arrival difference",
        ))
    cancellation_band = rear_operating_facts(document)["band_hz"]
    if cancellation_band:
        for peq in (entry for filters in candidate.room_correction.get("sides", {}).values()
                    for entry in filters if entry["gain"]):
            width = 2 ** bell_half_width_oct(peq["q"])
            room_band = [peq["freq"] / width, peq["freq"] * width]
            if max(room_band[0], cancellation_band[0]) < min(room_band[1], cancellation_band[1]):
                issues.append({
                    **_issue("warning", REAR_CALIBRATION_ROOM_BAND_OVERLAP,
                             "the room correction layer and the rear cancellation branch are "
                             "not reconciled against each other in v1"),
                    "room_band_hz": room_band, "cancellation_band_hz": cancellation_band,
                })
    return issues


def compile_commissioning_profile(
    *, applied_profile: Mapping[str, Any] | None, topology: OutputTopology | None = None,
    design_draft: Mapping[str, Any] | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
    find_candidate: Callable[[str], BankedCandidate] | None = None,
) -> dict[str, Any]:
    """Review the applied candidate, or bootstrap from the declared crossover.

    Read-only: the review compiles and proves the graph an apply would load,
    and writes nothing."""
    from .commissioning_experiment import commissioning_candidate  # lazy: candidate parts consumes baseline readers
    from .design_draft import load_design_draft  # lazy: design draft imports baseline readers
    from .measurement_emit import compile_tuning_graph, load_tuning_declaration, MeasurementGraphRefused  # lazy: graph compilation imports baseline readers
    from .runtime_contract import classify_bass_extension_graph, GRAPH_APPROVED_ACTIVE_RUNTIME  # lazy: graph proof imports baseline state
    from jasper.sound.settings import saved_sound_layers  # lazy: household settings import baseline readers

    profile: dict[str, Any] = {"artifact_schema_version": SCHEMA_VERSION, "kind": BASELINE_PROFILE_KIND,
                              "status": "blocked", "permissions": {"may_apply": False}, "issues": []}
    try:
        topology = topology if topology is not None else load_output_topology()
        draft = design_draft if design_draft is not None else load_design_draft(topology=topology)
        declaration = load_tuning_declaration(topology, design_draft=draft)
        applied = applied_profile
        if applied is not None:
            fingerprint = (applied.get("source") or {}).get("measured_candidate_fingerprint", "")
            banked = (find_candidate(fingerprint) if find_candidate is not None
                      else load_applied_candidate(fingerprint, applied_profile=applied))
        else:
            banked = bank_candidate(commissioning_candidate(topology, draft), find_candidate=find_candidate)
        candidate = banked.candidate
        preference_filters, trim_db = saved_sound_layers()
        text = compile_tuning_graph(declaration, candidate=candidate,
                                    preference_filters=preference_filters, output_trim_db=trim_db)
        target = baseline_candidate_config_path(text)
        profile.update(prepare_applied_baseline_profile(
            banked, declaration=declaration, design_draft=draft,
            config_path=target, config_sha256=config_text_sha256(text), crossover_preview=crossover_preview,
            saved_timing=(applied or {}).get("timing"),
        ))
        profile["issues"] = [*(candidate.analysis.get("issues") or []), *rear_calibration_issues(candidate)]
        profile["candidate_fingerprint"] = baseline_candidate_fingerprint(profile)
        profile["config"]["exists"] = target.exists()
        proof = classify_bass_extension_graph(topology, evidence_source="desired", graph_text=text, applied_baseline_state=profile)
        if not proof.allowed or proof.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
            raise MeasurementGraphRefused("baseline_graph_safety_proof_failed", proof.classification)
        profile.update(status="ready_to_compile", permissions={"may_apply": False, "may_compile": True})
    except (CandidateBankRefusal, ValueError) as exc:
        _commissioning_refusal(profile, exc)
    return profile


def _source_payload(
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    *,
    measured_candidate_fingerprint: str | None = None,
    driver_protection: Mapping[str, Any] | None = None,
    candidate_graph_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fingerprint declaration and evidence inputs, not emitted bytes."""
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        # Banked driver trims must match the declaration they measured.
        "crossover_preview_fingerprint": crossover_preview_fingerprint(
            crossover_preview, design_draft
        ),
        # Frozen at the retired driver-check record's no-record answer; see
        # empty_driver_check_summary.
        "measurements_updated_at": None,
        "measurement_summary_fingerprint": _fingerprint(empty_driver_check_summary(topology)),
    }
    if measured_candidate_fingerprint is not None:
        source["measured_candidate_fingerprint"] = measured_candidate_fingerprint
    if driver_protection is not None:
        source["driver_protection_fingerprint"] = _fingerprint(driver_protection)
    if candidate_graph_context is not None:
        device_context = {
            key: value for key, value in candidate_graph_context.items()
            if key != "measured_candidate_fingerprint"
        }
        source["candidate_graph_context_fingerprint"] = _fingerprint(device_context)
    return {**source, "fingerprint": _fingerprint(source),
            "design_draft_updated_at": design_draft.get("updated_at")}


def measured_level_trims(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any] | None = None,
    *,
    design_draft: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """This box's banked per-driver level offsets for the declaration in hand.

    The one owner of *"what does this box's own evidence say the per-driver
    level offsets are?"*, which the crossover-v2 MEASUREMENT graph levels by.
    ``meta['source']`` names the evidence that answered, which is what a caller
    discloses beside the trims; empty trims mean none did (``meta['base_trim']``
    says why), and no caller may substitute an estimate for them.
    """
    roles = required_driver_roles(preset.way_count)
    declaration_fingerprint = (
        crossover_preview_fingerprint(crossover_preview, design_draft)
        if isinstance(crossover_preview, Mapping) and crossover_preview
        else None
    )
    base_trims, base_trim_meta = banked_base_trims(declaration_fingerprint, roles)
    if not base_trims:
        return {}, {"base_trim": base_trim_meta}
    banked_group_ids = base_trim_meta.get("speaker_group_ids") or []
    return base_trims, {
        "source": "banked_base_trim",
        "base_trim": base_trim_meta,
        "newest_capture_at": base_trim_meta.get("measured_at"),
        # The record's own trim source, not a second word for it: the
        # apply that banked it stamped WHICH evidence levelled the
        # graph, and this ledger repeats that rather than minting a
        # comparison of its own.
        "comparison": base_trim_meta.get("trim_source"),
        "groups_total": len(banked_group_ids),
        "groups_measured": len(banked_group_ids),
        "measured_group_ids": list(banked_group_ids),
        # Empty because the record banks an ALREADY-APPLIED level
        # match: the per-crossover evidence behind it lives with the
        # profile that was applied, which ``base_trim.trim_source``
        # names.
        "deltas": [],
        "incomparable_groups": [],
        "trims": dict(base_trims),
    }


def _load_saved_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if (
        raw.get("artifact_schema_version") != SCHEMA_VERSION
        or raw.get("kind") != BASELINE_PROFILE_KIND
    ):
        return None
    return raw


def _applied_profile_anchor(
    saved: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Current or retained applied profile behind a mutable candidate state."""
    if not isinstance(saved, Mapping):
        return None
    if saved.get("status") == "applied":
        return saved
    prior = saved.get("applied_recomposition_profile")
    if isinstance(prior, Mapping) and prior.get("status") == "applied":
        return prior
    return None


def _frozen_applied_profile(
    saved: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the applied record fields consumed by runtime readers."""
    applied = _applied_profile_anchor(saved)
    if applied is None:
        return None
    snapshot = applied.get("recomposition_snapshot")
    # candidate_fingerprint is derived data, not an authority. Older saved
    # profiles may omit it and a partially written/corrupt profile may carry a
    # value that no longer identifies its immutable snapshot. Always migrate
    # or repair it from the exact source + snapshot content consumers trust.
    candidate_fingerprint = (
        baseline_candidate_fingerprint(applied)
        if isinstance(snapshot, Mapping)
        else None
    )
    return {
        "artifact_schema_version": applied.get("artifact_schema_version"),
        "kind": applied.get("kind"),
        "status": "applied",
        "applied_at": applied.get("applied_at"),
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_artifact_path": applied.get("candidate_artifact_path"),
        "source": dict(applied.get("source") or {}),
        "config": dict(applied.get("config") or {}),
        "corrections": dict(applied.get("corrections") or {}),
        "corrections_source": dict(applied.get("corrections_source") or {}),
        **({"timing": applied["timing"]} if "timing" in applied else {}),
        "gain_provenance": dict(applied.get("gain_provenance") or {}),
        "corrections_provenance": dict(applied.get("corrections_provenance") or {}),
        "level_match": dict(applied.get("level_match") or {}),
        "automatic_candidate": dict(applied.get("automatic_candidate") or {}),
        "linearization": dict(applied.get("linearization") or {}),
        "linearization_outcome": str(applied.get("linearization_outcome") or ""),
        "trim_decision": dict(applied.get("trim_decision") or {}),
        "blend_correction": list(applied.get("blend_correction") or []),
        "room_correction": dict(applied.get("room_correction") or {}),
        "tuning_owner": str(applied.get("tuning_owner") or ""),
        # Quality state belongs to the immutable applied anchor too.  Dropping
        # it here lets an older sensitivity-only profile masquerade as a
        # measured profile on every consumer of the frozen view.
        "provisional": bool(applied.get("provisional")),
        "recomposition_snapshot": dict(snapshot) if isinstance(snapshot, Mapping) else None,
    }


def _profile_branch_context(
    profile: Mapping[str, Any],
) -> dict[str, tuple[Any, float]]:
    """The ``(crossover sections, trim_db)`` context for one profile's charge.

    Rebuilt from the SAME two snapshot fields the graph was emitted from — the
    preset's crossover regions and the per-driver ``corrections`` gains — via
    the emitter's own :func:`~jasper.active_speaker.camilla_yaml._branch_context`,
    so a headroom read back off a profile is evaluated over the chain that
    profile actually carries.

    ``{}`` when either field is missing or unparseable. That is the same
    over-estimating fallback ``linearization_headroom_db`` applies to a role
    absent from a supplied context ("no crossover, no trim"), and it is the
    right direction here: an over-estimated headroom read makes the DECLARED
    apply-boundary offset larger than the graph's, which under-corrects the
    delta probe and leaves the difference visible as ``residual_offset_db``
    rather than hiding it.
    """
    snapshot = profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping):
        return {}
    corrections = snapshot.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    except (ActiveSpeakerConfigError, TypeError, ValueError):
        return {}
    return _branch_context(preset, corrections)


def profile_program_headroom_db(profile: Mapping[str, Any] | None) -> float:
    """The pre-split common attenuation one profile's linearization costs, dB."""
    linearization = profile_linearization(profile)
    if not linearization:
        return 0.0
    return linearization_headroom_db(
        linearization, branch_context=_profile_branch_context(profile),
    )


def profile_blend_correction(
    profile: Mapping[str, Any] | None,
) -> tuple[Any, ...] | None:
    """Read the snapshot first, then the legacy top-level blend correction.

    None means unknown; () means no correction. Never turn unknown into zero:
    that can double-count correction already present in the measured graph.
    Preserve every entry for blend_filters_from_mapping to validate.
    """

    if not isinstance(profile, Mapping):
        return None
    snapshot = profile.get("recomposition_snapshot")
    raw = (
        snapshot.get("blend_correction") if isinstance(snapshot, Mapping) else None
    )
    if raw is None:
        raw = profile.get("blend_correction")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, Mapping)):
        return None
    return tuple(raw)


def profile_linearization(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE reduced linearization, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    linearization = (
        snapshot.get("linearization") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(linearization, Mapping):
        linearization = profile.get("linearization")
    return linearization if isinstance(linearization, Mapping) else {}


def applied_layers(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    profile = profile or {}
    snapshot = profile.get("recomposition_snapshot") or {}
    return {row.purpose: bool(profile_linearization(profile) if row.purpose == PURPOSE_SPEAKER else
                              snapshot.get(row.candidate_fields[0].name,
                                           profile.get(row.candidate_fields[0].name) if row.profile_fallback else None))
            for row in PROGRAM_DOCUMENT_ORDER}


def applied_layer_names(profile: Mapping[str, Any] | None) -> dict[str, bool]:
    """``applied_layers``, keyed by its packet-facing layer name instead of purpose."""
    layers = applied_layers(profile)
    return {row.applied_name: layers[row.purpose] for row in PROGRAM_DOCUMENT_ORDER}


def profile_driver_corrections(profile: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """One profile's AUTHORITATIVE ``{role: {gain_db, delay_ms, inverted}}``, or ``{}``."""
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    corrections = (
        snapshot.get("corrections") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(corrections, Mapping):
        corrections = profile.get("corrections")
    if not isinstance(corrections, Mapping):
        return {}
    if any(
        isinstance(values, Mapping)
        and isinstance(values.get("delay_ms"), bool)
        for values in corrections.values()
    ):
        return {}
    return corrections


def applied_program_level_delta_db(
    previous_profile: Mapping[str, Any] | None,
    applied_profile: Mapping[str, Any] | None,
) -> float:
    """dB the emitted graph's BROADBAND level MOVED across one apply (#1811).

    Negative when the apply made the speaker quieter — the ordinary case, and
    the whole reason this exists: the applied correction's boost is absorbed as
    a pre-split common attenuation, so the same commanded volume produces a
    materially quieter speaker the instant the config swaps.

    **This is an input to ANALYSIS, never to the speaker's level.** The
    absorption keeps the boosted branch at or below unity (see
    ``camilla_yaml.program_headroom_db``). Compensating at main volume would
    undo that attenuation. The consumer is
    :func:`~jasper.active_speaker.delta_probe.classify_delta_probe`, whose
    realized-vs-commanded comparison is not mean-centered and would otherwise
    read this move as a defect.

    Read from the profiles, never assumed: the magnitude is whatever the fit
    charged (22.5 dB on the session that surfaced this; a few dB once the
    loudness-doctrine work shrinks the charge), and a correction that hands
    headroom BACK yields a positive number.

    **This is the pre-split term, and it is the ONLY level term the commanded
    axis cannot carry** (#2611). The absorption is applied BEFORE the branch
    split, so ``predicted_branch_sum`` — a model of the two branches — has no
    place to put it, which is why it travels as a scalar beside the commanded
    curve rather than inside it. Per-branch trims are excluded here for the
    complementary reason: since #2611 they ARE on the commanded axis, which is
    the applied graph's predicted sum minus the PREVIOUS graph's
    (:mod:`jasper.active_speaker.crossover_v2.commanded`), so counting them here
    too would remove them twice. Before that fix they were in neither account —
    both sides of the commanded delta carried the applied candidate's own
    trims, so a per-role gain step cancelled out of the model entirely and
    landed in ``residual_offset_db`` as a surprise (+3.2198 dB on the
    2026-08-16 jts3 round). The two accounts are disjoint and, between them,
    complete for everything the apply commands.

    **One known incompleteness, deliberate, caught downstream.**
    Room-PEQ and preference-EQ headroom are excluded. The candidate's own room
    set is emitted, including its boost headroom, and the preference layer is not
    emitted at all; neither term is read here, so a round that changes either
    can see a real level move this reader cannot see. That remainder is
    exactly what the probe's ``residual_offset_db`` measures and what
    ``delta_probe.VERDICT_LEVEL_MISMATCH`` names — which is why this function
    is allowed to be an honest partial account rather than having to be a
    complete one.
    """
    return profile_program_headroom_db(previous_profile) - (
        profile_program_headroom_db(applied_profile)
    )


def load_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read one validated baseline artifact without deriving fresh evidence."""
    return _load_saved_state(baseline_profile_state_path(path))


def load_applied_baseline_profile_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the applied Layer-A SSOT, even while a new candidate is staged."""
    return _frozen_applied_profile(load_baseline_profile_state(path))


#: :func:`applied_profile_displacement`'s answers. ``""`` is the fourth and it
#: means the record is authoritative — an empty string rather than a word so
#: the check reads as a guard at every call site.
APPLIED_PROFILE_DISPLACED = "applied_profile_displaced"
APPLIED_PROFILE_PATH_UNKNOWN = "applied_profile_path_unknown"
APPLIED_PROFILE_RUNNING_UNKNOWN = "running_config_path_unknown"
#: The record names a config file that is no longer on disk. A fifth answer to
#: the same question, and NOT one of the four above: the record's own
#: ``config.exists`` is frozen at apply time, so only a fresh stat can say it.
APPLIED_PROFILE_CONFIG_MISSING = "applied_profile_config_missing"


def applied_profile_displacement(
    applied: Mapping[str, Any] | None,
    *,
    statefile_path: str | Path | None = None,
) -> str:
    """Is this applied-profile record still what the speaker is PLAYING? (#2537)

    ``""`` when the record's own ``config.path`` is the path CamillaDSP's
    durable statefile selects — the record is authoritative. Otherwise one of
    the three codes above, naming why it cannot be trusted as "the current
    sound".

    **The defect this exists for.** On 2026-08-15 (jts3 cycle 4) the applied
    record still named run 2's candidate while the speaker had been reconciled
    out of band to ``sound_current.yml``. Nothing compared the two, so a restore
    faithfully put back "the previous sound" per the record — a graph the
    speaker had not played for six and a half hours. The record had silently
    stopped being the truth, and the statefile is the one place that says so.

    **This adds a READER, never a second writer.** ``reconcile-current-dsp``
    and every other out-of-band path stay entirely ignorant of the
    active-speaker profile system, which is the separation of concerns that
    makes them safe to run; the record's only writer remains
    :func:`persist_applied_baseline_profile` on the apply path. What changes is
    that consumers stop assuming the record won a race it never entered.

    Fail-soft and *reporting*, never gating on its own: a missing statefile or a
    record with no path yields a code, and each caller decides what an unknown
    provenance means for its own question. An unreadable statefile is
    :data:`APPLIED_PROFILE_RUNNING_UNKNOWN` — "we could not check" — which is a
    different answer from "we checked and it moved", and conflating them is the
    class of mistake this whole issue is about.
    """

    from .environment import read_camilla_statefile_config_path

    config = (applied or {}).get("config")
    recorded = str(config.get("path") or "") if isinstance(config, Mapping) else ""
    if not recorded:
        return APPLIED_PROFILE_PATH_UNKNOWN
    running = read_camilla_statefile_config_path(statefile_path)
    if not running:
        return APPLIED_PROFILE_RUNNING_UNKNOWN
    # ``same_config_file`` rather than a comparison here: "do two paths name
    # one config file" is one rule, and it shipped as two near-verbatim copies
    # until an adversarial gate caught them. Its own docstring carries the
    # resolve-vs-string-compare reasoning.
    return "" if same_config_file(running, recorded) else APPLIED_PROFILE_DISPLACED


def _baseline_apply_started(topology: OutputTopology, candidate: Mapping[str, Any]) -> None:
    log_event(
        logger, "correction.crossover_apply_started",
        config_path=str((candidate.get("config") or {}).get("path") or ""),
        tuning_owner=candidate.get("tuning_owner"), topology_id=topology.topology_id,
        graph_fingerprint=(candidate.get("source") or {}).get("fingerprint"),
        candidate_fingerprint=candidate.get("candidate_fingerprint"),
    )


async def _baseline_apply_result(
    topology: OutputTopology, profile: Mapping[str, Any],
    *, apply_state: DspApplyState, error: DspApplyError | None = None,
) -> dict[str, Any]:
    state = apply_state.to_dict()
    graph_fingerprint = (profile.get("source") or {}).get("fingerprint")
    if error is not None:
        target = baseline_profile_state_path()
        previous = _applied_profile_anchor(_load_saved_state(target))
        profile = {**profile, "status": "apply_failed", "apply": state, "updated_at": _utc_now(),
                   "permissions": {"may_apply": False},
                   "issues": [*profile.get("issues", []), _issue("blocker", "baseline_profile_apply_failed", str(error))]}
        if previous is not None:
            profile["applied_recomposition_profile"] = previous
        atomic_write_text(target, json.dumps(profile, indent=2, sort_keys=True) + "\n", mode=0o640, durable=True)
        # See docs/active-crossover-information-design.md: this event includes failed rollbacks.
        log_event(
            logger, "correction.crossover_apply_rolled_back", topology_id=topology.topology_id,
            graph_fingerprint=graph_fingerprint, reason=str(error),
            rollback_attempted=apply_state.rollback_attempted, rollback_succeeded=apply_state.rollback_succeeded,
            rollback_error=apply_state.rollback_error,
        )
    else:
        log_event(
            logger, "correction.crossover_apply_succeeded", topology_id=topology.topology_id,
            tuning_owner=profile.get("tuning_owner"), graph_fingerprint=graph_fingerprint,
            candidate_fingerprint=profile["candidate_fingerprint"], applied_fingerprint=profile["candidate_fingerprint"],
            applied_at=profile["applied_at"],
        )
        linearization = profile.get("linearization") or {}
        log_event(logger, "dsp.baseline_linearization", topology_id=topology.topology_id,
                  **({role: len(filters) for role, filters in linearization.items()} if linearization else {"none": True}))
    return {"status": profile["status"], "profile": profile, "apply": state, "issues": profile.get("issues", [])}


def _bank_applied_base_trim(candidate: Mapping[str, Any]) -> None:
    """Bank (or clear) the base trim the applied profile is actually playing.

    The single writer of
    :mod:`jasper.active_speaker.driver_base_trim`'s record, and the fix for the
    blind run's F-1: a box could apply a measured level match, report
    ``corrections_source: measured``, and still refuse a ``--level-matched``
    walk ``walk_level_match_no_evidence``, because the resolver
    (:func:`measured_level_trims`) reads only the banked record — never a
    candidate's applied corrections. Banking here makes the applied trim the
    very thing the resolver already looks for, so the walk door and the
    acoustic confirm unblock with no change of their own.

    Three answers, not two, and the middle one is the whole point:

    * **every** role sourced ``measured`` (beside ``level_match.applied``) —
      BANK. The profile is levelled by measurement end to end.
    * **some measured, and every other role OPERATOR-PINNED** — leave the bank
      ALONE, neither banking nor clearing. Pinning one driver by hand does not
      un-measure the speaker, so the prior full measurement is still the best
      evidence anyone has and destroying it on the strength of a pin loses
      real information.
    * **anything else** — CLEAR. No measured role at all, or a role that fell
      back to the datasheet (``sensitivity``/``estimate``) or to a preserved
      manual crossover. That is weaker evidence, not a pin, and a banked trim
      the box is not playing is the same lie pointing the other way.

    The pin/fallback line is drawn by :data:`_PINNED_GAIN_SOURCES` rather than
    by the ANY predicate alone, because ``level_match.applied`` is ITSELF only
    "some role was measured" — see the ANY-arm comment below.

    Fail-soft by contract, exactly as :func:`promote_applied_baseline_candidate`
    is: the graph is applied and read back by the time this runs, so a
    statefile that cannot be written must never turn a successful apply into a
    failure. The speaker then behaves as it did before this seam existed.
    """
    def emit(result: str, reason: str, detail: str, *, level: int) -> None:
        log_event(
            logger,
            "dsp.baseline_base_trim_banked",
            level=level,
            result=result,
            reason=reason,
            detail=detail,
        )

    def refused(reason: str, detail: str) -> None:
        emit("failed", reason, detail, level=logging.WARNING)

    def left_standing(reason: str, detail: str) -> None:
        emit("left_standing", reason, detail, level=logging.INFO)

    def cleared(reason: str, detail: str) -> None:
        if clear_base_trim():
            # A successful clear is a state change an operator must be able to
            # see: without this, the only evidence that a bank was dropped was
            # the absence of the file.
            emit("cleared", reason, detail, level=logging.INFO)
            return
        # The clear could not HAPPEN (EACCES, a read-only /var/lib), which is
        # the opposite of nothing-to-clear: a banked trim survives an apply
        # that is not playing it, and a --level-matched walk would level its
        # graph by numbers nothing applies.
        refused(BANK_CLEAR_FAILED, "a banked trim survived an apply it does not match")

    # Grouping artifacts must not replace the solo trim record.
    snapshot = candidate.get("recomposition_snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("domain") == "driver":
        return

    # A base trim is a FRAME — one role's level relative to the others.
    if snapshot_declares_single_branch(snapshot):
        left_standing(REFUSE_NO_FRAME, "one driver declared, so no roles to level")
        return

    corrections = candidate.get("corrections")
    sources = candidate.get("corrections_source")
    level_match = candidate.get("level_match")
    if not isinstance(corrections, Mapping) or not isinstance(sources, Mapping):
        refused(
            BANK_CORRECTIONS_UNREADABLE,
            "the applied profile names no corrections",
        )
        return
    measured = (
        isinstance(level_match, Mapping)
        and level_match.get("applied") is True
        and bool(corrections)
        and all(sources.get(role) == "measured" for role in corrections)
    )
    if not measured:
        # The middle arm, and it is narrower than "not every role measured".
        # `level_match.applied` is ALREADY only "some role was measured" (see
        # this module's own `level_match["applied"] = bool(measured_notes)`),
        # so the contract's ANY predicate alone cannot tell an operator PIN
        # from a measurement that was REFUSED and fell back to the datasheet.
        # Those are opposites: a pin leaves the speaker measured, while a
        # `sensitivity`/`estimate` fallback IS the weaker evidence the clear
        # exists for.
        if (
            measured_level_match_applied(candidate)
            and all(str(sources.get(role) or "") in _PINNED_GAIN_SOURCES
                    for role in corrections if sources.get(role) != "measured")
        ):
            left_standing(
                BANK_PARTLY_MEASURED,
                "some roles are operator-pinned; the prior banked trim "
                "remains the best measurement of this speaker",
            )
            return
        cleared(
            BANK_UNMEASURED,
            "the applied profile is not level-matched by measurement",
        )
        return
    assert isinstance(level_match, Mapping)  # narrowed by `measured` above
    readiness = candidate.get("automatic_candidate")
    source = candidate.get("source")
    if (
        not isinstance(readiness, Mapping)
        or not readiness.get("measured_group_ids")
        or not isinstance(source, Mapping)
    ):
        # Leave the bank standing rather than clearing it. A frozen applied
        # profile persisted before `_frozen_applied_profile` carried
        # `automatic_candidate` reaches the restore leg in exactly this shape,
        # and it is a MEASURED profile whose readiness block simply was not
        # kept — not evidence that the speaker was never measured.
        left_standing(
            BANK_READINESS_UNREADABLE,
            "the applied profile names no readiness or source block",
        )
        return
    trims_db: dict[str, float] = {}
    for role, entry in corrections.items():
        gain = (
            coerce_finite_float(entry.get("gain_db"))
            if isinstance(entry, Mapping)
            else None
        )
        if gain is None:
            # Typed refusal, never an exception: `float((entry or {}).get(...))`
            # raised AttributeError on a non-Mapping entry, and AttributeError
            # is not something a fail-soft seam catches — it escaped past
            # `persist_applied_baseline_profile` and failed a successful apply.
            left_standing(
                BANK_CORRECTION_ENTRY_UNREADABLE,
                f"correction {str(role)!r} names no finite gain_db",
            )
            return
        trims_db[str(role)] = gain
    # The record's ``measured_at`` is the EVIDENCE time, never this persist's
    # wall clock: this seam re-runs on frozen candidates (the apply retry
    # before the idempotent early-return, any re-apply of an older candidate),
    # and stamping now would re-date old evidence. Candidates carry their own
    # recency in the ledger; a frozen candidate from before that field existed
    # inherits the standing record's time (never re-dated forward), and only a
    # box with no dated evidence and no record lets the writer mint now.
    evidence_at = str(level_match.get("newest_capture_at") or "") or None
    if evidence_at is None:
        existing = load_base_trim()
        if existing is not None:
            evidence_at = str(existing.get("measured_at") or "") or None
    try:
        record = write_base_trim(
            trims_db=trims_db,
            roles=sorted(trims_db),
            speaker_group_ids=readiness.get("measured_group_ids") or [],
            declaration_fingerprint=str(
                source.get("crossover_preview_fingerprint") or ""
            ),
            trim_source=str(level_match.get("comparison") or ""),
            # WHICH CHAIN this trim was co-fitted with (#3479). The resolving
            # candidate's own fingerprint, already on the profile's source
            # block — passed through rather than derived, because the frame
            # exists at fit time and no later reader can reconstruct it. A
            # profile that names none banks as "frame unknown" rather than as
            # the bare frame.
            chain_fingerprint=_passive.measured_candidate_fingerprint(source) or None,
            measured_at=evidence_at,
        )
    except (OSError, DriverBaseTrimError) as exc:
        refused(
            exc.reason if isinstance(exc, DriverBaseTrimError) else BANK_WRITE_FAILED,
            str(exc),
        )
        # A measured graph is now playing and could not be banked, so whatever
        # was banked before describes some OTHER apply. Absent beats wrong:
        # the resolver's empty answer is conservative, while a stale record
        # levels the graph by numbers nothing is playing.
        cleared(BANK_WRITE_REFUSED, "the measured trim could not be banked")
        return
    log_event(
        logger,
        "dsp.baseline_base_trim_banked",
        result="ok",
        trims=" ".join(
            f"{role}={value:.1f}"
            for role, value in sorted(record["trims_db"].items())
        ),
        trim_source=record["trim_source"],
        declaration=record["declaration_fingerprint"][:12],
    )


def _measured_candidate_metadata(
    candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset,
    topology: OutputTopology, created_at: str,
) -> dict[str, Any]:
    roles = required_driver_roles(preset.way_count)
    groups = sorted(group.id for group in topology.speaker_groups if group.mode in {"active_2_way", "active_3_way"})
    measured = candidate.analysis.get("measurement_status") != "unmeasured"
    origin = PROVENANCE_MEASURED if measured else PROVENANCE_MANUAL
    return {
        "sources": {role: "measured" if measured else "operator_pinned" for role in roles},
        "gain_provenance": {role: "measured" if measured else "operator_pinned" for role in roles},
        "provisional": False,
        "corrections_provenance": {role: {"gain_db": origin} for role in roles},
        "level_match": {"groups_total": len(groups), "groups_measured": len(groups) if measured else 0,
                        "comparison": "strict_measured_candidate" if measured else "", "incomparable_groups": [],
                        "applied": measured, "newest_capture_at": created_at if measured else None},
        "automatic_candidate": {"ready": measured, "reason": None, "detail": "",
                                "required_group_ids": groups, "measured_group_ids": groups if measured else [],
                                "summed_group_ids": groups if measured else [],
                                "measurement_comparable": measured, "excitation_comparable": measured},
    }


def _protection_projection(profile: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "targets": [{
            "role": target["role"],
            "target_fingerprint": target["target_fingerprint"],
            "required_protection_filters": [dict(requirement) for requirement in target["required_protection_filters"]],
        } for target in profile["targets"]],
    }


def _candidate_timing(
    candidate: MeasuredCrossoverCandidate, at: str, provenance: Mapping[str, Any] | None,
    saved_timing: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    from jasper.audio_measurement.program_analysis.model import TIMING_AUTHORED  # lazy: analysis loads NumPy

    evidence = candidate.analysis
    source = (evidence.get("resolution") or {}).get("alignment")
    if source == "saved":
        return provenance.get("timing") if provenance is not None else dict(saved_timing) if saved_timing else None
    if source in ("cleared", "base"):
        return None
    if source is None and provenance is not None and provenance.get("timing") is not None:
        return provenance["timing"]
    commissioning = (evidence.get("evidence") or {}).get("commissioning") or {}
    read = commissioning.get("alignment") or {}
    if source == "measured":
        pair = read["committed"]
        return {"delay_us": pair["delay_us"], "polarity": pair["polarity"], "provenance": PROVENANCE_MEASURED,
                "measured": {**{key: read[key] for key in ("margin_db", "residual_rms_db", "repeat_spread_db", "repeat_spread_us",
                                                         "repeat_count", "round_id", "take_id", "graph_fingerprint")}, "at": at}}
    if (source == "document"
            or evidence.get("timing_verdict") == TIMING_AUTHORED) and candidate.alignment.delay_us is not None:
        roles = required_driver_roles(candidate.source_preset.way_count)
        return {"delay_us": candidate.alignment.delay_us * (1 if candidate.alignment.delay_role == roles[1] else -1),
                "polarity": "inverted" if candidate.alignment.polarity == "invert" else "normal",
                "provenance": PROVENANCE_AUTHORED_BY_MODEL}
    return None


def recomposition_snapshot_for(
    candidate: MeasuredCrossoverCandidate,
    *,
    declaration: MeasurementGraphProfile,
    design_draft: Mapping[str, Any],
    projected: MeasuredCrossoverCandidate | None = None,
    topology_fingerprint: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """THE writer of the section set every graph-safety proof recomposes from.

    The pre-apply proof and the persisted profile must recompose the same
    sections; one a single caller assembles by hand is a graph the runtime
    door cannot prove (ADR-0322's ``rear_calibration``).
    """
    from .linearization_fit import linearization_filters_by_role  # lazy: applied graph recording imports NumPy

    shaped = candidate if projected is None else projected
    return {
        **((provenance or {}).get("recomposition_snapshot") or {}),
        "schema_version": 1, "domain": "full", "topology_id": declaration.topology.topology_id,
        "topology_fingerprint": topology_fingerprint or topology_config_fingerprint(declaration.topology),
        "preset": effective_preset(candidate_on_declaration(shaped, declaration.preset)).to_dict(),
        "corrections": driver_corrections(shaped),
        "linearization": linearization_filters_by_role(candidate.linearization),
        **{field.name: field.type(getattr(candidate, field.name)) for row in PROGRAM_DOCUMENT_ORDER
           for field in row.candidate_fields if field.snapshot and field.name != "linearization"},
        "driver_protection": _protection_projection(design_draft.get("driver_safety_profile")),
        "playback_device": declaration.playback_device,
        "measured_candidate_fingerprint": candidate.fingerprint,
    }


def prepare_applied_baseline_profile(
    banked: BankedCandidate,
    *,
    declaration: MeasurementGraphProfile,
    design_draft: Mapping[str, Any],
    config_path: str | Path | None = None,
    crossover_preview: Mapping[str, Any] | None = None,
    config_sha256: str | None = None,
    applied_at: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    saved_timing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an apply record from resolved inputs without reading or writing the bank."""
    candidate = banked.candidate
    protection = _protection_projection(design_draft.get("driver_safety_profile"))
    if crossover_preview is None:
        crossover_preview = build_crossover_preview(design_draft)
    source = _source_payload(
        declaration.topology, design_draft, crossover_preview,
        measured_candidate_fingerprint=candidate.fingerprint, driver_protection=protection,
    )
    source = {**source, **((provenance or {}).get("source") or {}),
              **({"driver_protection_fingerprint": _fingerprint(protection)} if protection is not None else {}),
              "measured_candidate_fingerprint": candidate.fingerprint}
    source["fingerprint"] = _fingerprint({key: value for key, value in source.items() if key != "fingerprint"})
    from .crossover_v2.alignment_prescription import alignment_to_candidate_fields  # lazy: alignment_prescription loads NumPy

    at = applied_at or _utc_now()
    timing = _candidate_timing(candidate, at, provenance, saved_timing)
    projected = candidate
    if timing is not None:
        fields = alignment_to_candidate_fields({**timing, "alignment_status": "ok"},
                                              roles=required_driver_roles(candidate.source_preset.way_count))
        projected = replace(candidate, alignment=MeasuredCrossoverAlignment(*fields))
    meta = _measured_candidate_metadata(candidate, declaration.preset, declaration.topology, at)
    snapshot = recomposition_snapshot_for(candidate, declaration=declaration, design_draft=design_draft,
        projected=projected, topology_fingerprint=source["topology_fingerprint"], provenance=provenance)
    corrections, linearization = snapshot["corrections"], snapshot["linearization"]
    applied = {
        **(provenance or {}),
        "artifact_schema_version": SCHEMA_VERSION, "kind": BASELINE_PROFILE_KIND,
        "candidate_artifact_path": str(banked.path),
        "source": source,
        "config": {**((provenance or {}).get("config") or {}), "path": str(config_path or ""),
                   "basename": Path(config_path).name if config_path else "", "sha256": config_sha256, "exists": bool(config_path),
                   "playback_device": declaration.playback_device, "domain": "full"},
        "corrections": corrections, "linearization": linearization,
        "corrections_source": (provenance or {}).get("corrections_source", meta["sources"]),
        **{key: (provenance or {}).get(key, meta[key]) for key in
           ("gain_provenance", "corrections_provenance", "level_match", "automatic_candidate")},
        "linearization_outcome": (provenance or {}).get("linearization_outcome", candidate.linearization_outcome),
        "trim_decision": (provenance or {}).get("trim_decision", dict(candidate.trim_decision)),
        "tuning_owner": (provenance or {}).get("tuning_owner", "automatic"),
        "blend_correction": snapshot["blend_correction"], "room_correction": snapshot["room_correction"],
        "recomposition_snapshot": snapshot,
    }
    if timing is not None:
        applied["timing"] = timing
    else:
        applied.pop("timing", None)
    snapshot.pop("corrections_provenance", None)
    return applied


def persist_applied_baseline_profile(
    candidate: Mapping[str, Any], *, apply_state: Mapping[str, Any],
    state_path: str | Path | None = None, applied_at: str | None = None,
) -> dict[str, Any]:
    if apply_state.get("result") != "success":
        raise ValueError("successful apply proof is required")
    _bank_applied_base_trim(candidate)
    target = baseline_profile_state_path(state_path)
    existing = _load_saved_state(target)
    identity = baseline_candidate_fingerprint(candidate)
    if (existing and existing.get("status") == "applied"
            and baseline_candidate_fingerprint(existing) == identity
            and existing.get("config") == candidate.get("config")
            and existing.get("timing") == candidate.get("timing")):
        return existing
    now = applied_at or _utc_now()
    applied = {**candidate, "status": "applied", "applied_at": now, "updated_at": now,
               "apply": dict(apply_state), "candidate_fingerprint": identity,
               "permissions": {"may_apply": False}}
    applied.pop("applied_recomposition_profile", None)
    atomic_write_text(target, json.dumps(applied, indent=2, sort_keys=True) + "\n", mode=0o640, durable=True)
    return applied


# Newest-by-mtime content-addressed candidate siblings to keep around a
# canonical baseline config on every successful promote. Orphaned candidates
# accumulate forever now that promotion is a byte COPY, never a move/rename
# (a fleet Pi was observed carrying 38 of them); this is a bounded-I/O
# resilience floor, not a tunable, so it is a plain constant rather than an
# env override.
_MAX_BASELINE_CANDIDATE_FILES = 20


def promote_applied_baseline_candidate(applied: Mapping[str, Any]) -> None:
    """Publish a just-applied candidate's bytes as the canonical config file.

    Reviewed candidates use content-addressed siblings of ``baseline_config_path()``.
    Each candidate lands on its own
    content-addressed sibling, so a candidate that fails validation or
    activation can never appear at the canonical name. This is the ONLY
    place that publishes to that name, and every caller runs it AFTER its own
    ``apply_dsp_config`` + ``persist_applied_baseline_profile`` have already
    proven ``applied`` is the new applied truth -- the copy is durability
    convenience for readers of the canonical name (CamillaDSP's own
    statefile already self-persists the running path independently; the
    multiroom follower fallback in ``jasper.multiroom.follower_config``,
    jasper-doctor, and a human inspecting the box are what this serves), not
    part of the apply decision.

    Fail-soft by design: the running CamillaDSP graph and the JSON SSOT
    (``config.path``, which keeps the truthful applied sibling path -- this
    promotes a COPY, it never rewrites the SSOT) are already correct by the
    time this runs, so a copy failure must never fail an otherwise-successful
    apply. jasper-doctor's baseline-canonical check discloses a stale or
    missing canonical file as an `ok` row, never a service disruption.
    """

    applied_path_raw = (applied.get("config") or {}).get("path")
    if not applied_path_raw:
        return
    applied_path = Path(str(applied_path_raw))
    canonical = baseline_config_path()
    if applied_path == canonical:
        return
    try:
        text = applied_path.read_text(encoding="utf-8")
        atomic_write_text(canonical, text, mode=0o640)
    except (OSError, UnicodeError) as exc:
        # UnicodeError (read_text can raise UnicodeDecodeError on a
        # corrupted-but-present sibling) is a ValueError, not an OSError --
        # it must be caught here too, or a copy failure could raise out of
        # this "must never fail an otherwise-successful apply" boundary.
        log_event(
            logger,
            "dsp.baseline_promote",
            level=logging.WARNING,
            result="failed",
            reason=str(exc),
            candidate_path=applied_path,
            canonical_path=canonical,
        )
        return
    _prune_baseline_candidate_siblings(canonical, protect=applied_path)


def _prune_baseline_candidate_siblings(
    canonical: Path, *, protect: Path,
) -> None:
    """Bound unconditional candidate-sibling growth to the newest K by mtime.

    Deletes ``<stem>_candidate_*<suffix>`` files beside ``canonical`` beyond
    the newest :data:`_MAX_BASELINE_CANDIDATE_FILES` by mtime. Never deletes
    the PROTECTED sibling — ``protect``, the candidate
    :func:`promote_applied_baseline_candidate` just promoted, always the new
    applied anchor — or the canonical file itself (which never matches the
    glob below; it carries no ``_candidate_`` suffix). A displaced sibling
    needs no protection: apply re-emits the banked candidate
    artifact, never a pruned file.

    BLAST RADIUS, stated because the code cannot show it: since #2572 the
    CamillaDSP statefile may durably name a candidate sibling. A deploy's
    reconcile no longer rewrites a content-identical graph to
    ``sound_current.yml``, so a kept correction stays the running config under
    its own candidate name — and pruning that file would leave the statefile
    pointing at nothing, i.e. CamillaDSP failing to start. Safe today only
    because the one caller is the promote path, which always passes the live
    anchor as ``protect``. A future caller, or a prune that stops honouring
    ``protect``, inherits "CamillaDSP starts" as a consequence.
    """

    pattern = f"{canonical.stem}_candidate_*{canonical.suffix}"
    try:
        siblings = list(canonical.parent.glob(pattern))
        prunable = sorted(
            (p for p in siblings if p != protect),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        # The protected sibling always survives and costs one of the K slots,
        # so the on-disk total stays at K.
        protected_present = sum(1 for p in siblings if p == protect)
        keep = max(0, _MAX_BASELINE_CANDIDATE_FILES - protected_present)
        for stale in prunable[keep:]:
            stale.unlink()
    except OSError as exc:
        log_event(
            logger,
            "dsp.baseline_candidate_prune",
            level=logging.WARNING,
            result="failed",
            reason=str(exc),
            canonical_path=canonical,
        )
