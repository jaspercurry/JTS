# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compose bounded per-driver and summed measurement programs.

Summed sweeps use the tightest driver cap and duration. Scoped playback also
re-admits the rendered artifact against its protected graph.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    DEFAULT_PILOT_LEVELS_DB,
    MEASURE_SWEEP_BAND_HZ,
    ExcitationProgram,
    RoleBand,
    build_check_program,
    build_level_probe_program,
    build_measure_program,
    build_summed_level_probe_program,
    build_verify_program,
)
from jasper.audio_measurement.excitation import NEAR_FIELD_SILENCE_S
from jasper.audio_measurement.ramp import MAX_STEP_DB

from jasper.audio_measurement.branch_program import build_branch_program

from .measure_spec import branch_channels_for, solo_target
from .journey import (
    PHASE_CHECK,
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
    PHASE_TIMING,
    PHASE_VERIFY,
)

# --------------------------------------------------------------------------- #
# level policy
# --------------------------------------------------------------------------- #

#: The gain solver backs off this far below each driver's exact cap: testing
#: found ``prepare_driver_excitation_plan``'s strict ``>`` can refuse an
#: exactly-at-cap plan by one ulp.
GAIN_CAP_BACKOFF_DB = 0.01

# Without a graph-to-anchor gain reference, blind pilots keep a conservative cut.
CHECK_PROBE_BACKOFF_DB = 12.0

#: A level probe's first burst plays this loud at the output, its fader plus its
#: digital gain (ADR-0405).
LEVEL_PROBE_START_OUTPUT_DBFS = -60.0

#: A driver's take plays up to digital full scale, under its cap and the run's
#: fader: the seat-equivalent cap is removed (ADR-0403 §4).
DRIVER_TAKE_CEILING_DBFS = 0.0

#: The two pilot levels are this far apart (matches the CHECK behavioral check).
PILOT_LEVEL_DELTA_DB = abs(DEFAULT_PILOT_LEVELS_DB[1] - DEFAULT_PILOT_LEVELS_DB[0])

#: The phases whose capture OPENS a session's playback, and so carries the
#: courtesy prelude (#1677). No env/config switch. :data:`PHASE_TIMING` plays
#: VERIFY's program, prelude included.
COURTESY_PRELUDE_PHASES = frozenset(
    {PHASE_CHECK, PHASE_VERIFY, PHASE_TIMING}
)


def leading_pilot_role(roles: Sequence[RoleBand]) -> str:
    """The role whose solved gain the leading pilot pair rides — the lowest."""
    return roles[0].role


def pilot_gains(hi_gain_db: float) -> tuple[float, float]:
    """The ``(lo, hi)`` pilot pair at a given level, delta preserved."""
    return (hi_gain_db - PILOT_LEVEL_DELTA_DB, hi_gain_db)


def courtesy_prelude_for_phase(phase: str) -> bool:
    """Announce a session, not every take (#1677).

    Capture budgets share this decision so the prelude cannot overrun recording.
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

SUMMED_SWEEP_PHASES = frozenset(
    {PHASE_VERIFY, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY, PHASE_TIMING}
)

#: Position groups omit the courtesy prelude: each pose is not a new session.
GROUP_SUMMED_SWEEP_PHASES = frozenset({PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY})


class NoProgramForPhaseError(RuntimeError):
    """This session composes no excitation for that phase."""


def _scope_backoff_db(spec: Any) -> float:
    """The dB a summed take plays under the seat-equivalent level: how far its
    graph plays over the level anchor's, never negative."""
    return max(0.0, max((gain for role, gain in (spec.scope_gains_db or {}).items()
                         if not spec.branch_target_ids or role in spec.branch_target_ids), default=0.0))


def _stimulus_backoff_db(spec: Any, stimulus_dbfs: float | None) -> float:
    """How far under the base peak a summed take's stimulus plays: its graph's rise
    over the level anchor's, or the peak it asks for, whichever is lower. A retake
    plays the peak it asks for, never above its first attempt's (#5709)."""
    backoff = _scope_backoff_db(spec)
    return backoff if stimulus_dbfs is None else max(backoff, BASE_STIMULUS_PEAK_DBFS - stimulus_dbfs)


