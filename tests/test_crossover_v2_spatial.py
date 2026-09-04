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

from types import SimpleNamespace

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import capture_dispatch, spatial
from jasper.active_speaker.crossover_v2.contracts import (
    ENTRY_GRAPH_FINGERPRINT_UNKNOWN,
    MEASURE_KIND_BASELINE,
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_VERIFY,
    MEASURE_KINDS,
    POLARITY_INVERTED,
    REFERENCE_MARK_DESIGN_AXIS,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_VERIFY,
)
from jasper.active_speaker.crossover_v2.position_cycle import (
    POSITION_EVIDENCE_KIND,
)
from jasper.audio_measurement import gating, program
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


#: The resilience lens's repro value, written as a literal rather than as
#: ``spatial.SCREEN_LOCATE_FAILED``: the test below asks what the ladder
#: ANSWERS, and comparing it against the module's own constant would pass even
#: if both moved together.
SCREEN_LOCATE_FAILED_REPRO = "locate_failed"


def test_the_lateral_ladder_answers_before_anything_could_be_built():
    """The unit half of the screens-before-curves pin.

    :func:`~jasper.active_speaker.crossover_v2.spatial.lateral_curves_sufficient`
    is a SECOND call precisely so a rejected take never reaches the curve
    builder, which indexes its input's frequency axis and raises on an empty
    one. That claim is only true if the ladder returns a refusal from
    :func:`lateral_pose_screens` alone — with no curve count, and therefore
    nothing built.

    The conductor-path half, driving a real degenerate response through
    ``_consume_lateral_pose``, is
    ``test_a_screened_out_pose_refuses_before_any_curve_is_built`` in
    ``tests/test_crossover_v2_lateral_evidence.py``. This one pins the seam that
    makes it possible: a complete answer from an argument list that cannot
    contain a curve.
    """
    assert spatial.lateral_pose_screens(
        _screens(stimulus_located=False)
    ) == SCREEN_LOCATE_FAILED_REPRO

    # And the signature is what enforces it: the ladder takes screens only, so a
    # caller CANNOT hand it a curve count and there is nothing to build first.
    import inspect

    params = inspect.signature(spatial.lateral_pose_screens).parameters
    assert list(params) == ["screens"]


def test_no_screen_may_be_left_unstated():
    """Every ``CaptureScreens`` field is required, and none may regain a default.

    A default here is only ever "correct" because the ladder that reads this
    record today happens to ignore the field — and the day a rung is added that
    does read it, the caller was never asked, so the default answers for a
    capture nobody looked at. It would silently never fire while reading as
    covered from both ends.

    The four that regressed this way once (``glitch_detected``,
    ``sweep_locate_confidence_ok``, ``sweep_schedule_ok``, ``any_sweep_clipped``)
    are named rather than counted, because the failure mode is one field
    quietly acquiring a default, not the total changing.
    """
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(spatial.CaptureScreens)}

    assert set(fields) == {
        "stimulus_located", "pilot_snr_ok", "linearity_ok", "glitch_detected",
        "sweep_locate_confidence_ok", "sweep_schedule_ok", "any_sweep_clipped",
    }
    defaulted = [
        name for name, f in fields.items()
        if f.default is not dataclasses.MISSING
        or f.default_factory is not dataclasses.MISSING
    ]
    assert defaulted == [], (
        f"these screens can be left unstated: {defaulted} — a caller that omits "
        "one is asserting something about a capture it never examined"
    )


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
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
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
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
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
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
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
        reference_mark=REFERENCE_MARK_DESIGN_AXIS,
    )

    assert screen.kind == spatial.SCREEN_PILOT_LEVEL_COLLAPSE


# --------------------------------------------------------------------------- #
# 3. the records — the two fields a replay is joined on
# --------------------------------------------------------------------------- #


def _cloud_record(**overrides):
    """One retained cloud position, with only the field under test named."""
    fields = {
        "position_id": "cloud_measure_03", "phase": PHASE_CLOUD_MEASURE,
        "index": 3, "attempt": 7, "prompt": "stand here", "wide": False,
        "role": "onax",
        "geometry": spatial.PositionGeometry(
            axis=spatial.POSITION_AXIS_HORIZONTAL, degrees=-7,
            mark_distance_m=1.0,
        ),
        "captured_at": 1.0, "session_id": "sess", "gate_window_ms": 12.0,
        "gate_floor_source": "reflection", "gate_disclosure": "a wall",
        "gate_moved_rms_db": 2.59, "gate_reflection_delay_ms": 5.33,
        "gate_entanglement_floor_hz": 1000.0,
        "gate_entanglement_floor_source": gating.ENTANGLEMENT_SOURCE_MEASURED,
        "validity_floor_hz": 100.0, "gating_applied": True,
        "summed_ripple_db": 1.0, "glitch_detected": False, "wav_sha256": "abc",
    }
    return spatial.cloud_position_record(**{**fields, **overrides})


