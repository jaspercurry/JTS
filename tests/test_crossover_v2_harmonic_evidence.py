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
import shutil
import math
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.test_crossover_v2_feature_classifier import _bundle as feature_bundle, _resonant_ir, RESONANCE_HZ
from tests.crossover_v2_fixtures import SESSION_VOLUME_DB
from jasper.cli.round_views import ARTIFACT_BY_VIEW, main
from jasper.active_speaker.candidate_parts import COMPOSITION_KIND
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs, view_path
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND

from jasper.active_speaker.crossover_v2 import harmonic_evidence as he
from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_captures
from jasper.active_speaker.crossover_v2.evidence_packet import (
    DERIVED_VIEWS,
    HARMONICS_ARTIFACT,
    build_crossover_evidence_packet,
)
from jasper.json_fields import sha256_file
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.calibration import SUPPORTED_MODELS
from jasper.audio_measurement.distortion import DriveLevel, HarmonicReading
from jasper.audio_measurement.program import (
    FrequencyBand, RoleBand, build_measure_program, build_verify_program, render_program_pcm, write_program_wav,
)
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry, MeasurementPriors, analysis_diagnostic_summary, analyze_program_capture,
)
from jasper.active_speaker.crossover_v2.programs import (
    PILOT_LEVEL_DELTA_DB, courtesy_prelude_for_phase, pilot_gains,
)
from jasper.active_speaker.crossover_v2.measure_spec import branch_target_ids_for
from jasper.audio_measurement.sweep import synchronized_sweep_metadata
from jasper.cli._refusal import EXIT_REFUSED

ORDERS = (2, 3)


# --------------------------------------------------------------------------- #
# fixtures — a bundle, a ring, and a reading
# --------------------------------------------------------------------------- #


def _beside(session: Path, name: str) -> Path:
    """Where ``name`` files beside this bundle's round (ADR-0346)."""
    return view_path(round_inputs(session), name)


def _bundle_dir(tmp_path: Path) -> Path:
    """Where :func:`_bundle` puts the bundle: nested under a bank root's
    ``bundle/`` so ``view_path`` resolves beside the round (ADR-0346)
    instead of falling back to the caller's cwd. Shared with the two
    hand-rolled bundles below that must agree with it on the same tree.
    """
    return tmp_path / "bank" / "bundle" / "session"


def _bundle(tmp_path: Path, *, harmonics: dict[str, Any] | None = None) -> Path:
    """A commissioning bundle on disk, in the real tree shape.

    Deliberately minimal: the harmonics block reads exactly one file, and a
    fixture that also staged a receipt and a cloud artifact would let a test
    pass for a reason it did not name.
    """
    session = _bundle_dir(tmp_path)
    round_dir = session / "evidence/v1/artifacts/crossover_v2/cap_TESTONLY"
    round_dir.mkdir(parents=True)
    (session / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "c2a1812b849e",
        "fingerprints": {"build_sha": "200d54578"},
    }))
    if harmonics is not None:
        _beside(session, HARMONICS_ARTIFACT).write_text(json.dumps(harmonics))
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
        "schema": ARTIFACT_BY_VIEW["distortion"].schema,
        "round_dir": "cap_TESTONLY",
        "orders": list(ORDERS),
        "program": {
            "stimulus_id": "b542773d8a8d",
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


def _harmonics(session: Path) -> dict[str, Any]:
    return build_crossover_evidence_packet(session)[DERIVED_VIEWS]["harmonics"]


def test_a_round_with_no_reading_says_it_has_none(tmp_path):
    block = _harmonics(_bundle(tmp_path))

    assert block["available"] is False
    assert block["status"] == "not_evaluated"
    assert block["n_roles"] == 0


def test_a_banked_reading_carries_the_rows(tmp_path):
    block = _harmonics(_bundle(tmp_path, harmonics=_artifact()))

    assert block["available"] is True
    assert block["schema"] == ARTIFACT_BY_VIEW["distortion"].schema
    assert block["orders"] == [2, 3]
    assert block["n_roles"] == 1

    row = block["roles"][0]["rows"][0]
    assert row["hz"] == 200.0
    assert row["h2_below_fundamental_db"] is not None
    assert row["h3_below_fundamental_db"] is not None


def test_a_banked_artifact_with_no_role_block_refuses_rather_than_reading_empty(tmp_path):
    """A file present but empty is not a reading."""
    artifact = _artifact()
    artifact["roles"] = []

    assert _harmonics(_bundle(tmp_path, harmonics=artifact))["available"] is False


@pytest.mark.parametrize("banked", ["[]", '["a list"]', '"a string"', "7"])
def test_an_artifact_that_is_not_an_object_still_names_its_reason(tmp_path, banked):
    """A file that PARSED into something that is not an object has an empty
    read reason, because the read succeeded; the block must still name one."""
    session = _bundle(tmp_path)
    _beside(session, HARMONICS_ARTIFACT).write_text(banked)

    block = _harmonics(session)

    assert block["available"] is False
    assert block["reason"].strip()


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
    block = _harmonics(_bundle(tmp_path, harmonics=artifact))

    assert block["available"] is False


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
    blocks = _harmonics(_bundle(tmp_path, harmonics=_artifact(n_roles=2)))["roles"]

    assert len(blocks) == 2
    assert {block["role"] for block in blocks} == {"woofer"}
    assert len({block["wav_sha256_12"] for block in blocks}) == 2


# --------------------------------------------------------------------------- #
# the instrument's refusals
# --------------------------------------------------------------------------- #


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "gain_plan_db": {"woofer": -6.0, "tweeter": -31.2},
    }
    state.update(overrides)
    return state


