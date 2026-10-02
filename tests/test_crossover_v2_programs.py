# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5a-ii: what a session plays, how loud, and for which phase.

Level policy and program composition live in
:mod:`jasper.active_speaker.crossover_v2.programs`.  Three kinds of pin, in the
order a reviewer should read them:

1. **Regression pins.** The composed programs' current ``stimulus_id``s and
   segment gains, written as literals rather than recomputed, because a pin
   that derives its expectation from the code under test pins nothing.  A
   ``stimulus_id`` hashes the schedule and every segment's gain but not the
   session fader (#5012); a change that moves one recomputes it by composing
   this fixture and says why.
2. **The identity invariant** — the COMPARED pair (VERIFY and the entry
   baseline) gets the *same object*, not an equal one, and each position group
   gets one object of its own.  #2291's before→after comparison is keyed by
   ``stimulus_id`` equality; a copy that merely compared equal today would break
   that key the moment composition picked up any per-call state.
3. **The courtesy-prelude rule** (#1677, trimmed 2026-08-18) — the prelude
   announces a SESSION, not a capture, so it rides the phases that open one and
   the entry baseline that must stay identical to stage 2's anchor.  Pinned
   against the goldens in both directions: restoring the prelude reproduces the
   shipped id byte for byte, which is what makes "only the prelude moved" a
   measurement rather than a claim.

The fixture deliberately covers BOTH level regimes, because they are
discriminating in opposite directions: at the deep-cap corner (the JTS3 shape,
tweeter at −65 dBFS) the min-cap clamp binds and swallows ``extra_backoff_db``
entirely, so a dropped backoff would be invisible there; at the unclamped corner
it shows through.  A pin at one corner only would pass over half the policy.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from itertools import product
from types import SimpleNamespace

import pytest
import yaml

from jasper.active_speaker.crossover_v2 import contracts
from jasper.active_speaker.crossover_v2 import journey
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.excitation_safety_plan import resolve_driver_excitation_ceilings
from jasper.active_speaker.angle_capture import request_for_preset
from jasper.active_speaker.measurement_programs import (
    REGIME_BRANCHES, available_presets, near_field_drivers, preset, run_preset,
)
from jasper.active_speaker.measurement_level import scope_gains_db
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.crossover_v2 import programs
from jasper.active_speaker.crossover_v2.programs import (
    COURTESY_PRELUDE_PHASES,
    GROUP_SUMMED_SWEEP_PHASES,
    SUMMED_SWEEP_PHASES,
    NoProgramForPhaseError,
    SessionExcitation,
    back_off_gain,
    compose_target_program,
    courtesy_prelude_for_phase,
    program_for_phase,
    program_for_spec,
)
from jasper.active_speaker import graph_safety as gs
from jasper.active_speaker.branch_chain import confirmed_protection_sections
from jasper.active_speaker.crossover_v2.measure_spec import branch_channels_for, branch_probes, solo_target
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, emit_measurement_graph
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    KIND_COURTESY_TONE,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    RoleBand,
    is_level_probe,
)
from jasper.active_speaker.profile import ramp_bound_db_spl
from jasper.audio_measurement.ramp import MAX_STEP_DB
from jasper.platform.speaker_layout import measurement_target_id
from jasper.active_speaker.crossover_v2.composition import compose_plan_program
from tests.test_active_speaker_audition import ACTIVE_PCM
from tests.test_active_speaker_program_admission import _profile_and_targets
from tests.test_rear_output_foundation import _rear_pair

from tests.crossover_v2_fixtures import (
    CAPS,
    FC_HZ,
    SESSION,
    SESSION_VOLUME_DB,
    FakeSeams,
    _preset,
    _roles,
)

#: The solved per-driver gains a CHECK pass would hand MEASURE.
GAIN_PLAN_DB = {"woofer": -32.0, "tweeter": -38.0}

#: Regression pins of MEASURE and VERIFY's current ids at this fixture with the
#: courtesy prelude on (#5012). A change that moves one recomputes it by
#: composing this fixture and says why.
#:
#: ``measure`` is not what MEASURE ships (it no longer opens on the prelude):
#: :func:`test_only_the_prelude_moved_under_the_shipped_measure_program` puts the
#: prelude back and asserts THIS literal returns.
GOLDEN_DEEP_CAP = {
    "measure": "f46c38df6d18b72dff4e628019086475eae182675bd36b498120f4814cc244e6",
    "verify": "eafd6bd16424baed64f5b0a613e226509e4fd9a45e8de9978c91f4f7d73e7d25",
}

#: What the phases the courtesy prelude no longer announces ship TODAY, at the
#: same fixture and the same gains as :data:`GOLDEN_DEEP_CAP`. Read off the
#: composers after the trim; the pin that makes them meaningful is not this
#: literal but the round trip below, which shows each one becomes its
#: ``GOLDEN_DEEP_CAP`` twin the moment the prelude is put back.
GOLDEN_UNANNOUNCED = {
    "measure": "f3f924538a85f2d191c085f36600ed4bacb3c427f3ab786e13ff4bfc65c68808",
    "cloud": "7bbc3b2c8062ae850a2b169296b0af03ce28a2dfd3b18405f2c1ff25d5e9d570",
}


def _excitation(
    caps: dict[str, float],
    sweep_duration_limits_s: dict[str, float] | None = None,
) -> SessionExcitation:
    return SessionExcitation(
        roles=tuple(_roles()),
        caps_dbfs=caps,
        session_volume_db=SESSION_VOLUME_DB,
        fc_hz=FC_HZ,
        sweep_duration_limits_s=sweep_duration_limits_s or {},
    )


def _conductor(caps: dict[str, float]):
    return flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=caps,
        session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
        gain_plan_db=GAIN_PLAN_DB,
    )


# 1. regression pins of the current ids


def test_the_verify_program_is_the_one_that_shipped():
    ex = _excitation(CAPS)

    assert ex.verify_program().stimulus_id == GOLDEN_DEEP_CAP["verify"]


