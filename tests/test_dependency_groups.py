from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SUPPLY_CHAIN_DOC = ROOT / "docs" / "HANDOFF-supply-chain.md"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _init_git_repo(repo: Path) -> None:
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "JTS Tests"], cwd=repo)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=repo)


def _commit_all(repo: Path, message: str) -> None:
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "-m", message], cwd=repo)


def test_dev_dependency_group_matches_dev_extra() -> None:
    """Keep `uv sync` and `pip install -e '.[dev]'` dev deps in lockstep."""

    data = _pyproject()

    assert data["dependency-groups"]["dev"] == (
        data["project"]["optional-dependencies"]["dev"]
    )


def test_ci_installs_full_runtime_with_dev_extra() -> None:
    """The full pytest suite imports optional runtime packages."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "pip install -e '.[full,dev]'" in workflow


def test_ci_pytest_gate_is_parallel_and_hardware_free() -> None:
    """Keep the required Python merge gate fast without running paid voice-eval."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    test_merge = (ROOT / "scripts" / "test-merge").read_text(encoding="utf-8")

    assert "run: scripts/test-merge" in workflow
    assert "-q --tb=short --ignore=tests/voice_eval -n 4" in test_merge


def test_test_lane_scripts_are_agent_facing_and_executable() -> None:
    """Agents should have stable commands instead of inventing test strategy."""

    for relpath in ("scripts/test-fast", "scripts/test-merge", "scripts/rust-ci-needed"):
        path = ROOT / relpath
        assert path.is_file(), f"{relpath} must exist"
        assert path.stat().st_mode & 0o111, f"{relpath} must be executable"


def test_fast_lane_routes_untracked_tests_before_staging(tmp_path: Path) -> None:
    """Brand-new files must affect the fast lane before an agent stages them."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    (repo / "tests").mkdir()
    shutil.copy2(ROOT / "scripts" / "test-fast", repo / "scripts" / "test-fast")

    pytest_calls = repo / "pytest-calls.jsonl"
    fake_pytest = repo / "fake-pytest"
    fake_pytest.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json",
                "import os",
                "import sys",
                "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:",
                "    f.write(json.dumps(sys.argv[1:]) + '\\n')",
                "raise SystemExit(5 if '--last-failed' in sys.argv else 0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_pytest.chmod(0o755)

    fake_ruff = repo / "fake-ruff"
    fake_ruff.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    fake_ruff.chmod(0o755)

    (repo / "tests" / "test_dependency_groups.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_new_feature.py").write_text("", encoding="utf-8")

    env = {
        **os.environ,
        "PYTEST": str(fake_pytest),
        "PYTEST_CALLS": str(pytest_calls),
        "RUFF": str(fake_ruff),
        "TEST_BASE": "missing-base",
    }
    _run(["scripts/test-fast"], cwd=repo, env=env)

    calls = [
        json.loads(line)
        for line in pytest_calls.read_text(encoding="utf-8").splitlines()
    ]
    assert any("tests/test_new_feature.py" in call for call in calls), calls


def test_rust_ci_gate_is_path_aware_without_renaming_required_check() -> None:
    """Keep the required `rust` check present while avoiding unrelated apt/Cargo work."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    rust_router = (ROOT / "scripts" / "rust-ci-needed").read_text(encoding="utf-8")

    assert "  rust:" in workflow
    assert "run: scripts/rust-ci-needed" in workflow
    assert "steps.rust-needed.outputs.run == 'true'" in workflow
    assert "steps.rust-needed.outputs.run != 'true'" in workflow
    for surface in ("rust/*", "deploy/install.sh", ".github/workflows/tests.yml"):
        assert surface in rust_router


def _router_decision_for_changed_path(tmp_path: Path, changed_path: str) -> dict[str, str]:
    repo = tmp_path / changed_path.replace("/", "_").replace(".", "_")
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "rust-ci-needed", repo / "scripts" / "rust-ci-needed")
    _commit_all(repo, "base")

    path = repo / changed_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")
    _commit_all(repo, f"change {changed_path}")

    stdout = _run(
        ["scripts/rust-ci-needed"],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_BASE_REF": "main",
        },
    )
    return dict(line.split("=", 1) for line in stdout.strip().splitlines())