def _reserved_caps(excitation: SessionExcitation, spec: Any) -> dict[str, float]:
    """Each driver's cap less the dynamic bass boost the take's graph keeps on its
    output, the cap admission checks it against (ADR-0359)."""
    reserve = spec.bass_reserve_db or {}
    return {target: cap - reserve.get(target, 0.0) for target, cap in excitation.caps_dbfs.items()}


def _alone_gains_db(excitation: SessionExcitation, spec: Any) -> dict[str, float]:
    """Each branch of a branch take alone at its own level, by the sum's stimulus rule,
    under the tightest reserved cap of the take's two branches: the ceiling admission
    holds every channel of the take to (ADR-0407)."""
    if not spec.branch_levels_dbfs:
        return {}
    caps = _reserved_caps(excitation, spec)
    ceiling = min(caps[target] for target in spec.branch_target_ids)
    return {target: back_off_gain(BASE_STIMULUS_PEAK_DBFS - _stimulus_backoff_db(spec, level),
                                  excitation.session_volume_db, ceiling)
            for target, level in zip(spec.branch_target_ids, spec.branch_levels_dbfs)}


def compose_summed_program(excitation: SessionExcitation, spec: Any, stimulus_dbfs: float | None = None, *,
                           safety_profile: Mapping[str, Any], role_targets: Mapping[str, str]) -> ExcitationProgram:
    # The sum plays under every driver's reserved cap, so admission refuses none of it (ADR-0408).
    excitation = replace(excitation, summed_sweep_band_hz=spec.sweep_band_hz or None,
                         caps_dbfs=_reserved_caps(excitation, spec))
    backoff = _stimulus_backoff_db(spec, stimulus_dbfs)
    if spec.stimulus is not None:
        from ..bass_stimulus import build_bass_program  # lazy: keeps jasper.web numpy-free

        program = build_bass_program(excitation, spec.stimulus, safety_profile=safety_profile,
                                     role_targets=role_targets, extra_backoff_db=backoff,
                                     courtesy_prelude=courtesy_prelude_for_phase(spec.program_phase))
    else:
        program = (excitation.cloud_program(extra_backoff_db=backoff) if spec.program_phase == PHASE_CLOUD_VERIFY
                   else excitation.verify_program(extra_backoff_db=backoff, sweep_s=spec.sweep_s))
    return program


def _solo_take(excitation: SessionExcitation, spec: Any) -> tuple[RoleBand, float, int]:
    """A driver pose's target and band, its take's ceiling (full scale under the
    target's cap and the run's fader), and the width of the graph that plays it."""
    from ..camilla_yaml import program_channel_count  # lazy: import cost, the emitter package for one max()

    target = solo_target(spec)
    return (RoleBand(target, 0, excitation.target_bands[target]),
            back_off_gain(DRIVER_TAKE_CEILING_DBFS, excitation.session_volume_db, excitation.caps_dbfs[target]),
            program_channel_count(branch_channels_for(spec)))


def _solo_sweeps(spec: Any, role: str) -> dict[str, Any]:
    """A one-driver take plays its declared stimulus's band, sweep length and
    silences (the near-field row: ADR-0360 §4, #5684); with none, the band its
    spec states (a branch probe's: ADR-0403 §3), else MEASURE's band, with
    MEASURE's spacing (#5696)."""
    if spec.stimulus is None:
        return {"sweep_band_hz": spec.sweep_band_hz or MEASURE_SWEEP_BAND_HZ}
    gap_s = spec.stimulus["gap_s"]
    return {"sweep_band_hz": tuple(spec.stimulus["band_hz"]), "sweep_durations": {role: spec.stimulus["sweep_s"]},
            "gap_s": gap_s, "guard_s": gap_s / 2, "pilot_gap_s": gap_s / 2}