@pytest.mark.parametrize("phase,scope,stimulus,expected", [
    ("check", "drivers", None, "94f11dfeb764451eaf0a844b362b35645f0bda8758d78f5307126d1969e37140"),
    ("check", "drivers", -30.0, "94f11dfeb764451eaf0a844b362b35645f0bda8758d78f5307126d1969e37140"),
    ("check", "drivers", -60.0, "5910bb4eaab0311a5bbf88b64a24682da01cc6270fe3994b0552748d3a22de8f"),
    ("measure", "drivers", None, GOLDEN_UNANNOUNCED["measure"]),
    ("verify", "timing", None, GOLDEN_DEEP_CAP["verify"]),
    ("cloud_verify", "timing", None, GOLDEN_UNANNOUNCED["cloud"]),
    ("verify", "candidate_branches", None, "1a8a0f18d2748f345a50422466c567431c478a3f73c39580e577a494d6985816"),
])
def test_without_a_level_reference_programs_keep_their_shipped_identity(phase, scope, stimulus, expected):
    spec = MeasureSpec(kind="baseline", program_phase=phase, graph_scope=scope, scope_gains_db=None,
                       candidate_id="trial" if scope != "drivers" else "",
                       branch_target_ids=("woofer", "tweeter") if scope == "candidate_branches" else ())
    program = programs.program_for_spec(spec, _excitation(CAPS), GAIN_PLAN_DB, stimulus,
                                        safety_profile={}, role_targets={})
    assert program.stimulus_id == expected


@pytest.mark.parametrize("caps", [CAPS, {"woofer": 0.0, "tweeter": 0.0}])
@pytest.mark.parametrize("extra_backoff_db", [-3.0, 0.0, 6.0])
def test_check_pilots_do_not_exceed_the_summed_pilot_pair(caps, extra_backoff_db):
    ex = _excitation(caps)
    check = ex.check_program(extra_backoff_db=extra_backoff_db)
    verify = ex.verify_program()
    summed = {
        segment.segment_id.rsplit("_", 1)[-1]: segment.gain_db
        for segment in verify.stimulus_segments()
        if segment.kind == "pilot"
    }

    assert all(
        segment.gain_db <= summed[segment.segment_id.rsplit("_", 1)[-1]]
        for segment in check.stimulus_segments()
        if segment.kind == "pilot"
    )
    assert all(
        segment.gain_db == pytest.approx(
            ex.check_program().segment(segment.segment_id).gain_db - max(0.0, extra_backoff_db))
        for segment in check.stimulus_segments() if segment.kind == "pilot"
    )


@pytest.mark.parametrize("headroom", [0.0, 3.1, 5.61])
@pytest.mark.parametrize("phase,scope", [("check", "drivers"), ("measure", "drivers"), ("verify", "timing")])
def test_scope_gains_correct_blind_levels_and_preserve_the_measured_plan(headroom, phase, scope):
    def graph(headroom, trim, cut):
        return yaml.safe_dump({
            "devices": {"samplerate": 48000, "capture": {"channels": 2}},
            "filters": {
                "headroom": {"type": "Gain", "parameters": {"gain": -headroom}},
                "trim": {"type": "Gain", "parameters": {"gain": trim}},
                "linearization": {"type": "Biquad", "parameters": {
                    "type": "Peaking", "freq": 1000.0, "q": 30.0, "gain": cut}},
            },
            "pipeline": [
                {"type": "Filter", "channels": [0, 1], "names": ["headroom"]},
                {"type": "Filter", "channels": [0], "names": ["linearization"]},
                {"type": "Filter", "channels": [1], "names": ["trim"]},
            ],
        })
    topology, _, _ = _profile_and_targets()
    excitation = _excitation({"woofer": 0.0, "tweeter": 0.0})
    candidate, drivers = graph(headroom, -21.3, -8.0), graph(0.0, 0.0, 0.0)
    gain = scope_gains_db(drivers, candidate, excitation.roles, topology=topology)
    assert gain == pytest.approx({"woofer": headroom, "tweeter": headroom + 21.3}, abs=0.3)
    assert scope_gains_db(candidate, candidate, excitation.roles, topology=topology) == {"woofer": 0, "tweeter": 0}
    quieter = scope_gains_db(candidate, drivers, excitation.roles, topology=topology)
    assert quieter == pytest.approx({role: -db for role, db in gain.items()})
    spec = MeasureSpec(kind="baseline", program_phase=phase, graph_scope=scope,
                       candidate_id="trial" if scope != "drivers" else "")
    def compose(gain):
        return programs.program_for_spec(replace(spec, scope_gains_db=gain), excitation, GAIN_PLAN_DB,
                                         safety_profile={}, role_targets={})
    unchanged, lowered = compose({}), compose(gain)
    assert compose(quieter).stimulus_id == unchanged.stimulus_id
    if phase == "measure":
        assert lowered.stimulus_id == unchanged.stimulus_id
    for before, after in zip(unchanged.stimulus_segments(), lowered.stimulus_segments()):
        backoff = 0 if phase == "measure" else gain[before.role] if phase == "check" else max(gain.values())
        assert before.effective_peak_dbfs - after.effective_peak_dbfs == pytest.approx(backoff)


@pytest.mark.parametrize("asked_db,played_db", [(-6.0, -6.0), (4.0, 0.0)])
def test_a_summed_retake_plays_the_peak_it_asks_for(asked_db, played_db):
    """A boosted candidate's summed retake plays the peak it asks for, never
    above its first attempt's, whatever the scope backoff (#5709)."""
    spec = MeasureSpec(kind="baseline", program_phase="verify", graph_scope="timing", candidate_id="trial",
                       scope_gains_db={"woofer": 0.0, "tweeter": 9.0})

    def summed_peak(stimulus_dbfs=None):
        program = programs.program_for_spec(spec, _excitation({"woofer": 0.0, "tweeter": 0.0}), GAIN_PLAN_DB,
                                            stimulus_dbfs, safety_profile={}, role_targets={})
        peak, = {segment.gain_db for segment in program.segments if segment.kind == KIND_SUMMED_SWEEP}
        return peak

    first = summed_peak()
    assert summed_peak(first + asked_db) == pytest.approx(first + played_db)


def test_only_the_prelude_moved_under_the_shipped_measure_program(monkeypatch):
    """MEASURE ships a new id, and putting the prelude back gives the old one.

    The trim's whole scope claim, measured. Restoring MEASURE to the announced
    set must reproduce ``GOLDEN_DEEP_CAP["measure"]`` byte for byte — a
    SHA-256 over the entire schedule including every segment's gain — so no
    frequency, duration, or level moved with the prelude.
    """
    ex = _excitation(CAPS)

    assert ex.measure_program(GAIN_PLAN_DB).stimulus_id == GOLDEN_UNANNOUNCED["measure"]

    monkeypatch.setattr(
        programs, "COURTESY_PRELUDE_PHASES",
        frozenset(COURTESY_PRELUDE_PHASES | {journey.PHASE_MEASURE}),
    )

    assert ex.measure_program(GAIN_PLAN_DB).stimulus_id == GOLDEN_DEEP_CAP["measure"]


