# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5a-iv: what a capture-consuming phase decides about one take.

The sibling of ``tests/test_crossover_v2_programs.py`` and
``tests/test_crossover_v2_priors.py``, and it exists for their reason:
:mod:`jasper.active_speaker.crossover_v2.spatial` is almost entirely
*decisions expressed as an ORDER*, and an order is exactly the kind of claim
that survives in a docstring while dying in the code.

**Every test here was chosen by mutation, not by reading.** The extraction moved
three ladders out of the conductor; each rung was then flipped, reordered, or
deleted in turn and the whole crossover suite re-run. The rungs whose mutation
came back GREEN are the ones pinned below — they are the properties the shipped
suites describe in prose and nowhere assert. Rungs already covered elsewhere
(the lateral two-curve floor, the geometry retake's thin-evidence and
already-closed conjuncts, the lateral group floor) are deliberately NOT
re-pinned here; their owners are named in the sections that would have.

The mutation table, with the suite that caught each, is in the PR.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
)
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_FAIL,
    CaptureIntegrity,
    IntegrityCheck,
)


def _screens(**overrides) -> spatial.CaptureScreens:
    """A take that passes every screen, minus whatever the caller breaks.

    Every ladder test below starts from a CLEAN capture and breaks exactly one
    thing, which is what makes each assertion about that one rung rather than
    about whichever rung happens to fire first in a soup of failures.
    """
    base = {
        "stimulus_located": True,
        "pilot_snr_ok": True,
        "linearity_ok": True,
        "glitch_detected": False,
        "sweep_locate_confidence_ok": True,
        "sweep_schedule_ok": True,
        "any_sweep_clipped": False,
    }
    base.update(overrides)
    return spatial.CaptureScreens(**base)


# --------------------------------------------------------------------------- #
# 1. the ORDER — which of two simultaneous faults gets named
#
# A ladder's order is only observable when two rungs fail at once: with one
# fault, any order returns the same kind. So every ordering test below breaks
# TWO screens and asserts which one the household is told about.
# --------------------------------------------------------------------------- #


def test_a_cloud_position_names_the_room_before_it_names_the_phone():
    """Issue #1810, on the cloud ladder, in the only shape that can see it.

    A pilot pair that never cleared the room's floor is a room/level problem.
    The linearity screen answers a different question — AGC in the RECORDING
    CHAIN — and its household copy sends someone to check their phone. When
    both fail (which is the common case, because a capture too quiet to hear
    also measures as non-linear), reporting the phone first sends the household
    to fix a thing that is not broken while the real fault goes unnamed.

    Mutation-selected: swapping these two rungs left 371 tests green.
    """
    kind = spatial.cloud_position_screens(
        _screens(pilot_snr_ok=False, linearity_ok=False),
        has_summed_response=True,
    )

    assert kind == spatial.SCREEN_PILOT_LEVEL_COLLAPSE


def test_a_lateral_pose_names_too_quiet_before_it_names_glitched():
    """Issue #1838 D3, on the lateral ladder — and the SAME rule, one layer on.

    A sweep the locator could barely find is a capture too quiet to hear, and a
    quiet sweep CAUSES the residual that trips ``glitch_detected``: in session
    ``cap_-Us10xORVNlFa_dgi-sP7g`` the sweeps located at 0.0298 against a 0.3
    floor and the mis-located sweeps then produced a 1018-sample residual. So
    the two fail together by mechanism, not by coincidence, and ordering the
    confidence check first is what makes the reported cause the real one — a
    glitch verdict silently re-arms the same unwinnable level.

    Mutation-selected: swapping these two rungs left 28 tests green.
    """
    kind = spatial.lateral_pose_screens(
        _screens(sweep_locate_confidence_ok=False, glitch_detected=True),
    )

    assert kind == spatial.SCREEN_LOCATE_FAILED


def test_a_lateral_pose_names_the_room_before_the_schedule_or_the_clip():
    """The same #1810 precedence, against the two rungs BELOW it on this ladder.

    The cloud ladder has three rungs, so its pilot-before-linearity test covers
    its whole ordering claim. The lateral ladder has seven, and a capture too
    quiet to hear can trip the schedule residual and the clip detector as
    easily as the linearity screen — so the room verdict has to outrank all
    three, not just the one the shorter ladder happens to share.
    """
    kind = spatial.lateral_pose_screens(
        _screens(
            pilot_snr_ok=False, sweep_schedule_ok=False,
            any_sweep_clipped=True, linearity_ok=False,
        ),
    )

    assert kind == spatial.SCREEN_PILOT_LEVEL_COLLAPSE


