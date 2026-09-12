# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the conductor decides about a VERIFY capture, and how it says so.

**Re-housed, not rewritten.** Every pin here came verbatim out of
``tests/test_crossover_v2_conductor.py``, where the verify region sat as five
consecutive sections inside a 12,800-line suite. Ruling S1 (ADR-0228) renames
what they cover — VERIFY is ``measure`` with
:data:`~jasper.active_speaker.crossover_v2.contracts.MEASURE_KIND_VERIFY` plus
``analyze`` — and the strangler moves the region's production half after this.
These pins are what that move has to keep true, so they now live in a file named
for the question they ask rather than for the god object that currently answers
it.

Five sections, in the order the evidence has to survive, each with the incident
that produced it:

* **#1873 — a verify-fail that REPEATS is a finding, not a transient.** Two
  answers 0.16 dB apart, from an instrument whose measured consecutive-pair
  repeat floor is 0.052 dB median / 0.085 dB p95, are the SAME answer twice; the
  phone offered "Try again" anyway and the capture session's TTL expired mid-loop.
* **#1971 — the VERIFY capture-integrity gate.** Nothing on the VERIFY path
  checked whether the capture the tracking verdict grades was intact, because
  ``glitch_detected`` came from a splice filter over ``KIND_SWEEP`` while
  VERIFY's sweep is ``KIND_SUMMED_SWEEP``.
* **#1974 — the outcome and the verdict that produced it**, including the gate
  record that banks the two numbers its own sentence narrates.
* **PR-5 — the retired per-capture flatness relay.** One VERIFY capture graded
  on its own grid was a SECOND construction of "is the speaker flat". What stays
  pinned is the boundary that made removing it safe: the verdict never consulted
  flatness.
* **G3 — verify inter-attempt pilot consistency**, and #1924/#1927's copy rule:
  the microphone is named as a cause only by the code that holds the evidence
  for it.

**What stayed behind, and why.** Fragment ``15``'s instrument, re-run at HEAD,
counts **71 in-lane functions** in the conductor suite. **37 of them are here**,
inside these five sections, together with **8 more** that sit in the same
sections and answer the same question. The other 34 are scattered through
sections whose subject is something else — the model-error store, the diag
logger, the delta probe, the claims block — and lifting a function out of the
section whose prose explains it would cost more than the split buys. They move
when their own section does.