def test_a_position_plays_the_verify_sweep_with_the_prelude_taken_off(monkeypatch):
    """The cloud twin is the summed sweep at the same clamp, unannounced.

    Same round trip as MEASURE's, and it is what says the split costs nothing
    acoustically: announce the position groups again and the cloud program
    becomes the VERIFY program's own id, so the two differ in the prelude and in
    nothing else — not in the min-cap clamp that is their only level guard.
    """
    ex = _excitation(CAPS)

    assert ex.cloud_program().stimulus_id == GOLDEN_UNANNOUNCED["cloud"]
    assert ex.cloud_program().stimulus_id != ex.verify_program().stimulus_id

    monkeypatch.setattr(
        programs, "COURTESY_PRELUDE_PHASES",
        frozenset(COURTESY_PRELUDE_PHASES | GROUP_SUMMED_SWEEP_PHASES),
    )

    assert ex.cloud_program().stimulus_id == GOLDEN_DEEP_CAP["verify"]


def test_the_conductor_composes_through_the_same_owner():
    """The conductor's held objects ARE the ones this module composes.

    Not a restatement of the pin above: it would still pass if the constructor
    quietly composed at its own level and only the module's own composer matched
    the golden.
    """
    c = _conductor(CAPS)
    ex = _excitation(CAPS)

    assert c.program_for_phase(journey.PHASE_CHECK).stimulus_id == ex.check_program().stimulus_id
    assert c.program_for_phase(journey.PHASE_VERIFY).stimulus_id == GOLDEN_DEEP_CAP["verify"]
    assert (
        c.program_for_phase(journey.PHASE_MEASURE).stimulus_id
        == GOLDEN_UNANNOUNCED["measure"]
    )
    assert (
        c.program_for_phase(journey.PHASE_CLOUD_VERIFY).stimulus_id
        == GOLDEN_UNANNOUNCED["cloud"]
    )


def test_the_summed_sweep_is_clamped_to_the_most_restrictive_cap():
    """The one level guard on the post-apply sweep, asserted as a number.

    VERIFY plays through the applied production graph with no play-time
    admission gate, so this clamp is the only thing between a deep-cap
    compression driver and the shared reference base. With the tweeter at
    −65 dBFS and the session at −20, the admissible digital gain is
    ``−65 − (−20) − 0.01`` = −45.01 dBFS, and every level in the program —
    sweep and both pilots — must sit at or under it.
    """
    program = _excitation(CAPS).verify_program()
    gains = [seg.gain_db for seg in program.segments if seg.gain_db]

    assert max(gains) == pytest.approx(-45.01)
    assert min(CAPS.values()) == -65.0


def test_the_pilot_pair_keeps_its_ten_db_delta_at_any_level():
    ex = _excitation(CAPS)

    for hi in (-20.0, -45.01, -65.0):
        lo, hi_out = ex.pilot_gains(hi)
        assert hi_out == hi
        assert hi - lo == pytest.approx(programs.PILOT_LEVEL_DELTA_DB)


# 2. the clamp itself


@pytest.mark.parametrize(
    ("gain", "session_volume", "cap", "expected"),
    [
        # Unclamped: the requested gain already sits under the folded ceiling.
        (-45.0, -20.0, -35.0, -45.0),
        (-65.0, -20.0, -35.0, -65.0),
        # Clamped: the cap binds, with the ulp margin below it.
        (-20.0, -20.0, -65.0, -45.01),
        (-32.0, 0.0, -65.0, -65.01),
        # Exactly at the ceiling still backs off, which is the whole point of
        # the margin — admission's strict ``>`` refuses an at-cap plan by one
        # ulp.
        (-45.01, -20.0, -65.0, -45.01),
        (-45.0, -20.0, -65.0, -45.01),
    ],
)
def test_the_clamp_holds_the_effective_peak_under_the_cap(
    gain, session_volume, cap, expected,
):
    got = back_off_gain(gain, session_volume, cap)

    assert got == pytest.approx(expected)
    # The property the table is sampling: folded through the session volume, the
    # result never reaches the cap.
    assert got + session_volume < cap or got == pytest.approx(gain)


def test_the_backoff_is_swallowed_when_the_cap_already_binds():
    """A retry's extra backoff cannot make a clamped program louder OR quieter.

    Recorded because it surprised the extraction's own dual-run: at the deep-cap
    corner the clip-retry rearm composes a byte-identical program, so a pin that
    only looked here would pass over a DELETED ``extra_backoff_db``.
    """
    ex = _excitation(CAPS)

    assert (
        ex.verify_program(extra_backoff_db=3.0).stimulus_id
        == ex.verify_program().stimulus_id
    )


def test_the_backoff_shows_through_when_the_cap_does_not_bind():
    """The other corner, which is what makes the pin above non-vacuous."""
    ex = _excitation({"woofer": 0.0, "tweeter": 0.0})

    assert (
        ex.verify_program(extra_backoff_db=3.0).stimulus_id
        != ex.verify_program().stimulus_id
    )
    assert (
        ex.measure_program(GAIN_PLAN_DB, extra_backoff_db=3.0).stimulus_id
        != ex.measure_program(GAIN_PLAN_DB).stimulus_id
    )


# 3. the identity invariant


def test_the_compared_pair_gets_the_same_object():
    """``is``, not ``==``. The whole of #2291's comparability rests on it.

    ``stimulus_id`` equality is what a before→after comparison checks before it
    will compare a before against an after, and the reason that equality holds
    is that the entry baseline and VERIFY are handed one object. Asserting equal ids instead would keep passing under a
    composer that returned a fresh-but-equal program today and drifted tomorrow.
    """
    c = _conductor(CAPS)
    verify = c.program_for_phase(journey.PHASE_VERIFY)

    for phase in sorted(SUMMED_SWEEP_PHASES - GROUP_SUMMED_SWEEP_PHASES):
        assert c.program_for_phase(phase) is verify

    assert journey.PHASE_TIMING in SUMMED_SWEEP_PHASES
    assert journey.PHASE_TIMING not in GROUP_SUMMED_SWEEP_PHASES


