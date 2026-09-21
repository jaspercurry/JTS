# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for deploy/bin/jasper-usb-hcd-recover and its unit.

The helper re-binds a USB host controller the kernel tore down (#5443). It
writes to a sysfs driver directory, so what it may write, how often, in which
order, and what it reports are the behaviours worth pinning.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.install_surface import installer_text
from tests.systemd_unit_helpers import exec_argv_for, value_for, values_for

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-usb-hcd-recover"
UNIT = ROOT / "deploy" / "systemd" / "jasper-usb-hcd-recover.service"
AUTOSTOP_UNIT = ROOT / "deploy" / "systemd" / "jasper-turntable-autostop@.service"

DEATH = "xhci-hcd xhci-hcd.0: HC died; cleaning up\n"
REGISTERED = "xhci-hcd xhci-hcd.0: new USB bus registered, assigned bus number 1\n"
# Verbatim boot lines that name a controller but are NOT a death (#5444's corpus).
BOOT_LINES = (
    "xhci-hcd xhci-hcd.0: xHCI Host Controller\n"
    "xhci-hcd xhci-hcd.0: new USB bus registered, assigned bus number 1\n"
)


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Harness:
    """A fake platform-driver tree plus a fake kernel log.

    The fake ``sleep`` is the seam that lets the kernel's side of a re-bind be
    simulated: the script sleeps between its unbind and its bind, exactly where
    the real driver would tear the device down, so ``SLEEP_HOOK`` can remove the
    driver's entry there. No production seam exists for this.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir(parents=True)
        self.driver_dir = tmp_path / "drivers" / "xhci-hcd"
        self.driver_dir.mkdir(parents=True)
        (self.driver_dir / "xhci-hcd.0").mkdir()
        self.unbind = self.driver_dir / "unbind"
        self.bind = self.driver_dir / "bind"
        self.unbind.write_text("", encoding="utf-8")
        self.bind.write_text("", encoding="utf-8")
        self.state_dir = tmp_path / "state"
        self.inhibit = tmp_path / "rebinding"
        self.journal_log = tmp_path / "journalctl.log"
        self.counter = tmp_path / "confirm.count"
        self.journalctl = _write_exec(
            self.bin_dir / "journalctl",
            # Three call shapes, keyed on the grep pattern the script passes.
            'printf \'%s\\n\' "$*" >> "$JOURNAL_LOG"\n'
            'if [[ "$*" == *" -f "* ]]; then\n'
            '    printf \'%s\' "${JOURNAL_FOLLOW:-}"\n'
            "    exit 0\n"
            "fi\n"
            'if [[ "$*" == *"--grep xhci-hcd"* ]]; then\n'
            '    printf \'%s\' "${JOURNAL_CATCHUP:-}"\n'
            "    exit 0\n"
            "fi\n"
            # Confirmation read: the Nth call onward reports the bus back.
            'n=0\n'
            '[[ -r "$CONFIRM_COUNT" ]] && read -r n < "$CONFIRM_COUNT"\n'
            'n=$(( n + 1 ))\n'
            'printf \'%s\\n\' "$n" > "$CONFIRM_COUNT"\n'
            '(( n >= ${JOURNAL_CONFIRM_AFTER:-1} )) '
            '&& printf \'%s\' "${JOURNAL_CONFIRM:-}"\n'
            '(( ${JOURNAL_CONFIRM_FILLER:-0} > 0 )) || exit 0\n'
            'seq 1 "${JOURNAL_CONFIRM_FILLER}"\n'
            "exit $?\n",
        )
        _write_exec(
            self.bin_dir / "sleep",
            'if [[ -n "${SLEEP_HOOK:-}" && ! -e "$SLEEP_HOOK_DONE" ]]; then\n'
            '    : > "$SLEEP_HOOK_DONE"\n'
            '    eval "$SLEEP_HOOK"\n'
            "fi\n"
            "exit 0\n",
        )
        self.sleep_hook_done = tmp_path / "sleep_hook.done"

    def seed_recoveries(self, controller: str, count: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / controller).write_text(f"{count}\n", encoding="utf-8")

    def run(
        self,
        *,
        follow: str = "",
        confirm: str = "",
        catchup: str = "",
        confirm_tries: int = 2,
        # The script reads the re-registration count BEFORE it touches the
        # driver, to compare against. Call 1 is that baseline, so the default
        # makes the line appear from the first post-bind read onward.
        confirm_after: int = 2,
        confirm_filler: int = 0,
        sleep_hook: str = "",
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin_dir}:{env['PATH']}",
            "JASPER_USB_HCD_DRIVER_DIR": str(self.driver_dir),
            "JASPER_USB_HCD_JOURNALCTL": str(self.journalctl),
            "JASPER_USB_HCD_STATE_DIR": str(self.state_dir),
            "JASPER_USB_HCD_INHIBIT_MARKER": str(self.inhibit),
            "JASPER_USB_HCD_SETTLE_SECONDS": "0",
            "JASPER_USB_HCD_CONFIRM_TRIES": str(confirm_tries),
            "JASPER_USB_HCD_POLL_SECONDS": "0",
            "JOURNAL_LOG": str(self.journal_log),
            "JOURNAL_FOLLOW": follow,
            "JOURNAL_CONFIRM": confirm,
            "JOURNAL_CATCHUP": catchup,
            "JOURNAL_CONFIRM_AFTER": str(confirm_after),
            "JOURNAL_CONFIRM_FILLER": str(confirm_filler),
            "CONFIRM_COUNT": str(self.counter),
            "SLEEP_HOOK": sleep_hook,
            "SLEEP_HOOK_DONE": str(self.sleep_hook_done),
        })
        return subprocess.run(
            ["bash", str(SCRIPT)],
            check=False, cwd=ROOT, env=env, text=True,
            capture_output=True, timeout=60,
        )

    def unbind_becomes_unwritable(self) -> str:
        """Shell that removes the device entry AND makes any further unbind
        write fail — so a second unbind cannot pass unnoticed."""
        return (
            f"rm -rf {self.driver_dir / 'xhci-hcd.0'}; "
            f"rm -f {self.unbind}; mkdir {self.unbind}"
        )


def test_a_death_line_re_binds_the_named_controller_and_reports_recovered(tmp_path):
    h = Harness(tmp_path)

    proc = h.run(follow=DEATH, confirm=REGISTERED)

    assert h.unbind.read_text() == "xhci-hcd.0"
    assert h.bind.read_text() == "xhci-hcd.0"
    assert "event=usb_hcd_recover.recovered controller=0 attempt=1" in proc.stderr


@pytest.mark.parametrize(
    "line",
    [
        "xhci-hcd xhci-hcd.0: xHCI Host Controller\n",
        "xhci-hcd xhci-hcd.0: new USB bus registered, assigned bus number 1\n",
        "usb 1-2: USB disconnect, device number 4\n",
    ],
    ids=["host-controller-banner", "bus-registered", "device-disconnect"],
)
def test_a_line_that_is_not_a_death_never_reaches_sysfs(tmp_path, line):
    """--grep is the kernel log's filter, not this script's contract. Any line
    arriving on the pipe must be re-checked against the death marker."""
    h = Harness(tmp_path)

    proc = h.run(follow=line, confirm=REGISTERED)

    assert h.unbind.read_text() == ""
    assert h.bind.read_text() == ""
    assert "recovered" not in proc.stderr


@pytest.mark.parametrize(
    "line",
    [
        "xhci-hcd: HC died; cleaning up\n",
        "xhci-hcd xhci-hcd.7: HC died; cleaning up\n",
    ],
    ids=["no-controller-token", "index-the-driver-does-not-have"],
)
def test_a_controller_the_driver_does_not_have_is_never_written_to_sysfs(tmp_path, line):
    h = Harness(tmp_path)

    proc = h.run(follow=line, confirm=REGISTERED)

    assert h.unbind.read_text() == ""
    assert h.bind.read_text() == ""
    assert "event=usb_hcd_recover.ignored controller=none attempt=0" in proc.stderr


def test_a_retry_after_an_unbound_controller_binds_without_a_second_unbind(tmp_path):
    """An unbind whose bind did not take leaves the root hubs deregistered —
    worse than `HC died`. The retry must go straight to bind; a second unbind
    would take ENODEV and strand the controller there."""
    h = Harness(tmp_path)

    proc = h.run(
        follow=DEATH,
        confirm=REGISTERED,
        confirm_tries=1,
        # Reads are baseline, confirm, baseline, confirm: attempt 1's
        # confirmation comes back empty so it fails after its bind, and only
        # attempt 2's reports the bus back.
        confirm_after=4,
        sleep_hook=h.unbind_becomes_unwritable(),
    )

    assert "step=unbind" not in proc.stderr
    assert "event=usb_hcd_recover.recovered controller=0 attempt=2" in proc.stderr


def test_a_controller_left_unbound_gets_its_own_result(tmp_path):
    h = Harness(tmp_path)
    h.bind.unlink()
    h.bind.mkdir()

    proc = h.run(
        follow=DEATH, confirm=REGISTERED, confirm_tries=1,
        sleep_hook=f"rm -rf {h.driver_dir / 'xhci-hcd.0'}",
    )

    assert "event=usb_hcd_recover.left_unbound controller=0" in proc.stderr
    assert "step=bind" in proc.stderr


def test_a_rejected_sysfs_write_reports_the_kernels_own_error(tmp_path):
    h = Harness(tmp_path)
    h.bind.unlink()
    h.bind.mkdir()

    proc = h.run(follow=DEATH, confirm=REGISTERED, confirm_tries=1)

    assert "step=bind" in proc.stderr
    assert 'error="' in proc.stderr


def test_a_journal_larger_than_a_pipe_buffer_still_confirms_the_re_bind(tmp_path):
    """The confirmation may not read the journal through a pipe into an
    early-exiting matcher: the producer takes SIGPIPE and `pipefail` turns a
    successful re-bind into a reported failure plus a second, needless one."""
    h = Harness(tmp_path)

    proc = h.run(follow=DEATH, confirm=REGISTERED, confirm_filler=60_000)

    assert "event=usb_hcd_recover.recovered controller=0 attempt=1" in proc.stderr
    assert "failed" not in proc.stderr


def test_a_bus_that_never_re_registers_is_retried_once_then_reported_failed(tmp_path):
    h = Harness(tmp_path)

    proc = h.run(follow=DEATH, confirm="")

    assert "event=usb_hcd_recover.failed controller=0 attempt=1" in proc.stderr
    assert "event=usb_hcd_recover.failed controller=0 attempt=2" in proc.stderr
    assert "attempt=3" not in proc.stderr
    assert (h.state_dir / "xhci-hcd.0").read_text().strip() == "2"


def test_the_per_boot_cap_stops_re_binding_and_reports_capped(tmp_path):
    h = Harness(tmp_path)
    h.seed_recoveries("xhci-hcd.0", 3)

    proc = h.run(follow=DEATH, confirm=REGISTERED)

    assert h.unbind.read_text() == ""
    assert h.bind.read_text() == ""
    assert "event=usb_hcd_recover.capped controller=0 attempt=1" in proc.stderr


def test_a_death_that_predates_the_watcher_is_recovered_at_startup(tmp_path):
    """A death at boot, or inside a RestartSec gap, never reaches a follow that
    deliberately starts at the end of the log. Last marker wins, the same rule
    the doctor row applies (#5444)."""
    h = Harness(tmp_path)

    proc = h.run(catchup=BOOT_LINES + DEATH, confirm=REGISTERED)

    assert "event=usb_hcd_recover.catchup controller=0" in proc.stderr
    assert h.bind.read_text() == "xhci-hcd.0"
    assert "event=usb_hcd_recover.recovered controller=0 attempt=1" in proc.stderr


def test_a_controller_whose_last_marker_is_live_is_left_alone_at_startup(tmp_path):
    h = Harness(tmp_path)

    proc = h.run(catchup=BOOT_LINES + DEATH + REGISTERED, confirm=REGISTERED)

    assert h.unbind.read_text() == ""
    assert h.bind.read_text() == ""
    assert "catchup" not in proc.stderr


def test_the_turntable_inhibit_is_dropped_once_the_re_bind_window_closes(tmp_path):
    """The marker parks jasper-turntable-autostop@ for one hot-plug. A marker
    that outlived the window would park it for the rest of the boot."""
    h = Harness(tmp_path)
    h.inhibit.write_text("stale\n", encoding="utf-8")

    h.run(follow=DEATH, confirm=REGISTERED)

    assert not h.inhibit.exists()


def test_the_follow_starts_at_the_end_of_the_journal(tmp_path):
    """`journalctl -f` replays the last ten matches by default. Without `-n 0`
    every start of this unit would re-bind a healthy controller off the death
    line of an incident that is already over; the startup catch-up is what
    covers that gap instead, once and on purpose."""
    h = Harness(tmp_path)

    h.run()

    follow_argv = [
        line for line in h.journal_log.read_text().splitlines() if " -f " in line
    ]
    assert len(follow_argv) == 1
    assert "-n 0" in follow_argv[0]


def test_the_confirmation_does_not_depend_on_a_wall_clock_cutoff(tmp_path):
    """An NTP step mid-recovery must not be able to hide or invent the
    re-registration, so the confirmation counts this boot's lines."""
    h = Harness(tmp_path)

    h.run(follow=DEATH, confirm=REGISTERED)

    confirm_argv = [
        line for line in h.journal_log.read_text().splitlines()
        if "new USB bus registered" in line
    ]
    assert confirm_argv
    assert all("--since" not in line and "-b 0" in line for line in confirm_argv)


def test_a_closed_log_stream_exits_non_zero_so_systemd_restarts_the_watcher(tmp_path):
    h = Harness(tmp_path)

    proc = h.run()

    assert proc.returncode == 1
    assert "event=usb_hcd_recover.stream_closed controller=none attempt=0" in proc.stderr


def test_every_event_carries_the_same_leading_fields(tmp_path):
    h = Harness(tmp_path)
    proc = h.run(follow=DEATH, confirm=REGISTERED)

    events = [ln for ln in proc.stderr.splitlines() if ln.startswith("event=")]
    assert events
    for line in events:
        head = line.split()
        assert head[0].startswith("event=usb_hcd_recover.")
        assert head[1].startswith("controller=")
        assert head[2].startswith("attempt=")


def test_the_unit_only_runs_where_a_controller_is_bound_and_restarts_itself():
    unit = UNIT.read_text(encoding="utf-8")

    assert exec_argv_for(unit, "ExecStart") == (
        ["/usr/local/sbin/jasper-usb-hcd-recover"],
    )
    assert values_for(unit, "ConditionPathExistsGlob") == (
        "/sys/bus/platform/drivers/xhci-hcd/xhci-hcd.*",
    )
    assert value_for(unit, "ConditionPathExists") is None
    assert value_for(unit, "Restart") == "always"
    assert value_for(unit, "StartLimitBurst") is not None
    assert value_for(unit, "StartLimitIntervalSec") is not None


def test_the_unit_grants_write_access_to_the_driver_directory_only():
    unit = UNIT.read_text(encoding="utf-8")

    assert value_for(unit, "ProtectSystem") == "strict"
    assert value_for(unit, "ProtectKernelTunables") == "true"
    assert values_for(unit, "ReadWritePaths") == (
        "-/sys/bus/platform/drivers/xhci-hcd",
    )
    assert value_for(unit, "CapabilityBoundingSet") == ""
    assert value_for(unit, "NoNewPrivileges") == "true"
    assert value_for(unit, "RuntimeDirectoryPreserve") == "yes"


def test_the_turntable_autostop_stands_down_during_a_re_bind():
    """Opening the wedged CH340 is what killed the controller (#5443); the
    re-bind re-enumerates it and udev would open it again straight away."""
    unit = AUTOSTOP_UNIT.read_text(encoding="utf-8")

    assert "!/run/jasper-usb-hcd-recover/rebinding" in values_for(
        unit, "ConditionPathExists"
    )


def test_install_stages_the_helper_before_the_unit_that_names_it():
    text = installer_text()
    helper = text.index("deploy/bin/jasper-usb-hcd-recover")
    unit = text.index("deploy/systemd/jasper-usb-hcd-recover.service")

    assert helper < unit
    assert "systemctl enable --now jasper-usb-hcd-recover.service" in text
    # enable --now does not restart a unit already running the old script.
    assert "systemctl try-restart jasper-usb-hcd-recover.service" in text
