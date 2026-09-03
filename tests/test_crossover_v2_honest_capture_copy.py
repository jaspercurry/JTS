"""``locate_failed`` says what was measured, not what it inferred (#2085).

The defect these pin, measured on JTS3 on 2026-08-03 (issue #2083, entry 6).
One sitting produced the household sentence "Couldn't hear the speaker
clearly. Check the volume and the microphone, then try again." four times.
ONCE it was true — ``pilot_level_collapse``, pilot SNR 11.27 dB, a genuinely
quiet capture. The other three were ``locate_failed`` raised by
``program_analysis.capture_integrity`` with ``failed=summed_sweep_heard``,
and every one of those captures also carried ``pilot_snr_ok=True`` with the
pilot pair 13.9-15.5 dB over the room's own floor. The speaker had been heard.
Three households' worth of that advice sends someone to turn up a volume the
measurement had already proved was fine.

``summed_sweep_heard`` is a locate-CONFIDENCE check, not a level check — see
``program_analysis._verify_capture_integrity``, whose own docstring calls it
"the summed sweep's own locate confidence". Reading it as "nobody could hear
the speaker" was the inference; the pilot is the evidence that refutes it.

**And the obvious replacement inference is false too**, which is why the copy
these pin names no cause at all. Forensics on the same three WAVs found the
audio pristine: the analyzer anchored on ``pilot_lo``, the quietest segment in
the program, missed its gate by an NCC margin of 0.005-0.049, snapped to
``pilot_hi`` (+1296.5 ms, exactly the pilot spacing, on all three), and the
+/-30 ms search window did the rest. Re-scored whole-capture the same files
give 0.67-0.82. "The recording was damaged" would have been a third lie. The
tests below therefore assert what the copy must NOT claim as much as what it
says — see ``UNSUPPORTED_CAUSE_WORDS``.

What is pinned here is a rule, not a string: the sentence follows the
evidence, claims nothing the evidence does not carry, and one failure gets ONE
account of itself across every surface that narrates it.
"""

import logging

import pytest

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_PILOT_LEVEL_COLLAPSE,
    REASON_REGISTRY,
    REASON_CAPTURE_TIMEOUT,
    locate_failed_diagnosis,
    locate_failed_message,
    reason_message,
)
from jasper.active_speaker.crossover_v2_flow import (
    LOCATE_MIN_CONFIDENCE,
    MAX_EXTRA_ATTEMPTS_PER_POSITION,
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
)
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_status as v2status
from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginRefused
from tests.test_crossover_envelope_v2 import _status
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _check_analysis,
    _conductor,
    _run_phase,
    _verify_analysis,
    _verify_to_apply,
)

# The remedy words the misattribution actually sent households chasing. A
# capture whose own pilot pair was heard must not carry any of them: "check
# the volume" is the specific wrong action, and "too quiet" / "louder" are the
# same claim in other words.
VOLUME_REMEDY_WORDS = ("volume", "louder", "too quiet", "closer")

# The OTHER cause this copy is not entitled to name. Both stories that would
# explain a locate miss were false on the three real captures — the level was
# fine and so was the audio — so the sentence reports the failed operation and
# stops. This guard is what keeps a future "helpful" rewrite from reaching for
# the nearest plausible cause again, which is the whole bug class.
UNSUPPORTED_CAUSE_WORDS = ("damag", "corrupt", "glitch", "broken", "lost")


def _heard_but_unlocatable(fakes):
    """The measured defect: pilot heard, summed sweep scored under its floor.

    Confidence sits deliberately BETWEEN the two floors — over
    ``LOCATE_MIN_CONFIDENCE`` so ``_stimulus_locate_ok`` passes and the
    integrity gate is the thing that refuses, under
    ``SWEEP_LOCATE_CONFIDENCE_FLOOR`` so ``summed_sweep_heard`` is what fails.
    That is the exact routing the JTS3 captures took.

    The fixture states the SCORE, not a reason for it — matching what the
    production gate actually has. On the real captures the low score came from
    the analyzer's anchor, not from the audio; a fixture that modelled "a bad
    recording" would quietly re-assert the cause these tests exist to keep out
    of the copy.
    """
    fakes.verify = lambda program: _verify_analysis(
        program,
        locate_confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR - 0.05,
        pilot_snr_ok=True,
    )