def test_every_position_group_gets_the_same_object():
    """The other half of the invariant, and it is ``is`` for the same reason.

    A group's positions are combined into one curve, so a composer that handed
    two of them different-but-equal programs would be combining across a
    difference nothing downstream can see.
    """
    c = _conductor(CAPS)
    cloud = c.program_for_phase(journey.PHASE_CLOUD_MEASURE)

    for phase in sorted(GROUP_SUMMED_SWEEP_PHASES):
        assert c.program_for_phase(phase) is cloud

    assert cloud is not c.program_for_phase(journey.PHASE_VERIFY)
    assert GROUP_SUMMED_SWEEP_PHASES < SUMMED_SWEEP_PHASES


def test_a_lateral_pose_replays_the_measure_object_verbatim():
    c = _conductor(CAPS)

    assert c.program_for_phase(journey.PHASE_LATERAL) is c.program_for_phase(
        journey.PHASE_MEASURE
    )


def test_measure_before_the_gain_solve_refuses_rather_than_guessing():
    """No program is composed at a guessed level."""
    ex = _excitation(CAPS)
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            journey.PHASE_MEASURE,
            check=ex.check_program(),
            measure=None,
            verify=ex.verify_program(),
            cloud=ex.cloud_program(),
        )


def test_an_unplanned_phase_refuses():
    ex = _excitation(CAPS)
    with pytest.raises(NoProgramForPhaseError):
        program_for_phase(
            "not_a_phase",
            check=ex.check_program(),
            measure=None,
            verify=ex.verify_program(),
            cloud=ex.cloud_program(),
        )


# 3b. the courtesy-prelude rule (#1677, trimmed 2026-08-18)


def _has_prelude(program) -> bool:
    return any(seg.kind == KIND_COURTESY_TONE for seg in program.segments)


def test_the_capture_that_opens_a_session_is_announced():
    """Stage 1 opens on CHECK and stage 2 on VERIFY; both warn the room first.

    This is #1677 itself — the incident was a headless session whose FIRST sweep
    started over the household's music. Whatever else the rule trims, these two
    captures keep the beeps or the incident is reopened.
    """
    c = _conductor(CAPS)

    assert _has_prelude(c.program_for_phase(journey.PHASE_CHECK))
    assert _has_prelude(c.program_for_phase(journey.PHASE_VERIFY))
    assert courtesy_prelude_for_phase(journey.PHASE_CHECK)
    assert courtesy_prelude_for_phase(journey.PHASE_VERIFY)


def test_a_capture_the_household_began_inside_a_running_session_is_not():
    """Every capture behind the opener plays no prelude — the trim itself.

    MEASURE and each lateral pose are begun by the household's own tap at a
    position it has just walked to, inside a session whose measurement window is
    already held; a prompted cloud position likewise. 3.6 s each, twelve times
    on a Full journey.
    """
    c = _conductor(CAPS)

    for phase in (
        journey.PHASE_MEASURE, journey.PHASE_LATERAL,
        journey.PHASE_CLOUD_MEASURE, journey.PHASE_CLOUD_VERIFY,
    ):
        assert not _has_prelude(c.program_for_phase(phase)), phase
        assert not courtesy_prelude_for_phase(phase), phase


def test_the_timing_take_is_announced_because_its_twin_is():
    """Not an opener — held to the rule by ``stimulus_id``, and stated as such.

    Stage 1's last capture carries the prelude for one reason: its program
    object is stage 2's anchor, and that equality is #2291's before→after
    comparison. A rule that dropped it here would leave every round's before
    and after incomparable.
    """
    c = _conductor(CAPS)

    assert _has_prelude(c.program_for_phase(journey.PHASE_TIMING))
    assert c.program_for_phase(journey.PHASE_TIMING) is c.program_for_phase(
        journey.PHASE_VERIFY
    )
    assert journey.PHASE_TIMING in COURTESY_PRELUDE_PHASES


def test_the_conductor_translates_the_refusal_into_its_own_error():
    """Callers above the conductor handle ``CrossoverV2FlowError``; the pure
    selector has no business knowing that type."""
    c = flow.CrossoverV2Session(
        session_id=SESSION,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=FakeSeams().seams(),
        driver_spacing_m=0.15,
    )

    with pytest.raises(contracts.CrossoverV2FlowError):
        c.program_for_phase(journey.PHASE_MEASURE)


# 4. the bundle is frozen, so a subset cannot drift


def test_the_declarations_cannot_be_mutated_after_construction():
    caps = dict(CAPS)
    ex = _excitation(caps)

    caps["tweeter"] = 0.0  # the caller's dict moves on

    assert ex.caps_dbfs["tweeter"] == -65.0
    with pytest.raises(TypeError):
        ex.caps_dbfs["tweeter"] = 0.0  # type: ignore[index]
    with pytest.raises(Exception):
        ex.session_volume_db = 0.0  # type: ignore[misc]


@pytest.mark.parametrize("limit", [1.0, 2.0, 4.0])
@pytest.mark.parametrize("band", [None, (20.0, 20000.0)])
@pytest.mark.parametrize("requested_s", [0.5, 8.0])
def test_summed_sweep_fits_the_tightest_role_duration(limit, band, requested_s):
    excitation = _excitation(
        {"woofer": 0.0, "tweeter": -65.0},
        sweep_duration_limits_s={"woofer": 4.0, "tweeter": limit},
    )
    excitation = replace(excitation, summed_sweep_band_hz=band)
    verify = excitation.verify_program(sweep_s=requested_s)
    for program in (verify, excitation.cloud_program()):
        sweeps = [segment for segment in program.stimulus_segments() if segment.kind == "summed_sweep"]
        assert len(sweeps) == 1
        assert 0 < sweeps[0].n_samples / program.sample_rate_hz <= limit
        if program is verify and requested_s < 1:
            assert sweeps[0].n_samples / program.sample_rate_hz < 1
        assert max(s.effective_peak_dbfs for s in program.stimulus_segments()) <= -65.0


