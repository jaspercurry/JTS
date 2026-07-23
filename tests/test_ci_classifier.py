# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CLASSIFIER_PATH = ROOT / "scripts" / "ci-classify.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "tests.yml"


def _load_classifier():
    spec = importlib.util.spec_from_file_location("ci_classifier", CLASSIFIER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_classifier = _load_classifier()


def _changes(*paths: str, status: str = "M"):
    return tuple(ci_classifier.Change(status, (path,)) for path in paths)


@pytest.mark.parametrize(
    ("case", "event_name", "changes", "expected_lane"),
    [
        (
            "pr-1676",
            "pull_request",
            _changes(
                "deploy/index.html",
                "tests/test_landing_page_html.py",
            ),
            "fast-landing",
        ),
        (
            "index-only",
            "pull_request",
            _changes("deploy/index.html"),
            "fast-landing",
        ),
        (
            "all-registered-companions",
            "pull_request",
            _changes(
                "deploy/index.html",
                *ci_classifier.LANDING_TEST_FILES,
            ),
            "fast-landing",
        ),
        (
            "test-only",
            "pull_request",
            _changes("tests/test_landing_page_html.py"),
            "full",
        ),
        (
            "mixed-runtime",
            "pull_request",
            _changes("deploy/index.html", "jasper/control/server.py"),
            "full",
        ),
        (
            "unmatched-file",
            "pull_request",
            _changes("deploy/index.html", "mystery/new-surface.txt"),
            "full",
        ),
        (
            "dependency-config",
            "pull_request",
            _changes("deploy/index.html", "pyproject.toml"),
            "full",
        ),
        (
            "workflow",
            "pull_request",
            _changes("deploy/index.html", ".github/workflows/tests.yml"),
            "full",
        ),
        (
            "classifier",
            "pull_request",
            _changes("deploy/index.html", "scripts/ci-classify.py"),
            "full",
        ),
        (
            "main-push",
            "push",
            (),
            "full",
        ),
        (
            "non-pr-event",
            "workflow_dispatch",
            (),
            "full",
        ),
    ],
)
def test_lane_decision_table(
    case: str,
    event_name: str,
    changes,
    expected_lane: str,
) -> None:
    decision = ci_classifier.classify(event_name, changes)
    assert decision.lane == expected_lane, case


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            ci_classifier.Change("D", ("deploy/index.html",)),
            id="deletion",
        ),
        pytest.param(
            ci_classifier.Change(
                "R100",
                ("deploy/old-index.html", "deploy/index.html"),
            ),
            id="rename",
        ),
    ],
)
def test_rename_and_deletion_force_full(change) -> None:
    assert ci_classifier.classify("pull_request", (change,)).lane == "full"


def test_diff_failure_forces_full() -> None:
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    decision = ci_classifier.decision_from_git(
        "pull_request",
        "base",
        "head",
        runner=fail,
    )

    assert decision.lane == "full"
    assert "comparison failed closed" in decision.reason


def test_non_pr_does_not_need_a_git_comparison() -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("non-PR events must not compare a diff")

    decision = ci_classifier.decision_from_git(
        "push",
        "",
        "",
        runner=must_not_run,
    )

    assert decision.lane == "full"


def test_name_status_parser_preserves_rename_and_delete_metadata() -> None:
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


def _path_parts(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (*_path_parts(node.left), *_path_parts(node.right))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tuple(part for part in node.value.split("/") if part)
    return ()


def _direct_landing_test_files() -> tuple[str, ...]:
    direct: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.BinOp)
            and _path_parts(node)[-2:] == ("deploy", "index.html")
            for node in ast.walk(tree)
        ):
            direct.append(str(path.relative_to(ROOT)))
    return tuple(direct)


def test_fast_bundle_covers_every_direct_landing_page_test() -> None:
    """A new direct landing-page test must join the complete fast bundle."""

    assert _direct_landing_test_files() == ci_classifier.LANDING_TEST_FILES
    assert ci_classifier.LANDING_PYTEST_TARGETS == (
        *ci_classifier.LANDING_TEST_FILES,
        "tests/test_install_helpers.py"
        "::test_landing_page_app_css_version_uses_resolved_build_sha",
    )