def _applied_profile(fc_hz: float = 1648.7) -> dict[str, Any]:
    """The bare shape ``_crossover_fc_hz`` reads — no SSOT schema wrapper.

    Used only for direct ``_crossover_fc_hz`` calls, which read the fields
    below without going through :func:`~.evidence_packet.applied_profile_source`.
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

    ``load_applied_baseline_profile_state`` (via ``applied_profile_source``)
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
    assert he._state_capture_session_id({"session_id": "wired-abc"}) == "wired-abc"
    # Absent, empty, and non-string all resolve to None rather than to a value
    # a reader might compare against something.
    assert he._state_capture_session_id({}) is None
    assert he._state_capture_session_id({"session_id": ""}) is None
    assert he._state_capture_session_id({"session_id": 7}) is None

    assert _harmonics(_bundle(tmp_path, harmonics=_artifact()))["program"]["state_capture_session_id"] == "wired-TESTONLY"


def test_the_crossover_corner_is_read_from_the_applied_profile_not_a_flag():
    """It is a fact about the round, and the shipped analysis refuses without it.

    A flag would let an operator hand this instrument a different corner from
    the one the captures were taken through, which would move the analysis's
    per-driver expectations without moving anything a reader could see.
    """
    assert he._crossover_fc_hz(_applied_profile(), "") == pytest.approx(1648.7)

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he._crossover_fc_hz({}, "")
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert "fc_hz" in excinfo.value.detail["missing"]


def test_the_corner_reports_absent_with_reason_never_a_stash_fallback():
    """No readable applied-profile SSOT refuses with ITS reason, nothing else.

    ``_crossover_fc_hz`` used to read a flow state's ``pre_apply_profile`` — the
    Undo stash, one apply behind after any v2 apply and arbitrarily behind
    after an apply through a door that never touches v2 state. It now takes
    the SSOT (or the reason there is none) directly and has no stash to fall
    back to even if it wanted one.
    """
    with pytest.raises(EvidenceUnavailable) as excinfo:
        he._crossover_fc_hz(None, "no applied baseline profile was supplied")
    assert excinfo.value.reason == he.STATE_UNREADABLE
    assert excinfo.value.detail["reason"] == "no applied baseline profile was supplied"


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), True, "1648.7", None])
def test_an_unusable_corner_refuses_rather_than_being_coerced(value):
    """``True`` is an ``int`` in Python and would otherwise pass as 1 Hz."""
    with pytest.raises(EvidenceUnavailable) as excinfo:
        he._crossover_fc_hz({
            "recomposition_snapshot": {
                "preset": {"crossover_regions": [{"fc_hz": value}]}
            }
        }, "")
    assert excinfo.value.reason == he.STATE_UNREADABLE


def test_a_state_without_a_gain_plan_refuses_by_name():
    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.rebuild_measure_program(_state(gain_plan_db={"woofer": -6.0}), he_bands(), {"not-a-real-id"})
    assert excinfo.value.reason == he.STATE_UNREADABLE


def he_bands() -> dict[str, tuple[float, float]]:
    return {"woofer": (150.0, 4000.0), "tweeter": (1600.0, 20000.0)}


def test_a_program_that_cannot_prove_itself_is_refused_not_read():
    """The whole point of proving the rebuild instead of asserting it: the
    refusal is what a wrong band pair produces, rather than a reading taken
    through the wrong sweep L.
    """
    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.rebuild_measure_program(_state(), he_bands(), {"not-a-real-id"})

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.detail["stimulus_ids"] == ["not-a-real-i"]


def test_a_ring_with_no_measure_capture_says_why_a_verify_one_would_not_do(tmp_path):
    """Harmonics need the per-driver program; a summed capture cannot attribute."""
    ring = tmp_path / "dumps" / "sidecar"
    ring.mkdir(parents=True)
    (ring / "1_verify_x.json").write_text(json.dumps({"phase": "verify"}))

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.read_round_harmonics(
            tmp_path, tmp_path / "dumps", _state(), he_bands(),
            applied_profile_path=_write_applied_profile(tmp_path),
        )
    assert excinfo.value.reason == he.NO_ADMISSIBLE_CAPTURES


def _program_at(downstream_db: float):
    """A real MEASURE program composed at one session volume."""
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


@pytest.mark.parametrize("volume", [-20.0, -24.7, -43.0, -59.9])
def test_a_program_at_any_fader_is_proved_by_the_id_its_takes_recorded(volume):
    """The id leaves the fader out (#5012), so the proof needs no recorded volume."""
    recorded = _program_at(volume).stimulus_id

    program, prelude = he.rebuild_measure_program(_state(), he_bands(), {recorded})

    assert program.stimulus_id == recorded
    assert prelude is False


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

    state = _state(measure_sweep_durations_s=durations)

    rebuilt, prelude = he.rebuild_measure_program(state, he_bands(), {program.stimulus_id})

    assert rebuilt.stimulus_id == program.stimulus_id
    assert prelude is False


def test_a_duration_fitted_round_that_predates_banking_still_names_its_cause():
    """No replay capability is lost: the honest refusal from before this fix
    stands, unchanged, for a round that never banked its realized durations —
    #2921 fitted the sweep, and this round predates #2923's bank, so this
    replay composes at the nominal length and cannot match.
    """
    program = _fitted_program_at(-20.0)

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.rebuild_measure_program(_state(), he_bands(), {program.stimulus_id})

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.detail["measure_sweep_durations_banked"] is False
    assert excinfo.value.detail["measure_sweep_durations_usable"] is False
    assert excinfo.value.detail["causes"] == ["sweep_durations_unbanked", "bands_wrong", "not_a_measure_round"]


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

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.rebuild_measure_program(state, he_bands(), {"not-a-real-id"})

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.detail["measure_sweep_durations_banked"] is True
    assert excinfo.value.detail["measure_sweep_durations_usable"] is False
    assert excinfo.value.detail["causes"][0] == "sweep_durations_unusable"


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

    state = _state(measure_sweep_durations_s=durations)
    accepted = he._banked_sweep_durations_s(state, _DATASHEET_WIDE_BANDS)
    assert accepted is not None
    assert accepted == pytest.approx(durations)

    rebuilt, prelude = he.rebuild_measure_program(state, _DATASHEET_WIDE_BANDS, {program.stimulus_id})

    assert rebuilt.stimulus_id == program.stimulus_id
    assert prelude is False


def test_a_datasheet_wide_band_refusal_still_reports_the_bank_honestly():
    """Even when the round refuses for an UNRELATED reason (here: a
    ``stimulus_id`` that simply does not match anything, standing in for a
    wrong gain plan or wrong bands), a usable ≥24 kHz bank must still read
    as banked AND usable in the evidence — not as "did not bank" merely
    because its band happens to need the composer's clamp.
    """
    from jasper.active_speaker.crossover_v2 import priors

    program = _fitted_program_at(-20.0, bands=_DATASHEET_WIDE_BANDS)
    durations = priors.measure_sweep_durations_s(program)
    assert durations is not None
    state = _state(measure_sweep_durations_s=durations)

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he.rebuild_measure_program(state, _DATASHEET_WIDE_BANDS, {"not-a-real-id"})

    assert excinfo.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert excinfo.value.detail["measure_sweep_durations_banked"] is True
    assert excinfo.value.detail["measure_sweep_durations_usable"] is True
    assert excinfo.value.detail["causes"][0] == "banked_sweep_durations_wrong"


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
    """Both instruments read one sidecar and WAV layout.

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
    with pytest.raises(EvidenceUnavailable) as refusal:
        load_round_captures(round_dir, ring, session_id="mine")
    assert refusal.value.detail["phases_seen"] == {"measure": 1}
    assert refusal.value.detail["dumps_dir"] == ring.name


def test_the_ring_is_scoped_before_valid_capture_deduplication(tmp_path):
    """A rolling ring can hold another round's captures, and re-analyses of one."""
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "mine"),
        ("2_measure_b", "aaa", "mine"),      # same capture, second analysis
        ("3_measure_c", "ccc", "theirs"),    # another round
        ("4_measure_d", "ddd", "mine"),
    ])

    captures, scope = he._scope_captures(he._bind_measure_captures(ring), "mine")

    assert [capture["wav_sha256"] for capture in captures] == ["aaa", "aaa", "ddd"]
    assert scope["session_id"] == "mine"
    # The scope reports the whole ring it chose from, not just what survived —
    # otherwise a reader cannot tell a one-capture round from a one-capture
    # SLICE of a busy ring.
    assert scope["n_ring_captures"] == 4