def test_a_position_take_id_is_qualified_by_the_attempt():
    """A geometry retake reuses its position id, so the id alone is not a take.

    The evidence store is write-once, so an unqualified id would make the
    retake's sidecar a PATH_CONFLICT and leave the REPLACED take as the only
    record of a curve that is not in the cloud. Zero-padded so a lexical sort
    of the bundle is also a chronological one.

    Mutation-selected: dropping the attempt suffix left 374 tests green.
    """
    record = _cloud_record()

    assert record["take_id"] == "cloud_measure_03_a07"
    assert record["position_id"] == "cloud_measure_03"


def test_every_take_builder_states_one_identity_under_one_vocabulary():
    """The common core, asserted as a SET rather than key by key.

    A cloud position, a walk pose, an entry baseline and an unprompted-phase
    capture are different captures and their grading columns are never
    meaningful for each other — but the six facts that say WHICH take this is
    are the same question four times, and a reader that had to spell them
    differently per kind is the duplication row 4b names. A builder that spells
    one of these its own way turns this red — which is why the fourth one is
    here rather than pinned apart: the promise this docstring made was empty
    for as long as the tuple below listed only three.
    """
    core = {"phase", "index", "attempt", "take_id", "session_id", "wav_sha256"}
    cloud = _cloud_record()
    pose = _pose_record()
    entry = _entry_record(index=3, attempt=7, session_id="sess", wav_sha256="abc")
    unprompted = _phase_record(
        index=3, attempt=7, session_id="sess", wav_sha256="abc",
    )

    for record in (cloud, pose, entry, unprompted):
        assert core <= set(record)
        assert record["attempt"] == 7
        assert record["session_id"] == "sess"
        assert record["wav_sha256"] == "abc"
        # The verifier is not the index: a take id is derivable from the id and
        # the attempt, and never from the digest.
        assert record["wav_sha256"] not in record["take_id"]
    # Each kind keeps its OWN word for the place it names, which is the
    # role-tagged extension the common core sits under.
    assert cloud["position_id"] == "cloud_measure_03"
    assert pose["pose_id"] == "lateral_03"
    assert "pose_id" not in cloud and "position_id" not in pose


def _pose_record(**overrides):
    """One retained lateral pose, with only the field under test named."""
    fields = {
        "position_deg": -22, "lateral_consumer": "fc_selector",
        "session_id": "sess", "graph_fingerprint": "fp-applied",
        "captured_at": "2026-08-26T00:00:00Z", "wav_sha256": "abc",
    }
    return spatial.lateral_pose_record(
        spatial.LateralPose(
            pose_id="lateral_03", index=3, attempt=7, prompt="step left",
            role="offax", offset_cm=25.0, at_mark=False, curves=(),
        ),
        **{**fields, **overrides},
    )


def _entry_record(**overrides):
    """One retained entry-baseline take, with only the field under test named."""
    fields = {
        "index": 9, "attempt": 1, "session_id": "sess", "program_id": "prog",
        "reference_mark": REFERENCE_MARK_DESIGN_AXIS,
        "graph_fingerprint": "fp", "captured_at": "2026-08-11T00:00:00Z",
        "freqs_hz": (200.0, 400.0), "magnitude_db": (-1.5, 0.5),
        "excluded": (True, False),
        "validity_floor_hz": 100.0, "gate_window_ms": 12.0,
        "summed_ripple_db": 1.0, "glitch_detected": False, "wav_sha256": "abc",
    }
    return spatial.entry_baseline_record(**{**fields, **overrides})


def _phase_record(**overrides):
    """One retained unprompted-phase take (CHECK / MEASURE / VERIFY).

    The fourth builder. It joins every family pin below rather than getting
    pins of its own: "does this builder spell the family's vocabulary" is one
    question, and asking it three times while a fourth builder answers
    separately is how the shapes drift apart.
    """
    fields = {
        "phase": PHASE_VERIFY, "index": 3, "attempt": 1, "session_id": "sess",
        "graph_fingerprint": "fp", "captured_at": "2026-08-11T00:00:00Z",
        "wav_sha256": "abc",
    }
    return spatial.phase_capture_record(**{**fields, **overrides})


def test_every_retained_take_kind_states_when_it_was_captured():
    """``captured_at`` on every builder, not on some of them.

    A walk pose was the one retained take carrying no clock, so a banked round
    could say WHERE each capture was taken and in what ORDER the walk served
    them, but never WHEN. Sorting or windowing banked rounds by time had to
    fall back on file mtime, which WO-0 measured actively misrouting.
    """
    for record in (
        _cloud_record(), _pose_record(), _entry_record(), _phase_record(),
    ):
        assert record["captured_at"]


def test_two_walks_at_one_pose_are_told_apart_by_the_applied_candidate():
    """The pose identity cannot separate them; the graph fingerprint can.

    A walk re-run at the same bearing under a different applied candidate mints
    the SAME take id — the id is the pose plus the attempt, and neither moved.
    Without this column the two records are indistinguishable, which is what
    forced a reader wanting to know which graph a walk ran under to open the
    capture-retention ring's sidecar instead of the take itself.
    """
    before = _pose_record(graph_fingerprint="fp-entry")
    after = _pose_record(graph_fingerprint="fp-candidate")

    assert before["take_id"] == after["take_id"]
    assert before["graph_fingerprint"] != after["graph_fingerprint"]


