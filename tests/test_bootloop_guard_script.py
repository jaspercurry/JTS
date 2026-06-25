# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-bootloop-guard (audit C1).

Pure-bash policy script, tested via subprocess.run with a fake
systemctl that records its argv — same pattern as
tests/test_wifi_guardian_script.py / test_aec_reconcile.py.

Every seam is an env var:
  JASPER_BOOTLOOP_STATE_FILE   boot-timestamp history (plain epoch lines)
  JASPER_BOOTLOOP_MARKER_FILE  /state-readable JSON marker
  JASPER_BOOTLOOP_DROPIN_DIR   stands in for /run/systemd/system
  JASPER_BOOTLOOP_UNITS_DIR    stands in for /etc/systemd/system
  JASPER_BOOTLOOP_NOW          pinned clock for deterministic windows
  JASPER_SYSTEMCTL             fake systemctl
  JASPER_BOOTLOOP_WINDOW_SEC / JASPER_BOOTLOOP_THRESHOLD

Scenarios: healthy boots never trip; the threshold'th boot in the
window trips (drop-ins written only for StartLimitAction=reboot units +
daemon-reload + tripped marker); window pruning un-trips; corrupt state
fails open; the script exits 0 on every path (fail-open — it runs on
the boot path).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.install_surface import installer_text

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-bootloop-guard"

REBOOT_UNIT = """[Unit]
StartLimitIntervalSec=300
StartLimitBurst=4
StartLimitAction=reboot

[Service]
ExecStart=/bin/true
"""

PLAIN_UNIT = """[Unit]
StartLimitIntervalSec=300
StartLimitBurst=4

[Service]
ExecStart=/bin/true
"""


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.state_file = tmp_path / "state" / "bootloop_guard_boots"
        self.marker_file = tmp_path / "run" / "bootloop" / "state.json"
        self.dropin_dir = tmp_path / "run" / "systemd"
        self.units_dir = tmp_path / "etc-systemd"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.units_dir.mkdir(parents=True)
        fake = tmp_path / "fake-systemctl"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$*\" >> {self.systemctl_log}\n"
            "exit \"${JASPER_FAKE_SYSTEMCTL_RC:-0}\"\n"
        )
        fake.chmod(0o755)
        self.fake_systemctl = fake

    def add_unit(self, name: str, content: str) -> None:
        (self.units_dir / name).write_text(content)

    def run(
        self, *, now: int, window: int = 3600, threshold: int = 3,
        boot_id: str | None = None, reason: str = "test",
        systemctl_rc: int = 0,
    ):
        env = {
            "PATH": "/usr/bin:/bin",
            "JASPER_BOOTLOOP_STATE_FILE": str(self.state_file),
            "JASPER_BOOTLOOP_MARKER_FILE": str(self.marker_file),
            "JASPER_BOOTLOOP_DROPIN_DIR": str(self.dropin_dir),
            "JASPER_BOOTLOOP_UNITS_DIR": str(self.units_dir),
            "JASPER_BOOTLOOP_NOW": str(now),
            # Each run is a distinct boot unless the test pins boot_id
            # (the same-boot idempotency scenarios).
            "JASPER_BOOTLOOP_BOOT_ID": boot_id or f"boot-{now}",
            "JASPER_BOOTLOOP_WINDOW_SEC": str(window),
            "JASPER_BOOTLOOP_THRESHOLD": str(threshold),
            "JASPER_SYSTEMCTL": str(self.fake_systemctl),
            "JASPER_FAKE_SYSTEMCTL_RC": str(systemctl_rc),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "--reason", reason],
            env=env, capture_output=True, text=True, timeout=30,
        )

    def marker(self) -> dict:
        return json.loads(self.marker_file.read_text())

    def dropin_for(self, unit: str) -> Path:
        return self.dropin_dir / f"{unit}.d" / "90-jts-bootloop-guard.conf"

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text().splitlines()


