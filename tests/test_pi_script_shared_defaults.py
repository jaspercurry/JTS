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

from scripts import _pi_target


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


# sysexits EX_CONFIG: _lib.sh refuses rather than guess a speaker (#3498).
NO_TARGET_EXIT = 78


def _identity_file(tmp_path: Path, extra: str = "") -> Path:
    """A reconciler-shaped identity.env, as jasper-identity-reconcile writes
    it (key names owned by jasper/identity_state.py)."""
    path = tmp_path / "identity.env"
    path.write_text(
        "JASPER_IDENTITY_OS_HOSTNAME=jts7\n"
        "JASPER_IDENTITY_AVAHI_HOSTNAME=jts7.local\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts7.local\n" + extra,
        encoding="utf-8",
    )
    return path


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
                *"/opt/jasper/.venv/bin/python - 3.1"*)
                    printf 'catalog-default.test\n'
                    ;;
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
    # Hermetic against a real box: point the on-box identity lookup at a
    # path that does not exist unless a test names one.
    env["JASPER_IDENTITY_FILE"] = str(repo / "absent-identity.env")
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


@pytest.mark.parametrize(
    ("name", "remote_prefix"),
    [
        ("verify-ref-no-silence-bug.sh", "/tmp/ref-verify-"),
        ("wake-rate-test.sh", "/tmp/wake-rate-"),
    ],
)
def test_capture_scripts_clean_their_bounded_remote_directory_on_exit(
    name: str,
    remote_prefix: str,
) -> None:
    text = (ROOT / "scripts" / name).read_text(encoding="utf-8")

    assert "cleanup_remote_capture()" in text
    assert "trap cleanup_remote_capture EXIT" in text
    assert f"{remote_prefix}*)" in text
    assert 'printf -v remote_capture_q \'%q\' "$OUT_REMOTE"' in text
    assert '"sudo rm -rf -- ${remote_capture_q}"' in text


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
def test_explicit_environment_target_outranks_checkout_env_local(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    """.env.local is the checkout's default target, not an override (#2689)."""
    result, calls = _run_script(
        script_repo,
        name,
        env_local="PI_HOST=checkout.invalid\nPI_USER=checkout-user\n",
        inherited={"PI_HOST": "inherited.invalid", "PI_USER": "inherited-user"},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "inherited-user@inherited.invalid" in calls
    assert "checkout-user@checkout.invalid" not in calls


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_checkout_env_local_target_is_the_shared_default(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    result, calls = _run_script(
        script_repo,
        name,
        env_local="PI_HOST=checkout.invalid\nPI_USER=checkout-user\n",
        inherited={},
    )

    assert result.returncode == INVOCATIONS[name][1], result.stdout + result.stderr
    assert "checkout-user@checkout.invalid" in calls


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
def test_unnamed_target_refuses_instead_of_guessing_a_speaker(
    script_repo: tuple[Path, Path, Path],
    name: str,
) -> None:
    """#3498: `jts.local` resolves to whichever box on the LAN claimed the
    name, so an unnamed target is a refusal — never a guess, never ssh."""
    result, calls = _run_script(
        script_repo,
        name,
        env_local=None,
        inherited={},
    )

    assert result.returncode == NO_TARGET_EXIT, result.stdout + result.stderr
    assert calls == ""


def test_on_box_identity_file_supplies_the_target(
    script_repo: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """Run ON a speaker there IS a right answer: the hostname the
    reconciler recorded for this box (jasper/identity_state.py's file)."""
    result, calls = _run_script(
        script_repo,
        "tail-pi-logs.sh",
        env_local=None,
        inherited={"JASPER_IDENTITY_FILE": str(_identity_file(tmp_path))},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pi@jts7.local" in calls


@pytest.mark.parametrize(
    "flag", ["JASPER_IDENTITY_COLLISION", "JASPER_IDENTITY_DRIFT"]
)
def test_a_disagreeing_on_box_identity_refuses_instead_of_naming_a_speaker(
    script_repo: tuple[Path, Path, Path],
    tmp_path: Path,
    flag: str,
) -> None:
    """The reconciler flags the recorded names as disagreeing exactly when
    the configured one resolves to a DIFFERENT box on the LAN, so there is
    no name left to hand out (#3498)."""
    result, calls = _run_script(
        script_repo,
        "tail-pi-logs.sh",
        env_local=None,
        inherited={
            "JASPER_IDENTITY_FILE": str(_identity_file(tmp_path, f"{flag}=1\n")),
        },
    )

    assert result.returncode == NO_TARGET_EXIT, result.stdout + result.stderr
    assert calls == ""


def test_the_effective_mdns_name_outranks_the_configured_one(
    script_repo: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """What the LAN answers to is what reaches this box. A file recording
    the two without a collision/drift flag predates them; the effective
    name is still the one that resolves."""
    path = tmp_path / "identity.env"
    path.write_text(
        "JASPER_IDENTITY_AVAHI_HOSTNAME=jts7-2.local\n"
        "JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts7.local\n",
        encoding="utf-8",
    )

    result, calls = _run_script(
        script_repo,
        "tail-pi-logs.sh",
        env_local=None,
        inherited={"JASPER_IDENTITY_FILE": str(path)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pi@jts7-2.local" in calls


def test_an_explicit_host_override_resolves_when_nothing_else_names_a_target(
    script_repo: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--host <ip>` on a laptop script names the target itself, so _lib.sh's
    refusal must not swallow it -- while the checkout's PI_USER still
    applies (scripts/_pi_target.py's per-field override)."""
    repo, _fake_bin, _log = script_repo
    monkeypatch.setattr(_pi_target, "LIB_SH", repo / "scripts" / "_lib.sh")
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(repo / "absent-identity.env"))

    assert _pi_target.resolve_pi_target(host_override="192.168.1.5") == (
        "192.168.1.5", "pi")

    (repo / ".env.local").write_text("PI_USER=checkout-user\n", encoding="utf-8")
    assert _pi_target.resolve_pi_target(host_override="192.168.1.5") == (
        "192.168.1.5", "checkout-user")


def test_explicit_target_outranks_the_on_box_identity_file(
    script_repo: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    result, calls = _run_script(
        script_repo,
        "tail-pi-logs.sh",
        env_local=None,
        inherited={
            "JASPER_IDENTITY_FILE": str(_identity_file(tmp_path)),
            "PI_HOST": "explicit.invalid",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pi@explicit.invalid" in calls
    assert "jts7.local" not in calls


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
