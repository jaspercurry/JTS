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


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("driver-test", "driver-test"),
        ("driver-confirm", "driver-confirm"),
        ("driver-abort", "driver-abort"),
        ("summed-test", "summed-test"),
        ("driver-capture-sweep", "driver-capture-sweep"),
        ("summed-capture-sweep", "summed-capture-sweep"),
    ],
)
def test_crossover_module_calls_secure_measurement_routes(name, path):
    source = Path("deploy/assets/correction/js/crossover/main.js").read_text(
        encoding="utf-8",
    )

    assert f"'{path}'" in source or f'"{path}"' in source, name
    assert "micCaptureSupport" in source
    assert "support.message" in source
    assert "postJSON" in source


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


# --- crossover screen envelope (aligned with the room envelope pattern) -------
#
# The composition tests run the REAL loaders + REAL coordinator against REAL
# state files written to env-pointed tmp paths — never a monkeypatched
# coordinator or a hand-rolled view dict (the mock-shape-drift class the P7
# review caught: a fake view hid that the envelope starved
# build_commissioning_view of six of its inputs).


def _active_topology_mapping(*, identity_verified: bool) -> dict:
    # The same real active_2_way mapping tests/test_active_speaker_startup_load
    # builds; parsed by the REAL OutputTopology.from_mapping on load.
    return {
        "artifact_schema_version": 1,
        "kind": "jts_output_topology",
        "topology_id": "bench_mono",
        "name": "Bench mono cabinet",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "mono",
                "label": "Mono cabinet",
                "kind": "mono",
                "mode": "active_2_way",
                "channels": [
                    {
                        "role": "woofer",
                        "physical_output_index": 0,
                        "identity_verified": identity_verified,
                    },
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "identity_verified": identity_verified,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "software_guard_requested",
                    },
                ],
            }
        ],
        "routing": {"mono_group_id": "mono"},
    }


def _point_commissioning_state_at(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    """Env-point every durable commissioning state file at tmp_path."""
    paths = {
        "topology": tmp_path / "output_topology.json",
        "design_draft": tmp_path / "design_draft.json",
        "crossover_preview": tmp_path / "crossover_preview.json",
        "measurements": tmp_path / "measurements.json",
        "calibration_level": tmp_path / "calibration_level.json",
        "baseline_profile": tmp_path / "baseline_profile.json",
        "startup_load": tmp_path / "startup_load.json",
    }
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(paths["topology"]))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE", str(paths["design_draft"])
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE",
        str(paths["crossover_preview"]),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE", str(paths["measurements"])
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE",
        str(paths["calibration_level"]),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(paths["baseline_profile"]),
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE", str(paths["startup_load"])
    )
    return paths


def _write_ready_draft_and_preview(paths: dict[str, Path]) -> None:
    """Write a saved-draft + fresh-preview fixture in the REAL on-disk shapes.

    The preview must carry the REAL design-draft fingerprint or the real
    `load_crossover_preview(current_design_draft=...)` marks it stale.
    """
    import json

    from jasper.active_speaker.crossover_preview import _design_draft_fingerprint
    from jasper.active_speaker.design_draft import load_design_draft

    paths["design_draft"].write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_design_draft",
            "status": "ready_for_review",
            "summary": {
                "missing_driver_info_roles": [],
                "missing_crossover_candidate_pairs": [],
            },
        }),
        encoding="utf-8",
    )
    loaded_draft = load_design_draft(paths["design_draft"])
    paths["crossover_preview"].write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_active_speaker_crossover_preview",
            "status": "ready_for_protected_staging",
            "permissions": {"may_prepare_protected_startup_config": True},
            "source": {
                "design_draft_fingerprint": _design_draft_fingerprint(loaded_draft),
            },
        }),
        encoding="utf-8",
    )


_ACTIVE_STATUS = {
    "targets": {"drivers": [{"target_id": "mono:woofer"}], "summed": []},
    "measurements": {"summary": {}},
}


