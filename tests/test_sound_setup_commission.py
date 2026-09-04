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
import time
from pathlib import Path

import pytest
import yaml

import jasper.active_speaker.startup_load as startup_load_mod
import jasper.web.sound_setup as sound_setup
from jasper.active_speaker import (
    ActiveSpeakerPreset,
    load_commission_load_state,
    load_ramp_state,
    reset_ramp_state,
)
from jasper.active_speaker.calibration_level import AUDIBLE_RAMP_STEP_DB
from jasper.active_speaker.measurement import record_driver_measurement

from tests._armed_transport import arm_ring_transport
from tests.active_speaker_fixtures import mono_output_topology as _topology
from tests.test_active_speaker_cli import _FakeController
from tests.test_active_speaker_startup_load import _staged


@pytest.fixture(autouse=True)
def _stub_audio_hardware_reconcile(monkeypatch):
    def fake_manage_units(*units: str, **kwargs):
        return {"ok": True, "rc": 0}

    monkeypatch.setattr(startup_load_mod, "manage_units", fake_manage_units)


def _web_commission_env(monkeypatch, tmp_path, controller: _FakeController) -> dict:
    from jasper.output_topology import output_topology_mutation

    # #2285 P2: this box is on the ring, so the load gate's liveness conjuncts
    # have to answer. `resolve_output_layout` case 2 now names the ring
    # unconditionally, and Wave 3's `commissioning_transport_armed` gate reads
    # fan-in's coupling and outputd's ACTIVE marker FRESH and fails SAFE — so a
    # harness declaring neither reads `loopback` with the marker false and every
    # commission-load call through this env blocks. See `tests/_armed_transport.py`.
    topology_path = tmp_path / "output_topology.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    with output_topology_mutation(topology_path) as mutation:
        mutation.save(_topology())
    arm_ring_transport(monkeypatch)
    staged = _staged(tmp_path)
    staged_path = staged["config"]["path"]
    # rollback_driver_commissioning_config derives its target from
    # staged_config_path() directly (not the statefile), so it must resolve to
    # the same anchor _staged() wrote here, not the real /var/lib/camilladsp
    # default.
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH", str(staged_path))
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {staged_path}\nmute: false\n", encoding="utf-8")
    controller.persisted_path = staged_path

    monkeypatch.setattr(
        "jasper.active_speaker.staging.load_staged_startup_config", lambda: staged
    )
    for module in ("commission_load", "startup_load"):
        monkeypatch.setattr(
            f"jasper.active_speaker.{module}.load_staged_startup_config",
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

    def _fake_popen(args, **_kwargs):
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
        lambda args, **_kwargs: _FakeToneProcess(list(args)),
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


@pytest.mark.parametrize("lane_armed", [False, True])
def test_commission_tone_payload_reports_the_device_the_spawn_used(
    monkeypatch, tmp_path, lane_armed
):
    """Payload-equals-spawn is the sweep's actual promise, pinned ARMED.

    The device-fact sweep (P6c-ii) exists "precisely so an armed box can
    never spawn on the ring while its telemetry reports the substream" —
    but every prior payload assertion ran UNARMED, where the reader and
    the old constant agree by construction, so a payload regressing to
    the IMPORTED CONSTANT (`{"pcm": CORRECTION_SUBSTREAM}`) passed the
    whole suite while diverging on exactly the armed box (found
    empirically by the review panel; the SSOT literal guard is blind to
    imported-constant references by design). This drives the tone flow —
    the spawn through the shared helper AND web_commissioning's
    `_commission_tone_payload` builder — on BOTH transports and asserts
    the payload equals the SPAWN'S OWN argv device, which is stronger
    than asserting either value alone.
    """
    from jasper import renderer_lanes as rl

    map_path = tmp_path / "renderer_lanes.env"
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", str(map_path))
    lane = rl.lane_by_label("correction")
    assert lane is not None
    if lane_armed:
        map_path.write_text(rl.render_env_text((lane.label,)))
    expected_device = lane.ring_device if lane_armed else lane.aloop_device

    monkeypatch.setattr(sound_setup, "_COMMISSION_TONE_SESSION", None)
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"not a real wav; Popen is faked")
    monkeypatch.setattr(
        sound_setup,
        "_commission_tone_wav_path",
        lambda *, frequency_hz: wav_path,
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
    processes: list[_FakeToneProcess] = []

    def _fake_popen(args, **_kwargs):
        proc = _FakeToneProcess(list(args))
        processes.append(proc)
        return proc

    monkeypatch.setattr(sound_setup.subprocess, "Popen", _fake_popen)
    try:
        result = asyncio.run(
            sound_setup._active_speaker_play_commission_tone(
                group_id="mono",
                role="woofer",
                level_dbfs=-80.0,
                playback_id="armed-payload-pin",
                target={"speaker_group_id": "mono", "role": "woofer"},
                preset=_tone_preset(),
            )
        )
    finally:
        sound_setup._active_speaker_stop_commission_tone(reason="test_cleanup")

    assert result["status"] == "completed"
    assert processes, "the tone flow must have spawned aplay"
    spawn_device = processes[0].args[2]  # ["aplay", "-D", <device>, "-q", wav]
    assert result["audio_device"]["pcm"] == spawn_device == expected_device


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
        "jasper.active_speaker.commission_load.build_driver_commission_load_preflight",
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


def test_commission_load_refreshes_stale_anchor_after_identity_confirmation(
    monkeypatch, tmp_path
):
    controller = _FakeWebController("placeholder", tmp_path / "outputd-statefile.yml")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    controller.statefile = env["statefile"]

    fresh_staged = json.loads(json.dumps(env["staged"]))
    stale_staged = json.loads(json.dumps(env["staged"]))
    for target in stale_staged["targets"]:
        target["identity_verified"] = False

    staged_holder = {"payload": stale_staged}
    monkeypatch.setattr(
        "jasper.active_speaker.staging.load_staged_startup_config",
        lambda: staged_holder["payload"],
    )
    for module in ("commission_load", "startup_load"):
        monkeypatch.setattr(
            f"jasper.active_speaker.{module}.load_staged_startup_config",
            lambda: staged_holder["payload"],
        )

    setup_order: list[str] = []
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_crossover_preview_save_payload",
        lambda: setup_order.append("preview") or {
            "status": "ready_for_protected_staging",
            "permissions": {"may_prepare_protected_startup_config": True},
        },
    )

    def fake_stage(raw):
        setup_order.append("stage")
        staged_holder["payload"] = fresh_staged
        return fresh_staged

    monkeypatch.setattr(sound_setup, "_active_speaker_stage_config_payload", fake_stage)

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )

    assert setup_order == ["preview", "stage"]
    assert controller.path_loads == [env["staged_path"]]
    assert payload["startup_setup"]["status"] == "loaded"
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