The harness is ``tests/crossover_v2_fixtures.py``, unchanged: this module builds
the same real conductor over the same fake seams the suite it came from does.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import time

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_LOCATE_FAILED,
    REASON_REGISTRY,
    TRANSIENT_AUTO_RETRY_CODES,
)
from jasper.active_speaker.crossover_v2_flow import (
    SWEEP_LOCATE_CONFIDENCE_FLOOR,
    VERIFY_PILOT_TRANSFER_STEP_CEILING_DB,
    CrossoverV2Session,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.program_analysis import (
    INTEGRITY_CHECK_CLIPPED_RUN,
    INTEGRITY_CHECK_FRAME_LEDGER,
    INTEGRITY_CHECK_RENDER_GAP,
    INTEGRITY_CHECK_REPEAT_EPSILON,
    INTEGRITY_CHECK_SWEEP_HEARD,
    INTEGRITY_CHECK_SWEEP_SCHEDULE,
    _verify_capture_integrity,
)

from tests._log_events import event_fields
from tests.crossover_v2_fixtures import (
    _DIAG_LOGGER,
    FakeSeams,
    _gate_block,
    _loc,
    _rearm_conductor,
    _run_phase,
    _spliced_verify,
    _stage2_after_measure,
    _verify_analysis,
    _verify_to_apply,
)


def test_verify_evidence_carried_on_tolerance_verdict_reset_on_early_return():
    """Item 5b (#1605): the conductor carries the verify_fail expert-disclosure
    numbers on a verdict that reaches the tracking comparison, and resets them
    to None on an early-return verdict (gate comparability) so no stale numbers
    leak into a later attempt's disclosure."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    _run_phase(c, 3, 3)
    assert c.verify_outcome == "fail"
    evidence = c.verify_evidence
    assert evidence is not None
    assert evidence["max_db"] == 2.4
    assert evidence["rms_db"] == 0.4
    assert evidence["tolerance_db"] == 1.5

    # An early-return verdict (gate-comparability inconclusive) never reaches
    # the tracking numbers ⇒ evidence resets to None, no stale leak.
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    _run_phase(c, 3, 4)
    assert c.verify_outcome == "inconclusive"
    assert c.verify_evidence is None


# --- #1971: the VERIFY capture-integrity gate ------------------------------------
#
# Before this, nothing on the VERIFY path checked whether the capture the
# tracking verdict grades was intact: ``glitch_detected`` came from
# ``_estimate_drift`` (MEASURE-only), and the two flow gates that DO catch a
# splice filter ``KIND_SWEEP`` while VERIFY's sweep is ``KIND_SUMMED_SWEEP``.


def test_verify_splice_refuses_before_the_tracking_grade():
    """A glitched capture must not produce a pass/fail TRACKING verdict. The
    tracking max here is wildly out of tolerance, and the answer is still the
    capture-glitch code — because a spliced recording is not evidence about
    the speaker either way."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _spliced_verify(program, max_db=9.9)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"
    # §5.2's capture-glitch convention: a transient, silently auto-retried
    # code, not a new user-facing one and not a verify_fail decision screen.
    assert verdict["auto_retry"] is True
    assert verdict["code"] in TRANSIENT_AUTO_RETRY_CODES
    # The evidence rides the verdict, so the host event carries WHY.
    record = verdict["capture_integrity"]
    assert record["glitched"] is True
    assert [c["name"] for c in record["checks"] if c["status"] == "fail"] == [
        INTEGRITY_CHECK_SWEEP_SCHEDULE
    ]
    # And the not-evaluated register travels with it — a reader of this
    # payload can tell which checks were never asked.
    assert INTEGRITY_CHECK_REPEAT_EPSILON in [
        c["name"] for c in record["checks"] if c["status"] == "not_evaluated"
    ]
    # No tracking verdict was drawn from it.
    assert c.verify_evidence is None


def test_verify_unheard_sweep_routes_to_locate_failed_not_a_glitch():
    """#1838's D3 rule on the VERIFY path: "too quiet" and "spliced" produce
    the same symptom and must not share a verdict. This confidence clears
    ``_stimulus_locate_ok``'s 0.1 floor, so before #1971 it reached the
    tracking grade untouched."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=SWEEP_LOCATE_CONFIDENCE_FLOOR - 0.05,
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == REASON_LOCATE_FAILED
    # NOT the silent-auto-retry glitch code: re-running the same measurement
    # at the same level cannot fix a capture nobody could hear.
    assert verdict["code"] not in TRANSIENT_AUTO_RETRY_CODES
    record = verdict["capture_integrity"]
    failed = [c["name"] for c in record["checks"] if c["status"] == "fail"]
    assert failed == [INTEGRITY_CHECK_SWEEP_HEARD]
    # The residual the mislocated sweep shows is NOT reported as a splice.
    not_evaluated = [
        c["name"] for c in record["checks"] if c["status"] == "not_evaluated"
    ]
    assert INTEGRITY_CHECK_SWEEP_SCHEDULE in not_evaluated


def test_verify_clip_refuses_as_a_capture_glitch():
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    clipped = _loc("sweep_verify", "summed_sweep", clipped=True)
    fakes.verify = lambda program: _verify_analysis(
        program,
        integrity=_verify_capture_integrity(
            program, program.sample_rate_hz, (clipped,),
            # No capture here, so no browser report to reconcile: the
            # frame-accounting checks (#2094) stay not-evaluated and this test
            # keeps asking only about the clip.
            reconcile_capture_frames(None, received_frames=0),
        ),
    )
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"
    record = verdict["capture_integrity"]
    assert [c["name"] for c in record["checks"] if c["status"] == "fail"] == [
        INTEGRITY_CHECK_CLIPPED_RUN
    ]
    assert record["clipped_segments"] == ["sweep_verify"]


def test_verify_clean_integrity_still_passes_end_to_end():
    """The era-exact clean path: the SAME fixture every other verify test
    uses, now carrying a production-derived integrity record, still accepts."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert c.verify_outcome == "pass"


def test_verify_without_an_integrity_record_is_not_refused_but_says_so(caplog):
    """``None`` is "no evidence" — the same convention ``linearity_ok`` and
    ``pilot_snr_ok`` use two lines up, where only an explicit failure refuses.
    A pre-#1971 analysis shape must not become an un-passable capture at
    VERIFY's OWN capture gate; the live analyze seam always populates the
    record (pinned in tests/test_crossover_v2_program_pilots.py). It is not a
    SILENT pass either: the journal says ``unavailable``, its own value, so a
    missing record can never be read as a clean one.

    **#2537 update.** That is still true of the per-capture ``verify_diag``
    line asserted below (still ``accepted=true``, still names
    ``integrity=unavailable``) — VERIFY's own gate has not changed. What HAS
    changed is what the ROUND does with a capture it could not grade: an
    integrity-absent capture is unusable, so :func:`evaluate_evidence_trust`
    reads it as untrusted evidence, and #2537's table restores-or-recovers on
    untrusted evidence rather than silently keeping an unmeasured state (the
    pre-#2537 table's ``user_decision`` cell, which
    ``AdoptionOutcome.KEEP_FOR_ITERATION``'s own docstring calls out as a
    screen nobody rendered — treated exactly like ``keep``). This fixture's
    bare conductor binds no rollback anchor, so the round escalates loudly to
    ``recovery_required`` rather than promising a restore it cannot perform —
    the overall verdict is now a refusal, and it is REFUSED FOR ITS OWN NAMED
    REASON rather than silently."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _verify_analysis(program, integrity=None)
    verdict = _run_phase(c, 3, 3)
    # The ROUND refuses — untrusted evidence with no rollback anchor bound.
    assert verdict["accepted"] is True
    # …but VERIFY's OWN capture gate still accepted this capture, and the
    # journal still says so rather than folding it into the round's refusal.
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["accepted"] == "true"
    assert fields["integrity"] == "unavailable"
    assert fields["integrity_locate_confidence_min"] == "null"


def test_verify_integrity_gate_runs_ahead_of_the_linearity_branch():
    """A spliced capture's pilot-derived linearity verdict is drawn from a
    corrupted timeline, so it must not be the reported cause — the same order
    ``_measure_verdict`` puts its glitch branch in."""
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    fakes.verify = lambda program: _spliced_verify(program, linearity=False)
    verdict = _run_phase(c, 3, 3)
    assert verdict["code"] == "drift_baselines_disagree"


def test_verify_diag_discloses_integrity_on_pass_and_on_refusal(caplog):
    """Disclosed on EVERY verify: on a pass it is what makes "this capture was
    comparable" measured rather than assumed, and on a refusal it names which
    check fired — which is how telemetry tells this gate's ``locate_failed``
    from ``_stimulus_locate_ok``'s."""
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes = FakeSeams()
    c = _verify_to_apply(fakes)
    _run_phase(c, 3, 3)
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["integrity"] == "ok"
    # MEMBERSHIP, not position: #2094 put two frame-accounting checks ahead of
    # the repeat-only ones, and the fact under test is that every unevaluated
    # check is disclosed by name — never which name comes first.
    not_evaluated = fields["integrity_not_evaluated"].split(",")
    assert INTEGRITY_CHECK_REPEAT_EPSILON in not_evaluated
    assert INTEGRITY_CHECK_RENDER_GAP in not_evaluated
    assert INTEGRITY_CHECK_FRAME_LEDGER in not_evaluated
    assert fields["integrity_locate_confidence_min"] == "0.9"
    assert fields["integrity_residual_ms_worst"] == "0.0"

    caplog.clear()
    fakes.verify = _spliced_verify
    _run_phase(c, 3, 4)
    fields = event_fields(caplog, "correction.crossover_v2_verify_diag")
    assert fields["integrity"] == INTEGRITY_CHECK_SWEEP_SCHEDULE
    assert fields["integrity_residual_ms_worst"] == "15.0"


# --- #1974: the outcome and the verdict that produced it -------------------------


def test_the_verify_outcome_always_carries_the_code_that_produced_it():
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    cases = [
        (lambda program: _verify_analysis(program, max_db=2.4),
         "fail", "verify_out_of_tolerance"),
        (lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0),
         "inconclusive", "verify_inconclusive"),
        (_verify_analysis, "pass", None),
    ]
    for index, (factory, outcome, code) in enumerate(cases, start=3):
        fakes.verify = factory
        verdict = _run_phase(c, 3, index)
        assert c.verify_outcome == outcome, code
        assert c.verify_code == code, code
        assert (verdict.get("code") or None) == (code if outcome == "inconclusive" else None)
        assert verdict["accepted"] is (outcome != "inconclusive")


