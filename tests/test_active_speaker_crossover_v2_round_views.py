# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round view artifacts, answers, and grades over retained evidence."""

from __future__ import annotations

import dataclasses
import json
import shlex
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.evidence_packet.offline_reads import CLASSIFICATION_ARTIFACT
from jasper.active_speaker.crossover_v2.position_cycle import POSITION_CYCLE_FILENAME

from jasper.active_speaker.crossover_v2 import round_inputs as round_inputs_mod
from jasper.active_speaker.crossover_v2.candidate_ladder import REFUSE_NO_LADDER
from jasper.active_speaker.crossover_v2.journey import PHASE_LATERAL
from jasper.active_speaker.crossover_v2.round_views import (
    ENTRY_STATE_UNREADABLE,
    RoundViewsError,
    entry_state_grade,
    load_banked_round,
)
from jasper.active_speaker.crossover_v2.gate_sweep import REFUSE_SINGLE_POSE
from jasper.active_speaker.crossover_v2.round_captures import REFUSE_CAPTURE_UNREADABLE, REFUSE_NO_CAPTURES
from jasper.active_speaker import flat_spec
from jasper.active_speaker.frequency_view import FREQUENCY_VIEW_FILENAME
from jasper.active_speaker.repeat_floor import derive_repeat_floor
from jasper.active_speaker.flat_spec import evaluate_flat_spec

from tests.crossover_v2_banked_round import bank_measure_round
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_manifest
# The gate sweep's own pose IRs, reused rather than copied, so a deconvolved
# round's answer is as knowable here as it is there.
from tests.test_crossover_v2_gate_sweep import _pose_ir

#: A live session bundle resolves its three non-bundle inputs to the on-speaker
#: SSOT paths; no test may read whatever sits at those absolute paths on the
#: box running pytest.
pytestmark = pytest.mark.usefixtures("no_real_pi_paths")

#: A log-spaced curve grid spanning all three SPEC_BANDS rows
#: (250-2000 / 2000-8000 / 8000-16000 Hz) with plenty of bins in each.
GRID = np.geomspace(280.0, 16000.0, 90)
REFERENCE_DB = -20.0


def _flat_curve(*, offset_db: float = 0.0, ripple_db: float = 0.0) -> np.ndarray:
    """A curve at ``REFERENCE_DB + offset_db``, with optional deterministic
    ripple (a single +ripple_db bump at bin 10, -ripple_db at bin 40) so a
    test can tell a perfectly-flat golden case apart from a rippled one."""
    curve = np.full(GRID.shape, REFERENCE_DB + offset_db, dtype=float)
    if ripple_db:
        curve[10] += ripple_db
        curve[40] -= ripple_db
    return curve


def _make_round_dir(
    tmp_path: Path,
    name: str,
    *,
    position_curves: dict[str, tuple[str, np.ndarray]],
    position_degrees: dict[str, float] | None = None,
) -> Path:
    """One banked round directory, in the tree ``bank-crossover-round.sh``
    produces: ``<round-dir>/bundle/<session>/evidence/v1/artifacts/crossover_v2/<capture>/``.

    ``position_curves`` maps ``position_id -> (role, magnitude_db)``; the
    combined curve is their power mean, graded by the real evaluator.
    """
    round_dir = tmp_path / name
    session_dir = round_dir / "bundle" / "sess1"
    capture_dir = session_dir / "evidence/v1/artifacts/crossover_v2" / "cap1"
    capture_dir.mkdir(parents=True)

    (session_dir / "info.json").write_text(json.dumps({
        "kind": "jts_active_speaker_commissioning_bundle",
        "session_id": "sess1", "state": "closed", "started_at": 1.0,
        "placement": {"policy_id": "driver_same_distance_v1", "acknowledged": True},
        "fingerprints": {
            "topology_id": "default", "topology_fingerprint": "abc123",
            "output_assignments": [],
            "graph_fingerprint": None,
            "mic": {"calibration_id": "", "calibration_sha256": None},
            "build_sha": "deadbeef",
        },
    }))
    (capture_dir / "round_receipt.json").write_text(json.dumps({
        "kind": "jts_crossover_v2_round_receipt", "schema_version": 2, "round_id": "r1",
    }))
    stack = np.vstack([curve for _role, curve in position_curves.values()])
    combined_db = 10.0 * np.log10(np.mean(10.0 ** (stack / 10.0), axis=0))
    # ``position_deg`` is present only for the seats the caller named, exactly
    # as the real writer behaves: the packet's row filter drops a key whose
    # value is None, so a seat with no commanded bearing — and every seat of a
    # round banked before the 2026-08-24 geometry writer — carries no key at
    # all rather than a null.
    degrees = position_degrees or {}
    positions = [
        {
            "position_id": position_id, "index": index, "attempt": 1,
            "role": role, "take_id": "", "magnitude_db": curve.tolist(),
            **({"position_deg": degrees[position_id]} if position_id in degrees else {}),
        }
        for index, (position_id, (role, curve)) in enumerate(position_curves.items(), start=2)
    ]
    cloud = {
        "kind": "jts_crossover_v2_cloud_evidence", "schema_version": 1,
        "trusted_floor_hz": None, "validity_floor_hz": None,
        "curve": {"freqs_hz": GRID.tolist(), "magnitude_db": combined_db.tolist()},
        "flatness": {"evaluable": True, "n_bins": len(GRID), "n_excluded": 0, "rms_db": 0.0},
        "spec": evaluate_flat_spec(
            GRID, combined_db, np.zeros(GRID.shape, dtype=bool), smoothing_fraction=12,
        ).to_dict(),
        "merged_excluded_bands_hz": [], "screen_excluded_bands_hz": [],
        "null_registry": {"classification": "insufficient_evidence", "nulls": []},
        "null_registry_crossover_region": {"classification": "insufficient_evidence"},
        "carve_outs": [], "geometry": {"reason": "thin_evidence", "n_positions": len(positions)},
        "positions": {
            "available": True, "schema": "jts_attribution_position_evidence/1",
            "curve_grid": {
                "freqs_hz": GRID.tolist(), "fractional_octave": 12,
                "smoothing_fraction": 12, "floor_hz": None, "floor_source": None,
            },
            "positions": positions,
        },
    }
    (capture_dir / "cloud_verify.json").write_text(json.dumps(cloud))
    (capture_dir / "findings_cloud_verify.json").write_text(json.dumps({
        "findings": [], "field_descriptions": {},
    }))
    write_manifest(round_dir)
    return round_dir


