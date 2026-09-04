# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The H2/H3 reading: what the instrument refuses, and what the packet says.

Two halves, pinned separately because they are two modules with one file
between them. :mod:`jasper.active_speaker.crossover_v2.harmonic_evidence` reads
banked MEASURE captures and files a document; the evidence packet reads that
document and declares it. The join is ``harmonic_distortion.json``, and the
thing most worth pinning is the one the ticket is about: the packet's
``not_evaluated`` row for harmonics must DISAPPEAR when a reading is banked and
must be present, by name, when one is not.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import harmonic_evidence as he
from jasper.active_speaker.crossover_v2.feature_classifier import (
    FeatureClassificationRefused,
    load_round_captures,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    HARMONICS_ARTIFACT,
    PACKET_SCHEMA_VERSION,
    _harmonics_uncertainty,
    build_crossover_evidence_packet,
)
from jasper.active_speaker.crossover_v2.feature_classification import (
    UNCERTAINTY_KINDS,
    UNCERTAINTY_RANDOM,
)
from jasper.audio_measurement.distortion import DriveLevel, HarmonicReading
from jasper.audio_measurement.sweep import synchronized_sweep_metadata
from jasper.cli._refusal import EXIT_REFUSED

ORDERS = (2, 3)


# --------------------------------------------------------------------------- #
# fixtures — a bundle, a ring, and a reading
# --------------------------------------------------------------------------- #


def _bundle(tmp_path: Path, *, harmonics: dict[str, Any] | None = None) -> Path:
    """A commissioning bundle on disk, in the real tree shape.

    Deliberately minimal: the harmonics block reads exactly one file out of the
    round directory, and a fixture that also staged a receipt and a cloud
    artifact would let a test pass for a reason it did not name.
    """
    session = tmp_path / "session"
    round_dir = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    round_dir.mkdir(parents=True)
    (session / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "c2a1812b849e",
        "fingerprints": {"build_sha": "200d54578"},
    }))
    if harmonics is not None:
        (round_dir / HARMONICS_ARTIFACT).write_text(json.dumps(harmonics))
    return session


def _reading(
    role: str = "woofer",
    *,
    f1: float = 150.0,
    f2: float = 4000.0,
    offset_db: float = 0.0,
) -> HarmonicReading:
    """One real :class:`HarmonicReading` on a small synthetic grid.

    A real one rather than a stand-in, because ``_role_block`` reads six of its
    attributes and one of its methods, and a double would let the block drift
    away from the dataclass it consumes.
    """
    meta = synchronized_sweep_metadata(
        f1=f1, f2=f2, duration_approx_s=1.0, sample_rate=48_000, amplitude_dbfs=-6.0
    )
    freqs = np.array([200.0, 400.0, 800.0, 1000.0, 1500.0, 2000.0])
    # H3 is NaN past f2/3, which on this sweep is 1333 Hz — the two top bins.
    h3 = np.array([-40.0, -45.0, -50.0, -52.0, np.nan, np.nan]) + offset_db
    return HarmonicReading(
        segment_id=f"{role}_sweep",
        role=role,
        orders=ORDERS,
        band_hz=(178.4, 2000.0),
        freqs_hz=freqs,
        fundamental_db=np.full(freqs.shape, -20.0),
        relative_db={
            2: np.array([-50.0, -55.0, -60.0, -58.0, -57.0, -56.0]) + offset_db,
            3: h3,
        },
        floor_relative_db={
            # H2's floor sits 2 dB under its reading at 200 Hz (floor-limited)
            # and far below everywhere else.
            2: np.array([-52.0, -70.0, -75.0, -75.0, -75.0, -75.0]),
            3: np.array([-70.0, -70.0, -75.0, -75.0, np.nan, np.nan]),
        },
        thd_percent=np.array([1.0, 0.5, 0.3, 0.25, np.nan, np.nan]),
        drive=DriveLevel(
            stimulus_peak_dbfs=-6.0,
            effective_peak_dbfs=-26.0,
            capture_peak_dbfs=-14.75,
            capture_rms_dbfs=-25.96,
            notes={"wav_sha256_12": "abcdef012345"},
        ),
        sweep=meta,
        pre_guard_s=1.5,
        required_pre_guard_s=1.4,
        preceding_silence_s=1.8,
        smoothing_fraction=12,
    )


def _artifact(n_roles: int = 1) -> dict[str, Any]:
    """A banked reading, built by the block the instrument builds it with."""
    roles = [
        he._role_block("woofer", [_reading(), _reading(offset_db=0.6)], "abcdef012345", ORDERS)
    ]
    if n_roles > 1:
        roles.append(
            he._role_block("woofer", [_reading(), _reading(offset_db=0.2)], "0123abcdef45", ORDERS)
        )
    return {
        "artifact_kind": "jts_crossover_v2_harmonic_distortion",
        "artifact_schema_version": he.HARMONICS_SCHEMA_VERSION,
        "round_dir": "cap_TESTONLY",
        "orders": list(ORDERS),
        "program": {
            "program_id": "b542773d8a8d",
            "crossover_fc_hz": 1648.7,
            "state_capture_session_id": "wired-TESTONLY",
        },
        "captures": {"n_read": n_roles, "n_refused": 0, "refused": []},
        "calibration": {"applied": False},
        "roles": roles,
    }


# --------------------------------------------------------------------------- #
# the ticket — the not_evaluated row opens and closes
# --------------------------------------------------------------------------- #


def _not_evaluated_fields(packet: dict[str, Any]) -> set[str]:
    return {entry["field"] for entry in packet["not_evaluated"]}


def test_a_round_with_no_reading_says_so_about_that_round_by_name(tmp_path):
    """No artifact: the block refuses, and the honest list names the field.

    The reason must be about THIS round rather than about the corpus. The
    sentence it replaced ("H2/H3 are computable from banked captures but no
    round writes them") was a corpus-wide claim, and half of it stopped being
    true the moment an instrument existed to write one.
    """
    packet = build_crossover_evidence_packet(_bundle(tmp_path))

    block = packet["harmonics"]
    assert block["available"] is False
    assert block["status"] == "not_evaluated"
    assert block["n_roles"] == 0
    assert "harmonics" in _not_evaluated_fields(packet)
    assert not any(
        entry["field"] == "harmonic_distortion" for entry in packet["not_evaluated"]
    )
    # The corpus-wide claim is gone from the reason, not merely from the field
    # name: a reader told "no round writes them" would not go and run the
    # instrument that does.
    reason = next(
        entry["reason"] for entry in packet["not_evaluated"]
        if entry["field"] == "harmonics"
    )
    assert "no round writes" not in reason


def test_a_banked_reading_closes_the_row_and_carries_the_rows(tmp_path):
    """Ticket 1.4, as the state change it is: the row is gone and H2/H3 are in.

    This is the assertion the whole change exists for. A packet that carried
    the rows AND still printed "there is no distortion record to carry" would
    be the exact dishonesty the ``not_evaluated`` block exists to prevent.
    """
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))

    block = packet["harmonics"]
    assert block["available"] is True
    assert block["orders"] == [2, 3]
    assert block["n_roles"] == 1
    assert "harmonics" not in _not_evaluated_fields(packet)

    row = block["roles"][0]["rows"][0]
    assert row["hz"] == 200.0
    assert row["h2_below_fundamental_db"] is not None
    assert row["h3_below_fundamental_db"] is not None


