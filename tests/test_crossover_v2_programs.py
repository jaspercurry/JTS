# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5a-ii: what a session plays, how loud, and for which phase.

The extraction that moved level policy and program composition into
:mod:`jasper.active_speaker.crossover_v2.programs` was a pure re-home, and this
module is what makes that claim checkable rather than a promise.  Two kinds of
pin, in the order a reviewer should read them:

1. **Golden identities.** The composed programs' ``program_id``s and every
   segment gain, captured from the PRE-extraction conductor and asserted here.
   A ``program_id`` is a SHA-256 over the whole excitation schedule including
   every segment's gain, so an unchanged id is a strong statement: not one
   frequency, duration, or level moved.  They are written as literals rather
   than recomputed, because a pin that derives its expectation from the code
   under test pins nothing.
2. **The identity invariant** — the COMPARED pair (VERIFY and the entry
   baseline) gets the *same object*, not an equal one, and each position group
   gets one object of its own.  #2291's before→after benefit verdict is checked
   by ``program_id`` equality; a copy that merely compared equal today would be
   a latent ``BENEFIT_PROGRAM_MISMATCH`` the moment composition picked up any
   per-call state.
3. **The courtesy-prelude rule** (#1677, trimmed 2026-08-18) — the prelude
   announces a SESSION, not a capture, so it rides the phases that open one and
   the entry baseline that must stay identical to stage 2's anchor.  Pinned
   against the goldens in both directions: restoring the prelude reproduces the
   shipped id byte for byte, which is what makes "only the prelude moved" a
   measurement rather than a claim.

The fixture deliberately covers BOTH level regimes, because they are
discriminating in opposite directions: at the deep-cap corner (the JTS3 shape,
tweeter at −65 dBFS) the min-cap clamp binds and swallows ``extra_backoff_db``
entirely, so a dropped backoff would be invisible there; at the unclamped corner
it shows through.  A pin at one corner only would pass over half the policy.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import programs
from jasper.active_speaker.crossover_v2.programs import (
    COURTESY_PRELUDE_PHASES,
    GROUP_SUMMED_SWEEP_PHASES,
    SUMMED_SWEEP_PHASES,
    NoProgramForPhaseError,
    SessionExcitation,
    back_off_gain,
    courtesy_prelude_for_phase,
    program_for_phase,
)
from jasper.audio_measurement.program import KIND_COURTESY_TONE

from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    SESSION,
    SESSION_VOLUME_DB,
    FakeSeams,
    _preset,
    _roles,
)

#: The solved per-driver gains a CHECK pass would hand MEASURE.
GAIN_PLAN_DB = {"woofer": -32.0, "tweeter": -38.0}

#: Captured from ``origin/main`` at f06a280d2 — the commit before the
#: extraction — by composing each program on the pre-extraction conductor and
#: reading its id. Re-derive them ONLY by re-running that comparison; editing a
#: literal here to make a test pass would delete the evidence it exists to be.
#:
#: **Every one of these three is still the pre-extraction id, including
#: ``measure``.** The 2026-08-18 prelude trim moved what MEASURE *ships* (it no
#: longer opens on the courtesy prelude), and the way that change is pinned is
#: by restoring the prelude and asserting THIS literal comes back — see
#: :func:`test_only_the_prelude_moved_under_the_shipped_measure_program`. So the
#: evidence the extraction captured is spent proving the trim's own scope rather
#: than being overwritten by it.
GOLDEN_DEEP_CAP = {
    "check": "37d2c9f4bacfc6b5573611d37887170c5bbc4792685071fd003fbee31bc939bf",
    "measure": "90bd46b530f373531348acfc9786a9998c92dd11b2e5975d0cf7c9dec0be0814",
    "verify": "29c3a0e1cceee6c2d95862bcb50e0c2edb884f798bc877fc98b46f7d731a46e3",
}

#: What the phases the courtesy prelude no longer announces ship TODAY, at the
#: same fixture and the same gains as :data:`GOLDEN_DEEP_CAP`. Read off the
#: composers after the trim; the pin that makes them meaningful is not this
#: literal but the round trip below, which shows each one becomes its
#: ``GOLDEN_DEEP_CAP`` twin the moment the prelude is put back.
GOLDEN_UNANNOUNCED = {
    "measure": "f0ad3c2a2c683dbed508d684baf37fc99f874b0507943a58a75f77afaee1a9c8",
    "cloud": "99282a0841dcbcfc4c34da9f10ea522d3883e2b012079cbbb93a2aa51d2125e8",
}


