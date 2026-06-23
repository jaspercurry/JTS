# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/sound/ web payloads for per-driver commissioning + the Stage-5 ramp.

The payloads are thin wrappers over the library (exhaustively tested in
tests/test_active_speaker_commission_load.py + tests/test_active_speaker_stage5_ramp.py),
so these pin the WEB wiring: the inline CamillaController seams, single-flight,
the read-only state endpoint (no preflight side-effect), and that the happy path
reaches the guarded load. Tested as pure functions with a fake Camilla, the same
shape as tests/test_sound_setup.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

import jasper.active_speaker.startup_load as startup_load_mod
import jasper.web.sound_setup as sound_setup
from jasper.active_speaker import (
    ActiveSpeakerPreset,
    load_commission_load_state,
    load_ramp_state,
)
from jasper.active_speaker.calibration_level import AUDIBLE_RAMP_STEP_DB
from jasper.active_speaker.measurement import record_driver_measurement

from tests.test_active_speaker_cli import _FakeController
from tests.test_active_speaker_startup_load import _staged, _topology


@pytest.fixture(autouse=True)
def _stub_audio_hardware_reconcile(monkeypatch):
    def fake_manage_units(*units: str, **kwargs):
        return {"ok": True, "rc": 0}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)


def _web_commission_env(monkeypatch, tmp_path, controller: _FakeController) -> dict:
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {staged_path}\nmute: false\n", encoding="utf-8")
    controller.persisted_path = staged_path

    monkeypatch.setattr(sound_setup, "load_output_topology", lambda path=None: _topology())
    monkeypatch.setattr(
        "jasper.active_speaker.staging.load_staged_startup_config", lambda: staged
    )
    monkeypatch.setattr(
        "jasper.active_speaker.startup_load.load_staged_startup_config",
        lambda: staged,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.staging.commissioning_config_path",
        lambda **kwargs: tmp_path / "commission.yml",
    )
    monkeypatch.setattr(
        "jasper.active_speaker.design_draft.load_design_draft", lambda path=None: {}
    )
    monkeypatch.setattr(
        "jasper.active_speaker.crossover_preview.load_crossover_preview",
        lambda path=None, current_design_draft=None: {"status": "not_prepared"},
    )
    fake_camilla = tmp_path / "camilladsp"
    fake_camilla.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_camilla.chmod(0o755)
    monkeypatch.setenv("JASPER_CAMILLADSP_BIN", str(fake_camilla))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE", str(tmp_path / "path_safety.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        str(tmp_path / "commission_load.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE",
        str(tmp_path / "startup_load.json"),
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", str(tmp_path / "ramp.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(tmp_path / "safe.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(tmp_path / "calibration_level.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE",
        str(tmp_path / "measurements.json"),
    )
    tone_calls: list[dict] = []

    async def _fake_commission_tone(**kwargs):
        tone_calls.append(dict(kwargs))
        return {
            "status": "completed",
            "backend": "fake_commission_tone",
            "playback_id": kwargs.get("playback_id"),
            "audio_emitted": True,
            "confirmable": True,
            "tone": {
                "frequency_hz": 120.0,
                "source_level_dbfs": 0.0,
                "commission_gain_db": kwargs.get("level_dbfs"),
            },
            "issues": [],
        }

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_play_commission_tone",
        _fake_commission_tone,
    )
    return {
        "staged": staged,
        "staged_path": staged_path,
        "statefile": statefile,
        "tone_calls": tone_calls,
    }


class _FakeWebController(_FakeController):
    def __init__(self, persisted_path: str, statefile: Path) -> None:
        super().__init__(persisted_path)
        self.statefile = statefile
        self.path_loads: list[str] = []

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        self.path_loads.append(str(path))
        self.persisted_path = str(path)
        self.statefile.write_text(f"config_path: {path}\nmute: false\n", encoding="utf-8")
        self.running_raw = Path(path).read_text(encoding="utf-8")
        return True


