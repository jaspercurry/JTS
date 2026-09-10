# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for the installed-catalog voice provider switcher."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "switch-voice-provider.sh"


def test_switch_voice_provider_script_is_valid_bash():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def script_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Mirrors test_switch_gemini_model_script.py's script_repo: a real
    checkout shape (scripts/ + .env.local) plus a PATH-shimmed `ssh` that
    logs every remote command instead of touching the network, so the
    script's own catalog-driven branching runs for real."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    shutil.copy2(ROOT / "scripts" / "_lib.sh", scripts / "_lib.sh")
    (repo / ".env.local").write_text(
        "PI_HOST=checkout.invalid\nPI_USER=checkout-user\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'ssh' >> "$FAKE_SSH_LOG"
            for arg in "$@"; do
                flat="${arg//$'\\n'/\\\\n}"
                printf '\\t%s' "$flat" >> "$FAKE_SSH_LOG"
            done
            printf '\\n' >> "$FAKE_SSH_LOG"

            remote="${*: -1}"
            case "$remote" in
                *"from jasper.voice.catalog import PROVIDERS"*)
                    printf 'gemini\\tJASPER_GEMINI_API_KEY\\tJASPER_GEMINI_MODEL\\n'
                    printf 'openai\\tJASPER_OPENAI_API_KEY\\tJASPER_OPENAI_MODEL\\n'
                    ;;
                *"jasper_env_file_set"*)
                    printf 'active\\n'
                    ;;
                *"grep -h -E"*)
                    if [[ "${FAKE_KEY_PRESENT:-1}" == "1" ]]; then
                        printf 'JASPER_GEMINI_API_KEY=stub-key-value\\n'
                    fi
                    ;;
            esac
            """
        ),
    )
    return repo, fake_bin, log


def _run(
    script_repo: tuple[Path, Path, Path],
    args: list[str],
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    repo, fake_bin, log = script_repo
    log.unlink(missing_ok=True)
    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_SSH_LOG"] = str(log)
    result = subprocess.run(
        ["bash", str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_unknown_provider_is_rejected_without_switching(
    script_repo: tuple[Path, Path, Path],
):
    """An id the stubbed catalog never lists must fail closed, before any
    write or restart is attempted -- exactly one ssh call (the catalog
    fetch), never the key check or the switch+restart call."""
    result, calls = _run(script_repo, ["not-a-real-provider"])

    assert result.returncode == 2
    assert len(calls) == 1
    assert "from jasper.voice.catalog import PROVIDERS" in calls[0]


def test_known_provider_switches(script_repo: tuple[Path, Path, Path]):
    """A provider id the stubbed catalog does list must proceed through
    the key check and reach the switch+restart call."""
    result, calls = _run(script_repo, ["gemini"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3
    assert "from jasper.voice.catalog import PROVIDERS" in calls[0]
    assert "grep -h -E" in calls[1]
    assert "JASPER_GEMINI_API_KEY" in calls[1]
    assert "jasper_env_file_set" in calls[2]
    assert "/var/lib/jasper/voice_provider.env" in calls[2]
    assert "systemctl restart jasper-voice" in calls[2]
