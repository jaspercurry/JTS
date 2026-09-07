# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contracts for laptop-side scripts that target the active JTS speaker."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import textwrap

import pytest

from scripts import _pi_target


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_NAMES = (
    "switch-gemini-model.sh",
    "switch-voice-provider.sh",
    "switch-wake-word.sh",
    "tail-pi-logs.sh",
    "verify-ref-no-silence-bug.sh",
    "wake-rate-test.sh",
)
ROBUST_SCRIPT_DIR = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
LIB_SOURCE = '. "${SCRIPT_DIR}/_lib.sh"'
# Default argv per script; _run_script's only consumer. Also carries
# rename-speaker.sh, which is not a SCRIPT_NAMES member (see script_repo).
INVOCATIONS = {
    "switch-gemini-model.sh": ["3.1"],
    "switch-voice-provider.sh": ["gemini"],
    "switch-wake-word.sh": ["jarvis_v2"],
    "tail-pi-logs.sh": ["jasper-voice"],
    "verify-ref-no-silence-bug.sh": [],
    "wake-rate-test.sh": ["1"],
    "rename-speaker.sh": ["jts4", "--no-deploy"],
}
# Expected exit status for a default invocation — only meaningful for
# SCRIPT_NAMES members, which the blanket targeting tests below check.
EXPECTED_STATUS = {
    "switch-gemini-model.sh": 0,
    "switch-voice-provider.sh": 0,
    "switch-wake-word.sh": 0,
    "tail-pi-logs.sh": 0,
    "verify-ref-no-silence-bug.sh": 1,
    "wake-rate-test.sh": 23,
}