@pytest.mark.parametrize(("purpose", "poses"), [
    ("speaker", "speaker_mark"), ("room", "room_quick"), ("room", "seat_cloud"), ("bass", "room_quick"),
    ("bass", "seat_cloud"),
])
def test_prepared_summed_captures_name_no_band_of_their_purpose(purpose, poses):
    """A summed stop names no band for its purpose: a speaker or room take
    sweeps the audio band the resolved driver bands give (ADR-0328), and a
    bass take plays its stimulus (ADR-0400)."""
    layout = run_preset(purpose, poses)
    request = request_for_preset(layout, mover=layout.mover or "human")
    _, safety, targets = _profile_and_targets(woofer_floor=30, woofer_upper=4000,
                                             max_sweep_duration_s=4)
    roles = tuple(RoleBand(role, channel, resolve_driver_excitation_ceilings(
        safety, fingerprint, program_admission=True)[0])
        for channel, (role, fingerprint) in enumerate(targets.items()))
    captures = prepare_plan_captures(request, roles_bands=roles)
    excitation = replace(_excitation(CAPS, {"woofer": 4.0, "tweeter": 4.0}), roles=roles)
    host = SimpleNamespace(excitation=excitation, set_program=lambda *args: None)
    context = SimpleNamespace(safety_profile=safety, role_targets=targets)
    for capture in captures:
        spec = capture.spec
        if spec.graph_scope == "drivers":
            continue
        program = compose_plan_program(host, spec, None, context=context)
        sweeps = [s for s in program.stimulus_segments() if s.kind == "summed_sweep"]
        stop_purpose = capture.stop.purpose or purpose
        expected = {"speaker": (20, 20000), "room": (20, 20000), "bass": (30, 1100)}[stop_purpose]
        assert len(sweeps) == (3 if purpose == "bass" else 1)
        assert all((sweep.f1_hz, sweep.f2_hz) == expected for sweep in sweeps)
        assert spec.sweep_band_hz == ()


@pytest.mark.parametrize("row", ["branches", "front_rear"])
def test_a_branch_take_the_plan_host_composes_is_admitted(tmp_path, row):
    """The PRODUCTION composer, not the builder. ``compose_plan_program`` is what
    ``bind_production_play`` plays, and the same session's admission has to
    accept what it composed. The capture window is sized independently and must
    never end before the program it records.

    The registry row decides WHICH two targets sound: ``front_rear`` excites the
    two woofers and leaves the tweeter alone, and it reaches the composer and the
    emitted graph through the spec, never through the box's acoustic roles.
    """
    from jasper.active_speaker.crossover_v2.capture_plan import (
        CAPTURE_ENTRY_MARGIN_MS, _program_duration_ms, build_inline_session_spec,
    )
    from jasper.active_speaker.program_admission import readmit_summed_program_from_wav
    from jasper.audio_measurement.program import write_program_wav
    from tests.test_active_speaker_program_admission import (
        CARDIOID_TAKE, CROSSOVER_TAKE, _rear_take_inputs, _roles as _declared_roles,
    )

    take = CROSSOVER_TAKE if row == "branches" else CARDIOID_TAKE
    topology, safety, targets, graph = _rear_take_inputs(take)
    roles = tuple(_declared_roles())
    excitation = SessionExcitation(
        roles=roles, caps_dbfs=CAPS, session_volume_db=SESSION_VOLUME_DB, fc_hz=FC_HZ,
        sweep_duration_limits_s={"woofer": 4.0, "tweeter": 4.0})
    request = request_for_preset(preset(row), candidates=("trial",))
    captures = [capture for capture in prepare_plan_captures(request, roles_bands=roles)
                if capture.spec.graph_scope == "candidate_branches"]
    assert captures
    assert captures[0].spec.branch_target_ids == tuple(take)
    context = SimpleNamespace(safety_profile=safety, role_targets=targets)
    program = compose_plan_program(
        SimpleNamespace(excitation=excitation, gain_plan_db=None, set_program=lambda *args: None),
        captures[0].spec, None, context=context)
    assert program.channels == 2
    assert {s.role for s in program.stimulus_segments() if s.role} == set(take)

    wav = tmp_path / "branches.wav"
    write_program_wav(wav, program)
    admission = readmit_summed_program_from_wav(
        program, wav, graph_yaml=graph, topology=topology, safety_profile=safety,
        role_targets=targets, session_volume_db=excitation.session_volume_db)
    assert admission.allowed, admission.to_dict()
    assert {segment.role for segment in admission.segments} == set(take)

    plan = build_inline_session_spec(
        [(c.spec, c.resolved(request).prompt, "trial") for c in captures],
        roles_bands=roles, fc_hz=excitation.fc_hz, safety_profile=safety,
        role_targets=targets, acknowledgement_binding="a" * 32, retries_per_pose=0,
    ).capture_plan
    assert plan.entries[0].duration_ms >= (
        _program_duration_ms(program) + CAPTURE_ENTRY_MARGIN_MS)


def test_per_driver_measure_keeps_declared_bands_with_a_room_session():
    excitation = replace(_excitation(CAPS), summed_sweep_band_hz=(20.0, 20000.0))
    program = excitation.measure_program(GAIN_PLAN_DB)
    assert {
        s.role: (s.f1_hz, s.f2_hz) for s in program.stimulus_segments() if s.kind == "sweep"
    } == {rb.role: (rb.band.lower_hz, rb.band.upper_hz) for rb in excitation.roles}


def test_the_measurement_band_unions_the_roles_in_any_order():
    woofer = RoleBand("woofer", 0, FrequencyBand(45.0, 6000.0))
    tweeter = RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0))
    assert (programs.measurement_band_hz([woofer, tweeter])
            == programs.measurement_band_hz([tweeter, woofer]) == (45.0, 20000.0))


@pytest.mark.parametrize("floor", [20.0, 30.0, 45.0])
def test_every_summed_sweep_covers_the_audio_band_from_resolved_driver_bands(floor):
    """The resolved driver bands union to 20 Hz-20 kHz (ADR-0328), so a
    summed sweep needs no band of its own for any purpose (ADR-0400)."""
    _, safety, targets = _profile_and_targets(woofer_floor=floor, woofer_upper=4000)
    next(target for target in safety["targets"] if target["role"] == "tweeter")["hard_excitation_band_hz"][1] = 18000
    roles = [RoleBand(role, channel, resolve_driver_excitation_ceilings(
        safety, fingerprint, program_admission=True)[0])
        for channel, (role, fingerprint) in enumerate(targets.items())]
    assert programs.measurement_band_hz(roles) == (20.0, 20000.0)


