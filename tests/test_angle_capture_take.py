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
import json
import logging
import math
import os
from dataclasses import asdict, replace
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
    MEASURE_KIND_VERIFY,
    POLARITY_INVERTED,
    POLARITY_NORMAL,
)
from jasper.active_speaker.crossover_v2.journey import (
    LATERAL_CONSUMER_FORWARD_MODEL,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.round_captures import doc_pose_key
from jasper.active_speaker.crossover_v2 import door
from jasper.active_speaker.crossover_v2.wired_stimulus import (
    CapturedRecordStore, WiredCaptureAnswer,
)
from jasper.audio_measurement import gating
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.playback import PlaybackObservation
from jasper.audio_measurement.program import RoleBand
from jasper.web import correction_crossover_v2 as v2host
from tests.crossover_v2_fixtures import FakeSeams, _conductor, _run_phase, bank_into
from tests.test_bass_extension_dynamic import _descriptor

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


#: A measurement mic whose registry row names the channel an SPL watch reads.
_MIC = SimpleNamespace(model_key="minidsp_umik2")


def _take_full(
    shape=None, *, base_entries=3, lateral_group_present=False,
    plans_cloud_group=False, preset=None, topology=None, device=_MIC,
):
    return v2host._take_staged_angle_walk(
        shape if shape is not None else _hand_shape(),
        base_entries=base_entries,
        lateral_group_present=lateral_group_present,
        plans_cloud_group=plans_cloud_group,
        # Read ONLY by the level-match resolution and by a STATED SPL ceiling,
        # neither of which an ordinary walk reaches — it pays no statefile read.
        preset=preset,
        topology=topology,
        device=device,
    )


def _take(shape=None, **kwargs):
    """The walk's own shape. The SPL watch the take also returns is pinned by
    ``test_a_stated_ceiling_buys_a_watch_or_refuses_the_open`` alone."""
    taken = _take_full(shape, **kwargs)
    return taken if taken is None else taken[:5]


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
    doc["template"]["delay_us"] = "12us"
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

    Also the capture arithmetic: ``MAX_CLOUD_MEASURE_POSITIONS``' own note sizes
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


def _inverted_walk(**template):
    return ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, polarity=POLARITY_INVERTED, **template,
        ),
    )


def _engine_leg(monkeypatch, phase_map, specs=None, prompts=None):
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(v2host, "_session_abort_target", None)
    played, pending = [], []
    answer = WiredCaptureAnswer(wav=b"capture", wav_path="captures/take.wav")

    class _Store:
        async def bank(self, record):
            return "rec-1"

    capture = SimpleNamespace(take_answer=lambda: pending.pop() if pending else None)
    records = CapturedRecordStore(_Store(), capture)

    class _Tuning:
        session_id = "angle-walk"
        last_playback = PlaybackObservation()
        banked_record_ids = ()

        async def restore_graph(self):
            return None

        async def measure(self, spec):
            played.append(spec)
            pending.append(answer)
            record_id = await records.bank({
                "candidate_id": spec.candidate_id, "graph_scope": spec.graph_scope,
            })
            return SimpleNamespace(stimuli=(SimpleNamespace(
                record_id=record_id, banked=True, incident="", level_db=-22.0,
            ),))

    def consume(index, attempt, captured):
        assert captured is answer
        return {"accepted": True, "index": index, "attempt": attempt}

    return v2host._bind_engine_measure_leg(
        tuning=_Tuning(), stimulus_capture=capture, records=records,
        conductor=SimpleNamespace(
            consume_capture=consume, note_take_banked=lambda record: None,
            _prompt_shown_for=lambda phase, index: (prompts or {}).get(index, ac.pose_at_angle(0)),
        ),
        retention=SimpleNamespace(pending={}, enrich=lambda *args: {}, after_bank=lambda *args: None),
        index_phase_map=phase_map, run_async=lambda coro, **kwargs: asyncio.run(coro), specs_by_index=specs,
    ), played