# --------------------------------------------------------------------------- #
# load_banked_round
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("live", [False, True])
def test_a_round_loads_from_its_banked_tree_or_from_the_live_bundle(
    tmp_path, live
):
    """The SAME round, read the two ways it can be pointed at (#3498, #2882).

    A banked tree is the live bundle one level deeper plus three frozen
    siblings, so both readings must produce the same views — and the one thing
    that cannot be re-derived from the paths, which of the two shapes was
    found, is disclosed rather than inferred.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_04": ("offax", _flat_curve()),
        },
    )
    session_dir = round_dir / "bundle" / "sess1"

    loaded = load_banked_round(session_dir if live else round_dir)

    assert loaded.inputs.banked is not live
    assert loaded.inputs.session_dir == session_dir
    assert loaded.session_dir == session_dir
    assert loaded.report is not None and loaded.report.bands  # a real, rehydrated report


def test_a_live_bundle_takes_the_flow_state_only_when_it_names_that_session(
    tmp_path, monkeypatch
):
    """One flow state on the speaker, a dozen retained session directories.

    Every live bundle resolves to the SAME state file, so an older retained
    session handed the current one would be graded on another round's verify
    curve, verdicts and ordinal — wrong numbers rather than missing ones. The
    two ids are compared in the namespace they share: the state's own
    ``session_id`` against the capture directory the bundle filed its round
    artifacts under.
    """
    curves = {"cloud_verify_02": ("onax", _flat_curve())}
    mine = _make_round_dir(tmp_path, "r1", position_curves=curves) / "bundle" / "sess1"
    other = _make_round_dir(tmp_path, "r2", position_curves=curves) / "bundle" / "sess1"
    capture = other / "evidence/v1/artifacts/crossover_v2"
    (capture / "cap1").rename(capture / "cap2")
    state = tmp_path / "flow-state.json"
    state.write_text(json.dumps({"session_id": "cap2"}))
    monkeypatch.setattr(round_inputs_mod, "STATE_DEFAULT_PATH", state)

    assert round_inputs_mod.round_inputs(other).state_path == state
    stale = round_inputs_mod.round_inputs(mine)
    assert stale.state_path is None
    assert stale.state_reason == round_inputs_mod.STATE_SESSION_UNKNOWN


def test_a_directory_of_neither_shape_refuses(tmp_path):
    """No ``bundle/`` and no ``info.json`` is neither round shape, and the
    refusal keeps this module's own type so the CLI's load stage catches it."""
    neither = tmp_path / "neither"
    neither.mkdir()
    with pytest.raises(RoundViewsError):
        load_banked_round(neither)


def test_load_banked_round_refuses_multiple_bundle_sessions(tmp_path):
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    (round_dir / "bundle" / "second_session").mkdir()
    with pytest.raises(RoundViewsError, match="expected exactly one"):
        load_banked_round(round_dir)


def test_load_banked_round_reads_a_repeat_floor_banked_beside_it(tmp_path):
    """The side file reaches the packet exactly as applied-profile.json does:
    present, the accuracy budget's repeat-floor component is available."""
    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    component = "in_capture_repeat_floor"
    absent = load_banked_round(round_dir)
    assert absent.packet["accuracy_budget"]["components"][component]["available"] is False

    # The record the REAL deriver banks from two repeats, never a hand-typed one.
    floor = derive_repeat_floor(samples={"residual_db": [0.0, 0.2]}, rounds=[{}, {}])
    (round_dir / "repeat-floor.json").write_text(json.dumps({**floor, "aggregate_metric": "residual_db"}))
    present = load_banked_round(round_dir)
    assert present.packet["accuracy_budget"]["components"][component]["available"] is True


# --------------------------------------------------------------------------- #
# CLI wiring — jasper-round-views
# --------------------------------------------------------------------------- #


