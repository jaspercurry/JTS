# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behaviour contract for the jasper-fanin-pitch-neutralize helper (defect E).

The combo-mode host-clock belt-and-braces (C6) moved out of jasper-fanin.service's
inline `sh -c` ExecStopPost into this shipped helper, because the inline
`card="${card%%,*}"` collided with systemd's `%%` specifier escape (logged
"Invalid environment variable name evaluates to an empty string: card%,*" AND ran
the shortest-match `${card%,*}` at runtime). These tests pin the owner-gate
semantics the unit used to carry:

- fires ONLY when BOTH JASPER_FANIN_HOST_CLOCK and JASPER_FANIN_USB_DIRECT are
  `enabled` (case-insensitive) — the "fan-in owns the ctl only in combo mode"
  invariant, so a solo-mode usbsink L0 command is never stomped;
- derives the card from JASPER_FANIN_USB_DIRECT_DEVICE the same way the in-daemon
  actuator does (strip plughw:/hw: prefix + ,dev,subdev tail) — including the
  LONGEST-match tail strip that the `%%` bug silently broke;
- resets the pitch to neutral 1000000 on the correct element by name.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "bin" / "jasper-fanin-pitch-neutralize"


def _fake_amixer(tmp_path: Path, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A fake amixer that appends its full argv to a log and exits ``exit_code``."""
    log = tmp_path / "amixer.log"
    fake = tmp_path / "amixer"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$JASPER_FAKE_AMIXER_LOG"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, log


def _run(
    tmp_path: Path,
    *,
    host_clock: str | None,
    usb_direct: str | None,
    device: str | None = None,
    amixer_exit: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_amixer, log = _fake_amixer(tmp_path, exit_code=amixer_exit)
    env = os.environ.copy()
    env["JASPER_AMIXER"] = str(fake_amixer)
    env["JASPER_FAKE_AMIXER_LOG"] = str(log)
    # Start from a clean slate — the harness env may carry these.
    for key in (
        "JASPER_FANIN_HOST_CLOCK",
        "JASPER_FANIN_USB_DIRECT",
        "JASPER_FANIN_USB_DIRECT_DEVICE",
    ):
        env.pop(key, None)
    if host_clock is not None:
        env["JASPER_FANIN_HOST_CLOCK"] = host_clock
    if usb_direct is not None:
        env["JASPER_FANIN_USB_DIRECT"] = usb_direct
    if device is not None:
        env["JASPER_FANIN_USB_DIRECT_DEVICE"] = device
    proc = subprocess.run(
        [str(HELPER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, log


def test_helper_exists_and_is_executable():
    assert HELPER.exists(), f"missing helper at {HELPER}"
    assert os.access(HELPER, os.X_OK), "helper must be executable (0755)"


def test_fires_and_writes_neutral_when_both_flags_enabled(tmp_path: Path):
    proc, log = _run(tmp_path, host_clock="enabled", usb_direct="enabled")
    assert proc.returncode == 0, proc.stderr
    logged = log.read_text() if log.exists() else ""
    # The fake amixer logs `$*`, which word-splits away the shell quotes, so the
    # element name reaches the log as `name=Capture Pitch 1000000` (one arg to
    # amixer, quoting preserved on the real wire).
    assert "name=Capture Pitch 1000000" in logged, (
        f"helper must cset the pitch ctl by name; amixer log={logged!r}"
    )
    # The neutral value is the last token.
    assert logged.strip().endswith("1000000"), (
        f"helper must reset the pitch to neutral 1000000; amixer log={logged!r}"
    )
    # Default card token (no device override) is the bare UAC2Gadget.
    assert "-c UAC2Gadget " in logged, (
        f"default card must be UAC2Gadget (from hw:UAC2Gadget); log={logged!r}"
    )


def test_no_write_when_host_clock_off(tmp_path: Path):
    proc, log = _run(tmp_path, host_clock=None, usb_direct="enabled")
    assert proc.returncode == 0
    assert not log.exists() or log.read_text() == "", (
        "helper must NOT touch amixer when HOST_CLOCK is unset — an unconditional "
        "neutralize would stomp a solo-mode usbsink L0 command"
    )


def test_no_write_when_usb_direct_off(tmp_path: Path):
    """HOST_CLOCK=enabled alone (part-rolled-back combo) must NOT fire the belt:
    fan-in owns the ctl only when BOTH flags are set (review F2)."""
    proc, log = _run(tmp_path, host_clock="enabled", usb_direct=None)
    assert proc.returncode == 0
    assert not log.exists() or log.read_text() == "", (
        "helper must NOT touch amixer when USB_DIRECT is unset (HOST_CLOCK-only is "
        "a part-rolled-back combo box where solo usbsink owns the ctl)"
    )


@pytest.mark.parametrize("flag", ["Enabled", "ENABLED", "eNaBlEd"])
def test_gate_is_case_insensitive(tmp_path: Path, flag: str):
    """The config parser arms on eq_ignore_ascii_case; the belt must match, or a
    box armed with `Enabled` runs the servo with a DEAD belt (review F3)."""
    proc, log = _run(tmp_path, host_clock=flag, usb_direct=flag)
    assert proc.returncode == 0, proc.stderr
    assert log.exists() and "1000000" in log.read_text(), (
        f"helper must treat {flag!r} as enabled (case-insensitive gate)"
    )


def test_card_derives_from_device_override_with_longest_match_tail_strip(
    tmp_path: Path,
):
    """The card is derived from JASPER_FANIN_USB_DIRECT_DEVICE (review N6). This
    is the DIRECT regression for defect E: the old inline `${card%%,*}` ran as
    the SHORTEST-match `${card%,*}` under systemd's %-escaping, so a
    `hw:Card,0,0` device would leave a trailing `,0`. A real helper does the
    LONGEST-match strip, yielding the bare card token amixer -c wants."""
    proc, log = _run(
        tmp_path,
        host_clock="enabled",
        usb_direct="enabled",
        device="hw:MyGadget,0,0",
    )
    assert proc.returncode == 0, proc.stderr
    logged = log.read_text()
    assert "-c MyGadget " in logged, (
        "helper must strip the plughw:/hw: prefix AND the full ,dev,subdev tail "
        f"(longest-match) to the bare card; got amixer log={logged!r}"
    )
    assert "MyGadget,0" not in logged, (
        "the ,dev,subdev tail must be fully stripped (defect E: shortest-match "
        f"left a trailing ,0); got {logged!r}"
    )


def test_plughw_prefix_is_stripped(tmp_path: Path):
    proc, log = _run(
        tmp_path,
        host_clock="enabled",
        usb_direct="enabled",
        device="plughw:UAC2Gadget",
    )
    assert proc.returncode == 0, proc.stderr
    assert "-c UAC2Gadget " in log.read_text()


def test_amixer_failure_propagates_nonzero_exit_not_swallowed(tmp_path: Path):
    # Defect E comment correctness (2026-07-05): the helper `exec`s amixer, so a
    # missing card / amixer error is NOT swallowed to exit 0 — the script's exit
    # status IS amixer's. (jasper-fanin.service's leading `-` on the ExecStopPost
    # line is what makes that non-fatal to the stop; the helper itself surfaces the
    # real failure code for the journal.) This pins that the "exec, don't mask"
    # behaviour matches the corrected comment.
    proc, log = _run(
        tmp_path,
        host_clock="enabled",
        usb_direct="enabled",
        amixer_exit=1,
    )
    assert proc.returncode == 1, (
        "helper must PROPAGATE amixer's non-zero exit (exec, not swallow-to-0); "
        f"got returncode={proc.returncode}"
    )
    # It still attempted the cset (the failure came from amixer, not a skipped call).
    assert "Capture Pitch 1000000" in log.read_text()
