# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The projector, checked by the two readers it exists to feed.

The bar throughout is that the REAL loaders open the projection — not a copy of
their rule restated here. ``load_round_captures`` and ``_bind_measure_captures``
are imported and called, so a projection that satisfies this file is one those
two consume, and a drift in either of them fails here rather than in a silent
skip on a real round.

The fixture banks what a wired round banks: per-take records under
``evidence/v1/artifacts/crossover_v2/<capture>/positions/`` beside their capture
WAVs in ``summed/``, with BOTH ``captured_at`` shapes the builders emit — a
phase capture's ``%Y-%m-%dT%H:%M:%SZ`` and a cloud position's float epoch.
"""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2 import ring_projection as rp
from jasper.active_speaker.crossover_v2.contracts import (
    MEASURE_KIND_KEY,
    POSITION_EVIDENCE_KIND,
)
from jasper.active_speaker.crossover_v2.evidence_packet import (
    round_artifact_dir,
    round_program_dir,
)
from jasper.active_speaker.crossover_v2.feature_classifier import (
    ADMISSIBLE_PHASES,
    load_round_captures,
)
from jasper.active_speaker.crossover_v2.harmonic_evidence import (
    _bind_measure_captures,
    _scope_captures,
)
from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    read_session_identity,
)
from jasper.cli import round_views as cli

SR = 48000
BUNDLE_ID = "d0eca8f5a24d"
CAPTURE_ID = "wired-DBq7UPOcyyuJwCfWwQVOwA"

#: The fixture's takes: ``(take_id, phase, captured_at)``. The verify and the
#: measure carry ISO stamps where the cloud positions carry float epochs, which
#: is the split the builders actually produce — and the two cloud positions in
#: the same SECOND are the tie the stem has to stay unique across.
_TAKES = (
    ("verify_01_a01", "verify", "2026-08-31T00:19:52Z"),
    ("cloud_verify_02_a02", "cloud_verify", 1788135641.4366589),
    ("cloud_verify_03_a03", "cloud_verify", 1788135641.9012345),
    ("measure_04_a04", "measure", "2026-08-31T00:21:06Z"),
)

#: What a reader must still find on the projected sidecar. Not a full record —
#: the point is that the blocks the readers subscript survive the trip whole.
_CARRIED = ("diagnostic", "capture_integrity", "frame_ledger", "wav_sha256")


def _write_wav(path: Path, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    samples = rng.normal(0, 0.2, SR // 10)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def _bundle(
    root: Path,
    *,
    takes: tuple[tuple[str, str, object], ...] = _TAKES,
    session_id: str = BUNDLE_ID,
    capture_id: str = CAPTURE_ID,
    bank_wavs: bool = True,
) -> Path:
    """One banked round, in the shape a wired round actually banks."""
    bundle = root / "bundle" / session_id
    positions = bundle / f"evidence/v1/artifacts/crossover_v2/{capture_id}/positions"
    positions.mkdir(parents=True, exist_ok=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": session_id}))

    programs = bundle / "crossover_v2" / capture_id
    # One distinct program per phase, and its digest banked on the take the way
    # ``CaptureProvenance`` banks it: the feature reader binds a capture to the
    # program whose BYTES it heard, never to its phase label (#3504).
    program_sha: dict[str, str] = {}
    for index, phase in enumerate(sorted({phase for _, phase, _ in takes}, key=str)):
        path = programs / f"{phase}_program.wav"
        _write_wav(path, seed=index + 1)
        program_sha[phase] = hashlib.sha256(path.read_bytes()).hexdigest()

    for index, (take_id, phase, captured_at) in enumerate(takes):
        wav_rel = f"summed/summed_{take_id}_{index:032x}.wav"
        if bank_wavs:
            _write_wav(bundle / wav_rel, seed=index + 2)
        (positions / f"{take_id}.json").write_text(
            json.dumps({
                "kind": POSITION_EVIDENCE_KIND,
                MEASURE_KIND_KEY: "verify",
                "take_id": take_id,
                "phase": phase,
                "captured_at": captured_at,
                "session_id": capture_id,
                "capture_session_id": capture_id,
                "wav_path": wav_rel,
                "wav_sha256": f"{index:064x}",
                "diagnostic": {"epsilon_ppm": 1.0 + index, "linearity_ok": True},
                "capture_integrity": {"capture_chain": "alsa_s32le"},
                "frame_ledger": {"received_frames": 4800},
                "provenance": {
                    "stimulus": {"wav_sha256": program_sha[phase]},
                },
            })
        )
    return bundle


@pytest.fixture
def projection(tmp_path) -> tuple[Path, Path, rp.RingProjection]:
    bundle = _bundle(tmp_path)
    dumps = tmp_path / "ring"
    return bundle, dumps, rp.project_ring(bundle, dumps)


# --------------------------------------------------------------------------- #
# the two readers, on the real projection
# --------------------------------------------------------------------------- #


def test_the_feature_reader_binds_every_admissible_take(projection):
    bundle, dumps, result = projection
    round_dir, _ = round_artifact_dir(bundle)
    assert round_dir is not None
    programs = round_program_dir(bundle, round_dir, ADMISSIBLE_PHASES)

    captures = load_round_captures(programs, dumps, session_id=result.session_id)

    assert [capture.phase for capture in captures] == [
        "verify", "cloud_verify", "cloud_verify",
    ]
    assert all(capture.wav.is_file() for capture in captures)
    assert all(capture.program.is_file() for capture in captures)
    # Sorted by the stamp the stem carried, which is the order they were taken.
    assert list(captures) == sorted(captures, key=lambda capture: capture.stamp)


def test_the_distortion_reader_binds_and_scopes_the_measure_take(projection):
    bundle, dumps, result = projection

    bound = _bind_measure_captures(dumps)
    assert [capture["session_id"] for capture in bound] == [BUNDLE_ID]

    scoped, scope = _scope_captures(bound, result.session_id)
    assert len(scoped) == 1
    assert scope["session_id"] == BUNDLE_ID
    assert scoped[0]["wav"].is_file()


def test_a_projected_sidecar_carries_the_readers_blocks_whole(projection):
    _, _, result = projection
    document = json.loads(result.projected[0].sidecar.read_text())
    assert all(field in document for field in _CARRIED)


# --------------------------------------------------------------------------- #
# the four things the bank spells differently
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("take_id", "captured_at", "expected_us"),
    [
        ("verify_01_a01", "2026-08-31T00:19:52Z", 1788135592_000000),
        ("cloud_verify_02_a02", 1788135641.4366589, 1788135641_000000),
    ],
)
def test_the_stem_round_trips_the_capture_time(
    projection, take_id, captured_at, expected_us
):
    """Both banked shapes reach the stem as the SAME microsecond epoch.

    Second resolution because :func:`~.record_index.bundle_measurements`
    reconciles the float epoch onto the ISO shape the phase captures carry
    natively; a projection that invented sub-second digits for half the ring
    would be publishing precision one half never had.
    """
    _, _, result = projection
    take = next(row for row in result.projected if row.take_id == take_id)

    leading, _, rest = take.stem.partition("_")
    assert int(leading) == expected_us
    assert rest == take_id
    # The reader's own parse, not a restatement of it.
    assert float(take.sidecar.stem.split("_")[0]) / 1e6 == pytest.approx(
        expected_us / 1e6
    )


def test_two_takes_in_one_second_keep_distinct_stems(projection):
    """The take id is the tiebreak, and it is what makes the order stable.

    Second resolution means a tie is reachable, and the readers sort by the
    stamp — a stable sort over a glob already ordered by name, so equal stamps
    fall back to take-id order in both readers rather than to whatever the
    filesystem returned.
    """
    _, _, result = projection
    ties = [row for row in result.projected if row.stem.startswith("1788135641")]
    assert len(ties) == 2
    assert len({row.stem for row in ties}) == 2
    assert [row.take_id for row in ties] == sorted(row.take_id for row in ties)


def test_the_identity_is_the_bundle_id_with_the_capture_id_alongside(projection):
    """The bundle id scopes; the capture id rides as an alias.

    Both readers scope on ``jts_session_identity.session_id``, and the take
    record's own ``session_id`` is the CAPTURE namespace — stamping that one
    would scope every capture out. The alias is the join between the two
    namespaces that no banked artifact carried before.
    """
    _, _, result = projection
    for take in result.projected:
        identity = read_session_identity(json.loads(take.sidecar.read_text()))
        assert identity is not None
        assert identity.session_id == BUNDLE_ID
        assert identity.aliases[ALIAS_CAPTURE_SESSION_ID] == CAPTURE_ID


def test_the_setup_calibration_id_is_stamped_only_when_supplied(tmp_path):
    bundle = _bundle(tmp_path)
    bare = rp.project_ring(bundle, tmp_path / "bare")
    named = rp.project_ring(
        bundle, tmp_path / "named", setup_calibration_id="umik2-810-8494"
    )

    assert "setup_calibration_id" not in json.loads(
        bare.projected[0].sidecar.read_text()
    )
    assert json.loads(named.projected[0].sidecar.read_text())[
        "setup_calibration_id"
    ] == "umik2-810-8494"


# --------------------------------------------------------------------------- #
# how the bytes get there
# --------------------------------------------------------------------------- #


def test_the_capture_wav_is_hardlinked_by_default(projection):
    bundle, _, result = projection
    take = result.projected[0]
    source = bundle / json.loads(take.sidecar.read_text())["wav_path"]

    assert take.linked
    assert take.wav.stat().st_ino == source.stat().st_ino


def test_copy_places_the_same_bytes_at_a_new_inode(tmp_path):
    bundle = _bundle(tmp_path)
    result = rp.project_ring(bundle, tmp_path / "ring", copy=True)
    take = result.projected[0]
    source = bundle / json.loads(take.sidecar.read_text())["wav_path"]

    assert not take.linked
    assert take.wav.stat().st_ino != source.stat().st_ino
    assert take.wav.read_bytes() == source.read_bytes()


def test_projecting_twice_over_the_same_ring_replaces_the_wav(tmp_path):
    """A re-run must not fail on the link it made last time."""
    bundle = _bundle(tmp_path)
    first = rp.project_ring(bundle, tmp_path / "ring")
    second = rp.project_ring(bundle, tmp_path / "ring")

    assert [row.stem for row in second.projected] == [
        row.stem for row in first.projected
    ]
    assert all(row.wav.is_file() for row in second.projected)


# --------------------------------------------------------------------------- #
# what it will not project, by name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("take", "reason"),
    [
        (("no_phase_01", None, "2026-08-31T00:19:52Z"), rp.SKIP_NO_PHASE),
        (("no_time_02", "verify", None), rp.SKIP_NO_CAPTURED_AT),
    ],
)
def test_an_unprojectable_take_is_reported_by_name(tmp_path, take, reason):
    bundle = _bundle(tmp_path, takes=_TAKES + (take,))
    result = rp.project_ring(bundle, tmp_path / "ring")

    assert [(row.reason) for row in result.skipped] == [reason]
    assert result.skipped[0].path.endswith(f"{take[0]}.json")
    assert len(result.projected) == len(_TAKES)


def test_a_wav_the_bank_does_not_hold_is_reported_not_guessed_at(tmp_path):
    bundle = _bundle(tmp_path, bank_wavs=False)
    with pytest.raises(rp.RingProjectionRefused) as refusal:
        rp.project_ring(bundle, tmp_path / "ring")

    assert refusal.value.reason == rp.NOTHING_TO_PROJECT
    assert {row["reason"] for row in refusal.value.detail["skipped"]} == {
        rp.SKIP_WAV_MISSING
    }


def test_a_wav_path_leaving_the_bundle_is_refused_not_followed(tmp_path):
    outside = tmp_path / "outside.wav"
    _write_wav(outside, seed=99)
    bundle = _bundle(tmp_path)
    record = next(
        (bundle / "evidence/v1/artifacts/crossover_v2" / CAPTURE_ID / "positions").glob(
            "*.json"
        )
    )
    document = json.loads(record.read_text())
    document["wav_path"] = "../../outside.wav"
    record.write_text(json.dumps(document))

    result = rp.project_ring(bundle, tmp_path / "ring")
    assert [row.reason for row in result.skipped] == [rp.SKIP_WAV_ESCAPES_BUNDLE]


def test_a_bundle_carrying_two_rounds_is_refused_before_anything_is_written(tmp_path):
    """Two rounds under one bundle id is the pooling the scope guard exists for.

    It could not refuse it downstream either: every sidecar would carry the
    very id it scoped by.
    """
    bundle = _bundle(tmp_path)
    (bundle / "evidence/v1/artifacts/crossover_v2/wired-SECOND").mkdir(parents=True)

    with pytest.raises(ValueError):
        rp.project_ring(bundle, tmp_path / "ring")
    assert not (tmp_path / "ring").exists()


def test_a_bundle_with_no_session_id_is_unreadable_not_refused(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "info.json").write_text(json.dumps({"kind": "crossover_v2"}))

    with pytest.raises(ValueError):
        rp.project_ring(bundle, tmp_path / "ring")


# --------------------------------------------------------------------------- #
# the view that projects it
# --------------------------------------------------------------------------- #


def test_no_dumps_projects_this_bundles_ring_before_it_classifies(tmp_path):
    """The step the retired ``jasper-project-ring`` was: done by its consumer.

    ``classify-features`` with no ``--dumps`` projects the bundle into the ring
    at :data:`~jasper.cli.round_views.classify_features._PROJECTED_RING` and
    reads THAT, so a round banked today -- which carries no ring of its own --
    needs no second command before it can be classified. The verdict over this
    fixture's noise captures is not the subject here; where the ring lands is.
    """
    bundle = _bundle(tmp_path)
    cli.main(["classify-features", str(bundle)])

    ring = bundle / "ring"
    assert len(list((ring / "sidecar").glob("*.json"))) == len(_TAKES)
    assert len(list((ring / "wav").glob("*.wav"))) == len(_TAKES)
