# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The H2/H3 reading: what a take banks, what the view refuses, and what the packet says.

Two halves, pinned separately because they are two modules with one file
between them. :mod:`jasper.active_speaker.crossover_v2.harmonic_evidence` reads
the readings takes banked and files a document; the evidence packet reads that
document and declares it. The join is ``harmonic_distortion.json``, and the
thing most worth pinning is the one the ticket is about: the packet's
``not_evaluated`` row for harmonics must DISAPPEAR when a reading is banked and
must be present, by name, when one is not.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.crossover_v2_banked_round import bank_executor_take
from tests.crossover_v2_fixtures import FC_HZ
from tests.test_crossover_v2_feature_classifier import _bundle as feature_bundle, _resonant_ir, RESONANCE_HZ
from tests.run_manifest_fixture import write_bundle_manifest
from jasper.cli.round_views import ARTIFACT_BY_VIEW, main
from jasper.cli._refusal import EXIT_REFUSED
from jasper.active_speaker.bundles import mark_state
from jasper.active_speaker.round_bank import bank_round
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs, view_path

from jasper.active_speaker.crossover_v2 import harmonic_evidence as he
from jasper.active_speaker.crossover_v2.evidence_packet import (
    DERIVED_VIEWS,
    HARMONICS_ARTIFACT,
    build_crossover_evidence_packet,
)
from jasper.active_speaker.crossover_v2.measure_spec import branch_target_ids_for
from jasper.active_speaker.crossover_v2.programs import pilot_gains
from jasper.active_speaker.crossover_v2.record_index import measurement_documents, record_path, reopen_measurement_record
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.distortion import DriveLevel, HarmonicReading, read_segment_distortion
from jasper.audio_measurement.evidence_reasons import REASON_HARMONIC_WINDOW_OUT_OF_RANGE, TAKE_CURVES_NOT_BANKED, unavailable
from jasper.audio_measurement.household_mic import resolve_setup_calibration
from jasper.audio_measurement.program import (
    KIND_SWEEP, ExcitationProgram, FrequencyBand, RoleBand, build_level_probe_program, build_measure_program,
    build_verify_program, render_program_pcm,
)
from jasper.audio_measurement.program_analysis import MeasurementPriors, analyze_program_capture
from jasper.audio_measurement.sweep import synchronized_sweep_metadata
from jasper.audio_measurement.wired_capture import decode_wav_to_mono

ORDERS = (2, 3)
_ROLE_BANDS = (RoleBand("woofer", 0, FrequencyBand(150.0, 4000.0)), RoleBand("tweeter", 1, FrequencyBand(1600.0, 20000.0)))


# --------------------------------------------------------------------------- #
# fixtures — a bundle, a reading, and a banked take
# --------------------------------------------------------------------------- #


def _beside(session: Path, name: str) -> Path:
    """Where ``name`` files beside this bundle's round (ADR-0346)."""
    return view_path(round_inputs(session), name)


def _bundle_dir(tmp_path: Path) -> Path:
    """Where :func:`_bundle` puts the bundle: nested under a bank root's
    ``bundle/`` so ``view_path`` resolves beside the round (ADR-0346)
    instead of falling back to the caller's cwd.
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
        ),
        sweep=meta,
        pre_guard_s=1.5,
        required_pre_guard_s=1.4,
        preceding_silence_s=1.8,
        smoothing_fraction=12,
    )


def _artifact(n_roles: int = 1) -> dict[str, Any]:
    """A filed view, built by the block the reading banks."""
    roles = [{**he._role_block("woofer", [_reading(), _reading(offset_db=0.6)], ORDERS),
              "wav_sha256_12": "abcdef012345"}]
    if n_roles > 1:
        roles.append({**he._role_block("woofer", [_reading(), _reading(offset_db=0.2)], ORDERS),
                      "wav_sha256_12": "0123abcdef45"})
    return {
        "artifact_kind": he.HARMONICS_ARTIFACT_KIND,
        "schema": ARTIFACT_BY_VIEW["distortion"].schema,
        "orders": list(ORDERS),
        "captures": {"n_read": n_roles, "n_refused": 0, "read": [], "refused": []},
        "calibration": {"applied": False, "calibration_id": None, "curve_fingerprint": None},
        "roles": roles,
    }


def _program(branch: bool) -> ExcitationProgram:
    """The shipped MEASURE program's shape, or a front-rear candidate-branch program."""
    if branch:
        targets = branch_target_ids_for("front_rear", _ROLE_BANDS)
        return build_branch_program(build_verify_program(
            FC_HZ, gain_db=-14.0, downstream_gain_db=-20.0, sweep_band_hz=(150.0, 4000.0),
            leading_pilot_gains_db=pilot_gains(-14.0)), {target: channel for channel, target in enumerate(targets)})
    return build_measure_program({"woofer": -16.0, "tweeter": -26.0}, _ROLE_BANDS, downstream_gain_db=-20.0,
                                 leading_pilot_gains_db=pilot_gains(-16.0), leading_pilot_role="woofer",
                                 courtesy_prelude=False)


