# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""General packet blocks that are not any one prescription gate's own concern.

``accuracy_budget`` (6.5) and ``trim_history`` (6.6) both read fields the
packet already assembles from elsewhere in the tree; neither computes a new
measurement. Reuses the synthetic-bundle fixture
``test_crossover_v2_blend_prescription`` already established, on the same
reuse rule ``test_crossover_v2_driver_prescription`` follows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.evidence_packet import (
    build_crossover_evidence_packet,
    round_artifact_dir,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    UNCERTAINTY_KINDS,
    UNCERTAINTY_RANDOM,
    UNCERTAINTY_SYSTEMATIC,
    UNCERTAINTY_UNSEPARATED,
)
from jasper.active_speaker.linearization_envelope import MIC_TIERS

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


def test_repeat_floor_reads_declared_absent_never_defaulted(tmp_path):
    session, _ = _bundle(tmp_path)
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["in_capture_repeat_floor"]
    assert entry["kind"] == UNCERTAINTY_RANDOM
    assert entry["available"] is False
    assert "E2" in entry["reason"]


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
    assert entry["tier"] is None
    assert entry["trust_ceiling_hz"] is None


def test_mic_calibration_tier_reads_the_banked_candidates_own_tier(tmp_path):
    session, _ = _bundle(tmp_path)
    round_dir, _ = round_artifact_dir(session)
    assert round_dir is not None
    (round_dir / "candidate.json").write_text(json.dumps({
        "role_attenuations_db": {"woofer": -2.0, "tweeter": -2.0},
        "linearization": {
            "tweeter": {"filters": [], "mic_tier": "consumer"},
        },
    }))
    packet = build_crossover_evidence_packet(session)
    entry = packet["accuracy_budget"]["components"]["mic_calibration_tier"]
    assert entry["available"] is True
    assert entry["tier"] == "consumer"
    assert entry["tier_vocabulary"] == list(MIC_TIERS)
    assert entry["trust_ceiling_hz"] == {
        "full_to_hz": 6_000.0, "taper_zero_hz": 12_000.0,
    }


# --------------------------------------------------------------------------- #
# trim_history (ticket 6.6)
# --------------------------------------------------------------------------- #


def _sibling_bundle(
    root: Path,
    name: str,
    *,
    started_at: float,
    trim_db: dict[str, float] | None = None,
    linearization: dict[str, Any] | None = None,
) -> Path:
    """One MINIMAL bundle directly under ``root`` -- the TRUE production
    shape (``info.json`` inside the bundle dir itself), unlike this file's
    own ``_bundle`` helper, which nests everything one level under a
    "session" subdirectory for its own readability. A trim-history read
    across SIBLING bundles needs each one's ``info.json`` at the level
    ``bundles.list_bundles`` actually scans -- ``session_dir.parent`` in
    production, which for ``_bundle``'s own shape is one level too shallow.
    """
    bundle_dir = root / name
    round_dir = bundle_dir / "evidence/v1/artifacts/crossover_v2" / f"relay_{name}"
    round_dir.mkdir(parents=True)
    (bundle_dir / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": name, "state": "closed", "started_at": started_at,
    }))
    if trim_db is not None:
        (round_dir / "candidate.json").write_text(json.dumps({
            "role_attenuations_db": trim_db,
            "linearization": linearization or {},
        }))
    return bundle_dir


def test_trim_history_reads_sibling_bundles_oldest_first(tmp_path):
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
    history = packet["trim_history"]
    assert history["available"] is True
    assert history["max_rounds"] == 8
    assert history["rounds_covered"] == 3
    assert [row["ordinal"] for row in history["rounds"]] == [1, 2, 3]
    assert [row["round_id"] for row in history["rounds"]] == [
        "relay_r1", "relay_r2", "relay_r3",
    ]
    # The campaign's own runaway signature, oldest first -- readable left to
    # right, and no drift verdict published about it.
    assert [row["trim_db"]["tweeter"] for row in history["rounds"]] == [
        -1.4, -2.1, -3.1,
    ]
    assert all("verdict" not in row for row in history["rounds"])


def test_trim_history_pinned_roles_carry_their_own_flag(tmp_path):
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
    row = packet["trim_history"]["rounds"][0]
    assert row["pinned"] == {"woofer": False, "tweeter": True}


def test_trim_history_empty_shape_when_no_round_banked_a_candidate(tmp_path):
    root = tmp_path / "bundles"
    root.mkdir()
    only = _sibling_bundle(root, "r1", started_at=1.0)
    packet = build_crossover_evidence_packet(only)
    history = packet["trim_history"]
    assert history["available"] is False
    assert history["rounds_covered"] == 0
    assert history["rounds"] == []
    assert history["max_rounds"] == 8


def test_trim_history_is_bounded_at_max_rounds(tmp_path):
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
    history = packet["trim_history"]
    assert history["rounds_covered"] == 8
    assert [row["round_id"] for row in history["rounds"]] == [
        f"relay_r{i}" for i in range(3, 11)
    ]
