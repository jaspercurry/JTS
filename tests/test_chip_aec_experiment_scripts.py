# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free contracts for the chip-AEC experiment shell control plane."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import textwrap
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "_chip_aec_experiment_lib.sh"
CALLERS = (
    SCRIPTS / "chip-aec-baseline-check.sh",
    SCRIPTS / "chip-aec-capture-comparison.sh",
)
ROBUST_SCRIPT_DIR = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'


def _run_bash(
    body: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["bash", "-c", body],
        cwd=ROOT,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _source_prelude() -> str:
    return textwrap.dedent(
        f"""\
        set -euo pipefail
        SCRIPT_DIR={shlex_quote(str(SCRIPTS))}
        . "$SCRIPT_DIR/_lib.sh"
        . "$SCRIPT_DIR/_chip_aec_experiment_lib.sh"
        PI_HOST=test.invalid
        PI_USER=operator
        REF_DELAY_MS=0
        MIC_CHANNEL=0
        """
    )


def shlex_quote(value: str) -> str:
    """Quote a test-owned path for a bash snippet without another dependency."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("path", (*CALLERS, HELPER))
def test_chip_aec_shell_files_parse_and_are_shellcheck_clean(path: Path) -> None:
    parse = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, timeout=10
    )
    assert parse.returncode == 0, parse.stderr

    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    checked = subprocess.run(
        [shellcheck, "-x", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert checked.returncode == 0, checked.stdout


@pytest.mark.parametrize("path", CALLERS)
def test_callers_source_shared_owners_and_have_no_local_helper_copies(
    path: Path,
) -> None:
    source = path.read_text(encoding="utf-8")

    assert source.count(ROBUST_SCRIPT_DIR) == 1
    assert source.count('. "${SCRIPT_DIR}/_lib.sh"') == 1
    assert source.count('. "${SCRIPT_DIR}/_chip_aec_experiment_lib.sh"') == 1
    assert source.index("set -euo pipefail") < source.index(ROBUST_SCRIPT_DIR)
    assert source.index('_lib.sh"') < source.index('_chip_aec_experiment_lib.sh"')
    assert not re.search(r"(?m)^PI_(?:HOST|USER)=", source)
    assert not re.search(
        r"(?m)^(?:prompt|ssh_pi|set_bypass|daemon_set_mode)\(\)", source
    )
    assert not re.search(r"(?m)^\s*(?:if ! )?ssh\s", source)
    assert source.count("chip_aec_install_restore_trap") == 1
    assert source.count("chip_aec_enter_ref_only") == 1
    assert source.count("chip_aec_restore_full") == 1


def test_prompt_policy_and_baseline_restore_order_are_preserved() -> None:
    baseline = CALLERS[0].read_text(encoding="utf-8")
    comparison = CALLERS[1].read_text(encoding="utf-8")

    assert baseline.count("ASSUME_READY") == 2  # documented env + call-site policy
    assert 'if [[ "${ASSUME_READY:-0}" != "1" ]]; then' in baseline
    assert baseline.count("chip_aec_prompt ") == 1
    assert baseline.index("chip_aec_restore_full") < baseline.index(
        'echo "==> Analyzing baseline captures"'
    )

    assert "ASSUME_READY" not in comparison
    assert comparison.count('chip_aec_prompt "STEP ') == 3


@pytest.fixture
def fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "ssh.log"
    ssh = fake_bin / "ssh"
    ssh.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$@" > "$FAKE_SSH_LOG"
            exit "${FAKE_SSH_RC:-0}"
            """
        ),
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    return fake_bin, log