def test_a_banked_artifact_with_no_role_block_refuses_rather_than_reading_empty(tmp_path):
    """A file present but empty is not a reading, and must not close the row."""
    artifact = _artifact()
    artifact["roles"] = []
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=artifact))

    assert packet["harmonics"]["available"] is False
    assert "harmonics" in _not_evaluated_fields(packet)


@pytest.mark.parametrize("banked", ["[]", '["a list"]', '"a string"', "7"])
def test_an_artifact_that_is_not_an_object_still_names_itself_in_the_honest_list(
    tmp_path, banked
):
    """The honest list must have no silent gaps, including this one.

    A file that is absent or unreadable carries a read reason; a file that
    PARSED into something that is not an object carries the empty string,
    because the read succeeded. The not_evaluated builder drops any entry whose
    reason is falsy, so passing that empty string through would have removed
    the row from the one block whose entire job is to have no gaps.
    """
    session = _bundle(tmp_path)
    round_dir = next((session / "evidence/v1/artifacts/crossover_v2").iterdir())
    (round_dir / HARMONICS_ARTIFACT).write_text(banked)

    packet = build_crossover_evidence_packet(session)

    assert packet["harmonics"]["available"] is False
    assert packet["harmonics"]["reason"].strip()
    assert "harmonics" in _not_evaluated_fields(packet)
    for entry in packet["not_evaluated"]:
        assert entry["reason"].strip(), entry["field"]


@pytest.mark.parametrize("orders", [[], None, ["2"], [True], "23"])
def test_an_artifact_naming_no_order_refuses_rather_than_publishing_undeclared(
    tmp_path, orders
):
    """The one way this block could quietly break the rule it exists to keep.

    Every row column is declared by generating the declaration FROM the order
    list, so an artifact that names no readable order would publish `h2_`/`h3_`
    columns with nothing declaring them. `True` is in here because `bool`
    subclasses `int` in Python: admitted, it would declare an "h1" no row
    carries while still leaving the real columns undeclared.
    """
    artifact = _artifact()
    artifact["orders"] = orders
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=artifact))

    assert packet["harmonics"]["available"] is False
    assert "no harmonic order" in packet["harmonics"]["reason"]
    assert "harmonics" in _not_evaluated_fields(packet)


def test_the_packet_schema_version_does_not_move_for_an_added_block(tmp_path):
    """Additive widening keeps v1 — the rule the packet already keeps.

    ``harmonics`` is a NEW top-level key. Every v1 field is unchanged beside it
    and a v1 reader ignores what it does not know, so nothing is misread. The
    one v1 field that DID change is ``not_evaluated``, and it changed by losing
    an entry whose claim became false — a reader that acted on it would have
    been acting on a corpus-wide statement, and the field it names is now
    carried as evidence instead.
    """
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))

    assert PACKET_SCHEMA_VERSION == 1
    assert packet["artifact_schema_version"] == 1
    assert packet["harmonics"]["artifact_schema_version"] == he.HARMONICS_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# the enrichment rule — every published field is declared
# --------------------------------------------------------------------------- #


def test_every_published_harmonics_field_is_declared_exactly_once(tmp_path):
    """The Wave-1 rule, checked against the DATA rather than against a list.

    Every key a published row actually carries must appear in exactly one of
    the two declaration lists. Asserted over the rows the block emitted, not
    over a hand-written set of names, so a column added to ``_role_block``
    without a declaration fails here rather than shipping undeclared.
    """
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))
    uncertainty = packet["harmonics"]["uncertainty"]
    fields, not_uncertainties = uncertainty["fields"], uncertainty["not_uncertainties"]

    assert fields and not_uncertainties
    assert not set(fields) & set(not_uncertainties)
    declared = set(fields) | set(not_uncertainties)

    published: set[str] = set()
    for role_block in packet["harmonics"]["roles"]:
        for row in role_block["rows"]:
            published |= set(row)
    assert published, "the fixture published no rows, so this proves nothing"
    assert published <= declared, sorted(published - declared)

    for name, entry in fields.items():
        assert entry["kind"] in UNCERTAINTY_KINDS, name
        assert entry["of"].strip(), name
    for name, why in not_uncertainties.items():
        assert why.strip(), name

    # The block the rows sit INSIDE is declared too, and asserted the same way.
    # `sweep` and `drive` are the load-bearing pair: they are what says a row
    # could be believed and at what level, so leaving them as self-evident
    # structure would be the easy omission.
    role_fields = uncertainty["role_fields"]
    role_published: set[str] = set()
    for role_block in packet["harmonics"]["roles"]:
        role_published |= set(role_block)
    assert role_published, "the fixture published no role block"
    assert role_published <= set(role_fields), sorted(role_published - set(role_fields))
    for name, why in role_fields.items():
        assert why.strip(), name
    # And no field is filed at both levels, which would give one name two
    # explanations.
    assert not set(role_fields) & declared


def test_the_one_published_spread_is_random_and_the_declaration_says_why(tmp_path):
    """The honest answer to "which list", and the reason it is that list.

    A harmonic LEVEL is a reading, so it is on the second list. The one genuine
    uncertainty is the scatter across a role's sweep repeats, and it is
    ``random`` rather than ``unseparated`` for a specific reason worth pinning:
    those repeats never left one pose, so there is no position term in them to
    be unseparated FROM. The declaration has to carry that reason, because a
    reader who only sees ``random`` cannot check it.
    """
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))
    uncertainty = packet["harmonics"]["uncertainty"]

    assert sorted(uncertainty["fields"]) == [
        "h2_repeat_spread_db", "h3_repeat_spread_db",
    ]
    for entry in uncertainty["fields"].values():
        assert entry["kind"] == UNCERTAINTY_RANDOM
        assert "INSIDE ONE CAPTURE" in entry["of"]

    # The reading itself is NOT an uncertainty, and its declaration owns the
    # systematic this block knows about and does not publish as a field.
    reading = uncertainty["not_uncertainties"]["h2_below_fundamental_db"]
    assert "SYSTEMATIC" in reading
    assert "C(N*f) - C(f)" in reading
    # The floor reads exactly like an error bar and is not one — the same thing
    # the capture_snr block has to say about an SNR.
    assert "BOUNDS an error without being one" in (
        uncertainty["not_uncertainties"]["h2_floor_below_fundamental_db"]
    )


def test_the_declarations_follow_the_orders_actually_published():
    """A third order declares three, not two — the table is not hand-keyed."""
    assert sorted(_harmonics_uncertainty([2, 3, 4])["fields"]) == [
        "h2_repeat_spread_db", "h3_repeat_spread_db", "h4_repeat_spread_db",
    ]
    # And an order the artifact does not carry is not declared for.
    assert sorted(_harmonics_uncertainty([2])["fields"]) == ["h2_repeat_spread_db"]


