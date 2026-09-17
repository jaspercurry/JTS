# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and apply accepted active-speaker baseline profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Sequence

import yaml as yaml_parser

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.dsp_apply import (
    DspApplyError,
    DspApplyState,
    apply_dsp_config,
    dsp_writer_lock,
    same_config_file,
    validate_camilla_config,
)
from jasper.json_fields import utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    canonical_fingerprint as _fingerprint,
    load_output_topology,
    topology_config_fingerprint,
)

from ._common import finite_float as _finite_float, issue as _issue
from .camilla_yaml import (
    _branch_context,
    linearization_headroom_db,
)
from .candidate_bank import BankedCandidate, CandidateBankRefusal, find_banked_candidate, publish_authored_candidate
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
    STATUS_SUPERSEDED as BASE_TRIM_STATUS_SUPERSEDED,
    DriverBaseTrimError,
    banked_base_trims,
    clear_base_trim,
    load_base_trim,
    write_base_trim,
)
from .level_trim import (
    MAX_ATTENUATION_DB,
    LevelTrimError,
    attenuation_from_group_deltas,
)
from .measured_crossover_candidate import (
    MeasuredCrossoverAlignment,
    MeasuredCrossoverCandidate,
    candidate_on_declaration, driver_corrections, effective_preset,
)
from .measurement_programs import PURPOSE_BASS, PURPOSE_ROOM, PURPOSE_SPEAKER
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .profile import LEVEL_MATCH_AXIS, snapshot_declares_single_branch
from . import passive_profile as _passive
from .startup_hold import release_staged_startup_hold
from .state_paths import baseline_profile_state_path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"
DEFAULT_CONFIG_PATH = Path("/var/lib/camilladsp/configs/active_speaker_baseline.yml")
CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH"

REAR_CALIBRATION_WALL_GAP_MISMATCH = "rear_calibration_wall_gap_differs"
REAR_CALIBRATION_FRONT_DELAY_SHIFTS_TIMING = "rear_calibration_front_delay_shifts_timing"
# The wizard declares the wall gap in millimetres while a document carries an
# inch-derived value (0.2032 m), so only a millimetre-scale difference is real.
REAR_CALIBRATION_WALL_GAP_TOLERANCE_M = 0.001

# How far the MEASURED level match and the pad-folded DATASHEET sensitivity gap
# may disagree about the same pair of drivers before the measured value is
# refused (linearization-integrity PR-L4 item 3). Two independent frames for one
# physical quantity; on the 2026-07-27 JTS3 run they were ~12 dB apart and were
# compared nowhere.
#
# 6.0 dB, summed from what CAN honestly differ between them:
#
#   ~2 dB   driver datasheet sensitivity is typically specified +/-2 dB, and
#           a household transcribes it from a spec sheet
#   ~2 dB   an L-pad's REALIZED attenuation follows the driver's actual
#           impedance curve, not the nominal resistance the pad math assumes
#   ~1.3 dB the measured estimator's own frame spread — PR-L3's five archived
#           captures agreed with the fit frame to 1.30 dB worst case
#   ~0.5 dB that estimator's known linear-bin systematic
#
# ~5.8 dB of honest disagreement in the worst case; 6.0 is the first whole dB
# above it. The defect this exists to catch was more than twice that.
# Tightening it is a measurement question, not a taste one: it needs a corpus
# of households whose datasheet AND pad values are both known-good — and note
# how little headroom 6.0 leaves over 5.8, which is the real argument for
# gathering that corpus rather than nudging the number.
MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB = 6.0

# How far the crossover sweep and the level-match sitting may place one driver
# apart before the gap is DISCLOSED. A disclosure trigger, never an agreement
# bar and never a refusal (ruling S8): both sittings read THE level fact, so
# this decides when a gap is worth saying, never which number is right.
#
# Same value as the refusal bar above, and the reuse is accounted for rather
# than assumed, because only part of that derivation survives here: its ~2 dB
# datasheet-spec and ~2 dB realized-pad terms are DATASHEET artifacts, absent
# on a measured-vs-measured comparison. What survives is ~1.3 dB of frame
# spread plus ~0.5 dB of linear-bin systematic; the remaining ~4 dB is headroom
# for the term neither constant measures — two captures at different distances,
# on different axes, in different sittings.
#
# Tightening toward that ~1.8 dB is a MEASUREMENT question, not a taste one: it
# needs a corpus of speakers read both ways in one session. Until that exists
# the wider trigger only discloses less often, the direction that cannot
# mislead.
LEVEL_SITTING_TOLERANCE_DB = MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB

