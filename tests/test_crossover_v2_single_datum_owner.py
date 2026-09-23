# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2609 — one measurement owns the level datum, and no threshold arbitrates it.

Replaces ``test_crossover_v2_level_frame_dispute.py``, whose subject was
deleted with the mechanism it pinned. That file pinned a three-case rule in
:func:`jasper.active_speaker.crossover_v2.intervention.anchor_trims` under which
two per-driver estimators voted and a 3.0 dB cliff decided whether their
reconciliation was admitted. This file pins that there is no vote left and
that the safety normalize the migration ran past is intact.

**What went wrong, in one paragraph.** The anchor was
``base + giveback + level_frame_offset``, and the offset carried a
reconciliation between the trim solve's power-band average about Fc and the
fit's median over each driver's own radiating band. Those two routinely
disagree. When they disagreed past tolerance every offset was zeroed. On the
2026-08-16 shortfall round they disagreed by 3.326 dB against a 3.0 dB bar —
**a miss of 0.326 dB** — the zeroing fired, and the tweeter moved from a raw
measured trim of ``-10.835`` to a committed ``-7.043``, +3.79 dB hotter than
the measurement had asked for. The crossover band then realized +3.10 dB over
commanded, VERIFY's absolute check failed at -2.83 dB @ 1935 Hz, and the probe
rolled the round back.
"""

from __future__ import annotations

import inspect

import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv

# --------------------------------------------------------------------------- #
# the 2026-08-16 jts3 terms
# --------------------------------------------------------------------------- #

_ROLES = ("woofer", "tweeter")

#: The incident round's anchor terms, RECONSTRUCTED from the four numbers
#: #2609 states rather than copied from a bundle this repo does not hold.
#:
#: Stated on the issue: the raw measured tweeter trim ``-10.835`` and the
#: overlap-band estimate ``-6.893`` (the 12:27 comment), and the committed
#: ``-7.043`` with ``normalize_shift_db = 2.878`` (the conviction comment).
#: The woofer was the loud branch — it is what set the shift — so its
#: unnormalized term EQUALS the shift, and the rest follows::
#:
#:     giveback_t = committed + shift - base_used = -7.043 + 2.878 + 6.893
#:                = 2.728
#:     tba_w      = shift - giveback_t            = 0.150
#:     giveback_w = shift - tba_w                 = 2.728
#:
#: The reconstruction is checked rather than asserted: the first test below
#: replays the DELETED rule on these terms and gets the incident's own
#: ``-7.043`` back, and the move between the two rules comes out +3.792 dB
#: against the owner's stated +3.79. A fixture that reproduces the known bad
#: number under the old rule is the incident's; one that only produced the
#: good number under the new rule would prove nothing.
#:
#: ``scripts/derive-crossover-incident-fixture.py`` builds this shape from a
#: real bundle when one is on the laptop; these terms are the in-repo
#: equivalent, so the pin runs in CI with no gitignored evidence.
_RAW_TRIM_DB = {"woofer": 0.0, "tweeter": -10.835}
_TRIM_BAND_AVERAGE_DB = {"woofer": 0.150, "tweeter": -6.893}
_GIVEBACK_DB = {"woofer": 2.728, "tweeter": 2.728}

#: What the deleted rule committed, and what the measurement asked for.
_INCIDENT_COMMITTED_TWEETER_DB = -7.043
_MEASURED_TWEETER_DB = -10.8
#: The owner's conviction bar: the committed trim must land within 0.6 dB of
#: the measurement (the reigning tune's -10.214 sits inside the same window).
_INCIDENT_TOLERANCE_DB = 0.6


def _anchor(**overrides):
    kwargs = dict(
        roles=_ROLES,
        anchor_base_db=_RAW_TRIM_DB,
        giveback_db=_GIVEBACK_DB,
    )
    kwargs.update(overrides)
    return iv.anchor_trims(**kwargs)


# --------------------------------------------------------------------------- #
# (c) the contract: no branch keyed on a tolerance
# --------------------------------------------------------------------------- #


def test_anchor_trims_takes_no_tolerance_and_no_disagreement():
    """The three arbitration parameters are gone from the signature.

    A signature test rather than a behaviour one because the defect was
    structural: as long as ``anchor_trims`` can be TOLD a disagreement and a
    tolerance, some branch can compare them, and the cliff comes back. Its
    inputs are now exactly the two terms the anchor is made of.
    """
    params = set(inspect.signature(iv.anchor_trims).parameters)
    assert params == {"roles", "anchor_base_db", "giveback_db"}
    for gone in ("tolerance_db", "disagreement_db", "has_frame",
                 "level_frame_offset_db"):
        assert gone not in params


def test_anchor_trims_body_carries_no_threshold_comparison():
    """No surviving branch in the function compares against a tolerance.

    The signature test above closes the door the cliff came through; this one
    checks nobody re-opened it from inside by reaching for a module constant.
    Source inspection is the honest instrument here — the branch it forbids is
    one that fires on inputs a passing behaviour test would not supply.
    """
    body = inspect.getsource(iv.anchor_trims)
    _, _, code = body.partition('"""')
    _, _, code = code.partition('"""')
    for token in ("TOLERANCE", "tolerance", "disagreement"):
        assert token not in code, f"anchor_trims body still reaches for {token!r}"