def _never_heard(fakes):
    """The honest half: nothing located AND the pilot never cleared the room.

    Below ``LOCATE_MIN_CONFIDENCE`` so ``_stimulus_locate_ok`` is the refusing
    gate — which on the VERIFY ladder runs BEFORE the pilot branch, so this
    still lands on ``locate_failed`` rather than being intercepted by
    ``pilot_level_collapse``. Both facts point the same way here, and the
    original copy is the right copy.
    """
    fakes.verify = lambda program: _verify_analysis(
        program,
        locate_confidence=LOCATE_MIN_CONFIDENCE - 0.05,
        pilot_snr_ok=False,
    )


def test_a_heard_speaker_that_could_not_be_lined_up_is_not_a_volume_problem():
    """The defect, end to end: same code, and no longer the same advice."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    _heard_but_unlocatable(fakes)
    verdict = _run_phase(c, 3, 3)

    assert verdict["code"] == REASON_LOCATE_FAILED
    # The refutation travelled with the verdict rather than being re-derived.
    assert verdict["pilot_heard"] is True
    # And it really was the sweep-heard integrity check that fired, not some
    # other route that happens to share the code — otherwise this test would
    # pass while describing a different failure.
    failed = [
        check["name"] for check in verdict["capture_integrity"]["checks"]
        if check["status"] == "fail"
    ]
    assert failed == ["summed_sweep_heard"]

    reason = verdict["reason"]
    for word in VOLUME_REMEDY_WORDS:
        assert word not in reason.lower(), (
            f"copy still sends a heard-speaker failure after {word!r}: {reason!r}"
        )
    # ...and does not swap one unsupported cause for another. The audio on the
    # real captures was fine; only the lining-up failed.
    for word in UNSUPPORTED_CAUSE_WORDS:
        assert word not in reason.lower(), (
            f"copy asserts an unestablished cause ({word!r}): {reason!r}"
        )
    # It says the two things that WERE measured: the speaker was heard, and
    # the tones could not be lined up.
    assert "hear the speaker" in reason
    assert "line up" in reason


def test_a_capture_nobody_heard_keeps_the_volume_copy():
    """The branch that was always honest stays exactly as it was."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    _never_heard(fakes)
    verdict = _run_phase(c, 3, 3)

    assert verdict["code"] == REASON_LOCATE_FAILED
    assert verdict["pilot_heard"] is False
    assert verdict["reason"] == (
        "Couldn't hear the speaker clearly. Check the volume and the "
        "microphone, then try again."
    )


def test_the_discriminator_is_what_chooses_the_sentence():
    """The mutation guard: flip ONLY the pilot fact, and the copy must move.

    Both scenarios below produce ``locate_failed``. If the copy were keyed on
    the reason code — the bug — these two sentences would be equal and this
    assertion is the one that fails. It is deliberately stated as an
    inequality between two live conductor runs rather than as two literals, so
    it keeps its meaning when the wording changes.
    """
    heard_fakes = FakeSeams()
    heard = _verify_to_apply(heard_fakes)
    _heard_but_unlocatable(heard_fakes)
    heard_verdict = _run_phase(heard, 3, 3)

    unheard_fakes = FakeSeams()
    unheard = _verify_to_apply(unheard_fakes)
    _never_heard(unheard_fakes)
    unheard_verdict = _run_phase(unheard, 3, 3)

    assert heard_verdict["code"] == unheard_verdict["code"] == REASON_LOCATE_FAILED
    assert heard_verdict["pilot_heard"] is not unheard_verdict["pilot_heard"]
    assert heard_verdict["reason"] != unheard_verdict["reason"]


