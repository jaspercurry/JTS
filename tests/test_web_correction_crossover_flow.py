# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Secure correction crossover measurement flow."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker import web_measurement
from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow


def test_request_payload_parses_capture_query():
    handler = SimpleNamespace(
        path=(
            "/crossover/driver-capture?speaker_group_id=mono&role=woofer"
            "&playback_id=abc&test_level_dbfs=-42.5"
            "&has_mic_calibration=true&expect_null=0"
        )
    )

    payload = flow._request_payload(handler)

    assert payload == {
        "speaker_group_id": "mono",
        "role": "woofer",
        "playback_id": "abc",
        "test_level_dbfs": -42.5,
        "has_mic_calibration": True,
        "expect_null": False,
    }


def test_driver_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    calls = {}
    topology = object()
    preset = object()
    wav_path = tmp_path / "driver.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    monkeypatch.setattr(web_measurement, "capture_preset", lambda t: preset)
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {"sample_rate": 48000, "n_samples": 1},
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: ("curve", "cal-1", {"mode": "phase_aware"}),
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.safe_playback as safe_playback

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )
    monkeypatch.setattr(
        safe_playback,
        "load_safe_playback_state",
        lambda: {"status": "armed"},
    )

    def fake_record(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"recorded": True}

    monkeypatch.setattr(capture, "record_driver_acoustic_capture", fake_record)

    payload = backend.record_driver_capture(
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "playback_id": "play-1",
            "has_mic_calibration": True,
        },
        b"wav",
    )

    assert payload["recorded"] is True
    assert payload["calibration_id"] == "cal-1"
    assert payload["measurement_mode"] == {"mode": "phase_aware"}
    assert calls["args"] == (topology, preset)
    assert calls["kwargs"]["speaker_group_id"] == "mono"
    assert calls["kwargs"]["role"] == "woofer"
    assert calls["kwargs"]["captured_wav"] == wav_path
    assert calls["kwargs"]["playback_id"] == "play-1"
    assert calls["kwargs"]["calibration"] == "curve"


def test_summed_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    calls = {}
    topology = object()
    preset = object()
    wav_path = tmp_path / "summed.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    monkeypatch.setattr(web_measurement, "capture_preset", lambda t: preset)
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {"sample_rate": 48000, "n_samples": 1},
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: (None, None, {"mode": "magnitude_only"}),
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_capture as capture

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )

    def fake_record(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"recorded": True, "verdict": "blend_ok"}

    monkeypatch.setattr(capture, "record_summed_acoustic_capture", fake_record)

    payload = backend.record_summed_capture(
        {
            "speaker_group_id": "mono",
            "summed_test_id": "sum-1",
            "playback_id": "sum-1",
            "expect_null": True,
        },
        b"wav",
    )

    assert payload["recorded"] is True
    assert payload["measurement_mode"] == {"mode": "magnitude_only"}
    assert calls["args"] == (topology, preset)
    assert calls["kwargs"]["speaker_group_id"] == "mono"
    assert calls["kwargs"]["captured_wav"] == wav_path
    assert calls["kwargs"]["summed_test_id"] == "sum-1"
    assert calls["kwargs"]["expect_null"] is True


def test_backend_status_includes_active_speaker_commission_state(monkeypatch):
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {"ok": True, "targets": {"drivers": [], "summed": []}, "measurements": {}},
    )
    from jasper.active_speaker import web_commissioning

    monkeypatch.setattr(
        web_commissioning,
        "commission_status_payload",
        lambda: {"ramp": {"pending": None}},
    )

    payload = backend.status_payload()

    assert payload["commission"] == {"ramp": {"pending": None}}


def test_crossover_module_is_a_thin_server_envelope_renderer():
    source = Path("deploy/assets/correction/js/crossover/main.js").read_text(
        encoding="utf-8",
    )

    assert "fetchJSON('/correction/crossover/envelope')" in source
    assert "fetchJSON('status')" not in source
    assert "postJSON" in source
    assert "action.endpoint" in source
    assert "getUserMedia" not in source
    assert "createMonoRecorder" not in source
    assert "driver-test" not in source
    assert "setInterval" not in source
    assert "refreshInFlight" in source
    assert "refreshQueued" in source
    assert "renderEpoch" in source
    assert "visibilitychange" in source
    assert "schedulePoll(relayActive ? POLL_MS : null)" in source
    assert "renderActions(null);" in source
    assert "env.alternate_actions" in source