# Canonical per-parameter provenance vocabulary (SC-3). ``RECOMMENDED_START``
# is reserved for future profile prefills; no code path in this module emits
# it directly (it only appears via the gain-source migration map below).
PROVENANCE_MANUAL = "manual"
PROVENANCE_MEASURED = "measured"
PROVENANCE_AUTHORED_BY_MODEL = "authored_by_model"
PROVENANCE_SET_BY_USER = "set_by_user"
PROVENANCE_RECOMMENDED_START = "recommended_start"
PROVENANCE_PRESERVED = "preserved"

# Reporting-layer migration from the legacy per-role gain-trim vocabulary
# (this module's own ``sources[role]`` values, plus ``"explicit"`` kept as a
# legacy alias for completeness) to the canonical provenance strings above.
# The legacy ``corrections_source`` / ``gain_provenance`` payload keys are NOT
# renamed or removed by this map — it only feeds the additional
# ``corrections_provenance`` block. A source with no entry here (``"none"``)
# makes no provenance claim, mirroring an untouched role.
_GAIN_SOURCE_TO_PROVENANCE: dict[str, str] = {
    "measured": PROVENANCE_MEASURED,
    "operator_pinned": PROVENANCE_MANUAL,
    "explicit": PROVENANCE_MANUAL,
    "estimate": PROVENANCE_RECOMMENDED_START,
    "sensitivity": PROVENANCE_RECOMMENDED_START,
}