def test_an_inconclusive_capture_reads_its_gate_record_once(monkeypatch):
    """The gate record is derived once per consume, then read, never rebuilt."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    calls = []
    real = flow._declared_first_bounce_s
    monkeypatch.setattr(
        flow,
        "_declared_first_bounce_s",
        lambda distance_m: (calls.append(distance_m), real(distance_m))[1],
    )
    fakes.verify = lambda program: _verify_analysis(program, max_db=0.5, gate_ms=5.0)
    verdict = _run_phase(c, 3, 3)

    assert verdict["code"] == "verify_inconclusive"
    assert len(calls) == 1
    assert c.verify_gate is not None
    assert verdict["reflection_measured"] == c.verify_gate["reflection_measured"]


def test_a_level_shift_records_its_own_code_not_the_gates():
    """The second road to "inconclusive". Before #1974 both roads produced the
    same household sentence, which blamed a room reflection — on a verdict
    where no reflection and no window are involved at all."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    _run_phase(c, 3, 3)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.56, max_db=0.5,
    )
    verdict = _run_phase(c, 3, 4)
    assert verdict["code"] == "verify_level_shift"
    assert c.verify_outcome == "inconclusive"
    assert c.verify_code == "verify_level_shift"


def test_the_verify_gate_record_is_gate_disclosures_own_sentence():
    """Issue #1966's disclosure, at the seam where it enters the wizard.

    The conductor composes it ONCE, by calling ``describe_gate`` — it does not
    assemble a sentence of its own from the same fields, which is the failure
    mode the whole contract exists to prevent. So the assertion is equality
    against that function's output, not a substring match that a lookalike
    would also satisfy.
    """
    from jasper.audio_measurement import gate_disclosure

    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.5, gate_ms=5.0, floor_source=gating.FLOOR_SEARCH_BOUND,
    )
    _run_phase(c, 3, 3)
    record = c.verify_gate
    assert record is not None
    assert record["disclosure"] == gate_disclosure.describe_gate(
        {"applied": True, "window_ms": 5.0,
         "floor_source": gating.FLOOR_SEARCH_BOUND}
    )
    # The one fact the household copy branches on, and the state the whole
    # 2026-07-30 corpus was actually in.
    assert record["reflection_measured"] is False
    assert "no reflection found" in record["disclosure"]


