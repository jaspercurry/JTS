# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room prescriptions are bounded by their measured spatial median."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.candidate_bank import banked_candidates, find_banked_candidate, publish_authored_candidate
from jasper.active_speaker.branch_chain import chain_response
from jasper.active_speaker.measured_crossover_candidate import (
    candidate_room_peqs,
)
from jasper.audio_measurement.evidence_reasons import REASON_TOO_FEW_POSITIONS
from jasper.audio_measurement.room_boundary import ROOM_FLOOR_HZ
from jasper.audio_measurement.seat_figures import spread_rms_db
from jasper.audio_measurement.room_limits import ROOM_PEQ_Q_MIN, ROOM_PEQ_Q_MAX, spatial_support
from jasper.active_speaker.crossover_v2.room_selection import select_seat_takes
from jasper.active_speaker.crossover_v2.room_views import (
    room_ceiling,
    room_median,
    room_median_sha256,
)
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.crossover_v2.room_prescription import (
    BOOST_NOT_ADMITTED,
    COMPOSED_BOOST_EXCEEDED,
    FILTER_BOOST_TOO_HIGH,
    FILTER_COUNT_EXCEEDED,
    FILTER_OUTSIDE_REGION,
    FILTER_Q_OUT_OF_RANGE,
    ROOM_MEDIAN_UNAVAILABLE,
    ROOM_COMPOSED_TOLERANCE_DB,
    ROOM_PRESCRIPTION_KIND,
    SIDE_MALFORMED,
    TAPER_VIOLATED,
    RoomPrescriptionRefused,
    preview_room_prescription,
    read_room_median,
    read_room_prescription,
    room_prescription_to_candidate_fields,
)
from jasper.biquad import PeqFilter
from jasper.cli import crossover_prescriber as cli
from jasper.cli.round_views._common import default_out

from tests.crossover_v2_banked_round import SEAT_GRID_HZ, bank_seat_round
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.room_median_fixture import analyzed_room_documents as analyzed_room_documents
from tests.test_active_speaker_measured_crossover_candidate import _candidate

#: The digest the fixture document echoes when the test does not care which.
MEDIAN_SHA256 = "a" * 64

#: The fixture room's own three features, Hz: a mode, a dip the seats agree on,
#: and an interference null. Named so a refusal reads as the shape it is.
MODE_HZ = 33.0
DIP_HZ = 45.0
NULL_HZ = 90.0
#: Where the seats disagree most (sigma 9 dB), so the floor a cut there is
#: disclosed against sits well above the strategy's own.
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


def _room_median(*, present: int = 5, window: str = "ungated", dip_hz: float = DIP_HZ) -> dict[str, Any]:
    """One round's median document: 1/12-octave, 20 Hz to just under 400.

    ``present`` is how many of the seats see the dip at ``dip_hz``; the
    rest read it back to nearly flat, which is what an interference null looks
    like across a cloud.
    """
    freqs = [
        freq for freq in (20.0 * 2.0 ** (step / 12.0) for step in range(52))
        if freq <= CEILING_HZ
    ]
    return {
        "freqs_hz": freqs,
        "median_db": [
            _bell(freq, MODE_HZ, 6.0, 0.15)
            + _bell(freq, dip_hz, -8.0, 0.22)
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
                    if index >= present and abs(math.log2(freq / dip_hz)) <= 0.2
                    else 0.0
                    for freq in freqs
                ],
            }
            for index in range(SEATS)
        ],
        "ceiling_hz": CEILING_HZ,
        "ceiling_source": "applied_candidate",
        "window": window,
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


