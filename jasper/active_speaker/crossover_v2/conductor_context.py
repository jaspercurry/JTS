# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve crossover session inputs before capture starts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, Mapping, TypeVar, overload

from jasper.platform.log_event import log_event
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.profile import DRIVER_ROLES_BY_WAY, required_driver_roles
from jasper.active_speaker.design_draft import declared_driver_spacing_m
from jasper.active_speaker.excitation_safety_plan import (
    ExcitationSafetyPlanError,
    ExcitationSafetyPlanRefusal,
    driver_cap_dbfs,
)

from .refusal_copy import (
    REASON_DRIVER_SENSITIVITY_UNDECLARED,
    REASON_MEASUREMENT_TARGETS_MISSING,
    REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID,
    REASON_REGISTRY,
    REASON_SPEAKER_SHAPE_UNSUPPORTED,
    REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS,
    CrossoverV2Refused,
    driver_sensitivity_undeclared_message,
)
from jasper.audio_routes.output_topology import topology_is_subless_passive_mains
from jasper.platform.speaker_layout import measurement_target_id
from jasper.active_speaker._common import BASELINE_TOPOLOGY_CHANGED
from jasper.active_speaker.design_inputs import declared_by_target
from jasper.active_speaker.playback_route import resolve_active_playback_device
from jasper.audio_measurement.program import RoleBand

if TYPE_CHECKING:
    from jasper.audio_measurement.program import FrequencyBand

__all__ = [
    "V2ConductorContext",
    "ensure_crossover_preview_ready",
    "measurement_role_channels",
    "published_driver_caps",
    "resolve_conductor_context",
]

logger = logging.getLogger(__name__)
_Level = TypeVar("_Level", bound=float | None)
_BOX_NOT_READY = "measure_box_not_ready"


def driver_spacing_source(draft: Mapping[str, Any]) -> str:
    return "unknown" if declared_driver_spacing_m(draft) is None else "declared"


@dataclass(frozen=True)
class V2ConductorContext(Generic[_Level]):
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    #: The declared crossover corner, or ``None`` on a 1-way main, which has
    #: none. Never a stand-in figure — see ``resolve_conductor_context``.
    fc_hz: float | None
    driver_caps_dbfs: dict[str, float]
    # Per-target longest admissible ONE sweep, in seconds, from the SAME owner
    # the admission gate reads (``effective_sweep_duration_limit_s``), so a
    # MEASURE segment cannot overshoot the ceiling admission judges it against.
    driver_sweep_duration_limits_s: dict[str, float]
    #: Measurement target id (``measurement_target_id``) -> target fingerprint,
    #: one entry per physical driver output. The EMITTER map below
    #: (:attr:`role_channels`) stays many-to-one: a role's channel reaches its
    #: rear output too. These two never collapse into one.
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: _Level
    #: The declared woofer<->tweeter acoustic-center spacing, in metres,
    #: or ``None`` when undeclared -- see ``design_draft.declared_driver_spacing_m``,
    #: the ONE owner of this fact. ``MeasurementGeometry.parallax_us`` treats
    #: ``None``/``0.0`` identically (no correction), so this is never a gate.
    driver_spacing_m: float | None
    #: "declared" when :attr:`driver_spacing_m` came from the operator's own
    #: ``manual_settings.driver_spacing_mm``, "unknown" when the geometry
    #: folds an absent declaration into 0.0 (no correction). Disclosure only:
    #: nothing branches on it, it exists so "unknown" survives past the
    #: ``0.0`` fold instead of reading as a declared zero spacing.
    driver_spacing_source: str
    topology: Any
    playback_device: str
    role_channels: dict[str, int]
    sound_design_revision: int
    #: Per-target permitted excitation band, from the resolver the caps come
    #: from; keyed like :attr:`role_targets`, so a rear woofer has its own.
    driver_bands: dict[str, Any] = field(default_factory=dict)
    # Per-target declared radiating diameter in mm, the ka/beaming prior:
    # disclosure, never a bound. A target absent here gets no beaming prior,
    # disclosed as such rather than an assumed diameter.
    radiating_diameter_mm_by_target: dict[str, float] = field(default_factory=dict)
    # Per-role confirmed ``measurement_band_hz`` in Hz — the contract-derived
    # echo/null analysis band the cloud-group pipeline reads in place of
    # DEFAULT_ECHO_BAND_HZ's flat constant. A role missing here degrades to
    # that module default, never a refused session: a declared-metadata gap is
    # not a reason to block a measurement the household is entitled to run.
    measurement_band_hz_by_role: Mapping[str, tuple[float, float]] = field(
        default_factory=dict
    )

    def declared_band(self, role: str) -> FrequencyBand | None:
        """This role's declared ``FrequencyBand``, or ``None``."""
        return next(
            (entry.band for entry in self.roles_bands if entry.role == role),
            None,
        )