# --- passive-gating: Layer A hidden for a full-range passive speaker ----------


def _status_with_targets(monkeypatch, *, drivers, summed):
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {
            "ok": True,
            "targets": {"drivers": drivers, "summed": summed},
            "measurements": {"summary": {}},
        },
    )
    from jasper.active_speaker import web_commissioning

    monkeypatch.setattr(
        web_commissioning,
        "commission_status_payload",
        lambda: {"ramp": {"pending": None}},
    )


def test_status_active_flag_true_for_active_speaker(monkeypatch):
    # An active speaker has driver/summed targets → active=True (Layer A shown).
    _status_with_targets(
        monkeypatch,
        drivers=[{"target_id": "mono:woofer"}],
        summed=[{"speaker_group_id": "mono"}],
    )
    payload = backend.status_payload()
    assert payload["active"] is True


def test_status_active_flag_false_for_passive_speaker(monkeypatch):
    # A full_range_passive speaker has NO active targets → active=False. This is
    # the honest gate the frontend reads so passive users never see Layer A
    # (revision plan §1). Pins the conditional-pipeline rule.
    _status_with_targets(monkeypatch, drivers=[], summed=[])
    payload = backend.status_payload()
    assert payload["active"] is False


# --- crossover screen envelope: exactly one sequential next action -----------


def _envelope_status() -> dict:
    return {
        "active": True,
        "setup": {
            "active": True,
            "status": "ready",
            "acoustic_commissioning": {"allowed": False},
            "baseline_profile": {
                "source_fingerprint": "source-1",
                "revalidation": {"required": False},
            },
            "applied_crossover": {
                "valid": False,
                "owner": None,
                "reason": "active_crossover_profile_not_applied",
            },
            "manual_preservation": {
                "ready": False,
                "reason": "manual_crossover_not_legacy_applied",
            },
            "automatic_candidate": {
                "ready": False,
                "reason": "automatic_crossover_measurements_incomplete",
            },
        },
        "targets": {
            "drivers": [
                {"speaker_group_id": "mono", "role": "woofer"},
                {"speaker_group_id": "mono", "role": "tweeter"},
            ],
            "summed": [{"speaker_group_id": "mono"}],
        },
        "measurements": {"summary": {}},
        "level_match": {"running": False, "last": None},
        "applied_profile": {},
        "relay": None,
    }


def _locked_level(status: dict) -> None:
    status["level_match"] = {
        "running": False,
        # The target remains reusable after the safe lifecycle restores normal
        # listening volume between sweep windows.
        "last": {"ramp": {"state": "locked", "restored": True}},
    }


def _driver_acoustic() -> dict:
    return {"acoustic": {"verdict": "present"}}


def _summed_acoustic() -> dict:
    return {"validated": True, "acoustic": {"verdict": "blend_ok"}}


def test_crossover_envelope_passive_speaker_is_gated():
    # Passive speaker: envelope carries active=False, no steps, one explanatory
    # verdict, no next action — the frontend renders nothing of Layer A.
    from jasper.active_speaker import crossover_envelope

    env = crossover_envelope.build_crossover_envelope(
        {"active": False, "targets": {"drivers": [], "summed": []}}
    )
    assert env["active"] is False
    assert env["screen"] == "not_applicable"
    assert env["steps"] == []
    assert env["next_action"] == {
        "id": "room",
        "label": "Correct the room",
        "href": "/correction/room/",
    }
    assert env["nudges"] == []
    assert "crossover" in env["verdict_text"].lower()
    assert env["schema_version"] == 2


def test_crossover_envelope_requires_protected_setup_first():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["href"] == "/sound/"
    assert env["next_action"]["id"] == "speaker_setup"


