# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-headphone-monitor.

The self-healing watcher for the Apple dongle's Headphone mixer control
(#4121): blocks on `alsactl monitor` instead of an amixer poll at 1 Hz,
falling back to the poll when `alsactl` is unavailable or its monitor
process dies. It is the one `while true` daemon under deploy/bin/, so it
is run as a real subprocess in its own process group (fake
`amixer`/`alsactl`/python emitter) and killed by process group once the
expected journal lines land — there is no way to observe this loop's
behavior other than running it.

The fake `alsactl` is addressed by absolute path (`JASPER_ALSACTL`, the
same override style as `JASPER_OUTPUT_HARDWARE_PYTHON`) rather than a
bare PATH lookup, to keep `exec 8< <("$ALSACTL" monitor)` resolving the
one binary these tests actually wrote.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-headphone-monitor"

_FAKE_AMIXER = """#!/usr/bin/env bash
# args: -c <card> sget|sset <control> [value] [unmute]
if [ "$3" = "sget" ]; then
    printf 'Simple mixer control %s\\n' "'Headphone',0"
    printf '  Front Left: Playback 96 [80%%] [-4.00dB] [on]\\n'
    exit 0
fi
if [ "$3" = "sset" ]; then
    printf 'event=fake_amixer_sset args=%s\\n' "$*" >> "$AMIXER_SSET_LOG"
    exit 0
fi
exit 0
"""

_FAKE_PYTHON = """#!/usr/bin/env bash
printf 'OBSERVED_OUTPUT_APPLE_CARD_IDS="USB_Audio"\\n'
printf 'OBSERVED_OUTPUT_HEADPHONE_CONTROL="Headphone"\\n'
"""

_FAKE_ALSACTL_BLOCKS = """#!/usr/bin/env bash
# A real `alsactl monitor` blocks until a control event; this fake just
# blocks (no events) so the probe exercises the timeout-is-alive path.
if [ "$1" = "monitor" ]; then
    sleep 600
fi
exit 1
"""

_FAKE_ALSACTL_EXITS_IMMEDIATELY = """#!/usr/bin/env bash
# Simulates "unusable": exits non-zero as soon as it is asked to monitor.
exit 1
"""


def _write_fakes(bin_dir: Path, *, alsactl: str | None) -> Path | None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "amixer").write_text(_FAKE_AMIXER)
    (bin_dir / "python3").write_text(_FAKE_PYTHON)
    (bin_dir / "amixer").chmod(0o755)
    (bin_dir / "python3").chmod(0o755)
    if alsactl is None:
        return None
    path = bin_dir / "alsactl"
    path.write_text(alsactl)
    path.chmod(0o755)
    return path


def _wait_for(predicate, *, timeout: float = 5.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _start(tmp_path: Path, env: dict) -> tuple[subprocess.Popen, Path]:
    log = tmp_path / "out.log"
    fh = open(log, "w")
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "auto"],
        stdout=fh, stderr=subprocess.STDOUT,
        env=env, cwd=tmp_path, start_new_session=True,
    )
    return proc, log