def _excitation(caps: dict[str, float]) -> SessionExcitation:
    return SessionExcitation(
        roles=tuple(_roles()),
        caps_dbfs=caps,
        session_volume_db=SESSION_VOLUME_DB,
        fc_hz=FC_HZ,
    )


def _conductor(caps: dict[str, float]):
    return flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
        gain_plan_db=GAIN_PLAN_DB,
    )


# --------------------------------------------------------------------------- #
# 1. golden identities — the extraction changed nothing that plays
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("phase", ["check", "verify"])
def test_the_composed_programs_are_the_ones_that_shipped(phase):
    """The two ANNOUNCED programs' ids equal the pre-extraction conductor's."""
    ex = _excitation(CAPS)
    composed = {
        "check": ex.check_program(),
        "verify": ex.verify_program(),
    }[phase]

    assert composed.program_id == GOLDEN_DEEP_CAP[phase]


def test_only_the_prelude_moved_under_the_shipped_measure_program(monkeypatch):
    """MEASURE ships a new id, and putting the prelude back gives the old one.

    The trim's whole scope claim, measured. Restoring MEASURE to the announced
    set must reproduce ``GOLDEN_DEEP_CAP["measure"]`` byte for byte — a
    SHA-256 over the entire schedule including every segment's gain — so no
    frequency, duration, or level moved with the prelude.
    """
    ex = _excitation(CAPS)

    assert ex.measure_program(GAIN_PLAN_DB).program_id == GOLDEN_UNANNOUNCED["measure"]

    monkeypatch.setattr(
        programs, "COURTESY_PRELUDE_PHASES",
        frozenset(COURTESY_PRELUDE_PHASES | {flow.PHASE_MEASURE}),
    )

    assert ex.measure_program(GAIN_PLAN_DB).program_id == GOLDEN_DEEP_CAP["measure"]


def test_a_position_plays_the_verify_sweep_with_the_prelude_taken_off(monkeypatch):
    """The cloud twin is the summed sweep at the same clamp, unannounced.

    Same round trip as MEASURE's, and it is what says the split costs nothing
    acoustically: announce the position groups again and the cloud program
    becomes the VERIFY program's own id, so the two differ in the prelude and in
    nothing else — not in the min-cap clamp that is their only level guard.
    """
    ex = _excitation(CAPS)

    assert ex.cloud_program().program_id == GOLDEN_UNANNOUNCED["cloud"]
    assert ex.cloud_program().program_id != ex.verify_program().program_id

    monkeypatch.setattr(
        programs, "COURTESY_PRELUDE_PHASES",
        frozenset(COURTESY_PRELUDE_PHASES | GROUP_SUMMED_SWEEP_PHASES),
    )

    assert ex.cloud_program().program_id == GOLDEN_DEEP_CAP["verify"]


def test_the_conductor_composes_through_the_same_owner():
    """The conductor's held objects ARE the ones this module composes.

    Not a restatement of the pin above: it would still pass if the constructor
    quietly composed at its own level and only the module's own composer matched
    the golden.
    """
    c = _conductor(CAPS)

    assert c.program_for_phase(flow.PHASE_CHECK).program_id == GOLDEN_DEEP_CAP["check"]
    assert c.program_for_phase(flow.PHASE_VERIFY).program_id == GOLDEN_DEEP_CAP["verify"]
    assert (
        c.program_for_phase(flow.PHASE_MEASURE).program_id
        == GOLDEN_UNANNOUNCED["measure"]
    )
    assert (
        c.program_for_phase(flow.PHASE_CLOUD_VERIFY).program_id
        == GOLDEN_UNANNOUNCED["cloud"]
    )


def test_the_summed_sweep_is_clamped_to_the_most_restrictive_cap():
    """The one level guard on the post-apply sweep, asserted as a number.

    VERIFY plays through the applied production graph with no play-time
    admission gate, so this clamp is the only thing between a deep-cap
    compression driver and the shared reference base. With the tweeter at
    −65 dBFS and the session at −20, the admissible digital gain is
    ``−65 − (−20) − 0.01`` = −45.01 dBFS, and every level in the program —
    sweep and both pilots — must sit at or under it.
    """
    program = _excitation(CAPS).verify_program()
    gains = [seg.gain_db for seg in program.segments if seg.gain_db]

    assert max(gains) == pytest.approx(-45.01)
    assert min(CAPS.values()) == -65.0