@pytest.mark.parametrize("break_document", [
    lambda raw: raw["positions"][0].update(deviation_db=[0.0, 0.0]),
    lambda raw: raw.pop("spread_db"),
], ids=["row_length", "missing_spread"])
def test_an_unreadable_median_is_not_evidence(break_document):
    broken = _room_median()
    break_document(broken)
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
        *(pytest.param(
            FILTER_Q_OUT_OF_RANGE,
            {"filters": [{"freq": DIP_HZ, "q": q, "gain": -3.0}]},
            {},
            id=f"q_outside_the_room_range_{q}",
        ) for q in (-1.0, 0.0, 0.5, ROOM_PEQ_Q_MAX + 1.0)),
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
            SIDE_MALFORMED,
            {"sides": {"mono": ACCEPTED_FILTERS, " mono ": ACCEPTED_FILTERS}},
            {},
            id="one_side_named_twice_around_whitespace",
        ),
        pytest.param(
            ROOM_MEDIAN_UNAVAILABLE, {}, {"window": "gated"},
            id="a_median_the_gate_measured_instead_of_the_room",
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
    if reason == FILTER_Q_OUT_OF_RANGE:
        assert excinfo.value.evidence == {"q": document["filters"][0]["q"],
                                         "q_range": [ROOM_PEQ_Q_MIN, ROOM_PEQ_Q_MAX]}


@pytest.mark.parametrize("echo,author", [(None, None), ("b" * 64, {}), ("", {"model": "m"})])
def test_optional_median_echo_and_author_are_disclosed(echo, author):
    raw = _document()
    raw.pop("room_median_sha256")
    raw.pop("prescriber")
    if echo is not None:
        raw.update(room_median_sha256=echo, prescriber=author)
    receipt = _read(raw).to_dict()
    assert receipt["room_median_sha256"] == MEDIAN_SHA256
    assert receipt["round_id"] == "round-7"
    assert receipt["answers_median"] is (None if echo is None else False)
    assert receipt["prescriber"] == {"model": "m" if author else "", "operator": ""}
    preview = preview_room_prescription(raw, room_median=read_room_median(_room_median()),
                                       room_median_sha256=MEDIAN_SHA256, round_id="round-7", sides=SIDES)
    assert {key: preview[key] for key in ("room_median_sha256", "round_id", "answers_median")} == {
        key: receipt[key] for key in ("room_median_sha256", "round_id", "answers_median")}



@pytest.mark.parametrize("sides", [("mono",), ("left", "right")])
def test_taper_refusal_carries_every_bin_and_filter_contribution(sides):
    # A boost the seats admit below the knee whose skirt stays open at the ceiling.
    filters = [{"freq": 250.0, "q": 1.0, "gain": 4.0}, {"freq": 282.0, "q": 8.0, "gain": -2.0}]
    raw = _document(sides={side: filters for side in sides})
    with pytest.raises(RoomPrescriptionRefused) as refused:
        read_room_prescription(raw, room_median=read_room_median(_room_median(dip_hz=250.0)),
                               room_median_sha256=MEDIAN_SHA256, round_id="round-7", sides=sides)
    assert refused.value.reason == TAPER_VIOLATED
    evidence = refused.value.evidence
    assert evidence["tolerance_db"] == ROOM_COMPOSED_TOLERANCE_DB
    assert evidence["filters"]
    assert {row["side"] for row in evidence["bins"]} == set(sides)
    assert any(row["freq_hz"] == CEILING_HZ for row in evidence["bins"])
    for row in evidence["bins"]:
        assert row["composed_db"] - row["boost_cap_db"] > evidence["tolerance_db"]
        assert row["composed_db"] == pytest.approx(sum(f["response_db"] for f in row["filters"]))
        assert [{k: f[k] for k in ("freq", "q", "gain")} for f in row["filters"]] == filters


@pytest.mark.parametrize("sides,disclosed", [
    pytest.param({"mono": [{"freq": WIDE_SPREAD_HZ, "q": 3.0, "gain": -8.0}]}, {("mono", 0)},
                 id="cut_past_what_the_spread_supports"),
    pytest.param({"mono": [{"freq": 277.0, "q": 1.0, "gain": -6.0}]}, {("mono", 0)},
                 id="wide_cut_still_open_at_the_ceiling"),
    pytest.param({side: [{"freq": 277.0, "q": 1.0, "gain": -3.0}, {"freq": 282.0, "q": 8.0, "gain": -2.0}]
                  for side in ("left", "right")}, {("left", 0), ("right", 0)},
                 id="only_the_filter_cutting_where_the_side_is_past_the_floor"),
])
def test_a_cut_past_the_spread_floor_is_disclosed_not_refused(sides, disclosed):
    prescription = read_room_prescription(
        _document(sides=sides), room_median=read_room_median(_room_median()),
        room_median_sha256=MEDIAN_SHA256, round_id="round-7", sides=tuple(sides))
    assert room_prescription_to_candidate_fields(prescription)["room_correction"]["sides"] == sides
    receipt = prescription.to_dict()["sides"]
    beyond = {(side, position): entry.pop("cut_beyond_spread_db")
              for side, entries in receipt.items() for position, entry in enumerate(entries)
              if "cut_beyond_spread_db" in entry}
    assert set(beyond) == disclosed and all(value > 0.0 for value in beyond.values())
    assert receipt == sides


@pytest.mark.parametrize("preview", [False, True])
def test_room_judge_requires_a_set_on_a_two_set_round(tmp_path, capsys, preview):
    root = tmp_path / "candidates"
    base = publish_authored_candidate(replace(_candidate(), analysis={"measurement_status": "unmeasured"}), root=root)
    round_dir = bank_seat_round(tmp_path)
    groups = [manifest_set([], set_id=f"set-{i}") for i in range(2)]
    for index, group in enumerate(groups):
        group["capture_basis"]["candidate_id"] = f"candidate-{index}"
    write_manifest(round_dir, program="room", groups=groups)
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": base.fingerprint,
                                "rationale": "room", "sections": {"room": _document()}}))
    assert cli.main(["judge", str(path), "--round", str(round_dir), "--root", str(root),
                     *(["--preview"] if preview else [])]) == 1
    answer = json.loads(capsys.readouterr().out)
    assert (answer["code"], answer["detail"]["section"]) == ("set_required", "room")
    assert answer["detail"]["evidence"]["sets"] == [{"set_id": f"set-{i}", "candidate_id": f"candidate-{i}"} for i in range(2)]