def _played_measure_spec(measure_spec, monkeypatch):
    leg, played = _engine_leg(
        monkeypatch, {1: PHASE_MEASURE},
        {} if measure_spec is None else {1: measure_spec},
    )
    assert leg(1, 1, entry=None) == {"accepted": True, "index": 1, "attempt": 1}
    one, = played
    return one


def _banked(monkeypatch, *, room_correction=None, bass_extension=None, **alignment):
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
            room_correction=room_correction or {}, bass_extension=bass_extension or {},
            alignment=SimpleNamespace(**alignment),
        )),
    )
    return preset


@pytest.mark.parametrize("overlay", [
    {"level_matched": True},
    {"polarity": POLARITY_INVERTED, "inverted_role": DRIVER_ROLE_TWEETER},
    {"delayed_role": DRIVER_ROLE_TWEETER, "delay_us": 250.0},
])
@pytest.mark.parametrize("candidate_id", ["", "fp-a"])
def test_a_complete_graph_trial_refuses_walk_overlays(overlay, candidate_id):
    """A summed trial plays the selected graph's own trims and alignment, so a
    template overlay is refused where the walk is stated, not at the open."""
    with pytest.raises(ac.LateralWalkRefused) as excinfo:
        ac.AngleCaptureRequest(
            stops=(ac.AngleStop(0, ac.REGIME_SUMMED, 0, candidate_id),),
            template=ac.walk_template(kind=MEASURE_KIND_CANDIDATE, **overlay),
        )
    assert excinfo.value.reason == ac.WALK_CANDIDATE_NOT_MEASURABLE


@pytest.mark.parametrize("candidate_ids", [None, ("",), ("", "fp-a", "fp-b")])
def test_staged_walk_composes_and_analyzes_each_declared_graph(
    slot, monkeypatch, tmp_path, candidate_ids,
):
    preset = _banked(monkeypatch)
    regime = ac.REGIME_PER_DRIVER if candidate_ids is None else ac.REGIME_SUMMED
    spool.stage_angle_request(ac.AngleCaptureRequest(stops=tuple(
        ac.AngleStop(20, regime, 5, cid) for cid in (candidate_ids or ("",))
    )))
    prompts, consumer, specs, _trims, claims = _take(preset=preset)
    index_phases = flow.build_v2_cloud_index_phase_map(
        plan_shape=_hand_shape(), include_cloud_measure=False,
        include_lateral=True, lateral_prompts=prompts,
    )
    fakes = FakeSeams()
    records, analyzed_programs = [], []
    seams = fakes.seams()

    def analyze(program, *args, **kwargs):
        analyzed_programs.append(program)
        return seams.analyze(program, *args, **kwargs)

    conductor = _conductor(
        fakes, index_phase_map=index_phases, lateral_prompts=prompts,
        lateral_consumer=consumer, lateral_claims=claims,
        measure_specs_by_index=specs,
        seams=replace(seams, analyze=analyze, bank_take=bank_into(records, phase=PHASE_LATERAL)),
    )
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)
    monkeypatch.setattr(
        door, "bind_measurement_graph",
        lambda *a, **kw: SimpleNamespace(installed_graph_yaml=lambda: "graph: scoped\n"),
    )
    playback = v2host.bind_production_play(
        camilla_factory=object,
        evidence_store=SimpleNamespace(
            bundle_dir=tmp_path,
            identify_artifact=lambda rel: SimpleNamespace(fingerprint="fixture"),
        ),
        capture_session_id="scope-walk", topology=object(), preset=preset,
        role_channels={"woofer": 0, "tweeter": 1}, playback_device="hw:Test",
        safety_profile={}, role_targets={}, session_volume_db=-22,
        program_for_phase=conductor.program_for_phase,
    )
    lateral_indexes = [i for i, phase in index_phases.items() if phase == PHASE_LATERAL]
    for index, cid in zip(lateral_indexes, candidate_ids or ("",)):
        spec = replace(specs.get(index, MeasureSpec(kind="candidate")), program_phase=PHASE_LATERAL)
        expected_scope = "drivers" if candidate_ids is None else "candidate" if cid else "base"
        assert (spec.graph_scope, spec.candidate_id) == (expected_scope, cid)
        prepared = asyncio.run(playback.compose(spec=spec))
        verdict = _run_phase(conductor, index, 1)
        assert verdict["accepted"] is True
        assert analyzed_programs[-1] is prepared.program
        assert prepared.program is conductor.program_for_phase(
            PHASE_MEASURE if candidate_ids is None else flow.PHASE_CLOUD_MEASURE,
        )
    expected_roles = {"woofer", "tweeter"} if candidate_ids is None else {"summed"}
    assert [{curve["role"] for curve in record["curves"]} for record in records] == [expected_roles] * len(lateral_indexes)
    assert [record["candidate_id"] for record in records] == list(candidate_ids or ("",))
    assert len(list((tmp_path / "crossover_v2/scope-walk").glob("*.wav"))) == len(lateral_indexes)