class _FakeToneProcess:
    def __init__(self, args: list[str], *, exit_after_polls: int | None = None) -> None:
        self.args = args
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.exit_after_polls = exit_after_polls
        self.poll_count = 0

    def poll(self) -> int | None:
        if self.returncode is None and self.exit_after_polls is not None:
            self.poll_count += 1
            if self.poll_count >= self.exit_after_polls:
                self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


def _tone_preset(
    *,
    way_count: int = 2,
    woofer_tweeter_hz: float = 2000,
    woofer_mid_hz: float = 300,
    mid_tweeter_hz: float = 3000,
) -> ActiveSpeakerPreset:
    roles = ("woofer", "tweeter") if way_count == 2 else ("woofer", "mid", "tweeter")
    outputs = [
        {
            "index": index,
            "side": "mono",
            "driver_role": role,
            "label": f"mono {role}",
            "startup_muted": True,
        }
        for index, role in enumerate(roles)
    ]
    regions = (
        [{
            "id": "woofer_tweeter",
            "lower_driver": "woofer",
            "upper_driver": "tweeter",
            "fc_hz": woofer_tweeter_hz,
            "target_type": "LinkwitzRiley",
            "order": 4,
            "lower_polarity": "non-inverted",
            "upper_polarity": "non-inverted",
            "delay_range_ms": [0.0, 0.5],
            "null_depth_threshold_db": 25,
        }]
        if way_count == 2
        else [
            {
                "id": "woofer_mid",
                "lower_driver": "woofer",
                "upper_driver": "mid",
                "fc_hz": woofer_mid_hz,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
            {
                "id": "mid_tweeter",
                "lower_driver": "mid",
                "upper_driver": "tweeter",
                "fc_hz": mid_tweeter_hz,
                "target_type": "LinkwitzRiley",
                "order": 4,
                "lower_polarity": "non-inverted",
                "upper_polarity": "non-inverted",
                "delay_range_ms": [0.0, 0.5],
                "null_depth_threshold_db": 25,
            },
        ]
    )
    return ActiveSpeakerPreset.from_mapping({
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_preset",
        "preset_id": f"web-tone-{way_count}way",
        "name": f"Web tone {way_count}-way preset",
        "way_count": way_count,
        "channel_map": {"layout": "mono", "outputs": outputs},
        "drivers": {
            role: {"manufacturer": "Example", "model": role.title()}
            for role in roles
        },
        "crossover_regions": regions,
        "safety": {
            "require_physical_tweeter_protection": True,
            "require_channel_identity_before_drivers": True,
            "emergency_stop_required": True,
        },
    })


def test_commission_continuous_tone_reuses_running_process(monkeypatch, tmp_path):
    monkeypatch.setattr(sound_setup, "_COMMISSION_TONE_SESSION", None)
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"not a real wav; Popen is faked")
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        lambda *, frequency_hz: wav_path,
    )
    mux_actions: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_select_fanin_lane",
        lambda: mux_actions.append("select") or {
            "active_source": "correction",
            "test_source": "correction",
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: mux_actions.append(f"release:{reason}") or {
            "active_source": "airplay",
            "test_source": None,
        },
    )
    processes: list[_FakeToneProcess] = []

    def _fake_popen(args, stdout=None, stderr=None):
        proc = _FakeToneProcess(list(args))
        processes.append(proc)
        return proc

    monkeypatch.setattr(sound_setup.subprocess, "Popen", _fake_popen)
    try:
        first = asyncio.run(
            sound_setup._active_speaker_play_commission_tone(
                group_id="mono",
                role="woofer",
                level_dbfs=-80.0,
                playback_id="step-1",
                target={"speaker_group_id": "mono", "role": "woofer"},
            )
        )
        second = asyncio.run(
            sound_setup._active_speaker_play_commission_tone(
                group_id="mono",
                role="woofer",
                level_dbfs=-74.0,
                playback_id="step-2",
                target={"speaker_group_id": "mono", "role": "woofer"},
            )
        )
    finally:
        stop = sound_setup._active_speaker_stop_commission_tone(reason="test_cleanup")

    assert first["status"] == "completed"
    assert first["continuous"] is True
    assert second["session_reused"] is True
    assert second["tone"]["duration_ms"] == 35000
    assert len(processes) == 1
    assert processes[0].args[:4] == ["aplay", "-D", "correction_substream", "-q"]
    assert stop["status"] == "stopped"
    assert first["fanin_gate"]["active_source"] == "correction"
    assert stop["fanin_gate"]["active_source"] == "airplay"
    assert mux_actions == ["select", "select", "release:test_cleanup"]
    assert processes[0].terminated is True