#: Every fact the engine's own record carries
#: (``session.TuningSession._record``), under the name a retained take spells
#: it. A take banked by the flow and a take banked by the engine must be ONE
#: shape: offline re-analysis (ruling S3) reads the bank, and a reader that had
#: to ask which of two shapes it was holding could not run the same analysis
#: over both.
#:
#: One name differs, and only while the flow still publishes through an
#: envelope: the engine's ``kind`` is ``measure_kind`` here, because ``kind`` in
#: a published take is this package's document-type discriminator. See
#: ``spatial._take_identity``.
_ENGINE_RECORD_FIELDS = (
    "session_id", "measure_kind", "baseline_record_id", "position_deg",
    "position_axis", "vertical_deg", "prompt", "candidate_id", "regime",
    "polarity", "level_matched", "graph_fingerprint", "level_db",
    "stimulus_dbfs", "incident", "wav_path",
)


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_every_take_builder_carries_the_whole_engine_record(builder):
    """All of them, on all four — parametrized, because it is one question.

    Six of these were banked by NO builder before this pin: the comparand
    (``baseline_record_id``), which candidate was under test (``candidate_id``),
    which way the driver was wired (``polarity``), the PROVEN fader level
    (``level_db``), the ladder rung (``stimulus_dbfs``) and what went wrong
    (``incident``) — and ``wav_path``, the pointer that lets a banked record
    reach its own capture, was carried by none of the four.

    Presence, not value: what a caller does not state is honestly empty, and
    the fields' contents are each other tests' subject.
    """
    record = builder()

    assert set(_ENGINE_RECORD_FIELDS) <= set(record)


#: The six that no builder banked, each with a value nothing else on a record
#: could be mistaken for. Presence alone would go green against carriers that
#: emitted a constant empty, which is what these are until the retention lift
#: states them.
_STATED_CLAIM = spatial.TakeClaim(
    baseline_record_id="rec-before-7",
    candidate_id="cand-fp-42",
    polarity=POLARITY_INVERTED,
    level_matched=True,
    level_match_trims_db={"tweeter": -9.5},
    level_db=-18.5,
    stimulus_dbfs=-9.0,
    incident="unproven_level",
)


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
@pytest.mark.parametrize(
    "engine_field, stated",
    [
        ("baseline_record_id", "rec-before-7"),
        ("candidate_id", "cand-fp-42"),
        ("polarity", POLARITY_INVERTED),
        ("level_matched", True),
        ("level_match_trims_db", {"tweeter": -9.5}),
        ("level_db", -18.5),
        ("stimulus_dbfs", -9.0),
        ("incident", "unproven_level"),
    ],
)
def test_a_stated_claim_reaches_the_record_it_was_stated_for(
    builder, engine_field, stated,
):
    """Each of the six CARRIES, rather than merely appearing.

    A carrier that emitted a constant empty would satisfy a presence pin
    forever — and these six are inert in production until the retention lift
    binds a claim, so a presence pin is exactly the shape that would rot
    unnoticed. Distinct values per field, so a builder that stamped one of them
    into another's slot is red rather than lucky.

    ``level_db`` and ``stimulus_dbfs`` are two numbers on purpose: a ladder
    moves the stimulus, never the claim.
    """
    record = builder(claim=_STATED_CLAIM)

    assert record[engine_field] == stated


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_a_take_record_does_not_overwrite_the_envelopes_document_type(builder):
    """A retained take is published INSIDE an envelope, and it splats in last.

    ``kind`` in a published take names the DOCUMENT — the same convention every
    other artifact in this package follows — and both readers of a banked take
    refuse anything whose ``kind`` is not ``POSITION_EVIDENCE_KIND``. A record
    that spelled its measure kind ``kind`` would replace the discriminator on
    the way out and make every banked take unreadable, silently: the walk index
    would report a round with no poses rather than an error.
    """
    record = builder()

    # The key itself, not just the surviving value: an emission that wrote the
    # RIGHT word under the WRONG key would leave the envelope below intact and
    # still be the collision this pin exists for.
    assert "kind" not in record
    assert {"schema_version": 1, "kind": POSITION_EVIDENCE_KIND, **record}[
        "kind"
    ] == POSITION_EVIDENCE_KIND