@pytest.mark.parametrize(
    ("block_kwargs", "moved_rms_db", "reflection_delay_ms", "reflection_measured"),
    [
        pytest.param({}, 2.59, pytest.approx(5.33), True, id="both-numbers-banked"),
        # Null, never 0.0: nothing was found, so there is nothing to time, and a
        # 0.0 would say the reflection arrived with the direct sound. The delta
        # survives — ``SMALL_DELTA_RMS_DB``'s two readings: a capped gate still
        # moved the spectrum, which means "nothing was proven about
        # reflections" rather than "clean".
        pytest.param(
            {"first_reflection_ms": None, "floor_source": gating.FLOOR_SEARCH_BOUND},
            2.59, None, False, id="ceiling-capped-banks-no-delay",
        ),
        # ``pre_post_gate_delta`` is ``None`` when no band could price the gate
        # (an ungateable capture, or a program that declared no radiated band —
        # the over-report ``evaluation_band_hz`` refuses to make). A 0.0 here
        # would claim the gate changed nothing, which is a measurement.
        pytest.param({"rms_db": None}, None, pytest.approx(5.33), True,
                     id="unpriceable-banks-no-movement"),
    ],
)
def test_the_gate_record_banks_each_number_its_sentence_narrates_or_a_null(
    block_kwargs, moved_rms_db, reflection_delay_ms, reflection_measured,
):
    """Ticket 1.5. The sentence was the only copy of both numbers, and prose is
    not a number: the evidence packet's ``not_evaluated`` block said in so many
    words that the reflection time "is narrated inside verify.gate.disclosure
    prose and is not banked as a number anywhere in a round's artifacts".

    Equality against ``build_gate_disclosure``'s own derivations, not a
    recomputation — same discipline as the sentence's own test above. A record
    that assembled these from the raw block would be a second derivation of a
    fact that has one owner, and the digits in the prose and the digits in the
    fields could then disagree.

    The delay is a DELAY, not the absolute time the gating block spells
    ``first_reflection_ms`` — whose origin is the deconvolution window's, and
    which ``GateDisclosure.reflection_delay_ms`` calls meaningless to a reader
    on its own. 15.73 - 10.40, never 15.73.
    """
    from jasper.active_speaker.crossover_v2_flow import _gate_record
    from jasper.audio_measurement import gate_disclosure as gd
    from tests.crossover_v2_fixtures import _driver_response_diag

    block = _gate_block(**block_kwargs)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    typed = gd.build_gate_disclosure(block)
    record = _gate_record(response)

    assert record is not None
    assert record["moved_rms_db"] == typed.delta_rms_db == moved_rms_db
    assert record["reflection_delay_ms"] == typed.reflection_delay_ms
    assert record["reflection_delay_ms"] == reflection_delay_ms
    # The screen's two facts, beside the numbers.
    assert record["reflection_measured"] is reflection_measured
    assert record["disclosure"] == gd.describe_gate(block)


def _declared(tmp_path, **over):
    """One declared rig, written where only this test can see it (#3502).

    Never :data:`~jasper.audio_measurement.measurement_geometry.DEFAULT_PATH`:
    the production file is the operator's and a test must not read or write it.
    """
    from jasper.audio_measurement.measurement_geometry import DeclaredGeometry

    path = tmp_path / "measurement_geometry.json"
    DeclaredGeometry(**{
        "speaker_height_m": 0.84, "mic_height_m": 0.5, "distance_m": 1.0, **over,
    }).save(path)
    return path