def test_cli_inventory_names_what_is_missing_and_what_produces_it(tmp_path):
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    assert cli.main(["entry", str(round_dir)]) == 0

    assert cli.main(["inventory", str(round_dir)]) == 0
    payload = json.loads((round_dir / "inventory.json").read_text())
    rows = {row["artifact"]: row for row in payload["artifacts"]}

    present = rows["entry_state_grade.json"]
    assert present["present"] is True
    assert present["bytes"] == (round_dir / "entry_state_grade.json").stat().st_size
    assert payload["bytes_total"] == sum(
        row["bytes"] or 0 for row in payload["artifacts"]
    )

    # Every path this round can fill is filled: the row is a line to run.
    missing = rows[FREQUENCY_VIEW_FILENAME]
    assert missing["present"] is False
    assert missing["bytes"] is None
    assert missing["produced_by"] == f"jasper-round-views frequency {round_dir}"
    assert missing["producer_needs_more_than_this_round"] is False
    assert missing["path"] == str(round_dir / FREQUENCY_VIEW_FILENAME)
    # The producer it named writes the artifact it named as missing.
    assert cli.main(shlex.split(missing["produced_by"])[1:]) == 0
    assert Path(missing["path"]).is_file()

    # A view whose subcommand takes MORE than this round says so, and places
    # this round in the slot that writes the artifact beside it. What is left
    # in brackets is what no inventory of one round can fill, and running it
    # without that round argparse rejects.
    multi = rows["close_reference.json"]
    assert shlex.split(multi["produced_by"]) == [
        "jasper-round-views", "close-reference", "--far-round", str(round_dir),
        "--close-round", "<other-round>", "--close-m", "<distance-m>",
    ]
    assert multi["producer_needs_more_than_this_round"] is True
    with pytest.raises(SystemExit):
        cli.main(["close-reference", "--far-round", str(round_dir)])

    assert rows[CLASSIFICATION_ARTIFACT]["produced_by"] == (
        f"jasper-round-views classify-features {round_dir}"
    )
    assert rows[CLASSIFICATION_ARTIFACT][
        "producer_needs_more_than_this_round"
    ] is False

    assert "forward_model.json" not in rows

    # One row no view here writes: the banker's own pose index, named with the
    # command that makes it rather than with this tool's prog.
    assert rows[POSITION_CYCLE_FILENAME]["produced_by"] == (
        "jasper-round wait --run '<run-id>'"
    )

    # A view the evidence packet reads is read back where THAT reader looks —
    # inside the round's own artifact directory, never beside the round.
    assert rows["harmonic_distortion.json"]["path"] == str(
        round_dir / "bundle/sess1/evidence/v1/artifacts/crossover_v2/cap1"
        / "harmonic_distortion.json"
    )


def test_cli_frequency_writes_the_shared_web_contract(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    rc = main(["frequency", str(round_dir)])

    assert rc == 0
    payload = json.loads((round_dir / "frequency_view.json").read_text())
    assert payload["schema"] == "jts_frequency_view/1"
    assert payload["runs"][0]["series"][0]["kind"] == "average"


def test_cli_frequency_accepts_a_standalone_analysis_document(tmp_path):
    from jasper.cli.round_views import main

    source = tmp_path / "analysis.json"
    source.write_text(json.dumps({
        "analysis": {
            "summed_response": {
                "freqs_hz": [100.0, 1000.0],
                "magnitude_db": [-24.0, -23.0],
            },
        },
    }))

    assert main(["frequency", str(source)]) == 0
    payload = json.loads((tmp_path / "frequency_view.json").read_text())
    assert payload["runs"][0]["id"] == "analysis"
    assert payload["runs"][0]["series"][0]["kind"] == "analysis"


def test_cli_frequency_rejects_a_json_document_without_curves(tmp_path, capsys):
    from jasper.cli import round_views as cli

    source = tmp_path / "notes.json"
    source.write_text(json.dumps({"notes": "not a measurement"}))

    assert cli.main(["frequency", str(source)]) == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_reports_the_unreadable_exit_on_an_unreadable_round(tmp_path, capsys):
    from jasper.cli import round_views as cli

    rc = cli.main(["entry", str(tmp_path / "nope")])
    assert rc == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_reports_the_write_exit_when_the_view_cannot_be_written(tmp_path, capsys):
    """An ``--out`` this process cannot create has its own named exit code,
    apart from the unreadable round's: the two send an operator to different
    places, and neither is a traceback out of the writer."""
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    rc = cli.main([
        "entry", str(round_dir), "--out", str(tmp_path / "no-such-dir" / "o.json"),
    ])

    assert rc == cli.EXIT_WRITE_FAILED
    assert json.loads(capsys.readouterr().out)["status"] == "unwritable"


def test_a_payload_the_strict_writer_rejects_is_not_a_filesystem_problem(
    tmp_path, capsys, monkeypatch
):
    """The WRITE stage claims ``OSError`` and nothing else.

    The strict writer also rejects a payload carrying ``NaN``, and that is the
    run's doing, not the filesystem's. Sending that operator to check
    permissions sends them to the wrong place, so it falls to the refusal arm
    instead.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    def _strict(*_args, **_kwargs):
        raise ValueError("Out of range float values are not JSON compliant")

    monkeypatch.setattr(cli._common, "write_report", _strict)

    rc = cli.main(["entry", str(round_dir)])

    assert rc == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


def test_an_entry_grade_over_a_packet_missing_its_block_reads_as_unreadable(
    tmp_path, capsys, monkeypatch
):
    """A packet with no ``entry_baseline`` key is corrupt, not a view declining.

    The builder always emits the block — ``available: False`` is how it reports
    a round that banked no take — so a bare ``KeyError`` there can only mean a
    packet nothing built, which is the unreadable arm by the grade's own
    docstring. Hand-built because no fixture can produce it.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1", position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    read = cli._common.load_banked_round

    def _without_the_block(path):
        banked = read(path)
        return dataclasses.replace(
            banked,
            packet={k: v for k, v in banked.packet.items() if k != "entry_baseline"},
        )

    monkeypatch.setattr(cli._common, "load_banked_round", _without_the_block)

    rc = cli.main(["entry", str(round_dir)])

    assert rc == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_where_a_view_pointed_at_a_session_bundle_files_its_artifact(
    tmp_path, monkeypatch
):
    """Two bundles, two homes, and neither is inside the bundle itself.

    A bundle a round was banked AROUND files beside that round: the
    bundle-taking verbs can only be pointed at the bundle, and filing beside
    the caller would leave every artifact where ``inventory`` never looks. A
    bundle no round holds is the daemon's own directory, and defaulting inside
    it made the ordinary invocation — grade the round I just ran — depend on
    writing into the web host's tree (#3498).
    """
    from jasper.cli.round_views import main

    curves = {"cloud_verify_02": ("onax", _flat_curve())}
    round_dir = _make_round_dir(tmp_path, "r1", position_curves=curves)
    banked_bundle = round_dir / "bundle" / "sess1"
    # The on-speaker shape: /var/lib/jasper/active_speaker/sessions/<id>.
    sessions = tmp_path / "daemon" / "sessions"
    sessions.mkdir(parents=True)
    live = sessions / "live-1"
    (_make_round_dir(tmp_path / "other", "r2", position_curves=curves)
     / "bundle" / "sess1").rename(live)
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)

    assert main(["entry", str(banked_bundle)]) == 0
    assert main(["entry", str(live)]) == 0

    assert (round_dir / "entry_state_grade.json").is_file()
    assert not (banked_bundle / "entry_state_grade.json").exists()
    assert (here / "live-1-entry_state_grade.json").is_file()
    assert not (live / "entry_state_grade.json").exists()