@pytest.mark.parametrize("filters,code", [
    ([], None), (ACCEPTED_FILTERS, None), ([{"freq": 277.0, "q": 1.0, "gain": -6.0}], None),
    ([{"freq": NULL_HZ, "q": 1.0, "gain": 3.0}], None),
    ([{"freq": WIDE_SPREAD_HZ, "q": 3.0, "gain": -10.0}], None),
    ([{"freq": MODE_HZ, "q": 0.0, "gain": -1.0}], None),
    ([{"freq": MODE_HZ, "q": "1.0", "gain": -1.0}], "filter_malformed"),
    ([{"freq": MODE_HZ, "q": 1.0, "gain": 20000.0}], "filter_malformed"),
])
def test_room_preview_reports_margins_and_residual_without_banking(tmp_path, capsys, filters, code):
    root = tmp_path / "candidates"
    base = publish_authored_candidate(replace(_candidate(), analysis={"measurement_status": "unmeasured"}), root=root)
    round_dir = bank_seat_round(tmp_path)
    median = _room_median()
    median["median_db"] = [db - 30.0 for db in median["median_db"]]
    (round_dir / "room.json").write_text(json.dumps({"median": median}))
    path = tmp_path / "prescription.json"
    path.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": base.fingerprint,
                                "rationale": "room", "sections": {"room": _document(
                                    filters=filters, sha256=room_median_sha256(median))}}))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert cli.main(["judge", str(path), "--round", str(round_dir), "--root", str(root), "--preview"]) == (1 if code else 0)
    answer = json.loads(capsys.readouterr().out)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    if code:
        assert answer["code"] == code
        return
    assert "ok" not in answer and answer["banked"] is answer["adopted"] is False
    preview = answer["preview"]
    response = 20 * np.log10(np.abs(chain_response(filters, np.array(preview["freqs_hz"]))))
    side = preview["sides"]["mono"]
    np.testing.assert_allclose(side["composed_db"], response, atol=1e-10)
    np.testing.assert_allclose(side["cut_margin_db"], response - preview["cut_floor_db"], atol=1e-10)
    np.testing.assert_allclose(side["boost_margin_db"], preview["boost_cap_db"] - response, atol=1e-10)
    value = read_room_median(median)
    assert preview["residual"]["freqs_hz"] == median["freqs_hz"]
    assert preview["residual"]["level_reference_db"] == value.level_reference_db
    np.testing.assert_allclose(preview["residual"]["sides"]["mono"],
                               value.median_db + 20 * np.log10(np.abs(chain_response(filters, value.freqs_hz))), atol=1e-10)
    band_hz = [value.band_hz[0], value.ceiling_hz]
    spread = spread_rms_db(median["spread_db"], median["freqs_hz"], band_hz=band_hz)
    residual = spread_rms_db(preview["residual"]["sides"]["mono"], median["freqs_hz"], band_hz=band_hz)
    assert preview["summary"] == {
        "band_hz": band_hz, "spatial_support": spatial_support(value.n_positions),
        "seat_spread_rms_db": pytest.approx(spread),
        "sides": {"mono": {"residual_rms_db": pytest.approx(residual), "under_seat_spread": residual < spread}},
    }