def test_an_unscoped_ring_holding_several_sessions_refuses_instead_of_pooling(tmp_path):
    ring = _ring(tmp_path, [
        ("1_measure_a", "aaa", "session-one"),
        ("2_measure_b", "bbb", "session-two"),
    ])

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he._scope_captures(he._bind_measure_captures(ring), None)

    assert excinfo.value.reason == he.RING_NOT_SCOPED_TO_ONE_SESSION
    assert excinfo.value.detail["distinct_session_ids"] == [
        "session-one", "session-two",
    ]
    assert "amplitude-invariant" in excinfo.value.detail["note"]


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

    with pytest.raises(EvidenceUnavailable) as excinfo:
        he._scope_captures(he._bind_measure_captures(ring), None)

    assert excinfo.value.reason == he.RING_NOT_SCOPED_TO_ONE_SESSION
    assert excinfo.value.detail["n_unattributed"] == 1


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


def test_a_sidecar_with_no_wav_stays_available_for_the_omission_record(tmp_path):
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text(json.dumps({
        "phase": "measure", "wav_sha256": "aaa",
    }))

    captures = he._bind_measure_captures(ring)
    assert len(captures) == 1
    assert captures[0]["take_id"] == "1_measure_a"
    assert not captures[0]["wav"].exists()


def test_a_null_reading_never_reaches_json_as_a_number():
    assert he._nullable(float("nan")) is None
    assert he._nullable(float("inf")) is None
    assert he._nullable(-46.24) == -46.2
    assert he._nullable(0.27449, 3) == 0.274


def test_the_artifact_name_has_one_owner():
    """The writer, the reader and the CLI resolve one spelling."""
    assert he.HARMONICS_ARTIFACT == HARMONICS_ARTIFACT == "harmonic_distortion.json"
    assert ARTIFACT_BY_VIEW["distortion"].artifact is HARMONICS_ARTIFACT


