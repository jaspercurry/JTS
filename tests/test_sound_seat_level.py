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

import contextlib
import io
import stat
import textwrap
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker.auto_level import LevelResult
from jasper.cli._refusal import EXIT_REFUSED, failed
from jasper.web import sound_seat_level as seat_level


def _real_refusal_stdout(*, reason: str, sentence: str) -> str:
    """The CLI's LevelResult fields plus ``detail``, wrapped by ``failed()``.

    The sentence is nested under ``detail.detail``. A previous flat
    ``{"status","reason","detail": sentence}`` stub masked the [object
    Object] bug this fixture exists to catch (#2761 review M1).
    """
    carried = {
        key: value
        for key, value in {
            **asdict(LevelResult("refused", reason)),
            "reference_volume_db": -17.5,
            "measured_db_spl": 77.4,
            "restored": True,
        }.items()
        if key not in {"status", "reason"}
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        failed(EXIT_REFUSED, reason, {**carried, "detail": sentence})
    return buf.getvalue()


INTERRUPTED_REFUSAL_STDOUT = _real_refusal_stdout(
    reason="interrupted", sentence="stopped by operator"
)

STUB_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, signal, sys, time

    def handle_sigint(signum, frame):
        sys.stdout.write(__REFUSAL_STDOUT__)
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_sigint)
    ready_file = os.environ.get("SEAT_LEVEL_STUB_READY_FILE")
    if ready_file:
        open(ready_file, "w").close()
    time.sleep(float(os.environ.get("SEAT_LEVEL_STUB_DELAY_S", "5.0")))
    print(json.dumps({"reference_volume_db": -12.3, "measured_db_spl": 77.4,
                       "restored": True, "detail": "converged"}))
    sys.exit(0)
    """
).replace("__REFUSAL_STDOUT__", repr(INTERRUPTED_REFUSAL_STDOUT))


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
    assert seat_level.household_mic_summary() == {"status": "unavailable", "reason": "mic_calibration_unavailable"}


def test_household_mic_summary_available(monkeypatch: pytest.MonkeyPatch) -> None:
    record = SimpleNamespace(label="UMIK-2", serial_display="3219")
    resolved = SimpleNamespace(raw_path="/var/lib/jasper/mic-cal/umik2.txt")
    monkeypatch.setattr(
        seat_level.household_mic,
        "resolved_household_mic",
        lambda: (record, resolved),
    )
    assert seat_level.household_mic_summary() == {
        "status": "available",
        "label": "UMIK-2",
        "serial_display": "3219",
        "calibration_file": "/var/lib/jasper/mic-cal/umik2.txt",
    }


# --- seat_level_start_payload validation -----------------------------------


@pytest.mark.parametrize("raw_target", [None, "not-a-number", object()])
def test_start_refuses_non_numeric_target(raw_target) -> None:
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
    reap = session._reap
    unreaped: list = []
    monkeypatch.setattr(session, "_reap", unreaped.append)
    result = session.start(target_db_spl=78.0, calibration_file="/dev/null")
    assert result == {"status": "started", "target_db_spl": 78.0}

    _wait_until(lambda: unreaped)
    (proc,) = unreaped
    proc.wait()
    # Exited but not yet reaped: this window must not read as idle (#5925).
    assert session.status() == {"state": "running", "target_db_spl": 78.0}

    reap(proc)
    status = session.status()
    assert status["state"] == "converged"
    assert status["target_db_spl"] == 78.0
    assert status["measured_db_spl"] == 77.4
    assert status["detail"] == "converged"


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
    stub_cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_file = tmp_path / "sigint-ready"
    monkeypatch.setenv("SEAT_LEVEL_STUB_READY_FILE", str(ready_file))
    monkeypatch.setenv("SEAT_LEVEL_STUB_DELAY_S", "5.0")
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_CLI", str(stub_cli))
    session = seat_level._SeatLevelSession()
    session.start(target_db_spl=78.0, calibration_file="/dev/null")
    _wait_until(lambda: session.status()["state"] == "running")
    # The stub writes the marker right after it installs its SIGINT handler
    # (#5812). The real CLI has the same startup window, but main()'s own
    # outer `except KeyboardInterrupt` covers it there (nothing was claimed
    # yet, so "restored" reports nothing to restore); this stub has no such
    # second layer, so the test waits for the marker.
    _wait_until(lambda: ready_file.exists())

    stop_result = session.stop()
    assert stop_result["status"] == "stopping"

    _wait_until(lambda: session.status()["state"] == "refused")
    status = session.status()
    assert status["reason"] == "interrupted"
    assert status["detail"] == "stopped by operator"


def test_stop_when_idle_is_a_no_op() -> None:
    session = seat_level._SeatLevelSession()
    assert session.stop() == {"status": "idle"}


def test_force_stop_reports_written_sentence_not_raw_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: a stub that ignores SIGINT forces the SIGTERM/SIGKILL escalation
    (sound_seat_level.py's stop() timeout branch) -- the CLI's own
    fade-out/latch-restore teardown never runs, since only SIGINT is wired
    to it. status() must publish a named ``force_stopped`` field and a
    written sentence, never the process's raw stderr tail.

    The stop timeout is a module constant patched here (not a new
    ``JASPER_*`` env knob), so the escalation exercises well under the
    real 5s default.
    """
    script = tmp_path / "stubborn-seat-level"
    ready_file = tmp_path / "sigign-ready"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import signal, sys, time
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            open({str(ready_file)!r}, "w").close()
            sys.stderr.write("simulated hang, ignoring SIGINT\\n")
            time.sleep(30)
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_CLI", str(script))
    monkeypatch.setattr(seat_level, "SEAT_LEVEL_STOP_TIMEOUT_S", 0.2)

    session = seat_level._SeatLevelSession()
    session.start(target_db_spl=78.0, calibration_file="/dev/null")
    _wait_until(lambda: session.status()["state"] == "running")
    # Same startup window as test_stop_sends_sigint_and_reports_refused: until
    # the stub's SIG_IGN is in place, Python's default handler exits it well
    # within the 0.2s timeout and the escalation this test pins never happens.
    _wait_until(lambda: ready_file.exists())

    stop_result = session.stop()
    assert stop_result["status"] == "stopping"

    _wait_until(lambda: session.status()["state"] == "refused")
    status = session.status()
    assert status["force_stopped"] is True
    assert status["reason"] == "force_stopped"
    assert "force-stopped" in status["detail"]
    assert "simulated hang" not in status["detail"]


def test_cli_path_falls_back_beside_the_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seat_level.shutil, "which", lambda name: None)
    resolved = Path(seat_level._cli_path("jasper-seat-level"))
    assert resolved.is_absolute()
    assert resolved.parent == Path(seat_level.sys.executable).parent
    assert resolved.name == "jasper-seat-level"
