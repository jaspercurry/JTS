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

import json
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
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_LATERAL,
)
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand
from jasper.web import correction_crossover_v2 as v2host
from tests.crossover_v2_fixtures import _preset

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
    from jasper.active_speaker import preflight_live
    from tests.test_preflight import ready_facts
    def facts(plan, **kwargs):
        candidates = {stop.candidate_id: candidate_bank.find_banked_candidate(stop.candidate_id).candidate
                      for stop in plan.stops if ac.candidate_identity(stop.candidate_id) != ac.BASE_CANDIDATE}
        return ready_facts(plan, candidates=candidates)

    monkeypatch.setattr(preflight_live, "read_preflight_facts", facts)
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
_MIC = SimpleNamespace(model_key="minidsp_umik2", model_label="UMIK-2")


def _events(caplog) -> list[str]:
    return [
        rec.getMessage() for rec in caplog.records
        if "crossover_v2_angle_walk" in rec.getMessage()
    ]


# --- the ordinary session -----------------------------------------------------


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


# --- what the taken walk composes into ----------------------------------------


# --- R-1's reverse polarity ---------------------------------------------------


def _inverted_walk(**template):
    return ac.AngleCaptureRequest(
        stops=(ac.AngleStop(0, ac.REGIME_PER_DRIVER),),
        template=ac.walk_template(
            kind=MEASURE_KIND_CANDIDATE, polarity=POLARITY_INVERTED, **template,
        ),
    )


def _banked(monkeypatch, *, room_correction=None, bass_extension=None, **alignment):
    """A banked candidate whose corner is the preset ``_take`` is handed."""
    preset = _preset()
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate, MeasuredCrossoverAlignment
    from tests.test_active_speaker_measured_crossover_candidate import _room_correction

    candidate = MeasuredCrossoverCandidate(
        program_id="speaker", analysis={"status": "measured"}, source_preset=preset,
        role_attenuations_db={"woofer": 0.0, "tweeter": 0.0},
        room_correction=_room_correction() if room_correction else {},
        bass_extension=bass_extension or {}, alignment=MeasuredCrossoverAlignment(**alignment),
    )
    monkeypatch.setattr(candidate_bank, "find_banked_candidate",
                        lambda fingerprint, **kw: SimpleNamespace(candidate=candidate))
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


def _seat_index_phases(prompts):
    return flow.build_v2_cloud_index_phase_map(
        plan_shape=_hand_shape(), include_cloud_measure=False,
        include_lateral=True, lateral_prompts=prompts,
    )


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


# --- the take opens the session, whatever the document does -------------------


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