def test_commission_continuous_tone_uses_planner_frequency_for_tweeter(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sound_setup, "_COMMISSION_TONE_SESSION", None)
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"not a real wav; Popen is faked")
    requested_frequencies: list[float] = []
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        lambda *, frequency_hz: requested_frequencies.append(frequency_hz) or wav_path,
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_select_fanin_lane",
        lambda: {"active_source": "correction", "test_source": "correction"},
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: {"active_source": "airplay", "test_source": None},
    )
    monkeypatch.setattr(
        sound_setup.subprocess,
        "Popen",
        lambda args, stdout=None, stderr=None: _FakeToneProcess(list(args)),
    )
    try:
        result = asyncio.run(
            sound_setup._active_speaker_play_commission_tone(
                group_id="mono",
                role="tweeter",
                level_dbfs=-80.0,
                playback_id="tweeter-step",
                target={"speaker_group_id": "mono", "role": "tweeter"},
                preset=_tone_preset(woofer_tweeter_hz=2000),
            )
        )
    finally:
        sound_setup._active_speaker_stop_commission_tone(reason="test_cleanup")

    assert result["status"] == "completed"
    assert requested_frequencies == [6250.0]
    assert result["tone"]["frequency_hz"] == 6250.0
    assert result["tone"]["frequency_hz"] != 5000.0
    assert result["signal_plan"]["allowed_band"]["highpass_hz"] == 5000.0
    assert result["signal_plan"]["selection_reason"] == "above_strictest_highpass_edge"


def test_commission_continuous_tone_blocks_when_planner_has_no_safe_band(
    monkeypatch,
):
    monkeypatch.setattr(sound_setup, "_COMMISSION_TONE_SESSION", None)
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        lambda *, frequency_hz: (_ for _ in ()).throw(
            AssertionError("wav generation should not run")
        ),
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_select_fanin_lane",
        lambda: (_ for _ in ()).throw(
            AssertionError("fanin should not be selected")
        ),
    )

    result = asyncio.run(
        sound_setup._active_speaker_play_commission_tone(
            group_id="mono",
            role="mid",
            level_dbfs=-80.0,
            playback_id="mid-step",
            target={"speaker_group_id": "mono", "role": "mid"},
            preset=_tone_preset(
                way_count=3,
                woofer_mid_hz=1000,
                mid_tweeter_hz=1100,
            ),
        )
    )

    assert result["status"] == "blocked"
    assert result["audio_emitted"] is False
    assert result["tone"]["frequency_hz"] is None
    assert "driver_test_signal_no_safe_band" in {
        issue["code"] for issue in result["issues"]
    }


def test_commission_state_payload_is_idle_and_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        str(tmp_path / "commission_load.json"),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", str(tmp_path / "ramp.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(tmp_path / "safe.json")
    )
    # The state read must NOT run the preflight (which emits the candidate YAML).
    monkeypatch.setattr(
        "jasper.active_speaker.startup_load.build_driver_commission_load_preflight",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("preflight on a read")),
    )
    payload = asyncio.run(
        sound_setup._active_speaker_commission_state_payload(
            camilla_factory=lambda: (_ for _ in ()).throw(
                AssertionError("camilla should not be read while idle")
            )
        )
    )
    assert payload["commission_load"]["status"] == "idle"
    assert payload["ramp"]["confirmed_roles"] == []
    assert payload["ramp"]["pending"] is None
    assert payload["floor"]["status"] == "floor_required"