def bank_driver_take(root: Path, monkeypatch: pytest.MonkeyPatch, *, branch: bool = False) -> tuple[Path, dict[str, Any]]:
    """One per-driver take through the capture host, analysed from a recording
    with a mild nonlinearity: a MEASURE take, or a candidate-branch take. Its
    live bundle, and its banked record."""
    program = _program(branch)
    pcm = render_program_pcm(program).sum(axis=1) * 0.1
    signal = np.concatenate([np.zeros(24000), pcm + 0.5 * pcm ** 2 + 0.2 * pcm ** 3, np.zeros(24000)])
    signal += np.random.default_rng(8).normal(0, 1e-6, signal.size)
    record = bank_executor_take(root, monkeypatch, program=program, recording=(signal * (2 ** 31 - 1)).astype(np.int32),
                                raw_record={"graph_scope": "candidate_branches"} if branch else None)
    bundle, = {path.parent for path in (root / "sessions").glob("*/info.json")}
    return bundle, record


@pytest.fixture(scope="module")
def banked_takes(tmp_path_factory) -> dict[bool, tuple[Path, dict[str, Any]]]:
    """A MEASURE take and a candidate-branch take, keyed by ``branch``."""
    with pytest.MonkeyPatch.context() as patch:
        return {branch: bank_driver_take(tmp_path_factory.mktemp("branch" if branch else "measure"), patch, branch=branch)
                for branch in (False, True)}


def _copied(banked: tuple[Path, dict[str, Any]], tmp_path: Path) -> tuple[Path, Path]:
    """A copy of a banked take's bundle, and the path of its record in it."""
    bundle = shutil.copytree(banked[0], tmp_path / "bundle")
    record, = (bundle / record_path(row) for row, _ in measurement_documents(bundle))
    return bundle, record


# --------------------------------------------------------------------------- #
# the ticket — the not_evaluated row opens and closes
# --------------------------------------------------------------------------- #


def _harmonics(session: Path) -> dict[str, Any]:
    return build_crossover_evidence_packet(session)[DERIVED_VIEWS]["harmonics"]


def test_a_round_with_no_reading_says_it_has_none(tmp_path):
    block = _harmonics(_bundle(tmp_path))

    assert (block["status"], block["reason"], block["n_roles"]) == ("unavailable", "source_absent", 0)


def test_a_banked_reading_carries_the_rows(tmp_path):
    block = _harmonics(_bundle(tmp_path, harmonics=_artifact()))

    assert block["status"] == "available"
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

    block = _harmonics(_bundle(tmp_path, harmonics=artifact))
    assert (block["status"], block["reason"]) == ("unavailable", "field_null")


@pytest.mark.parametrize("banked", ["[]", '["a list"]', '"a string"', "7"])
def test_an_artifact_that_is_not_an_object_still_names_its_reason(tmp_path, banked):
    """A file that PARSED into something that is not an object has an empty
    read reason, because the read succeeded; the block must still name one."""
    session = _bundle(tmp_path)
    _beside(session, HARMONICS_ARTIFACT).write_text(banked)

    block = _harmonics(session)

    assert (block["status"], block["reason"]) == ("unavailable", "field_null")


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

    assert (block["status"], block["reason"]) == ("unavailable", "field_null")


# --------------------------------------------------------------------------- #
# the rows themselves
# --------------------------------------------------------------------------- #


def test_a_reading_past_an_orders_own_band_edge_is_null_not_a_number():
    """NaN reaches JSON as null, because a number there would read as clean.

    H3 on this sweep is real only to ``f2/3``; above it the image collapses into
    the regularization floor. A very negative float published there would be
    read as a preternaturally clean driver exactly where nothing was measured.
    """
    block = he._role_block("woofer", [_reading(), _reading(offset_db=0.4)], ORDERS)
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

    single = he._role_block("woofer", [_reading()], ORDERS)
    assert single["rows"][0]["h2_repeat_spread_db"] is None


def test_a_point_is_floor_limited_by_majority_vote_of_the_repeats():
    """One sweep's noise spike cannot flag a point the others read as clear."""
    block = he._role_block("woofer", [_reading(), _reading(offset_db=0.4)], ORDERS)
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
    block = he._role_block("woofer", readings, ORDERS)

    assert block["worst"]["h2"] is None
    assert block["floor_limited_fraction"]["h2"] == 1.0
    # H3 is untouched and still reports, so the refusal above is about H2's
    # floor rather than about the block having given up on the whole capture.
    assert block["worst"]["h3"] is not None


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


