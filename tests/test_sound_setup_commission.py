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
from pathlib import Path

import yaml

import jasper.web.sound_setup as sound_setup
from jasper.active_speaker import load_commission_load_state, load_ramp_state

from tests.test_active_speaker_cli import _FakeController
from tests.test_active_speaker_startup_load import _staged, _topology


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
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply.json"))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE", str(tmp_path / "ramp.json")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(tmp_path / "safe.json")
    )
    return {"staged": staged, "staged_path": staged_path}


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
    payload = sound_setup._active_speaker_commission_state_payload()
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


def test_commission_ramp_step_and_ack_payloads(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    _web_commission_env(monkeypatch, tmp_path, controller)
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
    assert step["safe_playback"]["floor_status"] == "floor_pending_operator"
    # The running graph now carries the woofer un-muted at the audible floor.
    assert yaml.safe_load(controller.running_raw)["filters"]["as_out0_commission_mute"][
        "parameters"
    ]["mute"] is False

    ack = asyncio.run(
        sound_setup._active_speaker_commission_ramp_ack_payload(
            {"outcome": "heard_correct_driver"}, camilla_factory=lambda: controller
        )
    )
    assert ack["status"] == "confirmed"
    assert load_ramp_state()["confirmed_roles"] == ["woofer"]


def test_commission_ramp_abort_payload_remutes(monkeypatch, tmp_path):
    controller = _FakeController("placeholder")
    env = _web_commission_env(monkeypatch, tmp_path, controller)
    asyncio.run(
        sound_setup._active_speaker_commission_load_payload(
            {"group": "mono", "role": "woofer"}, camilla_factory=lambda: controller
        )
    )
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
