# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Selecting banked takes: what a rescan reads off the files, and what it skips.

``test_a_banked_take_is_findable_by`` is the pin that carries the suite: it
fails if a take the store banked stops being selectable. There is no index file
to reconcile any more (ADR-0198), so what used to be the rebuild-agrees-with-
the-files claim is now structural — the files are the only thing read.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, NamedTuple

import numpy as np
import pytest

from jasper.active_speaker.commissioning_evidence_store import (
    CommissioningEvidenceStore,
)
from jasper.active_speaker.crossover_v2.candidate_ladder import candidate_ladder
from jasper.audio_measurement.evidence_reasons import EvidenceUnavailable
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_CANDIDATE,
    POSITION_EVIDENCE_KIND,
    ROUND_RECEIPT_KIND,
)
from jasper.active_speaker.crossover_v2.feature_classifier import load_round_pose_curves
from jasper.active_speaker.crossover_v2.journey import (
    PHASE_CHECK,
    PHASE_TIMING,
    PHASE_LATERAL,
    PHASE_MEASURE,
)
from jasper.active_speaker.crossover_v2.position_cycle import select_pose_curve_pair
from jasper.active_speaker.crossover_v2.record_index import bundle_measurements
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.active_speaker.crossover_v2.room_views import room_ceiling
from jasper.active_speaker.crossover_v2.round_inputs import round_inputs
from jasper.active_speaker.measurement_programs import PURPOSE_REAR, PURPOSE_ROOM, PURPOSE_SPEAKER
from jasper.audio_measurement.admission.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand, build_measure_program
from jasper.audio_measurement.room_boundary import CEILING_SOURCE_ROUND_GATE
from tests.crossover_v2_banked_round import (
    _DESIGN_AXIS_GEOMETRY,
    LateralPose,
    TakeClaim,
    bank_executor_take,
    lateral_pose_record,
)
from tests.crossover_v2_fixtures import _measure_analysis
from tests.run_manifest_fixture import write_bundle_manifest
from tests.test_crossover_v2_record_store import (
    CAPTURE,
    _bundle,
    _take,
)

ARTIFACTS = "evidence/v1/artifacts"


@pytest.fixture
def store(tmp_path):
    """The production store over a real bundle — what the rescan reads."""
    info = _bundle(tmp_path)
    return BankedRecordStore(
        evidence=CommissioningEvidenceStore.open(
            info["bundle_dir"], expected_session_id=info["session_id"],
        ),
        capture_session_id=CAPTURE,
    )


def _found(store: BankedRecordStore, **filters: Any):
    return bundle_measurements(store.evidence.bundle_dir, **filters)


def _artifacts(store: BankedRecordStore) -> Path:
    return Path(store.evidence.bundle_dir) / ARTIFACTS


def _builder_take(**overrides: Any) -> dict[str, Any]:
    """A take as the four take-record builders bank one: with a clock on it."""
    return {**_take(), "captured_at": "2026-08-28T11:22:33Z", **overrides}


@pytest.mark.parametrize(
    "field,value",
    [("kind", MEASURE_KIND_CANDIDATE), ("position_deg", 30)],
)
async def test_a_banked_take_is_findable_by(store, field, value):
    """The point of the reader: a take found without globbing a directory.

    Both axes the reader ships — the two ``position_cycle`` was asked for.
    """
    record_id = await store.bank(_builder_take(
        kind=MEASURE_KIND_CANDIDATE, position_deg=30, candidate_id="cand-7",
    ))

    found = _found(store, **{field: value})

    assert [row.path for row in found] == [record_id]


