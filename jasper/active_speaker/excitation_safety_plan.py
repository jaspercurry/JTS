# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-owned, fail-closed preparation for admitted driver excitation.

The closed sweep/level ledger below derives every field passed to Shared's
persisted admission types. It deliberately remains pure: the production
adapter owns fresh live-graph proof, persistence, exact WAV binding, guarded
playback, and writer-lock lifetime. The one deliberate exception is the
``log_event`` calls in :func:`resolve_driver_excitation_ceilings` -- two around
the undeclared-HF ceiling (one when the sensitivity derivation supersedes it,
carrying the delegation that let it and the low-frequency ANCHOR the derived
number is a delta from; one naming why it could not derive), and one when a
proven-HP high-frequency
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
    """How long ONE sweep of this target may run: the tighter of the operator's
    declared ``level_duration_limits.max_sweep_duration_s`` and the code-side
    per-role protocol duration (:func:`driver_sweep_duration_s`).

    The single owner of that ``min``. :func:`prepare_driver_excitation_plan`
    below compares a request's realized duration against it, so anything that
    COMPOSES a sweep intended to pass that comparison must fit the SAME number
    — a composer holding its own copy could drift from the gate by one edit and
    then refuse programs it had just built. Both callers that had restated the
    ``min`` read here now, and the composer's caller reads here too rather than
    deriving a third one (#2921).

    Takes the profile and a target fingerprint — the shape
    :func:`resolve_driver_excitation_ceilings` takes — so the role and the
    declared limits are read off ONE confirmed target rather than passed in
    beside it. Refuses ``TARGET_NOT_CURRENT`` for a fingerprint this profile
    does not carry exactly once, and ``PROFILE_NOT_CONFIRMED`` when that
    target declares no usable ``level_duration_limits``.
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
) -> tuple[float, str, float] | None:
    """``(ceiling, anchor provenance, anchor cap)`` for ``hf_role``, or ``None``.

    ``None`` when the declared specs cannot support a derivation (missing
    declared sensitivity on either side) -- the caller then keeps the existing
    class-default ceiling.

    Conservative across multiple low-frequency siblings (a 3-way's woofer AND
    mid): takes the MINIMUM derived candidate across every low-frequency
    target with a declared sensitivity, so the high-frequency driver's
    ceiling never exceeds what is safe against any one of them.

    **The anchor is returned, not just consumed, because it MOVES.** This
    ceiling is a delta from a low-frequency sibling's own cap, and that cap is
    itself a :func:`declared_level_ceiling_dbfs` answer. A woofer that declares
    a level limit anchors the derivation there; one that declares none anchors
    it at the woofer class default, which for a low-frequency role is full
    scale (``MAX_TEST_LEVEL_DBFS``) -- so the same tweeter derives -30.8 under
    a woofer declaring -20 and -10.8 under a woofer declaring nothing. Both are
    correct answers to different declarations, and the difference is 20 dB on a
    compression driver, so the caller names the anchor on its log line instead
    of leaving an operator to infer the contract shape from the level.
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
            # The cap comes from the one derivation path (which also validates
            # this sibling's shape and skips it below if malformed). Only the
            # PROVENANCE is read off the second call: for a low-frequency role
            # the two return the same number by construction, since the
            # supersede branch below is high-frequency-only and this call does
            # not take the proven-HP path.
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
    # ``min`` on the derived ceiling, and the anchor that PRODUCED it rides
    # along -- reporting a different sibling's anchor beside the binding
    # number would be a receipt that names the wrong cause.
    return min(candidates, key=lambda item: item[0]) if candidates else None


#: How a target's effective-peak ceiling got its number. The same three-way
#: shape :func:`jasper.active_speaker.driver_protection.resolve_driver_low_limit`
#: uses for the low limit, and for the same reason: "who decided this?" is a
#: provenance question, and answering it by comparing a value against a code
#: figure is how a magic number ends up steering a derivation.
LEVEL_CEILING_DECLARED = "declared"
LEVEL_CEILING_UNDECLARED = "undeclared"
LEVEL_CEILING_LEGACY_CLASS_SEED = "legacy_class_seed"