def _seat_index_phases(prompts):
    return flow.build_v2_cloud_index_phase_map(
        plan_shape=_hand_shape(), include_cloud_measure=False,
        include_lateral=True, lateral_prompts=prompts,
    )


def test_a_seat_walk_reaches_the_session_and_plays_the_applied_tune_whole(slot):
    """A seat stop names no candidate, so it plays the APPLIED tune WHOLE.

    Which is the VERIFY shape at the speaker's own graph, never the base or
    candidate scope a summed bearing walk selects: the room is measured
    through the speaker stage it sits on. The pose the household is reading is
    the pose that spec carries, in the program's own order.
    """
    program = mp.program("seat", "express")
    spool.stage_angle_request(ac.request_for_program(program))

    prompts, _consumer, specs, _trims, _claims = _take()

    assert [(prompt.kind, prompt.seat_offset_m) for prompt in prompts] == [
        (pose.kind, pose.seat_offset_m) for pose in program.poses
    ]
    lateral_indexes = [
        index for index, phase in _seat_index_phases(prompts).items()
        if phase == PHASE_LATERAL
    ]
    assert [
        (specs[index].kind, specs[index].graph_scope, specs[index].candidate_id,
         specs[index].pose_prompts)
        for index in lateral_indexes
    ] == [
        (MEASURE_KIND_VERIFY, "speaker_tune", "", (prompt.text,))
        for prompt in prompts
    ]