def test_a_takes_kind_is_read_off_the_graph_and_never_off_the_phase():
    """``PHASE_LATERAL`` is a baseline OR a candidate check; only the graph says.

    A silent ``phase → kind`` map does not merely mislabel some records — it is
    not well defined. One per-driver walk is the "before" when it ran on the
    round's entry graph and the candidate check when it ran on an applied one,
    and the take id, the pose and the phase are identical either way.
    """
    kinds = {
        pose["measure_kind"]
        for pose in (
            _pose_record(
                graph_fingerprint="fp-entry",
                claim=spatial.TakeClaim(baseline_fingerprint="fp-entry"),
            ),
            _pose_record(
                graph_fingerprint="fp-candidate",
                claim=spatial.TakeClaim(baseline_fingerprint="fp-entry"),
            ),
        )
    }

    assert kinds == {MEASURE_KIND_BASELINE, MEASURE_KIND_CANDIDATE}
    assert kinds <= set(MEASURE_KINDS)


def test_a_re_measure_after_the_apply_is_a_verify_and_not_a_candidate():
    """The one branch the pose pins above can never reach.

    A verify take and a candidate take BOTH played through a graph that is not
    the round's "before", so the fingerprints alone cannot separate them — the
    phase is what says this capture is the re-measure that grades an apply
    rather than the check that proposed it. That is the single job ``phase``
    has here, and it is why the rest of the rule refuses to read it.
    """
    verify = _cloud_record(
        phase=PHASE_CLOUD_VERIFY,
        graph_fingerprint="fp-candidate",
        claim=spatial.TakeClaim(baseline_fingerprint="fp-entry"),
    )
    candidate = _cloud_record(
        phase=PHASE_CLOUD_MEASURE,
        graph_fingerprint="fp-candidate",
        claim=spatial.TakeClaim(baseline_fingerprint="fp-entry"),
    )

    assert verify["measure_kind"] == MEASURE_KIND_VERIFY
    assert candidate["measure_kind"] == MEASURE_KIND_CANDIDATE


@pytest.mark.parametrize(
    "graph_fingerprint, baseline_fingerprint",
    [
        (ENTRY_GRAPH_FINGERPRINT_UNKNOWN, "fp-entry"),
        ("fp-candidate", ENTRY_GRAPH_FINGERPRINT_UNKNOWN),
        ("", "fp-entry"),
        ("fp-candidate", ""),
    ],
)
def test_a_take_whose_graph_is_unnamed_says_so_instead_of_guessing(
    graph_fingerprint, baseline_fingerprint,
):
    """Both spellings of "no graph", from both sides of the comparison.

    ``""`` is what a host that could not name its graph leaves, and
    ``unknown`` is what ``entry_graph_fingerprint`` returns when no applied
    profile was found. Either one on either side and the kind is unresolvable —
    so the record says nothing rather than picking the wrong one, which is
    ``baseline_record_id``'s precedent: an honest fact about the capture, never
    a refusal to bank it.
    """
    record = _pose_record(
        graph_fingerprint=graph_fingerprint,
        claim=spatial.TakeClaim(baseline_fingerprint=baseline_fingerprint),
    )

    assert record["measure_kind"] == ""


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_a_take_carries_the_path_of_the_capture_it_was_reduced_from(builder):
    """The pointer, round-tripped — what makes offline analysis reachable.

    A capture's bundle-relative path is NOT derivable from the take id:
    ``bundles.capture_artifact_relpath`` appends a ``uuid4`` hex, so whoever
    mints the path before the write is the only party that can state it. Banked
    without it, a record names a curve whose audio nothing can find again.
    """
    minted = "captures/summed/cloud_measure_03_a07-9f2c.wav"

    record = builder(claim=spatial.TakeClaim(wav_path=minted))

    assert record["wav_path"] == minted
    # The verifier and the pointer are two facts, not one: the digest says
    # whether the bytes are the right ones, the path says where they are.
    assert record["wav_sha256"] != record["wav_path"]


def test_a_banked_pose_curve_reconstructs_the_complex_transfer_function():
    """Ruling S3's whole point, as arithmetic: PHASE survives the bank.

    ``complex_tf`` had no serializer, so every offline re-analysis re-derived
    phase from the WAVs and the forward model could not run from the bank at
    all.  The pin is the round trip — magnitude and phase back to the complex
    value the transform produced — because a record that banked magnitude and
    a zero, or magnitude and an unwrapped view of the phase, would look
    perfectly well-formed and answer a different question.

    The fixture's phases are deliberately spread outside (-pi, pi] so a
    reconstruction that agreed only on the principal branch fails here.
    """
    import numpy as np

    freqs = np.array([100.0, 1000.0, 5000.0, 12000.0])
    tf = np.array([0.5, 2.0, 0.25, 1.0]) * np.exp(
        1j * np.array([0.3, 3.0, -2.9, 4.2])
    )
    record = spatial.pose_curve_record(
        spatial.LateralPoseCurve(
            role="woofer", freqs_hz=freqs, complex_tf=tf, band_hz=(80.0, 14000.0),
        )
    )

    assert record["role"] == "woofer"
    assert record["band_hz"] == [80.0, 14000.0]
    assert record["freqs_hz"] == [100.0, 1000.0, 5000.0, 12000.0]

    rebuilt = 10.0 ** (np.asarray(record["magnitude_db"]) / 20.0) * np.exp(
        1j * np.radians(np.asarray(record["phase_deg"]))
    )
    assert np.allclose(rebuilt, tf)