# --------------------------------------------------------------------------- #
# View 0 — entry_state_grade
# --------------------------------------------------------------------------- #


ENTRY_TAKE_ID = "entry_baseline_01_01"


def _bank_entry_baseline_take(
    round_dir: Path,
    *,
    magnitude_db: np.ndarray,
    excluded: np.ndarray | None = None,
    graph_fingerprint: str = "entrygraph0001",
    freqs_hz: np.ndarray | None = None,
) -> None:
    """One write-once entry-baseline take, in the tree the store banks to.

    ``crossover_v2/<capture>/positions/<take_id>.json`` — the path
    ``contracts.BANKED_TAKE_GLOB`` selects and
    ``position_cycle.read_entry_baseline_take`` opens. Written as the real
    record is shaped rather than as the reader's narrowed view, so a change to
    either the index columns or the accept rule fails these tests.
    """
    grid = GRID if freqs_hz is None else freqs_hz
    mask = np.zeros(grid.shape, dtype=bool) if excluded is None else excluded
    positions = (
        round_dir / "bundle" / "sess1" / "evidence/v1/artifacts"
        / "crossover_v2" / "cap1" / "positions"
    )
    positions.mkdir(parents=True, exist_ok=True)
    (positions / f"{ENTRY_TAKE_ID}.json").write_text(json.dumps({
        "kind": "jts_crossover_v2_position_evidence",
        "schema_version": 1,
        "session_id": "cap1",
        "measure_kind": "baseline",
        "phase": "entry_baseline",
        "take_id": ENTRY_TAKE_ID,
        "position_id": ENTRY_TAKE_ID,
        "index": 1,
        "attempt": 1,
        "position_deg": 0,
        "role": "onax",
        "program_id": "prog-entry",
        "reference_mark": "design_axis",
        "graph_fingerprint": graph_fingerprint,
        "captured_at": "2026-08-30T00:00:00Z",
        "freqs_hz": grid.tolist(),
        "magnitude_db": magnitude_db.tolist(),
        "excluded": [bool(flag) for flag in mask],
    }))


def _round_with_entry_baseline(tmp_path: Path, **kwargs: Any) -> Path:
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )
    _bank_entry_baseline_take(round_dir, **kwargs)
    write_manifest(round_dir)
    return round_dir


def test_entry_grades_the_only_round_shape_that_banks_an_entry_baseline(tmp_path):
    """Issue #3478: the entry view accepts a STAGE-1 round.

    An entry baseline exists in exactly one round shape — the measure stage —
    and that stage banks no cloud group, so it has neither cloud positions nor
    a graded ``spec`` block. The loader used to refuse it on both counts,
    which made the one view whose description names what stage 1 banks
    unreachable on every rig.

    The grade a round with no post-apply spec gets is stated in NO frame, and
    the report says so on its face: there is no span in this round for a
    before to be made comparable with.
    """
    banked = load_banked_round(bank_measure_round(tmp_path))

    grade = entry_state_grade(banked)

    assert grade.available is True
    assert grade.reason == ""
    assert grade.report is not None
    assert len(grade.report.bands) == len(flat_spec.SPEC_BANDS)
    assert grade.report.trusted_floor_hz is None
    assert grade.report.trusted_ceiling_hz is None
    assert grade.program_id == "prog-entry"


def test_the_cli_entry_and_frequency_verbs_read_a_stage_one_round(tmp_path, capsys):
    """Both verbs the campaign hit, through ``main`` and the real argv.

    ``frequency`` was refused by the same loader gate and for the same wrong
    reason: its own projector already renders an entry-baseline series from a
    packet with no positions, so nothing but the gate stood between a stage-1
    round and its curve.
    """
    from jasper.cli import round_views as cli

    round_dir = bank_measure_round(tmp_path)

    assert cli.main(["entry", str(round_dir)]) == 0
    grade = json.loads(Path(json.loads(capsys.readouterr().out)["out"]).read_text())
    assert grade["available"] is True
    assert grade["round_ordinal"] == 1

    assert cli.main(["frequency", str(round_dir)]) == 0
    view = json.loads(Path(json.loads(capsys.readouterr().out)["out"]).read_text())
    assert [s["kind"] for s in view["runs"][0]["series"]] == ["entry_baseline"]


def test_the_entry_state_is_graded_by_the_shipped_evaluator(tmp_path):
    """The door's whole contract: it CONSUMES the grading, never repeats it.

    Asserted against an independent ``evaluate_flat_spec`` call on the same
    inputs — the take's own curve and mask, in the round's own frame — so the
    door cannot pass by returning plausible numbers of its own. Field-for-field
    on the report, not a spot check: a door that graded the right curve in the
    WRONG frame would agree on the bands and disagree on the reference.
    """
    curve = _flat_curve(ripple_db=3.0)
    banked = load_banked_round(_round_with_entry_baseline(tmp_path, magnitude_db=curve))

    grade = entry_state_grade(banked)

    assert grade.available is True
    assert grade.reason == ""
    expected = evaluate_flat_spec(
        GRID, curve, np.zeros(GRID.shape, dtype=bool),
        smoothing_fraction=banked.report.smoothing_fraction,
        trusted_floor_hz=banked.report.trusted_floor_hz,
        trusted_ceiling_hz=banked.report.trusted_ceiling_hz,
    )
    assert grade.report is not None
    assert grade.report.to_dict() == expected.to_dict()