def declared_level_ceiling_dbfs(target: Mapping[str, Any]) -> tuple[float, str]:
    """One target's effective-peak ceiling and where that number came from.

    **The one owner of this question**, because the field is optional and two
    readers with two local interpretations of "absent" is exactly how one of
    them came to raise on the shape the other calls ordinary (2026-08-23).
    Floor, checked rather than asserted: a grep of ``jasper/`` for
    ``max_effective_peak_dbfs`` finds exactly one direct dict access, the one
    below; the two callers that want the answer are
    :func:`resolve_driver_excitation_ceilings` and
    ``commissioning_runtime.prepare_summed_excitation``, and everything further
    downstream (``web.correction_crossover_backend``) takes the already-resolved
    number as an argument. A third reader belongs here too.

    ``max_effective_peak_dbfs`` is OPTIONAL, and absent is the ordinary answer.
    It is the one datasheet fact in ``level_duration_limits``, so since the
    2026-08-23 owner ruling the research ask requests it only where a
    manufacturer publishes a level limit and ``_target_issues`` no longer
    requires it. **Absent** therefore means "no published level limit for this
    driver" — ``LEVEL_CEILING_UNDECLARED`` — which is exactly the
    no-driver-specific-level-intent the sensitivity derivation answers; the
    class default stands as the seed until it does.

    A **declared** value is honoured verbatim, never clamped down to the class
    figure. It is a published limit or an operator's own choice, and the same
    ruling bars a code figure from overruling either; the one bound that
    survives on it is digital full scale, enforced where the value is parsed
    (``driver_safety._normalise_level_duration_limits`` refuses a peak above
    0 dBFS). Until that ruling this was ``min(declared, class_default)``, so a
    household value typed LOUDER than the seed was silently clamped back — the
    trap the old comment here anticipated and left open.

    ``LEVEL_CEILING_LEGACY_CLASS_SEED`` is the third case and the only one that
    compares a value: a profile SAVED under the retired contract carries the
    class default itself, because the ask told the researcher to send exactly
    that number to mean "no level intent". It said then what absence says now,
    so it is read that way rather than silently regressing an already-
    commissioned speaker's tweeter by tens of decibels. It is named on the
    supersede log line so the residual is visible, and the arm is deletable
    once no stored profile carries a seed -- with the whole sensitivity
    derivation behind it, tracked with its inventory and its unblock check as
    `#2913 <https://github.com/jaspercurry/JTS/issues/2913>`_.
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
    ear-check ramps) defaults to ``False`` and keeps the declared ceiling, or
    the class default when no level limit is published -- one conditional, no
    new subsystem.

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
    maximum_peak, level_provenance = declared_level_ceiling_dbfs(target)
    # Supersede-the-seed rule (W6.5): only on the proven-HP path, only for
    # high-frequency roles, and only when NO driver-specific level was declared
    # -- see :func:`declared_level_ceiling_dbfs` for what that means and how a
    # stored profile still says it. A declared value is a real published or
    # household choice and is always respected as-is, never overridden.
    #
    # The step is real and deliberate: on this path a delegated ceiling
    # resolves to the sensitivity-derived one -- since the provisional absolute
    # hedge was retired on 2026-08-20, that is the low-frequency sibling's own
    # cap less the declared sensitivity delta, tens of decibels louder than the
    # class seed -- while a declared -66.0 resolves to -66.0.
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
                # The ANCHOR this number is a delta from, named. A
                # low-frequency sibling that declares no level limit anchors
                # the derivation at ITS class default, which for a
                # low-frequency role IS full scale -- so the high-frequency
                # ceiling moves with the sibling's contract shape, and an
                # operator triaging a level must be able to see which shape
                # produced it rather than inferring it from a number.
                anchor=anchor,
                anchor_cap_dbfs=f"{anchor_cap:.1f}",
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


def resolve_driver_protection_slope_db_per_octave(
    safety_profile: Mapping[str, Any], target_fingerprint: str,
) -> float | None:
    """The manufacturer's PUBLISHED high-pass slope condition, or ``None``.

    The second of this file's confirmed-record readers, and the slope half of
    the pair whose frequency half is
    :func:`~jasper.active_speaker.driver_protection.declared_protection_highpass_floor_hz`.
    One parse site each, so the two halves of one declaration cannot disagree.

    **The two halves read DIFFERENT fields, and that asymmetry is the point.**
    The frequency half reads ``required_protection_filters[highpass].cutoff_hz``
    — a projection, but a LOSSLESS one:
    :func:`~jasper.active_speaker.driver_protection.apply_driver_low_limit`
    stamps that cutoff as the declared frequency verbatim, with no floor
    applied.  The slope beside it is not lossless — it is
    ``max(published, PROTECTION_SLOPE_FLOOR_DB_PER_OCTAVE)``, so reading it
    cannot tell a published 24 from a published 12 raised to 24.  This function
    therefore reads the OWNER field instead
    (``recommended_highpass_slope_db_per_octave``, the pair
    ``driver_protection``'s decision-9 block describes), which a confirmed
    target carries only when the manufacturer actually published one.

    Reading the projection was what made the topology gate refuse a DE250 at
    order 2 "below the protected driver's declared minimum of 24 dB/octave"
    when B&C publish 12 — a code figure refusing a household's choice, which
    the 2026-08-22 ruling bars and the 2026-08-23 owner ruling struck.

    **It exists for the topology gate**
    (:func:`~jasper.active_speaker.crossover_v2.topology_prescription.read_topology_prescription`):
    a two-way corner high-passes the upper driver AT the corner, so that
    driver's published minimum slope is a claim about what the crossover's own
    filter must do.  Nothing downstream enforces a slope ABOVE 12 dB/octave on a
    crossover: ``graph_safety.output_highpass_protected`` reads the corner and
    no ``order`` at all, ``graph_safety.tweeter_guard_present`` reads ``order``
    absent or ``>= 2.0`` (so every emittable order clears it), and the derived
    requirement is proved only against the protective filter this build itself
    emitted, and ``camilla_yaml._assert_tweeter_crossover_hp_satisfies_floor``
    — which DID carry a second copy of this refusal on the VERIFY stage's call
    shape — now discloses its shortfall instead.  See
    ``topology_prescription``'s module docstring for the gate-by-gate
    quotation.

    **Returns ``None`` rather than raising**, unlike its sibling
    :func:`resolve_driver_measurement_band_hz` above, and the difference is
    deliberate: the measurement band bounds a program that is about to PLAY, so
    an unreadable one must stop the session.

    **``None`` means there is no published condition ON THE RECORD, and that has
    TWO causes — say both, because they are not equally comfortable.**  Either
    the maker prints no slope qualifier (an ordinary datasheet; BMS's 4590), or
    the profile was saved before the owner pair existed as a target field, in
    which case NO driver on that speaker has a published slope — not even one
    publishing 24 — until the next ``/sound/`` save re-derives the target.  The
    second case is every already-commissioned speaker on the deploy that ships
    this, and it is why the field is optional in
    ``driver_safety._validate_driver_safety_profile_shape`` rather than
    required: a stored profile stays confirmed rather than being invalidated.
    Both cases mean the same thing HERE — there is no published bound to apply,
    never a guessed default, on ``declared_protection_highpass_floor_hz``'s
    never-nanny rule — and in both the commissioning recommendation is still
    DISCLOSED on the pin's record.  What this function must never do is let a
    caller read ``None`` as "the manufacturer publishes nothing", because on a
    pre-field profile that is a claim about a datasheet nobody consulted.
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