def test_first_boot_is_healthy_and_armed(tmp_path):
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    r = h.run(now=1000)
    assert r.returncode == 0
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker() == {
        "tripped": False, "reload_ok": None,
        "boots_in_window": 1, "threshold": 3,
        "window_sec": 3600, "checked_at": 1000, "reason": "test",
        "units": ["jasper-camilla.service"],
    }
    assert not h.dropin_for("jasper-camilla.service").exists()
    assert h.systemctl_calls() == []


def test_threshold_boot_in_window_trips_and_writes_dropins(tmp_path):
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.add_unit("jasper-voice.service", REBOOT_UNIT)
    h.add_unit("jasper-snapserver.service", PLAIN_UNIT)  # no reboot action
    # Boots at t=0 and t=300 (the T5.1 loop cadence), trip check at t=600.
    h.run(now=0)
    h.run(now=300)
    r = h.run(now=600)
    assert r.returncode == 0
    assert "event=bootloop_guard.tripped" in r.stderr
    # Operator copy must carry the true StartLimitAction=none semantics:
    # the sick unit parks failed once its burst is exhausted (it does
    # NOT keep restart-looping), and reset-failed + start recovers it.
    assert "parks failed" in r.stderr
    assert "systemctl reset-failed" in r.stderr
    m = h.marker()
    assert m["tripped"] is True
    assert m["reload_ok"] is True
    assert m["boots_in_window"] == 3
    assert sorted(m["units"]) == [
        "jasper-camilla.service", "jasper-voice.service",
    ]
    for unit in ("jasper-camilla.service", "jasper-voice.service"):
        conf = h.dropin_for(unit).read_text()
        assert "[Unit]" in conf
        assert "StartLimitAction=none" in conf
    # Units without reboot escalation are left alone.
    assert not h.dropin_for("jasper-snapserver.service").exists()
    assert h.systemctl_calls() == ["daemon-reload"]


def test_daemon_reload_failure_is_not_reported_as_tripped(tmp_path):
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    h.run(now=300)
    r = h.run(now=600, systemctl_rc=1)
    assert r.returncode == 0
    assert "event=bootloop_guard.error reason=daemon_reload_failed" in r.stderr
    assert "event=bootloop_guard.tripped" not in r.stderr
    assert "note=daemon_reload_failed" in r.stderr
    marker = h.marker()
    assert marker["tripped"] is False
    assert marker["reload_ok"] is False
    assert marker["boots_in_window"] == 3
    assert not h.dropin_for("jasper-camilla.service").exists()
    assert h.systemctl_calls() == ["daemon-reload"]