def test_rust_ci_router_behavior_for_pull_request_paths(tmp_path: Path) -> None:
    """Exercise the path-aware Cargo skip decision, not just workflow strings."""

    assert _router_decision_for_changed_path(tmp_path, "docs/noop.md")["run"] == "false"
    for changed_path in (
        "rust/jasper-outputd/src/main.rs",
        "deploy/install.sh",
        ".github/workflows/tests.yml",
    ):
        decision = _router_decision_for_changed_path(tmp_path, changed_path)
        assert decision["run"] == "true", decision
        assert decision["reason"] == f"PR touches {changed_path}"


def test_rust_ci_router_runs_full_gate_for_non_pr_events(tmp_path: Path) -> None:
    """Main pushes must keep running the full Rust gate."""

    repo = tmp_path / "non-pr"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "rust-ci-needed", repo / "scripts" / "rust-ci-needed")

    stdout = _run(["scripts/rust-ci-needed"], cwd=repo)

    assert dict(line.split("=", 1) for line in stdout.strip().splitlines()) == {
        "run": "true",
        "reason": "non-PR event runs the full Rust gate",
    }


def test_python_resolution_artifacts_are_committed_and_documented() -> None:
    """Local dev and Pi deploys intentionally use different Python
    resolution artifacts; keep both present and keep the canonical doc
    from drifting back to the old "choose later" language."""

    assert (ROOT / "uv.lock").is_file()
    assert (ROOT / "deploy" / "constraints-pi.txt").is_file()

    doc = SUPPLY_CHAIN_DOC.read_text(encoding="utf-8")
    assert "uv.lock" in doc
    assert "deploy/constraints-pi.txt" in doc
    assert "choose one shared artifact" not in doc
    assert "does not currently commit a shared Python lock artifact" not in doc


def test_linux_only_c_extensions_have_platform_markers() -> None:
    """Keep macOS contributor installs from trying to build Linux-only wheels."""

    data = _pyproject()
    dependencies = list(data["project"]["dependencies"])
    for group in data["project"]["optional-dependencies"].values():
        dependencies.extend(group)
    expected = {
        "pyalsaaudio": "pyalsaaudio>=0.11; sys_platform == 'linux'",
        "evdev": "evdev>=1.7; sys_platform == 'linux'",
    }

    for package, requirement in expected.items():
        matches = [dep for dep in dependencies if dep.startswith(f"{package}>=")]
        assert matches == [requirement]


def test_documented_venv_build_commands_install_test_runtime_extras() -> None:
    """Every contributor-facing "build your test venv" instruction must install
    the runtime extras the hardware-free suite imports (numpy, httpx, scipy, ...).

    A bare `uv sync` (or `pip install -e '.[dev]'`) installs only the dev tools,
    so pytest dies with dozens of ModuleNotFoundError on a clean checkout. uv
    0.11 has no `[tool.uv] default-extras` knob to fix that from config, so the
    docs and help spell the extras out explicitly. Pin BOTH surfaces — the
    CONTRIBUTING.md quick start and the conftest wrong-Python rebuild hint — so
    the front door can't silently re-break (the 2026-06 OSS due-diligence
    finding, which regressed once because only one surface was fixed).
    """

    # Token-based, not an exact-substring match, so a future reformat of the
    # command (line wraps, flag reordering) doesn't false-fail as long as it
    # still invokes `uv sync` with both extras. The behavioural end-to-end check
    # (run the documented command and collect) belongs in CI; it's omitted here
    # only to avoid editing a workflow file from a non-`workflow`-scoped token.
    surfaces = {
        "CONTRIBUTING.md": (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
        "tests/conftest.py": (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "uv sync" in text, f"{name} should document `uv sync`"
        assert "--extra full" in text, f"{name} `uv sync` must include `--extra full`"
        assert "--extra streambox" in text, (
            f"{name} `uv sync` must include `--extra streambox`"
        )

    # The conftest pip fallback must also pull the extras (`.[full,dev]`, not `.[dev]`).
    assert "'.[full,dev]'" in surfaces["tests/conftest.py"]