def test_crossover_apply_requires_explicit_owner(monkeypatch):
    from jasper.web import correction_crossover_backend as backend

    seen = {}

    async def fake_apply_profile(*, tuning_owner, camilla_factory):
        seen["owner"] = tuning_owner
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(backend, "apply_profile", fake_apply_profile)

    def run_async(awaitable, *, timeout):
        import asyncio

        assert timeout == 30.0
        return asyncio.run(awaitable)

    refused, refused_status = flow.handle_apply({}, run_async, lambda: object())
    assert refused_status == 400
    assert refused["status"] == "refused"

    payload, status = flow.handle_apply(
        {"tuning_owner": "automatic"}, run_async, lambda: object()
    )
    assert status == 200
    assert payload["status"] == "applied"
    assert seen["owner"] == "automatic"


def test_crossover_envelope_walks_level_drivers_summed_apply_room():
    from jasper.active_speaker import crossover_envelope
    status = _envelope_status()
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"

    _locked_level(status)
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "driver"
    assert env["next_action"]["body"] == {
        "kind": "driver",
        "speaker_group_id": "mono",
        "role": "woofer",
    }

    summary = status["measurements"]["summary"]
    summary["latest_driver_measurements"] = {"mono:woofer": _driver_acoustic()}
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["next_action"]["body"]["role"] == "tweeter"

    summary["latest_driver_measurements"]["mono:tweeter"] = _driver_acoustic()
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "summed"
    assert env["next_action"]["body"] == {
        "kind": "summed",
        "speaker_group_id": "mono",
    }

    summary["latest_summed_validations"] = {"mono": _summed_acoustic()}
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "apply"
    assert env["next_action"]["endpoint"] == "/correction/crossover/apply"

    status["applied_profile"] = {
        "status": "applied",
        "provisional": False,
        "level_match": {"groups_measured": 1},
        "source": {"fingerprint": "source-1"},
        "tuning_owner": "automatic",
        "recomposition_snapshot": {
            "schema_version": 1,
            "tuning_owner": "automatic",
        },
    }
    status["setup"]["acoustic_commissioning"] = {"allowed": True}
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "done"
    assert env["next_action"]["href"] == "/correction/room/"
    assert env["progress"] == {"position": 5, "total": 5}


def test_crossover_envelope_uses_applied_anchor_while_candidate_is_incomplete():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"].update({
        "status": "ready",
        "baseline_profile": {
            "status": "blocked",
            "revalidation": {"required": True},
        },
        "protected_profile": {"status": "ready"},
    })
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic(),
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"]["body"]["role"] == "tweeter"


