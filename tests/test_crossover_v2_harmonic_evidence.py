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
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import harmonic_evidence as he
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
        "program": {"program_id": "b542773d8a8d", "crossover_fc_hz": 1648.7},
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
        "pre_apply_profile": {
            "recomposition_snapshot": {
                "preset": {"crossover_regions": [{"fc_hz": 1648.7}]}
            }
        },
    }
    state.update(overrides)
    return state


def test_the_crossover_corner_is_read_from_the_round_not_taken_as_a_flag():
    """It is a fact about the round, and the shipped analysis refuses without it.

    A flag would let an operator hand this instrument a different corner from
    the one the captures were taken through, which would move the analysis's
    per-driver expectations without moving anything a reader could see.
    """
    assert he._crossover_fc_hz(_state()) == pytest.approx(1648.7)

    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._crossover_fc_hz(_state(pre_apply_profile={}))
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert "fc_hz" in excinfo.value.evidence["missing"]


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), True, "1648.7", None])
def test_an_unusable_corner_refuses_rather_than_being_coerced(value):
    """``True`` is an ``int`` in Python and would otherwise pass as 1 Hz."""
    with pytest.raises(he.HarmonicEvidenceRefused) as excinfo:
        he._crossover_fc_hz(_state(pre_apply_profile={
            "recomposition_snapshot": {
                "preset": {"crossover_regions": [{"fc_hz": value}]}
            }
        }))
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
            tmp_path, tmp_path / "dumps", _real_state(), he_bands()
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


def test_the_ring_is_scoped_to_this_round_and_deduplicated_by_content(tmp_path):
    """A rolling ring can hold another round's captures, and re-analyses of one."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    for name, sha, session in (
        ("1_measure_a", "aaa", "mine"),
        ("2_measure_b", "aaa", "mine"),      # same capture, second analysis
        ("3_measure_c", "ccc", "theirs"),    # another round
        ("4_measure_d", "ddd", "mine"),
    ):
        (ring / "sidecar" / f"{name}.json").write_text(json.dumps({
            "phase": "measure",
            "wav_sha256": sha,
            "jts_session_identity": {"session_id": session},
        }))
        (ring / "wav" / f"{name}.wav").write_bytes(b"")

    bound = he._bind_measure_captures(ring, "mine")

    assert [capture["wav_sha256"] for capture in bound] == ["aaa", "ddd"]


def test_a_sidecar_with_no_wav_beside_it_is_skipped(tmp_path):
    """The ring rolls WAVs off before sidecars; a bare sidecar is not a capture."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text(json.dumps({
        "phase": "measure", "wav_sha256": "aaa",
    }))

    assert he._bind_measure_captures(ring, None) == []


def test_a_null_reading_never_reaches_json_as_a_number():
    assert he._nullable(float("nan")) is None
    assert he._nullable(float("inf")) is None
    assert he._nullable(-46.24) == -46.2
    assert he._nullable(0.27449, 3) == 0.274


def test_the_artifact_name_has_one_owner():
    """The writer, the reader and the CLI resolve one spelling."""
    from jasper.cli import read_distortion

    assert he.HARMONICS_ARTIFACT == HARMONICS_ARTIFACT == "harmonic_distortion.json"
    assert read_distortion.HARMONICS_ARTIFACT is HARMONICS_ARTIFACT


def test_the_orders_the_product_publishes_are_not_the_kernels_ceiling():
    """A kernel that learned a 4th order must not widen a banked schema."""
    from jasper.audio_measurement import distortion

    assert he.HARMONIC_ORDERS == (2, 3)
    assert he.HARMONIC_ORDERS is not distortion.DEFAULT_HARMONIC_ORDERS


def test_the_grid_the_session_volume_is_solved_over_covers_what_the_flow_composes():
    """Non-positive only, because the composer clamps there; 0.5 dB steps."""
    assert he._DOWNSTREAM_GRID_DB[0] == -40.0
    assert he._DOWNSTREAM_GRID_DB[-1] == 0.0
    assert all(value <= 0.0 for value in he._DOWNSTREAM_GRID_DB)
    assert len(he._DOWNSTREAM_GRID_DB) == 81


def test_reading_a_capture_ring_never_raises_on_a_hand_edited_sidecar(tmp_path):
    """A bad artifact is a fact this instrument reports, never a crash."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text("{not json")
    (ring / "sidecar" / "2_measure_b.json").write_text(json.dumps(["a list"]))
    (ring / "sidecar" / "3_measure_c.json").write_text(json.dumps({"phase": 7}))

    assert he._bind_measure_captures(ring, None) == []


def test_a_median_over_nothing_is_nan_not_zero():
    """A zero would be a reading; NaN becomes null, which is the honest answer."""
    assert math.isnan(he._median([]))
    assert math.isnan(he._median([float("nan")]))
    assert he._median([-50.0, -52.0, -54.0]) == -52.0
