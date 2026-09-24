# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the tuning CLIs speak ONE exit vocabulary, ``jasper/cli/_refusal.py``'s.

Every tool in the runbook's tool menu (``scripts/generate-tuning-tool-menu.py``'s
roster) takes ``EXIT_*`` from that module rather than numbering its own failures.
A tool that re-declares a code drifts silently: the same number came to mean
"refused" in one tool and "unreadable" in the next, which is what this pins shut.
Who is exempt is ``_refusal.OWN_EXIT_VOCABULARY``'s to say, not this file's.

The vocabulary is also what a tool PRINTS, and stdout is where it prints it:
every roster tool driven to a refusal publishes ``failed()``'s document there
and one sentence on stderr, and a success publishes the tool's answer -- one
document, bounded, naming the artifact it wrote rather than inlining it. Both
halves are asserted by calling each tool's own ``main``, because a shape only
holds where the tools actually reach it.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Callable, NamedTuple

import numpy as np
import pytest

from jasper.active_speaker.bench.replay import DSP_REPLAY_SCHEMA
from jasper.active_speaker.commissioning_evidence_store import EVIDENCE_ROOT
from jasper.active_speaker.wizard_client import WizardClient
from jasper.active_speaker.crossover_v2 import prescription_document
from jasper.active_speaker.crossover_v2.refusal_copy import refusal_copy_for
from jasper.cli import _refusal, round_views
from tests.crossover_v2_banked_round import (
    bank_measure_round,
    bank_seat_round,
    bank_verify_round,
)
from jasper.json_fields import sha256_file
from tests.crossover_v2_fixtures import bank_capture_round
from tests.test_take_impulses import bank_kept_impulse_take
from tests.room_median_fixture import write_room_median
from tests.run_manifest_fixture import manifest_set, write_manifest
from tests.test_cli_close_reference import _compare_argv as close_compare_argv, _round as capture_round
from tests.test_crossover_v2_feature_classifier import _bundle as feature_bundle, _resonant_ir as resonant_ir
from tests.test_crossover_v2_round_frequency_view import bass_fit_pairs, bass_run, summed_capture_bundle  # noqa: F401
from tests.test_crossover_v2_harmonic_evidence import bank_measure_capture
from tests.test_crossover_v2_nearfield_view import _take as nearfield_take
from tests.test_crossover_v2_harmonic_evidence import harmonic_capture  # noqa: F401
from tests.test_round_views_directivity import BASELINE, _take as directivity_take
from tests.test_round_views_repeat import _mark_take as mark_take

CLI_DIR = Path(_refusal.__file__).resolve().parent

_MENU_SCRIPT = CLI_DIR.parents[1] / "scripts" / "generate-tuning-tool-menu.py"
_spec = importlib.util.spec_from_file_location("generate_tuning_tool_menu", _MENU_SCRIPT)
assert _spec is not None and _spec.loader is not None
_menu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_menu)

SHARED_RULE = tuple(
    name
    for name in _menu.TUNING_TOOL_MODULES
    if name not in _refusal.OWN_EXIT_VOCABULARY
)


def _sources(module_name: str) -> list[Path]:
    """This tool's own source: one module, or every module of a package."""

    leaf = CLI_DIR / module_name.rsplit(".", 1)[-1]
    return sorted(leaf.glob("*.py")) if leaf.is_dir() else [leaf.with_suffix(".py")]


def _declared_exit_names(module_name: str) -> set[str]:
    """The ``EXIT_*`` names this module assigns at module scope.

    Annotated assignments count too: ``EXIT_FOO: int = 4`` is the same drift.
    """

    targets = [
        target
        for path in _sources(module_name)
        for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
        for target in (
            node.targets if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign)
            else []
        )
    ]
    return {
        target.id
        for target in targets
        if isinstance(target, ast.Name) and target.id.startswith("EXIT_")
    }


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_no_tuning_cli_numbers_its_own_exits(module_name: str) -> None:
    assert _declared_exit_names(module_name) == set()


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_every_tuning_cli_exit_name_is_the_shared_constant(module_name: str) -> None:
    """The names a tool exposes are ``_refusal``'s, with its values.

    Paired with the AST test above, which is what makes this more than an
    equality check: a module cannot satisfy both by re-typing the numbers.
    """

    module = importlib.import_module(module_name)
    names = {name for name in vars(module) if name.startswith("EXIT_")}
    assert names, f"{module_name} names no exit code"
    for name in names:
        assert getattr(module, name) is getattr(_refusal, name)


