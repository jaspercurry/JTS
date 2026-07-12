# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for repository-bound wake-tool Python wrappers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LIB = SCRIPTS / "_lib.sh"


@dataclass(frozen=True)
class WrapperCase:
    name: str
    target: str
    args: tuple[str, ...]
    forwarded: tuple[str, ...]
    warning: str = ""


WRAPPERS = (
    WrapperCase(
        "analyze-three-leg.sh",
        "_analyze_three_leg.py",
        ("--top", "7", "relative-corpus", "--since=2026-06-01"),
        ("relative-corpus", "--top", "7", "--since=2026-06-01"),
    ),
    WrapperCase(
        "analyze-wake-corpus-quality.sh",
        "_analyze_wake_corpus_quality.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
        "WARN: no venv with numpy/scipy found; falling back to python3\n",
    ),
    WrapperCase(
        "audit-wake-corpus.sh",
        "_audit_wake_corpus.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
        "WARN: no venv with numpy found; falling back to python3\n",
    ),
    WrapperCase(
        "audit-wake-events.sh",
        "_audit_wake_events.py",
        ("relative-events", "ignored-by-existing-contract"),
        ("relative-events",),
        "WARN: no venv with numpy/scipy found; falling back to python3\n",
    ),
    WrapperCase(
        "build-wake-feature-bank.sh",
        "_build_wake_feature_bank.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
    WrapperCase(
        "build-wake-negative-feature-bank.sh",
        "_build_wake_negative_feature_bank.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
    WrapperCase(
        "export-wake-corpus-bundle.sh",
        "_export_wake_corpus_bundle.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
    WrapperCase(
        "prepare-wake-livekit-smoke.sh",
        "_prepare_wake_livekit_smoke.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
    WrapperCase(
        "prepare-wake-training-workdir.sh",
        "_prepare_wake_training_workdir.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
    WrapperCase(
        "run-wake-training-phase0.sh",
        "_run_wake_training_phase0.py",
        ("--sentinel", "value with spaces"),
        ("--sentinel", "value with spaces"),
    ),
)

HELP_WRAPPERS = tuple(
    case for case in WRAPPERS if case.name != "audit-wake-events.sh"
)


def _clean_env(**updates: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHON", None)
    env.update(updates)
    return env


def _copy_lib(repo: Path) -> Path:
    scripts = repo / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    destination = scripts / "_lib.sh"
    shutil.copy2(LIB, destination)
    return destination


def _resolve(lib: Path, *, cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; resolve_repo_python',
            "bash",
            str(lib),
        ],
        cwd=cwd,
        env=env or _clean_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


def _executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=JTS Test",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


def test_resolver_precedence_explicit_then_local_then_python3(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    lib = _copy_lib(repo)
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    foreign_python = _executable(foreign_cwd / ".venv" / "bin" / "python")
    local_python = _executable(repo / ".venv" / "bin" / "python")

    assert _resolve(
        lib,
        cwd=foreign_cwd,
        env=_clean_env(PYTHON="operator-python"),
    ) == "operator-python"
    assert _resolve(lib, cwd=foreign_cwd) == str(local_python)

    local_python.unlink()
    assert _resolve(lib, cwd=foreign_cwd) == "python3"
    assert _resolve(lib, cwd=foreign_cwd) != str(foreign_python)


def test_resolver_uses_normalized_main_venv_from_linked_worktree(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _copy_lib(main)
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", "scripts/_lib.sh")
    _git(main, "commit", "-qm", "add lib")
    main_python = _executable(main / ".venv" / "bin" / "python")
    worktree = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "-b", "linked", str(worktree))
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()

    resolved = _resolve(worktree / "scripts" / "_lib.sh", cwd=foreign_cwd)

    assert resolved == str(main_python)
    assert ".." not in Path(resolved).parts


def _logging_python(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "python-args.log"
    executable = _executable(
        tmp_path / "operator-python",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$WRAPPER_LOG"\n',
    )
    return executable, log


@pytest.mark.parametrize("case", WRAPPERS, ids=lambda case: case.name)
def test_wrappers_honor_explicit_interpreter_and_preserve_arguments(
    case: WrapperCase,
    tmp_path: Path,
) -> None:
    python, log = _logging_python(tmp_path)
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()

    completed = subprocess.run(
        ["bash", str(SCRIPTS / case.name), *case.args],
        cwd=foreign_cwd,
        env=_clean_env(PYTHON=str(python), WRAPPER_LOG=str(log)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert log.read_text().splitlines() == [
        str(SCRIPTS / case.target),
        *case.forwarded,
    ]


@pytest.mark.parametrize("case", WRAPPERS, ids=lambda case: case.name)
def test_implicit_python3_preserves_existing_warning_contract(
    case: WrapperCase,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    _copy_lib(repo)
    shutil.copy2(SCRIPTS / case.name, scripts / case.name)
    python3, log = _logging_python(tmp_path)
    python3_path = python3.with_name("python3")
    python3.rename(python3_path)
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    env = _clean_env(WRAPPER_LOG=str(log))
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"

    completed = subprocess.run(
        ["bash", str(scripts / case.name), "--help"],
        cwd=foreign_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == case.warning


@pytest.mark.parametrize("case", HELP_WRAPPERS, ids=lambda case: case.name)
def test_help_still_works_from_foreign_cwd(
    case: WrapperCase,
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPTS / case.name), "--help"],
        cwd=tmp_path,
        env=_clean_env(PYTHON=sys.executable),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout.lower()


def test_audit_wake_events_retains_non_help_positional_contract(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPTS / "audit-wake-events.sh"), "--help"],
        cwd=tmp_path,
        env=_clean_env(PYTHON=sys.executable),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "ERROR: --help not a directory" in completed.stderr


def test_invalid_explicit_interpreter_fails_without_fallback_warning(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-python"
    completed = subprocess.run(
        ["bash", str(SCRIPTS / "audit-wake-corpus.sh"), "--help"],
        cwd=tmp_path,
        env=_clean_env(PYTHON=str(missing)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert "WARN: no venv" not in completed.stderr


def test_explicit_python3_token_does_not_emit_implicit_fallback_warning(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    _copy_lib(repo)
    shutil.copy2(SCRIPTS / "audit-wake-corpus.sh", scripts)
    python3, log = _logging_python(tmp_path)
    python3_path = python3.with_name("python3")
    python3.rename(python3_path)
    env = _clean_env(PYTHON="python3", WRAPPER_LOG=str(log))
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"

    completed = subprocess.run(
        ["bash", str(scripts / "audit-wake-corpus.sh"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""


def test_audit_corpus_imports_jasper_from_invoking_worktree(
    tmp_path: Path,
) -> None:
    foreign_package = tmp_path / "foreign-package" / "jasper"
    foreign_package.mkdir(parents=True)
    (foreign_package / "__init__.py").write_text(
        'raise RuntimeError("imported foreign jasper checkout")\n'
    )
    env = _clean_env(PYTHON=sys.executable)
    env["PYTHONPATH"] = str(foreign_package.parent)

    completed = subprocess.run(
        ["bash", str(SCRIPTS / "audit-wake-corpus.sh"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "imported foreign jasper checkout" not in completed.stderr


def test_phase0_child_wrappers_share_the_interpreter_contract() -> None:
    phase0 = (SCRIPTS / "_run_wake_training_phase0.py").read_text()
    children = (
        "export-wake-corpus-bundle.sh",
        "build-wake-feature-bank.sh",
        "build-wake-negative-feature-bank.sh",
        "prepare-wake-training-workdir.sh",
        "prepare-wake-livekit-smoke.sh",
    )
    for child in children:
        assert f'_script("{child}")' in phase0
        wrapper = (SCRIPTS / child).read_text()
        assert '. "${SCRIPT_DIR}/_lib.sh"' in wrapper
        assert 'PY="$(resolve_repo_python)"' in wrapper


def test_wake_wrapper_resolver_static_ratchet() -> None:
    for case in WRAPPERS:
        wrapper = (SCRIPTS / case.name).read_text()
        assert '. "${SCRIPT_DIR}/_lib.sh"' in wrapper
        assert 'PY="$(resolve_repo_python)"' in wrapper
        assert "CANDIDATES=(" not in wrapper
        assert ".venv/bin/python" not in wrapper
        assert "${PYTHON:-python3}" not in wrapper
