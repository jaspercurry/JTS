# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-owned, fail-closed preparation for admitted driver excitation.

The closed sweep/level ledger below derives every field passed to Shared's
persisted admission types. It deliberately remains pure: the production
adapter owns fresh live-graph proof, persistence, exact WAV binding, guarded
playback, and writer-lock lifetime. The one deliberate exception is the
``log_event`` calls in :func:`resolve_driver_excitation_ceilings` -- two when
it supersedes a stale HF class-default ceiling with the sensitivity-derived
one (or names why it could not), and one when a proven-HP high-frequency
role's excitation floor follows its declared hard band below its declared
analysis window (#1654). Audit lines, not state mutations; see the W6.5 and
"Low-side asymmetry" rulings in that function's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import math
from typing import Any, Mapping

from jasper.audio_measurement.evidence_identity import json_fingerprint
from jasper.audio_measurement.excitation_admission import (
    ExcitationLimits,
    ExcitationRequest,
    FrequencyBand,
)
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from ._common import require_sha256_hex
from .driver_protection import (
    HIGH_FREQUENCY_ROLES,
    LOW_FREQUENCY_ROLES,
    derive_hf_measurement_ceiling_dbfs,
    driver_protection_profile,
)
from .driver_safety import evaluate_driver_safety_profile
from .measurement import active_driver_targets
from .test_signal_plan import (
    MAX_DRIVER_TEST_FREQUENCY_HZ,
    MIN_DRIVER_TEST_FREQUENCY_HZ,
    driver_sweep_duration_s,
)

SCHEMA_VERSION = 1
PREPARED_PLAN_KIND = "jts_active_prepared_driver_excitation_plan"
ACTIVE_DRIVER_MAX_REPEAT_COUNT = 3

logger = logging.getLogger(__name__)


class ExcitationSafetyPlanError(ValueError):
    """The requested target/profile/plan cannot form a bounded preparation."""


class ExcitationSafetyPlanRefusal(str, Enum):
    PROFILE_NOT_CONFIRMED = "active_excitation_profile_not_confirmed"
    TARGET_NOT_CURRENT = "active_excitation_target_not_current"
    REQUEST_OUTSIDE_LIMITS = "active_excitation_request_outside_limits"


def _sha256(value: Any, *, field: str) -> str:
    return require_sha256_hex(
        value,
        field,
        ExcitationSafetyPlanError,
        message=f"{field} must be a lowercase SHA-256",
    )


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExcitationSafetyPlanError(f"{field} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ExcitationSafetyPlanError(f"{field} must be finite")
    return 0.0 if number == 0.0 else number


@dataclass(frozen=True)
class DriverSweepGeneratorPlan:
    """Closed normalized sweep plus the complete effective-peak ledger."""

    f1_hz: float
    f2_hz: float
    amplitude: float
    duration_s: float
    repeat_count: int
    commissioning_gain_db: float
    main_volume_db: float

    def __post_init__(self) -> None:
        f1 = _finite(self.f1_hz, field="f1_hz")
        f2 = _finite(self.f2_hz, field="f2_hz")
        amplitude = _finite(self.amplitude, field="amplitude")
        duration = _finite(self.duration_s, field="duration_s")
        commissioning_gain = _finite(
            self.commissioning_gain_db,
            field="commissioning_gain_db",
        )
        main_volume = _finite(self.main_volume_db, field="main_volume_db")
        if f1 <= 0.0 or f2 <= f1:
            raise ExcitationSafetyPlanError("sweep frequencies must increase")
        if amplitude <= 0.0 or amplitude > 1.0:
            raise ExcitationSafetyPlanError("amplitude must be in (0, 1]")
        if duration <= 0.0:
            raise ExcitationSafetyPlanError("duration_s must be positive")
        if type(self.repeat_count) is not int or self.repeat_count <= 0:
            raise ExcitationSafetyPlanError("repeat_count must be a positive integer")
        if commissioning_gain > 0.0 or main_volume > 0.0:
            raise ExcitationSafetyPlanError(
                "commissioning gain and main volume must be non-positive"
            )
        object.__setattr__(self, "f1_hz", f1)
        object.__setattr__(self, "f2_hz", f2)
        object.__setattr__(self, "amplitude", amplitude)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "commissioning_gain_db", commissioning_gain)
        object.__setattr__(self, "main_volume_db", main_volume)

    @property
    def band(self) -> FrequencyBand:
        return FrequencyBand(self.f1_hz, self.f2_hz)

    @property
    def effective_peak_dbfs(self) -> float:
        return (
            20.0 * math.log10(self.amplitude)
            + self.commissioning_gain_db
            + self.main_volume_db
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_log_sweep_generator_plan",
            "f1_hz": self.f1_hz,
            "f2_hz": self.f2_hz,
            "amplitude": self.amplitude,
            "duration_s": self.duration_s,
            "repeat_count": self.repeat_count,
            "commissioning_gain_db": self.commissioning_gain_db,
            "main_volume_db": self.main_volume_db,
            "effective_peak_dbfs": self.effective_peak_dbfs,
        }


@dataclass(frozen=True)
class RequestedDriverExcitationPlan:
    target_fingerprint: str
    commissioning_context_fingerprint: str
    generator: DriverSweepGeneratorPlan

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_fingerprint",
            _sha256(self.target_fingerprint, field="target_fingerprint"),
        )
        object.__setattr__(
            self,
            "commissioning_context_fingerprint",
            _sha256(
                self.commissioning_context_fingerprint,
                field="commissioning_context_fingerprint",
            ),
        )
        if not isinstance(self.generator, DriverSweepGeneratorPlan):
            raise ExcitationSafetyPlanError(
                "generator must be DriverSweepGeneratorPlan"
            )

    @property
    def band(self) -> FrequencyBand:
        return self.generator.band

    @property
    def effective_peak_dbfs(self) -> float:
        return self.generator.effective_peak_dbfs

    @property
    def duration_s(self) -> float:
        return self.generator.duration_s

    @property
    def repeat_count(self) -> int:
        return self.generator.repeat_count

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_requested_driver_excitation_plan",
            "target_fingerprint": self.target_fingerprint,
            "commissioning_context_fingerprint": (
                self.commissioning_context_fingerprint
            ),
            "generator": self.generator.to_dict(),
        }


