# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The DISPOSE half of the delay method: the acoustic null confirm's stimulus.

The confirm exists because ``jasper-delay-sweep propose`` computes a delay
landscape and nothing ever went and measured it. What makes it a confirmation
rather than a second system response is WHICH GRAPH it plays through, so that
is what the first test here pins — it is the one property the whole leg rests
on, and the cheapest one to lose in a refactor.
"""

from __future__ import annotations

import pytest

from jasper.active_speaker.angle_capture import (
    REGIME_NULL_CONFIRM,
    REGIMES,
    AngleCaptureRequest,
    AngleStop,
    program_for_stop,
    resolve_request,
)
from jasper.active_speaker.crossover_v2.contracts import MEASURE_KIND_NULL_CONFIRM
from jasper.active_speaker.crossover_v2.journey import PHASE_NULL_CONFIRM
from jasper.active_speaker.crossover_v2.measurement_phase import phase_for_measurement
from jasper.active_speaker.crossover_v2.programs import (
    SUMMED_SWEEP_PHASES,
    NoProgramForPhaseError,
    program_for_phase,
)
from jasper.audio_measurement.program import (
    NULL_CONFIRM_SHOULDER_MARGIN,
    build_null_confirm_program,
    build_verify_program,
    null_confirm_band_hz,
)

FC_HZ = 1600.0
GAIN_DB = -30.0


def _confirm(fc_hz: float = FC_HZ):
    return build_null_confirm_program(fc_hz, gain_db=GAIN_DB)


def _verify(fc_hz: float = FC_HZ):
    return build_verify_program(fc_hz, gain_db=GAIN_DB)


def test_the_confirm_never_plays_the_production_graph():
    """THE mutation guard for this leg.

    Every :data:`SUMMED_SWEEP_PHASES` member makes the play seam RESTORE the
    measurement graph and play the standing production one, because for those
    phases the applied system is what is under test. The confirm inverts that:
    the inversion, the candidate delay and the level-match trims under test all
    live in the measurement graph, so a confirm that restored first would read
    a null off a speaker carrying none of them and report it against a
    coordinate that was never installed.

    Joining that set is the single edit that would silently turn this
    measurement into a lie, which is why it is asserted directly on the
    production frozenset rather than inferred from a captured playback.
    """
    assert PHASE_NULL_CONFIRM not in SUMMED_SWEEP_PHASES


def test_the_confirm_sweeps_both_crossover_shoulders_with_run_up():
    """The null depth interpolates at Fc/2, Fc and 2*Fc, so the sweep must
    contain all three with margin — a shoulder read off an edge bin is a
    clamped value wearing a measurement's clothes."""
    lo, hi = null_confirm_band_hz(FC_HZ)
    assert lo < FC_HZ / 2.0
    assert hi > FC_HZ * 2.0
    assert lo == pytest.approx(FC_HZ / 2.0 / NULL_CONFIRM_SHOULDER_MARGIN)


@pytest.mark.parametrize("fc_hz", [250.0, 300.0, 10_000.0, 11_000.0])
def test_a_corner_with_no_run_up_to_give_is_refused(fc_hz: float):
    """The corners where the envelope lands EXACTLY on a shoulder.

    Below ~300 Hz VERIFY's own floor is ``fc/2``, so the low edge coincides with
    the lower shoulder; at 10 kHz the 20 kHz ceiling coincides with the upper
    one. A non-strict bound accepted both and returned a depth whose reference
    shoulder was a clamped endpoint — the precise failure this guard exists to
    refuse, passing silently at the two corners least able to afford it.
    """
    with pytest.raises(ValueError, match="run-up"):
        null_confirm_band_hz(fc_hz)


@pytest.mark.parametrize("fc_hz", [301.0, 800.0, 1600.0, 2400.0, 8000.0, 9000.0])
def test_an_accepted_corner_always_has_real_run_up(fc_hz: float):
    """Anti-vacuity for the refusals above: the accepted band must clear BOTH
    shoulders strictly, or the guard is refusing the wrong corners."""
    lo, hi = null_confirm_band_hz(fc_hz)
    assert lo < fc_hz / 2.0
    assert hi > fc_hz * 2.0


@pytest.mark.parametrize("fc_hz", [301.0, 800.0, 1600.0, 2400.0, 4000.0])
def test_the_confirm_sweep_stays_inside_the_summed_sweep_this_speaker_plays(
    fc_hz: float,
):
    """The safety argument, as a property rather than a paragraph.

    A confirm is a frequency SUBSET of VERIFY's summed sweep at the same clamp,
    so it can never excite a driver over a band the shipped summed sweep does
    not already cover. Narrowing to the crossover region takes energy AWAY from
    both extremes.
    """
    confirm = _confirm(fc_hz).segment("sweep_verify")
    verify = _verify(fc_hz).segment("sweep_verify")
    assert confirm.f1_hz >= verify.f1_hz
    assert confirm.f2_hz <= verify.f2_hz
    assert confirm.gain_db == verify.gain_db


def test_a_corner_whose_shoulders_do_not_fit_is_refused_not_narrowed():
    """Asserting, not re-judging: PROPOSE already refused a bank that cannot
    span the shoulders, so reaching here having failed it is a caller defect and
    a quietly narrowed band would return a depth built from clamped edges."""
    with pytest.raises(ValueError, match="shoulders"):
        null_confirm_band_hz(11_000.0)