def test_a_gate_record_carries_the_declared_room_floor_and_says_it_is_declared(
    tmp_path,
):
    """#3502 — the whole point of declaring a rig: the floor stops being unknown.

    The 2026-07-30 rig class never fires the measured reflection finder, so
    without a declaration every capture publishes ``unknown`` forever. With one,
    the same capture publishes a floor AND the word that says the operator's
    tape measure produced it — never a word that would let it read as measured.
    """
    from jasper.active_speaker.crossover_v2_flow import _gate_record
    from jasper.audio_measurement import gating
    from jasper.audio_measurement.measurement_geometry import declared_first_bounce_s
    from tests.crossover_v2_fixtures import _driver_response_diag

    block = _gate_block(floor_source=gating.FLOOR_SEARCH_BOUND)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    bounce_s = declared_first_bounce_s(1.0, path=_declared(tmp_path))

    declared = _gate_record(response, declared_first_bounce_s=bounce_s)
    undeclared = _gate_record(response)

    assert declared is not None and undeclared is not None
    assert declared["entanglement_floor_hz"] == pytest.approx(
        gating.f_entanglement_floor_hz(bounce_s)
    )
    assert declared["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_DECLARED
    assert undeclared["entanglement_floor_hz"] is None
    assert undeclared["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_UNKNOWN
    # Declaring a rig changes the floor and NOTHING else about the record.
    assert {k: v for k, v in declared.items() if "entanglement" not in k} == {
        k: v for k, v in undeclared.items()
        if "entanglement" not in k and k != "disclosure"
    } | {"disclosure": declared["disclosure"]}


def test_two_seats_of_one_rig_publish_different_floors_for_their_distances(tmp_path):
    """The floor is evaluated per CAPTURE, not once per rig.

    Same declared heights, two capture distances: the nearer seat's bounce
    arrives LATER relative to its direct sound, so its floor is lower. One
    number on both rows would hide that the seats are not equally trustworthy
    down low.
    """
    from jasper.active_speaker.crossover_v2_flow import _gate_record
    from jasper.audio_measurement import gating
    from jasper.audio_measurement.measurement_geometry import declared_first_bounce_s
    from tests.crossover_v2_fixtures import _driver_response_diag

    path = _declared(tmp_path)
    block = _gate_block(floor_source=gating.FLOOR_SEARCH_BOUND)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)

    near = _gate_record(
        response, declared_first_bounce_s=declared_first_bounce_s(0.3, path=path)
    )
    far = _gate_record(
        response, declared_first_bounce_s=declared_first_bounce_s(1.0, path=path)
    )

    assert near is not None and far is not None
    assert near["entanglement_floor_hz"] < far["entanglement_floor_hz"]
    assert (
        near["entanglement_floor_source"]
        == far["entanglement_floor_source"]
        == gating.ENTANGLEMENT_SOURCE_DECLARED
    )


def test_a_hand_edited_geometry_file_reads_as_unknown_instead_of_ending_the_round(
    tmp_path, monkeypatch
):
    """#3502 — a malformed declaration costs a floor, never the round.

    The reader raises on a file that exists and does not parse, deliberately,
    so ``jasper-declare-geometry show`` can report it. On the capture path the
    same exception would abort every VERIFY attempt and every seat of the round
    over a fact that clamps nothing, so the flow reads it as undeclared.
    """
    from jasper.active_speaker import crossover_v2_flow as flow
    from jasper.audio_measurement import gating
    from tests.crossover_v2_fixtures import _driver_response_diag

    path = tmp_path / "measurement_geometry.json"
    path.write_text('{"speaker_height_m": 0.84}', encoding="utf-8")
    monkeypatch.setattr(flow, "DECLARED_GEOMETRY_PATH", path)

    block = _gate_block(floor_source=gating.FLOOR_SEARCH_BOUND)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)

    assert flow._declared_first_bounce_s(1.0) is None
    record = flow._gate_record(
        response, declared_first_bounce_s=flow._declared_first_bounce_s(1.0)
    )
    assert record is not None
    assert record["entanglement_floor_hz"] is None
    assert record["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_UNKNOWN


@pytest.mark.parametrize(
    ("floor_source", "verify_kwargs", "accepted", "reflection_measured", "prose"),
    [
        # The one epistemic state where "reflections were removed" is a true
        # thing to say (``GateDisclosure.gated_anything``).
        pytest.param(gating.FLOOR_MEASURED, {"max_db": 0.5, "gate_ms": 5.0},
                     False, True, "reflection measured", id="measured-reflection"),
        pytest.param(gating.FLOOR_SEARCH_BOUND, {}, True, False, None,
                     id="passing-verify"),
    ],
)
def test_the_verify_gate_is_recorded_whatever_the_outcome(
    floor_source, verify_kwargs, accepted, reflection_measured, prose,
):
    """On EVERY outcome, like the graded band and the frame beside it: a pass
    is exactly when nobody would otherwise ask how much of the response the
    comparison could see."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(
        program, floor_source=floor_source, **verify_kwargs,
    )
    verdict = _run_phase(c, 3, 3)

    assert verdict["accepted"] is accepted
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is reflection_measured
    if prose is not None:
        assert prose in c.verify_gate["disclosure"]


def test_an_early_return_retry_cannot_repair_the_gate_onto_a_stale_verdict():
    """The desync the PR #1994 adversarial gate found, pinned as a property.

    The outcome, the code, and the gate are written by ONE call, so an attempt
    that early-returns (``locate_failed`` / ``pilot_level_collapse`` /
    ``agc_behavioral_fail`` — none of which reach ``_set_verify_outcome``)
    leaves all three of the previous attempt's facts standing TOGETHER.

    Before the fix the gate alone was recomputed at the top of every
    ``_verify_verdict`` call, so this exact sequence — an inconclusive whose
    window was capped at the search ceiling, then a locate failure whose
    capture DID find a reflection — paired attempt 1's verdict with attempt 2's
    gate. The done screen then said "a reflection reached the microphone
    sooner…" about a verdict whose own capture had found none: issue #1974
    re-created one layer down. The symmetric understatement (measured, then a
    ceiling-capped early return) is the same bug in the other direction.
    """
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    # Attempt 1 concludes: gate-comparability inconclusive, window capped.
    fakes.verify = lambda program: _verify_analysis(
        program, max_db=0.5, gate_ms=5.0, floor_source=gating.FLOOR_SEARCH_BOUND,
    )
    assert _run_phase(c, 3, 3)["code"] == "verify_inconclusive"
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is False

    # Attempt 2 never concludes — but its capture found a reflection.
    fakes.verify = lambda program: _verify_analysis(
        program, locate_confidence=0.0, floor_source=gating.FLOOR_MEASURED,
    )
    assert _run_phase(c, 3, 4)["code"] == REASON_LOCATE_FAILED

    # The triple is still attempt 1's, entire.
    assert c.verify_outcome == "inconclusive"
    assert c.verify_code == "verify_inconclusive"
    assert c.verify_gate is not None
    assert c.verify_gate["reflection_measured"] is False
    assert "no reflection found" in c.verify_gate["disclosure"]



def test_an_ungated_capture_records_no_gate_at_all():
    """Absent stays absent (the #1987 rule): a response carrying no gating
    block yields no record, so no screen can print a gate that never ran."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    def ungated(program):
        analysis = _verify_analysis(program)
        return dataclasses.replace(
            analysis,
            summed_response=dataclasses.replace(analysis.summed_response, gating={}),
        )

    fakes.verify = ungated
    _run_phase(c, 3, 3)
    assert c.verify_gate is None


# --- PR-5: the retired per-capture flatness relay --------------------------------
#
# The flat-linearization plan's PR-5 removed ``ProgramAnalysis.flatness_tracking``
# and the conductor's ``flatness_evidence`` stash: one VERIFY capture graded on
# its own grid against its own band mean was a SECOND construction of "is the
# speaker flat", disagreeing with the spatial cloud's spec evaluation by however
# much a single mic position differs from the cloud. The claim now has exactly
# one owner (``assemble_cloud_group_result``'s ``flatness`` key). What stays
# pinned here is the boundary that made the removal safe: the VERIFY verdict
# never consulted flatness, so removing it changed no accept/code.


def test_verify_payload_carries_tracking_only_no_flatness_claim():
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = _verify_analysis
    verdict = _run_phase(c, 3, 3)
    assert verdict["accepted"] is True
    assert "flatness_tracking" not in verdict
    assert verdict["tracking"]["max_db_notch_excluded"] is not None

    fakes.verify = lambda program: _verify_analysis(program, max_db=2.4)
    verdict = _run_phase(c, 3, 4)
    assert verdict["accepted"] is True
    assert c.verify_code == "verify_out_of_tolerance"
    assert "flatness_tracking" not in verdict


def test_conductor_exposes_no_per_capture_flatness_evidence():
    """The stash and its property are gone, not repointed — a household-facing
    flatness number must come from the cloud group's spec verdict
    (``group_cloud_result``), never from a per-VERIFY-attempt stash that a
    verify-only re-arm would silently re-derive from one position."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = _verify_analysis
    assert _run_phase(c, 3, 3)["accepted"] is True
    assert not hasattr(c, "flatness_evidence")


# --- measurement-honesty gate G3: verify inter-attempt pilot consistency --------


# ``reference`` is the pilot state of the attempt that ESTABLISHES the G3
# comparator, or ``None`` for a row with no preceding attempt; ``attempt`` is the
# pilot state of the attempt under test. Both are ``_verify_analysis`` kwargs.
_G3_CEILING_DB = VERIFY_PILOT_TRANSFER_STEP_CEILING_DB
_G3_STEPS = [
    # No reference yet: the FIRST usable attempt establishes one and never
    # rejects on its own.
    pytest.param(None, {"pilot_hi_dbfs": -20.0}, True, None, "pass",
                 id="first-usable-attempt-establishes-and-passes"),
    # A legacy VERIFY program with no leading pilot pair (the default fixture,
    # ``pilot_hi_dbfs=None`` => ``pilots=()``) never reaches the gate at all —
    # mirrors the other two gates' own skip conditions.
    pytest.param(None, {}, True, None, "pass", id="no-pilots-skips-the-gate"),
    # The 2026-07-22 hardware evidence: a phone's input chain stepped ~0.56 dB
    # between attempts and kept producing dishonest verify verdicts.
    pytest.param({"pilot_hi_dbfs": -20.0},
                 {"pilot_hi_dbfs": -20.0 + 0.56, "max_db": 0.5},
                 False, "verify_level_shift", "inconclusive",
                 id="step-0.56dB-fires"),
    pytest.param({"pilot_hi_dbfs": -20.0},
                 {"pilot_hi_dbfs": -20.0 + 0.1, "max_db": 0.5},
                 True, None, "pass", id="step-0.1dB-inside-the-ceiling-passes"),
    # The boundary, exclusive (``>``, not ``>=``), matching this file's other
    # comparators. ``programmed_hi_gain_db=0.0`` (not the -20.0 the rows above
    # use) so the transfer IS the pilot level with no subtraction involved: a
    # -20.0 baseline computes ``(0.0 - (-20.0)) - (0.35 - (-20.0))``, which
    # picks up a ~1e-15 float rounding artifact that would make an exactly-at-
    # the-boundary row flaky.
    pytest.param({"pilot_hi_dbfs": 0.0, "programmed_hi_gain_db": 0.0},
                 {"pilot_hi_dbfs": _G3_CEILING_DB, "programmed_hi_gain_db": 0.0,
                  "max_db": 0.5},
                 True, None, "pass", id="step-exactly-at-the-ceiling-passes"),
    pytest.param({"pilot_hi_dbfs": 0.0, "programmed_hi_gain_db": 0.0},
                 {"pilot_hi_dbfs": _G3_CEILING_DB + 0.01, "programmed_hi_gain_db": 0.0,
                  "max_db": 0.5},
                 False, "verify_level_shift", "inconclusive",
                 id="step-0.01dB-above-the-ceiling-fires"),
]


@pytest.mark.parametrize(
    ("reference", "attempt", "accepted", "code", "outcome"), _G3_STEPS
)
def test_the_g3_pilot_transfer_gate_fires_only_above_its_ceiling(
    reference, attempt, accepted, code, outcome,
):
    """Measurement-honesty gate G3 (2026-07-22): an attempt whose own pilot
    transfer stepped away from the reference cannot honestly be graded, however
    clean its tracking looks."""
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    index = 3
    if reference is not None:
        # Independently out of tolerance so a retry is admitted at all —
        # scaffolding, unrelated to G3.
        fakes.verify = lambda program: _verify_analysis(
            program, max_db=5.0, **reference
        )
        assert _run_phase(c, 3, index)["accepted"] is True
        assert c.verify_code == "verify_out_of_tolerance"
        index += 1

    fakes.verify = lambda program: _verify_analysis(program, **attempt)
    verdict = _run_phase(c, 3, index)

    assert verdict["accepted"] is accepted
    assert (verdict.get("code") or None) == code
    assert c.verify_outcome == outcome


def test_verify_pilot_level_shift_baseline_does_not_rebaseline():
    fakes = FakeSeams()
    c = _stage2_after_measure(fakes)

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    verdict1 = _run_phase(c, 3, 3)
    assert verdict1["accepted"] is True
    assert c.verify_code == "verify_out_of_tolerance"

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-19.7, max_db=8.0,
    )
    verdict2 = _run_phase(c, 3, 4)
    assert verdict2["accepted"] is True
    assert c.verify_code == "verify_out_of_tolerance"

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-19.4, max_db=11.0,
    )
    verdict3 = _run_phase(c, 3, 5)
    assert verdict3["accepted"] is False
    assert verdict3["code"] == "verify_level_shift"


def test_verify_pilot_reference_is_session_scoped_not_inherited():
    """#1927: a prior session's reference is HISTORY, never the comparator.

    This is the 2026-07-30 bench shape (#1870 finding 1): the day-later verify
    reads 0.775 dB away from yesterday's reference — over 2× the ceiling, and
    deterministic. Before the ruling that rehydrated baseline refused the
    attempt outright; now the attempt establishes its OWN reference and is
    graded on its merits."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": -20.0}, "at": time.time() - 86400.0,
        },
    )
    # Nothing to compare against yet — the prior did not become a baseline.
    assert c.verify_pilot_transfer_reference is None
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    verdict = _run_phase(c, 1, 1)
    assert verdict["accepted"] is True
    reference = c.verify_pilot_transfer_reference
    assert reference["values"]["summed"] == pytest.approx(0.775)
    assert reference["at"] > 0.0