@dataclass(frozen=True, init=False)
class PreparedDriverExcitationPlan:
    target_id: str
    target_role: str
    requested_plan: RequestedDriverExcitationPlan
    limits: ExcitationLimits
    request: ExcitationRequest
    minimum_cooldown_s: float
    refusals: tuple[ExcitationSafetyPlanRefusal, ...]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("use prepare_driver_excitation_plan")

    @classmethod
    def _from_preparation(
        cls,
        *,
        topology: OutputTopology,
        requested_plan: RequestedDriverExcitationPlan,
        limits: ExcitationLimits,
        request: ExcitationRequest,
        minimum_cooldown_s: float,
        refusals: tuple[ExcitationSafetyPlanRefusal, ...],
    ) -> "PreparedDriverExcitationPlan":
        """Freeze only a fully self-consistent bounded plan."""

        if not isinstance(topology, OutputTopology):
            raise ExcitationSafetyPlanError(
                "prepared topology must be OutputTopology"
            )
        if not isinstance(requested_plan, RequestedDriverExcitationPlan):
            raise ExcitationSafetyPlanError(
                "requested_plan must be RequestedDriverExcitationPlan"
            )
        if not isinstance(limits, ExcitationLimits) or not isinstance(
            request, ExcitationRequest
        ):
            raise ExcitationSafetyPlanError(
                "limits and request must be typed Shared admission inputs"
            )
        cooldown = _finite(minimum_cooldown_s, field="minimum_cooldown_s")
        if cooldown < 0.0:
            raise ExcitationSafetyPlanError(
                "minimum_cooldown_s must be non-negative"
            )
        current_targets = [
            target
            for target in active_driver_targets(topology)
            if target.get("target_fingerprint") == requested_plan.target_fingerprint
        ]
        if len(current_targets) != 1:
            raise ExcitationSafetyPlanError(
                ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
            )
        target_id = str(current_targets[0].get("target_id") or "")
        target_role = str(current_targets[0].get("role") or "")
        if not target_id or not target_role:
            raise ExcitationSafetyPlanError(
                ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
            )
        outside_limits = bool(
            not request.band.is_subset_of(limits.permitted_band)
            or request.effective_peak_dbfs > limits.maximum_effective_peak_dbfs
            or request.duration_s > limits.maximum_duration_s
            or request.repeat_count > limits.maximum_repeat_count
        )
        expected_refusals = (
            (ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,)
            if outside_limits
            else ()
        )
        if (
            type(refusals) is not tuple
            or any(not isinstance(reason, ExcitationSafetyPlanRefusal) for reason in refusals)
            or len(set(refusals)) != len(refusals)
            or refusals != expected_refusals
        ):
            raise ExcitationSafetyPlanError(
                "prepared plan refusal classification is inconsistent"
            )
        if (
            request.target_fingerprint != requested_plan.target_fingerprint
            or request.target_fingerprint != limits.target_fingerprint
            or request.safety_profile_fingerprint
            != limits.safety_profile_fingerprint
            or request.excitation_plan_fingerprint != requested_plan.fingerprint
            or limits.excitation_plan_fingerprint != requested_plan.fingerprint
            or request.authority_fingerprint != limits.fingerprint
            or request.band != requested_plan.band
            or request.effective_peak_dbfs != requested_plan.effective_peak_dbfs
            or request.duration_s != requested_plan.duration_s
            or request.repeat_count != requested_plan.repeat_count
        ):
            raise ExcitationSafetyPlanError(
                "prepared request, limits, and requested plan are inconsistent"
            )
        self = object.__new__(cls)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "target_role", target_role)
        object.__setattr__(self, "requested_plan", requested_plan)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "minimum_cooldown_s", cooldown)
        object.__setattr__(self, "refusals", refusals)
        return self

    @property
    def execution_allowed(self) -> bool:
        return not self.refusals

    @property
    def fingerprint(self) -> str:
        return json_fingerprint(self._core())

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": PREPARED_PLAN_KIND,
            "target_id": self.target_id,
            "target_role": self.target_role,
            "requested_plan": self.requested_plan.to_dict(),
            "limits": self.limits.to_dict(),
            "request": self.request.to_dict(),
            "minimum_cooldown_s": self.minimum_cooldown_s,
            "refusals": [reason.value for reason in self.refusals],
            "execution_allowed": self.execution_allowed,
            "accepts_protection_evidence": True,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}