def test_commission_rollback_payload_clears_pending_ramp_step(monkeypatch, tmp_path):
    """#2669: the web Stop-short — a bare rollback — re-mutes the graph, so the
    step it was waiting on goes with it. The ordering memory does not."""
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
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
    assert step["ramp"]["pending"]["role"] == "woofer"

    payload = asyncio.run(
        sound_setup._active_speaker_commission_rollback_payload(
            camilla_factory=lambda: controller
        )
    )
    assert payload["rollback"]["status"] == "rolled_back"
    assert payload["ramp"]["pending"] is None
    # A surface reading the ramp file between this rollback and the next arm
    # sees no audible step outstanding — because there isn't one.
    assert load_ramp_state()["pending"] is None
    assert load_ramp_state()["speaker_group_id"] == "mono"


def test_commission_rollback_payload_keeps_pending_when_rollback_fails(
    monkeypatch, tmp_path
):
    """Fail-closed on the surface the household actually drives: a rollback that
    did NOT reach the anchor leaves the step alone — the driver may still be
    audible and still needs its ACK. Twin of the CLI pin in
    tests/test_active_speaker_cli.py."""
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
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
    assert step["ramp"]["pending"]["role"] == "woofer"

    # The all-muted anchor is gone, so the rollback cannot reach it.
    Path(env["staged_path"]).unlink()

    payload = asyncio.run(
        sound_setup._active_speaker_commission_rollback_payload(
            camilla_factory=lambda: controller
        )
    )
    assert payload["rollback"]["status"] == "rollback_failed"
    assert "ramp" not in payload
    # The step is still outstanding, because the graph may still be audible.
    assert load_ramp_state()["pending"]["role"] == "woofer"


def test_confirm_output_identity_audition_can_play_tweeter_before_driver_sequence(
    monkeypatch,
    tmp_path,
):
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)

    load = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "tweeter", "identity_audition": True},
            camilla_factory=lambda: controller,
        )
    )
    assert load["load"]["status"] == "loaded"
    assert load_ramp_state()["confirmed_roles"] == []

    step = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "tweeter", "identity_audition": True},
            camilla_factory=lambda: controller,
        )
    )

    assert step["status"] == "stepped"
    assert step["gate"]["predecessors_required"] == ["woofer"]
    assert step["gate"]["checks"]["role_order_woofer_first"] is True
    assert step["tone_playback"]["audio_emitted"] is True
    assert env["tone_calls"][0]["role"] == "tweeter"
    assert env["tone_calls"][0]["level_dbfs"] == -80.0
    assert step["ramp"]["confirmed_roles"] == []
    assert load_ramp_state()["confirmed_roles"] == []


