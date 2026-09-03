# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-owned, fail-closed preparation for admitted driver excitation.

The closed sweep/level ledger below derives every field passed to Shared's
persisted admission types, and is pure: the production adapter owns live-graph
proof, persistence, WAV binding, guarded playback and writer-lock lifetime. The
one exception is the ``log_event`` audit lines in
:func:`resolve_driver_excitation_ceilings` — never state mutations.
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
from jasper.json_fields import finite_float
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
    number = finite_float(value)
    if number is None:
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


def effective_sweep_duration_limit_s(
    safety_profile: Mapping[str, Any], target_fingerprint: str
) -> float:
    """How long ONE sweep of this target may run: the tighter of the declared
    ``level_duration_limits.max_sweep_duration_s`` and the code-side per-role
    protocol duration (:func:`driver_sweep_duration_s`).

    The single owner of that ``min`` (#2921): :func:`prepare_driver_excitation_plan`
    compares a request's realized duration against it, so anything COMPOSING a
    sweep meant to pass that comparison must fit the SAME number.

    Refuses ``TARGET_NOT_CURRENT`` for a fingerprint this profile does not carry
    exactly once, and ``PROFILE_NOT_CONFIRMED`` when the target declares no
    usable ``level_duration_limits``.
    """
    target = _target_for_request(safety_profile, target_fingerprint)
    profile_limits = target.get("level_duration_limits")
    if not isinstance(profile_limits, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    declared = profile_limits.get("max_sweep_duration_s")
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    return min(
        float(declared),
        driver_sweep_duration_s(str(target.get("role") or "")),
    )


def _declared_sensitivity(
    declared_sensitivities: Mapping[str, Any] | None,
    role: str,
) -> float | None:
    """One role's declared datasheet sensitivity from the caller's mapping.

    ``declared_sensitivities`` is read from the DECLARATION
    (:func:`jasper.active_speaker.design_draft.declared_driver_sensitivities`),
    the one owner of this physical property; it never rides the confirmed safety
    profile. Missing on either side, the derivation degrades to the class
    default rather than refusing.
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
) -> tuple[float, str, float] | None:
    """``(ceiling, anchor provenance, anchor cap)`` for ``hf_role``, or ``None``.

    ``None`` when the declared specs cannot support a derivation (missing
    declared sensitivity on either side) -- the caller then keeps the existing
    class-default ceiling.

    Conservative across multiple low-frequency siblings (a 3-way's woofer AND
    mid): takes the MINIMUM derived candidate across every low-frequency
    target with a declared sensitivity, so the high-frequency driver's
    ceiling never exceeds what is safe against any one of them.

    The anchor is returned, not just consumed, because it MOVES: this ceiling is
    a delta from a low-frequency sibling's own cap, itself a
    :func:`declared_level_ceiling_dbfs` answer. A woofer declaring no level
    limit anchors at its class default, which for a low-frequency role is full
    scale — so the same tweeter derives -30.8 under a woofer declaring -20 and
    -10.8 under one declaring nothing. The caller names the anchor on its log
    line rather than leaving an operator to infer the contract shape.
    """

    sens_hf = _declared_sensitivity(declared_sensitivities, hf_role)
    if sens_hf is None:
        return None
    targets = safety_profile.get("targets")
    if not isinstance(targets, list):
        return None
    candidates: list[tuple[float, str, float]] = []
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
            # Only the PROVENANCE is read off the second call: for a
            # low-frequency role both return the same number by construction,
            # the supersede branch below being high-frequency-only.
            _lf_band, lf_cap = resolve_driver_excitation_ceilings(
                safety_profile, lf_fingerprint
            )
            _same_cap, lf_anchor = declared_level_ceiling_dbfs(candidate)
        except ExcitationSafetyPlanError:
            continue
        candidates.append(
            (
                derive_hf_measurement_ceiling_dbfs(
                    declared_lf_driver_cap_dbfs=lf_cap,
                    sens_hf_db=sens_hf,
                    sens_lf_db=sens_lf,
                ),
                lf_anchor,
                lf_cap,
            )
        )
    # ``min`` on the derived ceiling, with the anchor that PRODUCED it: another
    # sibling's anchor beside the binding number names the wrong cause.
    return min(candidates, key=lambda item: item[0]) if candidates else None


#: How a target's effective-peak ceiling got its number. Provenance is recorded
#: rather than inferred: comparing a value against a code figure to answer "who
#: decided this?" is how a magic number ends up steering a derivation.
LEVEL_CEILING_DECLARED = "declared"
LEVEL_CEILING_UNDECLARED = "undeclared"
LEVEL_CEILING_LEGACY_CLASS_SEED = "legacy_class_seed"


def declared_level_ceiling_dbfs(target: Mapping[str, Any]) -> tuple[float, str]:
    """One target's effective-peak ceiling and where that number came from.

    The one owner of this question: ``max_effective_peak_dbfs`` is optional, and
    two readers with two local interpretations of "absent" is how one comes to
    raise on the shape the other calls ordinary. Three cases:

    * **absent** — ``LEVEL_CEILING_UNDECLARED``: no published level limit for
      this driver, exactly the no-level-intent the sensitivity derivation
      answers. The class default stands as the seed until it does.
    * **declared** — honoured verbatim, never clamped down to the class figure;
      the one surviving bound is digital full scale, enforced where the value is
      parsed (``driver_safety._normalise_level_duration_limits``).
    * **``LEVEL_CEILING_LEGACY_CLASS_SEED``** — a profile saved under the
      retired contract carries the class default itself to mean "no level
      intent", so it is read that way rather than regressing an already
      commissioned speaker's tweeter. Deletable once no stored profile carries a
      seed (#2913).
    """

    profile_limits = target.get("level_duration_limits")
    if not isinstance(profile_limits, Mapping):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    protection = driver_protection_profile(
        str(target.get("role") or ""),
        driver_style=target.get("driver_style"),
    )
    declared_peak = profile_limits.get("max_effective_peak_dbfs")
    if declared_peak is None:
        return float(protection.max_auto_level_dbfs), LEVEL_CEILING_UNDECLARED
    if (
        isinstance(declared_peak, bool)
        or not isinstance(declared_peak, (int, float))
        or not math.isfinite(float(declared_peak))
    ):
        raise ExcitationSafetyPlanError(
            ExcitationSafetyPlanRefusal.PROFILE_NOT_CONFIRMED.value
        )
    peak = float(declared_peak)
    if peak == float(protection.max_auto_level_dbfs):
        return peak, LEVEL_CEILING_LEGACY_CLASS_SEED
    return peak, LEVEL_CEILING_DECLARED


def resolve_driver_excitation_ceilings(
    safety_profile: Mapping[str, Any],
    target_fingerprint: str,
    *,
    program_admission: bool = False,
    declared_sensitivities: Mapping[str, Any] | None = None,
) -> tuple[FrequencyBand, float]:
    """The confirmed permitted band + maximum effective-peak ceiling for one
    driver target.

    Shared math with no authority of its own: admission re-derives and
    re-validates these same ceilings against the actual requested plan.

    ``program_admission`` marks the PROVEN protective-HP path (operator ruling,
    2026-07-19). Callers whose excitation rides a graph carrying the driver's
    crossover high-pass by construction pass ``True``, so a high-frequency
    driver's ceiling derives from a low-frequency sibling's declared cap and the
    two declared sensitivities rather than sitting at the naked-tone class
    default. Every other caller defaults to ``False``.

    ``declared_sensitivities`` is optional; without it the proven-HP path keeps
    the class-default ceiling and logs the skip.

    Band-edge asymmetry (#1668): the UPPER permitted edge is
    ``min(MAX_DRIVER_TEST_FREQUENCY_HZ, hard_band[1])`` — ``measurement_band[1]``
    is deliberately EXCLUDED, being analysis-window metadata rather than a
    protection boundary.

    Low-side asymmetry, PROVEN-HP HIGH-FREQUENCY ROLES ONLY (#1654): the lower
    edge is normally ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0],
    measurement_band[0])`` and stays that for every low-frequency role and every
    naked-tone caller, where ``measurement_band[0]`` is a real EXCURSION hedge.
    On the proven-HP path a high-frequency role reaches the sub-window region
    ATTENUATED by the crossover high-pass, so the floor becomes
    ``max(MIN_DRIVER_TEST_FREQUENCY_HZ, hard_band[0])`` — otherwise a candidate
    below the analysis floor is scored on a mask excluding its own handoff. Only
    the DERIVED excitation floor moves; ``measurement_band`` is untouched.
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
    # measurement_band[0] binds the lower edge for every role and path EXCEPT a
    # high-frequency role on the proven-HP path — see "Low-side asymmetry" in
    # this function's docstring.
    lower_edges = [MIN_DRIVER_TEST_FREQUENCY_HZ, float(hard_band[0])]
    if not (program_admission and role in HIGH_FREQUENCY_ROLES):
        lower_edges.append(float(measurement_band[0]))
    lower = max(lower_edges)
    if lower < float(measurement_band[0]):
        # Named so a triage can see the driver was deliberately excited BELOW
        # its declared analysis window, and to what. Logged only when the
        # widening actually moves the floor.
        log_event(
            logger,
            "active_speaker.excitation_floor_widened_to_hard_band",
            target_id=target_id,
            role=role,
            declared_measurement_floor_hz=f"{float(measurement_band[0]):.1f}",
            excitation_floor_hz=f"{lower:.1f}",
        )
    # measurement_band[1] is deliberately NOT part of this min(): the hard band
    # and global ceiling are the only upper-edge protection boundaries.
    upper = min(
        MAX_DRIVER_TEST_FREQUENCY_HZ,
        float(hard_band[1]),
    )
    permitted_band = FrequencyBand(lower, upper)
    maximum_peak, level_provenance = declared_level_ceiling_dbfs(target)
    # Supersede-the-seed rule (W6.5): only on the proven-HP path, only for
    # high-frequency roles, and only when NO driver-specific level was declared
    # (see :func:`declared_level_ceiling_dbfs`). A declared value is always
    # respected as-is. The resulting step is real: a delegated ceiling resolves
    # to the low-frequency sibling's cap less the declared sensitivity delta,
    # tens of decibels louder than the class seed.
    if (
        program_admission
        and role in HIGH_FREQUENCY_ROLES
        and level_provenance != LEVEL_CEILING_DECLARED
    ):
        derived = _derived_hf_ceiling_dbfs(
            safety_profile, role, declared_sensitivities
        )
        if derived is None:
            # Named skip: the proven-HP path WOULD derive here but a declared
            # sensitivity is missing on one side, so the usually far too quiet
            # class default stays in force.
            log_event(
                logger,
                "active_speaker.excitation_ceiling_derivation_skipped",
                target_id=target_id,
                role=role,
                reason="declared_sensitivity_missing",
                ceiling_dbfs=f"{maximum_peak:.1f}",
            )
            return permitted_band, maximum_peak
        derived_peak, anchor, anchor_cap = derived
        if derived_peak != maximum_peak:
            log_event(
                logger,
                "active_speaker.excitation_ceiling_superseded",
                target_id=target_id,
                role=role,
                legacy_ceiling_dbfs=f"{maximum_peak:.1f}",
                derived_ceiling_dbfs=f"{derived_peak:.1f}",
                delegation=level_provenance,
                # The ANCHOR this number is a delta from. A low-frequency
                # sibling declaring no level limit anchors at ITS class
                # default, which for that role IS full scale, so the
                # high-frequency ceiling moves with the sibling's contract
                # shape and a triage must be able to see which shape produced it.
                anchor=anchor,
                anchor_cap_dbfs=f"{anchor_cap:.1f}",
            )
            maximum_peak = derived_peak
    return permitted_band, maximum_peak


def resolve_driver_measurement_band_hz(
    safety_profile: Mapping[str, Any], target_fingerprint: str,
) -> tuple[float, float]:
    """The confirmed ``measurement_band_hz`` for one driver target.

    :func:`resolve_driver_excitation_ceilings` validates this field but does not
    return it: its answer is the DERIVED EXCITATION ceiling, which deliberately
    excludes ``measurement_band[1]``. Exposed separately for consumers that need
    the declared analysis WINDOW itself.

    Raises the SAME ``ExcitationSafetyPlanError(PROFILE_NOT_CONFIRMED)`` on the
    identical malformed-shape check, both reading one confirmed record.
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


def resolve_driver_protection_slope_db_per_octave(
    safety_profile: Mapping[str, Any], target_fingerprint: str,
) -> float | None:
    """The manufacturer's PUBLISHED high-pass slope condition, or ``None``.

    The slope half of the pair whose frequency half is
    :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`.
    One parse site each, so the two halves of one declaration cannot disagree.

    The two halves read DIFFERENT fields, deliberately. The frequency half reads
    a lossless projection; the slope beside it is
    ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)`` and so cannot tell
    a published 24 from a published 12 raised to 24. This reads the OWNER field
    (``recommended_highpass_slope_db_per_octave``), which a confirmed target
    carries only when the manufacturer actually published one — reading the
    projection instead made the topology gate refuse a DE250 at order 2 against
    a code figure B&C never published.

    Returns ``None`` rather than raising, unlike
    :func:`resolve_driver_measurement_band_hz`: that band bounds a program about
    to PLAY, so an unreadable one must stop the session.

    ``None`` means no published condition ON THE RECORD, from either of two
    causes: the maker prints no slope qualifier, or the profile predates the
    field (in which case no driver on that speaker has one until the next
    ``/sound/`` save). Both mean the same thing here — no published bound to
    apply, never a guessed default — so a caller must never read ``None`` as
    "the manufacturer publishes nothing".
    """
    try:
        target = _target_for_request(safety_profile, target_fingerprint)
    except ExcitationSafetyPlanError:
        return None
    published = target.get("recommended_highpass_slope_db_per_octave")
    if isinstance(published, bool) or not isinstance(published, (int, float)):
        return None
    slope = float(published)
    return slope if math.isfinite(slope) and slope > 0 else None


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
    # Already validated by resolve_driver_excitation_ceilings above; this
    # re-check is mypy narrowing, not new runtime behavior.
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
    maximum_duration = effective_sweep_duration_limit_s(
        safety_profile, requested_plan.target_fingerprint
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