def _target_for_request(
    safety_profile: Mapping[str, Any],
    target_fingerprint: str,
) -> Mapping[str, Any]:
    targets = safety_profile.get("targets")
    if not isinstance(targets, list):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
        )
    matches = [
        target
        for target in targets
        if isinstance(target, Mapping)
        and target.get("target_fingerprint") == target_fingerprint
    ]
    if len(matches) != 1:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.TARGET_NOT_CURRENT.value
        )
    return matches[0]


def _declared_sensitivity(
    declared_sensitivities: Mapping[str, Any] | None,
    role: str,
) -> float | None:
    """One role's declared datasheet sensitivity from the caller's mapping.

    ``declared_sensitivities`` is read from the DECLARATION -- the design
    draft's ``manual_settings`` (see
    :func:`jasper.active_speaker.design_draft.declared_driver_sensitivities`),
    the one owner of this declared physical property. It never rides the
    confirmed safety profile: duplicating it there would make a second copy of
    the fact and would have required every already-declared box to re-declare
    before the derivation could fire. Many households won't know the value at
    all, and the derivation degrades gracefully (class-default ceiling) when
    it's missing on either side.
    """

    if not isinstance(declared_sensitivities, Mapping):
        return None
    value = declared_sensitivities.get(role)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _derived_hf_ceiling_dbfs(
    safety_profile: Mapping[str, Any],
    hf_role: str,
    declared_sensitivities: Mapping[str, Any] | None,
) -> float | None:
    """The sensitivity-derived ceiling for ``hf_role``, or ``None`` when the
    declared specs cannot support one (missing declared sensitivity on either
    side) -- the caller then keeps the existing class-default ceiling.

    Conservative across multiple low-frequency siblings (a 3-way's woofer AND
    mid): takes the MINIMUM derived candidate across every low-frequency
    target with a declared sensitivity, so the high-frequency driver's
    ceiling never exceeds what is safe against any one of them.
    """

    sens_hf = _declared_sensitivity(declared_sensitivities, hf_role)
    if sens_hf is None:
        return None
    targets = safety_profile.get("targets")
    if not isinstance(targets, list):
        return None
    candidates: list[float] = []
    for candidate in targets:
        if not isinstance(candidate, Mapping):
            continue
        candidate_role = str(candidate.get("role") or "")
        if candidate_role not in LOW_FREQUENCY_ROLES:
            continue
        sens_lf = _declared_sensitivity(declared_sensitivities, candidate_role)
        if sens_lf is None:
            continue
        lf_fingerprint = str(candidate.get("target_fingerprint") or "")
        if not lf_fingerprint:
            continue
        try:
            _lf_band, lf_cap = resolve_driver_excitation_ceilings(
                safety_profile, lf_fingerprint
            )
        except ExcitationSafetyPlanError:
            continue
        candidates.append(
            derive_hf_measurement_ceiling_dbfs(
                declared_lf_driver_cap_dbfs=lf_cap,
                sens_hf_db=sens_hf,
                sens_lf_db=sens_lf,
            )
        )
    return min(candidates) if candidates else None