@pytest.mark.parametrize("role", ["tweeter", "woofer"])
def test_identity_audition_is_granted_while_a_lane_is_still_unconfirmed(
    monkeypatch,
    tmp_path,
    role,
):
    """#2821: the audition IS the household's confirm-output flow. While the
    tweeter is unconfirmed the server grants it for that tweeter and for a
    replay of the already-confirmed woofer alike — both arm and play under the
    weaker mode, which is the shipped Play button's only path through here."""
    from jasper.output_topology import output_topology_mutation

    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    with output_topology_mutation() as mutation:
        mutation.save(_topology(tweeter_verified=False))

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": role, "identity_audition": True},
            camilla_factory=lambda: controller,
        )
    )

    assert payload["load"]["status"] == "loaded"
    identity = payload["preflight"]["startup_preflight"]["identity"]
    assert identity["physical_identity_required"] is False

    step = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": role, "identity_audition": True},
            camilla_factory=lambda: controller,
        )
    )

    assert step["status"] == "stepped"
    assert step["tone_playback"]["audio_emitted"] is True
    assert env["tone_calls"][0]["role"] == role


def test_identity_audition_request_is_refused_once_every_lane_is_confirmed(
    monkeypatch,
    tmp_path,
):
    """#2821: ``identity_audition`` is a client REQUEST, not the decision. With
    every assigned lane already confirmed there is no identity left to audition,
    so a POST carrying the flag is refused the weaker mode and the arm proves
    itself against the full startup-load evidence."""
    from jasper.active_speaker.path_safety import STARTUP_LOAD_EVIDENCE_MODE

    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)

    payload = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer", "identity_audition": True},
            camilla_factory=lambda: controller,
        )
    )

    identity = payload["preflight"]["startup_preflight"]["identity"]
    assert identity["physical_identity_required"] is True
    assert payload["load"]["status"] == "loaded"
    evidence = json.loads((tmp_path / "path_safety.json").read_text(encoding="utf-8"))
    assert evidence["evidence_mode"] == STARTUP_LOAD_EVIDENCE_MODE
    assert evidence["provenance"]["physical_identity_required"] is True


def test_commission_flow_uses_durable_driver_check_after_ramp_reset(
    monkeypatch,
    tmp_path,
):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
    )
    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["status"] == "stepped"
    ack = asyncio.run(
        sound_setup._active_speaker_commission_ramp_ack_payload(
            {"outcome": "heard_correct_driver"}, camilla_factory=lambda: controller
        )
    )
    assert ack["status"] == "confirmed"
    assert ack["measurements"]["summary"]["captured_driver_count"] == 1

    reset_ramp_state()
    assert load_ramp_state()["confirmed_roles"] == []
    state = asyncio.run(
        sound_setup._active_speaker_commission_state_payload(
            camilla_factory=lambda: controller
        )
    )
    assert state["ramp"]["confirmed_roles"] == ["woofer"]

    load_tweeter = asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "tweeter"}, camilla_factory=lambda: controller
        )
    )
    assert load_tweeter["load"]["status"] == "loaded"
    assert load_tweeter["ramp"]["confirmed_roles"] == ["woofer"]

    step_tweeter = asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "tweeter"}, camilla_factory=lambda: controller
        )
    )
    assert step_tweeter["status"] == "stepped"
    assert step_tweeter["gate"]["checks"]["role_order_woofer_first"] is True
    assert step_tweeter["ramp"]["confirmed_roles"] == ["woofer"]