def test_a_room_preview_without_a_seat_spread_says_why():
    median = _room_median()
    median.update(n_positions=1, positions=median["positions"][:1])
    preview = preview_room_prescription(_document(filters=[]), room_median=read_room_median(median),
                                        room_median_sha256=MEDIAN_SHA256, round_id="round-7", sides=SIDES)
    summary = preview["summary"]
    assert (summary["seat_spread_rms_db"], summary["spatial_support"]["reason"]) == (None, REASON_TOO_FEW_POSITIONS)
    assert summary["sides"]["mono"]["under_seat_spread"] is None


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


@pytest.mark.parametrize("freq_hz,coverage_floor_hz", [(25.0, 30.0), (55.0, 30.0), (55.0, 20.0)])
def test_room_prescription_and_preview_follow_the_measured_floor(tmp_path, freq_hz, coverage_floor_hz):
    bundle = round_inputs(bank_seat_round(tmp_path)).session_dir
    takes = tuple(replace(take, band_hz=(30.0, take.band_hz[1])) for take in select_seat_takes(bundle).takes)
    raw = room_median(takes, room_ceiling(bundle))
    if coverage_floor_hz != 30.0:
        # A grid that claims to start below the evidence is not this round's median.
        raw["coverage_hz"][0] = coverage_floor_hz
        with pytest.raises(RoomPrescriptionRefused) as excinfo:
            read_room_median(raw)
        assert excinfo.value.reason == ROOM_MEDIAN_UNAVAILABLE
        return
    document = _document(filters=[{"freq": freq_hz, "q": 3.0, "gain": -2.0}])
    if freq_hz < 30.0:
        with pytest.raises(RoomPrescriptionRefused) as excinfo:
            _read(document, raw)
        assert excinfo.value.reason == FILTER_OUTSIDE_REGION
        assert excinfo.value.evidence == {"freq_hz": freq_hz, "band_hz": raw["coverage_hz"]}
    else:
        accepted = _read(document, raw)
        assert accepted.band_hz == tuple(raw["coverage_hz"])
        assert list(accepted.sides["mono"]) == document["sides"]["mono"]
    preview = preview_room_prescription(document, room_median=read_room_median(raw),
                                        room_median_sha256=MEDIAN_SHA256, round_id="round-7", sides=SIDES)
    assert preview["freqs_hz"][0] == preview["residual"]["freqs_hz"][0] == 30.0
    assert preview["freqs_hz"][-1] == raw["ceiling_hz"]