def test_crossover_envelope_passive_speaker_is_gated():
    # Passive speaker: envelope carries active=False, no steps, one explanatory
    # verdict, no next action — the frontend renders nothing of Layer A.
    from jasper.active_speaker import crossover_envelope

    env = crossover_envelope.build_crossover_envelope(
        {"targets": {"drivers": [], "summed": []}, "measurements": {}}
    )
    assert env["active"] is False
    assert env["screen"] == crossover_envelope.SCREEN_NOT_APPLICABLE
    assert env["steps"] == []
    assert env["next_action"] is None
    assert env["nudges"] == []
    assert "crossover" in env["verdict_text"].lower()
    # Literal schema pin (a real shape pin, not a tautology against the const).
    assert env["schema_version"] == 1


def test_crossover_envelope_real_coordinator_moves_past_research(
    monkeypatch, tmp_path
):
    # THE Blocker-3 regression pin: with a saved design draft + fresh crossover
    # preview on disk, the envelope must move PAST the "research" step — through
    # the REAL loaders and the REAL coordinator. Before the shared
    # `load_commissioning_view` loader, the envelope starved the coordinator
    # (only `measurements` was passed), which pinned current_step to "research"
    # forever and pointed next_action backward at "Save values".

    from jasper.active_speaker import crossover_envelope
    from jasper.output_topology import OutputTopology, save_output_topology

    paths = _point_commissioning_state_at(monkeypatch, tmp_path)
    save_output_topology(
        OutputTopology.from_mapping(
            _active_topology_mapping(identity_verified=False)
        ),
        paths["topology"],
    )
    _write_ready_draft_and_preview(paths)

    env = crossover_envelope.build_crossover_envelope(_ACTIVE_STATUS)

    assert env["active"] is True
    # Draft + preview are saved → research is DONE; unverified outputs make
    # "map" the active step. The starved envelope reported "research" here.
    assert env["screen"] == "map"
    by_id = {s["id"]: s for s in env["steps"]}
    assert by_id["research"]["status"] == "done"
    assert by_id["map"]["status"] == "active"
    # And next_action is no longer the backward "save_driver_values".
    assert (env["next_action"] or {}).get("id") == "confirm_outputs"
    assert env["progress"]["position"] == 3  # map is 3rd of 5


def test_crossover_envelope_real_coordinator_reaches_done_when_applied(
    monkeypatch, tmp_path
):
    # SCREEN_DONE must be reachable: a saved baseline profile with
    # status="applied" and a MATCHING source fingerprint (computed by the REAL
    # candidate derivation, not hand-rolled) short-circuits
    # build_baseline_profile_candidate into returning the saved applied state,
    # and the envelope folds coordinator status "applied" onto "done". The
    # starved envelope could never reach this (baseline_profile was never
    # loaded).
    import json

    from jasper.active_speaker import crossover_envelope
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.output_topology import (
        OutputTopology,
        load_output_topology,
        save_output_topology,
    )

    paths = _point_commissioning_state_at(monkeypatch, tmp_path)
    save_output_topology(
        OutputTopology.from_mapping(
            _active_topology_mapping(identity_verified=True)
        ),
        paths["topology"],
    )
    _write_ready_draft_and_preview(paths)

    # Derive the REAL current-source fingerprint by running the real candidate
    # once over the same loaded state the envelope's loader will see.
    topology = load_output_topology()
    draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=draft)
    measurements = load_measurement_state(topology)
    first = build_baseline_profile_candidate(
        topology,
        design_draft=draft,
        crossover_preview=preview,
        measurements=measurements,
        write=False,
    )
    config_path = tmp_path / "baseline.yml"
    config_path.write_text("# applied baseline stub\n", encoding="utf-8")
    paths["baseline_profile"].write_text(
        json.dumps({
            **first,
            "status": "applied",
            "config": {
                **(first.get("config") or {}),
                "path": str(config_path),
                "exists": True,
            },
            "issues": [],
        }),
        encoding="utf-8",
    )

    env = crossover_envelope.build_crossover_envelope(_ACTIVE_STATUS)

    assert env["active"] is True
    assert env["screen"] == crossover_envelope.SCREEN_DONE
    assert env["progress"] == {"position": 5, "total": 5}
    assert "commissioned" in env["verdict_text"]


