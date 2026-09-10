# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for deploy/bin/jasper-camilla-topology-gate.

Covers the shell gate and the Python surfaces on both sides of it: the stamps
jasper.output_topology publishes, the record
jasper.control.camilla_topology_gate_state reads back, and jasper-doctor's
check_camilla_topology_gate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jasper.control import camilla_topology_gate_state as gate_state
from jasper.output_topology import (
    STATEFILE_TOPOLOGY_STAMP_SUFFIX,
    STATEFILE_UNPROVED_STAMP_SUFFIX,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-camilla-topology-gate"

#: systemd's ExecCondition= bands: 0 runs the unit, 1..254 skips it without
#: marking it failed, 255 or a signal FAILS it. The gate must never leave the
#: skip band, or a topology it could not read would take jasper-camilla into
#: its failure handler.
ALLOW = 0
SKIP_BAND = range(1, 255)


class _Gate:
    def __init__(self, tmp_path: Path) -> None:
        self.statefile = tmp_path / "outputd-statefile.yml"
        self.record = tmp_path / "run" / "gate.state"
        self.env = {
            "PATH": "/usr/bin:/bin",
            "JASPER_CAMILLA_STATEFILE": str(self.statefile),
            "JASPER_CAMILLA_TOPOLOGY_GATE_STATE": str(self.record),
        }

    def stamp(self, unproved: str | None, proved: str | None) -> None:
        for value, suffix in (
            (unproved, STATEFILE_UNPROVED_STAMP_SUFFIX),
            (proved, STATEFILE_TOPOLOGY_STAMP_SUFFIX),
        ):
            path = self.statefile.with_name(self.statefile.name + suffix)
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(value + "\n", encoding="utf-8")

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT)], env=self.env, capture_output=True, text=True, check=False,
        )


@pytest.mark.parametrize(
    ("unproved", "proved", "allowed"),
    [
        # A convergence that did not finish, for the topology the statefile
        # already holds: the graph is right, the pass failed over something
        # else, and stopping the speaker for it is what R8 removed.
        ("a" * 64, "a" * 64, True),
        # The topology moved and nothing proved a graph for it.
        ("b" * 64, "a" * 64, False),
        # Unknown is not mismatch, on either side and however it is unknown.
        (None, "a" * 64, True),
        ("b" * 64, None, True),
        (None, None, True),
        ("", "a" * 64, True),
        ("b" * 64, "", True),
    ],
)
def test_only_two_known_and_different_fingerprints_refuse_the_start(
    tmp_path: Path, unproved: str | None, proved: str | None, allowed: bool,
) -> None:
    gate = _Gate(tmp_path)
    gate.stamp(unproved, proved)

    result = gate.run()

    if allowed:
        assert result.returncode == ALLOW
        assert not gate.record.exists()
    else:
        assert result.returncode in SKIP_BAND
        assert gate.record.exists()


def test_the_refusal_record_reads_back_as_the_doctor_row_it_feeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One writer, one reader, one row: the shell record and the Python
    snapshot are the same contract, and the doctor fails on it."""
    from jasper.cli.doctor import audio_runtime_camilla as camilla_doctor

    gate = _Gate(tmp_path)
    gate.stamp("b" * 64, "a" * 64)
    assert gate.run().returncode in SKIP_BAND
    monkeypatch.setenv("JASPER_CAMILLA_TOPOLOGY_GATE_STATE", str(gate.record))

    snapshot = gate_state.snapshot()
    assert snapshot["status"] == "present"
    assert snapshot["refused"] is True
    assert snapshot["reason"] == "topology_mismatch"
    assert snapshot["unproved"] == "b" * 64
    assert snapshot["proved"] == "a" * 64
    assert isinstance(snapshot["refused_at"], (int, float))
    assert snapshot["action"]

    row = camilla_doctor.check_camilla_topology_gate()
    assert row.status == "fail"
    assert row.reason == camilla_doctor.REASON_CAMILLA_STATEFILE_TOPOLOGY_MISMATCH


def test_a_start_the_gate_allows_retires_the_previous_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record that outlived its refusal would keep the doctor red on a
    healthy speaker, so the allow path is what clears it."""
    from jasper.cli.doctor import audio_runtime_camilla as camilla_doctor

    gate = _Gate(tmp_path)
    gate.stamp("b" * 64, "a" * 64)
    gate.run()

    # What a convergence that succeeds leaves behind: the unproved stamp gone.
    gate.stamp(None, "b" * 64)
    assert gate.run().returncode == ALLOW

    monkeypatch.setenv("JASPER_CAMILLA_TOPOLOGY_GATE_STATE", str(gate.record))
    assert gate_state.snapshot() == {
        "status": "absent", "refused": False, "path": str(gate.record),
    }
    assert camilla_doctor.check_camilla_topology_gate().status == "ok"


@pytest.mark.parametrize(
    ("text", "reason"),
    [("", "unintelligible"), ("proved=a\n", "unintelligible")],
)
def test_a_record_with_no_reason_is_not_read_as_a_healthy_speaker(
    tmp_path: Path, text: str, reason: str,
) -> None:
    record = tmp_path / "gate.state"
    record.write_text(text, encoding="utf-8")

    snapshot = gate_state.snapshot(str(record))

    assert snapshot["status"] == reason
    assert snapshot["refused"] is False


def test_the_script_and_its_python_readers_name_the_same_paths() -> None:
    """Three literals live on both sides of the shell/Python boundary."""
    text = SCRIPT.read_text(encoding="utf-8")

    assert f'PROVED_STAMP="${{STATEFILE}}{STATEFILE_TOPOLOGY_STAMP_SUFFIX}"' in text
    assert (
        f'UNPROVED_STAMP="${{STATEFILE}}{STATEFILE_UNPROVED_STAMP_SUFFIX}"' in text
    )
    assert f":-{gate_state.DEFAULT_STATE_PATH}}}" in text


def test_the_unit_runs_the_gate_as_its_execcondition() -> None:
    """A gate nothing runs is not a gate; and it must be an ExecCondition, whose
    skip band leaves the unit successful, not an ExecStartPre, whose non-zero
    exit FAILS jasper-camilla into its recovery handler."""
    from tests.systemd_unit_helpers import values_for

    unit = (ROOT / "deploy" / "systemd" / "jasper-camilla.service").read_text(
        encoding="utf-8"
    )
    installed = f"/usr/local/sbin/{SCRIPT.name}"

    assert installed in values_for(unit, "ExecCondition")
    assert installed not in values_for(unit, "ExecStartPre")
    # The script must land before the unit that names it: a systemd exec
    # failure (203) is in the SKIP band, so the reverse order silently stops
    # starting CamillaDSP on a deploy interrupted between the two rows.
    rows = (ROOT / "deploy" / "lib" / "install" / "systemd-units.sh").read_text(
        encoding="utf-8"
    )
    assert rows.index(f"deploy/bin/{SCRIPT.name} {installed}") < rows.index(
        "deploy/systemd/jasper-camilla.service"
    )