def applied_bass_extension(profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = load_applied_baseline_profile_state() if profile is None else profile
    snapshot = (source or {}).get("recomposition_snapshot") or {}
    raw = snapshot.get("bass_extension") or {}
    return validate_dynamic_bass_descriptor(raw) if raw else {}


def baseline_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


def baseline_candidate_config_path(text: str, path: str | Path | None = None) -> Path:
    target = baseline_config_path(path)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return target.with_name(f"{target.stem}_candidate_{sha256[:12]}{target.suffix}")


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
            sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
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
        # ``newest_capture_at`` is evidence-recency metadata for the banked
        # trim's clock, not graph identity: the measured-candidate arm mints it
        # from the compose instant, and the seams that compare this fingerprint
        # across two composes of the same inputs (review -> apply, the
        # idempotent re-apply) must hash equal across a second boundary.
        # Absent-key candidates (pre-field artifacts) hash unchanged.
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


def rear_calibration_issues(candidate: MeasuredCrossoverCandidate) -> list[dict[str, str]]:
    """Disclose what a cardioid document assumes but cannot prove (ADR-0101).

    The declared-geometry reads live here. A disclosure the document alone
    carries is attached to the candidate at construction instead
    (``measured_crossover_candidate._rear_calibration_disclosure``), so it
    already rides ``analysis["issues"]`` and is never re-derived here.
    """
    from jasper.audio_measurement.measurement_geometry import load_declared_geometry  # lazy: geometry pulls NumPy in

    document = candidate.rear_calibration
    if not document:
        return []
    issues: list[dict[str, str]] = []
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
    return issues


def compile_commissioning_profile(
    *, topology: OutputTopology | None = None,
    design_draft: Mapping[str, Any] | None = None, write: bool = False,
    find_candidate: Callable[[str], BankedCandidate] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Review the applied candidate, or bootstrap from the declared crossover."""
    from .candidate_parts import candidate_from_applied_profile  # lazy: candidate parts consumes baseline readers
    from .commissioning_experiment import commissioning_candidate  # lazy: candidate parts consumes baseline readers
    from .design_draft import load_design_draft  # lazy: design draft imports baseline readers
    from .measurement import load_measurement_state  # lazy: measurement imports baseline readers
    from .measurement_emit import compile_tuning_graph, load_tuning_declaration, MeasurementGraphRefused  # lazy: graph compilation imports baseline readers
    from .runtime_contract import classify_bass_extension_graph, GRAPH_APPROVED_ACTIVE_RUNTIME  # lazy: graph proof imports baseline state
    from jasper.sound.settings import saved_sound_layers  # lazy: household settings import baseline readers

    profile: dict[str, Any] = {"artifact_schema_version": SCHEMA_VERSION, "kind": BASELINE_PROFILE_KIND,
                              "status": "blocked", "permissions": {"may_apply": False}, "issues": []}
    text = ""
    try:
        topology = topology if topology is not None else load_output_topology()
        draft = design_draft if design_draft is not None else load_design_draft(topology=topology)
        declaration = load_tuning_declaration(topology, design_draft=draft)
        applied = load_applied_baseline_profile_state()
        candidate = (candidate_from_applied_profile(topology, applied, find_candidate=find_candidate) if applied is not None
                     else commissioning_candidate(topology, draft))
        preference_filters, trim_db = saved_sound_layers()
        text = compile_tuning_graph(declaration, candidate=candidate,
                                    preference_filters=preference_filters, output_trim_db=trim_db)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        target = baseline_candidate_config_path(text)
        profile.update(prepare_applied_baseline_profile(
            candidate, declaration=declaration, design_draft=draft, measurements=load_measurement_state(topology),
            config_path=target, config_sha256=sha, find_candidate=find_candidate,
        ))
        profile["issues"] = [*(candidate.analysis.get("issues") or []), *rear_calibration_issues(candidate)]
        profile["candidate_fingerprint"] = baseline_candidate_fingerprint(profile)
        profile["config"]["exists"] = target.exists()
        proof = classify_bass_extension_graph(topology, evidence_source="desired", graph_text=text, applied_baseline_state=profile)
        if not proof.allowed or proof.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
            raise MeasurementGraphRefused("baseline_graph_safety_proof_failed", proof.classification)
        if write:
            atomic_write_text(target, text, mode=CONFIG_FILE_MODE)
            profile["config"]["exists"] = True
            validation = validate_camilla_config(target)
            if not validation.ok_to_apply:
                raise MeasurementGraphRefused("baseline_config_validation_failed", validation.to_dict())
        profile.update(status="ready_to_apply" if write else "ready_to_compile",
                       permissions={"may_apply": write, "may_compile": True})
    except (CandidateBankRefusal, ValueError) as exc:
        _commissioning_refusal(profile, exc)
    return text, profile


def _canonicalize_camilla_defaults(value: Any) -> Any:
    """Remove representation-only null defaults from Camilla readback.

    CamillaDSP's ``active_raw`` re-serialization writes omitted optional
    mapping fields back as explicit YAML nulls.  Omitted and null mean the same
    default to Camilla, so they must not make a safely loaded Layer-A graph
    appear different from the immutable YAML that produced it.  Non-null
    values and list positions remain exact and therefore hardware-bound.
    """

    if isinstance(value, Mapping):
        return {
            key: _canonicalize_camilla_defaults(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_canonicalize_camilla_defaults(item) for item in value]
    return value


def active_layer_a_projection(config_text: str) -> dict[str, Any]:
    """Project the exact driver-domain suffix of one active graph.

    Room and preference EQ are allowed to change the program-domain filter
    prefix before the active split.  Everything from the split onward, plus
    the output-side device contract, is Layer A: routing, crossover filters,
    polarity, delay, gain, and protection.  This projection lets Active bind
    its immutable applied snapshot to the graph Room is about to preserve
    without making Room reconstruct crossover evidence.

    :func:`active_layer_a_fingerprint` is this projection hashed.  The
    unhashed form exists so a caller that has already learned the two
    fingerprints differ can name WHICH member moved, rather than handing an
    operator two opaque digests.
    """

    try:
        raw = yaml_parser.safe_load(config_text)
    except yaml_parser.YAMLError as exc:
        raise ActiveSpeakerConfigError(
            "active Layer-A graph must be parseable YAML"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ActiveSpeakerConfigError("active Layer-A graph must be an object")

    pipeline = raw.get("pipeline")
    if not isinstance(pipeline, list):
        raise ActiveSpeakerConfigError("active Layer-A graph pipeline is missing")
    split_index = next(
        (
            index
            for index, step in enumerate(pipeline)
            if isinstance(step, Mapping) and step.get("type") == "Mixer"
        ),
        None,
    )
    if split_index is None:
        raise ActiveSpeakerConfigError("active Layer-A driver split is missing")
    suffix = pipeline[split_index:]

    filters = raw.get("filters")
    filter_map = filters if isinstance(filters, Mapping) else {}
    referenced_filters: dict[str, Any] = {}
    for step in suffix:
        if not isinstance(step, Mapping) or step.get("type") != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not name for name in names
        ):
            raise ActiveSpeakerConfigError(
                "active Layer-A filter step has invalid names"
            )
        for name in names:
            definition = filter_map.get(name)
            if not isinstance(definition, Mapping):
                raise ActiveSpeakerConfigError(
                    f"active Layer-A filter {name!r} is missing"
                )
            referenced_filters[name] = definition

    devices = raw.get("devices")
    if not isinstance(devices, Mapping):
        raise ActiveSpeakerConfigError("active Layer-A devices are missing")
    output_devices = {
        str(key): value
        for key, value in devices.items()
        if key != "capture"
    }
    mixers = raw.get("mixers")
    if not isinstance(mixers, Mapping):
        raise ActiveSpeakerConfigError("active Layer-A mixers are missing")
    referenced_mixers: dict[str, Any] = {}
    for step in suffix:
        if not isinstance(step, Mapping) or step.get("type") != "Mixer":
            continue
        name = step.get("name")
        definition = mixers.get(name) if isinstance(name, str) else None
        if not name or not isinstance(definition, Mapping):
            raise ActiveSpeakerConfigError("active Layer-A mixer is missing")
        referenced_mixers[name] = definition

    return _canonicalize_camilla_defaults({
        "schema_version": 1,
        "domain": "jts_active_layer_a_v1",
        "output_devices": output_devices,
        "mixers": referenced_mixers,
        "pipeline_suffix": suffix,
        "filters": referenced_filters,
    })


def active_layer_a_fingerprint(config_text: str) -> str:
    """Hash :func:`active_layer_a_projection` — the Layer-A graph identity."""
    return _fingerprint(active_layer_a_projection(config_text))


def _source_payload(
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    measured_candidate_fingerprint: str | None = None,
    driver_protection: Mapping[str, Any] | None = None,
    candidate_graph_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fingerprint declaration and evidence inputs, not emitted bytes."""
    measurement_summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), Mapping)
        else {}
    )
    # The baseline config cache invalidates whenever this source fingerprint
    # changes, so nothing spurious may ride the topology fingerprint — what it
    # covers and why is `topology_config_fingerprint`'s own docstring.
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        "design_draft_content_fingerprint": _fingerprint({
            key: design_draft.get(key)
            for key in ("manual_settings", "operator_inputs", "driver_research")
        }),
        # Banked driver trims must match the declaration they measured.
        "crossover_preview_fingerprint": crossover_preview_fingerprint(
            crossover_preview, design_draft
        ),
        "measurements_updated_at": measurements.get("updated_at"),
        "measurement_summary_fingerprint": _fingerprint(measurement_summary),
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