def compose_target_program(excitation: SessionExcitation, spec: Any,
                           stimulus_dbfs: float | None = None) -> ExcitationProgram:
    """A one-driver take's program: the one target its spec names, alone, its
    pilots and bit-identical sweeps on its own channel at its own band, cap and
    duration limit.

    The program is as wide as the graph that plays it
    (:func:`~jasper.active_speaker.camilla_yaml.program_channel_count`), so every
    channel but the target's is written silent rather than left to the ring.
    ``stimulus_dbfs`` is the peak a take asks for, never above its ceiling
    (ADR-0403 §4).
    """
    band, ceiling, channels = _solo_take(excitation, spec)
    gain = ceiling if stimulus_dbfs is None else min(ceiling, stimulus_dbfs)
    return build_measure_program(
        {band.role: gain}, (band,), **_solo_sweeps(spec, band.role),
        sweep_duration_limits_s={band.role: excitation.sweep_duration_limits_s[band.role]},
        downstream_gain_db=excitation.session_volume_db,
        leading_pilot_gains_db=pilot_gains(gain), leading_pilot_role=band.role,
        courtesy_prelude=courtesy_prelude_for_phase(spec.program_phase), channels=channels,
    )


def _probe_gains(fader_db: float, ceiling: float) -> tuple[float, ...]:
    """A probe's burst gains: at most ``MAX_STEP_DB`` apart, from −60 dBFS at the
    output to the take's own ceiling (ADR-0365, ADR-0405)."""
    start = min(LEVEL_PROBE_START_OUTPUT_DBFS - fader_db, ceiling)
    steps = math.ceil(round((ceiling - start) / MAX_STEP_DB, 6))
    return tuple(min(start + step * MAX_STEP_DB, ceiling) for step in range(steps + 1))


def probe_fader_db(caps_dbfs: Mapping[str, float]) -> float:
    """The fader a run's probe plays at: full scale reaches the loudest driver
    cap, never over 0 dB (ADR-0403 §4)."""
    return min(0.0, max(caps_dbfs.values()))


def _probe_ceiling(probe: ExcitationProgram, caps_dbfs: Mapping[str, float]) -> tuple[float, float, bool]:
    """A run probe's last burst gain, the fader it played at, and whether the tightest cap held that burst."""
    last = max(probe.stimulus_segments(), key=lambda segment: segment.gain_db)
    fader = last.effective_peak_dbfs - last.gain_db
    return last.gain_db, fader, last.gain_db >= back_off_gain(math.inf, fader, min(caps_dbfs.values())) - 1e-9


def run_fader_db(probe: ExcitationProgram, solved_dbfs: float, caps_dbfs: Mapping[str, float],
                 cut_db: float = 0.0) -> float:
    """The fader at which the take a run probed, with no level asked, plays no
    louder than the peak its probe solved, or than its own last burst when that
    is lower, less ``cut_db``, never above the probe's own fader (ADR-0403 §4).
    The probe's last burst is that take's level unless the tightest cap held it
    lower; then the fader is solved against the summed level before any scope
    cut, which no summed take plays over, so a take that cap holds at the output
    comes down too."""
    ceiling, fader, held = _probe_ceiling(probe, caps_dbfs)
    target = min(solved_dbfs, ceiling) - cut_db
    level = BASE_STIMULUS_PEAK_DBFS if held and target < ceiling else ceiling
    return min(fader, fader + target - level)


def probe_backoff_db(probe: ExcitationProgram, caps_dbfs: Mapping[str, float]) -> float:
    """How far a summed take on another graph can play over the take a run
    probed: that take's own scope backoff. None when the tightest cap held the
    probe, since that cap holds every summed take (ADR-0403 §4)."""
    ceiling, _, held = _probe_ceiling(probe, caps_dbfs)
    return 0.0 if held else max(0.0, BASE_STIMULUS_PEAK_DBFS - ceiling)


def compose_level_probe(excitation: SessionExcitation, spec: Any) -> ExcitationProgram:
    """A driver pose's level probe: its take's target, band and ceiling (ADR-0365)."""
    band, ceiling, channels = _solo_take(excitation, spec)
    return build_level_probe_program(
        band, _probe_gains(excitation.session_volume_db, ceiling),
        sweep_band_hz=_solo_sweeps(spec, band.role)["sweep_band_hz"], gap_s=NEAR_FIELD_SILENCE_S,
        downstream_gain_db=excitation.session_volume_db, channels=channels,
    )