@pytest.mark.parametrize("scope,ids,refused", [
    ("drivers", ("woofer:rear",), False),
    ("drivers", ("woofer", "tweeter"), True),
    ("drivers", ("",), True),
    ("candidate_branches", ("woofer",), True),
    ("candidate", ("woofer",), True),
])
def test_a_drivers_take_names_at_most_one_target(scope, ids, refused):
    """A drivers take plays one named target alone or the session's own roles;
    two named targets are a branch take, which needs the candidate graph."""
    def make():
        return MeasureSpec(kind="baseline", graph_scope=scope, branch_target_ids=ids,
                           candidate_id="" if scope == "drivers" else "trial")

    if refused:
        with pytest.raises(ValueError):
            make()
        return
    assert solo_target(make()) == ids[0]


NEAR_FIELD = preset("nearfield/each").stimulus


def _near_field_rear(cap_dbfs: float, stimulus=NEAR_FIELD) -> tuple[SessionExcitation, MeasureSpec]:
    band = FrequencyBand(20.0, 4000.0)
    return (SessionExcitation((RoleBand("woofer", 0, band),), {"woofer:rear": cap_dbfs}, -20.0, None,
                              {"woofer:rear": 8.0}, target_bands={"woofer:rear": band}),
            MeasureSpec(kind="baseline", branch_target_ids=("woofer:rear",), stimulus=stimulus, level_probe=True))


def _sweeps(program):
    return [segment for segment in program.segments if segment.kind == KIND_SWEEP]


@pytest.mark.parametrize("stimulus", [NEAR_FIELD, {"band_hz": [30.0, 1000.0], "sweep_s": 4.0, "gap_s": 0.4}])
def test_a_near_field_take_plays_its_declared_stimulus(stimulus):
    """The plan's near-field row is what each driver's take plays: pilots and
    three bit-identical sweeps over the declared band inside the driver's own,
    each about the declared length, with the declared gap between sweeps and
    half of it around the pilots (ADR-0360 §4)."""
    band, targets = FrequencyBand(20.0, 4000.0), ("woofer", "woofer:rear")
    excitation = SessionExcitation((RoleBand("woofer", 0, band),), dict.fromkeys(targets, 0.0), -20.0, None,
                                   dict.fromkeys(targets, 12.0), target_bands=dict.fromkeys(targets, band))
    request = request_for_preset(replace(preset("nearfield/each"), stimulus=stimulus), targets=targets)
    edges = (max(band.lower_hz, stimulus["band_hz"][0]), min(band.upper_hz, stimulus["band_hz"][1]))
    for capture in prepare_plan_captures(request):
        program = program_for_spec(capture.spec, excitation, None, 100.0, safety_profile={}, role_targets={})
        rate, sounds, sweeps = program.sample_rate_hz, program.stimulus_segments(), _sweeps(program)
        between = program.segments[program.segments.index(sounds[0]):program.segments.index(sounds[-1])]
        assert {(s.role, s.f1_hz, s.f2_hz) for s in sounds} == {(capture.spec.branch_target_ids[0], *edges)}
        assert len(sweeps) == 3 and len({(s.n_samples, s.gain_db) for s in sweeps}) == 1
        assert sweeps[0].n_samples / rate == pytest.approx(stimulus["sweep_s"], abs=0.25)
        assert {s.n_samples for s in between if s.kind == "silence"} == {
            round(stimulus["gap_s"] * rate), round(stimulus["gap_s"] / 2 * rate)}


BASS = preset("bass/axis").stimulus


@pytest.mark.parametrize("scope,ids,stimulus,accepted", [
    ("drivers", ("woofer:rear",), NEAR_FIELD, True), ("candidate", (), BASS, True),
    ("drivers", ("woofer:rear",), BASS, False), ("candidate", (), NEAR_FIELD, False),
    ("drivers", (), NEAR_FIELD, False), ("candidate_branches", ("woofer", "tweeter"), BASS, False),
    ("timing", (), BASS, False)])
def test_a_declared_stimulus_plays_on_one_driver_or_the_candidate_graph(scope, ids, stimulus, accepted):
    """A band stimulus plays on one driver alone and a ceiling stimulus on the
    candidate graph; a take that plays two drivers has no composer for either,
    so its spec refuses one rather than play without it (#5737)."""
    def make():
        return MeasureSpec(kind="baseline", graph_scope=scope, branch_target_ids=ids, stimulus=stimulus,
                           candidate_id="" if scope == "drivers" else "trial")

    if accepted:
        assert make().stimulus == stimulus
    else:
        with pytest.raises(ValueError):
            make()


@pytest.mark.parametrize("cap_dbfs,ceiling_dbfs", [(0.0, 0.0), (-40.0, -20.01)])
@pytest.mark.parametrize("asked_db,played_db", [(-14.0, -14.0), (6.0, 0.0)])
def test_a_near_field_take_plays_the_peak_it_asks_never_above_its_ceiling(cap_dbfs, ceiling_dbfs, asked_db, played_db):
    """A driver's take plays the peak it asks for, never above its ceiling: digital
    full scale under its driver's cap and the run's fader, since the seat-equivalent
    cap is removed (ADR-0403 §4). The fader here is −20 dB."""
    excitation, spec = _near_field_rear(cap_dbfs)
    ceiling, = {s.gain_db for s in _sweeps(compose_target_program(excitation, spec, 100.0))}
    played, = {s.gain_db for s in _sweeps(compose_target_program(excitation, spec, ceiling + asked_db))}
    assert (ceiling, played) == pytest.approx((ceiling_dbfs, ceiling + played_db))


@pytest.mark.parametrize("cap_dbfs,scope_gains_db,stimulus,band_hz", [
    (0.0, None, NEAR_FIELD, (20.0, 2000.0)), (-40.0, None, NEAR_FIELD, (20.0, 2000.0)),
    (0.0, {"woofer:rear": 0.09}, NEAR_FIELD, (20.0, 2000.0)), (0.0, None, None, (150.0, 4000.0))])
