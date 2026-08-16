"""#2599 — a DISPUTED level frame does not place the trim anchor.

The unit-level home of :func:`jasper.active_speaker.crossover_v2.intervention.anchor_trims`:
its three-case rule table, the threshold it disputes at, and the 2026-08-15
jts3 terms that sent it here. The end-to-end behaviour — which pair a disputed
session commits, what it discloses, and the one fixture whose outcome the rule
flips — lives with its fixtures in ``tests/test_crossover_v2_conductor.py``.

**What went wrong, in one paragraph.** The anchor is
``base + giveback + level_frame_offset``, normalized so no trim is positive.
The offset term is the shared level frame's per-role placement, and the frame
is built from two estimators that routinely disagree — the trim solve's
power-band average about Fc, and the fit's median over each driver's own
radiating band. When they disagree past
``LEVEL_FRAME_AGREEMENT_TOLERANCE_DB`` the session already said so: it banks an
M7 finding whose ``confidence`` is ``unsure``, whose ``fix_class`` is
``refit``, and whose ``probes_run`` is empty. It then anchored on it anyway.
On the 2026-08-15 jts3 run the woofer's ``+3.264`` dB offset became
``normalize_shift_db`` — the reference every trim is measured down from — and
the tweeter shipped 3.264 dB quieter than the undisputed arithmetic gives.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from jasper.active_speaker.crossover_v2 import intervention as iv
from jasper.active_speaker.crossover_v2_flow import (
    LEVEL_FRAME_AGREEMENT_TOLERANCE_DB,
)
from jasper.audio_measurement.program_analysis import (
    REALIZED_LEVEL_MATCH_TOLERANCE_DB,
)

# --------------------------------------------------------------------------- #
# the 2026-08-15 jts3 terms
# --------------------------------------------------------------------------- #

#: The anchor's three inputs as the run's own
#: ``correction.crossover_v2_linearization_giveback`` record logged them.
#:
#: **These are the journal's 3-dp values, not the run's full-precision ones**,
#: and the difference is visible in the last digit: replaying them reproduces
#: the shipped tweeter trim as −10.214 where the artifact records −10.2141.
#: Stating that rather than rounding it away is the point — a fixture that
#: claimed 4 dp it cannot support would be inviting the next reader to trust a
#: digit the source never had.
_ROLES = ("woofer", "tweeter")
_ANCHOR_BASE_DB = {"woofer": 0.0, "tweeter": -10.856}
_GIVEBACK_DB = {"woofer": 2.811, "tweeter": 6.717}
_OFFSET_DB = {"woofer": 3.264, "tweeter": -0.0}
_DISAGREEMENT_DB = 3.264


def _anchor(**overrides):
    kwargs = dict(
        roles=_ROLES,
        anchor_base_db=_ANCHOR_BASE_DB,
        giveback_db=_GIVEBACK_DB,
        level_frame_offset_db=_OFFSET_DB,
        has_frame=True,
        disagreement_db=_DISAGREEMENT_DB,
        tolerance_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    )
    kwargs.update(overrides)
    return iv.anchor_trims(**kwargs)


# --------------------------------------------------------------------------- #
# the incident, both ways
# --------------------------------------------------------------------------- #


def test_the_shipped_arithmetic_is_reproduced_from_the_runs_own_terms():
    """The BEFORE half, so the fix is measured against the real thing.

    Admitting the offsets — which is what the code did unconditionally until
    #2599, and still does whenever the frame agrees — reproduces the run:

        woofer   0.000 + 2.811 + 3.264 =  6.075  ->  0.000
        tweeter −10.856 + 6.717 + −0.0 = −4.139  -> −10.214

    The woofer's ``+3.264`` is the whole story. It is the largest term in
    ``normalize_shift_db``, so it does not move the woofer at all (it is
    normalized straight back out) — it moves the OTHER driver, by its full
    size, downward.
    """

    anchored, shift, admission = _anchor(tolerance_db=1e9)

    assert admission is not None and admission.admitted is True
    assert shift == pytest.approx(6.075, abs=1e-3)
    assert anchored["woofer"] == pytest.approx(0.0, abs=1e-3)
    assert anchored["tweeter"] == pytest.approx(-10.214, abs=1e-3)


def test_a_disputed_frame_does_not_place_the_anchor():
    """The AFTER half: 3.264 > 3.0, so the offsets are declined.

        woofer   0.000 + 2.811 =  2.811  ->  0.000
        tweeter −10.856 + 6.717 = −4.139  -> −6.950

    The tweeter ships **3.264 dB louder** — exactly the disputed offset,
    because that offset was the entire difference between the two
    normalizations. Nothing about the frame's own solve changes; the frame is
    still computed, still reported, and still banked as a finding. What
    changes is that it no longer gets to be the number the speaker hangs from.
    """

    anchored, shift, admission = _anchor()

    assert shift == pytest.approx(2.811, abs=1e-3)
    assert anchored["woofer"] == pytest.approx(0.0, abs=1e-3)
    assert anchored["tweeter"] == pytest.approx(-6.950, abs=1e-3)

    assert admission is not None
    assert admission.admitted is False
    assert admission.reason == iv.LEVEL_FRAME_DISPUTED_REASON
    assert admission.disagreement_db == pytest.approx(3.264, abs=1e-3)
    assert admission.tolerance_db == REALIZED_LEVEL_MATCH_TOLERANCE_DB
    assert admission.excluded_offset_db == pytest.approx(_OFFSET_DB, abs=1e-9)
    # The dB consequence, per role — the field a reader needs and would
    # otherwise have to re-derive by running the other arithmetic by hand.
    assert admission.anchor_delta_db["tweeter"] == pytest.approx(3.264, abs=1e-3)
    assert admission.anchor_delta_db["woofer"] == pytest.approx(0.0, abs=1e-3)


def test_the_two_arithmetics_differ_by_exactly_the_disputed_offset():
    """The identity behind both numbers above, stated once.

    Asserted as an identity rather than as the pair of magic numbers it
    produces, because THAT is the claim: the exclusion moves the committed
    pair by the disputed term and by nothing else. A future change to the
    give-back or the base moves both arms together and leaves this true; a
    change that quietly re-admits the offset does not.
    """

    admitted, _, _ = _anchor(tolerance_db=1e9)
    excluded, _, admission = _anchor()

    for role in _ROLES:
        assert excluded[role] - admitted[role] == pytest.approx(
            admission.anchor_delta_db[role], abs=1e-9
        )
    assert max(admission.anchor_delta_db.values()) == pytest.approx(
        admission.disagreement_db, abs=1e-3
    )


# --------------------------------------------------------------------------- #
# the rule table — three cases and no fourth
# --------------------------------------------------------------------------- #


def test_an_agreeing_frame_anchors_exactly_as_it_always_did():
    """The regression pin, and the reason this is a scalpel and not a hammer.

    Same terms, same offsets, one number changed: a disagreement INSIDE the
    tolerance. The offsets are admitted and the arithmetic is byte-identical
    to the pre-#2599 code, which is the ordinary case on a healthy speaker.
    """

    anchored, shift, admission = _anchor(disagreement_db=0.9)
    reference, reference_shift, _ = _anchor(tolerance_db=1e9)

    assert anchored == pytest.approx(reference, abs=1e-12)
    assert shift == pytest.approx(reference_shift, abs=1e-12)
    assert admission is not None
    assert admission.admitted is True
    assert admission.reason == ""
    # Nothing was excluded, so there is no consequence to report — and empty
    # says that, where a mapping of zeros would read as "measured, and zero".
    assert admission.excluded_offset_db == {}
    assert admission.anchor_delta_db == {}


def test_the_boundary_is_strict_so_a_disagreement_AT_tolerance_is_admitted():
    """``> tolerance``, matching the accountability gate's own comparison.

    Pinned because the two must agree at the boundary as well as away from it:
    a planner that excluded at exactly 3.0 while the gate banked only above it
    would put a session on the disputed anchor with no finding to explain why.
    """

    _, _, at = _anchor(disagreement_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB)
    _, _, just_over = _anchor(
        disagreement_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB + 1e-9
    )

    assert at.admitted is True
    assert just_over.admitted is False


def test_no_frame_is_a_third_state_and_not_a_quiet_admission():
    """``None``, because "we admitted the frame" would be a claim about a
    frame that was never solved. The arithmetic is identical either way — a
    frameless fit's offsets are 0.0 by construction — so the only thing this
    distinguishes is what the disclosure says, which is the whole reason the
    field exists."""

    anchored, shift, admission = _anchor(
        has_frame=False, level_frame_offset_db=dict.fromkeys(_ROLES, 0.0)
    )

    assert admission is None
    assert shift == pytest.approx(2.811, abs=1e-3)
    assert anchored["tweeter"] == pytest.approx(-6.950, abs=1e-3)


def test_the_dispute_is_a_property_of_the_frame_so_every_offset_is_dropped():
    """"What if BOTH drivers' offsets are disputed?" has no separate answer.

    ``disagreement_db`` is one ``max(|offset|)`` over every role, so one role
    past tolerance and all of them past tolerance take the identical path:
    every offset is dropped. Keeping one while dropping another would ship a
    placement neither estimator proposed — ``solve_shared_level_frame`` places
    all roles jointly against one ``system_level_db``.

    Exercised with three roles because at TWO it is unobservable: the frame's
    reference role always carries 0.0, so a 2-role frame can never have both
    offsets past tolerance. Production is 2-role today; this pins the rule
    rather than the reachable shape of it.
    """

    roles = ("woofer", "midrange", "tweeter")
    offsets = {"woofer": 5.0, "midrange": 4.0, "tweeter": 0.0}
    anchored, shift, admission = iv.anchor_trims(
        roles=roles,
        anchor_base_db=dict.fromkeys(roles, 0.0),
        giveback_db=dict.fromkeys(roles, 1.0),
        level_frame_offset_db=offsets,
        has_frame=True,
        disagreement_db=5.0,
        tolerance_db=REALIZED_LEVEL_MATCH_TOLERANCE_DB,
    )

    assert admission.admitted is False
    assert admission.excluded_offset_db == pytest.approx(offsets, abs=1e-9)
    # All three collapse onto the same base+giveback, so the pair is level and
    # the shift is that common value — no role kept a placement.
    assert shift == pytest.approx(1.0, abs=1e-9)
    assert anchored == pytest.approx(dict.fromkeys(roles, 0.0), abs=1e-9)


def test_no_committed_anchor_is_ever_positive_on_either_path():
    """The emitter's invariant, which the exclusion must not be allowed to
    break. Excluding a POSITIVE offset lowers ``normalize_shift_db``, which is
    the direction that could produce a boost if the normalize were skipped on
    that path — so it is asserted on both arms of the branch rather than
    inferred from one."""

    for kwargs in ({}, {"tolerance_db": 1e9}, {"has_frame": False}):
        anchored, _, _ = _anchor(**kwargs)
        assert max(anchored.values()) <= 0.0
        assert max(anchored.values()) == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# the threshold has one owner
# --------------------------------------------------------------------------- #


def test_the_planner_disputes_at_the_threshold_the_gate_banks_at():
    """One question, one number — pinned across the three modules that hold it.

    ``anchor_trims`` excludes a frame at the same disagreement the
    accountability gate banks one at. If they drifted apart, a session could
    take the disputed anchor with no finding minted to explain it (planner
    stricter), or bank a finding while still anchoring on the disputed number
    (gate stricter) — which is the exact defect #2599 fixes, reintroduced by
    a one-line retune.

    The planner cannot import the flow's ``LEVEL_FRAME_AGREEMENT_TOLERANCE_DB``
    (the flow imports the planner), so it defaults to the constant that name
    aliases. This is the test that makes the alias load-bearing rather than
    incidental.
    """

    assert LEVEL_FRAME_AGREEMENT_TOLERANCE_DB == REALIZED_LEVEL_MATCH_TOLERANCE_DB
    default = {
        f.name: f.default for f in fields(iv.LinearizationRequest)
    }["level_frame_tolerance_db"]
    assert default == LEVEL_FRAME_AGREEMENT_TOLERANCE_DB


def test_the_admission_serializes_once_for_every_surface():
    """``to_dict`` is the single serialization the journal, the plan and the
    banked record all quote. Pinned as a whole-dict equality so a field added
    to one reader without the others is a failure here rather than a surface
    that silently disagrees with its siblings."""

    _, _, admission = _anchor()

    assert admission.to_dict() == {
        "admitted": False,
        "reason": "frame_disagreement_unadjudicated",
        "disagreement_db": 3.264,
        "tolerance_db": 3.0,
        "excluded_offset_db": {"woofer": 3.264, "tweeter": -0.0},
        "anchor_delta_db": {"woofer": 0.0, "tweeter": 3.264},
    }