def test_the_confirm_is_admittable_and_verify_is_still_not():
    """The confirm is ROUTED, so the play-time readmit gate is on its path — and
    a gate that refused it would make the confirm unplayable, not safe.

    This is the half of the design that buys something: VERIFY rides the applied
    production graph with no load and no admission, so its only level guard is
    the compose-time min-cap clamp. A confirm loads the measurement graph and
    goes through ``play_program``, so it is additionally checked segment by
    segment against the declared driver caps. VERIFY's exclusion is unchanged,
    which is the other half — this widened the gate for one routed phase, not
    for the summed family.
    """
    from jasper.active_speaker.program_admission import (
        ProgramAdmissionError,
        _validate_program,
    )

    _validate_program(_confirm())
    with pytest.raises(ProgramAdmissionError):
        _validate_program(_verify())


def test_the_confirm_reads_through_the_one_null_depth_definition():
    """Its own stimulus phase, and deliberately the SAME analyzer.

    A second analyzer would be a second null depth, and the grader comparing a
    measured depth against a computed one could not tell which it had.
    """
    from jasper.audio_measurement.program import (
        PROGRAM_PHASE_NULL_CONFIRM,
        PROGRAM_PHASES,
    )

    assert _confirm().phase == PROGRAM_PHASE_NULL_CONFIRM
    assert PROGRAM_PHASE_NULL_CONFIRM in PROGRAM_PHASES
    # The segment id `_analyze_verify` reaches for, kept so one analyzer serves
    # both phases.
    assert _confirm().segment("sweep_verify") is not None


def test_the_confirm_and_the_verify_sweep_are_different_stimuli():
    """Different band, therefore different schedule, therefore different
    ``program_id`` — which is what keeps a confirm from being compared against
    a VERIFY capture as if the two had played the same thing."""
    assert _confirm().program_id != _verify().program_id


def test_a_confirm_spec_carries_its_coordinate_into_the_graph():
    """The end-to-end hop, in the two translations that decide it.

    The engine composes from the SPEC's kind (``_compose_stimulus`` asks
    ``phase_for_measurement``, never the index map), and installs the graph
    through ``measurement_delays_for``. Both have to answer for one spec or the
    confirm plays the right stimulus through the wrong graph, or the wrong
    stimulus through the right one.
    """
    from jasper.active_speaker.crossover_v2.measure_spec import (
        MeasureSpec,
        inverted_roles_for,
        measurement_delays_for,
    )

    spec = MeasureSpec(
        kind=MEASURE_KIND_NULL_CONFIRM,
        polarity="inverted",
        inverted_role="tweeter",
        delayed_role="tweeter",
        delay_us=250.0,
        level_matched=True,
    )
    assert phase_for_measurement(spec.kind) == PHASE_NULL_CONFIRM
    assert measurement_delays_for(spec) == {"tweeter": 250.0}
    assert inverted_roles_for(spec) == ("tweeter",)


def test_the_confirm_regime_stages_the_confirm_phase():
    """The operator's door: ``--regime null_confirm`` must reach the phase whose
    program plays through the measurement graph, not the summed regime's, and
    the resolved stop must be handed the confirm's own object."""
    assert REGIME_NULL_CONFIRM in REGIMES
    request = AngleCaptureRequest(stops=(AngleStop(0, REGIME_NULL_CONFIRM),))
    (stop,) = resolve_request(request)
    assert stop.program_phase == PHASE_NULL_CONFIRM

    confirm = _confirm()
    verify = _verify()
    assert (
        program_for_stop(
            stop,
            check=verify,
            measure=None,
            verify=verify,
            cloud=verify,
            null_confirm=confirm,
        )
        is confirm
    )


def test_a_confirm_armed_without_its_program_refuses_by_name():
    """The ``measure=None`` precedent: a session that composed no confirm says
    so, rather than playing a summed sweep at a guessed band."""
    check = _verify()
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            PHASE_NULL_CONFIRM,
            check=check,
            measure=None,
            verify=check,
            cloud=check,
        )


def test_the_confirm_gets_its_own_object_and_not_verifys():
    """By identity, like every other arm of the selector."""
    confirm = _confirm()
    verify = _verify()
    resolved = program_for_phase(
        PHASE_NULL_CONFIRM,
        check=verify,
        measure=None,
        verify=verify,
        cloud=verify,
        null_confirm=confirm,
    )
    assert resolved is confirm


def test_a_confirm_that_read_no_depth_is_refused_not_accepted_empty(monkeypatch):
    """The one number this phase exists to produce, missing.

    ``reverse_null_depth_db`` is ``None`` when the capture did not span both
    shoulders or sat under the validity floor. Accepting it would bank a
    confirmation take carrying no confirmation, and the offline grader would
    then report ``confirmation_missing`` for a capture the session had already
    called good — a green take and a missing result for the same capture.

    Note the asymmetry this pins: a SHALLOW null is accepted, because a shallow
    depth is a result the landscape grader weighs. No depth at all is not a
    result; it is a capture that failed to measure.
    """
    from types import SimpleNamespace

    from jasper.active_speaker import crossover_v2_flow as flow
    from jasper.active_speaker.crossover_v2.refusal_copy import REASON_SNR_FLOOR

    # The integrity screens are exercised by their own suite; stub them PASSING
    # so this pins the depth decision alone.
    monkeypatch.setattr(
        flow._spatial, "entry_baseline_screens",
        lambda *a, **k: SimpleNamespace(kind=None, integrity_payload=None),
    )
    monkeypatch.setattr(flow, "_stimulus_locate_ok", lambda analysis: True)

    consume = flow.CrossoverV2Session._consume_null_confirm

    missing = consume(None, 1, 1, SimpleNamespace(reverse_null_depth_db=None), None)
    assert missing.accepted is False
    assert missing.code == REASON_SNR_FLOOR

    # A shallow null is a RESULT and stays accepted.
    shallow = consume(None, 1, 1, SimpleNamespace(reverse_null_depth_db=3.2), None)
    assert shallow.accepted is True
    assert shallow.payload["null_depth_db"] == 3.2