def resolve_driver_excitation_ceilings(
    safety_profile: Mapping[str, Any],
    target_fingerprint: str,
    *,
    program_admission: bool = False,
    declared_sensitivities: Mapping[str, Any] | None = None,
) -> tuple[FrequencyBand, float]:
    """The confirmed permitted band + maximum effective-peak ceiling for one
    driver target.

    Extracted from :func:`prepare_driver_excitation_plan` so a caller that
    needs ONLY these two ceilings -- the level solver (W2.1), choosing a
    sweep's ``main_volume_db``/``commissioning_gain_db`` before any
    ``DriverSweepGeneratorPlan`` exists to admit -- does not have to
    duplicate this derivation. Admission itself (:func:`admit_excitation`
    via :func:`prepare_driver_excitation_plan`) still re-derives and
    re-validates these same ceilings against the actual requested plan; this
    function has no authority of its own, it is shared math.

    ``program_admission`` marks the PROVEN protective-HP path (operator
    ruling, 2026-07-19: two invariants, one owner each -- wrong-frequency-range
    stays the declared hard band + proven HP, untouched; too-loud becomes ONE
    derived ceiling instead of stacked hedges). Callers whose excitation rides
    a graph that carries the driver's crossover high-pass by construction --
    the v2 conductor context, :mod:`jasper.active_speaker.program_admission`'s
    per-segment plans + per-channel facts, and the session-volume derivation
    that serves them -- pass ``True`` so a high-frequency driver's measurement
    ceiling can be derived from a low-frequency sibling's own declared cap and
    the two drivers' declared sensitivities, rather than pinned at the
    naked-tone class default (sized for an UNPROTECTED tone, not a HP-proven
    one). Every other caller (isolated driver capture, the v1 ramp solver,
    ear-check ramps) defaults to ``False`` and keeps exactly today's
    ``min(declared, class_default)`` ceiling -- one conditional, no new
    subsystem.

    ``declared_sensitivities`` is the per-role declared datasheet sensitivity
    mapping read from the DECLARATION (the design draft's ``manual_settings``
    -- :func:`jasper.active_speaker.design_draft.declared_driver_sensitivities`),
    which is the one owner of that physical property. Optional: without it the
    proven-HP path simply keeps the class-default ceiling (and logs the skip).

    Band-edge asymmetry (sweep-composition PR-A, #1668): the UPPER permitted
    edge is ``min(MAX_DRIVER_TEST_FREQUENCY_HZ, hard_band[1])`` --
    ``measurement_band[1]`` is deliberately EXCLUDED from it.
    ``measurement_band`` is analysis-window metadata (what the wizard tells
    the confidence/SNR scoring to expect), not a protection boundary the
    driver must never be excited past; the declared HARD excitation band (the
    datasheet-backed physically-safe range) and the global test-frequency
    ceiling are what still bind the upper edge. A driver whose measurement
    band tops out below its hard band (e.g. a tweeter declared
    hard=[1600, 20000], measurement=[2000, 18000]) can now be swept up to its
    OWN hard band's edge (or the global ceiling, whichever is lower) instead
    of being silently capped at the narrower analysis window -- wider MEASURE
    sweeps without loosening excursion protection.

    Low-side asymmetry, PROVEN-HP HIGH-FREQUENCY ROLES ONLY (#1654): the same
    argument, applied to the LOWER edge, but deliberately NOT generalised. The
    lower edge is normally ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0],
    measurement_band[0])`` and stays exactly that for every low-frequency role
    and every naked-tone caller, because there ``measurement_band[0]`` is a
    real EXCURSION hedge: a woofer driven below its declared analysis floor
    has nothing between it and its own suspension. A high-frequency role on
    the ``program_admission`` path is the one case where that reasoning does
    not hold -- the graph carries the driver's crossover high-pass by
    construction (the same proven-HP property this flag already gates the
    level ceiling on), so the sub-window region reaches the driver ATTENUATED
    by that filter rather than naked, and the declared HARD floor is the
    operator-confirmed datasheet minimum for exactly this question. There the
    floor becomes ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0])``.

    Why it matters (#1654, R17's unblocker): a tweeter declared
    hard=[1600, 20000] / measurement=[2000, 18000] was swept from 2000 Hz --
    which on the shipped JTS3 box is also the configured Fc. Every scoring
    band a crossover candidate is judged over clamps its low edge up to that
    real sweep floor, so a candidate BELOW 2 kHz was scored on a mask that
    excluded its own handoff, and the measured -4.80 dB @ 1656 Hz dip
    (#1894 Gate 0) sat under the floor entirely. This widening is what lets a
    downward candidate be judged where it actually hands over. It moves only
    the DERIVED excitation floor -- the declared ``measurement_band`` is
    untouched, and :func:`resolve_driver_measurement_band_hz` still returns
    the declared window verbatim to its own consumers.
    """

    target = _target_for_request(safety_profile, target_fingerprint)
    role = str(target.get("role") or "")
    target_id = str(target.get("target_id") or "")
    hard_band = target.get("hard_excitation_band_hz")
    measurement_band = target.get("measurement_band_hz")
    profile_limits = target.get("level_duration_limits")
    required_filters = target.get("required_protection_filters")
    if (
        not target_id
        or not role
        or not isinstance(hard_band, list)
        or len(hard_band) != 2
        or not isinstance(measurement_band, list)
        or len(measurement_band) != 2
        or not isinstance(profile_limits, Mapping)
        or not isinstance(required_filters, list)
    ):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    # measurement_band[0] participates in the lower edge for every role and
    # every path EXCEPT a high-frequency role on the proven-HP path -- see the
    # "Low-side asymmetry" paragraph in this function's docstring for why the
    # excursion argument that keeps it binding elsewhere does not apply there.
    lower_edges = [MIN_DRIVER_TEST_FREQUENCY_HZ, float(hard_band[0])]
    if not (program_admission and role in HIGH_FREQUENCY_ROLES):
        lower_edges.append(float(measurement_band[0]))
    lower = max(lower_edges)
    if lower < float(measurement_band[0]):
        # Named, so an operator triaging a session can see that this driver was
        # deliberately excited BELOW its declared analysis window, and to what.
        # Logged only when the widening actually moves the floor (a declaration
        # whose analysis floor already equals its hard floor is silent).
        log_event(
            logger,
            "active_speaker.excitation_floor_widened_to_hard_band",
            target_id=target_id,
            role=role,
            declared_measurement_floor_hz=f"{float(measurement_band[0]):.1f}",
            excitation_floor_hz=f"{lower:.1f}",
        )
    # measurement_band[1] is deliberately NOT part of this min() -- see the
    # "Band-edge asymmetry" paragraph in this function's docstring. The hard
    # band + global ceiling are the only upper-edge protection boundaries.
    upper = min(
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[1]),
    )
    permitted_band = FrequencyBand(lower, upper)
    protection = driver_protection_profile(
        role,
        driver_style=target.get("driver_style"),
    )
    declared_peak = float(profile_limits["max_effective_peak_dbfs"])
    maximum_peak = min(declared_peak, protection.max_auto_level_dbfs)
    # Supersede-the-seed rule (W6.5): only on the proven-HP path, only for
    # high-frequency roles, and only when the declared cap still EQUALS the
    # class-default seed the wizard writes at declaration time -- a value
    # that exactly matches the seed is the unedited default, not a deliberate
    # operator choice, so it is safe to replace with the derived ceiling. A
    # declared value that differs (even if quieter) is a real household
    # choice and is always respected as-is, never overridden.
    #
    # Known corner: a household value typed LOUDER than the class default
    # (e.g. -50 against the -65 seed) also fails this equality, so no
    # derivation runs -- and the min() above still clamps it back to -65.
    # Safe direction (quieter than the typed intent), but the intent is
    # silently unmet; wizard-side validation of the declared HF cap is the
    # noted follow-up.
    #
    # #2186 widened who writes this value. The driver-research ask now tells an
    # assistant to send exactly this class default for a tweeter with no
    # published level limit, so a research reply is an AUTOMATED writer landing
    # deliberately on the sentinel. That is intended and it does not change the
    # premise -- it extends it: "equals the seed" used to mean "the operator
    # left the wizard default alone", and now also means "the reply had no
    # driver-specific level knowledge". Both are the same claim, that no
    # driver-specific level intent was expressed, which is exactly when
    # deriving from sensitivity is the better answer. A reply that DOES know
    # better sends a different number and is honoured literally by the branch
    # below never running.
    #
    # The discontinuity is real and deliberate: on this path -65.0 resolves to
    # the derived ceiling (up to HF_MEASUREMENT_ABS_CEILING_DBFS) while -66.0
    # resolves to -66.0. Anything that changes the class default, or the value
    # the research template emits, must move both together --
    # tests/test_active_speaker_driver_safety.py's
    # test_template_tweeter_peak_lands_on_the_derivation_sentinel pins the
    # three-way chain (template value == class default == this comparison).
    if (
        program_admission
        and role in HIGH_FREQUENCY_ROLES
        and declared_peak == protection.max_auto_level_dbfs
    ):
        derived_peak = _derived_hf_ceiling_dbfs(
            safety_profile, role, declared_sensitivities
        )
        if derived_peak is None:
            # Named skip: the proven-HP path WOULD derive here but a declared
            # sensitivity is missing on one side, so the (usually far too
            # quiet) class default stays in force. Without this line a
            # near-inaudible HF measurement is a puzzling triage.
            log_event(
                logger,
                "active_speaker.excitation_ceiling_derivation_skipped",
                target_id=target_id,
                role=role,
                reason="declared_sensitivity_missing",
                ceiling_dbfs=f"{maximum_peak:.1f}",
            )
        elif derived_peak != maximum_peak:
            log_event(
                logger,
                "active_speaker.excitation_ceiling_superseded",
                target_id=target_id,
                role=role,
                legacy_ceiling_dbfs=f"{maximum_peak:.1f}",
                derived_ceiling_dbfs=f"{derived_peak:.1f}",
            )
            maximum_peak = derived_peak
    return permitted_band, maximum_peak