def test_missing_reason_value_exits_instead_of_spinning(tmp_path):
    h = Harness(tmp_path)
    r = subprocess.run(
        ["bash", str(SCRIPT), "--reason"],
        env={
            "PATH": "/usr/bin:/bin",
            "JASPER_BOOTLOOP_STATE_FILE": str(h.state_file),
            "JASPER_BOOTLOOP_MARKER_FILE": str(h.marker_file),
            "JASPER_BOOTLOOP_DROPIN_DIR": str(h.dropin_dir),
            "JASPER_BOOTLOOP_UNITS_DIR": str(h.units_dir),
            "JASPER_BOOTLOOP_NOW": "1000",
            "JASPER_SYSTEMCTL": str(h.fake_systemctl),
        },
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert r.returncode == 2
    assert "Usage: jasper-bootloop-guard" in r.stderr
    assert not h.marker_file.exists()


def test_reason_shift_two_idiom_is_guarded_in_sibling_scripts():
    scripts = [
        ROOT / "deploy" / "bin" / "jasper-aec-reconcile",
        ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile",
        ROOT / "deploy" / "bin" / "jasper-bootloop-guard",
        ROOT / "deploy" / "bin" / "jasper-identity-reconcile",
        ROOT / "deploy" / "bin" / "jasper-wifi-guardian",
    ]
    for script in scripts:
        lines = script.read_text().splitlines()
        for idx, line in enumerate(lines):
            if "shift 2" not in line:
                continue
            window = "\n".join(lines[max(0, idx - 3):idx])
            assert "[[ $# -ge 2 ]]" in window, f"{script} has unguarded shift 2"


def test_bootloop_guard_avoids_bash_44_q_expansion():
    assert "@Q" not in SCRIPT.read_text()


def test_marker_write_uses_tempfile_then_rename_pattern():
    text = SCRIPT.read_text()
    assert ".bootloop_guard_marker." in text
    assert 'mv -f "$tmp" "$MARKER_FILE"' in text


def test_boots_outside_window_are_pruned_and_do_not_trip(tmp_path):
    """Three power-cycles spread over hours (normal household behaviour
    over a day) never trip — only a tight loop does."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    h.run(now=2000)
    r = h.run(now=5000)  # boot at t=0 has aged out of the 3600 s window
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["tripped"] is False
    assert h.marker()["boots_in_window"] == 2
    assert not h.dropin_for("jasper-camilla.service").exists()


def test_corrupt_state_file_fails_open_to_fresh_history(tmp_path):
    """A torn write / garbage in the history must reset history (count
    restarts at 1), never crash or block boot."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.state_file.parent.mkdir(parents=True)
    h.state_file.write_bytes(b"\x00garbage\nnot-a-number\n12.5\n")
    r = h.run(now=1000)
    assert r.returncode == 0
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["boots_in_window"] == 1


def test_future_timestamps_from_clock_jump_are_dropped(tmp_path):
    """NTP not yet synced on a previous boot can record a future
    timestamp; it must not count toward the loop verdict."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.state_file.parent.mkdir(parents=True)
    h.state_file.write_text("999999999\n999999998\n")
    r = h.run(now=1000)
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["boots_in_window"] == 1
    # Dropping history is no longer silent — one structured line names
    # the cause and count (clock_jump only; window pruning is the
    # designed steady state and stays quiet).
    assert (
        "event=bootloop_guard.history_dropped reason=clock_jump count=2"
        in r.stderr
    )


def test_window_pruning_stays_quiet(tmp_path):
    """Aging out of the window is normal behaviour, not an anomaly —
    no history_dropped line for it."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    r = h.run(now=5000)  # boot at t=0 pruned from the 3600 s window
    assert "event=bootloop_guard.ok" in r.stderr
    assert "history_dropped" not in r.stderr


def test_trip_with_no_reboot_units_is_a_noop(tmp_path):
    """All escalation already removed from the units (or none installed):
    nothing to disarm, no daemon-reload, marker stays untripped."""
    h = Harness(tmp_path)
    h.add_unit("jasper-snapserver.service", PLAIN_UNIT)
    h.run(now=0)
    h.run(now=300)
    r = h.run(now=600)
    assert r.returncode == 0
    assert "note=no_reboot_units" in r.stderr
    assert h.marker()["tripped"] is False
    assert h.systemctl_calls() == []


def test_recovery_boot_after_window_drains_rearms(tmp_path):
    """Operator fixes the daemon; once the window drains, the next boot
    is healthy again and writes no drop-ins (and the real /run drop-ins
    were wiped by the reboot itself)."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    h.run(now=300)
    h.run(now=600)            # trips
    assert h.marker()["tripped"] is True
    r = h.run(now=600 + 4000)  # next boot, window drained
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["tripped"] is False


def test_unwritable_state_dir_fails_open_with_exit_zero(tmp_path):
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, not a dir")
    env_state = blocked / "nested" / "boots"
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "JASPER_BOOTLOOP_STATE_FILE": str(env_state),
            "JASPER_BOOTLOOP_MARKER_FILE": str(h.marker_file),
            "JASPER_BOOTLOOP_DROPIN_DIR": str(h.dropin_dir),
            "JASPER_BOOTLOOP_UNITS_DIR": str(h.units_dir),
            "JASPER_BOOTLOOP_NOW": "1000",
            "JASPER_SYSTEMCTL": str(h.fake_systemctl),
        },
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    assert "event=bootloop_guard.error" in r.stderr


def test_repo_reboot_units_carry_the_exact_grep_literal():
    """The guard discovers units by grepping for the exact line
    `StartLimitAction=reboot`. Pin that the shipped units use that exact
    spelling so a formatting drift can't silently un-guard them."""
    systemd_dir = ROOT / "deploy" / "systemd"
    guarded = sorted(
        p.name for p in systemd_dir.glob("*.service")
        if "\nStartLimitAction=reboot\n" in p.read_text()
    )
    assert guarded == [
        "jasper-aec-bridge.service",
        "jasper-control.service",
        "jasper-fanin.service",
        "jasper-outputd.service",
        "jasper-voice.service",
    ]


def test_guard_unit_is_ordered_before_every_guarded_unit():
    unit = (ROOT / "deploy" / "systemd" / "jasper-bootloop-guard.service").read_text()
    for name in (
        "jasper-aec-bridge.service", "jasper-control.service",
        "jasper-fanin.service", "jasper-outputd.service",
        "jasper-voice.service",
    ):
        assert name in unit, f"{name} missing from Before= ordering"
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=" in unit


def test_install_sh_installs_and_enables_the_guard():
    text = installer_text()
    assert "deploy/systemd/jasper-bootloop-guard.service" in text
    assert "deploy/bin/jasper-bootloop-guard" in text
    assert "systemctl enable jasper-bootloop-guard.service" in text


def test_state_snapshot_reads_marker_fresh(tmp_path, monkeypatch):
    """/state surface: jasper.control.bootloop_guard_state reads the
    marker written by the bash script, fail-soft on absence."""
    from jasper.control import bootloop_guard_state

    marker = tmp_path / "state.json"
    monkeypatch.setenv("JASPER_BOOTLOOP_MARKER_FILE", str(marker))
    assert bootloop_guard_state.snapshot() == {"ran": False}

    h = Harness(tmp_path)
    h.marker_file = marker
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    h.run(now=300)
    h.run(now=600)
    snap = bootloop_guard_state.snapshot()
    assert snap["ran"] is True
    assert snap["tripped"] is True
    assert snap["reload_ok"] is True
    assert snap["boots_in_window"] == 3
    assert snap["units"] == ["jasper-camilla.service"]

    marker.write_text("{torn")
    assert bootloop_guard_state.snapshot() == {"ran": False}


def test_rerun_within_the_same_boot_is_idempotent(tmp_path):
    """An operator re-running the guard mid-diagnosis (or a unit
    retrigger) must not inflate the boot count toward a false trip —
    dedupe is keyed on the kernel boot_id."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    h.run(now=0)
    h.run(now=10, boot_id="boot-0")    # same boot, later wall-clock
    r = h.run(now=20, boot_id="boot-0")
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["boots_in_window"] == 1
    assert not h.dropin_for("jasper-camilla.service").exists()


def test_marker_json_survives_hostile_reason(tmp_path):
    """--reason is interpolated into the marker JSON; quotes/backslashes
    must not be able to break /state's parse. The script restricts the
    value to a safe charset (hostile bytes degrade to underscores)."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    r = h.run(now=1000, reason='a"b\\c\nd')
    assert r.returncode == 0
    m = h.marker()  # json.loads — the actual property under test
    assert m["reason"] == "a_b_c_d"
    assert m["tripped"] is False


def test_garbage_tuning_env_falls_back_to_defaults(tmp_path):
    """Bad WINDOW/THRESHOLD values must fail open to defaults (bash
    arithmetic on a non-integer would otherwise bias toward tripping)."""
    h = Harness(tmp_path)
    h.add_unit("jasper-camilla.service", REBOOT_UNIT)
    r = h.run(now=1000, window="bogus", threshold="0")  # type: ignore[arg-type]
    assert r.returncode == 0
    assert "reason=bad_window" in r.stderr
    assert "reason=bad_threshold" in r.stderr
    assert "event=bootloop_guard.ok" in r.stderr
    assert h.marker()["threshold"] == 3
    assert h.marker()["window_sec"] == 3600
    assert not h.dropin_for("jasper-camilla.service").exists()
