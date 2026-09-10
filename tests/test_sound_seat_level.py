# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper.web.sound_seat_level — the #2761 seat-level web surface.

Pins: target validation, the mic-unavailable refusal, at-most-one-running
guard, and the SIGINT-first stop contract (a plain SIGTERM/SIGKILL would
orphan the stimulus player mid-tone — see the module docstring). The
subprocess under test is a tiny stub standing in for ``jasper-seat-level``;
no CamillaDSP or mic hardware is exercised here.
"""

from __future__ import annotations

import stat
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.web import sound_seat_level as seat_level

STUB_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, signal, sys, time

    def handle_sigint(signum, frame):
        print(json.dumps({"status": "refused", "reason": "interrupted",
                           "detail": "stopped by operator"}))
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)
    time.sleep(float(os.environ.get("SEAT_LEVEL_STUB_DELAY_S", "5.0")))
    print(json.dumps({"reference_volume_db": -12.3, "measured_db_spl": 77.4,
                       "restored": True, "detail": "converged"}))
    sys.exit(0)
    """
)


@pytest.fixture()
def stub_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-seat-level"
    script.write_text(STUB_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _wait_until(predicate, *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition never became true"


# --- household_mic_summary ------------------------------------------------


def test_household_mic_summary_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        seat_level.household_mic, "resolved_household_mic", lambda: None
    )
    assert seat_level.household_mic_summary() == {"available": False}


def test_household_mic_summary_available(monkeypatch: pytest.MonkeyPatch) -> None:
    record = SimpleNamespace(label="UMIK-2", serial_display="3219")
    resolved = SimpleNamespace(raw_path="/var/lib/jasper/mic-cal/umik2.txt")
    monkeypatch.setattr(
        seat_level.household_mic,
        "resolved_household_mic",
        lambda: (record, resolved),
    )
    assert seat_level.household_mic_summary() == {
        "available": True,
        "label": "UMIK-2",
        "serial_display": "3219",
        "calibration_file": "/var/lib/jasper/mic-cal/umik2.txt",
    }


# --- seat_level_start_payload validation -----------------------------------


@pytest.mark.parametrize("raw_target", [None, "not-a-number", object()])
def test_start_refuses_non_numeric_target(
    raw_target, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = seat_level.seat_level_start_payload({"target_db_spl": raw_target})
    assert payload == {
        "status": "refused",
        "reason": "invalid_target",
        "detail": "target_db_spl must be a number",
    }


def test_start_refuses_non_finite_target() -> None:
    payload = seat_level.seat_level_start_payload({"target_db_spl": "inf"})
    assert payload["status"] == "refused"
    assert payload["reason"] == "invalid_target"


def test_start_refuses_when_mic_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        seat_level.household_mic, "resolved_household_mic", lambda: None
    )
    payload = seat_level.seat_level_start_payload({"target_db_spl": 78.0})
    assert payload["status"] == "refused"
    assert payload["reason"] == "mic_calibration_unavailable"


# --- _SeatLevelSession lifecycle, against the stub CLI ---------------------


def test_start_reports_running_then_converged(
    stub_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEAT_LEVEL_STUB_DELAY_S", "0.05")
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_CLI", str(stub_cli))
    session = seat_level._SeatLevelSession()
    result = session.start(target_db_spl=78.0, calibration_file="/dev/null")
    assert result == {"status": "started", "target_db_spl": 78.0}

    status = session.status()
    assert status["state"] in ("running", "converged")

    _wait_until(lambda: session.status()["state"] == "converged")
    status = session.status()
    assert status["target_db_spl"] == 78.0
    assert status["detail"]["reference_volume_db"] == -12.3


def test_start_refuses_second_start_while_running(
    stub_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEAT_LEVEL_STUB_DELAY_S", "5.0")
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_CLI", str(stub_cli))
    session = seat_level._SeatLevelSession()
    session.start(target_db_spl=78.0, calibration_file="/dev/null")
    _wait_until(lambda: session.status()["state"] == "running")

    second = session.start(target_db_spl=80.0, calibration_file="/dev/null")
    assert second["status"] == "refused"
    assert second["reason"] == "already_running"

    time.sleep(0.3)
    session.stop()
    _wait_until(lambda: session.status()["state"] != "running")


def test_stop_sends_sigint_and_reports_refused(
    stub_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEAT_LEVEL_STUB_DELAY_S", "5.0")
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_CLI", str(stub_cli))
    session = seat_level._SeatLevelSession()
    session.start(target_db_spl=78.0, calibration_file="/dev/null")
    _wait_until(lambda: session.status()["state"] == "running")
    # Give the stub's interpreter time to install its SIGINT handler before
    # stopping it -- the real CLI has the same startup window, but main()'s
    # own outer `except KeyboardInterrupt` covers it there (nothing was
    # claimed yet, so "restored" reports nothing to restore); this stub has
    # no such second layer, so the test avoids the race instead.
    time.sleep(0.5)

    stop_result = session.stop()
    assert stop_result["status"] == "stopping"

    _wait_until(lambda: session.status()["state"] == "refused")
    status = session.status()
    assert status["detail"]["reason"] == "interrupted"


def test_stop_when_idle_is_a_no_op() -> None:
    session = seat_level._SeatLevelSession()
    assert session.stop() == {"status": "idle"}