def test_commission_load_payload_arms_woofer_at_floor(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert payload["load"]["status"] == "loaded"
    assert payload["load"]["target"]["role"] == "woofer"
    # The inline seam applied the woofer commissioning config into the running graph.
    assert len(controller.applied_texts) == 1
    assert "audible_outputs=[0]" in controller.applied_texts[0]
    assert load_commission_load_state()["status"] == "loaded"


def test_commission_load_payload_loads_silent_startup_anchor(
    monkeypatch, tmp_path
):
    controller = _FakeWebController("placeholder", tmp_path / "outputd-statefile.yml")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    controller.statefile = env["statefile"]

    normal = tmp_path / "outputd-cutover.yml"
    normal.write_text(Path(env["staged_path"]).read_text(encoding="utf-8"), encoding="utf-8")
    controller.persisted_path = str(normal)
    env["statefile"].write_text(f"config_path: {normal}\nmute: false\n", encoding="utf-8")

    setup_order: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stage_config_payload",
        lambda raw: setup_order.append("stage") or env["staged"],
    )
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        lambda: setup_order.append("preview") or {
            "status": "ready_for_protected_staging",
            "permissions": {"may_prepare_protected_startup_config": True},
        },
    )

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )

    assert setup_order == ["preview", "stage"]
    assert controller.path_loads == [env["staged_path"]]
    assert payload["startup_setup"]["status"] == "loaded"
    assert payload["startup_setup"]["preview_status"] == "ready_for_protected_staging"
    assert payload["load"]["status"] == "loaded"
    assert load_commission_load_state()["status"] == "loaded"


def test_commission_load_payload_clears_stale_pending_ramp(
    monkeypatch, tmp_path
):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    ramp_path = tmp_path / "ramp.json"
    ramp_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_commission_ramp",
            "speaker_group_id": "mono",
            "confirmed_roles": ["woofer"],
            "pending": {
                "role": "woofer",
                "gain_db": -30.0,
                "playback_id": "old-step",
                "is_floor_step": False,
            },
            "last_action": "step",
        }),
        encoding="utf-8",
    )

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "tweeter"}, camilla_factory=lambda: controller
        )
    )

    assert payload["load"]["status"] == "loaded"
    ramp = load_ramp_state()
    assert ramp["pending"] is None
    assert ramp["confirmed_roles"] == ["woofer"]
    assert ramp["speaker_group_id"] == "mono"
    assert ramp["last_action"] == "clear_pending"


def test_commission_load_payload_single_flight_refuses(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    assert asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["load"]["status"] == "loaded"

    refused = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "tweeter"}, camilla_factory=lambda: controller
        )
    )
    assert refused["status"] == "refused"
    assert refused["reason"] == "commission_load_already_active"
    assert len(controller.applied_texts) == 1  # nothing new applied