def measurement_role_channels(preset: Any) -> dict[str, int]:
    return {role: channel for channel, role in enumerate(required_driver_roles(preset.way_count))}


def ensure_crossover_preview_ready(design_draft: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Refuse incomplete declarations before capture preset resolution."""
    from jasper.active_speaker.design_draft import load_design_draft  # lazy: reader boundary is patched by conductor tests

    preview = build_crossover_preview(load_design_draft() if design_draft is None else design_draft)
    if preview.get("status") != "ready_for_protected_staging":
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in (preview.get("issues") or [])
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        raise CrossoverV2Refused(
            "the crossover preview is not ready for measurement; finish "
            "speaker setup at /sound/ first"
            + (": " + "; ".join(messages[:2]) if messages else ""),
            code=_BOX_NOT_READY,
        )
    return preview


def _cap_refused(target_id: str, exc: ValueError) -> CrossoverV2Refused:
    """The door's refusal for a driver whose cap cannot be resolved (ADR-0382)."""
    if isinstance(exc, ExcitationSafetyPlanError) and exc.code == ExcitationSafetyPlanRefusal.SENSITIVITY_UNDECLARED.value:
        return CrossoverV2Refused(
            driver_sensitivity_undeclared_message(exc.detail["undeclared_roles"], exc.detail["disagreeing_roles"]),
            code=REASON_DRIVER_SENSITIVITY_UNDECLARED,
        )
    return CrossoverV2Refused(f"the {target_id}'s safe excitation limits could not be resolved", code=_BOX_NOT_READY)


def published_driver_caps(
    safety_profile: Mapping[str, Any], target_fingerprints: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Each driver's program-path ``cap_dbfs`` and ``cap_source``, or the registry code refusing it."""
    caps: dict[str, dict[str, Any]] = {}
    for target_id, fingerprint in target_fingerprints.items():
        try:
            cap, source = driver_cap_dbfs(safety_profile, fingerprint, program_admission=True)
        except (ExcitationSafetyPlanError, ValueError) as exc:
            caps[target_id] = {"cap_dbfs": None, "cap_source": None, "reason": _cap_refused(target_id, exc).code}
        else:
            caps[target_id] = {"cap_dbfs": cap, "cap_source": source}
    return caps


@overload
def resolve_conductor_context(
    status: Mapping[str, Any], *, topology: Any = None, require_banked_level: Literal[True] = True,
) -> V2ConductorContext[float]: ...


@overload
def resolve_conductor_context(
    status: Mapping[str, Any], *, topology: Any = None, require_banked_level: Literal[False],
) -> V2ConductorContext[None]: ...


def resolve_conductor_context(
    status: Mapping[str, Any], *, topology: Any = None, require_banked_level: bool = True,
) -> V2ConductorContext:
    """Resolve confirmed limits before capture starts, not at play time (#1821).

    Leveling resolves the speaker inputs before a session level can be banked.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset, resolve_commission_preset  # lazy: test_correction_crossover_v2_conductor_context patches commission_wiring
    from jasper.active_speaker.design_draft import load_design_draft  # lazy: reader boundary is patched by conductor tests
    from jasper.active_speaker.excitation_safety_plan import (  # lazy: test_correction_crossover_v2_conductor_context patches excitation_safety_plan
        require_driver_measurement_inputs,
        effective_sweep_duration_limit_s,
        resolve_driver_excitation_ceilings,
        resolve_driver_measurement_band_hz,
    )
    from jasper.active_speaker.session_volume_plan import (  # lazy: test_correction_crossover_v2_conductor_context patches session_volume_plan
        LevelUnresolved, session_measurement_volume_db,
    )
    from jasper.audio_routes.output_topology_store import load_output_topology  # lazy: test_correction_crossover_v2_conductor_context pins the store lookup

    topology = topology if topology is not None else load_output_topology()
    # A subless passive main has no active crossover, so the gates below — all
    # asking whether an ACTIVE one is commissioned — are not questions about it.
    passive_mains = topology_is_subless_passive_mains(topology)
    draft = None
    preview = None
    if not passive_mains:
        if not status.get("active"):
            raise CrossoverV2Refused(
                "this speaker has no active crossover to measure", code=_BOX_NOT_READY,
            )
        setup = status.get("setup") or {}
        if any(
            isinstance(issue, Mapping)
            and issue.get("code") == BASELINE_TOPOLOGY_CHANGED
            for issue in (setup.get("issues") or ())
        ):
            log_event(
                logger,
                "correction.crossover_v2_baseline_topology_stale",
                level=logging.WARNING,
                code=BASELINE_TOPOLOGY_CHANGED,
            )
        draft = load_design_draft(topology=topology)
        preview = ensure_crossover_preview_ready(draft)
    preset = (resolve_commission_preset(topology, crossover_preview=preview)
              if preview is not None else resolve_capture_preset(topology))
    if preset.way_count not in (1, 2):
        # Remove once the measurement programs support three driver roles (#5396).
        code = (REASON_WALK_LAYOUT_UNSUPPORTED_FOR_PER_DRIVER_PROGRAMS
                if set(preset.drivers) == set(DRIVER_ROLES_BY_WAY[3]) else REASON_SPEAKER_SHAPE_UNSUPPORTED)
        raise CrossoverV2Refused(REASON_REGISTRY[code].message, code=code)
    roles = required_driver_roles(preset.way_count)
    if draft is None:
        draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    try:
        require_driver_measurement_inputs(safety_profile or {})
    except ExcitationSafetyPlanError as exc:
        code = REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID
        log_event(
            logger, "correction.crossover_v2_measurement_inputs_invalid",
            level=logging.WARNING, gate="session_open", code=code,
            issues=",".join(issue["code"] for issue in (safety_profile or {}).get("issues", [])),
        )
        raise CrossoverV2Refused(REASON_REGISTRY[code].message, code=code) from exc
    assert isinstance(safety_profile, Mapping)
    targets_raw = status.get("targets")
    drivers = (
        targets_raw.get("drivers") if isinstance(targets_raw, Mapping) else None
    ) or []
    # One entry per PHYSICAL target (``measurement_target_id``), never one per
    # role: a rear woofer is a third target of a two-way speaker, and collapsing
    # it onto its role hides it from the play-door admission map.
    role_targets: dict[str, str] = {}
    primary_roles: set[str] = set()
    for target in drivers:
        if isinstance(target, Mapping):
            role = str(target.get("role") or "").lower()
            variant = str(target.get("output_variant") or "primary")
            fingerprint = str(target.get("target_fingerprint") or "")
            if role and fingerprint:
                role_targets[measurement_target_id(role, variant)] = fingerprint
                if variant == "primary":
                    primary_roles.add(role)
    if primary_roles != set(roles):
        # The registry copy cannot carry the roles; the journal line can.
        log_event(
            logger,
            "correction.crossover_v2_measurement_targets_missing",
            level=logging.WARNING,
            gate="session_open",
            code=REASON_MEASUREMENT_TARGETS_MISSING,
            declared=",".join(roles),
            found=",".join(sorted(role_targets)),
        )
        raise CrossoverV2Refused(
            REASON_REGISTRY[REASON_MEASUREMENT_TARGETS_MISSING].message,
            code=REASON_MEASUREMENT_TARGETS_MISSING,
        )
    roles_bands = []
    caps: dict[str, float] = {}
    bands = {}
    sweep_duration_limits_s: dict[str, float] = {}
    for target_id, fingerprint in role_targets.items():
        try:
            bands[target_id], caps[target_id] = resolve_driver_excitation_ceilings(
                safety_profile, fingerprint, program_admission=True,
            )
            sweep_duration_limits_s[target_id] = effective_sweep_duration_limit_s(
                safety_profile, fingerprint,
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise _cap_refused(target_id, exc) from exc
    measurement_bands: dict[str, tuple[float, float]] = {}
    for channel, role in enumerate(roles):
        # Flat-linearization plan PR-4: this role's confirmed measurement band.
        # Its OWN except arm: a declared-metadata gap on this optional surface
        # must never refuse a session.
        try:
            measurement_bands[role] = resolve_driver_measurement_band_hz(
                safety_profile, role_targets[role],
            )
        except (ExcitationSafetyPlanError, ValueError):
            pass
        roles_bands.append(RoleBand(role, channel, bands[role]))
    # ``None`` is "this speaker declares no corner", never a corner at zero —
    # see ``crossover_v2.priors`` and ``build_verify_program``.
    fc_hz = (
        float(preset.crossover_regions[0].fc_hz)
        if preset.crossover_regions else None
    )
    session_volume_db = None
    if require_banked_level:
        try:
            session_volume_db = session_measurement_volume_db(
                safety_profile, [role_targets[role] for role in roles],
            )
        except LevelUnresolved as exc:
            raise CrossoverV2Refused(exc.detail, code=exc.reason) from exc
    playback_device, _playback_device_source = resolve_active_playback_device(
        topology
    )
    playback_device = str(playback_device or "")
    if not playback_device:
        raise CrossoverV2Refused(
            "the active output device is not declared; finish speaker setup", code=_BOX_NOT_READY,
        )
    driver_spacing_m = declared_driver_spacing_m(draft)
    return V2ConductorContext(
        preset=preset,
        roles_bands=tuple(roles_bands),
        fc_hz=fc_hz,
        driver_caps_dbfs=caps,
        driver_bands=bands,
        driver_sweep_duration_limits_s=sweep_duration_limits_s,
        role_targets=role_targets,
        safety_profile=safety_profile,
        session_volume_db=session_volume_db,
        # #1864: threaded from the declaration (design_draft.py), never a
        # default. ``None`` when undeclared -- a missing parallax correction
        # is SELF-CANCELLING at the mic position (the same geometric excess is
        # baked into both MEASURE and VERIFY), so VERIFY passes while the
        # LISTENING POSITION still carries the full error (~23° at 2 kHz for
        # 15 cm spacing measured at 1 m). The correction only ever claims the
        # on-axis-tweeter §5.2 aim assumption -- it is not a toe-in or
        # vertical-offset model.
        driver_spacing_m=driver_spacing_m,
        driver_spacing_source=driver_spacing_source(draft),
        topology=topology,
        playback_device=playback_device,
        role_channels=measurement_role_channels(preset),
        sound_design_revision=int(draft.get("revision", 0)),
        radiating_diameter_mm_by_target=declared_by_target(draft, "radiating_diameter_mm"),
        measurement_band_hz_by_role=measurement_bands,
    )
