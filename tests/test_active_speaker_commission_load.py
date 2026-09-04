# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guarded per-driver commissioning load (gap-1 slice 2b-ii).

`load_driver_commissioning_config` loads a per-driver commissioning config into
the RUNNING CamillaDSP graph via the INLINE transport (set_active_config_raw),
so the durable boot config / outputd statefile stay pointed at the all-muted
staged config (crash-recovery-MUTED). These tests pin:

  * the live read-back gate (`running_commission_evidence`) — robust to
    CamillaDSP's block-style re-serialization, and fail-closed on drift;
  * the load transaction — re-runs prepare stateless (S2), loads only when the
    speaker + per-driver evidence are both ready, live-confirms the RUNNING
    graph, and rolls back to the staged anchor when the live graph disagrees;
  * S3 (MUST-TEST) — after a commissioning load, the durable outputd statefile
    STILL points at the all-muted staged boot config.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import yaml

import jasper.active_speaker.commission_load as commission_load_mod
import jasper.active_speaker.startup_load as startup_load_mod
from jasper.active_speaker import (
    ActiveSpeakerPreset,
    audible_outputs_for_role,
    driver_commission_audible_evidence,
    emit_active_speaker_commissioning_config,
    load_commission_load_state,
    load_driver_commissioning_config,
    load_summed_commissioning_config,
    parse_camilla_statefile_config_path,
    rollback_driver_commissioning_config,
    running_commission_evidence,
)
from jasper.active_speaker.staging import running_graph_matches_staged_anchor

# Reuse the canonical mono DAC8x topology + passing-validation stub + path-safety
# evidence writer from the protected-startup-load tests.
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from tests._armed_transport import arm_ring_transport
from tests.active_speaker_fixtures import mono_output_topology as _topology
from tests.test_active_speaker_startup_load import (
    _protected_prior,
    _staged,
    _valid_config,
    _write_path_safety,
)
from tests.test_active_speaker_profile import _two_way_preset


@pytest.fixture(autouse=True)
def reconcile_triggers(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_manage_units(*units: str, **kwargs):
        calls.append({"units": units, **kwargs})
        return {"ok": True, "rc": 0}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)
    return calls


def _block(text: str) -> str:
    """Re-serialize like CamillaDSP's active_raw(): block-style lists, sorted."""
    return yaml.safe_dump(yaml.safe_load(text), default_flow_style=False, sort_keys=True)


def _two_way() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _emit(preset: ActiveSpeakerPreset, audible: set[int]) -> str:
    return emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=audible
    )


def _intent(preset: ActiveSpeakerPreset, audible: set[int]) -> dict:
    """The off-device evidence the live check is parameterised by."""
    return driver_commission_audible_evidence(
        _emit(preset, audible), preset=preset, audible_outputs=audible
    )


# --- running_commission_evidence: live read-back gate ------------------------


def test_running_evidence_passes_on_block_style_roundtrip():
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    ev = _intent(preset, woofer)
    live = running_commission_evidence(
        _block(_emit(preset, woofer)),
        audible_outputs=ev["audible_outputs"],
        muted_outputs=ev["muted_outputs"],
        tweeter_outputs=ev["tweeter_outputs"],
        protective_hp_hz=ev["protective_highpass_hz"],
    )
    assert live["passed"] is True
    assert live["checks"]["audible_mask_correct"] is True
    assert live["checks"]["tweeter_protected_while_audible"] is True


def test_running_evidence_audible_tweeter_requires_live_high_pass():
    preset = _two_way()
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    ev = _intent(preset, tweeter)
    # Corrupt the protective HP in the RUNNING graph -> the live check must fail.
    running = yaml.safe_load(_emit(preset, tweeter))
    running["filters"]["as_tweeter_protective_hp"]["parameters"]["freq"] = 200.0
    live = running_commission_evidence(
        yaml.safe_dump(running),
        audible_outputs=ev["audible_outputs"],
        muted_outputs=ev["muted_outputs"],
        tweeter_outputs=ev["tweeter_outputs"],
        protective_hp_hz=ev["protective_highpass_hz"],
    )
    assert live["checks"]["tweeter_protected_while_audible"] is False
    assert live["passed"] is False