def test_commission_load_payload_same_target_is_idempotent(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    assert asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["load"]["status"] == "loaded"

    again = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert again["status"] == "loaded"
    assert again["load"]["status"] == "loaded"
    assert len(controller.applied_texts) == 1  # no re-load needed


def test_commission_load_payload_rearms_stale_persisted_state(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    assert asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["load"]["status"] == "loaded"

    # Simulate a later Camilla/web restart: the JSON still says loaded, but the
    # live graph is back at the all-muted startup anchor.
    controller.running_raw = Path(env["staged_path"]).read_text(encoding="utf-8")

    again = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert again["load"]["status"] == "loaded"
    assert len(controller.applied_texts) == 2
    assert load_commission_load_state()["status"] == "loaded"


def test_commission_state_payload_marks_stale_live_graph_read_only(
    monkeypatch, tmp_path
):
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    assert asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["load"]["status"] == "loaded"
    controller.running_raw = Path(env["staged_path"]).read_text(encoding="utf-8")

    payload = asyncio.run(
        sound_setup._active_speaker_commission_state_payload(
            camilla_factory=lambda: controller
        )
    )

    assert payload["commission_load"]["status"] == "stale"
    assert payload["commission_load"]["runtime_status"]["status"] == "stale"
    # GET/status is read-only; the next POST performs the self-heal/re-arm.
    assert load_commission_load_state()["status"] == "loaded"


def test_commission_ramp_step_and_ack_payloads(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    tone_stops: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: tone_stops.append(reason)
        or {"status": "stopped", "reason": reason},
    )
    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )

    step = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert step["status"] == "stepped"
    assert step["tone_playback"]["audio_emitted"] is True
    assert env["tone_calls"][0]["role"] == "woofer"
    assert env["tone_calls"][0]["level_dbfs"] == -80.0
    assert step["safe_playback"]["floor_status"] == "floor_pending_operator"
    assert step["ramp"]["pending"]["frequency_hz"] == 120.0
    # The running graph now carries the woofer un-muted at the audible floor.
    assert yaml.safe_load(controller.running_raw)["filters"]["as_out0_commission_mute"][
        "parameters"
    ]["mute"] is False

    retry = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "woofer", "auto_retry_pending": True},
            camilla_factory=lambda: controller,
        )
    )
    assert retry["status"] == "stepped"
    assert retry["ramp"]["pending"]["frequency_hz"] == 120.0
    assert env["tone_calls"][1]["level_dbfs"] == -80.0 + AUDIBLE_RAMP_STEP_DB

    ack = asyncio.run(
        sound_setup._active_speaker_commission_ramp_ack_payload(
            {"outcome": "heard_correct_driver"}, camilla_factory=lambda: controller
        )
    )
    assert ack["status"] == "confirmed"
    assert ack["rollback"]["status"] == "rolled_back"
    assert ack["tone_stop"] == {
        "status": "stopped",
        "reason": "ack_heard_correct_driver",
    }
    latest = ack["measurements"]["summary"]["latest_driver_measurements"][
        "mono:woofer"
    ]
    assert latest["captured"] is True
    assert latest["outcome"] == "heard_correct_driver"
    assert latest["playback_id"] == retry["ramp"]["pending"]["playback_id"]
    assert latest["test_level_dbfs"] == -80.0 + AUDIBLE_RAMP_STEP_DB
    assert ack["measurements"]["summary"]["captured_driver_count"] == 1
    assert tone_stops == ["ack_heard_correct_driver"]
    assert load_ramp_state()["confirmed_roles"] == ["woofer"]
    assert load_commission_load_state()["status"] == "rolled_back"


def test_commission_ramp_abort_payload_remutes(monkeypatch, tmp_path):
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    step = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert step["status"] == "stepped"
    out = asyncio.run(
        sound_setup._active_speaker_commission_ramp_abort_payload(
            camilla_factory=lambda: controller
        )
    )
    assert out["status"] == "aborted"
    # Re-muted: the last thing applied is the all-muted staged config.
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    safe = load_safe_playback_state()
    assert safe["status"] == "stopped"
    assert safe["quiet_start"]["status"] == "floor_required"


