# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What a commission session plays, how loud, and for which phase (#2291).

Phase 5's second vertical, and the *first* leg of the issue's sequence: before
anything can be captured, something has to be played.  Three questions had no
owner of their own and were answered from six places on
:class:`~jasper.active_speaker.crossover_v2_flow.CrossoverV2Session`:

* **How loud may a driver be driven?**  :func:`back_off_gain` — the one clamp
  that folds a per-driver digital gain through the session volume and holds it
  under that driver's cap.
* **What does each phase play?**  The four composers below, which are what the
  speaker actually plays.  They are not the flow's only callers of
  ``jasper.audio_measurement.program``'s builders: ``build_v2_capture_plan`` and
  ``build_v2_verify_capture_plan`` compose their own NOMINAL programs to derive
  the phone's recording window.  That second pair is the whole reason
  :func:`courtesy_prelude_for_phase` is one shared rule rather than a decision
  taken in each place — see its own note below.
* **Which composed program does a phase get?**  :func:`program_for_phase`.

**This module is hearing-safety territory, and its invariants are stated so
they can be checked rather than trusted:**

1. :meth:`SessionExcitation.verify_program`'s min-cap clamp is the ONLY level
   guard on the
   mono summed sweep.  That sweep plays through the applied production graph
   with no play-time admission gate (see
   ``jasper.web.correction_crossover_v2.bind_production_play``), so nothing
   downstream will catch a gain this function gets wrong.
   :meth:`SessionExcitation.cloud_program` is the same sweep at the same clamp
   — it composes THROUGH ``verify_program`` rather than beside it, so there is
   one clamp and not two.
2. The two phases whose captures are **compared** — :data:`PHASE_ENTRY_BASELINE`
   and :data:`PHASE_VERIFY` — receive the **identical program object**, not an
   equal one.  #2291's before→after benefit verdict is checked by ``program_id``
   equality, and that equality holds because :func:`program_for_phase` hands
   both sides the same object.  A copy that merely compared equal today would be
   a latent :data:`~.verification.BENEFIT_PROGRAM_MISMATCH` the first time
   composition picked up any per-call state.  The position groups
   (:data:`GROUP_SUMMED_SWEEP_PHASES`) share one object with each other for the
   same reason and differ from the compared pair in exactly one respect: they
   carry no courtesy prelude (see :func:`courtesy_prelude_for_phase`).  Nothing
   compares a position's ``program_id`` to anything — a cloud position's
   retained record does not even carry the field
   (``jasper.active_speaker.crossover_v2.spatial.cloud_position_record``) — so
   that split costs no comparability.
3. A lateral pose replays the MEASURE object verbatim (:func:`program_for_phase`
   again), so the prelude rule is asked of the OBJECT, never of the pose.

Both are pinned by ``tests/test_crossover_v2_programs.py`` against golden
identities captured from the pre-extraction conductor.

**What this module is not.**  It composes; it does not decide when to play, does
not consume a capture, does not journal, and holds no session state — a
:class:`SessionExcitation` is a frozen bundle of the session's own declarations
and every composer is a pure function of it.  The session still owns *when* a
program is composed and *which* composed objects it holds, because those are
lifecycle facts: MEASURE cannot be composed until the CHECK gain solve produces
a plan, and it is recomposed on the clip-retry rearm.

Dependency direction, as for every module here: no ``jasper.web`` import and
nothing from :mod:`jasper.active_speaker.crossover_v2_flow`.
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

#: The gain solver backs off this far below each driver's exact cap. The W2 gate
#: found ``prepare_driver_excitation_plan``'s strict ``>`` can refuse an
#: exactly-at-cap plan by one ulp, so a hair of headroom keeps an at-cap solve
#: admissible.
GAIN_CAP_BACKOFF_DB = 0.01

#: The two pilot levels are this far apart (matches the CHECK behavioral check).
PILOT_LEVEL_DELTA_DB = abs(DEFAULT_PILOT_LEVELS_DB[1] - DEFAULT_PILOT_LEVELS_DB[0])

#: The phases whose capture OPENS a session's playback, and so carries the
#: courtesy prelude (issue #1677). No env/config switch: a household that runs a
#: session gets the warning.
#:
#: :data:`PHASE_ENTRY_BASELINE` is not an opener — it is stage 1's LAST capture.
#: It is a member because it PLAYS the announced program: ``program_for_phase``
#: hands it and :data:`PHASE_VERIFY` the same object, and it is that OBJECT
#: DISPATCH — not this set — which makes their ``program_id``s equal (#2291's
#: before→after comparison and ``_delta_probe``'s anchor check). What this
#: membership decides is the other reader: ``build_v2_capture_plan`` sizes the
#: entry baseline's recording window from it, so dropping it here would budget
#: the phone 3.6 s short of the program the speaker actually plays.
COURTESY_PRELUDE_PHASES = frozenset(
    {PHASE_CHECK, PHASE_VERIFY, PHASE_ENTRY_BASELINE}
)