def _overlap_level_at(
    record: Any, fc: float, *, tol_hz: float = 1.0
) -> float | None:
    """A usable measured overlap-band level (dB) for ``fc``, or None (fail-closed).

    Requires the driver's acoustic verdict to be ``present`` (the driver actually
    produced in-band sound) and an overlap entry around ``fc`` flagged ``usable``
    (good SNR, not silent, not clipped, enough bins). Anything else returns None,
    so a missing / low-SNR / clipped capture cannot contribute a measured trim.
    """
    from .driver_acoustics import usable_overlap_level_db

    if not isinstance(record, Mapping):
        return None
    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping) or acoustic.get("verdict") != "present":
        return None
    return usable_overlap_level_db(
        acoustic.get("overlap_levels") or (), fc, tol_hz=tol_hz
    )


def _effective_excitation_dbfs(record: Any) -> float | None:
    """Return a verified analyzer excitation, or ``None`` (fail closed).

    The excitation artifact is a small gain ledger owned by the capture record:
    generated sweep peak + the role-varying commissioning gain + any exact
    server-owned main-volume lock = the effective digital drive (the remaining
    commissioning gains are common and cancel).
    We recompute the total instead of trusting a loose scalar, which makes the
    evidence independently auditable and lets captures made through different
    applied role trims be normalized onto one common 0 dB reference. The quiet
    by-ear identity-test level is not acoustic measurement evidence.
    """
    if not isinstance(record, Mapping):
        return None
    from .crossover_contract import verified_driver_excitation

    verified = verified_driver_excitation(record.get("excitation"))
    return (
        float(verified["effective_peak_dbfs"])
        if isinstance(verified, Mapping)
        else None
    )