@pytest.mark.parametrize(
    ("dated", "step_db", "disclosed"),
    [
        pytest.param(True, 0.775, True, id="step-0.775dB-past-the-ceiling"),
        # A prior the session's own chain agrees with is not news: the ceiling
        # that defines "the chain moved" (0.35 dB) is the one that defines
        # "worth saying". 0.30 dB sits comfortably inside it rather than exactly
        # on it — this row is about agreement, and the boundary itself is pinned
        # (with the float care it needs) by
        # ``test_the_g3_pilot_transfer_gate_fires_only_above_its_ceiling``.
        pytest.param(True, 0.30, False, id="step-0.30dB-inside-the-ceiling"),
        # An undated record cannot be shown as history (#1942's rule), so it is
        # not carried as one — the constructor drops it rather than inventing a
        # date, and it never reaches the comparator either way.
        pytest.param(False, 0.775, False, id="undated-prior"),
    ],
)
def test_the_level_reference_reset_is_disclosed_only_when_a_dated_prior_moved(
    dated, step_db, disclosed,
):
    """The reset is reported (dated, with the step) — never enforced."""
    fakes = FakeSeams()
    prior_at = time.time() - 86400.0
    prior: dict = {"values": {"summed": 0.0}}
    if dated:
        prior["at"] = prior_at
    c = _rearm_conductor(fakes, verify_pilot_transfer_prior=prior)
    assert c.verify_level_reference_reset is None

    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + step_db, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True

    reset = c.verify_level_reference_reset
    if not disclosed:
        assert reset is None
        return
    assert reset["prior_at"] == prior_at
    assert reset["step_db"] == pytest.approx(step_db)