def resolve_driver_measurement_band_hz(
    safety_profile: Mapping[str, Any], target_fingerprint: str,
) -> tuple[float, float]:
    """The confirmed ``measurement_band_hz`` for one driver target.

    :func:`resolve_driver_excitation_ceilings` already reads and validates
    this exact field internally (see its "Band-edge asymmetry" docstring
    paragraph) but does not return it — its own return value is the DERIVED
    EXCITATION ceiling, a different quantity that deliberately excludes
    ``measurement_band[1]``. Exposed separately for
    flat-linearization plan PR-4's contract-derived echo/null analysis band,
    which needs the declared analysis WINDOW itself ("what the wizard tells
    the confidence/SNR scoring to expect" — the same docstring), not the
    excitation ceiling.

    Raises the SAME ``ExcitationSafetyPlanError(PROFILE_NOT_CONFIRMED)`` as
    ``resolve_driver_excitation_ceilings`` on the identical malformed-shape
    check, since both functions read the same confirmed record via
    :func:`_target_for_request`.
    """
    target = _target_for_request(safety_profile, target_fingerprint)
    measurement_band = target.get("measurement_band_hz")
    if not isinstance(measurement_band, list) or len(measurement_band) != 2:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    lo, hi = float(measurement_band[0]), float(measurement_band[1])
    if not (math.isfinite(lo) and math.isfinite(hi)) or not 0.0 < lo < hi:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    return (lo, hi)