def measured_level_trims(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    crossover_preview: Mapping[str, Any] | None = None,
    *,
    design_draft: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The public door onto :func:`_measured_level_trims`, for callers outside
    the profile build.

    It exists because the crossover-v2 MEASUREMENT graph needs the same answer
    the applied profile needs — *"what does this box's own evidence say the
    per-driver level offsets are?"* — and a second derivation of it would be
    the third opinion the one-owner rule forbids. A thin wrapper rather than a
    rename because the private name is the one the profile's own callers and
    their fixtures spell.

    ``meta['source']`` names WHICH evidence answered (``banked_base_trim`` or
    ``guided_captures``), which is what a caller discloses beside the trims;
    an empty mapping means neither did, and no caller may substitute an
    estimate for it.
    """
    return _measured_level_trims(preset, measurements, crossover_preview, design_draft=design_draft)


def _measured_level_trims(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    crossover_preview: Mapping[str, Any] | None = None,
    *,
    design_draft: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    from .capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    roles = required_driver_roles(preset.way_count)
    declaration_fingerprint = (
        crossover_preview_fingerprint(crossover_preview, design_draft)
        if isinstance(crossover_preview, Mapping) and crossover_preview
        else None
    )
    base_trims, base_trim_meta = banked_base_trims(declaration_fingerprint, roles)
    # A valid banked trim does NOT return here: whether it wins depends on the
    # guided walk below — captures newer than the record supersede it (S20).

    active_comparison_set = measurements.get("active_comparison_set")
    latest = measurements.get("latest_by_target")
    if not isinstance(latest, Mapping):
        summary = measurements.get("summary")
        latest = (
            summary.get("latest_driver_measurements")
            if isinstance(summary, Mapping)
            else None
        )
    records = [
        record
        for record in (latest.values() if isinstance(latest, Mapping) else [])
        if isinstance(record, Mapping)
    ]

    regions = sorted(preset.crossover_regions, key=lambda region: region.fc_hz)

    by_group: dict[str, dict[str, Mapping[str, Any]]] = {}
    for record in records:
        group_id = record.get("speaker_group_id")
        role = record.get("role")
        if (
            isinstance(group_id, str)
            and group_id
            and isinstance(role, str)
            and role in roles
        ):
            by_group.setdefault(group_id, {})[role] = record

    per_group_delta_chains: list[list[tuple[str, str, float]]] = []
    deltas: list[dict[str, Any]] = []
    incomparable_groups: list[dict[str, Any]] = []
    # Newest ``created_at`` across the records the ACCEPTED chains consumed.
    # ISO-8601 UTC strings compare lexicographically; an undated record
    # contributes "" and so can never claim to be newer than the banked trim.
    newest_capture_at = ""
    for group_id, group_records in sorted(by_group.items()):
        if not any(
            isinstance(record.get("acoustic"), Mapping)
            for record in group_records.values()
        ):
            # Operator-only floor checks prove routing but are not attempted as
            # acoustic level evidence, so do not diagnose their intentionally
            # absent analyzer ledger as malformed.
            continue
        placement_invalid_roles = [
            role
            for role in roles
            if not capture_proof_valid(
                group_records.get(role),
                active_comparison_set,
                policy_id=DRIVER_PLACEMENT_POLICY_ID,
                role=role,
                speaker_group_id=group_id,
            )
        ]
        if placement_invalid_roles:
            incomparable_groups.append({
                "speaker_group_id": group_id,
                "reason": "placement_or_comparison_set_missing_or_invalid",
                "roles": placement_invalid_roles,
            })
            continue
        excitation_by_role = {
            role: _effective_excitation_dbfs(group_records.get(role))
            for role in roles
        }
        if any(value is None for value in excitation_by_role.values()):
            incomparable_groups.append({
                "speaker_group_id": group_id,
                "reason": "excitation_ledger_missing_or_invalid",
            })
            continue
        assert all(value is not None for value in excitation_by_role.values())
        adjacent_deltas: list[tuple[str, str, float]] = []
        group_deltas: list[dict[str, Any]] = []
        usable = True
        for region in regions:
            lo_role = region.lower_driver
            up_role = region.upper_driver
            fc = float(region.fc_hz)
            measured_lo = _overlap_level_at(group_records.get(lo_role), fc)
            measured_up = _overlap_level_at(group_records.get(up_role), fc)
            level_lo = (
                measured_lo - float(excitation_by_role[lo_role])
                if measured_lo is not None
                else None
            )
            level_up = (
                measured_up - float(excitation_by_role[up_role])
                if measured_up is not None
                else None
            )
            if level_lo is None or level_up is None:
                usable = False
                break
            adjacent_deltas.append((lo_role, up_role, level_up - level_lo))
            group_deltas.append({
                "speaker_group_id": group_id,
                "crossover_fc_hz": fc,
                "lower_role": lo_role,
                "upper_role": up_role,
                "delta_db": round(level_up - level_lo, 1),  # + => upper hotter
                "effective_peak_dbfs": {
                    lo_role: round(float(excitation_by_role[lo_role]), 2),
                    up_role: round(float(excitation_by_role[up_role]), 2),
                },
            })
        if not usable:
            continue
        per_group_delta_chains.append(adjacent_deltas)
        deltas.extend(group_deltas)
        for record in group_records.values():
            newest_capture_at = max(
                newest_capture_at, str(record.get("created_at") or "")
            )

    meta: dict[str, Any] = {
        "source": "guided_captures",
        "base_trim": base_trim_meta,
        "groups_total": len(by_group),
        "groups_measured": len(per_group_delta_chains),
        "measured_group_ids": sorted({
            str(item["speaker_group_id"])
            for item in deltas
            if item.get("speaker_group_id")
        }),
        "deltas": deltas,
        # WHEN this answer's evidence was measured. The apply seam banks it as
        # the record's ``measured_at``, so a re-persist of a frozen candidate
        # re-banks the evidence time, never the persist time.
        "newest_capture_at": newest_capture_at,
        "comparison": "placement_attested_gain_ledger_normalized",
        "placement_policy": DRIVER_PLACEMENT_POLICY_ID,
        "active_comparison_set_id": (
            active_comparison_set.get("comparison_set_id")
            if isinstance(active_comparison_set, Mapping)
            else None
        ),
        "incomparable_groups": incomparable_groups,
    }
    trims: dict[str, float] = {}
    if per_group_delta_chains:
        try:
            trims = attenuation_from_group_deltas(
                roles, per_group_delta_chains, minimum_db=MAX_ATTENUATION_DB
            )
        except LevelTrimError:
            trims = {}

    if base_trims:
        banked_measured_at = str(base_trim_meta.get("measured_at") or "")
        if not trims or newest_capture_at <= banked_measured_at:
            banked_group_ids = base_trim_meta.get("speaker_group_ids") or []
            return base_trims, {
                "source": "banked_base_trim",
                "base_trim": base_trim_meta,
                # The record's ``measured_at`` IS the evidence time, so a
                # candidate levelled by the bank re-banks that same instant.
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
        # Ruling S20: the captures behind this guided answer postdate the
        # banked record, so the newest measurement wins. Disclosed with both
        # evidence identities, so the receipt trail shows the handoff.
        log_event(
            logger,
            "dsp.baseline_base_trim_superseded",
            superseded_measured_at=banked_measured_at,
            superseded_trim_source=str(base_trim_meta.get("trim_source") or ""),
            declaration=str(
                base_trim_meta.get("declaration_fingerprint") or ""
            )[:12],
            newest_capture_at=newest_capture_at,
            comparison=str(meta["comparison"]),
            comparison_set_id=str(meta["active_comparison_set_id"] or ""),
        )
        meta["base_trim"] = {
            **base_trim_meta,
            "status": BASE_TRIM_STATUS_SUPERSEDED,
        }

    if not trims:
        return {}, meta
    meta["trims"] = dict(trims)
    return trims, meta


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
    return {
        PURPOSE_SPEAKER: bool(profile_linearization(profile)),
        PURPOSE_ROOM: bool(snapshot.get("room_correction", profile.get("room_correction"))),
        PURPOSE_BASS: bool(snapshot.get("bass_extension", profile.get("bass_extension"))),
    }


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


def _compare_level_sittings(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    candidate_trims_db: Mapping[str, float],
) -> tuple[list[str], dict[str, Any]]:
    """How far apart two SITTINGS place the same level fact, as copy strings.

    **One definition, read twice** (ruling S8), which is what separates this
    from its two siblings. "Level-matched" means matched acoustic output
    through the handover region, and both numbers here answer exactly that:

    * The persisted point-at-Fc read via :func:`_measured_level_trims` — both
      branches sit on their matched
      −6 dB Linkwitz-Riley shoulder, so their delta is the sensitivity delta,
      taken as a single interpolated point;
    * ``program_analysis.solve_branch_trims``' power-band average over the
      mirrored ±1-octave halves about Fc, which is what a v2 measured candidate
      carries and which S8 makes THE level fact.

    Its two siblings ask something else. ``intervention``'s
    :func:`~jasper.active_speaker.crossover_v2.intervention.compare_level_definitions`
    reports the handover level against the PASSBAND estimate — two physical
    questions on ONE capture; item 3(a)'s measured-vs-datasheet check grades
    one capture against a physical model. Here the definition is shared and the
    CAPTURE is not: the crossover MEASURE sweep and the GUIDED per-driver
    captures are separate sittings, and the candidate branch below never runs
    the point-at-Fc path itself.

    **The guided captures, never the banked base trim**, which is why no
    crossover preview reaches :func:`_measured_level_trims` here. Since ruling
    S16 the banked trim is written BY the apply, from the very candidate whose
    trims this compares — reading it back would compare a number with itself
    and report agreement that means nothing. Passing no preview is the
    documented way to ask that resolver for the guided sitting alone.

    So a gap here is neither a fault nor a verdict on either number — the
    sittings are taken at different distances and on different axes, which
    ``profile.LEVEL_MATCH_AXIS`` explains has no single correct answer.
    ``frame`` therefore names both: the MEASURE sweep's axis is that constant,
    while the other sitting's geometry is per-capture and is disclosed by the
    guided captures' own placement attestation. A level gap whose frames the
    reader cannot recover is unplaceable.

    **Disclosed, never refused**, and nothing is asked for: the candidate's own
    trim ships whatever this says, so there is no remediation to recommend.
    ``notes`` is empty when nothing crosses
    :data:`LEVEL_SITTING_TOLERANCE_DB`, the common case; ``frame`` rides
    unconditionally because it describes the captures rather than whichever
    condition was the reason.
    """
    frame: dict[str, Any] = {"crossover_sweep_axis": LEVEL_MATCH_AXIS}
    if not candidate_trims_db:
        return [], frame
    try:
        point_trims, point_meta = _measured_level_trims(preset, measurements)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # The point-at-Fc reader is fail-closed by design and this is a
        # disclosure path; an unreadable measurements blob means "no second
        # sitting available", never a failed profile build. Logged at WARNING
        # like its sibling below, so a comparison that silently stopped running
        # is visible rather than indistinguishable from two sittings agreeing.
        log_event(
            logger, "baseline_profile.level_sitting_comparison_unavailable",
            level=logging.WARNING, error=f"{type(exc).__name__}: {exc}",
        )
        return [], frame
    frame["level_match_sitting"] = point_meta.get("source")
    notes: list[str] = []
    for role in sorted(set(point_trims) & set(candidate_trims_db)):
        gap_db = abs(point_trims[role] - candidate_trims_db[role])
        if gap_db <= LEVEL_SITTING_TOLERANCE_DB:
            continue
        notes.append(
            f"{role} crossover sweep {candidate_trims_db[role]:.1f} dB vs "
            f"level match {point_trims[role]:.1f} dB "
            f"({gap_db:.1f} dB apart)"
        )
    return notes, frame


def _bundle_dir_from_measurements(measurements: Mapping[str, Any]) -> Path | None:
    """The open commissioning bundle a comparison set was stamped with, if any.

    A follower/driver_domain apply, a manual-only apply with no comparison
    set, or measurements shaped unexpectedly all resolve to ``None`` — there
    is simply nothing to record an apply outcome into.
    """

    try:
        comparison_set = measurements.get("active_comparison_set")
        session_id = (
            comparison_set.get("bundle_session_id")
            if isinstance(comparison_set, Mapping)
            else None
        )
    except (AttributeError, TypeError):
        return None
    if not session_id:
        return None

    from jasper.active_speaker import bundles as active_speaker_bundles

    return active_speaker_bundles.sessions_dir() / str(session_id)


async def _record_apply_outcome_into_bundle(
    measurements: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    apply_state: Mapping[str, Any] | None,
    rollback_target: Mapping[str, Any] | None,
) -> None:
    """Record the outcome off-thread; the bundle writer handles I/O failures."""

    bundle_dir = _bundle_dir_from_measurements(measurements)
    if bundle_dir is None:
        return

    from jasper.active_speaker import bundles as active_speaker_bundles

    await asyncio.to_thread(
        active_speaker_bundles.record_apply,
        bundle_dir,
        candidate=candidate,
        apply_state=apply_state,
        rollback_target=rollback_target,
    )


def _baseline_apply_started(topology: OutputTopology, candidate: Mapping[str, Any]) -> None:
    log_event(
        logger, "correction.crossover_apply_started",
        config_path=str((candidate.get("config") or {}).get("path") or ""),
        tuning_owner=candidate.get("tuning_owner"), topology_id=topology.topology_id,
        graph_fingerprint=(candidate.get("source") or {}).get("fingerprint"),
        candidate_fingerprint=candidate.get("candidate_fingerprint"),
    )


async def _baseline_apply_result(
    topology: OutputTopology, profile: Mapping[str, Any], measurements: Mapping[str, Any],
    *, apply_state: DspApplyState, error: DspApplyError | None = None, state_path: str | Path | None = None,
) -> dict[str, Any]:
    state = apply_state.to_dict()
    graph_fingerprint = (profile.get("source") or {}).get("fingerprint")
    if error is not None:
        target = baseline_profile_state_path(state_path)
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
    await _record_apply_outcome_into_bundle(
        measurements, candidate=profile, apply_state=state,
        rollback_target={"config_path": apply_state.prior_config_path} if apply_state.prior_config_path else None,
    )
    return {"status": profile["status"], "profile": profile, "apply": state, "issues": profile.get("issues", [])}


def _bank_applied_base_trim(candidate: Mapping[str, Any]) -> None:
    """Bank (or clear) the base trim the applied profile is actually playing.

    The single writer of
    :mod:`jasper.active_speaker.driver_base_trim`'s record, and the fix for the
    blind run's F-1: a box could apply a measured level match, report
    ``corrections_source: measured``, and still refuse a ``--level-matched``
    walk ``walk_level_match_no_evidence``, because the resolver
    (:func:`_measured_level_trims`) reads only the banked record and the guided
    captures — never a candidate's applied corrections. Banking here makes the
    applied trim the very thing the resolver already looks for, so the walk
    door and the acoustic confirm unblock with no change of their own.

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

    The pin/fallback line is drawn by this module's own
    ``_GAIN_SOURCE_TO_PROVENANCE`` map rather than by the ANY predicate alone,
    because ``level_match.applied`` is ITSELF only "some role was measured" —
    see the ANY-arm comment below.

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
        # exists for. The existing `_GAIN_SOURCE_TO_PROVENANCE` vocabulary
        # already draws that line, so it is consumed here rather than a third
        # classifier being minted for it.
        unmeasured_provenance = {
            _GAIN_SOURCE_TO_PROVENANCE.get(str(sources.get(role) or ""))
            for role in corrections
            if sources.get(role) != "measured"
        }
        if (
            measured_level_match_applied(candidate)
            and unmeasured_provenance <= {PROVENANCE_MANUAL}
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
            _finite_float(entry.get("gain_db"))
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
    # The record's ``measured_at`` is the EVIDENCE time (the S20 supersede
    # compares capture times against it), never this persist's wall clock:
    # this seam re-runs on frozen candidates (the apply retry before the
    # idempotent early-return, any re-apply of an older candidate), and
    # stamping now would re-date old evidence past strictly newer captures —
    # silently, forever. Candidates carry their own recency in the ledger;
    # a frozen candidate from before that field existed inherits the standing
    # record's time (never re-dated forward), and only a box with no dated
    # evidence and no record lets the writer mint now.
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
            # profile levelled by the guided captures names none, which banks
            # as "frame unknown" rather than as the bare frame.
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
        # the resolver's fallback (guided captures, then the datasheet) is
        # conservative, while a stale record levels the graph by numbers
        # nothing is playing.
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
    topology: OutputTopology, measurements: Mapping[str, Any], created_at: str,
) -> dict[str, Any]:
    roles = required_driver_roles(preset.way_count)
    groups = sorted(group.id for group in topology.speaker_groups if group.mode in {"active_2_way", "active_3_way"})
    measured = candidate.analysis.get("measurement_status") != "unmeasured"
    notes, frame = _compare_level_sittings(preset, measurements, dict(candidate.role_attenuations_db)) if measured else ([], {})
    origin = PROVENANCE_MEASURED if measured else PROVENANCE_MANUAL
    return {
        "sources": {role: "measured" if measured else "operator_pinned" for role in roles},
        "gain_provenance": {role: "measured" if measured else "operator_pinned" for role in roles},
        "provisional": False,
        "corrections_provenance": {role: {"gain_db": origin} for role in roles},
        "level_match": {"groups_total": len(groups), "groups_measured": len(groups) if measured else 0,
                        "comparison": "strict_measured_candidate" if measured else "", "incomparable_groups": [],
                        "applied": measured, "newest_capture_at": created_at if measured else None,
                        "sitting_differences": list(notes), "sitting_frame": frame},
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
) -> dict[str, Any] | None:
    from jasper.audio_measurement.program_analysis.model import TIMING_AUTHORED  # lazy: analysis loads NumPy

    evidence = candidate.analysis
    source = (evidence.get("resolution") or {}).get("alignment")
    if source == "saved":
        return ((provenance if provenance is not None else load_applied_baseline_profile_state()) or {}).get("timing")
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
        "blend_correction": list(candidate.blend_correction),
        "room_correction": dict(candidate.room_correction), "bass_extension": dict(candidate.bass_extension),
        "rear_calibration": dict(candidate.rear_calibration),
        "driver_protection": _protection_projection(design_draft.get("driver_safety_profile")),
        "playback_device": declaration.playback_device,
        "measured_candidate_fingerprint": candidate.fingerprint,
    }