def test_a_re_grade_reads_the_rounds_room_floor_back_instead_of_re_deriving_it(
    tmp_path,
):
    """#3502 — a re-evaluation states the ROUND's room floor, never its own.

    This door grades a stored take in the round's own frame. The room floor is
    part of that frame: the round pooled it from the seats it actually
    measured, and a floor recomputed at this door would be a second opinion
    about one room, stated over a curve that never saw it. So it is read off
    the banked report — provenance included, because a floor that arrived here
    as ``declared_geometry`` may not print as measured downstream.
    """
    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    banked_path = next(round_dir.glob("bundle/*/evidence/v1/artifacts/**/cloud_verify.json"))
    cloud = json.loads(banked_path.read_text())
    cloud["spec"]["entanglement_floor_hz"] = 610.0
    cloud["spec"]["entanglement_floor_source"] = "declared_geometry"
    banked_path.write_text(json.dumps(cloud))
    banked = load_banked_round(round_dir)
    assert banked.report is not None
    assert banked.report.entanglement_floor_hz == 610.0

    report = entry_state_grade(banked).report

    assert report is not None
    assert report.entanglement_floor_hz == banked.report.entanglement_floor_hz
    assert report.entanglement_floor_source == banked.report.entanglement_floor_source
    # It MARKS and does not clamp: the graded edges are the round's, untouched.
    assert [b.graded_lo_hz for b in report.bands] == [
        b.graded_lo_hz for b in banked.report.bands
    ]
    assert any(b.room_entangled_below_hz == 610.0 for b in report.bands)


def test_the_entry_grade_carries_a_per_band_table(tmp_path):
    """The same per-band rows a round's own ``spec`` block carries.

    Structural, not a spot value: every ``SPEC_BANDS`` row is answered for, and
    each row states its own tolerance and verdict. That is what makes this
    table readable beside a round's without a translation step.
    """
    banked = load_banked_round(
        _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    )

    report = entry_state_grade(banked).report

    assert report is not None
    assert len(report.bands) == len(flat_spec.SPEC_BANDS)
    assert [b.tolerance_db for b in report.bands] == [
        row[2] for row in flat_spec.SPEC_BANDS
    ]
    assert all(band.evaluable for band in report.bands)
    assert report.overall_within_target is True


def test_a_tilted_entry_state_fails_the_band_it_is_tilted_in(tmp_path):
    """Discriminating: the grade tracks the curve, not the fixture.

    A treble shelf far outside the top band's tolerance must fail THAT band and
    leave the others passing — a door returning a canned "within_target" report, or
    grading somebody else's curve, cannot produce this shape.
    """
    curve = _flat_curve()
    curve[GRID >= 8000.0] += 6.0
    banked = load_banked_round(_round_with_entry_baseline(tmp_path, magnitude_db=curve))

    report = entry_state_grade(banked).report

    assert report is not None
    assert report.overall_within_target is False
    by_edge = {band.f_lo_hz: band for band in report.bands}
    assert by_edge[8000.0].within_target is False
    assert all(
        band.within_target is True for lo, band in by_edge.items() if lo != 8000.0
    )


def test_the_entry_grade_reads_the_takes_OWN_exclusion_mask(tmp_path):
    """The mask belongs to this capture, not to the round's other one.

    A bin the entry-baseline screen flagged must not be graded. Pinned with a
    curve whose ONLY spec violation sits under the mask: unmasked it fails,
    masked it passes, so a door that dropped the mask (or reached for the
    round's post-apply exclusions instead) is a different answer, not a
    rounding difference.
    """
    curve = _flat_curve()
    spike = GRID >= 8000.0
    curve[spike] += 6.0

    unmasked = load_banked_round(
        _round_with_entry_baseline(tmp_path / "a", magnitude_db=curve)
    )
    masked = load_banked_round(
        _round_with_entry_baseline(tmp_path / "b", magnitude_db=curve, excluded=spike)
    )

    assert entry_state_grade(unmasked).report.overall_within_target is False
    masked_report = entry_state_grade(masked).report
    assert masked_report is not None
    # The masked band has no evidence left, so it is UNEVALUABLE — never a
    # silent pass. That is `BandResult`'s own contract and this door inherits
    # it rather than restating it.
    by_edge = {band.f_lo_hz: band for band in masked_report.bands}
    assert by_edge[8000.0].evaluable is False
    assert by_edge[8000.0].within_target is None
    assert by_edge[250.0].within_target is True


def test_the_entry_grade_names_WHICH_entry_state_it_graded(tmp_path):
    """An unattributed table is not a disclosure.

    The first round's entry graph is the declarations-derived config a fresh
    box wears; a later round's is whatever the previous round left playing.
    The fingerprint is what tells them apart, so it rides on the result.
    """
    banked = load_banked_round(
        _round_with_entry_baseline(
            tmp_path, magnitude_db=_flat_curve(), graph_fingerprint="fresh0000beef",
        )
    )

    grade = entry_state_grade(banked)

    assert grade.graph_fingerprint == "fresh0000beef"
    assert grade.program_id == "prog-entry"
    assert grade.reference_mark == "design_axis"
    assert grade.artifact_ref == ENTRY_TAKE_ID
    assert grade.to_dict()["graph_fingerprint"] == "fresh0000beef"


def test_a_round_that_banked_no_entry_baseline_says_so_with_a_reason(tmp_path):
    """The honest door for the case it cannot answer.

    Retention is fail-soft and never costs the household a retake, so "no take"
    is a fact to report rather than a failure to raise. It must arrive as a
    NAMED reason with no report beside it — never an empty table that reads as
    a clean bill of health.
    """
    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.available is False
    assert grade.report is None
    assert grade.reason  # named, never a bare False
    assert grade.to_dict()["report"] is None