def test_the_pilot_pair_keeps_its_ten_db_delta_at_any_level():
    ex = _excitation(CAPS)

    for hi in (-20.0, -45.01, -65.0):
        lo, hi_out = ex.pilot_gains(hi)
        assert hi_out == hi
        assert hi - lo == pytest.approx(programs.PILOT_LEVEL_DELTA_DB)


# --------------------------------------------------------------------------- #
# 2. the clamp itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("gain", "session_volume", "cap", "expected"),
    [
        # Unclamped: the requested gain already sits under the folded ceiling.
        (-45.0, -20.0, -35.0, -45.0),
        (-65.0, -20.0, -35.0, -65.0),
        # Clamped: the cap binds, with the ulp margin below it.
        (-20.0, -20.0, -65.0, -45.01),
        (-32.0, 0.0, -65.0, -65.01),
        # Exactly at the ceiling still backs off, which is the whole point of
        # the margin — admission's strict ``>`` refuses an at-cap plan by one
        # ulp.
        (-45.01, -20.0, -65.0, -45.01),
        (-45.0, -20.0, -65.0, -45.01),
    ],
)
def test_the_clamp_holds_the_effective_peak_under_the_cap(
    gain, session_volume, cap, expected,
):
    got = back_off_gain(gain, session_volume, cap)

    assert got == pytest.approx(expected)
    # The property the table is sampling: folded through the session volume, the
    # result never reaches the cap.
    assert got + session_volume < cap or got == pytest.approx(gain)


def test_the_backoff_is_swallowed_when_the_cap_already_binds():
    """A retry's extra backoff cannot make a clamped program louder OR quieter.

    Recorded because it surprised the extraction's own dual-run: at the deep-cap
    corner the clip-retry rearm composes a byte-identical program, so a pin that
    only looked here would pass over a DELETED ``extra_backoff_db``.
    """
    ex = _excitation(CAPS)

    assert (
        ex.verify_program(extra_backoff_db=3.0).program_id
        == ex.verify_program().program_id
    )


def test_the_backoff_shows_through_when_the_cap_does_not_bind():
    """The other corner, which is what makes the pin above non-vacuous."""
    ex = _excitation({"woofer": 0.0, "tweeter": 0.0})

    assert (
        ex.verify_program(extra_backoff_db=3.0).program_id
        != ex.verify_program().program_id
    )
    assert (
        ex.measure_program(GAIN_PLAN_DB, extra_backoff_db=3.0).program_id
        != ex.measure_program(GAIN_PLAN_DB).program_id
    )


# --------------------------------------------------------------------------- #
# 3. the identity invariant
# --------------------------------------------------------------------------- #


def test_the_compared_pair_gets_the_same_object():
    """``is``, not ``==``. The whole of #2291's comparability rests on it.

    ``program_id`` equality is what
    :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
    checks before it will compare a before against an after — and what
    ``_delta_probe`` asks of its entry anchor before it will subtract one — and
    the reason that equality holds is that the entry baseline and VERIFY are
    handed one object. Asserting equal ids instead would keep passing under a
    composer that returned a fresh-but-equal program today and drifted tomorrow.
    """
    c = _conductor(CAPS)
    verify = c.program_for_phase(flow.PHASE_VERIFY)

    for phase in sorted(SUMMED_SWEEP_PHASES - GROUP_SUMMED_SWEEP_PHASES):
        assert c.program_for_phase(phase) is verify

    assert flow.PHASE_ENTRY_BASELINE in SUMMED_SWEEP_PHASES
    assert flow.PHASE_ENTRY_BASELINE not in GROUP_SUMMED_SWEEP_PHASES


def test_every_position_group_gets_the_same_object():
    """The other half of the invariant, and it is ``is`` for the same reason.

    A group's positions are combined into one curve, so a composer that handed
    two of them different-but-equal programs would be combining across a
    difference nothing downstream can see.
    """
    c = _conductor(CAPS)
    cloud = c.program_for_phase(flow.PHASE_CLOUD_MEASURE)

    for phase in sorted(GROUP_SUMMED_SWEEP_PHASES):
        assert c.program_for_phase(phase) is cloud

    assert cloud is not c.program_for_phase(flow.PHASE_VERIFY)
    assert GROUP_SUMMED_SWEEP_PHASES < SUMMED_SWEEP_PHASES


def test_a_lateral_pose_replays_the_measure_object_verbatim():
    c = _conductor(CAPS)

    assert c.program_for_phase(flow.PHASE_LATERAL) is c.program_for_phase(
        flow.PHASE_MEASURE
    )