@pytest.mark.parametrize(
    ("mode", "expected_flag"), (("full", False), ("ref-only", True))
)
def test_daemon_lifecycle_command_keeps_all_restart_guards(
    fake_ssh: tuple[Path, Path], mode: str, expected_flag: bool
) -> None:
    fake_bin, log = fake_ssh
    result = _run_bash(
        _source_prelude() + f"_chip_aec_daemon_set_mode {mode}\n",
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(log),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    args = log.read_text(encoding="utf-8").splitlines()
    assert args[0] == "operator@test.invalid"
    remote = "\n".join(args[1:])
    for contract in (
        "sudo pkill -f",
        "for _ in 1 2 3 4 5 6 7 8 9 10",
        "sudo pkill -9 -f",
        "sudo bash -c",
        "sleep 2",
        "boundary + 1",
        "(ref feeder|mic pump) open failed",
    ):
        assert contract in remote
    assert ("--ref-only" in remote) is expected_flag
    assert "jasper.chip_aec_experiment" not in remote


@pytest.fixture
def executing_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "remote-bin"
    fake_bin.mkdir()
    state = tmp_path / "daemon.state"
    events = tmp_path / "remote-events.log"

    _write_executable(
        fake_bin / "ssh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            remote="${*: -1}"
            bash -c "$remote"
            """
        ),
    )
    _write_executable(
        fake_bin / "pgrep",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "$(cat "$FAKE_DAEMON_STATE" 2>/dev/null || true)" != "none" ]]; then
                printf '4242\n'
                exit 0
            fi
            exit 1
            """
        ),
    )
    _write_executable(
        fake_bin / "pkill",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'pkill:%s\n' "$*" >> "$FAKE_REMOTE_EVENTS"
            printf 'none\n' > "$FAKE_DAEMON_STATE"
            """
        ),
    )
    _write_executable(
        fake_bin / "sudo",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            case "$1" in
                pkill)
                    exec "$@"
                    ;;
                bash)
                    printf 'start:%s\n' "$*" >> "$FAKE_REMOTE_EVENTS"
                    if [[ "${FAKE_NEW_DAEMON_FAIL:-0}" != "1" ]]; then
                        printf 'running\n' > "$FAKE_DAEMON_STATE"
                    fi
                    ;;
                tail)
                    exit 0
                    ;;
                *)
                    exec "$@"
                    ;;
            esac
            """
        ),
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    return fake_bin, state, events


def _run_executing_remote(
    fixture: tuple[Path, Path, Path], *, fail_new: bool
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    fake_bin, state, events = fixture
    state.write_text("stale\n", encoding="utf-8")
    result = _run_bash(
        _source_prelude() + "_chip_aec_daemon_set_mode full\n",
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DAEMON_STATE": str(state),
            "FAKE_REMOTE_EVENTS": str(events),
            "FAKE_NEW_DAEMON_FAIL": "1" if fail_new else "0",
        },
    )
    event_lines = (
        events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    )
    return result, event_lines, state.read_text(encoding="utf-8").strip()


def test_remote_controller_terminates_stale_daemon_then_owns_new_process(
    executing_remote: tuple[Path, Path, Path],
) -> None:
    result, events, state = _run_executing_remote(executing_remote, fail_new=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert events[0].startswith("pkill:-f ^/opt/jasper/[.]venv/bin/python -m")
    assert any("-m jasper.chip_aec_experiment" in event for event in events)
    assert state == "running"
    assert "daemon now in full mode (PID 4242)" in result.stdout


def test_remote_controller_detects_failed_new_daemon_after_stale_termination(
    executing_remote: tuple[Path, Path, Path],
) -> None:
    result, events, state = _run_executing_remote(executing_remote, fail_new=True)

    assert result.returncode == 1
    assert events[0].startswith("pkill:-f ^/opt/jasper/[.]venv/bin/python -m")
    assert any(event.startswith("start:bash -c") for event in events)
    assert state == "none"
    assert "FAILED to restart daemon in full mode" in result.stdout


@pytest.mark.parametrize(
    "command",
    (
        "chip_aec_set_bypass 2",
        "_chip_aec_daemon_set_mode invalid",
        "MIC_CHANNEL=6; _chip_aec_daemon_set_mode full",
        "MIC_CHANNEL='0; false'; _chip_aec_daemon_set_mode full",
    ),
)
def test_invalid_control_values_fail_before_ssh(
    fake_ssh: tuple[Path, Path], command: str
) -> None:
    fake_bin, log = fake_ssh
    result = _run_bash(
        _source_prelude() + command + "\n",
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(log),
        },
    )

    assert result.returncode == 2
    assert not log.exists()


def test_bypass_ssh_failure_is_not_masked_by_success_message(
    fake_ssh: tuple[Path, Path],
) -> None:
    fake_bin, log = fake_ssh
    result = _run_bash(
        _source_prelude() + "chip_aec_set_bypass 0\n",
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(log),
            "FAKE_SSH_RC": "17",
        },
    )

    assert result.returncode == 17
    assert log.exists()
    assert "SHF_BYPASS = 0" not in result.stdout