def test_a_banked_take_whose_curve_does_not_rehydrate_is_not_graded(tmp_path):
    """A mask shorter than its curve is unreadable, not gradeable.

    ``EntryBaseline.from_dict`` owns that rule; this pins that the door takes
    its ``None`` as a refusal to grade rather than pushing a length-disagreeing
    pair into the evaluator.
    """
    round_dir = _round_with_entry_baseline(
        tmp_path, magnitude_db=_flat_curve(),
        excluded=np.zeros(GRID.shape[0] - 3, dtype=bool),
    )

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.available is False
    assert grade.report is None
    assert grade.reason == ENTRY_STATE_UNREADABLE


def test_the_cli_entry_verb_writes_the_grade_beside_the_evidence(tmp_path, capsys):
    """The DOOR, not just the view — through ``main`` and the real argv.

    A product view nothing can reach is not a door: before this verb the entry
    state could only be graded by an operator calling ``evaluate_flat_spec`` by
    hand. Drives the console script end to end and asserts the artifact it
    leaves behind.
    """
    from jasper.cli import round_views as cli

    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())

    assert cli.main(["entry", str(round_dir)]) == 0

    written = json.loads((round_dir / "entry_state_grade.json").read_text())
    assert written["available"] is True
    assert written["graph_fingerprint"] == "entrygraph0001"
    assert len(written["report"]["bands"]) == len(flat_spec.SPEC_BANDS)


def test_the_cli_entry_verb_exits_0_when_there_is_nothing_to_grade(tmp_path):
    """"No gradeable take" is an ANSWER, not an unreadable round.

    A caller can tell "I looked, and this round banked none" from "I could not
    look" by the exit code alone.
    """
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={"cloud_verify_02": ("onax", _flat_curve())},
    )

    assert cli.main(["entry", str(round_dir)]) == 0

    written = json.loads((round_dir / "entry_state_grade.json").read_text())
    assert written["available"] is False
    assert written["report"] is None
    assert written["reason"]


def _write_state(round_dir: Path, payload: dict[str, Any]) -> None:
    bundle = round_inputs_mod.round_inputs(round_dir).session_dir
    capture, _ = round_inputs_mod.round_artifact_dir(bundle)
    assert capture is not None
    (round_dir / "state.json").write_text(json.dumps({"session_id": capture.name, **payload}))


def test_the_entry_grade_attributes_the_round_and_its_ordinal_epoch(tmp_path):
    """An unattributed table is not a disclosure.

    "The entry state was this flat" means one thing at round 1 of a fresh box
    and another at round 1 after a republish reset the count — so the ordinal
    and the epoch it counts in ride on the result and its payload.
    """
    round_dir = _round_with_entry_baseline(tmp_path, magnitude_db=_flat_curve())
    _write_state(round_dir, {
        "round_receipt": {"round_ordinal": 2}, "round_ordinal_epoch": 3,
    })

    grade = entry_state_grade(load_banked_round(round_dir))

    assert grade.round_ordinal == 2
    assert grade.round_ordinal_epoch == 3
    payload = grade.to_dict()
    assert payload["round_ordinal"] == 2
    assert payload["round_ordinal_epoch"] == 3


def test_an_unrecorded_ordinal_reads_as_not_recorded_never_zero(tmp_path):
    """``None`` and ``0`` are different facts, and the epoch's whole meaning
    turns on the difference: ``0`` is "never reset", which a round that simply
    banked no state file has said nothing about.

    ``bool`` is rejected too — a hand-edited ``true`` must not publish as
    epoch 1.
    """
    no_state = _round_with_entry_baseline(tmp_path / "a", magnitude_db=_flat_curve())
    grade = entry_state_grade(load_banked_round(no_state))
    assert grade.round_ordinal is None
    assert grade.round_ordinal_epoch is None

    booly = _round_with_entry_baseline(tmp_path / "b", magnitude_db=_flat_curve())
    _write_state(booly, {
        "round_receipt": {"round_ordinal": True}, "round_ordinal_epoch": True,
    })
    boolean = entry_state_grade(load_banked_round(booly))
    assert boolean.round_ordinal is None
    assert boolean.round_ordinal_epoch is None


def test_the_cli_counts_an_unevaluable_band_apart_from_a_failing_one(tmp_path, capsys):
    """An UNEVALUABLE band is not a failing band.

    A band whose every bin the take's own gate clamped away has no evidence —
    ``passed is None``, never ``False`` — and an answer that counted it as
    failing would report a band nobody could measure as one that measured
    badly. Driven through the console script, on the same masked fixture the
    product-level mask test uses.
    """
    from jasper.cli import round_views as cli

    curve = _flat_curve()
    spike = GRID >= 8000.0
    curve[spike] += 6.0
    round_dir = _round_with_entry_baseline(
        tmp_path, magnitude_db=curve, excluded=spike,
    )

    assert cli.main(["entry", str(round_dir)]) == 0

    answer = json.loads(capsys.readouterr().out)
    assert answer["unevaluable"] == 1
    assert answer["outside_target"] == 0


#: The views one round directory answers, as the operator's own argv. One
#: fixture drives them all, so the ANSWER's shape is pinned once here rather
#: than re-asserted verb by verb.
_SINGLE_ROUND_VIEWS = ("entry", "frequency", "inventory")


def _longest_numeric_list(node: Any) -> int:
    """The longest run of numbers anywhere in a document."""
    if isinstance(node, list):
        numbers = all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in node
        )
        return max([
            len(node) if node and numbers else 0,
            *(_longest_numeric_list(item) for item in node),
        ])
    if isinstance(node, dict):
        return max([0, *(_longest_numeric_list(value) for value in node.values())])
    return 0