async def test_the_candidate_axis_separates_two_variants_of_one_pose(store):
    """The candidate axis: two takes, one pose, one label apart.

    A one-spot compare (``jasper-round run --poses 0 --candidates``) banks takes
    like the two below, which differ in nothing a reader can otherwise select
    on, so a filter that ignored the label would return both and the comparison
    could not be set up at all.
    """
    wanted = await store.bank(_builder_take(
        candidate_id="null_a1", take_id="candidate_00_a00",
    ))
    await store.bank(_builder_take(
        candidate_id="null_a2", take_id="candidate_01_a00",
    ))

    found = _found(store, candidate_id="null_a1")

    assert [row.path for row in found] == [wanted]
    assert [row.candidate_id for row in found] == ["null_a1"]


async def test_a_banked_walk_pose_is_selectable_by_the_candidate_it_measured(
    store,
):
    """The cycle's label reaches the reader through the WALK's own builder.

    Two poses at one bearing, one candidate apart: a per-pose cycle is only
    worth banking if a reader can afterwards ask for one variant's takes, and
    the pose record is where that label has to survive.
    """
    def _pose_record(index: int, candidate_id: str) -> dict[str, Any]:
        pose = LateralPose(
            pose_id=f"lateral_{index:02d}", index=index, attempt=1,
            prompt="+0 deg", role="onax", offset_cm=0.0, at_mark=True,
            curves=(),
        )
        return lateral_pose_record(
            pose, geometry=_DESIGN_AXIS_GEOMETRY, lateral_consumer="forward_model",
            run_id="sess-1", graph_fingerprint="fp-applied",
            captured_at="2026-08-28T11:22:33Z", wav_sha256=f"sha-{index}",
            claim=TakeClaim(candidate_id=candidate_id),
        )

    wanted = await store.bank(_pose_record(1, "fp-a"))
    await store.bank(_pose_record(2, "fp-b"))

    found = _found(store, phase=PHASE_LATERAL, candidate_id="fp-a")

    assert [row.path for row in found] == [wanted]
    assert [row.candidate_id for row in found] == ["fp-a"]


@pytest.mark.parametrize("phase", [PHASE_LATERAL, PHASE_TIMING])
async def test_the_phase_axis_selects_the_takes_that_ARE_that_phase(store, phase):
    """What a take IS, beside what it MEASURES — two columns, two questions.

    Two takes differing ONLY in phase, so a filter that narrowed by kind
    instead would return both: the axis has to select one and exclude the
    other, which is what the packet's lateral and entry-baseline blocks read
    it for.
    """
    wanted = await store.bank(_builder_take(phase=phase, take_id="pose_00_a01"))
    await store.bank(_builder_take(phase=PHASE_CHECK, take_id="pose_00_a02"))

    found = _found(store, phase=phase)

    assert [row.path for row in found] == [wanted]
    assert [row.phase for row in found] == [phase]


async def test_a_read_writes_nothing_into_the_bundle(store):
    """The reader has no side effect — no table, no file, nothing.

    ``jasper-crossover-prescriber status`` is pinned to leave a banked corpus
    byte-identical, which a reader that wrote an index could not promise.
    """
    await store.bank(_builder_take())
    before = {
        p: p.stat().st_mtime_ns
        for p in Path(store.evidence.bundle_dir).rglob("*")
        if p.is_file()
    }

    assert _found(store)

    after = {
        p: p.stat().st_mtime_ns
        for p in Path(store.evidence.bundle_dir).rglob("*")
        if p.is_file()
    }
    assert after == before
    assert list(Path(store.evidence.bundle_dir).rglob("*.sqlite3*")) == []


@pytest.mark.parametrize(
    "captured_at,expected",
    [
        # The lateral pose, the entry baseline and the phase capture.
        ("2026-08-28T11:22:33Z", "2026-08-28T11:22:33Z"),
        # The cloud position, which emits a Unix epoch float for that same
        # instant instead — the disagreement is the type, not the moment.
        (1787916153.0, "2026-08-28T11:22:33Z"),
        # The engine's own ``_record()``, which banks no clock at all.
        (None, None),
    ],
)
async def test_the_read_clock_is_the_records_own(store, captured_at, expected):
    """Two builder types normalize to one spelling; an absent one stays absent.

    The types genuinely disagree upstream, and normalizing at the read rather
    than at the builders is what keeps the store from rewriting records it
    already wrote once.
    """
    take = _take() if captured_at is None else _builder_take(
        captured_at=captured_at,
    )

    await store.bank(take)

    assert [row.captured_at for row in _found(store)] == [expected]