def test_a_driver_poses_first_play_is_its_level_probe(cap_dbfs, scope_gains_db, stimulus, band_hz):
    """With no level asked, a driver pose plays its level probe: its take's band,
    bursts rising at most MAX_STEP_DB from −60 dBFS at the output to its take's
    own ceiling, no two of one length (ADR-0365, ADR-0403 §4). A near-field take
    sweeps to 2 kHz; a far-field one sweeps MEASURE's band (#5696), both inside
    the driver's own."""
    excitation, spec = _near_field_rear(cap_dbfs, stimulus)
    spec = replace(spec, scope_gains_db=scope_gains_db)
    probe = program_for_spec(spec, excitation, None, safety_profile={}, role_targets={})
    take = compose_target_program(excitation, spec, 100.0)
    ceiling, = {s.gain_db for s in _sweeps(take)}
    gains = [s.gain_db for s in _sweeps(probe)]

    assert is_level_probe(probe) and not is_level_probe(take)
    assert (gains[0] + excitation.session_volume_db, gains[-1]) == pytest.approx((-60.0, ceiling))
    assert all(0.0 < later - earlier <= MAX_STEP_DB for earlier, later in zip(gains, gains[1:]))
    assert len({s.n_samples for s in _sweeps(probe)}) == len(gains)
    assert {(s.f1_hz, s.f2_hz) for s in _sweeps(probe)} == {(s.f1_hz, s.f2_hz) for s in _sweeps(take)} == {band_hz}


@pytest.mark.parametrize("scope_gains_db", [None, {"woofer": 3.0, "tweeter": 1.0}])
def test_a_close_driverless_take_probes_its_own_summed_sweep(scope_gains_db):
    """A driverless summed take that finds its level plays its probe first:
    bursts of its own summed sweep's band, rising at most MAX_STEP_DB from −60 dBFS
    at the output to that take's ceiling, no two of one length (ADR-0365,
    ADR-0403)."""
    excitation = _excitation({"woofer": 0.0, "tweeter": 0.0}, {"woofer": 4.0, "tweeter": 4.0})
    spec = MeasureSpec(kind="baseline", graph_scope="candidate", candidate_id="trial", program_phase="lateral",
                       scope_gains_db=scope_gains_db, level_probe=True)
    probe = program_for_spec(spec, excitation, GAIN_PLAN_DB, safety_profile={}, role_targets={})
    sweep = program_for_spec(spec, excitation, GAIN_PLAN_DB, 0.0, safety_profile={},
                             role_targets={}).segment("sweep_verify")
    bursts = probe.stimulus_segments()
    gains = [burst.gain_db for burst in bursts]

    assert is_level_probe(probe) and (probe.phase, probe.channels) == ("verify", 1)
    assert (gains[0] + excitation.session_volume_db, gains[-1]) == pytest.approx((-60.0, sweep.gain_db))
    assert all(0.0 < later - earlier <= MAX_STEP_DB for earlier, later in zip(gains, gains[1:]))
    assert len({burst.n_samples for burst in bursts}) == len(bursts)
    assert {(burst.kind, burst.f1_hz, burst.f2_hz) for burst in bursts} == {(KIND_SUMMED_SWEEP, sweep.f1_hz,
                                                                          sweep.f2_hz)}


@pytest.mark.parametrize("take_band_hz,probe_bands_hz", [
    ((), [(20.0, 4000.0), (30.0, 3000.0)]), ((40.0, 1000.0), [(40.0, 1000.0)] * 2)], ids=["unstated", "stated"])
def test_a_branch_take_finds_its_level_from_a_drivers_probe_of_each_branch(take_band_hz, probe_bands_hz):
    """A branch set's first take plays each branch alone first: the probe a
    driver's pose plays, on the drivers graph, measuring no candidate, over the
    take's own band within that driver's, down to its floor. A take that states
    no band sweeps every role's whole band, so its probes sweep their drivers'.
    The take has no probe of its own and plays at its ceiling until a level is
    asked (ADR-0403 §3)."""
    targets = ("woofer", "woofer:rear")
    bands = dict(zip(targets, (FrequencyBand(20.0, 4000.0), FrequencyBand(30.0, 3000.0))))
    excitation = SessionExcitation((RoleBand("woofer", 0, bands["woofer"]),), dict.fromkeys(targets, 0.0), -20.0,
                                   None, dict.fromkeys(targets, 8.0), target_bands=bands)
    take = MeasureSpec(kind="verify", graph_scope="candidate_branches", candidate_id="trial", program_phase="lateral",
                       branch_target_ids=targets, cleared_layers=("rear_calibration",), sweep_band_hz=take_band_hz,
                       level_probe=True)

    probes = branch_probes(take)
    programs = [program_for_spec(spec, excitation, None, safety_profile={}, role_targets={})
                for spec in (*probes, take)]

    assert [(p.graph_scope, p.branch_target_ids, p.candidate_id, p.cleared_layers) for p in probes] == [
        ("drivers", (target,), "", ()) for target in targets]
    assert [is_level_probe(program) for program in programs] == [True, True, False]
    assert [{(s.f1_hz, s.f2_hz) for s in program.stimulus_segments()} for program in programs[:2]] == [
        {band} for band in probe_bands_hz]
    assert branch_probes(replace(take, level_probe=False)) == ()


@pytest.mark.parametrize("rear,target", [
    (False, "woofer"), (False, "tweeter"),
    (True, "woofer"), (True, "woofer:rear"), (True, "tweeter"),
])
def test_a_one_driver_take_routes_its_target_alone(rear, target):
    """The protected neutral graph a one-driver take plays through sources that
    target's output from program channel 0 and parks every other declared
    output. An unfitted rear keeps its terminal mute unless it is the target.
    The capture stays at the ring's width and the volume ceiling at 0 dB."""
    topology, safety, targets = _profile_and_targets(rear=rear, woofer_floor=20, woofer_upper=4000)
    preset = _rear_pair("mono")[0] if rear else _preset()
    profile = MeasurementGraphProfile(
        preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM,
        protection_sections_by_role=confirmed_protection_sections(safety, targets))
    spec = MeasureSpec(kind="baseline", branch_target_ids=(target,))

    payload = yaml.safe_load(emit_measurement_graph(profile, excited_channels=branch_channels_for(spec)))

    outputs = {o.index: measurement_target_id(o.driver_role, o.output_variant)
               for o in preset.channel_map.outputs}
    assert {entry["dest"]: [source["channel"] for source in entry["sources"]]
            for entry in payload["mixers"]["split_active_2way"]["mapping"]} == {
        index: [0] if target_id == target else [] for index, target_id in outputs.items()}
    assert payload["devices"]["capture"]["channels"] == 2
    assert payload["devices"]["volume_limit"] == 0.0
    if rear:
        assert gs.output_terminally_muted(
            payload, gs.view_from_yaml_dict(payload), 2,
            mute_name="as_out2_rear_pending_mute", mute_gain_db=-120.0,
        ) is (target != "woofer:rear")