def test_crossover_envelope_legacy_applied_profile_requires_explicit_reapply():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["applied_profile"] = {
        "status": "applied",
        "provisional": False,
        "level_match": {"groups_measured": 1},
        # Profiles applied before immutable graph snapshots shipped have no
        # recomposition_snapshot. They are safe anchors, but cannot authorize
        # Room until the explicit apply transaction migrates them.
    }
    status["setup"]["manual_preservation"] = {
        "ready": True,
        "reason": None,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "choose_tuning"
    assert env["next_action"] == {
        "id": "keep_manual",
        "label": "Keep current manual crossover",
        "endpoint": "/correction/crossover/apply",
        "body": {"tuning_owner": "manual"},
    }
    assert [action["id"] for action in env["alternate_actions"]] == [
        "tune_automatic",
        "edit_manual",
    ]
    assert "current manual crossover is safe" in env["verdict_text"]


def test_changed_legacy_source_removes_keep_manual_action():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["applied_profile"] = {"status": "applied"}
    status["setup"]["manual_preservation"] = {
        "ready": False,
        "reason": "manual_crossover_source_changed",
        "detail": (
            "The saved crossover inputs changed after this manual crossover was applied. "
            "Edit and apply the manual crossover again, or tune automatically."
        ),
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["next_action"]["id"] == "edit_manual"
    assert [action["id"] for action in env["alternate_actions"]] == [
        "tune_automatic"
    ]
    assert "changed" in env["verdict_text"]


def test_crossover_envelope_manual_profile_offers_room_edit_or_automatic():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["acoustic_commissioning"] = {"allowed": True}
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "manual",
        "recomposition_snapshot": {
            "schema_version": 1,
            "tuning_owner": "manual",
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "done_manual"
    assert env["next_action"]["href"] == "/correction/room/"
    assert [action["id"] for action in env["alternate_actions"]] == [
        "tune_automatic",
        "edit_manual",
    ]


def test_completed_automatic_measurement_explicitly_replaces_manual_profile():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["acoustic_commissioning"] = {"allowed": True}
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "manual",
        "recomposition_snapshot": {
            "schema_version": 1,
            "tuning_owner": "manual",
        },
    }
    _locked_level(status)
    status["measurements"]["summary"].update({
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic(),
            "mono:tweeter": _driver_acoustic(),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
    })
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "apply"
    assert env["next_action"] == {
        "id": "replace_manual",
        "label": "Replace manual crossover with automatic tuning",
        "endpoint": "/correction/crossover/apply",
        "body": {"tuning_owner": "automatic"},
    }


def test_incomparable_automatic_evidence_never_offers_apply():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"] = {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic(),
            "mono:tweeter": _driver_acoustic(),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
    }
    status["setup"]["automatic_candidate"] = {
        "ready": False,
        "reason": "automatic_crossover_excitation_incomparable",
        "detail": (
            "Repeat the driver sweeps so their verified excitation can be compared."
        ),
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert env["next_action"].get("endpoint") != "/correction/crossover/apply"


def test_applied_automatic_snapshot_is_done_without_mutable_measurements():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "automatic",
        "recomposition_snapshot": {"schema_version": 1},
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "done"
    assert env["next_action"]["href"] == "/correction/room/"


def test_crossover_envelope_maxed_out_is_retry_not_a_lock():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"] = {
        "running": False,
        "last": {"ramp": {"state": "maxed_out", "restored": True}},
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert env["nudges"][0]["code"] == "external_amplifier_too_low"


def test_crossover_envelope_surfaces_bounded_low_lock_without_blocking_sweeps():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"] = {
        "running": False,
        "valid": True,
        "last": {
            "ramp": {
                "state": "locked",
                "lock_kind": "bounded_low_level",
                "window_shortfall_db": 13.07,
            }
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"]["id"] == "measure_driver"
    assert env["nudges"] == [{
        "code": "bounded_low_measurement_level",
        "severity": "warn",
        "text": (
            "The microphone level is stable and safe but lower than preferred "
            "(13.1 dB below the preferred window). JTS will verify each sweep "
            "before using it."
        ),
    }]


# --- phone-mic relay transport (P7) -------------------------------------------


def test_relay_kind_validation():
    assert flow.relay_kind_from_raw({"kind": "driver"}) == "driver"
    assert flow.relay_kind_from_raw({"kind": "summed"}) == "summed"
    with pytest.raises(ValueError, match="crossover relay kind"):
        flow.relay_kind_from_raw({"kind": "bogus"})
    with pytest.raises(ValueError, match="crossover relay kind"):
        flow.relay_kind_from_raw({})


def test_relay_driver_label_names_the_driver():
    # Server-driven capture-page copy comes from the Pi (no web deploy).
    assert flow.relay_driver_label({"role": "woofer"}) == "Woofer driver"
    assert flow.relay_driver_label({"role": ""}) == "summed crossover"
    assert flow.relay_driver_label({}) == "summed crossover"


def _run_coro(coro):
    """Run a coroutine on a fresh loop — stands in for correction_setup's
    `_run_async` (run_coroutine_threadsafe onto the correction loop). Works
    because the consume path calls it from run_capture's worker thread."""
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_relay_transport(monkeypatch, *, wav=b"phone-wav-bytes"):
    """Fake ONLY the relay transport boundary: run_capture arms then returns a
    CaptureResult-shaped object; purge is recorded. The play/record path stays
    real."""
    from jasper.capture_relay import session as relay_session

    purged = {}

    def fake_run_capture(client, pi_session, *, on_armed, **kw):
        on_armed(SimpleNamespace())  # phone armed → Pi plays the sweep
        return SimpleNamespace(wav=wav, device={"label": "iPhone mic"})

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(
        relay_session, "purge", lambda c, s: purged.setdefault("done", True)
    )
    return purged


def _real_play_boundary(monkeypatch, tmp_path, *, kind):
    """Boundary mocks that let the REAL play_driver/summed_capture_sweep run
    hardware-free: state loaders return real-shaped states, CamillaDSP
    config-load/rollback + fan-in lane + aplay are stubbed at their seams, and
    the sweep WAV (hence the REAL sweep_meta) is generated for real into an
    env-pointed cache dir. Mirrors tests/test_active_speaker_web_commissioning.py."""
    import jasper.correction.playback as correction_playback
    from jasper.active_speaker import web_commissioning as web

    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SWEEP_DIR", str(tmp_path / "sweeps"))
    if kind == "driver":
        measurements = {
            "summary": {
                "latest_driver_measurements": {
                    "mono:woofer": {
                        "captured": True,
                        "playback_id": "play-woofer",
                        "test_level_dbfs": -72.0,
                        "issues": [],
                    },
                },
            },
        }
    else:
        measurements = {
            "summary": {
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": True,
                        "summed_test_id": "sum-9",
                        "tone": {"level_dbfs": -80.8},
                        "issues": [],
                    },
                },
            },
        }
    monkeypatch.setattr(web, "load_output_topology", lambda: object())
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(web, "resolve_commission_inputs", lambda: (object(), None))
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    if kind == "driver":
        monkeypatch.setattr(
            web,
            "automatic_driver_excitation",
            lambda _topology, role: {
                "status": "ready",
                "schema_version": 1,
                "scope": "sweep_plus_role_varying_commission_gain",
                "sweep_peak_dbfs": -12.0,
                "commissioning_gain_db": -9.0,
                "effective_peak_dbfs": -21.0,
                "gain_source": web.AUTOMATIC_EXCITATION_GAIN_SOURCE,
                "baseline_id": "baseline-1",
                "topology_id": "topology-1",
                "role": role,
            },
        )

    async def _loaded(**kwargs):
        return {"load": {"status": "loaded"}}

    async def _rolled_back(*args, **kwargs):
        return {"status": "rolled_back"}

    async def _loaded_applied_summed(**kwargs):
        return {
            "load": {
                "status": "loaded",
                "previous_config_path": "/tmp/previous.yml",
            },
            "excitation": {
                "status": "ready",
                "schema_version": 1,
                "scope": "sweep_plus_applied_full_layer_a_graph",
                "sweep_peak_dbfs": -12.0,
                "gain_source": web.AUTOMATIC_EXCITATION_GAIN_SOURCE,
                "baseline_id": "baseline-1",
                "topology_id": "topology-1",
                "corrections": {
                    "woofer": {
                        "gain_db": -9.0,
                        "delay_ms": 0.25,
                        "inverted": False,
                        "effective_peak_dbfs": -21.0,
                    },
                    "tweeter": {
                        "gain_db": -3.0,
                        "delay_ms": 0.0,
                        "inverted": True,
                        "effective_peak_dbfs": -15.0,
                    },
                },
            },
        }

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", _loaded)
    monkeypatch.setattr(web, "_load_summed_commissioning_config", _loaded)
    monkeypatch.setattr(
        web,
        "_load_applied_summed_measurement_config",
        _loaded_applied_summed,
    )
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", _rolled_back)
    monkeypatch.setattr(
        web,
        "_rollback_applied_summed_measurement_config",
        _rolled_back,
    )
    monkeypatch.setattr(
        web, "_commission_tone_select_fanin_lane", lambda: {"status": "ok"}
    )
    monkeypatch.setattr(
        web,
        "_commission_tone_release_fanin_lane",
        lambda *, reason: {"status": "ok", "reason": reason},
    )

    async def _fake_play_sweep(wav_path, *, alsa_device, timeout_s):
        return None

    monkeypatch.setattr(correction_playback, "play_sweep", _fake_play_sweep)


@pytest.mark.asyncio
async def test_crossover_relay_consume_feeds_real_driver_play_payload(
    monkeypatch, tmp_path
):
    # THE Blocker-1 regression pin, real-shape edition: the consume path runs
    # the REAL backend.play_driver_capture_sweep (real web_commissioning
    # wrapper, real _play_capture_sweep, REAL generated sweep_meta) — only the
    # I/O seams (state files, CamillaDSP load/rollback, fan-in, aplay, relay
    # transport) are stubbed. The real payload nests audio_emitted under
    # `playback` and hoists test_level_dbfs/sweep_meta/playback_id to the top —
    # the flat fake shape the old test used made the drifted guard invisible.
    from jasper.web import correction_crossover_backend as be

    _real_play_boundary(monkeypatch, tmp_path, kind="driver")
    purged = _fake_relay_transport(monkeypatch)

    record_calls = {}

    def fake_record_driver(raw, wav_bytes):
        record_calls["raw"] = raw
        record_calls["wav"] = wav_bytes
        return {"recorded": True}

    monkeypatch.setattr(be, "record_driver_capture", fake_record_driver)

    host_events = []

    def post_host_event(session_id, pull_token, payload):
        host_events.append(payload.get("phase"))

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=post_host_event,
        blocking_phase=lambda: None,
    )
    await run_and_consume(
        object(), SimpleNamespace(session_id="sid", pull_token="ptok")
    )

    # The REAL sweep_meta (generated by _measurement_sweep_wav_path from
    # driver_acoustics defaults) rode into the record call — the deconv basis
    # is the played sweep, never the phone WAV.
    raw = record_calls["raw"]
    assert record_calls["wav"] == b"phone-wav-bytes"
    assert raw["role"] == "woofer"
    assert raw["playback_id"]
    # The old by-ear -72 dB floor record is identity evidence only. The played
    # automatic sweep uses the protected applied role gain.
    assert raw["test_level_dbfs"] == -9.0
    assert raw["excitation"] == {
        "schema_version": 1,
        "scope": "sweep_plus_role_varying_commission_gain",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -9.0,
        "effective_peak_dbfs": -21.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": "topology-1",
        "role": "woofer",
    }
    meta = raw["sweep_meta"]
    assert meta["sample_rate"] == 48000
    assert meta["duration_s"] > 0  # real synchronized-sine meta, not a stub
    assert {"f1", "f2", "n_samples", "amplitude_dbfs"} <= set(meta)
    assert meta["amplitude_dbfs"] == -12.0
    assert purged["done"] is True
    assert host_events == ["sweep_started", "sweep_complete"]