def test_ref_delay_is_one_literal_daemon_argument(
    fake_ssh: tuple[Path, Path], tmp_path: Path
) -> None:
    fake_bin, log = fake_ssh
    marker = tmp_path / "injection-marker"
    hostile = f"$(touch {marker})"
    result = _run_bash(
        _source_prelude()
        + f"REF_DELAY_MS={shlex_quote(hostile)}\n"
        + "_chip_aec_daemon_set_mode full\n",
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SSH_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

    remote = "\n".join(log.read_text(encoding="utf-8").splitlines()[1:])
    launch_line = next(
        line.strip()
        for line in remote.splitlines()
        if line.strip().startswith("sudo bash -c ")
    )
    outer_word = launch_line.removeprefix("sudo bash -c ")
    parsed = _run_bash(
        "module=jasper.chip_aec_experiment; "
        f"set -- {outer_word}; printf '%s\\0' \"$@\"\n"
    )
    assert parsed.returncode == 0, parsed.stderr
    argv = parsed.stdout.split("\0")[:-1]
    assert argv[argv.index("--ref-delay-ms") + 1] == hostile
    assert not marker.exists()


def _restore_harness(
    tmp_path: Path,
    *,
    body: str,
    bypass_rc: int = 0,
    ref_only_rc: int = 0,
    full_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    log = tmp_path / "restore.log"
    harness = _source_prelude() + textwrap.dedent(
        f"""\
        chip_aec_set_bypass() {{
            printf 'bypass:%s\\n' "$1" >> {shlex_quote(str(log))}
            return {bypass_rc}
        }}
        _chip_aec_daemon_set_mode() {{
            printf 'daemon:%s\\n' "$1" >> {shlex_quote(str(log))}
            if [[ "$1" == "full" ]]; then return {full_rc}; fi
            return {ref_only_rc}
        }}
        chip_aec_install_restore_trap
        {body}
        """
    )
    result = _run_bash(harness)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_exit_failure_restores_state_and_preserves_original_status(
    tmp_path: Path,
) -> None:
    result, calls = _restore_harness(tmp_path, body="chip_aec_enter_ref_only\nexit 23")

    assert result.returncode == 23
    assert calls == ["daemon:ref-only", "bypass:0", "daemon:full"]


def test_ref_only_failure_is_armed_before_mutation_and_restored(tmp_path: Path) -> None:
    result, calls = _restore_harness(
        tmp_path,
        body="chip_aec_enter_ref_only",
        ref_only_rc=9,
    )

    assert result.returncode == 9
    assert calls == ["daemon:ref-only", "bypass:0", "daemon:full"]


def test_restore_attempts_full_daemon_when_bypass_restore_fails(tmp_path: Path) -> None:
    result, calls = _restore_harness(
        tmp_path,
        body="chip_aec_enter_ref_only\nexit 23",
        bypass_rc=7,
    )

    assert result.returncode == 23
    assert calls == ["daemon:ref-only", "bypass:0", "daemon:full"]
    assert "SHF_BYPASS rc=7, daemon rc=0" in result.stderr


def test_restore_failure_makes_otherwise_successful_exit_fail(tmp_path: Path) -> None:
    result, calls = _restore_harness(
        tmp_path,
        body="chip_aec_enter_ref_only\nexit 0",
        full_rc=8,
    )

    assert result.returncode == 8
    assert calls == ["daemon:ref-only", "bypass:0", "daemon:full"]
    assert "SHF_BYPASS rc=0, daemon rc=8" in result.stderr


def test_explicit_restore_disarms_exit_trap_without_duplicate_restart(
    tmp_path: Path,
) -> None:
    result, calls = _restore_harness(
        tmp_path,
        body="chip_aec_enter_ref_only\nchip_aec_restore_full\nexit 0",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == ["daemon:ref-only", "bypass:0", "daemon:full"]
    assert result.stdout.count("Restoring full chip-AEC experiment state") == 1


@pytest.mark.parametrize(
    ("sent_signal", "expected_status"),
    (
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ),
)
def test_process_group_signal_restores_bypass_and_full_daemon(
    tmp_path: Path,
    sent_signal: signal.Signals,
    expected_status: int,
) -> None:
    log = tmp_path / "signal-restore.log"
    ready = tmp_path / "ready"
    harness = _source_prelude() + textwrap.dedent(
        f"""\
        chip_aec_set_bypass() {{
            printf 'bypass:%s\\n' "$1" >> {shlex_quote(str(log))}
        }}
        _chip_aec_daemon_set_mode() {{
            printf 'daemon:%s\\n' "$1" >> {shlex_quote(str(log))}
        }}
        chip_aec_install_restore_trap
        chip_aec_enter_ref_only
        printf 'ready\\n' > {shlex_quote(str(ready))}
        while :; do sleep 30; done
        """
    )
    proc = subprocess.Popen(
        ["bash", "-c", harness],
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not ready.exists() and proc.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists(), proc.communicate(timeout=1)
        os.killpg(proc.pid, sent_signal)
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)

    assert proc.returncode == expected_status, stdout + stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "daemon:ref-only",
        "bypass:0",
        "daemon:full",
    ]
    assert stdout.count("Restoring full chip-AEC experiment state") == 1