# sysexits EX_CONFIG: _lib.sh refuses rather than guess a speaker (#3498).
NO_TARGET_EXIT = 78


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _lib_function_output(function_call: str) -> str:
    """Run one _lib.sh function call in isolation; PI_HOST/PI_USER satisfy
    its own target resolution so sourcing it doesn't refuse first."""
    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)
    env.update({"PI_HOST": "explicit.invalid", "PI_USER": "operator"})
    result = subprocess.run(
        ["bash", "-c", f'source "{ROOT / "scripts" / "_lib.sh"}"\n{function_call}'],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _remote_env_file_set_cmd(file: str, key: str, value: str, *modes: str) -> str:
    args = " ".join(shlex.quote(a) for a in (file, key, value, *modes))
    return _lib_function_output(f"remote_env_file_set_cmd {args}")


@pytest.fixture
def script_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        *SCRIPT_NAMES,
        # Not a SCRIPT_NAMES member (predates ROBUST_SCRIPT_DIR); copied so
        # its env-file-write test below can reuse this fixture's fake ssh.
        "rename-speaker.sh",
        "_lib.sh",
        "_diagnostic_redaction.sh",
        "_wake_audio_metrics.py",
    ):
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
                *"from jasper.voice.catalog import PROVIDERS"*)
                    printf 'gemini\tGEMINI_API_KEY\tJASPER_GEMINI_MODEL\n'
                    ;;
                *"GEMINI_API_KEY=.*"*)
                    printf 'GEMINI_API_KEY=fake123\n'
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

    result = subprocess.run(
        [
            "bash",
            str(repo / "scripts" / name),
            *(INVOCATIONS[name] if args is None else args),
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

    assert result.returncode == EXPECTED_STATUS[name], result.stdout + result.stderr
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

    assert result.returncode == EXPECTED_STATUS[name], result.stdout + result.stderr
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

    assert result.returncode == EXPECTED_STATUS[name], result.stdout + result.stderr
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

    assert result.returncode == EXPECTED_STATUS[name], result.stdout + result.stderr
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

    assert _pi_target.resolve_pi_target(host_override="192.168.1.5") == (
        "192.168.1.5", "pi")

    (repo / ".env.local").write_text("PI_USER=checkout-user\n", encoding="utf-8")
    assert _pi_target.resolve_pi_target(host_override="192.168.1.5") == (
        "192.168.1.5", "checkout-user")


# Scripts whose --help / usage path runs AFTER they source _lib.sh, with the
# argv that reaches the speaker. They defer the refusal instead of taking it
# at source time, so help stays readable on a checkout with no target set.
_HELP_BEFORE_TARGET = (
    ("multiroom-spike.sh", ["--help"], ["--teardown"]),
    ("pi-run-diagnostic.sh", ["--help"], ["--", "true"]),
)


@pytest.mark.parametrize(
    ("script", "help_args", "action_args"),
    _HELP_BEFORE_TARGET,
    ids=[case[0] for case in _HELP_BEFORE_TARGET],
)
def test_help_needs_no_target_but_the_action_still_refuses(
    tmp_path: Path,
    script: str,
    help_args: list[str],
    action_args: list[str],
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("_lib.sh", script):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    env = os.environ.copy()
    for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
        env.pop(key, None)

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(repo / "scripts" / script), *args],
            env=env, capture_output=True, text=True, timeout=15,
        )

    helped = _run(help_args)
    assert helped.returncode == 0, helped.stdout + helped.stderr
    assert (helped.stdout + helped.stderr).strip()

    assert _run(action_args).returncode == NO_TARGET_EXIT


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


@pytest.mark.parametrize(
    "value",
    ["plain", "with space", "a $value with a \\backslash"],
    ids=["plain", "space", "dollar-and-backslash"],
)
def test_remote_env_file_set_cmd_executes_the_upsert_it_prints(
    tmp_path: Path, value: str,
) -> None:
    """Real-execute remote_env_file_set_cmd's own printed command (installed-
    lib path swapped for the repo copy): the value round-trips through
    jasper_env_file_get and the file lands at the requested mode. Apostrophe
    coverage is the lib's own concern, pinned in tests/test_env_file_lib.py.
    The target dir itself carries a space, pinning that FILE is quoted too
    (the dir does not exist yet, so the lib's own -d create also runs it)."""
    target = tmp_path / "a dir" / "target.env"
    real_lib = ROOT / "deploy" / "lib" / "jasper-env-file.sh"

    remote_cmd = _remote_env_file_set_cmd(str(target), "KEY", value, "0640", "0750")
    local_cmd = remote_cmd.replace("/usr/local/lib/jasper/jasper-env-file.sh", str(real_lib))
    exec_result = subprocess.run(
        ["bash", "-c", local_cmd], capture_output=True, text=True, timeout=10,
    )
    assert exec_result.returncode == 0, exec_result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    get_result = subprocess.run(
        ["bash", "-c", f'. "{real_lib}" && jasper_env_file_get "{target}" KEY'],
        capture_output=True, text=True, timeout=10,
    )
    assert get_result.returncode == 0, get_result.stderr
    assert get_result.stdout == f"{value}\n"


@pytest.mark.parametrize(
    ("script", "file", "key", "value", "modes", "prefix", "chained"),
    [
        ("switch-wake-word.sh", "/var/lib/jasper/wake_model.env", "JASPER_WAKE_MODEL",
         "/tmp/jarvis-v2.onnx", ("0644", "0770"), "sudo", True),
        ("rename-speaker.sh", "/etc/jasper/jasper.env", "JASPER_HOSTNAME",
         "jts4.local", ("0640", "0755"), "sudo -n", False),
        ("switch-voice-provider.sh", "/var/lib/jasper/voice_provider.env",
         "JASPER_VOICE_PROVIDER", "gemini", ("0640", "0770"), "sudo", True),
        ("switch-gemini-model.sh", "/var/lib/jasper/voice_provider.env",
         "JASPER_GEMINI_MODEL", "catalog-default.test", ("0640", "0770"), "sudo", False),
    ],
    ids=["switch-wake-word", "rename-speaker", "switch-voice-provider", "switch-gemini-model"],
)
def test_env_file_write_matches_the_shared_helper(
    script_repo: tuple[Path, Path, Path],
    script: str, file: str, key: str, value: str,
    modes: tuple[str, str], prefix: str, chained: bool,
) -> None:
    """Each script's write is exactly remote_env_file_set_cmd's own
    rendering — no second, hand-built spelling. The fixture's fake ssh cans
    every preflight/registry call, so nothing but the write is recorded;
    execution of the write is the helper test's job above."""
    result, calls = _run_script(
        script_repo, script, env_local=None,
        inherited={"PI_HOST": "explicit.invalid", "PI_USER": "operator"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    write_calls = [line for line in calls.splitlines() if "jasper_env_file_set" in line]
    assert len(write_calls) == 1, calls
    recorded = write_calls[0].split("\t")[-1]

    expected = f"{prefix} {_remote_env_file_set_cmd(file, key, value, *modes)}"
    if chained:
        expected += f" && {_lib_function_output('restart_voice_and_verify_cmd')}"
    assert recorded == expected


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_pi_target_scripts_are_valid_bash(name: str) -> None:
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)