@pytest.mark.asyncio
async def test_crossover_relay_consume_feeds_real_summed_play_payload(
    monkeypatch, tmp_path
):
    # Summed twin of the real-shape test: the REAL play_summed_capture_sweep
    # hoists summed_test_id/test_level_dbfs/sweep_meta to the top level; the
    # consume path must read them there.
    from jasper.web import correction_crossover_backend as be

    _real_play_boundary(monkeypatch, tmp_path, kind="summed")
    _fake_relay_transport(monkeypatch, wav=b"w")

    record_calls = {}
    monkeypatch.setattr(
        be,
        "record_summed_capture",
        lambda raw, wav: record_calls.setdefault("raw", raw) or {"recorded": True},
    )

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "summed", "speaker_group_id": "mono"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        blocking_phase=lambda: None,
    )
    await run_and_consume(object(), SimpleNamespace(session_id="s", pull_token="t"))

    raw = record_calls["raw"]
    assert raw["summed_test_id"] == "sum-9"
    assert raw["sweep_meta"]["sample_rate"] == 48000
    assert raw["excitation"]["scope"] == (
        "sweep_plus_applied_full_layer_a_graph"
    )
    assert raw["excitation"]["sweep_peak_dbfs"] == -12.0
    assert raw["excitation"]["corrections"]["tweeter"]["inverted"] is True
    assert "role" not in raw


