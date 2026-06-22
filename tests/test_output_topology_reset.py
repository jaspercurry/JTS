# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``jasper-output-topology-reset`` recovery command.

The command returns a box whose saved output topology has drifted from
physical reality (most often a leftover active-speaker / roleful topology) to a
clean passive standard-speaker topology derived from detected hardware, and
kicks the audio-hardware reconcile. These tests pin the safety-relevant
behaviour with no hardware: the reset produces a passive (``speaker_groups==[]``,
``requires_roleful_graph`` false) topology, preserves the DETECTED hardware,
recovers from a stale roleful topology, recovers from a corrupt file, and is
idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.active_speaker.runtime_contract import classify_output_contract
from jasper.cli import output_topology_reset as reset_cli
from jasper.output_hardware import OutputHardwareState
from jasper.output_hardware import write_state as write_output_hardware_state
from jasper.output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    load_output_topology_strict,
    save_output_topology,
)

DETECTED_LABEL = "Apple USB-C audio adapter"
DETECTED_OUTPUTS = 2


@pytest.fixture
def topo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point topology + detected-hardware state at the tmp dir; seed an Apple DAC.

    Detected hardware is a single Apple USB-C dongle (2 outputs), so the reset's
    fresh draft should describe a plain 2-output passive speaker regardless of
    whatever stale topology was on disk.
    """

    path = tmp_path / "output_topology.json"
    hw_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hw_path))
    write_output_hardware_state(
        OutputHardwareState(
            profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            profile_label=DETECTED_LABEL,
            status="ok",
            physical_output_count=DETECTED_OUTPUTS,
        ),
        path=hw_path,
    )
    return path


def _stale_active_two_way(*, claimed_outputs: int = 8) -> OutputTopology:
    """A leftover 'Mono active 2-way' topology — the drift this command fixes.

    Roleful: a woofer on output 0 and a protection-required tweeter on output 1,
    exactly the shape that makes the L0 runtime gate refuse a flat graph. Its
    saved hardware deliberately claims more outputs than the box actually has,
    so a passing reset must re-derive hardware from detection, not the file.
    """

    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Mono active 2-way",
        "status": "verified",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": claimed_outputs,
        },
        "speaker_groups": [
            {
                "id": "main",
                "label": "Main active speaker",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": True,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": True,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            }
        ],
    })


def test_reset_produces_passive_unconfigured_topology(topo_path: Path) -> None:
    # No prior topology on disk → reset still writes a clean passive draft.
    result = reset_cli.reset_to_detected_passive(reconcile=False)

    assert result["after"]["speaker_groups"] == []
    assert result["after"]["status"] == "draft"

    loaded = load_output_topology_strict()
    assert loaded.speaker_groups == ()
    # The L0 gate keys on this: a passive topology requires no roleful graph, so
    # the gate accepts the flat graph and the deploy is unblocked.
    assert classify_output_contract(loaded).requires_roleful_graph is False


def test_reset_preserves_detected_hardware(topo_path: Path) -> None:
    result = reset_cli.reset_to_detected_passive(reconcile=False)

    after = result["after"]
    assert after["device_label"] == DETECTED_LABEL
    assert after["physical_output_count"] == DETECTED_OUTPUTS

    loaded = load_output_topology_strict()
    assert loaded.hardware.device_label == DETECTED_LABEL
    assert loaded.hardware.physical_output_count == DETECTED_OUTPUTS


def test_reset_clears_stale_active_topology(topo_path: Path) -> None:
    # The incident: a passive box carrying a leftover roleful active topology.
    stale = _stale_active_two_way()
    save_output_topology(stale)
    assert classify_output_contract(stale).requires_roleful_graph is True

    result = reset_cli.reset_to_detected_passive(reconcile=False)

    # before/after report captures what was replaced.
    assert result["before"]["name"] == "Mono active 2-way"
    assert result["before"]["speaker_groups"] == [{"id": "main", "mode": "active_2_way"}]
    assert result["after"]["name"] == "Speaker outputs"
    assert result["after"]["speaker_groups"] == []

    loaded = load_output_topology_strict()
    contract = classify_output_contract(loaded)
    assert contract.requires_roleful_graph is False
    assert loaded.speaker_groups == ()
    # Hardware is re-derived from DETECTION, not the stale file's 8-output claim.
    assert loaded.hardware.device_label == DETECTED_LABEL
    assert loaded.hardware.physical_output_count == DETECTED_OUTPUTS


def test_reset_recovers_from_corrupt_topology(topo_path: Path) -> None:
    topo_path.write_text("{ this is not valid json", encoding="utf-8")

    result = reset_cli.reset_to_detected_passive(reconcile=False)

    # A corrupt prior file is reported, not fatal — recovering from it is the job.
    assert result["before"]["readable"] is False
    assert "error" in result["before"]
    # The file is now a valid passive topology again.
    loaded = load_output_topology_strict()
    assert loaded.speaker_groups == ()
    assert classify_output_contract(loaded).requires_roleful_graph is False


def test_reset_is_idempotent(topo_path: Path) -> None:
    save_output_topology(_stale_active_two_way())

    first = reset_cli.reset_to_detected_passive(reconcile=False)
    after_first = topo_path.read_text(encoding="utf-8")
    second = reset_cli.reset_to_detected_passive(reconcile=False)
    after_second = topo_path.read_text(encoding="utf-8")

    assert first["after"] == second["after"]
    assert after_first == after_second
    # Second run sees an already-passive topology as the "before".
    assert second["before"]["speaker_groups"] == []


def test_reset_skips_reconcile_when_disabled(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        reset_cli, "_trigger_reconcile", lambda: calls.append(1) or {"ok": True}
    )

    result = reset_cli.reset_to_detected_passive(reconcile=False)

    assert calls == []
    assert result["reconcile"] == {"ok": None, "skipped": True}


def test_reset_triggers_reconcile_by_default(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = {"ok": True, "action": "start"}
    calls: list[int] = []
    monkeypatch.setattr(
        reset_cli, "_trigger_reconcile", lambda: (calls.append(1), sentinel)[1]
    )

    result = reset_cli.reset_to_detected_passive()

    assert calls == [1]
    assert result["reconcile"] is sentinel


# --- CLI entry point -------------------------------------------------------


def test_cli_refuses_without_confirmation_non_interactive(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    rc = reset_cli.main(["--no-reconcile"])

    assert rc == 1
    # Nothing written — the box is untouched.
    assert not topo_path.exists()


def test_cli_yes_writes_passive_topology(
    topo_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_output_topology(_stale_active_two_way())

    rc = reset_cli.main(["--yes", "--no-reconcile"])

    assert rc == 0
    loaded = load_output_topology_strict()
    assert loaded.speaker_groups == ()
    out = capsys.readouterr().out
    assert "BEFORE:" in out and "AFTER:" in out


def test_cli_dry_run_writes_nothing(
    topo_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_output_topology(_stale_active_two_way())
    before_bytes = topo_path.read_bytes()

    rc = reset_cli.main(["--dry-run"])

    assert rc == 0
    # Dry run leaves the stale topology exactly as it was.
    assert topo_path.read_bytes() == before_bytes
    out = capsys.readouterr().out
    assert "WOULD RESET" in out


def test_cli_json_output_is_machine_readable(
    topo_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = reset_cli.main(["--yes", "--no-reconcile", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["after"]["speaker_groups"] == []
    assert payload["reconcile"]["skipped"] is True
