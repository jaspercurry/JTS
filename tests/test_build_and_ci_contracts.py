# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WORKFLOW = WORKFLOWS / "tests.yml"


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


def _dependabot() -> dict:
    return yaml.safe_load(
        (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )


def _uv_update_entry() -> dict:
    entries = [
        entry
        for entry in _dependabot()["updates"]
        if entry["package-ecosystem"] == "uv"
    ]
    assert len(entries) == 1, "expected exactly one uv ecosystem entry"
    return entries[0]


def test_dependabot_dev_tooling_group_covers_every_dev_dependency() -> None:
    """Every dev dependency must match the dev-tooling group, not the catch-all.

    Third declaration of "which packages are dev tooling": pyproject's `dev`
    extra, its `dev` dependency-group, and the dependabot group's globs. The
    first two are already pinned to each other by
    test_dev_dependency_group_matches_dev_extra; without this, adding a dev
    tool (pre-commit, coverage, a types-* stub) silently drops it into
    `python-runtime` and defeats the split's stated purpose — holding a
    runtime SDK update behind lint work.

    `dependency-type: development` would be the declarative seam, but GitHub
    documents it for bundler/composer/mix/maven/npm/pip and NOT for `uv`, so
    the name enumeration is forced and needs a guard instead.
    """

    import fnmatch

    patterns = _uv_update_entry()["groups"]["python-dev-tooling"]["patterns"]
    unmatched = [
        requirement
        for requirement in _pyproject()["dependency-groups"]["dev"]
        if not any(
            fnmatch.fnmatch(_requirement_name(requirement), pattern)
            for pattern in patterns
        )
    ]

    assert not unmatched, (
        "dev dependencies that would fall into the python-runtime catch-all; "
        f"extend python-dev-tooling patterns in .github/dependabot.yml: {unmatched}"
    )


def _requirement_name(requirement: str) -> str:
    """Strip version specifiers/extras from a PEP 508 requirement string."""

    name = requirement.strip()
    for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.strip()


def test_dependabot_dev_tooling_group_precedes_the_catch_all() -> None:
    """Dependabot assigns a dependency to the FIRST group it matches.

    So `python-dev-tooling` must be declared before the `["*"]` catch-all;
    reordering them would silently make the narrow group dead with no signal.
    """

    groups = list(_uv_update_entry()["groups"])

    assert groups.index("python-dev-tooling") < groups.index("python-runtime")
    assert _uv_update_entry()["groups"]["python-runtime"]["patterns"] == ["*"]


def test_dependabot_groups_leave_security_updates_ungrouped() -> None:
    """No group may claim security updates.

    `applies-to` defaults to "version-updates"; setting it to
    "security-updates" (or "all") on any group would fold CVE fixes into a
    batched PR that a single unrelated broken bump can hold up. Security
    updates are also exempt from open-pull-requests-limit, so they must stay
    on their own channel.
    """

    for entry in _dependabot()["updates"]:
        for name, config in entry.get("groups", {}).items():
            assert "applies-to" not in config, (
                f"{entry['package-ecosystem']} group {name!r} overrides "
                "applies-to; security updates must stay ungrouped"
            )
            assert config.get("patterns"), f"group {name!r} has no patterns"


def _alsa_linking_crates() -> dict[str, str]:
    """Map each `rust/<crate>` directory to the `alsa` version it declares."""

    found: dict[str, str] = {}
    for manifest in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
        spec = declared.get("dependencies", {}).get("alsa")
        if spec is None:
            continue
        version = spec if isinstance(spec, str) else spec.get("version")
        assert version, f"{manifest} declares alsa with no version"
        found[f"/rust/{manifest.parent.name}"] = version
    return found


def test_alsa_linking_crates_share_one_version() -> None:
    """Every libasound-linking crate must pin the same `alsa` version.

    They talk to the same libasound on the same Pi and share the same wrapper
    API, so a split pin means one of them is being type-checked against an API
    the others do not have. It drifted silently once already (issue #2266),
    behind manifest comments each asserting a parity that no longer held. A
    comment cannot fail; this can.
    """

    crates = _alsa_linking_crates()

    assert len(crates) == 3, f"expected 3 alsa-linking crates, found {crates}"
    assert len(set(crates.values())) == 1, (
        f"alsa version split across crates: {crates}"
    )


def _cargo_entry_directories() -> list[set[str]]:
    """The directory set each cargo `updates` entry covers, one set per entry.

    Dependabot accepts either `directory` (a single path) or `directories` (a
    list). Read both, so the guards below cannot be defeated by switching form.
    """

    covered: list[set[str]] = []
    for entry in _dependabot()["updates"]:
        if entry["package-ecosystem"] != "cargo":
            continue
        directories = set(entry.get("directories") or ())
        if "directory" in entry:
            directories.add(entry["directory"])
        covered.append(directories)
    return covered


def test_dependabot_watches_every_alsa_linking_crate() -> None:
    """An unwatched crate directory ages with nothing raising a PR.

    That is exactly how the split in test_alsa_linking_crates_share_one_version
    formed: only jasper-fanin and jasper-outputd had cargo entries, so the other
    two never got a bump PR and nobody noticed them falling behind.
    """

    watched = set().union(*_cargo_entry_directories())
    unwatched = sorted(set(_alsa_linking_crates()) - watched)

    assert not unwatched, (
        "alsa-linking crate directories with no cargo entry in "
        f".github/dependabot.yml: {unwatched}"
    )


def test_dependabot_bumps_the_alsa_crates_atomically() -> None:
    """All four must be covered by ONE cargo entry, via `directories`.

    Coverage alone is not enough, and the two guards are load-bearing together.
    Dependabot groups cannot span `updates` entries, so an entry per crate
    raises a PR per crate, each rewriting one manifest —
    test_alsa_linking_crates_share_one_version then fails every one of them
    individually and the bump deadlocks with nothing mergeable.

    Not hypothetical: commit adf15a2cc (PR #1725) is a dependabot PR that
    rewrote jasper-outputd's manifest alone and created the very drift #2266
    had to repair. Splitting this entry back up would re-arm that.
    """

    entries = _cargo_entry_directories()
    alsa_crates = set(_alsa_linking_crates())
    owning = [covered for covered in entries if covered & alsa_crates]

    assert len(owning) == 1, (
        "the alsa-linking crates must share ONE cargo updates entry so a bump "
        f"is atomic; found {len(owning)} entries covering them: {owning}"
    )
    assert alsa_crates <= owning[0], (
        f"cargo entry misses alsa-linking crates: {sorted(alsa_crates - owning[0])}"
    )


def test_hang_backstop_is_configured_and_uses_the_signal_method() -> None:
    """The suite must fail a hang, never block on one.

    An unbounded await whose producer dies never returns; one such test
    blocked the entire local suite with no failing test to point at. The
    backstop turns that into a reported failure, so removing it is a
    deliberate act, not a silent config drift.

    `signal` is load-bearing and not interchangeable with `thread`:
    measured, `thread` kills the whole pytest process (every result after
    the stuck test is lost), while `signal` fails only that test and lets
    the run continue. The floor on the value keeps someone from "fixing" a
    slow test by tightening the backstop — it is a hang-breaker, not a
    timing assertion, and the slowest healthy test measures ~15s.
    """

    ini = _pyproject()["tool"]["pytest"]["ini_options"]

    assert any(
        _requirement_name(requirement) == "pytest-timeout"
        for requirement in _pyproject()["dependency-groups"]["dev"]
    ), "pytest-timeout must stay a dev dependency for the backstop to load"
    assert ini["timeout_method"] == "signal"
    assert ini["timeout"] >= 60


def test_fast_landing_workflow_syncs_locked_group() -> None:
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert "uv sync --locked --group fast-landing" in workflow


def test_ci_syncs_full_runtime_from_committed_uv_lock() -> None:
    """The full pytest suite imports optional runtime packages.

    CI should replay the committed lock instead of resolving
    `.[full,dev]` from live PyPI on every run. The openWakeWord install is
    deliberately after the exact sync because it is an ONNX-only exception
    installed without its unsatisfiable Python 3.13 tflite dependency.
    """

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    test_merge = (ROOT / "scripts" / "test-merge").read_text(encoding="utf-8")
    lane_resolver = (ROOT / "scripts" / "_test_lane.sh").read_text(encoding="utf-8")
    setup_action = (
        ROOT / ".github" / "actions" / "setup-python-uv" / "action.yml"
    ).read_text(encoding="utf-8")

    sync = "uv sync --locked --extra full --extra dev --group openwakeword-onnx"
    openwakeword = (
        "uv pip install --python .venv/bin/python --no-deps openwakeword==0.6.0"
    )

    assert "astral-sh/setup-uv@" in setup_action
    assert 'version: "0.12.9"' in setup_action
    assert sync in workflow
    assert openwakeword in workflow
    assert workflow.index(sync) < workflow.index(openwakeword)
    assert ".venv/bin/ruff check ." in workflow
    assert "run: scripts/test-merge" in workflow
    # The lane must run the interpreter from the `.venv` this sync populates,
    # not whatever `pytest` happens to be on $PATH. That preference now lives
    # in the resolver both lanes source rather than in each lane (issue #1836),
    # so pin both halves of the chain.
    assert "resolve_lane_tool test-merge pytest" in test_merge
    assert 'resolved=".venv/bin/${tool}"' in lane_resolver
    assert "scikit-learn>=1,<2" not in workflow
    assert "uv pip install --python .venv/bin/python requests" not in workflow
    assert "pip install -e '.[full,dev]'" not in workflow


def _workflow_action_refs(
    node: object, path: tuple[str, ...] = ()
) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            is_job_action = len(path) == 2 and path[0] == "jobs"
            is_step_action = (
                len(path) == 4
                and path[0] == "jobs"
                and path[2:] == ("steps", "[]")
            )
            if key == "uses" and (is_job_action or is_step_action):
                refs.append(value if isinstance(value, str) else repr(value))
            else:
                refs.extend(_workflow_action_refs(value, (*path, str(key))))
    elif isinstance(node, list):
        for value in node:
            refs.extend(_workflow_action_refs(value, (*path, "[]")))
    return refs


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [
        (
            'jobs:\n  check:\n    steps:\n      - "uses": owner/action@v1\n',
            ["owner/action@v1"],
        ),
        (
            "jobs: {check: {steps: [{uses: owner/action@v1}]}}\n",
            ["owner/action@v1"],
        ),
        (
            'jobs:\n  check:\n    uses: "owner/action@' + "a" * 40 + '"\n',
            ["owner/action@" + "a" * 40],
        ),
        (
            "jobs:\n  check:\n    env: {uses: plain-value}\n    steps:\n"
            "      - run: |\n          echo 'uses: owner/action@v1'\n",
            [],
        ),
    ],
    ids=["quoted-key", "flow-mapping", "quoted-value", "run-block"],
)
def test_workflow_action_refs_follow_yaml_structure(
    workflow: str, expected: list[str]
) -> None:
    assert _workflow_action_refs(yaml.safe_load(workflow)) == expected