@pytest.mark.parametrize("program_id,coverage", [("seat", "express"), ("seat", "cube"), ("seat", "cloud"), ("room", "quick")])
@pytest.mark.parametrize("room_candidate", [False, True])
def test_a_seat_take_is_analyzed_ungated_and_banks_its_kind(
    slot, monkeypatch, program_id, coverage, room_candidate,
):
    room_roles = [
        RoleBand("woofer", 0, FrequencyBand(60.0, 4000.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
    ]
    program = mp.program(program_id, coverage)
    preset = _banked(monkeypatch, room_correction={"filters": []}) if room_candidate else None
    candidate_ids = ("room-fp",) if room_candidate else ()
    spool.stage_angle_request(ac.request_for_program(program, candidates=candidate_ids))
    prompts, consumer, specs, _trims, claims = _take(preset=preset)
    index_phases = _seat_index_phases(prompts)
    fakes = FakeSeams()
    records, analyses = [], []
    seams = fakes.seams()

    def analyze(*args, **kwargs):
        analyses.append(seams.analyze(*args, **kwargs))
        return analyses[-1]

    conductor = _conductor(
        fakes, roles_bands=room_roles, fc_hz=_FC_HZ,
        index_phase_map=index_phases, lateral_prompts=prompts,
        lateral_consumer=consumer, lateral_claims=claims,
        measure_specs_by_index=specs,
        seams=replace(
            seams, analyze=analyze,
            bank_take=bank_into(records, phase=PHASE_LATERAL),
        ),
    )
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)
    lateral_indexes = [
        index for index, phase in index_phases.items() if phase == PHASE_LATERAL
    ]
    room_sweep = next(
        segment for segment in conductor.program_for_phase(PHASE_LATERAL).segments
        if segment.kind == "summed_sweep"
    )
    assert (room_sweep.f1_hz, room_sweep.f2_hz) == (20.0, 20000.0)
    capture_plan = flow.build_v2_capture_plan(
        room_roles, _FC_HZ, plan_shape=_hand_shape(),
        include_cloud_measure=False, include_lateral=True,
        lateral_prompts=prompts,
        lateral_candidate_ids=(
            ("room-fp",) * len(prompts) if room_candidate else None
        ),
    )
    room_duration_ms = (
        conductor.program_for_phase(PHASE_LATERAL).total_samples * 1000
        / conductor.program_for_phase(PHASE_LATERAL).sample_rate_hz
    )
    assert all(
        entry.duration_ms >= room_duration_ms
        for entry in capture_plan.entries if entry.kind_label == "lateral"
    )
    for index in lateral_indexes:
        assert _run_phase(conductor, index, 1)["accepted"] is True

    assert [geometry.gate_exempt_reason for *_head, geometry in fakes.analyzed] == (
        [None, None] + [gating.SEAT_EXEMPT] * len(lateral_indexes)
    )
    assert [
        (record.get("pose_kind", mp.POSE_KIND_BEARING), record.get("seat_offset_m"), record["mark_distance_m"],
         record["gating_applied"])
        for record in records
    ] == [
        (pose.kind, list(pose.seat_offset_m) if pose.seat_offset_m is not None else None,
         None if pose.kind == mp.POSE_KIND_SEAT else flow.MARK_DISTANCE_M,
         bool(analysis.summed_response.gating["applied"]))
        for pose, analysis in zip(program.poses, analyses[-len(records):])
    ]
    assert {record["measurement_purpose"] for record in records} == {mp.PURPOSE_ROOM}
    assert len({doc_pose_key(record) for record in records}) == len(records)
    assert [specs[index].graph_scope for index in lateral_indexes] == [
        "room_candidate" if room_candidate else "speaker_tune"
    ] * len(program.poses)
    assert [record["candidate_id"] for record in records] == [
        "room-fp" if room_candidate else ""
    ] * len(program.poses)
    assert {
        tuple(curve["band_hz"])
        for record in records for curve in record["curves"]
    } == {(20.0, 20000.0)}

    speaker_prompts = tuple(
        replace(prompt, purpose=mp.PURPOSE_SPEAKER) for prompt in prompts
    )
    speaker = _conductor(
        FakeSeams(), roles_bands=room_roles, fc_hz=_FC_HZ,
        index_phase_map=index_phases, lateral_prompts=speaker_prompts,
        lateral_consumer=LATERAL_CONSUMER_FORWARD_MODEL,
        measure_specs_by_index=specs,
    )
    speaker_sweep = next(
        segment for segment in speaker.program_for_phase(PHASE_LATERAL).segments
        if segment.kind == "summed_sweep"
    )
    assert speaker_sweep.f1_hz == 150.0


@pytest.mark.parametrize("delay_us", [0.0, 250.0])
@pytest.mark.parametrize("bass_extension,scope", [
    ({}, "candidate"), (asdict(_descriptor()), "bass_candidate"),
])
def test_a_candidate_stop_selects_the_complete_graph_at_its_pose(
    slot, monkeypatch, delay_us, bass_extension, scope,
):
    preset = _banked(
        monkeypatch, polarity="invert", delay_role=DRIVER_ROLE_TWEETER,
        delay_us=delay_us, bass_extension=bass_extension,
    )
    spool.stage_angle_request(ac.AngleCaptureRequest(
        stops=(ac.AngleStop(20, ac.REGIME_SUMMED, 5, "fp-a"),),
    ))
    prompts, _consumer, specs, trims, claims = _take(preset=preset)

    spec, = [s for i, s in specs.items() if s.candidate_id]
    assert (spec.graph_scope, spec.candidate_id, claims[0].candidate_id) == (
        scope, "fp-a", "fp-a",
    )
    assert (spec.positions, spec.vertical_deg, spec.pose_prompts) == (
        (20,), 5, (prompts[0].text,),
    )
    assert (spec.delayed_role, spec.delay_us) == ("", 0.0)
    assert (spec.polarity, spec.inverted_role, spec.level_matched, trims) == (
        POLARITY_NORMAL, "", False, {},
    )


def test_the_engine_leg_plays_the_spec_its_own_index_names(monkeypatch):
    at_pose = MeasureSpec(
        kind=MEASURE_KIND_CANDIDATE, positions=(20,), candidate_id="fp-a", graph_scope="candidate",
    )
    leg, played = _engine_leg(
        monkeypatch, {1: PHASE_MEASURE, 3: PHASE_LATERAL, 4: PHASE_LATERAL}, {3: at_pose},
        {3: ac.pose_at_angle(20, 5), 4: ac.pose_at_angle(-7)},
    )

    for attempt, index in enumerate((4, 3, 1), start=1):
        assert leg(index, attempt, entry=None) == {"accepted": True, "index": index, "attempt": attempt}
    assert [(s.graph_scope, s.candidate_id, s.positions, s.program_phase) for s in played] == [
        ("drivers", "", (-7,), PHASE_LATERAL),
        ("candidate", "fp-a", (20,), PHASE_LATERAL),
        ("drivers", "", (), PHASE_MEASURE),
    ]
    assert played[1].vertical_deg == 5
    assert played[1].pose_prompts == (ac.pose_at_angle(20, 5).text,)


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


def test_a_staged_stimulus_reaches_every_spec_the_walk_plays(slot, monkeypatch):
    """Request-level, so EVERY spec this walk plays carries it -- the design-axis
    MEASURE capture and each stop alike. Dropped between the document and the
    spec, the session would play the program's own single stimulus and bank it
    as the matched batch the operator staged.
    """
    ladder = (-20.0, -14.0)
    spool.stage_angle_request(replace(
        ac.summed_at([0, 7]),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, level_ladder_dbfs=ladder,
        ),
    ))
    _prompts, _consumer, specs, _trims, _claims = _take()

    # Both construction sites, named by the scope only each one builds: the
    # walk-level design-axis spec and the per-stop summed specs.
    assert {spec.graph_scope for spec in specs.values()} == {"drivers", "base"}
    assert {spec.level_ladder_dbfs for spec in specs.values()} == {ladder}
    assert _played_measure_spec(
        specs[_MEASURE_INDEX], monkeypatch
    ).level_ladder_dbfs == ladder

    # …and a walk that states none builds the spec it always did.
    spool.stage_angle_request(ac.summed_at([0, 7]))
    _prompts, _consumer, bare, _trims, _claims = _take()
    assert {spec.level_ladder_dbfs for spec in bare.values()} == {()}


