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
2. **The identity invariant** — every :data:`SUMMED_SWEEP_PHASES` member gets
   the *same object*, not an equal one.  #2291's before→after benefit verdict is
   checked by ``program_id`` equality; a copy that merely compared equal today
   would be a latent ``BENEFIT_PROGRAM_MISMATCH`` the moment composition picked
   up any per-call state.

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
    SUMMED_SWEEP_PHASES,
    NoProgramForPhaseError,
    SessionExcitation,
    back_off_gain,
    program_for_phase,
)

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
GOLDEN_DEEP_CAP = {
    "check": "37d2c9f4bacfc6b5573611d37887170c5bbc4792685071fd003fbee31bc939bf",
    "measure": "90bd46b530f373531348acfc9786a9998c92dd11b2e5975d0cf7c9dec0be0814",
    "verify": "29c3a0e1cceee6c2d95862bcb50e0c2edb884f798bc877fc98b46f7d731a46e3",
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


@pytest.mark.parametrize("phase", sorted(GOLDEN_DEEP_CAP))
def test_the_composed_programs_are_the_ones_that_shipped(phase):
    """Every program_id equals the pre-extraction conductor's, exactly."""
    ex = _excitation(CAPS)
    composed = {
        "check": ex.check_program(),
        "measure": ex.measure_program(GAIN_PLAN_DB),
        "verify": ex.verify_program(),
    }[phase]

    assert composed.program_id == GOLDEN_DEEP_CAP[phase]


def test_the_conductor_composes_through_the_same_owner():
    """The conductor's held objects ARE the ones this module composes.

    Not a restatement of the pin above: it would still pass if the constructor
    quietly composed at its own level and only the module's own composer matched
    the golden.
    """
    c = _conductor(CAPS)

    assert c.program_for_phase(flow.PHASE_CHECK).program_id == GOLDEN_DEEP_CAP["check"]
    assert c.program_for_phase(flow.PHASE_MEASURE).program_id == GOLDEN_DEEP_CAP["measure"]
    assert c.program_for_phase(flow.PHASE_VERIFY).program_id == GOLDEN_DEEP_CAP["verify"]


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


def test_every_summed_sweep_phase_gets_the_same_object():
    """``is``, not ``==``. The whole of #2291's comparability rests on it.

    ``program_id`` equality is what
    :func:`~jasper.active_speaker.crossover_v2.verification.evaluate_benefit`
    checks before it will compare a before against an after, and the reason that
    equality holds is that the entry baseline and VERIFY are handed one object.
    Asserting equal ids instead would keep passing under a composer that
    returned a fresh-but-equal program today and drifted tomorrow.
    """
    c = _conductor(CAPS)
    verify = c.program_for_phase(flow.PHASE_VERIFY)

    for phase in sorted(SUMMED_SWEEP_PHASES):
        assert c.program_for_phase(phase) is verify

    assert flow.PHASE_ENTRY_BASELINE in SUMMED_SWEEP_PHASES


def test_a_lateral_pose_replays_the_measure_object_verbatim():
    c = _conductor(CAPS)

    assert c.program_for_phase(flow.PHASE_LATERAL) is c.program_for_phase(
        flow.PHASE_MEASURE
    )


def test_measure_before_the_gain_solve_refuses_rather_than_guessing():
    """No program is composed at a guessed level."""
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            flow.PHASE_MEASURE,
            check=_excitation(CAPS).check_program(),
            measure=None,
            verify=_excitation(CAPS).verify_program(),
        )


def test_an_unplanned_phase_refuses():
    ex = _excitation(CAPS)
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            "not_a_phase",
            check=ex.check_program(),
            measure=None,
            verify=ex.verify_program(),
        )


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
    assert flow.COURTESY_PRELUDE_ENABLED == programs.COURTESY_PRELUDE_ENABLED
