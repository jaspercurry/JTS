# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The anchor phases' capture ladders (#2291 Phase 5a-vii).

What these pin is what the extraction ASSERTS and nothing else checked: the two
orderings bought with live incidents, the laziness of the two ports, the
directive a MEASURE rung carries back, and the one deliberate difference
between VERIFY's integrity ladder and the entry baseline's.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import capture_dispatch as cd
from jasper.active_speaker.crossover_v2 import spatial


# --------------------------------------------------------------------------- #
# helpers — a facts object whose every field is stated, then one field moved
# --------------------------------------------------------------------------- #


def _check(**overrides) -> cd.CheckScreens:
    base = dict(
        stimulus_located=True,
        anchor_ambiguous=False,
        channel_map_ok=True,
        pilot_snr_ok=True,
        linearity_ok=True,
        gain_plan_present=True,
        gain_plan_snr_floor_ok=True,
    )
    base.update(overrides)
    return cd.CheckScreens(**base)


class _Counter:
    """A port that records how many times it was asked."""

    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.answer


def _measure(**overrides) -> tuple[cd.MeasureScreens, _Counter, _Counter]:
    schedule = overrides.pop("_schedule", _Counter(True))
    plausible = overrides.pop("_plausible", _Counter(True))
    base = dict(
        stimulus_located=True,
        pilot_snr_ok=True,
        sweep_locate_confidence_ok=True,
        glitch_detected=False,
        any_sweep_clipped=False,
        linearity_ok=True,
        alignment_present=True,
        alignment_status_ok=True,
        alignment_confidence_ok=True,
    )
    base.update(overrides)
    return (
        cd.MeasureScreens(
            sweep_schedule_ok=schedule, delay_physically_plausible=plausible, **base
        ),
        schedule,
        plausible,
    )


# --------------------------------------------------------------------------- #
# CHECK
# --------------------------------------------------------------------------- #


def test_check_accepts_a_clean_capture():
    assert cd.check_screens(_check()) is None


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"stimulus_located": False}, cd.SCREEN_LOCATE_FAILED),
        ({"anchor_ambiguous": True}, cd.SCREEN_ANCHOR_AMBIGUOUS),
        ({"channel_map_ok": False}, cd.SCREEN_CHANNEL_MAP_MISMATCH),
        ({"pilot_snr_ok": False}, cd.SCREEN_SNR_FLOOR),
        ({"gain_plan_present": False}, cd.SCREEN_SNR_FLOOR),
        ({"gain_plan_snr_floor_ok": False}, cd.SCREEN_SNR_FLOOR),
    ],
)
def test_check_rungs_report_their_own_finding(override, expected):
    assert cd.check_screens(_check(**override)) == expected


def test_check_never_refuses_on_an_unestablished_fact():
    """``None`` is no evidence, not a failure — the convention the analysis uses.

    A capture whose channel map or linearity could not be judged must not be
    refused for it; only an explicit ``False`` is a finding.
    """
    assert cd.check_screens(_check(channel_map_ok=None, linearity_ok=None)) is None


def test_a_bent_curve_blames_the_room_when_the_gain_solve_already_said_so():
    """W6.12 — CHECK is the one phase that can tell the room from the phone.

    Its gain solve has a band-resolved ambient verdict against THIS capture in
    hand, so a linearity failure alongside a floor failure is the room's; with
    the floor clear it is the recording chain's AGC.
    """
    assert cd.check_screens(
        _check(linearity_ok=False, gain_plan_snr_floor_ok=False)
    ) == cd.SCREEN_NOISY_ROOM_LINEARITY
    assert cd.check_screens(
        _check(linearity_ok=False, gain_plan_snr_floor_ok=True)
    ) == cd.SCREEN_LINEARITY_FAILED


def test_a_lost_ambient_window_blames_the_room_for_a_wiring_fault():
    """A KNOWN DEFECT, pinned so it cannot drift out of sight (issue #2052).

    These are the exact screens a real miswire produces when its ambient
    window is lost — a silent non-anchor driver plus a late recording. The
    analysis half is measured and pinned by
    `test_channel_map_fallback_never_passes_a_driver_that_never_played`
    (channel map UNKNOWN, linearity False); this is what the ladder then
    answers.

    ``noisy_room_linearity`` is the wrong remedy: it tells the household the
    room bent the curve, when nothing here measured the room at all.
    ``gain_plan_snr_floor_ok`` is ``False`` because
    `program_analysis._snr_floor_ok` collapses "the ambient report is empty"
    into the same bool as "the room's worst band is over the bound", so the
    rung above reads no-evidence as a room verdict.

    The refusal itself is correct and safety holds — this capture must not
    commission a speaker, and it does not. What is wrong is only which lever
    the household is pointed at. Fixing it needs a rung (or a rung condition)
    that can tell the two apart, which is a change to THIS ladder; #2052
    shipped the analysis half and left this recorded rather than guessed at.
    When that fix lands, this test is the one that must change.
    """
    assert cd.check_screens(
        _check(channel_map_ok=None, linearity_ok=False, gain_plan_snr_floor_ok=False)
    ) == cd.SCREEN_NOISY_ROOM_LINEARITY