def test_workflow_actions_are_sha_pinned() -> None:
    """Third-party actions must resolve to an immutable commit SHA.

    A tag (`@v9`) is mutable upstream, so a compromised re-tag would reach
    CI with no diff here. The SHA VALUE is Dependabot's to move: restating
    one in a test made every `setup-uv` bump fail on the assertion rather
    than on anything real (#2713), so this pins the shape, not the value.
    """
    unpinned: list[str] = []
    workflow_paths = (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"))
    for path in sorted(workflow_paths):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for ref in _workflow_action_refs(workflow):
            # `./…` is an in-repo action; it carries no upstream supply chain.
            if ref.startswith("./"):
                continue
            _, _, rev = ref.rpartition("@")
            if len(rev) != 40 or any(char not in "0123456789abcdef" for char in rev):
                unpinned.append(f"{path.name}: {ref}")

    assert not unpinned, (
        "workflow actions must be pinned to a 40-hex commit SHA, not a tag:\n  "
        + "\n  ".join(unpinned)
    )


def test_ci_pytest_gate_is_parallel_and_hardware_free() -> None:
    """Keep the full Python lane fast without running paid voice-eval."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    test_merge = (ROOT / "scripts" / "test-merge").read_text(encoding="utf-8")

    assert "run: scripts/test-merge" in workflow
    assert "-q --tb=short --ignore=tests/voice_eval -n 4" in test_merge


def test_ci_compiles_both_host_safe_ring_benchmarks() -> None:
    """Keep the C benchmarks and the plugin compile check inside the host
    build gate, and keep CI from ever installing the plugin (Pi-only, via
    build-on-pi.sh)."""
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "c" / "jts-ring-ioplug" / "Makefile").read_text(
        encoding="utf-8"
    )

    assert "run: make test bench plugin" in workflow
    assert "run: make install" not in workflow
    assert "bench: ring_writer_bench ring_reader_bench" in makefile


def test_test_lane_scripts_are_agent_facing_and_executable() -> None:
    """Agents should have stable commands instead of inventing test strategy."""

    for relpath in (
        "scripts/test-fast",
        "scripts/test-merge",
        "scripts/check-rust.sh",
        "scripts/rust-ci-needed",
    ):
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
    # The lane sources its sibling tool resolver, so the scratch repo has to
    # carry it too (issue #1836).
    shutil.copy2(ROOT / "scripts" / "_test_lane.sh", repo / "scripts" / "_test_lane.sh")
    shutil.copy2(
        ROOT / "scripts" / "ci-classify.py", repo / "scripts" / "ci-classify.py"
    )

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


def test_fast_lane_routes_an_experiment_kit_to_its_own_guard(tmp_path: Path) -> None:
    """An edit inside experiments/<kit>/ selects that kit's guard, only.

    Experiment kits keep their guards under tests/, so without this routing
    an edit to a kit selects nothing in the fast lane and its layout/path
    pins first run in the merge lane. The second assertion is what makes
    the arm worth having in this shape: it derives the guard from the
    directory name, so editing one kit does not drag in every other kit's
    tests.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "scripts").mkdir()
    (repo / "tests").mkdir()
    (repo / "experiments" / "e0-capture").mkdir(parents=True)
    for name in ("test-fast", "_test_lane.sh", "ci-classify.py"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)

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

    (repo / "tests" / "test_e0_capture_experiment.py").write_text("", encoding="utf-8")
    # A second kit's guard, present but untouched by the change below.
    (repo / "tests" / "test_usb_turntable_experiment.py").write_text(
        "", encoding="utf-8"
    )
    # Committed first: the lane treats untracked files as changed, so an
    # uncommitted guard would be selected by the `tests/test_*.py` arm and
    # prove nothing about this one.
    _commit_all(repo, "scaffold")

    (repo / "experiments" / "e0-capture" / "README.md").write_text(
        "kit prose\n", encoding="utf-8"
    )

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
    selected = [arg for call in calls for arg in call if arg.startswith("tests/")]
    assert "tests/test_e0_capture_experiment.py" in selected, calls
    assert "tests/test_usb_turntable_experiment.py" not in selected, calls


