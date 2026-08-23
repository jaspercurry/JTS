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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Sequence

import yaml as yaml_parser

from jasper.atomic_io import atomic_write_text
from jasper.bass_extension.profile import (
    BassExtensionProfile,
    evaluate_bass_extension_profile,
)
from jasper.camilla_config_contract import (
    FilterSpec,
    PeqFilter,
)
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    config_file_sha256,
    dsp_writer_lock,
    same_config_file,
    validate_camilla_config,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

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
from .crossover_contract import (
    TUNING_OWNERS,
    automatic_candidate_readiness,
    crossover_snapshot_state,
    legacy_manual_preservation_state,
)
from .crossover_preview import crossover_preview_fingerprint
from .driver_base_trim import (
    REFUSED_STATUSES as BASE_TRIM_REFUSED_STATUSES,
    banked_base_trims,
)
from .driver_pad import effective_sensitivity_db
from .level_trim import (
    MAX_ATTENUATION_DB as _MAX_ATTENUATION_DB,
    LevelTrimError,
    attenuation_from_group_deltas,
)
from .playback_route import (
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles
from .revalidation import applied_profile_revalidation_satisfies_driver_target_proof
from .startup_hold import release_staged_startup_hold
from .staging import (
    build_passive_mains_with_sub_preset,
    compile_preset_from_crossover_preview,
    topology_is_passive_mains_with_sub,
)

if TYPE_CHECKING:
    from .measured_candidate import MeasuredElectricalCandidate
    from .measured_crossover_candidate import MeasuredCrossoverCandidate

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BASELINE_PROFILE_KIND = "jts_active_speaker_baseline_profile_candidate"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_baseline_profile.json")
DEFAULT_CONFIG_PATH = Path("/var/lib/camilladsp/configs/active_speaker_baseline.yml")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE"
CONFIG_PATH_ENV = "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH"
_DEFAULT_PERSISTED_BASS_PROFILE = object()

# Sensitivity deltas below this magnitude (dB) are treated as level-matched and
# get no derived trim, so the least-sensitive (reference) driver and any ties
# stay at unity.
_SENSITIVITY_TRIM_EPS_DB = 0.05

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


def _bass_extension_graph_summary(
    profile: BassExtensionProfile | None,
) -> dict[str, Any]:
    """Freeze authority evidence beside one just-emitted composition."""

    if (
        profile is None
        or profile.status != "accepted"
        or profile.enclosure["adapter_id"] != "sealed_v1"
    ):
        return {"authority_valid": True, "runtime_block_required": False}
    natural = profile.targets[-1]
    protected = all(target.subsonic is not None for target in profile.targets)
    return {
        "authority_valid": protected,
        "runtime_block_required": True,
        "bass_owner_channels": list(profile.bass_owner["channels"]),
        "natural": {
            "fp_hz": natural.fp_hz,
            "qp": natural.qp,
            "boost_headroom_db": natural.boost_headroom_db,
            "subsonic": (
                dict(natural.subsonic) if natural.subsonic is not None else None
            ),
        },
    }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def baseline_profile_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def baseline_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


def _safe_id(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in value)
    return out.strip("_")[:80] or "active_speaker"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def baseline_candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Identify the exact immutable Layer-A candidate, not its cache source."""

    source = candidate.get("source")
    snapshot = candidate.get("recomposition_snapshot")
    return _fingerprint({
        "artifact_schema_version": candidate.get("artifact_schema_version"),
        "kind": candidate.get("kind"),
        "source_fingerprint": (
            source.get("fingerprint") if isinstance(source, Mapping) else None
        ),
        "recomposition_snapshot": (
            dict(snapshot) if isinstance(snapshot, Mapping) else None
        ),
    })


def topology_config_fingerprint(topology: OutputTopology) -> str:
    """Fingerprint only topology fields that determine emitted DSP config."""
    return _fingerprint({
        key: value
        for key, value in topology.to_dict().items()
        if key != "pairing_intent"
    })


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


def active_layer_a_fingerprint(config_text: str) -> str:
    """Fingerprint the exact driver-domain suffix of one active graph.

    Room and preference EQ are allowed to change the program-domain filter
    prefix before the active split.  Everything from the split onward, plus
    the output-side device contract, is Layer A: routing, crossover filters,
    polarity, delay, gain, and protection.  This projection lets Active bind
    its immutable applied snapshot to the graph Room is about to preserve
    without making Room reconstruct crossover evidence.
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

    return _fingerprint(_canonicalize_camilla_defaults({
        "schema_version": 1,
        "domain": "jts_active_layer_a_v1",
        "output_devices": output_devices,
        "mixers": referenced_mixers,
        "pipeline_suffix": suffix,
        "filters": referenced_filters,
    }))


def _source_payload(
    topology: OutputTopology,
    design_draft: Mapping[str, Any],
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    *,
    measured_candidate_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Fingerprint the SOURCE inputs one baseline candidate was compiled from.

    ``build_baseline_profile_candidate`` names every solo candidate
    ``<stem>_candidate_<fingerprint12><suffix>`` from the ``fingerprint`` below,
    so this payload decides that filename (it also gates the ``write=False``
    cache hit there, which separately compares ``candidate_graph_context``).
    What it covers is exactly what it is handed: the topology's config
    fingerprint, the design draft's and crossover preview's identities, the
    measurement summary's identity, and — when the caller has one — the measured
    candidate's own fingerprint.

    **It is a source fingerprint, not a content hash of the emitted graph.**
    ``bass_extension_profile`` goes straight into
    ``emit_active_speaker_baseline_config`` and never reaches here, and neither
    do ``candidate_graph_context``'s graph fields — resolved playback device,
    capture device and format, domain, program channel, pair trim — since that
    dict is assembled after this call returns (only its
    ``measured_candidate_fingerprint`` is also a keyword argument here). Two
    candidates differing only in one of those land on the SAME filename
    carrying DIFFERENT bytes.

    **So a candidate's path must never be read as its graph identity.** PR
    #2311's adversarial gate refuted a double-load guard that made exactly that
    inference — same filename as the live config, therefore skip the CamillaDSP
    load. It reproduced the unsafe direction (household disables bass boost, the
    UI reports the apply applied, the speaker keeps running the boosted graph)
    and the guard was dropped. If a "skip the redundant load" optimization is
    ever wanted again, its oracle has to be the LIVE graph —
    :meth:`jasper.camilla.CamillaController.get_active_config_raw` normalized —
    never the path. That method's own docstring says the same thing for a second
    reason: ``set_active_config_raw`` deliberately leaves the persisted
    ``config_file_path`` unchanged, so after any audition the path reports the
    durable anchor rather than what is running.
    """
    measurement_summary = (
        measurements.get("summary")
        if isinstance(measurements.get("summary"), Mapping)
        else {}
    )
    # The baseline config cache invalidates whenever this source fingerprint
    # changes, so the topology fingerprint must cover ONLY topology fields that
    # determine the emitted CamillaDSP config (a one-directional constraint --
    # nothing spurious in; see this function's docstring for what is left out
    # altogether). `pairing_intent` is commission-time design intent that, by
    # contract, drives no config (the multiroom reconciler resolves the runtime
    # role from grouping.env, not from this field — see
    # output_topology.py and docs/HANDOFF-distributed-active.md gap 1), so it is
    # excluded: toggling it must not force a needless baseline recompile, and
    # excluding it keeps the fingerprint stable across the field's introduction.
    # The "pairing field never changes the cache" contract is pinned by
    # test_pairing_intent_change_does_not_invalidate_baseline_cache, which fails
    # if this key is ever renamed without updating the exclusion here.
    source = {
        "topology_id": topology.topology_id,
        "topology_fingerprint": topology_config_fingerprint(topology),
        "design_draft_updated_at": design_draft.get("updated_at"),
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


def _measured_level_trims(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    crossover_preview: Mapping[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Per-role attenuation-only trim from the MEASURED overlap-band level deltas.

    For each adjacent-driver crossover, both drivers' near-field captures through
    the production graph give their level in a band centred on the shared Fc; the
    driver-to-driver delta is the relative sensitivity at the handoff (the −6 dB
    Linkwitz-Riley shoulder cancels). We chain those deltas into a per-driver
    attenuation (quietest driver = reference, 0 dB), so the acoustic sum is level
    across every crossover — the MEASURED refinement of the datasheet sensitivity
    trim ``_derive_corrections`` otherwise seeds.

    TWO evidence sources answer that one question, and this function is the
    single owner of which one wins. The PREFERRED source is the base trim
    :mod:`jasper.active_speaker.driver_base_trim` banks — the headless
    ``jasper-driver-trim`` step every driver goes through before any crossover
    or linearization work, whose captures are driven by the speaker itself
    rather than by a household holding a phone. The FALLBACK, unchanged, is the
    guided per-driver captures the relay flow promotes. Both feed the same
    estimator and the same chain solver; a banked trim is preferred only because
    it is the deliberate measurement, not because it is a better one.
    ``crossover_preview`` is what a banked trim is keyed to: absent it (the
    cross-check path), only the guided captures are considered.

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
    if base_trims:
        # The banked record names the speaker groups it levelled, and the ledger
        # reports them under the SAME keys the guided path uses: readiness
        # (``crossover_contract.automatic_candidate_readiness``) and the setup
        # status both gate on those, so a measured speaker reporting zero
        # measured groups would read as un-measured. ``deltas`` stays empty
        # because the per-crossover evidence lives in the record itself, which
        # ``base_trim.state_path`` names.
        banked_group_ids = base_trim_meta.get("speaker_group_ids") or []
        return base_trims, {
            "source": "banked_base_trim",
            "base_trim": base_trim_meta,
            "comparison": "declared_crossover_gain_ledger_normalized",
            "groups_total": len(banked_group_ids),
            "groups_measured": len(banked_group_ids),
            "measured_group_ids": list(banked_group_ids),
            "deltas": [],
            "incomparable_groups": [],
            "trims": dict(base_trims),
        }

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
        "comparison": "placement_attested_gain_ledger_normalized",
        "placement_policy": DRIVER_PLACEMENT_POLICY_ID,
        "active_comparison_set_id": (
            active_comparison_set.get("comparison_set_id")
            if isinstance(active_comparison_set, Mapping)
            else None
        ),
        "incomparable_groups": incomparable_groups,
    }
    if not per_group_delta_chains:
        return {}, meta

    try:
        trims = attenuation_from_group_deltas(
            roles, per_group_delta_chains, minimum_db=_MAX_ATTENUATION_DB
        )
    except LevelTrimError:
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
    for region in preset.crossover_regions:
        if region.delay_ms is not None and region.delay_target_driver in corrections:
            corrections[region.delay_target_driver]["delay_ms"] = max(
                0.0, min(region.delay_ms, 20.0)
            )
            delay_provenance[region.delay_target_driver] = PROVENANCE_MANUAL

    drivers = crossover_preview.get("drivers")
    pinned_gain_roles: set[str] = set()
    estimated_gains: dict[str, float] = {}
    gain_provenance: dict[str, str] = {}
    sensitivities: dict[str, float] = {}
    if isinstance(drivers, Mapping):
        for role, driver in drivers.items():
            if role not in corrections or not isinstance(driver, Mapping):
                continue
            # #1665: fold any declared in-line pad into the naked datasheet
            # sensitivity before it feeds the datasheet-trim estimate below --
            # an L-pad'd driver's effective output is quieter than its bare
            # rating, and the interim trim must attenuate from THAT figure,
            # not the pre-pad one.
            naked_sensitivity = _finite_float(driver.get("sensitivity_db_2v83_1m"))
            sensitivity = effective_sensitivity_db(naked_sensitivity, driver.get("pad"))
            if sensitivity is not None:
                sensitivities[str(role)] = sensitivity
            gain = _finite_float(driver.get("gain_offset_db"))
            if gain is None:
                continue
            if gain > 0:
                issues.append(_issue(
                    "warning",
                    "positive_driver_gain_ignored",
                    f"positive gain for {role} was ignored; baseline gains only attenuate",
                ))
                gain = 0.0
            if gain < -60:
                issues.append(_issue(
                    "warning",
                    "driver_gain_clamped",
                    f"gain for {role} was clamped to -60 dB",
                ))
                gain = -60.0
            provenance = str(driver.get("gain_offset_db_provenance") or "").strip()
            # Pre-provenance preview artifacts are conservatively treated as a
            # pin: an upgrade must not replace a deliberate safety attenuation.
            if provenance not in {"research_estimate", "sensitivity_estimate"}:
                provenance = "operator_pinned"
                corrections[str(role)]["gain_db"] = gain
                pinned_gain_roles.add(str(role))
            else:
                estimated_gains[str(role)] = gain
            gain_provenance[str(role)] = provenance

    # Interim datasheet trim. When research declares no explicit gain_offset_db
    # for a driver but the sensitivities are known, attenuate the hotter drivers
    # down to the least-sensitive (reference) driver so a high-sensitivity
    # compression/horn driver can never start at full level relative to the
    # woofer (the shrill / horn-dominant failure mode, and a diaphragm hazard).
    # These are computed but NOT committed here: a usable MEASURED trim
    # overrides them below — from either evidence source ``_measured_level_trims``
    # accepts, the banked base trim ``jasper-driver-trim`` writes or the guided
    # per-driver captures — falling back to this datasheet estimate (marked
    # provisional) when no measurement is available.
    datasheet_trims: dict[str, float] = {}
    derivable_roles = [
        role for role in sensitivities if role not in pinned_gain_roles
    ]
    if len(sensitivities) >= 2 and derivable_roles:
        reference_db = min(sensitivities.values())
        for role in derivable_roles:
            trim_db = reference_db - sensitivities[role]  # <= 0 by construction
            if trim_db >= -_SENSITIVITY_TRIM_EPS_DB:
                continue  # reference driver and ties stay at unity
            datasheet_trims[role] = max(round(trim_db, 1), _MAX_ATTENUATION_DB)

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

    # --- MEASURED polarity refinement (Lane E), automatic tier -------------
    # Delay is intentionally absent: only Lane F's bounded, repeatable null
    # walk may author a delay value. A scalar carried by one capture is
    # forensic metadata, never an apply input.
    if tuning_owner == "automatic":
        from .crossover_contract import preset_matches_applied_profile

        context_matches = preset_matches_applied_profile(
            preset,
            applied_profile_context,
            candidate_corrections=corrections,
        )
        pairs_by_group = measurements.get("latest_summed_pairs_by_group")
        measured_groups = sorted(
            str(group_id)
            for group_id, group_pairs in (
                pairs_by_group.items()
                if isinstance(pairs_by_group, Mapping)
                else ()
            )
            if isinstance(group_pairs, Mapping) and group_pairs
        )
        if measured_groups and not context_matches:
            issues.append(_issue(
                "warning",
                "summed_alignment_graph_context_changed",
                (
                    "summed alignment evidence belongs to different crossover "
                    "settings; capture the pair again before applying polarity"
                ),
            ))
        elif len(measured_groups) > 1:
            issues.append(_issue(
                "warning",
                "group_specific_alignment_not_applied",
                (
                    "measurement-derived group-specific polarity evidence is "
                    "saved but not emitted yet"
                ),
            ))
        elif len(measured_groups) == 1:
            from .commissioning_capture import build_crossover_alignment_proposal
            from .crossover_alignment import POLARITY_INVERT

            alignment = build_crossover_alignment_proposal(
                preset,
                measurements,
                speaker_group_id=measured_groups[0],
                expected_profile_context_id=expected_profile_context_id,
                expected_applied_profile=applied_profile_context,
            )
            for item in alignment.get("proposals") or ():
                proposal = item.get("proposal") if isinstance(item, Mapping) else None
                proposal_issues = (
                    proposal.get("issues") if isinstance(proposal, Mapping) else None
                )
                if isinstance(proposal_issues, list) and any(
                    isinstance(issue, Mapping)
                    and issue.get("code") == "summed_decision_evidence_rejected"
                    for issue in proposal_issues
                ) and not any(
                    issue.get("code") == "summed_alignment_evidence_not_applied"
                    for issue in issues
                ):
                    issues.append(_issue(
                        "warning",
                        "summed_alignment_evidence_not_applied",
                        (
                            "summed alignment evidence failed the current applied-"
                            "graph, playback, placement, or analyzer contract; "
                            "capture the normal/reverse pair again"
                        ),
                    ))
                if (
                    not isinstance(proposal, Mapping)
                    or proposal.get("authorized") is not True
                    or proposal.get("polarity_margin_db") is None
                    or proposal.get("polarity_action") != POLARITY_INVERT
                ):
                    continue
                quality = (
                    item.get("decision_quality")
                    if isinstance(item.get("decision_quality"), Mapping)
                    else {}
                )
                if (
                    quality.get("alignment_snr_ok") is not True
                    or quality.get("null_depth_capped") is not False
                ):
                    if not any(
                        issue.get("code") == "summed_alignment_quality_not_applied"
                        for issue in issues
                    ):
                        issues.append(_issue(
                            "warning",
                            "summed_alignment_quality_not_applied",
                            (
                                "measured polarity was not applied because both "
                                "summed captures need affirmative overlap-band SNR "
                                "and an uncapped null"
                            ),
                        ))
                    continue
                role = str(proposal.get("upper_role") or "")
                if role in corrections:
                    corrections[role]["inverted"] = not bool(
                        corrections[role]["inverted"]
                    )
                    inverted_provenance[role] = PROVENANCE_MEASURED

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
        "baseline_id": f"baseline-{_safe_id(topology.topology_id)}",
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
    """Small immutable record sufficient to recompose the running Layer A."""
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
        "baseline_id": applied.get("baseline_id"),
        "applied_at": applied.get("applied_at"),
        "candidate_fingerprint": candidate_fingerprint,
        "source": dict(applied.get("source") or {}),
        "config": dict(applied.get("config") or {}),
        "corrections": dict(applied.get("corrections") or {}),
        "corrections_source": dict(applied.get("corrections_source") or {}),
        "gain_provenance": dict(applied.get("gain_provenance") or {}),
        "corrections_provenance": dict(applied.get("corrections_provenance") or {}),
        "level_match": dict(applied.get("level_match") or {}),
        # Layer-1a driver linearization (#1668 PR-D). Mirrors "corrections"'s
        # own top-level convenience copy — the authoritative copy consumed by
        # recompose_applied_baseline_yaml lives inside recomposition_snapshot
        # (already carried whole below); this top-level key is for callers
        # that want "what's currently applied" without unpacking the
        # snapshot. Absent on any pre-PR-D applied profile (era-tolerant).
        "linearization": dict(applied.get("linearization") or {}),
        # Gauge fix (2026-07-24): mirrors "linearization" immediately
        # above — same top-level convenience copy, same era-tolerant
        # absence. This is the field setup_status.read_active_speaker_setup_status
        # reads (via load_applied_baseline_profile_state -> here) to surface
        # WHY linearization did or didn't run on /state's protected_profile;
        # dropping it here (an allowlist function, unlike
        # persist_applied_baseline_profile's whole-object spread) would
        # silently strip it on every read even though the write side
        # persists it — see test_frozen_applied_profile_carries_linearization_top_level's
        # own docstring for the identical Gap 3c bug class this mirrors.
        "linearization_outcome": str(applied.get("linearization_outcome") or ""),
        # Crossover blend correction (design doc decision 10). Mirrors
        # "linearization" above exactly: an ALLOWLIST entry, so omitting it
        # would silently strip on every read a field the write side persists —
        # the Gap 3c bug class. A list, not a mapping, because it describes the
        # SUM rather than a driver.
        "blend_correction": list(applied.get("blend_correction") or []),
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
    """The pre-split common attenuation one profile's linearization costs, dB.

    Reads the profile's own emitter input — the reduced
    ``{role: [filter_dict, ...]}`` linearization mapping — through
    :func:`~jasper.active_speaker.camilla_yaml.linearization_headroom_db`, the
    emitter's OWN reducer, so this number cannot drift from the
    ``active_baseline_headroom`` gain the graph actually carries. Always ``>=
    0`` (it is an attenuation); ``0.0`` for a cut-only or absent
    linearization, which is every profile written before PR-L5.

    Prefers ``recomposition_snapshot["linearization"]`` — the copy
    :func:`recompose_applied_baseline_yaml` re-emits from — and falls back to
    the top-level convenience mirror for an era-older frozen profile that
    carries one without a snapshot. The two are written from a single variable
    (see the candidate payload), so the preference is about which one is
    AUTHORITATIVE, never about reconciling a disagreement.

    **One bounded cross-era case** (#1808 landed the peak rule on 2026-07-28).
    A profile emitted under the OLD sum rule carries a graph attenuated by
    ``H_sum``, but this reader — evaluating the same filters through the same
    chain the emitter uses today — returns ``H_peak``. For the one household
    whose PREVIOUS profile predates that deploy, the first post-deploy
    session's declared offset is therefore short by ``H_sum − H_peak`` (22.458
    vs 4.00 dB on the JTS3 profile that motivated the rule, so potentially
    large). Nothing is mis-levelled by it: the declared offset is an ANALYSIS
    input, no level moves either way, and the shortfall lands in the delta
    probe's ``residual_offset_db`` — visible, and named ``level_mismatch`` if
    it is material. It self-clears the moment that household applies once
    under the peak rule. Not worth an era flag; worth knowing when reading a
    first-session residual.

    Deliberately NOT the whole program-domain headroom: ``baseline_headroom_db``
    is a module constant and the room-PEQ / preference-EQ terms are
    recompose-time inputs that an active-crossover apply does not touch, so
    their contributions cancel in the difference this exists to serve
    (:func:`applied_program_level_delta_db`).
    """
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
    """One profile's AUTHORITATIVE reduced linearization, or ``{}``.

    The ``{role: [filter_dict, ...]}`` mapping the emitter takes — ALREADY
    reduced, so callers must not pass it through
    :func:`~jasper.active_speaker.linearization_fit.linearization_filters_by_role`
    (that helper's own docstring warns it silently returns ``{}`` for an
    already-reduced mapping, which would read as "this graph boosts nothing").

    The preference rule lives here and nowhere else, because it decides WHICH
    copy is authoritative and a second transcription is how two readers start
    disagreeing about what a speaker is playing:
    ``recomposition_snapshot["linearization"]`` is the copy
    :func:`recompose_applied_baseline_yaml` re-emits from, so it wins; the
    top-level key is an era-older convenience mirror for a frozen profile that
    carries one without a snapshot. Both are written from a single variable,
    so the preference is about authority, never about reconciling a
    disagreement.

    ``{}`` for a cut-only or absent linearization — every profile written
    before PR-L5 — and that is a true answer rather than a failure: both
    questions asked of this mapping (what does it cost, does it boost) are
    correct for a profile that linearizes nothing.
    """
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
    """One profile's AUTHORITATIVE ``{role: {gain_db, delay_ms, inverted}}``, or ``{}``.

    The per-driver refinement the emitted graph carries — the exact mapping
    :func:`~jasper.active_speaker.measured_crossover_candidate.driver_corrections`
    produced for the candidate that became this profile, so a reader gets the
    per-role trim, the branch delay, and the branch inversion that are ACTUALLY
    live rather than three values re-derived from a candidate nobody kept.

    Same authority rule, and the same reason for it, as
    :func:`profile_linearization` one function up:
    ``recomposition_snapshot["corrections"]`` is the copy
    :func:`recompose_applied_baseline_yaml` re-emits from, so it wins, and the
    top-level key is the era-older convenience mirror. Both are written from a
    single variable; the preference decides which is authoritative, never which
    of two disagreeing copies to believe.

    ``{}`` when neither copy is present or parseable — a true "this profile does
    not say", which every caller must treat as an absence rather than as a graph
    that trims nothing.
    """
    if not isinstance(profile, Mapping):
        return {}
    snapshot = profile.get("recomposition_snapshot")
    corrections = (
        snapshot.get("corrections") if isinstance(snapshot, Mapping) else None
    )
    if not isinstance(corrections, Mapping):
        corrections = profile.get("corrections")
    return corrections if isinstance(corrections, Mapping) else {}


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
    Room-PEQ and preference-EQ headroom are excluded because an
    active-crossover candidate is emitted without them
    (``build_baseline_profile_candidate`` passes no ``room_peqs`` /
    ``preference_filters``), so a household that has either can see a real
    level move this reader cannot see. That remainder is exactly what the
    probe's ``residual_offset_db`` measures and what
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
    # ``same_config_file`` rather than a comparison here: "do two paths name one
    # config file" is one rule with two askers (the other is
    # ``crossover_v2.round_anchor.running_config_diverged``), and it shipped as
    # two near-verbatim copies until an adversarial gate caught them. Its own
    # docstring carries the resolve-vs-string-compare reasoning.
    return "" if same_config_file(running, recorded) else APPLIED_PROFILE_DISPLACED


def _revalidation_payload(
    saved: Mapping[str, Any] | None,
    current_source: Mapping[str, Any],
    *,
    status: str,
    issues: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Describe whether a previously applied profile is stale.

    ``build_baseline_profile_candidate`` deliberately re-derives readiness from
    current evidence instead of trusting the saved JSON. When that re-derivation
    invalidates a profile that had already been applied, keep that fact visible:
    the household needs a "revalidate" path, not a mysterious blocked profile.
    """

    saved = _applied_profile_anchor(saved)
    if saved is None:
        return {"required": False, "status": "not_required"}
    saved_source = (
        saved.get("source") if isinstance(saved.get("source"), Mapping) else {}
    )
    saved_fingerprint = saved_source.get("fingerprint")
    current_fingerprint = current_source.get("fingerprint")
    if not saved_fingerprint or saved_fingerprint == current_fingerprint:
        return {"required": False, "status": "not_required"}

    issue_codes = {
        str(issue.get("code") or "")
        for issue in (issues or [])
        if isinstance(issue, Mapping)
    }
    if issue_codes == {"baseline_summed_validation_missing"}:
        next_step = "combined_check"
        message = (
            "active speaker setup changed after this profile was applied; "
            "re-run the combined crossover check, then save and apply a fresh profile"
        )
    elif status in {"ready_to_compile", "ready_to_apply", "compiled_apply_blocked"}:
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

    changed = [
        key
        for key in (
            "topology_fingerprint",
            "design_draft_updated_at",
            "crossover_preview_updated_at",
            "crossover_preview_fingerprint",
            "measurements_updated_at",
            "measurement_summary_fingerprint",
        )
        if saved_source.get(key) != current_source.get(key)
    ]
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


def measured_candidate_evidence_counts(candidate: Any) -> dict[str, int]:
    """How much per-driver and summed evidence a measured candidate actually
    carries — ``{"driver": n, "summed": n}``, both ``0`` for ``None``.

    **Existence is not evidence** (linearization-integrity PR-L4 item 5). Both
    completeness flags used to be satisfied by ``measured_candidate is not
    None``, which is a statement about a Python reference, not about anything
    measured. On the 2026-07-27 JTS3 profile that published
    ``summed_validation_complete: true`` beside ``validated_summed_group_count:
    0`` — a self-contradicting record that a test pinned as intended, and one of
    the reasons nothing in the verification chain objected to a 10 dB-dark
    speaker. This asks the candidate what it is holding.

    The two candidate shapes hold their evidence differently and both are
    honoured on their own terms:

    * :class:`~jasper.active_speaker.measured_candidate.MeasuredElectricalCandidate`
      (the evidence-store path) names an isolated and a summed evidence
      artifact. Each present artifact counts as one.
    * :class:`~jasper.active_speaker.measured_crossover_candidate.MeasuredCrossoverCandidate`
      (the v2 phone path) is counted from what its analysis RECORDED, never
      from fields its ``__post_init__`` guarantees. Per-driver: the roles whose
      level the trim solve actually produced (``analysis.trim_band_average_db``
      — ``None`` on a legacy candidate), or the roles the fit corrected, taking
      whichever is larger. Summed: the pre-apply cloud's walked positions
      (``exclusion_evidence.n_positions``), plus one for the MEASURE capture
      itself IF that capture carries a real cross-branch alignment
      (``alignment_confidence``) — the alignment is measured across both
      branches at once, which is summed-domain evidence. A trims-only candidate
      — the ineligible-mic-tier / fit-failed fallback, a real production shape —
      is NOT vacuous and still counts via those two.

    **Counting only what varies is the whole point** (PR-L4 review S1). A first
    cut counted ``len(role_attenuations_db)`` and "is ``analysis`` non-empty",
    both of which ``MeasuredCrossoverCandidate.__post_init__`` already enforces
    — so the counts were ≥1 by construction and the flags meant exactly what
    ``is not None`` had meant, which is the vacuity this item exists to remove.
    An "evidence count" that cannot be zero is not evidence.

    A count, not a boolean, because the caller publishes it: a reader seeing
    ``complete: true`` is entitled to see the number behind it.
    """
    if candidate is None:
        return {"driver": 0, "summed": 0}
    role_trims = getattr(candidate, "role_attenuations_db", None)
    if isinstance(role_trims, Mapping):
        analysis = getattr(candidate, "analysis", None)
        analysis = analysis if isinstance(analysis, Mapping) else {}
        solved_levels = analysis.get("trim_band_average_db")
        driver = len(solved_levels) if isinstance(solved_levels, Mapping) else 0
        linearization = getattr(candidate, "linearization", None)
        if isinstance(linearization, Mapping):
            driver = max(driver, len(linearization))
        exclusion = getattr(candidate, "exclusion_evidence", None)
        positions = 0
        if isinstance(exclusion, Mapping):
            try:
                positions = max(0, int(exclusion.get("n_positions") or 0))
            except (TypeError, ValueError):
                positions = 0
        if analysis.get("alignment_confidence") is not None:
            positions += 1
        return {"driver": driver, "summed": positions}
    return {
        "driver": 1 if getattr(candidate, "isolated_evidence_artifact", None) else 0,
        "summed": 1 if getattr(candidate, "summed_evidence_artifact", None) else 0,
    }


def _estimator_cross_check(
    preset: ActiveSpeakerPreset,
    measurements: Mapping[str, Any],
    candidate_trims_db: Mapping[str, float],
    crossover_preview: Mapping[str, Any] | None = None,
) -> list[str]:
    """Where the two level-match ESTIMATORS disagree, per role, as copy strings.

    PR-L4 item 3(b) / PR-L3 review finding N2. The repo carries two ways to
    measure the same inter-driver level gap and never compared them:

    * ``driver_acoustics``' point-at-Fc read (via
      :func:`_measured_level_trims`) — at Fc both branches sit on their matched
      -6 dB LR shoulder, so their delta IS the sensitivity delta, taken as a
      single interpolated point;
    * ``program_analysis.solve_branch_trims``' power-band average over each
      branch's own half of Fc, which is what a v2 measured candidate carries.

    **Disclosed, not refused** — and the distinction is deliberate. Unlike item
    3(a)'s measured-vs-datasheet check, whose two frames describe the SAME
    capture against a physical model, these two estimators in practice read
    DIFFERENT captures (the phone level-match session and the crossover MEASURE
    sweep are separate sittings, and the candidate branch below never runs the
    point-at-Fc path itself). Mic placement moves between sittings, so a
    disagreement here is weaker evidence than 3(a)'s and does not justify
    refusing a candidate the realized-level assertion has already graded. It
    justifies saying so.

    Tolerance is :data:`MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB`, reused rather
    than reinvented: this is the same "two independent estimates of one physical
    quantity" question with a comparable error budget (~2 dB of estimator-frame
    difference plus ~2-3 dB of between-sitting placement), and a second
    weakly-grounded constant would be worse than one well-argued one. Empty list
    when there is nothing to compare — the common case.
    """
    if not candidate_trims_db:
        return []
    try:
        point_trims, _meta = _measured_level_trims(
            preset, measurements, crossover_preview
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # The point-at-Fc reader is fail-closed by design and this is a
        # disclosure path; an unreadable measurements blob means "no second
        # opinion available", never a failed profile build. Logged at WARNING
        # like its sibling below, so a cross-check that silently stopped
        # running is visible rather than indistinguishable from agreement.
        log_event(
            logger, "baseline_profile.level_estimator_cross_check_unavailable",
            level=logging.WARNING, error=f"{type(exc).__name__}: {exc}",
        )
        return []
    notes: list[str] = []
    for role in sorted(set(point_trims) & set(candidate_trims_db)):
        disagreement_db = abs(point_trims[role] - candidate_trims_db[role])
        if disagreement_db <= MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB:
            continue
        notes.append(
            f"{role} crossover sweep {candidate_trims_db[role]:.1f} dB vs "
            f"level match {point_trims[role]:.1f} dB "
            f"({disagreement_db:.1f} dB apart)"
        )
    return notes


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

    The single source of the preview-readiness gate, shared by
    :func:`build_baseline_profile_candidate` and the mutable-evidence preview
    helper :func:`recompose_baseline_yaml` so those candidate paths cannot drift
    on what "ready" means. Production EQ recompose reads the applied snapshot.
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
    measured_candidate: "MeasuredElectricalCandidate | MeasuredCrossoverCandidate | None" = (
        None
    ),
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
    created_at: str | None = None,
    bass_extension_profile: BassExtensionProfile | None = None,
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
    follower (gap 1 of ``docs/HANDOFF-distributed-active.md``). The
    graph shape — crossover, per-driver limiters, tweeter high-pass, 0 dB
    ceiling — is unaffected; only the capture source line changes.

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
    if measured_candidate is not None:
        from .measured_candidate import MeasuredElectricalCandidate
        from .measured_crossover_candidate import MeasuredCrossoverCandidate

        if not isinstance(
            measured_candidate, (MeasuredElectricalCandidate, MeasuredCrossoverCandidate)
        ):
            raise TypeError(
                "measured_candidate must be MeasuredElectricalCandidate or "
                "MeasuredCrossoverCandidate"
            )
        if tuning_owner != "automatic":
            raise ValueError("measured_candidate requires automatic tuning ownership")
    if driver_domain and program_channel not in DRIVER_DOMAIN_PROGRAM_CHANNELS:
        raise ValueError(
            "driver_domain requires program_channel in "
            f"{DRIVER_DOMAIN_PROGRAM_CHANNELS}, not {program_channel!r}"
        )

    state_target = baseline_profile_state_path(state_path)
    config_target = baseline_config_path(config_path)
    now = created_at or _utc_now()
    source = _source_payload(
        topology,
        design_draft,
        crossover_preview,
        measurements,
        measured_candidate_fingerprint=(
            measured_candidate.fingerprint if measured_candidate is not None else None
        ),
    )
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
    # BOTH DEVICE HALVES follow the RESOLVED sink, in the same ONE derivation
    # the re-emit seam reads — the second production emit site learning the
    # lesson the first one already carries. The sink here is marker-aware
    # (``resolve_active_playback_device`` answers the ring on an armed box)
    # while the capture lane and the whole wire/latency geometry used to take
    # the emitter's ALSA-lane defaults, which is a candidate naming the ring and
    # sourcing the snd-aloop tap fan-in stops feeding the moment the coupling
    # arms. The arm ladder never traverses this builder and
    # ``recompose_applied_baseline_yaml``'s mirror check catches such a graph
    # loudly, so this closes a drift, not an outage — but two emit sites where
    # only one has learned is how the next one gets written.
    #
    # Every non-ring device (every box that is not armed, and every lab
    # override) answers exactly the emitter's own defaults, so this is
    # byte-identical there. Derived BEFORE ``candidate_graph_context`` because
    # the fingerprint has to describe the graph that will actually be emitted:
    # recording the caller's ``None`` would let two different capture lanes
    # share one candidate identity.
    #
    # A ring device whose declared wire this repo cannot parse REFUSES here
    # rather than resolving to something plausible: failing loud beats emitting
    # the half-moved graph this derivation exists to prevent, and nothing has
    # been written at this point either way. Only an armed box with a typo'd
    # ``JASPER_FANIN_RING_WIRE_FORMAT`` reaches it — and it reaches it with a
    # perfectly healthy jasper-fanin, because fan-in resolves that value once in
    # ``Config::from_env`` at process start while this reader is file-fresh per
    # call. A file edited after fan-in came up is a refusal here and nothing at
    # all there; the two coexist indefinitely.
    #
    # The parser raises a bare ``ValueError``; ``ActiveSpeakerConfigError`` is
    # the shape this function owes its callers. Being a ``ValueError`` subclass
    # it leaves every existing rendering alone (typed blocker, 400, 502) and
    # adds the one this conversion is for: the multiroom follower gate
    # (``jasper.multiroom.follower_config``) catches it, converts it to
    # ``ActiveFollowerError`` and reaches ``fall_back_to_solo()``. A bare
    # ``ValueError`` escapes that gate and aborts the grouping reconcile
    # instead, skipping the fallback the gate exists for. The parser's own
    # sentence rides through: it names the env var and the value that was typed.
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
    saved = _load_saved_state(state_target)
    candidate_graph_context = {
        "playback_device": resolved_playback_device,
        "domain": "driver" if driver_domain else "full",
        "program_channel": program_channel if driver_domain else None,
        "driver_domain_pair_trim_db": (
            driver_domain_pair_trim_db if driver_domain else 0.0
        ),
        "capture_device": emit_capture_device,
        "capture_format": emit_capture_format,
        "measured_candidate_fingerprint": (
            measured_candidate.fingerprint if measured_candidate is not None else None
        ),
    }
    saved_snapshot = (
        saved.get("recomposition_snapshot")
        if isinstance(saved, Mapping)
        and isinstance(saved.get("recomposition_snapshot"), Mapping)
        else {}
    )
    applied_anchor = _applied_profile_anchor(saved)
    applied_profile_context_id = ""
    if isinstance(applied_anchor, Mapping):
        applied_snapshot = applied_anchor.get("recomposition_snapshot")
        if isinstance(applied_snapshot, Mapping):
            # Never trust the persisted derived field for evidence admission.
            # Re-derive the context from the applied immutable graph inputs.
            applied_profile_context_id = baseline_candidate_fingerprint(applied_anchor)
    # Every SOLO (non-driver-domain) candidate lands on its OWN
    # source-fingerprinted sibling file -- unconditionally, whether or not a
    # profile was ever applied before, and regardless of what that prior
    # profile's own path was. (``_source_payload``'s docstring is the single
    # statement of what that fingerprint does and does not cover, and why a
    # candidate's path is never its graph identity.)
    #
    # Never the bare ``baseline_config_path()`` name (issue #1666): a
    # candidate write must never overwrite the file CamillaDSP's own
    # statefile, jasper-doctor, the multiroom follower fallback, and a human
    # inspecting the box all read as the durable truth. Before this was
    # unconditional, an applied profile's path alternated between the
    # canonical name and a sibling on every successive apply (whichever the
    # PREVIOUS apply did NOT use) -- so an apply landing on the canonical
    # half of that alternation wrote unvalidated candidate bytes there
    # BEFORE validation/activation, and a rejected apply could leave the
    # canonical file holding rejected bytes. The canonical name is now
    # written ONLY by the post-success promote step in
    # ``_apply_baseline_profile_locked`` / ``restore_applied_baseline_profile``
    # (and commissioning's ``finalize_retained_candidate_apply``), which runs
    # after ``apply_dsp_config`` has already proven the candidate live.
    #
    # ``driver_domain=True`` candidates are deliberately EXCLUDED: they are
    # the multiroom follower/leader bonding machinery's own role-specific
    # compile-then-immediately-consume seam (jasper.multiroom.follower_config
    # / active_leader_config), which passes its OWN dedicated config_path +
    # state_path precisely "so the solo baseline artifacts are never
    # clobbered" (this function's own docstring). That state_path never
    # reaches ``persist_applied_baseline_profile`` / ``status="applied"``, so
    # ``applied_anchor`` above is always None for it -- there is no applied
    # lineage to protect or promote, no Undo-to-a-prior-candidate feature for
    # it, and its caller re-proves the freshly written file synchronously
    # before ever loading it. Forcing it onto a sibling would silently break
    # that caller's read of its OWN just-written config_path (confirmed by
    # tests/test_multiroom_follower_config.py and
    # tests/test_multiroom_active_leader_config.py against this change).
    if not driver_domain:
        config_target = config_target.with_name(
            f"{config_target.stem}_candidate_{source['fingerprint'][:12]}"
            f"{config_target.suffix}"
        )
    retained_applied = _frozen_applied_profile(applied_anchor)

    def finalize(payload: dict[str, Any]) -> dict[str, Any]:
        if retained_applied is not None:
            payload["applied_recomposition_profile"] = retained_applied
        payload["revalidation"] = _revalidation_payload(
            saved,
            source,
            status=str(payload.get("status") or ""),
            issues=[
                issue for issue in payload.get("issues", [])
                if isinstance(issue, Mapping)
            ],
        )
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
    # PR-L4 item 5: a candidate satisfies a completeness flag by the evidence it
    # CARRIES, never by existing. See `measured_candidate_evidence_counts`.
    candidate_evidence = measured_candidate_evidence_counts(measured_candidate)
    driver_target_proof_complete = bool(candidate_evidence["driver"]) or bool(
        summary.get("driver_checks_complete")
        or summary.get("driver_measurements_complete")
    )
    driver_target_proof_source = (
        "measured_candidate"
        if candidate_evidence["driver"]
        else ("measurements" if driver_target_proof_complete else "missing")
    )
    summed_validation_complete = bool(candidate_evidence["summed"]) or bool(
        summary.get("summed_validation_complete")
    )
    summed_validation_source = (
        "measured_candidate"
        if candidate_evidence["summed"]
        else ("measurements" if summed_validation_complete else "missing")
    )
    # A passive-mains + local-subwoofer topology has NO inter-driver crossover, so
    # it never produces an active crossover preview and has no per-driver / summed
    # active-crossover measurements to complete. It is still roleful (bass
    # management splits the program), so it rides the SAME multi-output emitter as
    # the active path — but via a degenerate 1-way preset built directly from the
    # topology, skipping the preview-readiness and active-measurement gates that
    # only apply to a real active crossover. A SUBLESS passive speaker never
    # reaches here (it takes the flat emit_sound_config lane).
    passive_sub = topology_is_passive_mains_with_sub(topology)

    preset: ActiveSpeakerPreset | None = None
    preset_gates: list[dict[str, Any]] = []
    if passive_sub:
        if not resolved_playback_device:
            issues.append(_issue(
                "blocker",
                "baseline_playback_device_missing",
                "active profile compiler needs an explicit active playback device",
            ))
        for issue in route_capability.issues:
            if issue.get("code") == "active_playback_route_too_narrow":
                issues.append(issue)
        preset, preset_issues, preset_gates = build_passive_mains_with_sub_preset(
            topology
        )
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
                issues=issues,
            )
            if applied_profile_revalidation_satisfies_driver_target_proof(
                revalidation_for_driver_proof
            ):
                driver_target_proof_complete = True
                driver_target_proof_source = "applied_profile_revalidation"
        if not driver_target_proof_complete:
            issues.append(_issue(
                "blocker",
                "baseline_driver_measurements_missing",
                "confirm each driver with a quiet test before saving the active profile",
            ))
        summed_validation_complete = (
            bool(candidate_evidence["summed"])
            or bool(summary.get("summed_validation_complete"))
            or (
                driver_target_proof_complete
                and _summed_validation_evidence_complete(summary)
            )
        )
        summed_validation_source = (
            "measured_candidate"
            if candidate_evidence["summed"]
            else ("measurements" if summed_validation_complete else "missing")
        )
        if not summed_validation_complete:
            issues.append(_issue(
                "blocker",
                "baseline_summed_validation_missing",
                "validate the combined crossover before saving the active profile",
            ))
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

    if measured_candidate is not None and measured_candidate.source_preset != preset:
        return finalize(_blocked_payload(
            topology=topology,
            source=source,
            issues=[_issue(
                "blocker",
                "measured_candidate_preset_mismatch",
                "the reviewed measured candidate no longer equals the saved crossover",
            )],
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
    if measured_candidate is not None:
        corrections = measured_candidate.driver_corrections()
        roles = required_driver_roles(preset.way_count)
        measured_group_count = sum(
            group.mode in {"active_2_way", "active_3_way"}
            for group in topology.speaker_groups
        )
        correction_issues: list[dict[str, str]] = []
        # PR-L4 item 3(b): the ONLY place the repo's two level-match estimators
        # can be read side by side (PR-L3 review finding N2). The candidate's
        # trims come from `solve_branch_trims`' power-band average over each
        # branch's own half of Fc; `_measured_level_trims` reads
        # `driver_acoustics`' point-at-Fc interpolation of the same pair. Two
        # different frames for one physical quantity, and until now nothing
        # compared them.
        estimator_notes = _estimator_cross_check(
            preset,
            measurements,
            dict(measured_candidate.role_attenuations_db),
            crossover_preview,
        )
        if estimator_notes:
            correction_issues.append(_issue(
                "warning",
                "driver_level_estimator_disagreement",
                (
                    "the two ways JTS can measure the driver level gap disagree "
                    "by more than "
                    f"{MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB:.0f} dB ("
                    + "; ".join(estimator_notes)
                    + "); the crossover measurement's own value was used"
                ),
            ))
            log_event(
                logger, "baseline_profile.level_estimator_disagreement",
                level=logging.WARNING,
                tolerance_db=MEASURED_VS_DATASHEET_TRIM_TOLERANCE_DB,
                detail="; ".join(estimator_notes),
            )
        correction_meta = {
            "sources": {role: "measured" for role in roles},
            "gain_provenance": {role: "measured" for role in roles},
            "provisional": False,
            "level_match": {
                "groups_total": measured_group_count,
                "groups_measured": measured_group_count,
                "comparison": "strict_measured_candidate",
                "incomparable_groups": [],
                "applied": True,
                "estimator_disagreements": list(estimator_notes),
            },
            "corrections_provenance": {
                role: {
                    "gain_db": PROVENANCE_MEASURED,
                    "delay_ms": PROVENANCE_MEASURED,
                    "inverted": PROVENANCE_MEASURED,
                }
                for role in roles
            },
        }
    else:
        corrections, correction_issues, correction_meta = _derive_corrections(
            preset,
            crossover_preview,
            measurements,
            tuning_owner=tuning_owner,
            expected_profile_context_id=expected_profile_context_id or None,
            applied_profile_context=applied_anchor,
        )
    issues.extend(correction_issues)
    # Layer-1a driver linearization (#1668 PR-D). Threads whatever the
    # measured candidate carries: empty for a plain trims candidate, a
    # legacy MeasuredElectricalCandidate (no ``.linearization`` attribute —
    # hence ``getattr`` with a default), or a pre-PR-C persisted
    # MeasuredCrossoverCandidate. Reduced to the emitter's own input shape by
    # the shared helper, ``linearization_fit.linearization_filters_by_role``
    # — the same reduction ``measured_crossover_candidate.compile_candidate_config``
    # uses, so the two RICH-candidate call sites reduce identically. NOT
    # shared with ``recompose_applied_baseline_yaml`` below: that seam reads
    # THIS function's already-reduced output back out of a persisted
    # snapshot, so it deliberately re-validates the reduced shape inline
    # instead of calling this helper again (calling it on an already-reduced
    # mapping silently returns {} for every role — see
    # linearization_filters_by_role's own docstring). Do not "consolidate"
    # the two.
    from .linearization_fit import linearization_filters_by_role

    linearization = linearization_filters_by_role(
        getattr(measured_candidate, "linearization", None) or {}
    )
    # Gauge fix (2026-07-24): the single writer's own verdict for WHY
    # linearization did or didn't run this attempt — "" (empty, the
    # ``getattr`` default) for a plain trims candidate, a legacy
    # MeasuredElectricalCandidate (no ``.linearization_outcome`` attribute),
    # or a pre-gauge-fix persisted MeasuredCrossoverCandidate. Never
    # re-derived here — see MeasuredCrossoverCandidate.linearization_outcome's
    # own docstring.
    linearization_outcome = str(
        getattr(measured_candidate, "linearization_outcome", "") or ""
    )
    # Decision 10's blend correction, already in the emitter's flat shape (the
    # solver writes it that way). ``getattr`` with a default for the same
    # reason the two above use one: a legacy MeasuredElectricalCandidate has no
    # such attribute at all.
    blend_correction = [
        dict(entry)
        for entry in (getattr(measured_candidate, "blend_correction", ()) or ())
    ]
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
                or gain < _MAX_ATTENUATION_DB
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
    required_group_ids = sorted(
        group.id
        for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
    )
    if measured_candidate is not None:
        automatic_candidate = {
            "ready": True,
            "reason": None,
            "detail": "The exact reviewed measured candidate is ready to apply.",
            "required_group_ids": required_group_ids,
            "measured_group_ids": required_group_ids,
            "summed_group_ids": required_group_ids,
            "measurement_comparable": True,
            "excitation_comparable": True,
        }
    else:
        automatic_candidate = automatic_candidate_readiness(
            required_group_ids=required_group_ids,
            level_match=correction_meta["level_match"],
            measurement_summary=summary,
            active_comparison_set=measurements.get("active_comparison_set"),
        )
    if tuning_owner == "automatic" and not automatic_candidate["ready"]:
        issues.append(_issue(
            "blocker",
            str(automatic_candidate["reason"]),
            str(automatic_candidate["detail"]),
        ))
    provisional = bool(correction_meta.get("provisional"))
    if driver_domain and bass_extension_profile is None:
        applied_bass_anchor = load_applied_baseline_profile_state()
        evaluation = evaluate_bass_extension_profile(
            topology=topology,
            applied_baseline_state=applied_bass_anchor,
        )
        if evaluation.status == "accepted":
            bass_extension_profile = evaluation.profile
    validation = {"status": "skipped", "reason": "not_written"}
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
                out_path=config_target,
                baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
                bass_extension_profile=bass_extension_profile,
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
                out_path=config_target,
                baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
                bass_extension_profile=bass_extension_profile,
                linearization=linearization,
                blend_correction=blend_correction,
            )
            # A v2 measured candidate carrying delay/polarity re-proves its
            # exact requested delay binding against the freshly compiled text
            # before this candidate can ever reach "ready_to_apply" — the
            # delay_graph + graph_safety proofs named in the crossover
            # measurement v2 design (§5.8). A failed proof is a blocker issue,
            # exactly like a failed CamillaDSP validation below: fail closed,
            # no partial write reaches "ready". Scoped to the new candidate
            # type only (isinstance), so a legacy MeasuredElectricalCandidate
            # or a plain trims candidate is completely unaffected.
            from .measured_crossover_candidate import (
                MeasuredCrossoverCandidate,
                MeasuredCrossoverCandidateError,
                prove_candidate_config,
            )

            if (
                isinstance(measured_candidate, MeasuredCrossoverCandidate)
                and measured_candidate.alignment.delay_role is not None
            ):
                try:
                    prove_candidate_config(measured_candidate, yaml)
                except MeasuredCrossoverCandidateError as exc:
                    log_event(
                        logger,
                        "correction.crossover_alignment_proof_blocked",
                        level=logging.ERROR,
                        code=exc.code,
                        detail=exc.detail,
                        candidate_fingerprint=measured_candidate.fingerprint,
                        delay_role=measured_candidate.alignment.delay_role,
                    )
                    issues.append(_issue(
                        "blocker",
                        "measured_candidate_alignment_proof_failed",
                        str(exc),
                    ))
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
        status = "ready_to_apply" if not any(
            issue["severity"] == "blocker" for issue in issues
        ) else "blocked"
        handoff_issue = _apply_handoff_issue(playback_device_source)
        if status == "ready_to_apply" and handoff_issue:
            issues.append(handoff_issue)
            status = "compiled_apply_blocked"
    else:
        config_sha256 = None
        status = "ready_to_compile"

    payload = {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": status,
        "baseline_id": f"baseline-{_safe_id(topology.topology_id)}",
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
            "measured_candidate_evidence": dict(candidate_evidence),
        },
        "corrections": corrections,
        "corrections_source": correction_meta["sources"],
        "gain_provenance": correction_meta["gain_provenance"],
        "corrections_provenance": correction_meta["corrections_provenance"],
        "level_match": correction_meta["level_match"],
        # Layer-1a driver linearization (#1668 PR-D) — same reduced
        # {role: [filter_dict, ...]} shape emit_active_speaker_baseline_config
        # consumes; mirrors "corrections" (top-level convenience copy of what
        # the immutable recomposition_snapshot below also carries). N1
        # (#1668 PR-D review): this top-level copy is NOT what
        # recompose_applied_baseline_yaml reads -- see that field's own
        # comment inside recomposition_snapshot below.
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
        # Crossover blend correction (decision 10) — top-level convenience
        # copy, mirroring "linearization" above. The authoritative copy the
        # recompose re-emits lives inside recomposition_snapshot below; this
        # one is what a "what is applied right now" read (`/state`, the apply
        # observability line) uses without unpacking the snapshot.
        "blend_correction": blend_correction,
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
        # Immutable Layer-A inputs captured at Save. Once this candidate is
        # explicitly applied, every production recompose reads ONLY this
        # snapshot; later measurement/design edits remain candidates and cannot
        # alter playback as a side effect of applying room/preference EQ.
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
            # Layer-1a driver linearization (#1668 PR-D): the immutable input
            # every future recompose (room/preference EQ, /sound) re-emits
            # verbatim — THIS is the copy recompose_applied_baseline_yaml
            # reads (not the top-level mirror above). Absent/empty for every
            # profile saved before this stage existed (era-tolerant on read;
            # see that function's own snapshot.get("linearization", {})).
            "linearization": linearization,
            # Decision 10's blend correction: an INPUT every future recompose
            # (room/preference EQ, /sound) must re-emit verbatim, so it belongs
            # in the immutable snapshot on the same test "linearization" passes
            # — is this something a later recompose needs to rebuild the graph?
            # It is: dropping it here would silently revert the blend
            # correction on the next preference-EQ save.
            "blend_correction": blend_correction,
            **candidate_graph_context,
        },
    }
    if driver_domain:
        # The bond precheck consumes this immutable sidecar in the independent
        # whole-graph verifier. It comes from the already-evaluated profile
        # passed to the emitter, never from filter-name inference or caller I/O.
        payload["bass_extension_profile_summary"] = (
            _bass_extension_graph_summary(bass_extension_profile)
        )
    payload = finalize(payload)
    payload["candidate_fingerprint"] = baseline_candidate_fingerprint(payload)
    if write:
        atomic_write_text(
            state_target,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
    return payload


def applied_baseline_hardware_match(
    topology: OutputTopology,
    *,
    applied_profile: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, list[dict[str, str]]]:
    """Is this applied profile a usable baseline that STILL MATCHES the hardware?

    Returns ``(snapshot, [])`` when it is, and ``(None, [blocker])`` otherwise,
    following this module's ``(value | None, issues)`` convention. The snapshot
    comes back because proving it usable and reading it are the same act — a
    caller that re-fetched ``recomposition_snapshot`` itself would be a second
    reader of a key this function exists to validate.

    ONE OWNER, TWO CALLERS, and the second caller is why this is a function.
    :func:`recompose_applied_baseline_yaml` has always asked these four questions
    inline before emitting; it still asks them, through here. The new caller is
    the unattended roleful gate
    (``jasper.fanin.coupling_reconcile.ring_roleful_unattended_ready``), which
    must answer "does this box HAVE a hardware-matched applied baseline?" without
    emitting anything. Re-deriving the compare there would put the definition of
    "matches the hardware" in two places — the failure mode where one site is
    fixed and the other silently keeps admitting.

    The four questions, in refusal order, are unchanged and so are their codes:
    the record is an APPLIED profile; it carries a schema-1 recomposition
    snapshot (pre-snapshot profiles cannot be recomposed at all); its domain is
    ``full``; and its recorded topology identity AND fingerprint both still match
    the topology passed in. The last is the DAC-swap guard — a genuine hardware
    change refuses here rather than emitting a graph composed for other drivers.
    """
    if applied_profile.get("status") != "applied":
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_unavailable",
            "the saved active-speaker profile is not an applied profile",
        )]
    snapshot = applied_profile.get("recomposition_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("schema_version") != 1:
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_unavailable",
            (
                "the applied active-speaker profile predates immutable "
                "recomposition; apply it again before adding EQ"
            ),
        )]
    if snapshot.get("domain") != "full":
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_domain_invalid",
            "only a full solo active-speaker profile can host room or preference EQ",
        )]
    if (
        snapshot.get("topology_id") != topology.topology_id
        or snapshot.get("topology_fingerprint")
        != topology_config_fingerprint(topology)
    ):
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_topology_stale",
            (
                "the applied active-speaker profile belongs to a different "
                "output topology; reapply speaker setup first"
            ),
        )]
    return snapshot, []