@pytest.mark.asyncio
async def test_crossover_gain_is_scoped_to_the_measurement_window(monkeypatch):
    """Normal renderers must never resume while measurement gain is asserted."""
    from contextlib import asynccontextmanager

    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    order = []

    @asynccontextmanager
    async def window():
        order.append("window_enter")
        try:
            yield
        finally:
            order.append("window_exit")

    async def prepare():
        order.append("prepare")
        return True

    async def restore():
        order.append("restore")
        return True

    async def play(*_args, **_kwargs):
        order.append("play")
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "playback_id": "play-1",
            "test_level_dbfs": -72.0,
            "sweep_meta": {"sample_rate": 48000},
        }

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda _raw, _wav: {"recorded": True},
    )

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        prepare_play=prepare,
        restore_play=restore,
    )
    await run_and_consume(object(), SimpleNamespace(session_id="s", pull_token="t"))

    assert order == ["window_enter", "prepare", "play", "restore", "window_exit"]


@pytest.mark.asyncio
async def test_crossover_relay_consume_refusal_carries_real_reason(monkeypatch):
    # Armed-time mutual exclusion, against the REAL play function with ZERO
    # play-path mocks: blocking_phase is evaluated fresh when the phone arms,
    # the real play_driver_capture_sweep refuses with its real payload, and the
    # raised error carries that payload's own next_step text (not a generic
    # "did not play"). sweep_failed reaches the phone.
    _fake_relay_transport(monkeypatch)

    host_events = []

    def post_host_event(session_id, pull_token, payload):
        host_events.append(payload)

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=post_host_event,
        # A room sweep started between POST and armed:
        blocking_phase=lambda: "correction:sweeping",
    )
    with pytest.raises(ValueError, match="Finish the other measurement"):
        await run_and_consume(
            object(), SimpleNamespace(session_id="s", pull_token="t")
        )
    phases = [p.get("phase") for p in host_events]
    assert phases == ["sweep_started", "sweep_failed"]
    assert "Finish the other measurement" in host_events[1]["error"]