def test_verify_level_reference_reset_is_journalled(caplog):
    """The bench's grep target. ``pilot_transfer_step_db`` in the verify diag
    is the WITHIN-session step; this is the cross-session one it can no longer
    be, and #1870-style corpus sweeps want to count resets without parsing
    every diag line. INFO — a reset is ordinary, not a fault."""
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": 0.0}, "at": time.time() - 86400.0,
        },
    )
    caplog.set_level(logging.INFO, logger=_DIAG_LOGGER)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=0.5,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    fields = event_fields(caplog, "correction.crossover_v2_level_reference_reset")
    assert fields["step_db"] == "0.775"
    assert fields["ceiling_db"] == "0.35"
    assert "prior_age_s" in fields


def test_verify_level_shift_still_fires_within_one_session():
    """The gate keeps its stated purpose: a chain that moves DURING a sitting
    still refuses to grade. Attempt 1 fails independently (out of tolerance),
    attempt 2 moves 0.775 dB from it — the round-5 number, now measured
    against a reference this session set itself."""
    fakes = FakeSeams()
    c = _rearm_conductor(fakes)
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0, max_db=5.0,
    )
    assert _run_phase(c, 1, 1)["accepted"] is True
    assert c.verify_code == "verify_out_of_tolerance"
    fakes.verify = lambda program: _verify_analysis(
        program, pilot_hi_dbfs=-20.0 + 0.775, max_db=5.0,
    )
    verdict = _run_phase(c, 1, 2)
    assert verdict["accepted"] is False
    assert verdict["code"] == "verify_level_shift"