def leading_pilot_role(roles: Sequence[RoleBand]) -> str:
    """The role whose solved gain the leading pilot pair rides — the lowest.

    Named so every composer states the rule rather than repeating ``roles[0]``,
    including :func:`~.harmonic_evidence.rebuild_measure_program`, which holds
    no session object to ask.
    """
    return roles[0].role


def pilot_gains(hi_gain_db: float) -> tuple[float, float]:
    """The ``(lo, hi)`` pilot pair at a given level, delta preserved."""
    return (hi_gain_db - PILOT_LEVEL_DELTA_DB, hi_gain_db)


def courtesy_prelude_for_phase(phase: str) -> bool:
    """Does this phase's capture announce itself with the courtesy prelude?

    **The prelude announces a SESSION, not a capture** (issue #1677, trimmed
    2026-08-18). #1677 is a live incident: a headless session's first sweep
    started while the owner was playing music, and the session had to be voided
    and re-run. What it asked for is a warning before *the first sound a session
    makes*, from the speaker itself, so the room can go quiet. Stage 1 opens on
    :data:`PHASE_CHECK` and stage 2 — a separate relay session, begun after the
    household has reviewed and applied — opens on :data:`PHASE_VERIFY`; both
    open on a warning.

    What it does NOT ask for is a re-warning before every capture a Full
    journey plays. Two things that were not true when the prelude shipped make
    the repeat redundant rather than merely cheap:

    * the mux **measurement window is held for the whole session**, so no
      household audio can start mid-session for a sweep to collide with — the
      incident's own hazard is closed at the session boundary, which is where
      the warning now sits;
    * every capture after the first is begun deliberately — a hand-walked tier
      taps "I'm there — play the tone" at each position (``_entry_advance``'s
      ``AUTO_ADVANCE_TAP``), an externally positioned one is released by its
      driver through the position gate, and a hand-walked round on the WIRED
      source (#2879) does BOTH: it keeps the tap and its begins are held by
      that same gate until the person releases them — inside a session the
      room has already been told is running.

    A repeat therefore carries no information the household does not have, and
    it costs 3.6 s (0.6 s of beeps + a 3.0 s settle) of held-still silence each
    time. Every capture used to pay it; on a Full journey only the three that
    open or anchor a session still do.

    **Why it is one shared rule rather than a decision at each site.** Every
    ``build_v2_capture_plan`` / ``build_v2_verify_capture_plan`` entry (the
    phone's DURATION BUDGET) and every composer below (the actual playback) asks
    THIS function, so the two can never disagree about whether the prelude is
    present — the phone would otherwise budget a shorter recording window than
    the program it is actually capturing (see the ``+3.6 s`` proof in
    ``tests/test_crossover_v2_conductor.py``, mirroring PR-A's ``+15 s`` MEASURE
    lengthening). ``jasper.audio_measurement.program``'s own composers default
    ``courtesy_prelude`` to ``False`` so every OTHER caller (tests, future tools)
    keeps its byte-identical shape unless it opts in explicitly.
    """
    return phase in COURTESY_PRELUDE_PHASES


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

#: The :data:`SUMMED_SWEEP_PHASES` members that are prompted POSITION GROUPS.
#: They play :meth:`SessionExcitation.cloud_program` — the same sweep at the same
#: clamp as :meth:`SessionExcitation.verify_program`, carrying no courtesy
#: prelude because a position is not a session opener
#: (:func:`courtesy_prelude_for_phase`).
#:
#: The complement — ``SUMMED_SWEEP_PHASES - GROUP_SUMMED_SWEEP_PHASES`` — is the
#: COMPARED pair, and it is the pair invariant 2 is about. Splitting the family
#: here costs no comparability because nothing compares a position's
#: ``program_id`` to anything: ``spatial.cloud_position_record`` does not carry
#: the field, and ``spatial.entry_baseline_record`` — which does — belongs to the
#: other half.
GROUP_SUMMED_SWEEP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY})


class NoProgramForPhaseError(RuntimeError):
    """This session composes no excitation for that phase."""


# --------------------------------------------------------------------------- #
# the session's own declarations, and the three programs they compose
# --------------------------------------------------------------------------- #


def measurement_band_hz(roles: Sequence[RoleBand]) -> tuple[float, float]:
    """The summed system's swept band — the union of every declared
    ``RoleBand.band``, which for ONE declaration is that declaration itself.

    Each ``RoleBand.band`` is one driver's own excitation-ceiling band (from
    ``excitation_safety_plan.resolve_driver_excitation_ceilings``); no other
    function composes across roles. It lives here because it is session-owned
    wiring policy (which roles participate in the passband), not a pure-DSP
    concern that belongs in ``spatial_combine`` or ``audio_measurement.program``.
    """
    return (
        min(float(rb.band.lower_hz) for rb in roles),
        max(float(rb.band.upper_hz) for rb in roles),
    )


