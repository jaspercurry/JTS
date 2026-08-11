# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a commission session plays, how loud, and for which phase (#2291).

Phase 5's second vertical, and the *first* leg of the issue's sequence: before
anything can be captured, something has to be played.  Three questions had no
owner of their own and were answered from six places on
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor`:

* **How loud may a driver be driven?**  :func:`back_off_gain` — the one clamp
  that folds a per-driver digital gain through the session volume and holds it
  under that driver's cap.
* **What does each phase play?**  The three composers below, which are the only
  callers of ``jasper.audio_measurement.program``'s builders in the v2 flow.
* **Which composed program does a phase get?**  :func:`program_for_phase`.

**This module is hearing-safety territory, and its invariants are stated so
they can be checked rather than trusted:**

1. :meth:`SessionExcitation.verify_program`'s min-cap clamp is the ONLY level
   guard on the
   mono summed sweep.  That sweep plays through the applied production graph
   with no play-time admission gate (see
   ``jasper.web.correction_crossover_v2.bind_production_play``), so nothing
   downstream will catch a gain this function gets wrong.
2. Every member of :data:`SUMMED_SWEEP_PHASES` receives the **identical program
   object**, not an equal one.  #2291's before→after benefit verdict is checked
   by ``program_id`` equality, and that equality holds because
   :func:`program_for_phase` hands both sides the same object.  A copy that
   merely compared equal today would be a latent
   :data:`~.verification.BENEFIT_PROGRAM_MISMATCH` the first time composition
   picked up any per-call state.

Both are pinned by ``tests/test_crossover_v2_programs.py`` against golden
identities captured from the pre-extraction conductor.

**What this module is not.**  It composes; it does not decide when to play, does
not consume a capture, does not journal, and holds no session state — a
:class:`SessionExcitation` is a frozen bundle of the session's own declarations
and every composer is a pure function of it.  The conductor still owns *when* a
program is composed and *which* composed objects it holds, because those are
lifecycle facts: MEASURE cannot be composed until the CHECK gain solve produces
a plan, and it is recomposed on the clip-retry rearm.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

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

#: The gain solver backs off this far below each driver's exact cap. The W2 gate
#: found ``prepare_driver_excitation_plan``'s strict ``>`` can refuse an
#: exactly-at-cap plan by one ulp, so a hair of headroom keeps an at-cap solve
#: admissible.
GAIN_CAP_BACKOFF_DB = 0.01

#: The two pilot levels are this far apart (matches the CHECK behavioral check).
PILOT_LEVEL_DELTA_DB = abs(DEFAULT_PILOT_LEVELS_DB[1] - DEFAULT_PILOT_LEVELS_DB[0])

#: Every v2 program plays the spoken courtesy prelude.
#:
#: The household hears what is about to happen before it happens (the 2026-07-14
#: bench run started a sweep while music was playing, forcing a void + re-run)
#: plus the house "no-silent-failure" / "no speculative flexibility" rules both
#: point the same way — every household benefits from the warning, and there is
#: no stated case for wanting it off. Every ``build_v2_capture_plan`` /
#: ``build_v2_verify_capture_plan`` (phone duration budget) and every composer
#: below (actual playback) passes this SAME constant, so the two can never
#: disagree about whether the prelude is present — the phone would otherwise
#: budget a shorter recording window than the program it is actually capturing.
#: ``jasper.audio_measurement.program``'s own composers default
#: ``courtesy_prelude`` to ``False`` so every OTHER caller (tests, future tools)
#: keeps today's byte-identical shape unless it opts in explicitly.
COURTESY_PRELUDE_ENABLED = True


def back_off_gain(gain_db: float, session_volume_db: float, cap_dbfs: float,
                  *, margin_db: float = GAIN_CAP_BACKOFF_DB) -> float:
    """Clamp a per-driver digital gain so its effective peak stays under the cap.

    The effective peak folded through the session volume is
    ``gain_db + session_volume_db``; admission caps it at the driver's
    ``cap_dbfs``. The W2 gate found the admission's strict ``>`` can refuse an
    exactly-at-cap plan by one ulp, so this backs off ``margin_db`` (≥0.01 dB)
    below the cap — an at-cap solve stays admissible.
    """
    ceiling = cap_dbfs - session_volume_db - margin_db
    return min(float(gain_db), ceiling)


# --------------------------------------------------------------------------- #
# which phases share one composed program
# --------------------------------------------------------------------------- #

#: The phases whose excitation is the mono summed sweep played through the LIVE
#: production graph with no program-graph load and no play-time admission gate
#: (see ``jasper.web.correction_crossover_v2.bind_production_play``). VERIFY has
#: always been one; the two cloud groups join it because a spatial cloud
#: measures the SUMMED system — pre-apply for CLOUD_MEASURE ("what the speaker
#: does today"), post-apply for CLOUD_VERIFY. The compose-time min-cap clamp in
#: :meth:`SessionExcitation.verify_program` is the only level guard for all
#: four, and its argument ("a summed signal reaches every driver, so clamp to
#: the most restrictive cap") holds identically before and after apply.
#:
#: ``PHASE_ENTRY_BASELINE`` is a member for a stronger reason than the other
#: three: for the two clouds this is an efficiency (one composed program serves
#: them all), but for the entry baseline it is the CORRECTNESS condition.
#: #2291's before→after comparison is checked by ``program_id`` equality, and
#: equality holds because :func:`program_for_phase` hands both sides the
#: identical program OBJECT. Removing it from this set would not merely change
#: the stimulus — it would make every round's benefit verdict
#: :data:`~jasper.active_speaker.crossover_v2.verification.BENEFIT_PROGRAM_MISMATCH`.
SUMMED_SWEEP_PHASES = frozenset(
    {PHASE_VERIFY, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_ENTRY_BASELINE}
)


class NoProgramForPhaseError(RuntimeError):
    """This session composes no excitation for that phase."""


# --------------------------------------------------------------------------- #
# the session's own declarations, and the three programs they compose
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionExcitation:
    """What one session may play, and how loud — its declarations, bundled.

    Four values that were four conductor fields, frozen together because every
    composer below reads a subset of them and a subset that could drift is how a
    program gets composed at one level and budgeted at another. Construction
    copies ``caps_dbfs`` behind a read-only view for the same reason.
    """

    #: The two driver role/band declarations, woofer first.
    roles: tuple[RoleBand, ...]
    #: Per-role excitation ceiling, dBFS. The min across roles is what clamps a
    #: summed signal, which reaches every driver.
    caps_dbfs: Mapping[str, float]
    #: The session's own output level, which every per-driver gain folds through.
    session_volume_db: float
    #: The declared crossover corner, for the summed sweep's shape.
    fc_hz: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(
            self, "caps_dbfs", MappingProxyType(dict(self.caps_dbfs)),
        )

    @property
    def leading_pilot_role(self) -> str:
        """The role whose solved gain the leading pilot pair rides — the woofer.

        First in ``roles`` by the conductor's own 2-way contract, named here so
        the composers below state the rule rather than repeating ``roles[0]``.
        """
        return self.roles[0].role

    def pilot_gains(self, hi_gain_db: float) -> tuple[float, float]:
        """The ``(lo, hi)`` pilot pair at a given level, delta preserved."""
        return (hi_gain_db - PILOT_LEVEL_DELTA_DB, hi_gain_db)

    def check_program(self) -> ExcitationProgram:
        """CHECK's two-pilot behavioural probe, clamped PER ROLE.

        Cap-aware (W6.1): each driver's pilot base is clamped so the loudest
        (hi) pilot's effective peak stays under that driver's cap folded through
        the session volume — the same :func:`back_off_gain` margin the MEASURE
        composer uses. The tweeter (compression driver, deep cap) rides a base
        ~40 dB below the woofer's; both pilots keep their fixed
        ``DEFAULT_PILOT_LEVELS_DB`` offsets against that per-role base, so the
        10 dB behavioral-linearity delta is preserved while the absolute level
        degrades honestly (recorded in the segment gains). Before this the CHECK
        program used the shared reference base and admission refused it on the
        JTS3 tweeter (``program_channel_peak_over_cap``).
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
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )

    def measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's per-driver sweeps at the solved gains, clamped PER ROLE."""
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
            downstream_gain_db=self.session_volume_db,
            leading_pilot_gains_db=self.pilot_gains(gains[self.leading_pilot_role]),
            leading_pilot_role=self.leading_pilot_role,
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )

    def verify_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        """The mono summed sweep, clamped to the MOST RESTRICTIVE cap.

        Cap-aware (W6.1): VERIFY plays a MONO summed sweep through the APPLIED
        production graph with NO play-time admission gate (it does not ride
        ``play_program``/``readmit`` — see ``bind_production_play``), so the
        compose-time clamp here is the ONLY level guard. A summed signal reaches
        every driver, so it is clamped to the MOST RESTRICTIVE (min) cap: at the
        worst case (no crossover attenuation) no driver is driven past its own
        limit. Without this the summed sweep played at the shared reference base
        (effective ~-32 dBFS) would over-drive a deep-cap tweeter (e.g. the JTS3
        B&C DE250 at -65 dBFS effective). The :meth:`pilot_gains` pair rides the
        same clamped level, so its 10 dB delta is preserved. A genuinely-too-
        quiet clamp surfaces as the existing snr_floor / agc_behavioral_fail
        verdicts, not a precheck (§5.10).
        """
        binding_cap = min(self.caps_dbfs.values()) if self.caps_dbfs else 0.0
        gain = back_off_gain(
            BASE_STIMULUS_PEAK_DBFS - extra_backoff_db,
            self.session_volume_db,
            binding_cap,
        )
        return build_verify_program(
            self.fc_hz,
            gain_db=gain,
            downstream_gain_db=self.session_volume_db,
            leading_pilot_gains_db=self.pilot_gains(gain),
            courtesy_prelude=COURTESY_PRELUDE_ENABLED,
        )


def program_for_phase(
    phase: str,
    *,
    check: ExcitationProgram,
    measure: ExcitationProgram | None,
    verify: ExcitationProgram,
) -> ExcitationProgram:
    """Which composed program this phase plays — **by identity, not by value**.

    The caller passes the objects it is holding and gets one of them back. That
    is the whole mechanism behind invariant 2 in this module's docstring: no
    branch here composes, copies, or replaces, so every
    :data:`SUMMED_SWEEP_PHASES` member is answered with the *same* ``verify``
    object and their captures share one ``program_id``.

    ``measure`` is ``None`` until the CHECK gain solve produces a plan;
    requesting MEASURE (or a lateral pose, which replays MEASURE's program
    verbatim) before then raises :class:`NoProgramForPhaseError` rather than
    composing something at a guessed level.
    """
    if phase == PHASE_CHECK:
        return check
    # R16: a lateral pose replays the ANCHOR's program object VERBATIM — same
    # stimulus, same solved per-driver gains, same protected-neutral graph. That
    # identity is not an optimisation: the return-to-mark bracket and every §4.4
    # falloff comparison are differences against the anchor, and a pose measured
    # at a different level or with a different sweep would make those
    # differences uninterpretable.
    if phase in (PHASE_MEASURE, PHASE_LATERAL):
        if measure is None:
            raise NoProgramForPhaseError(
                "MEASURE armed before the CHECK gain solve produced a program"
            )
        return measure
    if phase in SUMMED_SWEEP_PHASES:
        # One composed mono summed sweep serves VERIFY and both position groups:
        # identical excitation, identical min-cap clamp, identical
        # ``program.phase`` ("verify") so the analyzer routes it to
        # ``_analyze_verify`` unchanged. What differs between the three is the
        # PRIORS the conductor hands the analysis and the verdict it draws —
        # never the sound the speaker makes.
        return verify
    raise NoProgramForPhaseError(f"no program for phase {phase!r}")