async def test_an_artifact_that_is_not_a_measurement_is_not_selected(store):
    """Five of the six banked kinds are not takes, and none of them get a row."""
    await store.bank({
        "kind": ROUND_RECEIPT_KIND, "session_id": "engine-session",
        "phase": "verify",
    })

    assert _found(store) == ()


async def test_a_rescanned_nan_clock_reads_no_timestamp(store):
    """``json.loads`` accepts a bare ``NaN``, so the rescan is where it lands.

    The banked file is edited under the store rather than written through it,
    because the store is what makes this shape unbankable in the first place.
    """
    record_id = await store.bank(_builder_take())
    banked = _artifacts(store) / record_id
    document = json.loads(banked.read_text())
    document["captured_at"] = float("nan")
    banked.write_text(json.dumps(document))

    assert [row.captured_at for row in _found(store)] == [None]


async def test_a_file_the_rescan_cannot_parse_costs_only_itself(store):
    """One unreadable take is not a reason to refuse the rest of the corpus."""
    good = await store.bank(_builder_take(take_id="pose_00_a01"))
    broken = await store.bank(_builder_take(take_id="pose_00_a02"))
    (_artifacts(store) / broken).write_text("{not json at all")

    assert [row.path for row in _found(store)] == [good]


# --------------------------------------------------------------------------- #
# the record scanners: a take the round kept, of their phase and purpose
# --------------------------------------------------------------------------- #


class _Scanner(NamedTuple):
    """What a scanner answers for a round, the phase and purpose it reads, a
    phase and a purpose it passes over (``None``: it reads every purpose),
    and its answer when it reads the first two takes, then all three."""

    answer: Callable[[Path], Any]
    phase: str
    other_phase: str
    other_purpose: str | None
    two: Any
    three: Any


def _session(round_dir: Path) -> Path:
    return round_dir / "bundle" / "sess"


_SCANNERS = {
    "room_ceiling": _Scanner(
        lambda root: room_ceiling(_session(root)).trusted_floor_hz,
        PHASE_MEASURE, PHASE_TIMING, PURPOSE_ROOM, 300.0, 450.0),
    "delay_pair": _Scanner(
        lambda root: Path(select_pose_curve_pair(
            _session(root), phases=(PHASE_MEASURE, PHASE_LATERAL), position_deg=None,
            roles=("woofer", "tweeter")).take.path).stem,
        PHASE_MEASURE, PHASE_TIMING, PURPOSE_REAR, "take_0002", "take_0003"),
    "pose_bank": _Scanner(
        lambda root: sorted({curve.pose_id for curve in load_round_pose_curves(_session(root))}),
        PHASE_LATERAL, PHASE_TIMING, PURPOSE_ROOM, ["take_0001", "take_0002"],
        ["take_0001", "take_0002", "take_0003"]),
    "candidate_ladder": _Scanner(
        lambda root: candidate_ladder(root, round_inputs(root))["summary"]["candidates"],
        PHASE_LATERAL, PHASE_MEASURE, None, ["cfg-a", "cfg-b"], ["cfg-a", "cfg-b", "cfg-c"]),
}