def _kill(proc: subprocess.Popen) -> None:
    """Kill the whole process group: `alsactl monitor` (real or this
    test's fake) is a grandchild via process substitution, so killing
    only the direct child would leak it."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def _run_and_capture(tmp_path: Path, env: dict, *, until: str, timeout: float = 5.0) -> str:
    """Wait for `until` in the journal AND for the amixer sset it precedes
    to actually land, before killing — `maybe_reset` prints the
    `action=reset_to_100` line BEFORE calling `amixer sset`, so killing
    the moment the line appears can kill that still-in-flight call too."""
    proc, log = _start(tmp_path, env)
    sset_log = tmp_path / "amixer_sset.log"
    try:
        _wait_for(
            lambda: until in log.read_text(), timeout=timeout,
            message=f"never saw {until!r} in:\n{log.read_text()}",
        )
        if until == "action=reset_to_100":
            _wait_for(
                lambda: sset_log.read_text().strip() != "",
                message="amixer sset was never called",
            )
        return log.read_text()
    finally:
        _kill(proc)


def _base_env(tmp_path: Path, bin_dir: Path, *, alsactl_path: Path | None) -> dict:
    sys_class_sound = tmp_path / "sys_class_sound" / "card0"
    proc_asound = tmp_path / "proc_asound" / "card0"
    sys_class_sound.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    (proc_asound / "id").write_text("USB_Audio\n")
    sset_log = tmp_path / "amixer_sset.log"
    sset_log.touch()
    if alsactl_path is None:
        # No real `alsactl` may be reachable here (this is the
        # "unavailable" case): a minimal PATH, deliberately not the
        # ambient one.
        path = f"{bin_dir}:/usr/bin:/bin"
    else:
        path = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    env = {
        **os.environ,
        "PATH": path,
        "JASPER_SYS_CLASS_SOUND": str(tmp_path / "sys_class_sound"),
        "JASPER_PROC_ASOUND": str(tmp_path / "proc_asound"),
        "AMIXER_SSET_LOG": str(sset_log),
    }
    env.pop("JASPER_ALSACTL", None)
    if alsactl_path is not None:
        env["JASPER_ALSACTL"] = str(alsactl_path)
    return env


def _assert_reset_was_delivered(tmp_path: Path) -> None:
    """The drift/reset-to-100 journal line is printed BEFORE the amixer
    sset call it describes, so wait for the sset log rather than reading
    it the instant the journal line lands."""
    sset_log = tmp_path / "amixer_sset.log"
    _wait_for(
        lambda: "event=fake_amixer_sset" in sset_log.read_text(),
        message="amixer sset was never called",
    )
    assert (
        "event=fake_amixer_sset args=-c USB_Audio sset Headphone 100% unmute"
        in sset_log.read_text()
    )


def test_falls_back_to_poll_when_alsactl_is_not_on_path(tmp_path):
    bin_dir = tmp_path / "bin"
    _write_fakes(bin_dir, alsactl=None)
    env = _base_env(tmp_path, bin_dir, alsactl_path=None)

    out = _run_and_capture(tmp_path, env, until="action=reset_to_100")

    assert "mode=poll reason=alsactl_unavailable" in out
    assert "mode=alsactl_monitor" not in out
    _assert_reset_was_delivered(tmp_path)


def test_falls_back_to_poll_when_alsactl_exits_immediately(tmp_path):
    """#4121: a binary that exists but exits as soon as it is asked to
    monitor must not hang the daemon — the same EOF check that catches a
    monitor dying mid-run catches this on the very first loop iteration."""
    bin_dir = tmp_path / "bin"
    alsactl_path = _write_fakes(bin_dir, alsactl=_FAKE_ALSACTL_EXITS_IMMEDIATELY)
    env = _base_env(tmp_path, bin_dir, alsactl_path=alsactl_path)

    out = _run_and_capture(tmp_path, env, until="action=reset_to_100")

    assert "mode=poll reason=alsactl_died" in out
    _assert_reset_was_delivered(tmp_path)


def test_uses_alsactl_monitor_when_available_and_still_self_heals(tmp_path):
    bin_dir = tmp_path / "bin"
    alsactl_path = _write_fakes(bin_dir, alsactl=_FAKE_ALSACTL_BLOCKS)
    env = _base_env(tmp_path, bin_dir, alsactl_path=alsactl_path)

    out = _run_and_capture(tmp_path, env, until="action=reset_to_100")

    assert "mode=alsactl_monitor" in out
    assert "mode=poll" not in out
    # The self-heal behavior itself is unchanged by the trigger mechanism.
    _assert_reset_was_delivered(tmp_path)


def test_falls_back_to_poll_if_the_monitor_process_dies_mid_run(tmp_path):
    """A killed `alsactl monitor` must not turn the blocking wait into a
    busy loop — the main loop's own EOF check falls back to polling. The
    defensive wait is set well past this test's own timeout, so a
    fallback here can only be the EOF check firing, not the periodic
    timeout coincidentally landing at the same time."""
    bin_dir = tmp_path / "bin"
    alsactl_path = _write_fakes(bin_dir, alsactl=_FAKE_ALSACTL_BLOCKS)
    env = _base_env(tmp_path, bin_dir, alsactl_path=alsactl_path)
    env["JASPER_ALSACTL_WAIT_TIMEOUT_SEC"] = "20"
    proc, log = _start(tmp_path, env)
    try:
        _wait_for(
            lambda: "mode=alsactl_monitor" in log.read_text(),
            message="daemon never reported alsactl_monitor mode",
        )
        subprocess.run(["pkill", "-9", "-f", f"{alsactl_path} monitor"], check=False)
        _wait_for(
            lambda: "mode=poll reason=alsactl_died" in log.read_text(),
            timeout=5.0,
            message="daemon never fell back after the monitor died",
        )
    finally:
        _kill(proc)


def test_stays_in_monitor_mode_across_a_defensive_timeout(tmp_path):
    """A `read -t` timeout on a live `alsactl monitor` (no event, no
    death) must NOT be mistaken for the monitor dying — that would
    permanently downgrade a healthy box to the 1 Hz poll this issue
    exists to remove."""
    bin_dir = tmp_path / "bin"
    alsactl_path = _write_fakes(bin_dir, alsactl=_FAKE_ALSACTL_BLOCKS)
    env = _base_env(tmp_path, bin_dir, alsactl_path=alsactl_path)
    env["JASPER_ALSACTL_WAIT_TIMEOUT_SEC"] = "1"
    proc, log = _start(tmp_path, env)
    try:
        _wait_for(
            lambda: "mode=alsactl_monitor" in log.read_text(),
            message="daemon never reported alsactl_monitor mode",
        )
        # Let several 1 s timeouts elapse with the fake still running.
        time.sleep(3)
        assert "mode=poll" not in log.read_text()
        assert subprocess.run(
            ["pgrep", "-f", f"{alsactl_path} monitor"], stdout=subprocess.DEVNULL
        ).returncode == 0, "the fake alsactl monitor should still be running"
    finally:
        _kill(proc)
