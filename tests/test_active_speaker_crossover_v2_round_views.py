# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Round view artifacts, answers, and grades over retained evidence."""

from __future__ import annotations

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
    RoundViewsError,
    load_banked_round,
)
from jasper.active_speaker.crossover_v2.gate_sweep import REFUSE_SINGLE_POSE
from jasper.active_speaker.crossover_v2.round_captures import REFUSE_CAPTURE_UNREADABLE, REFUSE_NO_CAPTURES
from jasper.active_speaker.frequency_view import FREQUENCY_VIEW_FILENAME
from jasper.active_speaker.measurement_programs import PURPOSE_SPEAKER
from jasper.active_speaker.repeat_floor import derive_repeat_floor
from jasper.active_speaker.round_packet import store_banked_evidence
from jasper.active_speaker.run_manifest import RUN_MANIFEST_FILENAME

from tests.crossover_v2_banked_round import bank_measure_round
from tests.crossover_v2_fixtures import bank_capture_round
from tests.run_manifest_fixture import manifest_set, write_bundle_manifest, write_manifest
# The gate sweep's own pose IRs, reused rather than copied, so a deconvolved
# round's answer is as knowable here as it is there.
from tests.test_crossover_v2_gate_sweep import _pose_ir

#: A live session bundle resolves its three non-bundle inputs to the on-speaker
#: SSOT paths; no test may read whatever sits at those absolute paths on the
#: box running pytest.
pytestmark = pytest.mark.usefixtures("no_real_pi_paths")

GRID = np.geomspace(280.0, 16000.0, 90)


def _make_round_dir(tmp_path: Path, name: str, *, take: bool = False) -> Path:
    """One banked round directory, in the tree ``bank-crossover-round.sh``
    produces: ``<round-dir>/bundle/<session>/evidence/v1/artifacts/crossover_v2/<capture>/``.
    ``take`` banks one design-axis take with its summed curve."""
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
    if take:
        (capture_dir / "positions").mkdir()
        (capture_dir / "positions" / "take_0001.json").write_text(json.dumps({
            "kind": POSITION_EVIDENCE_KIND, "phase": "measure", "take_id": "take_0001",
            "measurement_purpose": PURPOSE_SPEAKER, "position_deg": 0,
            "curves": [_summed_curve(GRID, np.full(GRID.shape, -20.0))],
        }))
    write_manifest(round_dir)
    store_banked_evidence(round_dir)
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
    round_dir = _make_round_dir(tmp_path, "r1")
    session_dir = round_dir / "bundle" / "sess1"

    loaded = load_banked_round(session_dir if live else round_dir)

    assert loaded.inputs.banked is not live
    assert loaded.inputs.session_dir == session_dir
    assert loaded.session_dir == session_dir


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
    mine = _make_round_dir(tmp_path, "r1") / "bundle" / "sess1"
    other = _make_round_dir(tmp_path, "r2") / "bundle" / "sess1"
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
    round_dir = _make_round_dir(tmp_path, "r1")
    (round_dir / "bundle" / "second_session").mkdir()
    with pytest.raises(RoundViewsError, match="expected exactly one"):
        load_banked_round(round_dir)


def test_load_banked_round_reads_a_repeat_floor_banked_beside_it(tmp_path):
    """The side file reaches the packet the bank builds exactly as
    applied-profile.json does: present, the accuracy budget's repeat-floor
    component is available."""
    round_dir = _make_round_dir(tmp_path, "r1")
    component = "in_capture_repeat_floor"
    absent = load_banked_round(round_dir)
    assert absent.packet["accuracy_budget"]["components"][component]["status"] == "unavailable"

    # The record the REAL deriver banks from two repeats, never a hand-typed one.
    floor = derive_repeat_floor(samples={"residual_db": [0.0, 0.2]}, rounds=[{}, {}])
    (round_dir / "repeat-floor.json").write_text(json.dumps({**floor, "aggregate_metric": "residual_db"}))
    store_banked_evidence(round_dir)
    present = load_banked_round(round_dir)
    assert present.packet["accuracy_budget"]["components"][component]["status"] == "available"


# --------------------------------------------------------------------------- #
# CLI wiring — jasper-round-views
# --------------------------------------------------------------------------- #