def test_commission_ack_records_backend_acknowledged_step_when_ramp_races(
    monkeypatch,
    tmp_path,
):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
    )

    stale_pending = {
        "speaker_group_id": "mono",
        "confirmed_roles": ["woofer"],
        "pending": {
            "role": "tweeter",
            "playback_id": "old-playback",
            "gain_db": -80.0,
        },
    }

    async def _fake_ack(**kwargs):
        return {
            "status": "confirmed",
            "outcome": "heard_correct_driver",
            "acknowledged_step": {
                "role": "tweeter",
                "playback_id": "new-playback",
                "gain_db": -70.0,
            },
            "issues": [],
            "ramp": {
                "speaker_group_id": "mono",
                "confirmed_roles": ["woofer", "tweeter"],
                "pending": None,
            },
            "safe_playback": {"floor_status": "floor_confirmed"},
            "rollback": {"status": "rolled_back"},
        }

    monkeypatch.setattr(
        "jasper.active_speaker.commission_ramp.load_ramp_state",
        lambda *args, **kwargs: stale_pending,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.commission_ramp.record_ramp_operator_ack",
        _fake_ack,
    )
    monkeypatch.setattr(
        "jasper.active_speaker.safe_playback.load_safe_playback_state",
        lambda *args, **kwargs: {
            "status": "armed",
            "quiet_start": {
                "status": "floor_confirmed",
                "floor_audio_confirmed": True,
                "last_operator_result": {
                    "accepted": True,
                    "outcome": "heard_correct_driver",
                    "playback_id": "new-playback",
                    "target": {
                        "speaker_group_id": "mono",
                        "role": "tweeter",
                        "output_index": 1,
                    },
                },
            },
        },
    )

    ack = asyncio.run(
        sound_setup._active_speaker_commission_ramp_ack_payload(
            {"outcome": "heard_correct_driver"}, camilla_factory=lambda: controller
        )
    )

    latest = ack["measurements"]["summary"]["latest_driver_measurements"][
        "mono:tweeter"
    ]
    assert latest["captured"] is True
    assert latest["playback_id"] == "new-playback"
    assert latest["test_level_dbfs"] == -70.0
    assert latest["playback_id"] != stale_pending["pending"]["playback_id"]


def test_commission_wrong_driver_ack_records_negative_driver_evidence(
    monkeypatch,
    tmp_path,
):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_stop_commission_tone",
        lambda *, reason: {"status": "stopped", "reason": reason},
    )
    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
    assert asyncio.run(
        sound_setup._active_speaker_commission_ramp_step_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )["status"] == "stepped"

    ack = asyncio.run(
        sound_setup._active_speaker_commission_ramp_ack_payload(
            {"outcome": "heard_wrong_driver"}, camilla_factory=lambda: controller
        )
    )

    assert ack["status"] == "aborted"
    latest = ack["measurements"]["summary"]["latest_driver_measurements"][
        "mono:woofer"
    ]
    assert latest["outcome"] == "heard_wrong_driver"
    assert latest["captured"] is False
    assert load_ramp_state()["confirmed_roles"] == []


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


def _summed_test_stubs(monkeypatch, tmp_path) -> dict:
    """Install the common summed-test audio boundary and return its probes."""

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
        "_combined_speech_stimulus_wav_path",
        lambda: (
            wav_path,
            {
                "kind": "jts_active_speaker_speech_stimulus",
                "text": "Like and subscribe to Jasper tech.",
                "duration_s": 12.0,
                "duration_ms": 12000,
                "phrase_repetitions": 4,
            },
        ),
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
    return {
        "controller": controller,
        "env": env,
        "wav_path": wav_path,
        "processes": processes,
        "fanin_actions": fanin_actions,
    }


@pytest.mark.parametrize("lane_armed", [False, True])
def test_summed_test_audio_path_loads_plays_rolls_back_and_records(
    monkeypatch, tmp_path, lane_armed
):
    # Both lane transports (P6c-ii): the payload-equals-spawn assertions
    # below are the armed-state pin for sound_setup's summed payload site —
    # an unarmed-only run is satisfied by an imported-constant regression
    # by construction (reader == old constant there).
    from jasper import renderer_lanes as rl

    lane_map = tmp_path / "renderer_lanes.env"
    monkeypatch.setattr(rl, "RENDERER_LANES_ENV", str(lane_map))
    lane = rl.lane_by_label("correction")
    assert lane is not None
    if lane_armed:
        lane_map.write_text(rl.render_env_text((lane.label,)))
    expected_device = lane.ring_device if lane_armed else lane.aloop_device

    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    env = summed["env"]
    wav_path = summed["wav_path"]
    processes = summed["processes"]
    fanin_actions = summed["fanin_actions"]

    async def _run_confirmed_test():
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
        assert processes, "summed test should start aplay before confirmation"
        stop_payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="operator_confirmed"
        )
        return stop_payload, await task

    stop, payload = asyncio.run(_run_confirmed_test())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert stop["status"] == "stopped"
    assert stop["reason"] == "operator_confirmed"
    assert processes[0].terminated is True
    assert playback["status"] == "completed", json.dumps(
        playback,
        indent=2,
        sort_keys=True,
        default=str,
    )
    assert playback["backend"] == sound_setup.SUMMED_COMMISSION_SPEECH_BACKEND
    assert playback["audio_emitted"] is True
    assert playback["confirmable"] is True
    assert playback["stop_reason"] == "operator_confirmed"
    assert playback["tone"]["level_dbfs"] == -40.0
    assert payload["calibration_level"]["test_signal"][
        "requested_level_dbfs"
    ] == -40.0
    # Payload-equals-spawn (P6c-ii): the payload must report the device the
    # spawn ACTUALLY opened, on whichever transport this box is on.
    assert playback["audio_device"]["pcm"] == processes[0].args[2] == expected_device
    assert playback["commissioning_load"]["load"]["status"] == "loaded"
    assert playback["commissioning_load"]["load"]["target"]["role"] == "summed"
    assert playback["rollback"]["rollback"]["status"] == "rolled_back"
    assert latest["captured"] is True
    assert latest["audio_emitted"] is True
    assert latest["backend"] == sound_setup.SUMMED_COMMISSION_SPEECH_BACKEND
    assert latest["stimulus"]["text"] == "Like and subscribe to Jasper tech."
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
        expected_device,
        "-q",
        str(wav_path),
    ]]
    assert playback["stimulus"]["duration_ms"] == 12000