def test_a_genuinely_quiet_capture_still_gets_its_own_code():
    """Scope check: the honest quarter of the JTS3 sitting is untouched.

    The one capture that really was too quiet routed to
    ``pilot_level_collapse``, which owns copy about the room and the level.
    This change must not have pulled that verdict onto the locate ladder.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    # Locates fine; the pilot is what fails. That ordering is what separates
    # this from `_never_heard` above.
    fakes.verify = lambda program: _verify_analysis(program, pilot_snr_ok=False)
    verdict = _run_phase(c, 3, 3)

    assert verdict["code"] == REASON_PILOT_LEVEL_COLLAPSE
    assert verdict["reason"] == REASON_REGISTRY[REASON_PILOT_LEVEL_COLLAPSE].message


def test_the_result_event_distinguishes_the_two_failures(caplog):
    """Observability: the journal separates them without opening a WAV.

    Four ``code=locate_failed`` lines were indistinguishable in the JTS3
    journal. The discriminator is now a field on the per-capture result event,
    so the three-versus-one split is a grep.
    """
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    _heard_but_unlocatable(fakes)
    with caplog.at_level(logging.INFO, logger="jasper.active_speaker.crossover_v2_flow"):
        _run_phase(c, 3, 3)

    lines = [
        rec.getMessage() for rec in caplog.records
        if "event=correction.crossover_v2_result" in rec.getMessage()
    ]
    assert lines, "no result event was emitted"
    # ``log_event`` renders booleans lowercase, so this is the literal an
    # operator would actually grep for.
    assert any(
        "code=locate_failed" in line and "pilot_heard=true" in line
        for line in lines
    ), lines


def test_the_registry_holds_the_no_evidence_rendering():
    """A reader of ``REASON_REGISTRY`` gets copy that is true, not copy that
    guesses — the same contract ``verify_inconclusive`` established (#1974).

    With no capture in hand nothing refutes "couldn't hear the speaker", so the
    registry keeps that sentence. What it must NOT hold is the heard-speaker
    one, which would assert a measurement the reader never made.
    """
    spec = REASON_REGISTRY[REASON_LOCATE_FAILED]
    assert spec.message == locate_failed_message(None)
    assert spec.message == locate_failed_message(False)
    assert spec.message != locate_failed_message(True)
    # The routing this change is not allowed to touch.
    assert spec.template == "fix_and_retry"
    assert spec.retry_budget == 1


@pytest.mark.parametrize("pilot_heard", [True, False, None])
def test_the_selector_is_the_one_voice_for_every_surface(pilot_heard):
    """One failure, one account of it.

    The relay verdict, the budget refusal, and the envelope each render this
    sentence from their own surface. They agree because they all ask
    :func:`reason_message`; a caller that went back to ``spec.message`` would
    break this.
    """
    spec = REASON_REGISTRY[REASON_LOCATE_FAILED]
    assert reason_message(
        REASON_LOCATE_FAILED, spec, pilot_heard=pilot_heard,
    ) == locate_failed_message(pilot_heard)


def test_every_capture_of_a_heard_speaker_gets_the_measured_sentence():
    """CHECK's locate gate reaches the same copy by a different route.

    Deliberately a different gate from the VERIFY-integrity scenario above:
    the rule is that the sentence follows the evidence, so a second gate
    firing on the same fact must not need its own wiring to be honest.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)

    verdict = _run_phase(c, 1, 1)
    assert verdict["code"] == REASON_LOCATE_FAILED
    assert verdict["pilot_heard"] is True
    assert "volume" not in verdict["reason"].lower()
    assert verdict["reason"] == locate_failed_message(True)


def test_budget_exhaustion_does_not_contradict_the_captures_that_caused_it():
    """The observation agrees; the action follows the attempt state.

    Retryable copy may invite another take. Once the pooled extras are spent,
    that advice would be a dead affordance: exhaustion must keep the same
    evidence-derived diagnosis, add the count and honest terminal outcome, and
    drop the unavailable action.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)

    first = _run_phase(c, 1, 1)
    for attempt in range(2, MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        final = _run_phase(c, 1, attempt)

    diagnosis = locate_failed_diagnosis(True)
    assert final["terminal"] is True
    assert final["terminal_outcome"] == "phase_cannot_proceed"
    assert final["reason"].startswith(diagnosis)
    assert "try again" not in final["reason"].lower()
    assert "cannot continue" in final["reason"].lower()
    assert "volume" not in final["reason"].lower()

    # A replayed next begin still gets the same defensive backstop, but the
    # ordinary page never needs to reach it: the final capture was terminal.
    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, MAX_EXTRA_ATTEMPTS_PER_POSITION + 2)

    assert excinfo.value.code == REASON_LOCATE_FAILED
    assert first["reason"] == f"{diagnosis} Try again."
    assert excinfo.value.user_message.startswith(diagnosis)
    assert "try again" not in excinfo.value.user_message.lower()
    assert "cannot continue" in excinfo.value.user_message.lower()
    assert "volume" not in excinfo.value.user_message.lower()


def test_exhaustion_uses_the_addressed_slots_evidence_not_the_global_failure():
    """A replayed begin can address slot B after slot A became global-last.

    The exhausted slot's pilot evidence remains its own. Using the global
    discriminator would silently change a heard-but-unlocatable capture into
    the registry's volume diagnosis when another slot failed more recently.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)
    for attempt in range(1, MAX_EXTRA_ATTEMPTS_PER_POSITION + 2):
        _run_phase(c, 1, attempt)

    # Poison only the GLOBAL persistence pair as if a different slot failed
    # later with the same code and opposite evidence. Slot B's pair stays True.
    c._last_failure_code = REASON_LOCATE_FAILED
    c._last_failure_pilot_heard = False

    with pytest.raises(CaptureBeginRefused) as excinfo:
        c.authorize_begin(1, MAX_EXTRA_ATTEMPTS_PER_POSITION + 2)

    assert excinfo.value.user_message.startswith(locate_failed_diagnosis(True))
    assert "volume" not in excinfo.value.user_message.lower()


