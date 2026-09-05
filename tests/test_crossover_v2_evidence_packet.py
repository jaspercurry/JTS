# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""General packet blocks that are not any one prescription gate's own concern.

``accuracy_budget`` (6.5) and ``structural_history`` (6.6) both read fields the
packet already assembles from elsewhere in the tree; neither computes a new
measurement. Reuses the synthetic-bundle fixture
``test_crossover_v2_blend_prescription`` already established, on the same
reuse rule ``test_crossover_v2_driver_prescription`` follows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jasper.active_speaker.attempts_loop import CLAIM_FLOOR_P95_MULTIPLE
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    MEASURE_KIND_KEY,
    POSITION_EVIDENCE_KIND,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    NO_CANDIDATE_TAKES,
    REPEAT_FLOOR_UNMEASURED,
    REPEAT_FLOOR_UNREADABLE,
    REPEAT_FLOOR_UNUSABLE,
    STRUCTURAL_HISTORY_AXES,
    build_crossover_evidence_packet,
    round_artifact_dir,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    UNCERTAINTY_KINDS,
    UNCERTAINTY_RANDOM,
    UNCERTAINTY_SYSTEMATIC,
    UNCERTAINTY_UNSEPARATED,
)
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CLOUD_MEASURE,
    PHASE_CLOUD_VERIFY,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.round_evidence import (
    ITERATION_PLATEAU_DB,
    MEASURED_BENEFIT_MARGIN_DB,
)
from jasper.active_speaker.linearization_envelope import MIC_TIERS
from jasper.active_speaker.repeat_floor import (
    REPEAT_FLOOR_KIND,
    SCHEMA_VERSION as REPEAT_FLOOR_SCHEMA_VERSION,
    SHIPPED_POOL_METRIC,
    write_repeat_floor,
)

from tests.test_crossover_v2_blend_prescription import _bundle

# --------------------------------------------------------------------------- #
# accuracy_budget (ticket 6.5)
# --------------------------------------------------------------------------- #


