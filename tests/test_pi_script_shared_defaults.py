# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for laptop-side scripts that target the active JTS speaker."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAMES = (
    "switch-gemini-model.sh",
    "switch-wake-word.sh",
    "tail-pi-logs.sh",
    "verify-ref-no-silence-bug.sh",
    "wake-rate-test.sh",
)
ROBUST_SCRIPT_DIR = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
LIB_SOURCE = '. "${SCRIPT_DIR}/_lib.sh"'
INVOCATIONS = {
    "switch-gemini-model.sh": (["3.1"], 0),
    "switch-wake-word.sh": (["jarvis_v2"], 0),
    "tail-pi-logs.sh": (["jasper-voice"], 0),
    "verify-ref-no-silence-bug.sh": ([], 1),
    "wake-rate-test.sh": (["1"], 23),
}


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def script_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (*SCRIPT_NAMES, "_lib.sh", "_wake_audio_metrics.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "_offline_wake_count.py").write_text("# test stub\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "ssh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'ssh' >> "$FAKE_COMMAND_LOG"
            for arg in "$@"; do printf '\t%s' "$arg" >> "$FAKE_COMMAND_LOG"; done
            printf '\n' >> "$FAKE_COMMAND_LOG"
            if [[ "${FAKE_SSH_FAIL:-0}" == "1" ]]; then
                exit 23
            fi
            case "$*" in
                *"from jasper.wake_models import by_key"*)
                    printf '/tmp/jarvis-v2.onnx|1\n'
                    ;;
                *"from jasper.wake_models import REGISTRY"*)
                    printf '  jarvis_v2      Jarvis v2 (recommended)\n'
                    ;;
            esac
            """
        ),
    )
    _write_executable(
        fake_bin / "scp",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'scp' >> "$FAKE_COMMAND_LOG"
            for arg in "$@"; do printf '\t%s' "$arg" >> "$FAKE_COMMAND_LOG"; done
            printf '\n' >> "$FAKE_COMMAND_LOG"
            exit 23
            """
        ),
    )
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    return repo, fake_bin, log


def _run_script(
    script_repo: tuple[Path, Path, Path],
    name: str,
    *,
    env_local: str | None,
    inherited: dict[str, str],
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    repo, fake_bin, log = script_repo
    if env_local is None:
        (repo / ".env.local").unlink(missing_ok=True)
    else:
        (repo / ".env.local").write_text(env_local, encoding="utf-8")
    log.unlink(missing_ok=True)

    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)
    env.update(inherited)
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_COMMAND_LOG": str(log),
            "SESSION": "contract-test",
            "DURATION": "0",
        }
    )
    if name == "verify-ref-no-silence-bug.sh":
        env["FAKE_SSH_FAIL"] = "1"

    default_args, _expected_status = INVOCATIONS[name]
    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts" / name),
            *(default_args if args is None else args),
        ],
        cwd=repo.parent / "foreign-cwd",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_pi_target_scripts_source_the_shared_owner_without_local_defaults(
    name: str,
) -> None:
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")

    assert text.count(ROBUST_SCRIPT_DIR) == 1
    assert text.count(LIB_SOURCE) == 1
    assert not re.search(r"(?m)^\s*(?:export\s+)?PI_(?:HOST|USER)=", text)
    assert text.index("set -euo pipefail") < text.index(ROBUST_SCRIPT_DIR)
    assert text.index(ROBUST_SCRIPT_DIR) < text.index(LIB_SOURCE)


def test_capture_scripts_use_repo_root_from_the_shared_owner() -> None:
    for name in ("verify-ref-no-silence-bug.sh", "wake-rate-test.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'REPO_ROOT="$(cd ' not in text
        assert '"$REPO_ROOT/' in text


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_explicit_environment_target_works_from_any_cwd(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    result, calls = _run_script(
        script_repo,
        name,
        env_local=None,
        inherited={"PI_HOST": "explicit.invalid", "PI_USER": "operator"},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "operator@explicit.invalid" in calls


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_checkout_env_local_target_has_shared_precedence(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    result, calls = _run_script(
        script_repo,
        name,
        env_local="PI_HOST=checkout.invalid\nPI_USER=checkout-user\n",
        inherited={"PI_HOST": "inherited.invalid", "PI_USER": "inherited-user"},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "checkout-user@checkout.invalid" in calls
    assert "inherited-user@inherited.invalid" not in calls


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_jasper_hostname_compatibility_fallback_comes_from_shared_owner(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    result, calls = _run_script(
        script_repo,
        name,
        env_local=None,
        inherited={"JASPER_HOSTNAME": "legacy.invalid"},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "pi@legacy.invalid" in calls


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_final_pi_target_defaults_come_from_shared_owner(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    result, calls = _run_script(
        script_repo,
        name,
        env_local=None,
        inherited={},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "pi@jts.local" in calls


def test_gemini_unknown_alias_exits_without_network(
    script_repo: tuple[Path, Path, Path],
) -> None:
    result, calls = _run_script(
        script_repo,
        "switch-gemini-model.sh",
        env_local=None,
        inherited={"PI_HOST": "explicit.invalid", "PI_USER": "operator"},
        args=["unknown"],
    )

    assert result.returncode == 2
    assert "unknown model alias" in result.stderr
    assert calls == ""


def test_wake_word_current_and_usage_path_is_safe_with_stubbed_ssh(
    script_repo: tuple[Path, Path, Path],
) -> None:
    result, calls = _run_script(
        script_repo,
        "switch-wake-word.sh",
        env_local=None,
        inherited={"PI_HOST": "explicit.invalid", "PI_USER": "operator"},
        args=[],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Current wake model on explicit.invalid:" in result.stdout
    assert "Usage:  bash scripts/switch-wake-word.sh <key>" in result.stdout
    assert calls.count("operator@explicit.invalid") == 2


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_pi_target_scripts_are_valid_bash(name: str) -> None:
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
