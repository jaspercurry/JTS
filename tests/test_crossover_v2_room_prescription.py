# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the room prescription door, and what it will not let through.

The door's whole job is that a room PEQ set reaching a candidate was measured
against the round's own spatial median. So the pins here are the refusals --
one per bar, by slug -- plus the one path that must work end to end: an
accepted document's candidate fields build a real
:class:`MeasuredCrossoverCandidate` whose emitted room PEQs are the filters
that were prescribed.

The median fixture is one builder with knobs rather than a family of files: a
room is a shape (a mode, a persistent dip, an interference null, a band where
the seats disagree) and every refusal below is that shape read against one
proposal.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.candidate_bank import find_banked_candidate
from jasper.active_speaker.crossover_v2.blend_prescription import prescription_sha256
from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
    candidate_room_peqs,
)
from jasper.active_speaker.crossover_v2.room_prescription import (
    BOOST_NOT_ADMITTED,
    COMPOSED_BOOST_EXCEEDED,
    FILTER_BOOST_TOO_HIGH,
    FILTER_COUNT_EXCEEDED,
    FILTER_CUT_TOO_DEEP,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    LAYOUT_UNAVAILABLE,
    ROOM_MEDIAN_MISMATCH,
    ROOM_MEDIAN_UNAVAILABLE,
    ROOM_PRESCRIPTION_KIND,
    SIDE_MALFORMED,
    TAPER_VIOLATED,
    RoomPrescriptionRefused,
    read_room_median,
    read_room_prescription,
    room_prescription_to_candidate_fields,
)
from jasper.camilla_config_contract import PeqFilter
from jasper.cli import crossover_prescriber as cli

from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_active_speaker_profile import _two_way_preset
from tests.test_crossover_v2_candidate_republish import _publish
from tests.test_crossover_v2_driver_prescription import applied_profile

#: The digest the fixture document echoes when the test does not care which.
MEDIAN_SHA256 = "a" * 64

#: The fixture room's own three features, Hz: a mode, a dip the seats agree on,
#: and an interference null. Named so a refusal reads as the shape it is.
MODE_HZ = 33.0
DIP_HZ = 45.0
NULL_HZ = 90.0
#: Where the seats disagree most (sigma 9 dB), so a cut there is bounded well
#: above the strategy's own floor.
WIDE_SPREAD_HZ = 200.0
CEILING_HZ = 350.0
#: Seats in the fixture cloud. Five of them see the dip, which clears the 70%
#: presence bar by one seat.
SEATS = 7
#: The sides the fixture speaker declares -- a mono cabinet declares one.
SIDES = ("mono",)


def _bell(freq_hz: float, center_hz: float, depth_db: float, sigma: float) -> float:
    """One log-symmetric feature, ``sigma`` in octaves."""
    return depth_db * math.exp(-((math.log2(freq_hz / center_hz)) / sigma) ** 2)


def _room_median(*, present: int = 5) -> dict[str, Any]:
    """One round's median document: 1/12-octave, 20 Hz to just under 400.

    ``present`` is how many of the seats see the dip at :data:`DIP_HZ`; the
    rest read it back to nearly flat, which is what an interference null looks
    like across a cloud.
    """
    freqs = [20.0 * 2.0 ** (step / 12.0) for step in range(52)]
    return {
        "freqs_hz": freqs,
        "median_db": [
            _bell(freq, MODE_HZ, 6.0, 0.15)
            + _bell(freq, DIP_HZ, -8.0, 0.22)
            + _bell(freq, NULL_HZ, -14.0, 0.04)
            for freq in freqs
        ],
        "spread_db": [
            9.0 if abs(math.log2(freq / WIDE_SPREAD_HZ)) <= 0.25 else 2.0
            for freq in freqs
        ],
        "n_positions": SEATS,
        "positions": [
            {
                "id": f"seat-{index}",
                "deviation_db": [
                    6.0
                    if index >= present and abs(math.log2(freq / DIP_HZ)) <= 0.2
                    else 0.0
                    for freq in freqs
                ],
            }
            for index in range(SEATS)
        ],
        "ceiling_hz": CEILING_HZ,
        "ceiling_source": "applied_candidate",
        "window": "ungated",
    }


#: The correction the fixture room asks for: the mode cut, the agreed dip
#: filled to half its depth, and a modest cut where the seats disagree.
ACCEPTED_FILTERS = [
    {"freq": MODE_HZ, "q": 3.0, "gain": -5.0},
    {"freq": DIP_HZ, "q": 3.0, "gain": 4.0},
    {"freq": WIDE_SPREAD_HZ, "q": 4.0, "gain": -4.0},
]


def _document(
    *,
    filters: list[dict[str, Any]] | None = None,
    sha256: str = MEDIAN_SHA256,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "artifact_schema_version": 1,
        "kind": ROOM_PRESCRIPTION_KIND,
        "room_median_sha256": sha256,
        "prescriber": {"model": "a model", "operator": "an operator"},
        "sides": {"mono": ACCEPTED_FILTERS if filters is None else filters},
        **overrides,
    }