def test_a_take_nothing_located_is_named_that_whatever_else_is_wrong():
    """The top rung of all three ladders, asserted against a maximally broken take.

    ``stimulus_located`` is False means no driver was heard at all. Every screen
    below it is then measuring noise, so any of their verdicts would be a
    confident statement about a capture that contains nothing — the honest
    answer is the one the household can act on.
    """
    broken = _screens(
        stimulus_located=False, pilot_snr_ok=False, linearity_ok=False,
        glitch_detected=True, sweep_locate_confidence_ok=False,
        sweep_schedule_ok=False, any_sweep_clipped=True,
    )

    assert spatial.cloud_position_screens(
        broken, has_summed_response=False,
    ) == spatial.SCREEN_LOCATE_FAILED
    assert spatial.lateral_pose_screens(broken) == spatial.SCREEN_LOCATE_FAILED


def test_a_clean_take_clears_every_ladder():
    """The control the four tests above need.

    Without it a ladder that refused unconditionally would satisfy every
    ordering assertion here and fail nobody.
    """
    assert spatial.cloud_position_screens(
        _screens(), has_summed_response=True,
    ) is None
    assert spatial.lateral_pose_screens(_screens()) is None
    assert spatial.lateral_curves_sufficient(2) is None


# --------------------------------------------------------------------------- #
# 2. the entry baseline's integrity split
#
# One evaluator verdict (UNUSABLE), two household sentences, and the branch
# between them is a membership test on the FAILED check names. Both arms and
# the absent case, because "fails closed on an absent record" is the whole
# reason this ladder differs from VERIFY's.
# --------------------------------------------------------------------------- #


class _Analysis:
    """The three fields the entry ladder reads, and nothing else.

    A stand-in rather than a real ``ProgramAnalysis`` because this section is
    about the ladder's branching, not about the analyzer: the reduction step is
    never reached in any of these cases (every one refuses before it), which
    the ``measured is None`` assertion in each test makes checkable rather than
    assumed.
    """

    def __init__(self, *, integrity, pilot_snr_ok=True, linearity_ok=True):
        self.capture_integrity = integrity
        self.pilot_snr_ok = pilot_snr_ok
        self.linearity_ok = linearity_ok


def _failed(name: str) -> CaptureIntegrity:
    return CaptureIntegrity(
        checks=(IntegrityCheck(name=name, status=INTEGRITY_FAIL),),
    )


def test_a_sweep_nobody_could_hear_is_a_level_problem_not_a_glitch():
    """The first arm: ``sweep_heard`` failed ⇒ the locate sentence.

    #1838 §5.2's split, and the household action is the difference — a level or
    microphone fix, versus a silent auto-retry of the same capture. Mutation-
    selected: inverting this membership test left 21 tests green.
    """
    screen = spatial.entry_baseline_screens(
        _Analysis(integrity=_failed(flow.INTEGRITY_CHECK_SWEEP_HEARD)),
        stimulus_located=True,
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
    )

    assert screen.kind == spatial.SCREEN_LOCATE_FAILED
    assert screen.measured is None
    assert screen.integrity_payload == {
        "capture_integrity": _failed(
            flow.INTEGRITY_CHECK_SWEEP_HEARD
        ).to_dict()
    }


def test_any_other_failed_check_is_the_transient_glitch_class():
    """The second arm, and the reason the first is a membership test rather
    than a truthiness one: a spliced timeline is a different fault with a
    different remedy, and it must not inherit the level sentence."""
    screen = spatial.entry_baseline_screens(
        _Analysis(integrity=_failed("timeline_contiguous")),
        stimulus_located=True,
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
    )

    assert screen.kind == spatial.SCREEN_CAPTURE_GLITCH
    assert screen.measured is None


def test_an_absent_integrity_record_fails_this_capture_closed():
    """The case that distinguishes this ladder from VERIFY's, and it is a REFUSAL.

    VERIFY treats an absent integrity record as no-evidence-and-continue. The
    entry baseline cannot: it exists only to be compared, and a before-side
    nobody graded cannot carry a before→after claim — it would seed a benefit
    verdict with a capture whose validity is unknown.

    Mutation-selected: letting an absent record through left 21 tests green,
    which is how a fail-closed rule quietly becomes a fail-open one.
    """
    screen = spatial.entry_baseline_screens(
        _Analysis(integrity=None),
        stimulus_located=True,
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
    )

    assert screen.kind == spatial.SCREEN_CAPTURE_GLITCH, (
        "an absent record takes the glitch kind's silent auto-retry — there is "
        "nothing for the household to fix, and it is worth one more try"
    )
    assert screen.measured is None
    assert screen.integrity_payload == {"capture_integrity": None}