def resolve_driver_crossover_search_band_hz(
    safety_profile: Mapping[str, Any], target_fingerprint: str,
) -> tuple[float, float] | None:
    """The confirmed ``crossover_search_band_hz`` for one driver, or ``None``.

    The declared range this driver may be crossed over IN — read by R17's Fc
    selector (``crossover_v2.fc_sweep.resolve_fc_search_band``), which intersects
    the participating roles' bands because a two-way Fc puts BOTH drivers at
    that frequency.

    **Returns ``None`` rather than raising**, unlike its sibling
    :func:`resolve_driver_measurement_band_hz` above, and the difference is
    deliberate: the measurement band bounds a program that is about to PLAY, so
    an unreadable one must stop the session. This one bounds what may be
    PROPOSED, and the fail-closed reading of a missing declaration is "no
    proposal from this role" — which is exactly what ``resolve_fc_search_band``
    does with a ``None``, naming the role in ``undeclared_roles`` so the
    household is told which declaration to fix. Refusing a whole measurement
    session over a selector's optional input would be the wrong direction.
    """
    try:
        target = _target_for_request(safety_profile, target_fingerprint)
    except ExcitationSafetyPlanError:
        return None
    band = target.get("crossover_search_band_hz")
    if not isinstance(band, list) or len(band) != 2:
        return None
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lo) and math.isfinite(hi)) or not 0.0 < lo < hi:
        return None
    return (lo, hi)