def test_a_banked_pose_curve_floors_a_deep_null_instead_of_banking_minus_infinity():
    """A bin that cancelled to exactly zero is not JSON as ``-inf``.

    Same 1e-12 floor, at the same place in the arithmetic, that
    ``magnitude_response`` applies for the same reason.
    """
    import numpy as np

    record = spatial.pose_curve_record(
        spatial.LateralPoseCurve(
            role="tweeter",
            freqs_hz=np.array([1000.0]),
            complex_tf=np.array([0.0 + 0.0j]),
            band_hz=(1500.0, 20000.0),
        )
    )

    assert np.isfinite(record["magnitude_db"]).all()


def test_the_pose_record_banks_one_curve_per_driver_it_measured():
    """The pose's OWN curves ride its record — not a re-derivation, and not a
    count the walk bookkeeping supplied separately."""
    import numpy as np

    def _curve(role: str) -> spatial.LateralPoseCurve:
        return spatial.LateralPoseCurve(
            role=role, freqs_hz=np.array([1000.0]),
            complex_tf=np.array([1.0 + 0.0j]), band_hz=(80.0, 20000.0),
        )

    record = spatial.lateral_pose_record(
        spatial.LateralPose(
            pose_id="lateral_03", index=3, attempt=7, prompt="step left",
            role="offax", offset_cm=25.0, at_mark=False,
            curves=(_curve("woofer"), _curve("tweeter")),
        ),
        position_deg=-22, lateral_consumer="fc_selector", session_id="sess",
        graph_fingerprint="fp-applied", captured_at="2026-08-26T00:00:00Z",
        wav_sha256="abc",
    )

    assert [c["role"] for c in record["curves"]] == ["woofer", "tweeter"]
    assert all("phase_deg" in c for c in record["curves"])


@pytest.mark.parametrize("vertical_deg", [0, 20, -20])
def test_a_pose_records_the_elevation_it_was_GIVEN_on_the_horizontal_axis(
    vertical_deg,
):
    """A COMPOUND pose states both numbers, and the axis word does not move.

    ``position_axis`` names the plane a pose's stated BEARING lies in, and
    every pose reaching this builder commands one — ``vertical`` is reserved
    for a pose that commands NO bearing (``PositionGeometry``). So a pose swung
    AND raised stays ``horizontal`` and carries the rise beside the bearing;
    flipping the axis word instead would delete the bearing from the record.

    0 is the honest default rather than an unstated fact: a pose nobody raised
    IS at mark height, which is why the field needs no ``None``.
    """
    record = _pose_record(vertical_deg=vertical_deg)

    assert record["vertical_deg"] == vertical_deg
    assert record["position_deg"] == -22
    assert record["position_axis"] == spatial.POSITION_AXIS_HORIZONTAL
    assert _pose_record()["vertical_deg"] == 0


def _sweep_program(*segments):
    """A program that declares only the sweep bands under test."""
    return SimpleNamespace(segments=list(segments))


def _sweep_segment(kind: str, role: str | None, f1_hz: float, f2_hz: float):
    return SimpleNamespace(kind=kind, role=role, f1_hz=f1_hz, f2_hz=f2_hz)


def _response(role: str, freqs, tf):
    import numpy as np

    from jasper.audio_measurement.program_analysis import DriverResponse

    return DriverResponse(
        role=role, freqs_hz=np.asarray(freqs, dtype=float),
        magnitude_db=np.zeros(len(freqs)),
        complex_tf=np.asarray(tf, dtype=complex),
        gating={}, snr=None, validity_floor_hz=None,
    )


def _analysis_of_shape(shape: str):
    """One analysis of each shape ``program_analysis`` produces, plus its program.

    Returns the analysis, the program that drove it, and the responses by role
    so a caller can compare a record against the values it came from.
    """
    import numpy as np

    freqs = np.geomspace(20.0, 20000.0, 512)
    tf = (0.3 + np.linspace(0.0, 2.0, 512)) * np.exp(
        1j * np.linspace(-9.0, 9.0, 512)
    )
    if shape == "per_driver":
        bands = {"woofer": (100.0, 6000.0), "tweeter": (2000.0, 20000.0)}
        sources = {
            role: _response(role, freqs, tf * (n + 1))
            for n, role in enumerate(bands)
        }
        analysis = SimpleNamespace(
            driver_responses=tuple(sources.values()), summed_response=None,
        )
        segments = [
            _sweep_segment(program.KIND_SWEEP, role, lo, hi)
            for role, (lo, hi) in bands.items()
        ]
    else:
        bands = {"summed": (30.0, 20000.0)}
        sources = {"summed": _response("summed", freqs, tf)}
        analysis = SimpleNamespace(
            driver_responses=(), summed_response=sources["summed"],
        )
        segments = [
            _sweep_segment(program.KIND_SUMMED_SWEEP, None, 30.0, 20000.0),
        ]
    return analysis, _sweep_program(*segments), sources, bands