def test_no_constructor_argument_can_seed_the_g3_comparator():
    """The #1927 negative, engineered rather than checked: there is no longer
    an argument through which a previous session's numbers can become this
    session's baseline. The prior travels ONLY as dated history."""
    params = inspect.signature(CrossoverV2Session.__init__).parameters
    assert "verify_pilot_transfer_baseline" not in params
    assert "verify_pilot_transfer_prior" in params
    fakes = FakeSeams()
    c = _rearm_conductor(
        fakes,
        verify_pilot_transfer_prior={
            "values": {"summed": -20.0}, "at": time.time() - 86400.0,
        },
    )
    assert c._verify_pilot_baseline is None


def test_verify_level_shift_copy_is_true_on_both_surfaces():
    """#1924's routing half. One string renders on the measurement page's
    in-session retry (which re-compares the same reference and CAN repeat)
    and on the wizard's fresh-session retry (which since #1927 settles it in
    one capture). So it must command neither and discredit neither: state the
    fact, contextualize the retry, name the escalation conditionally."""
    message = REASON_REGISTRY["verify_level_shift"].message
    assert message == (
        "The microphone's levels changed between measurements, so this check "
        "couldn't settle. Try again — if it repeats, re-measure."
    )
    # The retired routing: it commanded the retry the phone cannot win.
    assert "re-verify" not in message.lower()
    # The visible primary is named, not undermined — the sibling
    # ``verify_out_of_tolerance`` names its primary too.
    assert "Try again" in message
    # …and the escalation is conditional on the retry repeating, never
    # presented as the only way forward.
    assert "if it repeats, re-measure" in message
