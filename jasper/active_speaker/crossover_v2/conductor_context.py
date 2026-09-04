# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The inputs a crossover-v2 conductor session opens with, and the gate that
resolves them.

:func:`resolve_conductor_context` is the fail-closed session-open predicate:
ONE derivation of the preset, the per-role bands/caps/duration limits, the
measurement targets, the session volume and the playback device, from live
status plus the declared topology. Its front ends — the ``/correction/``
wizard, the null door and the measurement CLI — consume it rather than
re-deriving any of it, which is why it sits here rather than in the wizard.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from jasper.log_event import log_event

from .refusal_copy import (
    REASON_MEASUREMENT_TARGETS_MISSING,
    REASON_PROGRAM_PROFILE_INCOMPLETE,
    REASON_PROGRAM_PROFILE_MISSING,
    REASON_PROGRAM_PROFILE_NOT_CONFIRMED,
    REASON_REGISTRY,
    REASON_SPEAKER_SHAPE_UNSUPPORTED,
    CrossoverV2Refused,
)

__all__ = [
    "V2ConductorContext",
    "conductor_status",
    "ensure_crossover_preview_ready",
    "profile_refusal_code",
    "resolve_conductor_context",
]

logger = logging.getLogger(__name__)


def conductor_status() -> dict[str, Any]:
    """The live status :func:`resolve_conductor_context` reads.

    Its three keys — ``targets``, ``setup`` and ``active`` — derived once here,
    so the wizard's page payload
    (``jasper.web.correction_crossover_backend.status_payload``, which extends
    this) and a CLI door cannot disagree about whether a box may be measured.

    ``active`` is read off the SUMMED targets alone, which only
    ``active_2_way`` / ``active_3_way`` groups have: a subless
    ``full_range_passive`` speaker carries a driver target too, so counting
    those would flip the flag wrongly.
    """
    from jasper.active_speaker import web_measurement
    from jasper.active_speaker.setup_status import read_active_speaker_setup_status

    payload = web_measurement.status_payload()
    targets = payload.get("targets")
    payload["active"] = bool(
        targets.get("summed") if isinstance(targets, Mapping) else None
    )
    payload["setup"] = read_active_speaker_setup_status()
    return payload


@dataclass(frozen=True)
class V2ConductorContext:
    """Everything the production conductor needs, resolved from live status."""

    preset: Any
    roles_bands: tuple
    #: The declared crossover corner, or ``None`` on a 1-way main, which has
    #: none. Never a stand-in figure — see ``resolve_conductor_context``.
    fc_hz: float | None
    driver_caps_dbfs: dict[str, float]
    # Per-role longest admissible ONE sweep, in seconds, from the SAME owner
    # the admission gate reads (``effective_sweep_duration_limit_s``), so a
    # MEASURE segment cannot overshoot the ceiling admission judges it against.
    driver_sweep_duration_limits_s: dict[str, float]
    role_targets: dict[str, str]
    safety_profile: Mapping[str, Any]
    session_volume_db: float
    driver_spacing_m: float
    topology: Any
    playback_device: str
    role_channels: dict[str, int]
    sound_design_revision: int
    # Per-role declared EFFECTIVE sensitivities in dB SPL/2.83V @1m (the
    # datasheet figure with any declared in-line pad folded in), from the
    # design draft, which owns that fact. Threaded into every cap resolution
    # AND the play-time readmission so the composed levels and the admission
    # gate cannot disagree about a derived HF ceiling.
    declared_sensitivities: dict[str, float] = field(default_factory=dict)
    # Per-role declared driver technology class, feeding the conductor's
    # Layer-1a linearization fit (``linearization_envelope.compose_envelope``'s
    # class_prior_limit term). A role absent here fits under the conservative
    # "unknown" class default.
    driver_class_by_role: dict[str, str] = field(default_factory=dict)
    # Per-role declared effective radiating diameter in mm, the ka/beaming
    # prior, which is DISCLOSURE and never a bound. It reaches the conductor by
    # the SAME draft path ``driver_class_by_role`` takes. A role absent here
    # gets no beaming prior, disclosed as such rather than an assumed diameter.
    radiating_diameter_mm_by_role: dict[str, float] = field(default_factory=dict)
    # Per-role confirmed ``measurement_band_hz`` in Hz — the contract-derived
    # echo/null analysis band the cloud-group pipeline reads in place of
    # DEFAULT_ECHO_BAND_HZ's flat constant. A role missing here degrades to
    # that module default, never a refused session: a declared-metadata gap is
    # not a reason to block a measurement the household is entitled to run.
    measurement_band_hz_by_role: Mapping[str, tuple[float, float]] = field(
        default_factory=dict
    )


