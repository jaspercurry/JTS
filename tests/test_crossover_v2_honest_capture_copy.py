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

from jasper.web import correction_crossover_v2_state as v2state


import pytest

from jasper.active_speaker.crossover_envelope_v2 import build_crossover_envelope_v2
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    locate_failed_message,
    reason_message,
)
from jasper.active_speaker.crossover_v2.capture_dispatch import LOCATE_MIN_CONFIDENCE
from jasper.audio_measurement.program_analysis.model import SWEEP_LOCATE_CONFIDENCE_FLOOR
from tests.test_crossover_envelope_v2 import _status
from tests.crossover_v2_fixtures import (
    FakeSeams,
    _run_phase,
    _verify_analysis,
    _stage2_conductor,
)

# The remedy words the misattribution actually sent households chasing. A
# capture whose own pilot pair was heard must not carry any of them: "check
# the volume" is the specific wrong action, and "too quiet" / "louder" are the
# same claim in other words.

# The OTHER cause this copy is not entitled to name. Both stories that would
# explain a locate miss were false on the three real captures — the level was
# fine and so was the audio — so the sentence reports the failed operation and
# stops. This guard is what keeps a future "helpful" rewrite from reaching for
# the nearest plausible cause again, which is the whole bug class.


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


def test_the_discriminator_is_what_chooses_the_sentence():
    """The mutation guard: flip ONLY the pilot fact, and the copy must move.

    Both scenarios below produce ``locate_failed``. If the copy were keyed on
    the reason code — the bug — these two sentences would be equal and this
    assertion is the one that fails. It is deliberately stated as an
    inequality between two live conductor runs rather than as two literals, so
    it keeps its meaning when the wording changes.
    """
    from jasper.active_speaker.crossover_v2.refusal_copy import reason_message
    heard_fakes, unheard_fakes = FakeSeams(), FakeSeams()
    heard, unheard = _stage2_conductor(heard_fakes), _stage2_conductor(unheard_fakes)
    _heard_but_unlocatable(heard_fakes)
    _never_heard(unheard_fakes)
    heard_verdict, unheard_verdict = _run_phase(heard, 3, 3), _run_phase(unheard, 3, 3)
    heard_pilot = heard_fakes.verify(heard.program_for_phase("verify")).pilot_snr_ok
    unheard_pilot = unheard_fakes.verify(unheard.program_for_phase("verify")).pilot_snr_ok
    assert heard_verdict.fault == unheard_verdict.fault == REASON_LOCATE_FAILED
    assert heard_pilot is not unheard_pilot
    assert reason_message(REASON_LOCATE_FAILED, REASON_REGISTRY[REASON_LOCATE_FAILED], pilot_heard=heard_pilot) != reason_message(
        REASON_LOCATE_FAILED, REASON_REGISTRY[REASON_LOCATE_FAILED], pilot_heard=unheard_pilot)


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

    The capture verdict, the budget refusal, and the envelope each render this
    sentence from their own surface. They agree because they all ask
    :func:`reason_message`; a caller that went back to ``spec.message`` would
    break this.
    """
    spec = REASON_REGISTRY[REASON_LOCATE_FAILED]
    assert reason_message(
        REASON_LOCATE_FAILED, spec, pilot_heard=pilot_heard,
    ) == locate_failed_message(pilot_heard)


@pytest.fixture
def isolated_v2_state(tmp_path):
    """Point the v2 state file at a tmp path, like the endpoints suite does."""
    v2state.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2state.set_state_path_for_tests(None)


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