def test_rust_ci_gate_is_path_aware_without_renaming_visible_job() -> None:
    """Keep the visible `rust` job while avoiding unrelated apt/Cargo work."""

    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    rust_router = (ROOT / "scripts" / "rust-ci-needed").read_text(encoding="utf-8")

    assert "  rust:" in workflow
    assert "run: scripts/rust-ci-needed" in workflow
    assert "run: scripts/check-rust.sh" in workflow
    assert "steps.rust-needed.outputs.run == 'true'" in workflow
    assert "steps.rust-needed.outputs.run != 'true'" in workflow
    for surface in (
        "rust/*",
        "deploy/install.sh",
        ".github/workflows/tests.yml",
        "scripts/check-rust.sh",
    ):
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
        "scripts/check-rust.sh",
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

    env = {**os.environ, "GITHUB_EVENT_NAME": ""}
    env.pop("GITHUB_BASE_REF", None)
    env.pop("GITHUB_OUTPUT", None)

    stdout = _run(["scripts/rust-ci-needed"], cwd=repo, env=env)

    assert dict(line.split("=", 1) for line in stdout.strip().splitlines()) == {
        "run": "true",
        "reason": "non-PR event runs the full Rust gate",
    }


def test_mypy_dev_tooling_is_packaged_and_in_ci() -> None:
    """Keep the lenient type-checker wiring intact across packaging surfaces."""

    data = _pyproject()
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")

    assert [
        dep for dep in data["dependency-groups"]["dev"] if dep.startswith("mypy")
    ] == ["mypy>=2.3.0,<2.4"]
    assert [
        dep
        for dep in data["project"]["optional-dependencies"]["dev"]
        if dep.startswith("mypy")
    ] == ["mypy>=2.3.0,<2.4"]
    assert data["tool"]["mypy"]["files"] == [
        "jasper",
        "experiments/usb-turntable/jts_turntable.py",
    ]
    assert data["tool"]["mypy"]["ignore_missing_imports"] is True
    assert {
        override["follow_imports"]
        for override in data["tool"]["mypy"]["overrides"]
        if "dbus_next" in override["module"]
    } == {"skip"}
    assert "Type check (mypy; lenient baseline)" in workflow
    assert "run: .venv/bin/mypy" in workflow
    assert (ROOT / "jasper" / "py.typed").is_file()
    assert "py.typed" in data["tool"]["setuptools"]["package-data"]["jasper"]


