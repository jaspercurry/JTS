# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a commission session plays, how loud, and for which phase (#2291).

Hearing-safety territory, so the invariants are stated to be checked, not
trusted:

1. :meth:`SessionExcitation.verify_program`'s min-cap clamp is the ONLY level
   guard on the mono summed sweep. That sweep plays through the applied
   production graph with no play-time admission gate, so nothing downstream
   will catch a gain this module gets wrong.
2. The two phases whose captures are COMPARED — :data:`PHASE_ENTRY_BASELINE`
   and :data:`PHASE_VERIFY` — receive the IDENTICAL program object, not an
   equal one: #2291's before→after verdict is checked by ``program_id``
   equality, which holds only because :func:`program_for_phase` hands both
   sides the same object.
3. A lateral pose replays the MEASURE object verbatim, so the prelude rule is
   asked of the OBJECT, never of the pose.

It composes and holds no session state. No ``jasper.web`` import and nothing
from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    DEFAULT_PILOT_LEVELS_DB,
    ExcitationProgram,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
)

from .journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_ENTRY_BASELINE,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_VERIFY,
)

# --------------------------------------------------------------------------- #
# level policy
# --------------------------------------------------------------------------- #

#: The gain solver backs off this far below each driver's exact cap: the W2 gate
#: found ``prepare_driver_excitation_plan``'s strict ``>`` can refuse an
#: exactly-at-cap plan by one ulp.
GAIN_CAP_BACKOFF_DB = 0.01

#: The two pilot levels are this far apart (matches the CHECK behavioral check).
PILOT_LEVEL_DELTA_DB = abs(DEFAULT_PILOT_LEVELS_DB[1] - DEFAULT_PILOT_LEVELS_DB[0])

#: The phases whose capture OPENS a session's playback, and so carries the
#: courtesy prelude (#1677). No env/config switch. :data:`PHASE_ENTRY_BASELINE`
#: is stage 1's LAST capture rather than an opener, but it PLAYS the announced
#: program, and ``build_v2_capture_plan`` sizes its recording window from this
#: set — dropping it would budget the phone 3.6 s short.
COURTESY_PRELUDE_PHASES = frozenset(
    {PHASE_CHECK, PHASE_VERIFY, PHASE_ENTRY_BASELINE}
)


def leading_pilot_role(roles: Sequence[RoleBand]) -> str:
    """The role whose solved gain the leading pilot pair rides — the lowest."""
    return roles[0].role


def pilot_gains(hi_gain_db: float) -> tuple[float, float]:
    """The ``(lo, hi)`` pilot pair at a given level, delta preserved."""
    return (hi_gain_db - PILOT_LEVEL_DELTA_DB, hi_gain_db)


def courtesy_prelude_for_phase(phase: str) -> bool:
    """Does this phase's capture announce itself with the courtesy prelude?

    **The prelude announces a SESSION, not a capture** (#1677). Stage 1 opens on
    :data:`PHASE_CHECK` and stage 2 on :data:`PHASE_VERIFY`; both open on a
    warning, and a re-warning before every capture costs 3.6 s — 0.6 s of beeps
    plus a 3.0 s settle — of held-still silence for information the household
    already has.

    ONE shared rule rather than a decision at each site: the phone's DURATION
    BUDGET and the composers below both ask this function, so the phone can
    never budget a shorter recording window than the program it is capturing.
    """
    return phase in COURTESY_PRELUDE_PHASES


def back_off_gain(gain_db: float, session_volume_db: float, cap_dbfs: float,
                  *, margin_db: float = GAIN_CAP_BACKOFF_DB) -> float:
    """Clamp a per-driver digital gain so its effective peak stays under the cap.

    The effective peak folded through the session volume is
    ``gain_db + session_volume_db``, and admission caps it at the driver's
    ``cap_dbfs``; ``margin_db`` (≥0.01 dB) is why an at-cap solve stays
    admissible — see :data:`GAIN_CAP_BACKOFF_DB`.
    """
    ceiling = cap_dbfs - session_volume_db - margin_db
    return min(float(gain_db), ceiling)


# --------------------------------------------------------------------------- #
# which phases share one composed program
# --------------------------------------------------------------------------- #

#: The phases whose excitation is the mono summed sweep played through the LIVE
#: production graph with no program-graph load and no play-time admission gate.
#: A spatial cloud measures the SUMMED system — pre-apply for CLOUD_MEASURE,
#: post-apply for CLOUD_VERIFY — and
#: :meth:`SessionExcitation.verify_program`'s clamp is the only level guard for
#: all four. ``PHASE_ENTRY_BASELINE`` is a member for a stronger reason than the
#: other three: it is invariant 2's correctness condition, so removing it here
#: would make every round's benefit verdict
#: :data:`~.verification.BENEFIT_PROGRAM_MISMATCH`.
SUMMED_SWEEP_PHASES = frozenset(
    {PHASE_VERIFY, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_ENTRY_BASELINE}
)