def test_the_anchor_is_base_plus_giveback_and_nothing_else():
    anchored, shift = _anchor()
    unnormalized = {
        role: _RAW_TRIM_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    expected_shift = max(0.0, max(unnormalized.values()))
    assert shift == pytest.approx(expected_shift)
    for role in _ROLES:
        assert anchored[role] == pytest.approx(unnormalized[role] - expected_shift)


# --------------------------------------------------------------------------- #
# (a) the incident replay
# --------------------------------------------------------------------------- #


def test_the_fixture_reproduces_the_incidents_own_committed_trim():
    """The deleted rule, replayed on these terms, gives back its own -7.043.

    This is what makes the fixture the INCIDENT's rather than three numbers
    chosen to make the next test pass. The deleted third case zeroed every
    per-role offset and anchored on the overlap-band estimate, which is
    exactly ``place(trim_band_average + giveback)`` — reproduced here by hand
    because the branch that did it no longer exists to call.
    """
    unnormalized = {
        role: _TRIM_BAND_AVERAGE_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    shift = max(0.0, max(unnormalized.values()))
    committed = {r: v - shift for r, v in unnormalized.items()}
    assert shift == pytest.approx(2.878, abs=1e-3)
    assert committed["tweeter"] == pytest.approx(
        _INCIDENT_COMMITTED_TWEETER_DB, abs=1e-3
    )


def test_the_incident_round_commits_the_measured_tweeter_trim():
    """The 2026-08-16 shortfall round, replayed: the anchor lands near -10.8.

    The whole claim of the migration in one assertion. Under the deleted rule
    this pair committed -7.043 while the raw measurement asked for -10.835 and
    the reigning tune sat at -10.214. With no branch left to take, the anchor
    commits what the measurement said.
    """
    anchored, _ = _anchor()
    assert anchored["tweeter"] == pytest.approx(
        _MEASURED_TWEETER_DB, abs=_INCIDENT_TOLERANCE_DB
    )
    # And explicitly NOT the value the exclusion branch shipped.
    assert abs(
        anchored["tweeter"] - _INCIDENT_COMMITTED_TWEETER_DB
    ) > _INCIDENT_TOLERANCE_DB


def test_the_move_the_migration_removes_is_the_owners_stated_plus_3_79_db():
    """Old rule minus new rule == +3.79 dB, the number the forensic named."""
    unnormalized = {
        role: _TRIM_BAND_AVERAGE_DB[role] + _GIVEBACK_DB[role] for role in _ROLES
    }
    old_shift = max(0.0, max(unnormalized.values()))
    old_tweeter = unnormalized["tweeter"] - old_shift
    new_tweeter, _ = _anchor()
    assert old_tweeter - new_tweeter["tweeter"] == pytest.approx(3.79, abs=0.01)


# --------------------------------------------------------------------------- #
# the safety invariant the migration ran past
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "giveback",
    [
        {"woofer": 0.0, "tweeter": 0.0},
        {"woofer": 2.811, "tweeter": 6.717},
        # Give-back far exceeding the raw attenuation on BOTH roles: the case
        # the normalize exists for.
        {"woofer": 40.0, "tweeter": 40.0},
    ],
)
def test_no_committed_trim_is_ever_a_boost(giveback):
    """Every returned trim is <= 0. The hearing-safety invariant, unchanged.

    The migration moved where the anchor's base number comes from. It did not
    touch the clamp around it, and this is what says so: a branch whose own
    cuts give back more than its raw attenuation must still land non-positive,
    because the emitter refuses a positive trim and the hardware must never
    see one.
    """
    anchored, shift = _anchor(giveback_db=giveback)
    assert shift >= 0.0
    for role, value in anchored.items():
        assert value <= 0.0, f"{role} committed a boost: {value}"


def test_the_normalize_preserves_relative_placement_exactly():
    """The shift is common-mode, so it cannot change the inter-driver balance."""
    anchored, shift = _anchor()
    hot, hot_shift = _anchor(
        giveback_db={role: v + 12.0 for role, v in _GIVEBACK_DB.items()}
    )
    assert hot_shift == pytest.approx(shift + 12.0)
    spread = anchored["tweeter"] - anchored["woofer"]
    assert hot["tweeter"] - hot["woofer"] == pytest.approx(spread)


# --------------------------------------------------------------------------- #
# what the migration deleted stays deleted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["LevelFrameAdmission", "LEVEL_FRAME_DISPUTED_REASON"],
)
def test_the_arbitration_vocabulary_is_gone(name):
    assert not hasattr(iv, name)
    assert name not in iv.__all__


@pytest.mark.parametrize(
    "name", ["SharedLevelFrame", "solve_shared_level_frame"],
)
def test_the_frame_solver_is_gone(name):
    from jasper.active_speaker import linearization_fit

    assert not hasattr(linearization_fit, name)


def test_the_trim_search_step_meets_the_setting_precision():
    """**<=0.5 dB is how precisely the trim must LAND** once the level fact is
    known — met by construction, because the trim search steps finer than that.
    """
    from jasper.audio_measurement.program_analysis import RIPPLE_TRIM_SEARCH_STEP_DB

    setting_precision_db = 0.5
    assert RIPPLE_TRIM_SEARCH_STEP_DB <= setting_precision_db


def test_the_summed_owner_plumbing_is_gone_not_dormant():
    """The deletion test, as a test.

    An earlier cut of this migration made the summed at-the-mark capture the
    level owner. It is unreachable in production — the session that holds the
    baseline never plans (the VERIFY map has no MEASURE), the session that
    plans never holds one (stage 1 captures the baseline AFTER the fit, and
    hydrate passes none) — and the two captures are in different frames anyway:
    the per-branch sweeps ride the protected-NEUTRAL graph while the baseline
    rides the applied incumbent, so combining them double-counts the
    incumbent's trims. Fixture-only code is not kept as aspiration.
    """
    from jasper.audio_measurement import program_analysis

    assert not hasattr(program_analysis, "summed_level_reference_db")
