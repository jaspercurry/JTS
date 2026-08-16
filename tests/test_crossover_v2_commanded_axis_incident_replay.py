# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free replay of the 2026-08-16 jts3 round-1 rollback (#2611).

**The incident.** Round 1 of the post-fix-wave overnight series applied
candidate ``45407c6426dd`` over the round-3 profile. The first on-axis VERIFY
capture measured the tune BETTER and in limit — blend −1.80 dB at 1003 Hz
against a 2.0 dB limit (it had been −3.75 at 1453 Hz for three rounds), level
error 0.39 against 1.5, tracking 0.07 — and then
``event=correction.crossover_v2_delta_probe verdict=model_error
reason=realized_shape_differs_from_commanded rollback=true`` fired and the
correction came off, with ``residual_offset_db = +3.2198`` on the record.

The offline analysis (2026-08-16, issue #2611) attributed that number exactly:
the probe's expected level move was the program-headroom step and ONLY that
step (−0.2997 dB), while the apply also commanded a **+3.3209 dB** per-role
tweeter trim step (−10.2141 → −6.8932), a polarity flip (inverted → keep) and a
delay change (96 → 59.6 µs). None of the three were on the commanded axis,
because it evaluated both of its sides at the applied candidate's own polarity,
delay and role gains, so all three cancelled by construction.

**What replays exactly, and what does not.** The GRAPH parameters are the
incident's: both profiles' tweeter trims, both polarities, both delays, both
program headrooms. The per-driver measured RESPONSES are not — they were never
retained as arrays, the same limitation
``tests/test_crossover_v2_incident_replay.py`` states for its own fixture — so
the branch pair here is a synthetic LR4 two-way and the post-apply capture is
built from the applied model plus a standing mic-vs-model frame. Every
assertion below is therefore about what the incident's own PARAMETERS do to the
instrument, never about reproducing its capture.

**The three channels the commanded axis reaches the verdict through**, because
the tests are organised by them and the third one is not obvious:

1. ``residual_offset_db`` — the LEVEL door. Directly and decisively wrong under
   the retired axis: the commanded tweeter step lands in bins the retired axis
   calls quiet, and is reported as a level nobody asked for.
2. WHICH bins are graded — the retired axis grades only where the correction
   FILTERS act; the new one grades everywhere the apply changes anything.
3. WHICH bins are quiet, and therefore WHERE THE FRAME IS FITTED. This is the
   one that decides rollbacks. ``error_db`` itself (``measured − predicted``) is
   algebraically independent of the commanded curve, so the retired axis cannot
   change it — what it changes is that the frame whose removal re-asks the
   rollback question was fitted, on this round, ENTIRELY ABOVE the band it was
   then applied to. An extrapolated frame is wrong by however much the standing
   disagreement is not a straight line, and that error is charged to the
   correction.
"""
from __future__ import annotations

import numpy as np
import pytest

from jasper.active_speaker.branch_chain import (
    CrossoverSection,
    crossover_response_complex,
)
from jasper.active_speaker.crossover_v2 import commanded as cmd
from jasper.active_speaker.delta_probe import (
    DELTA_PROBE_MIN_BINS,
    VERDICT_MODEL_ERROR,
    classify_delta_probe,
)

# --------------------------------------------------------------------------- #
# the incident's own numbers
# --------------------------------------------------------------------------- #

#: The round-3 profile the apply replaced.
PREVIOUS_TWEETER_TRIM_DB = -10.2141
PREVIOUS_POLARITY_SIGN = -1          # "inverted"
PREVIOUS_DELAY_US = 96.0
PREVIOUS_HEADROOM_DB = 1.8357

#: Candidate ``45407c6426dd``, applied clean.
APPLIED_TWEETER_TRIM_DB = -6.8932
APPLIED_POLARITY_SIGN = 1            # "keep"
APPLIED_DELAY_US = 59.6
APPLIED_HEADROOM_DB = 2.1354

#: The per-role step the apply commanded and the retired axis dropped. Derived,
#: never restated: the analysis quotes +3.3209 and this must BE that number.
COMMANDED_TRIM_STEP_DB = APPLIED_TWEETER_TRIM_DB - PREVIOUS_TWEETER_TRIM_DB

#: What ``baseline_profile.applied_program_level_delta_db`` declares across this
#: apply — the pre-split headroom step, and the whole of what the probe knew.
DECLARED_OFFSET_DB = PREVIOUS_HEADROOM_DB - APPLIED_HEADROOM_DB

#: The number on the incident's record.
INCIDENT_RESIDUAL_OFFSET_DB = 3.2198

FC_HZ = 1500.0
FREQS_HZ = np.linspace(200.0, 20000.0, 2048)
TRUSTED_BAND_HZ = (400.0, 16000.0)
ROLES = {"woofer_role": "woofer", "tweeter_role": "tweeter"}

#: The correction filters the applied candidate emitted. Shape, not identity:
#: the incident's own filter list was not retained in a form this test can read,
#: and what every assertion below needs from them is only that they act over a
#: bounded band — which is what makes the retired axis's quiet set the top
#: octaves, exactly as it was on the night.
APPLIED_FILTERS = {
    "woofer": (
        {"biquad_type": "Peaking", "freq": 700.0, "q": 1.2, "gain": -2.4},
    ),
    "tweeter": (
        {"biquad_type": "Peaking", "freq": 3200.0, "q": 1.4, "gain": -3.1},
        {"biquad_type": "Peaking", "freq": 5200.0, "q": 2.0, "gain": 2.2},
    ),
}


def _branch_tf() -> dict[str, np.ndarray]:
    """A two-way branch pair, each carrying its own LR4 section and a mild tilt.

    The same shape ``tests/crossover_v2_fixtures._fixture_branch_db`` argues
    for: a per-driver measurement is captured through the crossover the speaker
    is running, so the woofer's low-pass and the tweeter's high-pass belong in
    it.
    """
    octaves = np.log2(FREQS_HZ / FC_HZ)
    woofer = crossover_response_complex(
        FREQS_HZ, (CrossoverSection(fc_hz=FC_HZ, order=4, highpass=False),),
    ) * 10.0 ** ((-0.6 * octaves) / 20.0)
    tweeter = crossover_response_complex(
        FREQS_HZ, (CrossoverSection(fc_hz=FC_HZ, order=4, highpass=True),),
    ) * 10.0 ** ((-0.4 * octaves) / 20.0)
    return {"woofer": woofer, "tweeter": tweeter}


def _graph(
    *,
    tweeter_trim_db: float,
    polarity_sign: int,
    delay_us: float,
    filters: dict[str, tuple] | None = None,
) -> cmd.GraphSummation:
    return cmd.GraphSummation(
        trim_db={"woofer": 0.0, "tweeter": tweeter_trim_db},
        delay_us=delay_us,
        polarity_sign=polarity_sign,
        linearization=filters or {},
    )


def _summed(graph: cmd.GraphSummation) -> tuple[np.ndarray, np.ndarray]:
    summed = cmd.graph_predicted_sum(
        FREQS_HZ, _branch_tf(), graph, anchor_delay_us=0.0, **ROLES,
    )
    assert summed is not None
    return summed


APPLIED_GRAPH = _graph(
    tweeter_trim_db=APPLIED_TWEETER_TRIM_DB,
    polarity_sign=APPLIED_POLARITY_SIGN,
    delay_us=APPLIED_DELAY_US,
    filters=APPLIED_FILTERS,
)
PREVIOUS_GRAPH = _graph(
    tweeter_trim_db=PREVIOUS_TWEETER_TRIM_DB,
    polarity_sign=PREVIOUS_POLARITY_SIGN,
    delay_us=PREVIOUS_DELAY_US,
)
#: The RETIRED previous side: the applied candidate's own polarity, delay and
#: role gains, with the correction filters taken back out. Spelled here rather
#: than imported because it no longer exists in the product — this is what the
#: axis WAS, kept only so the tests can show what it missed.
RETIRED_PREVIOUS_GRAPH = _graph(
    tweeter_trim_db=APPLIED_TWEETER_TRIM_DB,
    polarity_sign=APPLIED_POLARITY_SIGN,
    delay_us=APPLIED_DELAY_US,
)


def _commanded(previous: cmd.GraphSummation) -> np.ndarray:
    delta = cmd.commanded_delta(_summed(previous), _summed(APPLIED_GRAPH))
    assert delta is not None
    return delta[1]


def _capture(*, curvature_db_per_octave2: float, injection_db: float = 0.0):
    """``(measured_post, measured_pre)`` for a speaker that DID what was asked.

    Both curves are their own graph's model, minus that graph's pre-split
    headroom, plus one standing mic-vs-model frame. The frame is the reason this
    fixture exists at all: an in-room gated measurement and an on-axis two-branch
    model never share a level anchor, and the disagreement is not a straight line
    in log frequency. ``curvature_db_per_octave2`` is how far from a straight
    line it is — the axis the rollback threshold below is measured along.

    ``injection_db`` is the positive control: a band-limited shift the graph
    never asked for, added to the post-apply capture only.
    """
    octaves = np.log2(FREQS_HZ / 1000.0)
    standing = -2.36 - 0.5 * octaves + curvature_db_per_octave2 * octaves ** 2
    ripple = 0.15 * np.sin(octaves * 3.1)
    injected = np.where(
        (FREQS_HZ >= 6000.0) & (FREQS_HZ <= 11000.0), injection_db, 0.0,
    )
    _f, applied_db = _summed(APPLIED_GRAPH)
    _f, previous_db = _summed(PREVIOUS_GRAPH)
    anchor = PREVIOUS_HEADROOM_DB
    return (
        applied_db - APPLIED_HEADROOM_DB + anchor + standing + ripple + injected,
        previous_db - PREVIOUS_HEADROOM_DB + anchor + standing,
    )


def _probe(commanded_db, measured_post, measured_pre):
    """The production classifier, fed exactly as ``_run_delta_probe`` feeds it."""
    _f, applied_db = _summed(APPLIED_GRAPH)
    return classify_delta_probe(
        FREQS_HZ,
        (measured_post - applied_db) + commanded_db,
        commanded_db,
        band_hz=TRUSTED_BAND_HZ,
        expected_offset_db=DECLARED_OFFSET_DB,
        entry_delta_db=(measured_pre - applied_db) + commanded_db,
    )


# --------------------------------------------------------------------------- #
# channel 1 — the level door
# --------------------------------------------------------------------------- #


def test_the_incidents_own_numbers_are_self_consistent():
    """The two derived steps the analysis quotes, re-derived from the profiles.

    A guard on the fixture rather than on the product: every assertion below
    rests on these four trims and headrooms being the incident's, and the
    analysis quotes exactly these two differences.
    """
    assert COMMANDED_TRIM_STEP_DB == pytest.approx(3.3209, abs=5e-5)
    assert DECLARED_OFFSET_DB == pytest.approx(-0.2997, abs=5e-5)


def test_the_retired_axis_reports_the_commanded_trim_step_as_uncommanded_level():
    """The +3.2198 dB on the incident's record, from the incident's parameters.

    The retired axis leaves the top octaves quiet — the correction's filters
    stop below them — and in a tweeter-only band the commanded per-role step IS
    the whole level, so the probe measures the step it should have commanded and
    reports it as a level nobody asked for.
    """
    post, pre = _capture(curvature_db_per_octave2=0.0)
    probe = _probe(_commanded(RETIRED_PREVIOUS_GRAPH), post, pre)

    # The step, less what the woofer's own stopband leaks into those bins.
    assert probe.residual_offset_db == pytest.approx(
        COMMANDED_TRIM_STEP_DB, abs=0.1,
    )
    # ...which is the number the incident rolled back on, to within the
    # difference between this synthetic branch pair and jts3's own.
    assert probe.residual_offset_db == pytest.approx(
        INCIDENT_RESIDUAL_OFFSET_DB, abs=0.05,
    )
    # And it was measured where only the tweeter plays, which is why a per-role
    # step could masquerade as a whole-band one.
    assert probe.quiet_core_band_hz is not None
    assert probe.quiet_core_band_hz[0] > 4.0 * FC_HZ


def test_the_new_axis_leaves_no_uncommanded_level_at_all():
    """The same capture, the same speaker: the step is commanded, so it is not a
    residual. Nothing else about the round changed."""
    post, pre = _capture(curvature_db_per_octave2=0.0)
    probe = _probe(_commanded(PREVIOUS_GRAPH), post, pre)
    assert probe.residual_offset_db == pytest.approx(0.0, abs=0.05)


# --------------------------------------------------------------------------- #
# channel 2 — what the axis contains
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,value",
    [
        ("trim_db", {"woofer": 0.0, "tweeter": PREVIOUS_TWEETER_TRIM_DB}),
        ("polarity_sign", PREVIOUS_POLARITY_SIGN),
        ("delay_us", PREVIOUS_DELAY_US),
    ],
)
def test_each_commanded_element_moves_the_new_axis_and_moved_neither_the_retired_one(
    field, value,
):
    """The defect, one element at a time — and its own positive control.

    Take the RETIRED previous side (the applied candidate's own parameters) and
    change exactly one element to the previous profile's. The retired axis is
    bit-identical for all three, because it never evaluated the previous graph
    at all; the new axis moves for every one, because it does.
    """
    import dataclasses

    moved = dataclasses.replace(RETIRED_PREVIOUS_GRAPH, **{field: value})
    baseline = _commanded(RETIRED_PREVIOUS_GRAPH)

    # The positive control for the harness itself: the SAME machinery, fed the
    # element the retired axis DID carry, moves the curve — so a bit-identical
    # result below is the axis being blind, not the comparison being broken.
    filtered = _commanded(
        dataclasses.replace(RETIRED_PREVIOUS_GRAPH, linearization=APPLIED_FILTERS),
    )
    assert not np.allclose(baseline, filtered)

    assert not np.allclose(baseline, _commanded(moved), atol=1e-6)


def test_the_retired_axis_fitted_its_frame_outside_the_band_it_graded():
    """Channel 3, stated structurally rather than through a verdict.

    The retired axis graded where the FILTERS act and fitted its frame above
    that band, so the offset and slope it removed from every graded bin were
    EXTRAPOLATED into it. The new axis grades everywhere the apply changes
    anything, so its frame is fitted inside the band it corrects — interpolated,
    not extrapolated. That is the whole mechanism by which a commanded change
    the model could not see became a shape defect the household paid for.
    """
    post, pre = _capture(curvature_db_per_octave2=0.0)
    retired = _probe(_commanded(RETIRED_PREVIOUS_GRAPH), post, pre)
    new = _probe(_commanded(PREVIOUS_GRAPH), post, pre)

    assert retired.quiet_core_band_hz is not None
    assert retired.quiet_core_band_hz[0] > retired.probe_band_hz[1]

    assert new.quiet_core_band_hz is not None
    assert new.probe_band_hz[0] <= new.quiet_core_band_hz[0]
    assert new.quiet_core_band_hz[1] <= new.probe_band_hz[1]
    # And the frame it fitted there is still a MEASUREMENT, not a fabrication:
    # enough bins for ``fit_frame`` to have run at all.
    assert new.frame.fitted
    assert new.quiet_n_bins >= DELTA_PROBE_MIN_BINS


# --------------------------------------------------------------------------- #
# channel 3 — the rollback the incident took
# --------------------------------------------------------------------------- #

#: Curvature sweep step. Coarse on purpose: the claim is that the two axes cross
#: the rollback bar at DIFFERENT curvatures, not where either crossing sits to
#: three decimals.
_CURVATURE_STEP = 0.025
_CURVATURE_MAX = 0.55


def _first_rollback_curvature(previous: cmd.GraphSummation) -> float:
    commanded_db = _commanded(previous)
    curvature = 0.0
    while curvature <= _CURVATURE_MAX:
        post, pre = _capture(curvature_db_per_octave2=curvature)
        if _probe(commanded_db, post, pre).rollback:
            return round(curvature, 6)
        curvature += _CURVATURE_STEP
    return float("inf")


def test_the_retired_axis_rolls_back_a_room_the_new_axis_keeps():
    """The headline, measured as a THRESHOLD rather than asserted at one point.

    Sweep how far the standing mic-vs-model disagreement departs from a straight
    line. Both axes eventually refuse — a room that curves enough really is
    doing something no model commanded — but the retired axis refuses FIRST, and
    by a wide margin, because it is correcting the graded band with a frame
    fitted outside it.

    Reporting the crossing points rather than one hand-picked curvature is what
    keeps this from being a fixture tuned until it agreed.
    """
    retired_at = _first_rollback_curvature(RETIRED_PREVIOUS_GRAPH)
    new_at = _first_rollback_curvature(PREVIOUS_GRAPH)

    assert np.isfinite(retired_at), "the retired axis must reach a rollback"
    assert np.isfinite(new_at), "and so must the new one — this is not a mute button"
    assert new_at > retired_at, (
        f"the new axis rolled back at {new_at} dB/octave^2 of room curvature and "
        f"the retired one at {retired_at}; the fix must WIDEN what the "
        f"instrument tolerates before refusing, not narrow it"
    )


#: One curvature inside the window the sweep above measures: enough departure
#: from a straight line that the retired axis refuses, not enough that the new
#: one does.
_DISPUTED_CURVATURE = 0.30


def test_at_the_disputed_room_the_retired_axis_refuses_and_the_new_one_does_not():
    post, pre = _capture(curvature_db_per_octave2=_DISPUTED_CURVATURE)

    retired = _probe(_commanded(RETIRED_PREVIOUS_GRAPH), post, pre)
    assert retired.verdict == VERDICT_MODEL_ERROR
    assert retired.rollback is True

    new = _probe(_commanded(PREVIOUS_GRAPH), post, pre)
    assert new.rollback is False


def test_an_uncommanded_shape_change_still_rolls_back_under_the_new_axis():
    """The positive control, at the exact room the test above keeps.

    A 3 dB step across 6-11 kHz that no graph asked for. The instrument is not
    quieter after this fix — it is only correct about what was asked.
    """
    post, pre = _capture(
        curvature_db_per_octave2=_DISPUTED_CURVATURE, injection_db=3.0,
    )
    probe = _probe(_commanded(PREVIOUS_GRAPH), post, pre)
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.rollback is True


# --------------------------------------------------------------------------- #
# the wiring: profile -> graph -> previous side
# --------------------------------------------------------------------------- #


def _incident_profile() -> dict[str, object]:
    """The round-3 applied profile, in the shape the reader consumes.

    Written in the emitter's own vocabulary — a non-negative delay magnitude on
    the role that carries it, a per-role ``inverted`` flag — rather than in the
    summed model's, because translating between the two is exactly what
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_graph_summation`
    is being asked to do here.
    """
    return {
        "status": "applied",
        "recomposition_snapshot": {
            "corrections": {
                "woofer": {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False},
                "tweeter": {
                    "gain_db": PREVIOUS_TWEETER_TRIM_DB,
                    "delay_ms": PREVIOUS_DELAY_US / 1000.0,
                    "inverted": True,
                },
            },
            "linearization": {},
        },
    }


def test_the_profile_reader_recovers_the_graph_the_speaker_was_playing():
    graph = cmd.profile_graph_summation(_incident_profile(), **ROLES)
    assert graph is not None
    assert graph.trim_db["tweeter"] == pytest.approx(PREVIOUS_TWEETER_TRIM_DB)
    assert graph.delay_us == pytest.approx(PREVIOUS_DELAY_US)
    assert graph.polarity_sign == PREVIOUS_POLARITY_SIGN
    # And the model it produces is the previous side these tests grade against,
    # so the reader and the model cannot drift apart without this failing.
    np.testing.assert_allclose(_summed(graph)[1], _summed(PREVIOUS_GRAPH)[1])


def test_the_previous_graphs_own_correction_filters_enter_the_previous_side():
    """A repeat round REPLACES a correction, it does not stack one.

    The round-3 profile this incident replaced happened to carry no
    linearization, so the incident alone cannot pin this element — every
    ordinary repeat round can. A previous side blind to the old filters would
    command the new correction in full and read the OLD one's removal as
    something the room did on its own.
    """
    profile = _incident_profile()
    snapshot = profile["recomposition_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["linearization"] = {
        "tweeter": [
            {"biquad_type": "Peaking", "freq": 4000.0, "q": 1.5, "gain": -2.0},
        ],
    }
    graph = cmd.profile_graph_summation(profile, **ROLES)
    assert graph is not None
    assert len(graph.linearization["tweeter"]) == 1

    corrected = _summed(graph)[1]
    uncorrected = _summed(PREVIOUS_GRAPH)[1]
    assert not np.allclose(corrected, uncorrected)
    # A 2 dB cut at 4 kHz is a tweeter-band cut, and the tweeter is the only
    # branch playing there, so the previous graph was about 2 dB quieter.
    at_4k = int(np.argmin(np.abs(FREQS_HZ - 4000.0)))
    assert corrected[at_4k] - uncorrected[at_4k] == pytest.approx(-2.0, abs=0.15)


def test_a_profile_that_names_no_graph_is_an_absence_not_a_unity_graph():
    """``None``, never a fabricated flat graph.

    A unity stand-in would make the commanded axis claim the apply commands the
    whole of the previous profile's trim — the same class of wrong answer this
    module exists to remove, pointing the other way.
    """
    assert cmd.profile_graph_summation(None, **ROLES) is None
    assert cmd.profile_graph_summation({}, **ROLES) is None
    assert cmd.profile_graph_summation(
        {"recomposition_snapshot": {"corrections": {"woofer": {"gain_db": 0.0}}}},
        **ROLES,
    ) is None


def _incident_analysis():
    from jasper.audio_measurement.program_analysis import (
        ALIGNMENT_OK,
        AlignmentEstimate,
        DriverResponse,
        ProgramAnalysis,
    )

    branch = _branch_tf()
    return ProgramAnalysis(
        phase="measure",
        program_id="cap_test_2611",
        locations=(),
        driver_responses=tuple(
            DriverResponse(
                role=role,
                freqs_hz=FREQS_HZ,
                magnitude_db=20.0 * np.log10(np.maximum(np.abs(tf), 1e-12)),
                complex_tf=tf,
                gating={},
                snr=None,
                validity_floor_hz=None,
            )
            for role, tf in branch.items()
        ),
        alignment=AlignmentEstimate(
            delay_us=APPLIED_DELAY_US, raw_delay_us=APPLIED_DELAY_US,
            parallax_us=0.0, polarity="normal", polarity_sign=1,
            polarity_agrees_with_sum=True, confidence=0.8,
            status=ALIGNMENT_OK, anchor_delay_us=0.0,
        ),
    )


def _session(seams):
    from tests.crossover_v2_fixtures import CAPS, SESSION_VOLUME_DB, _preset, _roles
    from jasper.active_speaker.crossover_v2_flow import CrossoverV2Session

    return CrossoverV2Session(
        session_id="cap_test_2611",
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=seams,
        driver_spacing_m=0.15,
    )


def test_the_session_builds_its_previous_side_from_the_applied_profile():
    """The whole chain, wired: seam -> profile reader -> summed model.

    This is the test a revert has to get past. It drives the session's own
    method, so replacing the previous side with the retired one — or dropping
    the polarity, the delay or the role gains on the way through the reader —
    lands here.
    """
    from tests.crossover_v2_fixtures import FakeSeams

    fakes = FakeSeams(applied_profile_state=_incident_profile())
    session = _session(fakes.seams())

    built = session._previous_graph_predicted_sum(_incident_analysis())
    assert built is not None
    np.testing.assert_allclose(built[0], FREQS_HZ)
    np.testing.assert_allclose(built[1], _summed(PREVIOUS_GRAPH)[1])

    # ...and it is NOT the retired axis's previous side, which is what the
    # session used to subtract.
    assert not np.allclose(built[1], _summed(RETIRED_PREVIOUS_GRAPH)[1])


def test_an_unbound_applied_profile_seam_leaves_the_commanded_axis_unavailable(caplog):
    """No fallback to the retired axis, and the reason is on the journal.

    A session that cannot learn what the speaker is playing cannot state what an
    apply commands. ``None`` makes the probe ``unavailable`` — no rollback, and
    no pass either.
    """
    import dataclasses
    import logging

    from tests.crossover_v2_fixtures import FakeSeams

    session = _session(
        dataclasses.replace(FakeSeams().seams(), applied_profile=None)
    )
    with caplog.at_level(logging.WARNING):
        assert session._previous_graph_predicted_sum(_incident_analysis()) is None
    assert "event=correction.crossover_v2_previous_graph_unavailable" in caplog.text
    assert "reason=no_applied_profile_seam" in caplog.text

    assert session._commanded_delta_for(
        _incident_analysis(), _summed(APPLIED_GRAPH),
    ) is None
