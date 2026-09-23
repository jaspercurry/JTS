# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free replay of the 2026-08-16 jts3 round-1 rollback (#2611).

**The incident.** Round 1 of the post-fix-wave overnight series applied
candidate ``45407c6426dd`` over the round-3 profile. The first on-axis VERIFY
capture measured the tune BETTER and in limit — blend −1.80 dB at 1003 Hz
against a 2.0 dB limit (it had been −3.75 at 1453 Hz for three rounds), level
error 0.39 against 1.5, tracking 0.07 — and then
The probe reported ``model_error`` with ``residual_offset_db = +3.2198``.

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

# the incident's own numbers

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
ROLES = {"roles": ("woofer", "tweeter")}

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
        FREQS_HZ, _branch_tf(), graph, anchor_delay_us=0.0,
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


# channel 1 — the level door


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


# channel 2 — what the axis contains


@pytest.mark.parametrize(
    "field,value",
    [
        ("trim_db", {"woofer": 0.0, "tweeter": PREVIOUS_TWEETER_TRIM_DB}),
        # The WOOFER's own role gain, alone (#2614). The incident moved the
        # tweeter's, so every case above holds the woofer at 0.0 and a previous
        # side that dropped the woofer gain entirely still passed all of them
        # (adversarial panel, mutation: 500/500 green). A non-zero woofer trim
        # is ordinary production output — ``anchor_trims``' normalize step
        # shifts every branch — so this is a live element, not a symmetry.
        ("trim_db", {"woofer": -2.5, "tweeter": APPLIED_TWEETER_TRIM_DB}),
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
    bit-identical for all of them, because it never evaluated the previous graph
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


# channel 3 — the rollback the incident took

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
        if _probe(commanded_db, post, pre).advises_against_keep:
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
    assert retired.advises_against_keep is True

    new = _probe(_commanded(PREVIOUS_GRAPH), post, pre)
    assert new.advises_against_keep is False


def test_an_uncommanded_shape_change_still_rolls_back_under_the_new_axis():
    """The positive control, at the exact room the test above keeps.

    A 3 dB step across 6-11 kHz that no graph asked for. The instrument is not
    quieter after this fix — it is only correct about what was asked.

    **What this is and is not sensitive to.** It guards the PROBE, not the axis.
    ``realized − commanded`` is ``measured_post − predicted_post`` whichever
    graph the two sides are stated against, so the graded error curve is
    invariant to any additive change in the commanded axis and this assertion
    cannot detect one (mutation-verified, adversarial panel PR #2614). What it
    does detect is the probe going blind to an injected shape defect — the
    regression the fix above must not buy. The axis's own contents are pinned by
    the element-by-element cases in channel 2, and WHERE the axis is graded by
    channel 3.
    """
    post, pre = _capture(
        curvature_db_per_octave2=_DISPUTED_CURVATURE, injection_db=3.0,
    )
    probe = _probe(_commanded(PREVIOUS_GRAPH), post, pre)
    assert probe.verdict == VERDICT_MODEL_ERROR
    assert probe.advises_against_keep is True


# the wiring: profile -> graph -> previous side


#: The draft declares both branches non-inverted, which is jts3's own state and
#: the case where the profile's ABSOLUTE flags and the branch frame agree. The
#: inverted-draft case — where they do not — has its own test below.
DRAFT_UPRIGHT = {"woofer": False, "tweeter": False}


def _incident_profile(*, fc_hz: float = FC_HZ) -> dict[str, object]:
    """The round-3 applied profile, in the shape the reader consumes.

    Written in the emitter's own vocabulary — a non-negative delay magnitude on
    the role that carries it, a per-role ``inverted`` flag — rather than in the
    summed model's, because translating between the two is exactly what
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_graph_summation`
    is being asked to do here.

    ``fc_hz`` is the corner the graph was built at, which
    :func:`~jasper.active_speaker.crossover_v2.commanded.profile_crossover_fc_hz`
    reads off the snapshot preset and the session checks the capture against.
    """
    from tests.test_active_speaker_profile import _two_way_preset

    preset = _two_way_preset()
    preset["crossover_regions"] = [
        {**region, "fc_hz": float(fc_hz)} for region in preset["crossover_regions"]
    ]
    return {
        "status": "applied",
        "recomposition_snapshot": {
            "preset": preset,
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
    graph = cmd.profile_graph_summation(
        _incident_profile(), draft_inverted_by_role=DRAFT_UPRIGHT, **ROLES,
    )
    assert graph is not None
    assert graph.trim_db["tweeter"] == pytest.approx(PREVIOUS_TWEETER_TRIM_DB)
    assert graph.delay_us == pytest.approx(PREVIOUS_DELAY_US)
    assert graph.polarity_sign == PREVIOUS_POLARITY_SIGN
    # And the model it produces is the previous side these tests grade against,
    # so the reader and the model cannot drift apart without this failing.
    np.testing.assert_allclose(_summed(graph)[1], _summed(PREVIOUS_GRAPH)[1])


def test_the_reader_carries_the_woofers_own_role_gain_too():
    """The WOOFER half of the trim, which the incident cannot pin (#2614).

    The incident's round-3 profile trimmed only the tweeter, so every assertion
    above holds the woofer at 0.0 — and a reader that dropped the woofer gain
    entirely passed all of them (adversarial panel, mutation: 500/500 green). A
    non-zero woofer trim is ordinary production output, because
    ``intervention.anchor_trims``' normalize step shifts every branch, so the
    reader is asserted on one.

    Both ends are checked — the value the reader returns AND the curve the model
    then produces — so neither a reader that drops it nor a model that ignores
    what the reader returned can pass.
    """
    profile = _incident_profile()
    profile["recomposition_snapshot"]["corrections"]["woofer"]["gain_db"] = -2.5  # type: ignore[index]
    graph = cmd.profile_graph_summation(
        profile, draft_inverted_by_role=DRAFT_UPRIGHT, **ROLES,
    )
    assert graph is not None
    assert graph.trim_db["woofer"] == pytest.approx(-2.5)

    # A 2.5 dB woofer cut is a woofer-band cut, and the woofer is the only
    # branch playing an octave below Fc, so the summed model moved with it.
    at_low = int(np.argmin(np.abs(FREQS_HZ - FC_HZ / 2.0)))
    trimmed = _summed(graph)[1]
    assert trimmed[at_low] - _summed(PREVIOUS_GRAPH)[1][at_low] == pytest.approx(
        -2.5, abs=0.15,
    )


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
    graph = cmd.profile_graph_summation(
        profile, draft_inverted_by_role=DRAFT_UPRIGHT, **ROLES,
    )
    assert graph is not None
    assert len(graph.linearization["tweeter"]) == 1

    corrected = _summed(graph)[1]
    uncorrected = _summed(PREVIOUS_GRAPH)[1]
    assert not np.allclose(corrected, uncorrected)
    # A 2 dB cut at 4 kHz is a tweeter-band cut, and the tweeter is the only
    # branch playing there, so the previous graph was about 2 dB quieter.
    at_4k = int(np.argmin(np.abs(FREQS_HZ - 4000.0)))
    assert corrected[at_4k] - uncorrected[at_4k] == pytest.approx(-2.0, abs=0.15)


def _read(profile, draft=None):
    return cmd.profile_graph_summation(
        profile, draft_inverted_by_role=draft or DRAFT_UPRIGHT, **ROLES,
    )


def test_a_profile_that_names_no_graph_is_an_absence_not_a_unity_graph():
    """``None``, never a fabricated flat graph.

    A unity stand-in would make the commanded axis claim the apply commands the
    whole of the previous profile's trim — the same class of wrong answer this
    module exists to remove, pointing the other way.
    """
    assert _read(None) is None
    assert _read({}) is None
    assert _read(
        {"recomposition_snapshot": {"corrections": {"woofer": {"gain_db": 0.0}}}},
    ) is None


def test_a_third_branch_is_refused_rather_than_silently_dropped():
    """One delay, one relative sign — a third branch has no place to be stated,
    and the model reads only ``roles[0]`` and ``roles[-1]``."""
    profile = _incident_profile()
    profile["recomposition_snapshot"]["corrections"]["mid"] = {  # type: ignore[index]
        "gain_db": -1.0, "delay_ms": 0.0, "inverted": False,
    }
    assert cmd.profile_graph_summation(
        profile,
        roles=("woofer", "mid", "tweeter"),
        draft_inverted_by_role={**DRAFT_UPRIGHT, "mid": False},
    ) is None


def test_a_boolean_delay_is_unreadable_not_one_millisecond():
    profile = _incident_profile()
    profile["recomposition_snapshot"]["corrections"]["tweeter"]["delay_ms"] = True  # type: ignore[index]
    assert _read(profile) is None


def test_a_role_named_without_a_gain_is_an_absence_not_unity():
    """``gain_db`` missing is "this profile does not say", never 0 dB (#2614).

    The module's own absence-never-unity rule, which ``float(x or 0.0)`` broke
    for exactly this field: a role whose trim is unstated would have modelled
    the previous graph at unity and put the whole of the real trim on the
    commanded axis as something the apply asked for. An absent ``delay_ms`` is
    deliberately NOT the same — the profile records a magnitude only on the
    delayed role, so its absence there is a statement.
    """
    profile = _incident_profile()
    corrections = profile["recomposition_snapshot"]["corrections"]  # type: ignore[index]
    assert _read(profile) is not None

    without_gain = {**corrections["tweeter"]}
    without_gain.pop("gain_db")
    profile["recomposition_snapshot"]["corrections"] = {  # type: ignore[index]
        **corrections, "tweeter": without_gain,
    }
    assert _read(profile) is None

    # ...while the delay's absence keeps its meaning.
    undelayed = _incident_profile()
    tweeter = {**undelayed["recomposition_snapshot"]["corrections"]["tweeter"]}  # type: ignore[index]
    tweeter.pop("delay_ms")
    undelayed["recomposition_snapshot"]["corrections"]["tweeter"] = tweeter  # type: ignore[index]
    graph = _read(undelayed)
    assert graph is not None
    assert graph.delay_us == pytest.approx(0.0)


def test_the_previous_sides_polarity_is_stated_in_the_branches_own_frame():
    """An inverted DRAFT must not read as an inverted previous graph (#2614).

    The measured branches already carry the draft's declared polarity
    (``program_analysis._compose_configured_path_ir``), so the applied side's
    ``alignment.polarity_sign`` is a flip RELATIVE TO the draft: ``+1`` means
    "as the preset declares". Taking the profile's ABSOLUTE per-role flags as
    the sign put the two sides of the subtraction in different frames on every
    speaker whose draft declares an inverted branch — reachable from ``/sound``
    Alignment.

    Both halves are asserted, because only the pair shows it is a FRAME and not
    an offset: a profile that agrees with an inverted draft is ``+1``, and one
    that disagrees with it is ``−1``.
    """
    agrees = _incident_profile()
    agrees["recomposition_snapshot"]["corrections"]["tweeter"]["inverted"] = True  # type: ignore[index]
    # Draft: tweeter inverted too. The profile matches the draft, so in the
    # branches' own frame nothing is flipped.
    assert _read(
        agrees, {"woofer": False, "tweeter": True},
    ).polarity_sign == 1
    # Draft upright, profile inverted: a real flip relative to the branches.
    assert _read(agrees, DRAFT_UPRIGHT).polarity_sign == -1

    # And the reverse pairing, so the rule is not "the draft wins".
    upright = _incident_profile()
    upright["recomposition_snapshot"]["corrections"]["tweeter"]["inverted"] = False  # type: ignore[index]
    assert _read(upright, DRAFT_UPRIGHT).polarity_sign == 1
    assert _read(upright, {"woofer": False, "tweeter": True}).polarity_sign == -1


def test_the_snapshot_preset_names_the_corner_the_graph_was_built_at():
    """The corner reader, and its "cannot say" (#2614).

    ``None`` for a profile with no snapshot preset — an era-older record — so
    the session refuses rather than affirming a previous graph whose crossover
    it cannot check.
    """
    assert cmd.profile_crossover_fc_hz(
        _incident_profile(fc_hz=1234.0),
    ) == pytest.approx(1234.0)
    assert cmd.profile_crossover_fc_hz(None) is None
    assert cmd.profile_crossover_fc_hz({"status": "applied"}) is None
    assert cmd.profile_crossover_fc_hz(
        {"recomposition_snapshot": {"corrections": {}}},
    ) is None


# channel 4 — the STATE axis, and what the CHANGE axis alone stops watching
#
# #2614's blocker. Everything above is about the CHANGE the apply commands,
# which is the right axis for "did the correction realize what it asked for".
# It is the wrong axis for the one hearing-safety question this probe asks —
# *is the speaker putting more energy into a driver than the applied graph
# declares* — because a REPEAT round changes nothing in the bands it leaves
# alone, and a band it leaves alone still has a driver in it.

#: A repeat round that touches ONLY the woofer band. The +5 dB tweeter boost at
#: 5.2 kHz is in both graphs, byte-identical, so this apply commands nothing
#: there — and the applied graph still declares it.
_STANDING_BOOST = (
    {"biquad_type": "Peaking", "freq": 5200.0, "q": 1.2, "gain": 5.0},
)
_NEW_WOOFER_CUT = (
    {"biquad_type": "Peaking", "freq": 600.0, "q": 1.0, "gain": -3.0},
)
_REPEAT_TRIMS = {"woofer": 0.0, "tweeter": -6.9}
_BOOST_BAND_HZ = (4200.0, 6400.0)


def _repeat_round_graphs():
    """``(applied, previous, raw)`` for the repeat round described above."""
    import dataclasses

    applied = cmd.GraphSummation(
        trim_db=_REPEAT_TRIMS, delay_us=60.0, polarity_sign=1,
        linearization={"woofer": _NEW_WOOFER_CUT, "tweeter": _STANDING_BOOST},
    )
    return (
        applied,
        dataclasses.replace(
            applied, linearization={"woofer": (), "tweeter": _STANDING_BOOST},
        ),
        dataclasses.replace(applied, linearization={"woofer": (), "tweeter": ()}),
    )


def _repeat_round_axes():
    """``(applied_db, commanded_db, declared_db, boost_band_mask)`` for that round."""
    applied, previous, raw = _repeat_round_graphs()
    _f, applied_db = _summed(applied)
    commanded = cmd.commanded_delta(_summed(previous), (FREQS_HZ, applied_db))
    declared = cmd.commanded_delta(_summed(raw), (FREQS_HZ, applied_db))
    assert commanded is not None and declared is not None
    band = (FREQS_HZ >= _BOOST_BAND_HZ[0]) & (FREQS_HZ <= _BOOST_BAND_HZ[1])
    return applied_db, commanded[1], declared[1], band


def _repeat_round_curves(*, hot_db: float):
    """``(measured_post, measured_pre)`` for the repeat round at ``hot_db``.

    The ``standing`` term is a model-vs-mic disagreement present in BOTH
    captures — the shape series-2 D1 turns on. It is what the pre-D1 safety
    rules graded as delivered energy, and what anchoring cancels: only the
    ``hot_db`` term is in the post capture alone.
    """
    _applied, previous, _raw = _repeat_round_graphs()
    applied_db, _commanded_db, _declared_db, band = _repeat_round_axes()
    standing = -1.0 - 0.2 * np.log2(FREQS_HZ / 1000.0)
    return (
        applied_db + standing + np.where(band, hot_db, 0.0),
        _summed(previous)[1] + standing,
    )


def _repeat_round_probe(*, hot_db: float, state_axis: bool, anchored: bool = True):
    """The probe for a speaker realizing ``hot_db`` more than declared, at 5.2 kHz.

    ``state_axis`` is #2614's fix: whether the classifier is told what the
    applied graph DECLARES, or only what this apply CHANGES. ``anchored`` is
    series-2 D1's: whether it is given the PRE-apply capture that turns the two
    directional findings into measurements of the speaker.
    """
    applied_db, commanded_db, declared_db, _band = _repeat_round_axes()
    measured_post, measured_pre = _repeat_round_curves(hot_db=hot_db)
    return classify_delta_probe(
        FREQS_HZ,
        (measured_post - applied_db) + commanded_db,
        commanded_db,
        band_hz=TRUSTED_BAND_HZ,
        expected_offset_db=0.0,
        entry_delta_db=(
            (measured_pre - applied_db) + commanded_db if anchored else None
        ),
        declared_transfer_db=declared_db if state_axis else None,
    )


def test_the_repeat_rounds_untouched_boost_band_commands_nothing():
    """The fixture's own guard: this apply really does leave that band alone.

    Both assertions below rest on the boost being invisible to the CHANGE axis
    and plain on the STATE one, so both facts are pinned before they are used.

    "Invisible" is stated against the probe's own graded floor rather than
    against zero, because it is the floor that decides which bins get graded —
    and the woofer's new cut does leak a few thousandths of a dB up here through
    its own stopband, which is physics rather than a commanded change.
    """
    from jasper.active_speaker.delta_probe import graded_command_floor_db

    _applied_db, commanded_db, declared_db, band = _repeat_round_axes()
    floor = graded_command_floor_db(FREQS_HZ)
    assert bool(np.all(np.abs(commanded_db[band]) < floor[band]))
    assert float(np.max(np.abs(commanded_db[band]))) < 0.05
    assert float(np.max(declared_db[band])) == pytest.approx(5.0, abs=0.1)


def test_an_untouched_boost_realized_hot_still_reaches_the_hard_stop():
    """The blocker (#2614): the adoption table's hard stop fires again.

    An existing +5 dB tweeter boost this apply does not change, realized 4 dB
    hotter than the graph declares. With only the CHANGE axis the probe grades
    that band as commanding nothing, reports ``boost_overshoot_db=None``, and a
    correction putting 4 dB of unasked-for energy into a tweeter stays on the
    speaker. With the STATE axis it is measured and the hard stop fires.
    """
    from jasper.active_speaker.crossover_v2.contracts import SafetyStatus
    from jasper.active_speaker.crossover_v2.verification import (
        SAFETY_BOOST_OVER_DECLARED_BOUND,
        evaluate_applied_safety,
    )

    without = _repeat_round_probe(hot_db=4.0, state_axis=False)
    assert without.boost_over_declared_bound is False
    assert without.boost_overshoot_db is None
    assert without.realized_louder_than_commanded is False
    assert evaluate_applied_safety(
        probe=without, integrity=None,
    ).status is not SafetyStatus.UNSAFE

    probe = _repeat_round_probe(hot_db=4.0, state_axis=True)
    assert probe.boost_over_declared_bound is True
    assert probe.boost_overshoot_db is not None
    assert probe.boost_overshoot_db > 1.5
    assert probe.realized_louder_than_commanded is True
    # The amount is the delivered energy EXACTLY, and that is series-2 D1's
    # half of this pin: the ``standing`` model error is in both captures and
    # cancels, so what is left is the 4 dB the speaker actually put out. Before
    # anchoring this read 2.586 dB — the hazard, less a model error that had
    # wandered into the same number.
    assert probe.boost_overshoot_db == pytest.approx(4.0, abs=1e-9)
    # ...and it is the ADOPTION TABLE's hard stop this feeds, so the seam that
    # reads the probe has to see it too.
    safety = evaluate_applied_safety(probe=probe, integrity=None)
    assert safety.status is SafetyStatus.UNSAFE
    assert safety.reason == SAFETY_BOOST_OVER_DECLARED_BOUND
    assert safety.evidence["safety_anchored"] is True


def test_the_negative_control_measures_the_untouched_band_and_finds_nothing():
    """Same round, same fixture, a speaker that realized what was declared.

    The control that makes the test above a measurement rather than a tripwire:
    ``boost_overshoot_db`` is a NUMBER — the band was looked at — and no finding
    fires. ``None`` here would mean the mask had simply gone empty again.

    **It reads exactly 0.0, and that is series-2 D1's control** (pre-D1:
    −1.213 dB). The speaker delivered precisely what the graph declared, so a
    rule measuring delivered energy must return zero; the old rule returned the
    ``standing`` model error instead, which is a real quantity about a
    prediction and no quantity at all about a driver.
    """
    probe = _repeat_round_probe(hot_db=0.0, state_axis=True)
    assert probe.boost_overshoot_db is not None
    assert probe.boost_overshoot_db == pytest.approx(0.0, abs=1e-9)
    assert probe.boost_over_declared_bound is False
    assert probe.realized_louder_than_commanded is False
    assert probe.advises_against_keep is False


def test_the_untouched_boost_hard_stop_needs_a_pre_apply_capture(caplog):
    """The same 4 dB hazard with no anchor: not measured, and it says so.

    Series-2 D1's fail-direction, stated as a measurement rather than as prose.
    Without a pre-apply capture the only thing computable here is
    ``(measured_post − predicted_post)``, which is the acoustic model's own
    error — the quantity that took a measured, safe, improving round off jts3
    on 2026-08-17. So the finding is not made, ``safety_anchored`` is False, and
    the safety axis reports "nothing looked" rather than "nothing found".

    What still holds with no anchor is disclosed beside it: the model's own
    departure is a number, and it lands on the QUALITY axis as a target.
    """
    unanchored = _repeat_round_probe(hot_db=4.0, state_axis=True, anchored=False)
    assert unanchored.safety_anchored is False
    assert unanchored.boost_over_declared_bound is False
    assert unanchored.boost_overshoot_db is None
    assert unanchored.realized_louder_than_commanded is False
    # ...and the disclosure that replaces it.
    assert unanchored.model_departure_over_tolerance is True
    assert unanchored.max_signed_error_db == pytest.approx(2.5856, abs=5e-4)

    from jasper.active_speaker.crossover_v2.contracts import SafetyStatus
    from jasper.active_speaker.crossover_v2.verification import (
        evaluate_applied_safety,
    )

    safety = evaluate_applied_safety(probe=unanchored, integrity=None)
    assert safety.status is SafetyStatus.SAFE
    assert safety.evidence["safety_anchored"] is False
    assert safety.evidence["model_departure_over_tolerance"] is True


#: The mirror-image round: the previous graph CUT 5.2 kHz by 5 dB and the
#: applied graph removes that cut. The applied graph then emits nothing at all,
#: so the STATE axis is ~0 across the whole band while the CHANGE axis is +5 dB
#: in the removal band.
_STANDING_CUT = (
    {"biquad_type": "Peaking", "freq": 5200.0, "q": 1.2, "gain": -5.0},
)


def _cut_removal_axes():
    """``(commanded_db, declared_db, realized_db, entry_db, band)`` for that round."""
    import dataclasses

    applied = cmd.GraphSummation(
        trim_db=_REPEAT_TRIMS, delay_us=60.0, polarity_sign=1,
        linearization={"woofer": (), "tweeter": ()},
    )
    previous = dataclasses.replace(
        applied, linearization={"woofer": (), "tweeter": _STANDING_CUT},
    )
    _f, applied_db = _summed(applied)
    # The applied graph emits nothing, so it IS the raw crossover here and the
    # STATE axis is flat zero across the whole band.
    declared_db = cmd.commanded_delta(
        (FREQS_HZ, applied_db), (FREQS_HZ, applied_db),
    )[1]
    commanded_db = cmd.commanded_delta(_summed(previous), (FREQS_HZ, applied_db))[1]
    band = (FREQS_HZ >= _BOOST_BAND_HZ[0]) & (FREQS_HZ <= _BOOST_BAND_HZ[1])
    standing = -1.0 - 0.2 * np.log2(FREQS_HZ / 1000.0)
    measured_post = applied_db + standing + np.where(band, 4.0, 0.0)
    measured_pre = _summed(previous)[1] + standing
    return (
        commanded_db, declared_db,
        (measured_post - applied_db) + commanded_db,
        (measured_pre - applied_db) + commanded_db,
        band,
    )


def test_a_removed_cut_realized_hot_is_watched_by_the_unions_change_half():
    """The union's OTHER half, which no test pinned (#2614 delta review).

    The mirror of the standing-boost case above: the previous graph cut 5.2 kHz
    and this apply REMOVES the cut, so the applied graph declares nothing there
    while the change axis commands +5 dB — energy going into a tweeter that the
    STATE axis alone cannot see, because there is no state axis to see it with.
    The union watches it because it keeps the change bins.

    Asserted on the CLASSIFIER, not on :func:`boost_overshoot`: the mask is
    built inside ``classify_delta_probe``, so a test that assembled its own
    would keep passing while the classifier swapped the union for the state
    mask. That swap passed 495/495 before this test existed, which is why the
    monotonicity argument needed a pin rather than three paragraphs of prose.
    """
    commanded_db, declared_db, realized_db, entry_db, band = _cut_removal_axes()
    # The fixture's own guard: the two axes really do disagree in this band, and
    # the state axis is empty everywhere — so a state-only mask has NO bins and
    # everything below rests on the change half.
    assert float(np.max(commanded_db[band])) == pytest.approx(5.0, abs=0.1)
    assert float(np.max(np.abs(declared_db))) == pytest.approx(0.0, abs=1e-9)

    probe = classify_delta_probe(
        FREQS_HZ, realized_db, commanded_db,
        band_hz=TRUSTED_BAND_HZ, expected_offset_db=0.0,
        entry_delta_db=entry_db,
        declared_transfer_db=declared_db,
    )
    assert probe.boost_over_declared_bound is True
    # 4.0 exactly, not 2.586: the fixture's standing model tilt is in both
    # captures and cancels (series-2 D1), leaving the energy the speaker really
    # delivered into that band.
    assert probe.boost_overshoot_db == pytest.approx(4.0, abs=1e-9)
    assert probe.realized_louder_than_commanded is True


def test_the_state_axis_adds_bins_and_changes_nothing_else():
    """It is a MASK, not a second error curve.

    The graded statistics, the exceedance width, the gain fit and the quiet
    residual are all algebraically independent of which graph the two sides are
    stated against (``realized − commanded == measured_post − predicted_post``
    either way), so supplying the state axis must move the two directional
    findings and nothing else.
    """
    without = _repeat_round_probe(hot_db=4.0, state_axis=False)
    with_state = _repeat_round_probe(hot_db=4.0, state_axis=True)
    for field in (
        "verdict", "reason", "advises_against_keep", "max_error_db", "rms_error_db",
        "worst_hz", "exceedance_octaves", "gain_factor", "residual_offset_db",
        "probe_band_hz", "n_bins", "quiet_n_bins",
    ):
        assert getattr(without, field) == getattr(with_state, field), field


def test_a_malformed_state_axis_is_an_absence_not_a_grid_error():
    """A wrong-length curve means "nothing known", exactly like ``entry_delta_db``.

    It must not become a ``grid_mismatch`` refusal: the three graded arrays are
    what that verdict is about, and an optional record that arrived truncated
    should narrow the safety mask honestly rather than void the whole probe.
    """
    _applied, previous, _raw = _repeat_round_graphs()
    applied_db, commanded_db, _declared_db, band = _repeat_round_axes()
    standing = -1.0 - 0.2 * np.log2(FREQS_HZ / 1000.0)
    measured_post = applied_db + standing + np.where(band, 4.0, 0.0)
    measured_pre = _summed(previous)[1] + standing

    def _probe_with(declared):
        return classify_delta_probe(
            FREQS_HZ,
            (measured_post - applied_db) + commanded_db,
            commanded_db,
            band_hz=TRUSTED_BAND_HZ,
            expected_offset_db=0.0,
            entry_delta_db=(measured_pre - applied_db) + commanded_db,
            declared_transfer_db=declared,
        )

    truncated = _probe_with(np.zeros(7))
    absent = _probe_with(None)
    assert truncated.verdict == absent.verdict
    assert truncated.reason == absent.reason
    assert truncated.boost_overshoot_db is absent.boost_overshoot_db


# channel 5 — the alternative-Fc round, where there IS no change axis
#
# #2614 delta review. The corner guard refuses the previous graph on every
# committed alternative-Fc candidate, which took the whole probe down with it:
# the two directional hearing-safety rules never ran, ``evaluate_applied_safety``
# reported SAFE on a round where nothing had looked, and nothing said so. The
# STATE axis needs no corner match, so the probe runs on that alone — and since
# series-2 D1 what it grades there is the MODEL's departure, not the speaker's
# delivered energy: the two directional rules still do not run, but now the
# verdict, ``safety_anchored`` and the axis's own reason all say so.