@pytest.mark.parametrize("module_name", sorted(_refusal.OWN_EXIT_VOCABULARY))
def test_the_exempt_modules_are_real_and_in_the_menu(module_name: str) -> None:
    """An exemption for a tool that left the menu is an exemption to delete."""

    assert module_name in _menu.TUNING_TOOL_MODULES


@pytest.mark.parametrize("fields", [
    {}, {"code": "measurement_candidate_speaker_mismatch"},
    {"next_action": {"id": "apply_matching_room_layer"}},
    {"code": "measurement_candidate_speaker_mismatch",
     "next_action": {"id": "apply_matching_room_layer"}},
])
@pytest.mark.parametrize(("code", "status"), sorted(_refusal.STATUS_BY_CODE.items()))
def test_the_record_status_and_the_exit_code_always_agree(code, status, fields, capsys):
    assert _refusal.failed(code, "a_slug", {}, **fields) == code
    assert json.loads(capsys.readouterr().out) == {
        "status": status, "reason": "a_slug", "detail": {}, **fields,
    }


def test_the_failing_codes_are_exactly_one_two_three() -> None:
    """A fourth failure word would need a fourth number, and there is none."""

    assert _refusal.EXIT_OK == 0
    assert sorted(_refusal.STATUS_BY_CODE) == [1, 2, 3]


def _basic_profile_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(WizardClient, "open", lambda *a, **kw: (0, "unavailable"))
    return ["review", "--hostname", "jts.local"]


def _audition_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The one roster tool with no refusable INPUT: what it declines is the
    speaker's own state, so the state is what this sets. The refusal lands
    before the writer lock, so nothing reaches CamillaDSP."""

    from jasper.active_speaker import baseline_profile

    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: None
    )
    return ["start"]


def _mic_calibration_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The household record is the input here, so its absence is the refusal:
    the door declines to show a mic nothing has registered."""

    monkeypatch.setenv(
        "JASPER_CORRECTION_HOUSEHOLD_MIC_PATH", str(tmp_path / "absent.json")
    )
    return ["show"]


#: One invocation per tool that PASSES argparse and reaches the tool, and that
#: the tool must decline: a round, bundle or spec that is not there, a program
#: nobody ships, a coordinate off the walk's grid, an unavailable door. Argparse's own usage errors are deliberately absent -- the
#: parser exits before the tool can publish anything. None of these touches
#: hardware or the network, and none reaches a measurement door.
_REFUSING_ARGV: dict[str, Callable[[Path, pytest.MonkeyPatch], list[str]]] = {
    "jasper.cli.basic_profile": _basic_profile_argv,
    "jasper.cli.mic_calibration": _mic_calibration_argv,
    "jasper.cli.seat_level": lambda tmp, mp: [
        "--mic-serial", "no-such-serial",
    ],
    "jasper.cli.angle_capture": lambda tmp, mp: [
        "serve", "--attest-rig-clear", "--hostname", "jts.local", "--settle-s", "-1",
    ],
    "jasper.cli.crossover_prescriber": lambda tmp, mp: [
        "contract", "--round", str(tmp / "absent-round"),
    ],
    "jasper.cli.round": lambda tmp, mp: [
        "run", "--poses", "not-a-layout",
    ],
    "jasper.cli.round_views": lambda tmp, mp: ["entry", str(tmp / "absent-round")],
    "jasper.cli.audition": _audition_argv,
}