def test_the_entry_ladder_still_names_the_room_before_the_record():
    """#1810 once more, on the third ladder — ahead of the integrity branch.

    A pilot pair that never cleared the floor will also produce an integrity
    record that fails, so without this ordering every quiet room in the world
    is reported as a capture glitch.
    """
    screen = spatial.entry_baseline_screens(
        _Analysis(
            integrity=_failed("timeline_contiguous"), pilot_snr_ok=False,
        ),
        stimulus_located=True,
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
    )

    assert screen.kind == spatial.SCREEN_PILOT_LEVEL_COLLAPSE


# --------------------------------------------------------------------------- #
# 3. the records — the two fields a replay is joined on
# --------------------------------------------------------------------------- #


def test_a_position_take_id_is_qualified_by_the_attempt():
    """A geometry retake reuses its position id, so the id alone is not a take.

    The evidence store is write-once, so an unqualified id would make the
    retake's sidecar a PATH_CONFLICT and leave the REPLACED take as the only
    record of a curve that is not in the cloud. Zero-padded so a lexical sort
    of the bundle is also a chronological one.

    Mutation-selected: dropping the attempt suffix left 374 tests green.
    """
    record = spatial.cloud_position_record(
        position_id="cloud_measure_03", phase=PHASE_CLOUD_MEASURE, index=3,
        attempt=7, prompt="stand here", wide=False, role="onax",
        captured_at=1.0, session_id="sess", gate_window_ms=12.0,
        gate_floor_source="reflection", gate_disclosure="a wall",
        validity_floor_hz=100.0, gating_applied=True, summed_ripple_db=1.0,
        glitch_detected=False, wav_sha256="abc",
    )

    assert record["take_id"] == "cloud_measure_03_a07"
    assert record["position_id"] == "cloud_measure_03"


def test_an_entry_baseline_take_id_carries_index_and_attempt():
    """The same rule on the phase that is NOT a group member.

    The entry baseline rides the same retention seam and lands in the same
    ``position_artifacts`` namespace, so it needs the same collision-free id —
    but nothing in the group bookkeeping would give it one.

    Mutation-selected: dropping the attempt suffix left 21 tests green.
    """
    record = spatial.entry_baseline_record(
        index=9, attempt=2, session_id="sess", program_id="prog",
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
        graph_fingerprint="fp", captured_at="2026-08-11T00:00:00Z",
        validity_floor_hz=100.0, gate_window_ms=12.0, summed_ripple_db=1.0,
        glitch_detected=False, wav_sha256="abc",
    )

    assert record["take_id"] == "entry_baseline_09_a02"
    assert record["position_id"] == record["take_id"]


def test_the_three_comparability_facts_ride_the_entry_record():
    """WHAT was played, WHERE from, and THROUGH WHICH graph.

    A before→after claim is only as good as those three matching on both sides,
    and they are the whole reason this is a separate builder rather than a
    keyword on the position one.
    """
    record = spatial.entry_baseline_record(
        index=9, attempt=1, session_id="sess", program_id="prog-42",
        reference_mark=flow.REFERENCE_MARK_DESIGN_AXIS,
        graph_fingerprint="fp-entry", captured_at="2026-08-11T00:00:00Z",
        validity_floor_hz=None, gate_window_ms=None, summed_ripple_db=None,
        glitch_detected=False, wav_sha256=None,
    )

    assert record["program_id"] == "prog-42"
    assert record["reference_mark"] == flow.REFERENCE_MARK_DESIGN_AXIS
    assert record["graph_fingerprint"] == "fp-entry"


# --------------------------------------------------------------------------- #
# 4. the geometry retake, over its whole truth table
#
# Two of its conjuncts are pinned through the conductor already (a thin lock is
# accepted; a re-close never re-asks) — ``tests/test_crossover_v2_conductor.py``
# owns those, and mutating either turns it red. What is NOT covered there is the
# FIFTH condition, which is not a conjunct at all.
# --------------------------------------------------------------------------- #


def test_a_close_with_no_take_never_asks_for_a_retake():
    """The SETTLED close: warranted on every conjunct, and it still must not ask.

    The retake lever works by REJECTING the take at this index. A group that
    closed because its last position was settled without a curve has no take to
    reject, so asking would re-open the slot whose tries are exactly what just
    ran out — an unbounded loop offered to a household who has already run out
    of attempts.
    """
    warranted = dict(
        locked=True, thin_evidence=False, retries_used=0,
        budget=flow.GEOMETRY_RETRY_POSITIONS, group_already_closed=False,
    )

    assert spatial.geometry_retake(**warranted, have_take_to_replace=True)
    assert spatial.geometry_retake(**warranted, have_take_to_replace=False) is None