def test_the_packet_cannot_be_a_route_to_editing_the_declarations(tmp_path):
    """A caller holding the packet holds a copy, as the lab-row block promises."""
    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))
    packet["harmonics"]["uncertainty"]["fields"]["h2_repeat_spread_db"]["kind"] = "x"

    assert he.HARMONIC_ORDERS == (2, 3)
    fresh = build_crossover_evidence_packet(_bundle(tmp_path / "b", harmonics=_artifact()))
    assert fresh["harmonics"]["uncertainty"]["fields"]["h2_repeat_spread_db"]["kind"] == (
        UNCERTAINTY_RANDOM
    )


# --------------------------------------------------------------------------- #
# the rows themselves
# --------------------------------------------------------------------------- #


def test_a_reading_past_an_orders_own_band_edge_is_null_not_a_number():
    """NaN reaches JSON as null, because a number there would read as clean.

    H3 on this sweep is real only to ``f2/3``; above it the image collapses into
    the regularization floor. A very negative float published there would be
    read as a preternaturally clean driver exactly where nothing was measured.
    """
    block = he._role_block("woofer", [_reading(), _reading(offset_db=0.4)], "abc", ORDERS)
    top = block["rows"][-1]

    assert top["hz"] == 2000.0
    assert top["h3_below_fundamental_db"] is None
    assert top["h3_floor_below_fundamental_db"] is None
    # And the FLAG is null too, not False: "no clean point" must not read as
    # "a point clear of the floor".
    assert top["h3_floor_limited"] is None
    assert top["h2_below_fundamental_db"] is not None


def test_a_spread_over_fewer_than_two_repeats_is_absent_not_zero():
    """The cross-seat block's rule, kept: 0.0 would say the repeats agreed."""
    assert he._spread([]) is None
    assert he._spread([-50.0]) is None
    assert he._spread([float("nan"), -50.0]) is None
    assert he._spread([-50.0, -51.0]) == pytest.approx(0.7, abs=0.05)

    single = he._role_block("woofer", [_reading()], "abc", ORDERS)
    assert single["rows"][0]["h2_repeat_spread_db"] is None


def test_a_point_is_floor_limited_by_majority_vote_of_the_repeats():
    """One sweep's noise spike cannot flag a point the others read as clear."""
    block = he._role_block("woofer", [_reading(), _reading(offset_db=0.4)], "abc", ORDERS)
    rows = {row["hz"]: row for row in block["rows"]}

    # 200 Hz: H2 sits 2 dB above its floor, inside the 6 dB margin.
    assert rows[200.0]["h2_floor_limited"] is True
    # 400 Hz: 15 dB clear.
    assert rows[400.0]["h2_floor_limited"] is False


def test_the_worst_point_refuses_when_nothing_clears_the_floor():
    """An order buried in its own floor reports nothing, never the floor.

    The tweeter case on the real corpus: at a low drive every point is
    floor-limited, and a summary that headlined the loudest noise bin would be
    reporting the instrument as if it were the speaker.
    """
    readings = [_reading(), _reading(offset_db=0.1)]
    for reading in readings:
        # Bury H2 in its own floor everywhere. BOTH readings, because the vote
        # below is a majority one — burying just the one would (correctly) be
        # outvoted, which is what the next assertion exists to keep true.
        reading.floor_relative_db[2][:] = reading.relative_db[2] + 1.0
    block = he._role_block("woofer", readings, "abc", ORDERS)

    assert block["worst"]["h2"] is None
    assert block["floor_limited_fraction"]["h2"] == 1.0
    # H3 is untouched and still reports, so the refusal above is about H2's
    # floor rather than about the block having given up on the whole capture.
    assert block["worst"]["h3"] is not None


def test_pooling_by_index_across_disagreeing_grids_is_refused():
    """Pooling by index lies silently otherwise, so it is checked not assumed."""
    other = _reading()
    object.__setattr__(other, "freqs_hz", other.freqs_hz + 1.0)

    with pytest.raises(ValueError, match="grids disagree"):
        he._role_block("woofer", [_reading(), other], "abc", ORDERS)


def test_two_captures_are_two_blocks_because_captures_are_poses(tmp_path):
    """The reason the one spread above can be called random.

    A MEASURE capture is one pose. Merging two of them would mix the in-capture
    repeat scatter with whatever differs between takes, which is exactly the
    unseparated case — so the instrument does not merge them, and a reader who
    wants them combined can see what they are combining.
    """
    packet = build_crossover_evidence_packet(
        _bundle(tmp_path, harmonics=_artifact(n_roles=2))
    )
    blocks = packet["harmonics"]["roles"]

    assert len(blocks) == 2
    assert {block["role"] for block in blocks} == {"woofer"}
    assert len({block["wav_sha256_12"] for block in blocks}) == 2


# --------------------------------------------------------------------------- #
# the instrument's refusals
# --------------------------------------------------------------------------- #


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "gain_plan_db": {"woofer": -6.0, "tweeter": -31.2},
        "candidate": {"program_id": "not-a-real-id"},
    }
    state.update(overrides)
    return state


def _applied_profile(fc_hz: float = 1648.7) -> dict[str, Any]:
    """The bare shape ``_crossover_fc_hz`` reads — no SSOT schema wrapper.

    Used only for direct ``_crossover_fc_hz`` calls, which read the fields
    below without going through :func:`~.evidence_packet._applied_profile_source`.
    A test that goes through the real loader (:func:`_write_applied_profile`)
    needs the wrapper; this one does not.
    """
    return {
        "recomposition_snapshot": {
            "preset": {"crossover_regions": [{"fc_hz": fc_hz}]}
        }
    }


def _write_applied_profile(tmp_path: Path, *, fc_hz: float = 1648.7) -> Path:
    """A minimal applied-profile SSOT file, valid enough for the real loader.

    ``load_applied_baseline_profile_state`` (via ``_applied_profile_source``)
    only accepts a document carrying its own schema stamp and an "applied"
    status — see ``jasper.active_speaker.baseline_profile._load_saved_state``
    and ``_applied_profile_anchor``.
    """
    from jasper.active_speaker.baseline_profile import (
        BASELINE_PROFILE_KIND,
        SCHEMA_VERSION,
    )

    path = tmp_path / "applied-profile.json"
    path.write_text(json.dumps({
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": BASELINE_PROFILE_KIND,
        "status": "applied",
        **_applied_profile(fc_hz),
    }))
    return path


