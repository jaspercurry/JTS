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
from pathlib import Path

import yaml

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    audible_outputs_for_role,
    driver_commission_audible_evidence,
    emit_active_speaker_commissioning_config,
    load_commission_load_state,
    load_driver_commissioning_config,
    parse_camilla_statefile_config_path,
    rollback_driver_commissioning_config,
    running_commission_evidence,
)

# Reuse the canonical mono DAC8x topology + passing-validation stub + path-safety
# evidence writer from the protected-startup-load tests.
from tests.test_active_speaker_startup_load import (
    _protected_prior,
    _staged,
    _topology,
    _valid_config,
    _write_path_safety,
)
from tests.test_active_speaker_profile import _two_way_preset


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

    async def apply_running_config(self, path: str) -> bool:
        text = Path(path).read_text(encoding="utf-8")
        # CamillaDSP re-serializes the running graph in its own YAML dialect.
        self.running_raw = _block(text)
        self.loaded_paths.append(str(path))
        return True

    async def read_running_config(self) -> str | None:
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
):
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
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
            validate=_valid_config,
        )
    )
    return result, cam, staged, staged_path, statefile, state_path


def test_woofer_commissioning_load_happy_path(monkeypatch, tmp_path):
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

    state = load_commission_load_state(state_path=state_path)
    assert state["status"] == "loaded"
    assert state["rollback_available"] is True
    assert state["previous_config_path"] == staged_path
    assert state["candidate_config_path"] == commission_path


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
    import jasper.active_speaker.startup_load as startup_load_mod

    commission_path = str(tmp_path / "commission.yml")
    with caplog.at_level("WARNING", logger=startup_load_mod.logger.name):
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