def test_the_distortion_door_composes_the_shape_the_round_actually_swept(tmp_path):
    """Both directions, because a derivation that answered ``full_range`` for
    every round would break every 2-way one — and the 1-way arm is then driven
    all the way through the rebuild, which proves itself against the banked
    ``stimulus_id`` and so could only refuse a two-role composition."""
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
        with pytest.raises(EvidenceUnavailable) as excinfo:
            he.round_bands_hz(state, overrides)
        assert excinfo.value.reason == he.STATE_UNREADABLE
    # And the verb publishes that named refusal as the refused exit, rather
    # than letting an instrument's own exception reach the operator raw.
    bundle = tmp_path / "bundle"
    (bundle / "evidence/v1/artifacts/crossover_v2/cap-1").mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": "s-1"}))
    flow_state = bundle / "crossover-v2-state.json"
    flow_state.write_text(json.dumps({"session_id": "cap-1", "gain_plan_db": {"woofer": -6.0, "horn": -31.2}}))
    assert main([
        "distortion", str(bundle),
    ]) == EXIT_REFUSED

    program = build_measure_program(
        {"full_range": -11.0},
        (RoleBand("full_range", 0, FrequencyBand(*band)),),
        downstream_gain_db=-20.0,
        leading_pilot_gains_db=(-11.0 - PILOT_LEVEL_DELTA_DB, -11.0),
        leading_pilot_role="full_range",
        courtesy_prelude=courtesy_prelude_for_phase("measure"),
    )
    state = {"gain_plan_db": {"full_range": -11.0}}
    bands = he.round_bands_hz(state, overrides)

    assert bands == {"full_range": band}
    rebuilt, _prelude = he.rebuild_measure_program(state, bands, {program.stimulus_id})
    assert rebuilt.stimulus_id == program.stimulus_id

    # A 1-way round measured on a non-default band gets an operator remedy,
    # same as the pair's --woofer-band / --tweeter-band.
    args = build_parser().parse_args(
        ["distortion", "bundle",
         "--full-range-band", "45:18000"],
    )
    assert args.full_range_band == (45.0, 18000.0)


def test_the_orders_the_product_publishes_are_not_the_kernels_ceiling():
    """A kernel that learned a 4th order must not widen a banked schema."""
    from jasper.audio_measurement import deconv

    assert he.HARMONIC_ORDERS == (2, 3)
    assert he.HARMONIC_ORDERS is not deconv.DEFAULT_HARMONIC_ORDERS


def test_reading_a_capture_ring_never_raises_on_a_hand_edited_sidecar(tmp_path):
    """A bad artifact is a fact this instrument reports, never a crash."""
    ring = tmp_path / "dumps"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    (ring / "sidecar" / "1_measure_a.json").write_text("{not json")
    (ring / "sidecar" / "2_measure_b.json").write_text(json.dumps(["a list"]))
    (ring / "sidecar" / "3_measure_c.json").write_text(json.dumps({"phase": 7}))

    omissions = []
    captures = he._bind_measure_captures(ring, unscoped_omissions=omissions)
    assert captures == []
    assert omissions == [
        {"sidecar": f"{index}_measure_{letter}.json", "reason": "sidecar_malformed"}
        for index, letter in enumerate("abc", 1)
    ]


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
    with pytest.raises(ValueError):
        he._read_mono(path, sample_rate_hz=44_100)
    truncated = tmp_path / "truncated.wav"
    truncated.write_bytes(path.read_bytes()[:-width])
    with pytest.raises(ValueError):
        he._read_mono(truncated)

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


def _write_harmonic_capture(ring, tmp_path, name, program, state, *, take_id="take-a",
                            volume_db=SESSION_VOLUME_DB, scale=0.1):
    wav = ring / f"wav/{name}.wav"
    samples = np.pad(render_program_pcm(program).sum(axis=1).astype(float) * scale, (24000, 24000))
    with wave.open(str(wav), "wb") as writer:
        writer.setparams((1, 4, 48000, len(samples), "NONE", "not compressed"))
        writer.writeframes((samples * 2**31).astype("<i4").tobytes())
    diagnostic = analysis_diagnostic_summary(analyze_program_capture(
        program, he._read_mono(wav), 48000, geometry=MeasurementGeometry(),
        priors=MeasurementPriors(crossover_fc_hz=1800.0),
    ))
    stimulus = tmp_path / "stimulus.wav"
    write_program_wav(stimulus, program)
    sidecar = ring / f"sidecar/{name}.json"
    document = {
        "phase": "measure", "take_id": take_id, "position_deg": 0,
        "wav_sha256": sha256_file(wav), "diagnostic": diagnostic,
        "jts_session_identity": {"session_id": "c2a1812b849e",
                                 "aliases": {"capture_session_id": state["session_id"]}},
        "provenance": {"stimulus": {"stimulus_id": program.stimulus_id,
                                    "wav_sha256": sha256_file(stimulus)}},
    }
    if volume_db is not None:
        document["provenance"]["session_volume_db"] = volume_db
    if any(":" in role for role in state["gain_plan_db"]):
        document.update(phase="cloud_verify", graph_scope="candidate_branches", program=program.to_dict())
    sidecar.write_text(json.dumps(document))
    return sidecar, wav, document


@pytest.fixture
def harmonic_capture(tmp_path, request):
    bundle = _bundle(tmp_path)
    round_dir = next((bundle / "evidence/v1/artifacts/crossover_v2").iterdir())
    bands = he_bands()
    role_bands = tuple(RoleBand(role, i, FrequencyBand(*band)) for i, (role, band) in enumerate(bands.items()))
    branch_pair = getattr(request, "param", None)

    def compose(gain, pair=branch_pair, volume_db=SESSION_VOLUME_DB):
        gains = {"woofer": gain, "tweeter": gain - 10.0}
        program = build_measure_program(
            gains, role_bands, downstream_gain_db=volume_db,
            leading_pilot_gains_db=pilot_gains(gain), leading_pilot_role="woofer",
            courtesy_prelude=False,
        )
        if pair:
            targets = branch_target_ids_for(pair, role_bands)
            program = build_branch_program(build_verify_program(
                1800.0, gain_db=gain, downstream_gain_db=volume_db,
                sweep_band_hz=(150.0, 4000.0), leading_pilot_gains_db=pilot_gains(gain),
            ), {target: channel for channel, target in enumerate(targets)})
            gains = dict.fromkeys(targets, gain)
        return program, {"session_id": round_dir.name, "gain_plan_db": gains}

    program, state = compose(-16.0)
    ring = tmp_path / "ring"
    (ring / "sidecar").mkdir(parents=True)
    (ring / "wav").mkdir()
    sidecar, wav, document = _write_harmonic_capture(
        ring, tmp_path, "1_measure_a", program, state,
    )
    profile = _write_applied_profile(tmp_path, fc_hz=1800.0)

    def read(supplied_state=state, *, scope="c2a1812b849e", output_dir=round_dir):
        return he.read_round_harmonics(output_dir, ring, supplied_state, bands,
                                       session_id=scope, applied_profile_path=profile)

    return read, compose, sidecar, wav, document


