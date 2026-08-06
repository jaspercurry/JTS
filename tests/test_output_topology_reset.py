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


# --- #2135 blocker F2(b): the reset must converge the RUNNING graph ---------
# The reconcile kick re-derives outputd's env; it never touches CamillaDSP. A box
# parked silent by a roleful topology therefore stayed on /dev/null after being
# reset to passive, until the next deploy re-seeded the statefile — "reset output
# topology to passive", the second of parking's two exits, did not exit.


@pytest.fixture
def flat_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real flat cutover config where the runtime contract looks for it."""
    from jasper.active_speaker import runtime_contract
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    path = tmp_path / "outputd-cutover.yml"
    emit_flat_outputd_cutover_config(out_path=path)
    monkeypatch.setattr(runtime_contract, "DEFAULT_FLAT_OUTPUTD_CONFIG", path)
    return path


def test_reset_loads_the_passive_graph_into_camilla(
    topo_path: Path, flat_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded: list[str] = []

    class _FakeController:
        async def set_config_file_path(self, path: str, *, best_effort: bool = False):
            loaded.append(path)
            return True

    monkeypatch.setattr(reset_cli, "_trigger_reconcile", lambda: {"ok": True})
    monkeypatch.setattr(
        "jasper.camilla.primary_controller", lambda: _FakeController()
    )

    result = reset_cli.reset_to_detected_passive()

    # The graph the fresh PASSIVE topology may run — the flat cutover, never a
    # parked or roleful graph.
    assert result["camilla"]["ok"] is True
    assert result["camilla"]["status"] == "select_flat"
    assert loaded == [result["camilla"]["config_path"]]
    assert loaded == [str(flat_config)]


def test_reset_rerenders_the_cutover_so_a_mono_era_mute_cannot_survive(
    topo_path: Path, flat_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset must not load the PREVIOUS topology's width-matched graph.

    On a mono box the on-disk cutover hard-mutes the channel that topology did
    not claim. The reset writes an unconfigured draft, which claims nothing — so
    the emission check has nothing to compare against and would happily accept
    the stale half-muted graph. The box comes back audible on one output and
    silent on the other, with no blocker to explain it, on the documented escape
    hatch. Re-rendering first is what closes it.
    """
    from jasper.output_topology import OutputTopology
    from jasper.sound.camilla_yaml import emit_flat_outputd_cutover_config

    mono = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "mono",
        "name": "Mono passive output",
        "status": "verified",
        "hardware": {
            "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
            "device_label": DETECTED_LABEL,
            "physical_output_count": DETECTED_OUTPUTS,
        },
        "speaker_groups": [{
            "id": "main", "label": "Main speaker", "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 0}],
        }],
        "routing": {"mono_group_id": "main"},
    })
    # The mono-era render, exactly as the last deploy left it on disk.
    emit_flat_outputd_cutover_config(out_path=flat_config, topology=mono)
    assert "commission_mute" in flat_config.read_text(encoding="utf-8")

    loaded: list[str] = []

    class _FakeController:
        async def set_config_file_path(self, path: str, *, best_effort: bool = False):
            loaded.append(path)
            return True

    monkeypatch.setattr(reset_cli, "_trigger_reconcile", lambda: {"ok": True})
    monkeypatch.setattr(
        "jasper.camilla.primary_controller", lambda: _FakeController()
    )

    result = reset_cli.reset_to_detected_passive()

    # BEHAVIOUR FIRST (this is the assertion that must fail without the fix):
    # the graph the box actually loaded has BOTH channels live again.
    assert loaded == [str(flat_config)]
    reloaded = flat_config.read_text(encoding="utf-8")
    assert "commission_mute" not in reloaded
    assert reloaded == emit_flat_outputd_cutover_config(topology=_after_draft())
    # install's file mode is preserved, not narrowed to the emitter's 0640.
    assert flat_config.stat().st_mode & 0o777 == 0o644
    assert result["camilla"]["ok"] is True
    assert result["camilla"]["render_error"] is None


def _after_draft():
    """The unconfigured passive draft the reset writes."""
    from jasper.output_topology import new_topology_draft

    return new_topology_draft()


def test_reset_from_the_web_sandbox_reports_the_render_it_could_not_do(
    topo_path: Path, flat_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Called from /sound/, the in-process render CANNOT write /etc/camilladsp.

    jasper-web.service's ReadWritePaths does not include it — a WS1-deliberate
    boundary that must not be widened for this. So the render raises OSError,
    which `_CONVERGENCE_ERRORS` swallows; before this round the reset then
    reported plain success and loaded the stale half-muted graph, with the
    failure visible nowhere. Two things must hold now: the reset still succeeds
    (the topology is already passive and safe), and the failure is REPORTED so
    the page can say so.
    """
    from jasper.sound import camilla_yaml as sound_camilla_yaml

    def _sandboxed(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(
        sound_camilla_yaml, "render_flat_cutover_configs", _sandboxed
    )
    monkeypatch.setattr(reset_cli, "_trigger_reconcile", lambda: {"ok": True})

    class _FakeController:
        async def set_config_file_path(self, path: str, *, best_effort: bool = False):
            return True

    monkeypatch.setattr(
        "jasper.camilla.primary_controller", lambda: _FakeController()
    )

    result = reset_cli.reset_to_detected_passive()

    assert result["after"]["speaker_groups"] == []
    assert result["camilla"]["ok"] is True
    assert "Read-only file system" in (result["camilla"]["render_error"] or "")


def test_sound_page_surfaces_the_reset_render_error() -> None:
    """The honesty belt: `render_error` must reach the household, not just logs.

    `resetCleanupWarning` used to read only `active_speaker_reset.status`, so a
    failed render was invisible on the page that triggered it.
    """
    main_js = (
        Path(__file__).resolve().parent.parent
        / "deploy" / "assets" / "sound-profile" / "js" / "main.js"
    ).read_text(encoding="utf-8")

    warning = main_js[main_js.index("function resetCleanupWarning") :]
    warning = warning[: warning.index("\n  async function")]
    compact = "".join(warning.split())

    # The exact payload path the reset returns, so a renamed field breaks here
    # rather than going quiet on the page.
    assert "payload.reset.camilla&&payload.reset.camilla.render_error" in compact
    # ...and it must actually be surfaced, not merely read.
    assert "warnings.push" in compact
    # The pre-existing artifact warning is still emitted.
    assert "active_speaker_reset" in compact


def test_reset_camilla_convergence_is_best_effort_and_reported(
    topo_path: Path, flat_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A down CamillaDSP must not fail the reset: the topology is already
    passive and safe, and the graph self-heals on the next deploy."""

    def _boom():
        raise RuntimeError("camilla socket refused")

    monkeypatch.setattr(reset_cli, "_trigger_reconcile", lambda: {"ok": True})
    monkeypatch.setattr("jasper.camilla.primary_controller", _boom)

    result = reset_cli.reset_to_detected_passive()

    assert result["camilla"]["ok"] is False
    assert "camilla socket refused" in result["camilla"]["error"]
    # The reset itself still succeeded.
    assert result["after"]["speaker_groups"] == []


def test_reset_skips_camilla_convergence_when_reconcile_disabled(
    topo_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _never():
        raise AssertionError("camilla must not be contacted with reconcile=False")

    monkeypatch.setattr("jasper.camilla.primary_controller", _never)

    result = reset_cli.reset_to_detected_passive(reconcile=False)

    assert result["camilla"] == {"ok": None, "skipped": True}


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