def _record_driver_checks_for_summed_test() -> None:
    topology = _topology()
    for role, output_index in (("woofer", 0), ("tweeter", 1)):
        playback_id = f"playback-{role}"
        target = {
            "speaker_group_id": "mono",
            "role": role,
            "driver_role": role,
            "output_index": output_index,
        }
        record_driver_measurement(
            topology,
            {
                "speaker_group_id": "mono",
                "role": role,
                "outcome": "heard_correct_driver",
                "observed_mic_dbfs": -42,
                "playback_id": playback_id,
            },
            safe_session={
                "status": "armed",
                "quiet_start": {
                    "status": "floor_confirmed",
                    "floor_audio_confirmed": True,
                    "last_operator_result": {
                        "accepted": True,
                        "outcome": "heard_correct_driver",
                        "playback_id": playback_id,
                        "target": target,
                    },
                },
            },
        )


def test_summed_test_audio_path_loads_plays_rolls_back_and_records(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "resolve_commission_inputs",
        lambda preset=None: (_tone_preset(), None),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    _record_driver_checks_for_summed_test()

    wav_path = tmp_path / "summed.wav"
    wav_path.write_bytes(b"fake wav; subprocess.Popen is faked")
    requested_wavs: list[dict[str, float]] = []

    def _fake_wav_path(
        *,
        frequency_hz: float,
        duration_s: float = sound_setup.COMMISSION_TONE_DURATION_S,
    ) -> Path:
        requested_wavs.append({
            "frequency_hz": float(frequency_hz),
            "duration_s": float(duration_s),
        })
        return wav_path

    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        _fake_wav_path,
    )
    fanin_actions: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_select_fanin_lane",
        lambda: fanin_actions.append("select") or {
            "active_source": "correction",
            "test_source": "correction",
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: fanin_actions.append(f"release:{reason}") or {
            "active_source": "airplay",
            "test_source": None,
        },
    )
    processes: list[_FakeToneProcess] = []
    real_popen = sound_setup.subprocess.Popen

    def _fake_popen(args, *popen_args, **kwargs):
        if args and Path(str(args[0])).name == "aplay":
            proc = _FakeToneProcess(list(args), exit_after_polls=2)
            processes.append(proc)
            return proc
        return real_popen(args, *popen_args, **kwargs)

    monkeypatch.setattr(sound_setup.subprocess, "Popen", _fake_popen)

    payload = asyncio.run(
        sound_setup._active_speaker_summed_test_payload(
            {"speaker_group_id": "mono", "audio": True, "level_dbfs": -40.0},
            camilla_factory=lambda: controller,
        )
    )

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert playback["status"] == "completed", json.dumps(
        playback,
        indent=2,
        sort_keys=True,
        default=str,
    )
    assert playback["backend"] == sound_setup.SUMMED_COMMISSION_TONE_BACKEND
    assert playback["audio_emitted"] is True
    assert playback["tone"]["level_dbfs"] == -40.0
    assert payload["calibration_level"]["test_signal"][
        "requested_level_dbfs"
    ] == -40.0
    assert playback["audio_device"]["pcm"] == sound_setup.COMMISSION_TONE_ALSA_DEVICE
    assert playback["commissioning_load"]["load"]["status"] == "loaded"
    assert playback["commissioning_load"]["load"]["target"]["role"] == "summed"
    assert playback["rollback"]["rollback"]["status"] == "rolled_back"
    assert latest["captured"] is True
    assert latest["audio_emitted"] is True
    assert latest["backend"] == sound_setup.SUMMED_COMMISSION_TONE_BACKEND
    assert latest["target_output_indices"] == [0, 1]
    assert len(controller.applied_texts) == 2
    assert "audible_outputs=[0, 1]" in controller.applied_texts[0]
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    assert fanin_actions == ["select", "release:summed_test"]
    assert [proc.args for proc in processes] == [[
        "aplay",
        "-D",
        sound_setup.COMMISSION_TONE_ALSA_DEVICE,
        "-q",
        str(wav_path),
    ]]
    assert requested_wavs and requested_wavs[0]["frequency_hz"] > 0