@pytest.mark.parametrize("view", _SINGLE_ROUND_VIEWS)
def test_a_view_answers_on_stdout_and_leaves_the_curves_in_its_artifact(
    tmp_path, capsys, view
):
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(
        tmp_path, "r1",
        position_curves={
            "cloud_verify_02": ("onax", _flat_curve()),
            "cloud_verify_04": ("offax", _flat_curve(ripple_db=1.0)),
        },
    )

    assert cli.main([*shlex.split(view), str(round_dir)]) == cli.EXIT_OK

    answer = json.loads(capsys.readouterr().out)
    assert answer["view"] == shlex.split(view)[0]
    # ``status`` is how a FAILURE is recognised; a success never carries one.
    assert "status" not in answer
    written = Path(answer["out"])
    assert written.is_file()
    assert written.stat().st_size == answer["bytes"]
    assert _longest_numeric_list(answer) <= 16


# --------------------------------------------------------------------------- #
# banked lateral-pose takes, shared with the candidate-ladder suite
# --------------------------------------------------------------------------- #


def _bank_lateral_pose(
    session_dir: Path, *, take_id: str, position_deg: int,
    curves: list[dict[str, Any]], vertical_deg: int = 0,
    capture: str = "wired-TEST", candidate_id: str = "",
) -> None:
    """Directly write a banked ``positions/<take_id>.json`` lateral-pose
    take — the exact shape :func:`~jasper.active_speaker.crossover_v2.record_index.bundle_measurements`
    and :func:`~jasper.active_speaker.crossover_v2.position_cycle.read_take_curves`
    read, real-shaped without going through the retention engine. Mirrors
    ``test_crossover_v2_feature_classifier.py``'s own fixture builder for
    the same take shape.
    """
    positions_dir = (
        session_dir / "evidence/v1/artifacts/crossover_v2" / capture / "positions"
    )
    positions_dir.mkdir(parents=True, exist_ok=True)
    (positions_dir / f"{take_id}.json").write_text(json.dumps({
        "kind": POSITION_EVIDENCE_KIND,
        "phase": PHASE_LATERAL,
        "position_deg": position_deg,
        "vertical_deg": vertical_deg,
        "candidate_id": candidate_id,
        "curves": curves,
    }))


def _summed_curve(freqs_hz: np.ndarray, magnitude_db: np.ndarray) -> dict[str, Any]:
    return {
        "role": "summed",
        "band_hz": [20.0, 20000.0],
        "freqs_hz": [float(v) for v in freqs_hz],
        "magnitude_db": [float(v) for v in magnitude_db],
        "phase_deg": [0.0] * len(freqs_hz),
    }


# --------------------------------------------------------------------------- #
# gate-sweep: the ladder itself, over the round's own captures
# --------------------------------------------------------------------------- #


@pytest.fixture
def gate_sweep_round(tmp_path):
    root = bank_capture_round(tmp_path, [_pose_ir(i, late_copy_ms=8.0) for i in range(3)])
    bundle = root / "bundle/b0"
    records = []
    for i, path in enumerate(sorted((bundle / "summed").glob("*.json"))):
        record = json.loads(path.read_text())
        record["level_db"] = -30.0 if i < 2 else -20.0
        path.write_text(json.dumps(record))
        records.append((str(path.relative_to(bundle)), record))
    write_manifest(root, groups=[manifest_set(records[:2], set_id="first"), manifest_set(records[2:], set_id="second")])
    return root


@pytest.mark.parametrize("set_id,ids", [(None, None), ("first", ("cloud_verify_00", "cloud_verify_01"))])
def test_cli_gate_sweep_writes_its_report_beside_the_round(gate_sweep_round, set_id, ids):
    from jasper.cli import round_views as cli
    from jasper.cli._report import render_report
    from jasper.active_speaker.crossover_v2.gate_sweep import sweep_round

    expected = sweep_round(gate_sweep_round, rungs_ms=[5, 20], take_ids=ids)
    flags = ["--set", set_id] if set_id else []
    rc = cli.main(["sweep", "--scope", "round", str(gate_sweep_round), "--rungs-ms", "5", "20", *flags])
    assert rc == cli.EXIT_OK
    path = gate_sweep_round / (f"gate_sweep-{set_id}.json" if set_id else "gate_sweep.json")
    assert path.read_bytes() == (render_report(expected) + "\n").encode()
    assert {pose["capture_id"] for pose in expected["poses"]} == set(ids or ("cloud_verify_00", "cloud_verify_01", "cloud_verify_02"))


def test_cli_gate_sweep_out_puts_the_report_where_it_is_told(
    gate_sweep_round, tmp_path
):
    from jasper.cli import round_views as cli

    elsewhere = tmp_path / "sweep.json"
    rc = cli.main(
        ["sweep", "--scope", "round", str(gate_sweep_round), "--rungs-ms", "5", "20",
         "--out", str(elsewhere)]
    )

    assert rc == cli.EXIT_OK
    assert elsewhere.is_file()
    assert not (
        gate_sweep_round / cli.ARTIFACT_BY_VIEW["sweep --scope round"].artifact
    ).exists()


def test_cli_gate_sweep_at_hz_reports_the_named_bin(gate_sweep_round):
    from jasper.cli import round_views as cli

    rc = cli.main(
        ["sweep", "--scope", "round", str(gate_sweep_round), "--rungs-ms", "5", "20",
         "--at-hz", "800"]
    )

    assert rc == cli.EXIT_OK
    report = json.loads(
        (gate_sweep_round / cli.ARTIFACT_BY_VIEW["sweep --scope round"].artifact).read_text()
    )
    (feature,) = report["features"]
    assert feature["requested_hz"] == 800.0