def _read(document: dict[str, Any], median: dict[str, Any] | None = None):
    return read_room_prescription(
        document,
        room_median=read_room_median(median or _room_median()),
        room_median_sha256=MEDIAN_SHA256,
        round_id="round-7",
        sides=SIDES,
    )


def test_no_document_is_the_deterministic_path():
    assert read_room_prescription(
        None,
        room_median=read_room_median(_room_median()),
        room_median_sha256=MEDIAN_SHA256,
        round_id="round-7",
        sides=SIDES,
    ) is None


def test_a_median_whose_rows_do_not_match_its_grid_is_not_evidence():
    """Every fault in the median is one reason: a median that cannot be read
    into limits is evidence this door does not have."""
    broken = _room_median()
    broken["positions"][0]["deviation_db"] = [0.0, 0.0]
    with pytest.raises(RoomPrescriptionRefused) as excinfo:
        read_room_median(broken)
    assert excinfo.value.reason == ROOM_MEDIAN_UNAVAILABLE


@pytest.mark.parametrize(
    "reason,document,median_knobs",
    [
        pytest.param(
            BOOST_NOT_ADMITTED, {}, {"present": 4},
            id="boost_the_seats_do_not_agree_on",
        ),
        pytest.param(
            BOOST_NOT_ADMITTED,
            {"filters": [{"freq": NULL_HZ, "q": 4.0, "gain": 3.0}]},
            {},
            id="boost_into_an_interference_null",
        ),
        pytest.param(
            FILTER_OUTSIDE_REGION,
            {"filters": [{"freq": 380.0, "q": 3.0, "gain": -3.0}]},
            {},
            id="filter_above_the_ceiling",
        ),
        pytest.param(
            COMPOSED_BOOST_EXCEEDED,
            {"filters": [
                {"freq": DIP_HZ, "q": 3.0, "gain": 5.0},
                {"freq": DIP_HZ + 0.5, "q": 3.0, "gain": 5.0},
            ]},
            {},
            id="side_total_boost_over_cap",
        ),
        pytest.param(
            FILTER_Q_OUT_OF_RANGE,
            {"filters": [{"freq": DIP_HZ, "q": 0.5, "gain": -3.0}]},
            {},
            id="q_below_the_room_range",
        ),
        pytest.param(
            FILTER_CUT_TOO_DEEP,
            {"filters": [{"freq": WIDE_SPREAD_HZ, "q": 3.0, "gain": -8.0}]},
            {},
            id="cut_past_what_the_spread_supports",
        ),
        pytest.param(
            TAPER_VIOLATED,
            {"filters": [{"freq": 277.0, "q": 1.0, "gain": -6.0}]},
            {},
            id="wide_cut_still_open_at_the_ceiling",
        ),
        pytest.param(
            FILTER_COUNT_EXCEEDED,
            {"filters": [
                {"freq": 30.0 + index, "q": 3.0, "gain": -1.0} for index in range(9)
            ]},
            {},
            id="more_filters_than_a_side_may_hold",
        ),
        pytest.param(
            FILTER_BOOST_TOO_HIGH,
            {"filters": [{"freq": DIP_HZ, "q": 3.0, "gain": 20000.0}]},
            {},
            id="boost_past_the_cap_the_probe_cannot_evaluate",
        ),
        pytest.param(
            SIDE_MALFORMED, {"sides": {"left": ACCEPTED_FILTERS}}, {},
            id="a_side_this_speaker_does_not_declare",
        ),
        pytest.param(
            ROOM_MEDIAN_MISMATCH, {"sha256": "b" * 64}, {},
            id="answers_a_different_median",
        ),
    ],
)
def test_the_room_door_refuses_by_slug(reason, document, median_knobs):
    """Every bar, by slug. The prescription is the fixture room's own good one
    unless the case names a different set: what varies is one bound at a time.
    """
    with pytest.raises(RoomPrescriptionRefused) as excinfo:
        _read(_document(**document), _room_median(**median_knobs))
    assert excinfo.value.reason == reason


def test_an_accepted_set_becomes_the_candidates_room_peqs():
    """The one path that must work end to end: what the door admits is what
    the emitter is handed, through the candidate's own second check."""
    prescription = _read(_document())
    assert prescription is not None
    assert prescription.prescription_class == "boost"
    assert prescription.boost_db_total == prescription.level_cost_db == 4.0
    assert [finding.admitted for finding in prescription.admissions] == [True]

    fields = room_prescription_to_candidate_fields(prescription)
    candidate = _candidate(room_correction=fields["room_correction"])
    assert candidate_room_peqs(candidate) == tuple(
        PeqFilter(freq=entry["freq"], q=entry["q"], gain=entry["gain"])
        for entry in ACCEPTED_FILTERS
    )
    assert candidate.room_correction["basis"]["round_id"] == "round-7"
    assert candidate.room_correction["ceiling_hz"] == CEILING_HZ