def test_summed_test_confirm_before_audio_does_not_validate(monkeypatch, tmp_path):
    # _summed_test_stubs already installs an aplay-scoped Popen fake that
    # records into `processes` and passes everything else (e.g. rollback's
    # own camilladsp --check validation, now reached unconditionally on
    # teardown) through to the real subprocess -- which lands on the fake
    # JASPER_CAMILLADSP_BIN stub, not a real binary. A second, unscoped
    # override here would intercept that validation call too and hand it a
    # process object with no context-manager protocol, which is not what
    # this test is pinning.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]

    async def _run_pre_audio_confirm_test():
        load_started = asyncio.Event()
        release_load = asyncio.Event()

        async def _fake_load(**kwargs):
            load_started.set()
            await release_load.wait()
            return {
                "load": {
                    "status": "loaded",
                    "target": {"role": "summed"},
                },
            }

        monkeypatch.setattr(
            sound_setup,
            "_active_speaker_load_summed_commissioning_config",
            _fake_load,
        )
        task = asyncio.create_task(
            sound_setup._active_speaker_summed_test_payload(
                {"speaker_group_id": "mono", "audio": True, "level_dbfs": -40.0},
                camilla_factory=lambda: controller,
            )
        )
        await asyncio.wait_for(load_started.wait(), timeout=1.0)
        stop_payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="operator_confirmed"
        )
        release_load.set()
        return stop_payload, await task

    stop, payload = asyncio.run(_run_pre_audio_confirm_test())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert stop["status"] == "stopping"
    assert stop["reason"] == "operator_confirmed"
    assert processes == []
    assert playback["status"] == "stopped"
    assert playback["audio_emitted"] is False
    assert playback["confirmable"] is False
    assert playback["stop_reason"] == "operator_stop_before_audio"
    assert latest["captured"] is False
    assert latest["audio_emitted"] is False


def _exit_cleanly_after_two_polls(monkeypatch, processes):
    """Make the faked aplay finish a stimulus repeat instead of running forever."""

    previous_popen = sound_setup.subprocess.Popen

    def _fake_popen(args, *popen_args, **kwargs):
        if args and Path(str(args[0])).name == "aplay":
            proc = _FakeToneProcess(list(args), exit_after_polls=2)
            processes.append(proc)
            return proc
        return previous_popen(args, *popen_args, **kwargs)

    monkeypatch.setattr(sound_setup.subprocess, "Popen", _fake_popen)


def test_summed_test_duration_ms_completes_without_a_second_connection(
    monkeypatch, tmp_path
):
    # The headless shape: ONE request, no concurrent stop. The play ends itself
    # once the requested budget elapses and the record is captured, so the
    # validation POST that follows it has something to reference.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    env = summed["env"]
    processes = summed["processes"]
    fanin_actions = summed["fanin_actions"]
    _exit_cleanly_after_two_polls(monkeypatch, processes)

    payload = asyncio.run(
        sound_setup._active_speaker_summed_test_payload(
            {
                "speaker_group_id": "mono",
                "audio": True,
                "level_dbfs": -40.0,
                "duration_ms": 10,
            },
            camilla_factory=lambda: controller,
        )
    )

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert playback["status"] == "completed", json.dumps(
        playback, indent=2, sort_keys=True, default=str
    )
    assert playback["stop_reason"] == "duration_elapsed"
    assert playback["audio_emitted"] is True
    assert playback["confirmable"] is True
    assert playback["tone"]["level_dbfs"] == -40.0
    assert playback["rollback"]["rollback"]["status"] == "rolled_back"
    assert latest["captured"] is True
    assert latest["audio_emitted"] is True
    assert latest["issues"] == []
    # A budget shorter than one stimulus repeat still buys a whole repeat: the
    # completion claim is never made without audio behind it.
    assert len(processes) == 1
    assert processes[0].returncode == 0
    assert processes[0].terminated is False
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    assert fanin_actions == ["select", "release:summed_test"]
    # The lane is free the moment the request returns, so the validation POST
    # that follows is not refused with active_summed_test_running.
    assert sound_setup._active_speaker_summed_validation_active_conflict(
        {"speaker_group_id": "mono"}
    ) is None