def test_a_summed_sweep_rides_the_summed_stops_only(slot):
    """The band and duration are a SUMMED sweep's, so they reach each stop's
    summed spec and never the per-driver design-axis MEASURE spec, which plays
    the program's own excitation; ``MeasureSpec`` refuses them on that scope.
    """
    band, seconds = (200.0, 3000.0), 2.5
    spool.stage_angle_request(replace(
        ac.summed_at([0, 7]),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, sweep_band_hz=band, sweep_s=seconds,
        ),
    ))
    _prompts, _consumer, specs, _trims, _claims = _take()

    by_scope = {spec.graph_scope: spec for spec in specs.values()}
    assert (by_scope["base"].sweep_band_hz, by_scope["base"].sweep_s) == (band, seconds)
    assert (by_scope["drivers"].sweep_band_hz, by_scope["drivers"].sweep_s) == ((), None)


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



@pytest.mark.parametrize(
    ("stated", "calibrated", "watched"),
    [(None, True, False), (80.0, True, True), (80.0, False, None)],
    ids=["no-ceiling-no-watch", "ceiling-watched", "ceiling-uncalibrated"],
)
def test_a_stated_ceiling_buys_a_watch_or_refuses_the_open(
    slot, monkeypatch, stated, calibrated, watched,
):
    """A walk's SPL ceiling is a bound the session must actually hold.

    The door that plays a walk installs the monitor that watches it, and a box
    whose microphone cannot be turned into dB SPL refuses such a walk instead of
    playing it at the level the commissioning stop proved. A walk stating no
    ceiling asks for no live watch: the held session level is already bounded by
    that stop.
    """
    from jasper.audio_measurement.wired_capture import WiredSplMonitor

    monkeypatch.setattr(
        v2host, "_household_mic_sensitivity",
        lambda: SimpleNamespace() if calibrated else None,
    )
    preset = SimpleNamespace(
        safety=SimpleNamespace(max_commissioning_level_db_spl=85.0),
    )
    spool.stage_angle_request(ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_SUMMED),),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, spl_ceiling_db_spl=stated,
        ),
    ))

    if watched is None:
        with pytest.raises(v2host.CrossoverV2Refused) as refused:
            _take_full(preset=preset)
        assert ac.WALK_SPL_CALIBRATION_REQUIRED in str(refused.value)
        assert ac.WALK_SPL_CALIBRATION_REQUIRED in ac.WALK_REFUSAL_REASONS
        return

    monitor = _take_full(preset=preset)[5]
    assert isinstance(monitor, WiredSplMonitor) is watched
    if watched:
        assert monitor.ceiling_db_spl == stated


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