def test_the_state_the_program_came_from_is_recorded_for_audit(tmp_path):
    """The one seam `_scope_captures` cannot refuse is at least auditable.

    Nothing checks that the ``--state`` belongs to the round the scope selects:
    the state's id is a CAPTURE id and the ring stamps a BUNDLE id, two
    namespaces with no banked mapping between them, so a mismatched pair
    publishes a wrong drive through every gate cleanly. Recording the state's
    own id does not refuse that — it makes it checkable afterwards, which is
    the honest thing available until a capture banks its own program_id.

    Pinned on both halves: the writer resolves it, and the packet carries it
    (a recorded fact with no reader is not an audit trail).
    """
    assert he._state_capture_session_id({"session_id": "wired-abc"}) == "wired-abc"
    # Absent, empty, and non-string all resolve to None rather than to a value
    # a reader might compare against something.
    assert he._state_capture_session_id({}) is None
    assert he._state_capture_session_id({"session_id": ""}) is None
    assert he._state_capture_session_id({"session_id": 7}) is None

    packet = build_crossover_evidence_packet(_bundle(tmp_path, harmonics=_artifact()))
    assert packet["harmonics"]["program"]["state_capture_session_id"] == "wired-TESTONLY"


def test_the_crossover_corner_is_read_from_the_applied_profile_not_a_flag():
    """It is a fact about the round, and the shipped analysis refuses without it.

    A flag would let an operator hand this instrument a different corner from
    the one the captures were taken through, which would move the analysis's
    per-driver expectations without moving anything a reader could see.
    """
    assert he._crossover_fc_hz(_applied_profile(), "") == pytest.approx(1648.7)

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._crossover_fc_hz({}, "")
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert "fc_hz" in excinfo.value.evidence["missing"]


def test_the_corner_reports_absent_with_reason_never_a_stash_fallback():
    """No readable applied-profile SSOT refuses with ITS reason, nothing else.

    ``_crossover_fc_hz`` used to read a flow state's ``pre_apply_profile`` — the
    Undo stash, one apply behind after any v2 apply and arbitrarily behind
    after an apply through a door that never touches v2 state. It now takes
    the SSOT (or the reason there is none) directly and has no stash to fall
    back to even if it wanted one.
    """
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._crossover_fc_hz(None, "no applied baseline profile was supplied")
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert excinfo.value.evidence["reason"] == "no applied baseline profile was supplied"


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), True, "1648.7", None])
def test_an_unusable_corner_refuses_rather_than_being_coerced(value):
    """``True`` is an ``int`` in Python and would otherwise pass as 1 Hz."""
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._crossover_fc_hz({
            "recomposition_snapshot": {
                "preset": {"crossover_regions": [{"fc_hz": value}]}
            }
        }, "")
    assert excinfo.value.reason == he.STATE_UNREADABLE


def test_a_state_without_a_program_id_refuses_before_any_audio_is_read():
    """An unproved program cannot be read: every offset derives from its L."""
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(_state(candidate={}), he_bands())
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert excinfo.value.evidence["missing"] == "candidate.program_id"


def test_a_state_without_a_gain_plan_refuses_by_name():
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(_state(gain_plan_db={"woofer": -6.0}), he_bands())
    assert excinfo.value.reason == he.STATE_UNREADABLE


def he_bands() -> dict[str, tuple[float, float]]:
    return {"woofer": (150.0, 4000.0), "tweeter": (1600.0, 20000.0)}


def test_a_program_that_cannot_prove_itself_is_refused_not_read():
    """The whole point of proving the rebuild instead of asserting it.

    Slow-ish (it walks the solve grid twice before giving up), and that is the
    behaviour: the refusal is what a wrong band pair produces, rather than a
    reading taken through the wrong sweep L.
    """
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(_state(), he_bands())

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.evidence["program_id"] == "not-a-real-i"[:12]


def test_a_ring_with_no_measure_capture_says_why_a_verify_one_would_not_do(tmp_path):
    """Harmonics need the per-driver program; a summed capture cannot attribute."""
    ring = tmp_path / "dumps" / "sidecar"
    ring.mkdir(parents=True)
    (ring / "1_verify_x.json").write_text(json.dumps({"phase": "verify"}))

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.read_round_harmonics(
            tmp_path, tmp_path / "dumps", _real_state(), he_bands(),
            applied_profile_path=_write_applied_profile(tmp_path),
        )
    assert excinfo.value.reason == he.NO_ADMISSIBLE_CAPTURES
    assert "one driver at a time" in excinfo.value.evidence["note"]


def _real_state() -> dict[str, Any]:
    """A state whose program id is one ``build_measure_program`` can reach.

    Solved once here rather than hardcoded, so a change to the program builder
    moves this fixture with it instead of turning every test below into a
    program-reproduction failure.
    """
    state = _state()
    from jasper.active_speaker.crossover_v2.programs import (
        PILOT_LEVEL_DELTA_DB,
        courtesy_prelude_for_phase,
    )
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )

    program = build_measure_program(
        {"woofer": -6.0, "tweeter": -31.2},
        (
            RoleBand("woofer", 0, FrequencyBand(150.0, 4000.0)),
            RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
        ),
        downstream_gain_db=-20.0,
        leading_pilot_gains_db=(-6.0 - PILOT_LEVEL_DELTA_DB, -6.0),
        leading_pilot_role="woofer",
        courtesy_prelude=courtesy_prelude_for_phase("measure"),
    )
    state["candidate"] = {"program_id": program.program_id}
    return state


def test_the_solve_recovers_the_session_volume_the_round_never_banked():
    """The one parameter no artifact carries, proved rather than asserted."""
    program, downstream, prelude = he.rebuild_measure_program(
        _real_state(), he_bands()
    )

    assert downstream == pytest.approx(-20.0)
    assert prelude is False
    assert program.program_id == _real_state()["candidate"]["program_id"]


# --------------------------------------------------------------------------- #
# #2923 — a fitted round banks its realized durations; rebuild reads them
# --------------------------------------------------------------------------- #


def _fitted_program_at(
    downstream_db: float,
    *,
    woofer_limit_s: float = 3.5,
    bands: Mapping[str, tuple[float, float]] | None = None,
):
    """A real MEASURE program whose woofer sweep is FITTED (#2921) below the
    4.0 s nominal default.

    Deterministic regardless of the band: the nominal always realizes AT OR
    ABOVE its own request (``phase_closing_duration_s``'s own guarantee), so
    any limit below the 4.0 s default forces the fit. ``bands`` defaults to
    ``he_bands()``'s own pair — pass a wider one (e.g. a tweeter upper edge
    at or above 24 kHz) to exercise the composer's own MEASURE-window clamp.
    """
    from jasper.active_speaker.crossover_v2.programs import (
        PILOT_LEVEL_DELTA_DB, courtesy_prelude_for_phase,
    )
    from jasper.audio_measurement.program import (
        FrequencyBand, RoleBand, build_measure_program,
    )

    resolved = bands if bands is not None else he_bands()
    return build_measure_program(
        {"woofer": -6.0, "tweeter": -31.2},
        (
            RoleBand("woofer", 0, FrequencyBand(*resolved["woofer"])),
            RoleBand("tweeter", 1, FrequencyBand(*resolved["tweeter"])),
        ),
        sweep_duration_limits_s={"woofer": woofer_limit_s},
        downstream_gain_db=downstream_db,
        leading_pilot_gains_db=(-6.0 - PILOT_LEVEL_DELTA_DB, -6.0),
        leading_pilot_role="woofer",
        courtesy_prelude=courtesy_prelude_for_phase("measure"),
    )