def test_workflow_keeps_one_fail_closed_required_aggregate() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    triggers = parsed.get("on", parsed.get(True))

    assert "  pull_request:\n" in workflow
    assert triggers["pull_request"] is None
    assert triggers["push"] == {"branches": ["main"]}
    assert "  classify:\n" in workflow
    assert "  fast-landing:\n" in workflow
    assert "  ci:\n" in workflow
    assert "name: ci" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "Unexpected CI lane" in workflow
    assert "fast-landing lane selected work did not succeed" in workflow
    assert "full lane selected work did not succeed" in workflow
    assert 'python-version: ["3.11", "3.12", "3.13"]' in workflow
    assert "python3 scripts/ci-classify.py --landing-pytest-targets" in workflow


def _aggregate_script() -> str:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["ci"]["steps"]
    return next(
        step["run"] for step in steps if step["name"] == "Require the selected CI work"
    )


def _run_aggregate(**overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LANE": "full",
        "CLASSIFY_RESULT": "success",
        "FAST_LANDING_RESULT": "skipped",
        "SHELL_RESULT": "success",
        "PYTEST_MATRIX_RESULT": "success",
        "PYTEST_RESULT": "success",
        "JS_RESULT": "success",
        "RUST_RESULT": "success",
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
    )


def test_ci_aggregate_accepts_only_the_two_complete_result_shapes() -> None:
    assert _run_aggregate().returncode == 0
    assert _run_aggregate(
        LANE="fast-landing",
        FAST_LANDING_RESULT="success",
        SHELL_RESULT="skipped",
        PYTEST_MATRIX_RESULT="skipped",
        PYTEST_RESULT="skipped",
        JS_RESULT="skipped",
        RUST_RESULT="skipped",
    ).returncode == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"CLASSIFY_RESULT": "failure"},
        {"FAST_LANDING_RESULT": "success"},
        {"SHELL_RESULT": "failure"},
        {"PYTEST_MATRIX_RESULT": "cancelled"},
        {"PYTEST_RESULT": "skipped"},
        {"JS_RESULT": "failure"},
        {"RUST_RESULT": "cancelled"},
        {"LANE": "unexpected"},
        {
            "LANE": "fast-landing",
            "FAST_LANDING_RESULT": "failure",
            "SHELL_RESULT": "skipped",
            "PYTEST_MATRIX_RESULT": "skipped",
            "PYTEST_RESULT": "skipped",
            "JS_RESULT": "skipped",
            "RUST_RESULT": "skipped",
        },
        {
            "LANE": "fast-landing",
            "FAST_LANDING_RESULT": "success",
            "SHELL_RESULT": "success",
            "PYTEST_MATRIX_RESULT": "skipped",
            "PYTEST_RESULT": "skipped",
            "JS_RESULT": "skipped",
            "RUST_RESULT": "skipped",
        },
    ],
)
def test_ci_aggregate_fails_closed(overrides: dict[str, str]) -> None:
    assert _run_aggregate(**overrides).returncode != 0


def test_classifier_renders_lane_reason_and_changed_paths() -> None:
    decision = ci_classifier.classify(
        "pull_request",
        _changes("deploy/index.html", "tests/test_landing_page_html.py"),
    )

    summary = ci_classifier.render_summary(decision)

    assert "fast-landing" in summary
    assert decision.reason in summary
    assert "<code>deploy/index.html</code>" in summary
    assert "<code>tests/test_landing_page_html.py</code>" in summary


def test_policy_docs_and_pr_template_name_the_actual_review_contract() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )

    for policy in (agents, contributing):
        normalized = " ".join(policy.lower().split())
        assert "`ci`" in policy
        assert "fast-landing" in policy
        assert "every `main` push" in policy
        assert "no required reviewer" in normalized
        assert "conversation" in normalized
        assert "resolved" in normalized
    assert "otherwise `N/A` is sufficient" in template
    assert "Hardware/Pi evidence:" in template