@pytest.mark.parametrize("verb", ["judge", "compose"])
@pytest.mark.parametrize("measured_base", [None, "same", "different"])
def test_document_room_section_uses_selected_median_and_keeps_basis(tmp_path, capsys, verb, measured_base):
    root = tmp_path / "candidates"
    base = publish_authored_candidate(replace(_candidate(), analysis={"measurement_status": "unmeasured"}), root=root)
    round_dir = bank_seat_round(tmp_path)
    set_id = write_manifest(round_dir, program="room")["sets"][0]["set_id"]
    median = _room_median()
    basis = {} if measured_base is None else {
        "candidate_id": base.fingerprint if measured_base == "same" else "another-speaker-tune",
        "graph_fingerprint": "played-graph",
    }
    median["evidence"] = {"basis": basis, "take_ids": [p["id"] for p in median["positions"]]}
    room_path = default_out(round_inputs(round_dir), round_dir, "room.json", set_id)
    room_path.write_text(json.dumps({"median": median, "incumbent": {"round_id": "old"}}))
    document = tmp_path / "prescription.json"
    document.write_text(json.dumps({
        "kind": "jts_prescription", "schema": 1, "base": base.fingerprint, "rationale": "room",
        "sections": {"room": _document(sha256=room_median_sha256(median))},
    }))
    args = [verb, str(document), "--round", str(round_dir), "--set", set_id, "--root", str(root)]
    assert cli.main(args) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["resolution"]["room"] == "document"
    assert len(banked_candidates(root=root)) == (1 if verb == "judge" else 2)
    if verb == "judge":
        room = answer["sections"]["room"]
        assert room["kind"] == ROOM_PRESCRIPTION_KIND
        assert room["sides"]["mono"] == ACCEPTED_FILTERS
        assert room["measured_basis"] == basis
    else:
        child = find_banked_candidate(answer["candidate_fingerprint"], root=root).candidate
        assert candidate_room_peqs(child) == tuple(PeqFilter(**entry) for entry in ACCEPTED_FILTERS)
        assert child.room_correction["basis"]["round_id"] == round_dir.name
        assert child.analysis["room_source"]["measured_basis"] == basis
        assert "base_match" not in child.analysis["room_source"]
        assert child.analysis["measurement_status"] == "unmeasured"


@pytest.mark.parametrize("n_positions,gain,count,legacy,discloses", [
    (1, -9.0, 1, False, True),
    (1, -9.0, 1, True, True),
    (3, -9.0, 1, False, False),
    (1, -6.0, 1, False, False),
    (1, -4.0, 2, False, True),
    (3, -4.0, 2, False, False),
])
def test_the_producers_spatial_support_sets_the_disclosed_cut_floor(tmp_path, n_positions, gain, count, legacy, discloses):
    round_dir = bank_seat_round(tmp_path, magnitudes_db=[np.full(SEAT_GRID_HZ.shape, -30.0)] * n_positions)
    bundle_dir = round_inputs(round_dir).session_dir
    document = room_median(select_seat_takes(bundle_dir).takes, room_ceiling(bundle_dir))
    if legacy:
        document.pop("spatial_support", None)
        document["spread_db"] = [0.0] * len(document["freqs_hz"])
    median = read_room_median(document)

    assert median.n_positions == document["n_positions"] == n_positions
    assert (median.spread_db is None) is (n_positions == 1)
    assert median.level_reference_db == pytest.approx(-30.0)
    assert np.allclose(median.median_db, 0.0)
    assert median.freqs_hz[0] >= ROOM_FLOOR_HZ and median.freqs_hz[-1] <= median.ceiling_hz
    proposal = _document(filters=[{"freq": MODE_HZ, "q": 3.0, "gain": gain}] * count)
    accepted = _read(proposal, document)
    assert accepted is not None
    assert list(accepted.sides["mono"]) == proposal["sides"]["mono"]
    # One position earns only the -6 dB envelope floor; three earn the -10 dB one.
    beyond = [entry.get("cut_beyond_spread_db") for entry in accepted.to_dict()["sides"]["mono"]]
    assert beyond == [pytest.approx(-6.0 - gain * count, abs=0.01) if discloses else None] * count