def test_cli_inventory_names_what_is_missing_and_what_produces_it(tmp_path):
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(tmp_path, "r1", take=True)

    def inventory():
        assert cli.main(["inventory", str(round_dir)]) == 0
        payload = json.loads((round_dir / "inventory.json").read_text())
        assert payload["bytes_total"] == sum(row["bytes"] or 0 for row in payload["artifacts"])
        return {row["artifact"]: row for row in payload["artifacts"]}

    rows = inventory()
    # Every path this round can fill is filled: the row is a line to run.
    missing = rows[FREQUENCY_VIEW_FILENAME]
    assert missing["present"] is False
    assert missing["bytes"] is None
    assert missing["produced_by"] == f"jasper-round-views frequency {round_dir}"
    assert missing["producer_needs_more_than_this_round"] is False
    assert missing["path"] == str(round_dir / FREQUENCY_VIEW_FILENAME)
    # The producer it named writes the artifact it named as missing.
    assert cli.main(shlex.split(missing["produced_by"])[1:]) == 0
    present = inventory()[FREQUENCY_VIEW_FILENAME]
    assert present["present"] is True
    assert present["bytes"] == Path(missing["path"]).stat().st_size

    # A view whose subcommand takes MORE than this round says so, and places
    # this round in its own slot. What is left in brackets is what no
    # inventory of one round can fill.
    multi = rows["repeatability.json"]
    assert shlex.split(multi["produced_by"]) == [
        "jasper-round-views", "repeat", str(round_dir), "<other-round>",
    ]
    assert multi["producer_needs_more_than_this_round"] is True

    assert rows[CLASSIFICATION_ARTIFACT]["produced_by"] == (
        f"jasper-round-views classify-features {round_dir}"
    )
    assert rows[CLASSIFICATION_ARTIFACT][
        "producer_needs_more_than_this_round"
    ] is False

    assert "forward_model.json" not in rows
    # A take read files one artifact per take; no round row stands for it.
    assert {row["view"] for row in rows.values()}.isdisjoint({"impulse", "group-delay", "compare"})

    # One row no view here writes: the banker's own pose index, named with the
    # command that makes it rather than with this tool's prog.
    assert rows[POSITION_CYCLE_FILENAME]["produced_by"] == (
        "jasper-round wait --run '<run-id>'"
    )

    # Every view files beside the round, the ones the evidence packet cites
    # too; only the executor's run manifest sits inside the round's evidence.
    assert rows["harmonic_distortion.json"]["path"] == str(round_dir / "harmonic_distortion.json")
    assert rows[RUN_MANIFEST_FILENAME]["path"] == str(
        round_dir / "bundle/sess1/evidence/v1/artifacts/crossover_v2/cap1" / RUN_MANIFEST_FILENAME
    )


def test_cli_frequency_writes_the_shared_web_contract(tmp_path):
    from jasper.cli.round_views import main

    round_dir = _make_round_dir(tmp_path, "r1", take=True)
    rc = main(["frequency", str(round_dir)])

    assert rc == 0
    payload = json.loads((round_dir / "frequency_view.json").read_text())
    assert payload["schema"] == "jts_frequency_view/2"
    assert payload["runs"][0]["series"][0]["kind"] == "measurement"


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

    rc = cli.main(["inventory", str(tmp_path / "nope")])
    assert rc == cli.EXIT_UNREADABLE
    assert json.loads(capsys.readouterr().out)["status"] == "unreadable"


def test_cli_reports_the_write_exit_when_the_view_cannot_be_written(tmp_path, capsys):
    """An ``--out`` this process cannot create has its own named exit code,
    apart from the unreadable round's: the two send an operator to different
    places, and neither is a traceback out of the writer."""
    from jasper.cli import round_views as cli

    round_dir = _make_round_dir(tmp_path, "r1")

    rc = cli.main([
        "inventory", str(round_dir), "--out", str(tmp_path / "no-such-dir" / "o.json"),
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

    round_dir = _make_round_dir(tmp_path, "r1")

    def _strict(*_args, **_kwargs):
        raise ValueError("Out of range float values are not JSON compliant")

    monkeypatch.setattr(cli._common, "write_report", _strict)

    rc = cli.main(["inventory", str(round_dir)])

    assert rc == cli.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["status"] == "refused"


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

    round_dir = _make_round_dir(tmp_path, "r1")
    banked_bundle = round_dir / "bundle" / "sess1"
    # The on-speaker shape: /var/lib/jasper/active_speaker/sessions/<id>.
    sessions = tmp_path / "daemon" / "sessions"
    sessions.mkdir(parents=True)
    live = sessions / "live-1"
    (_make_round_dir(tmp_path / "other", "r2")
     / "bundle" / "sess1").rename(live)
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)

    assert main(["inventory", str(banked_bundle)]) == 0
    assert main(["inventory", str(live)]) == 0

    assert (round_dir / "inventory.json").is_file()
    assert not (banked_bundle / "inventory.json").exists()
    assert (here / "live-1-inventory.json").is_file()
    assert not (live / "inventory.json").exists()


#: The views one round directory answers, as the operator's own argv. One
#: fixture drives them all, so the ANSWER's shape is pinned once here rather
#: than re-asserted verb by verb.
_SINGLE_ROUND_VIEWS = ("frequency", "inventory")


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

    round_dir = _make_round_dir(tmp_path, "r1", take=True)

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
    """Directly write a banked lateral speaker take's ``positions/<take_id>.json``,
    real-shaped without going through the retention engine, and a run
    manifest keeping every take banked so far.
    """
    positions_dir = (
        session_dir / "evidence/v1/artifacts/crossover_v2" / capture / "positions"
    )
    positions_dir.mkdir(parents=True, exist_ok=True)
    (positions_dir / f"{take_id}.json").write_text(json.dumps({
        "kind": POSITION_EVIDENCE_KIND,
        "phase": PHASE_LATERAL,
        "measurement_purpose": PURPOSE_SPEAKER,
        "position_deg": position_deg,
        "vertical_deg": vertical_deg,
        "candidate_id": candidate_id,
        "curves": curves,
    }))
    write_bundle_manifest(session_dir)


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

    expected = {**sweep_round(gate_sweep_round, rungs_ms=[5, 20], take_ids=ids),
                "schema": cli.ARTIFACT_BY_VIEW["sweep --scope round"].schema}
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

    round_dir = _make_round_dir(tmp_path, "round's $(touch surprise) <x>", take=True)
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