@dataclass(frozen=True)
class SessionExcitation:
    """What one session may play, how loud, and for how long — its
    declarations, bundled.

    Five values the composers below read, frozen together because each reads a
    subset and a subset that could drift is how a program gets composed at one
    level and budgeted at another. The first four were conductor fields;
    ``sweep_duration_limits_s`` arrived with the fit (#2921) and is bundled here
    for the same reason. Construction copies both mappings behind read-only
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
    #: a 1-way main, which has none — the summed sweep then takes its shape from
    #: the declared band (:meth:`_summed_sweep`).
    fc_hz: float | None
    #: Per-role longest admissible ONE sweep, seconds — the resolver's
    #: ``effective_sweep_duration_limit_s``, which is also what the admission
    #: gate compares each composed segment against. MEASURE composes to it (see
    #: :meth:`measure_program`); a role absent here composes at its nominal.
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
        """This session's leading pilot role — see :func:`leading_pilot_role`."""
        return leading_pilot_role(self.roles)

    def pilot_gains(self, hi_gain_db: float) -> tuple[float, float]:
        """This session's pilot pair — see :func:`pilot_gains`."""
        return pilot_gains(hi_gain_db)

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
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_CHECK),
        )

    def measure_program(
        self, gain_plan_db: Mapping[str, float], *, extra_backoff_db: float = 0.0,
    ) -> ExcitationProgram:
        """MEASURE's per-driver sweeps at the solved gains, clamped PER ROLE
        and fitted to each role's duration limit.

        Also what every lateral pose plays, verbatim
        (:func:`program_for_phase`) — so the prelude question is asked of
        MEASURE, the object's own phase, and a pose inherits the answer.

        Duration-aware (#2921): a sweep realizes at the nearest phase-closing
        length, so the composer's nominal 4 s woofer realizes 4.00577 s over
        150–4000 Hz and admission refused the whole program against a declared
        4 s limit (``program_segment_outside_limits``, deterministic — it killed
        the arm walk 2-for-2 on jts3). Handing the composer
        :attr:`sweep_duration_limits_s` makes it compose the longest
        phase-closing sweep AT OR BELOW that limit instead. The admission
        comparison is unchanged and stays the independent tripwire; what changed
        is that the request now fits the limit it will be judged against.
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
        return self._summed_sweep(
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY),
            extra_backoff_db=extra_backoff_db,
        )

    def cloud_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        """A prompted position's summed sweep — :meth:`verify_program`, unannounced.

        The SAME sweep through the SAME clamp: both go through
        :meth:`_summed_sweep`, so invariant 1's "only level guard" stays one
        function and cannot drift into two. The single difference is the
        courtesy prelude, which a position does not carry because it does not
        open a session (:func:`courtesy_prelude_for_phase`) — 3.6 s a household
        holds a microphone still, per position.

        A separate METHOD rather than a flag on ``verify_program`` because the
        two are held as distinct objects for a whole session and
        :func:`program_for_phase` answers by identity: "which object is this"
        must stay a question about the phase, not about an argument someone
        passed at a call site.
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

    The caller passes the objects it is holding and gets one of them back. That
    is the whole mechanism behind invariant 2 in this module's docstring: no
    branch here composes, copies, or replaces, so the COMPARED pair (VERIFY and
    the entry baseline) is answered with the *same* ``verify`` object and their
    captures share one ``program_id``. Every :data:`GROUP_SUMMED_SWEEP_PHASES`
    position is likewise answered with the same ``cloud`` object — the identical
    sweep at the identical clamp, minus the courtesy prelude no position needs.

    ``measure`` is ``None`` until the CHECK gain solve produces a plan;
    requesting MEASURE before then raises :class:`NoProgramForPhaseError` rather
    than composing something at a guessed level. Three callers reach that arm,
    and they are worth naming because two of them do not spell "measure": the
    MEASURE anchor itself, a lateral pose (which replays MEASURE's program
    verbatim), and a per-driver stop of an angle-capture request
    (:mod:`jasper.active_speaker.angle_capture`, which asks this function for
    ``PHASE_MEASURE``'s object by that same identity rule).
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
    if phase in GROUP_SUMMED_SWEEP_PHASES:
        # One composed sweep serves both position groups. Same excitation, same
        # min-cap clamp, same ``program.phase`` ("verify") so the analyzer
        # routes it to ``_analyze_verify`` unchanged; what differs from the
        # compared pair below is the courtesy prelude alone, which is
        # analysis-invisible by construction (``KIND_COURTESY_TONE`` is not a
        # ``STIMULUS_KIND``) and which no position needs.
        return cloud
    if phase in SUMMED_SWEEP_PHASES:
        # The COMPARED pair — VERIFY and the entry baseline — is one object, and
        # that identity is invariant 2. What differs between the two is the
        # PRIORS the session hands the analysis and the verdict it draws — never
        # the sound the speaker makes.
        return verify
    raise NoProgramForPhaseError(f"no program for phase {phase!r}")
