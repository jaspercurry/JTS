# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The take: a session picks up an operator's staged angle walk (#2732 P2).

The half the door was missing. ``tests/test_angle_capture_trigger.py`` covers
staging the document, ``tests/test_angle_capture_seam.py`` covers composing one
into poses, and this covers the one place a SESSION picks it up: who the walk is
declared to be for, that a document is spent exactly once whichever way it goes,
and that a household session opens either way.

What is deliberately NOT re-asserted here is anything those two files own -- the
angle bounds, the pose round trip, the three refusals' own arithmetic. The
second validator is the thing this design exists to avoid.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from jasper.active_speaker import angle_capture as ac
from jasper.active_speaker import angle_capture_spool as spool
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2.journey import (
    LATERAL_CONSUMER_FC_SELECTOR,
    LATERAL_CONSUMER_FORWARD_MODEL,
    PHASE_LATERAL,
)
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


def _hand_shape():
    return flow.resolve_plan_shape(flow.TIER_FULL)


def _arm_shape():
    return flow.resolve_plan_shape(flow.TIER_REMOTE)


def _take(shape=None, *, base_entries=3, lateral_group_present=False):
    return v2host._take_staged_angle_walk(
        shape if shape is not None else _hand_shape(),
        base_entries=base_entries,
        lateral_group_present=lateral_group_present,
    )


def _events(caplog) -> list[str]:
    return [
        rec.getMessage() for rec in caplog.records
        if "crossover_v2_angle_walk" in rec.getMessage()
    ]


# --- the ordinary session -----------------------------------------------------


def test_no_staged_walk_is_an_ordinary_session(slot, caplog):
    """Every household session. Nothing taken, nothing said."""
    with caplog.at_level(logging.INFO):
        assert _take() is None
    assert _events(caplog) == []


def test_the_shipped_stage_1_still_plans_no_lateral_group(slot):
    """The pause is untouched by the take existing.

    With no staged document the session's own flag decides, and it is still off
    -- so the shipped map is the 3-entry shape and the walk's indexes are not in
    it. This is the control every claim below rests on.
    """
    assert flow.STAGE1_INCLUDES_LATERAL is False
    shipped = flow.build_v2_cloud_index_phase_map(
        plan_shape=_hand_shape(),
        include_cloud_measure=flow.STAGE1_INCLUDES_CLOUD_MEASURE,
        include_lateral=flow.STAGE1_INCLUDES_LATERAL,
        include_entry_baseline=flow.STAGE1_INCLUDES_ENTRY_BASELINE,
    )
    assert PHASE_LATERAL not in shipped.values()
    assert len(shipped) == 3


# --- the take -----------------------------------------------------------------


def test_a_staged_walk_is_taken_once_and_named_as_evidence(slot, caplog):
    """The consumer identity is assigned HERE, not carried in the document.

    Any walk an operator stages is evidence for the offline forward model. A
    document field would be a second writer of the one fact that decides
    whether #2711's paused statistic runs, and this is the writer.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    with caplog.at_level(logging.INFO):
        taken = _take()

    assert taken is not None
    prompts, consumer = taken
    assert consumer == LATERAL_CONSUMER_FORWARD_MODEL
    assert [flow.position_angle_deg(p) for p in prompts] == CAMPAIGN_ANGLES

    line, = _events(caplog)
    assert "crossover_v2_angle_walk_taken" in line
    assert "stops=5" in line
    assert "angles=+0,+7,-7,+22,-22" in line
    assert f"consumer={LATERAL_CONSUMER_FORWARD_MODEL}" in line
    assert "fc_statistic_paused=true" in line

    # Single-use: the next session is an ordinary one. The document is spent by
    # the take, not by the session succeeding.
    assert _take() is None


def test_a_refused_walk_is_consumed_and_the_session_still_opens(slot, caplog):
    """Fail-open on the SESSION, fail-closed on the walk.

    A household session is never hostage to an operator's staged file: the
    refusal is named in the journal, the document is spent so it refuses once
    rather than every session, and the caller gets ``None`` -- which is the
    ordinary session it would have had anyway.
    """
    spool.stage_angle_request(ac.per_driver_at([7], mover=ac.MOVER_ARM))
    with caplog.at_level(logging.WARNING):
        assert _take(_hand_shape()) is None

    line, = _events(caplog)
    assert "crossover_v2_angle_walk_refused" in line
    assert f"reason={ac.WALK_MOVER_MISMATCH}" in line
    assert "consumed=true" in line and "session_continues=true" in line
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
        assert _take() is None

    line, = _events(caplog)
    assert f"reason={spool.SPOOL_MALFORMED}" in line


def test_a_staged_walk_refuses_while_the_session_already_walks_one(slot, caplog):
    """Two lateral groups cannot share one session's index space.

    Also the relay arithmetic: ``MAX_CLOUD_MEASURE_POSITIONS``' own note sizes
    the ceiling for the paused walk's six poses and says not to spend the slack,
    and a second walk on top is exactly that spend.
    """
    spool.stage_angle_request(ac.per_driver_at(CAMPAIGN_ANGLES))
    with caplog.at_level(logging.WARNING):
        assert _take(lateral_group_present=True) is None

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
    prompts, _consumer = _take()
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
    prompts, _consumer = _take(_arm_shape())
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
    prompts, _consumer = _take()
    wide = flow.walk_shape_for(
        cloud_positions=0, lateral=True, lateral_prompts=prompts,
    )
    ratified = flow.walk_shape_for(cloud_positions=0, lateral=True)
    assert wide != ratified
    assert "100 cm" in wide or "110 cm" in wide


# --- one take, four surfaces --------------------------------------------------


def test_the_preparer_feeds_map_spec_and_conductor_from_one_take():
    """ONE take, read by everything that must agree about the walk.

    Source-read rather than driven, for the reason the sibling tier pin gives:
    driving ``_open`` needs a live relay. What matters is the wiring -- a second
    take would hand the map and the spec different walks, and a surface left
    unthreaded would render one walk while the conductor ran another.
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