@pytest.mark.parametrize("scanner,intruder", [
    (name, intruder) for name, scanner in _SCANNERS.items()
    for intruder in ("kept", "phase", "purpose", "refused", "unselected")
    if intruder != "purpose" or scanner.other_purpose is not None
])
def test_a_scanner_reads_only_the_takes_the_round_kept(tmp_path, scanner, intruder):
    """A third take at the same pose changes each scanner's answer when the
    round kept it, as a higher trusted floor, a newer driver pair, a third
    pose or a third candidate. The scanner passes it over when it is of
    another phase, of another purpose, refused, or not selected by the run
    manifest, and still reads the two takes it should."""
    spec = _SCANNERS[scanner]
    positions = _session(tmp_path) / ARTIFACTS / "crossover_v2" / "cap" / "positions"
    positions.mkdir(parents=True)
    freqs = np.geomspace(200.0, 12000.0, 24).tolist()
    for index, (candidate, floor_hz) in enumerate((("cfg-a", 300.0), ("cfg-b", 300.0), ("cfg-c", 450.0)), 1):
        intruding = index == 3
        (positions / f"take_{index:04d}.json").write_text(json.dumps({
            "kind": POSITION_EVIDENCE_KIND, "take_id": f"take_{index:04d}",
            "phase": spec.other_phase if intruding and intruder == "phase" else spec.phase,
            "measurement_purpose": spec.other_purpose if intruding and intruder == "purpose" else PURPOSE_SPEAKER,
            "position_deg": 0, "vertical_deg": 0, "pose_kind": "bearing", "candidate_id": candidate, "gating_applied": True,
            "curves": [{"role": role, "band_hz": [200.0, 12000.0], "freqs_hz": freqs,
                        "magnitude_db": [0.0] * len(freqs), "phase_deg": [0.0] * len(freqs),
                        "window": "gated", "gate_window_ms": 5.0, "trusted_floor_hz": floor_hz}
                       for role in ("woofer", "tweeter")],
        }))
    write_bundle_manifest(
        _session(tmp_path), refused={"take_0003"} if intruder == "refused" else (),
        selected={"take_0001", "take_0002"} if intruder == "unselected" else None,
    )

    assert spec.answer(tmp_path) == (spec.three if intruder == "kept" else spec.two)


def test_the_scanners_read_the_speaker_takes_the_host_banks(tmp_path, monkeypatch):
    """Every take banks its curves (ADR-0383), so the scanners read speaker
    takes: a gated MEASURE take gives the room ceiling its round gate, the
    delay pair both drivers and the pose bank its pose, and a lateral
    candidate take names its candidate on the ladder."""
    def bundle_of(root: Path) -> Path:
        bundle, = {path.parent for path in (root / "sessions").glob("*/info.json")}
        return bundle

    program = build_measure_program({"woofer": -20.0, "tweeter": -24.0}, [
        RoleBand("woofer", 0, FrequencyBand(150, 4000)), RoleBand("tweeter", 1, FrequencyBand(1600, 20000))])
    gated = tuple(replace(response, gating={**response.gating, "f_trusted_hz": 450.0})
                  for response in _measure_analysis(program).driver_responses)
    measure = bank_executor_take(tmp_path / "measure", monkeypatch, program=program,
                                 analysis_fields={"driver_responses": gated})
    bundle = bundle_of(tmp_path / "measure")
    assert {curve["window"] for curve in measure["curves"]} == {"gated"}
    ceiling = room_ceiling(bundle)
    assert (ceiling.source, ceiling.trusted_floor_hz, ceiling.source_take_id) == (
        CEILING_SOURCE_ROUND_GATE, 450.0, measure["take_id"])
    pair = select_pose_curve_pair(bundle, phases=(PHASE_MEASURE, PHASE_LATERAL), position_deg=None,
                                  roles=("woofer", "tweeter"))
    assert pair is not None and pair.document["take_id"] == measure["take_id"]
    assert sorted(curve.role for curve in load_round_pose_curves(bundle)) == ["tweeter", "woofer"]

    lateral = bank_executor_take(tmp_path / "lateral", monkeypatch, raw_record={"phase": PHASE_LATERAL})
    bundle = bundle_of(tmp_path / "lateral")
    with pytest.raises(EvidenceUnavailable) as refused:
        candidate_ladder(bundle, round_inputs(bundle))
    assert refused.value.detail["candidates_named"] == [lateral["candidate_id"]]