# --- the CLI ----------------------------------------------------------------


@pytest.fixture
def bank(tmp_path, monkeypatch) -> Path:
    """The candidate bank this suite reads: never the box running pytest."""
    root = tmp_path / "sessions"
    monkeypatch.setattr("jasper.active_speaker.bundles.sessions_dir", lambda: root)
    return root


@pytest.fixture
def applied(tmp_path: Path) -> str:
    """The applied-profile SSOT the door reads this speaker's sides off."""
    path = tmp_path / "applied-profile.json"
    path.write_text(json.dumps(applied_profile(preset=_two_way_preset("mono"))))
    return str(path)


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[str, str]:
    """The two files every room verb takes: the median and one document.

    The document echoes the median's REAL digest, which is what the CLI
    computes over the bytes it read.
    """
    median = tmp_path / "round-7" / "room_median.json"
    median.parent.mkdir()
    median.write_text(json.dumps(_room_median()))
    document = tmp_path / "prescription.json"
    document.write_text(
        json.dumps(_document(sha256=prescription_sha256(median.read_bytes())))
    )
    return str(document), str(median)


def test_propose_judges_a_room_document_against_its_median(
    evidence, applied, capsys,
):
    document, median = evidence
    assert cli.main([
        "propose", "--prescription", document, "--room-median", median,
        "--applied-profile", applied,
    ]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["accepted"] is True
    assert answer["candidate_fields"] == ["room_correction"]
    assert answer["prescription_class"] == "boost"
    assert answer["n_filters"] == len(ACCEPTED_FILTERS)
    # The receipt lands beside the median, and `next` is the composition it
    # becomes rather than a staging it cannot have.
    receipt = json.loads(Path(answer["out"]).read_text())
    assert receipt["prescription"]["kind"] == ROOM_PRESCRIPTION_KIND
    assert "compose" in answer["next"] and "--room-prescription" in answer["next"]


def test_stage_refuses_the_room_class(evidence, applied, tmp_path, capsys):
    document, median = evidence
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"round_receipt": {"round_ordinal": 3}}))
    assert cli.main([
        "stage", "--prescription", document, "--room-median", median,
        "--applied-profile", applied, "--state", str(state),
    ]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == cli.ROOM_NOT_STAGEABLE


@pytest.mark.parametrize("readable, reason", [
    (True, SIDE_MALFORMED),
    (False, LAYOUT_UNAVAILABLE),
])
def test_propose_takes_the_sides_from_the_applied_profile(
    evidence, applied, tmp_path, capsys, readable, reason,
):
    """Which sides exist is the speaker's fact, not the document's.

    A propose that reads different ones refuses and says which it expected; one
    that can read none refuses too, because it is a dry run of a compose that
    will resolve them from the base candidate's own preset.
    """

    document, median = evidence
    body = json.loads(Path(document).read_text())
    body["sides"] = {"left": body["sides"].pop("mono")}
    path = tmp_path / "left.json"
    path.write_text(json.dumps(body))
    assert cli.main([
        "propose", "--prescription", str(path), "--room-median", median,
        "--applied-profile", applied if readable else str(tmp_path / "absent.json"),
    ]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert answer["reason"] == reason
    if readable:
        assert answer["detail"]["evidence"]["expected_sides"] == ["mono"]


def test_compose_carries_the_room_set_onto_the_candidate(evidence, bank, capsys):
    document, median = evidence
    base = _candidate()
    _publish(bank, base)
    assert cli.main([
        "compose", "--root", str(bank), "--base", base.fingerprint,
        "--room-prescription", document, "--room-median", median,
    ]) == 0
    answer = json.loads(capsys.readouterr().out)
    child = find_banked_candidate(answer["candidate_fingerprint"], root=bank).candidate
    assert candidate_room_peqs(child) == tuple(
        PeqFilter(freq=entry["freq"], q=entry["q"], gain=entry["gain"])
        for entry in ACCEPTED_FILTERS
    )
    assert child.room_correction["basis"]["round_id"] == "round-7"
    assert set(child.analysis["room_source"]) == {
        "prescription_sha256", "room_median_sha256",
    }
    # It reopens with the field: the room set is inside the fingerprint.
    assert MeasuredCrossoverCandidate.from_mapping(
        child.to_dict()
    ).room_correction == child.room_correction


def test_compose_refuses_half_the_room_evidence(evidence, bank, capsys):
    document, _median = evidence
    base = _candidate()
    _publish(bank, base)
    assert cli.main([
        "compose", "--root", str(bank), "--base", base.fingerprint,
        "--room-prescription", document,
    ]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == cli.REASON_EVIDENCE_SOURCE
