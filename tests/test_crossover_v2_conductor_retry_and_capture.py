# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: bounded retries and capture evidence."""

from __future__ import annotations

import pytest

from jasper.active_speaker.crossover_v2 import capture_plan
from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2 import spatial
from jasper.active_speaker.crossover_v2.capture_plan import (
    CLOUD_POSITION_PROMPTS,
)
from jasper.active_speaker.crossover_v2.contracts import CrossoverV2FlowError
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
)


def test_every_retriable_reason_has_one_structured_diagnosis_source():
    """Exhaustive negative guard for the count-only regression.

    Every retriable registry row must carry a diagnosis, and its historical
    retryable message/banner must be composed from that same value. Adding a
    new retriable code as a bare literal fails here before exhaustion can ship
    generic count-only copy for it.
    """
    retriable = {
        code: spec for code, spec in REASON_REGISTRY.items()
        if spec.retry_budget > 0
    }
    assert retriable
    for code, spec in retriable.items():
        assert spec.retry_copy is not None, code
        assert (spec.message or spec.banner) == spec.retry_copy.message, code
        assert refusal_copy.reason_diagnosis(code, spec), code


def test_a_vertical_seat_states_its_elevation_and_still_banks_no_bearing():
    """A raised pose commands NO bearing, and 0 would read as the design axis.

    Where it WAS raised to is ``vertical_deg``, derived from the row's own
    ``offset_cm`` against the mark distance exactly as a lateral row's bearing
    is, and signed by the row's own ABOVE/BELOW word — so the two 40 cm rows
    stop being byte-identical records.

    ``position_angle_deg`` still refuses a vertical row outright, and that
    refusal is deliberately kept: it aims an external POSITIONER, and no
    positioner can raise the microphone. This derivation runs on the retention
    path instead, where a raise would fail a capture the household already
    gave, so it states the axis and leaves the angle ``None``.
    """
    vertical = [
        p for p in CLOUD_POSITION_PROMPTS if p.role == spatial.POSITION_ROLE_XOVR
    ]
    geometries = [capture_plan.position_geometry(p) for p in vertical]

    assert {g.axis for g in geometries} == {"vertical"}
    assert {g.degrees for g in geometries} == {None}
    assert {g.mark_distance_m for g in geometries} == {spatial.MARK_DISTANCE_M}
    assert [g.vertical_deg for g in geometries] == [7, -7, 22, -22]
    for prompt in vertical:
        with pytest.raises(CrossoverV2FlowError):
            capture_plan.position_angle_deg(prompt)


def test_the_compound_retake_rung_states_the_rise_it_asks_for():
    """The one shipped pose that moves BOTH ways states both, or it lies.

    Rung 2 asks for 75 cm sideways AND 30 cm up. Its two displacements differ,
    so a single ``offset_cm`` cannot carry them — and a record that defaulted
    its elevation to 0 would claim mark height for a microphone the household
    was told to raise, and would pair that take against a mark-height baseline.
    """
    rung_2 = capture_plan.CloudPositionPrompt(
        capture_plan.CLOUD_GEOMETRY_RETRY_PROMPTS[1],
        offset_cm=capture_plan.GEOMETRY_RETRY_OFFSET_CM,
        role=spatial.POSITION_ROLE_OFFAX,
        vertical_sign=1,
        vertical_offset_cm=capture_plan.CLOUD_GEOMETRY_RETRY_RISE_CM[1],
    )
    geometry = capture_plan.position_geometry(rung_2)

    assert capture_plan.CLOUD_GEOMETRY_RETRY_RISE_CM[1] > 0
    assert geometry.vertical_deg == 17
    # Its lateral distance is the wider one and is NOT what the rise came from.
    assert capture_plan.GEOMETRY_RETRY_OFFSET_CM != capture_plan.CLOUD_GEOMETRY_RETRY_RISE_CM[1]
    # Rung 1 is at mark height and says so.
    assert capture_plan.CLOUD_GEOMETRY_RETRY_RISE_CM[0] == 0.0


def test_a_raised_seat_joins_no_bearing_set_the_walk_already_had():
    """The mixed walk's horizontal aggregates do not notice the raised seats.

    The shipped cloud table is already mixed — seven lateral rows and four
    raised ones. Banking an elevation must not move what the horizontal-only
    consumers see, and the mechanism that guarantees it is ``position_deg``
    staying ``None`` on a raised seat: every pooled bearing set in the tree
    (``evidence_packet._angle_deg_block`` is the one a reader sees) is built by
    filtering for an ``int`` bearing, so a raised seat is excluded there and
    included, AS LABELLED, everywhere a seat is listed.
    """
    geometries = [capture_plan.position_geometry(p) for p in CLOUD_POSITION_PROMPTS]
    bearings = [g.degrees for g in geometries if isinstance(g.degrees, int)]

    assert bearings == [-7, 7, -22, 22, -14, 14, -31]
    assert [
        capture_plan.position_angle_deg(p) for p in CLOUD_POSITION_PROMPTS
        if p.role != spatial.POSITION_ROLE_XOVR
    ] == bearings
    # Every lateral seat is at mark height, so the new field says nothing new
    # about any of them — which is why an old bundle missing it reads as 0.
    assert {
        g.vertical_deg for g in geometries if g.axis == "horizontal"
    } == {0}