def test_capture_sweep_played_reads_the_nested_playback_shape():
    # Shape unit pins for the guard + issue-text readers, mirroring the JS
    # assertCapturePlayback/issueMessage contract exactly.
    real_success = {
        "status": "completed",
        "playback": {"audio_emitted": True, "issues": []},
        "playback_id": "p1",
        "test_level_dbfs": -72.0,
    }
    assert flow.capture_sweep_played(real_success) is True
    # The pre-fix flat shape the backend never returns must NOT satisfy it.
    assert flow.capture_sweep_played(
        {"status": "completed", "audio_emitted": True}
    ) is False
    # Refused/blocked payloads (top-level audio_emitted False) fail it.
    assert flow.capture_sweep_played(
        {"status": "refused", "audio_emitted": False}
    ) is False
    # Issue text prefers issues[].message, then nested playback issues, then
    # next_step/reason — a rollback failure is not reported as "did not play".
    assert flow.playback_issue_text(
        {
            "status": "failed",
            "playback": {
                "issues": [{
                    "code": "capture_sweep_rollback_failed",
                    "message": "measurement sweep played, but JTS could not re-mute",
                }],
            },
        },
        "fallback",
    ).startswith("measurement sweep played")
    assert flow.playback_issue_text(
        {"status": "refused", "next_step": "Finish the other measurement."},
        "fallback",
    ) == "Finish the other measurement."
    assert flow.playback_issue_text({}, "fallback") == "fallback"


def test_crossover_relay_endpoint_refuses_while_other_measurement_active(
    monkeypatch,
):
    # POST-time mutual exclusion is SERVER-computed (mirrors sync's
    # relay_precheck): while room/balance/sync is active the endpoint refuses
    # before any relay registration or slot claim — and the client cannot
    # override it (blocking_phase is not read from the body).
    from jasper.web import correction_setup

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.jasper.tech")
    monkeypatch.setattr(
        correction_setup, "_crossover_blocking_phase", lambda: "balance:ramping"
    )
    correction_setup._set_relay_capture(None)
    with pytest.raises(ValueError, match="another measurement is in progress"):
        correction_setup._handle_crossover_relay_capture(None)
    assert correction_setup._get_relay_capture() is None  # slot not claimed


def test_crossover_relay_endpoint_inert_when_unconfigured(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        correction_setup._handle_crossover_relay_capture(None)


def test_crossover_relay_route_is_registered():
    from jasper.web import correction_setup

    assert "/crossover/relay-capture" in correction_setup._POST_ROUTES