def test_running_evidence_fails_when_live_mask_drifts_from_intent():
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    intent = _intent(preset, woofer)  # we INTEND woofer-only
    # ...but the RUNNING graph actually unmutes the tweeter too.
    live = running_commission_evidence(
        _block(_emit(preset, woofer | tweeter)),
        audible_outputs=intent["audible_outputs"],
        muted_outputs=intent["muted_outputs"],
        tweeter_outputs=intent["tweeter_outputs"],
        protective_hp_hz=intent["protective_highpass_hz"],
    )
    assert live["checks"]["audible_mask_correct"] is False
    assert live["passed"] is False


def test_running_evidence_fails_closed_on_unparseable_readback():
    for raw in (None, "", "::not yaml::", "devices: {}"):
        live = running_commission_evidence(
            raw,
            audible_outputs=[0],
            muted_outputs=[1],
            tweeter_outputs=[1],
            protective_hp_hz=3200.0,
        )
        assert live["passed"] is False


# --- the guarded load transaction --------------------------------------------


class FakeCommissionCamilla:
    """Inline transport: ``apply_running_config`` swaps the running graph;
    the persisted ``config_file_path`` (the statefile anchor) NEVER changes."""

    def __init__(self, persisted_path: str | Path) -> None:
        self.persisted_path = str(persisted_path)
        self.running_raw: str | None = None
        self.loaded_paths: list[str] = []
        self.read_calls = 0

    async def apply_running_config(self, path: str) -> bool:
        text = Path(path).read_text(encoding="utf-8")
        # CamillaDSP re-serializes the running graph in its own YAML dialect.
        self.running_raw = _block(text)
        self.loaded_paths.append(str(path))
        return True

    async def read_running_config(self) -> str | None:
        self.read_calls += 1
        return self.running_raw

    async def get_config_file_path(self) -> str | None:
        return self.persisted_path


class DriftingCamilla(FakeCommissionCamilla):
    """The live graph disagrees with the config we loaded (drift / silent fail)."""

    def __init__(self, persisted_path: str | Path, drift_raw: str) -> None:
        super().__init__(persisted_path)
        self._drift = drift_raw

    async def apply_running_config(self, path: str) -> bool:
        await super().apply_running_config(path)
        self.running_raw = self._drift
        return True


def _statefile(tmp_path: Path, config_path: str) -> Path:
    sf = tmp_path / "outputd-statefile.yml"
    sf.write_text(
        f"config_path: {config_path}\nmute: false\nvolume: -30.0\n",
        encoding="utf-8",
    )
    return sf


class ReadFailingCamilla(FakeCommissionCamilla):
    """The running graph cannot be read back after the load (camilla wedged)."""

    async def read_running_config(self) -> str | None:
        raise RuntimeError("camilla unavailable")


class SettlingCamilla(FakeCommissionCamilla):
    """CamillaDSP acks the inline load but the readback lags: the first
    ``lag_reads`` reads still return the staged all-muted anchor before the
    switch lands (the hardware-observed 2026-07-15 race)."""

    def __init__(
        self, persisted_path: str | Path, staged_raw: str, lag_reads: int
    ) -> None:
        super().__init__(persisted_path)
        self._staged_raw = staged_raw
        self._lag_reads = lag_reads

    async def read_running_config(self) -> str | None:
        self.read_calls += 1
        if self.read_calls <= self._lag_reads:
            return self._staged_raw
        return self.running_raw


class StuckCamilla(FakeCommissionCamilla):
    """The readback NEVER leaves the staged anchor (the load never took)."""

    def __init__(self, persisted_path: str | Path, staged_raw: str) -> None:
        super().__init__(persisted_path)
        self._staged_raw = staged_raw

    async def read_running_config(self) -> str | None:
        self.read_calls += 1
        return self._staged_raw