def test_summed_test_budget_already_spent_still_plays_one_whole_repeat(
    monkeypatch, tmp_path
):
    # A budget that has already elapsed by the time the loop first looks does
    # NOT let the route claim a completed play with no audio behind it: the
    # first whole stimulus repeat is played and only then does the budget end
    # the loop. Without that condition this records captured/audio_emitted with
    # zero aplay spawns, which is the dishonesty the completion claim invites.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]
    _exit_cleanly_after_two_polls(monkeypatch, processes)

    # Burn more than the 10 ms budget on every stop-reason look, so the loop's
    # first look already finds the budget spent.
    real_stop_reason = sound_setup._summed_test_session_stop_reason
    looks: list[bool] = []

    def _spend_the_budget_before_each_look(session):
        looks.append(True)
        time.sleep(0.05)
        return real_stop_reason(session)

    monkeypatch.setattr(
        sound_setup,
        "_summed_test_session_stop_reason",
        _spend_the_budget_before_each_look,
    )

    payload = asyncio.run(
        sound_setup._active_speaker_summed_test_payload(
            {
                "speaker_group_id": "mono",
                "audio": True,
                "level_dbfs": -40.0,
                "duration_ms": 10,
            },
            camilla_factory=lambda: controller,
        )
    )

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert len(looks) >= 2, "the loop should have looked at least once"
    assert len(processes) == 1
    assert processes[0].returncode == 0
    assert playback["stop_reason"] == "duration_elapsed"
    assert playback["audio_emitted"] is True
    assert latest["captured"] is True


def test_summed_test_confirm_during_a_duration_bounded_play_still_wins(
    monkeypatch, tmp_path
):
    # The browser's shape: it replays the coordinator's start_combined_test
    # action, which carries duration_ms, and presses "Sounds right" mid-play on
    # a second connection. The confirmation is what gets recorded.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]

    async def _run_confirmed_bounded_test():
        task = asyncio.create_task(
            sound_setup._active_speaker_summed_test_payload(
                {
                    "speaker_group_id": "mono",
                    "audio": True,
                    "stimulus": "speech",
                    "duration_ms": 12000,
                    "level_dbfs": -40.0,
                },
                camilla_factory=lambda: controller,
            )
        )
        for _ in range(50):
            if processes:
                break
            await asyncio.sleep(0.01)
        assert processes, "summed test should start aplay before confirmation"
        stop_payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="operator_confirmed"
        )
        return stop_payload, await task

    stop, payload = asyncio.run(_run_confirmed_bounded_test())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert stop["status"] == "stopped"
    assert playback["status"] == "completed"
    assert playback["stop_reason"] == "operator_confirmed"
    assert playback["audio_emitted"] is True
    assert playback["confirmable"] is True
    assert latest["captured"] is True


def test_summed_validation_records_after_a_duration_bounded_test(
    monkeypatch, tmp_path
):
    # The whole sequential ordering end to end, which is what a headless client
    # runs: bounded combined test, then the verdict, on one connection.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    _exit_cleanly_after_two_polls(monkeypatch, summed["processes"])

    test_payload = asyncio.run(
        sound_setup._active_speaker_summed_test_payload(
            {
                "speaker_group_id": "mono",
                "audio": True,
                "level_dbfs": -40.0,
                "duration_ms": 10,
            },
            camilla_factory=lambda: controller,
        )
    )
    summed_test_id = test_payload["measurements"]["summary"][
        "latest_summed_tests"
    ]["mono"]["summed_test_id"]

    validation = sound_setup._active_speaker_summed_validation_payload({
        "speaker_group_id": "mono",
        "summed_test_id": summed_test_id,
        "outcome": "blend_ok",
        "operator_listening_check": True,
    })

    latest = validation["summary"]["latest_summed_validations"]["mono"]
    # No blockers at all: in particular neither summed_validation_test_missing
    # (the test was captured) nor summed_validation_audio_missing. The one
    # remaining issue is the mic-reading warning the operator listening check
    # stands in for.
    assert [
        issue for issue in latest["issues"] if issue["severity"] == "blocker"
    ] == [], json.dumps(latest["issues"], indent=2, sort_keys=True, default=str)
    assert latest["validated"] is True
    assert validation["summary"]["validated_summed_group_count"] == 1