@pytest.mark.parametrize(
    "shape,expected_roles",
    [("per_driver", ["woofer", "tweeter"]), ("summed", ["summed"])],
)
def test_every_shape_of_analysis_banks_its_complex_response(
    shape, expected_roles,
):
    """Ruling S3, at the seam three banked kinds reach it through.

    ``program_analysis`` produces complex responses in exactly two shapes — a
    per-driver tuple and one summed response — and before this only the walk's
    per-driver one ever reached a file. Both must come back reconstructible:
    the record is magnitude+phase, and the pair IS the transfer function.

    Compared at the bins the record itself NAMES, because the curve is sampled
    and never interpolated — an interpolated complex value is a number no
    microphone produced, and a phase interpolated across a wrap is wrong.
    """
    import numpy as np

    analysis, prog, sources, bands = _analysis_of_shape(shape)

    records = spatial.analysis_curve_records(analysis, prog)

    assert [record["role"] for record in records] == expected_roles
    for record in records:
        source = sources[record["role"]]
        rebuilt = 10.0 ** (np.asarray(record["magnitude_db"]) / 20.0) * np.exp(
            1j * np.radians(np.asarray(record["phase_deg"]))
        )
        at = {float(hz): i for i, hz in enumerate(source.freqs_hz)}
        sampled = [at[hz] for hz in record["freqs_hz"]]
        assert np.allclose(rebuilt, np.asarray(source.complex_tf)[sampled])
        assert np.any(np.abs(np.asarray(record["phase_deg"])) > 1.0)
        # The band the PROGRAM declared for that role, not a guessed one.
        assert record["band_hz"] == list(bands[record["role"]])


def test_an_analysis_that_measured_no_response_banks_an_empty_list():
    """CHECK, honestly. It solves gains off pilots and computes no transfer
    function at all, so there is nothing to bank and the record says so rather
    than claiming a clean curve.

    The same answer for a response whose band the program never declared: an
    unbanked curve, not one banked on a guessed band.
    """
    import numpy as np

    check = SimpleNamespace(driver_responses=(), summed_response=None)
    unbanded = SimpleNamespace(
        driver_responses=(
            _response("woofer", np.array([1000.0]), np.array([1.0 + 0.0j])),
        ),
        summed_response=None,
    )
    program_with_bands = _sweep_program(
        _sweep_segment(program.KIND_SWEEP, "tweeter", 300.0, 20000.0),
    )

    assert spatial.analysis_curve_records(check, program_with_bands) == []
    assert spatial.analysis_curve_records(unbanded, program_with_bands) == []


_A_BANKED_CURVE = {
    "role": "summed", "band_hz": [30.0, 20000.0], "freqs_hz": [100.0, 200.0],
    "magnitude_db": [-1.0, 0.5], "phase_deg": [12.5, -170.25],
}


@pytest.mark.parametrize("builder", ["cloud", "entry", "phase"])
def test_the_carry_adds_curves_and_changes_nothing_else(builder):
    """Additive at the builder: one key, and every other field untouched.

    The three kinds that gained the carry spell the SAME key the walk pose
    already did, so the reader the cutover's flip row builds parses one shape
    rather than four. Driven THROUGH the ``curves=`` keyword — a test that
    wrote the key onto a finished dict would pass with the carry reverted.
    """
    build = {
        "cloud": _cloud_record, "entry": _entry_record, "phase": _phase_record,
    }[builder]
    without, carrying = build(), build(curves=[_A_BANKED_CURVE])

    assert carrying["curves"] == [_A_BANKED_CURVE]
    assert without["curves"] == []
    assert {k: v for k, v in without.items() if k != "curves"} == {
        k: v for k, v in carrying.items() if k != "curves"
    }


@pytest.mark.parametrize(
    "builder", [_cloud_record, _pose_record, _entry_record, _phase_record],
)
def test_an_unmatched_take_states_no_level_match_trims_at_all(builder):
    """Additive at the builder, on ``vertical_deg``'s terms: the numbers key
    is ABSENT on a take that declared no level match, so a record banked before
    this existed and one banked by an unmatched take are the same shape. An
    empty mapping would be a third state a reader has to interpret.
    """
    without = builder()
    carrying = builder(claim=spatial.TakeClaim(
        level_matched=True, level_match_trims_db={"tweeter": -9.5},
    ))

    assert without["level_matched"] is False
    assert "level_match_trims_db" not in without
    assert carrying["level_match_trims_db"] == {"tweeter": -9.5}
    assert {
        k: v for k, v in without.items() if k != "level_matched"
    } == {
        k: v for k, v in carrying.items()
        if k not in ("level_matched", "level_match_trims_db")
    }