def test_the_rung_shown_and_the_counter_written_advance_together():
    """One fact, two consumers — so they are computed in one place.

    ``rung`` picks the sentence the household reads and ``retries_after`` is
    what the budget check sees next time. They were two expressions over the
    same counter at the call site, which is how a rung can repeat while the
    budget still advances.
    """
    first = spatial.geometry_retake(
        locked=True, thin_evidence=False, retries_used=0, budget=2,
        group_already_closed=False, have_take_to_replace=True,
    )
    second = spatial.geometry_retake(
        locked=True, thin_evidence=False, retries_used=1, budget=2,
        group_already_closed=False, have_take_to_replace=True,
    )

    assert (first.rung, first.retries_after) == (0, 1)
    assert (second.rung, second.retries_after) == (1, 2)
    # …and the budget really binds.
    assert spatial.geometry_retake(
        locked=True, thin_evidence=False, retries_used=2, budget=2,
        group_already_closed=False, have_take_to_replace=True,
    ) is None


@pytest.mark.parametrize("locked", [False, None])
def test_only_a_LOCKED_verdict_can_warrant_a_retake(locked):
    """``is True``, not truthiness: ``None`` is "the instrument did not say",
    and walking two more positions cannot improve a verdict that was never
    made."""
    assert spatial.geometry_retake(
        locked=locked, thin_evidence=False, retries_used=0, budget=2,
        group_already_closed=False, have_take_to_replace=True,
    ) is None


# --------------------------------------------------------------------------- #
# 5. the group floor, and the kind→code mapping's completeness
# --------------------------------------------------------------------------- #


def test_only_the_lateral_walk_can_stand_on_nothing():
    """§4.4: "side evidence owns robustness, not the target".

    A cloud is an AVERAGE — below the floor there is nothing to combine. The
    lateral walk's coefficients are the anchor's and already in hand, so a pose
    nobody could capture costs a robustness sample and nothing else.
    """
    floor = flow.MIN_RESOLVED_CLOUD_POSITIONS

    assert spatial.group_position_floor(
        PHASE_LATERAL, min_resolved_cloud_positions=floor,
    ) == 0
    for phase in (PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY):
        assert spatial.group_position_floor(
            phase, min_resolved_cloud_positions=floor,
        ) == floor


def test_every_screen_kind_has_a_household_sentence():
    """The completeness check the mapping exists to make possible.

    ``SCREEN_KINDS`` is declared so the flow's mapping can be verified rather
    than trusted — the same discipline ``coordinator.REFUSAL_KINDS`` keeps. A
    kind added without an arm ships wearing another kind's copy, and this is
    what stops that from being discovered by a household.
    """
    assert set(flow.SCREEN_KIND_REASONS) == set(spatial.SCREEN_KINDS)
    for code in flow.SCREEN_KIND_REASONS.values():
        assert code in flow.REASON_REGISTRY, code


def test_an_unrecognised_kind_is_loud_rather_than_silent(caplog):
    """The other half: the fallback exists, and it shouts.

    Reached with a kind no released ladder returns, which is the shape of the
    future defect — the point is that it cannot arrive quietly. It still
    refuses rather than raising: the capture WAS screened and something was
    wrong with it, and losing that to a mapping gap is worse than naming it
    imprecisely for one release.
    """
    with caplog.at_level("INFO"):
        code = flow._screen_refusal_code("a_kind_from_the_future")

    unmapped = [
        r for r in caplog.records
        if "crossover_v2_screen_kind_unmapped" in r.getMessage()
    ]
    assert [r.levelname for r in unmapped] == ["ERROR"]
    assert code == flow.REASON_LOCATE_FAILED


def test_the_declared_kinds_are_the_ones_the_ladders_can_return():
    """``SCREEN_KINDS`` is a declaration, and a declaration can go stale.

    Every kind the three ladders actually produce is driven out of them here,
    so a kind that stops being reachable — or one returned but never declared —
    shows up as a set difference rather than as a household reading the wrong
    sentence.
    """
    produced = {
        spatial.cloud_position_screens(
            _screens(stimulus_located=False), has_summed_response=False,
        ),
        spatial.cloud_position_screens(
            _screens(pilot_snr_ok=False), has_summed_response=True,
        ),
        spatial.cloud_position_screens(
            _screens(linearity_ok=False), has_summed_response=True,
        ),
        spatial.lateral_pose_screens(_screens(glitch_detected=True)),
        spatial.lateral_pose_screens(_screens(any_sweep_clipped=True)),
        spatial.lateral_curves_sufficient(0),
    }

    assert produced == set(spatial.SCREEN_KINDS)


