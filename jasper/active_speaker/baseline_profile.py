# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compile and apply accepted active-speaker baseline profiles.

The baseline profile is the handoff from commissioning into normal playback:
it consumes saved crossover settings plus measurement evidence, writes a
durable CamillaDSP candidate YAML, and can explicitly load that YAML through
the shared DSP apply transaction. It does not play tones or capture audio.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Sequence

import yaml as yaml_parser

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.camilla_config_contract import (
    PeqFilter,
)
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
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
    DRIVER_DOMAIN_PROGRAM_CHANNELS,
    _branch_context,
    _role_polarity,
    active_emit_devices,
    emit_active_speaker_baseline_config,
    emit_active_speaker_driver_domain_config,
    linearization_headroom_db,
)
from .candidate_bank import CandidateBankRefusal, find_banked_candidate, publish_authored_candidate
from .measurement_emit import MeasurementGraphProfile
from .branch_chain import confirmed_protection_sections
from .crossover_contract import (
    TUNING_OWNERS,
    automatic_candidate_readiness,
    legacy_manual_preservation_state,
    measured_level_match_applied,
)
from .crossover_preview import crossover_design_fingerprint, crossover_preview_fingerprint, load_crossover_preview
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
    REFUSED_STATUSES as BASE_TRIM_REFUSED_STATUSES,
    STATUS_SUPERSEDED as BASE_TRIM_STATUS_SUPERSEDED,
    DriverBaseTrimError,
    banked_base_trims,
    clear_base_trim,
    load_base_trim,
    write_base_trim,
)
from .driver_safety import evaluate_driver_safety_profile
from .level_trim import (
    MAX_ATTENUATION_DB,
    LevelTrimError,
    attenuation_from_group_deltas,
    declared_driver_gains,
)
from .measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_on_declaration, driver_corrections, effective_preset,
)
from .playback_route import (
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .profile import LEVEL_MATCH_AXIS, declared_role_delays, snapshot_declares_single_branch
from . import passive_profile as _passive
from .revalidation import applied_profile_revalidation_satisfies_driver_target_proof
from .startup_hold import release_staged_startup_hold
from .state_paths import baseline_profile_state_path
from .staging import build_passive_mains_preset, compile_preset_from_crossover_preview

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"
DEFAULT_CONFIG_PATH = Path("/var/lib/camilladsp/configs/active_speaker_baseline.yml")
CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH"

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


def compile_commissioning_profile(
    *, topology: OutputTopology | None = None,
    design_draft: Mapping[str, Any] | None = None, write: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Review the applied candidate, or bootstrap from the declared crossover."""
    from .candidate_parts import candidate_from_applied_profile, candidate_from_design_draft  # lazy: candidate parts consumes baseline readers
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
        candidate = (candidate_from_applied_profile(topology, applied) if applied is not None
                     else candidate_from_design_draft(topology, draft))
        preference_filters, trim_db = saved_sound_layers()
        text = compile_tuning_graph(declaration, candidate=candidate,
                                    preference_filters=preference_filters, output_trim_db=trim_db)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        target = baseline_candidate_config_path(text)
        profile.update(prepare_applied_baseline_profile(
            candidate, declaration=declaration, design_draft=draft, measurements=load_measurement_state(topology),
            config_path=target, config_sha256=sha,
        ))
        profile["issues"] = list(candidate.analysis.get("issues") or [])
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


async def apply_commissioning_profile(
    *, expected_candidate_fingerprint: str,
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]],
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    from .design_draft import load_design_draft  # lazy: design draft imports baseline readers
    from .measurement import load_measurement_state  # lazy: measurement imports baseline readers
    from .measurement_emit import load_tuning_declaration  # lazy: graph compilation imports baseline readers

    async with dsp_writer_lock(baseline_config_path().parent, source="active_speaker_baseline_apply"):
        profile: dict[str, Any] = {}
        prepared: dict[str, Any] | None = None
        measurements: Mapping[str, Any] = {}
        try:
            topology = load_output_topology()
            measurements = load_measurement_state(topology)
            draft = load_design_draft(topology=topology)
            for write in (False, True):
                text, profile = compile_commissioning_profile(topology=topology, design_draft=draft, write=write)
                if any(issue.get("severity") == "blocker" for issue in profile["issues"]):
                    break
                refusal = reviewed_candidate_refusal(profile, expected_candidate_fingerprint)
                if refusal:
                    profile = refusal["profile"]
                    break
            else:
                declaration = load_tuning_declaration(topology, design_draft=draft)
                candidate = find_banked_candidate(profile["source"]["measured_candidate_fingerprint"]).candidate
                prepared = prepare_applied_baseline_profile(
                    candidate, declaration=declaration, design_draft=draft, measurements=measurements, provenance=profile,
                    config_path=profile["config"]["path"], config_sha256=profile["config"]["sha256"],
                )
        except (CandidateBankRefusal, ValueError) as exc:
            _commissioning_refusal(profile, exc)
        if prepared is None:
            await _record_apply_outcome_into_bundle(measurements, candidate=profile, apply_state=None, rollback_target=None)
            return {"status": "blocked", "profile": profile, "apply": None, "issues": profile["issues"]}
        if on_candidate_verified is not None:
            await on_candidate_verified()
        _baseline_apply_started(topology, prepared)
        try:
            async with load_composed_graph(text, source="active_speaker_baseline_apply", profile=prepared,
                    load_config=load_config, get_current_config_path=get_current_config_path) as (state, applied):
                return await _baseline_apply_result(topology, applied, measurements, apply_state=state)
        except DspApplyError as exc:
            return await _baseline_apply_result(topology, prepared, measurements, apply_state=exc.state, error=exc)


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
    preview_source = crossover_preview.get("source") or {}
    draft_updated_at = design_draft.get("updated_at")
    if preview_source.get("design_draft_fingerprint") == crossover_design_fingerprint(design_draft):
        # A hardware-only save does not change the design that the preview proved.
        draft_updated_at = preview_source.get("design_draft_updated_at", draft_updated_at)
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        "design_draft_updated_at": draft_updated_at,
        "crossover_preview_updated_at": crossover_preview.get("updated_at"),
        # Bind the exact normalized candidate that protected staging consumes,
        # not merely the design draft it came from.
        "crossover_preview_fingerprint": crossover_preview_fingerprint(
            crossover_preview
        ),
        "measurements_updated_at": measurements.get("updated_at"),
        "measurement_summary_fingerprint": _fingerprint(measurement_summary),
    }
    if measured_candidate_fingerprint is not None:
        source["measured_candidate_fingerprint"] = measured_candidate_fingerprint
    if driver_protection is not None:
        source["driver_protection_fingerprint"] = _fingerprint(driver_protection)
    if candidate_graph_context is not None:
        # Excludes ``measured_candidate_fingerprint``: that field already
        # rides its own top-level key above, exempted from staleness by
        # ``_REBUILD_BLIND_SOURCE_KEYS`` because a write-free rebuild is
        # handed no measured candidate and must not read as superseded for
        # knowing less. Folding it into this composite would defeat that
        # exemption the moment the composite (not the bare field) is what
        # ``_changed_source_keys`` compares.
        device_context = {
            key: value for key, value in candidate_graph_context.items()
            if key != "measured_candidate_fingerprint"
        }
        source["candidate_graph_context_fingerprint"] = _fingerprint(device_context)
    return {**source, "fingerprint": _fingerprint(source)}


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
    return _measured_level_trims(preset, measurements, crossover_preview)


def _measured_level_trims(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    crossover_preview: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Per-role attenuation-only trim from the MEASURED overlap-band level deltas.

    For each adjacent-driver crossover, both drivers' near-field captures through
    the production graph give their level at the shared Fc; the driver-to-driver
    delta is the relative sensitivity at the handoff (the −6 dB Linkwitz-Riley
    shoulder cancels). We chain those deltas into a per-driver
    attenuation (quietest driver = reference, 0 dB), so the acoustic sum is level
    across every crossover — the MEASURED refinement of the datasheet sensitivity
    trim ``_derive_corrections`` otherwise seeds.

    TWO evidence sources answer that one question, and this function is the
    single owner of which one wins. The PREFERRED source is the base trim
    :mod:`jasper.active_speaker.driver_base_trim` banks — the level match a
    successful apply was actually playing, written by the apply seam itself.
    The FALLBACK, unchanged, is the guided per-driver captures the capture flow
    promotes. A banked trim is preferred only because it is the level match the
    speaker last committed to, not because it is a better measurement — so a
    usable guided answer whose captures postdate the record's ``measured_at``
    supersedes it (ruling S20: the newest measurement wins), and the handoff is
    disclosed as ``dsp.baseline_base_trim_superseded`` naming both evidences.
    ``crossover_preview`` is what a banked trim is keyed to, so a caller with
    none has nothing to match a banked record against and gets the guided
    captures alone.

    Returns ``(trims_by_role, meta)``. ``trims_by_role`` is empty (fail-closed)
    unless at least one speaker group has a usable overlap level for BOTH drivers
    of EVERY crossover — any silent / clipped / low-SNR / missing capture drops
    that group, and if no group qualifies the caller keeps the datasheet trim and
    marks the config provisional. Magnitude only: never a phase/delay decision.
    """
    from .capture_geometry import (
        DRIVER_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    roles = required_driver_roles(preset.way_count)
    declaration_fingerprint = (
        crossover_preview_fingerprint(crossover_preview)
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
            # The banked record names the speaker groups it levelled, and the
            # ledger reports them under the SAME keys the guided path uses:
            # readiness (``crossover_contract.automatic_candidate_readiness``)
            # and the setup status both gate on those, so a measured speaker
            # reporting zero measured groups would read as un-measured.
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


def _derive_corrections(
    preset: ActiveSpeakerPreset,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    tuning_owner: str = "manual",
    expected_profile_context_id: str | None = None,
    applied_profile_context: Mapping[str, Any] | None = None,
) -> tuple[dict[str, dict[str, float | bool]], list[dict[str, str]], dict[str, Any]]:
    if tuning_owner not in TUNING_OWNERS:
        raise ValueError(f"unsupported crossover tuning owner: {tuning_owner!r}")
    issues: list[dict[str, str]] = []
    corrections: dict[str, dict[str, float | bool]] = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in required_driver_roles(preset.way_count)
    }
    delay_provenance: dict[str, str] = {}
    inverted_provenance: dict[str, str] = {}

    # --- Persisted working-crossover values (Slice 0), manual/preview tier --
    # Region polarity/delay are REGION-level, hence symmetric across every
    # group a preset applies to (a stereo pair's L/R groups share one preset),
    # so they populate every role unconditionally, before any measured
    # evidence below. ``_role_polarity`` is camilla_yaml's own per-role
    # reduction PLUS its cross-region consistency guard (raises if a role is
    # inverted in one region but not another) — reused here so the derive
    # path and the emit-time guard can never drift on what "this role is
    # inverted" means. Only an explicit "inverted" region makes a provenance
    # claim; "non-inverted" is indistinguishable from the schema default and
    # stays unclaimed, mirroring gain's "none" -> no entry below.
    # NOTE: ``_role_polarity`` raises ``ActiveSpeakerConfigError`` on
    # cross-region-inconsistent polarity. Exception-safety here relies on
    # ``preset`` having already passed ``ActiveSpeakerPreset.validate()`` — both
    # current callers obtain it from ``compile_preset_from_crossover_preview``,
    # which rejects that shape and returns ``preset=None`` before this runs. A
    # future caller passing an unvalidated preset would crash rather than get a
    # bounded issue.
    for role, inverted in _role_polarity(preset).items():
        if inverted and role in corrections:
            corrections[role]["inverted"] = True
            inverted_provenance[role] = PROVENANCE_MANUAL
    for role, delay_ms in declared_role_delays(preset).items():
        if role in corrections:
            corrections[role]["delay_ms"] = delay_ms
            delay_provenance[role] = PROVENANCE_MANUAL

    drivers = crossover_preview.get("drivers")
    gains, gain_provenance, datasheet_trims, gain_issues = declared_driver_gains(
        tuple(corrections), drivers if isinstance(drivers, Mapping) else {},
    )
    issues.extend(gain_issues)
    pinned_gain_roles = {role for role, source in gain_provenance.items() if source == "operator_pinned"}
    estimated_gains = {role: gains[role] for role in gain_provenance if role not in pinned_gain_roles}
    for role in pinned_gain_roles:
        corrections[role]["gain_db"] = gains[role]

    # MEASURED refinement overrides research, UI-suggested, and sensitivity
    # estimates. Manual tuning keeps an operator pin authoritative. Automatic
    # tuning is an explicit replacement operation, so its measured result wins
    # over the old manual pins (measured > pin > estimate > sensitivity).
    measured_trims, level_match = _measured_level_trims(
        preset, measurements, crossover_preview
    )
    base_trim_meta = level_match.get("base_trim")
    if (
        isinstance(base_trim_meta, Mapping)
        and base_trim_meta.get("status") in BASE_TRIM_REFUSED_STATUSES
    ):
        # Refused, and said so. A banked trim that is silently dropped is
        # indistinguishable from a speaker that was never measured, and the
        # operator would have no way to tell which one they are looking at.
        # The three refused statuses share one issue code because they share
        # one remedy — measure this speaker again — and splitting them would
        # give a household three sentences for one action.
        issues.append(_issue(
            "warning",
            "driver_base_trim_not_applied",
            (
                "the banked measured base trim does not describe this speaker "
                f"({base_trim_meta.get('status')}); JTS kept the safe existing "
                "or estimated trim — "
                + str(base_trim_meta.get("remediation") or "")
            ).strip(),
        ))
    if level_match.get("incomparable_groups"):
        issues.append(_issue(
            "warning",
            "driver_measurement_comparison_incomparable",
            (
                "saved driver captures do not share verified placement, "
                "microphone, level, and excitation evidence; JTS kept the safe "
                "existing or estimated trim and needs new guided captures"
            ),
        ))
    if tuning_owner == "manual" and pinned_gain_roles and measured_trims:
        level_match["applied"] = False
        level_match["skipped_reason"] = "operator_pinned_gain"
        measured_trims = {}

    # PR-L4 item 3(a): the two independent level-frame estimates finally meet.
    #
    # `datasheet_trims` (pad-folded driver sensitivity) and `measured_trims`
    # (whichever measured source `_measured_level_trims` accepted) answer the
    # SAME question from completely
    # independent evidence, and until now the precedence ladder below simply
    # dropped whichever lost. On the 2026-07-27 JTS3 run they disagreed by
    # ~12 dB — the datasheet path correct, the measured path carrying the frame
    # defect PR-L3 later located — and no line of code was ever holding both
    # numbers at once to notice. `solve_branch_trims`' own PR-L3 note defers its
    # remaining +0.54 dB estimator-residual question to exactly this check,
    # because two independent frames agreeing is the only way to tell a
    # half-dB estimator artifact from a real acoustic difference.
    #
    # A disagreement beyond tolerance REFUSES the measured trim (falling through
    # to the datasheet rung below) and surfaces BOTH numbers, because a measured
    # value that far from physics is not a refinement of the datasheet — it is
    # evidence that one of the two frames is broken, and the datasheet path is
    # the one with a physical model behind it.
    frame_disagreements: list[str] = []
    for role in sorted(set(measured_trims) & set(datasheet_trims)):
        disagreement_db = abs(measured_trims[role] - datasheet_trims[role])
        if disagreement_db <= MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB:
            continue
        frame_disagreements.append(
            f"{role} measured {measured_trims[role]:.1f} dB vs "
            f"datasheet {datasheet_trims[role]:.1f} dB "
            f"({disagreement_db:.1f} dB apart)"
        )
    if frame_disagreements:
        measured_trims = {
            role: value
            for role, value in measured_trims.items()
            if role not in {note.split(" ", 1)[0] for note in frame_disagreements}
        }
        # Recorded on the level-match ledger, not as an `applied: False` — the
        # refusal is per ROLE, and the loop below recomputes `applied` from the
        # roles whose measured trim survived. A pair where only the tweeter's
        # frame is broken still legitimately applies the woofer's reference 0 dB.
        level_match["frame_disagreements"] = list(frame_disagreements)
        level_match["frame_tolerance_db"] = MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB
        issues.append(_issue(
            "warning",
            "driver_level_frame_disagreement",
            (
                "the measured level match and the driver sensitivity data "
                "disagree by more than "
                f"{MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB:.0f} dB ("
                + "; ".join(frame_disagreements)
                + "); JTS kept the sensitivity-derived trim — re-check the "
                "driver sensitivity and pad values, then measure again"
            ),
        ))
        log_event(
            logger, "baseline_profile.level_frame_disagreement",
            level=logging.WARNING,
            tolerance_db=MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB,
            detail="; ".join(frame_disagreements),
        )

    sources: dict[str, str] = {}
    measured_notes: list[str] = []
    estimate_notes: list[str] = []
    datasheet_notes: list[str] = []
    for role in corrections:
        if tuning_owner == "automatic" and role in measured_trims:
            corrections[role]["gain_db"] = measured_trims[role]
            sources[role] = "measured"
            measured_notes.append(f"{role} {measured_trims[role]:.1f} dB")
        elif role in pinned_gain_roles:
            sources[role] = "operator_pinned"
        elif role in measured_trims:
            corrections[role]["gain_db"] = measured_trims[role]
            sources[role] = "measured"
            measured_notes.append(f"{role} {measured_trims[role]:.1f} dB")
        elif role in estimated_gains:
            corrections[role]["gain_db"] = estimated_gains[role]
            sources[role] = "estimate"
            estimate_notes.append(f"{role} {estimated_gains[role]:.1f} dB")
        elif role in datasheet_trims:
            corrections[role]["gain_db"] = datasheet_trims[role]
            sources[role] = "sensitivity"
            datasheet_notes.append(f"{role} {datasheet_trims[role]:.1f} dB")
        else:
            sources[role] = "none"
    level_match["applied"] = bool(measured_notes)

    if measured_notes:
        issues.append(_issue(
            "info",
            "driver_gain_derived_from_measurement",
            (
                "applied a measured level match ("
                + ", ".join(measured_notes)
                + ")"
            ),
        ))
    if datasheet_notes:
        issues.append(_issue(
            "warning",
            "driver_gain_derived_from_sensitivity",
            (
                "applied an interim level trim from the sensitivity gap ("
                + ", ".join(datasheet_notes)
                + "); confirm against measurement before final tuning"
            ),
        ))
    if estimate_notes:
        issues.append(_issue(
            "warning",
            "driver_gain_from_unmeasured_estimate",
            (
                "applied an interim suggested driver trim ("
                + ", ".join(estimate_notes)
                + "); confirm against measurement before final tuning"
            ),
        ))
    provisional = any(
        source in {"estimate", "sensitivity"} for source in sources.values()
    )
    if provisional:
        issues.append(_issue(
            "warning",
            "baseline_level_match_provisional",
            (
                "per-driver level match is an unmeasured estimate; run the "
                "guided level-match to measure it"
            ),
        ))

    corrections_provenance: dict[str, dict[str, str]] = {}
    for role in corrections:
        entry: dict[str, str] = {}
        gain_provenance_value = _GAIN_SOURCE_TO_PROVENANCE.get(sources.get(role, "none"))
        if gain_provenance_value is not None:
            entry["gain_db"] = gain_provenance_value
        if role in delay_provenance:
            entry["delay_ms"] = delay_provenance[role]
        if role in inverted_provenance:
            entry["inverted"] = inverted_provenance[role]
        if entry:
            corrections_provenance[role] = entry

    meta = {
        "sources": sources,
        "gain_provenance": gain_provenance,
        "provisional": provisional,
        "level_match": level_match,
        "corrections_provenance": corrections_provenance,
    }
    return corrections, issues, meta


def _blocked_payload(
    *,
    topology: OutputTopology,
    source: Mapping[str, Any],
    issues: list[dict[str, str]],
    status: str = "blocked",
    config_path: Path,
    playback_device: str | None,
    playback_device_source: str,
) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": status,
        "created_at": None,
        "updated_at": None,
        "source": dict(source),
        "config": {
            "path": str(config_path),
            "basename": config_path.name,
            "exists": config_path.exists(),
            "playback_device": playback_device,
            "playback_device_source": playback_device_source,
        },
        "verification": {},
        "corrections": {},
        "corrections_source": {},
        "gain_provenance": {},
        "corrections_provenance": {},
        "level_match": {"groups_total": 0, "groups_measured": 0, "applied": False},
        "provisional": False,
        "validation": {"status": "skipped", "reason": status},
        "permissions": {
            "may_compile": False,
            "may_apply": False,
            "may_not_emit_audio": True,
            "loads_camilla_on_apply": True,
        },
        "safety": {
            "no_audio": True,
            "compile_loads_camilla": False,
            "apply_requires_explicit_action": True,
            "volume_limit_db_max": 0.0,
            "positive_gain_allowed": False,
        },
        "issues": issues,
    }


def _apply_handoff_issue(playback_device_source: str) -> dict[str, str] | None:
    if playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE:
        return None
    return _issue(
        "blocker",
        "baseline_output_handoff_not_supported",
        (
            "active profile YAML can be compiled, but applying it is disabled "
            "until outputd owns this DAC handoff"
        ),
    )


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
    """The blend correction an applied profile carries, or ``None`` if unknown.

    Same snapshot-first authority rule :func:`profile_linearization` states —
    the ``recomposition_snapshot`` copy is the one a recompose re-emits, so it
    is the one that describes the graph; the top-level mirror is the fallback
    for a profile written before the snapshot carried it.

    **``None`` and ``()`` are different answers and both are load-bearing.**
    ``()`` means "this profile applied no blend correction" — true of every
    profile written before decision 10, and of every first round. ``None``
    means "there is no readable applied profile", i.e. the incumbent cannot be
    established at all. The round refuses to prescribe on ``None`` rather than
    assuming zero, because assuming zero would double-count the correction the
    measurement was actually taken through — the precise shape #2653 reverted
    for the level datum.

    What this CANNOT detect, stated rather than implied: a graph applied out of
    band, by hand, that the profile no longer describes. The applied profile is
    this speaker's single record of what is running, and every other consumer
    of it (linearization, boosts, alignment) trusts it the same way.

    **This answers WHERE, not WHETHER-VALID, and it deliberately does not
    filter.** Entry-level validation belongs to
    ``crossover_v2.blend_correction.blend_filters_from_mapping``, which is the
    single owner of "is this a record this system wrote". An earlier version
    dropped non-mapping entries here, which silently TRUNCATED a corrupt list
    into a shorter valid-looking one — a caller would then have applied a
    partial correction believing it was whole. Every entry is passed through
    exactly as persisted so the strict reader can refuse the list.
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
    absorption is the excitation-safety property (see
    ``camilla_yaml.MAX_LINEARIZATION_BOOST_DB``'s note: it is what keeps the
    boosted band "at or under unity no matter how deep the correction"), so
    compensating it at the main volume would put the boosted band over the
    driver's excitation cap by the branch's own boost — up to the full charge,
    on a sustained swept sine, below the per-driver limiters' reach. The
    correct consumer is
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
    set IS emitted (``build_baseline_profile_candidate`` passes ``room_peqs``,
    whose boost the graph absorbs) and the household's preference layer is not
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


#: Source keys an apply path can record that no write-free rebuild can
#: reproduce, because only the apply paths are handed a measured candidate.
#: A key here present on the applied record and absent from a rebuild means the
#: rebuild knows less; any OTHER saved-only key is incomparable and supersedes.
#: See ADR-0195.
_REBUILD_BLIND_SOURCE_KEYS = frozenset({"measured_candidate_fingerprint"})

#: Source keys ``_source_payload`` started writing after profiles already on
#: disk were saved. Absent-on-saved means "predates this key", not "changed"
#: — without this, every fleet profile applied before the key existed reads
#: as superseded on its first post-upgrade rebuild. Unlike
#: ``_REBUILD_BLIND_SOURCE_KEYS`` (a permanent apply-vs-rebuild asymmetry),
#: this is a one-way migration exemption: remove the key here once no fleet
#: profile predates ``candidate_graph_context_fingerprint``'s introduction
#: (#2416).
_MIGRATION_EXEMPT_SOURCE_KEYS = frozenset({"candidate_graph_context_fingerprint"})


def _changed_source_keys(
    saved_source: Mapping[str, Any],
    current_source: Mapping[str, Any],
) -> list[str]:
    """Source keys that differ between an applied record and a re-derivation.

    Derived from the payloads rather than a hand-listed subset: a subset stops
    naming whatever ``_source_payload`` grows next, which is how a supersede
    shipped with an empty ``changed``. The composite ``fingerprint`` is not a
    key of its own — it is a hash of the rest.
    """

    keys = (set(current_source) | set(saved_source)) - {"fingerprint"}
    return sorted(
        key
        for key in keys
        if key not in _REBUILD_BLIND_SOURCE_KEYS or key in current_source
        if key not in _MIGRATION_EXEMPT_SOURCE_KEYS
        or (key in saved_source and key in current_source)
        if saved_source.get(key) != current_source.get(key)
    )


def _applied_profile_proves_driver_targets(
    saved: Mapping[str, Any] | None,
    current_source: Mapping[str, Any],
) -> bool:
    """Whether a standing applied profile carries its own driver-target proof.

    Compared-and-clean only, and measured only: a record with no source
    fingerprint was never compared against anything, and a ``provisional`` one
    was applied from sensitivity estimates rather than from measurement, so
    neither may stand in for driver evidence. See ADR-0195.
    """

    applied = _applied_profile_anchor(saved)
    if applied is None or bool(applied.get("provisional")):
        return False
    applied_source = (
        applied.get("source") if isinstance(applied.get("source"), Mapping) else {}
    )
    if not applied_source.get("fingerprint"):
        return False
    return not _changed_source_keys(applied_source, current_source)


def _revalidation_payload(
    saved: Mapping[str, Any] | None,
    current_source: Mapping[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Describe whether a previously applied profile is stale.

    ``build_baseline_profile_candidate`` deliberately re-derives readiness from
    current evidence instead of trusting the saved JSON. When that re-derivation
    invalidates a profile that had already been applied, keep that fact visible:
    the household needs a "revalidate" path, not a mysterious blocked profile.

    Staleness is decided by :func:`_changed_source_keys`, never by the
    composite fingerprint. See ADR-0195.
    """

    saved = _applied_profile_anchor(saved)
    if saved is None:
        return {"required": False, "status": "not_required"}
    saved_source = (
        saved.get("source") if isinstance(saved.get("source"), Mapping) else {}
    )
    saved_fingerprint = saved_source.get("fingerprint")
    current_fingerprint = current_source.get("fingerprint")
    changed = _changed_source_keys(saved_source, current_source)
    if not saved_fingerprint or not changed:
        return {"required": False, "status": "not_required"}

    if status in {"ready_to_compile", "ready_to_apply", "compiled_apply_blocked"}:
        next_step = "save_profile" if status == "ready_to_compile" else "apply_profile"
        message = (
            "active speaker revalidation is saved; save and apply a fresh profile"
        )
    else:
        next_step = "setup_checks"
        message = (
            "active speaker setup changed after this profile was applied; "
            "finish the highlighted setup checks, then save and apply a fresh profile"
        )

    saved_config = (
        saved.get("config") if isinstance(saved.get("config"), Mapping) else {}
    )
    saved_config_path = str(saved_config.get("path") or "")
    return {
        "required": True,
        "status": "required",
        "reason": "applied_profile_superseded",
        "next_step": next_step,
        "message": message,
        "changed": changed,
        "applied_at": saved.get("applied_at"),
        "applied_source_fingerprint": saved_fingerprint,
        "current_source_fingerprint": current_fingerprint,
        "superseded_profile": {
            "status": saved.get("status"),
            "updated_at": saved.get("updated_at"),
            "applied_at": saved.get("applied_at"),
            "config": {
                "path": saved_config_path or None,
                "basename": saved_config.get("basename"),
                "exists": bool(saved_config_path) and Path(saved_config_path).exists(),
            },
        },
    }


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


def _summed_validation_evidence_complete(summary: Mapping[str, Any]) -> bool:
    if summary.get("summed_validation_complete"):
        return True
    required = int(summary.get("required_summed_group_count") or 0)
    validated = int(summary.get("validated_summed_group_count") or 0)
    missing = summary.get("missing_summed_targets")
    return (
        required > 0
        and validated >= required
        and isinstance(missing, list)
        and not missing
    )


def _crossover_preview_ready(crossover_preview: Mapping[str, Any]) -> bool:
    """True when the saved crossover preview is a fresh, staging-ready artifact.

    The preview-readiness gate for :func:`build_baseline_profile_candidate`.
    Production EQ recompose reads the applied snapshot instead.
    """
    return (
        crossover_preview.get("kind") == "jts_active_speaker_crossover_preview"
        and crossover_preview.get("status") == "ready_for_protected_staging"
        and bool(
            (crossover_preview.get("permissions") or {}).get(
                "may_prepare_protected_startup_config"
            )
        )
    )


def build_baseline_profile_candidate(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    write: bool = False,
    compile_config: bool = False,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    playback_device: str | None = None,
    capture_device: str | None = None,
    capture_format: str | None = None,
    driver_domain: bool = False,
    program_channel: str | None = None,
    driver_domain_pair_trim_db: float = 0.0,
    tuning_owner: str = "manual",
    preserved_applied_profile: Mapping[str, Any] | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Build or write a baseline candidate from current accepted evidence.

    ``capture_device`` is the CamillaDSP capture source the emitted baseline
    reads from. ``None`` (the default) takes it — with the rest of the
    ``devices:`` block — from :func:`active_emit_devices` against the RESOLVED
    playback device, so both halves of the emit follow one derivation instead of
    a marker-aware sink meeting a hardcoded tap: on an armed box that pairing
    emits playback=ring with capture=the snd-aloop tap fan-in has stopped
    feeding, the half-moved graph. Every non-ring device answers the emitter's
    own defaults, so a candidate on a box that is not armed is byte-identical.
    An explicit value still wins for the one caller that owns its own capture
    lane: the multiroom reconciler passes the grouping ring for a wireless
    follower. The graph shape — crossover, per-driver limiters, tweeter
    high-pass, 0 dB ceiling — is unaffected; only the capture source line
    changes.

    ``driver_domain`` switches the emit to the **driver-domain-only** graph
    (``emit_active_speaker_driver_domain_config``, Slice 2): a wireless active
    follower's Layer A — ``channel_select (pick L/R/mono) -> split -> per-driver
    crossover/limiter`` — with **no** program-domain headroom and **no**
    preference EQ (the leader baked Layer B/C into the streamed program). It
    requires ``program_channel`` (one of ``DRIVER_DOMAIN_PROGRAM_CHANNELS``: the
    inter-speaker channel this box plays). ``driver_domain_pair_trim_db`` is the
    attenuate-only pair-balance trim for this member, applied after
    channel-select and before the driver split; default zero keeps the full solo
    baseline emit byte-identical (invariant 7). The reconciler's active-member
    branches pass ``driver_domain=True`` + ``program_channel`` + the loopback
    ``capture_device``, writing to role-specific ``config_path`` / ``state_path``
    so the solo baseline artifacts are never clobbered.
    """
    if tuning_owner not in TUNING_OWNERS:
        raise ValueError(f"unsupported crossover tuning owner: {tuning_owner!r}")
    if driver_domain and program_channel not in DRIVER_DOMAIN_PROGRAM_CHANNELS:
        raise ValueError(
            "driver_domain requires program_channel in "
            f"{DRIVER_DOMAIN_PROGRAM_CHANNELS}, not {program_channel!r}"
        )

    state_target = baseline_profile_state_path(state_path)
    config_target = baseline_config_path(config_path)
    now = created_at or _utc_now()
    saved = _load_saved_state(state_target)
    applied_anchor = _applied_profile_anchor(saved)
    protection_anchor = preserved_applied_profile or applied_anchor or {}
    protection = (protection_anchor.get("recomposition_snapshot") or {}).get("driver_protection")
    safety_profile = design_draft.get("driver_safety_profile")
    if evaluate_driver_safety_profile(safety_profile, topology).confirmed_and_current:
        protection = _protection_projection(safety_profile)
    if driver_domain:
        protection = None
    resolved_playback_device, playback_device_source = (
        resolve_active_playback_device(
            topology,
            playback_device=playback_device,
        )
    )
    route_capability = active_playback_route_capability(
        topology,
        playback_device=playback_device,
    )
    try:
        devices = active_emit_devices(resolved_playback_device, topology=topology)
    except ValueError as exc:
        raise ActiveSpeakerConfigError(str(exc)) from exc
    emit_capture_device = (
        devices.capture_device if capture_device is None else capture_device
    )
    emit_capture_format = (
        devices.capture_format if capture_format is None else capture_format
    )
    candidate_graph_context = {
        "playback_device": resolved_playback_device,
        "domain": "driver" if driver_domain else "full",
        "program_channel": program_channel if driver_domain else None,
        "driver_domain_pair_trim_db": (
            driver_domain_pair_trim_db if driver_domain else 0.0
        ),
        "capture_device": emit_capture_device,
        "capture_format": emit_capture_format,
        **({"driver_protection": protection} if protection is not None else {}),
    }
    source = _source_payload(
        topology,
        design_draft,
        crossover_preview,
        measurements,
        driver_protection=protection,
        candidate_graph_context=candidate_graph_context,
    )
    saved_snapshot = (
        saved.get("recomposition_snapshot")
        if isinstance(saved, Mapping)
        and isinstance(saved.get("recomposition_snapshot"), Mapping)
        else {}
    )
    applied_profile_context_id = ""
    if isinstance(applied_anchor, Mapping):
        applied_snapshot = applied_anchor.get("recomposition_snapshot")
        if isinstance(applied_snapshot, Mapping):
            # Never trust the persisted derived field for evidence admission.
            # Re-derive the context from the applied immutable graph inputs.
            applied_profile_context_id = baseline_candidate_fingerprint(applied_anchor)
    retained_applied = _frozen_applied_profile(applied_anchor)
    applied_profile_proves = _applied_profile_proves_driver_targets(saved, source)

    def finalize(payload: dict[str, Any]) -> dict[str, Any]:
        if retained_applied is not None:
            payload["applied_recomposition_profile"] = retained_applied
        payload["revalidation"] = _revalidation_payload(
            saved,
            source,
            status=str(payload.get("status") or ""),
        )
        # THE applied verdict, derived once where both the record and the
        # comparison are in hand. Consumers read this rather than the rebuild's
        # own status, which cannot reach "applied" for a measured profile.
        # Whether the speaker still PLAYS it needs the statefile, so that half
        # stays with the loader. See ADR-0195.
        payload["applied_profile_stands"] = bool(
            retained_applied is not None
            and payload["revalidation"].get("required") is not True
        )
        # Attached even on a blocked payload, whose `verification` is empty:
        # the /sound/ combined-test door and the validation writer read this
        # answer through the commissioning view, and a speaker whose driver
        # proof rides on its applied profile is exactly the one that blocks.
        payload["driver_target_proof_from_applied_profile"] = applied_profile_proves
        return payload

    if (
        not write
        and saved
        and isinstance(saved.get("source"), Mapping)
        and saved["source"].get("fingerprint") == source["fingerprint"]
        and str(saved.get("tuning_owner") or "manual") == tuning_owner
        and all(
            saved_snapshot.get(key) == value
            for key, value in candidate_graph_context.items()
        )
        and Path(str((saved.get("config") or {}).get("path") or "")).exists()
    ):
        out = dict(saved)
        out["candidate_fingerprint"] = baseline_candidate_fingerprint(out)
        out["config"] = dict(out.get("config") or {})
        out["config"]["exists"] = True
        issues = [
            issue for issue in out.get("issues", [])
            if isinstance(issue, dict)
            and issue.get("code") != "baseline_output_handoff_not_supported"
        ]
        handoff_issue = _apply_handoff_issue(
            str(out["config"].get("playback_device_source") or playback_device_source)
        )
        if handoff_issue:
            issues.append(handoff_issue)
            if out.get("status") == "ready_to_apply":
                out["status"] = "compiled_apply_blocked"
        out["issues"] = issues
        out["permissions"] = dict(out.get("permissions") or {})
        out["permissions"]["may_apply"] = out.get("status") == "ready_to_apply"
        out["permissions"]["may_compile"] = out.get("status") in {
            "ready_to_compile",
            "ready_to_apply",
            "compiled_apply_blocked",
        }
        return finalize(out)

    issues: list[dict[str, str]] = []
    summary = measurements.get("summary") if isinstance(measurements.get("summary"), Mapping) else {}
    driver_target_proof_complete = bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    driver_target_proof_source = (
        "measurements" if driver_target_proof_complete else "missing"
    )
    summed_validation_complete = bool(
        summary.get("summed_validation_complete")
    )
    summed_validation_source = (
        "measurements" if summed_validation_complete else "missing"
    )
    # Passive mains ride the SAME multi-output emitter as the active path, via a
    # degenerate 1-way preset built from the topology — the preset
    # ``commission_wiring`` answers a passive capture with, so the compiled
    # graph and the measured plant are ONE.
    passive_mains = _passive.passive_mains_compiles_roleful(
        topology, None, applied_anchor
    )

    preset: ActiveSpeakerPreset | None = None
    preset_gates: list[dict[str, Any]] = []
    if passive_mains:
        if not resolved_playback_device:
            issues.append(_issue(
                "blocker",
                "baseline_playback_device_missing",
                "active profile compiler needs an explicit active playback device",
            ))
        for issue in route_capability.issues:
            if issue.get("code") == "active_playback_route_too_narrow":
                issues.append(issue)
        preset, preset_issues, preset_gates = build_passive_mains_preset(topology)
        issues.extend(preset_issues)
    else:
        preview_ready = _crossover_preview_ready(crossover_preview)
        if not preview_ready:
            issues.append(_issue(
                "blocker",
                "baseline_crossover_preview_not_ready",
                "save a fresh crossover preview before compiling an active profile",
            ))
        if not resolved_playback_device:
            issues.append(_issue(
                "blocker",
                "baseline_playback_device_missing",
                "active profile compiler needs an explicit active playback device",
            ))
        for issue in route_capability.issues:
            if issue.get("code") == "active_playback_route_too_narrow":
                issues.append(issue)
        # A routed local subwoofer compiles through the SAME multi-output emitter as
        # the mains: the preset builder (compile_preset_from_crossover_preview below)
        # resolves the sub lane onto the preset fail-closed — when it cannot pin the
        # sub to a safe contiguous output it returns a blocker instead of emitting a
        # full-range sub feed. The emitted graph is then re-proven structurally by
        # classify_camilla_graph + CamillaDSP --check, so there is no separate
        # subwoofer-not-supported gate here.
        if preview_ready:
            preset, preset_issues, preset_gates = compile_preset_from_crossover_preview(
                topology,
                dict(crossover_preview),
            )
            issues.extend(preset_issues)
        if not driver_target_proof_complete:
            probe_status = "ready_to_compile" if not issues else "blocked"
            revalidation_for_driver_proof = _revalidation_payload(
                saved,
                source,
                status=probe_status,
            )
            if applied_profile_revalidation_satisfies_driver_target_proof(
                revalidation_for_driver_proof
            ):
                driver_target_proof_complete = True
                driver_target_proof_source = "applied_profile_revalidation"
            elif applied_profile_proves:
                # The revalidation relaxer above answers only for a profile
                # something HAS superseded. One nothing has superseded proves
                # the same targets with nothing outstanding against it, and
                # since ADR-0195 stopped a measured apply reading as superseded
                # this is the branch that state lands in.
                driver_target_proof_complete = True
                driver_target_proof_source = "applied_profile"
        if not driver_target_proof_complete:
            issues.append(_issue(
                "blocker",
                "baseline_driver_measurements_missing",
                "confirm each driver with a quiet test before saving the active profile",
            ))
        summed_validation_complete = (
            bool(summary.get("summed_validation_complete"))
            or (
                driver_target_proof_complete
                and _summed_validation_evidence_complete(summary)
            )
        )
        summed_validation_source = (
            "measurements" if summed_validation_complete else "missing"
        )
    if issues:
        return finalize(_blocked_payload(
            topology=topology,
            source=source,
            issues=issues,
            status="blocked",
            config_path=config_target,
            playback_device=resolved_playback_device,
            playback_device_source=playback_device_source,
        ))
    if preset is None or resolved_playback_device is None:
        return finalize(_blocked_payload(
            topology=topology,
            source=source,
            issues=[
                _issue(
                    "blocker",
                    "baseline_preset_unavailable",
                    "active profile compiler could not build speaker preset intent",
                )
            ],
            status="blocked",
            config_path=config_target,
            playback_device=resolved_playback_device,
            playback_device_source=playback_device_source,
        ))

    preservation = None
    if preserved_applied_profile is not None:
        preservation = legacy_manual_preservation_state(
            preserved_applied_profile,
            current_source_fingerprint=str(source.get("fingerprint") or ""),
        )
        if not preservation["ready"]:
            return finalize(_blocked_payload(
                topology=topology,
                source=source,
                issues=[_issue(
                    "blocker",
                    str(preservation["reason"]),
                    str(preservation["detail"]),
                )],
                status="blocked",
                config_path=config_target,
                playback_device=resolved_playback_device,
                playback_device_source=playback_device_source,
            ))
        if not isinstance(preserved_applied_profile.get("corrections"), Mapping):
            return finalize(_blocked_payload(
                topology=topology,
                source=source,
                issues=[_issue(
                    "blocker",
                    "preserved_manual_corrections_missing",
                    "the applied manual crossover has no corrections to preserve",
                )],
                status="blocked",
                config_path=config_target,
                playback_device=resolved_playback_device,
                playback_device_source=playback_device_source,
            ))

    from .crossover_contract import preset_matches_applied_profile

    expected_profile_context_id = (
        applied_profile_context_id
        if preset_matches_applied_profile(preset, applied_anchor)
        else ""
    )
    corrections, correction_issues, correction_meta = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
        tuning_owner=tuning_owner,
        expected_profile_context_id=expected_profile_context_id or None,
        applied_profile_context=applied_anchor,
    )
    issues.extend(correction_issues)
    linearization: dict[str, Any] = {}
    linearization_outcome = ""
    trim_decision: dict[str, Any] = {}
    blend_correction: list[dict[str, Any]] = []
    bass_extension: dict[str, Any] = {}
    room_correction: dict[str, Any] = {}
    room_peqs: Sequence[PeqFilter] = ()
    if preserved_applied_profile is not None:
        preserved_corrections = (
            preserved_applied_profile.get("corrections")
            if isinstance(preserved_applied_profile.get("corrections"), Mapping)
            else None
        )
    else:
        preserved_corrections = None
    if preserved_corrections is not None:
        normalized: dict[str, dict[str, float | bool]] = {}
        for role in required_driver_roles(preset.way_count):
            raw = preserved_corrections.get(role)
            gain = _finite_float(raw.get("gain_db")) if isinstance(raw, Mapping) else None
            delay = _finite_float(raw.get("delay_ms")) if isinstance(raw, Mapping) else None
            inverted = raw.get("inverted") if isinstance(raw, Mapping) else None
            if (
                gain is None
                or gain > 0.0
                or gain < MAX_ATTENUATION_DB
                or delay is None
                or not 0.0 <= delay <= 20.0
                or not isinstance(inverted, bool)
            ):
                issues.append(_issue(
                    "blocker",
                    "preserved_manual_correction_invalid",
                    f"the applied manual correction for {role} is incomplete or unsafe",
                ))
                continue
            normalized[role] = {
                "gain_db": gain,
                "delay_ms": delay,
                "inverted": inverted,
            }
        if len(normalized) == len(required_driver_roles(preset.way_count)):
            corrections = normalized
            correction_meta["sources"] = {
                role: "operator_pinned" for role in normalized
            }
            correction_meta["gain_provenance"] = {
                role: "operator_pinned" for role in normalized
            }
            # Wholesale carry-forward of the applied manual profile: every
            # sub-parameter of every role came from that preserved snapshot,
            # not from this derivation, so all three are stamped "preserved"
            # (distinct from the legacy "operator_pinned" sources/gain_provenance
            # stamping above, kept byte-compatible).
            correction_meta["corrections_provenance"] = {
                role: {
                    "gain_db": PROVENANCE_PRESERVED,
                    "delay_ms": PROVENANCE_PRESERVED,
                    "inverted": PROVENANCE_PRESERVED,
                }
                for role in normalized
            }
            correction_meta["provisional"] = False
            correction_meta["level_match"] = {
                "groups_total": 0,
                "groups_measured": 0,
                "deltas": [],
                "comparison": "preserved_applied_manual_profile",
                "incomparable_groups": [],
                "applied": False,
            }
            issues.append(_issue(
                "info",
                "manual_crossover_preserved",
                "preserved the currently applied manual crossover corrections",
            ))
    automatic_candidate = automatic_candidate_readiness(
        required_group_ids=sorted(group.id for group in topology.speaker_groups
                                  if group.mode in {"active_2_way", "active_3_way"}),
        level_match=correction_meta["level_match"], measurement_summary=summary,
        active_comparison_set=measurements.get("active_comparison_set"),
    )
    provisional = bool(correction_meta.get("provisional"))
    if driver_domain and not bass_extension:
        bass_extension = applied_bass_extension()
    validation = {"status": "skipped", "reason": "not_written"}
    if write or compile_config:
        if write:
            config_target.parent.mkdir(parents=True, exist_ok=True)
        if driver_domain:
            # v2 measured candidates (measured_crossover_candidate) are not
            # routed through the driver_domain (wireless-follower) emit today
            # — only the multiroom reconciler passes driver_domain=True, and
            # it never supplies a measured_candidate. If W5+ ever applies a
            # measured delay/polarity candidate to a follower, the alignment
            # proof below (the else-branch prove_candidate_config call) must
            # be added to this branch too, against the follower's channel
            # map.
            assert program_channel is not None  # validated above
            yaml = emit_active_speaker_driver_domain_config(
                preset,
                playback_device=resolved_playback_device,
                program_channel=program_channel,
                pair_trim_db=driver_domain_pair_trim_db,
                corrections=corrections,
                capture_device=emit_capture_device,
                capture_format=emit_capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize,
                target_level=devices.target_level,
                queuelimit=devices.queuelimit,
                enable_rate_adjust=devices.enable_rate_adjust,
                bass_extension=bass_extension,
            )
        else:
            yaml = emit_active_speaker_baseline_config(
                preset,
                playback_device=resolved_playback_device,
                corrections=corrections,
                capture_device=emit_capture_device,
                capture_format=emit_capture_format,
                playback_format=devices.playback_format,
                chunksize=devices.chunksize,
                target_level=devices.target_level,
                queuelimit=devices.queuelimit,
                enable_rate_adjust=devices.enable_rate_adjust,
                bass_extension=bass_extension,
                linearization=linearization,
                blend_correction=blend_correction,
                room_peqs=room_peqs,
                protection_sections_by_role=(confirmed_protection_sections(protection) if protection is not None else None),
            )
        if not driver_domain:
            config_target = baseline_candidate_config_path(yaml, config_target)
        if write:
            atomic_write_text(config_target, yaml, mode=CONFIG_FILE_MODE)
            validation = validate(config_target).to_dict()
            if not validation.get("ok_to_apply") and validation.get("status") not in {
                "valid",
                "missing",
            }:
                issues.append(_issue(
                    "blocker",
                    "baseline_config_validation_failed",
                    "generated active profile did not pass CamillaDSP validation",
                ))
        config_sha256 = hashlib.sha256(yaml.encode("utf-8")).hexdigest()
        if write:
            status = "ready_to_apply" if not any(
                issue["severity"] == "blocker" for issue in issues
            ) else "blocked"
            handoff_issue = _apply_handoff_issue(playback_device_source)
            if status == "ready_to_apply" and handoff_issue:
                issues.append(handoff_issue)
                status = "compiled_apply_blocked"
        else:
            status = "ready_to_compile"
    else:
        config_sha256 = None
        status = "ready_to_compile"

    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": status,
        "created_at": (
            saved.get("created_at") if saved and saved.get("created_at") else now
        ),
        "updated_at": now if write else None,
        "source": source,
        "preset": {
            "preset_id": preset.preset_id,
            "name": preset.name,
            "way_count": preset.way_count,
            "channel_map": preset.channel_map.to_dict(),
            "gates": preset_gates,
        },
        "config": {
            "path": str(config_target),
            "basename": config_target.name,
            "exists": config_target.exists(),
            "sha256": config_sha256,
            "playback_device": resolved_playback_device,
            "playback_device_source": playback_device_source,
            # "driver" = a wireless follower's Layer-A-only graph (no B/C);
            # "full" = the solo baseline (B/C + A). Observability only.
            "domain": "driver" if driver_domain else "full",
            "program_channel": program_channel if driver_domain else None,
        },
        "verification": {
            "driver_measurements_complete": bool(
                summary.get("driver_measurements_complete")
            ),
            "driver_target_proof_complete": driver_target_proof_complete,
            "driver_target_proof_source": driver_target_proof_source,
            "summed_validation_complete": summed_validation_complete,
            # PR-L4 item 5: which lane satisfied the summed flag, mirroring the
            # driver flag's own source field. Without it a reader met
            # `summed_validation_complete: true` beside
            # `validated_summed_group_count: 0` and had no way to tell a
            # candidate-backed claim from a vacuous one.
            "summed_validation_source": summed_validation_source,
            "captured_driver_count": summary.get("captured_driver_count", 0),
            "validated_summed_group_count": summary.get(
                "validated_summed_group_count",
                0,
            ),
            # ...and the counts BEHIND a measured-candidate claim, so every
            # `complete: true` in this block has a number under it somewhere.
        },
        "corrections": corrections,
        "corrections_source": correction_meta["sources"],
        "gain_provenance": correction_meta["gain_provenance"],
        "corrections_provenance": correction_meta["corrections_provenance"],
        "level_match": correction_meta["level_match"],
        "linearization": linearization,
        # Gauge fix (2026-07-24): WHY linearization did or didn't run for
        # THIS candidate — "" / "fitted" / "trim_rejected" /
        # "ineligible_mic_tier" / "ineligible_repeats" / "fit_failed". Top
        # level only (not inside recomposition_snapshot): unlike
        # "linearization" above, this is not an input a later recompose
        # needs to re-emit the graph, only descriptive provenance about how
        # the currently-applied filters (or their absence) came to be.
        # setup_status.read_active_speaker_setup_status surfaces this on
        # /state's protected_profile (the applied artifact), and the v2
        # wizard surfaces the in-session equivalent straight off the live
        # candidate.
        "linearization_outcome": linearization_outcome,
        # The trim DECISION behind "corrections", not its values. Top level
        # for the same reason "linearization_outcome" is, and NOT inside
        # recomposition_snapshot, which baseline_candidate_fingerprint hashes.
        "trim_decision": trim_decision,
        "blend_correction": blend_correction,
        # Convenience mirror of the accepted room layer. Recomposition reads
        # the immutable snapshot below; the mirror supports profiles saved
        # before the snapshot carried this field.
        "room_correction": room_correction,
        "automatic_candidate": automatic_candidate,
        "tuning_owner": tuning_owner,
        # An unmeasured per-driver trim is explicitly provisional. Surfaced in
        # /state + the wizard so a household knows to run the guided level-match;
        # the speaker is safe (attenuation-only) either way.
        "provisional": provisional,
        "validation": validation,
        "permissions": {
            "may_compile": status in {
                "ready_to_compile",
                "ready_to_apply",
                "compiled_apply_blocked",
            },
            "may_apply": status == "ready_to_apply",
            "may_not_emit_audio": True,
            "loads_camilla_on_apply": True,
        },
        "safety": {
            "no_audio": True,
            "compile_loads_camilla": False,
            "apply_requires_explicit_action": True,
            "volume_limit_db_max": 0.0,
            "positive_gain_allowed": False,
            "per_driver_limiters": True,
        },
        "issues": issues,
        "recomposition_snapshot": {
            "schema_version": 1,
            "topology_id": topology.topology_id,
            "topology_fingerprint": source["topology_fingerprint"],
            "preset": preset.to_dict(),
            "corrections": corrections,
            "corrections_source": correction_meta["sources"],
            "gain_provenance": correction_meta["gain_provenance"],
            "corrections_provenance": correction_meta["corrections_provenance"],
            "level_match": correction_meta["level_match"],
            "tuning_owner": tuning_owner,
            "linearization": linearization,
            # Decision 10's blend correction: an INPUT every future recompose
            # (room/preference EQ, /sound) must re-emit verbatim, so it belongs
            # in the immutable snapshot on the same test "linearization" passes
            # — is this something a later recompose needs to rebuild the graph?
            # It is: dropping it here would silently revert the blend
            # correction on the next preference-EQ save.
            "blend_correction": blend_correction,
            **({"room_correction": room_correction} if room_correction else {}),
            **({"bass_extension": bass_extension} if bass_extension else {}),
            **candidate_graph_context,
        },
    }
    payload = finalize(payload)
    if compile_config and not write:
        payload["_compiled_graph_text"] = yaml
    payload["candidate_fingerprint"] = baseline_candidate_fingerprint(payload)
    if write:
        atomic_write_text(
            state_target,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
        )
    return payload


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
      real information. This is the arm that was missing: such a candidate
      reads ``automatic`` to
      :func:`~jasper.active_speaker.crossover_contract._snapshot_owner`, the
      predicate this seam claims to mirror, and used to CLEAR here.
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

    # A follower's Layer-A-only graph, excluded for the reason
    # `build_baseline_profile_candidate` already documents at its own
    # `if not driver_domain:` guard: that flow compiles and immediately
    # consumes its own config, never reaching `status="applied"`, and it must
    # never clobber the SOLO artifacts. This artifact is one of those. The
    # gate is unreachable today (no driver-domain candidate reaches this
    # function); remove it if that exclusion is ever retired deliberately
    # rather than by a multiroom consolidation nobody re-read this seam for.
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
        "corrections_provenance": {role: dict.fromkeys(("gain_db", "delay_ms", "inverted"), origin) for role in roles},
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
        "profile_fingerprint": profile["profile_fingerprint"],
        "targets": [{
            "role": target["role"],
            "target_fingerprint": target["target_fingerprint"],
            "required_protection_filters": [dict(requirement) for requirement in target["required_protection_filters"]],
        } for target in profile["targets"]],
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
) -> dict[str, Any]:
    """Resolve the complete applied record before changing the DSP graph."""
    from .linearization_fit import linearization_filters_by_role  # lazy: applied graph recording imports NumPy
    try:
        find_banked_candidate(candidate.fingerprint)
    except CandidateBankRefusal as exc:
        if exc.code != "not_found":
            raise
        publish_authored_candidate(candidate)
    protection = _protection_projection(design_draft.get("driver_safety_profile"))
    source = _source_payload(
        declaration.topology, design_draft, load_crossover_preview(current_design_draft=design_draft), measurements,
        measured_candidate_fingerprint=candidate.fingerprint, driver_protection=protection,
    )
    source = {**source, **((provenance or {}).get("source") or {}),
              **({"driver_protection_fingerprint": _fingerprint(protection)} if protection is not None else {}),
              "measured_candidate_fingerprint": candidate.fingerprint}
    source["fingerprint"] = _fingerprint({key: value for key, value in source.items() if key != "fingerprint"})
    corrections = driver_corrections(candidate)
    linearization = linearization_filters_by_role(candidate.linearization)
    meta = _measured_candidate_metadata(candidate, declaration.preset, declaration.topology, measurements, applied_at or _utc_now())
    snapshot = {
        **((provenance or {}).get("recomposition_snapshot") or {}),
        "schema_version": 1, "domain": "full", "topology_id": declaration.topology.topology_id,
        "topology_fingerprint": source["topology_fingerprint"],
        "preset": effective_preset(candidate_on_declaration(candidate, declaration.preset)).to_dict(), "corrections": corrections,
        "linearization": linearization, "blend_correction": list(candidate.blend_correction),
        "room_correction": dict(candidate.room_correction), "bass_extension": dict(candidate.bass_extension),
        "driver_protection": protection, "playback_device": declaration.playback_device,
        "measured_candidate_fingerprint": candidate.fingerprint,
    }
    applied = {
        **(provenance or {}),
        "artifact_schema_version": SCHEMA_VERSION, "kind": BASELINE_PROFILE_KIND,
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
            and existing.get("config") == candidate.get("config")):
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

    ``build_baseline_profile_candidate`` never writes ``baseline_config_path()``
    directly (issue #1666) -- every ``write=True`` candidate lands on its own
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


async def apply_baseline_profile(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]] | None = None,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    capture_device: str | None = None,
    capture_format: str | None = None,
    driver_domain: bool = False,
    program_channel: str | None = None,
    driver_domain_pair_trim_db: float = 0.0,
    tuning_owner: str = "manual",
    preserved_applied_profile: Mapping[str, Any] | None = None,
    expected_candidate_fingerprint: str | None = None,
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
    refresh_inputs: Callable[
        [],
        tuple[
            OutputTopology,
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
        ],
    ] | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Serialize candidate proof, compile, load, confirmation, and rollback."""

    async with dsp_writer_lock(
        baseline_config_path(config_path).parent,
        source="active_speaker_baseline_apply",
    ):
        if refresh_inputs is not None:
            topology, design_draft, crossover_preview, measurements = refresh_inputs()
        return await _apply_baseline_profile_locked(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            state_path=state_path,
            config_path=config_path,
            capture_device=capture_device,
            capture_format=capture_format,
            driver_domain=driver_domain,
            program_channel=program_channel,
            driver_domain_pair_trim_db=driver_domain_pair_trim_db,
            tuning_owner=tuning_owner,
            preserved_applied_profile=preserved_applied_profile,
            expected_candidate_fingerprint=expected_candidate_fingerprint,
            on_candidate_verified=on_candidate_verified,
            validate=validate,
        )


async def _apply_baseline_profile_locked(
    topology: OutputTopology,
    *,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]] | None = None,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    capture_device: str | None = None,
    capture_format: str | None = None,
    driver_domain: bool = False,
    program_channel: str | None = None,
    driver_domain_pair_trim_db: float = 0.0,
    tuning_owner: str = "manual",
    preserved_applied_profile: Mapping[str, Any] | None = None,
    expected_candidate_fingerprint: str | None = None,
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Apply the saved baseline candidate through the shared DSP transaction."""

    state_target = baseline_profile_state_path(state_path)

    def build_candidate(
        *,
        write: bool,
        compile_config: bool = False,
    ) -> dict[str, Any]:
        return build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=write,
            compile_config=compile_config,
            state_path=state_target,
            config_path=config_path,
            capture_device=capture_device,
            capture_format=capture_format,
            driver_domain=driver_domain,
            program_channel=program_channel,
            driver_domain_pair_trim_db=driver_domain_pair_trim_db,
            tuning_owner=tuning_owner,
            preserved_applied_profile=preserved_applied_profile,
            validate=validate,
        )

    reviewed_candidate = build_candidate(write=False)
    if expected_candidate_fingerprint is not None:
        refusal = reviewed_candidate_refusal(
            {**reviewed_candidate, "candidate_fingerprint": baseline_candidate_fingerprint(reviewed_candidate)},
            expected_candidate_fingerprint,
        )
        if refusal:
            return refusal
    candidate = build_candidate(write=True)
    if not driver_domain and (candidate.get("config") or {}).get("sha256"):
        from .runtime_contract import classify_bass_extension_graph, GRAPH_APPROVED_ACTIVE_RUNTIME  # lazy: graph proof imports baseline state

        proof = classify_bass_extension_graph(
            topology, evidence_source="desired",
            graph_text=Path(candidate["config"]["path"]).read_text(), applied_baseline_state=candidate,
        )
        if not proof.allowed or proof.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
            candidate["permissions"]["may_apply"] = False
            candidate["issues"].append(_issue("blocker", "baseline_graph_safety_proof_failed", proof.classification))
    if not candidate.get("permissions", {}).get("may_apply"):
        await _record_apply_outcome_into_bundle(
            measurements,
            candidate=candidate,
            apply_state=None,
            rollback_target=None,
        )
        return {
            "status": "blocked",
            "profile": candidate,
            "apply": None,
            "issues": [
                *candidate.get("issues", []),
                _issue(
                    "blocker",
                    "baseline_profile_not_ready_to_apply",
                    "save a ready active profile before applying it",
                ),
            ],
        }

    if on_candidate_verified is not None:
        await on_candidate_verified()

    _baseline_apply_started(topology, candidate)
    from .candidate_parts import candidate_from_applied_profile  # lazy: candidate parts consumes baseline readers

    prepared = candidate
    if not driver_domain:
        bank_candidate = candidate_from_applied_profile(topology, {**candidate, "status": "applied"})
        prepared = prepare_applied_baseline_profile(
            bank_candidate,
            declaration=MeasurementGraphProfile(
                preset=bank_candidate.source_preset, topology=topology, role_channels={},
                playback_device=str(candidate["config"]["playback_device"]),
            ),
            design_draft=design_draft, measurements=measurements,
            config_path=candidate["config"]["path"], config_sha256=candidate["config"]["sha256"],
            provenance=candidate,
        )

    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_baseline_apply",
            candidate_path=str((candidate.get("config") or {}).get("path")),
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            expected_candidate_sha256=str(
                (candidate.get("config") or {}).get("sha256") or ""
            ),
            validate=validate,
        )
    except DspApplyError as exc:
        return await _baseline_apply_result(topology, candidate, measurements, apply_state=exc.state, error=exc, state_path=state_target)

    applied = persist_applied_baseline_profile(prepared, apply_state=apply_state.to_dict(), state_path=state_target)
    promote_applied_baseline_candidate(applied, config_path=config_path)
    return await _baseline_apply_result(topology, applied, measurements, apply_state=apply_state)