def test_summed_test_duration_ms_absent_still_loops_until_stopped(
    monkeypatch, tmp_path
):
    # The browser's shape is unchanged: no budget means the loop keeps playing
    # repeats until a stop arrives, and a plain stop is still incomplete.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]
    _exit_cleanly_after_two_polls(monkeypatch, processes)

    async def _run_until_several_repeats():
        task = asyncio.create_task(
            sound_setup._active_speaker_summed_test_payload(
                {"speaker_group_id": "mono", "audio": True, "level_dbfs": -40.0},
                camilla_factory=lambda: controller,
            )
        )
        for _ in range(200):
            if len(processes) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(processes) >= 3, "loop should keep repeating without a budget"
        sound_setup._active_speaker_stop_summed_test_tone(reason="operator_stop")
        return await task

    payload = asyncio.run(_run_until_several_repeats())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert playback["status"] == "stopped"
    assert playback["stop_reason"] == "operator_stop"
    assert playback["audio_emitted"] is False
    assert latest["captured"] is False
    assert "summed_test_playback_incomplete" in {
        issue["code"] for issue in latest["issues"]
    }


def test_summed_test_stop_before_the_budget_elapses_stays_incomplete(
    monkeypatch, tmp_path
):
    # A budget is not a promise of completion: stopping early still records an
    # incomplete test, so "captured" cannot be bought by asking for a duration.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]

    async def _run_and_stop_mid_play():
        task = asyncio.create_task(
            sound_setup._active_speaker_summed_test_payload(
                {
                    "speaker_group_id": "mono",
                    "audio": True,
                    "level_dbfs": -40.0,
                    "duration_ms": 600_000,
                },
                camilla_factory=lambda: controller,
            )
        )
        for _ in range(50):
            if processes:
                break
            await asyncio.sleep(0.01)
        assert processes, "summed test should start aplay before stop"
        sound_setup._active_speaker_stop_summed_test_tone(reason="operator_stop")
        return await task

    payload = asyncio.run(_run_and_stop_mid_play())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert playback["status"] == "stopped"
    assert playback["stop_reason"] == "operator_stop"
    assert playback["audio_emitted"] is False
    assert playback["confirmable"] is False
    assert latest["captured"] is False
    assert "summed_test_playback_incomplete" in {
        issue["code"] for issue in latest["issues"]
    }


def test_summed_test_stop_cannot_borrow_the_duration_elapsed_reason(
    monkeypatch, tmp_path
):
    # `duration_elapsed` is the loop's own end reason. A client that posts it to
    # /summed-test/stop gets the string recorded and nothing else: completion is
    # passed in code by the loop, never read back off a client-supplied reason.
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    processes = summed["processes"]

    async def _run_and_stop_with_the_machine_reason():
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
        sound_setup._active_speaker_stop_summed_test_tone(
            reason=sound_setup.SUMMED_TEST_DURATION_ELAPSED_REASON
        )
        return await task

    payload = asyncio.run(_run_and_stop_with_the_machine_reason())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert playback["stop_reason"] == "duration_elapsed"
    assert playback["status"] == "stopped"
    assert playback["audio_emitted"] is False
    assert latest["captured"] is False


def test_summed_test_stop_terminates_aplay_and_rolls_back(monkeypatch, tmp_path):
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    env = summed["env"]
    processes = summed["processes"]
    fanin_actions = summed["fanin_actions"]

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


def test_summed_test_watchdog_stops_abandoned_loop(monkeypatch, tmp_path):
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(sound_setup, "SUMMED_TEST_MAX_LOOP_SECONDS", 0.04)
    controller = summed["controller"]
    env = summed["env"]
    processes = summed["processes"]
    fanin_actions = summed["fanin_actions"]

    payload = asyncio.run(
        sound_setup._active_speaker_summed_test_payload(
            {"speaker_group_id": "mono", "audio": True, "level_dbfs": -40.0},
            camilla_factory=lambda: controller,
        )
    )

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert processes, "summed test should start aplay before watchdog timeout"
    assert processes[0].terminated is True
    assert playback["status"] == "stopped"
    assert playback["audio_emitted"] is False
    assert playback["confirmable"] is False
    assert playback["stop_reason"] == "watchdog_timeout"
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