@pytest.mark.parametrize(
    "analysis_phase, composed, protection_emitted, stamped",
    [
        (program.PROGRAM_PHASE_MEASURE, True, True, "crossover_composed"),
        (program.PROGRAM_PHASE_MEASURE, False, True, "protection_retained"),
        # Nothing was emitted for the curve to retain.
        (program.PROGRAM_PHASE_MEASURE, False, False, None),
        # The analyzer that composes never ran, so the flag is a default.
        (program.PROGRAM_PHASE_VERIFY, False, True, None),
    ],
)
def test_a_take_states_which_phase_composition_its_curves_carry(
    analysis_phase, composed, protection_emitted, stamped,
):
    """docs/tuning-methodology.md §4 step 1's question, answered by the record.

    A MEASURE analysis either divided the emitted protection out and multiplied
    the configured crossover in or it did not, and the phase COMMANDED does not
    say which — a lateral walk runs the same analyzer without the priors that
    compose. So the fact is read off the analysis and stamped, and the delay
    proposal echoes it instead of the operator stating it by hand.

    Three-valued on purpose, and ABSENT on ``level_match_trims_db``'s terms for
    the third: a capture whose analyzer never composes and a box that emitted
    no protection would both otherwise read as ``protection_retained``, which
    is a contamination claim that is untrue of either.
    """
    record = _phase_record(
        phase=analysis_phase,
        claim=spatial.TakeClaim(
            phase_composition=spatial.phase_composition(
                SimpleNamespace(
                    phase=analysis_phase, configured_path_composed=composed,
                ),
                protection_emitted=protection_emitted,
            ),
        ),
    )

    assert record.get("phase_composition") == stamped
    assert ("phase_composition" in record) is (stamped is not None)


def test_a_record_banked_before_curves_existed_still_reads_clean(tmp_path):
    """Additive at the reader: the field's ABSENCE changes nothing it sees.

    A take banked before ``curves`` existed carries no such key. The reader
    narrows to its own field list, so a legacy record and a carrying one read
    identically — the whole claim behind widening the schema instead of
    versioning it.

    Asserted against a record with the key REMOVED, not one built empty: an
    empty list is what a new take with nothing to bank writes, and it is a
    different shape from the one already on disk.

    The entry baseline is the subject because it is the one kind that BOTH
    gained the carry and has a shipped reader; ``read_lateral_take`` narrows
    through the same comprehension and the pose record is unchanged here. A
    cloud seat and an unprompted-phase take are rejected on ``phase`` by both
    readers, so asserting over them would be two ``None``s agreeing.
    """
    import json

    from jasper.active_speaker.crossover_v2 import position_cycle

    carrying = {
        **_entry_record(curves=[_A_BANKED_CURVE]), "kind": POSITION_EVIDENCE_KIND,
    }
    legacy = {k: v for k, v in carrying.items() if k != "curves"}
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text(json.dumps(legacy))
    new.write_text(json.dumps(carrying))

    assert position_cycle.read_entry_baseline_take(new) is not None
    assert position_cycle.read_entry_baseline_take(
        old
    ) == position_cycle.read_entry_baseline_take(new)


def test_the_storage_seam_names_the_take_the_record_names():
    """One index convention, minted ONCE — by the builder, read by the seam.

    The seam names the bundle path and the builder names the record inside it.
    While the two spelled the convention separately a change to one silently
    made the path and its contents disagree about which take it was; worse, for
    the entry baseline (whose ``position_id`` IS a take id already) the second
    mint appended a second ``_aNN`` every time. The seam re-mints nothing now:
    the record carries ``take_id`` and the store names the artifact from it.
    """
    import asyncio

    from jasper.web import correction_crossover_v2 as v2host

    minted: list[str] = []

    class _Store:
        bundle_dir = "/no-such-bundle"
        session_id = "sess"

        def publish_json_artifact(self, path, payload):
            minted.append(path)
            return SimpleNamespace(fingerprint="fp")

        def identify_artifact(self, path):
            return SimpleNamespace(fingerprint="fp")

    seam = v2host.bind_position_retention(_Store(), "capture", {}, asyncio.run)
    take_id = spatial.take_id_for("cloud_measure_03", 7)
    seam(
        SimpleNamespace(wav=None),
        {"attempt": 7, "take_id": take_id, "measure_kind": ""},
    )

    assert minted == [f"crossover_v2/capture/positions/{take_id}.json"]
    assert take_id == "cloud_measure_03_a07"


def test_an_entry_baseline_take_id_carries_index_and_attempt():
    """The same rule on the phase that is NOT a group member.

    The entry baseline rides the same retention seam and lands in the same
    ``position_artifacts`` namespace, so it needs the same collision-free id —
    but nothing in the group bookkeeping would give it one.

    Mutation-selected: dropping the attempt suffix left 21 tests green.
    """
    record = _entry_record(index=9, attempt=2)

    assert record["take_id"] == "entry_baseline_09_a02"
    assert record["position_id"] == record["take_id"]


def test_the_three_comparability_facts_ride_the_entry_record():
    """WHAT was played, WHERE from, and THROUGH WHICH graph.

    A before→after claim is only as good as those three matching on both sides,
    and they are the whole reason this is a separate builder rather than a
    keyword on the position one.
    """
    record = _entry_record(program_id="prog-42", graph_fingerprint="fp-entry")

    assert record["program_id"] == "prog-42"
    assert record["reference_mark"] == REFERENCE_MARK_DESIGN_AXIS
    assert record["graph_fingerprint"] == "fp-entry"