def test_a_banked_duration_fit_reproduces_without_a_search():
    """The durable fix, end to end. A fitted sweep's realized length is a
    continuous float no search grid could ever land on — banking it is what
    makes a fitted round reproducible AT ALL, not merely faster to reproduce.
    Before this field existed, this exact state was
    ``test_a_duration_fitted_round_that_predates_banking_still_names_its_cause``
    below: an honest refusal, unconditionally.
    """
    from jasper.active_speaker.crossover_v2 import priors

    program = _fitted_program_at(-20.0)
    durations = priors.measure_sweep_durations_s(program)
    assert durations is not None
    assert durations["woofer"] <= 3.5  # confidence check: the fit actually bit

    state = _state(
        candidate={"program_id": program.program_id},
        measure_sweep_durations_s=durations,
    )

    rebuilt, downstream, prelude = he.rebuild_measure_program(state, he_bands())

    assert rebuilt.program_id == program.program_id
    assert downstream == pytest.approx(-20.0)
    assert prelude is False


def test_a_duration_fitted_round_that_predates_banking_still_names_its_cause():
    """No replay capability is lost: the honest refusal from before this fix
    stands, unchanged, for a round that never banked its realized durations —
    #2921 fitted the sweep, and this round predates #2923's bank, so this
    replay composes at the nominal length and cannot match.
    """
    program = _fitted_program_at(-20.0)
    state = _state(candidate={"program_id": program.program_id})

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(state, he_bands())

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.evidence["measure_sweep_durations_banked"] is False
    assert excinfo.value.evidence["measure_sweep_durations_usable"] is False
    note = excinfo.value.evidence["note"]
    assert "FITTED" in note
    assert "#2921" in note
    assert "did not bank the realized durations (#2923)" in note
    # The fix narrows cause (2); it does not remove the other three.
    assert "not on the solve grid" in note
    assert "driver bands supplied are wrong" in note
    assert "does not describe a MEASURE round" in note


@pytest.mark.parametrize("raw", [
    {},
    {"woofer": 3.9},                        # tweeter missing
    {"woofer": 3.9, "tweeter": "3.0"},      # non-numeric
    {"woofer": 3.9, "tweeter": True},       # bool is not a real duration
    {"woofer": 3.9, "tweeter": float("nan")},
    {"woofer": 3.9, "tweeter": float("inf")},
    {"woofer": 3.9, "tweeter": 0.0},        # non-positive
    {"woofer": 3.9, "tweeter": -1.0},
    "not-a-mapping",
])
def test_a_malformed_banked_duration_is_treated_as_absent(raw):
    """Hydrate must tolerate a hand-edited or partially-written state: a
    malformed shape falls back to nominal composition, exactly like an absent
    key, rather than raising.
    """
    state = _state(measure_sweep_durations_s=raw)
    assert he._banked_sweep_durations_s(state, he_bands()) is None


def test_an_old_shape_state_with_no_banked_duration_key_composes_at_nominal():
    """The field's outright absence — every round banked before it existed —
    is the same "compose at nominal" path a malformed value falls back to.
    """
    state = _state()
    assert "measure_sweep_durations_s" not in state
    assert he._banked_sweep_durations_s(state, he_bands()) is None


def test_a_well_formed_banked_duration_is_read_back_exactly():
    state = _state(
        measure_sweep_durations_s={"woofer": 3.983876, "tweeter": 3.0}
    )

    assert he._banked_sweep_durations_s(state, he_bands()) == {
        "woofer": pytest.approx(3.983876), "tweeter": pytest.approx(3.0),
    }


# --------------------------------------------------------------------------- #
# gate fix round (#2923): a below-one-cycle banked value must not escape as
# a bare ValueError — it is malformed the same way the shapes above are
# --------------------------------------------------------------------------- #


def _one_cycle_floor_s(band: tuple[float, float]) -> float:
    """The exact boundary :func:`synchronized_sweep_metadata` raises below —
    computed independently here (not imported from the composer) so this test
    is a real cross-check of the fix rather than a restatement of it."""
    f1, f2 = band
    return 0.5 * math.log(f2 / f1) / f1


@pytest.mark.parametrize("role", ["woofer", "tweeter"])
def test_a_below_one_cycle_banked_duration_is_treated_as_absent(role):
    """The should-fix. A positive, finite value that is too short to close
    even one cycle at f1 passes every OTHER guard here, but must not reach
    ``synchronized_sweep_metadata`` and raise out of this fail-soft function —
    it is malformed on the SAME terms as the shapes above, not a new
    vocabulary.
    """
    floor_s = _one_cycle_floor_s(he_bands()[role])
    other = "tweeter" if role == "woofer" else "woofer"
    for fraction in (0.5, 0.999):
        durations = {role: floor_s * fraction, other: 3.0}
        state = _state(measure_sweep_durations_s=durations)
        assert he._banked_sweep_durations_s(state, he_bands()) is None, (
            f"{role} at {fraction:.3f} of its one-cycle floor "
            f"({floor_s * fraction:.6f}s) should be treated as absent"
        )


def test_a_below_one_cycle_banked_duration_refuses_honestly_instead_of_raising():
    """The CLI-visible half of the should-fix: ``rebuild_measure_program``
    must reach the named ``program_not_reproducible`` refusal, never an
    escaped ``ValueError`` — that escape used to mis-classify the round at
    ``jasper-round-views distortion`` as ``EXIT_UNREADABLE`` instead of
    ``EXIT_REFUSED``.

    The state file DOES carry a ``measure_sweep_durations_s`` entry here —
    this is the fix round 2 honesty requirement: the round banked SOMETHING,
    it just was not usable, and the refusal must say that rather than "did
    not bank" (which is the true story for a genuinely-absent key, pinned by
    ``test_a_duration_fitted_round_that_predates_banking_still_names_its_cause``
    above).
    """
    floor_s = _one_cycle_floor_s(he_bands()["woofer"])
    state = _state(
        measure_sweep_durations_s={"woofer": floor_s * 0.5, "tweeter": 3.0}
    )

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(state, he_bands())

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.evidence["measure_sweep_durations_banked"] is True
    assert excinfo.value.evidence["measure_sweep_durations_usable"] is False
    note = excinfo.value.evidence["note"]
    assert "did not bank" not in note
    assert "carries a measure_sweep_durations_s entry" in note
    assert "TREATED THE SAME AS ABSENT" in note


# --------------------------------------------------------------------------- #
# gate fix round 2 (#2923): the guard must validate the band the COMPOSER
# actually sweeps (post-intersection), not the raw declared one
# --------------------------------------------------------------------------- #


#: An ordinary compression-driver/horn datasheet upper edge — comfortably
#: past the 24 kHz Nyquist boundary at this module's 48 kHz sample rate, and
#: past ``he_bands()``'s own 20 kHz, which is exactly why the round-1 tests
#: never exercised the composer's [150, 23000] Hz clamp: 20 kHz is already
#: inside it, so intersecting changes nothing.
_DATASHEET_WIDE_BANDS = {"woofer": (150.0, 4000.0), "tweeter": (1600.0, 30_000.0)}