def _load(
    tmp_path,
    monkeypatch,
    *,
    role: str = "woofer",
    group_id: str = "mono",
    drift_raw: str | None = None,
    statefile_target: str | None = None,
    camilla: FakeCommissionCamilla | None = None,
    with_path_safety: bool = True,
    reconcile_output_hardware: bool = True,
    playback_device: str | None = None,
    arm_transport: bool = True,
):
    # DEFAULTS ON since #2285 P2, and the flip is the whole point of the
    # handoff (#2412 Wave 6 determined it, this branch executes it). While the
    # chooser still had two answers, arming here was NOT inert — the
    # ACTIVE-endpoint marker is read by `resolve_output_layout` as well as by
    # the load gate, so arming unconditionally would have moved all 37 tests
    # that funnel through this helper (17 in this module plus the 20 reaching it
    # through `test_active_speaker_stage5_ramp.py::_ramp_step`) off the
    # snd-aloop path they were written to cover. P2 retires that path: case 2 of
    # `resolve_output_layout` now names the ring unconditionally, so there is no
    # longer a second transport to lose coverage of, and arming supplies only
    # the liveness Wave 3's `commissioning_transport_armed` gate asks for.
    #
    # It stays a PARAMETER rather than becoming unconditional so the negative
    # controls keep working: `test_an_unarmed_ring_blocks_the_load_and_the_arming_is_what_lifts_it`
    # passes `False` to prove the arming is load-bearing, and the load-line
    # polarity test passes it explicitly per case. Removing the knob would
    # delete both proofs. See `tests/_armed_transport.py`.
    if arm_transport:
        arm_ring_transport(monkeypatch)
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    # rollback_driver_commissioning_config derives its target from
    # staged_config_path() directly (not the statefile), so every test through
    # this helper needs that resolution to land on the same anchor _staged()
    # wrote, not the real /var/lib/camilladsp default.
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH", str(staged_path))
    statefile = _statefile(tmp_path, statefile_target or staged_path)
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    path_safety = (
        _write_path_safety(
            tmp_path / "path_safety.json",
            staged=staged,
            current_config_path=staged_path,
        )
        if with_path_safety
        else None
    )
    cam: FakeCommissionCamilla = camilla or (
        DriftingCamilla(staged_path, drift_raw)
        if drift_raw is not None
        else FakeCommissionCamilla(staged_path)
    )
    state_path = tmp_path / "commission_load.json"
    result = asyncio.run(
        load_driver_commissioning_config(
            _topology(),
            speaker_group_id=group_id,
            role=role,
            load_config=cam.apply_running_config,
            read_running_config=cam.read_running_config,
            get_current_config_path=cam.get_config_file_path,
            path_safety_evidence_path=path_safety,
            staged_config=staged,
            config_path=tmp_path / "commission.yml",
            statefile_path=statefile,
            state_path=state_path,
            reconcile_output_hardware=reconcile_output_hardware,
            playback_device=playback_device,
            validate=_valid_config,
        )
    )
    return result, cam, staged, staged_path, statefile, state_path


def test_woofer_commissioning_load_happy_path(monkeypatch, tmp_path, reconcile_triggers):
    result, cam, staged, staged_path, statefile, state_path = _load(
        tmp_path, monkeypatch, role="woofer"
    )

    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "loaded"
    assert result["load"]["live_evidence"]["passed"] is True
    # The loaded config is the transient commissioning config, not the boot one.
    commission_path = str(tmp_path / "commission.yml")
    assert cam.loaded_paths == [commission_path]
    # The running graph carries the woofer-only mask.
    assert result["load"]["target"]["role"] == "woofer"
    assert result["load"]["target"]["audible_outputs"] == [0]
    assert reconcile_triggers == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "active_speaker_driver_commission_load",
        "no_block": False,
        "timeout": 15.0,
    }]

    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "loaded"
    assert state["rollback_available"] is True
    assert state["previous_config_path"] == staged_path
    assert state["candidate_config_path"] == commission_path