def test_the_room_arm_needs_a_plan_to_have_said_it():
    """No gain plan means no ambient verdict, so the room cannot be blamed."""
    assert cd.check_screens(
        _check(linearity_ok=False, gain_plan_present=False, gain_plan_snr_floor_ok=False)
    ) == cd.SCREEN_LINEARITY_FAILED


def test_the_pilot_is_asked_before_linearity():
    """Issue #1838's ordering, stated as the two-fact discriminator.

    Below the floor the ambient-subtracted delta is not evidence either way, so
    a capture failing BOTH must report the room and the level — never the
    phone's microphone.  Swapping the two rungs turns this green→red.
    """
    assert cd.check_screens(
        _check(pilot_snr_ok=False, linearity_ok=False)
    ) == cd.SCREEN_SNR_FLOOR


def test_the_stimulus_is_asked_before_everything():
    assert cd.check_screens(
        _check(stimulus_located=False, anchor_ambiguous=True,
               channel_map_ok=False, pilot_snr_ok=False)
    ) == cd.SCREEN_LOCATE_FAILED


def test_an_unattributed_anchor_is_asked_before_the_wiring_verdict():
    """Issue #2644's ordering, stated as the two-fact discriminator.

    A capture the analyzer could not pin reads every per-driver window one
    pilot spacing from where that driver played, so ``channel_map_ok=False``
    beside it is a statement about WHERE the analyzer looked, not about how the
    speaker is wired.  On 2026-08-16 exactly this pair sent a household to
    check the wiring of a speaker that had passed the identical program two
    hours earlier.  Swapping the two rungs turns this green→red.
    """
    assert cd.check_screens(
        _check(anchor_ambiguous=True, channel_map_ok=False)
    ) == cd.SCREEN_ANCHOR_AMBIGUOUS


def test_an_unattributed_anchor_is_retriable_and_never_a_wiring_instruction():
    """The point of the rung, at the copy layer rather than the ladder layer.

    ``channel_map_mismatch`` is a HARD STOP with a zero retry budget whose
    sentence tells the household to open its speaker.  The kind this rung
    returns must be neither of those things — otherwise the ladder change is
    cosmetic.
    """
    from jasper.active_speaker.crossover_v2 import vocabulary as vocab

    code = vocab.SCREEN_KIND_REASONS[cd.SCREEN_ANCHOR_AMBIGUOUS]
    spec = vocab.REASON_REGISTRY[code]
    assert spec.retry_budget > 0, "an un-attributed capture is fixed by retaking it"
    assert code not in vocab.NON_RETRIABLE_CODES
    assert spec.template != vocab.TEMPLATE_HARD_STOP
    sentence = f"{spec.message} {spec.banner}".lower()
    for forbidden in ("wiring", "wire", "rewire", "speaker wiring"):
        assert forbidden not in sentence, (
            f"anchor-ambiguous copy must not mention {forbidden!r}: {sentence!r}"
        )


# --------------------------------------------------------------------------- #
# MEASURE
# --------------------------------------------------------------------------- #


def test_measure_accepts_a_clean_capture():
    screens, schedule, plausible = _measure()
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) is None
    assert (schedule.calls, plausible.calls) == (1, 1)


def test_too_quiet_is_reported_before_glitched():
    """D3 (issue #1838), the rung order a live session bought.

    Low SNR CAUSES the glitch signal.  With the glitch rung first, a session
    playing 33 dB below flat was told its capture had glitched and silently
    re-armed the same unwinnable level until it timed out.  Both facts are true
    here; the honest one is the level.
    """
    screens, _, _ = _measure(pilot_snr_ok=False, glitch_detected=True)
    screen = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert screen is not None
    assert screen.kind == cd.SCREEN_PILOT_LEVEL_COLLAPSE


def test_neither_level_rung_rearms():
    """Re-running an inaudible measurement at the same level cannot succeed."""
    for override in ({"stimulus_located": False}, {"pilot_snr_ok": False}):
        screens, _, _ = _measure(**override)
        screen = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
        assert screen is not None and screen.rearm is False