def test_the_entry_records_curve_is_the_durable_copy_of_the_before():
    """The arrays ride the write-once take, not only the rewritten state file.

    Fragment ``02``'s duplication #2: the flow state file's ``verify_priors``
    is rebuilt from the conductor on every persist, so before this the round's
    "before" stopped existing the moment the next round persisted. The names
    are ``EntryBaseline.from_dict``'s so one reader covers both.
    """
    record = _entry_record(
        freqs_hz=(200.0, 400.0), magnitude_db=(-1.5, 0.5), excluded=(True, False),
    )

    assert record["freqs_hz"] == [200.0, 400.0]
    assert record["magnitude_db"] == [-1.5, 0.5]
    assert record["excluded"] == [True, False]


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
    floor = spatial.MIN_RESOLVED_CLOUD_POSITIONS

    assert spatial.group_position_floor(PHASE_LATERAL) == 0
    for phase in (PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY):
        assert spatial.group_position_floor(phase) == floor


def test_every_screen_kind_has_a_household_sentence():
    """The completeness check the mapping exists to make possible.

    The kind sets are declared so the flow's mapping can be verified rather
    than trusted — the same discipline ``coordinator.REFUSAL_KINDS`` keeps. A
    kind added without an arm ships wearing another kind's copy, and this is
    what stops that from being discovered by a household.

    Since #2291 Phase 5a-vii there are TWO owners: ``spatial.SCREEN_KINDS``
    (the walked phases) and ``capture_dispatch.ANCHOR_SCREEN_KINDS`` (CHECK,
    MEASURE, VERIFY).  The registry is checked against their union, which
    :data:`~jasper.active_speaker.crossover_v2.capture_dispatch.CAPTURE_SCREEN_KINDS`
    is.  Equality, not containment, so the guard bites in BOTH directions: a
    kind that ships without a sentence fails, and so does a registry arm for a
    kind no ladder can return — a stale arm is how a mapping outlives the rung
    it was written for.
    """
    assert capture_dispatch.CAPTURE_SCREEN_KINDS == (
        spatial.SCREEN_KINDS | capture_dispatch.ANCHOR_SCREEN_KINDS
    )
    # The two owners partition the vocabulary; an overlap would mean one kind
    # with two declarations, which is the drift a union quietly hides.
    assert not (spatial.SCREEN_KINDS & capture_dispatch.ANCHOR_SCREEN_KINDS)
    assert set(refusal_copy.SCREEN_KIND_REASONS) == set(capture_dispatch.CAPTURE_SCREEN_KINDS)
    for code in refusal_copy.SCREEN_KIND_REASONS.values():
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

    Two functions here have a journal line in the shipped flow — the boost
    derivation and the cloud combine — and both return those fields as data
    instead (:class:`~.spatial.BoostExclusion`,
    :class:`~.spatial.CloudCombine`), leaving the flow to emit them under the
    event names it owns.
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


# --------------------------------------------------------------------------- #
# the cloud combine, and the line it hands back instead of writing
#
# The corpus acceptance tests
# (``tests/test_crossover_v2_cloud_geometry_corpus.py``) exercise the same two
# functions against real S0 captures, and SKIP wherever those captures are not
# on disk. These two run everywhere, so the seam is never unpinned.
# --------------------------------------------------------------------------- #


def _unusable_position() -> spatial._CloudPosition:
    """A retained position the combiner cannot read.

    ``response`` is what ``cloud_position_capture`` reaches into for the grid,
    so ``None`` reaches the combiner's own refusal rather than faking one.
    """
    return spatial._CloudPosition(
        position_id="cloud_01",
        index=1,
        attempt=1,
        prompt="",
        wide=False,
        captured_at=0.0,
        response=None,
        sample_rate_hz=48000,
    )


def test_an_empty_group_names_its_own_degraded_path_and_journals_nothing():
    answer = spatial.cloud_geometry_verdict([])

    assert answer.verdict == {
        "locked": False, "reason": "no_positions", "n_positions": 0,
    }
    assert answer.diagnostics is None
    assert spatial.combine_cloud_positions([]).combined is None


def test_a_combiner_failure_returns_its_journal_fields_instead_of_writing_them():
    """A group's captures are already-accepted evidence, so a combiner failure
    degrades to an honest "unknown" — and still says what went wrong, as data
    the flow emits under its own event name."""
    positions = [_unusable_position()]

    combine = spatial.combine_cloud_positions(positions)
    answer = spatial.cloud_geometry_verdict(positions)

    assert combine.combined is None
    assert set(combine.diagnostics) == {"positions", "error"}
    assert combine.diagnostics["positions"] == 1
    assert combine.diagnostics["error"]
    assert answer.verdict["locked"] is False
    assert answer.verdict["reason"] == "combine_failed"
    assert answer.diagnostics == combine.diagnostics