def test_summed_test_level_update_reloads_active_loop(monkeypatch, tmp_path):
    summed = _summed_test_stubs(monkeypatch, tmp_path)
    controller = summed["controller"]
    env = summed["env"]
    processes = summed["processes"]
    fanin_actions = summed["fanin_actions"]

    async def _run_level_update_test():
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
        assert processes, "summed test should be playing before level update"
        level_payload = await sound_setup._active_speaker_summed_test_level_payload(
            {"speaker_group_id": "mono", "level_dbfs": -25.0},
            camilla_factory=lambda: controller,
        )
        stop_payload = sound_setup._active_speaker_stop_summed_test_tone(
            reason="operator_confirmed"
        )
        return level_payload, stop_payload, await task

    level_payload, stop, payload = asyncio.run(_run_level_update_test())

    playback = payload["playback"]
    latest = payload["measurements"]["summary"]["latest_summed_tests"]["mono"]
    assert level_payload["status"] == "loaded"
    assert level_payload["calibration_level"]["test_signal"][
        "requested_level_dbfs"
    ] == -25.0
    assert level_payload["commissioning_load"]["load"]["output_reconcile"] == {
        "status": "skipped",
        "reason": "same_active_output_lane",
        "unit": startup_load_mod.AUDIO_HARDWARE_RECONCILE_UNIT,
    }
    assert stop["status"] == "stopped"
    assert playback["status"] == "completed"
    assert playback["tone"]["level_dbfs"] == -25.0
    assert payload["calibration_level"]["test_signal"][
        "requested_level_dbfs"
    ] == -25.0
    assert latest["captured"] is True
    assert latest["tone"]["level_dbfs"] == -25.0
    assert len(controller.applied_texts) == 3
    assert controller.applied_texts[-1] == Path(env["staged_path"]).read_text(
        encoding="utf-8"
    )
    assert fanin_actions == ["select", "release:summed_test"]


def test_summed_test_failed_level_update_keeps_active_session_metadata(
    monkeypatch,
):
    initial_load = {"load": {"status": "loaded", "marker": "initial"}}
    session = {
        "speaker_group_id": "mono",
        "playback_id": "play-1",
        "level_dbfs": -40.0,
        "load_payload": initial_load,
    }
    monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", session)
    monkeypatch.setattr(sound_setup, "load_output_topology", lambda path=None: _topology())
    monkeypatch.setattr(
        sound_setup,
        "resolve_commission_inputs",
        lambda preset=None: (_tone_preset(), None),
    )
    load_kwargs: dict[str, object] = {}

    async def _fake_failed_load(**kwargs):
        load_kwargs.update(kwargs)
        return {
            "load": {
                "status": "failed",
                "issues": [{
                    "severity": "blocker",
                    "code": "test_reload_failed",
                    "message": "reload failed",
                }],
            },
        }

    monkeypatch.setattr(
        sound_setup,
        "_active_speaker_load_summed_commissioning_config",
        _fake_failed_load,
    )
    try:
        payload = asyncio.run(
            sound_setup._active_speaker_summed_test_level_payload(
                {"speaker_group_id": "mono", "level_dbfs": -25.0},
                camilla_factory=lambda: _FakeController("placeholder"),
            )
        )
    finally:
        monkeypatch.setattr(sound_setup, "_SUMMED_TEST_TONE_SESSION", None)

    assert payload["status"] == "failed"
    assert payload["calibration_level"]["test_signal"][
        "requested_level_dbfs"
    ] == -25.0
    assert load_kwargs["level_dbfs"] == -25.0
    assert session["level_dbfs"] == -40.0
    assert session["load_payload"] is initial_load


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
    from jasper.output_topology import (
        load_output_topology_strict,
        output_topology_mutation,
        set_channel_protection_status,
    )

    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)

    # Drift the tweeter to required_missing (the live jts3 state).
    drifted = set_channel_protection_status(
        _topology(),
        speaker_group_id="mono",
        role="tweeter",
        protection_status="required_missing",
    )
    with output_topology_mutation() as mutation:
        mutation.save(drifted)

    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )

    persisted = load_output_topology_strict()
    tweeter = next(
        channel
        for group in persisted.speaker_groups
        for channel in group.channels
        if channel.role == "tweeter"
    )
    assert tweeter.protection_status == "software_guard_requested"