def recompose_applied_baseline_yaml(
    topology: OutputTopology,
    *,
    applied_profile: Mapping[str, Any],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    out_path: str | Path | None = None,
    playback_device: str | None = None,
    bass_extension_profile: BassExtensionProfile | None | object = (
        _DEFAULT_PERSISTED_BASS_PROFILE
    ),
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit Layer A strictly from the immutable applied-profile snapshot.

    This is the production graph-carrier seam. Mutable design drafts,
    crossover previews, and measurement stores are deliberately not parameters:
    captures remain candidates until :func:`apply_baseline_profile` snapshots
    them under an explicit Apply transaction.

    ``playback_device`` is the ONE axis a re-emit may legitimately move, and it
    is opt-in: ``None`` emits against the device the snapshot recorded,
    byte-for-byte as before. An explicit value re-points the SAME applied
    evidence at a different transport for the SAME lane — the
    active ALSA lane and the ACTIVE RING carry identical post-crossover
    per-driver program at identical width to the same reader. What differs
    besides the name is the whole ``devices:`` block the sink implies — its
    CAPTURE lane (the ring coupling is end-to-end), its wire format, and its
    CamillaDSP latency/queue geometry — which ``active_emit_devices`` derives
    FROM the device so this function has one place that knows, not several.
    Moving the device is what makes the ring arm possible at all: the
    reconciler derives its endpoint marker FROM the loaded graph, so the graph
    has to name the ring first. Passing anything that is not a legal active
    endpoint is refused by the emitter's own forbidden-token and width guards,
    not here.
    """
    snapshot, hardware_issues = applied_baseline_hardware_match(
        topology, applied_profile=applied_profile
    )
    if snapshot is None:
        return None, hardware_issues
    if bass_extension_profile is _DEFAULT_PERSISTED_BASS_PROFILE:
        evaluation = evaluate_bass_extension_profile(
            topology=topology,
            applied_baseline_state=applied_profile,
        )
        bass_extension_profile = (
            evaluation.profile if evaluation.status == "accepted" else None
        )
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
    except (ActiveSpeakerConfigError, TypeError, ValueError) as exc:
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_invalid",
            f"the applied active-speaker snapshot is invalid: {exc}",
        )]
    corrections = snapshot.get("corrections")
    # The snapshot's device is the DEFAULT, never the only answer: an explicit
    # ``playback_device`` re-points this evidence at the other transport of the
    # same lane (see the parameter's note). The validity check below still runs
    # against the SNAPSHOT's value, because an override cannot repair a snapshot
    # that recorded no device — that snapshot is invalid whatever endpoint the
    # caller asks for, and letting an override mask it would emit a graph from
    # evidence we just failed to verify.
    snapshot_playback_device = snapshot.get("playback_device")
    expected_roles = set(required_driver_roles(preset.way_count))
    correction_roles = set(corrections) if isinstance(corrections, Mapping) else set()
    corrections_valid = (
        correction_roles == expected_roles
        and all(isinstance(value, Mapping) for value in corrections.values())
    ) if isinstance(corrections, Mapping) else False
    if (
        not corrections_valid
        or not isinstance(snapshot_playback_device, str)
        or not snapshot_playback_device
    ):
        return None, [_issue(
            "blocker",
            "applied_baseline_snapshot_invalid",
            "the applied active-speaker snapshot is missing corrections or playback device",
        )]
    emit_playback_device = playback_device or snapshot_playback_device
    # BOTH DEVICE HALVES follow the sink, in ONE derivation. Composed here rather
    # than left to each caller because a graph naming the ring with the ALSA
    # lane's format, latency geometry or queue depth is a graph that names the
    # right device and behaves like the wrong one — and neither half of that is
    # hypothetical. The FORMAT: a ring re-emit that inherited the box's
    # program-lane default put S32_LE on jts3's ring while the resolver answered
    # S16_LE, a sheared attach waiting at the arm's last rung (2026-08-11,
    # captures/r7b-jts3-arm2-20260811T132227Z). The CAPTURE: moving only the
    # playback device leaves the graph sourcing the snd-aloop tap that fan-in
    # stops feeding the moment the coupling arms — silence with every daemon
    # healthy, and quiet, because the plan compares capture CHANNELS (2 == 2) and
    # the width gate only holds ring-NAMED lanes to the wire. Non-ring devices
    # answer the emitter's own defaults. Nothing restores the tap: the ring is
    # the one legal ACTIVE endpoint, so there is no reverse derivation to run.
    # The topology goes in because the ring's resolution is per-box.
    #
    # The ring branch resolves the box's DECLARED wire, which FAILS LOUD on a
    # token neither language recognizes (a typo in
    # JASPER_FANIN_RING_WIRE_FORMAT). That is the right verdict — jasper-fanin
    # parks on the same value — but it must reach the operator as this
    # function's ordinary refusal, not as a traceback out of
    # `jasper-active-speaker baseline-reemit`. Every caller gets the parser's own
    # sentence, and nothing has been written yet.
    try:
        devices = active_emit_devices(emit_playback_device, topology=topology)
    except ValueError as exc:
        return None, [_issue(
            "blocker",
            "ring_wire_declaration_invalid",
            f"this box declares a ring wire neither jasper-fanin nor JTS can "
            f"resolve, so there is no wire to emit against: {exc}",
        )]
    # Layer-1a driver linearization (#1668 PR-D): read era-tolerantly (absent
    # on any pre-PR-D snapshot -> {}, "no linearization was fit" — the same
    # convention MeasuredCrossoverCandidate.from_mapping uses for its own
    # "linearization" key). Already in the emitter's reduced input shape —
    # build_baseline_profile_candidate stores it that way (see its own
    # linearization_filters_by_role call) — so no further reduction here.
    # This is the fix for the CRITICAL silent-reversion gap: before this
    # read, every /sound preference-EQ recompose (and any other
    # recompose_applied_baseline_yaml caller) silently dropped an applied
    # profile's linearization stage on the next recompose.
    linearization_raw = snapshot.get("linearization")
    linearization = (
        {
            str(role): list(filters)
            for role, filters in linearization_raw.items()
            if isinstance(filters, Sequence) and not isinstance(filters, (str, bytes))
        }
        if isinstance(linearization_raw, Mapping)
        else {}
    )
    # Decision 10's blend correction, read era-tolerantly for exactly the
    # reason above and against exactly the same gap: absent on every snapshot
    # written before the stage existed -> [] -> no stage emitted, and a
    # /sound preference-EQ save on a corrected speaker must NOT silently
    # revert the blend correction the household is listening to.
    blend_correction_raw = snapshot.get("blend_correction")
    blend_correction = (
        [dict(entry) for entry in blend_correction_raw
         if isinstance(entry, Mapping)]
        if isinstance(blend_correction_raw, Sequence)
        and not isinstance(blend_correction_raw, (str, bytes, Mapping))
        else []
    )
    yaml = emit_active_speaker_baseline_config(
        preset,
        playback_device=emit_playback_device,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
        corrections={str(role): dict(value) for role, value in corrections.items()},
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
        baseline_id=str(
            applied_profile.get("baseline_id")
            or f"baseline-{_safe_id(topology.topology_id)}"
        ),
        bass_extension_profile=(
            bass_extension_profile
            if isinstance(bass_extension_profile, BassExtensionProfile)
            else None
        ),
        linearization=linearization,
        blend_correction=blend_correction,
    )
    return yaml, []


def recompose_baseline_yaml(
    topology: OutputTopology,
    *,
    crossover_preview: Mapping[str, Any],
    measurements: Mapping[str, Any],
    room_peqs: Sequence[PeqFilter] = (),
    preference_filters: Sequence[FilterSpec] = (),
    output_trim_db: float = 0.0,
    playback_device: str | None = None,
    out_path: str | Path | None = None,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit the active-speaker baseline YAML for the current accepted
    evidence, with optional program-domain room PEQ / preference EQ inserted
    pre-split.

    This is the candidate/debug composition seam retained for callers that are
    deliberately previewing current mutable evidence. Production graph
    recomposition uses :func:`recompose_applied_baseline_yaml` instead. This
    helper rebuilds the SAME structural baseline from the supplied evidence — reusing the
    exact derivation primitives :func:`build_baseline_profile_candidate` uses
    (``resolve_active_playback_device`` → ``compile_preset_from_crossover_preview``
    → ``_derive_corrections`` → ``emit_active_speaker_baseline_config``) — rather
    than parsing the running config (the explicit anti-pattern). Only the
    ``room_peqs``, ``preference_filters`` (and the explicit ``output_trim_db``
    attenuation) differ from the durable baseline; the crossover, per-driver
    limiters, tweeter high-pass, and 0 dB ceiling are identical, so the emitted
    YAML re-proves as ``GRAPH_APPROVED_ACTIVE_RUNTIME``.

    ``room_peqs`` are preserved room-correction filters. They run pre-split on
    channels [0, 1], and their positive-boost headroom is folded into
    ``active_baseline_headroom`` rather than emitted as a second program-domain
    gain.

    ``output_trim_db`` is the household's manual headroom + loudness-match
    attenuation; the emitter folds it into ``active_baseline_headroom`` so the
    active EQ apply honours it exactly like the stereo path.

    Unlike :func:`build_baseline_profile_candidate` /
    :func:`apply_baseline_profile`, this re-emit takes no ``capture_device``: it
    inserts program-domain (Layer C) preference EQ, which only ever runs on the
    fan-in-fed program domain — a solo speaker's single graph and a pair
    leader's bake instance (``camilla#1``). A wireless follower (and a leader's
    own-driver instance, ``camilla#2``) is Layer-A-only and never recomposes
    preference EQ, so this seam always captures from the fan-in program tap. The
    role-varying capture (the grouping ring) belongs to the driver-domain
    emit on build/apply, where ``capture_device`` lives.

    The fan-in program tap is the ALSA snd-aloop capture. Layer A is rebuilt
    from the canonical evidence and unchanged.

    **Gate scope (intentionally a subset of the candidate builder).** This only
    re-checks what it needs to EMIT a structurally-valid baseline — playback
    device, preview-readiness (the shared :func:`_crossover_preview_ready`
    predicate), and a compilable preset. It deliberately does NOT re-run the
    candidate builder's *readiness/quality* gates (driver-measurement /
    summed-validation completeness, route width, subwoofer-block). It is not a
    production apply authorization; callers must validate the emitted graph.

    Returns ``(yaml, [])`` on success (writing to ``out_path`` when given, at the
    same group-readable mode the emitter uses), or ``(None, issues)`` when the
    evidence can no longer produce a baseline (preview not ready, preset
    uncompilable, no playback device) so the carrier can refuse with a typed
    reason instead of emitting a partial graph. It does NOT validate against
    CamillaDSP or touch the durable baseline state — the caller (the DSP-apply
    transaction) owns validation.
    """
    resolved_device, _device_source = resolve_active_playback_device(
        topology,
        playback_device=playback_device,
    )
    if not resolved_device:
        return None, [_issue(
            "blocker",
            "baseline_playback_device_missing",
            "active profile compiler needs an explicit active playback device",
        )]
    if not _crossover_preview_ready(crossover_preview):
        return None, [_issue(
            "blocker",
            "baseline_crossover_preview_not_ready",
            "save a fresh crossover preview before re-emitting the active baseline",
        )]
    preset, preset_issues, _gates = compile_preset_from_crossover_preview(
        topology,
        dict(crossover_preview),
    )
    blockers = [i for i in preset_issues if i.get("severity") == "blocker"]
    if preset is None or blockers:
        return None, (blockers or [_issue(
            "blocker",
            "baseline_preset_unavailable",
            "active profile compiler could not build speaker preset intent",
        )])
    corrections, _correction_issues, _correction_meta = _derive_corrections(
        preset,
        crossover_preview,
        measurements,
    )
    # BOTH DEVICE HALVES follow the resolved sink, exactly as the durable
    # builder and the applied-path seam already do. This call used to pass only
    # the playback device and let the emitter default the rest, which was
    # byte-identical ONLY while the resolver answered a non-ring device —
    # non-ring devices get the emitter's own defaults, so the two agreed by
    # coincidence rather than by derivation. Once the resolver answers the ring
    # the coincidence breaks and the omission emits the half-moved graph this
    # module warns about elsewhere: playback=ring with capture=the snd-aloop tap
    # fan-in stops feeding, which is silence with every daemon healthy, and
    # quiet — the plan compares capture CHANNELS (2 == 2) and the width gate
    # only holds ring-NAMED lanes to the wire.
    # Same refusal as the durable builder above, framed the same way: an
    # unresolvable ring wire reads identically wherever it surfaces, and the
    # parser's own sentence rides on the end as detail. A bare `str(exc)` here
    # was the odd one out of this code's four mint sites — it named no subject,
    # so the one code carried two different sentences depending on which path
    # reached it.
    try:
        devices = active_emit_devices(resolved_device, topology=topology)
    except ValueError as exc:
        return None, [_issue(
            "blocker",
            "ring_wire_declaration_invalid",
            f"this box declares a ring wire neither jasper-fanin nor JTS can "
            f"resolve, so there is no wire to emit against: {exc}",
        )]
    yaml = emit_active_speaker_baseline_config(
        preset,
        playback_device=resolved_device,
        capture_device=devices.capture_device,
        capture_format=devices.capture_format,
        playback_format=devices.playback_format,
        chunksize=devices.chunksize,
        target_level=devices.target_level,
        queuelimit=devices.queuelimit,
        enable_rate_adjust=devices.enable_rate_adjust,
        corrections=corrections,
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=output_trim_db,
        out_path=out_path,
        baseline_id=f"baseline-{_safe_id(topology.topology_id)}",
    )
    return yaml, []


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
    """Record one apply attempt (blocked, failed, or applied) into the bundle.

    ``jasper.active_speaker.bundles.record_apply`` is already fail-soft —
    this wrapper only resolves which bundle (via the comparison set's
    ``bundle_session_id`` — see ``jasper.active_speaker.bundles``) and moves
    the small JSON-only write off the event loop, since
    :func:`apply_baseline_profile` is async. Lane E's apply lifecycle events
    (``correction.crossover_apply_started`` / ``_succeeded`` /
    ``_rolled_back``) land at this same boundary.
    """

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


def persist_applied_baseline_profile(
    candidate: Mapping[str, Any],
    *,
    apply_state: Mapping[str, Any],
    state_path: str | Path | None = None,
    applied_at: str | None = None,
) -> dict[str, Any]:
    """Persist one already-read-back compiler candidate as the Layer-A SSOT.

    Always durable (fsync-before-rename, see
    :func:`jasper.atomic_io.atomic_write_text`): this write IS the apply
    seam — the moment a successfully loaded CamillaDSP graph becomes the
    record JTS trusts as "what's actually running" — so unlike the
    draft/preview writers upstream, there is no non-accept caller for this
    function to keep cheap for.
    """

    if (
        candidate.get("kind") != BASELINE_PROFILE_KIND
        or candidate.get("status") not in {"ready_to_apply", "applied"}
        or not isinstance(candidate.get("recomposition_snapshot"), Mapping)
        or apply_state.get("result") != "success"
        or baseline_candidate_fingerprint(candidate)
        != candidate.get("candidate_fingerprint")
    ):
        raise ValueError(
            "baseline candidate and successful apply proof are required"
        )
    # The commission is over: a baseline is what boots now, not the all-muted
    # staged anchor, so the startup-load hold that protected that anchor is
    # spent. Releasing it HERE rather than in a web handler is what makes the
    # release cover every way a baseline becomes applied — this function is the
    # apply seam all three callers funnel through (the apply path, the
    # commissioning apply, and the restore) — and the guard above has already
    # proved `apply_state["result"] == "success"`. Before the idempotent
    # early-return below as well as the write, because an already-applied
    # baseline leaves the hold just as stale. Best-effort by contract: a failed
    # clear never turns a successful apply into a failure, and the marker is
    # ephemeral (/run) either way.
    #
    # A lingering hold is inert today — `safe_graph_for_current_topology`'s rung
    # ALSO requires the current graph to classify as all-muted-active-startup,
    # which an applied baseline does not — so this is a latent-surprise and
    # doctor-honesty fix, not a live-bug fix. Observed on jts3 after a
    # save-and-apply that left the marker set.
    release_staged_startup_hold()
    target = baseline_profile_state_path(state_path)
    existing = _load_saved_state(target)
    candidate_identity = baseline_candidate_fingerprint(candidate)
    if (
        isinstance(existing, Mapping)
        and existing.get("status") == "applied"
        and baseline_candidate_fingerprint(existing) == candidate_identity
    ):
        return dict(existing)
    now = applied_at or _utc_now()
    applied = {
        **candidate,
        "status": "applied",
        "applied_at": now,
        "updated_at": now,
        "apply": dict(apply_state),
        "revalidation": {"required": False, "status": "not_required"},
    }
    applied.pop("applied_recomposition_profile", None)
    applied["permissions"] = dict(applied.get("permissions") or {})
    applied["permissions"]["may_apply"] = False
    atomic_write_text(
        target,
        json.dumps(applied, indent=2, sort_keys=True) + "\n",
        mode=0o640,
        group_from_parent=True,
        durable=True,
    )
    return applied


# Newest-by-mtime source-fingerprinted candidate siblings to keep around a
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
    also_protect: Sequence[str | Path] = (),
) -> None:
    """Publish a just-applied candidate's bytes as the canonical config file.

    ``build_baseline_profile_candidate`` never writes ``baseline_config_path()``
    directly (issue #1666) -- every ``write=True`` candidate lands on its own
    source-fingerprinted sibling, so a candidate that fails validation or
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
    apply. jasper-doctor's baseline-canonical check surfaces a stale or
    missing canonical file as a WARN, never a service disruption.
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
        atomic_write_text(canonical, text, mode=0o640, group_from_parent=True)
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
    _prune_baseline_candidate_siblings(
        canonical, protect=applied_path, also_protect=also_protect
    )


def _prune_baseline_candidate_siblings(
    canonical: Path, *, protect: Path, also_protect: Sequence[str | Path] = (),
) -> None:
    """Bound unconditional candidate-sibling growth to the newest K by mtime.

    Deletes ``<stem>_candidate_*<suffix>`` files beside ``canonical`` beyond
    the newest :data:`_MAX_BASELINE_CANDIDATE_FILES` by mtime. Never deletes a
    PROTECTED sibling — ``protect`` (the candidate
    :func:`promote_applied_baseline_candidate` just promoted, always the new
    applied anchor) or any path in ``also_protect`` — or the canonical file
    itself (which never matches the glob below; it carries no ``_candidate_``
    suffix). ``also_protect`` carries the Undo target the apply just stashed as
    ``pre_apply_profile`` (#1605): a source-fingerprinted sibling like any other
    that ``handle_v2_restore`` reloads, and would otherwise be prunable by
    mtime once ~K newer candidates accumulate — silently breaking Undo. A
    protected sibling costs one of the K slots rather than adding to K, so the
    on-disk total stays at K.

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
        protected = {protect, *(Path(p) for p in also_protect if p)}
        siblings = list(canonical.parent.glob(pattern))
        prunable = sorted(
            (p for p in siblings if p not in protected),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        # Protected siblings always survive; keep enough of the REMAINING files
        # that protected-present + kept == K total (never negative) -- with no
        # also_protect this is exactly the prior "keep newest (K - 1) + protect
        # = K" behaviour.
        protected_present = sum(1 for p in siblings if p in protected)
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
    measured_candidate: "MeasuredElectricalCandidate | MeasuredCrossoverCandidate | None" = (
        None
    ),
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
    """Serialize candidate proof, compile, load, confirmation, and rollback.

    ``measured_candidate`` is optional and defaults to ``None`` so every
    existing caller is byte-identical; passing one threads
    :func:`build_baseline_profile_candidate`'s ``measured_candidate`` seam
    through this same atomic apply-with-rollback transaction (see
    ``jasper.active_speaker.measured_crossover_candidate`` for the v2 measured
    candidate that carries optional delay/polarity).
    """

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
            measured_candidate=measured_candidate,
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
    measured_candidate: "MeasuredElectricalCandidate | MeasuredCrossoverCandidate | None" = (
        None
    ),
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Apply the saved baseline candidate through the shared DSP transaction.

    ``capture_device`` is threaded to :func:`build_baseline_profile_candidate`
    so the reconciler can apply a follower's grouping-ring baseline. It
    stays ``None`` by default rather than materialising the tap here, so the
    apply path reaches that function's one device derivation instead of pinning
    the capture half against a sink it does not look at.

    ``driver_domain`` + ``program_channel`` switch the emit to a wireless active
    follower's driver-domain-only Layer-A graph (Slice 2 emitter). The optional
    ``driver_domain_pair_trim_db`` follows the same parameter on
    :func:`build_baseline_profile_candidate` so direct apply callers cannot drift
    from the candidate builder. The follower branch of the multiroom reconciler
    passes follower-specific ``state_path`` / ``config_path`` alongside these so
    the solo baseline state is not overwritten.

    ``measured_candidate`` forwards unchanged to
    :func:`build_baseline_profile_candidate`; ``None`` (the default) keeps
    every existing caller byte-identical.
    """

    state_target = baseline_profile_state_path(state_path)

    def build_candidate(
        *,
        write: bool,
        bass_extension_profile: BassExtensionProfile | None = None,
    ) -> dict[str, Any]:
        return build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=write,
            state_path=state_target,
            config_path=config_path,
            capture_device=capture_device,
            capture_format=capture_format,
            driver_domain=driver_domain,
            program_channel=program_channel,
            driver_domain_pair_trim_db=driver_domain_pair_trim_db,
            tuning_owner=tuning_owner,
            preserved_applied_profile=preserved_applied_profile,
            measured_candidate=measured_candidate,
            validate=validate,
            bass_extension_profile=bass_extension_profile,
        )

    def matches_expected(candidate: Mapping[str, Any]) -> bool:
        actual = baseline_candidate_fingerprint(candidate)
        return bool(
            expected_candidate_fingerprint
            and actual
            and expected_candidate_fingerprint == actual
        )

    async def refuse_stale(candidate: Mapping[str, Any]) -> dict[str, Any]:
        refused = dict(candidate)
        refused["permissions"] = dict(refused.get("permissions") or {})
        refused["permissions"]["may_apply"] = False
        refused["issues"] = [
            *refused.get("issues", []),
            _issue(
                "blocker",
                "baseline_candidate_fingerprint_mismatch",
                (
                    "the crossover candidate changed after review; refresh and "
                    "review the current candidate before applying"
                ),
            ),
        ]
        return {
            "status": "blocked",
            "profile": refused,
            "apply": None,
            "issues": refused["issues"],
        }

    reviewed_candidate = build_candidate(write=False)
    candidate_bass_emission_profile = None
    candidate_bass_proof_profile = None
    if not driver_domain:
        bass_evaluation = evaluate_bass_extension_profile(
            topology=topology,
            applied_baseline_state=reviewed_candidate,
        )
        candidate_bass_proof_profile = bass_evaluation.profile
        if bass_evaluation.status == "accepted":
            candidate_bass_emission_profile = bass_evaluation.profile

    if expected_candidate_fingerprint is not None:
        if not matches_expected(reviewed_candidate):
            return await refuse_stale(reviewed_candidate)

    candidate = build_candidate(
        write=True,
        bass_extension_profile=candidate_bass_emission_profile,
    )
    if expected_candidate_fingerprint is not None and not matches_expected(candidate):
        return await refuse_stale(candidate)
    snapshot_state = crossover_snapshot_state(
        candidate,
        expected_topology_id=topology.topology_id,
        expected_topology_fingerprint=str(
            (candidate.get("source") or {}).get("topology_fingerprint") or ""
        ),
        expected_domain="driver" if driver_domain else "full",
        require_applied=False,
    )
    if candidate.get("permissions", {}).get("may_apply") and not snapshot_state["valid"]:
        candidate["status"] = "compiled_apply_blocked"
        candidate["permissions"]["may_apply"] = False
        candidate["issues"] = [
            *candidate.get("issues", []),
            _issue(
                "blocker",
                str(snapshot_state["reason"]),
                str(snapshot_state["detail"]),
            ),
        ]
        atomic_write_text(
            state_target,
            json.dumps(candidate, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
    if not driver_domain and candidate.get("permissions", {}).get("may_apply"):
        from jasper.active_speaker.runtime_contract import (
            GRAPH_APPROVED_ACTIVE_RUNTIME,
            classify_bass_extension_graph,
        )

        try:
            candidate_graph_text = Path(
                str((candidate.get("config") or {}).get("path") or "")
            ).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            graph_proof = None
            proof_detail = f"the emitted active graph is unreadable: {type(exc).__name__}"
        else:
            graph_proof = classify_bass_extension_graph(
                topology,
                evidence_source="desired",
                graph_text=candidate_graph_text,
                applied_baseline_state=candidate,
                desired_profile=candidate_bass_proof_profile,
            )
            proof_detail = (
                graph_proof.issues[0].get("message")
                if graph_proof.issues
                else "the emitted active graph failed whole-graph proof"
            )
        if (
            graph_proof is None
            or not graph_proof.allowed
            or graph_proof.classification != GRAPH_APPROVED_ACTIVE_RUNTIME
        ):
            candidate["status"] = "compiled_apply_blocked"
            candidate["permissions"]["may_apply"] = False
            candidate["issues"] = [
                *candidate.get("issues", []),
                _issue(
                    "blocker",
                    "baseline_graph_safety_proof_failed",
                    proof_detail,
                ),
            ]
            atomic_write_text(
                state_target,
                json.dumps(candidate, indent=2, sort_keys=True) + "\n",
                mode=0o640,
                group_from_parent=True,
            )
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

    graph_fingerprint = (candidate.get("source") or {}).get("fingerprint")
    candidate_identity = candidate.get("candidate_fingerprint")
    log_event(
        logger,
        "correction.crossover_apply_started",
        config_path=str((candidate.get("config") or {}).get("path") or ""),
        baseline_id=candidate.get("baseline_id"),
        tuning_owner=tuning_owner,
        topology_id=topology.topology_id,
        graph_fingerprint=graph_fingerprint,
        candidate_fingerprint=candidate_identity,
    )
    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_baseline_apply",
            candidate_path=str((candidate.get("config") or {}).get("path")),
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            acquire_lock=False,
            expected_candidate_sha256=str(
                (candidate.get("config") or {}).get("sha256") or ""
            ),
            validate=validate,
        )
    except DspApplyError as exc:
        failed = {
            **candidate,
            "status": "apply_failed",
            "apply": exc.state.to_dict(),
            "updated_at": _utc_now(),
            "issues": [
                *candidate.get("issues", []),
                _issue(
                    "blocker",
                    "baseline_profile_apply_failed",
                    str(exc),
                ),
            ],
        }
        atomic_write_text(
            state_target,
            json.dumps(failed, indent=2, sort_keys=True) + "\n",
            mode=0o640,
            group_from_parent=True,
        )
        # Spec-pinned failure event name: apply_rolled_back covers every
        # DspApplyError, whether or not the underlying rollback itself
        # succeeded -- see docs/active-crossover-information-design.md
        # "Structured events" (there is no separate apply_failed event).
        log_event(
            logger,
            "correction.crossover_apply_rolled_back",
            baseline_id=candidate.get("baseline_id"),
            topology_id=topology.topology_id,
            graph_fingerprint=graph_fingerprint,
            reason=str(exc),
            rollback_attempted=exc.state.rollback_attempted,
            rollback_succeeded=exc.state.rollback_succeeded,
            rollback_error=exc.state.rollback_error,
        )
        await _record_apply_outcome_into_bundle(
            measurements,
            candidate=failed,
            apply_state=exc.state.to_dict(),
            rollback_target=(
                {"config_path": exc.state.prior_config_path}
                if exc.state.prior_config_path
                else None
            ),
        )
        return {
            "status": "apply_failed",
            "profile": failed,
            "apply": exc.state.to_dict(),
            "issues": failed["issues"],
        }

    applied = persist_applied_baseline_profile(
        candidate,
        apply_state=apply_state.to_dict(),
        state_path=state_target,
    )
    # Protect the Undo target (#1605): the profile this apply replaced is what
    # v2 Undo reloads. handle_v2_restore restores pre_apply_profile.config.path,
    # which is exactly this candidate's frozen applied_recomposition_profile
    # (persist_applied_baseline_profile popped it from the applied copy, not
    # from ``candidate``). Pass it so the mtime prune can't evict it.
    _prior = candidate.get("applied_recomposition_profile")
    _undo_target = (
        str((_prior.get("config") or {}).get("path") or "")
        if isinstance(_prior, Mapping)
        else ""
    )
    promote_applied_baseline_candidate(
        applied,
        config_path=config_path,
        also_protect=(_undo_target,) if _undo_target else (),
    )
    log_event(
        logger,
        "correction.crossover_apply_succeeded",
        baseline_id=candidate.get("baseline_id"),
        topology_id=topology.topology_id,
        tuning_owner=tuning_owner,
        graph_fingerprint=graph_fingerprint,
        # Apply loads this exact frozen candidate without transforming it, so
        # candidate and applied identities are equal. ``graph_fingerprint``
        # remains the separate source/cache context identifier.
        candidate_fingerprint=candidate_identity,
        applied_fingerprint=candidate_identity,
        applied_at=applied["applied_at"],
    )
    # Layer-1a driver linearization observability (#1668 PR-D review SF3):
    # one line per successful apply recording what linearization (if any)
    # reached hardware -- per-role filter counts, or none=true when the
    # candidate carried no linearization stage. Read from the candidate's
    # own top-level "linearization" mirror (see
    # build_baseline_profile_candidate), the same reduced
    # {role: [filter_dict, ...]} shape the emitter consumed.
    applied_linearization = candidate.get("linearization")
    if not isinstance(applied_linearization, Mapping):
        applied_linearization = {}
    log_event(
        logger,
        "dsp.baseline_linearization",
        baseline_id=candidate.get("baseline_id"),
        topology_id=topology.topology_id,
        **(
            {role: len(filters) for role, filters in applied_linearization.items()}
            if applied_linearization
            else {"none": True}
        ),
    )
    await _record_apply_outcome_into_bundle(
        measurements,
        candidate=applied,
        apply_state=apply_state.to_dict(),
        rollback_target=(
            {"config_path": apply_state.prior_config_path}
            if apply_state.prior_config_path
            else None
        ),
    )
    return {
        "status": "applied",
        "profile": applied,
        "apply": apply_state.to_dict(),
        "issues": applied.get("issues", []),
    }


async def restore_applied_baseline_profile(
    retained_profile: Mapping[str, Any],
    *,
    load_config: Callable[[str], Awaitable[bool]],
    get_current_config_path: Callable[[], Awaitable[str | None]] | None = None,
    state_path: str | Path | None = None,
    config_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Restore one previously-applied baseline profile snapshot (the v2 Undo).

    ``retained_profile`` is the frozen ``applied_recomposition_profile`` a
    later apply preserved before overwriting the Layer-A SSOT (see
    :func:`build_baseline_profile_candidate`'s ``finalize`` /
    ``_frozen_applied_profile``). Its ``config.path`` points at that prior
    profile's own already-compiled, already-validated YAML, and this reloads
    THAT exact file — never recomposed — through the same atomic
    validate-load-confirm-rollback transaction
    (:func:`jasper.dsp_apply.apply_dsp_config`)
    :func:`apply_baseline_profile` rides, then persists it back as the
    applied SSOT via :func:`persist_applied_baseline_profile`.

    **That path is not the graph's identity, so the file is not immutable
    (#2519).** ``build_baseline_profile_candidate`` names every solo candidate
    from its SOURCE fingerprint, and :func:`_source_payload` — which owns the
    list of what that fingerprint covers, and is the only place it should be
    read — states the consequence outright: several inputs that DO reach the
    emitted bytes are excluded from it, so "two candidates differing only in
    one of those land on the SAME filename carrying DIFFERENT bytes."

    A different-fingerprint candidate therefore cannot touch this file, and the
    mtime pruner cannot evict it (``also_protect``) — but a same-fingerprint
    recompile can rewrite it underneath the anchor. That is why the integrity
    check below exists and what ``restore_target_changed`` reports: a live
    condition, not defensive scaffolding.

    Never raises for an ordinary refusal: returns a ``status`` in
    ``{"restored", "blocked", "restore_failed"}`` the caller maps to an HTTP
    code, mirroring :func:`apply_baseline_profile`'s own contract.
    """
    if (
        retained_profile.get("kind") != BASELINE_PROFILE_KIND
        or retained_profile.get("status") != "applied"
        or not isinstance(retained_profile.get("recomposition_snapshot"), Mapping)
        or baseline_candidate_fingerprint(retained_profile)
        != retained_profile.get("candidate_fingerprint")
    ):
        return {
            "status": "blocked",
            "issues": [_issue(
                "blocker",
                "restore_target_invalid",
                "the previous crossover profile is not a valid applied snapshot",
            )],
        }
    config = retained_profile.get("config")
    candidate_path = (
        str(config.get("path") or "") if isinstance(config, Mapping) else ""
    )
    candidate_sha256 = (
        str(config.get("sha256") or "") if isinstance(config, Mapping) else ""
    )
    if not candidate_path or not candidate_sha256 or not Path(candidate_path).is_file():
        return {
            "status": "blocked",
            "issues": [_issue(
                "blocker",
                "restore_target_missing",
                "the previous crossover configuration could not be found on "
                "disk; a full remeasure is required",
            )],
        }
    # The Undo anchor's integrity is THIS function's to prove, and it is a
    # different question from the one :func:`~jasper.dsp_apply.apply_dsp_config`
    # asks (#2519). That proof is a validate-to-load race check over a candidate
    # compiled seconds earlier; the digest above was recorded by the apply that
    # wrote this file, which can be days back. Handing the older digest to the
    # race check made every way of failing it — including an unreadable file —
    # report "DSP candidate changed after validation and before load", the
    # sentence a jts3 Undo refused under twice nine minutes apart.
    #
    # So the anchor is verified here, under names that say what is wrong with
    # it, and the digest THIS call computed is what threads into the apply
    # transaction — which then proves only what it can honestly prove: that the
    # bytes did not move between validation and load.
    retained_digest = config_file_sha256(candidate_path)
    if retained_digest is None:
        return {
            "status": "blocked",
            "issues": [_issue(
                "blocker",
                "restore_target_unreadable",
                "the previous crossover configuration is on disk but could not "
                "be read; check its permissions, then try again",
            )],
        }
    if retained_digest != candidate_sha256:
        return {
            "status": "blocked",
            "issues": [_issue(
                "blocker",
                "restore_target_changed",
                "the previous crossover configuration file no longer matches "
                "what was applied from it; a full remeasure is required",
            )],
        }

    async with dsp_writer_lock(
        baseline_config_path(config_path).parent,
        source="active_speaker_baseline_restore",
    ):
        try:
            apply_state = await apply_dsp_config(
                source="active_speaker_baseline_restore",
                candidate_path=candidate_path,
                load_config=load_config,
                get_current_config_path=get_current_config_path,
                acquire_lock=False,
                expected_candidate_sha256=retained_digest,
                validate=validate,
            )
        except DspApplyError as exc:
            return {
                "status": "restore_failed",
                "issues": [_issue("blocker", "restore_apply_failed", str(exc))],
            }
        restored = persist_applied_baseline_profile(
            dict(retained_profile),
            apply_state=apply_state.to_dict(),
            state_path=state_path,
        )
        promote_applied_baseline_candidate(restored, config_path=config_path)

    return {"status": "restored", "profile": restored, "apply": apply_state.to_dict()}