def test_summed_commissioning_load_happy_path(monkeypatch, tmp_path, reconcile_triggers):
    # Armed HERE, not in `_load`: this test inlines the production entry point
    # (`load_summed_commissioning_config`, itself a wrapper over
    # `load_driver_commissioning_config`), so it meets Wave 3's
    # `commissioning_transport_armed` gate without reaching any shared harness —
    # no helper edit can arm it. One of exactly two rows in that position.
    arm_ring_transport(monkeypatch)
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    statefile = _statefile(tmp_path, staged_path)
    path_safety = _write_path_safety(
        tmp_path / "path_safety.json",
        staged=staged,
        current_config_path=staged_path,
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    cam = FakeCommissionCamilla(staged_path)
    state_path = tmp_path / "commission_load.json"

    result = asyncio.run(
        load_summed_commissioning_config(
            _topology(),
            speaker_group_id="mono",
            load_config=cam.apply_running_config,
            read_running_config=cam.read_running_config,
            get_current_config_path=cam.get_config_file_path,
            path_safety_evidence_path=path_safety,
            staged_config=staged,
            config_path=tmp_path / "commission.yml",
            statefile_path=statefile,
            state_path=state_path,
            validate=_valid_config,
        )
    )

    assert result["operation"] == "summed_commissioning"
    assert result["preflight"]["load_allowed"] is True
    assert result["load"]["status"] == "loaded"
    assert result["load"]["target"]["role"] == "summed"
    assert result["load"]["target"]["audible_outputs"] == [0, 1]
    assert result["load"]["live_evidence"]["passed"] is True
    assert result["load"]["live_evidence"]["audible_tweeter_outputs"] == [1]
    assert reconcile_triggers == [{
        "units": (startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,),
        "verb": "start",
        "reason": "active_speaker_driver_commission_load",
        "no_block": False,
        "timeout": 15.0,
    }]


def test_commissioning_ramp_reload_can_skip_output_reconcile(
    monkeypatch, tmp_path, reconcile_triggers
):
    result, _cam, _staged, _staged_path, _statefile, state_path = _load(
        tmp_path,
        monkeypatch,
        role="woofer",
        reconcile_output_hardware=False,
    )

    assert result["load"]["status"] == "loaded"
    assert result["load"]["output_reconcile"] == {
        "status": "skipped",
        "reason": "same_active_output_lane",
        "unit": startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,
    }
    assert reconcile_triggers == []
    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "loaded"
    assert state["output_reconcile"]["status"] == "skipped"


def test_commissioning_load_fails_closed_when_reconcile_trigger_fails(
    monkeypatch, tmp_path
):
    def fail_manage_units(*units: str, **kwargs):
        return {"ok": False, "rc": 3, "error": "systemd unavailable"}

    monkeypatch.setattr(startup_load_mod, "manage_units", fail_manage_units)
    result, _cam, _staged, _staged_path, _statefile, state_path = _load(
        tmp_path, monkeypatch, role="woofer"
    )

    assert result["load"]["status"] == "failed"
    assert result["load"]["last_action"] == "output_reconcile_failed"
    assert {
        issue["code"] for issue in result["load"]["issues"]
    } == {"commission_output_hardware_reconcile_failed"}
    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "failed"
    assert state["loaded"] is False


def test_tweeter_commissioning_load_confirms_live_high_pass(monkeypatch, tmp_path):
    result, cam, *_ = _load(tmp_path, monkeypatch, role="tweeter")
    assert result["load"]["status"] == "loaded"
    live = result["load"]["live_evidence"]
    assert live["passed"] is True
    assert live["checks"]["tweeter_protected_while_audible"] is True


def test_commissioning_load_keeps_boot_statefile_all_muted(monkeypatch, tmp_path):
    # S3 (MUST-TEST): the transient per-driver config is loaded into RUNNING
    # CamillaDSP only; the durable outputd statefile must STILL point at the
    # all-muted staged boot config (crash-recovery-MUTED).
    result, cam, staged, staged_path, statefile, _ = _load(
        tmp_path, monkeypatch, role="tweeter"
    )
    assert result["load"]["status"] == "loaded"

    after = parse_camilla_statefile_config_path(statefile.read_text(encoding="utf-8"))
    assert after == staged_path
    assert "active_speaker_commissioning.yml" not in statefile.read_text()
    assert result["load"]["durable_statefile_intact"] is True
    assert result["load"]["durable_statefile_target"] == staged_path
    # The staged boot config on disk is untouched (the loader never wrote it).
    assert Path(staged_path).exists()


def test_live_confirm_mismatch_rolls_back_to_staged(monkeypatch, tmp_path):
    # The loader applies the woofer config, but the RUNNING graph drifts to also
    # unmute the tweeter -> the live read-back gate must fail closed and the apply
    # must roll back to the all-muted staged anchor.
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    drift_raw = _block(_emit(preset, woofer | tweeter))

    result, cam, staged, staged_path, statefile, state_path = _load(
        tmp_path, monkeypatch, role="woofer", drift_raw=drift_raw
    )

    assert result["load"]["status"] == "failed"
    codes = {issue["code"] for issue in result["load"]["issues"]}
    assert "driver_commission_load_failed" in codes
    # Rolled back to the staged anchor (last load_config call targets it).
    assert cam.loaded_paths[-1] == staged_path
    # The boot statefile is still the all-muted staged config.
    assert parse_camilla_statefile_config_path(statefile.read_text()) == staged_path
    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "failed"
    assert state["rollback_available"] is False


def test_load_blocks_when_role_is_unknown(monkeypatch, tmp_path):
    result, cam, *_ = _load(tmp_path, monkeypatch, role="subwoofer")
    assert result["preflight"]["load_allowed"] is False
    assert result["load"]["status"] == "blocked"
    assert cam.loaded_paths == []  # loads nothing


def test_load_blocks_when_speaker_not_ready(monkeypatch, tmp_path):
    # No path-safety evidence -> the startup-load half of the gate fails.
    result, cam, *_ = _load(tmp_path, monkeypatch, with_path_safety=False)
    assert result["preflight"]["load_allowed"] is False
    assert result["load"]["status"] == "blocked"
    assert cam.loaded_paths == []
    gates = {g["id"]: g["passed"] for g in result["preflight"]["required_gates"]}
    assert gates["speaker_ready_for_active_load"] is False


def test_durable_statefile_drift_fails_closed(monkeypatch, caplog, tmp_path):
    # Defensive: if the persisted statefile somehow points at the transient
    # commissioning config (impossible with the inline transport), the load must
    # FAIL CLOSED inside the lock (roll back to staged), not report 'loaded' with
    # a buried blocker -- a reboot must never come up on the transient config.
    commission_path = str(tmp_path / "commission.yml")
    with caplog.at_level("WARNING", logger=commission_load_mod.logger.name):
        result, cam, staged, staged_path, statefile, state_path = _load(
            tmp_path, monkeypatch, role="woofer", statefile_target=commission_path
        )
    assert result["load"]["status"] == "failed"
    assert result["load"]["durable_statefile_intact"] is False
    assert "drifted" in result["load"]["dsp_apply"]["persist_error"]
    # Rolled the running graph back to the all-muted staged anchor.
    assert cam.loaded_paths[-1] == staged_path
    # The safety reason reaches the journal, not just the state file.
    assert "result=failed" in caplog.text
    assert "drifted" in caplog.text
    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "failed"
    assert state["rollback_available"] is False


def test_load_blocks_when_active_graph_is_not_staged(monkeypatch, tmp_path):
    # Armed HERE, not in `_load`: this test inlines
    # `load_driver_commissioning_config` directly, so no helper edit can arm it.
    # The arming is what keeps this test ISOLATING the gate it names — without
    # it the ring-transport liveness gate also fires and pollutes the issue-code
    # set the assertion below reads. One of exactly two rows in that position.
    arm_ring_transport(monkeypatch)
    # Precondition: commissioning requires the all-muted staged boot config to be
    # the active graph first. If a different config is persisted/active, fail
    # closed (the path-safety binding + the explicit precondition gate both
    # refuse) and load nothing.
    staged = _staged(tmp_path)
    other = _protected_prior(tmp_path, staged, "other_active.yml")
    statefile = _statefile(tmp_path, str(other))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    # Path-safety evidence is bound to `other` as the current config, so the
    # startup binding PASSES -- isolating the active-graph-not-staged gate.
    path_safety = _write_path_safety(
        tmp_path / "path_safety.json", staged=staged, current_config_path=other
    )
    cam = FakeCommissionCamilla(str(other))
    result = asyncio.run(
        load_driver_commissioning_config(
            _topology(),
            speaker_group_id="mono",
            role="woofer",
            load_config=cam.apply_running_config,
            read_running_config=cam.read_running_config,
            get_current_config_path=cam.get_config_file_path,
            path_safety_evidence_path=path_safety,
            staged_config=staged,
            config_path=tmp_path / "commission.yml",
            statefile_path=statefile,
            state_path=tmp_path / "commission_load.json",
            validate=_valid_config,
        )
    )
    assert result["load"]["status"] == "blocked"
    assert cam.loaded_paths == []
    codes = {issue["code"] for issue in result["load"]["issues"]}
    assert "commission_active_graph_not_staged" in codes


def test_live_confirm_readback_failure_is_observable(monkeypatch, tmp_path):
    # If the running graph can't be read back after the load (camilla wedged), the
    # gate fails closed (rolls back) AND records WHY -- distinguishable from an
    # actual unsafe-graph failure.
    result, cam, staged, staged_path, statefile, state_path = _load(
        tmp_path,
        monkeypatch,
        role="woofer",
        camilla=ReadFailingCamilla(_staged(tmp_path)["config"]["path"]),
    )
    assert result["load"]["status"] == "failed"
    assert result["load"]["live_evidence"]["checks"] == {
        "running_config_readable": False
    }
    assert cam.loaded_paths[-1] == staged_path


def test_running_graph_matches_staged_anchor_discriminates():
    # The convergence discriminator: True only when every INTENDED-audible
    # output is still hard-muted (the staged anchor's defining feature). An
    # unparseable readback or an empty intent cannot positively prove "still
    # the anchor", so both return False and the live evidence decides.
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    staged_raw = _block(_emit(preset, set()))  # all-muted anchor shape
    commission_raw = _block(_emit(preset, woofer))
    assert (
        running_graph_matches_staged_anchor(staged_raw, audible_outputs=woofer)
        is True
    )
    assert (
        running_graph_matches_staged_anchor(commission_raw, audible_outputs=woofer)
        is False
    )
    for raw in (None, "", "::not yaml::"):
        assert running_graph_matches_staged_anchor(raw, audible_outputs=woofer) is False
    assert running_graph_matches_staged_anchor(staged_raw, audible_outputs=[]) is False


def test_live_confirm_polls_until_readback_leaves_staged_anchor(
    monkeypatch, tmp_path
):
    # THE 2026-07-15 JTS3 bug (hardware-reproduced 2/2): CamillaDSP acks the
    # inline load ~22 ms before its readback reflects the new graph, so a
    # single-shot read still sees the staged all-muted anchor and fails safety
    # (audible_mask_correct + startup_headroom). The live confirm must POLL —
    # re-read until the readback leaves the anchor — then pass on the converged
    # graph.
    monkeypatch.setattr(commission_load_mod, "LIVE_CONFIRM_POLL_INTERVAL_S", 0.0)
    staged = _staged(tmp_path)
    staged_raw = _block(Path(staged["config"]["path"]).read_text(encoding="utf-8"))
    cam = SettlingCamilla(staged["config"]["path"], staged_raw, lag_reads=3)

    result, cam, *_ = _load(tmp_path, monkeypatch, role="woofer", camilla=cam)

    assert result["load"]["status"] == "loaded"
    assert result["load"]["live_evidence"]["passed"] is True
    # It re-read the running graph (polled), not slept once and hoped: three
    # staged readbacks, then the converged one.
    assert cam.read_calls == 4


def test_live_confirm_never_converging_raises_convergence_not_safety(
    monkeypatch, caplog, tmp_path
):
    # If the readback NEVER stops matching the staged anchor within the budget,
    # the failure is a load/convergence failure — a DISTINCT taxonomy from the
    # safety-check failure, so downstream surfaces don't burn the operator's
    # repeat budget with "keep the room quiet" advice for an infra fault.
    monkeypatch.setattr(commission_load_mod, "LIVE_CONFIRM_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(commission_load_mod, "LIVE_CONFIRM_CONVERGENCE_BUDGET_S", 0.05)
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    staged_raw = _block(Path(staged_path).read_text(encoding="utf-8"))
    cam = StuckCamilla(staged_path, staged_raw)
    # Deterministic budget expiry: with a real wall clock, a CI scheduling
    # stall > the 50 ms budget between loop start and the first post-read
    # check ends the loop after ONE read and flakes `read_calls > 1`
    # (observed twice on the py3.12 leg, 2026-07-16). Freeze the module's
    # clock at 0 until the second read has happened, then expire the
    # budget — read-count-keyed, so it stays valid if the loop gains or
    # loses monotonic() call sites. Scoped to the module's `time` binding
    # (delegating wrapper), never the global time module.
    class _ReadKeyedTime:
        def monotonic(self):
            return 0.0 if cam.read_calls < 2 else 1000.0

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(commission_load_mod, "time", _ReadKeyedTime())

    with caplog.at_level("INFO", logger=commission_load_mod.logger.name):
        result, cam, *_ = _load(tmp_path, monkeypatch, role="woofer", camilla=cam)

    assert result["load"]["status"] == "failed"
    reason = result["load"]["dsp_apply"]["persist_error"]
    assert "never switched off the staged all-muted anchor" in reason
    assert "commissioning load did not take effect" in reason
    assert "failed live commissioning safety" not in reason
    # It kept polling until the budget, and rolled back to the staged anchor.
    assert cam.read_calls > 1
    assert cam.loaded_paths[-1] == staged_path
    # One structured convergence-outcome line per commission attempt.
    assert "event=active_speaker.driver_commission_live_confirm" in caplog.text
    assert "converged=false" in caplog.text


def test_live_confirm_converged_unsafe_graph_keeps_safety_taxonomy(
    monkeypatch, tmp_path
):
    # A readback that HAS left the staged anchor but genuinely violates the
    # intended mask keeps the existing safety-failure message — and decides on
    # the first read (no pointless convergence polling of an unsafe graph).
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    tweeter = set(audible_outputs_for_role(preset, "tweeter"))
    drift_raw = _block(_emit(preset, woofer | tweeter))

    result, cam, staged, staged_path, *_ = _load(
        tmp_path, monkeypatch, role="woofer", drift_raw=drift_raw
    )

    assert result["load"]["status"] == "failed"
    reason = result["load"]["dsp_apply"]["persist_error"]
    assert "failed live commissioning safety" in reason
    assert "audible_mask_correct" in reason
    assert "did not take effect" not in reason
    assert cam.read_calls == 1
    assert cam.loaded_paths[-1] == staged_path


def test_commissioning_load_re_emits_candidate_inside_the_lock(monkeypatch, tmp_path):
    # The candidate is re-emitted under apply_dsp_config's writer lock (with
    # run_config_check=False) immediately before load, so a concurrent prepare
    # cannot overwrite the shared commissioning path between gate and load
    # (TOCTOU) and the live mask is fresh.
    real = commission_load_mod.prepare_driver_commissioning_config
    run_checks: list[bool] = []

    def _spy(*args, **kwargs):
        run_checks.append(kwargs.get("run_config_check", True))
        return real(*args, **kwargs)

    monkeypatch.setattr(
        commission_load_mod, "prepare_driver_commissioning_config", _spy
    )
    result, cam, *_ = _load(tmp_path, monkeypatch, role="woofer")

    assert result["load"]["status"] == "loaded"
    # Preflight prepares with the full syntax check; the in-lock re-emit skips it
    # (apply_dsp_config's own validate is the load-time gate).
    assert True in run_checks  # preflight gate
    assert False in run_checks  # in-lock re-emit


def test_rollback_reloads_the_staged_all_muted_config(monkeypatch, tmp_path):
    result, cam, staged, staged_path, statefile, state_path = _load(
        tmp_path, monkeypatch, role="woofer"
    )
    assert result["load"]["status"] == "loaded"

    rollback = asyncio.run(
        rollback_driver_commissioning_config(
            load_config=cam.apply_running_config,
            state_path=state_path,
            validate=_valid_config,
        )
    )
    assert rollback["rollback"]["status"] == "rolled_back"
    # The last thing loaded into the running graph is the all-muted staged config.
    assert cam.loaded_paths[-1] == staged_path
    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "rolled_back"
    assert state["rollback_available"] is False


# --- #2412 Wave 4: the load line names the transport -------------------------


@pytest.mark.parametrize(
    "playback_device, arm, expect_transport",
    [
        # The LAB/CI OVERRIDE route — `resolve_output_layout` case 1, which
        # honours any explicit device. Since ADR-0100 there is no second
        # transport to name it, so the line reports the journal's own "no
        # answer" literal. The device is this campaign's established lab idiom
        # and is NOT one of `FORBIDDEN_ACTIVE_PLAYBACK_TOKENS`.
        ("hw:CARD=Lab,DEV=0", False, "-"),
        # The PRODUCTION route — no override, so this walks case 2, the chooser
        # P2 made unconditional. Previously this row passed the ring device
        # explicitly and so also went through case 1; routing it through the
        # chooser is what makes the pair cover both resolution paths.
        (None, True, "ring"),
    ],
    ids=["explicit_lab_lane", "production_chooser_ring"],
)
def test_the_load_line_names_the_transport_on_both_polarities(
    monkeypatch, tmp_path, caplog, playback_device, arm, expect_transport
):
    """`result=loaded` says WHICH transport the swap landed on.

    "The swap failed" and "the swap failed ON THE RING" are the same journal
    line without this field, and the second is the one new failure mode
    commissioning-on-the-ring introduces. Both polarities, because one would
    pass against a line that hard-coded either answer.

The pair is ring vs the journal's `-`: ADR-0100 left one transport, so a
    device that is not a ring end has no name to be given, and this line says so
    rather than inventing one.

    The ring arm is also the PROOF that `arm_ring_transport` does something —
    the reason #2412 can ship that helper for P2 to call rather than shipping an
    untested one. It reaches `status="loaded"` only because the transport was
    armed, and the control below runs the same call without arming.
    """
    with caplog.at_level("INFO", logger=commission_load_mod.logger.name):
        result, *_ = _load(
            tmp_path,
            monkeypatch,
            role="woofer",
            playback_device=playback_device,
            arm_transport=arm,
        )

    assert result["load"]["status"] == "loaded", result
    lines = [
        record.message
        for record in caplog.records
        if "event=active_speaker.driver_commission_load result=loaded" in record.message
    ]
    assert len(lines) == 1, lines
    assert f"transport={expect_transport}" in lines[0]


def test_an_unarmed_ring_blocks_the_load_and_the_arming_is_what_lifts_it(
    monkeypatch, tmp_path
):
    """The arming is load-bearing, not decorative — the control that proves it.

    THE MUTATION, AS A TEST. Without this, `arm_ring_transport` could silently
    stop working and the ring polarity above would be the only thing that
    noticed — a helper whose effect is asserted by one green test and nothing
    else. This runs the identical call with arming withheld and requires the
    gate Wave 3 shipped to refuse it.

    Only the MARKER conjunct is asserted: with no `outputd.env` the endpoint
    reads unarmed, while no `fanin.env` names no transport and ADR-0100 leaves
    that a fan-in ON the ring, so the coupling half correctly does not refuse
    here. Its refusal is pinned against real files in
    `tests/test_transport_endpoint_preservation.py`.
    """
    result, *_ = _load(
        tmp_path,
        monkeypatch,
        role="woofer",
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        arm_transport=False,
    )

    assert result["load"]["status"] == "blocked", result
    gate = next(
        g
        for g in result["preflight"]["required_gates"]
        if g["id"] == "commissioning_transport_armed"
    )
    assert gate["passed"] is False, gate
    codes = {issue["code"] for issue in result["preflight"]["issues"]}
    assert "commissioning_active_endpoint_unarmed" in codes, codes