#: The :data:`SUMMED_SWEEP_PHASES` members that are prompted POSITION GROUPS.
#: They play :meth:`SessionExcitation.cloud_program`, carrying no courtesy
#: prelude because a position is not a session opener. The complement is
#: invariant 2's COMPARED pair; splitting the family costs no comparability,
#: because nothing compares a position's ``program_id``.
GROUP_SUMMED_SWEEP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY})


class NoProgramForPhaseError(RuntimeError):
    """This session composes no excitation for that phase."""


# --------------------------------------------------------------------------- #
# the session's own declarations, and the three programs they compose
# --------------------------------------------------------------------------- #


def measurement_band_hz(roles: Sequence[RoleBand]) -> tuple[float, float]:
    """The summed system's swept band — the union of every declared
    ``RoleBand.band``, which for ONE declaration is that declaration itself.

    Each ``RoleBand.band`` is one driver's own excitation-ceiling band; no other
    function composes across roles.
    """
    return (
        min(float(rb.band.lower_hz) for rb in roles),
        max(float(rb.band.upper_hz) for rb in roles),
    )


@dataclass(frozen=True)
class SessionExcitation:
    """What one session may play, how loud, and for how long — its declarations,
    bundled so a subset that could drift cannot compose a program at one level
    and budget it at another. Construction copies both mappings behind read-only
    views.
    """

    #: The driver role/band declarations, lowest first. Two on a 2-way; one on
    #: a 1-way passive main, whose single declaration is its own hull.
    roles: tuple[RoleBand, ...]
    #: Per-role excitation ceiling, dBFS. The min across roles is what clamps a
    #: summed signal, which reaches every driver.
    caps_dbfs: Mapping[str, float]
    #: The session's own output level, which every per-driver gain folds through.
    session_volume_db: float
    #: The declared crossover corner, for the summed sweep's shape. ``None`` on
    #: a 1-way main, whose summed sweep takes its shape from the declared band.
    fc_hz: float | None
    #: Per-role longest admissible ONE sweep, seconds — the resolver's
    #: ``effective_sweep_duration_limit_s``, which is also what the admission
    #: gate compares each composed segment against. A role absent here composes
    #: at its nominal.
    sweep_duration_limits_s: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(
            self, "caps_dbfs", MappingProxyType(dict(self.caps_dbfs)),
        )
        object.__setattr__(
            self,
            "sweep_duration_limits_s",
            MappingProxyType(dict(self.sweep_duration_limits_s)),
        )

    @property
    def leading_pilot_role(self) -> str:
        """This session's leading pilot role."""
        return leading_pilot_role(self.roles)

    def pilot_gains(self, hi_gain_db: float) -> tuple[float, float]:
        """This session's pilot pair."""
        return pilot_gains(hi_gain_db)

    def check_program(self) -> ExcitationProgram:
        """CHECK's two-pilot behavioural probe, clamped PER ROLE.

        Each driver's pilot base is clamped so the loudest (hi) pilot's effective
        peak stays under that driver's cap folded through the session volume; the
        tweeter (compression driver, deep cap) rides a base ~40 dB below the
        woofer's. Both pilots keep their fixed ``DEFAULT_PILOT_LEVELS_DB`` offsets
        against that per-role base, so the 10 dB behavioral-linearity delta is
        preserved while the absolute level degrades honestly.
        """
        role_base = {
            rb.role: back_off_gain(
                BASE_STIMULUS_PEAK_DBFS,
                self.session_volume_db,
                self.caps_dbfs.get(rb.role, 0.0),
            )
            for rb in self.roles
        }
        return build_check_program(
            self.roles,
            downstream_gain_db=self.session_volume_db,
            role_base_peak_dbfs=role_base,
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
        )

    def measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's per-driver sweeps at the solved gains, clamped PER ROLE
        and fitted to each role's duration limit.

        Also what every lateral pose plays, verbatim, so the prelude question is
        asked of MEASURE, the object's own phase.

        A sweep realizes at the nearest phase-closing length (#2921), so a
        nominal 4 s woofer realizes 4.00577 s and admission refused the whole
        program against a declared 4 s limit. :attr:`sweep_duration_limits_s`
        makes the composer pick the longest phase-closing sweep AT OR BELOW that
        limit; the admission comparison stays the independent tripwire.
        """
        gains = {}
        for rb in self.roles:
            cap = self.caps_dbfs.get(rb.role, 0.0)
            gains[rb.role] = back_off_gain(
                float(gain_plan_db[rb.role]) - extra_backoff_db,
                self.session_volume_db,
                cap,
            )
        return build_measure_program(
            gains, self.roles,
            sweep_duration_limits_s=self.sweep_duration_limits_s,
            downstream_gain_db=self.session_volume_db,
            leading_pilot_gains_db=self.pilot_gains(gains[self.leading_pilot_role]),
            leading_pilot_role=self.leading_pilot_role,
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_MEASURE),
        )

    def verify_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        """The mono summed sweep, clamped to the MOST RESTRICTIVE cap.

        Cap-aware (W6.1): VERIFY plays a MONO summed sweep through the APPLIED
        production graph with NO play-time admission gate (it does not ride
        ``play_program``/``readmit``), so the compose-time clamp here is the ONLY
        level guard. A summed signal reaches every driver, so it is clamped to
        the MOST RESTRICTIVE (min) cap: at the worst case (no crossover
        attenuation) no driver is driven past its own limit. At the shared
        reference base (effective ~-32 dBFS) it would over-drive a deep-cap
        tweeter (the JTS3 B&C DE250 at -65 dBFS effective). The
        :meth:`pilot_gains` pair rides the same clamped level, so its 10 dB delta
        is preserved. A genuinely-too-quiet clamp surfaces as the existing
        ``snr_floor``/``agc_behavioral_fail`` verdicts, not a precheck (§5.10).
        """
        return self._summed_sweep(
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
            extra_backoff_db=extra_backoff_db,
        )

    def cloud_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        """A prompted position's summed sweep — :meth:`verify_program`, unannounced.

        The SAME sweep through the SAME clamp: both go through
        :meth:`_summed_sweep`, so invariant 1's "only level guard" stays one
        function and cannot drift into two. The single difference is the courtesy
        prelude, which a position does not carry — 3.6 s a household holds a
        microphone still, per position.

        A separate METHOD rather than a flag because :func:`program_for_phase`
        answers by identity: "which object is this" must stay a question about
        the phase.
        """
        return self._summed_sweep(
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
            extra_backoff_db=extra_backoff_db,
        )

    def _summed_sweep(
        self, *, courtesy_prelude: bool, extra_backoff_db: float,
    ) -> ExcitationProgram:
        """The mono summed sweep and its ONE min-cap clamp — see
        :meth:`verify_program` for why that clamp is the only level guard."""
        binding_cap = min(self.caps_dbfs.values()) if self.caps_dbfs else 0.0
        gain = back_off_gain(
            BASE_STIMULUS_PEAK_DBFS - extra_backoff_db,
            self.session_volume_db,
            binding_cap,
        )
        return build_verify_program(
            self.fc_hz,
            measurement_band_hz=measurement_band_hz(self.roles),
            gain_db=gain,
            downstream_gain_db=self.session_volume_db,
            leading_pilot_gains_db=self.pilot_gains(gain),
            courtesy_prelude=courtesy_prelude,
        )


def program_for_phase(
    phase: str,
    *,
    check: ExcitationProgram,
    measure: ExcitationProgram | None,
    verify: ExcitationProgram,
    cloud: ExcitationProgram,
) -> ExcitationProgram:
    """Which composed program this phase plays — **by identity, not by value**.

    No branch here composes, copies, or replaces, which is invariant 2's whole
    mechanism: the COMPARED pair gets the same ``verify`` object (shared
    ``program_id``), and every :data:`GROUP_SUMMED_SWEEP_PHASES` position gets
    the same ``cloud`` object.

    ``measure`` is ``None`` until the CHECK gain solve produces a plan;
    requesting MEASURE before then raises :class:`NoProgramForPhaseError` rather
    than composing something at a guessed level.
    """
    if phase == PHASE_CHECK:
        return check
    # R16: a lateral pose replays the ANCHOR's program object VERBATIM. That
    # identity is not an optimisation: the return-to-mark bracket and every §4.4
    # falloff comparison are differences against the anchor, and a pose measured
    # at a different level or with a different sweep would be uninterpretable.
    if phase in (PHASE_MEASURE, PHASE_LATERAL):
        if measure is None:
            raise NoProgramForPhaseError(
                "MEASURE armed before the CHECK gain solve produced a program"
            )
        return measure
    if phase in GROUP_SUMMED_SWEEP_PHASES:
        # One composed sweep serves both position groups: same excitation, same
        # min-cap clamp, same ``program.phase`` ("verify") so the analyzer routes
        # it unchanged. What differs from the compared pair is the courtesy
        # prelude alone, which is analysis-invisible (``KIND_COURTESY_TONE`` is
        # not a ``STIMULUS_KIND``).
        return cloud
    if phase in SUMMED_SWEEP_PHASES:
        # The COMPARED pair is one object, and that identity is invariant 2.
        # What differs between the two is the PRIORS the session hands the
        # analysis and the verdict it draws — never the sound the speaker makes.
        return verify
    raise NoProgramForPhaseError(f"no program for phase {phase!r}")