def prepare_applied_baseline_profile(
    candidate: MeasuredCrossoverCandidate,
    *,
    declaration: MeasurementGraphProfile,
    design_draft: Mapping[str, Any],
    measurements: Mapping[str, Any],
    config_path: str | Path | None = None,
    config_sha256: str | None = None,
    applied_at: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    find_candidate: Callable[[str], BankedCandidate] | None = None,
) -> dict[str, Any]:
    """Resolve the complete applied record before changing the DSP graph."""
    try:
        banked = (find_candidate or find_banked_candidate)(candidate.fingerprint)
    except CandidateBankRefusal as exc:
        if exc.code != "not_found":
            raise
        banked = publish_authored_candidate(candidate)
    protection = _protection_projection(design_draft.get("driver_safety_profile"))
    source = _source_payload(
        declaration.topology, design_draft, build_crossover_preview(design_draft), measurements,
        measured_candidate_fingerprint=candidate.fingerprint, driver_protection=protection,
    )
    source = {**source, **((provenance or {}).get("source") or {}),
              **({"driver_protection_fingerprint": _fingerprint(protection)} if protection is not None else {}),
              "measured_candidate_fingerprint": candidate.fingerprint}
    source["fingerprint"] = _fingerprint({key: value for key, value in source.items() if key != "fingerprint"})
    from .crossover_v2.planning import alignment_to_candidate_fields  # lazy: planning loads NumPy

    at = applied_at or _utc_now()
    timing = _candidate_timing(candidate, at, provenance)
    projected = candidate
    if timing is not None:
        fields = alignment_to_candidate_fields({**timing, "alignment_status": "ok"},
                                              roles=required_driver_roles(candidate.source_preset.way_count))
        projected = replace(candidate, alignment=MeasuredCrossoverAlignment(*fields))
    meta = _measured_candidate_metadata(candidate, declaration.preset, declaration.topology, measurements, at)
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
    release_staged_startup_hold()
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
               "revalidation": {"required": False, "status": "not_required"},
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


def promote_applied_baseline_candidate(
    applied: Mapping[str, Any],
    *,
    config_path: str | Path | None = None,
) -> None:
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
    canonical = baseline_config_path(config_path)
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