def test_python_resolution_artifacts_are_committed() -> None:
    """Local dev/CI and Pi deploys intentionally use different Python
    resolution artifacts; keep both present."""

    assert (ROOT / "uv.lock").is_file()
    assert (ROOT / "deploy" / "constraints-pi.pins").is_file()


def test_linux_only_c_extensions_have_platform_markers() -> None:
    """Keep macOS contributor installs from trying to build Linux-only wheels."""

    data = _pyproject()
    dependencies = list(data["project"]["dependencies"])
    for group in data["project"]["optional-dependencies"].values():
        dependencies.extend(group)
    expected = {
        "pyalsaaudio": "pyalsaaudio>=0.11; sys_platform == 'linux'",
        "evdev": "evdev>=2.0; sys_platform == 'linux'",
    }

    # A package may appear in more than one extra (evdev is in both install
    # profiles); every occurrence must carry the marker verbatim.
    for package, requirement in expected.items():
        matches = [dep for dep in dependencies if dep.startswith(f"{package}>=")]
        assert matches, package
        assert set(matches) == {requirement}


def test_documented_venv_build_commands_install_test_runtime_extras() -> None:
    """Every contributor-facing "build your test venv" instruction must install
    the runtime extras the hardware-free suite imports (numpy, httpx, scipy, ...).

    A bare `uv sync` (or `pip install -e '.[dev]'`) installs only the dev tools,
    so pytest dies with dozens of ModuleNotFoundError on a clean checkout. uv
    0.11 has no `[tool.uv] default-extras` knob to fix that from config, so the
    docs and help spell the extras out explicitly. Pin ALL THREE surfaces — the
    CONTRIBUTING.md quick start, the conftest wrong-Python rebuild hint, and the
    test lanes' unresolvable-interpreter FATAL block — so the front door can't
    silently re-break (the 2026-06 OSS due-diligence finding, which regressed
    once because only one surface was fixed).

    `--all-groups` is called out by name because it is the plausible-looking
    wrong answer, and a *destructive* one: it syncs dependency GROUPS, so it
    uninstalls the `full`/`streambox` extras that carry the packages the suite
    imports. Following it leaves pytest resolvable — silencing the #1836 guard
    — on top of a suite that now dies at collection. That is the same
    false-green one layer down, so the ban is asserted, not just the command.
    """

    # Token-based, not an exact-substring match, so a future reformat of the
    # command (line wraps, flag reordering) doesn't false-fail as long as it
    # still invokes `uv sync` with both extras. The behavioural end-to-end check
    # (run the documented command and collect) belongs in CI; it's omitted here
    # only to avoid editing a workflow file from a non-`workflow`-scoped token.
    surfaces = {
        "CONTRIBUTING.md": (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
        "tests/conftest.py": (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"),
        "scripts/_test_lane.sh": (
            ROOT / "scripts" / "_test_lane.sh"
        ).read_text(encoding="utf-8"),
    }
    for name, text in surfaces.items():
        assert "uv sync" in text, f"{name} should document `uv sync`"
        assert "--extra full" in text, f"{name} `uv sync` must include `--extra full`"
        assert "--extra streambox" in text, (
            f"{name} `uv sync` must include `--extra streambox`"
        )
        assert "--all-groups" not in text, (
            f"{name} must not tell anyone to run `uv sync --all-groups`; it syncs "
            "dependency groups and uninstalls the extras the suite imports"
        )

    # The conftest pip fallback must also pull the extras (`.[full,dev]`, not `.[dev]`).
    assert "'.[full,dev]'" in surfaces["tests/conftest.py"]