def test_a_datasheet_wide_declared_band_reproduces_a_fitted_round():
    """The round-2 regression, reproduced then proved fixed.

    A tweeter declared to 30 kHz used to fail the kernel's Nyquist check
    here (raw ``f2=30000 >= 24000``) even though the composer clamps to
    23,000 Hz before that check is ever reached — a perfectly reproducible
    round lost its bank and fell back to nominal-only search. The direct
    ``_banked_sweep_durations_s`` call below is the "evidence" half: it
    proves the value was ACCEPTED as usable, not merely that some other path
    happened to also reproduce.
    """
    from jasper.active_speaker.crossover_v2 import priors

    program = _fitted_program_at(-20.0, bands=_DATASHEET_WIDE_BANDS)
    durations = priors.measure_sweep_durations_s(program)
    assert durations is not None

    state = _state(
        candidate={"program_id": program.program_id},
        measure_sweep_durations_s=durations,
    )
    accepted = he._banked_sweep_durations_s(state, _DATASHEET_WIDE_BANDS)
    assert accepted is not None
    assert accepted == pytest.approx(durations)

    rebuilt, downstream, prelude = he.rebuild_measure_program(
        state, _DATASHEET_WIDE_BANDS
    )

    assert rebuilt.program_id == program.program_id
    assert downstream == pytest.approx(-20.0)
    assert prelude is False


def test_a_datasheet_wide_band_refusal_still_reports_the_bank_honestly():
    """Even when the round refuses for an UNRELATED reason (here: a
    ``program_id`` that simply does not match anything, standing in for a
    wrong gain plan or wrong bands), a usable ≥24 kHz bank must still read
    as banked AND usable in the evidence — not as "did not bank" merely
    because its band happens to need the composer's clamp.
    """
    from jasper.active_speaker.crossover_v2 import priors

    program = _fitted_program_at(-20.0, bands=_DATASHEET_WIDE_BANDS)
    durations = priors.measure_sweep_durations_s(program)
    assert durations is not None
    state = _state(
        candidate={"program_id": "not-a-real-id"},
        measure_sweep_durations_s=durations,
    )

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(state, _DATASHEET_WIDE_BANDS)

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.evidence["measure_sweep_durations_banked"] is True
    assert excinfo.value.evidence["measure_sweep_durations_usable"] is True
    assert "did not bank" not in excinfo.value.evidence["note"]


def test_a_sidecar_carrying_no_gate_field_is_refused_rather_than_read_ungated():
    """Zero comparisons is not a passed gate — fail-closed, by name."""
    readings, failures, disclosure, compared = he._read_one_capture(
        object(), None, {"diagnostic": {}},
        orders=ORDERS, calibration=None, fc_hz=1000.0,
    )

    assert readings == []
    assert compared == 0
    assert disclosure is None
    assert "nothing was validated" in failures[0]


def test_a_field_the_bank_recorded_and_the_replay_lost_is_a_failure():
    """The reconstruction losing something the session had is not a pass."""
    assert he._fidelity_failures({"epsilon_ppm": 1.0}, {"epsilon_ppm": 1.0}) == []
    # Inside the tolerance.
    assert he._fidelity_failures({"epsilon_ppm": 1.004}, {"epsilon_ppm": 1.0}) == []
    assert he._fidelity_failures({"epsilon_ppm": 1.5}, {"epsilon_ppm": 1.0})
    assert he._fidelity_failures({}, {"epsilon_ppm": 1.0})
    # A field the sidecar never recorded is not compared: the bank predates
    # some of them, and comparing an absence would fail every older round.
    assert he._fidelity_failures({}, {}) == []
    assert he._fidelity_failures({"linearity_ok": False}, {"linearity_ok": True})


def test_an_integrity_disagreement_is_disclosed_rather_than_dropped():
    """Reading a capture the session rejected is a fact the reader is owed."""
    assert he._glitch_disclosure({"glitch_detected": False}, {"glitch_detected": False}) is None
    assert "pre-D7 desync guard" in he._glitch_disclosure(
        {"glitch_detected": False}, {"glitch_detected": True}
    )
    assert "read with suspicion" in he._glitch_disclosure(
        {"glitch_detected": True}, {"glitch_detected": False}
    )


def _ring(tmp_path: Path, rows: Sequence[tuple[str, str, str | None]]) -> Path:
    """A capture ring on disk: ``(name, wav_sha256, session_id)`` per sidecar."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    for name, sha, session in rows:
        doc: dict[str, Any] = {"phase": "measure", "wav_sha256": sha}
        if session is not None:
            doc["jts_session_identity"] = {"session_id": session}
        (ring / "sidecar" / f"{name}.json").write_text(json.dumps(doc))
        (ring / "wav" / f"{name}.wav").write_bytes(b"")
    return ring


def test_both_readers_of_the_capture_ring_take_the_same_directory(tmp_path):
    """``--dumps`` means ONE directory across two tools, proven on one ring.

    ``jasper-round-views distortion`` and ``classify-features`` both take the
    ring ROOT — the ``dumps/wav/`` beside ``dumps/sidecar/`` split a
    pre-removal bank produced. An operator who had to know which tool wanted
    the parent would eventually hand one of them the wrong path and get a
    silent empty answer, so this asserts on BEHAVIOUR — one fixture ring, both
    readers observed finding the same sidecar — rather than on the two
    spelling the same pattern, which is a thing that can be true while the
    contract is broken.

    These two are what is LEFT of the glob's readers. The evidence packet was
    the third and no longer reads it at all: it wanted a number the banked
    take now carries, where these two want capture BYTES no record holds.
    """
    ring = _ring(tmp_path, [("1_measure_a", "aaa", "mine")])
    round_dir = tmp_path / "round"
    round_dir.mkdir()

    assert [c["wav_sha256"] for c in he._bind_measure_captures(ring)] == ["aaa"]

    # The classifier refuses this ring — one non-admissible capture is not a
    # round — but its refusal COUNTS what the glob found, which is the half
    # being pinned here.
    with pytest.raises(FeatureClassificationRefused) as refusal:
        load_round_captures(round_dir, ring, session_id="mine")
    assert refusal.value.detail["phases_seen"] == {"measure": 1}
    assert refusal.value.detail["dumps_dir"] == ring.name


def test_the_ring_is_scoped_to_this_round_and_deduplicated_by_content(tmp_path):
    """A rolling ring can hold another round's captures, and re-analyses of one."""
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "mine"),
        ("2_measure_b", "aaa", "mine"),      # same capture, second analysis
        ("3_measure_c", "ccc", "theirs"),    # another round
        ("4_measure_d", "ddd", "mine"),
    ])

    captures, scope = he._scope_captures(he._bind_measure_captures(ring), "mine")

    assert [capture["wav_sha256"] for capture in captures] == ["aaa", "ddd"]
    assert scope["session_id"] == "mine"
    # The scope reports the whole ring it chose from, not just what survived —
    # otherwise a reader cannot tell a one-capture round from a one-capture
    # SLICE of a busy ring.
    assert scope["n_ring_captures"] == 3