def compose_summed_probe(excitation: SessionExcitation, spec: Any, *, safety_profile: Mapping[str, Any],
                         role_targets: Mapping[str, str]) -> ExcitationProgram:
    """A driverless summed take's level probe: the same bursts, of the band its take sweeps, up to the
    summed gain its take plays at when no level is asked (ADR-0403)."""
    backoff = _scope_backoff_db(spec)
    if spec.stimulus is not None:
        from ..bass_stimulus import bass_band_hz  # lazy: keeps jasper.web numpy-free

        band = bass_band_hz(excitation, spec.stimulus, safety_profile=safety_profile, role_targets=role_targets)
    else:
        band = spec.sweep_band_hz or measurement_band_hz(excitation.roles)
    return build_summed_level_probe_program(
        _probe_gains(excitation.session_volume_db, excitation._summed_gain(backoff)),
        sweep_band_hz=band, gap_s=NEAR_FIELD_SILENCE_S, downstream_gain_db=excitation.session_volume_db,
    )


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
    #: Per-target longest admissible ONE sweep, seconds — the resolver's
    #: ``effective_sweep_duration_limit_s``, which is also what the admission
    #: gate compares each composed segment against. A role absent here composes
    #: at its nominal.
    sweep_duration_limits_s: Mapping[str, float]
    summed_sweep_band_hz: tuple[float, float] | None = None
    #: Per-target permitted band, measurement target id -> band, for a take
    #: that plays one target alone; the roles above carry the primary ones'.
    target_bands: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", tuple(self.roles))
        for name in ("caps_dbfs", "sweep_duration_limits_s", "target_bands"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def leading_pilot_role(self) -> str:
        """This session's leading pilot role."""
        return leading_pilot_role(self.roles)

    def pilot_gains(self, hi_gain_db: float) -> tuple[float, float]:
        """This session's pilot pair."""
        return pilot_gains(hi_gain_db)

    def check_program(self, *, extra_backoff_db: float = 0.0,
                      scope_gains_db: Mapping[str, float] | None = None) -> ExcitationProgram:
        """Probe at the summed base, with paired backoff for the graph and retries."""
        summed_base = self._summed_gain()
        role_base = {
            rb.role: min(
                back_off_gain(
                    BASE_STIMULUS_PEAK_DBFS,
                    self.session_volume_db,
                    self.caps_dbfs.get(rb.role, 0.0),
                ),
                summed_base,
            ) - max(0.0, extra_backoff_db) - max(0.0, (scope_gains_db or {}).get(rb.role, 0.0))
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

    def verify_program(
        self, *, extra_backoff_db: float = 0.0, sweep_s: float | None = None,
        courtesy_prelude: bool | None = None, leading_pilots: bool = True,
    ) -> ExcitationProgram:
        """The mono summed sweep, bounded by every driven role's cap and duration."""
        return self._summed_sweep(
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_VERIFY) if courtesy_prelude is None else courtesy_prelude,
            leading_pilots=leading_pilots,
            extra_backoff_db=extra_backoff_db,
            sweep_s=sweep_s,
        )

    def cloud_program(self, *, extra_backoff_db: float = 0.0) -> ExcitationProgram:
        """The same summed sweep without a courtesy prelude at each pose."""
        return self._summed_sweep(
            courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_VERIFY),
            extra_backoff_db=extra_backoff_db,
        )

    def _summed_sweep(
        self, *, courtesy_prelude: bool, extra_backoff_db: float, sweep_s: float | None = None,
        leading_pilots: bool = True,
    ) -> ExcitationProgram:
        gain = self._summed_gain(extra_backoff_db)
        band = measurement_band_hz(self.roles)
        return build_verify_program(
            self.fc_hz,
            roles=self.roles,
            sweep_duration_limits_s=self.sweep_duration_limits_s,
            measurement_band_hz=band,
            sweep_band_hz=self.summed_sweep_band_hz or band,
            gain_db=gain,
            downstream_gain_db=self.session_volume_db,
            leading_pilot_gains_db=self.pilot_gains(gain) if leading_pilots else None,
            courtesy_prelude=courtesy_prelude,
            **({"sweep_s": sweep_s} if sweep_s is not None else {}),
        )

    def _summed_gain(self, extra_backoff_db: float = 0.0) -> float:
        binding_cap = min(self.caps_dbfs.values()) if self.caps_dbfs else 0.0
        return back_off_gain(
            BASE_STIMULUS_PEAK_DBFS - extra_backoff_db,
            self.session_volume_db,
            binding_cap,
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

    The timing take and VERIFY get the same ``verify`` object (shared
    ``stimulus_id``), and every :data:`GROUP_SUMMED_SWEEP_PHASES` position gets
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
        # it unchanged. What differs from ``verify`` is the courtesy
        # prelude alone, which is analysis-invisible (``KIND_COURTESY_TONE`` is
        # not a ``STIMULUS_KIND``).
        return cloud
    if phase in SUMMED_SWEEP_PHASES:
        # What differs between the two is the PRIORS the session hands the
        # analysis and the verdict it draws — never the sound the speaker makes.
        return verify
    raise NoProgramForPhaseError(f"no program for phase {phase!r}")


def program_for_spec(spec: Any, excitation: SessionExcitation, gain_plan_db: Mapping[str, float] | None,
                     stimulus_dbfs: float | None = None, *, safety_profile: Mapping[str, Any],
                     role_targets: Mapping[str, str]) -> ExcitationProgram:
    if spec.level_probe and stimulus_dbfs is None and spec.graph_scope != "candidate_branches":
        # A take that finds its level plays its probe until a level is asked; a branch take
        # plays its branches' own probes first (branch_probes; ADR-0365, ADR-0403).
        return (compose_level_probe(excitation, spec) if solo_target(spec) else
                compose_summed_probe(excitation, spec, safety_profile=safety_profile, role_targets=role_targets))
    if solo_target(spec):
        return compose_target_program(excitation, spec, stimulus_dbfs)
    if spec.program_phase == PHASE_CHECK:
        fallback = CHECK_PROBE_BACKOFF_DB if spec.scope_gains_db is None else 0.0
        program = excitation.check_program(extra_backoff_db=fallback)
        peak = max(segment.gain_db for segment in program.stimulus_segments())
        return excitation.check_program(
            extra_backoff_db=fallback + (0.0 if stimulus_dbfs is None else max(0.0, peak - stimulus_dbfs)),
            scope_gains_db=spec.scope_gains_db)
    if spec.graph_scope == "drivers":
        gains = gain_plan_db
        if not gains:
            raise ValueError("The CHECK level solve is unavailable")
        if stimulus_dbfs is not None and stimulus_dbfs != max(gains.values()):
            delta = stimulus_dbfs - max(gains.values())
            gains = {role: gain + delta for role, gain in gains.items()}
        return excitation.measure_program(gains)
    program = compose_summed_program(excitation, spec, stimulus_dbfs,
                                     safety_profile=safety_profile, role_targets=role_targets)
    if spec.graph_scope == "candidate_branches":
        program = build_branch_program(program, branch_channels_for(spec), _alone_gains_db(excitation, spec))
    return program


def excitation_from_context(context: Any, session_volume_db: float = 0.0) -> SessionExcitation:
    """The session's declarations as the conductor context resolved them."""
    return SessionExcitation(context.roles_bands, context.driver_caps_dbfs, session_volume_db, context.fc_hz,
                             context.driver_sweep_duration_limits_s, target_bands=context.driver_bands)


def predictive_program_for_spec(context: Any) -> Callable[..., ExcitationProgram]:
    # A take's gain never changes its segment count; preview can precede the CHECK level solve.
    excitation = excitation_from_context(context)
    return partial(program_for_spec, excitation=excitation,
                   gain_plan_db={r.role: BASE_STIMULUS_PEAK_DBFS for r in excitation.roles},
                   safety_profile=context.safety_profile, role_targets=context.role_targets)