def test_crossover_envelope_screen_folds_applied_onto_done():
    from jasper.active_speaker import crossover_envelope

    view = {"status": "applied", "current_step": "profile", "steps": []}
    assert crossover_envelope._screen_for(view) == crossover_envelope.SCREEN_DONE
    assert crossover_envelope._progress(crossover_envelope.SCREEN_DONE) == {
        "position": 5, "total": 5,
    }


def test_crossover_envelope_nudges_come_from_real_coordinator_output():
    # The retry-nudge mapping consumes the REAL coordinator's output shape: a
    # failed combined test computed by build_commissioning_view itself (real
    # composer, real measurement-summary input shape) surfaces as a warn NUDGE,
    # never a block.
    from jasper.active_speaker import crossover_envelope
    from jasper.active_speaker.commissioning_coordinator import (
        build_commissioning_view,
    )

    from tests.test_active_speaker_startup_load import _topology

    view = build_commissioning_view(
        _topology(),
        design_draft={
            "kind": "jts_active_speaker_design_draft",
            "status": "ready_for_review",
            "summary": {
                "missing_driver_info_roles": [],
                "missing_crossover_candidate_pairs": [],
            },
        },
        crossover_preview={
            "kind": "jts_active_speaker_crossover_preview",
            "status": "ready_for_protected_staging",
            "permissions": {"may_prepare_protected_startup_config": True},
        },
        measurements={
            "summary": {
                "driver_checks_complete": True,
                "latest_summed_tests": {
                    "mono": {
                        "captured": True,
                        "audio_emitted": False,
                        "summed_test_id": "sum-1",
                        "issues": [{
                            "severity": "blocker",
                            "code": "tone_backend_failed",
                            "message": "backend failed",
                        }],
                    },
                },
                "latest_summed_validations": {},
            },
        },
    )
    nudges = crossover_envelope._nudges(view, active=True)
    assert any(n["code"].startswith("combined_test_retry") for n in nudges)
    assert all(n["severity"] in ("info", "warn") for n in nudges)


def test_crossover_envelope_progress_spine_is_the_coordinators_export():
    # Single-sourced spine: the envelope derives from the coordinator's exported
    # tuple, and the REAL coordinator's emitted step ids match that export — a
    # coordinator step-id rename now fails here instead of silently degrading
    # _screen_for to its fallback.
    from jasper.active_speaker import crossover_envelope
    from jasper.active_speaker.commissioning_coordinator import (
        COMMISSIONING_STEP_IDS,
        build_commissioning_view,
    )

    from tests.test_active_speaker_startup_load import _topology

    assert crossover_envelope._PROGRESS_SPINE is COMMISSIONING_STEP_IDS
    view = build_commissioning_view(_topology())
    assert tuple(s["id"] for s in view["steps"]) == COMMISSIONING_STEP_IDS


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
                        "tone": {"level_dbfs": -72.0},
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

    async def _loaded(**kwargs):
        return {"load": {"status": "loaded"}}

    async def _rolled_back(**kwargs):
        return {"status": "rolled_back"}

    monkeypatch.setattr(web, "_load_driver_commissioning_config_for_level", _loaded)
    monkeypatch.setattr(web, "_load_summed_commissioning_config", _loaded)
    monkeypatch.setattr(web, "_rollback_summed_commissioning_config", _rolled_back)
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
    assert raw["test_level_dbfs"] == -72.0  # top-level, as the JS reads it
    meta = raw["sweep_meta"]
    assert meta["sample_rate"] == 48000
    assert meta["duration_s"] > 0  # real synchronized-sine meta, not a stub
    assert {"f1", "f2", "n_samples", "amplitude_dbfs"} <= set(meta)
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
    assert "role" not in raw


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