def test_an_unscoped_ring_holding_several_sessions_refuses_instead_of_pooling(tmp_path):
    """The fail-open this instrument shipped with, pinned as the refusal it is.

    Every capture is read against ONE rebuilt program, and the published drive
    is that PROGRAM's, not the capture's. Pooling a neighbouring round's capture
    therefore publishes the wrong level silently: measured on the shipped
    corpus, one capture read against another session's program reported
    -6.0/-26.0 dBFS where its own says -10.2/-30.2 — 4.2 dB, on the field this
    module insists must never be published apart from the ratio.

    Neither other gate can catch it. The program gate proves program-vs-STATE,
    and every FIDELITY_FIELD is amplitude-invariant, which is why this needs a
    gate of its own rather than trust.
    """
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "session-one"),
        ("2_measure_b", "bbb", "session-two"),
    ])

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._scope_captures(he._bind_measure_captures(ring), None)

    assert excinfo.value.reason == he.RING_NOT_SCOPED_TO_ONE_SESSION
    assert excinfo.value.evidence["distinct_session_ids"] == [
        "session-one", "session-two",
    ]
    assert "amplitude-invariant" in excinfo.value.evidence["note"]


def test_an_unscoped_ring_holding_an_unattributable_capture_refuses(tmp_path):
    """A capture with no readable identity cannot be shown to belong here.

    Distinct from the several-sessions case only in what the evidence says: a
    missing identity is not a matching one, and admitting it would reopen the
    hole for exactly the captures whose provenance is least knowable.
    """
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "session-one"),
        ("2_measure_b", "bbb", None),
    ])

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._scope_captures(he._bind_measure_captures(ring), None)

    assert excinfo.value.reason == he.RING_NOT_SCOPED_TO_ONE_SESSION
    assert excinfo.value.evidence["n_unattributed"] == 1


def test_an_unscoped_ring_of_one_session_is_admitted_and_says_so(tmp_path):
    """The case the unscoped default exists for stays workable, and is recorded.

    A ring holding one round needs no scope to be unambiguous — but the artifact
    still says WHICH session it turned out to be and by what rule, so "nobody
    passed a scope" and "the scope was checked" are distinguishable afterwards.
    """
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "only-one"),
        ("2_measure_b", "bbb", "only-one"),
    ])

    captures, scope = he._scope_captures(he._bind_measure_captures(ring), None)

    assert [capture["wav_sha256"] for capture in captures] == ["aaa", "bbb"]
    assert scope["session_id"] == "only-one"
    assert "no scope supplied" in scope["source"]


def test_an_empty_ring_is_not_an_ambiguous_one(tmp_path):
    """Nothing to pool is not the same finding as too much to pool.

    A ring with no MEASURE capture must reach NO_ADMISSIBLE_CAPTURES, which
    tells an operator to go and measure; routing it to the scope refusal would
    send them to fix a scope that was never the problem.
    """
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)

    captures, scope = he._scope_captures(he._bind_measure_captures(ring), None)

    assert captures == []
    assert scope["session_id"] is None


def test_a_sidecar_with_no_wav_beside_it_is_skipped(tmp_path):
    """The ring rolls WAVs off before sidecars; a bare sidecar is not a capture."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text(json.dumps({
        "phase": "measure", "wav_sha256": "aaa",
    }))

    assert he._bind_measure_captures(ring) == []


def test_a_null_reading_never_reaches_json_as_a_number():
    assert he._nullable(float("nan")) is None
    assert he._nullable(float("inf")) is None
    assert he._nullable(-46.24) == -46.2
    assert he._nullable(0.27449, 3) == 0.274


def test_the_artifact_name_has_one_owner():
    """The writer, the reader and the CLI resolve one spelling."""
    from jasper.cli.round_views import ARTIFACT_BY_VIEW

    assert he.HARMONICS_ARTIFACT == HARMONICS_ARTIFACT == "harmonic_distortion.json"
    assert ARTIFACT_BY_VIEW["distortion"].artifact is HARMONICS_ARTIFACT


def test_the_distortion_door_composes_the_shape_the_round_actually_swept(tmp_path):
    """Both directions, because a derivation that answered ``full_range`` for
    every round would break every 2-way one — and the 1-way arm is then driven
    all the way through the rebuild, which proves itself against the banked
    ``program_id`` and so could only refuse a two-role composition."""
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )
    from jasper.active_speaker.crossover_v2.programs import (
        PILOT_LEVEL_DELTA_DB,
        courtesy_prelude_for_phase,
    )
    from jasper.cli.round_views import build_parser, main

    band = he.DEFAULT_FULL_RANGE_BAND_HZ
    overrides = {
        "woofer": (150.0, 4000.0), "tweeter": (1600.0, 20000.0), "full_range": band,
    }

    assert he.round_bands_hz(
        {"gain_plan_db": {"woofer": -6.0, "tweeter": -31.2}}, overrides,
    ) == {"woofer": (150.0, 4000.0), "tweeter": (1600.0, 20000.0)}
    # A state that names no roles, or roles that are not a shape any speaker
    # declares, is REFUSED by name — never composed as a pair nobody measured,
    # and never quietly reduced to the roles that happen to match.
    for state in ({}, {"gain_plan_db": {"woofer": -6.0, "horn": -31.2}}):
        with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
            he.round_bands_hz(state, overrides)
        assert excinfo.value.reason == he.STATE_UNREADABLE
    # And the verb publishes that named refusal as the refused exit, rather
    # than letting an instrument's own exception reach the operator raw.
    bundle = tmp_path / "bundle"
    (bundle / "evidence/v1/artifacts/crossover_v2/cap-1").mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": "s-1"}))
    flow_state = tmp_path / "flow_state.json"
    flow_state.write_text(json.dumps({"gain_plan_db": {"woofer": -6.0, "horn": -31.2}}))
    assert main([
        "distortion", str(bundle), "--dumps", str(tmp_path),
        "--state", str(flow_state),
    ]) == EXIT_REFUSED

    program = build_measure_program(
        {"full_range": -11.0},
        (RoleBand("full_range", 0, FrequencyBand(*band)),),
        downstream_gain_db=-20.0,
        leading_pilot_gains_db=(-11.0 - PILOT_LEVEL_DELTA_DB, -11.0),
        leading_pilot_role="full_range",
        courtesy_prelude=courtesy_prelude_for_phase("measure"),
    )
    state = {
        "gain_plan_db": {"full_range": -11.0},
        "candidate": {"program_id": program.program_id},
    }
    bands = he.round_bands_hz(state, overrides)

    assert bands == {"full_range": band}
    rebuilt, downstream, _prelude = he.rebuild_measure_program(state, bands)
    assert rebuilt.program_id == program.program_id
    assert downstream == pytest.approx(-20.0)

    # A 1-way round measured on a non-default band gets an operator remedy,
    # same as the pair's --woofer-band / --tweeter-band.
    args = build_parser().parse_args(
        ["distortion", "bundle", "--dumps", "d", "--state", "s",
         "--full-range-band", "45:18000"],
    )
    assert args.full_range_band == (45.0, 18000.0)


def test_the_orders_the_product_publishes_are_not_the_kernels_ceiling():
    """A kernel that learned a 4th order must not widen a banked schema."""
    from jasper.audio_measurement import distortion

    assert he.HARMONIC_ORDERS == (2, 3)
    assert he.HARMONIC_ORDERS is not distortion.DEFAULT_HARMONIC_ORDERS


def _program_at(downstream_db: float):
    """A real MEASURE program composed at one session volume."""
    from jasper.active_speaker.crossover_v2.programs import (
        PILOT_LEVEL_DELTA_DB,
        courtesy_prelude_for_phase,
    )
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )

    return build_measure_program(
        {"woofer": -6.0, "tweeter": -31.2},
        (
            RoleBand("woofer", 0, FrequencyBand(150.0, 4000.0)),
            RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)),
        ),
        downstream_gain_db=downstream_db,
        leading_pilot_gains_db=(-6.0 - PILOT_LEVEL_DELTA_DB, -6.0),
        leading_pilot_role="woofer",
        courtesy_prelude=courtesy_prelude_for_phase("measure"),
    )


@pytest.mark.parametrize("volume", [-20.0, -20.5, -40.0])
def test_a_session_volume_on_the_solve_grid_is_recovered(volume):
    """The half-dB reference volumes the grid does cover, solved by hash."""
    state = _state(candidate={"program_id": _program_at(volume).program_id})

    _program, solved, prelude = he.rebuild_measure_program(state, he_bands())

    assert solved == pytest.approx(volume)
    assert prelude is False


@pytest.mark.parametrize("volume", [-24.7, -43.0, -59.9])
def test_a_session_volume_off_the_solve_grid_refuses_and_names_the_real_cause(volume):
    """The claim that used to sit on the grid was false, and the note misled.

    `session_measurement_volume_db` returns `min(reference, loudest_cap)` as an
    UNQUANTIZED float, and its reference half admits any value above the -60 dB
    floor — so the true domain is (-60, 0] with no step, and a half-dB grid from
    -40 cannot cover it. Refusing is the CORRECT behaviour; what was wrong was
    the docstring saying the grid covered everything, and a refusal note that
    offered two causes ("bands wrong", "not a MEASURE round") when neither is
    what an operator on a real box has hit.

    -43.0 is in here deliberately: it is ON the half-dB step but BELOW the
    grid's -40 floor, so it isolates the range half of the claim from the
    quantization half.
    """
    state = _state(candidate={"program_id": _program_at(volume).program_id})

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he.rebuild_measure_program(state, he_bands())

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    note = excinfo.value.evidence["note"]
    assert "not on the solve grid" in note
    assert "unquantized float" in note
    assert "-60 dB floor" in note
    # The two causes that were the ONLY ones offered before are still named,
    # because they are real — they are just no longer the whole list.
    assert "driver bands supplied are wrong" in note
    assert "does not describe a MEASURE round" in note


def test_the_solve_grid_is_half_db_steps_from_minus_forty_to_zero():
    """The grid's own shape, asserted apart from any claim about coverage."""
    assert he._DOWNSTREAM_GRID_DB[0] == -40.0
    assert he._DOWNSTREAM_GRID_DB[-1] == 0.0
    assert all(value <= 0.0 for value in he._DOWNSTREAM_GRID_DB)
    assert len(he._DOWNSTREAM_GRID_DB) == 81
    # The composer's floor is -60, not -40, so the grid is a SUBSET of what the
    # flow can produce. Pinned so the docstring's honesty cannot quietly rot
    # back into "covers every value".
    from jasper.active_speaker.seat_level_reference import (
        seat_level_reference_volume_db,
    )

    assert seat_level_reference_volume_db is not None
    assert he._DOWNSTREAM_GRID_DB[0] > -60.0