def test_summed_test_stop_terminates_aplay_and_rolls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "resolve_commission_inputs",
        lambda preset=None: (_tone_preset(), None),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
        str(tmp_path / "tone-artifacts"),
    )
    _record_driver_checks_for_summed_test()

    wav_path = tmp_path / "summed.wav"
    wav_path.write_bytes(b"fake wav; subprocess.Popen is faked")
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        lambda *, frequency_hz, duration_s=sound_setup.COMMISSION_TONE_DURATION_S: wav_path,
    )
    fanin_actions: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_select_fanin_lane",
        lambda: fanin_actions.append("select") or {
            "active_source": "correction",
            "test_source": "correction",
        },
    )
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: fanin_actions.append(f"release:{reason}") or {
            "active_source": "airplay",
            "test_source": None,
        },
    )
    processes: list[_FakeToneProcess] = []
    real_popen = sound_setup.subprocess.Popen

    def _fake_popen(args, *popen_args, **kwargs):
        if args and Path(str(args[0])).name == "aplay":
            proc = _FakeToneProcess(list(args))
            processes.append(proc)
            return proc
        return real_popen(args, *popen_args, **kwargs)

    monkeypatch.setattr(sound_setup.subprocess, "Popen", _fake_popen)

    async def _run_stop_test():
        task = asyncio.create_task(
            sound_setup._active_speaker_summed_test_payload(
                {"speaker_group_id": "mono", "audio": True, "level_dbfs": -40.0},
                camilla_factory=lambda: controller,
            )
        )
        for _ in range(50):
            if processes:
                break
            await asyncio.sleep(0.01)
        assert processes, "summed test should start aplay before stop"
        stop_payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="test_stop"
        )
        return stop_payload, await task

    stop, payload = asyncio.run(_run_stop_test())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert stop["status"] == "stopped"
    assert stop["phase"] == "playing"
    assert processes[0].terminated is True
    assert playback["status"] == "stopped"
    assert playback["audio_emitted"] is False
    assert playback["confirmable"] is False
    assert playback["stop_reason"] == "test_stop"
    assert playback["rollback"]["rollback"]["status"] == "rolled_back"
    assert latest["captured"] is False
    assert latest["audio_emitted"] is False
    assert "summed_test_playback_incomplete" in {
        issue["code"] for issue in latest["issues"]
    }
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    assert fanin_actions == ["select", "release:summed_test"]


def test_summed_test_stop_marks_preparing_session(monkeypatch):
    session = {
        "playback_id": "pending-summed-test",
        "process": None,
        "stop_reason": None,
    }
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", session)
    try:
        payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="test_stop"
        )
    finally:
        monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)

    assert payload == {
        "status": "stopping",
        "reason": "test_stop",
        "playback_id": "pending-summed-test",
        "phase": "preparing",
    }
    assert session["stop_reason"] == "test_stop"


def test_commission_load_repairs_drifted_tweeter_guard(monkeypatch, tmp_path):
    """Arming must repair a tweeter that drifted to ``required_missing``.

    Commission-load is the target-specific arming boundary now. It must
    re-request missing software guards itself so the live topology cannot drift
    away from the staged config and block driver commissioning forever (the jts3
    "speaker isn't fully set up for driver tests yet" wedge).
    """
    from jasper.output_topology import set_channel_protection_status

    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)

    # Drift the tweeter to required_missing (the live jts3 state).
    drifted = set_channel_protection_status(
        _topology(),
        speaker_group_id="mono",
        role="tweeter",
        protection_status="required_missing",
    )
    live = {"topology": drifted}
    monkeypatch.setattr(
        sound_setup, "load_output_topology", lambda path=None: live["topology"]
    )
    monkeypatch.setattr(
        sound_setup,
        "save_output_topology",
        lambda topology: live.__setitem__("topology", topology),
    )

    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )

    tweeter = next(
        channel
        for group in live["topology"].speaker_groups
        for channel in group.channels
        if channel.role == "tweeter"
    )
    assert tweeter.protection_status == "software_guard_requested"