def test_the_three_transient_rungs_rearm_and_only_the_clipped_one_backs_off():
    screens, _, _ = _measure(glitch_detected=True)
    glitch = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert glitch == cd.MeasureScreen(cd.SCREEN_CAPTURE_GLITCH, rearm=True)

    screens, _, _ = _measure(_schedule=_Counter(False))
    schedule = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert schedule == cd.MeasureScreen(
        cd.SCREEN_CAPTURE_GLITCH, guard="sweep_schedule", rearm=True
    )

    screens, _, _ = _measure(any_sweep_clipped=True)
    clipped = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert clipped == cd.MeasureScreen(
        cd.SCREEN_CLIPPED, rearm=True, rearm_backoff_db=3.0
    )


def test_the_backoff_is_the_callers_number_not_a_constant_here():
    """Stated, never reached for — so a policy change lands in one place."""
    screens, _, _ = _measure(any_sweep_clipped=True)
    screen = cd.measure_screens(screens, clip_retry_backoff_db=7.5)
    assert screen is not None and screen.rearm_backoff_db == 7.5


def test_the_two_shared_code_rungs_are_told_apart_by_guard():
    """§5.2 reuses one household code; ``guard=`` is telemetry's only handle."""
    screens, _, _ = _measure(sweep_locate_confidence_ok=False)
    locate = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert locate == cd.MeasureScreen(
        cd.SCREEN_LOCATE_FAILED, guard="sweep_locate_confidence"
    )
    screens, _, _ = _measure(stimulus_located=False)
    plain = cd.measure_screens(screens, clip_retry_backoff_db=3.0)
    assert plain is not None and plain.guard == ""


def test_the_schedule_port_is_not_asked_when_a_rung_above_refuses():
    """The laziness that makes it a port rather than a value.

    ``sweep_schedule_ok`` reaches ``program_for_phase``, which RAISES when
    MEASURE has no composed program.  Resolving it eagerly would move that
    failure above the three rungs the shipped ladder answers first.
    """
    for override in (
        {"stimulus_located": False},
        {"pilot_snr_ok": False},
        {"sweep_locate_confidence_ok": False},
        {"glitch_detected": True},
    ):
        screens, schedule, _ = _measure(**override)
        cd.measure_screens(screens, clip_retry_backoff_db=3.0)
        assert schedule.calls == 0, override


def test_the_plausibility_port_is_asked_only_of_a_resolved_trusted_estimate():
    """Fix 3's backstop is the last alignment rung, and only that.

    A trims-only capture has no estimate; an unresolved or untrusted one is
    already refused.  Asking anyway would call the preset-bound check on
    alignments the shipped ladder never handed it.
    """
    for override in (
        {"alignment_present": False},
        {"alignment_status_ok": False},
        {"alignment_confidence_ok": False},
    ):
        screens, _, plausible = _measure(**override)
        cd.measure_screens(screens, clip_retry_backoff_db=3.0)
        assert plausible.calls == 0, override


def test_a_trims_only_capture_skips_all_three_alignment_rungs():
    screens, _, _ = _measure(
        alignment_present=False, alignment_status_ok=False, alignment_confidence_ok=False
    )
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) is None


def test_the_three_alignment_rungs_report_their_own_finding():
    screens, _, _ = _measure(alignment_status_ok=False)
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) == cd.MeasureScreen(
        cd.SCREEN_ALIGNMENT_UNRESOLVED
    )
    screens, _, _ = _measure(alignment_confidence_ok=False)
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) == cd.MeasureScreen(
        cd.SCREEN_LOW_ALIGNMENT_CONFIDENCE
    )
    screens, _, _ = _measure(_plausible=_Counter(False))
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) == cd.MeasureScreen(
        cd.SCREEN_LOW_ALIGNMENT_CONFIDENCE
    )


def test_an_unresolved_alignment_is_reported_before_an_untrusted_one():
    """Two findings, two household actions; the resolve verdict comes first."""
    screens, _, _ = _measure(alignment_status_ok=False, alignment_confidence_ok=False)
    assert cd.measure_screens(screens, clip_retry_backoff_db=3.0) == cd.MeasureScreen(
        cd.SCREEN_ALIGNMENT_UNRESOLVED
    )


# --------------------------------------------------------------------------- #
# the ripple disclosure — a rung that accepts
# --------------------------------------------------------------------------- #


def test_the_ripple_reservation_is_due_only_above_the_threshold():
    assert cd.ripple_reservation_due(
        predicted_ripple_db=15.1, has_alignment=True, disclosure_threshold_db=15.0
    )
    assert not cd.ripple_reservation_due(
        predicted_ripple_db=15.0, has_alignment=True, disclosure_threshold_db=15.0
    )


def test_a_trims_only_path_owes_no_reservation():
    """The alignment half of the converted gate's skip (#2087)."""
    assert not cd.ripple_reservation_due(
        predicted_ripple_db=99.0, has_alignment=False, disclosure_threshold_db=15.0
    )


# --------------------------------------------------------------------------- #
# VERIFY
# --------------------------------------------------------------------------- #


