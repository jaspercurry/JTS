# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The CI routing policy: the fail-closed lane predicate and its registries.

This is the file `scripts/ci-classify.py --routing-policy-pytest-targets`
names, so it runs in the workflow's `python-policy` preflight and in
`scripts/test-fast` before any broader work.
"""

from __future__ import annotations

import ast
import functools
import html
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "tests.yml"


def _load_classifier():
    spec = importlib.util.spec_from_file_location(
        "ci_classifier", ROOT / "scripts" / "ci-classify.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_classifier = _load_classifier()


def _changes(*paths: str, status: str = "M") -> tuple:
    return tuple(ci_classifier.Change(status, (path,)) for path in paths)


# ---------------------------------------------------------------- lane policy

_PAGE = "deploy/index.html"
_DOC = "docs/HANDOFF-aec.md"
_POLICY_TEST = "tests/test_ci_classifier.py"
# The calibration-agent corpus is a package resource, not prose (#2981), so
# nothing under jasper/ is a docs subject however it is spelled.
_CORPUS = "jasper/calibration_agent/corpus/README.md"


@pytest.mark.parametrize(
    ("expected_lane", "event_name", "paths"),
    [
        # fast-landing: the page, alone or with registered companions only.
        ("fast-landing", "pull_request", (_PAGE,)),
        ("fast-landing", "pull_request", (_PAGE, "tests/test_landing_page_html.py")),
        ("fast-landing", "pull_request", (_PAGE, *ci_classifier.LANDING_TEST_FILES)),
        # docs: a prose subject, alone or with registered companions only.
        ("docs", "pull_request", (_DOC,)),
        ("docs", "pull_request", ("docs/bass-extension-waves/protocol.md",)),
        ("docs", "pull_request", ("README.md", "AGENTS.md", "CONTRIBUTING.md")),
        ("docs", "pull_request", ("CHANGELOG.md", "CODE_OF_CONDUCT.md")),
        ("docs", "pull_request", (".github/PULL_REQUEST_TEMPLATE.md",)),
        ("docs", "pull_request", ("docs/doc-map.toml",)),
        ("docs", "pull_request", (_DOC, "tests/test_docs_impact.py")),
        (
            "docs",
            "pull_request",
            ("README.md", *sorted(ci_classifier.DOCS_COMPANION_TEST_FILES)),
        ),
        # full: no subject, mixed subjects, or any unregistered companion.
        ("full", "pull_request", ()),
        ("full", "pull_request", ("mystery/new-surface.txt",)),
        ("full", "pull_request", ("tests/test_landing_page_html.py",)),
        ("full", "pull_request", ("tests/test_docs_impact.py",)),
        ("full", "pull_request", (_PAGE, "README.md")),
        ("full", "pull_request", (_PAGE, "jasper/control/server.py")),
        ("full", "pull_request", (_PAGE, "mystery/new-surface.txt")),
        ("full", "pull_request", (_PAGE, "pyproject.toml")),
        ("full", "pull_request", (_PAGE, ".github/workflows/tests.yml")),
        ("full", "pull_request", (_PAGE, "scripts/ci-classify.py")),
        ("full", "pull_request", (_DOC, "jasper/control/server.py")),
        ("full", "pull_request", (_DOC, "pyproject.toml")),
        ("full", "pull_request", (_DOC, ".github/workflows/tests.yml")),
        ("full", "pull_request", (_DOC, "scripts/ci-classify.py")),
        ("full", "pull_request", (_DOC, "deploy/install.sh")),
        ("full", "pull_request", (_DOC, "rust/jasper-fanin/src/main.rs")),
        ("full", "pull_request", (_DOC, "docs/assets/diagram.png")),
        ("full", "pull_request", (_DOC, "tests/test_mux.py")),
        # The registration guard is a bundle member, never a companion.
        ("full", "pull_request", (_DOC, _POLICY_TEST)),
        # Markdown outside the prose trees is not a subject.
        ("full", "pull_request", (_CORPUS,)),
        ("full", "pull_request", (_DOC, _CORPUS)),
        ("full", "pull_request", ("jasper/README.md",)),
        # Non-PR events never take a narrow lane.
        ("full", "push", ()),
        ("full", "workflow_dispatch", ()),
        ("full", "", ()),
    ],
)
def test_lane_decision_table(
    expected_lane: str, event_name: str, paths: tuple[str, ...]
) -> None:
    assert ci_classifier.classify(event_name, _changes(*paths)).lane == expected_lane


@pytest.mark.parametrize(
    ("status", "paths", "expected_lane"),
    [
        ("D", ("deploy/index.html",), "full"),
        # A docs-only delete/rename is still docs-lane-safe (#4036).
        ("D", ("docs/HANDOFF-aec.md",), "docs"),
        ("D", ("README.md",), "docs"),
        ("T", ("deploy/index.html",), "full"),
        ("U", ("deploy/index.html",), "full"),
        ("X", ("README.md",), "full"),
        ("R100", ("deploy/old-index.html", "deploy/index.html"), "full"),
        ("R100", ("docs/HANDOFF-old.md", "docs/HANDOFF-new.md"), "docs"),
        # A rename whose content also changed (similarity < 100%) is gated
        # the same as a pure rename.
        ("R087", ("docs/HANDOFF-old.md", "docs/HANDOFF-changed.md"), "docs"),
        ("R100", (_DOC, "jasper/control/server.py"), "full"),
        ("C100", ("deploy/source.html", "deploy/index.html"), "full"),
    ],
)
def test_change_status_gates_narrow_lane_eligibility(
    status: str, paths: tuple[str, ...], expected_lane: str
) -> None:
    change = ci_classifier.Change(status, paths)
    assert ci_classifier.classify("pull_request", (change,)).lane == expected_lane


@pytest.mark.parametrize(
    ("event_name", "base", "head", "runner_fails"),
    [
        ("pull_request", "base", "head", True),
        ("pull_request", "", "", False),
        ("pull_request", "base", "", False),
        ("push", "", "", False),
        ("workflow_dispatch", "base", "head", False),
    ],
)
def test_an_unusable_diff_comparison_falls_back_to_full(
    event_name: str, base: str, head: str, runner_fails: bool
) -> None:
    def runner(*args, **kwargs):
        if runner_fails:
            raise subprocess.CalledProcessError(128, args[0])
        raise AssertionError("no git comparison should have been attempted")

    decision = ci_classifier.decision_from_git(event_name, base, head, runner=runner)
    assert decision.lane == "full"


def test_name_status_parser_keeps_rename_sources() -> None:
    payload = (
        b"M\0deploy/index.html\0"
        b"D\0tests/old.py\0"
        b"R100\0tests/before.py\0tests/after.py\0"
    )

    assert ci_classifier.parse_name_status_z(payload) == (
        ci_classifier.Change("M", ("deploy/index.html",)),
        ci_classifier.Change("D", ("tests/old.py",)),
        ci_classifier.Change("R100", ("tests/before.py", "tests/after.py")),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"M\0/absolute/path\0",
        b"M\0deploy/index.html\nforged-summary\0",
        b"M\0deploy/index.html\x1b[31m\0",
        b"M\x1b\0deploy/index.html\0",
        b"M\0\xff\0",
        b"M\0\0",
        b"R100\0only-one-path\0",
        # An empty status is ascii and printable, so it reaches Change unless
        # the parser rejects it explicitly.
        b"\0README.md\0",
    ],
)
def test_name_status_parser_rejects_unsafe_or_malformed_records(
    payload: bytes,
) -> None:
    with pytest.raises(ci_classifier.ChangedFileError):
        ci_classifier.parse_name_status_z(payload)


def test_summary_escapes_markup_carried_by_a_changed_path() -> None:
    hostile = 'docs/<img src="x" onerror=alert(1)>.md'

    summary = ci_classifier.render_summary(
        ci_classifier.classify("pull_request", _changes("deploy/index.html", hostile))
    )

    assert html.escape(hostile) in summary
    assert hostile not in summary


# ------------------------------------------------------------- bundle guards
#
# The registries are only as good as the discovery they are checked against:
# a test that READS a document (or the landing page) must be in the matching
# bundle, and a bundle entry that discovery cannot see must carry a reason.

_READ_CALLS = frozenset({
    "Path",
    "PurePath",
    "PurePosixPath",
    "absolute",
    "join",
    "joinpath",
    "open",
    "read_bytes",
    "read_text",
    "resolve",
})
# A doc reader is as likely to sweep a directory as to name one file.
_DOC_READ_CALLS = _READ_CALLS | {"glob", "rglob"}


def _path_parts(node: ast.AST, calls: frozenset[str]) -> tuple[str, ...]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (*_path_parts(node.left, calls), *_path_parts(node.right, calls))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tuple(part for part in node.value.split("/") if part)
    if not isinstance(node, ast.Call):
        return ()
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    if name not in calls:
        return ()
    receiver = _path_parts(func.value, calls) if isinstance(func, ast.Attribute) else ()
    return (
        *receiver,
        *(part for argument in node.args for part in _path_parts(argument, calls)),
    )


@functools.lru_cache(maxsize=None)
def _parsed_tests(tests_root: Path) -> tuple[tuple[str, tuple[ast.AST, ...]], ...]:
    """Parse the tree once; three scans share it."""

    return tuple(
        (
            str(path.relative_to(tests_root.parent)),
            tuple(
                node
                for node in ast.walk(
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                )
                if isinstance(node, (ast.BinOp, ast.Call))
            ),
        )
        for path in sorted(tests_root.rglob("test_*.py"))
    )


def _scan(
    reads: Callable[[tuple[str, ...]], bool],
    calls: frozenset[str],
    tests_root: Path = ROOT / "tests",
) -> tuple[str, ...]:
    return tuple(
        relpath
        for relpath, nodes in _parsed_tests(tests_root)
        if any(reads(_path_parts(node, calls)) for node in nodes)
    )


def _reads_landing(parts: tuple[str, ...]) -> bool:
    return parts[-2:] == ("deploy", "index.html")


def _reads_a_document(parts: tuple[str, ...]) -> bool:
    if not parts:
        return False
    return parts[-1].endswith(".md") or parts[-2:] == ("docs", "doc-map.toml")


@pytest.mark.parametrize(
    ("body", "landing", "document"),
    [
        ('(ROOT / "deploy" / "index.html").read_text()', True, False),
        ('ROOT.joinpath("deploy", "index.html").open()', True, False),
        ('open(ROOT / Path("deploy/index.html"))', True, False),
        ('(ROOT / "docs" / "HANDOFF-x.md").read_text()', False, True),
        ('list((ROOT / "docs").rglob("HANDOFF-*.md"))', False, True),
        ('(ROOT / "docs" / "doc-map.toml").read_text()', False, True),
        ('NOTE = "deploy/index.html"', False, False),
        ('NOTE = "docs/HANDOFF-x.md"', False, False),
    ],
)
def test_discovery_sees_reads_and_ignores_mentions(
    tmp_path: Path, body: str, landing: bool, document: bool
) -> None:
    nested = tmp_path / "tests" / "web"
    nested.mkdir(parents=True)
    (nested / "test_probe.py").write_text(
        f'from pathlib import Path\nROOT = Path("/repo")\ndef test_x():\n    {body}\n',
        encoding="utf-8",
    )
    tests_root = tmp_path / "tests"

    assert bool(_scan(_reads_landing, _READ_CALLS, tests_root)) is landing
    assert bool(_scan(_reads_a_document, _DOC_READ_CALLS, tests_root)) is document


def test_landing_bundle_is_exactly_the_tests_that_read_the_landing_page() -> None:
    files = ci_classifier.LANDING_TEST_FILES

    assert _scan(_reads_landing, _READ_CALLS) == files
    # Plus exactly one function-scoped node id (an install contract), which
    # scripts/test-fast splits on `::` before its existence check.
    assert ci_classifier.LANDING_PYTEST_TARGETS[: len(files)] == files
    extra = ci_classifier.LANDING_PYTEST_TARGETS[len(files) :]
    assert len(extra) == 1 and "::" in extra[0]


def test_docs_bundle_registers_every_discoverable_doc_reading_test() -> None:
    discovered = set(_scan(_reads_a_document, _DOC_READ_CALLS))

    missing = sorted(discovered - set(ci_classifier.DOCS_TEST_FILES))

    assert not missing, missing


def test_hand_registered_readers_are_exactly_what_discovery_cannot_see() -> None:
    """Set EQUALITY, both directions, so neither side can rot silently.

    An undiscoverable entry cannot be added with no recorded reason, and a
    note cannot outlive the refactor that made its entry discoverable.
    """

    discovered = set(_scan(_reads_a_document, _DOC_READ_CALLS))
    registered = set(ci_classifier.DOCS_TEST_FILES)
    documented = set(ci_classifier.DOCS_HAND_REGISTERED_READERS)

    assert registered - discovered == documented, {
        "undocumented": sorted((registered - discovered) - documented),
        "now discoverable (delete the note)": sorted(
            documented - (registered - discovered)
        ),
    }
    assert all(
        reason.strip()
        for reason in ci_classifier.DOCS_HAND_REGISTERED_READERS.values()
    )


def test_docs_bundle_is_sorted_unique_and_present_on_disk() -> None:
    assert list(ci_classifier.DOCS_TEST_FILES) == sorted(
        set(ci_classifier.DOCS_TEST_FILES)
    )
    for path in ci_classifier.DOCS_TEST_FILES:
        assert (ROOT / path).is_file(), path


def test_the_registration_guard_is_a_bundle_member_not_a_companion() -> None:
    policy = "tests/test_ci_classifier.py"

    assert ci_classifier.ROUTING_POLICY_PYTEST_TARGETS == (policy,)
    assert policy in ci_classifier.DOCS_TEST_FILES
    assert policy not in ci_classifier.DOCS_COMPANION_TEST_FILES
    assert not ci_classifier.is_docs_lane_path(policy)


def test_the_prose_registries_still_name_things_that_exist() -> None:
    """The rglob below also keeps THIS file discoverable as a doc reader,
    which its own bundle registration depends on."""

    for path in ci_classifier.DOCS_PROSE_FILES:
        assert (ROOT / path).is_file(), path
    assert (ROOT / "docs" / "doc-map.toml").is_file()
    assert list((ROOT / "docs").rglob("*.md")), "no prose left under docs/"


def test_cli_target_flags_print_their_registry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """scripts/test-fast and the workflow consume these three flags verbatim."""

    for flag, targets in (
        ("--landing-pytest-targets", ci_classifier.LANDING_PYTEST_TARGETS),
        ("--docs-pytest-targets", ci_classifier.DOCS_TEST_FILES),
        (
            "--routing-policy-pytest-targets",
            ci_classifier.ROUTING_POLICY_PYTEST_TARGETS,
        ),
    ):
        assert ci_classifier.main([flag]) == 0
        assert capsys.readouterr().out.splitlines() == list(targets)


# ------------------------------------------------------------------- workflow


def test_workflow_keeps_one_fail_closed_required_aggregate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    jobs = parsed["jobs"]
    triggers = parsed.get("on", parsed.get(True))

    # An unfiltered trigger: a workflow-level `paths:`/`paths-ignore:` skip
    # leaves the required check Pending forever, while a job skipped by
    # conditional reports success.
    assert triggers["pull_request"] is None
    assert triggers["push"] == {"branches": ["main"]}

    # Campaign speed measure during the 2026-08 right-sizing refactor:
    # deployed interpreter only. Restoring the fan-out is an owner call.
    assert 'python-version: ["3.13"]' in workflow
    for flag in ci_classifier.TARGET_REGISTRIES:
        assert f"python3 scripts/ci-classify.py --{flag}" in workflow

    # Branch protection requires the check named `ci`, and it must be able to
    # observe every conditional lane and preflight.
    assert jobs["ci"]["name"] == "ci"
    assert jobs["ci"]["if"] == "${{ always() }}"
    assert jobs["ci"]["needs"] == [
        "classify",
        "fast-landing",
        "docs",
        "shell",
        "python-policy",
        "pytest-matrix",
        "pytest",
        "js",
        "rust",
    ]
    for job in ("fast-landing", "docs", "shell", "python-policy", "js", "rust"):
        assert jobs[job]["needs"] == "classify", job
        assert "needs.classify.outputs.lane ==" in jobs[job]["if"], job
    assert jobs["pytest-matrix"]["needs"] == ["classify", "python-policy"]
    assert "needs.classify.outputs.lane ==" in jobs["pytest-matrix"]["if"]
    assert jobs["pytest"]["needs"] == ["classify", "python-policy", "pytest-matrix"]


def test_every_workflow_run_script_parses() -> None:
    """An embedded preflight must parse before it can protect anything."""

    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    scripts = [
        step["run"]
        for job in parsed["jobs"].values()
        for step in job.get("steps", ())
        if "run" in step
    ]
    assert scripts

    for script in scripts:
        result = subprocess.run(
            ["bash", "-n"],
            input=script,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr


def test_every_workflow_cancels_superseded_pull_request_runs() -> None:
    """A superseded run must not keep occupying a finite runner slot.

    Asserted as an invariant, not an exact expression: `main` pushes must run
    to completion, so cancellation stays scoped to pull requests.
    """

    workflows = sorted(
        path
        for suffix in ("*.yml", "*.yaml")
        for path in (ROOT / ".github" / "workflows").glob(suffix)
    )
    assert workflows

    for path in workflows:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        concurrency = parsed.get("concurrency")
        assert concurrency, f"{path.name} has no concurrency group"

        group = concurrency["group"]
        assert "github.workflow" in group, path.name
        assert "github.ref" in group, path.name

        cancel = str(concurrency.get("cancel-in-progress", ""))
        if "pull_request" in (parsed.get("on", parsed.get(True)) or {}):
            assert "pull_request" in cancel, path.name
            assert cancel not in ("True", "true"), path.name


# The aggregate's own result table: the one required check must fail closed on
# a failure, a cancellation, or an unexpectedly skipped or started job.
_SKIPPED_FULL_FARM = {
    "SHELL_RESULT": "skipped",
    "PYTHON_POLICY_RESULT": "skipped",
    "PYTEST_MATRIX_RESULT": "skipped",
    "PYTEST_RESULT": "skipped",
    "JS_RESULT": "skipped",
    "RUST_RESULT": "skipped",
}
_AGGREGATE_SHAPES = {
    "full": {
        "CLASSIFY_RESULT": "success",
        "FAST_LANDING_RESULT": "skipped",
        "DOCS_RESULT": "skipped",
        "SHELL_RESULT": "success",
        "PYTHON_POLICY_RESULT": "success",
        "PYTEST_MATRIX_RESULT": "success",
        "PYTEST_RESULT": "success",
        "JS_RESULT": "success",
        "RUST_RESULT": "success",
    },
    "fast-landing": {
        "CLASSIFY_RESULT": "success",
        "FAST_LANDING_RESULT": "success",
        "DOCS_RESULT": "skipped",
        **_SKIPPED_FULL_FARM,
    },
    "docs": {
        "CLASSIFY_RESULT": "success",
        "FAST_LANDING_RESULT": "skipped",
        "DOCS_RESULT": "success",
        **_SKIPPED_FULL_FARM,
    },
}
_JOB_RESULTS = frozenset({
    "",
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "skipped",
    "success",
    "timed_out",
})


def _aggregate_script() -> str:
    parsed = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    [script] = [
        step["run"] for step in parsed["jobs"]["ci"]["steps"] if "run" in step
    ]
    return script


def _run_aggregate(lane: str, **overrides: str) -> int:
    env = {
        **os.environ,
        **_AGGREGATE_SHAPES.get(lane, _AGGREGATE_SHAPES["full"]),
        "LANE": lane,
        **overrides,
    }
    return subprocess.run(
        ["bash", "-c", _aggregate_script()],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).returncode


@pytest.mark.parametrize("lane", sorted(_AGGREGATE_SHAPES))
def test_ci_aggregate_accepts_only_its_lane_complete_result_shape(lane: str) -> None:
    assert _run_aggregate(lane) == 0

    for name, expected in _AGGREGATE_SHAPES[lane].items():
        for mutated in _JOB_RESULTS - {expected}:
            assert _run_aggregate(lane, **{name: mutated}) != 0, (lane, name, mutated)


@pytest.mark.parametrize("lane", ["", "unexpected"])
def test_ci_aggregate_rejects_an_unknown_lane(lane: str) -> None:
    assert _run_aggregate(lane) != 0