@pytest.mark.parametrize("module_name", SHARED_RULE)
def test_every_tuning_cli_publishes_the_shared_refusal_document(
    module_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every CLI emits status, reason and detail, with optional code and next_action."""

    module = importlib.import_module(module_name)

    code = module.main(_REFUSING_ARGV[module_name](tmp_path, monkeypatch))

    printed = capsys.readouterr()
    assert code in _refusal.STATUS_BY_CODE
    document = json.loads(printed.out)
    required = {"status", "reason", "detail"}
    assert required <= document.keys() <= required | {"code", "next_action"}
    assert document["status"] == _refusal.STATUS_BY_CODE[code]
    assert printed.err.startswith(
        f"{document['status']} ({document['reason']}): "
    )


@pytest.mark.parametrize("module_name,verb", [
    ("jasper.cli.crossover_prescriber", "judge"),
    ("jasper.cli.crossover_prescriber", "compose"),
    ("jasper.cli.round", "reset"),
])
def test_document_failures_use_the_shared_contract(module_name, verb, tmp_path, monkeypatch, capsys):
    error = prescription_document.PrescriptionDocumentRefused(
        "bass_fit_inputs_missing", "bass", "missing takes", evidence={"round_id": "round-1"})
    def refuse(*args, **kwargs):
        raise error
    monkeypatch.setattr(prescription_document, "saved_base", refuse)
    path = tmp_path / "document.json"
    path.write_text(json.dumps({"kind": "jts_prescription", "schema": 1, "base": "saved",
                                "sections": {}, "rationale": "Test a refusal."}))
    module = importlib.import_module(module_name)
    if verb != "reset":
        monkeypatch.setattr(module, "saved_base", refuse)
    assert module.main([verb, *([] if verb == "reset" else [str(path)])]) == _refusal.EXIT_REFUSED
    printed = capsys.readouterr()
    assert json.loads(printed.out) == {
        "status": "refused", "reason": error.code, "code": error.code,
        "detail": {"section": error.section, "error": error.error, "evidence": error.evidence},
        "next_action": refusal_copy_for(error.code)[1],
    }
    assert printed.err


#: The ceiling on a numeric array an answer may carry: a curve or a grid
#: belongs in the artifact the answer names, never in the answer.
MAX_ANSWER_ARRAY = 16

_NO_CAPTURES = "the fixture banks no WAVs, so no capture ring and no summed takes"


class _FixtureRound(NamedTuple):
    """The two rounds ``tests/crossover_v2_banked_round`` banks -- stage 1's
    solos, ladder and entry baseline, stage 2's VERIFY sum -- and the bundle
    inside the first, which the bundle-taking verbs read instead of the tree."""

    measured: Path
    verified: Path
    bundle: Path
    seat: Path


def _fixture_round(root: Path) -> _FixtureRound:
    measured = bank_measure_round(root, candidates=("cand-a", "cand-b"))
    bundle, = (measured / "bundle").iterdir()
    return _FixtureRound(
        measured=measured, verified=bank_verify_round(root), bundle=bundle,
        seat=bank_seat_round(root),
    )


def _room_grade_argv(round_: _FixtureRound) -> list[str]:
    write_room_median(round_.measured)
    return ["room-grade", str(round_.measured)]


def _directivity_argv(round_: _FixtureRound) -> list[str]:
    takes = [directivity_take(index, pose, level) for index, (pose, level) in enumerate(BASELINE)]
    write_manifest(round_.measured, groups=[{"set_id": "woofer", "capture_basis": {"role": "woofer"}, "takes": takes}])
    return ["directivity", str(round_.measured), "--set", "woofer"]


def _repeat_argv(round_: _FixtureRound) -> list[str]:
    for root in (round_.measured, round_.verified):
        write_manifest(root, groups=[{"set_id": root.name, "capture_basis": {"role": "woofer"},
                                      "takes": [mark_take(f"{root.name}-{i}", 0.5 * i) for i in range(2)]}])
    return ["repeat", str(round_.measured), str(round_.verified)]


def _on_fixture_round(argv: Callable[[_FixtureRound], list[str]]) -> Callable[[pytest.FixtureRequest, Path], list[str]]:
    return lambda request, root: argv(_fixture_round(root))


def _sweep_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    impulse = np.zeros(1800)
    impulse[100] = 1.0
    return ["sweep", str(bank_capture_round(root / "capture", [impulse] * 3)), "--scope", "round"]


def _kept_take_argv(view: str) -> Callable[[pytest.FixtureRequest, Path], list[str]]:
    def argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
        impulse = np.random.default_rng(0).normal(0.0, 1e-4, 36_000)
        impulse[12_150] += 1.0
        bundle, doc = bank_kept_impulse_take(root, request.getfixturevalue("monkeypatch"), impulse)
        return [view, str(bundle), "--take", doc["take_id"]]
    return argv


def _compare_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    impulse = np.random.default_rng(0).normal(0.0, 1e-4, 36_000)
    impulse[12_150] += 1.0
    monkeypatch = request.getfixturevalue("monkeypatch")
    a_bundle, a_doc = bank_kept_impulse_take(root / "a", monkeypatch, impulse)
    b_bundle, b_doc = bank_kept_impulse_take(root / "b", monkeypatch, 2 * impulse)
    return ["compare", str(a_bundle), str(b_bundle), "--a-take", a_doc["take_id"], "--b-take", b_doc["take_id"]]


def _close_reference_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    rounds = (capture_round(root / "far", 1.0), capture_round(root / "close", 0.30, take_ids=("verify_02_a01",)))
    return close_compare_argv(rounds, root / "close_reference.json")


def _dsp_levels_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    rate = 48000
    tone = np.sin(2 * np.pi * 60 * np.arange(rate) / rate)
    raw = root / "output.f64le"
    np.column_stack([tone, tone / 2]).astype("<f8").tofile(raw)
    manifest = root / "dsp_replay.json"
    manifest.write_text(json.dumps({
        "schema": DSP_REPLAY_SCHEMA, "render": {"output_sha256": sha256_file(raw)}, "sample_rate_hz": rate,
        "channels": 2, "graph_sha256": "graph", "stimulus_sha256": "stimulus", "main_db": -20, "bass_reference_db": -20,
    }))
    return ["dsp-levels", str(manifest), "--raw", str(raw), "--window-s", "0", "1"]


def _nearfield_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    bundle = root / "sessions" / "nearfield"
    bundle.mkdir(parents=True)
    (bundle / "info.json").write_text(json.dumps({"session_id": bundle.name}))
    takes = [nearfield_take("w15", "woofer", 15, 90.0), nearfield_take("w30", "woofer", 30, 87.9, seed=1)]
    write_manifest(bundle, program="nearfield/woofer", groups=[{"set_id": "nearfield", "capture_basis": {}, "takes": takes}])
    return ["nearfield", str(bundle)]


def _bass_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    bundle, _, _, bank = request.getfixturevalue("summed_capture_bundle")
    path = asyncio.run(bank("baseline"))
    record = json.loads((bundle / EVIDENCE_ROOT / "artifacts" / path).read_text())
    write_manifest(bundle, program="bass", groups=[manifest_set([(path, record)], set_id="bass")])
    return ["bass", str(bundle), "--set", "bass"]


def _bass_run_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    run = request.getfixturevalue("bass_run")
    run.write()
    if request.param == "bass-fit-table":
        return run.argv
    return ["bass-compare", str(run.roots[0]), str(run.roots[0]),
            "--before-set", "set-0", "--after-set", "set-1", "--change", "candidate"]


def _distortion_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    return ["distortion", str(bank_measure_capture(request.getfixturevalue("harmonic_capture"), root))]


def _classify_argv(request: pytest.FixtureRequest, root: Path) -> list[str]:
    bundle, _ = feature_bundle(root, resonant_ir(+3.0))
    return ["classify-features", str(bundle)]


def _applied_calibration(basis: Mapping[str, Any]) -> Any:
    calibration = basis.get("capture_calibration") or {}
    return calibration.get("calibration_id") if calibration.get("applied") else None


class _ViewRun(NamedTuple):
    """How one view is run to an answer; the parameters it declares; the ids
    its subject names, per round for a view that compares rounds; and whether
    one parameter equals the artifact's own record of it."""

    argv: Callable[[pytest.FixtureRequest, Path], list[str]]
    parameters: frozenset[str] = frozenset()
    subject: frozenset[str] = frozenset({"round_id"})
    recorded: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | None = None


_ROUND_SET_TAKES = frozenset({"round_id", "set_id", "take_ids"})

#: How each view is run to an answer -- or, for a view no fixture here can
#: feed, why not.
_VIEW_RUN: dict[str, str | _ViewRun] = {
    "entry": _ViewRun(
        _on_fixture_round(lambda r: ["entry", str(r.measured)]),
        frozenset({"smoothing_fraction", "band_hz", "reference_band_hz"}),
        recorded=lambda p, a: p["smoothing_fraction"] == a["report"]["smoothing_fraction"]),
    "repeat": _ViewRun(_on_fixture_round(_repeat_argv)),
    "candidates": _ViewRun(_on_fixture_round(lambda r: ["candidates", str(r.measured)])),
    "directivity": _ViewRun(
        _on_fixture_round(_directivity_argv),
        frozenset({"reference_pose", "ladder", "smoothing_fraction", "band_hz", "grid", "calibration_id"}),
        _ROUND_SET_TAKES, lambda p, a: p["band_hz"] == a["parameters"]["band_hz"]),
    "speaker-fit": "answer-only fit inputs are covered in test_round_views_speaker_fit",
    "sweep": _ViewRun(
        _sweep_argv, frozenset({"rungs_ms", "smoothing_fraction", "at_hz", "role"}), frozenset({"round_id", "take_ids"}),
        lambda p, a: p["rungs_ms"] == a["frame"]["rungs_ms"]),
    "impulse": _ViewRun(
        _kept_take_argv("impulse"),
        frozenset({"role", "impulse_source", "time_reference", "calibration_applied", "span_ms",
                   "onset_below_peak_db", "noise_before_onset_ms", "etc_span_fraction"}),
        frozenset({"set_id", "take_ids", "candidate_id"}),
        lambda p, a: p["span_ms"] == a["parameters"]["span_ms"]),
    "group-delay": _ViewRun(
        _kept_take_argv("group-delay"),
        frozenset({"role", "impulse_source", "time_reference", "calibration_applied", "window_ms",
                   "window_source", "lead_ms", "band_hz", "points_per_octave", "slope_span_octave"}),
        frozenset({"set_id", "take_ids", "candidate_id"}),
        lambda p, a: p["band_hz"] == a["parameters"]["band_hz"]),
    "decay": _ViewRun(
        _kept_take_argv("decay"),
        frozenset({"role", "impulse_source", "time_reference", "calibration_applied", "band_filter",
                   "figure_ranges_db", "noise_margin_db", "envelope_ms", "noise_tail_fraction"}),
        frozenset({"set_id", "take_ids", "candidate_id"}),
        lambda p, a: p["figure_ranges_db"] == a["parameters"]["figure_ranges_db"]),
    "compare": _ViewRun(
        _compare_argv,
        frozenset({"roles", "window_ms", "window_source", "lead_ms", "smoothing_fraction", "points_per_octave",
                   "band_hz", "level_removed", "calibration_applied"}),
        frozenset({"set_id", "take_ids", "candidate_id"}),
        lambda p, a: p["window_ms"] == a["parameters"]["window_ms"]),
    "frequency": _ViewRun(
        _on_fixture_round(lambda r: ["frequency", str(r.measured)]),
        frozenset({"ref_band_hz", "normalize", "analyze_wavs", "reference_db"}),
        recorded=lambda p, a: all(curve["plot"]["ref_band_hz"] == p["ref_band_hz"]
                                  for run in a["runs"] for curve in run["series"])),
    "distortion": _ViewRun(
        _distortion_argv, frozenset({"band_hz", "setup_calibration_id"}), frozenset({"round_id", "take_ids"}),
        lambda p, a: all(block["sweep"]["read_band_hz"] in p["band_hz"][block["role"]] for block in a["roles"])),
    "bass": _ViewRun(
        _bass_argv, frozenset({"calibration_id"}), frozenset({"set_id", "candidate_id"}),
        lambda p, a: p["calibration_id"] == a["takes"][0]["calibration"]["calibration_id"]),
    "bass-compare": _ViewRun(
        _bass_run_argv, frozenset({"change"}), _ROUND_SET_TAKES | {"candidate_id"},
        lambda p, a: p["change"] == a["change"]),
    "bass-fit-table": _ViewRun(
        _bass_run_argv, frozenset({"reference_band_hz"}),
        recorded=lambda p, a: all(table["reference_band_hz"] == p["reference_band_hz"] for table in a["tables"])),
    "rear": "a banked rear batch is covered in test_round_views_rear_cli",
    "dsp-replay": "rendering needs the native DSP binary",
    "dsp-levels": _ViewRun(
        _dsp_levels_argv, frozenset({"window_s"}), frozenset(), lambda p, a: p["window_s"] == a["window_s"]),
    "classify-features": _ViewRun(
        _classify_argv, frozenset({"window_ms", "rungs_ms", "smoothing_fraction", "at_hz"}), frozenset(),
        lambda p, a: p["window_ms"] == a["measurement"]["gate_ms_primary"]),
    "room-grade": _ViewRun(
        _on_fixture_round(_room_grade_argv), frozenset({"calibration_id"}), frozenset({"round_id", "set_id"}),
        lambda p, a: p["calibration_id"] == _applied_calibration((a["evidence"] or {}).get("basis") or {})),
    "close-reference": _ViewRun(
        _close_reference_argv, frozenset({"window_ms", "smoothing_fraction", "far_m", "close_m", "fc_hz", "at_hz"}),
        frozenset({"round_id", "take_ids"}),
        lambda p, a: p["window_ms"]["far"] == a["close_reference"]["frame"]["far_gate_ms"]),
    "room": _ViewRun(
        _on_fixture_round(lambda r: ["room", str(r.seat)]), frozenset({"calibration_id"}),
        _ROUND_SET_TAKES | {"candidate_id"},
        lambda p, a: p["calibration_id"] == _applied_calibration(a["median"]["evidence"]["basis"])),
    "delay-landscape": _ViewRun(
        _on_fixture_round(lambda r: ["delay-landscape", str(r.bundle), "--fc-hz", "1800"]),
        frozenset({"fc_hz", "step_us", "path_difference_m", "inverted_role"}), frozenset({"round_id", "take_ids"}),
        lambda p, a: p["step_us"] == a["landscape"]["spec"]["step_us"]),
    "inventory": _ViewRun(_on_fixture_round(lambda r: ["inventory", str(r.measured)])),
    "nearfield": _ViewRun(
        _nearfield_argv, frozenset({"ladder", "trusted_snr_db", "step_band_hz", "step_tolerance_db"}),
        frozenset({"take_ids"}),
        lambda p, a: p["step_tolerance_db"] == a["parameters"]["step_tolerance_db"]),
}


def _numeric_arrays(node: Any) -> Iterator[list[Any]]:
    """Every array carrying numbers in a document, at any depth.

    ANY number makes an array one, not every: a curve with a null at an
    excluded bin is the same curve, and a rule keyed on "all of them" would
    let exactly the longest ones through.
    """

    if isinstance(node, Mapping):
        for value in node.values():
            yield from _numeric_arrays(value)
    elif isinstance(node, list):
        if any(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in node
        ):
            yield node
        for item in node:
            yield from _numeric_arrays(item)


class _Answered(NamedTuple):
    view: str
    code: int
    answer: dict[str, Any]
    artifact: dict[str, Any]
    artifact_bytes: int


#: Each view's one run, shared by the three tests below. It happens inside the
#: first of them to ask, under that test's own host isolation.
_ANSWERED: dict[str, _Answered] = {}


@pytest.fixture(params=_menu._subcommand_names(round_views.build_parser()))
def view_answer(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> _Answered:
    """Each registered view run once to an answer, with the artifact it names."""

    run = _VIEW_RUN[request.param]
    if isinstance(run, str):
        pytest.skip(run)
    if request.param not in _ANSWERED:
        # A view of a LIVE session bundle lands beside the CALLER, so the
        # caller stands in the temporary directory.
        monkeypatch.chdir(tmp_path)
        code = round_views.main(run.argv(request, tmp_path))
        answer = json.loads(capsys.readouterr().out)
        written = Path(answer["out"])
        _ANSWERED[request.param] = _Answered(
            request.param, code, answer, json.loads(written.read_text()), written.stat().st_size,
        )
    return _ANSWERED[request.param]


def test_a_view_that_succeeds_prints_one_bounded_answer(view_answer: _Answered) -> None:
    """A success is an ANSWER: one document, and the artifact named rather
    than poured onto the operator's terminal."""

    assert view_answer.code == _refusal.EXIT_OK
    assert max((len(a) for a in _numeric_arrays(view_answer.answer)), default=0) <= MAX_ANSWER_ARRAY
    assert view_answer.artifact_bytes == view_answer.answer["bytes"]


def test_every_view_answers_under_one_envelope(view_answer: _Answered) -> None:
    """The answer names its view, the schema its artifact carries too, the ids
    of what it read, and exactly the parameters it declares, one of them equal
    to the artifact's own record of it."""

    run = _VIEW_RUN[view_answer.view]
    assert isinstance(run, _ViewRun)
    answer = view_answer.answer
    assert answer["view"] == view_answer.view
    assert answer["schema"] == view_answer.artifact["schema"]
    subject = answer["subject"]
    assert [set(one) for one in subject.get("rounds", [subject])] == [run.subject] * len(subject.get("rounds", [subject]))
    assert set(answer["parameters"]) == run.parameters
    assert run.recorded is None or run.recorded(answer["parameters"], view_answer.artifact)


def test_no_success_answer_or_artifact_carries_the_failure_status(view_answer: _Answered) -> None:
    """``status`` is how a failure is recognised (ADR-0237)."""

    assert "status" not in view_answer.answer
    assert "status" not in view_answer.artifact