def test_cli_gate_sweep_refusal_names_the_missing_input(tmp_path, capsys):
    """The ladder's own refusal reaches the operator under ITS reason, not the
    stage bucket: which input was missing is the answer."""
    from jasper.cli import round_views as cli

    root = bank_measure_round(tmp_path)
    assert cli.main(["sweep", "--scope", "round", str(root)]) == cli.EXIT_REFUSED

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "refused"
    assert payload["reason"] == REFUSE_NO_CAPTURES


@pytest.mark.parametrize("bad", [1, 2])
def test_cli_gate_sweep_names_the_captures_it_left_out(gate_sweep_round, capsys, bad):
    """A capture whose WAV is gone is never read and never costs the round its
    other poses: the sweep answers while two remain, and names what it left
    out either way."""
    from jasper.cli import round_views as cli

    sidecars = sorted((gate_sweep_round / "bundle/b0/summed").glob("*.json"))[:bad]
    for sidecar in sidecars:
        sidecar.with_suffix(".wav").unlink()
    omitted = [
        {"capture_id": sidecar.stem.removeprefix("summed_"), "sidecar": sidecar.name,
         "reason": REFUSE_CAPTURE_UNREADABLE}
        for sidecar in sidecars
    ]
    rc = cli.main(["sweep", "--scope", "round", str(gate_sweep_round), "--rungs-ms", "5", "20"])

    payload = json.loads(capsys.readouterr().out)
    if bad == 1:
        assert rc == cli.EXIT_OK
        assert (payload["poses"], payload["omitted"]) == (2, omitted)
        artifact = gate_sweep_round / cli.ARTIFACT_BY_VIEW["sweep --scope round"].artifact
        assert json.loads(artifact.read_text())["omitted"] == omitted
    else:
        assert rc == cli.EXIT_REFUSED
        assert payload["reason"] == REFUSE_SINGLE_POSE
        assert json.loads(payload["detail"])["omitted"] == omitted


@pytest.mark.parametrize(
    ("evidence", "published"),
    [({"b": 2, "a": 1}, '{"a": 1, "b": 2}'), ("a sentence", "a sentence")],
)
def test_a_named_refusal_publishes_the_evidence_it_was_given(evidence, published, capsys):
    """Fields publish as sorted JSON; a refusal carrying only its own sentence
    publishes that sentence, never a JSON-quoted string of it."""
    from jasper.cli import round_views as cli

    assert cli.refused_by_name("a_slug", evidence) == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["detail"] == published


@pytest.mark.parametrize(
    "argv", [["--rungs-ms", "7"], ["--at-hz", "100"]],
    ids=["one-rung-ladder", "bin-off-the-analysis-grid"],
)
def test_cli_gate_sweep_an_unusable_request_is_the_unreadable_exit(
    gate_sweep_round, capsys, argv
):
    from jasper.cli import round_views as cli
    rc = cli.main(["sweep", "--scope", "round", str(gate_sweep_round), *argv])

    assert rc == cli.EXIT_UNREADABLE
    assert not (
        gate_sweep_round / cli.ARTIFACT_BY_VIEW["sweep --scope round"].artifact
    ).exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unreadable"
    assert payload["reason"] == cli.REASON_UNREADABLE


def test_cli_gate_sweep_an_unwritable_out_is_the_write_exit(gate_sweep_round, capsys):
    """The round read and the sweep ran; only the filing failed."""
    from jasper.cli import round_views as cli

    blocker = gate_sweep_round / "not-a-dir"
    blocker.write_text("")

    rc = cli.main(
        ["sweep", "--scope", "round", str(gate_sweep_round), "--rungs-ms", "5", "20",
         "--out", str(blocker / "x.json")]
    )

    assert rc == cli.EXIT_WRITE_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unwritable"
    assert payload["reason"] == cli.REASON_UNWRITABLE


# --------------------------------------------------------------------------- #
# candidates -- the CLI shape over candidate_ladder
# --------------------------------------------------------------------------- #


def test_cli_candidates_publishes_the_ladders_named_refusal(tmp_path, capsys):
    """The engine raises BY NAME and this verb publishes that name, not the
    stage bucket a caught ``Exception`` would fall into.

    The numbers, and what the refusal carries, are pinned on
    ``candidate_ladder`` itself; the success shape is pinned once for every
    view by ``tests/test_cli_exit_vocabulary.py``'s roster.
    """
    from jasper.cli import round_views as cli

    assert cli.main(
        ["candidates", str(bank_measure_round(tmp_path))]
    ) == cli.EXIT_REFUSED

    record = json.loads(capsys.readouterr().out)
    assert record["status"] == "refused"
    assert record["reason"] == REFUSE_NO_LADDER


def test_inventory_commands_preserve_path_tokens_and_required_inputs(tmp_path, capsys):
    from jasper.cli.round_views import main, build_parser

    round_dir = _make_round_dir(tmp_path, "round's $(touch surprise) <x>", position_curves={
        "seat": ("onax", _flat_curve()),
    })
    assert main(["inventory", str(round_dir)]) == 0
    rows = {row["artifact"]: row for row in json.loads(Path(json.loads(capsys.readouterr().out)["out"]).read_text())["artifacts"]}
    command = shlex.split(rows[FREQUENCY_VIEW_FILENAME]["next_command"])
    assert command == ["jasper-round-views", "frequency", str(round_dir)]
    assert main(command[1:]) == 0
    assert (round_dir / FREQUENCY_VIEW_FILENAME).is_file()
    distortion = rows["harmonic_distortion.json"]
    args = build_parser().parse_args(shlex.split(distortion["next_command"])[1:])
    assert args.bundle_dir == round_dir
    assert distortion["required_inputs"] == []
    assert rows[FREQUENCY_VIEW_FILENAME]["required_inputs"] == []
    assert rows[POSITION_CYCLE_FILENAME]["next_command"] is None
    assert rows[POSITION_CYCLE_FILENAME]["repair_reason"] == "banked_pose_index_missing"