def prepare_driver_excitation_plan(
    topology: OutputTopology,
    safety_profile: Mapping[str, Any],
    requested_plan: RequestedDriverExcitationPlan,
    *,
    program_admission: bool = False,
    declared_sensitivities: Mapping[str, Any] | None = None,
) -> PreparedDriverExcitationPlan:
    """Bind exact current policy for Shared admission or a typed refusal.

    ``program_admission`` and ``declared_sensitivities`` are forwarded
    verbatim to :func:`resolve_driver_excitation_ceilings` -- see that
    function's docstring for the proven-HP-path ceiling derivation they gate.
    """

    if not isinstance(topology, OutputTopology):
        raise ExcitationSafetyPlanError("topology must be OutputTopology")
    if not isinstance(safety_profile, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    if not isinstance(requested_plan, RequestedDriverExcitationPlan):
        raise ExcitationSafetyPlanError(
            "requested_plan must be RequestedDriverExcitationPlan"
        )
    evaluation = evaluate_driver_safety_profile(safety_profile, topology)
    if not evaluation.confirmed_and_current or evaluation.profile_fingerprint is None:
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    target = _target_for_request(safety_profile, requested_plan.target_fingerprint)
    role = str(target.get("role") or "")
    target_id = str(target.get("target_id") or "")
    profile_limits = target.get("level_duration_limits")
    required_filters = target.get("required_protection_filters")
    # resolve_driver_excitation_ceilings already validated an equivalent
    # profile_limits mapping (on its own re-fetched target) and would have
    # raised above if it were malformed; this re-check is for mypy's
    # narrowing in THIS function's scope, not new runtime behavior.
    if not isinstance(profile_limits, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    permitted_band, maximum_peak = resolve_driver_excitation_ceilings(
        safety_profile,
        requested_plan.target_fingerprint,
        program_admission=program_admission,
        declared_sensitivities=declared_sensitivities,
    )
    protection = driver_protection_profile(
        role,
        driver_style=target.get("driver_style"),
    )
    maximum_duration = min(
        float(profile_limits["max_sweep_duration_s"]),
        driver_sweep_duration_s(role),
    )
    maximum_repeats = min(
        int(profile_limits["max_repeat_count"]),
        ACTIVE_DRIVER_MAX_REPEAT_COUNT,
    )
    minimum_cooldown = float(profile_limits["minimum_cooldown_s"])
    requirement_fingerprint = json_fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "jts_active_driver_protection_requirement",
            "target_id": target_id,
            "target_fingerprint": requested_plan.target_fingerprint,
            "driver_protection_policy": protection.to_dict(),
            "required_filters": required_filters,
        }
    )
    plan_fingerprint = requested_plan.fingerprint
    limits = ExcitationLimits(
        permitted_band=permitted_band,
        maximum_effective_peak_dbfs=maximum_peak,
        maximum_duration_s=maximum_duration,
        maximum_repeat_count=maximum_repeats,
        target_fingerprint=requested_plan.target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        protection_requirement_fingerprint=requirement_fingerprint,
        excitation_plan_fingerprint=plan_fingerprint,
    )
    request = ExcitationRequest(
        band=requested_plan.band,
        effective_peak_dbfs=requested_plan.effective_peak_dbfs,
        duration_s=requested_plan.duration_s,
        repeat_count=requested_plan.repeat_count,
        target_fingerprint=requested_plan.target_fingerprint,
        safety_profile_fingerprint=evaluation.profile_fingerprint,
        authority_fingerprint=limits.fingerprint,
        excitation_plan_fingerprint=plan_fingerprint,
    )
    outside_limits = bool(
        not requested_plan.band.is_subset_of(permitted_band)
        or requested_plan.effective_peak_dbfs > maximum_peak
        or requested_plan.duration_s > maximum_duration
        or requested_plan.repeat_count > maximum_repeats
    )
    refusals = (
        (ExcitationSafetyPlanRefusal.REQUEST_OUTSIDE_LIMITS,)
        if outside_limits
        else ()
    )
    return PreparedDriverExcitationPlan._from_preparation(
        topology=topology,
        requested_plan=requested_plan,
        limits=limits,
        request=request,
        minimum_cooldown_s=minimum_cooldown,
        refusals=refusals,
    )