def bank_measure_capture(harmonic_capture, tmp_path: Path) -> Path:
    """The fixture's MEASURE capture filed in a session, banked; the banked round."""
    _, compose, _, wav, document = harmonic_capture
    session = _bundle_dir(tmp_path)
    capture_id = document["jts_session_identity"]["aliases"]["capture_session_id"]
    artifacts = session / f"evidence/v1/artifacts/crossover_v2/{capture_id}"
    positions = artifacts / "positions"
    positions.mkdir()
    captured = session / "summed/measure.wav"
    captured.parent.mkdir()
    shutil.copyfile(wav, captured)
    document.update(kind=POSITION_EVIDENCE_KIND, run_id=capture_id,
                    captured_at="2026-08-31T00:19:52Z", wav_path="summed/measure.wav")
    (positions / "measure.json").write_text(json.dumps(document))
    program, state = compose(-16.0)
    write_program_wav(artifacts / "measure_program.wav", program)
    (session / "crossover-v2-state.json").write_text(json.dumps(state, sort_keys=True))
    return bank_round(session, campaign_root=tmp_path / "bank",
                      applied_profile_path=tmp_path / "applied-profile.json").path


@pytest.mark.parametrize("harmonic_capture,targets", [
    (None, {"woofer", "tweeter"}),
    ("front_rear", {"woofer", "woofer:rear"}),
], indirect=["harmonic_capture"])
def test_harmonics_publishes_banked_physical_targets(harmonic_capture, tmp_path, capsys, targets):
    banked = bank_measure_capture(harmonic_capture, tmp_path)
    assert main(["distortion", str(banked)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["captures_read"], result["captures_refused"]) == (1, 0)
    artifact = json.loads(next(banked.rglob(he.HARMONICS_ARTIFACT)).read_text())
    assert {row["role"] for row in artifact["roles"]} == targets
    assert all(row["rows"] for row in artifact["roles"])


def test_harmonics_reads_each_capture_with_its_own_program(harmonic_capture, tmp_path):
    read, compose, sidecar, _, _ = harmonic_capture
    program, state = compose(-14.0, "front_rear")
    _write_harmonic_capture(sidecar.parent.parent, tmp_path, "2_branch_b", program, state, take_id="take-b")
    artifact = read()
    assert (artifact["captures"]["n_read"], artifact["captures"]["n_refused"]) == (2, 0)
    assert {row["role"] for row in artifact["roles"]} == {"woofer", "tweeter", "woofer:rear"}


@pytest.mark.parametrize("fault,reason", [
    ("program", None),
    ("state", "capture_session_mismatch"),
    ("capture", "capture_session_mismatch"),
    ("stimulus", "stimulus_wav_mismatch"),
])
def test_harmonic_drive_requires_this_takes_program_and_source(harmonic_capture, fault, reason):
    read, compose, sidecar, wav, document = harmonic_capture
    original = read()
    assert original["captures"]["n_read"] == 1
    assert original["captures"]["read"][0]["identity"]["stimulus_id_status"] == "matched"
    assert original["captures"]["read"][0]["fidelity_fields_compared"] == 5
    changed_program, state = compose(-12.0 if fault == "program" else -16.0)
    if fault == "program":
        readings, failures, _, compared = he._read_one_capture(
            changed_program, he._read_mono(wav), document,
            orders=ORDERS, calibration=None, fc_hz=1800.0,
        )
        assert failures == [] and compared == 5
        old_drive = next(row for row in original["roles"] if row["role"] == "woofer")["drive"]
        new_drive = next(row for row in readings if row.role == "woofer").drive
        assert new_drive.stimulus_peak_dbfs - old_drive["stimulus_peak_dbfs"] == pytest.approx(4.0)
    elif fault == "state":
        state["session_id"] = "capture-other"
    elif fault == "capture":
        document["jts_session_identity"]["aliases"]["capture_session_id"] = "capture-other"
    else:
        document["provenance"]["stimulus"]["wav_sha256"] = "f" * 64
    sidecar.write_text(json.dumps(document))
    with pytest.raises(EvidenceUnavailable) as refused:
        read(state)
    if reason is None:
        # A gain plan that cannot rebuild the stimulus the take recorded proves nothing.
        assert refused.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    else:
        assert refused.value.reason == he.NO_CAPTURE_PASSED_THE_GATES
        assert refused.value.detail["refused"][0]["reason"] == reason


@pytest.mark.parametrize("fault,reason", [("changed", "capture_wav_mismatch"), ("missing", "capture_wav_missing")])
def test_harmonics_keeps_good_takes_and_names_lost_or_changed_wavs(harmonic_capture, fault, reason):
    read, _, sidecar, wav, document = harmonic_capture
    other_sidecar = sidecar.with_name("2_measure_b.json")
    other_wav = wav.with_name("2_measure_b.wav")
    other = {**document, "take_id": "take-b", "position_deg": 15}
    other_sidecar.write_text(json.dumps(other))
    if fault == "changed":
        other_wav.write_bytes(wav.read_bytes() + b"changed")
    artifact = read()
    assert artifact["captures"]["n_read"] == 1
    assert artifact["captures"]["n_refused"] == 1
    omitted = artifact["captures"]["refused"][0]
    assert (omitted["take_id"], omitted["position_deg"], omitted["reason"]) == ("take-b", 15, reason)
    assert all(block["rows"] for block in artifact["roles"])


def test_each_take_reads_its_drive_at_its_own_recorded_session_volume(harmonic_capture, tmp_path):
    """A quieter take first in the ring (a ladder's lowest rung, an earlier
    re-measure) neither sinks the round nor lends the others its fader."""
    read, compose, sidecar, _, _ = harmonic_capture
    quiet_db = SESSION_VOLUME_DB - 5.0
    quiet, state = compose(-16.0, volume_db=quiet_db)
    _write_harmonic_capture(sidecar.parent.parent, tmp_path, "0_measure_quiet", quiet, state,
                            take_id="take-quiet", volume_db=quiet_db, scale=0.05)

    artifact = read()

    volumes = {take["wav_sha256_12"]: take["program"]["session_volume_db"] for take in artifact["captures"]["read"]}
    assert sorted(volumes.values()) == [quiet_db, SESSION_VOLUME_DB]
    for block in artifact["roles"]:
        drive = block["drive"]
        assert drive["effective_peak_dbfs"] == pytest.approx(
            drive["stimulus_peak_dbfs"] + volumes[block["wav_sha256_12"]])


def test_a_ring_that_recorded_no_session_volume_reads_its_effective_peak_as_unknown(harmonic_capture):
    read, _, sidecar, _, document = harmonic_capture
    del document["provenance"]["session_volume_db"]
    sidecar.write_text(json.dumps(document))
    unrecorded = read()
    assert unrecorded["captures"]["read"][0]["program"]["session_volume_db"] is None
    for block in unrecorded["roles"]:
        assert block["drive"]["status"] == "unknown"
        assert block["drive"]["stimulus_peak_dbfs"] is not None
        assert block["drive"]["effective_peak_dbfs"] is None
        assert block["drive"]["reason"] == "session_volume_unrecorded"


@pytest.mark.parametrize("readback_offset_db,status,readback", [
    (None, "recorded_session_volume", "unrecorded"),
    (0.0, "recorded_session_volume", "matched"),
    (6.0, "unknown", "mismatched"),
])
def test_a_measure_takes_drive_rests_on_its_recorded_volume_checked_by_the_readback(
        harmonic_capture, readback_offset_db, status, readback):
    """The id does not prove a recorded session volume (#5012): the drive says it is
    recorded, and a fader readback that disagrees withholds it."""
    read, _, sidecar, _, document = harmonic_capture
    if readback_offset_db is not None:
        document["provenance"]["main_volume_db"] = SESSION_VOLUME_DB + readback_offset_db
    sidecar.write_text(json.dumps(document))

    for block in read()["roles"]:
        drive = block["drive"]
        assert (drive["status"], drive["session_volume_readback"]) == (status, readback)
        assert (drive["effective_peak_dbfs"] is None) is (status == "unknown")


def test_a_round_whose_takes_banked_a_superseded_program_schema_names_it(harmonic_capture):
    read, compose, sidecar, _, document = harmonic_capture
    program, _ = compose(-16.0)
    document["program"] = {**program.to_dict(), "schema_version": 1}
    document["provenance"]["stimulus"]["stimulus_id"] = "0" * 64
    sidecar.write_text(json.dumps(document))

    with pytest.raises(EvidenceUnavailable) as refused:
        read()

    assert refused.value.reason == he.PROGRAM_NOT_REPRODUCIBLE
    assert refused.value.detail["causes"] == ["program_schema_superseded"]
    assert refused.value.detail["recorded_program_schema_versions"] == [1]


def test_measure_takes_with_no_recorded_stimulus_id_are_refused_one_by_one(harmonic_capture):
    read, _, sidecar, _, document = harmonic_capture
    del document["provenance"]["stimulus"]
    sidecar.write_text(json.dumps(document))

    with pytest.raises(EvidenceUnavailable) as refused:
        read()

    assert refused.value.reason == he.NO_CAPTURE_PASSED_THE_GATES
    assert [take["reason"] for take in refused.value.detail["refused"]] == ["stimulus_id_unrecorded"]


@pytest.mark.parametrize("old,new_key_takes,readable", [
    ("measure", (), []),
    ("measure", ("take-b", "take-c"), ["take-b", "take-c"]),
    ("branch", (), ["take-a"]),
])
def test_a_take_banked_under_the_old_key_is_refused_as_superseded(
        harmonic_capture, tmp_path, old, new_key_takes, readable):
    """Whatever the other takes recorded: the old key has no alias (#2902)."""
    read, compose, sidecar, _, document = harmonic_capture
    ring = sidecar.parent.parent
    program, state = compose(-16.0)
    for index, take_id in enumerate(new_key_takes):
        _write_harmonic_capture(ring, tmp_path, f"2_measure_{index}", program, state,
                                take_id=take_id, scale=0.09 - 0.01 * index)
    if old == "branch":
        branch, branch_state = compose(-14.0, "front_rear")
        sidecar, _, document = _write_harmonic_capture(ring, tmp_path, "3_branch", branch, branch_state,
                                                       take_id="take-branch")
        document["program"]["program_id"] = document["program"].pop("stimulus_id")
    stimulus = document["provenance"]["stimulus"]
    stimulus["program_id"] = stimulus.pop("stimulus_id")
    document["program"] = {**document.get("program", {}), "schema_version": 2}
    sidecar.write_text(json.dumps(document))

    if readable:
        captures = read()["captures"]
        refused = captures["refused"]
        assert sorted(take["take_id"] for take in captures["read"]) == readable
    else:
        with pytest.raises(EvidenceUnavailable) as excinfo:
            read()
        refused = excinfo.value.detail["refused"]
    assert [(take["take_id"], take["reason"]) for take in refused] == [
        (document["take_id"], "program_schema_superseded")]


def test_a_stray_stimulus_id_is_refused_on_its_own_take(harmonic_capture, tmp_path):
    """One take of another stimulus proves nothing and costs the others nothing."""
    read, compose, sidecar, _, _ = harmonic_capture
    ring = sidecar.parent.parent
    program, state = compose(-16.0)
    for name, scale in (("b", 0.09), ("c", 0.08), ("d", 0.07)):
        _write_harmonic_capture(ring, tmp_path, f"2_measure_{name}", program, state, take_id=f"take-{name}", scale=scale)
    stray, _ = compose(-14.0)
    _write_harmonic_capture(ring, tmp_path, "3_measure_stray", stray, state, take_id="take-stray")

    captures = read()["captures"]

    assert sorted(take["take_id"] for take in captures["read"]) == ["take-a", "take-b", "take-c", "take-d"]
    assert [(take["take_id"], take["reason"]) for take in captures["refused"]] == [
        ("take-stray", "stimulus_program_mismatch")]


def test_a_round_whose_candidate_carries_a_label_reads_its_measure_takes(harmonic_capture):
    """candidate_parts labels every candidate it builds; the takes' recorded id proves the program."""
    read, compose, _, _, _ = harmonic_capture
    _, state = compose(-16.0)
    state["candidate"] = {"program_id": COMPOSITION_KIND}

    artifact = read(state)

    assert [take["take_id"] for take in artifact["captures"]["read"]] == ["take-a"]


def test_an_all_legacy_ring_still_reads_its_branch_takes(harmonic_capture, tmp_path):
    read, compose, sidecar, _, document = harmonic_capture
    document.pop("provenance")
    sidecar.write_text(json.dumps(document))
    branch, state = compose(-14.0, "front_rear")
    _write_harmonic_capture(sidecar.parent.parent, tmp_path, "2_branch_b", branch, state,
                            take_id="take-b", volume_db=None)

    artifact = read()

    assert [take["take_id"] for take in artifact["captures"]["read"]] == ["take-b"]
    assert [(take["take_id"], take["reason"]) for take in artifact["captures"]["refused"]] == [
        ("take-a", "stimulus_id_unrecorded")]


@pytest.mark.parametrize("harmonic_capture", ["front_rear"], indirect=True)
def test_a_branch_take_without_provenance_keeps_ratios_without_claiming_its_drive(harmonic_capture):
    """A branch take records its own program, so its ratios survive a missing provenance."""
    read, _, sidecar, _, document = harmonic_capture
    bound = read()
    document.pop("provenance")
    document["jts_session_identity"].pop("aliases")
    sidecar.write_text(json.dumps(document))
    legacy = read()
    assert legacy["captures"]["n_read"] == 1
    assert legacy["program"]["session_volume_db"] is None
    for original, historical in zip(bound["roles"], legacy["roles"]):
        assert historical["rows"] == original["rows"]
        assert historical["drive"]["status"] == "unknown"
        assert historical["drive"]["stimulus_peak_dbfs"] is None
        assert historical["drive"]["effective_peak_dbfs"] is None
        assert historical["drive"]["capture_peak_dbfs"] == original["drive"]["capture_peak_dbfs"]


@pytest.mark.parametrize("scope", ["c2a1812b849e", None])
def test_harmonics_accounts_for_unscoped_omissions_and_duplicate_takes(harmonic_capture, scope, monkeypatch):
    read, _, sidecar, wav, document = harmonic_capture
    for stem, take_id in (("2_measure_duplicate", "take-copy"), ("3_measure_missing", "take-lost")):
        sidecar.with_name(f"{stem}.json").write_text(json.dumps({**document, "take_id": take_id}))
    wav.with_name("2_measure_duplicate.wav").write_bytes(wav.read_bytes())
    sidecar.with_name("4_unknown.json").write_text("{bad json")
    unreadable = sidecar.with_name("5_unknown.json")
    unreadable.write_text("{}")
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    expected_unscoped = [
        {"sidecar": "4_unknown.json", "reason": "sidecar_malformed"},
        {"sidecar": "5_unknown.json", "reason": "sidecar_unreadable"},
    ]
    if scope is not None:
        sidecar.with_name("6_measure_other_round.json").write_text(json.dumps({
            **document, "jts_session_identity": {"session_id": "other-round"},
        }))
        unattributed = {key: value for key, value in document.items() if key != "jts_session_identity"}
        sidecar.with_name("7_measure_unattributed.json").write_text(json.dumps(unattributed))
        expected_unscoped.append({"sidecar": "7_measure_unattributed.json", "reason": "session_identity_missing"})
    artifact = read(scope=scope)
    captures = artifact["captures"]
    assert captures["n_read"] == captures["n_refused"] == captures["n_skipped"] == 1
    assert captures["refused"][0]["take_id"] == "take-lost"
    assert captures["refused"][0]["reason"] == "capture_wav_missing"
    assert captures["skipped"][0]["take_id"] == "take-copy"
    assert captures["skipped"][0]["duplicate_of"] == "take-a"
    assert captures["skipped"][0]["reason"] == "duplicate_wav"
    assert captures["n_unscoped_omissions"] == len(expected_unscoped)
    assert captures["unscoped_omissions"] == expected_unscoped
    assert captures["scope"]["session_id"] == "c2a1812b849e"
    assert len(artifact["roles"]) == 2


@pytest.mark.parametrize("capture_identity", ["alias", "capture_session_id", "missing"])
def test_harmonics_output_directory_name_is_not_capture_identity(harmonic_capture, tmp_path, capture_identity):
    read, _, sidecar, wav, document = harmonic_capture
    capture_session = document["jts_session_identity"]["aliases"]["capture_session_id"]
    if capture_identity != "alias":
        document["jts_session_identity"].pop("aliases")
        if capture_identity == "capture_session_id":
            document["capture_session_id"] = capture_session
    sidecar.write_text(json.dumps(document))
    original = read()
    renamed = tmp_path / "renamed-analysis-output"
    renamed.mkdir()
    output = renamed / "existing-output.json"
    output.write_text(json.dumps(original))
    assert read(output_dir=renamed) == {**original, "round_dir": renamed.name}
    assert original["captures"]["n_read"] == 1
    assert all(role["drive"]["status"] == "recorded_session_volume" for role in original["roles"])
    assert sha256_file(wav) == document["wav_sha256"]
    assert json.loads(sidecar.read_text()) == document
    assert json.loads(output.read_text()) == original


@pytest.mark.parametrize("first", ["distortion", "classify-features"])
@pytest.mark.parametrize("convention", ["response", "correction"])
def test_instruments_read_a_fresh_bank_in_either_order(harmonic_capture, tmp_path, capsys, monkeypatch, first, convention):
    monkeypatch.setitem(SUPPORTED_MODELS, "test_mic", {"sign_convention": convention})
    calibration_id = "vendor-test_mic-hash"
    calibration = tmp_path / "mic.txt"
    calibration.write_text("20 2\n20000 4\n")
    _, compose, _, wav, document = harmonic_capture
    session = _bundle_dir(tmp_path)
    info = json.loads((session / "info.json").read_text())
    info["fingerprints"] = {"mic": {"calibration_id": calibration_id}}
    (session / "info.json").write_text(json.dumps(info))
    capture_id = document["jts_session_identity"]["aliases"]["capture_session_id"]
    artifacts = session / f"evidence/v1/artifacts/crossover_v2/{capture_id}"
    positions = artifacts / "positions"
    positions.mkdir()
    captured = session / "summed" / "measure.wav"
    captured.parent.mkdir()
    shutil.copyfile(wav, captured)
    document.update(kind=POSITION_EVIDENCE_KIND, run_id=capture_id,
                    captured_at="2026-08-31T00:19:52Z", wav_path="summed/measure.wav")
    (positions / "measure.json").write_text(json.dumps(document))
    program, state = compose(-16.0)
    write_program_wav(artifacts / "measure_program.wav", program)
    (session / "crossover-v2-state.json").write_text(json.dumps(state))
    feature, ring = feature_bundle(tmp_path / "feature", _resonant_ir(3.0), phases=("lateral",))
    feature_program = next(feature.glob("evidence/v1/artifacts/**/lateral_program.wav"))
    shutil.copyfile(feature_program, artifacts / "lateral_program.wav")
    feature_doc = json.loads(next((ring / "sidecar").glob("*.json")).read_text())
    feature_doc.update(kind=POSITION_EVIDENCE_KIND, run_id=capture_id, take_id="lateral",
                       captured_at=1788135641.4, wav_path="summed/lateral.wav", position_deg=15)
    (positions / "lateral.json").write_text(json.dumps(feature_doc))
    shutil.copyfile(next((ring / "wav").glob("*.wav")), captured.with_name("lateral.wav"))
    bank = bank_round(session, campaign_root=tmp_path / "bank",
                      applied_profile_path=tmp_path / "applied-profile.json")
    shutil.rmtree(session)
    (tmp_path / "applied-profile.json").unlink()
    inputs = round_inputs(bank.path)

    def evidence() -> tuple[dict[Path, bytes], dict[str, Any]]:
        files = {p.relative_to(inputs.session_dir): p.read_bytes()
                 for p in inputs.session_dir.rglob("*") if p.is_file()}
        return files, build_crossover_evidence_packet(inputs.session_dir, round_context=inputs)

    before, packet = evidence()
    assert {json.loads(raw)["setup_calibration_id"] for path, raw in before.items()
            if path.parts[0] == "ring" and path.suffix == ".json"} == {calibration_id}
    commands = [first, "classify-features" if first == "distortion" else "distortion"]
    out = {}
    for command in commands:
        flags = ["--at", str(RESONANCE_HZ)] if command == "classify-features" else ["--calibration", str(calibration)]
        assert main([command, str(bank.path), *flags]) == 0
        answer = json.loads(capsys.readouterr().out)
        assert answer["view"] == command
        out[command] = Path(answer["out"])
    # A view is a function of the takes: it files beside the round, and the
    # round's evidence and the fingerprint a prescription answers stay put.
    after, cited = evidence()
    assert {path.parent for path in out.values()} == {bank.path}
    assert after == before
    assert cited["packet_fingerprint"] == packet["packet_fingerprint"]
    harmonic = json.loads(out["distortion"].read_text())
    feature_result = json.loads(out["classify-features"].read_text())
    views = cited[DERIVED_VIEWS]
    assert views["harmonics"]["n_roles"] == len(harmonic["roles"])
    assert views["feature_classification"]["n_rows_banked"] == len(feature_result["rows"])
    assert harmonic["captures"]["n_read"] == 1
    assert harmonic["calibration"] == {
        "applied": True, "sign_convention": convention,
        "setup_calibration_id": calibration_id, "n_points": 2,
    }
    assert {row["role"] for row in harmonic["roles"]} == {"woofer", "tweeter"}
    assert feature_result["measurement"]["n_captures"] == 1
    assert feature_result["timing_scatter"]["available"] is False