def test_reading_a_capture_ring_never_raises_on_a_hand_edited_sidecar(tmp_path):
    """A bad artifact is a fact this instrument reports, never a crash."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text("{not json")
    (ring / "sidecar" / "2_measure_b.json").write_text(json.dumps(["a list"]))
    (ring / "sidecar" / "3_measure_c.json").write_text(json.dumps({"phase": 7}))

    assert he._bind_measure_captures(ring) == []


def test_a_median_over_nothing_is_nan_not_zero():
    """A zero would be a reading; NaN becomes null, which is the honest answer."""
    assert math.isnan(he._median([]))
    assert math.isnan(he._median([float("nan")]))
    assert he._median([-50.0, -52.0, -54.0]) == -52.0


@pytest.mark.parametrize(
    ("width", "values", "expected"),
    [
        (2, (0, 16384, -16384, 32767), (0.0, 0.5, -0.5, 32767 / 2**15)),
        (4, (0, 2**30, -(2**30), 2**31 - 1), (0.0, 0.5, -0.5, (2**31 - 1) / 2**31)),
    ],
)
def test_a_capture_is_decoded_at_the_width_its_container_declares(
    tmp_path, width, values, expected,
):
    """The dump ring holds 16-bit phone captures AND 32-bit wired captures.

    A width-blind int16 decode re-strides a 32-bit take into garbage the H2/H3
    read then misattributes, so the width comes from the container's own fmt
    chunk. An unsupported width raises rather than being mis-analyzed.
    """
    import struct
    import wave

    path = tmp_path / f"capture-{width}.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(width)
        writer.setframerate(48_000)
        writer.writeframes(struct.pack({2: "<4h", 4: "<4i"}[width], *values))

    assert np.allclose(he._read_mono(path), expected)

    eight = tmp_path / "unsupported.wav"
    with wave.open(str(eight), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(48_000)
        writer.writeframes(bytes([128, 129, 127]))
    with pytest.raises(ValueError):
        he._read_mono(eight)


@pytest.mark.parametrize(
    "calibration_id",
    [
        "minidsp-minidsp_umik2-b7343c0c625b",
        "minidsp-minidsp_umik1-abcdef123456",
        "dayton_audio-dayton_umm6-abcdef123456",
        "",
        "vendor-not_a_registered_model-abc",
    ],
)
def test_the_sign_convention_comes_from_the_mic_registry(calibration_id):
    """A vendor file states either the mic's RESPONSE or a CORRECTION.

    The two differ by a sign, and reading one as the other moves every
    magnitude without moving a single timing diagnostic the fidelity gate
    checks — so it must be the REGISTRY's answer for the id the session
    banked, never a literal pinned here. An unregistered or empty id falls
    back to DEFAULT_SIGN_CONVENTION rather than to whatever the last default
    was.
    """
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        SUPPORTED_MODELS,
    )

    assert he._sign_convention(calibration_id) in {"response", "correction"}
    for key, spec in SUPPORTED_MODELS.items():
        assert he._sign_convention(f"vendor-{key}-hash") == spec["sign_convention"]
    assert he._sign_convention("") == DEFAULT_SIGN_CONVENTION
    assert he._sign_convention("vendor-not_a_registered_model-abc") == (
        DEFAULT_SIGN_CONVENTION
    )