#: The seat-equivalent peak, dBFS: the level a summed take plays at (ADR-0361 §1).
SEAT_EQUIVALENT_DBFS = -12.0
#: dB CHECK keeps under it while its graph's level against the anchor is unknown.
BLIND_CUT_DB = 12.0
#: A driver's take plays up to digital full scale under its cap: the seat-equivalent cap is removed (ADR-0403 §4).
DRIVER_CEILING_DBFS = 0.0
#: A probe's first burst at the output, fader plus digital gain (ADR-0405).
PROBE_START_OUTPUT_DBFS = -60.0
#: Fake chains: what the microphone reads for digital full scale at the output, from a seat to 15 mm.
FAKE_CHAINS_DB_SPL = (100.0, 115.0, 125.0, 135.0)
#: A driver's pose at each kind and distance (ADR-0366 §1).
DRIVER_POSES = """[
    {"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.015, "driver": "woofer"},
    {"azimuth_deg": 0, "elevation_deg": 0, "kind": "close", "distance_m": 0.3, "driver": "tweeter"},
    {"azimuth_deg": 20, "elevation_deg": 0, "distance_m": 2.0, "driver": "woofer:rear"},
    {"azimuth_deg": 0, "elevation_deg": 0, "kind": "behind", "distance_m": 0.2, "driver": "woofer:rear"},
    {"azimuth_deg": 0, "elevation_deg": 0, "kind": "seat", "seat_offset_m": [0, 0, 0], "driver": "woofer"}]"""


def _assert_probe_rises_from_minus_60_under_its_ramp_bound(program, fader_db):
    gains = [segment.gain_db for segment in program.stimulus_segments()]
    assert gains[0] == pytest.approx(min(PROBE_START_OUTPUT_DBFS - fader_db, gains[-1]))
    assert all(0.0 < later - earlier <= MAX_STEP_DB + 1e-9 for earlier, later in zip(gains, gains[1:]))
    assert all(gains[0] + fader_db + chain <= ramp_bound_db_spl(85.0) for chain in FAKE_CHAINS_DB_SPL)


@pytest.mark.parametrize("fader_db", [-45.0, -20.0, 0.0])
def test_no_take_plays_above_its_ceiling_at_any_level_asked(fader_db):
    """Every take of every shipped preset and layout, and a driver's pose at each
    kind and distance, at any level asked up to full scale, plays at or under
    that level: one driver alone (a branch take's probes too) at or under full
    scale under that driver's cap, since the seat-equivalent cap is removed
    (ADR-0403 §4); each MEASURE driver at or under its own CHECK plan, moved with
    the level asked, under its cap; and every other take at or under the
    seat-equivalent level under the tightest cap. CHECK, whose graph's level is
    unknown, stays its blind cut lower.

    Every probe's first burst plays at −60 dBFS at the output, or at its ceiling
    when that is lower, and each burst at most 6 dB over the one before. On fake
    chains from a seat to 15 mm, its first burst reads under the ramp bound (76 dB
    under the 85 dB stop), which ends the bursts after it (ADR-0405)."""
    topology, safety, targets = _profile_and_targets(rear=True, woofer_floor=30, woofer_upper=4000,
                                                     max_sweep_duration_s=8)
    bands = {target: resolve_driver_excitation_ceilings(safety, fingerprint, program_admission=True)[0]
             for target, fingerprint in targets.items()}
    caps = {"woofer": 0.0, "woofer:rear": -3.0, "tweeter": -65.0}
    roles = tuple(RoleBand(role, channel, bands[role]) for channel, role in enumerate(("woofer", "tweeter")))
    excitation = SessionExcitation(roles, caps, fader_db, FC_HZ, dict.fromkeys(targets, 8.0), target_bands=bands)
    rng = random.Random(5737)
    asked_levels = (None, -150.0, -40.0, -12.0, 0.0, *(rng.uniform(-100.0, 0.0) for _ in range(6)))
    runs = [(name, layout, None) for name in available_presets() for layout in preset(name).layouts]
    runs += [("drivers/each", None, DRIVER_POSES),
             ("speaker/mark", None, '[{"azimuth_deg": 0, "elevation_deg": 0, "driver": "tweeter"}]')]
    for name, layout, poses in runs:
        plan = run_preset(name, layout, poses)
        request = request_for_preset(plan, mover=plan.mover or "human", targets=near_field_drivers(topology),
                                     candidates=("trial",) if plan.regime == REGIME_BRANCHES else ())
        for played in (each for capture in prepare_plan_captures(request, roles_bands=roles)
                       for each in (capture.spec, *branch_probes(capture.spec))):
            target = solo_target(played)
            measure = played.graph_scope == "drivers" and played.program_phase != journey.PHASE_CHECK and not target
            # A blind take keeps its fixed cut; one whose graph is known plays at the ceiling itself.
            for spec, asked in product((played, replace(played, scope_gains_db={})), asked_levels):
                program = program_for_spec(spec, excitation, GAIN_PLAN_DB, asked,
                                           safety_profile=safety, role_targets=targets)
                blind = spec.scope_gains_db is None and spec.program_phase == journey.PHASE_CHECK
                for segment in program.stimulus_segments():
                    cap_db = caps[target or segment.role] if target or measure else min(caps.values())
                    level_db = (DRIVER_CEILING_DBFS if target else
                                GAIN_PLAN_DB[segment.role] + (0.0 if asked is None else asked - max(GAIN_PLAN_DB.values()))
                                if measure else SEAT_EQUIVALENT_DBFS - (BLIND_CUT_DB if blind else 0.0))
                    ceiling = min(cap_db - fader_db, level_db, math.inf if asked is None else asked)
                    assert segment.gain_db <= ceiling + 1e-9, (name, layout, spec.program_phase, asked, segment.segment_id)
                if is_level_probe(program):
                    _assert_probe_rises_from_minus_60_under_its_ramp_bound(program, fader_db)