def test_a_hand_edited_template_refuses_the_open_in_the_specs_own_words(slot, caplog):
    """The pair is judged by the SPEC, and a document can still carry one it
    refuses: staging cannot reach a file an operator edits afterwards.

    It refuses the open there, like every other walk this session cannot honour,
    rather than raising out of a capture callback mid-round — and the detail is
    the spec's own refusal, compared against what the spec actually raises so
    this pin cannot become the second vocabulary it exists to forbid.
    """
    spool.stage_angle_request(_inverted_walk(inverted_role=DRIVER_ROLE_TWEETER))
    path = spool.angle_request_spool_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["template"]["inverted_role"] = ""
    path.write_text(json.dumps(doc), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        sentence = _refused()

    with pytest.raises(ValueError) as spec_refusal:
        MeasureSpec(kind=MEASURE_KIND_CANDIDATE, polarity=POLARITY_INVERTED)
    assert str(spec_refusal.value) in sentence

    line, = _events(caplog)
    assert f"reason={spool.SPOOL_MALFORMED}" in line
    assert "consumed=true" in line and "session_continues=false" in line
    assert spool.staged_angle_request_pending() is False


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


def test_the_take_reads_the_sessions_own_cloud_shape(slot):
    """The capacity gate needs this session's retake budget, not a guess.

    A cloud group costs two more capture indexes than a cloud-less session of the
    same length, because only a cloud budgets geometry retakes. The stop count
    below is chosen so that fact is the ONLY thing separating the two calls:
    same document length, same ``base_entries``, opposite verdicts. Anything
    less discriminating passes with the flag ignored — the first version of this
    test did.
    """
    stops = [0] * 111  # 11 + 111 + 5 = 127 fits; + 7 = 129 does not
    spool.stage_angle_request(ac.per_driver_at(stops))
    assert _take(base_entries=11, plans_cloud_group=False) is not None

    spool.stage_angle_request(ac.per_driver_at(stops))
    assert ac.WALK_OVER_CAPTURE_CAPACITY in _refused(
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
        spool.SESSION_ALREADY_LIVE,
    })