def profile_refusal_code(evaluation_status: str) -> str:
    """Map a :class:`~jasper.active_speaker.driver_safety.DriverSafetyProfileEvaluation`
    status to the reason code whose copy names the action that ACTUALLY clears it.

    The pre-flight holds evidence the play seam does not — the play seam's
    admission vocabulary carries one ``PROFILE_NOT_CONFIRMED`` slug for every
    un-playable profile state — so this is where the three genuinely different
    household actions separate:

    * ``missing``    → finish the driver details. ``/sound/`` renders no
      callout at all in this state, so "review the safety limits" would name a
      panel that is not on the page.
    * ``incomplete`` → add the missing values first. Saving with values missing
      just rebuilds the same ``incomplete`` profile, so "save again" would send
      the household in a circle.
    * everything else (``stale``, ``malformed``) → review and save. Both are
      cleared by one ordinary save: it rebuilds the profile from the visible
      values, so an output change and an unreadable artifact end the same way.
    """
    status = str(evaluation_status or "")
    if status == "missing":
        return REASON_PROGRAM_PROFILE_MISSING
    if status == "incomplete":
        return REASON_PROGRAM_PROFILE_INCOMPLETE
    return REASON_PROGRAM_PROFILE_NOT_CONFIRMED




def ensure_crossover_preview_ready(*, durable: bool = False) -> dict[str, Any]:
    """Ensure a ready crossover preview exists before a v2 session reads one.

    ``durable`` only matters on the regenerate branch (a reused preview
    writes nothing) and passes straight through to
    :func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`.
    The default keeps the session-open/verify-re-arm callers cheap;
    :func:`handle_v2_apply`'s crossover-accept branch opts in.

    ``/sound/``'s Preview button was the ONLY historical writer of
    ``active_speaker_crossover_preview.json``; the v2 flow never called it, so
    a household that went straight to ``/correction/`` without visiting
    ``/sound/`` first baked its MEASURE candidate's ``source_preset`` against
    the generic bundled-preset fallback (:func:`~jasper.active_speaker.commission_wiring.resolve_capture_preset`'s
    no-preview branch) — which then can NEVER match a preview generated later,
    so Apply refuses ``measured_candidate_preset_mismatch`` forever. This is
    called at the top of :func:`resolve_conductor_context` — the one place
    both stages of :func:`prepare_v2_session`, session-open and the verify
    re-arm, resolve the design draft/topology — so the fallback branch is
    never reached from a v2 entry point again.

    Reuses the SAME generator ``/sound/`` drives
    (:func:`~jasper.active_speaker.web_commissioning.regenerate_crossover_preview_from_current_draft`,
    itself a thin wrapper around :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`)
    rather than reimplementing preview generation. Idempotent: an existing
    preview that is already ``ready_for_protected_staging`` for the CURRENT
    design draft (the freshness/fingerprint check already built into
    :func:`~jasper.active_speaker.crossover_preview.load_crossover_preview`)
    is left byte-untouched — reused, not regenerated. Anything else (absent,
    stale, or blocked) is regenerated once; if the fresh attempt still cannot
    reach ``ready_for_protected_staging`` (a safety profile whose declared
    values are ``incomplete`` or whose outputs moved under it, a blocked design
    draft, etc.), this raises a named :class:`CrossoverV2Refused`
    pointing at ``/sound/`` instead of leaving the surprise for apply time.
    """
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.web_commissioning import (
        regenerate_crossover_preview_from_current_draft,
    )

    preview = load_crossover_preview(current_design_draft=load_design_draft())
    outcome = "reused"
    if preview.get("status") != "ready_for_protected_staging":
        preview = regenerate_crossover_preview_from_current_draft(durable=durable)
        outcome = (
            "generated"
            if preview.get("status") == "ready_for_protected_staging"
            else "refused"
        )
    log_event(
        logger,
        "correction.crossover_v2_preview_ensured",
        outcome=outcome,
        preview_status=str(preview.get("status")),
    )
    if outcome == "refused":
        messages = [
            str(issue.get("message") or issue.get("code"))
            for issue in (preview.get("issues") or [])
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        raise CrossoverV2Refused(
            "the crossover preview is not ready for measurement; finish "
            "speaker setup at /sound/ first"
            + (": " + "; ".join(messages[:2]) if messages else "")
        )
    return preview


def _resolve_driver_class_by_role(draft: Mapping[str, Any]) -> dict[str, str]:
    """Per-role declared driver technology class (#1665 component entry).

    Mirrors :func:`jasper.active_speaker.design_draft.declared_driver_sensitivities`'s
    exact shape — role-keyed, a role with disagreeing declarations drops
    entirely, fails soft on anything malformed rather than raising. This
    resolver runs inside conductor-context resolution: an unexpected value
    should fall back to :func:`~jasper.active_speaker.linearization_envelope.compose_envelope`'s
    own conservative "unknown" default for that one role, never abort the
    whole session.
    """

    from jasper.active_speaker._common import DRIVER_CLASSES

    manual = draft.get("manual_settings") if isinstance(draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, str] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("driver_class")
        if not role or not isinstance(value, str) or value not in DRIVER_CLASSES:
            continue
        if role in out and out[role] != value:
            conflicted.add(role)
            continue
        out[role] = value
    for role in conflicted:
        out.pop(role, None)
    return out


def _resolve_radiating_diameter_by_role(draft: Mapping[str, Any]) -> dict[str, float]:
    """Per-role declared effective radiating diameter, mm (#1665 / #1675).

    The same shape and the same fail-soft contract as
    :func:`_resolve_driver_class_by_role` — role-keyed, a role with disagreeing
    declarations drops entirely, anything malformed is skipped rather than
    raised. A diameter is a beaming PRIOR, so a bad one must cost that one
    role its prior, never the session.

    Deliberately no default: absent means "not declared", and the receipt says
    so. Substituting a nominal diameter would manufacture a beaming ceiling out
    of nothing, and #1675 is explicit that this is geometry
    guidance derived from a declared dimension.
    """
    manual = draft.get("manual_settings") if isinstance(draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, float] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("radiating_diameter_mm")
        if not role or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        millimetres = float(value)
        if not math.isfinite(millimetres) or millimetres <= 0.0:
            continue
        if role in out and out[role] != millimetres:
            conflicted.add(role)
            continue
        out[role] = millimetres
    for role in conflicted:
        out.pop(role, None)
    return out


def resolve_conductor_context(status: Mapping[str, Any]) -> V2ConductorContext:
    """Resolve preset/bands/caps/targets/volume from live status + topology.

    Fail-closed: every missing input is a :class:`CrossoverV2Refused` naming
    what to finish first — never a guessed default.

    This runs at SESSION OPEN — ``prepare_v2_session`` calls it before the
    capture session is registered, and before a
    verify-only re-arm — which is what makes the driver-safety-profile gate
    below a pre-flight rather than a surprise (issue
    #1821). Before that gate existed, this function checked only that a
    profile object was PRESENT while its refusal text claimed confirmation had
    been checked; the real confirmation gate lived four screens later inside
    ``prepare_driver_excitation_plan`` at CHECK-phase program admission. A
    household with an un-confirmed profile therefore burned a link, walked to
    the phone, and hit a deterministic refusal that was knowable before any of
    it — the exact 2026-07-28 JTS3 dead-end.
    """
    from jasper.active_speaker.commission_wiring import resolve_capture_preset
    from jasper.active_speaker._common import BASELINE_TOPOLOGY_CHANGED
    from jasper.active_speaker.design_draft import (
        declared_effective_driver_sensitivities,
        load_design_draft,
    )
    from jasper.active_speaker.driver_safety import evaluate_driver_safety_profile
    from jasper.active_speaker.excitation_safety_plan import (
        ExcitationSafetyPlanError,
        effective_sweep_duration_limit_s,
        resolve_driver_excitation_ceilings,
        resolve_driver_measurement_band_hz,
    )
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.active_speaker.profile import required_driver_roles
    from jasper.active_speaker.session_volume_plan import (
        session_measurement_volume_db,
    )
    from jasper.audio_measurement.program import RoleBand
    from jasper.output_topology import (
        load_output_topology,
        topology_is_subless_passive_mains,
    )

    topology = load_output_topology()
    # A subless passive main has no active crossover, so the gates below — all
    # asking whether an ACTIVE one is commissioned — are not questions about it.
    passive_mains = topology_is_subless_passive_mains(topology)
    if not passive_mains:
        if not status.get("active"):
            raise CrossoverV2Refused(
                "this speaker has no active crossover to measure"
            )
        setup = status.get("setup")
        if not isinstance(setup, Mapping) or setup.get("status") != "ready":
            raise CrossoverV2Refused(
                "protected speaker setup is not ready; finish it before measuring"
            )
        # The one loud line 7j buys. A rotated topology fingerprint used to make
        # `setup.status` `blocked` and refuse this session outright; it is now a
        # notice, and this is the moment it is worth saying — once per session
        # open, not on every `/state` poll.
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
        # Ensure a ready crossover preview BEFORE resolving the capture preset —
        # otherwise resolve_capture_preset's no-preview fallback silently bakes
        # the generic bundled preset into every MEASURE candidate (see docstring).
        ensure_crossover_preview_ready()
    preset = resolve_capture_preset(topology)
    if preset.way_count not in (1, 2):
        raise CrossoverV2Refused(
            REASON_REGISTRY[REASON_SPEAKER_SHAPE_UNSUPPORTED].message,
            code=REASON_SPEAKER_SHAPE_UNSUPPORTED,
        )
    roles = required_driver_roles(preset.way_count)
    draft = load_design_draft(topology=topology)
    safety_profile = draft.get("driver_safety_profile")
    # The gate the refusal text has always CLAIMED (issue #1821). Evaluated
    # against the live topology, so a stale profile (an output change) and an
    # un-confirmed one (a driver-detail edit rotated the fingerprint) are both
    # caught here rather than at play time. Copy comes from the SAME registry
    # entry the phone's terminal failure screen renders, so the two surfaces
    # cannot say different things about the same missing confirmation.
    safety_evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not safety_evaluation.confirmed_and_current or not isinstance(
        safety_profile, Mapping
    ):
        code = profile_refusal_code(safety_evaluation.status)
        log_event(
            logger,
            "correction.crossover_v2_profile_not_confirmed",
            level=logging.WARNING,
            gate="session_open",
            profile_status=safety_evaluation.status,
            code=code,
            reasons=",".join(safety_evaluation.reasons),
        )
        raise CrossoverV2Refused(REASON_REGISTRY[code].message, code=code)
    targets_raw = status.get("targets")
    drivers = (
        targets_raw.get("drivers") if isinstance(targets_raw, Mapping) else None
    ) or []
    role_targets: dict[str, str] = {}
    for target in drivers:
        if isinstance(target, Mapping):
            role = str(target.get("role") or "").lower()
            fingerprint = str(target.get("target_fingerprint") or "")
            if role and fingerprint:
                role_targets[role] = fingerprint
    if set(role_targets) != set(roles):
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
    # The declaration's per-role EFFECTIVE datasheet sensitivities -- naked
    # figure with any declared in-line pad folded in (#1665) -- threaded into
    # every cap resolution below. This is the one owner of that fact (W6.5).
    declared_sensitivities = declared_effective_driver_sensitivities(draft)
    # The declaration's per-role driver technology class (#1665), threaded
    # into the conductor construction sites below so the Layer-1a
    # linearization fit (compose_envelope's class_prior_limit term) sees it.
    driver_class_by_role = _resolve_driver_class_by_role(draft)
    # #1675: the ka/beaming prior, off the SAME draft path, as disclosure.
    radiating_diameter_mm_by_role = _resolve_radiating_diameter_by_role(draft)
    roles_bands = []
    caps: dict[str, float] = {}
    sweep_duration_limits_s: dict[str, float] = {}
    measurement_bands: dict[str, tuple[float, float]] = {}
    for channel, role in enumerate(roles):
        try:
            # program_admission=True: this context exists solely to serve the
            # admission-gated CHECK/MEASURE programs, whose channel routing
            # carries each driver's crossover filter (the tweeter's protective
            # HP included) by construction — the proven-HP path, the same
            # justification as session_volume_plan. Without it the W6.5
            # derived HF ceiling is inert exactly where it matters: these
            # context caps clamp every composed level (CHECK pilot bases,
            # MEASURE back_off_gain, VERIFY min(caps)).
            band, cap = resolve_driver_excitation_ceilings(
                safety_profile,
                role_targets[role],
                program_admission=True,
                declared_sensitivities=declared_sensitivities,
            )
            # The DURATION half of the same confirmed limits, off the same
            # target and under the same refusal copy. The composer must be
            # HANDED this number, never derive a second one (#2921).
            sweep_duration_limits_s[role] = effective_sweep_duration_limit_s(
                safety_profile, role_targets[role],
            )
        except (ExcitationSafetyPlanError, ValueError) as exc:
            raise CrossoverV2Refused(
                f"the {role}'s safe excitation limits could not be resolved"
            ) from exc
        # Flat-linearization plan PR-4: this role's confirmed measurement band.
        # Its OWN except arm: a declared-metadata gap on this optional surface
        # must never refuse a session.
        try:
            measurement_bands[role] = resolve_driver_measurement_band_hz(
                safety_profile, role_targets[role],
            )
        except (ExcitationSafetyPlanError, ValueError):
            pass
        roles_bands.append(RoleBand(role, channel, band))
        caps[role] = float(cap)
    # ``None`` is "this speaker declares no corner", never a corner at zero —
    # see ``crossover_v2.priors`` and ``build_verify_program``.
    fc_hz = (
        float(preset.crossover_regions[0].fc_hz)
        if preset.crossover_regions else None
    )
    session_volume_db = session_measurement_volume_db(
        safety_profile,
        [role_targets[role] for role in roles],
        declared_sensitivities=declared_sensitivities,
    )
    playback_device, _playback_device_source = resolve_active_playback_device(
        topology
    )
    playback_device = str(playback_device or "")
    if not playback_device:
        raise CrossoverV2Refused(
            "the active output device is not declared; finish speaker setup"
        )
    return V2ConductorContext(
        preset=preset,
        roles_bands=tuple(roles_bands),
        fc_hz=fc_hz,
        driver_caps_dbfs=caps,
        driver_sweep_duration_limits_s=sweep_duration_limits_s,
        role_targets=role_targets,
        safety_profile=safety_profile,
        session_volume_db=session_volume_db,
        # W6 CHECKLIST ITEM: driver_spacing_m stays 0.0 until a declared
        # woofer↔tweeter spacing input exists (topology/preset carry none
        # today), so the §3.2 parallax correction is INERT — the analysis
        # subtracts nothing. Do not assume VERIFY covers this: a missing
        # parallax correction is SELF-CANCELLING at the mic position (the
        # same geometric excess is baked into both MEASURE and VERIFY), so
        # VERIFY passes while the LISTENING POSITION carries the full error
        # (~23° at 2 kHz for 15 cm spacing measured at 1 m).
        # W6 CHECKLIST ITEM (pre-existing): a deliberate household volume
        # action mid-session (remote / voice "louder" / :8780 HTTP) still moves
        # the CamillaDSP main volume — the session measurement pause holds off
        # the idle reconciler, not VolumeCoordinator writes. W6 validation
        # runs hands-off; a session-long volume guard is a follow-up.
        driver_spacing_m=0.0,
        topology=topology,
        playback_device=playback_device,
        role_channels={role: channel for channel, role in enumerate(roles)},
        sound_design_revision=int(draft.get("revision", 0)),
        declared_sensitivities=declared_sensitivities,
        driver_class_by_role=driver_class_by_role,
        radiating_diameter_mm_by_role=radiating_diameter_mm_by_role,
        measurement_band_hz_by_role=measurement_bands,
    )