class _Integrity:
    def __init__(self, failed) -> None:
        self.failed = failed

    def to_dict(self) -> dict:
        return {"failed": list(self.failed)}


class _Analysis:
    def __init__(self, *, pilot_snr_ok=True, linearity_ok=True, capture_integrity=None):
        self.pilot_snr_ok = pilot_snr_ok
        self.linearity_ok = linearity_ok
        self.capture_integrity = capture_integrity


def test_verify_accepts_a_clean_capture_and_goes_on_grading():
    assert (
        cd.verify_integrity_screens(_Analysis(), stimulus_located=True) is None
    )


def test_verify_reports_the_level_rungs_before_the_record():
    assert cd.verify_integrity_screens(
        _Analysis(pilot_snr_ok=False, capture_integrity=_Integrity(["x"])),
        stimulus_located=True,
    ) == cd.VerifyIntegrityScreen(cd.SCREEN_PILOT_LEVEL_COLLAPSE)
    assert cd.verify_integrity_screens(
        _Analysis(pilot_snr_ok=False), stimulus_located=False
    ) == cd.VerifyIntegrityScreen(cd.SCREEN_LOCATE_FAILED)


def test_one_integrity_record_produces_two_kinds():
    """#1838's D3: a sweep nobody heard and a spliced timeline differ.

    The first is a level/mic problem re-running cannot fix; the second is the
    transient capture-glitch class, which auto-retries.
    """
    heard = _Integrity([flow.INTEGRITY_CHECK_SWEEP_HEARD])
    screen = cd.verify_integrity_screens(
        _Analysis(capture_integrity=heard), stimulus_located=True
    )
    assert screen is not None
    assert screen.kind == cd.SCREEN_LOCATE_FAILED
    assert screen.integrity_payload == {"capture_integrity": {"failed": [
        flow.INTEGRITY_CHECK_SWEEP_HEARD
    ]}}

    spliced = _Integrity(["timeline_spliced"])
    screen = cd.verify_integrity_screens(
        _Analysis(capture_integrity=spliced), stimulus_located=True
    )
    assert screen is not None
    assert screen.kind == cd.SCREEN_CAPTURE_GLITCH
    assert screen.integrity_payload == {
        "capture_integrity": {"failed": ["timeline_spliced"]}
    }


def test_only_the_integrity_arm_carries_a_payload():
    screen = cd.verify_integrity_screens(
        _Analysis(linearity_ok=False), stimulus_located=True
    )
    assert screen == cd.VerifyIntegrityScreen(cd.SCREEN_LINEARITY_FAILED)
    assert screen.integrity_payload is None


def test_an_absent_record_continues_here_and_refuses_at_the_entry_baseline():
    """The ONE deliberate difference between the two sibling ladders.

    ``None`` is the pre-#1971 shape and means no evidence, so VERIFY carries on
    and its diagnostic prints ``integrity=unavailable``.  The entry baseline
    exists ONLY to be compared, so a before-side nobody graded fails closed
    rather than seeding a before→after claim it cannot support.  If these two
    ever answer the same way, one of the two documented contracts has moved.
    """
    assert cd.verify_integrity_screens(
        _Analysis(capture_integrity=None), stimulus_located=True
    ) is None
    baseline = spatial.entry_baseline_screens(
        _Analysis(capture_integrity=None), stimulus_located=True, reference_mark="m",
    )
    assert baseline.kind == cd.SCREEN_CAPTURE_GLITCH


def test_a_passing_record_is_not_a_refusal():
    assert cd.verify_integrity_screens(
        _Analysis(capture_integrity=_Integrity([])), stimulus_located=True
    ) is None


# --------------------------------------------------------------------------- #
# the vocabulary boundary this module exists behind
# --------------------------------------------------------------------------- #


def test_no_household_vocabulary_reaches_this_module():
    """A kind is all that leaves; the sentence is the flow's.

    Enumerated from the AST rather than argued, because "the module does not
    import the registry" is exactly the property a future convenience import
    would quietly remove — and a substring scan would be fooled by the prose
    above, which names all three symbols on purpose.

    The package-wide no-flow/no-web guard lives in ``test_crossover_v2_journey``
    and walks every module; this one is narrower and names the three household
    symbols whose absence is THIS extraction's load-bearing claim.
    """
    import ast

    source = cd.__file__ or ""
    assert source
    tree = ast.parse(open(source).read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    for symbol in (
        "REASON_REGISTRY",
        "reason_message",
        "TRANSIENT_AUTO_RETRY_CODES",
        "PhaseVerdict",
    ):
        assert symbol not in imported, symbol
    assert not any("crossover_v2_flow" in name for name in imported)
    assert not any(name.startswith("jasper.web") for name in imported)