def test_complete_branch_batch_reaches_browser_and_banks_all_three_curves(slot, monkeypatch):
    from jasper.audio_measurement.branch_program import is_branch_program
    from jasper.audio_measurement.program_analysis import MeasurementPriors, analyze_program_capture
    from tests.test_audio_measurement_program_analysis import SR, _band_impulse, _synthesize

    preset = _banked(monkeypatch)
    request = ac.request_for_program(mp.program("branches", "express"), candidates=("fp-a",))
    spool.stage_angle_request(request)
    prompts, consumer, specs, trims, claims = _take(preset=preset)
    assert len(prompts) == 1 and not trims
    index_phases = _seat_index_phases(prompts)
    index, = [i for i, phase in index_phases.items() if phase == PHASE_LATERAL]
    assert specs[index].graph_scope == "candidate_branches"
    fakes, records = FakeSeams(), []
    seams = fakes.seams()

    def analyze(program, *args, **kwargs):
        if not is_branch_program(program):
            return seams.analyze(program, *args, **kwargs)
        capture = _synthesize(program, woofer_ir=_band_impulse(200, 150, 20000, 1),
                              tweeter_ir=_band_impulse(212, 150, 20000, .7), noise=1e-8)
        return analyze_program_capture(program, capture, SR, priors=MeasurementPriors(crossover_fc_hz=2000))

    conductor = _conductor(fakes, index_phase_map=index_phases, lateral_prompts=prompts,
                          lateral_consumer=consumer, lateral_claims=claims, measure_specs_by_index=specs,
                          seams=replace(seams, analyze=analyze, bank_take=bank_into(records, phase=PHASE_LATERAL)))
    _run_phase(conductor, 1, 1)
    _run_phase(conductor, 2, 1)
    program = conductor.program_for_phase(PHASE_LATERAL)
    assert is_branch_program(program)
    plan = flow.build_v2_capture_plan(_ROLES_BANDS, 2000, plan_shape=_hand_shape(),
                                     include_cloud_measure=False, include_lateral=True,
                                     lateral_prompts=prompts, lateral_candidate_ids=("fp-a",),
                                     branch_diagnostic=True)
    entry = next(e for e in plan.entries if e.index == index - 1)
    assert entry.duration_ms >= program.total_samples * 1000 / program.sample_rate_hz
    assert _run_phase(conductor, index, 1)["accepted"]
    record, = records
    assert {r["role"] for r in record["curves"]} == {"woofer", "tweeter", "summed"}
    assert record["candidate_id"] == "fp-a"
    assert record["phase_composition"] == "complete_tune_measured"
    assert record["branch_diagnostic"]["sample_rate_hz"] == SR


@pytest.mark.parametrize("program,purpose,scope", [("room", mp.PURPOSE_ROOM, "speaker_tune"), ("bass", mp.PURPOSE_BASS, "room_tune")])
def test_arm_plan_preserves_upstream_layers_without_changing_positions(slot, program, purpose, scope):
    request = ac.request_for_program(mp.program(program, "quick"), mover=ac.MOVER_ARM)
    spool.stage_angle_request(request)
    prompts, _, specs, _, claims = _take(shape=_arm_shape())
    assert [flow.position_angle_deg(p) for p in prompts] == [0, -20, 20]
    assert {p.kind for p in prompts} == {mp.POSE_KIND_BEARING}
    assert {p.purpose for p in prompts} == {purpose}
    assert {s.graph_scope for s in specs.values() if s.kind == MEASURE_KIND_VERIFY} == {scope}
    assert {claim.measurement_purpose for claim in claims} == {purpose}