def test_measure_before_the_gain_solve_refuses_rather_than_guessing():
    """No program is composed at a guessed level."""
    ex = _excitation(CAPS)
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            flow.PHASE_MEASURE,
            check=ex.check_program(),
            measure=None,
            verify=ex.verify_program(),
            cloud=ex.cloud_program(),
        )


def test_an_unplanned_phase_refuses():
    ex = _excitation(CAPS)
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            "not_a_phase",
            check=ex.check_program(),
            measure=None,
            verify=ex.verify_program(),
            cloud=ex.cloud_program(),
        )


# --------------------------------------------------------------------------- #
# 3b. the courtesy-prelude rule (#1677, trimmed 2026-08-18)
# --------------------------------------------------------------------------- #


def _has_prelude(program) -> bool:
    return any(seg.kind == KIND_COURTESY_TONE for seg in program.segments)


def test_the_capture_that_opens_a_session_is_announced():
    """Stage 1 opens on CHECK and stage 2 on VERIFY; both warn the room first.

    This is #1677 itself — the incident was a headless session whose FIRST sweep
    started over the household's music. Whatever else the rule trims, these two
    captures keep the beeps or the incident is reopened.
    """
    c = _conductor(CAPS)

    assert _has_prelude(c.program_for_phase(flow.PHASE_CHECK))
    assert _has_prelude(c.program_for_phase(flow.PHASE_VERIFY))
    assert courtesy_prelude_for_phase(flow.PHASE_CHECK)
    assert courtesy_prelude_for_phase(flow.PHASE_VERIFY)


def test_a_capture_the_household_began_inside_a_running_session_is_not():
    """Every capture behind the opener plays no prelude — the trim itself.

    MEASURE and each lateral pose are begun by the household's own tap at a
    position it has just walked to, inside a session whose measurement window is
    already held; a prompted cloud position likewise. 3.6 s each, twelve times
    on a Full journey.
    """
    c = _conductor(CAPS)

    for phase in (
        flow.PHASE_MEASURE, flow.PHASE_LATERAL,
        flow.PHASE_CLOUD_MEASURE, flow.PHASE_CLOUD_VERIFY,
    ):
        assert not _has_prelude(c.program_for_phase(phase)), phase
        assert not courtesy_prelude_for_phase(phase), phase


def test_the_entry_baseline_is_announced_because_its_twin_is():
    """Not an opener — held to the rule by ``program_id``, and stated as such.

    Stage 1's last capture carries the prelude for one reason: its program
    object is stage 2's anchor, and that equality is #2291's before→after
    comparison and the delta probe's anchor check. A rule that dropped it here
    would grade every round ``BENEFIT_PROGRAM_MISMATCH``.
    """
    c = _conductor(CAPS)

    assert _has_prelude(c.program_for_phase(flow.PHASE_ENTRY_BASELINE))
    assert c.program_for_phase(flow.PHASE_ENTRY_BASELINE) is c.program_for_phase(
        flow.PHASE_VERIFY
    )
    assert flow.PHASE_ENTRY_BASELINE in COURTESY_PRELUDE_PHASES


def test_the_conductor_translates_the_refusal_into_its_own_error():
    """Callers above the conductor handle ``CrossoverV2FlowError``; the pure
    selector has no business knowing that type."""
    c = flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )

    with pytest.raises(flow.CrossoverV2FlowError):
        c.program_for_phase(flow.PHASE_MEASURE)


# --------------------------------------------------------------------------- #
# 4. the bundle is frozen, so a subset cannot drift
# --------------------------------------------------------------------------- #


def test_the_declarations_cannot_be_mutated_after_construction():
    caps = dict(CAPS)
    ex = _excitation(caps)

    caps["tweeter"] = 0.0  # the caller's dict moves on

    assert ex.caps_dbfs["tweeter"] == -65.0
    with pytest.raises(TypeError):
        ex.caps_dbfs["tweeter"] = 0.0  # type: ignore[index]
    with pytest.raises(Exception):
        ex.session_volume_db = 0.0  # type: ignore[misc]


def test_the_flow_re_exports_resolve_to_the_one_definition():
    """Every moved name keeps its old import path, pointing at the new owner."""
    assert flow.back_off_gain is programs.back_off_gain
    assert flow.SUMMED_SWEEP_PHASES is programs.SUMMED_SWEEP_PHASES
    assert flow.GAIN_CAP_BACKOFF_DB == programs.GAIN_CAP_BACKOFF_DB
    assert flow.PILOT_LEVEL_DELTA_DB == programs.PILOT_LEVEL_DELTA_DB
    assert flow.courtesy_prelude_for_phase is programs.courtesy_prelude_for_phase