def test_every_accuracy_budget_component_labels_its_own_kind(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    components = packet["accuracy_budget"]["components"]
    assert set(components) == {
        "cross_seat_position_spread",
        "in_capture_repeat_floor",
        "gate_leakage",
        "mic_calibration_tier",
    }
    # UNCERTAINTY_KINDS is the closed random/systematic set and deliberately
    # excludes UNSEPARATED (the substrate rule's third, non-poolable label).
    valid_kinds = UNCERTAINTY_KINDS | {UNCERTAINTY_UNSEPARATED}
    for name, entry in components.items():
        assert entry["kind"] in valid_kinds, name
        assert isinstance(entry["available"], bool), name


def test_cross_seat_component_points_at_the_positions_block_it_mirrors(tmp_path):
    """The default fixture's 4 onax/offax positions give cross_seat_sigma a
    real reading; this component juxtaposes it rather than re-embedding it."""
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["cross_seat_position_spread"]
    assert entry["kind"] == UNCERTAINTY_UNSEPARATED
    assert entry["available"] is True
    assert entry["n_seats"] == packet["positions"]["cross_seat_sigma"]["n_seats"]
    assert entry["reason"] == ""
    assert "per_bin_sigma_db" not in entry


def _floor_record() -> dict[str, Any]:
    """One banked record, written through the REAL writer by every test below."""
    return {
        "artifact_schema_version": REPEAT_FLOOR_SCHEMA_VERSION,
        "kind": REPEAT_FLOOR_KIND,
        "measured_at": "2026-09-01T00:00:00Z",
        "n_repeats": 3,
        "aggregate_metric": SHIPPED_POOL_METRIC,
        "rounds": [
            {"label": "r1", "bundle_session_id": "sess1",
             "graph_fingerprint": "gf1", "mic_calibration_id": "cal1",
             "started_at": 1.0},
            {"label": "r2", "bundle_session_id": "sess2",
             "graph_fingerprint": "gf1", "mic_calibration_id": "cal1",
             "started_at": 2.0},
            {"label": "r3", "bundle_session_id": "sess3",
             "graph_fingerprint": None, "mic_calibration_id": "cal1",
             "started_at": 3.0},
        ],
        "metrics": {
            SHIPPED_POOL_METRIC: {
                "n": 3, "mean_db": 1.0, "sd_db": 0.5, "range_db": 1.0,
                "min_db": 0.5, "max_db": 1.5,
                "pairwise_abs_delta_p95_db": 0.4,
                "pairwise_abs_delta_median_db": 0.3,
            },
        },
        "note": "test vector",
    }


def test_repeat_floor_reads_declared_absent_never_defaulted(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["in_capture_repeat_floor"]
    assert entry["kind"] == UNCERTAINTY_RANDOM
    assert entry["available"] is False
    assert entry["absence"] == REPEAT_FLOOR_UNMEASURED
    assert "E2" in entry["reason"]
    # Absent means the consumers fall back to the two constants that
    # self-describe as assumptions, and the packet says which source it used.
    assert entry["thresholds"]["source"] == "codified_assumption"
    assert entry["thresholds"]["margin_db"] == MEASURED_BENEFIT_MARGIN_DB
    assert entry["thresholds"]["plateau_db"] == ITERATION_PLATEAU_DB


def test_repeat_floor_banked_but_unreadable_falls_back_to_the_assumptions(tmp_path):
    """A record that exists but carries no finite aggregate p95 is a floor that
    cannot be read, not a floor nobody measured — unavailable either way, and
    the thresholds fall back rather than deriving from a non-number."""
    session, _ = _bundle(tmp_path)
    floor_path = tmp_path / "repeat-floor.json"
    write_repeat_floor(
        {**_floor_record(), "metrics": {SHIPPED_POOL_METRIC: {
            "n": 3, "mean_db": 1.0, "sd_db": 0.5, "range_db": 1.0,
            "min_db": 0.5, "max_db": 1.5,
            "pairwise_abs_delta_p95_db": float("nan"),
            "pairwise_abs_delta_median_db": 0.3,
        }}},
        state_path=floor_path,
    )
    packet = build_crossover_evidence_packet(session, repeat_floor_path=floor_path)
    entry = packet["accuracy_budget"]["components"]["in_capture_repeat_floor"]

    assert entry["kind"] == UNCERTAINTY_RANDOM
    assert entry["available"] is False
    assert entry["absence"] == REPEAT_FLOOR_UNUSABLE
    assert entry["thresholds"]["source"] == "codified_assumption"
    assert entry["thresholds"]["margin_db"] == MEASURED_BENEFIT_MARGIN_DB
    assert entry["thresholds"]["plateau_db"] == ITERATION_PLATEAU_DB


@pytest.mark.parametrize("on_disk", ["{not json", "{}"], ids=["not-json", "not-a-floor"])
def test_repeat_floor_file_that_is_not_a_record_is_unreadable_not_unmeasured(
    tmp_path, on_disk
):
    """A file that is there but is not a floor is a re-copy errand, never an
    invitation to run E2 again."""
    session, _ = _bundle(tmp_path)
    floor_path = tmp_path / "repeat-floor.json"
    floor_path.write_text(on_disk)
    packet = build_crossover_evidence_packet(session, repeat_floor_path=floor_path)
    entry = packet["accuracy_budget"]["components"]["in_capture_repeat_floor"]
    assert entry["available"] is False
    assert entry["absence"] == REPEAT_FLOOR_UNREADABLE
    assert entry["thresholds"]["source"] == "codified_assumption"


def test_repeat_floor_reads_the_banked_record_when_present(tmp_path):
    session, _ = _bundle(tmp_path)
    floor_path = tmp_path / "repeat-floor.json"
    record = write_repeat_floor(_floor_record(), state_path=floor_path)
    packet = build_crossover_evidence_packet(session, repeat_floor_path=floor_path)
    entry = packet["accuracy_budget"]["components"]["in_capture_repeat_floor"]
    p95 = record["metrics"][SHIPPED_POOL_METRIC]["pairwise_abs_delta_p95_db"]

    assert entry["kind"] == UNCERTAINTY_RANDOM
    assert entry["available"] is True
    assert entry["absence"] is None
    assert entry["n_repeats"] == record["n_repeats"]
    assert entry["aggregate_metric"] == SHIPPED_POOL_METRIC
    assert entry["bundle_session_ids"] == ["sess1", "sess2", "sess3"]
    assert entry["graph_fingerprints"] == ["gf1"]
    assert entry["thresholds"]["source"] == "banked_repeat_floor"
    assert entry["thresholds"]["margin_db"] == pytest.approx(
        CLAIM_FLOOR_P95_MULTIPLE * p95
    )
    assert entry["thresholds"]["plateau_db"] == pytest.approx(p95)


def test_gate_leakage_is_absent_when_no_capture_carries_a_gate_number(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["gate_leakage"]
    assert entry["kind"] == UNCERTAINTY_SYSTEMATIC
    assert entry["available"] is False
    assert entry["reason"]


def test_gate_leakage_is_available_when_a_position_carries_the_reading(tmp_path):
    session, _ = _bundle(
        tmp_path,
        position_over={
            "gate_moved_rms_db": 0.31, "gate_reflection_delay_ms": 2.4,
        },
    )
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["gate_leakage"]
    assert entry["available"] is True
    assert entry["reason"] == ""


def test_mic_calibration_tier_reads_absent_with_no_banked_candidate(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["mic_calibration_tier"]
    assert entry["kind"] == UNCERTAINTY_SYSTEMATIC
    assert entry["available"] is False
    assert entry["tier_by_role"] == {}
    assert entry["trust_ceiling_hz_by_tier"] == {}


def test_mic_calibration_tier_publishes_each_roles_own_tier(tmp_path):
    """Roles fitted under DIFFERENT tiers surface as the disagreement they
    are — never collapsed to whichever entry a dict happened to yield first.
    """
    session, _ = _bundle(tmp_path)
    round_dir, _ = round_artifact_dir(session)
    assert round_dir is not None
    (round_dir / "candidate.json").write_text(json.dumps({
        "role_attenuations_db": {"woofer": -2.0, "tweeter": -2.0},
        "linearization": {
            "woofer": {"filters": [], "mic_tier": "phone"},
            "tweeter": {"filters": [], "mic_tier": "consumer"},
        },
    }))
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["mic_calibration_tier"]
    assert entry["available"] is True
    assert entry["tier_by_role"] == {"woofer": "phone", "tweeter": "consumer"}
    assert entry["tier_vocabulary"] == list(MIC_TIERS)
    assert entry["trust_ceiling_hz_by_tier"] == {
        "phone": {"full_to_hz": 3_000.0, "taper_zero_hz": 8_000.0},
        "consumer": {"full_to_hz": 6_000.0, "taper_zero_hz": 12_000.0},
    }


# --------------------------------------------------------------------------- #
# structural_history (ticket 6.6, #3484)
# --------------------------------------------------------------------------- #


def _sibling_bundle(
    root: Path,
    name: str,
    *,
    started_at: float,
    trim_db: dict[str, float] | None = None,
    linearization: dict[str, Any] | None = None,
    alignment: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    fc_hz: float | None = None,
) -> Path:
    """One MINIMAL bundle directly under ``root`` -- the TRUE production
    shape (``info.json`` inside the bundle dir itself), unlike this file's
    own ``_bundle`` helper, which nests everything one level under a
    "session" subdirectory for its own readability. A structural-history read
    across SIBLING bundles needs each one's ``info.json`` at the level
    ``bundles.list_bundles`` actually scans -- ``session_dir.parent`` in
    production, which for ``_bundle``'s own shape is one level too shallow.
    """
    bundle_dir = root / name
    round_dir = bundle_dir / "evidence/v1/artifacts/crossover_v2" / f"capture_{name}"
    round_dir.mkdir(parents=True)
    (bundle_dir / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": name, "state": "closed", "started_at": started_at,
    }))
    if trim_db is not None or alignment is not None:
        (round_dir / "candidate.json").write_text(json.dumps({
            "role_attenuations_db": trim_db or {},
            "linearization": linearization or {},
            "alignment": alignment or {},
            "analysis": analysis or {},
            "source_preset": (
                {} if fc_hz is None
                else {"crossover_regions": [{"fc_hz": fc_hz, "order": 4}]}
            ),
        }))
    return bundle_dir


def test_the_history_reads_sibling_bundles_oldest_first(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir()
    _sibling_bundle(
        root, "r1", started_at=1.0,
        trim_db={"woofer": -1.4, "tweeter": -1.4},
    )
    _sibling_bundle(
        root, "r2", started_at=2.0,
        trim_db={"woofer": -1.4, "tweeter": -2.1},
    )
    latest = _sibling_bundle(
        root, "r3", started_at=3.0,
        trim_db={"woofer": -1.4, "tweeter": -3.1},
    )
    packet = build_crossover_evidence_packet(latest)
    history = packet["structural_history"]
    assert history["available"] is True
    assert history["max_rounds"] == 8
    assert history["rounds_covered"] == 3
    assert [row["ordinal"] for row in history["rounds"]] == [1, 2, 3]
    assert [row["round_id"] for row in history["rounds"]] == [
        "capture_r1", "capture_r2", "capture_r3",
    ]
    # The campaign's own runaway signature, oldest first -- readable left to
    # right, and no drift verdict published about it.
    assert [
        row["axes"]["trim_db"]["value"]["tweeter"] for row in history["rounds"]
    ] == [-1.4, -2.1, -3.1]
    assert all("verdict" not in row for row in history["rounds"])


def test_pinned_trim_roles_carry_their_own_flag(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir()
    only = _sibling_bundle(
        root, "r1", started_at=1.0,
        trim_db={"woofer": -1.4, "tweeter": -2.0},
        linearization={
            "woofer": {"filters": []},
            "tweeter": {"filters": [], "trim_pinned": True},
        },
    )
    packet = build_crossover_evidence_packet(only)
    row = packet["structural_history"]["rounds"][0]
    assert row["axes"]["trim_db"]["pinned"] == {"woofer": False, "tweeter": True}


def test_the_history_is_empty_when_no_round_banked_a_candidate(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir()
    only = _sibling_bundle(root, "r1", started_at=1.0)
    packet = build_crossover_evidence_packet(only)
    history = packet["structural_history"]
    assert history["available"] is False
    assert history["rounds_covered"] == 0
    assert history["rounds"] == []
    assert history["max_rounds"] == 8


def test_the_history_carries_every_declared_structural_axis(tmp_path):
    """#3484: the cross-round surface stops being trim-only.

    The witnessed defect: r4 applied ``invert``, r5's candidate re-derived
    ``keep`` at an essentially unchanged delay, ``polarity_pinned`` false, and
    the review surface for a flipped structural axis was byte-identical to one
    that held it. The trim's own runaway was caught the same night precisely
    BECAUSE its per-round values were lined up here. Every axis a prescription
    can pin now gets the same row, and the axis list is a declaration rather
    than a branch per axis, so an axis cannot be silently left out of it.
    """
    root = tmp_path / "bundles"
    root.mkdir()
    _sibling_bundle(
        root, "r4", started_at=4.0,
        trim_db={"woofer": 0.0, "tweeter": -12.481},
        alignment={"delay_us": 76.265, "polarity": "invert"},
        analysis={"polarity_pinned": False},
        fc_hz=1800.0,
    )
    latest = _sibling_bundle(
        root, "r5", started_at=5.0,
        trim_db={"woofer": 0.0, "tweeter": -2.65},
        alignment={"delay_us": 76.300, "polarity": "keep"},
        analysis={"polarity_pinned": False},
        fc_hz=1800.0,
    )
    packet = build_crossover_evidence_packet(latest)
    history = packet["structural_history"]
    assert history["axes"] == list(STRUCTURAL_HISTORY_AXES)
    rows = history["rounds"]
    assert [row["ordinal"] for row in rows] == [1, 2]
    # Every row answers for every declared axis — an axis with nothing banked
    # says so with a null, never by being missing.
    for row in rows:
        assert set(row["axes"]) == set(STRUCTURAL_HISTORY_AXES)
    # The flip, readable left to right, exactly as the trim runaway already is.
    assert [row["axes"]["polarity"]["value"] for row in rows] == [
        "invert", "keep",
    ]
    assert [row["axes"]["polarity"]["pinned"] for row in rows] == [False, False]
    assert [row["axes"]["delay_us"]["value"] for row in rows] == [76.265, 76.300]
    assert [row["axes"]["trim_db"]["value"]["tweeter"] for row in rows] == [
        -12.481, -2.65,
    ]
    assert [row["axes"]["crossover_fc_hz"]["value"] for row in rows] == [
        1800.0, 1800.0,
    ]
    assert all("verdict" not in row for row in rows)


def test_a_round_that_banked_only_an_alignment_still_gets_a_row(tmp_path):
    """Row admission follows the declared axes, not the trim alone: a
    candidate that names ANY structural axis is a round whose structure can
    have moved, and a skipped row is the silence this block exists to end."""
    root = tmp_path / "bundles"
    root.mkdir()
    only = _sibling_bundle(
        root, "r1", started_at=1.0,
        alignment={"delay_us": -405.7, "polarity": "keep"},
    )
    packet = build_crossover_evidence_packet(only)
    rows = packet["structural_history"]["rounds"]
    assert len(rows) == 1
    assert rows[0]["axes"]["delay_us"]["value"] == -405.7
    assert rows[0]["axes"]["trim_db"]["value"] == {}


def test_the_history_is_bounded_at_max_rounds(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir()
    latest = None
    for i in range(1, 11):
        latest = _sibling_bundle(
            root, f"r{i}", started_at=float(i),
            trim_db={"woofer": -1.0, "tweeter": -float(i) / 10.0},
        )
    assert latest is not None
    packet = build_crossover_evidence_packet(latest)
    history = packet["structural_history"]
    assert history["rounds_covered"] == 8
    assert [row["round_id"] for row in history["rounds"]] == [
        f"capture_r{i}" for i in range(3, 11)
    ]


# --------------------------------------------------------------------------- #
# candidates (#3498 WP4)
# --------------------------------------------------------------------------- #


def _bank_candidate_take(
    round_dir: Path, *, take_id: str, candidate_id: str, position_deg: int,
    phase: str | None = None,
) -> None:
    """One banked take carrying the candidate it measured.

    The ENGINE's record shape as ``record_store.bank`` envelopes it: the
    artifact kind and ``measure_kind`` the store writes, and no ``phase`` —
    ``session._record`` carries none, which is the reason this block cannot
    select on one. ``phase`` is passed only to stand in for the walk's own
    lateral take, which carries both.
    """
    positions = round_dir / "positions"
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{take_id}.json").write_text(json.dumps({
        "kind": POSITION_EVIDENCE_KIND,
        MEASURE_KIND_KEY: MEASURE_KIND_CANDIDATE,
        "take_id": take_id,
        "candidate_id": candidate_id,
        "position_deg": position_deg,
        "vertical_deg": 0,
        **({"phase": phase} if phase is not None else {}),
    }))


def test_candidates_reads_absent_when_no_take_names_one(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    assert packet["candidates"]["available"] is False
    assert packet["candidates"]["reason"] == NO_CANDIDATE_TAKES
    assert "candidates" in {row["field"] for row in packet["not_evaluated"]}


def test_candidates_groups_the_takes_by_the_candidate_they_measured(tmp_path):
    session, _ = _bundle(tmp_path)
    round_dir, _ = round_artifact_dir(session)
    assert round_dir is not None
    # Both producers: the engine's phase-less candidate record, and a walk
    # take that carries the lateral phase beside its candidate id.
    for take_id, candidate_id, deg, phase in (
        ("t1", "cand_b", 0, None),
        ("t2", "cand_a", 0, None),
        ("t3", "cand_a", -20, PHASE_LATERAL),
    ):
        _bank_candidate_take(
            round_dir, take_id=take_id, candidate_id=candidate_id,
            position_deg=deg, phase=phase,
        )
    packet = build_crossover_evidence_packet(session)
    block = packet["candidates"]
    assert block["available"] is True
    assert [row["candidate_id"] for row in block["candidates"]] == [
        "cand_a", "cand_b",
    ]
    assert block["candidates"][0]["n_takes"] == 2
    assert block["candidates"][0]["poses"] == [
        {"position_deg": -20, "vertical_deg": 0},
        {"position_deg": 0, "vertical_deg": 0},
    ]
    assert "candidates" not in {row["field"] for row in packet["not_evaluated"]}
    assert packet["packet_fingerprint"]


# --------------------------------------------------------------------------- #
# session.declared_geometry (#3498) — the fifth banked SSOT sibling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stored, room",
    [
        (
            json.dumps({
                "speaker_height_m": 0.9, "mic_height_m": 1.0,
                "distance_m": 1.05, "ceiling_height_m": 2.4,
            }),
            {
                "speaker_height_m": 0.9, "mic_height_m": 1.0,
                "distance_m": 1.05, "ceiling_height_m": 2.4,
            },
        ),
        (None, None),
        ("{ not a document", None),
    ],
    ids=["declared", "absent", "unreadable"],
)
def test_the_session_block_reads_the_declaration_the_caller_resolved(
    tmp_path, stored, room,
):
    """The room comes from the path the CALLER hands in, never an on-box read.

    A banked round freezes its declaration beside the bundle like the other
    SSOT documents, so a round read on another machine reports the room the
    SPEAKER declared rather than that machine's. Never declared (most
    households) and declared-but-unreadable are two different facts about the
    round, kept apart because an offline reader must not derive an
    entanglement floor from a room nobody stated.
    """
    session, _ = _bundle(tmp_path)
    declared = tmp_path / "declared-geometry.json"
    if stored is not None:
        declared.write_text(stored, encoding="utf-8")

    block = build_crossover_evidence_packet(
        session, declared_geometry_path=declared
    )["session"]["declared_geometry"]
    if room is not None:
        assert block == room
        return
    assert block["status"] == "not_evaluated"
    assert block["field"] == "declared_geometry"
    assert (block["reason"] == "source_absent") is (stored is None)


def test_an_unbanked_declaration_never_reads_the_machine_building_the_packet(
    tmp_path,
):
    """No path handed in means NO ROOM — not whatever this machine declares.

    The packet is rebuilt by every reader, so a builder that fell back to the
    on-box SSOT would make a banked round's room, and its
    ``packet_fingerprint``, drift to the reading speaker's own declaration.
    """
    session, _ = _bundle(tmp_path)

    block = build_crossover_evidence_packet(session)["session"]["declared_geometry"]

    assert block["status"] == "not_evaluated"
    assert block["reason"] == "source_absent"


# --------------------------------------------------------------------------- #
# findings — every phase, whether it ran, and the band that bounds the set
# --------------------------------------------------------------------------- #


def _bank_finding_set(round_dir: Path, phase: str, *, findings: list[Any]) -> None:
    """One phase's banked set — the keys the packet reads out of one."""
    (round_dir / f"findings_{phase}.json").write_text(json.dumps({
        "produced_by": f"test.{phase}",
        "findings": findings,
        "field_descriptions": {"finding": {"mechanism": "prose"}},
    }))


def test_the_packet_reads_a_finding_set_from_every_phase_that_banks_one(tmp_path):
    """Three phases bank findings; a reader of one alone loses the other two.

    The M7 level-frame finding is banked under ``measure`` and the carve-out
    sets under the two cloud closes, so a packet that opened only
    ``cloud_verify`` could not see a level-frame disagreement at all.
    """
    session, _ = _bundle(tmp_path)
    round_dir, _ = round_artifact_dir(session)
    assert round_dir is not None
    _bank_finding_set(round_dir, PHASE_MEASURE, findings=[{"mechanism": "M7"}])
    _bank_finding_set(
        round_dir, PHASE_CLOUD_MEASURE, findings=[{"mechanism": "M2"}],
    )

    block = build_crossover_evidence_packet(session)["findings"]

    # Scalar-first: the summary lands before either per-phase list.
    assert list(block) == ["summary", "phases", "field_descriptions"]
    assert set(block["phases"]) == {
        PHASE_MEASURE, PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY,
    }
    assert block["summary"]["finding_count"] == {
        PHASE_MEASURE: 1, PHASE_CLOUD_MEASURE: 1, PHASE_CLOUD_VERIFY: 0,
    }
    assert block["phases"][PHASE_MEASURE]["findings"] == [{"mechanism": "M7"}]


def test_a_finding_set_that_ran_and_found_nothing_is_not_one_that_never_ran(
    tmp_path,
):
    """``present``/``produced_by`` per phase — the distinction ``produced_by``
    exists for. The fixture banks an empty cloud-verify set and nothing else."""
    session, _ = _bundle(tmp_path)
    round_dir, _ = round_artifact_dir(session)
    assert round_dir is not None

    phases = build_crossover_evidence_packet(session)["findings"]["phases"]
    assert phases[PHASE_CLOUD_VERIFY]["present"] is True
    assert phases[PHASE_CLOUD_VERIFY]["produced_by"]
    assert phases[PHASE_CLOUD_VERIFY]["reason"] == ""
    assert phases[PHASE_MEASURE]["present"] is False
    assert phases[PHASE_MEASURE]["produced_by"] is None
    assert phases[PHASE_MEASURE]["reason"] == "source_absent"

    (round_dir / f"findings_{PHASE_CLOUD_VERIFY}.json").unlink()
    unbanked = build_crossover_evidence_packet(session)["findings"]
    assert unbanked["summary"]["phases_present"] == []
    assert unbanked["summary"]["finding_count"] == {
        PHASE_MEASURE: None, PHASE_CLOUD_MEASURE: None, PHASE_CLOUD_VERIFY: None,
    }

    # The third absence: a set IS banked and this install cannot read it.
    (round_dir / f"findings_{PHASE_CLOUD_MEASURE}.json").write_text("[]")
    unreadable = build_crossover_evidence_packet(session)["findings"]["phases"]
    assert unreadable[PHASE_CLOUD_MEASURE]["present"] is False
    assert unreadable[PHASE_CLOUD_MEASURE]["reason"] != "source_absent"


def test_the_findings_block_publishes_the_band_that_bounds_the_finding_set(
    tmp_path,
):
    """Carve-out promotion reads the echo band only, so nothing below it can
    ever be a finding in those two sets; the packet publishes the resolved band
    and names which sets it bounds."""
    session, _ = _bundle(tmp_path, cloud_over={"echo_band_hz": [4000.0, 16000.0]})
    summary = build_crossover_evidence_packet(session)["findings"]["summary"]
    assert summary["echo_band_hz"] == [4000.0, 16000.0]
    # The level-frame set is not scanned FOR, so the band does not bound it.
    assert summary["echo_band_bounds"] == [PHASE_CLOUD_MEASURE, PHASE_CLOUD_VERIFY]