def test_a_null_reading_never_reaches_json_as_a_number():
    assert he._nullable(float("nan")) is None
    assert he._nullable(float("inf")) is None
    assert he._nullable(-46.24) == -46.2
    assert he._nullable(0.27449, 3) == 0.274


def test_a_median_over_nothing_is_nan_not_zero():
    """A zero would be a reading; NaN becomes null, which is the honest answer."""
    assert math.isnan(he._median([]))
    assert math.isnan(he._median([float("nan")]))
    assert he._median([-50.0, -52.0, -54.0]) == -52.0


def test_the_orders_the_product_publishes_are_not_the_kernels_ceiling():
    """A kernel that learned a 4th order must not widen a banked schema."""
    from jasper.audio_measurement import deconv

    assert he.HARMONIC_ORDERS == (2, 3)
    assert he.HARMONIC_ORDERS is not deconv.DEFAULT_HARMONIC_ORDERS


# --------------------------------------------------------------------------- #
# what a take banks, and what the view reads of it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("branch", [False, True], ids=["measure", "branch"])
def test_a_driver_take_banks_the_reading_a_decode_of_its_recording_reads(banked_takes, tmp_path, capsys, branch):
    """The capture host banks each role's pooled H2/H3 rows exactly as a fresh
    decode of the take's recording reads them, at its analysis's anchors, drift
    and calibration, and the view serves them with every recording gone."""
    live, document = banked_takes[branch]
    program = ExcitationProgram.from_dict(document["program"])
    (row, _), = measurement_documents(live)
    samples, rate = decode_wav_to_mono(reopen_measurement_record(live, record_path(row))[1]())
    curve = resolve_setup_calibration(document["capture_setup"], device=document["capture_device"],
                                      root=live.parent.parent / "calibration").curve
    analysis = analyze_program_capture(program, samples, rate, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    anchors = {location.segment_id: location.scheduled_start for location in analysis.locations}
    readings: dict[str, list[HarmonicReading]] = {}
    for segment in program.stimulus_segments():
        if segment.kind == KIND_SWEEP:
            readings.setdefault(str(segment.role), []).append(read_segment_distortion(
                program, samples, segment.segment_id, anchors[segment.segment_id], orders=ORDERS,
                calibration=curve, epsilon=analysis.drift.epsilon_ppm / 1e6))
    banked = document["analysis"]["distortion"]
    assert banked == {"orders": list(ORDERS), "roles": [he._role_block(role, readings[role], ORDERS) for role in sorted(readings)]}

    bundle, _ = _copied(banked_takes[branch], tmp_path)
    for recording in bundle.rglob("*.wav"):
        recording.unlink()
    assert main(["distortion", str(bundle), "--out", str(tmp_path / "harmonics.json")]) == 0
    answer = json.loads(capsys.readouterr().out)
    artifact = json.loads((tmp_path / "harmonics.json").read_text())
    label = {"status": "program_declared"} if branch else {
        "status": "recorded_session_volume", "session_volume_readback": "unrecorded"}
    assert artifact["roles"] == [{**block, "wav_sha256_12": document["wav_sha256"][:12], "drive": {**block["drive"], **label}}
                                 for block in banked["roles"]]
    assert artifact["calibration"] == document["capture_calibration"]
    assert (answer["captures_read"], answer["captures_refused"]) == (1, 0)
    assert answer["parameters"]["calibration_id"] == document["capture_calibration"]["calibration_id"]


@pytest.mark.parametrize("program", [
    build_verify_program(FC_HZ, sweep_s=0.5),
    build_level_probe_program(_ROLE_BANDS[0], (-40.0, -34.0), sweep_band_hz=(150.0, 4000.0), gap_s=0.5,
                              downstream_gain_db=-20.0, channels=2),
], ids=["summed", "level_probe"])
def test_a_take_with_no_per_driver_sweep_or_a_level_probe_banks_no_reading(program):
    """Only a program that plays per-driver sweeps banks a reading (ADR-0394),
    and nothing else of the analysis is read."""
    assert he.distortion_evidence(program, None, np.zeros(8), None) is None


def test_a_take_without_a_reading_is_refused_by_its_reason_beside_the_takes_that_read(banked_takes, tmp_path):
    """A take whose analysis failed, or whose reading banked a gap, is listed
    with its reason; a take whose program banks no reading is passed over."""
    bundle, record = _copied(banked_takes[False], tmp_path)
    document = json.loads(record.read_text())
    analysis = document["analysis"]
    failed = {key: value for key, value in document.items() if key not in {"analysis", "curves"}}
    for name, take in (("take-failed", {**failed, "analysis_error": {"code": "internal_error", "error_type": "ValueError"}}),
                       ("take-gap", {**document, "analysis": {**analysis, "distortion": unavailable(REASON_HARMONIC_WINDOW_OUT_OF_RANGE)}}),
                       ("take-probe", {**document, "analysis": {**analysis, "distortion": None}})):
        record.with_name(f"{name}.json").write_text(json.dumps({**take, "take_id": name}))

    captures = he.read_round_harmonics(bundle)["captures"]

    assert [take["take_id"] for take in captures["read"]] == [document["take_id"]]
    assert [(take["take_id"], take["reason"]) for take in captures["refused"]] == [
        ("take-failed", TAKE_CURVES_NOT_BANKED), ("take-gap", REASON_HARMONIC_WINDOW_OUT_OF_RANGE)]


def test_a_take_banked_before_its_reading_refuses_the_view_by_that_field(banked_takes, tmp_path, capsys):
    bundle, record = _copied(banked_takes[False], tmp_path)
    document = json.loads(record.read_text())
    del document["analysis"]["distortion"]
    record.write_text(json.dumps(document))

    assert main(["distortion", str(bundle), "--out", str(tmp_path / "harmonics.json")]) == EXIT_REFUSED
    answer = json.loads(capsys.readouterr().out)
    assert (answer["reason"], json.loads(answer["detail"])["field"]) == (TAKE_CURVES_NOT_BANKED, "analysis.distortion")


@pytest.mark.parametrize("provenance,drive", [
    ({}, {"status": "recorded_session_volume", "session_volume_readback": "unrecorded"}),
    ({"main_volume_db": -20.0}, {"status": "recorded_session_volume", "session_volume_readback": "matched"}),
    ({"main_volume_db": -14.0}, {"status": "unknown", "effective_peak_dbfs": None,
                                 "reason": "session_volume_readback_mismatch", "session_volume_readback": "mismatched"}),
    ({"session_volume_db": None}, {"status": "unknown", "effective_peak_dbfs": None, "reason": "session_volume_unrecorded"}),
])
def test_a_measure_takes_drive_rests_on_its_recorded_volume_checked_by_the_readback(banked_takes, tmp_path, provenance, drive):
    """The id does not prove a recorded session volume (#5012): the drive says it is
    recorded, and a fader readback that disagrees withholds it."""
    bundle, record = _copied(banked_takes[False], tmp_path)
    document = json.loads(record.read_text())
    document["provenance"].update(provenance)
    record.write_text(json.dumps(document))

    blocks = he.read_round_harmonics(bundle)["roles"]

    assert [block["drive"] for block in blocks] == [{**banked["drive"], **drive}
                                                    for banked in document["analysis"]["distortion"]["roles"]]


def test_instruments_read_a_fresh_bank_in_either_order(tmp_path, capsys, monkeypatch):
    """A view is a function of the takes (ADR-0346): it files beside the round,
    and the round's evidence and the fingerprint a prescription answers stay put."""
    session, record = bank_driver_take(tmp_path, monkeypatch)
    positions = next(session.rglob("positions"))
    feature, feature_positions = feature_bundle(tmp_path / "feature", _resonant_ir(3.0), phases=("lateral",),
                                                position_deg=15)
    shutil.copytree(feature / "impulses", session / "impulses", dirs_exist_ok=True)
    for take in feature_positions.glob("*.json"):
        shutil.copyfile(take, positions / take.name)
    write_bundle_manifest(session, selected={take.stem for take in feature_positions.glob("*.json")})
    mark_state(session, "closed")
    bank = bank_round(session, campaign_root=tmp_path / "bank")
    shutil.rmtree(session)
    inputs = round_inputs(bank.path)

    def evidence() -> tuple[dict[Path, bytes], dict[str, Any]]:
        files = {p.relative_to(inputs.session_dir): p.read_bytes()
                 for p in inputs.session_dir.rglob("*") if p.is_file()}
        return files, build_crossover_evidence_packet(inputs.session_dir, round_context=inputs)

    before, packet = evidence()
    for first in ("distortion", "classify-features"):
        out = {}
        for command in (first, "classify-features" if first == "distortion" else "distortion"):
            flags = ["--at", str(RESONANCE_HZ)] if command == "classify-features" else []
            assert main([command, str(bank.path), *flags]) == 0
            answer = json.loads(capsys.readouterr().out)
            assert answer["view"] == command
            out[command] = Path(answer["out"])
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
    assert harmonic["calibration"] == record["capture_calibration"]
    assert {row["role"] for row in harmonic["roles"]} == {"woofer", "tweeter"}
    assert feature_result["measurement"]["n_captures"] == 1