def test_a_refusal_never_borrows_another_failures_evidence():
    """``_pilot_heard_for``'s pairing rule, mutated at the boundary.

    A refusal can name a code the capture loop never produced. Handing it the
    last capture's pilot evidence would put a confident sentence about THIS
    failure in front of a household using another one's measurement. Unknown
    pairing degrades to the registry copy, which claims nothing.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert c.last_failure_pilot_heard is True

    # A refusal for a DIFFERENT code: the evidence must not travel with it.
    refusal = c._refuse(REASON_PILOT_LEVEL_COLLAPSE)
    assert refusal.user_message == REASON_REGISTRY[REASON_PILOT_LEVEL_COLLAPSE].message
    assert c.last_failure_pilot_heard is None


@pytest.fixture
def isolated_v2_state(tmp_path):
    """Point the v2 state file at a tmp path, like the endpoints suite does."""
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)


def _persisted_envelope_verdict():
    """Build the envelope the way the page does: from the persisted state."""
    return build_crossover_envelope_v2({
        "active": True,
        "setup": {"active": True, "status": "ready"},
        "crossover_v2": v2status.crossover_v2_status_block(),
    })["verdict_text"]


def test_the_persisted_failure_reaches_the_envelope_with_its_evidence(
    isolated_v2_state,
):
    """The production link, end to end — conductor to state file to screen.

    Every other test here stops at ``to_capture_dict`` or feeds the envelope a
    hand-built dict, so between them the real write was unpinned: deleting the
    ``pilot_heard`` write from ``persist_conductor_state`` left the whole
    targeted suite green while every real session silently reverted to the
    volume copy. This is the test that fails when that write goes.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)
    capture_reason = _run_phase(c, 1, 1)["reason"]

    v2host.persist_conductor_state(c, failure_code=c.last_failure_code)

    # The screen the household lands on says what the capture said.
    assert _persisted_envelope_verdict() == capture_reason
    assert "volume" not in _persisted_envelope_verdict().lower()


def test_a_persisted_code_never_carries_another_failures_evidence(
    isolated_v2_state,
):
    """The caller-side half of the pairing rule.

    The relay-death arm persists ``capture_timeout`` over whatever the last
    capture failed on. Ungated, that wrote ``{"code": "capture_timeout",
    "pilot_heard": True}`` — one failure's code with another's evidence. Only
    ``locate_failed`` reads the key today, so no wrong sentence shipped, but
    the record is the thing a later reader trusts.
    """
    fakes = FakeSeams()
    fakes.check = lambda program: _check_analysis(
        program, locate_confidence=0.01, pilot_snr_ok=True,
    )
    c = _conductor(fakes)
    _run_phase(c, 1, 1)
    assert c.last_failure_pilot_heard is True

    v2host.persist_conductor_state(c, failure_code=REASON_CAPTURE_TIMEOUT)

    failure = v2host.load_v2_state()["failure"]
    assert failure["code"] == REASON_CAPTURE_TIMEOUT
    assert "pilot_heard" not in failure


@pytest.mark.parametrize(
    ("persisted", "expect_volume_copy"),
    [
        ({"pilot_heard": True}, False),
        ({"pilot_heard": False}, True),
        # No pilot evidence recorded — a failure that ran no capture, or a
        # state file written before this shipped. Unknown is not False.
        ({}, True),
    ],
)
def test_the_envelope_renders_the_persisted_evidence(persisted, expect_volume_copy):
    """The jts.local page reads the same fact the phone was shown.

    Before this, the envelope rendered the registry literal unconditionally —
    so a household that saw the honest sentence on the measurement page and
    then looked at the speaker's own page would have been given the other
    account of the same failure.
    """
    env = build_crossover_envelope_v2(_status(
        applied=False,
        failure={"code": REASON_LOCATE_FAILED, **persisted},
    ))
    verdict = env["verdict_text"]

    assert verdict == locate_failed_message(persisted.get("pilot_heard"))
    assert ("volume" in verdict.lower()) is expect_volume_copy
    # The nudge is the same sentence — two renderings of one verdict on one
    # screen must not disagree either.
    assert [n["text"] for n in env["nudges"]] == [verdict]