# --------------------------------------------------------------------------- #
# 5b. the boost derivation's journal fields — ORDER included
# --------------------------------------------------------------------------- #

#: The field names ``event=correction.crossover_v2_boost_evidence`` carried, in
#: the order it carried them, read off the PRE-EXTRACTION call site
#: (``origin/main`` at 5dcd872a4, ``_boost_excluded_bands_hz``). Anchored on
#: that declaration rather than on what the module returns today, for the reason
#: the fc-selector golden states: a pin that reads the thing under test is blind
#: to uniform drift, which is exactly what a port produces.
#:
#: Order is load-bearing, not cosmetic — ``log_event``'s own contract is
#: "keyword fields become ``k=v`` pairs in the order given", so a reshuffled
#: mapping silently reshuffles every line an operator greps.
_BOOST_EVIDENCE_FIELDS = (
    "registry_band_hz",
    "registry_classification",
    "registry_reason",
    "unadjudicated_span_hz",
    "variance_reason",
    "n_dips",
    "n_position_dependent",
    "boost_excluded_bands_hz",
)


def test_the_boost_diagnostics_render_the_shipped_event_unchanged():
    """The derivation moved; its journal line must not have.

    ``spatial`` is side-effect-free, so it hands these back as data and the flow
    expands them into the event it still owns. That expansion preserves dict
    order, which makes the mapping's key order the line's field order — so this
    asserts the ORDER, not just the set.

    ``variance_check_failed`` is the one key that is NOT a field: it routes the
    second, separate event and the flow pops it before expanding. It is asserted
    here too, because a key that stopped being popped would silently append a
    ninth field to every line.
    """
    exclusion = spatial.boost_excluded_bands_hz(
        object(), {}, echo_band_hz=(4000.0, 16000.0),
    )

    keys = list(exclusion.diagnostics)
    assert keys[-1] == "variance_check_failed"
    assert tuple(keys[:-1]) == _BOOST_EVIDENCE_FIELDS


def test_a_cloud_with_no_grid_fails_open_rather_than_banning_boost():
    """The fail-OPEN arm, which is the one with a hearing consequence.

    A span too narrow to analyse, a cloud that never retained per-position
    curves, or a numeric failure must all yield NO exclusions — i.e. exactly the
    permission the gate already grants. Failing closed would blanket-ban boost
    below 4 kHz on a computation hiccup, which is the blunt outcome the whole
    function exists to avoid.
    """
    exclusion = spatial.boost_excluded_bands_hz(
        object(), {}, echo_band_hz=(4000.0, 16000.0),
    )

    assert exclusion.bands == ()
    assert exclusion.diagnostics["variance_reason"] == "no_blind_span"
    assert exclusion.diagnostics["variance_check_failed"] is False


# --------------------------------------------------------------------------- #
# 6. the module's own boundary
# --------------------------------------------------------------------------- #


def _spatial_imports() -> set[str]:
    """Every module name :mod:`.spatial` imports, including inside functions.

    Parsed rather than read as text, because the module's own docstring NAMES
    the modules it must not import — a substring check calls that a violation.
    Parsed rather than introspected, because an import that only runs inside a
    function leaves no trace on the module object, and that is exactly the
    shape a later edit would take.
    """
    import ast
    from pathlib import Path

    source = (
        Path(spatial.__file__).resolve()
    ).read_text()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_module_reaches_neither_the_flow_nor_the_web_layer():
    """The dependency direction every module in this package states."""
    imported = _spatial_imports()

    assert not [n for n in imported if "crossover_v2_flow" in n], imported
    assert not [n for n in imported if n.startswith("jasper.web")], imported


def test_the_module_writes_no_journal_lines():
    """:mod:`.coordinator`'s docstring claims it is this package's ONLY
    side-effecting module, and a claim nobody checks is how that stops being
    true.

    The boost derivation is the one function here with a journal line in the
    shipped flow; it returns those fields as data instead
    (:class:`~.spatial.BoostExclusion`), and the flow emits them under the
    event name it owns.
    """
    import ast
    from pathlib import Path

    assert "jasper.log_event" not in _spatial_imports()
    assert not hasattr(spatial, "logger")

    tree = ast.parse(Path(spatial.__file__).resolve().read_text())
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "log_event" not in called
