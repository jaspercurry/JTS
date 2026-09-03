# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The take: a session picks up an operator's staged angle walk (#2732 P2).

The half the door was missing. ``tests/test_angle_capture_trigger.py`` covers
staging the document, ``tests/test_angle_capture_seam.py`` covers composing one
into poses, and this covers the one place a SESSION picks it up: who the walk is
declared to be for, that a document is spent exactly once whichever way it goes,
and that a walk the session cannot honour refuses the open rather than quietly
changing its shape (#2879).

What is deliberately NOT re-asserted here is anything those two files own -- the
angle bounds, the pose round trip, the three refusals' own arithmetic. The
second validator is the thing this design exists to avoid.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
from types import SimpleNamespace

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import candidate_bank
from jasper.active_speaker import angle_capture_spool as spool
from jasper.active_speaker import measurement_programs as mp
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.contracts import (
    DRIVER_ROLE_TWEETER,
    MEASURE_KIND_CANDIDATE,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
)
from jasper.active_speaker.crossover_v2.journey import (
    LATERAL_CONSUMER_FC_SELECTOR,
    LATERAL_CONSUMER_FORWARD_MODEL,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.web import correction_crossover_v2 as v2host

CAMPAIGN_ANGLES = [0, 7, -7, 22, -22]
_FC_HZ = 2000.0
_ROLES_BANDS = (
    RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
    RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
)


@pytest.fixture
def slot(tmp_path, monkeypatch):
    """A writable pending slot, and an idle speaker.

    Same fixture shape as the trigger suite's, and for its reason: without the
    volume-state redirect every test here would read the real
    ``/var/lib/jasper`` state of whatever machine runs the suite, and a
    developer's box mid-measurement would fail the suite for a reason that has
    nothing to do with this code.
    """
    spool.set_angle_request_spool_path_for_tests(tmp_path / "angle_request.json")
    monkeypatch.setattr(
        "jasper.active_speaker.session_volume_plan.DEFAULT_SESSION_VOLUME_STATE_PATH",
        tmp_path / "session_volume.json",
    )
    try:
        yield
    finally:
        spool.set_angle_request_spool_path_for_tests(None)


#: Where the design-axis MEASURE capture sits in a stage-1 plan, which is the
#: index a walk's own walk-level spec is keyed to.
_MEASURE_INDEX = 2


def _hand_shape():
    return flow.resolve_plan_shape(flow.TIER_FULL)


def _arm_shape():
    return flow.resolve_plan_shape(flow.TIER_REMOTE)


def _take(
    shape=None, *, base_entries=3, lateral_group_present=False,
    plans_cloud_group=False, preset=None, topology=None,
):
    return v2host._take_staged_angle_walk(
        shape if shape is not None else _hand_shape(),
        base_entries=base_entries,
        lateral_group_present=lateral_group_present,
        plans_cloud_group=plans_cloud_group,
        # Read ONLY by the level-match resolution, which an unmatched walk
        # never reaches — the ordinary walk pays no statefile read.
        preset=preset,
        topology=topology,
    )


def _events(caplog) -> list[str]:
    return [
        rec.getMessage() for rec in caplog.records
        if "crossover_v2_angle_walk" in rec.getMessage()
    ]


def _refused(shape=None, **kwargs) -> str:
    """Take a walk that must REFUSE THE OPEN, and hand back its sentence.

    Every refusal arm raises now (#2879): a staged walk the session cannot
    honour used to journal and return ``None``, and the session then opened in
    its ordinary 3-capture shape — an operator got a measurement that silently
    answered a different question. ``pytest.raises`` here IS that pin.
    """
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        _take(shape, **kwargs)
    return str(excinfo.value)


# --- the ordinary session -----------------------------------------------------


def test_no_staged_walk_is_an_ordinary_session(slot, caplog):
    """Every household session. Nothing taken, nothing said."""
    with caplog.at_level(logging.INFO):
        assert _take() is None
    assert _events(caplog) == []


def test_the_shipped_stage_1_still_plans_no_lateral_group(slot):
    """The retirement is untouched by the take existing.

    With no staged document the session ships no lateral group at all -- so
    the shipped map is the 3-entry shape and the walk's indexes are not in
    it. This is the control every claim below rests on.
    """
    shipped = flow.build_v2_cloud_index_phase_map(
        plan_shape=_hand_shape(),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=False,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    assert PHASE_LATERAL not in shipped.values()
    assert len(shipped) == 3


# --- the take -----------------------------------------------------------------


def test_a_staged_walk_is_taken_once_and_named_as_evidence(slot, caplog):
    """The consumer identity is assigned HERE, not carried in the document.

    Any walk an operator stages is evidence for the offline forward model. A
    document field would be a second writer of the one fact that decides
    which pose table the walk runs, and this is the writer.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    with caplog.at_level(logging.INFO):
        taken = _take()

    assert taken is not None
    prompts, consumer, _specs, _trims, _candidates = taken
    assert consumer == LATERAL_CONSUMER_FORWARD_MODEL
    assert [flow.position_angle_deg(p) for p in prompts] == CAMPAIGN_ANGLES

    line, = _events(caplog)
    assert "crossover_v2_angle_walk_taken" in line
    assert "stops=5" in line
    assert "angles=+0,+7,-7,+22,-22" in line
    assert f"consumer={LATERAL_CONSUMER_FORWARD_MODEL}" in line

    # Single-use: the next session is an ordinary one. The document is spent by
    # the take, not by the session succeeding.
    assert _take() is None


def test_a_peek_reads_the_staged_walk_without_spending_it(slot):
    """The page prices a staged walk before Start; the open is still the take.

    Same reader, same request object -- what a peek does NOT do is empty the
    slot, so a household that reads the price and never presses Start still has
    its walk.
    """
    assert spool.peek_staged_angle_request() is None

    request = ac.request_for_program(mp.program("baseline", "express"))
    spool.stage_angle_request(request)
    assert spool.peek_staged_angle_request() == request
    assert spool.staged_angle_request_pending() is True
    assert spool.peek_staged_angle_request() == request

    assert spool.take_staged_angle_request() == request
    assert spool.staged_angle_request_pending() is False
    assert spool.peek_staged_angle_request() is None


@pytest.mark.parametrize(
    "read", [spool.peek_staged_angle_request, spool.take_staged_angle_request],
)
def test_a_field_the_document_cannot_coerce_refuses_by_name(slot, read):
    """A hand-edited ``delay_us`` is a REFUSAL, not a bare ``ValueError``.

    Both readers, because the page peeks this slot on every poll while only the
    session open takes it: a coercion escaping as ``ValueError`` would take the
    tier chooser down on every poll and 500 the open, instead of costing the
    chooser one offer and refusing the open by name.
    """
    path = spool.angle_request_spool_path()
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["delay_us"] = "12us"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(spool.AngleRequestRefused) as excinfo:
        read()
    assert excinfo.value.reason == spool.SPOOL_MALFORMED


def test_a_staged_walks_stated_price_covers_the_session_that_takes_it(slot):
    """A program's clocks are honoured STRUCTURALLY, not wired a second time.

    The walk's stops enter the plan's ``capture_target``
    (``build_v2_cloud_index_phase_map`` counts ``lateral_prompts``), and the
    session ceiling scales on that target -- so the price stated before Start
    is never under what the session that takes the walk actually budgets.
    """
    express = mp.program("baseline", "express")
    request = ac.request_for_program(express)
    spool.stage_angle_request(request)
    prompts = _take()[0]

    plan = flow.build_v2_capture_plan(
        _ROLES_BANDS, _FC_HZ, plan_shape=_hand_shape(),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=True,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
        lateral_prompts=prompts,
    )
    assert plan.capture_target >= express.capture_count
    session_ceiling_s = flow.session_wall_clock_ceiling_s(plan)
    # The stops are the ONLY entries this walk adds to the base plan, so the
    # stated price is that session's own ceiling rounded up -- not merely a
    # bound over it, and not a base the price counted for itself.
    assert ac.walk_price(request)["ceiling_min"] == math.ceil(session_ceiling_s / 60)
    assert (
        flow.wall_clock_ceiling_s(flow.stage1_base_entries() + len(request.stops))
        == session_ceiling_s
    )
def test_a_refused_walk_refuses_the_open_and_is_consumed(slot, caplog):
    """Fail-closed on BOTH the walk and the session (#2879).

    The refusal is named in the journal AND raised, so an operator who staged a
    walk this session cannot honour is told rather than handed a measurement
    that answers a different question. The document is still spent, so the
    NEXT session is the ordinary one it would have had -- a refusal that
    repeated forever would be its own kind of trap.
    """
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    with caplog.at_level(logging.WARNING):
        sentence = _refused(_hand_shape())

    assert ac.WALK_MOVER_MISMATCH in sentence
    line, = _events(caplog)
    assert "crossover_v2_angle_walk_refused" in line
    assert f"reason={ac.WALK_MOVER_MISMATCH}" in line
    assert "consumed=true" in line and "session_continues=false" in line
    assert spool.staged_angle_request_pending() is False
    assert _take(_hand_shape()) is None


def test_a_document_the_spool_itself_refuses_is_reported_in_its_own_words(
    slot, caplog,
):
    """The refusal vocabulary is the producing module's, never re-worded here.

    A malformed document is the spool's refusal and an incompatible one is the
    seam's; both reach the same journal line with the slug their owner minted,
    so an operator reading it can go straight to the thing that objected.
    """
    spool.angle_request_spool_path().write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        sentence = _refused()

    assert spool.SPOOL_MALFORMED in sentence
    line, = _events(caplog)
    assert f"reason={spool.SPOOL_MALFORMED}" in line


def test_a_banked_stop_past_the_movers_reach_refuses_at_the_take_too(slot, caplog):
    """The door refuses these, and the take re-validates anyway.

    ``walk_over_mover_envelope`` is decided by the request alone, so ``plan`` and
    ``stage`` normally catch it. A document banked before that bound existed --
    or edited on disk, as here -- reaches the take, and the take rebuilds every
    banked document rather than trusting it. Same slug either way, so an
    operator reading the journal is sent to the same place.
    """
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["stops"][0]["angle_deg"] = ac.ARM_ENVELOPE_DEG + 1
    path.write_text(json.dumps(doc), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        sentence = _refused(_arm_shape())

    assert ac.WALK_OVER_MOVER_ENVELOPE in sentence
    line, = _events(caplog)
    assert f"reason={ac.WALK_OVER_MOVER_ENVELOPE}" in line
    assert "consumed=true" in line and "session_continues=false" in line


def test_a_raised_walk_survives_the_spool_and_reaches_the_session(slot):
    """BOTH bearings cross the document, not just the azimuth.

    The spool is the only thing between a stated walk and the session that
    runs it, so an elevation dropped there re-plans the walk as a horizontal
    one — silently, and with the operator's own receipt still saying otherwise.
    """
    spool.stage_angle_request(
        ac.AngleCaptureRequest(
            stops=(
                ac.AngleStop(0, ac.REGIME_PER_DRIVER, 0),
                ac.AngleStop(22, ac.REGIME_PER_DRIVER, 20),
                ac.AngleStop(-22, ac.REGIME_PER_DRIVER, -20),
            ),
            mover=ac.MOVER_HUMAN,
        )
    )
    prompts, _consumer, _specs, _trims, _candidates = _take()

    assert [flow.position_elevation_deg(p) for p in prompts] == [0, 20, -20]
    assert [flow.position_angle_deg(p) for p in prompts] == [0, 22, -22]


def test_a_document_staged_before_elevation_existed_reads_as_mark_height(slot):
    """Additive at the reader, at the SAME schema version.

    The rule the polarity pair already follows: the key is written
    unconditionally and read back with a default, so a document banked before
    the axis was sayable still runs — as the walk at mark height it always was.
    """
    spool.stage_angle_request(ac.per_driver_at([7]))
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    for stop in doc["stops"]:
        del stop["elevation_deg"]
    path.write_text(json.dumps(doc), encoding="utf-8")

    request = spool.take_staged_angle_request()

    assert [stop.elevation_deg for stop in request.stops] == [0]


def test_a_staged_walk_refuses_while_the_session_already_walks_one(slot, caplog):
    """Two lateral groups cannot share one session's index space.

    Also the relay arithmetic: ``MAX_CLOUD_MEASURE_POSITIONS``' own note sizes
    the ceiling for the paused walk's six poses and says not to spend the slack,
    and a second walk on top is exactly that spend.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    with caplog.at_level(logging.WARNING):
        sentence = _refused(lateral_group_present=True)

    assert ac.WALK_LATERAL_GROUP_ALREADY_PLANNED in sentence
    line, = _events(caplog)
    assert f"reason={ac.WALK_LATERAL_GROUP_ALREADY_PLANNED}" in line
    assert "consumed=true" in line
    assert spool.staged_angle_request_pending() is False


def test_the_take_reads_the_sessions_own_mover(slot):
    """The mover check is against THIS session, not against a default.

    The same document is right for one session and wrong for another, which is
    why the pair is judged at the take rather than at staging.
    """
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    taken = _take(_arm_shape())
    assert taken is not None
    assert _arm_shape().externally_positioned is True


# --- what the taken walk composes into ----------------------------------------


def test_the_taken_walk_becomes_the_sessions_map_and_its_prompted_entries(slot):
    """The composed session: the walk's indexes run the lateral phase, and the
    entries the phone renders carry the walk's OWN angle copy.

    Reading the ratified table here instead would prompt the household through
    six spots while the conductor measured five.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    prompts, _consumer, _specs, _trims, _candidates = _take()
    shape = _hand_shape()

    mapping = flow.build_v2_cloud_index_phase_map(
        plan_shape=shape,
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=True,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
        lateral_prompts=prompts,
    )
    assert [i for i, p in sorted(mapping.items()) if p == PHASE_LATERAL] == [
        3, 4, 5, 6, 7
    ]
    assert len(mapping) == 3 + len(prompts)

    plan = flow.build_v2_capture_plan(
        _ROLES_BANDS, _FC_HZ, plan_shape=shape,
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=True,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
        lateral_prompts=prompts,
    )
    lateral_entries = [e for e in plan.entries if e.kind_label == "lateral"]
    assert [e.screen["title"] for e in lateral_entries] == [
        p.headline for p in prompts
    ]
    # Degrees plus a TAP -- the combination no shipped tier can express, which
    # is what the seam exists for. A hand-walked session keeps the angle copy.
    assert "7°" in lateral_entries[1].screen["title"]
    assert lateral_entries[1].screen["auto_advance"] == flow.AUTO_ADVANCE_TAP


def test_an_arm_driven_walk_declares_the_angle_its_gate_waits_for(slot):
    """For an externally positioned session every entry also carries the bearing
    the position gate holds the begin on -- read off the POSE, so the number the
    gate acts on is the number the banked pose carries."""
    spool.stage_angle_request(
        ac.per_driver_at(CAMPAIGN_ANGLES, mover=ac.MOVER_ARM)
    )
    prompts, _consumer, _specs, _trims, _candidates = _take(_arm_shape())
    plan = flow.build_v2_capture_plan(
        _ROLES_BANDS, _FC_HZ, plan_shape=_arm_shape(),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=True,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
        lateral_prompts=prompts,
    )
    lateral_entries = [e for e in plan.entries if e.kind_label == "lateral"]
    assert [
        int(e.screen[flow.POSITION_DEG_KEY]) for e in lateral_entries
    ] == CAMPAIGN_ANGLES
    assert all(
        e.screen["auto_advance"] == flow.AUTO_ADVANCE_COUNTDOWN
        for e in lateral_entries
    )


def test_the_consent_copy_quotes_the_walk_the_household_will_actually_take(slot):
    """A ±45° stop is a metre off a 1 m mark. Quoting the ratified table's 40 cm
    reach at a household about to be walked past it is the dishonesty the
    orientation sentence exists to prevent."""
    spool.stage_angle_request(ac.per_driver_at([0, 45, -45]))
    prompts, _consumer, _specs, _trims, _candidates = _take()
    wide = flow.walk_shape_for(
        cloud_positions=0, lateral=True, lateral_prompts=prompts,
    )
    ratified = flow.walk_shape_for(cloud_positions=0, lateral=True)
    assert wide != ratified
    assert "100 cm" in wide or "110 cm" in wide


# --- R-1's reverse polarity ---------------------------------------------------


def _inverted_walk(**pair):
    return ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
        polarity=POLARITY_INVERTED,
        **pair,
    )


def _played_measure_spec(measure_spec, monkeypatch):
    """The spec the engine leg actually hands ``TuningSession.measure``.

    The leg's play runs under the session's measurement isolation; taking the
    held-window arm keeps this pin off the real coordinator, the same way the
    engine-leg suite's own fixture does.
    """
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(v2host, "_session_abort_target", None)
    played: list = []

    class _Tuning:
        async def measure(self, spec):
            played.append(spec)
            return SimpleNamespace(stimuli=(SimpleNamespace(
                record_id="rec-1", banked=True, incident="", level_db=-22.0,
            ),))

    leg = v2host._bind_engine_measure_leg(
        tuning=_Tuning(),
        stimulus_capture=SimpleNamespace(take_answer=lambda: "the-engine-take"),
        index_phase_map={1: PHASE_MEASURE},
        run_async=asyncio.run,
        specs_by_index={} if measure_spec is None else {1: measure_spec},
    )
    assert leg(1, 1, entry=None) == "the-engine-take"
    one, = played
    return one


def _banked(monkeypatch, **alignment):
    """A banked candidate whose corner is the preset ``_take`` is handed."""
    region = SimpleNamespace(
        fc_hz=2000.0, target_type="LinkwitzRiley", order=4,
        lower_driver="woofer", upper_driver=DRIVER_ROLE_TWEETER,
    )
    preset = SimpleNamespace(crossover_regions=(region,))
    monkeypatch.setattr(
        candidate_bank, "find_banked_candidate",
        lambda fingerprint, **kw: SimpleNamespace(candidate=SimpleNamespace(
            fingerprint=fingerprint, linearization={}, source_preset=preset,
            alignment=SimpleNamespace(**alignment),
        )),
    )
    return preset


def test_a_candidate_stop_banks_under_the_graph_it_actually_played(
    slot, monkeypatch,
):
    """The pose record's claim is the STOP's graph, not the walk's default.

    A reader selecting by ``candidate_id`` is asking what that variant
    measured; a claim that said ``normal`` and ``level_matched=false`` while
    the stop rode a flipped branch through the speaker's own level match would
    answer with a graph that never played.
    """
    preset = _banked(monkeypatch, polarity="invert", delay_role=None, delay_us=None)
    _with_measured_trims(monkeypatch, {DRIVER_ROLE_TWEETER: -9.5})
    spool.stage_angle_request(ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER, 0, "fp-a"),),
        level_matched=True,
    ))
    _prompts, _consumer, specs, _trims, claims = _take(preset=preset)

    spec, = [s for i, s in specs.items() if s.candidate_id]
    claim, = claims
    assert (claim.candidate_id, claim.polarity) == ("fp-a", spec.polarity)
    assert claim.polarity == POLARITY_INVERTED
    assert (claim.level_matched, spec.level_matched) == (True, True)
    assert claim.level_match_trims_db == {DRIVER_ROLE_TWEETER: -9.5}


def test_a_polarity_only_candidate_reaches_a_spec_the_open_accepts(
    slot, monkeypatch,
):
    """A zero delay stated with a branch is a pair ``MeasureSpec`` refuses, and
    at the open the document is already consumed — so the walk must reach a
    spec that builds rather than a ValueError with nothing left to re-stage."""
    preset = _banked(
        monkeypatch, polarity="invert", delay_role=DRIVER_ROLE_TWEETER, delay_us=0.0,
    )
    spool.stage_angle_request(ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER, 0, "fp-a"),),
    ))
    _prompts, _consumer, specs, _trims, _claims = _take(preset=preset)

    spec, = [s for i, s in specs.items() if s.candidate_id]
    assert (spec.delayed_role, spec.delay_us) == ("", 0.0)
    assert spec.inverted_role == DRIVER_ROLE_TWEETER


def test_the_engine_leg_plays_the_spec_its_own_index_names(monkeypatch):
    """One leg, one spec per claimed capture (#3498).

    A stop that names a candidate rides the alignment that candidate was minted
    with, so the leg cannot hand every index one spec. An index no spec names
    and no MEASURE phase claims stays on the flow leg, which is what keeps a
    candidate-free walk on the path it already ran on.
    """
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(v2host, "_session_abort_target", None)
    played: list = []

    class _Tuning:
        async def measure(self, spec):
            played.append(spec)
            return SimpleNamespace(stimuli=(SimpleNamespace(
                record_id="rec-1", banked=True, incident="", level_db=-22.0,
            ),))

    at_pose = MeasureSpec(
        kind=MEASURE_KIND_CANDIDATE, positions=(20,), candidate_id="fp-a",
    )
    leg = v2host._bind_engine_measure_leg(
        tuning=_Tuning(),
        stimulus_capture=SimpleNamespace(take_answer=lambda: "the-engine-take"),
        index_phase_map={1: PHASE_MEASURE, 3: PHASE_LATERAL, 4: PHASE_LATERAL},
        run_async=asyncio.run,
        specs_by_index={3: at_pose},
    )

    assert leg(4, 1, entry=None) is None
    assert leg(3, 1, entry=None) == "the-engine-take"
    assert leg(1, 1, entry=None) == "the-engine-take"
    assert [(s.candidate_id, s.positions) for s in played] == [
        ("fp-a", (20,)), ("", ()),
    ]


def test_a_staged_polarity_reaches_the_engine_legs_measure_spec(slot, monkeypatch):
    """R-1's carry, end to end at the host: document -> take -> engine spec.

    The pair is walk-level because the reverse-null is one act at one place, so
    it names what this session's design-axis MEASURE capture rides rather than
    what happens at a stop. The spec the leg plays is the one ADOPTION built --
    never a second one rebuilt downstream from the same two words, which is how
    the validated pair and the played pair get to differ.
    """
    spool.stage_angle_request(_inverted_walk(inverted_role=DRIVER_ROLE_TWEETER))
    _prompts, _consumer, specs, _trims, _candidates = _take()
    spec = specs[_MEASURE_INDEX]

    played = _played_measure_spec(spec, monkeypatch)
    assert (played.kind, played.polarity, played.inverted_role) == (
        MEASURE_KIND_CANDIDATE, POLARITY_INVERTED, DRIVER_ROLE_TWEETER,
    )

    # …and every ordinary session is untouched: nothing staged, no spec handed
    # over, and the leg plays the bare candidate it always did.
    ordinary = _played_measure_spec(None, monkeypatch)
    assert (ordinary.polarity, ordinary.inverted_role) == (POLARITY_NORMAL, "")


def test_a_staged_confirmation_coordinate_reaches_the_engine_legs_measure_spec(
    slot, monkeypatch
):
    """R-1's DISPOSE half, carried the same road its polarity is: document ->
    take -> engine spec. Dropped anywhere along it, the leg plays an undelayed
    capture and banks it as the coordinate that was asked for."""
    spool.stage_angle_request(_inverted_walk(
        inverted_role=DRIVER_ROLE_TWEETER,
        delayed_role=DRIVER_ROLE_TWEETER,
        delay_us=250.0,
    ))
    _prompts, _consumer, specs, _trims, _candidates = _take()
    spec = specs[_MEASURE_INDEX]

    played = _played_measure_spec(spec, monkeypatch)
    assert (played.delayed_role, played.delay_us) == (DRIVER_ROLE_TWEETER, 250.0)

    ordinary = _played_measure_spec(None, monkeypatch)
    assert (ordinary.delayed_role, ordinary.delay_us) == ("", 0.0)


def _with_measured_trims(monkeypatch, trims, source="banked_base_trim"):
    """Answer the level-match evidence question with a stated verdict.

    Patched at the HOST's own door rather than at ``baseline_profile``, so this
    pins that adoption asks the question and carries the answer; whether the
    precedence behind it is right is that module's own subject and has its own
    tests. ``{}`` is the box with no measured evidence at all.
    """
    monkeypatch.setattr(
        v2host, "_resolve_measurement_level_trims",
        lambda spec, *, preset, topology: (
            (dict(trims), source) if spec.level_matched else ({}, "")
        ),
    )


def test_a_level_matched_walk_carries_the_boxs_own_trims_to_the_session(
    slot, monkeypatch, caplog,
):
    """The values are resolved at ADOPTION and travel from there: the spec says
    only WHETHER, and the numbers reach the session that installs the graph.
    Resolving them a second time downstream would be a second answer to one
    question."""
    _with_measured_trims(monkeypatch, {DRIVER_ROLE_TWEETER: -9.5})
    spool.stage_angle_request(_inverted_walk(
        inverted_role=DRIVER_ROLE_TWEETER, level_matched=True,
    ))
    with caplog.at_level(logging.INFO):
        _prompts, _consumer, specs, trims, _candidates = _take()
        spec = specs[_MEASURE_INDEX]

    assert spec.level_matched is True
    assert trims == {DRIVER_ROLE_TWEETER: -9.5}
    # WHICH evidence answered rides the journal, so a take's receipts name the
    # source of the gains its graph carries.
    line, = _events(caplog)
    assert "level_match_source=banked_base_trim" in line


def test_an_ordinary_walk_resolves_no_trims_and_reads_no_evidence(
    slot, monkeypatch,
):
    """The ordinary session pays nothing: no statefile read, no preview load."""
    asked: list[object] = []

    def _spy(spec, *, preset, topology):
        asked.append(spec)
        return {}, ""

    monkeypatch.setattr(v2host, "_resolve_measurement_level_trims", _spy)
    spool.stage_angle_request(ac.per_driver_at([0]))
    _prompts, _consumer, specs, trims, _candidates = _take()
    spec = specs[_MEASURE_INDEX]

    assert spec.level_matched is False and trims == {}
    # Called once and answered empty — the real resolver short-circuits on the
    # flag before it opens anything.
    assert len(asked) == 1


def test_a_level_match_with_no_evidence_refuses_the_open_under_its_own_slug(
    slot, monkeypatch,
):
    """The two honest arms are refusing and measuring unmatched under a record
    that says matched. The second is the S12 lie, so this refuses — with its
    OWN slug, so an operator reading ``reason=`` learns the box needs a driver
    trim rather than being told something about polarity."""
    _with_measured_trims(monkeypatch, {})
    spool.stage_angle_request(_inverted_walk(
        inverted_role=DRIVER_ROLE_TWEETER, level_matched=True,
    ))
    sentence = _refused()

    assert ac.WALK_LEVEL_MATCH_NO_EVIDENCE in sentence
    assert ac.WALK_POLARITY_NOT_ACCEPTED not in sentence
    assert ac.WALK_DELAY_NOT_ACCEPTED not in sentence
    assert ac.WALK_LEVEL_MATCH_NO_EVIDENCE in ac.WALK_REFUSAL_REASONS


def test_a_walk_asking_for_no_level_match_never_refuses_on_evidence(
    slot, monkeypatch,
):
    """A box with no measured trims is an ordinary box. Only a walk that ASKED
    for the level match may be refused for the absence of one."""
    _with_measured_trims(monkeypatch, {})
    spool.stage_angle_request(ac.per_driver_at([0]))

    assert _take() is not None


def test_a_genuinely_empty_box_refuses_no_evidence_through_the_real_resolver(
    slot, monkeypatch,
):
    """End to end through the REAL wiring, no seam mock: the real
    :func:`~jasper.web.correction_crossover_v2._resolve_measurement_level_trims`
    calling the real
    :func:`~jasper.active_speaker.baseline_profile.measured_level_trims` over a
    genuinely empty box (loaders stubbed to empty documents, no banked base
    trim, no guided captures).

    This is the wall a virgin blind-run box hits when it stages a level-matched
    reverse-null before it has measured its per-driver trims — the refusal that
    teaches it to measure and apply a level match first. Pinning it through the real
    path, not a resolver mock, is what makes that lesson real: a regression that
    let an empty box resolve non-empty trims would sail past every test that
    stubs the resolver.
    """
    _stub_evidence_loaders(monkeypatch)  # loaders empty; resolver + owner REAL
    preset = SimpleNamespace(
        way_count=2,
        crossover_regions=(
            SimpleNamespace(
                lower_driver="woofer", upper_driver="tweeter", fc_hz=2000.0,
            ),
        ),
    )
    spool.stage_angle_request(_inverted_walk(
        inverted_role=DRIVER_ROLE_TWEETER, level_matched=True,
    ))
    sentence = _refused(preset=preset)

    assert ac.WALK_LEVEL_MATCH_NO_EVIDENCE in sentence



def _stub_evidence_loaders(monkeypatch):
    """The two banked documents the owner is handed, stubbed to empty.

    Their CONTENT is the owner's subject, not this seam's; what these tests pin
    is that this resolver asks that owner and carries its verdict.
    """
    from jasper.active_speaker import crossover_preview, measurement

    monkeypatch.setattr(measurement, "load_measurement_state", lambda _t: {})
    monkeypatch.setattr(
        crossover_preview, "load_crossover_preview", lambda *a, **k: {}
    )


def test_the_resolver_asks_the_ONE_owner_and_states_which_evidence_answered(
    monkeypatch,
):
    """Precedence has one owner. This resolver hands that owner the same two
    inputs the applied profile's own build hands it and reports its verdict —
    it does not re-rank banked against guided, and it does not substitute a
    datasheet estimate for a measurement of this cabinet."""
    from jasper.active_speaker import baseline_profile

    seen: list[object] = []

    def _owner(preset, measurements, crossover_preview=None):
        seen.append((preset, measurements, crossover_preview))
        return {DRIVER_ROLE_TWEETER: -9.5}, {"source": "guided_captures"}

    monkeypatch.setattr(baseline_profile, "measured_level_trims", _owner)
    _stub_evidence_loaders(monkeypatch)
    trims, source = v2host._resolve_measurement_level_trims(
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, level_matched=True),
        preset=object(), topology=None,
    )

    assert trims == {DRIVER_ROLE_TWEETER: -9.5}
    assert source == "guided_captures"
    assert len(seen) == 1


def test_the_resolver_answers_empty_for_a_walk_that_asked_for_no_level_match(
    monkeypatch,
):
    """The short circuit is the flag, before any read: an ordinary session must
    not pay a statefile read or a preview load for a feature it did not use."""
    from jasper.active_speaker import baseline_profile

    def _never(*_args, **_kwargs):
        raise AssertionError("an unmatched walk must ask no evidence question")

    monkeypatch.setattr(baseline_profile, "measured_level_trims", _never)

    assert v2host._resolve_measurement_level_trims(
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE), preset=None, topology=None,
    ) == ({}, "")


def test_an_unexpected_resolve_fault_propagates_instead_of_masquerading(
    monkeypatch,
):
    """There is no catch here, and that is the point. The loaders fail soft — an
    absent, unreadable or corrupt-but-readable document returns a status dict,
    never a raise — and the estimator is fail-closed, so a box with nothing to
    level by reaches the caller as EMPTY trims (the e2e test above pins that
    whole path). No exception is expected at all, so an exception that DOES
    arise is a real fault in the derivation; swallowing it to answer empty would
    misdirect the operator to "run the driver trim step" over a bug. With no
    catch it PROPAGATES, its traceback pointing straight at this function."""
    from jasper.active_speaker import baseline_profile

    class _Boom(RuntimeError):
        pass

    def _blows_up(*_args, **_kwargs):
        raise _Boom("a real defect in the derivation")

    monkeypatch.setattr(baseline_profile, "measured_level_trims", _blows_up)
    _stub_evidence_loaders(monkeypatch)

    with pytest.raises(_Boom):
        v2host._resolve_measurement_level_trims(
            MeasureSpec(kind=MEASURE_KIND_CANDIDATE, level_matched=True),
            preset=object(), topology=None,
        )


def test_a_coordinate_the_spec_refuses_is_named_as_a_DELAY_refusal(slot):
    """Its own slug, so an operator reading ``reason=`` learns which half of
    R-1 was refused rather than being told 'polarity' about a delay."""
    spool.stage_angle_request(_inverted_walk(
        inverted_role=DRIVER_ROLE_TWEETER,
        delayed_role="tweater",          # not a driver branch
        delay_us=250.0,
    ))
    sentence = _refused()

    assert ac.WALK_DELAY_NOT_ACCEPTED in sentence
    assert ac.WALK_POLARITY_NOT_ACCEPTED not in sentence
    # The detail is the spec's own refusal, compared against what the spec
    # actually raises rather than a copy of its wording.
    with pytest.raises(ValueError) as spec_refusal:
        MeasureSpec(
            kind=MEASURE_KIND_CANDIDATE,
            polarity=POLARITY_INVERTED,
            inverted_role=DRIVER_ROLE_TWEETER,
            delayed_role="tweater",
            delay_us=250.0,
        )
    assert str(spec_refusal.value) in sentence


def test_a_one_sided_polarity_refuses_the_open_in_the_specs_own_words(slot, caplog):
    """The pair is judged by the SPEC, at adoption, and nowhere upstream.

    Staging is a dumb carrier on purpose — one copy of the rule, in
    ``MeasureSpec`` — so a walk naming ``inverted`` and no branch does reach
    the host. It refuses the open there, like every other walk this session
    cannot honour, rather than raising out of a capture callback mid-round.
    """
    spool.stage_angle_request(_inverted_walk())
    with caplog.at_level(logging.WARNING):
        sentence = _refused()

    assert ac.WALK_POLARITY_NOT_ACCEPTED in sentence
    # The DETAIL is the spec's own refusal. Compared against what the spec
    # actually raises rather than against a copy of its wording, so this pin
    # cannot become the second vocabulary it exists to forbid.
    with pytest.raises(ValueError) as spec_refusal:
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, polarity=POLARITY_INVERTED)
    assert str(spec_refusal.value) in sentence

    line, = _events(caplog)
    assert f"reason={ac.WALK_POLARITY_NOT_ACCEPTED}" in line
    assert "consumed=true" in line and "session_continues=false" in line
    assert spool.staged_angle_request_pending() is False


# --- one take, four surfaces --------------------------------------------------


def test_the_preparer_feeds_map_spec_and_conductor_from_one_take():
    """ONE take, read by everything that must agree about the walk.

    Source-read rather than driven, for the reason the sibling tier pin gives:
    driving ``_open`` needs a live relay. What matters is the wiring -- a second
    take would hand the map and the spec different walks, and a surface left
    unthreaded would render one walk while the conductor ran another.

    Dropping the CONSUMER thread also fails closed at RUNTIME, because
    ``validated_lateral_consumer`` refuses a session handed a pose table with
    the selector consumer. This pin catches that earlier and by name.
    """
    source = inspect.getsource(v2host.prepare_v2_session)
    assert source.count("_take_staged_angle_walk(") == 1
    assert "lateral_prompts=lateral_prompts" in source
    assert "lateral_consumer=lateral_consumer" in source
    # The map, the spec, and the conductor: three readers, one local.
    assert source.count("lateral_prompts=lateral_prompts") == 3
    # ...and the default when nothing is staged is the ratified walk's owner.
    assert f"lateral_consumer = {LATERAL_CONSUMER_FC_SELECTOR!s}" not in source
    assert "lateral_consumer = LATERAL_CONSUMER_FC_SELECTOR" in source
    # The take is fed the session's own shape, never a default one.
    assert "base_entries=len(stage1_index_phase)" in source
    assert "lateral_group_present=include_lateral" in source

# --- the take opens the session, whatever the document does -------------------


def test_a_stop_the_seam_can_no_longer_build_refuses_instead_of_escaping(
    slot, caplog,
):
    """A hand-edited angle reaches the take as the seam's OWN exception.

    ``take_staged_angle_request`` deliberately re-raises the seam's own
    ``CrossoverV2FlowError`` un-wrapped for a banked stop that no longer
    satisfies the contract, because ``_validated_angle``'s sentence beats a
    second vocabulary. That is a third refusal class, and it reaches
    ``prepare_v2_session`` — so it is caught here, given the slug it arrived
    without, and re-raised as the host's own refusal rather than escaping as a
    flow error nothing on this path claims.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["stops"][1]["angle_deg"] = 999
    path.write_text(json.dumps(doc), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        sentence = _refused()

    assert ac.WALK_STOP_NO_LONGER_VALID in sentence
    line, = _events(caplog)
    assert f"reason={ac.WALK_STOP_NO_LONGER_VALID}" in line
    # The producing module's own sentence survives as the detail rather than
    # being re-worded by a second validator.
    assert "+999 deg" in line
    assert "consumed=true" in line
    assert spool.staged_angle_request_pending() is False


def test_consumed_is_read_back_from_the_spool_not_asserted(slot, caplog):
    """The spool's two unreadable arms deliberately do NOT consume.

    A permissions mistake must refuse every session until it is fixed rather
    than destroying the only evidence of itself — so the journal has to say what
    the spool actually did. Asserting ``consumed=true`` here would have told an
    operator their document was spent while it sat on disk, refusing forever.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    path = spool.angle_request_spool_path()
    os.chmod(path, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            _refused()
            _refused()
        assert path.is_file(), "the unreadable arm must not consume"
        assert [line for line in _events(caplog) if "consumed=false" in line]
        assert not [line for line in _events(caplog) if "consumed=true" in line]
    finally:
        os.chmod(path, 0o600)


def test_a_taken_walk_still_says_it_was_consumed(slot, caplog):
    """The control for the pin above: an ordinary refusal DID consume, and says
    so — so ``consumed`` is a read, not a constant in either direction."""
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    with caplog.at_level(logging.WARNING):
        _refused(_hand_shape())
    line, = _events(caplog)
    assert "consumed=true" in line
    assert spool.staged_angle_request_pending() is False


def test_the_silent_shape_change_is_gone_from_the_take(slot):
    """The pin on the DELETED behaviour, at the source (#2879).

    Every refusal arm used to end in ``return None``, and ``None`` is what the
    preparer reads as "no walk staged" — so a refused walk and an ordinary
    session were the same value, and the session opened in its ordinary
    3-capture shape with only a WARNING nobody was reading to say otherwise.
    The behavioural pins above cover each arm; this one says the SHAPE cannot
    come back: exactly one ``return None`` survives, and it is the arm that
    genuinely means "nothing was staged".
    """
    source = inspect.getsource(v2host._take_staged_angle_walk)
    body = source.split('"""', 2)[-1]
    assert body.count("return None") == 1
    assert "if request is None:\n            return None" in body
    # ...and the journal says so too, rather than claiming the session lives on.
    assert "session_continues=False" in body
    assert "session_continues=True" not in body


def test_the_take_reads_the_sessions_own_cloud_shape(slot):
    """The capacity gate needs this session's retake budget, not a guess.

    A cloud group costs two more relay indexes than a cloud-less session of the
    same length, because only a cloud budgets geometry retakes. The stop count
    below is chosen so that fact is the ONLY thing separating the two calls:
    same document length, same ``base_entries``, opposite verdicts. Anything
    less discriminating passes with the flag ignored — the first version of this
    test did.
    """
    stops = list(range(1, 16))  # 11 + 15 + 5 = 31 fits; + 7 = 33 does not
    spool.stage_angle_request(ac.per_driver_at(stops))
    assert _take(base_entries=11, plans_cloud_group=False) is not None

    spool.stage_angle_request(ac.per_driver_at(stops))
    assert ac.WALK_OVER_RELAY_CAPACITY in _refused(
        base_entries=11, plans_cloud_group=True,
    )


def test_the_unprefixed_spool_refusal_reasons_name_is_gone():
    """This module's own member of the two-file ``SPOOL_REFUSAL_REASONS``
    collision with :mod:`.crossover_v2.prescription_spool` — renamed to
    :data:`~jasper.active_speaker.angle_capture_spool.ANGLE_SPOOL_REFUSAL_REASONS`
    so importing both modules unqualified cannot shadow one vocabulary with
    the other. The bare name must not still be an attribute of this module.
    """
    assert not hasattr(spool, "SPOOL_REFUSAL_REASONS")
    assert spool.ANGLE_SPOOL_REFUSAL_REASONS == frozenset({
        spool.SPOOL_MALFORMED,
        spool.SPOOL_TOO_LARGE,
        spool.SPOOL_TOO_MANY_STOPS,
        spool.SESSION_ALREADY_LIVE,
    })
