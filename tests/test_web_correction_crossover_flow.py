# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Secure correction crossover measurement flow."""

from __future__ import annotations

import io
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker import web_measurement
from jasper.active_speaker.capture_geometry import comparison_set_fingerprint
from jasper.web import correction_crossover_backend as backend
from jasper.web import correction_crossover_flow as flow
from tests.active_speaker_fixtures import mono_output_topology


def _topology(**kwargs):
    return mono_output_topology(topology_name="Bench mono", **kwargs)


def _json_handler(payload):
    body = json.dumps(payload).encode("utf-8")
    return SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=io.BytesIO(body),
    )


def test_relay_link_opens_a_new_tab_so_the_wizard_tab_survives():
    # A single-device household follows this link on the SAME phone/laptop
    # that is running the wizard. Without target=_blank + rel=noopener, the
    # only tab navigates away and the wizard is stranded (mirrors the room
    # flow's own relay-tap-link in correction_setup.py).
    page = flow.render_page("jts.local")
    html = page.decode("utf-8")
    assert (
        '<a id="crossover-relay-link" class="btn btn--primary" '
        'href="#" target="_blank" rel="noopener" hidden>Open phone capture</a>'
    ) in html


def test_crossover_page_css_styles_the_step_spine_and_nudges():
    # crossover/main.js's renderSteps()/renderNudges() emit
    # `wizard-step <status>` / `wizard-nudge <severity>` markup (the same
    # shape the room flow's correction.css already styles), but crossover.css
    # never carried the rules. The server's in-progress step status is
    # literally "active" (crossover_envelope.py's `_step_payload`), so the
    # CSS must target `.wizard-step.active`, not the room page's `.current`.
    css = (
        Path("deploy/assets/correction/crossover.css").read_text(encoding="utf-8")
    )
    assert ".wizard-step.active" in css
    assert ".wizard-step.done" in css
    assert ".wizard-nudge.info" in css
    assert ".wizard-nudge.warn" in css
    assert ".wizard-step.current" not in css


def test_request_payload_parses_capture_query():
    handler = SimpleNamespace(
        path=(
            "/crossover/summed-capture?speaker_group_id=mono&role=woofer"
            "&playback_id=abc&test_level_dbfs=-42.5"
            "&has_mic_calibration=true&expect_null=0"
            "&placement_proof=client-forged"
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


def test_capture_preset_prefers_frozen_applied_snapshot(monkeypatch):
    from jasper.active_speaker.profile import ActiveSpeakerPreset
    from jasper.active_speaker import commission_wiring
    from tests.test_active_speaker_profile import _two_way_preset

    frozen = ActiveSpeakerPreset.from_mapping(_two_way_preset())
    monkeypatch.setattr(
        commission_wiring,
        "resolve_commission_inputs",
        lambda: pytest.fail("frozen analysis must not read the mutable draft"),
    )

    assert web_measurement.capture_preset(object(), frozen) is frozen


# ---------- _noise_band_report_value (SC-1 browser-input validation) --------


def test_noise_band_report_value_accepts_correction_shape_list():
    raw = [
        {"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -80},
        {"band_id": "treble", "band_hz": [4000.0, 12000.0], "level_dbfs": -75.5},
    ]
    out = web_measurement._noise_band_report_value(raw)
    assert out == [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0},
        {"band_id": "treble", "band_hz": [4000.0, 12000.0], "level_dbfs": -75.5},
    ]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-list",
        [],
        [{"band_id": "mid", "band_hz": [1000.0, 4000.0]}],  # missing level_dbfs
        [{"band_id": "mid", "level_dbfs": -80.0}],  # missing band_hz
        [{"band_hz": [1000.0, 4000.0], "level_dbfs": -80.0}],  # missing band_id
        [{"band_id": "", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0}],
        [{"band_id": "mid", "band_hz": [1000.0], "level_dbfs": -80.0}],  # wrong len
        [{"band_id": "mid", "band_hz": "1000-4000", "level_dbfs": -80.0}],
        [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": "loud"}],
        ["not-a-mapping"],
        [{"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0}, "bad"],
    ],
)
def test_noise_band_report_value_rejects_malformed_input(raw):
    assert web_measurement._noise_band_report_value(raw) is None


def test_driver_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    # Hygiene: scope the commissioning-bundle sessions dir to tmp_path so this
    # test can never touch the real /var/lib path when the suite runs as root.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions")
    )
    calls = {}
    topology = object()
    frozen_preset = object()
    wav_path = tmp_path / "driver.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    def capture_preset(_topology, supplied=None):
        calls["analysis_preset"] = supplied
        return supplied

    monkeypatch.setattr(web_measurement, "capture_preset", capture_preset)
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
    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )
    monkeypatch.setattr(measurement, "load_measurement_state", lambda _t: {})
    confirmation = {
        "accepted": True,
        "playback_id": "play-1",
        "target": {"speaker_group_id": "mono", "role": "woofer", "output_index": 0},
    }
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_args, **_kwargs: {
            "valid": True,
            "source": "durable_current_driver_measurement",
            "confirmation": confirmation,
        },
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
            "noise_band_report": [
                {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0},
            ],
        },
        b"wav",
        preset=frozen_preset,
    )

    assert payload["recorded"] is True
    assert payload["calibration_id"] == "cal-1"
    assert payload["measurement_mode"] == {"mode": "phase_aware"}
    assert calls["analysis_preset"] is frozen_preset
    assert calls["args"] == (topology, frozen_preset)
    assert calls["kwargs"]["speaker_group_id"] == "mono"
    assert calls["kwargs"]["role"] == "woofer"
    assert calls["kwargs"]["captured_wav"] == wav_path
    # Validated and threaded through to the analyzer call.
    assert calls["kwargs"]["noise_band_report"] == [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0},
    ]
    assert calls["kwargs"]["playback_id"] == "play-1"
    assert calls["kwargs"]["calibration"] == "curve"
    assert calls["kwargs"]["safe_session"] is None
    assert calls["kwargs"]["placement_proof"] is None
    assert calls["kwargs"]["durable_floor_confirmation"] == confirmation
    # No capture_geometry in the request payload -> defaults to near_field.
    assert calls["kwargs"]["capture_geometry"] == "near_field"


def test_backend_authoritative_adapter_requires_handoff_and_binds_current_run(
    monkeypatch,
):
    from jasper.active_speaker import baseline_profile
    from jasper.active_speaker import commissioning_isolated_producer
    from jasper.active_speaker.commissioning_evidence_store import (
        CommissioningEvidenceStore,
    )

    run = SimpleNamespace(
        session_id="session-1",
        session_fingerprint="f" * 64,
    )
    run_store = SimpleNamespace(current_handle=lambda: run)
    evidence_store = object()
    applied = {"status": "applied"}
    promoted = []
    recorders = []
    comparison = {
        "bundle_session_id": run.session_id,
        "fingerprint": run.session_fingerprint,
    }

    monkeypatch.setattr(backend, "_COMMISSIONING_RUN_STORE", run_store)
    monkeypatch.setattr(
        backend._LEVEL_LEASE,
        "assert_volume_safety_resolved",
        lambda: None,
    )
    monkeypatch.setattr(
        baseline_profile,
        "load_applied_baseline_profile_state",
        lambda: applied,
    )
    monkeypatch.setattr(
        CommissioningEvidenceStore,
        "open",
        classmethod(lambda _cls, *_args, **_kwargs: evidence_store),
    )
    monkeypatch.setattr(
        commissioning_isolated_producer,
        "promote_isolated_driver_capture",
        lambda **kwargs: promoted.append(kwargs) or {"status": "collecting"},
    )

    def fake_record(*_args, authoritative_recorder=None, **_kwargs):
        recorders.append(authoritative_recorder)
        if authoritative_recorder is not None:
            return {
                "authoritative_evidence": authoritative_recorder(
                    comparison_set=comparison,
                    marker="relay-wav",
                )
            }
        return {"authoritative_evidence": None}

    monkeypatch.setattr(web_measurement, "record_driver_capture", fake_record)

    without_handoff = backend.record_driver_capture({}, b"wav")
    with_handoff = backend.record_driver_capture(
        {},
        b"wav",
        admission_handoff={"server": "owned"},
    )

    assert without_handoff["authoritative_evidence"] is None
    assert recorders[0] is None
    assert callable(recorders[1])
    assert with_handoff["authoritative_evidence"] == {"status": "collecting"}
    assert promoted == [
        {
            "comparison_set": comparison,
            "marker": "relay-wav",
            "applied_profile": applied,
            "run": run,
            "run_store": run_store,
            "evidence_store": evidence_store,
        }
    ]


def test_authoritative_adapter_excludes_rejected_and_non_authoritative_records():
    calls = []

    def recorder(**kwargs):
        calls.append(kwargs)
        return {"status": "collecting"}

    assert web_measurement._record_authoritative_driver_capture(
        recorder=recorder,
        capture_geometry="reference_axis",
        accepted=False,
        admission_handoff={"server": "owned"},
        inputs={"marker": "rejected"},
    ) is None
    assert web_measurement._record_authoritative_driver_capture(
        recorder=recorder,
        capture_geometry="near_field",
        accepted=True,
        admission_handoff={"server": "owned"},
        inputs={"marker": "near-field"},
    ) is None
    assert web_measurement._record_authoritative_driver_capture(
        recorder=None,
        capture_geometry="reference_axis",
        accepted=True,
        admission_handoff=None,
        inputs={"marker": "historical"},
    ) is None
    with pytest.raises(ValueError, match="admitted playback proof"):
        web_measurement._record_authoritative_driver_capture(
            recorder=recorder,
            capture_geometry="reference_axis",
            accepted=True,
            admission_handoff=None,
            inputs={"marker": "admission-less"},
        )
    assert calls == []


def test_driver_capture_rejects_post_play_topology_change_even_with_old_session(
    monkeypatch, tmp_path,
):
    # Hygiene: scope the commissioning-bundle sessions dir to tmp_path so this
    # test can never touch the real /var/lib path when the suite runs as root.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions")
    )
    from jasper.active_speaker.measurement import active_driver_targets
    import jasper.active_speaker.measurement as measurement
    import jasper.active_speaker.safe_playback as safe_playback

    old_topology = _topology(tweeter_output=1)
    current_topology = _topology(tweeter_output=2)
    old_target = next(
        target
        for target in active_driver_targets(old_topology)
        if target["role"] == "tweeter"
    )
    old_record = {
        "captured": True,
        "target_id": "mono:tweeter",
        "target_fingerprint": old_target["target_fingerprint"],
        "speaker_group_id": "mono",
        "role": "tweeter",
        "output_index": 1,
        "outcome": "heard_correct_driver",
        "playback_id": "old-play",
        "floor_confirmation": {
            "accepted": True,
            "playback_id": "old-play",
            "target": {
                "speaker_group_id": "mono",
                "role": "tweeter",
                "output_index": 1,
            },
        },
        "issues": [],
    }
    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: current_topology)
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: {
            "summary": {
                "latest_driver_measurements": {"mono:tweeter": old_record},
                # No acoustic block -- an accurate real-shaped by-ear
                # confirmation record, so it belongs in the confirmation-only
                # index too (see measurement._latest_current_driver_confirmations).
                "latest_driver_confirmations": {"mono:tweeter": old_record},
            },
        },
    )
    old_session_reads = []
    monkeypatch.setattr(
        safe_playback,
        "load_safe_playback_state",
        lambda: old_session_reads.append(True) or {"status": "armed"},
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda *_args, **_kwargs: pytest.fail("must reject before storing the WAV"),
    )

    with pytest.raises(ValueError, match="incomplete"):
        backend.record_driver_capture(
            {
                "speaker_group_id": "mono",
                "role": "tweeter",
                "playback_id": "old-play",
            },
            b"wav",
        )

    assert old_session_reads == []


def test_summed_capture_records_through_active_speaker_layer(monkeypatch, tmp_path):
    # Hygiene: scope the commissioning-bundle sessions dir to tmp_path so this
    # test can never touch the real /var/lib path when the suite runs as root.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(tmp_path / "sessions")
    )
    calls = {}
    topology = object()
    preset = object()
    wav_path = tmp_path / "summed.wav"

    monkeypatch.setattr(
        web_measurement,
        "load_output_topology",
        lambda: topology,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_preset",
        lambda _topology, _frozen=None: preset,
    )
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
            "capture_geometry": "reference_axis",
            "expect_null": True,
            "noise_floor_dbfs": -70.0,
            "noise_band_report": "not-a-list",  # invalid -> validated to None
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
    assert calls["kwargs"]["noise_floor_dbfs"] == -70.0
    assert calls["kwargs"]["noise_band_report"] is None
    assert calls["kwargs"]["capture_geometry"] == "near_field"


def test_driver_capture_geometry_comes_from_server_placement_policy(
    monkeypatch, tmp_path
):
    """Browser geometry cannot opt into reference-axis analysis.

    The future Lane B capture reaches that analyzer mode through the verified
    placement proof while reusing this same record/repeat path.
    """
    from jasper.active_speaker.capture_geometry import (
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        normalized_placement_proof,
    )
    from jasper.active_speaker.measurement import active_driver_targets

    topology = _topology()
    wav_path = tmp_path / "driver.wav"

    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web_measurement, "capture_preset", lambda _t, supplied=None: supplied
    )
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
        web_measurement, "capture_calibration", lambda raw: (None, None, {})
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(
        calibration_level, "load_calibration_level_state", lambda: {}
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _t: {"active_comparison_set": _COMPARISON_SET},
    )
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_a, **_k: {"valid": True, "source": "test", "confirmation": None},
    )

    seen: list[str] = []

    def fake_record(*_args, capture_geometry, **_kwargs):
        seen.append(capture_geometry)
        return {"recorded": True}

    monkeypatch.setattr(capture, "record_driver_acoustic_capture", fake_record)

    target = next(
        value
        for value in active_driver_targets(topology)
        if value["target_id"] == "mono:woofer"
    )
    proof = normalized_placement_proof(
        policy_id=REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        acknowledgement_binding="fixed-axis-acknowledgement",
        relay_session_id="relay-fixed-axis",
        capture_page={
            "capture_protocol_version": 2,
            "capture_page_build": "20260712.1",
        },
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=target["target_fingerprint"],
        comparison_set=_COMPARISON_SET,
    )
    backend.record_driver_capture(
        {
            "speaker_group_id": "mono",
            "role": "woofer",
            "capture_geometry": "reference_axis",
        },
        b"wav",
    )
    backend.record_driver_capture(
        {
            "speaker_group_id": "mono",
            "role": "woofer",
        },
        b"wav",
        placement_proof=proof,
    )

    assert seen == ["near_field", "reference_axis"]


def test_driver_capture_rejects_unknown_server_placement_policy():
    with pytest.raises(ValueError, match="placement policy is unsupported"):
        web_measurement._driver_capture_geometry({
            "schema_version": 1,
            "policy_id": "client_invented",
            "accepted": True,
            "confirmation_source": "relay_begin_capture",
            "capture_protocol_version": 2,
        })


def test_driver_capture_rejects_incomplete_reference_axis_proof():
    with pytest.raises(ValueError, match="placement proof is invalid"):
        web_measurement._driver_capture_geometry({
            "policy_id": "driver_reference_axis_v1",
            "accepted": True,
        })


def test_summed_capture_geometry_requires_fixed_axis_proof_and_current_bindings():
    from jasper.active_speaker.capture_geometry import (
        SUMMED_PLACEMENT_POLICY_ID,
        normalized_placement_proof,
    )
    from jasper.active_speaker.measurement import active_summed_targets

    topology = _topology()
    target = active_summed_targets(topology)[0]
    proof = normalized_placement_proof(
        policy_id=SUMMED_PLACEMENT_POLICY_ID,
        acknowledgement_binding="summed-fixed-axis-acknowledgement",
        relay_session_id="relay-summed-fixed-axis",
        capture_page={
            "capture_protocol_version": 2,
            "capture_page_build": "20260712.1",
        },
        speaker_group_id="mono",
        role="summed",
        target_fingerprint=target["group_fingerprint"],
        comparison_set=_COMPARISON_SET,
    )

    assert web_measurement._summed_capture_geometry(
        proof,
        _COMPARISON_SET,
        speaker_group_id="mono",
        target_fingerprint=target["group_fingerprint"],
    ) == "reference_axis"
    assert web_measurement._summed_capture_geometry(None) == "near_field"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("acknowledgement_binding_sha256", "bad"),
        ("relay_session_id", ""),
        ("capture_page_build", "unversioned"),
        ("speaker_group_id", "another-group"),
        ("role", "woofer"),
        ("target_fingerprint", "f" * 64),
        ("comparison_set_fingerprint", "e" * 64),
    ),
)
def test_summed_capture_geometry_rejects_fabricated_or_stale_proof(
    field: str,
    value: str,
):
    from jasper.active_speaker.capture_geometry import (
        SUMMED_PLACEMENT_POLICY_ID,
        normalized_placement_proof,
    )
    from jasper.active_speaker.measurement import active_summed_targets

    target = active_summed_targets(_topology())[0]
    proof = normalized_placement_proof(
        policy_id=SUMMED_PLACEMENT_POLICY_ID,
        acknowledgement_binding="summed-fixed-axis-acknowledgement",
        relay_session_id="relay-summed-fixed-axis",
        capture_page={
            "capture_protocol_version": 2,
            "capture_page_build": "20260712.1",
        },
        speaker_group_id="mono",
        role="summed",
        target_fingerprint=target["group_fingerprint"],
        comparison_set=_COMPARISON_SET,
    )
    proof[field] = value

    with pytest.raises(ValueError, match="invalid or stale"):
        web_measurement._summed_capture_geometry(
            proof,
            _COMPARISON_SET,
            speaker_group_id="mono",
            target_fingerprint=target["group_fingerprint"],
        )


@pytest.mark.parametrize(
    (
        "final_write_fails",
        "abort_write_fails",
        "complete_write_fails",
        "promotion_fails",
    ),
    [
        (False, False, False, False),
        (True, False, False, False),
        (True, True, False, False),
        (False, False, True, False),
        (False, True, True, False),
        (False, False, False, True),
    ],
)
@pytest.mark.parametrize("capture_geometry", ("near_field", "reference_axis"))
def test_driver_capture_wires_three_repeats_before_one_durable_record(
    monkeypatch,
    tmp_path,
    final_write_fails,
    abort_write_fails,
    complete_write_fails,
    promotion_fails,
    capture_geometry,
):
    topology = object()
    wav_path = tmp_path / "driver.wav"
    wav_path.write_bytes(b"wav")
    comparison_set = dict(_COMPARISON_SET)
    target_fingerprint = "c" * 64
    policy_id = (
        "driver_reference_axis_v1"
        if capture_geometry == "reference_axis"
        else "driver_same_distance_v1"
    )
    placement_proof = _placement_proof(policy_id, "woofer", target_fingerprint)
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=target_fingerprint,
        capture_geometry=capture_geometry,
    )
    admission_path = tmp_path / "repeat-admission.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE", str(admission_path)
    )
    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web_measurement, "capture_preset", lambda _topology, supplied=None: supplied
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {
            "sample_rate": 48000,
            "n_samples": 48000,
            "f1": 20.0,
            "f2": 12000.0,
            "duration_s": 1.0,
            "amplitude_dbfs": -12.0,
        },
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: (None, None, {"mode": "magnitude_only"}),
    )
    ambient = {
        "schema_version": 1,
        "domain": "deconvolved",
        "method": "deconvolved_band_difference",
        "bands": [],
    }
    monkeypatch.setattr(
        web_measurement, "_stored_ambient_report", lambda *a, **k: ambient
    )
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    monkeypatch.setattr(
        web_measurement,
        "_resolve_bundle_for_capture",
        lambda *a, **k: (bundle_dir, "captures/driver.wav"),
    )
    monkeypatch.setattr(
        web_measurement,
        "_driver_target_fingerprint",
        lambda *a, **k: target_fingerprint,
    )
    repeat_events = []
    monkeypatch.setattr(
        web_measurement,
        "log_event",
        lambda _logger, event, **fields: repeat_events.append((event, fields)),
    )

    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.bundles as active_speaker_bundles
    import jasper.active_speaker.commissioning_admission as commissioning_admission
    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.measurement as measurement
    import jasper.active_speaker.repeat_admission as repeat_admission

    monkeypatch.setattr(
        repeat_admission,
        "log_event",
        lambda _logger, event, **fields: repeat_events.append((event, fields)),
    )
    if abort_write_fails:
        monkeypatch.setattr(
            repeat_admission,
            "abort_ready",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("repeat admission state is read-only")
            ),
        )
    if complete_write_fails:
        monkeypatch.setattr(
            repeat_admission,
            "complete",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("repeat admission completion is read-only")
            ),
        )

    monkeypatch.setattr(calibration_level, "load_calibration_level_state", lambda: {})
    monkeypatch.setattr(
        commissioning_admission,
        "validate_capture_admission_handoff",
        lambda handoff, **_kwargs: dict(handoff),
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: {"active_comparison_set": comparison_set},
    )
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_a, **_k: {
            "valid": True,
            "source": "durable_current_driver_measurement",
            "confirmation": {},
        },
    )

    calls = []

    def fake_record(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("record") is not None:
            index = sum(1 for call in calls if call.get("record") is not None)
            return {
                "recorded": True,
                "verdict": "present",
                "outcome": "heard_correct_driver",
                    "acoustic": {
                        "verdict": "present",
                        "capture_geometry": capture_geometry,
                    "observed_mic_dbfs": -30.0 + index / 10.0,
                    "mic_clipping": False,
                    "snr": {
                        "verdict": "ok",
                        "worst_relevant": {
                            "band_id": "mid",
                            "estimated_snr_db": 31.0,
                            "verdict": "ok",
                        },
                    },
                    **(
                        {
                            "gating": {
                                "applied": True,
                                "f_valid_floor_hz": 150.0,
                            },
                            "overlap_levels": [{
                                "fc_hz": 1000.0,
                                "above_validity_floor": True,
                                "usable": True,
                            }],
                        }
                        if capture_geometry == "reference_axis"
                        else {}
                    ),
                },
                "excitation": {},
                "placement_proof": kwargs.get("placement_proof"),
            }
        repeats = kwargs["repeats"]
        if final_write_fails:
            raise OSError("measurement state is read-only")
        return {
            "recorded": True,
            "verdict": "present",
            "acoustic": {"verdict": "present", "snr": {"verdict": "ok"}},
            "measurement": {"repeats": repeats},
            "placement_proof": kwargs.get("placement_proof"),
        }

    monkeypatch.setattr(capture, "record_driver_acoustic_capture", fake_record)
    repeat_artifact_payloads = []

    def append_repeat_capture(*_args, **kwargs):
        repeat_artifact_payloads.append(kwargs["payload"])
        return {"artifact_path": f"captures/repeat-{kwargs['index']}.wav"}

    monkeypatch.setattr(
        active_speaker_bundles,
        "append_repeat_capture",
        append_repeat_capture,
    )
    monkeypatch.setattr(
        active_speaker_bundles,
        "record_repeat_progress",
        lambda *_a, **kwargs: dict(kwargs),
    )
    repeat_admission.activate(comparison_set, path=admission_path)
    store = backend.CrossoverLevelLease()
    raw = {
        "speaker_group_id": "mono",
        "role": "woofer",
        "playback_id": "play",
        "ambient_duration_s": 12.0,
    }
    authoritative_calls = []

    def record_authoritative(**kwargs):
        authoritative_calls.append(kwargs)
        if promotion_fails:
            raise OSError("strict promotion failed")
        accepted = len(authoritative_calls)
        return {
            "status": "complete" if accepted == 3 else "collecting",
            "accepted": accepted,
            "required": 3,
            "driver_complete": accepted == 3,
            "complete": accepted == 3,
        }

    def capture_attempt(attempt):
        reservation = repeat_admission.reserve(
            comparison_set,
            target_id=repeat_target_id,
            target_fingerprint=repeat_target_fingerprint,
            path=admission_path,
        )
        assert reservation["attempt"] == attempt
        return web_measurement.record_driver_capture(
            {
                **raw,
                "repeat_reservation": reservation,
            },
            b"wav",
            placement_proof=placement_proof,
            repeat_store=store,
            admission_handoff={"admission_id": f"admission-{attempt}"},
            authoritative_recorder=record_authoritative,
        )

    if promotion_fails and capture_geometry == "reference_axis":
        with pytest.raises(OSError, match="strict promotion failed"):
            capture_attempt(1)
        assert store.driver_repeats(
            store.repeat_session_key(
                comparison_set["comparison_set_id"],
                repeat_target_fingerprint,
            )
        ) == []
        failure = store.repeat_failure(repeat_target_id)
        assert failure is not None
        assert failure["reason"] == "authoritative_promotion_failed"
        assert repeat_admission.snapshot(
            comparison_set, path=admission_path
        )["targets"][repeat_target_id]["status"] == "refused"
        assert len(authoritative_calls) == 1
        return

    first = capture_attempt(1)
    second = capture_attempt(2)
    if final_write_fails or complete_write_fails:
        with pytest.raises(OSError, match="read-only"):
            capture_attempt(3)
        target_state = repeat_admission.snapshot(
            comparison_set, path=admission_path
        )["targets"][repeat_target_id]
        expected_status = "ready" if abort_write_fails else "aborted"
        assert target_state["status"] == expected_status
        with pytest.raises(ValueError, match=f"is {expected_status}"):
            repeat_admission.reserve(
                comparison_set,
                target_id=repeat_target_id,
                target_fingerprint=repeat_target_fingerprint,
                path=admission_path,
            )
        attempts = [
            fields
            for event, fields in repeat_events
            if event == "correction.crossover_repeat_attempt"
        ]
        assert [entry["attempt"] for entry in attempts] == [1, 2, 3]
        aborts = [
            fields
            for event, fields in repeat_events
            if event == "correction.crossover_repeat_aborted"
        ]
        abort_failures = [
            fields
            for event, fields in repeat_events
            if event == "correction.crossover_repeat_abort_failed"
        ]
        if complete_write_fails:
            assert len([call for call in calls if call.get("repeats") is not None]) == 1
            reason = "repeat_completion_failed"
        else:
            reason = "measurement_persistence_failed"
        if abort_write_fails:
            assert aborts == []
            assert len(abort_failures) == 1
            assert abort_failures[0]["reason"] == reason
            assert abort_failures[0]["failure_type"] == "OSError"
        else:
            assert len(aborts) == 1
            assert aborts[0]["reason"] == reason
            assert abort_failures == []
            fresh_comparison = {
                "comparison_set_id": "c" * 32,
                "fingerprint": "d" * 64,
            }
            repeat_admission.activate(fresh_comparison, path=admission_path)
            fresh = repeat_admission.reserve(
                fresh_comparison,
                target_id="mono:woofer",
                target_fingerprint="fresh-target-fp",
                path=admission_path,
            )
            assert fresh["attempt"] == 1
        return
    third = capture_attempt(3)

    assert first["repeat_progress"] == {
        "attempts": 1,
        "accepted": 1,
        "target": 3,
        "bounded_recapture": False,
        "latest_rejection": None,
    }
    assert second["repeat_progress"]["attempts"] == 2
    assert third["recorded"] is True
    assert third["measurement"]["repeats"]["accepted"] == 3
    assert third["acoustic"]["snr"] is not None
    assert repeat_admission.snapshot(comparison_set, path=admission_path)["targets"][
        repeat_target_id
    ]["status"] == "completed"
    assert len([call for call in calls if call.get("record") is not None]) == 3
    assert all(
        call.get("emit_lifecycle_event") is False
        for call in calls
        if call.get("record") is not None
    )
    assert len([call for call in calls if call.get("repeats") is not None]) == 1
    assert store.repeat_snapshot()["targets"] == {}
    attempts = [
        fields
        for event, fields in repeat_events
        if event == "correction.crossover_repeat_attempt"
    ]
    assert [entry["attempt"] for entry in attempts] == [1, 2, 3]
    assert all(entry["accepted"] is True for entry in attempts)
    assert all(entry["snr_db"] == 31.0 for entry in attempts)
    assert all(entry["clipping"] is False for entry in attempts)
    assert len(repeat_artifact_payloads) == 3
    assert len(authoritative_calls) == (
        3 if capture_geometry == "reference_axis" else 0
    )
    for artifact in repeat_artifact_payloads:
        analysis_input = artifact["analysis_input"]
        assert analysis_input["schema_version"] == 1
        assert analysis_input["response_amplitude"] == "recompute_from_raw_wav"
        assert analysis_input["display_fr_curve_peak_normalized"] is True
        assert analysis_input["sweep_meta"]["amplitude_dbfs"] == -12.0
        assert analysis_input["capture_geometry"] == capture_geometry
        assert analysis_input["ambient_duration_s"] == 12.0
        assert analysis_input["calibration"] is None


def test_driver_analysis_input_preserves_calibrated_absolute_replay_contract():
    from jasper.active_speaker.web_measurement import driver_analysis_input_evidence
    from jasper.audio_measurement.calibration import CalibrationCurve

    evidence = driver_analysis_input_evidence(
        sweep_meta={
            "sample_rate": 48000,
            "f1": 20.0,
            "f2": 20000.0,
            "duration_s": 8.0,
            "amplitude_dbfs": -12.0,
        },
        excitation={
            "schema_version": 1,
            "scope": "sweep_plus_role_gain_and_driver_level_lock",
            "locked_main_volume_db": -6.5,
            "effective_peak_dbfs": -21.5,
        },
        calibration_curve=CalibrationCurve(
            freqs_hz=[20.0, 1000.0, 20000.0],
            correction_db=[1.2, 0.0, -2.3],
        ),
        calibration_id="calibration-safe-id",
        capture_geometry="reference_axis",
        ambient_duration_s=12.0,
    )

    assert evidence["response_amplitude"] == "recompute_from_raw_wav"
    assert evidence["sweep_meta"]["duration_s"] == 8.0
    assert evidence["excitation"]["locked_main_volume_db"] == -6.5
    assert evidence["calibration"] == {
        "calibration_id": "calibration-safe-id",
        "curve": {
            "freqs_hz": [20.0, 1000.0, 20000.0],
            "correction_db": [1.2, 0.0, -2.3],
            "phase_deg": None,
        },
    }
    assert "serial" not in str(evidence).lower()


@pytest.mark.parametrize("corruption", ("stale_target", "conflicting_geometry"))
def test_driver_repeat_finalization_revalidates_winner_placement_context(
    monkeypatch,
    corruption: str,
) -> None:
    """A process-local winner is re-proved before admission becomes ready."""
    import jasper.active_speaker.commissioning_capture as capture

    target_fingerprint = "c" * 64
    proof = _placement_proof(
        "driver_same_distance_v1", "woofer", target_fingerprint
    )
    acoustic = {"capture_geometry": "near_field"}
    if corruption == "stale_target":
        proof["target_fingerprint"] = "f" * 64
    else:
        acoustic["capture_geometry"] = "reference_axis"
    winner = {
        "analysis_kwargs": {},
        "preset": object(),
        "placement_proof": proof,
        "acoustic": acoustic,
        "wav_path": "winner.wav",
        "bundle_dir": None,
    }
    monkeypatch.setattr(
        capture,
        "record_driver_repeat_aggregate",
        lambda **_kwargs: {
            "accepted": 2,
            "aggregate_repeat": winner,
            "per_repeat": [],
        },
    )
    monkeypatch.setattr(
        capture,
        "record_driver_acoustic_capture",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid winner must be refused before durable measurement"
        ),
    )
    store = SimpleNamespace(
        repeat_session_key=lambda *_args: ("comparison", "target")
    )

    with pytest.raises(RuntimeError, match="placement|geometry"):
        web_measurement._finalize_driver_repeat_set(
            topology=object(),
            comparison_set=_COMPARISON_SET,
            speaker_group_id="mono",
            role="woofer",
            topology_target_fingerprint=target_fingerprint,
            repeat_target_id="mono:woofer",
            repeat_target_fingerprint=target_fingerprint,
            reservation={"attempt": 3, "token": "reservation"},
            admission_result={"accepted": True},
            repeats=[{"bundle_dir": None}],
            repeat_store=store,
        )
def test_repeat_capture_refuses_without_controlled_quiet_interval(monkeypatch, tmp_path):
    topology = object()
    wav_path = tmp_path / "driver.wav"
    wav_path.write_bytes(b"wav")
    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(web_measurement, "capture_preset", lambda *_a, **_k: object())
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda _raw: (None, None, {}),
    )
    monkeypatch.setattr(web_measurement, "capture_sweep_meta", lambda _raw: {})

    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(measurement, "load_measurement_state", lambda _topology: {})
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_a, **_k: {"valid": True, "confirmation": None},
    )
    with pytest.raises(ValueError, match="controlled pre-sweep quiet"):
        web_measurement.record_driver_capture(
            {"speaker_group_id": "mono", "role": "woofer"},
            b"wav",
            repeat_store=backend.CrossoverLevelLease(),
        )


@pytest.mark.parametrize(
    ("capture_geometry", "should_finalize"),
    (("near_field", True), ("reference_axis", False)),
)
def test_terminal_transport_failure_never_completes_thin_fixed_axis_evidence(
    monkeypatch, tmp_path, capture_geometry, should_finalize
):
    from jasper.active_speaker import (
        bundles as active_speaker_bundles,
        commissioning_capture,
        repeat_admission,
    )

    comparison = dict(_COMPARISON_SET)
    target_fingerprint = "c" * 64
    placement_proof = _placement_proof(
        (
            "driver_reference_axis_v1"
            if capture_geometry == "reference_axis"
            else "driver_same_distance_v1"
        ),
        "woofer",
        target_fingerprint,
    )
    admission_path = tmp_path / "repeat-admission.json"
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE", str(admission_path)
    )
    repeat_admission.activate(comparison, path=admission_path)
    store = backend.CrossoverLevelLease()
    key = store.repeat_session_key(
        comparison["comparison_set_id"], target_fingerprint
    )
    wav_path = tmp_path / "accepted.wav"
    wav_path.write_bytes(b"wav")
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    acoustic = {
        "verdict": "present",
        "capture_geometry": capture_geometry,
        "observed_mic_dbfs": -30.0,
        "mic_clipping": False,
        "snr": {
            "verdict": "ok",
            "worst_relevant": {
                "band_id": "mid",
                "estimated_snr_db": 30.0,
                "verdict": "ok",
            },
        },
    }
    for attempt in (1, 2):
        reservation = repeat_admission.reserve(
            comparison,
            target_id="mono:woofer",
            target_fingerprint=target_fingerprint,
            path=admission_path,
        )
        store.append_driver_repeat(
            key,
            target_id="mono:woofer",
            attempt=attempt,
            item={
                "attempt": attempt,
                "verdict": "present",
                "acoustic": acoustic,
                "wav_path": str(wav_path),
                "sweep_meta": {"sample_rate": 48000},
                "playback_id": f"play-{attempt}",
                "test_level_dbfs": -12.0,
                "excitation": {},
                "placement_proof": placement_proof,
                "ambient_report": {},
                "ambient_duration_s": 14.0,
                "analysis_kwargs": {},
                "preset": object(),
                "measurement_mode": {"mode": "magnitude_only"},
                "calibration_id": None,
                "bundle_dir": str(bundle_dir),
                "capture_relpath": "captures/driver.wav",
                "floor_evidence_source": "durable_current_driver_measurement",
            },
        )
        repeat_admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint=target_fingerprint,
            token=reservation["token"],
            result={"accepted": True},
            status="active",
            path=admission_path,
        )
    third = repeat_admission.reserve(
        comparison,
        target_id="mono:woofer",
        target_fingerprint=target_fingerprint,
        path=admission_path,
    )
    repeat_admission.finish(
        comparison,
        target_id="mono:woofer",
        target_fingerprint=target_fingerprint,
        token=third["token"],
        result={"accepted": False, "reject_reason": "level_outlier"},
        status="active",
        path=admission_path,
    )
    fourth = repeat_admission.reserve(
        comparison,
        target_id="mono:woofer",
        target_fingerprint=target_fingerprint,
        path=admission_path,
    )
    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: object())
    monkeypatch.setattr(
        web_measurement,
        "_driver_target_fingerprint",
        lambda *_args, **_kwargs: target_fingerprint,
    )
    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: {"active_comparison_set": comparison},
    )
    recorded = {}
    appended = []
    events = []

    def fake_record(*_args, repeats, placement_proof, **_kwargs):
        recorded.update(repeats)
        return {
            "recorded": True,
            "verdict": "present",
            "measurement": {"repeats": repeats},
            "placement_proof": placement_proof,
        }

    monkeypatch.setattr(
        commissioning_capture, "record_driver_acoustic_capture", fake_record
    )
    monkeypatch.setattr(
        active_speaker_bundles,
        "record_repeat_progress",
        lambda *_args, **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(
        active_speaker_bundles,
        "append_capture",
        lambda *_args, **kwargs: appended.append(kwargs),
    )
    monkeypatch.setattr(
        web_measurement,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )
    payload = web_measurement.finalize_driver_repeats_after_terminal_failure(
        comparison_set=comparison,
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=target_fingerprint,
        capture_geometry=capture_geometry,
        reservation=fourth,
        failure_type="CaptureAborted",
        repeat_store=store,
    )
    if not should_finalize:
        assert payload is None
        repeat_admission.finish(
            comparison,
            target_id="mono:woofer",
            target_fingerprint=target_fingerprint,
            token=fourth["token"],
            result={"accepted": False, "reject_reason": "capture_failed"},
            status=repeat_admission.failure_status(fourth["attempt"]),
            path=admission_path,
        )
        assert repeat_admission.snapshot(
            comparison, path=admission_path
        )["targets"]["mono:woofer"]["status"] == "refused"
        assert store.driver_repeats(key) != []
        assert recorded == {}
        assert appended == []
        return
    assert payload is not None and payload["recorded"] is True
    assert recorded["accepted"] == 2
    assert recorded["confidence"] == "reduced"
    assert recorded["admission_attempts"] == 4
    assert appended[0]["relative_path"] == "captures/driver.wav"
    assert appended[0]["payload"]["placement_proof"]["accepted"] is True
    assert {event for event, _fields in events} >= {
        "active_speaker.web_driver_capture",
        "correction.crossover_repeats_finalized_after_transport_failure",
    }
    assert repeat_admission.snapshot(comparison, path=admission_path)["targets"][
        "mono:woofer"
    ]["status"] == "completed"
    assert store.driver_repeats(key) == []


def test_repeat_store_never_pairs_attempts_across_comparison_sets():
    store = backend.CrossoverLevelLease()
    acoustic = {
        "verdict": "present",
        "observed_mic_dbfs": -30.0,
        "mic_clipping": False,
        "snr": {"verdict": "ok"},
    }
    item = {"verdict": "present", "acoustic": acoustic}

    store.append_driver_repeat(
        store.repeat_session_key("a" * 32, "driver-fp"),
        target_id="mono:woofer",
        item=item,
    )
    store.append_driver_repeat(
        store.repeat_session_key("b" * 32, "driver-fp"),
        target_id="mono:woofer:new-context",
        item=item,
    )

    snapshot = store.repeat_snapshot()["targets"]
    assert snapshot["mono:woofer"]["attempts"] == 1
    assert snapshot["mono:woofer:new-context"]["attempts"] == 1
    assert snapshot["mono:woofer"]["comparison_set_id"] != snapshot[
        "mono:woofer:new-context"
    ]["comparison_set_id"]


def test_controlled_ambient_intent_never_claims_capture_prefix(tmp_path):
    import wave

    import numpy as np
    from jasper.audio_measurement import sweep

    sample_rate = 48000
    rng = np.random.default_rng(7)
    samples = np.clip(
        rng.normal(0.0, 0.001, sample_rate * 3), -1.0, 1.0
    )
    wav_path = tmp_path / "capture.wav"
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes((samples * 32767.0).astype("<i2").tobytes())
    _reference, meta = sweep.synchronized_swept_sine(
        f1=20.0,
        f2=12000.0,
        duration_approx_s=1.0,
        sample_rate=sample_rate,
        amplitude_dbfs=-12.0,
    )

    report = web_measurement._stored_ambient_report(
        wav_path,
        meta.to_dict(),
        calibration=None,
        ambient_duration_s=2.0,
    )

    assert report is not None
    assert report["schema_version"] == 2
    assert report["domain"] == "controlled_pre_sweep"
    assert report["method"] == "paired_signal_window_deconvolution"
    assert report["source"] == {
        "kind": "pending_signal_boundary",
        "protocol_paused_duration_s": 2.0,
    }
    assert report["ambient_duration_s"] == 2.0
    assert "bands" not in report


def test_aborted_repeat_set_cannot_restart_without_new_level_context(
    monkeypatch
):
    store = backend.CrossoverLevelLease()
    store.record_repeat_failure(
        "mono:woofer",
        {
            "status": "aborted",
            "reason": "correction_service_restarted",
            "attempts": 3,
        },
    )
    monkeypatch.setattr(web_measurement, "load_output_topology", object)

    with pytest.raises(ValueError, match="run the driver level check again"):
        backend.record_driver_capture(
            {"speaker_group_id": "mono", "role": "woofer"},
            b"wav",
            repeat_store=store,
        )


def _stub_driver_capture_collaborators(monkeypatch, wav_path: Path) -> None:
    """Shared fakes for the two commissioning-bundle glue tests below.

    Mirrors test_driver_capture_records_through_active_speaker_layer's setup
    (capture_preset/capture_sweep_meta/capture_calibration/calibration_level/
    current_driver_floor_evidence), but leaves load_output_topology and
    load_measurement_state to each caller since those two are what
    distinguish the lazily-opened-bundle case from the reused-bundle case.
    """

    monkeypatch.setattr(
        web_measurement,
        "capture_preset",
        lambda _topology, supplied=None: supplied,
    )
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
    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(
        calibration_level,
        "load_calibration_level_state",
        lambda: {"level": "ok"},
    )
    confirmation = {
        "accepted": True,
        "playback_id": "play-1",
        "target": {"speaker_group_id": "mono", "role": "woofer", "output_index": 0},
    }
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_args, **_kwargs: {
            "valid": True,
            "source": "durable_current_driver_measurement",
            "confirmation": confirmation,
        },
    )


def test_driver_capture_appends_into_lazily_opened_bundle(monkeypatch, tmp_path):
    """No comparison set has stamped a bundle_session_id yet (a fresh
    measurement state): record_driver_capture must lazily open a new
    commissioning bundle via _resolve_bundle_for_capture and thread its
    bundle_ref through to both record_driver_acoustic_capture and the
    durable info.json/artifact-manifest evidence on disk."""

    import json

    from jasper.correction.bundles import read_artifact_manifest
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(sessions))

    topology = _topology()
    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)

    wav_path = tmp_path / "driver.wav"
    wav_path.write_bytes(b"\x00" * 64)
    _stub_driver_capture_collaborators(monkeypatch, wav_path)

    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.measurement as measurement

    # No stamped bundle_session_id -- forces the lazy-open path.
    monkeypatch.setattr(measurement, "load_measurement_state", lambda _t: {})

    seen_kwargs: dict = {}

    def fake_record(*_args, **kwargs):
        seen_kwargs.update(kwargs)
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

    bundle_dirs = [p for p in sessions.iterdir() if p.is_dir()]
    assert len(bundle_dirs) == 1
    bundle_dir = bundle_dirs[0]
    assert (bundle_dir / "info.json").is_file()

    bundle_ref = seen_kwargs.get("bundle_ref")
    assert isinstance(bundle_ref, dict)
    assert bundle_ref["session_id"] == bundle_dir.name

    dest = bundle_dir / bundle_ref["artifact_path"]
    assert dest.is_file()
    assert dest.read_bytes() == wav_path.read_bytes()

    manifest = read_artifact_manifest(bundle_dir)
    manifest_paths = {entry["path"] for entry in manifest["artifacts"]}
    assert bundle_ref["artifact_path"] in manifest_paths

    info = json.loads((bundle_dir / "info.json").read_text())
    assert len(info["captures"]) == 1
    capture_entry = info["captures"][0]
    assert capture_entry["group"] == "mono"
    assert capture_entry["role"] == "woofer"


def test_driver_capture_reuses_stamped_session_bundle(monkeypatch, tmp_path):
    """A comparison set already stamped a bundle_session_id (the normal case
    once start_active_comparison_set has run): record_driver_capture must
    append into THAT bundle rather than lazily opening a second one."""

    from jasper.active_speaker import bundles as active_speaker_bundles
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SESSIONS_DIR", str(sessions))
    sessions.mkdir(parents=True, exist_ok=True)

    topology = _topology()
    info = active_speaker_bundles.open_bundle(
        topology, calibration_id="cal-1", sessions_dir=sessions
    )
    assert info is not None

    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)

    wav_path = tmp_path / "driver.wav"
    wav_path.write_bytes(b"\x00" * 64)
    _stub_driver_capture_collaborators(monkeypatch, wav_path)

    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.measurement as measurement

    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _t: {
            "active_comparison_set": {"bundle_session_id": info["session_id"]},
        },
    )
    monkeypatch.setattr(
        capture, "record_driver_acoustic_capture", lambda *_a, **_k: {"recorded": True}
    )

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

    bundle_dirs = [p for p in sessions.iterdir() if p.is_dir()]
    assert len(bundle_dirs) == 1
    assert bundle_dirs[0].name == info["session_id"]

    bundle_dir = Path(info["bundle_dir"])
    assert list(bundle_dir.glob("captures/*.wav"))


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
    monkeypatch.setattr(
        backend,
        "commissioning_region_status",
        lambda: {"status": "collecting", "next_capture": {"evidence_kind": "server_selected"}},
    )

    payload = backend.status_payload()

    assert payload["commission"] == {"ramp": {"pending": None}}
    assert payload["region_commissioning"] == {
        "status": "collecting",
        "next_capture": {"evidence_kind": "server_selected"},
    }


def _commissioning_comparison(
    *,
    session_id: str = "session-1",
    comparison_id: str = "1" * 32,
) -> dict:
    comparison = {
        "schema_version": 2,
        "comparison_set_id": comparison_id,
        "created_at": "2026-07-14T12:00:00+00:00",
        "topology_id": "topology-1",
        "profile_context_id": "protected-profile",
        "setup_sha256": "3" * 64,
        "device_sha256": "4" * 64,
        "calibration_id": "",
        "driver_level_locks": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "speaker_group_id": "mono",
                "role": "woofer",
                "tone_frequency_hz": 100.0,
                "tone_peak_dbfs": -20.0,
                "commissioning_gain_db": 0.0,
                "locked_main_volume_db": -12.0,
            },
        },
    }
    comparison["fingerprint"] = comparison_set_fingerprint(comparison)
    comparison["bundle_session_id"] = session_id
    return comparison


def test_commissioning_run_status_is_current_only_for_exact_comparison(
    monkeypatch, tmp_path
):
    from jasper.active_speaker.commissioning_run import CommissioningRunStore

    store = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="1" * 32,
    )
    monkeypatch.setattr(backend, "_COMMISSIONING_RUN_STORE", store)
    comparison = _commissioning_comparison()
    other = _commissioning_comparison(
        session_id="session-2",
        comparison_id="2" * 32,
    )

    handle = backend.begin_commissioning_run(comparison)
    current = backend.commissioning_run_status(
        comparison,
        expected_topology_id="topology-1",
        expected_profile_context_id="protected-profile",
    )
    stale = backend.commissioning_run_status(
        other,
        expected_topology_id="topology-1",
        expected_profile_context_id="protected-profile",
    )

    assert current == {
        "status": "current",
        "reason": None,
        "profile_context_id": "protected-profile",
        "session_id": "session-1",
        "run_id": handle.run_id,
        "owner_generation": 1,
        "lifecycle_state": "unconfigured",
        "attempt_count": 0,
        "last_transition": None,
        "updated_at": current["updated_at"],
        "state_fingerprint": store.snapshot()["fingerprint"],
        "isolated_evidence": {
            "status": "unavailable",
            "reason": "isolated_evidence_state_unavailable",
            "error_type": "CommissioningEvidenceStoreError",
        },
    }
    assert stale["status"] == "stale"
    assert stale["reason"] == "commissioning_comparison_set_changed"
    assert stale["run_id"] == handle.run_id


def test_commissioning_run_status_refuses_malformed_hash_and_context_drift(
    monkeypatch, tmp_path
):
    from jasper.active_speaker.commissioning_run import CommissioningRunStore

    store = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="1" * 32,
    )
    monkeypatch.setattr(backend, "_COMMISSIONING_RUN_STORE", store)
    comparison = _commissioning_comparison()
    backend.begin_commissioning_run(comparison)
    malformed = {
        "bundle_session_id": comparison["bundle_session_id"],
        "fingerprint": comparison["fingerprint"],
    }
    hash_drift = {**comparison, "setup_sha256": "5" * 64}

    refused = (
        backend.commissioning_run_status(
            malformed,
            expected_topology_id="topology-1",
            expected_profile_context_id="protected-profile",
        ),
        backend.commissioning_run_status(
            hash_drift,
            expected_topology_id="topology-1",
            expected_profile_context_id="protected-profile",
        ),
        backend.commissioning_run_status(
            comparison,
            expected_topology_id="topology-2",
            expected_profile_context_id="protected-profile",
        ),
        backend.commissioning_run_status(
            comparison,
            expected_topology_id="topology-1",
            expected_profile_context_id="other-profile",
        ),
    )

    assert all(item["status"] == "stale" for item in refused)
    assert all(
        item["reason"] == "commissioning_comparison_set_changed"
        for item in refused
    )


def test_begin_commissioning_run_refuses_malformed_comparison(monkeypatch, tmp_path):
    from jasper.active_speaker.commissioning_run import CommissioningRunStore

    path = tmp_path / "commissioning-run.json"
    monkeypatch.setattr(
        backend,
        "_COMMISSIONING_RUN_STORE",
        CommissioningRunStore(path=path, owner_id="1" * 32),
    )

    with pytest.raises(ValueError, match="comparison set is invalid"):
        backend.begin_commissioning_run(
            {"bundle_session_id": "session-1", "fingerprint": "a" * 64}
        )

    assert not path.exists()


def test_commissioning_run_status_is_fail_closed_for_absent_and_corrupt_state(
    monkeypatch, tmp_path
):
    from jasper.active_speaker.commissioning_run import CommissioningRunStore

    path = tmp_path / "commissioning-run.json"
    store = CommissioningRunStore(path=path, owner_id="1" * 32)
    monkeypatch.setattr(backend, "_COMMISSIONING_RUN_STORE", store)

    absent = backend.commissioning_run_status(
        None,
        expected_topology_id="topology-1",
        expected_profile_context_id="protected-profile",
    )
    path.write_text("not json", encoding="utf-8")
    corrupt = backend.commissioning_run_status(
        None,
        expected_topology_id="topology-1",
        expected_profile_context_id="protected-profile",
    )

    assert absent["status"] == "not_started"
    assert absent["reason"] == "commissioning_run_not_started"
    assert corrupt == {
        "status": "unavailable",
        "reason": "commissioning_run_state_unavailable",
        "error_type": "CommissioningRunError",
    }


def test_crossover_module_is_a_thin_server_envelope_renderer():
    source = Path("deploy/assets/correction/js/crossover/main.js").read_text(
        encoding="utf-8",
    )

    assert "getJSON('/correction/crossover/envelope')" in source
    assert "getJSON('status')" not in source
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
    assert "schedulePoll(relayIsActive(env.relay) ? POLL_MS : null)" in source
    assert "postJSON('/correction/crossover/relay-cancel', {})" in source
    assert "env.alternate_actions" in source
    assert "baseline_candidate_fingerprint_mismatch" in source
    assert "candidateChanged" in source
    assert "A failed mutation may still have advanced durable authority" in source
    assert "await refresh();" in source

    # Single render authority for the action row (2026-07-16 hardware bug: a
    # primary action button rendered ungated in runAction()'s finally, so it
    # could appear beside the relay's own "Open phone capture" primary link).
    # renderActions() must have exactly one call site — inside
    # renderActionRow(), the sole function permitted to decide what the
    # action row shows — plus its own definition.
    assert source.count("renderActions(") == 2
    assert "function renderActionRow(env)" in source
    # render(), stopRelay()'s finally, and both of runAction()'s relay
    # touch-points (the optimistic hide, and the finally re-render) all route
    # through the one authority: definition + 4 call sites.
    assert source.count("renderActionRow(") == 5
    # The relay-in-flight predicate is centralized in one helper for the
    # action-row gate (renderRelay() keeps its own separate RELAY_IN_FLIGHT
    # check to decide the relay panel/QR/stop-button visibility — a different
    # DOM region, not the two-primary-buttons bug this test guards).
    assert "function relayIsActive(relay)" in source
    assert source.count("RELAY_IN_FLIGHT.has(") == 2


def test_fast_terminal_stop_reenables_the_authoritative_next_action():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = Path("tests/js/crossover_stop_render_test.mjs")
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"ok": True, "passed": 18}


def test_hidden_tab_slows_polling_instead_of_stopping():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = Path("tests/js/crossover_hidden_poll_test.mjs")
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"ok": True, "passed": 6}


def test_action_row_has_a_single_render_authority():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = Path("tests/js/crossover_action_row_authority_test.mjs")
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # 15 original cases + 3 W6.10 review-during-hold cases (Apply renders as
    # the single primary while the phone holds; connect link/QR suppressed).
    assert result == {"ok": True, "passed": 18}


def test_start_over_confirm_is_grouping_aware_and_partial_is_honest():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = Path("tests/js/crossover_start_over_test.mjs")
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"ok": True, "passed": 9}


def test_applied_chip_renders_server_state_and_hides_for_none():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = Path("tests/js/crossover_applied_chip_test.mjs")
    proc = subprocess.run(
        [node, str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"ok": True, "passed": 11}


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


def test_status_payload_reports_capture_entry_pending(monkeypatch, tmp_path):
    # Wires jasper.active_speaker.capture_entry_anchor.pending_entry() into
    # the envelope's inputs (see crossover_envelope._setup_ready) — the
    # capture-entry stash's own read fixture is autouse-isolated per test
    # (tests/conftest.py: _isolate_capture_entry_anchor).
    from jasper.active_speaker import capture_entry_anchor

    _status_with_targets(
        monkeypatch,
        drivers=[{"target_id": "mono:woofer"}],
        summed=[{"speaker_group_id": "mono"}],
    )

    assert backend.status_payload()["capture_entry_pending"] is False

    capture_entry_anchor.record_entry(str(tmp_path / "production.yml"))

    assert backend.status_payload()["capture_entry_pending"] is True


def test_new_level_run_drops_prior_in_memory_comparison_context(monkeypatch):
    lease = backend.CrossoverLevelLease()
    invalidations = []
    monkeypatch.setattr(
        lease._level_run_store,
        "invalidate_succeeded_result",
        lambda **kwargs: invalidations.append(kwargs),
    )
    lease._last = SimpleNamespace(snapshot=lambda: {"ramp": {"state": "locked"}})
    lease.context_id = "old-profile"
    lease.noise_floor_db = -42.0
    lease.mic_calibration = object()
    lease.input_device = {"label": "old mic"}
    lease.relay_setup_binding = object()

    lease.invalidate_comparison_context()

    assert lease.level_match_snapshot()["last"] is None
    assert lease.context_id is None
    assert lease.noise_floor_db is None
    assert lease.mic_calibration is None
    assert lease.input_device is None
    assert lease.relay_setup_binding is None
    assert invalidations == [{}]


def test_discard_driver_level_outcome_invalidates_matching_terminal_run(monkeypatch):
    lease = backend.CrossoverLevelLease()
    invalidations = []
    monkeypatch.setattr(
        lease._level_run_store,
        "invalidate_succeeded_result",
        lambda **kwargs: invalidations.append(kwargs),
    )

    lease.discard_driver_level_outcome(
        "mono",
        "woofer",
        capture_geometry="near_field",
    )

    assert invalidations == [
        {"geometry": "near_field_driver:mono:woofer"},
    ]


def test_discard_driver_level_outcome_restores_prior_continuation_context(
    monkeypatch,
):
    lease = backend.CrossoverLevelLease()
    monkeypatch.setattr(
        lease._level_run_store,
        "invalidate_succeeded_result",
        lambda **_kwargs: None,
    )
    prior = SimpleNamespace()
    discarded = SimpleNamespace()
    prior_geometry = "near_field_driver:mono:woofer"
    discarded_geometry = "reference_axis_driver:mono:tweeter"
    lease._outcomes = {
        prior_geometry: prior,
        discarded_geometry: discarded,
    }
    lease._last = discarded
    lease.context_id = "protected-profile-1"

    lease.discard_driver_level_outcome(
        "mono",
        "tweeter",
        capture_geometry="reference_axis",
    )

    assert lease._outcomes == {prior_geometry: prior}
    assert lease._last is prior
    assert lease.context_id == "protected-profile-1"

    lease.discard_driver_level_outcome(
        "mono",
        "woofer",
        capture_geometry="near_field",
    )

    assert lease._outcomes == {}
    assert lease._last is None
    assert lease.context_id is None


# --- crossover screen envelope: exactly one sequential next action -----------


def _envelope_status() -> dict:
    return {
        "active": True,
        "topology": {"topology_id": "topology-1", "status": "configured"},
        "setup": {
            "active": True,
            "status": "ready",
            "acoustic_commissioning": {"allowed": False},
            "baseline_profile": {
                "source_fingerprint": "source-1",
                "candidate_fingerprint": "manual-candidate",
                "revalidation": {"required": False},
            },
            "protected_profile": {
                "status": "ready",
                "source_fingerprint": "protected-profile",
                "candidate_fingerprint": "protected-profile",
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
                "candidate_fingerprint": "automatic-candidate",
            },
        },
        "targets": {
            "drivers": [
                {
                    "speaker_group_id": "mono",
                    "role": "woofer",
                    "target_fingerprint": "6" * 64,
                },
                {
                    "speaker_group_id": "mono",
                    "role": "tweeter",
                    "target_fingerprint": "7" * 64,
                },
            ],
            "summed": [{"speaker_group_id": "mono"}],
        },
        "measurements": {
            "active_comparison_set": _COMPARISON_SET,
            "summary": {},
        },
        "level_match": {"running": False, "last": None},
        "applied_profile": {},
        "relay": None,
    }


def _locked_level(status: dict) -> None:
    status["level_match"] = {
        "running": False,
        "valid": True,
        "ready": True,
        "context_id": "protected-profile",
        # The target remains reusable after the safe lifecycle restores normal
        # listening volume between sweep windows.
        "last": {"ramp": {"state": "locked", "restored": True}},
    }


def test_applied_candidate_keeps_exact_run_current_through_status_and_envelope(
    monkeypatch, tmp_path
):
    from jasper.active_speaker import (
        baseline_profile,
        crossover_envelope,
        design_draft as design_draft_module,
        repeat_admission,
        setup_status,
        web_commissioning,
    )
    from jasper.active_speaker.commissioning_run import CommissioningRunStore
    import jasper.output_topology as output_topology

    comparison = _commissioning_comparison()
    status = _envelope_status()
    status["measurements"]["active_comparison_set"] = comparison
    store = CommissioningRunStore(
        path=tmp_path / "commissioning-run.json",
        owner_id="1" * 32,
    )
    monkeypatch.setattr(backend, "_COMMISSIONING_RUN_STORE", store)
    backend.begin_commissioning_run(comparison)
    monkeypatch.setattr(output_topology, "load_output_topology", lambda: _topology())
    # A commissioning run cannot reach "current" (commissioning_service.py's
    # own precondition) unless the driver safety profile was already
    # confirmed-and-current; reflect that here rather than exercising the
    # real design-draft file this sandbox does not have.
    monkeypatch.setattr(
        design_draft_module,
        "load_design_draft",
        lambda **_kwargs: {
            "driver_safety_profile_evaluation": {
                "status": "confirmed",
                "confirmed_and_current": True,
                "profile_fingerprint": "a" * 64,
                "reasons": [],
                "authorizes_playback": False,
            }
        },
    )
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {
            "ok": True,
            "topology": status["topology"],
            "targets": status["targets"],
            "measurements": status["measurements"],
        },
    )
    monkeypatch.setattr(web_commissioning, "commission_status_payload", lambda: {})
    applied_setup = dict(status["setup"])
    applied_setup["protected_profile"] = {
        "status": "ready",
        "candidate_fingerprint": "newly-applied-profile",
    }
    applied_setup["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }
    monkeypatch.setattr(
        setup_status, "read_active_speaker_setup_status", lambda: applied_setup
    )
    monkeypatch.setattr(
        backend,
        "commissioning_region_status",
        lambda: {
            "status": "applied_unverified",
            "profile_context_id": comparison["profile_context_id"],
        },
    )
    monkeypatch.setattr(repeat_admission, "snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(backend, "_LEVEL_LEASE", backend.CrossoverLevelLease())
    run_status = backend.commissioning_run_status

    def complete_isolated(*args, **kwargs):
        result = run_status(*args, **kwargs)
        result["isolated_evidence"] = {"status": "complete"}
        return result

    monkeypatch.setattr(backend, "commissioning_run_status", complete_isolated)

    live = backend.status_payload()
    envelope = crossover_envelope.build_crossover_envelope(live)

    assert live["setup"]["protected_profile"]["candidate_fingerprint"] == (
        "newly-applied-profile"
    )
    assert live["commissioning_run"]["status"] == "current"
    assert live["commissioning_run"]["profile_context_id"] == comparison[
        "profile_context_id"
    ]
    assert live["region_commissioning"]["status"] == "applied_unverified"
    assert envelope["screen"] == "alignment"
    assert envelope["next_action"] == {
        "id": "measure_post_apply_verification",
        "label": "Verify combined response — capture 1",
        "endpoint": "/correction/crossover/relay-capture",
        "body": {"kind": "verification"},
    }
    assert "applied and freshly read back" in envelope["verdict_text"]


def test_crossover_envelope_exposes_only_explicit_volume_recovery():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["unresolved_volume_safety"] = {
        "status": "unresolved",
        "reason": "service_restarted_during_volume_transition",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "volume_recovery"
    assert env["next_action"] == {
        "id": "recover_volume",
        "label": "Recover safe listening volume",
        "endpoint": "/correction/crossover/recover-volume",
        "body": {},
    }
    assert env["alternate_actions"] == []


def test_repeat_rejection_nudge_distinguishes_infra_from_acoustic_failure():
    """An infra failure (no tone ever played) must not tell the operator to
    quiet the room — there is nothing acoustic to fix. Transport-phase
    failures (`_finish_failed_repeat_attempt` in correction_setup.py records
    `phase: "transport"`) get their own copy; everything else keeps the
    existing acoustic-rejection text."""
    from jasper.active_speaker import crossover_envelope

    def _durable_repeat_state(result: dict) -> dict:
        status = _envelope_status()
        status["level_match"]["repeats"] = {
            "targets": {},
            "failures": {},
            "durable": {
                "status": "active",
                "targets": {
                    "mono:woofer": {
                        "target_fingerprint": "6" * 64,
                        "status": "active",
                        "attempts": 1,
                        "results": [{"attempt": 1, "accepted": False, **result}],
                    },
                },
            },
        }
        return status

    infra_status = _durable_repeat_state({
        "reject_reason": "capture_failed",
        "failure_type": "RuntimeError",
        "phase": "transport",
    })
    infra_env = crossover_envelope.build_crossover_envelope(infra_status)
    infra_nudge = next(
        n for n in infra_env["nudges"] if n["code"] == "crossover_repeat_rejected"
    )
    assert infra_nudge["text"] == (
        "That attempt didn't finish on the speaker's side — nothing to fix "
        "in the room. Try again."
    )
    assert infra_nudge["severity"] == "warn"

    acoustic_status = _durable_repeat_state({
        "reject_reason": "insufficient_accepted_repeats",
    })
    acoustic_env = crossover_envelope.build_crossover_envelope(acoustic_status)
    acoustic_nudge = next(
        n for n in acoustic_env["nudges"] if n["code"] == "crossover_repeat_rejected"
    )
    assert acoustic_nudge["text"] == (
        "The latest sweep was not usable (insufficient accepted repeats). "
        "Keep the room quiet and retry."
    )


def test_repeat_rejection_nudge_clip_copy_is_honest_and_actionable():
    """W2.2 (hardware run 18): "reduce the input gain" was unactionable
    advice for a calibrated measurement mic with no gain control. The clip
    nudge now describes the automatic de-escalation behavior instead."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {},
        "durable": {
            "status": "active",
            "targets": {
                "mono:woofer": {
                    "target_fingerprint": "6" * 64,
                    "status": "active",
                    "attempts": 1,
                    "results": [{
                        "attempt": 1,
                        "accepted": False,
                        "reject_reason": "unusable_capture",
                        "clipping": True,
                        "estimated_snr_db": None,
                    }],
                },
            },
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)
    nudge = next(
        n for n in env["nudges"] if n["code"] == "crossover_repeat_rejected"
    )
    assert nudge["text"] == (
        "That sweep was too loud for the microphone at this distance. "
        "JTS will try again a bit quieter."
    )
    assert "input gain" not in nudge["text"].lower()


def test_repeat_rejection_nudge_clip_copy_does_not_promise_a_retry_when_terminal():
    """Hardware run 19: a NON-RESUMABLE repeat set ("The repeat sequence
    ended and cannot be resumed") rendered the clip nudge's "JTS will try
    again a bit quieter" alongside it -- a false promise, since the attempt
    budget was already spent and no automatic retry was coming. Once the
    owning target's repeat status is terminal (refused/aborted/malformed,
    or a completed set the controller never durably observed), the nudge
    must say what the household actually needs to do instead."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {},
        "durable": {
            "status": "active",
            "targets": {
                "mono:woofer": {
                    "target_fingerprint": "6" * 64,
                    "status": "refused",
                    "attempts": 4,
                    "results": [{
                        "attempt": 4,
                        "accepted": False,
                        "reject_reason": "unusable_capture",
                        "clipping": True,
                        "estimated_snr_db": None,
                    }],
                },
            },
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert "cannot be resumed" in env["verdict_text"]
    nudge = next(
        n for n in env["nudges"] if n["code"] == "crossover_repeat_rejected"
    )
    assert nudge["text"] == (
        "That sweep was too loud for the microphone at this distance. "
        "Run the driver level check again before measuring."
    )
    assert "try again" not in nudge["text"].lower()


_COMPARISON_SET = {
    "schema_version": 2,
    "comparison_set_id": "1" * 32,
    "created_at": "2026-07-11T12:00:00+00:00",
    "topology_id": "topology-1",
    "profile_context_id": "protected-profile",
    "setup_sha256": "3" * 64,
    "device_sha256": "4" * 64,
    "calibration_id": "",
    "driver_level_locks": {
        "mono:woofer": {
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "tone_frequency_hz": 100.0,
            "tone_peak_dbfs": -20.0,
            "commissioning_gain_db": 0.0,
            "locked_main_volume_db": -12.0,
        },
        "mono:tweeter": {
            "target_id": "mono:tweeter",
            "speaker_group_id": "mono",
            "role": "tweeter",
            "tone_frequency_hz": 1000.0,
            "tone_peak_dbfs": -20.0,
            "commissioning_gain_db": -2.0,
            "locked_main_volume_db": -14.0,
        },
    },
}
_COMPARISON_SET["fingerprint"] = comparison_set_fingerprint(_COMPARISON_SET)


def test_capture_context_revalidates_topology_profile_level_set_and_target():
    import copy

    status = _envelope_status()
    _locked_level(status)
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["level_match"]["run"] = {"terminal_reason": "state_unavailable"}
    status["targets"]["drivers"][0]["target_fingerprint"] = "target-woofer"
    kwargs = {
        "current_topology_id": _COMPARISON_SET["topology_id"],
        "expected_topology_id": _COMPARISON_SET["topology_id"],
        "expected_profile_context_id": _COMPARISON_SET["profile_context_id"],
        "expected_comparison_set": _COMPARISON_SET,
        "kind": "driver",
        "speaker_group_id": "mono",
        "role": "woofer",
        "capture_geometry": "near_field",
        "expected_target_fingerprint": "target-woofer",
    }

    flow.validate_current_capture_context(status, **kwargs)

    stale = copy.deepcopy(status)
    stale["setup"]["protected_profile"]["candidate_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_capture_context(stale, **kwargs)

    stale = copy.deepcopy(status)
    stale["level_match"]["context_id"] = "changed"
    with pytest.raises(ValueError, match="measurement level changed"):
        flow.validate_current_capture_context(stale, **kwargs)

    stale = copy.deepcopy(status)
    stale["targets"]["drivers"][0]["target_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="measurement target changed"):
        flow.validate_current_capture_context(stale, **kwargs)

    with pytest.raises(ValueError, match="topology changed"):
        flow.validate_current_capture_context(
            status,
            **{**kwargs, "current_topology_id": "changed"},
        )


def test_restarted_lease_can_arm_exact_fixed_axis_driver_after_relevel():
    from jasper.audio_measurement.ramp import RampState

    status = _envelope_status()
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["targets"]["drivers"][0]["target_fingerprint"] = "target-woofer"
    lease = backend.CrossoverLevelLease()
    lease.context_id = _COMPARISON_SET["profile_context_id"]
    lease.configure_targets([
        {
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "geometry": "near_field_driver:mono:woofer",
            "tone_frequency_hz": 250.0,
            "commissioning_gain_db": 0.0,
        }
    ])
    lease._outcomes["reference_axis_driver:mono:woofer"] = SimpleNamespace(
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            locked_main_volume_db=-18.0,
        )
    )
    status["level_match"] = lease.level_match_snapshot(
        current_context_id=_COMPARISON_SET["profile_context_id"]
    )
    assert status["level_match"]["ready"] is False

    kwargs = {
        "current_topology_id": _COMPARISON_SET["topology_id"],
        "expected_topology_id": _COMPARISON_SET["topology_id"],
        "expected_profile_context_id": _COMPARISON_SET["profile_context_id"],
        "expected_comparison_set": _COMPARISON_SET,
        "kind": "driver",
        "speaker_group_id": "mono",
        "role": "woofer",
        "capture_geometry": "reference_axis",
        "expected_target_fingerprint": "target-woofer",
    }

    flow.validate_current_capture_context(status, **kwargs)
    status["level_match"]["reference_axis_driver_locks"] = {}
    with pytest.raises(ValueError, match="measurement level changed"):
        flow.validate_current_capture_context(status, **kwargs)


def test_level_target_context_revalidates_before_tone():
    import copy

    status = _envelope_status()
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["targets"]["drivers"][0]["target_fingerprint"] = "target-woofer"
    kwargs = {
        "current_topology_id": "topology-1",
        "expected_topology_id": "topology-1",
        "expected_profile_context_id": "protected-profile",
        "speaker_group_id": "mono",
        "role": "woofer",
        "expected_target_fingerprint": "target-woofer",
    }

    flow.validate_current_level_target_context(status, **kwargs)

    changed = copy.deepcopy(status)
    changed["setup"]["protected_profile"]["candidate_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_level_target_context(changed, **kwargs)

    changed = copy.deepcopy(status)
    changed["targets"]["drivers"][0]["target_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="driver level target changed"):
        flow.validate_current_level_target_context(changed, **kwargs)


def test_level_target_context_admits_mid_sequence_anchor():
    # Run-11 repro (gate #4 of the class): tweeter tone-prep calls
    # validate_current_level_target_context mid-sequence, when the persisted
    # config is (by #1523 design) the all-muted staged anchor -- setup reads
    # blocked/active_speaker_commissioning_config_loaded while the
    # fingerprint and applied_crossover checks pass. The raw ready
    # requirement raised the MISLEADING "protected crossover setup changed
    # after this link was created" even though nothing changed.
    #
    # Confirmed FAILING pre-fix: without the shared predicate in this
    # validator, the first call below raises that exact ValueError
    # (verified by running this test against the pre-fix validator).
    import copy

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_speaker_commissioning_config_loaded"
    status["capture_entry_pending"] = True
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["targets"]["drivers"][0]["target_fingerprint"] = "target-woofer"
    kwargs = {
        "current_topology_id": "topology-1",
        "expected_topology_id": "topology-1",
        "expected_profile_context_id": "protected-profile",
        "speaker_group_id": "mono",
        "role": "woofer",
        "expected_target_fingerprint": "target-woofer",
    }

    flow.validate_current_level_target_context(status, **kwargs)

    # A different blocked reason (even with the stash) refuses as before.
    changed = copy.deepcopy(status)
    changed["setup"]["reason"] = "active_baseline_profile_unreadable"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_level_target_context(changed, **kwargs)

    # The in-sequence reason WITHOUT a pending stash refuses as before.
    changed = copy.deepcopy(status)
    changed["capture_entry_pending"] = False
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_level_target_context(changed, **kwargs)

    # Copy accuracy: post-fix, the "setup changed" error still fires for the
    # genuine changed-underneath case -- a profile fingerprint that really
    # did change after the link was created, even mid-anchor.
    changed = copy.deepcopy(status)
    changed["setup"]["protected_profile"]["candidate_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_level_target_context(changed, **kwargs)


def test_capture_context_admits_mid_sequence_anchor():
    # Arm-time sweep equivalent of the tone-prep repro: the phone arms
    # minutes after POST, mid-sequence, while the persisted config is the
    # staged anchor. validate_current_capture_context must admit exactly the
    # in-sequence state and keep refusing every genuine change.
    import copy

    status = _mid_sequence_sweep_status()
    status["level_match"]["run"] = {"terminal_reason": "state_unavailable"}
    status["targets"]["drivers"][0]["target_fingerprint"] = "target-woofer"
    kwargs = {
        "current_topology_id": _COMPARISON_SET["topology_id"],
        "expected_topology_id": _COMPARISON_SET["topology_id"],
        "expected_profile_context_id": _COMPARISON_SET["profile_context_id"],
        "expected_comparison_set": _COMPARISON_SET,
        "kind": "driver",
        "speaker_group_id": "mono",
        "role": "woofer",
        "capture_geometry": "near_field",
        "expected_target_fingerprint": "target-woofer",
    }

    flow.validate_current_capture_context(status, **kwargs)

    changed = copy.deepcopy(status)
    changed["setup"]["reason"] = "active_baseline_profile_unreadable"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_capture_context(changed, **kwargs)

    changed = copy.deepcopy(status)
    changed["capture_entry_pending"] = False
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_capture_context(changed, **kwargs)

    changed = copy.deepcopy(status)
    changed["setup"]["protected_profile"]["candidate_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="protected crossover setup changed"):
        flow.validate_current_capture_context(changed, **kwargs)


def test_setup_readiness_gates_share_the_in_sequence_anchor_predicate():
    """Gate-class closure pin (runs 10-11; PRs #1525/#1526 + this one).

    Four separate gates each carried a raw `setup.get("status") != "ready"`
    comparison against the active-speaker setup payload, and each one
    independently wedged an automatic capture sequence while the persisted
    config was (by #1523 design) the all-muted staged anchor. Any raw
    readiness comparison in these files must either consult
    setup_blocked_only_by_in_sequence_anchor in the same statement or carry
    an explicit `in-sequence-anchor-exempt` justification comment -- so
    gate #5 of the class can never land silently.
    """
    import re
    from pathlib import Path

    import jasper

    root = Path(jasper.__file__).parent
    files = (
        root / "web" / "correction_setup.py",
        root / "web" / "correction_crossover_flow.py",
        root / "web" / "correction_crossover_backend.py",
        root / "active_speaker" / "crossover_envelope.py",
    )
    # Matches the setup-status dict's status compared to "ready"
    # (setup/raw_setup/setup_status variables, .get() or subscript). Scoped
    # to those variable names so unrelated "ready" statuses (excitation,
    # signal plans, repeat entries, level_match.ready) never false-positive.
    pattern = re.compile(
        r'\b(?:raw_)?setup(?:_status)?'
        r'(?:\.get\(\s*"status"\s*\)|\[\s*"status"\s*\])'
        r'\s*[!=]=\s*"ready"'
    )
    offenders = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            # The predicate must be consulted in the SAME condition — look
            # forward only (a nearby import or comment naming it must not
            # satisfy the pin; a reverted gate under an intact import would
            # otherwise slip through).
            forward = "\n".join(lines[i:i + 6])
            if "setup_blocked_only_by_in_sequence_anchor" in forward:
                continue
            # A justified exclusion is an explicit marker in the comment
            # block directly above the comparison.
            preceding_comments = []
            for prior in reversed(lines[max(0, i - 12):i]):
                stripped = prior.strip()
                if stripped.startswith("#"):
                    preceding_comments.append(stripped)
                elif stripped:
                    break
            if any(
                "in-sequence-anchor-exempt" in comment
                for comment in preceding_comments
            ):
                continue
            offenders.append(f"{path.name}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "raw active-speaker setup readiness comparison without the shared "
        "in-sequence-anchor predicate (wire it, or justify with an "
        "'in-sequence-anchor-exempt' comment):\n" + "\n".join(offenders)
    )


def test_automatic_candidate_requires_driver_evidence_not_summed_capture():
    from jasper.active_speaker.crossover_contract import (
        automatic_candidate_readiness,
    )

    readiness = automatic_candidate_readiness(
        required_group_ids=["mono"],
        level_match={
            "applied": True,
            "groups_measured": 1,
            "measured_group_ids": ["mono"],
            "incomparable_groups": [],
        },
        measurement_summary={},
        active_comparison_set={},
    )

    assert readiness["ready"] is True
    assert readiness["summed_group_ids"] == []
    assert "frequency and slope remain operator-owned" in readiness["detail"]


def _placement_proof(policy: str, role: str, target_fingerprint: str) -> dict:
    return {
        "schema_version": 1,
        "policy_id": policy,
        "accepted": True,
        "confirmation_source": "relay_begin_capture",
        "acknowledgement_binding_sha256": "5" * 64,
        "relay_session_id": f"relay-{role}",
        "capture_protocol_version": 2,
        "capture_page_build": "20260711.1",
        "speaker_group_id": "mono",
        "role": role,
        "target_fingerprint": target_fingerprint,
        "captured": True,
        "comparison_set_id": _COMPARISON_SET["comparison_set_id"],
        "comparison_set_fingerprint": _COMPARISON_SET["fingerprint"],
    }


def _driver_acoustic(role: str) -> dict:
    target_fingerprint = ("6" if role == "woofer" else "7") * 64
    return {
        "speaker_group_id": "mono",
        "role": role,
        "target_fingerprint": target_fingerprint,
        "captured": True,
        "mic_clipping": False,
        "repeats": {
            "target": 3,
            "accepted": 3,
            "admission_attempts": 3,
        },
        "acoustic": {
            "verdict": "present",
            "capture_geometry": "near_field",
            "mic_clipping": False,
            "gating": {
                "applied": False,
                "exempt_reason": "near_field",
                "f_valid_floor_hz": None,
            },
            "overlap_levels": [{
                "region_id": "woofer_tweeter",
                "above_validity_floor": True,
                "usable": True,
            }],
        },
        "placement_proof": _placement_proof(
            "driver_same_distance_v1",
            role,
            target_fingerprint,
        ),
    }


def _reference_axis_driver_acoustic(role: str) -> dict:
    target_fingerprint = ("6" if role == "woofer" else "7") * 64
    return {
        "speaker_group_id": "mono",
        "role": role,
        "target_fingerprint": target_fingerprint,
        "captured": True,
        "mic_clipping": False,
        "repeats": {
            "target": 3,
            "accepted": 3,
            "admission_attempts": 3,
        },
        "acoustic": {
            "verdict": "present",
            "capture_geometry": "reference_axis",
            "mic_clipping": False,
            "gating": {"applied": True, "f_valid_floor_hz": 320.0},
            "overlap_levels": [
                {
                    "region_id": "woofer_tweeter",
                    "above_validity_floor": True,
                    "usable": True,
                }
            ],
        },
        "placement_proof": _placement_proof(
            "driver_reference_axis_v1",
            role,
            target_fingerprint,
        ),
    }


def _complete_reference_axis(status: dict) -> None:
    status["measurements"]["summary"][
        "latest_reference_axis_driver_measurements"
    ] = {
        "mono:woofer": _reference_axis_driver_acoustic("woofer"),
        "mono:tweeter": _reference_axis_driver_acoustic("tweeter"),
    }


def _lock_reference_axis_driver(status: dict, role: str) -> None:
    status["level_match"].setdefault("reference_axis_driver_locks", {})[
        f"mono:{role}"
    ] = -12.0


def _completed_near_field_repeat_state(status: dict) -> None:
    targets = {
        f"mono:{role}": {
            "target_fingerprint": ("6" if role == "woofer" else "7") * 64,
            "status": "completed",
            "attempts": 3,
            "results": [
                {"attempt": attempt, "accepted": True}
                for attempt in (1, 2, 3)
            ],
        }
        for role in ("woofer", "tweeter")
    }
    status["level_match"]["repeats"] = {
        "targets": targets,
        "failures": {},
        "durable": {"status": "active", "targets": targets},
    }


def _completed_reference_axis_repeat_state(status: dict) -> None:
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    repeats = status["level_match"].setdefault(
        "repeats", {"targets": {}, "failures": {}}
    )
    targets = repeats.setdefault("targets", {})
    durable = repeats.setdefault("durable", {"status": "active", "targets": targets})
    durable_targets = durable.setdefault("targets", targets)
    for role in ("woofer", "tweeter"):
        target_id, target_fingerprint = driver_repeat_binding(
            speaker_group_id="mono",
            role=role,
            target_fingerprint=("6" if role == "woofer" else "7") * 64,
            capture_geometry="reference_axis",
        )
        entry = {
            "target_fingerprint": target_fingerprint,
            "status": "completed",
            "attempts": 3,
            "results": [
                {"attempt": attempt, "accepted": True}
                for attempt in (1, 2, 3)
            ],
        }
        targets[target_id] = entry
        durable_targets[target_id] = entry


def _summed_acoustic() -> dict:
    group_fingerprint = "8" * 64
    return {
        "speaker_group_id": "mono",
        "group_fingerprint": group_fingerprint,
        "validated": True,
        "acoustic": {"verdict": "blend_ok"},
        "placement_proof": _placement_proof(
            "summed_reference_axis_v1",
            "summed",
            group_fingerprint,
        ),
    }


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
    assert env["schema_version"] == 6


def test_crossover_envelope_requires_protected_setup_first():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["href"] == "/sound/"
    assert env["next_action"]["id"] == "speaker_setup"


def _incomplete_driver_safety_evaluation() -> dict:
    # Mirrors the JTS3 hardware shape (docs/HANDOFF-active-crossover-...):
    # status="incomplete", authorizes_playback=False, confirmation=None, with
    # BLOCKER issues naming the missing woofer bands/limits.
    return {
        "status": "incomplete",
        "confirmed_and_current": False,
        "profile_fingerprint": "9" * 64,
        "reasons": [
            "woofer:hard_excitation_band_missing",
            "woofer:measurement_band_missing",
            "woofer:crossover_search_band_missing",
            "woofer:level_duration_limits_missing",
        ],
        "authorizes_playback": False,
    }


def test_crossover_envelope_gates_on_incomplete_driver_safety_profile():
    # JTS3 evidence: setup_ready (protected setup applied) was true while the
    # driver safety profile self-described as incomplete/unauthorized. The
    # envelope must not offer any measurement action in that state -- the
    # deep excitation admission only refuses it later, after locks and
    # acceptance repeats were already spent.
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["driver_safety_profile_evaluation"] = _incomplete_driver_safety_evaluation()

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "speaker_setup"
    assert env["next_action"] == {
        "id": "speaker_setup",
        "label": "Finish speaker setup",
        "href": "/sound/",
    }
    assert env["alternate_actions"] == []
    step_by_id = {step["id"]: step for step in env["steps"]}
    assert step_by_id["speaker_setup"]["status"] != "done"
    assert any(
        "Woofer" in nudge["text"] and "speaker setup" in nudge["text"]
        for nudge in env["nudges"]
    )
    # Language guide: no internal vocabulary (field/status jargon) leaks into
    # the plain-language copy.
    for nudge in env["nudges"]:
        lowered = nudge["text"].lower()
        for banned in ("fingerprint", "authority", "candidate", "authorizes"):
            assert banned not in lowered


def test_crossover_envelope_gates_when_driver_safety_profile_missing():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["driver_safety_profile_evaluation"] = None

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["id"] == "speaker_setup"
    assert env["alternate_actions"] == []


def test_crossover_envelope_authorized_driver_safety_profile_is_unchanged():
    # No-regression pin: a confirmed-and-current profile behaves exactly like
    # a caller that omits the key entirely (legacy/pre-gate status shape).
    from jasper.active_speaker import crossover_envelope

    baseline_env = crossover_envelope.build_crossover_envelope(_envelope_status())

    authorized_status = _envelope_status()
    authorized_status["driver_safety_profile_evaluation"] = {
        "status": "confirmed",
        "confirmed_and_current": True,
        "profile_fingerprint": "9" * 64,
        "reasons": [],
        "authorizes_playback": False,
    }
    authorized_env = crossover_envelope.build_crossover_envelope(authorized_status)

    assert authorized_env == baseline_env


def test_crossover_envelope_mid_sequence_anchor_is_not_unfinished_setup():
    # THE REPRO — JTS3 punch #24. PR #1523 intentionally keeps the persisted
    # CamillaDSP path anchored on the all-muted staged config *between*
    # capture attempts within one automatic measurement sequence (crash-safe
    # posture). read_active_speaker_setup_status() correctly reports
    # setup blocked/active_speaker_commissioning_config_loaded while
    # anchored -- but composed literally with #1511's gate, that forced
    # screen=speaker_setup permanently after the very first driver lock,
    # with no flow-owned recovery (only exit invalidated the locks already
    # captured). Mirrors test_crossover_envelope_walks_level_drivers_apply_room's
    # "after woofer lock 1" step, but with the mid-sequence-anchored setup
    # status instead of "ready".
    #
    # Confirmed FAILING before the fix: without the capture_entry_pending
    # carve-out in crossover_envelope._setup_ready, this instead asserts
    # env["screen"] == "speaker_setup" (verified by running this test
    # against the pre-fix _setup_ready).
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_speaker_commissioning_config_loaded"
    status["capture_entry_pending"] = True
    _locked_level(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"]["body"] == {
        "kind": "driver",
        "speaker_group_id": "mono",
        "role": "woofer",
    }
    step_by_id = {step["id"]: step for step in env["steps"]}
    assert step_by_id["speaker_setup"]["status"] == "done"


def test_crossover_envelope_commissioning_config_loaded_without_stash_still_gates():
    # Same blocked reason as the repro, but with no pending capture-entry
    # stash (a speaker that never de-anchored, or a caller/test that
    # predates this key) -- must gate exactly as before the fix.
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_speaker_commissioning_config_loaded"
    _locked_level(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "speaker_setup"
    assert env["next_action"] == {
        "id": "speaker_setup",
        "label": "Finish speaker setup",
        "href": "/sound/",
    }


def test_crossover_envelope_different_blocked_reason_with_stash_still_gates():
    # The carve-out is scoped to the exact
    # active_speaker_commissioning_config_loaded reason. Any other blocked
    # reason gates normally even with a capture-entry stash pending -- a
    # pending stash from a prior sequence must never paper over a
    # genuinely-unfinished setup.
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_baseline_profile_unreadable"
    status["capture_entry_pending"] = True
    _locked_level(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["id"] == "speaker_setup"


def test_crossover_envelope_mid_sequence_anchor_still_gates_on_incomplete_driver_safety():
    # The mid-sequence-anchor carve-out only bypasses the setup-status
    # check; #1511's driver-safety-profile gate is untouched (per the task
    # scope -- do not touch it) and still wins even while a capture-entry
    # stash is pending, matching
    # test_crossover_envelope_gates_on_incomplete_driver_safety_profile.
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_speaker_commissioning_config_loaded"
    status["capture_entry_pending"] = True
    status["driver_safety_profile_evaluation"] = _incomplete_driver_safety_evaluation()

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "speaker_setup"
    assert env["next_action"] == {
        "id": "speaker_setup",
        "label": "Finish speaker setup",
        "href": "/sound/",
    }
    assert env["alternate_actions"] == []


def test_crossover_apply_requires_explicit_owner(monkeypatch):
    from jasper.active_speaker import safe_playback
    from jasper.web import correction_crossover_backend as backend

    seen = {}

    async def fake_apply_profile(
        *, tuning_owner, expected_candidate_fingerprint, camilla_factory
    ):
        seen["owner"] = tuning_owner
        seen["fingerprint"] = expected_candidate_fingerprint
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(backend, "apply_profile", fake_apply_profile)
    monkeypatch.setattr(
        safe_playback,
        "stop_safe_playback_session",
        lambda **kwargs: {
            "status": "idle",
            "session_id": None,
            "last_action": kwargs["reason"],
        },
    )

    def run_async(awaitable, *, timeout):
        import asyncio

        assert timeout == 30.0
        return asyncio.run(awaitable)

    refused, refused_status = flow.handle_apply({}, run_async, lambda: object())
    assert refused_status == 400
    assert refused["status"] == "refused"

    payload, status = flow.handle_apply(
        {
            "tuning_owner": "automatic",
            "expected_candidate_fingerprint": "reviewed-candidate",
        }, run_async, lambda: object()
    )
    assert status == 200
    assert payload["status"] == "applied"
    assert seen["owner"] == "automatic"
    assert seen["fingerprint"] == "reviewed-candidate"


def test_crossover_apply_refuses_while_relay_measurement_is_active(monkeypatch):
    from jasper.web import correction_crossover_backend as backend

    monkeypatch.setattr(
        backend,
        "apply_profile",
        lambda **_kwargs: pytest.fail("apply must not start during measurement"),
    )

    payload, status = flow.handle_apply(
        {"tuning_owner": "automatic"},
        lambda *_args, **_kwargs: pytest.fail("async apply must not run"),
        lambda: object(),
        blocking_phase="relay:crossover_sweep:driver",
    )

    assert status == 409
    assert payload["status"] == "refused"
    assert payload["reason"] == "measurement_in_progress"
    # A top-level `error` is required so the shared JS parser
    # (assets/shared/js/http.js parseResponse) surfaces this sentence instead
    # of falling back to a bare "HTTP 409".
    assert payload["error"] == payload["next_step"]
    assert payload["error"] == (
        "Finish the active measurement before applying the crossover."
    )


@pytest.mark.asyncio
async def test_automatic_apply_does_not_fall_back_past_strict_candidate_authority(
    monkeypatch,
):
    from jasper.web import correction_crossover_backend as backend

    monkeypatch.setattr(
        backend._LEVEL_LEASE,
        "assert_volume_safety_resolved",
        lambda: None,
    )
    monkeypatch.setattr(
        backend,
        "_commissioning_capture_service",
        lambda: (_ for _ in ()).throw(ValueError("candidate artifact unreadable")),
    )
    monkeypatch.setattr(
        backend._COMMISSIONING_RUN_STORE,
        "snapshot",
        lambda: {"current": {"lifecycle_state": "candidate_ready"}},
    )

    with pytest.raises(ValueError, match="strict commissioning candidate"):
        await backend.apply_profile(
            tuning_owner="automatic",
            expected_candidate_fingerprint="reviewed-candidate",
            camilla_factory=lambda: pytest.fail("DSP mutation must not start"),
        )


@pytest.mark.asyncio
async def test_automatic_apply_does_not_use_legacy_evidence_during_strict_run(
    monkeypatch,
):
    from jasper.web import correction_crossover_backend as backend

    monkeypatch.setattr(
        backend._LEVEL_LEASE,
        "assert_volume_safety_resolved",
        lambda: None,
    )
    monkeypatch.setattr(
        backend,
        "_commissioning_capture_service",
        lambda: SimpleNamespace(
            run="strict-run",
            run_store=SimpleNamespace(lifecycle_state=lambda _run: "measured"),
        ),
    )

    with pytest.raises(ValueError, match="reviewed strict candidate"):
        await backend.apply_profile(
            tuning_owner="automatic",
            expected_candidate_fingerprint="legacy-candidate",
            camilla_factory=lambda: pytest.fail("DSP mutation must not start"),
        )


def test_direct_crossover_audio_actions_include_active_relay_blocker(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(
        correction_setup,
        "_active_relay_phase",
        lambda: "relay:level_ramp:crossover",
    )
    monkeypatch.setattr(
        correction_setup,
        "_crossover_blocking_phase",
        lambda: pytest.fail("relay blocker should win without a second probe"),
    )

    assert correction_setup._crossover_direct_audio_blocking_phase() == (
        "relay:level_ramp:crossover"
    )


def test_direct_driver_test_dispatch_refuses_active_relay_before_audio(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(correction_setup, "_read_json_body", lambda _handler: {})
    monkeypatch.setattr(
        correction_setup,
        "_active_relay_phase",
        lambda: "relay:crossover_sweep:driver",
    )

    def handle_driver_test(_raw, _run_async, _camilla, *, blocking_phase):
        assert blocking_phase == "relay:crossover_sweep:driver"
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
        }, 409

    monkeypatch.setattr(flow, "handle_driver_test", handle_driver_test)
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/driver-test")

    assert sent == [({
        "status": "refused",
        "reason": "measurement_in_progress",
    }, 409)]


@pytest.mark.parametrize(
    "tuning_owner",
    ["manual", "automatic"],
)
def test_explicit_crossover_apply_releases_room_correction_lock(
    tmp_path, monkeypatch, tuning_owner
):
    """The terminal explicit apply closes commissioning's durable SSOT lock."""
    import json
    import time

    from jasper.web import active_speaker_flow, correction_crossover_backend
    from jasper.web import balance_flow, correction_setup, sync_flow

    state_path = tmp_path / "safe-playback.json"
    state_path.write_text(
        json.dumps(
            {
                "artifact_schema_version": 1,
                "kind": "jts_active_speaker_safe_playback_session",
                "status": "armed",
                "session_id": "safe-1",
                "created_at": "2026-07-11T11:00:00Z",
                "expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120)
                ),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE", str(state_path)
    )
    assert active_speaker_flow.active_phase() == "commissioning"

    async def fake_apply_profile(
        *, tuning_owner, expected_candidate_fingerprint, camilla_factory
    ):
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(
        correction_crossover_backend, "apply_profile", fake_apply_profile
    )
    payload, status = flow.handle_apply(
        {
            "tuning_owner": tuning_owner,
            "expected_candidate_fingerprint": f"{tuning_owner}-candidate",
        },
        lambda awaitable, *, timeout: _run_coro(awaitable),
        lambda: object(),
    )

    assert status == 200
    assert payload["commissioning_session"] == {
        "status": "stopped",
        "session_id": "safe-1",
        "last_action": "crossover_profile_applied",
    }
    assert active_speaker_flow.active_phase() is None

    monkeypatch.setattr(balance_flow, "active_phase", lambda: None)
    monkeypatch.setattr(sync_flow, "active_phase", lambda: None)
    monkeypatch.setattr(correction_setup, "_start_in_progress", False)
    monkeypatch.setattr(correction_setup, "_session", None)
    assert correction_setup._reserve_start_slot() is None
    correction_setup._clear_start_slot()


def test_failed_crossover_apply_preserves_commissioning_session(monkeypatch):
    from jasper.active_speaker import safe_playback
    from jasper.web import correction_crossover_backend as backend

    async def fake_apply_profile(
        *, tuning_owner, expected_candidate_fingerprint, camilla_factory
    ):
        return {"status": "blocked", "issues": [{"code": "missing_evidence"}]}

    monkeypatch.setattr(backend, "apply_profile", fake_apply_profile)
    monkeypatch.setattr(
        safe_playback,
        "stop_safe_playback_session",
        lambda **kwargs: pytest.fail("blocked apply must keep commissioning armed"),
    )

    payload, status = flow.handle_apply(
        {
            "tuning_owner": "automatic",
            "expected_candidate_fingerprint": "automatic-candidate",
        },
        lambda awaitable, *, timeout: _run_coro(awaitable),
        lambda: object(),
    )

    assert status == 409
    assert payload["status"] == "blocked"


def test_crossover_apply_close_failure_is_truthful_and_retryable(monkeypatch):
    from jasper.active_speaker import safe_playback
    from jasper.web import correction_crossover_backend as backend

    async def fake_apply_profile(
        *, tuning_owner, expected_candidate_fingerprint, camilla_factory
    ):
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(backend, "apply_profile", fake_apply_profile)

    def fail_close(**kwargs):
        raise OSError("read-only state filesystem")

    monkeypatch.setattr(safe_playback, "stop_safe_playback_session", fail_close)

    payload, status = flow.handle_apply(
        {
            "tuning_owner": "automatic",
            "expected_candidate_fingerprint": "automatic-candidate",
        },
        lambda awaitable, *, timeout: _run_coro(awaitable),
        lambda: object(),
    )

    assert status == 409
    assert payload["status"] == "applied"
    assert payload["commissioning_session"] == {
        "status": "close_failed",
        "last_action": "crossover_profile_applied",
    }
    assert payload["issues"][0]["code"] == "crossover_commissioning_close_failed"
    assert payload["issues"][0]["severity"] == "blocker"


def test_crossover_envelope_walks_level_drivers_apply_room():
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
    summary["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer")
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["next_action"]["body"]["role"] == "tweeter"

    summary["latest_driver_measurements"]["mono:tweeter"] = _driver_acoustic(
        "tweeter"
    )
    _completed_near_field_repeat_state(status)
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["next_action"] == {
        "id": "level_match_reference_axis_driver",
        "label": "Set fixed-axis woofer microphone level",
        "endpoint": "/correction/crossover/level-match",
        "body": {
            "capture_geometry": "reference_axis",
            "speaker_group_id": "mono",
            "role": "woofer",
        },
    }
    status["level_match"]["reference_axis_driver_locks"] = {
        "mono:woofer": -10.0,
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "driver_reference_axis"
    assert env["next_action"]["body"]["capture_geometry"] == "reference_axis"
    summary["latest_reference_axis_driver_measurements"] = {
        "mono:woofer": _reference_axis_driver_acoustic("woofer"),
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["next_action"]["id"] == "level_match_reference_axis_driver"
    assert env["next_action"]["body"]["role"] == "tweeter"
    status["level_match"]["reference_axis_driver_locks"]["mono:tweeter"] = -12.0
    summary["latest_reference_axis_driver_measurements"]["mono:tweeter"] = (
        _reference_axis_driver_acoustic("tweeter")
    )
    _completed_reference_axis_repeat_state(status)
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
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
            "level_match": {
                "active_comparison_set_id": _COMPARISON_SET[
                    "comparison_set_id"
                ],
            },
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


def test_crossover_envelope_projects_active_owned_alignment_actions():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["commissioning_run"] = {
        "status": "current",
        "isolated_evidence": {"status": "complete"},
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["region_commissioning"] = {
        "status": "needs_geometry",
        "next_geometry": {
            "target_fingerprint": "f" * 64,
            "lower_role": "woofer",
            "upper_role": "tweeter",
            "fc_hz": 2_100.0,
        },
    }

    geometry = crossover_envelope.build_crossover_envelope(status)

    assert geometry["progress"] == {"position": 4, "total": 5}
    assert next(
        step for step in geometry["steps"] if step["id"] == "apply"
    )["status"] == "pending"
    assert geometry["next_action"] == {
        "id": "attest_region_geometry",
        "label": "Confirm signed geometry",
        "endpoint": "/correction/crossover/region-geometry",
        "body": {"expected_target_fingerprint": "f" * 64},
        "fields": [
            {
                "name": "signed_acoustic_path_difference_mm",
                "label": "woofer path minus tweeter path (mm)",
                "type": "number",
                "step": "0.1",
                "required": True,
            }
        ],
    }

    status["region_commissioning"] = {
        "status": "collecting",
        "next_capture": {"evidence_kind": "server_selected"},
    }
    capture = crossover_envelope.build_crossover_envelope(status)
    assert capture["progress"] == {"position": 4, "total": 5}
    assert capture["next_action"] == {
        "id": "measure_region_alignment",
        "label": "Measure next server-selected combined response",
        "endpoint": "/correction/crossover/relay-capture",
        "body": {"kind": "summed"},
    }
    assert "JTS chooses everything else" in capture["verdict_text"]

    status["region_commissioning"] = {"status": "measured"}
    measured = crossover_envelope.build_crossover_envelope(status)
    assert measured["screen"] == "review"
    assert measured["next_action"] == {
        "id": "prepare_measured_candidate",
        "label": "Prepare measured candidate",
        "endpoint": "/correction/crossover/candidate",
        "body": {},
    }
    assert next(
        step for step in measured["steps"] if step["id"] == "apply"
    )["status"] == "active"

    review = {
        "fingerprint": "candidate-1",
        "retained_crossover_regions": [{"fc_hz": 2_100.0}],
        "drivers": [{"role": "woofer"}, {"role": "tweeter"}],
    }
    status["region_commissioning"] = {
        "status": "candidate_ready",
        "candidate": review,
    }
    ready = crossover_envelope.build_crossover_envelope(status)
    assert ready["screen"] == "review"
    assert ready["next_action"] == {
        "id": "apply_measured_candidate",
        "label": "Apply reviewed crossover",
        "endpoint": "/correction/crossover/apply",
        "body": {
            "tuning_owner": "automatic",
            "expected_candidate_fingerprint": "candidate-1",
        },
    }
    assert ready["candidate_review"] == review
    assert "Frequency, filter family, and order stay" in ready["verdict_text"]

    status["region_commissioning"] = {"status": "verification_failed"}
    failed = crossover_envelope.build_crossover_envelope(status)
    assert failed["screen"] == "review"
    assert failed["next_action"] == {
        "id": "edit_after_verification_failure",
        "label": "Back to speaker setup",
        "href": "/sound/",
    }
    assert "Room correction remains locked" in failed["verdict_text"]

    status["region_commissioning"] = {
        "status": "verified",
        "verification": {"receipt": {"fingerprint": "a" * 64}},
    }
    verified = crossover_envelope.build_crossover_envelope(status)
    assert verified["screen"] == "done"
    assert verified["next_action"] == {
        "id": "room",
        "label": "Continue to Room correction",
        "href": "/correction/room/",
    }
    assert all(step["status"] == "done" for step in verified["steps"])
    assert verified["progress"] == {"position": 5, "total": 5}

    status["region_commissioning"] = {
        "status": "restore_finalization_required",
        "detail": "The exact previous crossover is already restored.",
    }
    finishing_restore = crossover_envelope.build_crossover_envelope(status)
    assert finishing_restore["screen"] == "apply"
    assert finishing_restore["next_action"] == {
        "id": "finish_candidate_restore",
        "label": "Finish restore",
        "endpoint": "/correction/crossover/restore",
        "body": {},
    }
    assert "already restored" in finishing_restore["verdict_text"]

    status["region_commissioning"] = {
        "status": "candidate_refused",
        "detail": "Exact evidence could not authorize a candidate.",
        "candidate_failure": {
            "reason": "candidate_polarity_inconclusive",
            "detail": "normal and reverse evidence did not prove one polarity",
        },
    }
    refused = crossover_envelope.build_crossover_envelope(status)
    assert refused["screen"] == "microphone"
    assert refused["candidate_review"] is None
    assert refused["next_action"] == {
        "id": "level_match",
        "label": "Restart driver and alignment measurements",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }
    assert any(
        nudge["code"] == "measured_candidate_refused"
        for nudge in refused["nudges"]
    )


def test_strict_alignment_precedes_prior_automatic_applied_profile():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["commissioning_run"] = {
        "status": "current",
        "isolated_evidence": {"status": "complete"},
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "automatic",
        "recomposition_snapshot": {
            "level_match": {
                "active_comparison_set_id": _COMPARISON_SET["comparison_set_id"],
            },
        },
    }
    cases = (
        (
            {
                "status": "needs_geometry",
                "next_geometry": {
                    "target_fingerprint": "f" * 64,
                    "lower_role": "woofer",
                    "upper_role": "tweeter",
                    "fc_hz": 2_100.0,
                },
            },
            "alignment_geometry",
            "attest_region_geometry",
        ),
        (
            {
                "status": "collecting",
                "next_capture": {"evidence_kind": "server_selected"},
            },
            "alignment",
            "measure_region_alignment",
        ),
        ({"status": "measured"}, "review", "prepare_measured_candidate"),
        (
            {"status": "candidate_ready", "candidate": {}},
            "review",
            "apply_measured_candidate",
        ),
    )

    for region_status, expected_screen, expected_action in cases:
        status["region_commissioning"] = region_status
        envelope = crossover_envelope.build_crossover_envelope(status)
        action = envelope["next_action"]

        assert envelope["screen"] == expected_screen
        assert (action["id"] if action is not None else None) == expected_action
        assert next(
            step for step in envelope["steps"] if step["id"] == "apply"
        )["status"] == ("active" if expected_screen == "review" else "pending")


def test_driver_capture_geometry_must_match_server_owned_next_step():
    from jasper.web import correction_setup

    status = _envelope_status()
    _locked_level(status)
    correction_setup._assert_crossover_driver_action(
        status,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="near_field",
    )
    with pytest.raises(ValueError, match="server-owned next step"):
        correction_setup._assert_crossover_driver_action(
            status,
            speaker_group_id="mono",
            role="woofer",
            capture_geometry="reference_axis",
        )

    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    correction_setup._assert_crossover_reference_axis_level_action(
        status,
        speaker_group_id="mono",
        role="woofer",
    )
    with pytest.raises(ValueError, match="server-owned next step"):
        correction_setup._assert_crossover_reference_axis_level_action(
            status,
            speaker_group_id="mono",
            role="tweeter",
        )
    _lock_reference_axis_driver(status, "woofer")
    correction_setup._assert_crossover_driver_action(
        status,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="reference_axis",
    )
    with pytest.raises(ValueError, match="server-owned next step"):
        correction_setup._assert_crossover_driver_action(
            status,
            speaker_group_id="mono",
            role="woofer",
            capture_geometry="near_field",
        )


def test_plan_admission_match_is_scoped_to_the_live_authorized_target():
    """``_plan_admission_matches`` (SPEC W2.3) is the seam that lets a v3
    capture plan's own ``repeat_admission`` reservation stand in for the
    envelope-derivation guard (``_assert_crossover_driver_action`` /
    ``_assert_crossover_reference_axis_level_action``) for exactly the
    capture it authorized — see hardware run 21. Every mismatch below must
    still fall through to the full guard: the protection did not weaken,
    it moved to its rightful owner (the durable ledger reservation), and
    only for the EXACT (group, role, geometry) it actually admitted."""

    from jasper.web import correction_setup

    live = {
        "target_id": "mono:woofer",
        "target_fingerprint": "6" * 64,
        "status": "active",
        "inflight": "a" * 32,
    }
    assert correction_setup._plan_admission_matches(
        live,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="near_field",
        target_fingerprint="6" * 64,
    )

    # No admission at all (every wizard-initiated v2/direct request) never
    # matches -- the full guard always runs for those.
    assert not correction_setup._plan_admission_matches(
        None,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="near_field",
        target_fingerprint="6" * 64,
    )

    # Fixed-axis (reference_axis) variant: near_field and reference_axis
    # captures of the SAME driver are bound to DIFFERENT repeat_admission
    # targets (test_driver_repeat_bindings_are_geometry_scoped) precisely so
    # they cannot complete each other's set. A near_field reservation must
    # not exempt a reference_axis request, and vice versa.
    assert not correction_setup._plan_admission_matches(
        live,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="reference_axis",
        target_fingerprint="6" * 64,
    )
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    fixed_axis_target_id, fixed_axis_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="reference_axis",
    )
    fixed_axis_live = {
        "target_id": fixed_axis_target_id,
        "target_fingerprint": fixed_axis_fingerprint,
        "status": "active",
        "inflight": "b" * 32,
    }
    assert correction_setup._plan_admission_matches(
        fixed_axis_live,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="reference_axis",
        target_fingerprint="6" * 64,
    )
    assert not correction_setup._plan_admission_matches(
        fixed_axis_live,
        speaker_group_id="mono",
        role="woofer",
        capture_geometry="near_field",
        target_fingerprint="6" * 64,
    )

    # Different role/group never matches, even with an otherwise-live entry.
    assert not correction_setup._plan_admission_matches(
        live,
        speaker_group_id="mono",
        role="tweeter",
        capture_geometry="near_field",
        target_fingerprint="7" * 64,
    )
    assert not correction_setup._plan_admission_matches(
        live,
        speaker_group_id="stereo",
        role="woofer",
        capture_geometry="near_field",
        target_fingerprint="6" * 64,
    )

    # A finished (not "active", or no longer inflight) reservation is a
    # completed fact, not a live authorization -- a stale wizard tab must
    # not be able to replay it to skip the guard.
    for finished in (
        {**live, "status": "completed", "inflight": None},
        {**live, "status": "ready", "inflight": None},
        {**live, "status": "active", "inflight": None},
    ):
        assert not correction_setup._plan_admission_matches(
            finished,
            speaker_group_id="mono",
            role="woofer",
            capture_geometry="near_field",
            target_fingerprint="6" * 64,
        )


def test_driver_repeat_bindings_are_geometry_scoped():
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    near = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="near_field",
    )
    fixed = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="reference_axis",
    )

    assert near == ("mono:woofer", "6" * 64)
    assert fixed[0] == "reference_axis/mono:woofer"
    assert fixed[1] != near[1]

    legal_colon_group = driver_repeat_binding(
        speaker_group_id="reference_axis:mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="near_field",
    )
    assert fixed[0] != legal_colon_group[0]


def test_reference_axis_envelope_uses_canonical_repeat_binding(monkeypatch):
    from jasper.active_speaker import capture_geometry, crossover_envelope

    status = _envelope_status()
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    status["level_match"]["reference_axis_driver_locks"] = {
        "mono:woofer": -12.0,
    }
    sentinel_id = "canonical-fixed-controller-id"
    status["level_match"]["repeats"]["durable"]["targets"][sentinel_id] = {
        "status": "active",
        "attempts": 1,
        "results": [{"attempt": 1, "accepted": True}],
    }
    real_binding = capture_geometry.driver_repeat_binding

    def canonical_binding(**kwargs):
        if kwargs["capture_geometry"] == "reference_axis":
            return sentinel_id, "8" * 64
        return real_binding(**kwargs)

    monkeypatch.setattr(capture_geometry, "driver_repeat_binding", canonical_binding)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["next_action"]["id"] == "measure_reference_axis_driver"
    assert env["next_action"]["label"] == "Measure fixed-axis woofer again"


def test_restart_after_near_field_capture_returns_to_fixed_axis_level():
    """Completed durable near-field work survives correction-web restart."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    status["level_match"] = {
        "running": False,
        "valid": False,
        "ready": False,
        "reference_axis_driver_locks": {},
    }
    _completed_near_field_repeat_state(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match_reference_axis_driver"
    assert env["next_action"]["body"] == {
        "capture_geometry": "reference_axis",
        "speaker_group_id": "mono",
        "role": "woofer",
    }


def test_restart_refuses_stale_near_field_repeat_fingerprint():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    status["level_match"] = {
        "running": False,
        "valid": False,
        "ready": False,
        "reference_axis_driver_locks": {},
    }
    _completed_near_field_repeat_state(status)
    status["level_match"]["repeats"]["durable"]["targets"]["mono:woofer"][
        "target_fingerprint"
    ] = "f" * 64

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"


@pytest.mark.parametrize(
    "target_id", ("mono:woofer", "reference_axis/mono:woofer")
)
def test_completed_two_of_four_repeat_set_requires_fresh_level_run(target_id):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _completed_reference_axis_repeat_state(status)
    entry = status["level_match"]["repeats"]["durable"]["targets"][target_id]
    entry.update({
        "status": "completed",
        "attempts": 4,
        "accepted": 2,
        "results": [
            {"attempt": 1, "accepted": True},
            {"attempt": 2, "accepted": True},
            {"attempt": 3, "accepted": False},
            {"attempt": 4, "accepted": False},
        ],
    })

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert "cannot be resumed" in env["verdict_text"]


def _insufficient_driver_acoustic(role: str) -> dict:
    """A driver that produced sound but never cleared the per-band SNR floor.

    Distinct from a repeat *failure*: every repeat was individually accepted
    (no clipping/outlier/transport rejection -- see
    ``_completed_insufficient_near_field_repeat_state``), but the aggregate
    overlap-band magnitude SNR is "insufficient"
    (jasper.audio_measurement.snr_policy), so the band is not ``usable``.
    """
    acoustic = _driver_acoustic(role)
    acoustic["acoustic"]["overlap_levels"] = [{
        "region_id": "woofer_tweeter",
        "above_validity_floor": True,
        "usable": False,
        "snr_verdict": "insufficient",
    }]
    return acoustic


def _completed_insufficient_near_field_repeat_state(status: dict) -> None:
    """Woofer's near-field repeat set: completed, 3/3 accepted, insufficient SNR.

    Mirrors the exact shape ``web_measurement._finalize_driver_repeat_set``
    persists: repeat *acceptance* (outlier/clipping/transport check) is
    independent of the aggregate SNR verdict, so the final accepted attempt's
    own ``admission_result`` still carries ``snr_verdict: "insufficient"``.
    JTS3 run 13: per-repeat SNR 8.4-10.5 dB, well under the 20 dB warn floor
    (docs/active-crossover-information-design.md "Level control and SNR").
    """
    targets = {
        "mono:woofer": {
            "target_fingerprint": "6" * 64,
            "status": "completed",
            "attempts": 3,
            "results": [
                {
                    "attempt": 1,
                    "accepted": True,
                    "estimated_snr_db": 10.5,
                    "snr_verdict": "insufficient",
                    "snr_shortfall_db": 9.5,
                    "worst_band_id": "upper_bass",
                },
                {
                    "attempt": 2,
                    "accepted": True,
                    "estimated_snr_db": 9.1,
                    "snr_verdict": "insufficient",
                    "snr_shortfall_db": 10.9,
                    "worst_band_id": "upper_bass",
                },
                {
                    "attempt": 3,
                    "accepted": True,
                    "estimated_snr_db": 8.4,
                    "snr_verdict": "insufficient",
                    "snr_shortfall_db": 11.6,
                    "worst_band_id": "upper_bass",
                },
            ],
        },
    }
    status["level_match"]["repeats"] = {
        "targets": targets,
        "failures": {},
        "durable": {"status": "active", "targets": targets},
    }


def test_completed_insufficient_woofer_repeat_set_renders_honest_terminal():
    """JTS3 run 13 (punch #28): the woofer repeat set completed 3/3 (every
    repeat individually accepted) but its SNR never cleared the floor, so the
    aggregate acoustic evidence stayed unusable. Pre-fix, the envelope kept
    deriving "repeat N+1" from attempt count alone and offered a fourth
    repeat that fails at reservation
    (repeat_admission.reserve() raises "the crossover repeat set is
    completed") -- a closed loop. This must render an honest terminal with
    the existing "Restart driver level check" recovery affordance instead of
    offering another repeat."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _insufficient_driver_acoustic("woofer"),
    }
    _completed_insufficient_near_field_repeat_state(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"] == {
        "id": "level_match",
        "label": "Restart woofer driver level check",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }
    assert "repeat" not in env["next_action"]["label"].lower()
    verdict = env["verdict_text"]
    assert "wasn't enough signal" in verdict
    assert "8.4 dB SNR" in verdict
    assert "11.6 dB more needed" in verdict
    assert "upper bass band" in verdict
    # Language guide (docs/active-crossover-information-design.md): no
    # internal vocabulary leaks into the plain-language copy.
    lowered = verdict.lower()
    for banned in (
        "fingerprint", "authority", "candidate", "authorizes",
        "ledger", "repeat_admission", "comparison set",
    ):
        assert banned not in lowered


def test_completed_insufficient_with_active_correction_says_jts_plays_louder():
    """W2.3 (hardware run 19): once the completion-time correction has
    actually written (``level_match.solve_correction[target]["writes"] >
    0`` -- see ``CrossoverLevelLease.record_solve_correction``'s
    ``"completed_insufficient"`` trigger), the terminal copy must say that
    JTS itself will play the next measurement louder, not tell the
    household to manually "raise the measurement level" -- #1552 left that
    as the only lever; W2.3 automates it."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _insufficient_driver_acoustic("woofer"),
    }
    _completed_insufficient_near_field_repeat_state(status)
    status["level_match"]["solve_correction"] = {
        "mono:woofer": {"writes": 1, "adjustment_db": 11.6, "exhausted": False},
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    verdict = env["verdict_text"]
    assert "wasn't enough signal" in verdict
    assert "JTS will play the next measurement louder" in verdict
    assert "raise the measurement level" not in verdict.lower()


def test_completed_insufficient_with_exhausted_correction_routes_to_refusal_screen():
    """W2.3 (hardware run 19, REFUSAL REACHABILITY): once the completion-time
    correction has exhausted the bounded budget
    (``level_match.solve_correction[target]["exhausted"] is True``), the
    envelope must route straight to the placement-lever refusal screen
    instead of one more dead-end "restart the level check" round trip that
    would just refuse again on the very next attempt -- see
    ``_active_level_solve_refusal``."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _insufficient_driver_acoustic("woofer"),
    }
    _completed_insufficient_near_field_repeat_state(status)
    status["level_match"]["solve_correction"] = {
        "mono:woofer": {"writes": 3, "adjustment_db": 9.4, "exhausted": True},
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "level_solve_refused"
    assert "move the phone close to the driver" in env["verdict_text"].lower()
    assert env["next_action"] == {
        "id": "level_match",
        "label": "Redo the quick level check (about 2 minutes)",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }
    # The level lock itself must be untouched by a synthesized refusal.
    assert status["level_match"]["last"]["ramp"]["restored"] is True


def test_missing_driver_with_clobbered_confirmation_offers_confirm_not_measure():
    """JTS3 run 13 -> run 14 (punch #29): the woofer's driver confirmation
    was clobbered by a later sweep capture, so ``current_driver_floor_evidence``
    refuses every subsequent measurement pre-playback. Pre-fix, the envelope
    kept offering "Position the mic, then measure woofer" -- a dead end that
    fails at the same gate every time, and it blamed the room instead of the
    stale confirmation. This must route to re-confirming the driver by ear
    instead of another futile sweep attempt.
    """
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {}
    status["measurements"]["summary"]["latest_driver_confirmations"] = {}

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"] == {
        "id": "confirm_driver",
        "label": "Confirm woofer by ear",
        "endpoint": "/correction/crossover/driver-test",
        "body": {"speaker_group_id": "mono", "role": "woofer"},
    }
    verdict = env["verdict_text"]
    assert "nothing to fix in the room" in verdict
    assert "confirm" in verdict.lower()
    # Language guide (docs/active-crossover-information-design.md): no
    # internal vocabulary leaks into the plain-language copy.
    lowered = verdict.lower()
    for banned in (
        "fingerprint", "authority", "candidate", "authorizes",
        "ledger", "repeat_admission", "comparison set", "playback",
    ):
        assert banned not in lowered


def test_missing_driver_with_intact_confirmation_still_offers_measure():
    """Regression: a driver that has simply never been swept yet (confirmed
    by ear, no acoustic evidence recorded) must still be offered the normal
    "measure" action -- the new confirmation check must not block the
    ordinary first-time measurement flow.
    """
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {}
    status["measurements"]["summary"]["latest_driver_confirmations"] = {
        "mono:woofer": {
            "captured": True,
            "outcome": "heard_correct_driver",
            "target_fingerprint": "6" * 64,
            "issues": [],
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"]["id"] == "measure_driver"
    assert env["next_action"]["label"] == "Position the mic, then measure woofer"


def test_completed_insufficient_fixed_axis_repeat_set_renders_honest_terminal():
    """Same defect, fixed-axis geometry: the reference-axis repeat set can
    independently complete 3/3 accepted with insufficient SNR while
    near-field stays healthy."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    status["measurements"]["summary"][
        "latest_reference_axis_driver_measurements"
    ] = {
        "mono:woofer": _insufficient_driver_acoustic("woofer"),
    }
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["acoustic"]["capture_geometry"] = "reference_axis"
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["acoustic"]["gating"] = {"applied": True, "f_valid_floor_hz": 320.0}
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["placement_proof"]["policy_id"] = "driver_reference_axis_v1"
    _lock_reference_axis_driver(status, "woofer")
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="reference_axis",
    )
    entry = {
        "target_fingerprint": repeat_target_fingerprint,
        "status": "completed",
        "attempts": 3,
        "results": [
            {
                "attempt": attempt,
                "accepted": True,
                "estimated_snr_db": 8.4,
                "snr_verdict": "insufficient",
                "snr_shortfall_db": 11.6,
                "worst_band_id": "upper_bass",
            }
            for attempt in (1, 2, 3)
        ],
    }
    status["level_match"]["repeats"]["targets"][repeat_target_id] = entry
    status["level_match"]["repeats"]["durable"]["targets"][repeat_target_id] = entry

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"] == {
        "id": "level_match",
        "label": "Restart woofer driver level check",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }
    assert "wasn't enough signal" in env["verdict_text"]
    assert "8.4 dB SNR" in env["verdict_text"]


def test_completed_insufficient_fixed_axis_with_active_correction_says_jts_plays_louder():
    """W2.3: the fixed-axis branch's own ``_completed_insufficient_verdict``
    call site (a separate elif branch from the near-field one) must ALSO
    read ``level_match.solve_correction`` keyed by the SAME
    ``{group_id}:{role}`` physical target id and render the "JTS will play
    louder" copy."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    status["measurements"]["summary"][
        "latest_reference_axis_driver_measurements"
    ] = {
        "mono:woofer": _insufficient_driver_acoustic("woofer"),
    }
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["acoustic"]["capture_geometry"] = "reference_axis"
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["acoustic"]["gating"] = {"applied": True, "f_valid_floor_hz": 320.0}
    status["measurements"]["summary"]["latest_reference_axis_driver_measurements"][
        "mono:woofer"
    ]["placement_proof"]["policy_id"] = "driver_reference_axis_v1"
    _lock_reference_axis_driver(status, "woofer")
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint="6" * 64,
        capture_geometry="reference_axis",
    )
    entry = {
        "target_fingerprint": repeat_target_fingerprint,
        "status": "completed",
        "attempts": 3,
        "results": [
            {
                "attempt": attempt,
                "accepted": True,
                "estimated_snr_db": 8.4,
                "snr_verdict": "insufficient",
                "snr_shortfall_db": 11.6,
                "worst_band_id": "upper_bass",
            }
            for attempt in (1, 2, 3)
        ],
    }
    status["level_match"]["repeats"]["targets"][repeat_target_id] = entry
    status["level_match"]["repeats"]["durable"]["targets"][repeat_target_id] = entry
    status["level_match"]["solve_correction"] = {
        "mono:woofer": {"writes": 1, "adjustment_db": 11.6, "exhausted": False},
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert "JTS will play the next measurement louder" in env["verdict_text"]


def test_completed_sufficient_woofer_advances_to_next_driver_target():
    """The happy path (untested on hardware before run 14): once the woofer's
    repeat set completes 3/3 with USABLE acoustic evidence, the envelope
    must advance past it to the next target (tweeter) rather than looping or
    rendering a terminal state."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
    }
    targets = {
        "mono:woofer": {
            "target_fingerprint": "6" * 64,
            "status": "completed",
            "attempts": 3,
            "results": [
                {"attempt": attempt, "accepted": True}
                for attempt in (1, 2, 3)
            ],
        },
    }
    status["level_match"]["repeats"] = {
        "targets": targets,
        "failures": {},
        "durable": {"status": "active", "targets": targets},
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"] == {
        "id": "measure_driver",
        "label": "Position the mic, then measure tweeter",
        "endpoint": "/correction/crossover/relay-capture",
        "body": {
            "kind": "driver",
            "speaker_group_id": "mono",
            "role": "tweeter",
        },
    }


def test_envelope_refuses_apply_without_fixed_axis_repeat_controller():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    status["setup"]["automatic_candidate"] = {"ready": True, "reason": None}

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert next(step for step in env["steps"] if step["id"] == "apply")[
        "status"
    ] == "pending"


@pytest.mark.parametrize(
    "summary_key",
    ["latest_driver_measurements", "latest_reference_axis_driver_measurements"],
)
def test_envelope_refuses_fabricated_four_accepted_acoustic_repeats(summary_key):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _completed_reference_axis_repeat_state(status)
    status["measurements"]["summary"][summary_key]["mono:woofer"]["repeats"][
        "accepted"
    ] = 4
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] != "apply"
    assert next(step for step in env["steps"] if step["id"] == "apply")[
        "status"
    ] == "pending"


def _guard_lease(discarded):
    return SimpleNamespace(
        relay_setup_binding=None,
        input_device=None,
        mic_calibration=None,
        noise_floor_db=None,
        context_id=None,
        discard_driver_level_outcome=lambda group, role, *, capture_geometry: (
            discarded.append((group, role, capture_geometry))
        ),
    )


@pytest.mark.asyncio
async def test_fixed_axis_guard_rolls_back_identity_after_failure():
    from jasper.web.correction_setup import _fixed_axis_level_identity_guard

    discarded: list[tuple[str, str, str]] = []
    lease = _guard_lease(discarded)

    with pytest.raises(ValueError, match="wrong microphone"):
        async with _fixed_axis_level_identity_guard(
            lease,
            speaker_group_id="mono",
            role="woofer",
        ):
            lease.relay_setup_binding = "wrong-binding"
            lease.input_device = {"device_id_hash": "wrong"}
            raise ValueError("wrong microphone")

    assert discarded == [("mono", "woofer", "reference_axis")]
    assert lease.relay_setup_binding is None
    assert lease.input_device is None


@pytest.mark.asyncio
async def test_fixed_axis_guard_keeps_completed_identity():
    from jasper.web.correction_setup import _fixed_axis_level_identity_guard

    discarded: list[tuple[str, str, str]] = []
    lease = _guard_lease(discarded)
    async with _fixed_axis_level_identity_guard(
        lease,
        speaker_group_id="mono",
        role="woofer",
    ):
        lease.input_device = {"device_id_hash": "confirmed"}

    assert discarded == []
    assert lease.input_device == {"device_id_hash": "confirmed"}


def _latch_volume_safety(
    lease,
    *,
    original=-21.0,
    source="level_match",
    speaker_group_id="mono",
    role="woofer",
):
    lease._begin_volume_transition(
        source=source,
        speaker_group_id=speaker_group_id,
        role=role,
        original_main_volume_db=original,
    )
    lease._mark_volume_unresolved("volume_restore_unconfirmed")


@pytest.mark.asyncio
async def test_crossover_volume_safety_hydrates_and_confirms_exact_recovery(tmp_path):
    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        EMERGENCY_SWEEP_VOLUME_DB,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "crossover-volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    _latch_volume_safety(lease)
    attempted: list[float] = []
    assert lease.level_match_snapshot()["unresolved_volume_safety"] == {
        "status": "unresolved",
        "reason": "volume_restore_unconfirmed",
        "source": "level_match",
        "speaker_group_id": "mono",
        "role": "woofer",
        "original_main_volume_db": -21.0,
        "emergency_volume_db": EMERGENCY_SWEEP_VOLUME_DB,
    }
    hydrated = CrossoverLevelLease(volume_safety_state_path=state_path)
    assert hydrated.unresolved_volume_safety == lease.unresolved_volume_safety

    current = -5.0

    async def accept(db):
        nonlocal current
        attempted.append(db)
        current = db
        return True

    async def readback():
        return current

    assert (
        await hydrated.recover_unresolved_volume_safety(accept, readback)
        is UnresolvedVolumeRecoveryResult.EXACT_RESTORED
    )
    assert attempted[-1] == -21.0
    assert hydrated.level_match_snapshot()["unresolved_volume_safety"] is None
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=state_path
        ).unresolved_volume_safety
        is None
    )

    # The original in-process lease remains latched until it performs or
    # observes recovery itself; another process clearing the durable tombstone
    # cannot silently mutate this process's authority.
    assert lease.unresolved_volume_safety is not None


@pytest.mark.asyncio
async def test_crossover_volume_recovery_uses_confirmed_emergency_or_stays_latched(
    tmp_path,
):
    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        EMERGENCY_SWEEP_VOLUME_DB,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "crossover-volume-safety.json"
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    _latch_volume_safety(lease, original=-18.0)
    current = -4.0
    writes = []

    async def emergency_only(db):
        nonlocal current
        writes.append(db)
        if db == -18.0:
            return False
        current = db
        return True

    async def readback():
        return current

    assert (
        await lease.recover_unresolved_volume_safety(emergency_only, readback)
        is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
    )
    assert writes == [-18.0, EMERGENCY_SWEEP_VOLUME_DB]
    assert current == EMERGENCY_SWEEP_VOLUME_DB
    assert lease.unresolved_volume_safety is None

    failed_path = tmp_path / "failed-volume-safety.json"
    failed = CrossoverLevelLease(volume_safety_state_path=failed_path)
    _latch_volume_safety(failed, original=-12.0, role="tweeter")

    async def ack_without_readback(_db):
        return True

    async def wrong_readback():
        return -3.0

    assert (
        await failed.recover_unresolved_volume_safety(
            ack_without_readback,
            wrong_readback,
        )
        is UnresolvedVolumeRecoveryResult.FAILED
    )
    assert failed.unresolved_volume_safety is not None
    assert (
        CrossoverLevelLease(
            volume_safety_state_path=failed_path
        ).unresolved_volume_safety
        is not None
    )
    with pytest.raises(RuntimeError, match="not confirmed safe"):
        failed.invalidate_comparison_context()


@pytest.mark.asyncio
async def test_explicit_volume_recovery_refuses_a_live_transition(tmp_path):
    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        UnresolvedVolumeRecoveryResult,
    )

    lease = CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    lease._begin_volume_transition(
        source="driver_sweep",
        speaker_group_id="mono",
        role="woofer",
        original_main_volume_db=-21.0,
    )

    async def unexpected(*_args):
        pytest.fail("live volume recovery must not touch CamillaDSP")

    assert (
        await lease.recover_unresolved_volume_safety(unexpected, unexpected)
        is UnresolvedVolumeRecoveryResult.FAILED
    )


@pytest.mark.asyncio
async def test_confirmed_recovery_stays_latched_when_tombstone_write_fails(
    monkeypatch, tmp_path
):
    from jasper.web import correction_crossover_backend as backend

    state_path = tmp_path / "volume-safety.json"
    lease = backend.CrossoverLevelLease(volume_safety_state_path=state_path)
    _latch_volume_safety(lease)
    real_write = backend._write_volume_safety_state

    def fail_resolved(path, payload):
        if payload.get("status") == "resolved":
            raise OSError("tombstone unavailable")
        real_write(path, payload)

    monkeypatch.setattr(backend, "_write_volume_safety_state", fail_resolved)
    current = -3.0

    async def set_volume(value):
        nonlocal current
        current = value
        return True

    async def get_volume():
        return current

    assert (
        await lease.recover_unresolved_volume_safety(set_volume, get_volume)
        is backend.UnresolvedVolumeRecoveryResult.FAILED
    )
    assert lease.unresolved_volume_safety["reason"] == ("volume_safety_clear_failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reason"),
    (
        ("{not-json", "volume_safety_state_unreadable"),
        (
            json.dumps({"kind": "wrong", "schema_version": 99}),
            "volume_safety_state_malformed",
        ),
    ),
)
async def test_corrupt_volume_state_fails_closed_and_recovers_emergency_only(
    tmp_path, content, reason
):
    from jasper.web.correction_crossover_backend import (
        CrossoverLevelLease,
        EMERGENCY_SWEEP_VOLUME_DB,
        UnresolvedVolumeRecoveryResult,
    )

    state_path = tmp_path / "volume-safety.json"
    state_path.write_text(content, encoding="utf-8")
    lease = CrossoverLevelLease(volume_safety_state_path=state_path)
    assert lease.unresolved_volume_safety["reason"] == reason
    assert lease.unresolved_volume_safety["original_main_volume_db"] is None
    with pytest.raises(RuntimeError, match="not confirmed safe"):
        lease.assert_volume_safety_resolved()
    current = -3.0
    writes = []

    async def set_volume(value):
        nonlocal current
        writes.append(value)
        current = value
        return True

    async def get_volume():
        return current

    assert (
        await lease.recover_unresolved_volume_safety(set_volume, get_volume)
        is UnresolvedVolumeRecoveryResult.EMERGENCY_ATTENUATED
    )
    assert writes == [EMERGENCY_SWEEP_VOLUME_DB]
    assert lease.unresolved_volume_safety is None


def test_crossover_envelope_uses_applied_anchor_while_candidate_is_incomplete():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"].update({
        "status": "ready",
        "baseline_profile": {
            "status": "blocked",
            "revalidation": {"required": True},
        },
            "protected_profile": {
                "status": "ready",
                "source_fingerprint": "protected-profile",
                "candidate_fingerprint": "protected-profile",
            },
    })
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
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
        "body": {
            "tuning_owner": "manual",
            "expected_candidate_fingerprint": "manual-candidate",
        },
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
    # A fresh, uninterrupted done_manual carries no interrupted-retune nudge.
    assert env["nudges"] == []


def test_crossover_envelope_done_manual_run_steps_stay_monotonic():
    """Pre-fix bug (live on the deployed box, 2026-07-17): a crossover applied
    manually with no fresh measurement run marked "apply" done while
    microphone/drivers/alignment stayed pending -- steps rendered
    done, pending, pending, pending, done, a non-monotonic run stepper.
    Root cause: durable applied-state was conflated into per-run step
    status. The fix keeps the run stepper a MONOTONIC prefix (see
    crossover_envelope._project_run_steps) and surfaces "a crossover is
    applied" as its own `applied` chip (crossover_envelope._applied_chip),
    separate from the per-run steps. Mirrors the fixture in
    test_crossover_envelope_manual_profile_offers_room_edit_or_automatic.
    """
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
    statuses = {step["id"]: step["status"] for step in env["steps"]}
    assert statuses == {
        "speaker_setup": "done",
        "microphone": "pending",
        "drivers": "pending",
        "alignment": "pending",
        "apply": "pending",
    }
    assert env["applied"] == {
        "state": "manual",
        "label": "Manual crossover applied",
    }


def _assert_steps_monotonic(steps, label: str) -> None:
    """Once a step's status is not "done", no later step may read "done"
    either -- the run stepper must always render as a monotonic prefix of
    completed steps, regardless of screen or durable applied-state."""
    seen_non_done = False
    for step in steps:
        if seen_non_done:
            assert step["status"] != "done", (
                f"{label}: non-monotonic steps {steps!r}"
            )
        if step["status"] != "done":
            seen_non_done = True


def _automatic_done_status() -> dict:
    """Mirrors the cumulative fixture in
    test_crossover_envelope_walks_level_drivers_apply_room's final state: an
    automatic crossover applied with no fresh measurement run, and no
    region_commissioning ever tracked (so "alignment" is never marked done).
    This is a SECOND, previously-unnoticed instance of the same non-monotonic
    bug the done_manual fix above also covers -- apply done while alignment
    stays pending -- caught by the general projection rather than a
    screen-specific patch.
    """
    status = _envelope_status()
    _locked_level(status)
    summary = status["measurements"]["summary"]
    summary["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    status["level_match"]["reference_axis_driver_locks"] = {
        "mono:woofer": -10.0,
        "mono:tweeter": -12.0,
    }
    summary["latest_reference_axis_driver_measurements"] = {
        "mono:woofer": _reference_axis_driver_acoustic("woofer"),
        "mono:tweeter": _reference_axis_driver_acoustic("tweeter"),
    }
    _completed_reference_axis_repeat_state(status)
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }
    status["applied_profile"] = {
        "status": "applied",
        "provisional": False,
        "level_match": {"groups_measured": 1},
        "source": {"fingerprint": "source-1"},
        "tuning_owner": "automatic",
        "recomposition_snapshot": {
            "schema_version": 1,
            "tuning_owner": "automatic",
            "level_match": {
                "active_comparison_set_id": _COMPARISON_SET["comparison_set_id"],
            },
        },
    }
    status["setup"]["acoustic_commissioning"] = {"allowed": True}
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }
    return status


def test_crossover_envelope_steps_are_always_monotonic():
    """General invariant guard (the durable pin): for a representative matrix
    of screens, once a run step is not "done", no later step may read "done"
    either. Pre-fix, an applied-state screen (done_manual, choose_tuning, and
    -- previously unnoticed -- the plain automatic "done" screen before
    region_commissioning is ever tracked) could mark "apply" done while
    earlier steps stayed pending -- see
    test_crossover_envelope_done_manual_run_steps_stay_monotonic for the
    concrete repro this generalizes.
    """
    from jasper.active_speaker import crossover_envelope

    passive_status = {"active": False, "targets": {"drivers": [], "summed": []}}

    speaker_setup_status = _envelope_status()
    speaker_setup_status["setup"]["status"] = "blocked"

    done_manual_status = _envelope_status()
    done_manual_status["setup"]["acoustic_commissioning"] = {"allowed": True}
    done_manual_status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    done_manual_status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "manual",
        "recomposition_snapshot": {
            "schema_version": 1,
            "tuning_owner": "manual",
        },
    }

    # choose_tuning: the LEGACY re-apply trigger (applied_profile.status ==
    # "applied" with no recomposition_snapshot) alongside a durably-applied
    # manual crossover -- the same shape as done_manual, reached through the
    # legacy branch instead.
    choose_tuning_status = _envelope_status()
    choose_tuning_status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    choose_tuning_status["setup"]["manual_preservation"] = {
        "ready": True,
        "reason": None,
    }
    choose_tuning_status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "manual",
    }

    alignment_status = _envelope_status()
    _locked_level(alignment_status)
    alignment_status["commissioning_run"] = {
        "status": "current",
        "isolated_evidence": {"status": "complete"},
    }
    alignment_status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    alignment_status["region_commissioning"] = {
        "status": "needs_geometry",
        "next_geometry": {
            "target_fingerprint": "f" * 64,
            "lower_role": "woofer",
            "upper_role": "tweeter",
            "fc_hz": 2_100.0,
        },
    }

    cases = (
        ("not_applicable", passive_status, "not_applicable"),
        ("speaker_setup", speaker_setup_status, "speaker_setup"),
        ("done_manual", done_manual_status, "done_manual"),
        ("choose_tuning", choose_tuning_status, "choose_tuning"),
        ("alignment_geometry", alignment_status, "alignment_geometry"),
        ("automatic_done", _automatic_done_status(), "done"),
    )

    for label, status, expected_screen in cases:
        env = crossover_envelope.build_crossover_envelope(status)
        assert env["screen"] == expected_screen, (
            f"{label}: expected screen {expected_screen!r}, got {env['screen']!r}"
        )
        _assert_steps_monotonic(env["steps"], label)


def test_crossover_envelope_automatic_done_terminal_marks_every_step_done():
    """Completeness pin for the automatic success terminal (screen="done").

    The general monotonic-invariant guard above CANNOT catch this: a stepper
    of [done, done, done, pending, pending] is still monotonic, so an
    understated apply slips past it silently. That was the real regression the
    first monotonic projection introduced -- the automatic path legitimately
    SKIPS the `alignment` sweep step, so `alignment` was never in the raw
    `done` set and, with `active_step="apply"` excluded from the done-prefix
    (`done - {active}`), the frontier broke at the missing `alignment` and
    dropped `apply` to pending -- while the verdict, the `applied` chip, and
    `progress` (5/5) all reported applied. The fix backfills `alignment` into
    `done` for a completed automatic run (declared frequency+slope IS the
    alignment) and uses the "complete" terminal sentinel for `active_step`
    (matching the verified terminal). Mirrors the verified terminal's own
    all-steps-done assertion in
    test_crossover_envelope_projects_active_owned_alignment_actions.
    """
    from jasper.active_speaker import crossover_envelope

    env = crossover_envelope.build_crossover_envelope(_automatic_done_status())

    assert env["screen"] == "done"
    assert all(step["status"] == "done" for step in env["steps"]), env["steps"]
    assert env["progress"] == {"position": 5, "total": 5}
    assert env["applied"] == {
        "state": "automatic",
        "label": "Automatic crossover applied",
    }


def test_crossover_envelope_done_manual_surfaces_interrupted_retune_nudge():
    """A manual profile is applied, but an in-progress automatic retune's
    level context was discarded (durable repeat safety state unavailable).
    Pre-fix, done_manual's verdict read as a clean terminal ("Your manual
    crossover is applied and ready for room correction.") and the only hint
    was the generic jargon nudge ("Repeat safety state is unavailable...")
    -- hardware-confirmed 2026-07-16. Post-fix, one tailored nudge names the
    interruption and where the flow resumes, and it SUPERSEDES the generic
    crossover_repeat_admission_unavailable nudge that fires from the same
    condition: one fact, one nudge. Pinning the full nudge list makes that
    dedupe decision explicit and regression-proof.
    """
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
    status["level_match"]["repeats"] = {"durable": {"status": "unavailable"}}

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "done_manual"
    # The FULL nudge list: exactly the tailored nudge, nothing else -- the
    # generic crossover_repeat_admission_unavailable nudge must not co-fire.
    assert env["nudges"] == [{
        "code": "crossover_done_manual_retune_interrupted",
        "severity": "warn",
        "text": (
            "An earlier measurement attempt was interrupted. Measuring "
            "starts again from the microphone step."
        ),
    }]

    # Contrast: on every other screen the generic nudge is unchanged --
    # the supersede is scoped to done_manual only.
    other_status = _envelope_status()
    other_status["level_match"]["repeats"] = {"durable": {"status": "unavailable"}}
    other_env = crossover_envelope.build_crossover_envelope(other_status)
    assert other_env["screen"] != "done_manual"
    assert [n["code"] for n in other_env["nudges"]] == [
        "crossover_repeat_admission_unavailable"
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
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
    })
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _completed_reference_axis_repeat_state(status)
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "apply"
    assert env["next_action"] == {
        "id": "replace_manual",
            "label": "Replace manual trims with automatic levels",
        "endpoint": "/correction/crossover/apply",
        "body": {
            "tuning_owner": "automatic",
            "expected_candidate_fingerprint": "automatic-candidate",
        },
    }


def test_incomparable_automatic_evidence_never_offers_apply():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"] = {
        "active_comparison_set": _COMPARISON_SET,
        "summary": {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
        },
    }
    _complete_reference_axis(status)
    status["setup"]["automatic_candidate"] = {
        "ready": False,
        "reason": "automatic_crossover_measurements_incomparable",
        "detail": (
            "Repeat the driver sweeps in one guided run so microphone placement, "
            "level, and excitation can be compared."
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
        "recomposition_snapshot": {
            "schema_version": 1,
            "level_match": {
                "active_comparison_set_id": _COMPARISON_SET[
                    "comparison_set_id"
                ],
            },
        },
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "done"
    assert env["next_action"]["href"] == "/correction/room/"
    assert env["alternate_actions"] == [
        {
            "id": "retune_automatic",
                "label": "Level-match drivers again",
            "endpoint": "/correction/crossover/level-match",
            "body": {},
        },
        {
            "id": "edit_manual",
            "label": "Set crossover manually",
            "href": "/sound/",
        },
    ]


def test_applied_automatic_profile_can_run_a_fresh_sequential_retune():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": "automatic",
        "recomposition_snapshot": {
            "schema_version": 1,
            "level_match": {"active_comparison_set_id": "9" * 32},
        },
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "automatic",
        "reason": None,
    }
    _locked_level(status)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert env["next_action"]["body"]["role"] == "woofer"
    assert next(step for step in env["steps"] if step["id"] == "apply")[
        "status"
    ] == "pending"

    status["measurements"]["summary"] = {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _completed_reference_axis_repeat_state(status)
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "apply"
    assert env["next_action"]["label"] == "Apply updated driver levels"


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
@pytest.mark.parametrize(
    ("attempts", "results", "apply_ready"),
    (
        (3, [{"attempt": 1, "accepted": True}] * 3, False),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": True},
                {"attempt": 3, "accepted": True},
            ],
            False,
        ),
        (
            4,
            [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": False},
                {"attempt": 3, "accepted": True},
                {"attempt": 4, "accepted": True},
            ],
            True,
        ),
    ),
)
def test_crossover_envelope_requires_exact_controller_attempt_coverage(
    capture_geometry, attempts, results, apply_ready
):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"] = {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {"mono": _summed_acoustic()},
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _completed_reference_axis_repeat_state(status)
    target_id = (
        "mono:woofer"
        if capture_geometry == "near_field"
        else "reference_axis/mono:woofer"
    )
    status["level_match"]["repeats"]["durable"]["targets"][target_id].update(
        {
            "attempts": attempts,
            "results": results,
        }
    )
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert (env["screen"] == "apply") is apply_ready


@pytest.mark.parametrize("attempts", (0, 1, 2))
def test_crossover_envelope_driver_label_never_leaks_repeat_numeral(attempts):
    """The repeat-ledger counter must never appear in the button label --
    it belongs in verdict_text (render_repeat_progress), not next_action.
    Covers attempts 0 (no prior repeat), 1, and 2."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    if attempts:
        status["level_match"]["repeats"] = {
            "targets": {
                "mono:woofer": {
                    "attempts": attempts,
                    "accepted": attempts,
                    "target": 3,
                }
            },
            "failures": {},
        }

    env = crossover_envelope.build_crossover_envelope(status)

    label = env["next_action"]["label"]
    assert "repeat" not in label.lower()
    if attempts:
        assert label == "Measure woofer again"
    else:
        assert label == "Position the mic, then measure woofer"


def test_crossover_envelope_surfaces_server_owned_repeat_progress():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["repeats"] = {
        "targets": {
            "mono:woofer": {
                "attempts": 2,
                "accepted": 2,
                "target": 3,
            }
        },
        "failures": {},
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver"
    assert "2 of 3 measurements accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure woofer again"


def test_crossover_envelope_surfaces_fixed_axis_repeat_progress():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    _lock_reference_axis_driver(status, "woofer")
    status["level_match"]["repeats"]["targets"][
        "reference_axis/mono:woofer"
    ] = {
        "attempts": 2,
        "accepted": 2,
        "target": 3,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver_reference_axis"
    assert "2 of 3 measurements accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure fixed-axis woofer again"


@pytest.mark.parametrize("capture_geometry", ["near_field", "reference_axis"])
def test_crossover_envelope_malformed_repeat_progress_fails_closed_without_500(
    capture_geometry,
):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    if capture_geometry == "reference_axis":
        status["measurements"]["summary"]["latest_driver_measurements"] = {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        }
        _completed_near_field_repeat_state(status)
        _lock_reference_axis_driver(status, "woofer")
        target_id = "reference_axis/mono:woofer"
        expected_screen = "driver_reference_axis"
    else:
        target_id = "mono:woofer"
        expected_screen = "driver"
    repeats = status["level_match"].setdefault(
        "repeats", {"targets": {}, "failures": {}}
    )
    repeats.setdefault("targets", {})[target_id] = {
        "attempts": "three",
        "accepted": {"bad": "shape"},
        "target": -1,
        "results": 7,
    }
    repeats["failures"] = 7

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == expected_screen
    assert "stationary repeats" in env["verdict_text"]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda record: record.pop("mic_clipping"),
        lambda record: record["acoustic"].pop("mic_clipping"),
        lambda record: record["acoustic"].update({"overlap_levels": []}),
        lambda record: record["acoustic"]["gating"].update(
            {"f_valid_floor_hz": None}
        ),
    ),
)
def test_fixed_axis_completion_fails_closed_on_malformed_evidence(mutate):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    _complete_reference_axis(status)
    _lock_reference_axis_driver(status, "woofer")
    record = status["measurements"]["summary"][
        "latest_reference_axis_driver_measurements"
    ]["mono:woofer"]
    mutate(record)

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "driver_reference_axis"
    assert env["next_action"]["body"]["role"] == "woofer"


def test_durable_attempts_override_process_only_repeat_count():
    from jasper.active_speaker import crossover_envelope

    store = backend.CrossoverLevelLease()
    store.set_durable_repeat_progress({
        "comparison": {
            "comparison_set_id": "a" * 32,
            "fingerprint": "b" * 64,
        },
        "targets": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "target_fingerprint": "target-fp",
                "attempts": 3,
                "status": "active",
                "results": [
                    {"attempt": 1, "accepted": False},
                    {"attempt": 2, "accepted": False},
                    {"attempt": 3, "accepted": True},
                ],
            }
        },
    })
    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["repeats"] = store.repeat_snapshot()
    env = crossover_envelope.build_crossover_envelope(status)
    assert "1 of 3 measurements accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure woofer again"


@pytest.mark.parametrize(
    "results",
    (
        7,
        True,
        {"attempt": 1, "accepted": True},
        [7],
        [
            {"attempt": 1, "accepted": True},
            {"attempt": 2, "accepted": True},
            {"attempt": 3, "accepted": True},
            7,
        ],
        [{"attempt": 1, "accepted": True, "reject_reason": {"bad": "shape"}}],
        [{"attempt": 1, "accepted": True}] * 3,
        [
            {"attempt": 1, "accepted": True},
            {"attempt": 3, "accepted": True},
        ],
    ),
)
def test_durable_repeat_projection_malformed_results_fail_closed(results):
    store = backend.CrossoverLevelLease()

    store.set_durable_repeat_progress({
        "targets": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "target_fingerprint": "6" * 64,
                "attempts": 3,
                "status": "completed",
                "inflight": None,
                "results": results,
            }
        }
    })

    snapshot = store.repeat_snapshot()
    durable = snapshot["durable"]["targets"]["mono:woofer"]
    assert durable["status"] == "malformed"
    assert durable["attempts"] == 0
    assert durable["results"] == []
    assert snapshot["targets"]["mono:woofer"]["accepted"] == 0


@pytest.mark.parametrize("status", ("active", "completed"))
def test_durable_repeat_projection_rejects_full_results_with_inflight(status):
    store = backend.CrossoverLevelLease()

    store.set_durable_repeat_progress({
        "targets": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "target_fingerprint": "6" * 64,
                "attempts": 3,
                "status": status,
                "inflight": "still-owned",
                "results": [
                    {"attempt": attempt, "accepted": True}
                    for attempt in (1, 2, 3)
                ],
            }
        }
    })

    entry = store.repeat_snapshot()["durable"]["targets"]["mono:woofer"]
    assert entry["status"] == "malformed"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("estimated_snr_db", float("nan")),
        ("snr_shortfall_db", float("inf")),
        ("validity_floor_hz", True),
        ("clipping", "no"),
        ("above_validity_floor", 1),
        ("reject_reason", 7),
        ("snr_verdict", False),
        ("phase", {"bad": "shape"}),
    ),
)
def test_durable_repeat_projection_validates_field_specific_types(field, value):
    store = backend.CrossoverLevelLease()
    results = [{"attempt": attempt, "accepted": True} for attempt in (1, 2, 3)]
    results[0][field] = value

    store.set_durable_repeat_progress(
        {
            "targets": {
                "mono:woofer": {
                    "target_id": "mono:woofer",
                    "target_fingerprint": "6" * 64,
                    "attempts": 3,
                    "status": "completed",
                    "inflight": None,
                    "results": results,
                }
            }
        }
    )

    entry = store.repeat_snapshot()["durable"]["targets"]["mono:woofer"]
    assert entry["status"] == "malformed"


def test_durable_repeat_projection_is_strict_json_and_retains_malformed_targets():
    store = backend.CrossoverLevelLease()
    store.set_durable_repeat_progress(
        {
            "schema_version": True,
            "kind": {"bad": "shape"},
            "status": ["bad"],
            "comparison": {
                "comparison_set_id": {"bad": "shape"},
                "fingerprint": float("nan"),
            },
            "targets": {"mono:woofer": 7},
            "updated_at": float("nan"),
        }
    )

    snapshot = store.repeat_snapshot()
    entry = snapshot["durable"]["targets"]["mono:woofer"]
    assert entry["status"] == "malformed"
    assert snapshot["failures"]["mono:woofer"]["status"] == "malformed"
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.asyncio
async def test_automatic_apply_direct_post_refuses_unresolved_ready_controller(
    monkeypatch
):
    from jasper.active_speaker import (
        baseline_profile,
        crossover_preview,
        design_draft,
        measurement,
        repeat_admission,
        setup_status,
    )
    from jasper import output_topology

    topology = SimpleNamespace(topology_id="topology-1")
    measurements = {
        "active_comparison_set": dict(_COMPARISON_SET),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _driver_acoustic("woofer"),
            },
            "latest_reference_axis_driver_measurements": {
                "mono:woofer": _reference_axis_driver_acoustic("woofer"),
            },
        },
    }
    monkeypatch.setattr(output_topology, "load_output_topology", lambda: topology)
    monkeypatch.setattr(design_draft, "load_design_draft", lambda: {})
    monkeypatch.setattr(
        crossover_preview, "load_crossover_preview", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: measurements,
    )
    monkeypatch.setattr(
        measurement,
        "active_driver_targets",
        lambda _topology: [{
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
        }],
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda: {
            "protected_profile": {
                "candidate_fingerprint": "protected-profile"
            }
        },
    )
    apply_calls = []
    monkeypatch.setattr(
        baseline_profile,
        "apply_baseline_profile",
        lambda **_kwargs: apply_calls.append(True),
    )
    monkeypatch.setattr(
        repeat_admission,
        "snapshot",
        lambda _comparison: {
            "targets": {
                "mono:woofer": {
                    "status": "ready",
                    "target_fingerprint": "6" * 64,
                }
            }
        },
    )
    with pytest.raises(ValueError, match="persistence.*complete"):
        await backend.apply_profile(
            tuning_owner="automatic",
            expected_candidate_fingerprint="source-1",
            camilla_factory=lambda: object(),
        )
    assert apply_calls == []


@pytest.mark.asyncio
async def test_automatic_apply_refuses_missing_reference_axis_controller(monkeypatch):
    from jasper.active_speaker import (
        baseline_profile,
        crossover_preview,
        design_draft,
        measurement,
        repeat_admission,
        setup_status,
    )
    from jasper import output_topology

    topology = SimpleNamespace(topology_id="topology-1")
    measurements = {
        "active_comparison_set": dict(_COMPARISON_SET),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _driver_acoustic("woofer"),
            },
            "latest_reference_axis_driver_measurements": {
                "mono:woofer": _reference_axis_driver_acoustic("woofer"),
            },
        },
    }
    monkeypatch.setattr(output_topology, "load_output_topology", lambda: topology)
    monkeypatch.setattr(design_draft, "load_design_draft", lambda: {})
    monkeypatch.setattr(
        crossover_preview, "load_crossover_preview", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: measurements,
    )
    monkeypatch.setattr(
        measurement,
        "active_driver_targets",
        lambda _topology: [{
            "target_id": "mono:woofer",
            "speaker_group_id": "mono",
            "role": "woofer",
            "target_fingerprint": "6" * 64,
        }],
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda: {
            "protected_profile": {
                "candidate_fingerprint": "protected-profile"
            }
        },
    )
    monkeypatch.setattr(
        repeat_admission,
        "snapshot",
        lambda _comparison: {
            "targets": {
                "mono:woofer": {
                    "status": "completed",
                    "target_fingerprint": "6" * 64,
                }
            }
        },
    )

    with pytest.raises(ValueError, match="fixed-axis.*complete"):
        await backend.apply_profile(
            tuning_owner="automatic",
            expected_candidate_fingerprint="automatic-candidate",
            camilla_factory=lambda: object(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "apply_allowed"),
    (
        (None, True),
        ("acoustic_near", False),
        ("acoustic_fixed", False),
        ("controller_near_all_four", False),
        ("controller_fixed_all_four", False),
        ("controller_near_duplicate", False),
        ("controller_fixed_duplicate", False),
        ("controller_near_incomplete", False),
        ("controller_fixed_incomplete", False),
        ("controller_near_scalar", False),
        ("controller_fixed_scalar", False),
        ("controller_near_inflight", False),
        ("controller_fixed_inflight", False),
        ("controller_near_valid_four", True),
        ("controller_fixed_valid_four", True),
    ),
)
async def test_automatic_apply_requires_exact_three_completed_geometry_bindings(
    monkeypatch, scenario, apply_allowed
):
    from jasper.active_speaker import (
        baseline_profile,
        crossover_preview,
        design_draft,
        measurement,
        repeat_admission,
        setup_status,
    )
    from jasper.active_speaker.capture_geometry import driver_repeat_binding
    from jasper import output_topology

    topology = SimpleNamespace(topology_id="topology-1")
    target = {
        "target_id": "mono:woofer",
        "speaker_group_id": "mono",
        "role": "woofer",
        "target_fingerprint": "6" * 64,
    }
    bindings = dict(
        driver_repeat_binding(
            speaker_group_id="mono",
            role="woofer",
            target_fingerprint="6" * 64,
            capture_geometry=geometry,
        )
        for geometry in ("near_field", "reference_axis")
    )
    measurements = {
        "active_comparison_set": dict(_COMPARISON_SET),
        "summary": {
            "latest_driver_measurements": {
                "mono:woofer": _driver_acoustic("woofer"),
            },
            "latest_reference_axis_driver_measurements": {
                "mono:woofer": _reference_axis_driver_acoustic("woofer"),
            },
        },
    }
    if scenario == "acoustic_near":
        measurements["summary"]["latest_driver_measurements"]["mono:woofer"]["repeats"][
            "accepted"
        ] = 4
    elif scenario == "acoustic_fixed":
        measurements["summary"]["latest_reference_axis_driver_measurements"][
            "mono:woofer"
        ]["repeats"]["accepted"] = 4
    repeat_state = {
        "targets": {
            target_id: {
                "status": "completed",
                "target_fingerprint": fingerprint,
                "attempts": 3,
                "results": [
                    {"attempt": attempt, "accepted": True}
                    for attempt in (1, 2, 3)
                ],
            }
            for target_id, fingerprint in bindings.items()
        }
    }
    fabricated_controller = {
        value: ("mono:woofer" if "near" in value else "reference_axis/mono:woofer")
        for value in (
            "controller_near_all_four",
            "controller_fixed_all_four",
            "controller_near_duplicate",
            "controller_fixed_duplicate",
            "controller_near_incomplete",
            "controller_fixed_incomplete",
            "controller_near_scalar",
            "controller_fixed_scalar",
            "controller_near_inflight",
            "controller_fixed_inflight",
            "controller_near_valid_four",
            "controller_fixed_valid_four",
        )
    }.get(scenario)
    if fabricated_controller is not None:
        if scenario.endswith("duplicate"):
            attempts = 3
            results = [{"attempt": 1, "accepted": True}] * 3
        elif scenario.endswith("incomplete"):
            attempts = 4
            results = [{"attempt": attempt, "accepted": True} for attempt in (1, 2, 3)]
        elif scenario.endswith("valid_four"):
            attempts = 4
            results = [
                {"attempt": 1, "accepted": True},
                {"attempt": 2, "accepted": False},
                {"attempt": 3, "accepted": True},
                {"attempt": 4, "accepted": True},
            ]
        elif scenario.endswith("scalar"):
            attempts = 3
            results = [
                {"attempt": attempt, "accepted": True}
                for attempt in (1, 2, 3)
            ] + [7]
        elif scenario.endswith("inflight"):
            attempts = 3
            results = [
                {"attempt": attempt, "accepted": True}
                for attempt in (1, 2, 3)
            ]
        else:
            attempts = 4
            results = [
                {"attempt": attempt, "accepted": True} for attempt in (1, 2, 3, 4)
            ]
        repeat_state["targets"][fabricated_controller].update(
            {
                "attempts": attempts,
                "target": 3,
                "results": results,
            }
        )
        if scenario.endswith("inflight"):
            repeat_state["targets"][fabricated_controller]["inflight"] = "owned"
    monkeypatch.setattr(output_topology, "load_output_topology", lambda: topology)
    monkeypatch.setattr(design_draft, "load_design_draft", lambda: {})
    monkeypatch.setattr(
        crossover_preview, "load_crossover_preview", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: measurements,
    )
    monkeypatch.setattr(
        measurement, "active_driver_targets", lambda _topology: [target]
    )
    monkeypatch.setattr(
        repeat_admission,
        "snapshot",
        lambda _comparison: repeat_state,
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda: {
            "protected_profile": {
                "candidate_fingerprint": "protected-profile"
            }
        },
    )
    calls = []

    async def fake_apply(*_args, **_kwargs):
        calls.append(True)
        return {"status": "applied", "issues": []}

    monkeypatch.setattr(baseline_profile, "apply_baseline_profile", fake_apply)

    if not apply_allowed:
        with pytest.raises(ValueError, match="must all be complete"):
            await backend.apply_profile(
                tuning_owner="automatic",
                expected_candidate_fingerprint="automatic-candidate",
                camilla_factory=lambda: object(),
            )
        assert calls == []
    else:
        payload = await backend.apply_profile(
            tuning_owner="automatic",
            expected_candidate_fingerprint="automatic-candidate",
            camilla_factory=lambda: object(),
        )
        assert payload["status"] == "applied"
        assert calls == [True]


def test_crossover_envelope_requires_new_level_check_after_repeat_abort():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {
            "mono:woofer": {
                "status": "aborted",
                "reason": "correction_service_restarted",
                "attempts": 3,
                "accepted": 2,
                "target": 3,
            }
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert "attempts were preserved" in env["verdict_text"]
    assert env["next_action"] == {
        "id": "level_match",
        "label": "Restart woofer driver level check",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }


def test_ready_controller_blocks_apply_even_after_measurement_write():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"] = {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {},
    }
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }
    status["level_match"]["repeats"] = {
        "targets": {
            "mono:woofer": {
                "attempts": 3,
                "accepted": 3,
                "target": 3,
                "status": "ready",
            }
        },
        "failures": {},
        "durable": {
            "targets": {
                "mono:woofer": {
                    "attempts": 3,
                    "status": "ready",
                    "inflight": None,
                    "results": [{"attempt": 3, "accepted": True}],
                }
            }
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert any(
        nudge["code"] == "crossover_repeat_persistence_interrupted"
        for nudge in env["nudges"]
    )


@pytest.mark.parametrize("controller_status", ["aborted", "refused"])
def test_terminal_controller_blocks_apply_with_complete_candidate(controller_status):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"] = {
        "latest_driver_measurements": {
            "mono:woofer": _driver_acoustic("woofer"),
            "mono:tweeter": _driver_acoustic("tweeter"),
        },
        "latest_summed_validations": {},
    }
    status["setup"]["automatic_candidate"] = {
        "ready": True,
        "reason": None,
        "candidate_fingerprint": "automatic-candidate",
    }
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {},
        "durable": {
            "targets": {
                "mono:woofer": {
                    "attempts": 3,
                    "status": controller_status,
                    "inflight": None,
                    "results": [{"attempt": 3, "accepted": True}],
                },
                "mono:tweeter": {
                    "attempts": 3,
                    "status": "completed",
                    "inflight": None,
                    "results": [{"attempt": 3, "accepted": True}],
                },
            }
        },
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert env["next_action"]["endpoint"] == "/correction/crossover/level-match"
    assert env["next_action"]["id"] != "apply_automatic"
    assert "cannot be resumed" in env["verdict_text"]
    assert all(
        nudge["code"] != "crossover_repeat_rejected" for nudge in env["nudges"]
    )


def test_fresh_process_status_drives_restart_failure_into_envelope(
    monkeypatch, tmp_path
):
    from jasper.active_speaker import (
        baseline_profile,
        crossover_envelope,
        repeat_admission,
        setup_status,
        web_commissioning,
    )

    path = tmp_path / "repeat.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE", str(path))
    comparison = dict(_COMPARISON_SET)
    repeat_admission.activate(comparison, path=path)
    repeat_admission.reserve(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="woofer-fp",
        path=path,
    )
    monkeypatch.setattr(repeat_admission, "OWNER_ID", "fresh-process")
    repeat_admission.claim_owner(path=path)
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {
            "ok": True,
            "targets": _envelope_status()["targets"],
            "measurements": {
                "active_comparison_set": comparison,
                "summary": {},
            },
        },
    )
    monkeypatch.setattr(web_commissioning, "commission_status_payload", lambda: {})
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda: {
            "status": "ready",
            "protected_profile": {"source_fingerprint": "protected-profile"},
        },
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(backend, "_LEVEL_LEASE", backend.CrossoverLevelLease())

    live = backend.status_payload()
    failure = live["level_match"]["repeats"]["failures"]["mono:woofer"]
    assert failure["status"] == "aborted"
    assert failure["attempts"] == 1

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["repeats"] = live["level_match"]["repeats"]
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"


def test_backend_status_redacts_repeat_token_and_owner(monkeypatch, tmp_path):
    import json

    from jasper.active_speaker import (
        baseline_profile,
        repeat_admission,
        setup_status,
        web_commissioning,
    )

    path = tmp_path / "repeat.json"
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE", str(path))
    comparison = dict(_COMPARISON_SET)
    repeat_admission.activate(comparison, path=path)
    reservation = repeat_admission.reserve(
        comparison,
        target_id="mono:woofer",
        target_fingerprint="woofer-fp",
        path=path,
    )
    monkeypatch.setattr(
        web_measurement,
        "status_payload",
        lambda: {
            "ok": True,
            "targets": _envelope_status()["targets"],
            "measurements": {
                "active_comparison_set": comparison,
                "summary": {},
            },
        },
    )
    monkeypatch.setattr(web_commissioning, "commission_status_payload", lambda: {})
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda: {
            "status": "ready",
            "protected_profile": {"source_fingerprint": "protected-profile"},
        },
    )
    monkeypatch.setattr(
        baseline_profile, "load_applied_baseline_profile_state", lambda: {}
    )
    monkeypatch.setattr(backend, "_LEVEL_LEASE", backend.CrossoverLevelLease())
    payload = backend.status_payload()
    public_repeats = payload["level_match"]["repeats"]
    serialized = json.dumps(public_repeats)
    assert reservation["token"] not in serialized
    assert "owner_id" not in serialized
    assert public_repeats["durable"]["targets"]["mono:woofer"][
        "inflight"
    ] is True
    backend._LEVEL_LEASE.set_durable_repeat_progress({
        "targets": {
            "mono:woofer": {
                "target_id": "mono:woofer",
                "target_fingerprint": "woofer-fp",
                "owner_id": "owner-secret",
                "attempts": 4,
                "status": "refused",
                "inflight": None,
                "results": [{
                    "attempt": 4,
                    "accepted": False,
                    "reject_reason": "capture_failed",
                    "detail": reservation["token"],
                }],
            }
        }
    })
    refused_serialized = json.dumps(backend._LEVEL_LEASE.repeat_snapshot())
    assert reservation["token"] not in refused_serialized
    assert "owner-secret" not in refused_serialized
    assert "detail" not in refused_serialized


@pytest.mark.parametrize("controller_status", ["active", "ready"])
def test_orphaned_inflight_or_ready_without_measurement_restarts_level_check(
    controller_status
):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {},
        "durable": {
            "targets": {
                "mono:woofer": {
                    "status": controller_status,
                    "inflight": "a" * 32 if controller_status == "active" else None,
                    "attempts": 3,
                    "results": [],
                }
            }
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert any(
        nudge["code"] == "crossover_repeat_persistence_interrupted"
        for nudge in env["nudges"]
    )


@pytest.mark.parametrize(
    "relay_status", ["awaiting_phone", "finishing", "committing", "stopping"]
)
def test_live_relay_does_not_misclassify_its_inflight_repeat_as_orphaned(
    relay_status,
):
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["relay"] = {"status": relay_status, "kind": "crossover_sweep:driver"}
    status["level_match"]["repeats"] = {
        "targets": {},
        "failures": {},
        "durable": {
            "targets": {
                "mono:woofer": {
                    "status": "active",
                    "inflight": "a" * 32,
                    "attempts": 1,
                    "results": [],
                }
            }
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "waiting"
    assert env["next_action"] is None
    if relay_status == "stopping":
        assert "restoring the speaker safely" in env["verdict_text"]
    elif relay_status == "finishing":
        assert "phone is finishing and uploading" in env["verdict_text"]
    elif relay_status == "committing":
        assert "saving the verified measurement" in env["verdict_text"]
    assert not any(
        nudge["code"] == "crossover_repeat_persistence_interrupted"
        for nudge in env["nudges"]
    )


def test_v3_capture_plan_session_renders_passive_measuring_state():
    """SPEC W2.3: while one relay session carries the driver's whole repeat
    set (protocol v3), the wizard is a passive progress mirror — no wizard
    action (the page's red Stop is the only control), and the per-repeat
    "Measure {role}" actions never render."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["relay"] = {
        "status": "awaiting_phone",
        "kind": "crossover_sweep:driver",
        "capture_plan": {
            "role": "woofer",
            "capture_target": 3,
            "accepted": 1,
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "waiting"
    assert env["verdict_text"] == (
        "Measuring the woofer — follow your phone. 1 of 3 done."
    )
    assert env["next_action"] is None
    assert env["alternate_actions"] == []
    # The relay snapshot (with its Stop affordance) passes through untouched.
    assert env["relay"]["capture_plan"]["capture_target"] == 3

    # The passive copy holds through the per-capture finishing/committing
    # phases too — the phone still owns the next action mid-set.
    for phase in ("finishing", "committing"):
        status["relay"]["status"] = phase
        env = crossover_envelope.build_crossover_envelope(status)
        assert env["screen"] == "waiting"
        assert "follow your phone" in env["verdict_text"]
        assert env["next_action"] is None


def test_v3_capture_plan_stop_keeps_the_honest_stopping_copy():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["relay"] = {
        "status": "stopping",
        "kind": "crossover_sweep:driver",
        "capture_plan": {
            "role": "woofer",
            "capture_target": 3,
            "accepted": 2,
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "waiting"
    assert "restoring the speaker safely" in env["verdict_text"]
    assert env["next_action"] is None


def test_v3_session_death_recovers_through_the_existing_driver_terminal():
    """Session death (TTL / failure) leaves the in-flight relay statuses, so
    the envelope falls back to the ordinary v2 per-driver action — the
    documented recovery path (SPEC W2.3)."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["relay"] = {
        "status": "failed",
        "kind": "crossover_sweep:driver",
        "error": "phone never armed within 120s",
        "capture_plan": {
            "role": "woofer",
            "capture_target": 3,
            "accepted": 1,
        },
    }
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "driver"
    assert env["next_action"]["id"] == "measure_driver"
    assert env["next_action"]["body"]["role"] == "woofer"


def test_durable_level_run_keeps_waiting_without_volatile_relay_state():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["run"] = {
        "schema_version": 1,
        "run_id": "a" * 32,
        "phase": "awaiting_phone",
        "phone_timeout": False,
        "late_success": False,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "waiting"
    assert env["next_action"] is None
    assert env["schema_version"] == 6


def test_phone_timeout_keeps_exact_run_waiting_and_explains_correlation():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["run"] = {
        "schema_version": 1,
        "run_id": "a" * 32,
        "phase": "running",
        "phone_timeout": True,
        "late_success": False,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "waiting"
    assert env["next_action"] is None
    assert "same exact" in env["verdict_text"]
    assert any(
        nudge["code"] == "crossover_level_run_phone_timeout"
        for nudge in env["nudges"]
    )


def test_late_success_is_visible_from_durable_run_state():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["run"] = {
        "schema_version": 1,
        "run_id": "a" * 32,
        "phase": "succeeded",
        "phone_timeout": True,
        "late_success": True,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"
    assert any(
        nudge["code"] == "crossover_level_run_late_success"
        for nudge in env["nudges"]
    )


def test_unavailable_level_run_state_refuses_retry_and_surfaces_diagnostics():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"]["run"] = {
        "schema_version": 1,
        "phase": "failed",
        "terminal_reason": "state_unavailable",
        "late_success": False,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"] is None
    assert "refusing" in env["verdict_text"]
    assert any(
        nudge["code"] == "crossover_level_run_state_unavailable"
        for nudge in env["nudges"]
    )


@pytest.mark.parametrize("applied_owner", ["manual", "automatic"])
def test_applied_profile_keeps_partial_driver_retune_active(applied_owner):
    """The old applied result must not hide the next driver between relays."""
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["applied_profile"] = {
        "status": "applied",
        "tuning_owner": applied_owner,
        "recomposition_snapshot": {
            "schema_version": 1,
            "level_match": {
                "active_comparison_set_id": _COMPARISON_SET[
                    "comparison_set_id"
                ],
            },
        },
    }
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": applied_owner,
        "reason": None,
    }
    status["measurements"]["active_comparison_set"] = None
    status["level_match"] = {
        "running": False,
        "valid": True,
        "ready": False,
        "targets": [
            {"target_id": "mono:woofer", "role": "woofer"},
            {
                "target_id": "mono:tweeter",
                "speaker_group_id": "mono",
                "role": "tweeter",
                "tone_frequency_hz": 6250.0,
            },
        ],
        "driver_level_locks": {
            "mono:woofer": {"target_id": "mono:woofer"},
        },
        "missing_targets": ["mono:tweeter"],
        "next_target": {
            "target_id": "mono:tweeter",
            "speaker_group_id": "mono",
            "role": "tweeter",
            "tone_frequency_hz": 6250.0,
        },
        "last": {"ramp": {"state": "locked", "restored": True}},
    }
    status["relay"] = {
        "status": "complete",
        "kind": "level_ramp:crossover",
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"] == {
        "id": "level_match",
        "label": "Set tweeter microphone level",
        "endpoint": "/correction/crossover/level-match",
        "body": {},
    }
    assert "6250 Hz" in env["verdict_text"]


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


def test_stale_locked_level_without_comparison_set_returns_to_level_step():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["active_comparison_set"] = None

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "microphone"
    assert env["next_action"]["id"] == "level_match"


def test_crossover_envelope_surfaces_bounded_low_lock_without_blocking_sweeps():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["level_match"] = {
        "running": False,
        "valid": True,
        "ready": True,
        "context_id": "protected-profile",
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
    # W2.1 deleted the user-facing bounded-low nudge (the level solver, not
    # the lock window, now decides sweep level -- bounded_low_level is an
    # internal lock annotation only). A bounded-low lock must still not
    # block sweeps from being offered.
    assert env["nudges"] == []


def _refusal(target_id: str, role: str) -> dict:
    return {
        "target_id": target_id,
        "role": role,
        "code": "room_too_noisy_for_safe_measurement",
        "failing_band_hz": [40.0, 400.0],
        "required_db": 20.0,
        "available_db": 15.3,
    }


def test_crossover_envelope_renders_level_solve_refusal_before_measuring():
    """W2.1: a closed-loop level-solve refusal fires BEFORE any tone plays --
    the envelope must render the dedicated terminal instead of offering
    "Measure {role}", which would just burn a doomed repeat attempt."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["solve_refusal"] = _refusal("mono:woofer", "woofer")

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "level_solve_refused"
    assert "40" in env["verdict_text"] and "400" in env["verdict_text"]
    assert "too high to measure reliably at safe levels" in env["verdict_text"]
    assert env["next_action"]["id"] == "level_match"
    assert env["next_action"]["endpoint"] == "/correction/crossover/level-match"
    assert env["progress"]["position"] == 2  # microphone step
    # Honest copy: posting /level-match from this state re-runs the guided
    # level sequence (today ambient is a ramp byproduct, so a quieter room
    # can only be re-measured by re-locking -- the `continuing` gate in
    # _handle_crossover_relay_level_match invalidates the prior locks). The
    # label and verdict must say the level check gets redone; neither may
    # imply the saved levels survive.
    assert env["next_action"]["label"] == (
        "Redo the quick level check (about 2 minutes)"
    )
    assert "redo the quick level check" in env["verdict_text"]
    assert "lock" not in env["verdict_text"].lower()


def test_crossover_envelope_refusal_screen_hides_stale_repeat_rejected_nudge():
    """W2.4 (hardware run 20): a level-solve refusal can coexist with a
    PRIOR rejected repeat attempt's evidence still sitting in the durable
    repeat ledger -- that rejection is very often WHY the correction that
    then refused was written in the first place. The generic
    "crossover_repeat_rejected" nudge is built early, straight from that
    ledger entry, independent of which screen ultimately renders, and its
    copy ("nothing to fix in the room -- try again" / "JTS will try again a
    bit quieter") flatly contradicts the refusal screen's own honest verdict
    (quiet the room / move the mic / redo the level check) and duplicates
    its action. It must not render alongside level_solve_refused."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["solve_refusal"] = _refusal("mono:woofer", "woofer")
    status["level_match"]["repeats"] = {
        "durable": {
            "targets": {
                "mono:woofer": {
                    "status": "active",
                    "results": [
                        {
                            "accepted": False,
                            "reject_reason": "insufficient_snr",
                            "snr_shortfall_db": 5.2,
                            "worst_band_id": "80_160hz",
                        }
                    ],
                }
            }
        }
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "level_solve_refused"
    assert "crossover_repeat_rejected" not in {n["code"] for n in env["nudges"]}


def test_crossover_envelope_refusal_scoped_to_the_refused_target():
    """A refusal for a DIFFERENT target than the one currently being
    measured must not hijack this driver's screen."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["solve_refusal"] = _refusal("mono:tweeter", "tweeter")

    env = crossover_envelope.build_crossover_envelope(status)

    # missing_drivers[0] is the woofer (measurements.summary is empty); the
    # stored refusal is for the tweeter, so the woofer's normal
    # "measure_driver" flow renders unaffected.
    assert env["screen"] == "driver"
    assert env["next_action"]["id"] == "measure_driver"


def test_crossover_envelope_reference_axis_level_solve_refusal():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["measurements"]["summary"]["latest_driver_measurements"] = {
        "mono:woofer": _driver_acoustic("woofer"),
        "mono:tweeter": _driver_acoustic("tweeter"),
    }
    _completed_near_field_repeat_state(status)
    status["level_match"]["reference_axis_driver_locks"] = {"mono:woofer": -3.0}
    status["level_match"]["solve_refusal"] = _refusal("mono:woofer", "woofer")

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "level_solve_refused"
    assert env["next_action"]["id"] == "level_match"
    # Same honest-copy contract as the near-field branch.
    assert env["next_action"]["label"] == (
        "Redo the quick level check (about 2 minutes)"
    )
    assert "redo the quick level check" in env["verdict_text"]


def test_crossover_envelope_renders_measurement_window_unreachable_refusal():
    """W2.2: a target that burned its bounded clip/SNR correction budget
    (CrossoverLevelLease.record_solve_correction) and rejected again gets
    the honest mic-placement copy, not the room_too_noisy copy -- and, like
    every level-solve refusal, does not touch the level lock."""

    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["level_match"]["solve_refusal"] = {
        "target_id": "mono:woofer",
        "role": "woofer",
        "code": "measurement_window_unreachable",
        "failing_band_hz": [40.0, 400.0],
        "required_db": -12.0,
        "available_db": 0.0,
    }

    env = crossover_envelope.build_crossover_envelope(status)

    assert env["screen"] == "level_solve_refused"
    assert env["next_action"]["id"] == "level_match"
    assert env["progress"]["position"] == 2  # microphone step
    assert env["verdict_text"] == (
        "The microphone can't get a clean reading at this distance — "
        "it's picking up too much on loud passages and too little on "
        "quiet ones. Move the phone close to the driver being measured "
        "(about 3 cm / just over an inch away), then measure again."
    )
    # Honest, provider/jargon-free copy: no internal band/dB numbers or
    # "room" framing leak into this mic-placement message.
    assert "Hz" not in env["verdict_text"]
    assert "room" not in env["verdict_text"].lower()
    # The lock survives -- verified via _locked_level's ramp state staying
    # untouched (the refusal only ever READS level_match state here).
    assert status["level_match"]["last"]["ramp"]["state"] == "locked"


# --- phone-mic relay transport (P7) -------------------------------------------


def test_relay_kind_validation():
    assert flow.relay_kind_from_raw({"kind": "driver"}) == "driver"
    assert flow.relay_kind_from_raw({"kind": "summed"}) == "summed"
    assert flow.relay_kind_from_raw({"kind": "verification"}) == "verification"
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
    """Fake the relay and host-service boundaries around the real play path.

    ``run_capture`` arms then returns a CaptureResult-shaped object; purge is
    recorded. Renderer probes/stops are isolated because this suite runs on
    development hosts without systemd. The correction play/record path stays
    real.
    """
    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator

    purged = {}

    class AlwaysActive:
        def __init__(self, *_args):
            pass

        def assert_active(self):
            return None

    def fake_run_capture(client, pi_session, *, on_armed, **kw):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))  # phone armed + acknowledged → Pi plays the sweep
        return SimpleNamespace(wav=wav, device={"label": "iPhone mic"})

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AlwaysActive)
    monkeypatch.setattr(
        relay_session, "purge", lambda c, s: purged.setdefault("done", True)
    )

    async def acquire_measurement_gate():
        purged.setdefault("gate", []).append("acquire")

    async def release_measurement_gate(**_kwargs):
        purged.setdefault("gate", []).append("release")

    monkeypatch.setattr(
        coordinator,
        "_acquire_measurement_gate",
        acquire_measurement_gate,
    )
    monkeypatch.setattr(
        coordinator,
        "_release_measurement_gate",
        release_measurement_gate,
    )
    return purged


def _relay_pi_session(kind: str, *, session_id: str = "sid") -> SimpleNamespace:
    from jasper.capture_relay.spec import build_crossover_sweep_spec

    role = "woofer" if kind == "driver" else "summed"
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver" if kind == "driver" else "summed crossover",
        driver_role=role,
        acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
    )
    return SimpleNamespace(session_id=session_id, pull_token="ptok", spec=spec)


def _relay_contract() -> dict:
    return {
        "comparison_set": _COMPARISON_SET,
        "target_fingerprint": "",
        "ambient_duration_s": 0.0,
    }


def _real_play_boundary(monkeypatch, tmp_path, *, kind):
    """Boundary mocks that let the REAL play_driver/summed_capture_sweep run
    hardware-free: state loaders return real-shaped states, CamillaDSP
    config-load/rollback + fan-in lane + aplay are stubbed at their seams, and
    the sweep WAV (hence the REAL sweep_meta) is generated for real into an
    env-pointed cache dir. Mirrors tests/test_active_speaker_web_commissioning.py."""
    import jasper.correction.playback as correction_playback
    from jasper.active_speaker import web_commissioning as web
    from jasper.active_speaker.measurement import active_driver_targets
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_SWEEP_DIR", str(tmp_path / "sweeps"))
    topology = _topology()
    from tests.test_active_speaker_web_commissioning import (
        _applied_excitation_profile,
    )

    applied_profile = _applied_excitation_profile(topology=topology)
    if kind == "driver":
        from contextlib import asynccontextmanager

        from jasper import dsp_apply
        from jasper.active_speaker import baseline_profile
        from jasper.active_speaker import commissioning_admission, design_draft
        from jasper.audio_measurement.sweep import synchronized_sweep_metadata

        monkeypatch.setattr(
            baseline_profile,
            "load_applied_baseline_profile_state",
            lambda: applied_profile,
        )
        monkeypatch.setattr(
            design_draft,
            "load_design_draft",
            lambda: {"driver_safety_profile": {"status": "confirmed"}},
        )

        @asynccontextmanager
        async def _writer_lock(*_args, **_kwargs):
            yield

        async def _admitted(**_kwargs):
            return SimpleNamespace(
                sweep_meta=synchronized_sweep_metadata(
                    f1=500.0,
                    f2=8000.0,
                    duration_approx_s=4.0,
                    amplitude_dbfs=-12.0,
                ),
                handoff=SimpleNamespace(
                    admission_id="admission-woofer",
                    to_dict=lambda: {"admission_id": "admission-woofer"},
                ),
            )

        monkeypatch.setattr(dsp_apply, "dsp_writer_lock", _writer_lock)
        monkeypatch.setattr(
            commissioning_admission, "play_admitted_driver_capture", _admitted
        )
        monkeypatch.setattr(web, "commission_seams", lambda _cam: (None, None, None))
        driver_target = next(
            target
            for target in active_driver_targets(topology)
            if target["role"] == "woofer"
        )
        woofer_confirmation = {
            "captured": True,
            "target_id": "mono:woofer",
            "target_fingerprint": driver_target["target_fingerprint"],
            "speaker_group_id": "mono",
            "role": "woofer",
            "output_index": 0,
            "outcome": "heard_correct_driver",
            "playback_id": "play-woofer",
            "test_level_dbfs": -72.0,
            "floor_confirmation": {
                "accepted": True,
                "playback_id": "play-woofer",
                "target": {
                    "speaker_group_id": "mono",
                    "role": "woofer",
                    "output_index": 0,
                },
            },
            "issues": [],
        }
        measurements = {
            "summary": {
                "latest_driver_measurements": {
                    "mono:woofer": woofer_confirmation,
                },
                # No acoustic block -- an accurate real-shaped by-ear
                # confirmation record, so it belongs in the confirmation-only
                # index too (see measurement._latest_current_driver_confirmations).
                "latest_driver_confirmations": {
                    "mono:woofer": woofer_confirmation,
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
    measurements["active_comparison_set"] = _COMPARISON_SET
    monkeypatch.setattr(web, "load_output_topology", lambda: topology)
    monkeypatch.setattr(web, "load_measurement_state", lambda topology: measurements)
    monkeypatch.setattr(web, "load_safe_playback_state", lambda: {"status": "armed"})
    monkeypatch.setattr(
        web,
        "resolve_commission_inputs",
        (
            lambda: pytest.fail("driver sweep must use the applied snapshot")
            if kind == "driver"
            else lambda: (object(), None)
        ),
    )
    monkeypatch.setattr(web, "commission_status_payload", lambda: {})
    if kind == "driver":
        monkeypatch.setattr(
            web,
            "automatic_driver_excitation",
            lambda _topology, role, *, applied_profile=None,
            locked_main_volume_db=None: {
                "status": "ready",
                "schema_version": 1,
                "scope": "sweep_plus_role_gain_and_driver_level_lock",
                "sweep_peak_dbfs": -12.0,
                "commissioning_gain_db": -9.0,
                "effective_peak_dbfs": -21.0 + float(locked_main_volume_db or 0.0),
                "gain_source": web.AUTOMATIC_EXCITATION_GAIN_SOURCE,
                "baseline_id": "baseline-1",
                "topology_id": "topology-1",
                "role": role,
                "locked_main_volume_db": float(locked_main_volume_db),
            },
        )

    async def _loaded(**kwargs):
        return {"load": {"status": "loaded"}}

    async def _rolled_back(*args, **kwargs):
        return {"status": "rolled_back"}

    async def _restored_driver_entry(*args, **kwargs):
        return {"status": "rolled_back", "config_path": "/tmp/sound_current.yml"}

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
        "_restore_automatic_driver_entry_config",
        _restored_driver_entry,
    )
    monkeypatch.setattr(
        web,
        "_rollback_applied_summed_measurement_config",
        _rolled_back,
    )
    monkeypatch.setattr(
        web, "_commission_tone_select_fanin_lane", lambda *_a: {"status": "ok"}
    )
    monkeypatch.setattr(
        web,
        "_commission_tone_release_fanin_lane",
        lambda *, reason, fanin_gate_context=None: {
            "status": "ok", "reason": reason,
        },
    )

    async def _fake_play_sweep(wav_path, *, alsa_device, timeout_s):
        return None

    monkeypatch.setattr(correction_playback, "play_sweep", _fake_play_sweep)
    return applied_profile


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

    applied_profile = _real_play_boundary(monkeypatch, tmp_path, kind="driver")
    purged = _fake_relay_transport(monkeypatch)

    record_calls = {}

    def fake_record_driver(
        raw,
        wav_bytes,
        *,
        placement_proof,
        admission_handoff,
        preset=None,
        repeat_store=None,
    ):
        record_calls["raw"] = raw
        record_calls["wav"] = wav_bytes
        record_calls["placement_proof"] = placement_proof
        record_calls["preset"] = preset
        record_calls["repeat_store"] = repeat_store
        record_calls["admission_handoff"] = admission_handoff
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
        applied_profile=applied_profile,
        driver_locked_main_volume_db=lambda: -12.0,
        **_relay_contract(),
    )
    await run_and_consume(object(), _relay_pi_session("driver"))

    # The REAL sweep_meta (generated by _measurement_sweep_wav_path from
    # driver_acoustics defaults) rode into the record call — the deconv basis
    # is the played sweep, never the phone WAV.
    raw = record_calls["raw"]
    assert record_calls["wav"] == b"phone-wav-bytes"
    assert record_calls["placement_proof"]["policy_id"] == (
        "driver_same_distance_v1"
    )
    assert record_calls["admission_handoff"] == {
        "admission_id": "admission-woofer"
    }
    assert raw["role"] == "woofer"
    assert raw["playback_id"]
    # The old by-ear -72 dB floor record is identity evidence only. The played
    # automatic sweep uses the protected applied role gain.
    assert raw["test_level_dbfs"] == -9.0
    assert raw["excitation"] == {
        "schema_version": 1,
        "scope": "sweep_plus_role_gain_and_driver_level_lock",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -9.0,
        "effective_peak_dbfs": -33.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": "topology-1",
        "role": "woofer",
        "locked_main_volume_db": -12.0,
    }
    assert record_calls["preset"].crossover_regions[0].fc_hz == 1600.0
    meta = raw["sweep_meta"]
    assert meta["sample_rate"] == 48000
    assert meta["duration_s"] > 0  # real synchronized-sine meta, not a stub
    assert {"f1", "f2", "n_samples", "amplitude_dbfs"} <= set(meta)
    assert meta["amplitude_dbfs"] == -12.0
    assert purged["done"] is True
    assert purged["gate"] == ["acquire", "release"]
    assert host_events == ["sweep_started", "sweep_complete"]


@pytest.mark.asyncio
async def test_crossover_relay_driver_sweep_nests_under_correction_measurement_gate(
    monkeypatch, tmp_path
):
    """The crossover-driver-sweep relay flow's _play() runs INSIDE
    coordinator.measurement_window(), which already holds the mux's single
    test fan-in gate under owner=MEASUREMENT_GATE_OWNER
    ('correction-measurement'). Before this fix, play_driver_capture_sweep
    always claimed its own 'active-speaker-commissioning' owner — refused
    outright by the mux while correction's owner already held the gate
    (hardware-observed on JTS3: RuntimeError "test fan-in gate is owned by
    'correction-measurement'", deterministic 2/2). This pins that
    build_crossover_relay_run_and_consume threads a FaninGateContext for the
    OUTER owner all the way down to _commission_tone_select_fanin_lane."""
    from jasper.active_speaker.web_commissioning import FaninGateContext
    from jasper.active_speaker import web_commissioning as wc
    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    applied_profile = _real_play_boundary(monkeypatch, tmp_path, kind="driver")
    _fake_relay_transport(monkeypatch)
    monkeypatch.setattr(
        be, "record_driver_capture", lambda *_a, **_k: {"recorded": True}
    )

    select_calls: list[FaninGateContext | None] = []

    def capture_select(fanin_gate_context=None):
        select_calls.append(fanin_gate_context)
        return {"status": "ok"}

    monkeypatch.setattr(wc, "_commission_tone_select_fanin_lane", capture_select)

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda *_a, **_k: None,
        blocking_phase=lambda: None,
        applied_profile=applied_profile,
        driver_locked_main_volume_db=lambda: -12.0,
        **_relay_contract(),
    )
    await run_and_consume(object(), _relay_pi_session("driver"))

    assert select_calls == [
        FaninGateContext(
            owner=coordinator.MEASUREMENT_GATE_OWNER,
            restore_label=coordinator.MEASUREMENT_FANIN_LABEL,
        ),
    ]


@pytest.mark.asyncio
async def test_crossover_relay_refuses_summed_without_group_admission(
    monkeypatch, tmp_path
):
    # Summed twin of the real-shape test: the REAL play_summed_capture_sweep
    # hoists summed_test_id/test_level_dbfs/sweep_meta to the top level; the
    # consume path must read them there.
    from jasper.web import correction_crossover_backend as be

    applied_profile = _real_play_boundary(monkeypatch, tmp_path, kind="summed")
    _fake_relay_transport(monkeypatch, wav=b"w")

    record_calls = {}
    play_calls = {}
    real_play_summed = be.play_summed_capture_sweep

    async def spy_play_summed(raw, **kwargs):
        play_calls["raw"] = dict(raw)
        return await real_play_summed(raw, **kwargs)

    monkeypatch.setattr(be, "play_summed_capture_sweep", spy_play_summed)

    def fake_record_summed(*_args, **_kwargs):
        pytest.fail("summed capture must not record without group admission")

    monkeypatch.setattr(
        be,
        "record_summed_capture",
        fake_record_summed,
    )

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {
            "kind": "summed",
            "speaker_group_id": "mono",
            "expect_null": False,
            "crossover_fc_hz": 1600.0,
            "polarity": "normal",
        },
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        blocking_phase=lambda: None,
        applied_profile=applied_profile,
        **_relay_contract(),
    )
    with pytest.raises(ValueError, match="multi-driver protection authority"):
        await run_and_consume(object(), _relay_pi_session("summed", session_id="s"))

    expected_region_fields = {
        "expect_null": False,
        "crossover_fc_hz": 1600.0,
        "polarity": "normal",
    }
    assert {
        key: play_calls["raw"][key] for key in expected_region_fields
    } == expected_region_fields
    assert record_calls == {}


@pytest.mark.asyncio
async def test_crossover_relay_never_records_an_unloaded_alignment_candidate(
    monkeypatch, tmp_path
):
    """Transport forwards the candidate, but unchanged playback cannot label it."""
    from jasper.web import correction_crossover_backend as be

    applied_profile = _real_play_boundary(monkeypatch, tmp_path, kind="summed")
    _fake_relay_transport(monkeypatch, wav=b"w")
    play_calls = {}
    real_play_summed = be.play_summed_capture_sweep

    async def spy_play_summed(raw, **kwargs):
        play_calls["raw"] = dict(raw)
        return await real_play_summed(raw, **kwargs)

    monkeypatch.setattr(be, "play_summed_capture_sweep", spy_play_summed)
    monkeypatch.setattr(
        be,
        "record_summed_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unplayed candidate must never be recorded")
        ),
    )
    candidate = {
        "expect_null": True,
        "crossover_fc_hz": 2500.0,
        "polarity": "invert_tweeter",
        "delay_ms": 0.35,
        "delay_target_role": "tweeter",
    }
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "summed", "speaker_group_id": "mono", **candidate},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        blocking_phase=lambda: None,
        applied_profile=applied_profile,
        **_relay_contract(),
    )

    with pytest.raises(ValueError, match="multi-driver protection authority"):
        await run_and_consume(object(), _relay_pi_session("summed", session_id="s"))

    assert {key: play_calls["raw"][key] for key in candidate} == candidate


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
        lambda _raw, _wav, *, placement_proof, admission_handoff,
        repeat_store=None: {"recorded": True},
    )

    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        prepare_play=prepare,
        restore_play=restore,
        **_relay_contract(),
    )
    await run_and_consume(object(), _relay_pi_session("driver", session_id="s"))

    assert order == ["window_enter", "prepare", "play", "restore", "window_exit"]


@pytest.mark.asyncio
async def test_driver_excitation_ledger_uses_reasserted_geometry_lock(monkeypatch):
    from contextlib import asynccontextmanager

    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    observed: dict[str, object] = {}

    @asynccontextmanager
    async def window():
        yield

    async def play(*_args, **kwargs):
        observed.update(kwargs)
        return {
            "status": "completed",
            "playback": {
                "audio_emitted": True,
                "excitation": {
                    "locked_main_volume_db": kwargs["locked_main_volume_db"]
                },
            },
            "sweep_meta": {"sample_rate": 48000},
        }

    async def prepare():
        return True

    async def restore():
        return True

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda *_args, **_kwargs: {"recorded": True},
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        prepare_play=prepare,
        restore_play=restore,
        driver_locked_main_volume_db=lambda: -3.5,
        **_relay_contract(),
    )

    await run_and_consume(object(), _relay_pi_session("driver", session_id="s"))

    assert observed["locked_main_volume_db"] == -3.5


@pytest.mark.asyncio
async def test_supplied_geometry_lock_callback_cannot_fall_back_when_missing(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    restored = []

    @asynccontextmanager
    async def window():
        yield

    async def play(*_args, **_kwargs):
        pytest.fail("missing geometry lock must refuse before playback")

    async def prepare():
        return True

    async def restore():
        restored.append(True)
        return True

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        prepare_play=prepare,
        restore_play=restore,
        driver_locked_main_volume_db=lambda: None,
        **_relay_contract(),
    )

    with pytest.raises(RuntimeError, match="geometry-scoped driver level lock"):
        await run_and_consume(
            object(), _relay_pi_session("driver", session_id="missing-lock")
        )

    assert restored == [True]


@pytest.mark.asyncio
async def test_repeat_admission_precedes_ambient_prepare_and_audio(monkeypatch):
    from contextlib import asynccontextmanager

    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    order = []

    @asynccontextmanager
    async def window():
        order.append("window")
        yield

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
            "sweep_meta": {"sample_rate": 48000},
        }

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda raw, *_a, **_k: order.append("record") or {"recorded": True},
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        reserve_repeat_attempt=lambda: order.append("reserve") or {
            "token": "token",
            "attempt": 1,
        },
        prepare_play=prepare,
        restore_play=restore,
        **_relay_contract(),
    )
    await run_and_consume(object(), _relay_pi_session("driver"))
    assert order == ["reserve", "window", "prepare", "play", "restore", "record"]


@pytest.mark.asyncio
async def test_repeat_reservation_failure_prevents_prepare_and_audio(monkeypatch):
    import logging

    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    play_calls = []
    events = []
    monkeypatch.setattr(
        flow,
        "log_event",
        lambda _logger, event, **fields: events.append((event, fields)),
    )

    async def play(*_args, **_kwargs):
        play_calls.append(True)
        return {"status": "completed", "playback": {"audio_emitted": True}}

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        reserve_repeat_attempt=lambda: (_ for _ in ()).throw(OSError("disk full")),
        **_relay_contract(),
    )
    with pytest.raises(OSError, match="disk full"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert play_calls == []
    assert events == [
        (
            "correction.crossover_repeat_persistence_failed",
            {"level": logging.ERROR, "reason": "OSError", "op": "reserve"},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_at", ["play", "restore", "run_capture", "purge", "validate", "record"]
)
async def test_every_post_reservation_failure_consumes_and_releases_attempt(
    monkeypatch, failure_at
):
    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    finish_calls = []

    def fake_run_capture(client, pi_session, *, on_armed, **_kwargs):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={"capture_protocol_version": 2, "capture_page_build": "test"},
        ))
        if failure_at == "run_capture":
            raise OSError("run capture failed")
        return SimpleNamespace(wav=b"wav", device={"label": "UMIK-2"})

    def purge(*_args):
        if failure_at == "purge":
            raise OSError("purge failed")

    async def play(*_args, **_kwargs):
        if failure_at == "play":
            raise OSError("play failed")
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    async def restore():
        return failure_at != "restore"

    def validate(_result):
        if failure_at == "validate":
            raise ValueError("device changed")

    def record(*_args, **_kwargs):
        if failure_at == "record":
            raise OSError("record failed")
        return {"recorded": True}

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "purge", purge)
    monkeypatch.setattr(
        relay_session,
        "CaptureActivityProbe",
        lambda *_args: SimpleNamespace(assert_active=lambda: None),
    )
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(be, "record_driver_capture", record)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        reserve_repeat_attempt=lambda: {"token": "token", "attempt": 4},
        finish_failed_repeat_attempt=lambda reservation, error: finish_calls.append(
            (dict(reservation), error)
        ),
        prepare_play=(lambda: _async_true()),
        restore_play=restore,
        validate_capture=validate,
        **_relay_contract(),
    )
    with pytest.raises((OSError, RuntimeError, ValueError)):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert len(finish_calls) == 1
    assert finish_calls[0][0]["attempt"] == 4


@pytest.mark.asyncio
async def test_authenticated_phone_abort_during_ambient_restores_before_window_exit(
    monkeypatch
):
    import asyncio
    import threading
    from contextlib import asynccontextmanager

    from jasper.capture_relay import session as relay_session
    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    prepared = threading.Event()
    restore_started = threading.Event()
    release_restore = threading.Event()
    order = []
    finish_calls = []
    play_calls = []
    runner_timeouts = []

    def fake_run_capture(client, pi_session, *, on_armed, **_kwargs):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))

    class AbortAfterPrepare:
        def __init__(self, *_args):
            pass

        def assert_active(self):
            assert prepared.wait(timeout=2)
            raise relay_session.CaptureAborted("phone backgrounded")

    @asynccontextmanager
    async def window():
        order.append("window_enter")
        try:
            yield
        finally:
            order.append("window_exit")

    async def prepare():
        order.append("prepare")
        prepared.set()
        return True

    async def restore():
        order.append("restore_start")
        restore_started.set()
        await asyncio.to_thread(release_restore.wait)
        order.append("restore_done")
        return True

    async def play(*_args, **_kwargs):
        play_calls.append(True)
        return {"status": "completed", "playback": {"audio_emitted": True}}

    def run_async(coro, timeout=None):
        runner_timeouts.append(timeout)
        return _run_coro(coro)

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AbortAfterPrepare)
    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        run_async,
        lambda: object(),
        reserve_repeat_attempt=lambda: {"token": "token", "attempt": 1},
        finish_failed_repeat_attempt=lambda reservation, failure_type: finish_calls.append(
            (dict(reservation), failure_type)
        ),
        prepare_play=prepare,
        restore_play=restore,
        ambient_duration_s=1.0,
        comparison_set=_COMPARISON_SET,
        target_fingerprint="target-fp",
    )
    task = asyncio.create_task(
        run_and_consume(object(), _relay_pi_session("driver"))
    )
    assert await asyncio.to_thread(restore_started.wait, 3)
    assert "window_exit" not in order
    release_restore.set()
    with pytest.raises(relay_session.CaptureAborted, match="backgrounded"):
        await task
    assert order == [
        "window_enter",
        "prepare",
        "restore_start",
        "restore_done",
        "window_exit",
    ]
    assert play_calls == []
    assert finish_calls == [({"token": "token", "attempt": 1}, "CaptureAborted")]
    from jasper.active_speaker.test_signal_plan import (
        CROSSOVER_CAPTURE_HARD_TIMEOUT_S,
    )

    assert runner_timeouts == [CROSSOVER_CAPTURE_HARD_TIMEOUT_S - 2.0]


@pytest.mark.asyncio
async def test_host_stop_drains_crossover_restore_before_returning(monkeypatch):
    import asyncio
    import threading

    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    stop_event = threading.Event()
    play_started = threading.Event()
    restore_started = threading.Event()
    release_restore = threading.Event()
    order = []

    class AlwaysActive:
        def __init__(self, *_args):
            pass

        def assert_active(self):
            return None

    def fake_run_capture(
        client,
        pi_session,
        *,
        on_armed,
        stop_requested,
        **_kwargs,
    ):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))
        if stop_requested():
            raise relay_session.CaptureStopped("capture stopped")
        raise AssertionError("capture returned without observing Stop")

    async def play(*_args, **_kwargs):
        order.append("play")
        play_started.set()
        await asyncio.Event().wait()

    async def restore():
        order.append("restore_start")
        restore_started.set()
        await asyncio.to_thread(release_restore.wait)
        order.append("restore_done")
        return True

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AlwaysActive)
    monkeypatch.setattr(
        relay_session,
        "purge",
        lambda *_args: order.append("purge"),
    )
    monkeypatch.setattr(flow, "CROSSOVER_CANCEL_OBSERVATION_GRACE_S", 0.0)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda _sid, _token, payload: order.append(
            payload["phase"]
        ),
        restore_play=restore,
        stop_event=stop_event,
        **_relay_contract(),
    )

    task = asyncio.create_task(
        run_and_consume(object(), _relay_pi_session("driver"))
    )
    assert await asyncio.to_thread(play_started.wait, 2)
    stop_event.set()
    assert await asyncio.to_thread(restore_started.wait, 2)
    assert not task.done()
    release_restore.set()
    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await task
    assert order == [
        "sweep_started",
        "play",
        "restore_start",
        "restore_done",
        "sweep_cancelled",
        "purge",
    ]


@pytest.mark.asyncio
async def test_pre_arm_stop_is_observable_before_relay_session_purge(monkeypatch):
    import asyncio

    from jasper.capture_relay import session as relay_session
    from jasper.capture_relay.client import RelayClient
    from tests.test_capture_relay_session import FakeRelayBackend

    backend = FakeRelayBackend()
    backend.sessions["sid"] = {
        "pull_token": "ptok",
        "host_event": None,
    }
    client = RelayClient("https://relay.test", transport=backend)

    def stopped_before_arm(*_args, **_kwargs):
        raise relay_session.CaptureStopped("capture stopped")

    monkeypatch.setattr(relay_session, "run_capture", stopped_before_arm)
    monkeypatch.setattr(flow, "CROSSOVER_CANCEL_OBSERVATION_GRACE_S", 0.05)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=client.post_host_event,
        **_relay_contract(),
    )

    task = asyncio.create_task(
        run_and_consume(client, _relay_pi_session("driver"))
    )
    for _ in range(100):
        session = backend.sessions.get("sid")
        if session is not None and session["host_event"] is not None:
            break
        await asyncio.sleep(0.001)

    assert backend.sessions["sid"]["host_event"] == {
        "phase": "sweep_cancelled"
    }
    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await task
    assert "sid" not in backend.sessions


@pytest.mark.asyncio
async def test_finishing_gate_follows_restore_and_precedes_phone_release(monkeypatch):
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    order = []

    async def play(*_args, **_kwargs):
        order.append("play")
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    async def restore():
        order.append("restore")
        return True

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda *_args, **_kwargs: order.append("record") or {"recorded": True},
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda _sid, _token, payload: order.append(
            payload["phase"]
        ),
        restore_play=restore,
        begin_finishing=lambda: order.append("finishing") or True,
        **_relay_contract(),
    )

    await run_and_consume(object(), _relay_pi_session("driver"))

    assert order == [
        "sweep_started",
        "play",
        "restore",
        "finishing",
        "sweep_complete",
        "record",
    ]


@pytest.mark.asyncio
async def test_finishing_gate_refusal_withholds_phone_release_and_evidence(monkeypatch):
    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    purged = _fake_relay_transport(monkeypatch)
    monkeypatch.setattr(flow, "CROSSOVER_CANCEL_OBSERVATION_GRACE_S", 0.0)
    host_events = []
    record_calls = []

    async def play(*_args, **_kwargs):
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda *_args, **_kwargs: record_calls.append(True),
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda _sid, _token, payload: host_events.append(
            payload["phase"]
        ),
        begin_finishing=lambda: False,
        **_relay_contract(),
    )

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await run_and_consume(object(), _relay_pi_session("driver"))

    assert host_events == ["sweep_started", "sweep_cancelled"]
    assert purged == {
        "gate": ["acquire", "release"],
        "done": True,
    }
    assert record_calls == []


@pytest.mark.asyncio
async def test_host_stop_after_capture_prevents_late_evidence_write(monkeypatch):
    import threading

    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    stop_event = threading.Event()
    record_calls = []

    class AlwaysActive:
        def __init__(self, *_args):
            pass

        def assert_active(self):
            return None

    def fake_run_capture(client, pi_session, *, on_armed, **_kwargs):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))
        stop_event.set()
        return SimpleNamespace(wav=b"wav", device={"label": "UMIK-2"})

    async def play(*_args, **_kwargs):
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AlwaysActive)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda *_args, **_kwargs: record_calls.append(True),
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        stop_event=stop_event,
        **_relay_contract(),
    )

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert record_calls == []


@pytest.mark.asyncio
async def test_commit_gate_refusal_prevents_evidence_write(monkeypatch):
    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    record_calls = []

    async def play(*_args, **_kwargs):
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    monkeypatch.setattr(
        be,
        "record_driver_capture",
        lambda *_args, **_kwargs: record_calls.append(True),
    )
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        begin_commit=lambda: False,
        **_relay_contract(),
    )

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert record_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_result", [False, RuntimeError("graph restore failed")])
async def test_stop_does_not_mask_playback_restore_failure(
    monkeypatch,
    restore_result,
):
    import asyncio
    import threading

    from jasper.web import correction_crossover_backend as be

    stop_event = threading.Event()
    _fake_relay_transport(monkeypatch)

    async def play(*_args, **_kwargs):
        stop_event.set()
        await asyncio.Event().wait()

    async def restore():
        if isinstance(restore_result, BaseException):
            raise restore_result
        return restore_result

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        restore_play=restore,
        stop_event=stop_event,
        **_relay_contract(),
    )

    expected = (
        "graph restore failed"
        if isinstance(restore_result, BaseException)
        else "volume was not restored"
    )
    with pytest.raises(RuntimeError, match=expected):
        await run_and_consume(object(), _relay_pi_session("driver"))


@pytest.mark.asyncio
async def test_stop_that_wins_armed_entry_cannot_reserve_repeat(monkeypatch):
    import threading

    from jasper.capture_relay import session as relay_session

    stop_event = threading.Event()
    stop_event.set()
    reservations = []
    host_events = []

    def fake_run_capture(_client, pi_session, *, on_armed, **_kwargs):
        required = pi_session.spec.acknowledgement
        on_armed(SimpleNamespace(
            acknowledgement={
                "schema_version": required.schema_version,
                "id": required.id,
                "binding_id": required.binding_id,
                "accepted": True,
            },
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        reserve_repeat_attempt=lambda: reservations.append(True) or {
            "token": "late",
            "attempt": 1,
        },
        post_host_event=lambda _sid, _token, payload: host_events.append(payload),
        stop_event=stop_event,
        **_relay_contract(),
    )

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert reservations == []
    assert host_events == [{"phase": "sweep_cancelled"}]


@pytest.mark.asyncio
async def test_stop_is_refused_after_commit_begins(monkeypatch):
    import asyncio
    import threading

    from jasper.web import correction_crossover_backend as be
    from jasper.web import correction_setup

    kind = "crossover_sweep:driver"
    record_started = threading.Event()
    release_record = threading.Event()
    stop_event = threading.Event()
    _fake_relay_transport(monkeypatch)

    async def play(*_args, **_kwargs):
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    def record(*_args, **_kwargs):
        record_started.set()
        assert release_record.wait(timeout=2)
        return {"recorded": True}

    monkeypatch.setattr(be, "record_driver_capture", record)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture(
        kind,
        request_stop=stop_event.set,
    )
    correction_setup._publish_relay_waiting(kind, "https://capture.test/#s=cap")
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        begin_commit=lambda: correction_setup._begin_relay_commit(kind),
        stop_event=stop_event,
        **_relay_contract(),
    )

    task = asyncio.create_task(
        asyncio.to_thread(
            asyncio.run,
            run_and_consume(object(), _relay_pi_session("driver")),
        )
    )
    try:
        assert await asyncio.to_thread(record_started.wait, 2)
        assert correction_setup._get_relay_capture()["status"] == "committing"
        with pytest.raises(ValueError, match="no matching phone capture"):
            correction_setup._request_relay_stop("crossover_sweep:")
        assert not stop_event.is_set()
        release_record.set()
        await task
    finally:
        release_record.set()
        correction_setup._set_relay_capture(None)


def test_relay_cancel_on_already_dead_relay_gets_plain_language_error():
    # A poll-cycle race can let a Stop click reach the server after the relay
    # already finished (phone completed, or another tab already stopped it).
    # The handler must map _request_relay_stop's diagnostic ValueError to a
    # sentence a homeowner can act on, not leak it as-is (and definitely not
    # let the shared JS parser fall back to "HTTP 409" — see
    # http.js parseResponse).
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    with pytest.raises(
        ValueError,
        match=r"^This measurement already stopped — nothing more to do here\.$",
    ):
        correction_setup._handle_crossover_relay_cancel()


@pytest.mark.asyncio
async def test_cancelled_relay_owner_drains_poll_worker_without_late_arm(monkeypatch):
    import asyncio
    import threading
    import time

    from jasper.capture_relay import session as relay_session

    worker_started = threading.Event()
    worker_stopped = threading.Event()
    purged = []

    def fake_run_capture(
        _client,
        _pi_session,
        *,
        on_armed: object,
        stop_requested,
        **_kwargs,
    ):
        assert callable(on_armed)
        worker_started.set()
        while not stop_requested():
            time.sleep(0.001)
        worker_stopped.set()
        raise relay_session.CaptureStopped("capture stopped")

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: purged.append(True))
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        **_relay_contract(),
    )

    task = asyncio.create_task(
        run_and_consume(object(), _relay_pi_session("driver"))
    )
    assert await asyncio.to_thread(worker_started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker_stopped.is_set()
    assert purged == [True]


@pytest.mark.asyncio
async def test_prepare_side_effect_then_failure_restores_before_window_exit(
    monkeypatch
):
    from contextlib import asynccontextmanager

    from jasper.correction import coordinator
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    order = []
    finish_calls = []

    @asynccontextmanager
    async def window():
        order.append("window_enter")
        try:
            yield
        finally:
            order.append("window_exit")

    async def prepare():
        order.append("prepare_lease_active")
        raise OSError("camilla failed after lease side effect")

    async def restore():
        order.append("restore")
        return True

    async def play(*_args, **_kwargs):
        order.append("play")
        return {"status": "completed", "playback": {"audio_emitted": True}}

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        reserve_repeat_attempt=lambda: {"token": "token", "attempt": 1},
        finish_failed_repeat_attempt=lambda reservation, failure_type: finish_calls.append(
            (dict(reservation), failure_type)
        ),
        prepare_play=prepare,
        restore_play=restore,
        **_relay_contract(),
    )
    with pytest.raises(OSError, match="lease side effect"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert order == [
        "window_enter",
        "prepare_lease_active",
        "restore",
        "window_exit",
    ]
    assert finish_calls == [({"token": "token", "attempt": 1}, "OSError")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_phase", ["ambient_started", "sweep_started", "sweep_complete"]
)
async def test_required_host_event_failure_consumes_repeat_without_stray_audio(
    monkeypatch, failed_phase
):
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    finish_calls = []
    play_calls = []

    async def play(*_args, **_kwargs):
        play_calls.append(True)
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
        }

    def post_host_event(_session, _token, payload):
        if payload.get("phase") == failed_phase:
            raise OSError(f"{failed_phase} post failed")

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=post_host_event,
        reserve_repeat_attempt=lambda: {"token": "token", "attempt": 1},
        finish_failed_repeat_attempt=lambda reservation, failure_type: finish_calls.append(
            (dict(reservation), failure_type)
        ),
        ambient_duration_s=(0.001 if failed_phase == "ambient_started" else 0.0),
        comparison_set=_COMPARISON_SET,
        target_fingerprint="target-fp",
    )
    with pytest.raises(OSError, match="post failed"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert len(finish_calls) == 1
    assert finish_calls[0][1] == "OSError"
    if failed_phase in {"ambient_started", "sweep_started"}:
        assert play_calls == []
    else:
        assert play_calls == [True]


@pytest.mark.asyncio
async def test_ambient_started_host_event_still_carries_duration_s(monkeypatch):
    """The phone-side countdown is driven entirely by this event's
    ``duration_s`` field, so it must never silently drop off the wire. This
    pins the LEGACY-``play_sequence`` fallback path specifically (a
    ``play_sequence`` with the pre-W2.6 single-``on_sweep_ready`` argument
    shape -- exactly what the summed-commissioning host's own
    ``_play_sequence`` in ``correction_setup.py`` still has, since it has no
    ``prepare_play`` phase to fire the post from): ``duration_s`` carries and
    the relative order (``ambient_started`` before ``sweep_started``) holds,
    but the exact WHEN (before ``play_sequence`` even starts) is
    intentionally unchanged from before W2.6 -- see
    ``test_ambient_started_falls_back_to_eager_post_for_legacy_play_sequence``
    for that ordering pinned explicitly, and
    ``test_ambient_started_fires_after_prepare_work_and_before_the_real_sleep``
    for the FIXED ordering a ``play_sequence`` that accepts
    ``on_ambient_ready`` (``correction_crossover_flow._play``, used by every
    driver capture, v2 and v3) now gets."""

    import asyncio

    _fake_relay_transport(monkeypatch)
    host_events: list[dict] = []

    async def trivial_play_sequence(on_sweep_ready):
        if on_sweep_ready is not None:
            await asyncio.to_thread(on_sweep_ready)
        return {"status": "completed"}

    result, _played = await flow.run_crossover_relay_transport(
        object(),
        _relay_pi_session("driver"),
        run_async=lambda coro, timeout=None: _run_coro(coro),
        play_sequence=trivial_play_sequence,
        validate_playback=lambda _payload: None,
        prepare_armed=lambda _state, _ack: None,
        post_host_event=lambda _sid, _token, payload: host_events.append(payload),
        ambient_duration_s=2.5,
    )
    assert result is not None
    phases = [event.get("phase") for event in host_events]
    assert "ambient_started" in phases
    assert phases.index("ambient_started") < phases.index("sweep_started")
    ambient_event = host_events[phases.index("ambient_started")]
    assert ambient_event == {"phase": "ambient_started", "duration_s": 2.5}


@pytest.mark.asyncio
async def test_ambient_started_falls_back_to_eager_post_for_legacy_play_sequence(
    monkeypatch,
):
    """Backward-compat half of the W2.6 fix: a ``play_sequence`` that does
    not accept ``on_ambient_ready`` (today, only the summed-commissioning
    host's ``_play_sequence`` in ``correction_setup.py``) keeps its ORIGINAL
    ordering byte-for-byte -- the post fires before ``play_sequence`` starts
    at all, exactly as every crossover capture behaved before this PR."""

    import asyncio

    _fake_relay_transport(monkeypatch)
    order: list[str] = []

    def post_host_event(_sid, _token, payload):
        order.append(f"event:{payload.get('phase')}")

    async def legacy_play_sequence(on_sweep_ready):
        order.append("play_sequence_started")
        if on_sweep_ready is not None:
            await asyncio.to_thread(on_sweep_ready)
        return {"status": "completed"}

    result, _played = await flow.run_crossover_relay_transport(
        object(),
        _relay_pi_session("driver"),
        run_async=lambda coro, timeout=None: _run_coro(coro),
        play_sequence=legacy_play_sequence,
        validate_playback=lambda _payload: None,
        prepare_armed=lambda _state, _ack: None,
        post_host_event=post_host_event,
        ambient_duration_s=2.5,
    )
    assert result is not None
    assert order == [
        "event:ambient_started",
        "play_sequence_started",
        "event:sweep_started",
        "event:sweep_complete",
    ]


@pytest.mark.asyncio
async def test_ambient_started_fires_after_prepare_work_and_before_the_real_sleep(
    monkeypatch,
):
    """W2.6 countdown-accuracy fix (SPEC W2.3): a ``play_sequence`` that
    accepts ``on_ambient_ready`` (``correction_crossover_flow._play``, used
    by every driver capture sweep -- v2 single-capture AND the v3
    session-spanning plan runner) now gets the post threaded through as a
    callback it fires ITSELF, after its own prepare work (solve + volume
    acquire) and immediately before the real quiet-window sleep -- not
    synchronously in ``on_armed`` before ``play_sequence`` even starts.

    Hardware run 18 saw no phone countdown and ~4-5s more arm->tone latency
    than expected; PR #1552's own investigation ("Countdown finding")
    confirmed the structural cause: #1543's ``prepare_play`` (real solve
    work — topology/design-draft loads, ``level_solver.solve_level``, two
    CamillaDSP round trips) ran in the gap between the event posting and the
    real sleep starting, so the phone's countdown (driven by ``duration_s``
    from the moment it observes this event) could reach zero before the tone
    ever played. This test proves the relocated post actually lands after
    prepare work and before the sleep, not just that ordering metadata is
    unchanged."""

    import asyncio

    _fake_relay_transport(monkeypatch)
    order: list[str] = []

    def post_host_event(_sid, _token, payload):
        order.append(f"event:{payload.get('phase')}")

    async def prepare_then_play(
        on_sweep_ready, on_ambient_ready=None
    ) -> dict:
        order.append("prepare_work")
        if on_ambient_ready is not None:
            await asyncio.to_thread(on_ambient_ready)
        order.append("sleep")
        if on_sweep_ready is not None:
            await asyncio.to_thread(on_sweep_ready)
        return {"status": "completed"}

    result, _played = await flow.run_crossover_relay_transport(
        object(),
        _relay_pi_session("driver"),
        run_async=lambda coro, timeout=None: _run_coro(coro),
        play_sequence=prepare_then_play,
        validate_playback=lambda _payload: None,
        prepare_armed=lambda _state, _ack: None,
        post_host_event=post_host_event,
        ambient_duration_s=2.5,
    )
    assert result is not None
    assert order == [
        "prepare_work",
        "event:ambient_started",
        "sleep",
        "event:sweep_started",
        "event:sweep_complete",
    ]


@pytest.mark.asyncio
async def test_driver_capture_sweep_play_fires_ambient_ready_after_prepare_play(
    monkeypatch, tmp_path
):
    """The SAME fix, exercised at the ``build_crossover_relay_run_and_consume``
    layer (the real ``_play`` v2 driver captures use) rather than the raw
    ``run_crossover_relay_transport`` seam above — proves the wiring inside
    ``_play`` itself, not just a hand-rolled ``play_sequence`` double."""

    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    order: list[str] = []

    async def prepare() -> bool:
        order.append("prepare_play")
        return True

    async def play(*_args, **_kwargs):
        order.append("play_sweep")
        # Stop here — deliberately before record_driver_capture, which needs
        # a full real topology/comparison-set fixture this test does not
        # set up. Only the play-phase ordering above is under test.
        raise RuntimeError("stop before record")

    def post_host_event(_sid, _token, payload):
        phase = payload.get("phase")
        if phase in ("ambient_started", "sweep_started"):
            order.append(f"event:{phase}")

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=post_host_event,
        reserve_repeat_attempt=lambda: {"token": "token", "attempt": 1},
        finish_failed_repeat_attempt=lambda reservation, failure_type: None,
        prepare_play=prepare,
        ambient_duration_s=1.5,
        comparison_set=_COMPARISON_SET,
        target_fingerprint="target-fp",
    )
    with pytest.raises(RuntimeError, match="stop before record"):
        await run_and_consume(object(), _relay_pi_session("driver"))
    assert order == [
        "prepare_play",
        "event:ambient_started",
        "event:sweep_started",
        "play_sweep",
    ]


async def _async_true():
    return True


@pytest.mark.asyncio
async def test_crossover_restore_false_fails_before_measurement_window_exits(
    monkeypatch,
):
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

    async def play(*_args, **_kwargs):
        order.append("play")
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
        }

    async def prepare():
        return True

    async def restore():
        order.append("restore_rejected")
        return False

    monkeypatch.setattr(coordinator, "measurement_window", window)
    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        prepare_play=prepare,
        restore_play=restore,
        **_relay_contract(),
    )

    with pytest.raises(RuntimeError, match="volume was not restored"):
        await run_and_consume(object(), _relay_pi_session("driver", session_id="s"))

    assert order == ["window_enter", "play", "restore_rejected", "window_exit"]


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
        **_relay_contract(),
    )
    with pytest.raises(ValueError, match="Finish the other measurement"):
        await run_and_consume(
            object(), _relay_pi_session("driver", session_id="s")
        )
    phases = [p.get("phase") for p in host_events]
    assert phases == ["sweep_started", "sweep_failed"]
    assert "Finish the other measurement" in host_events[1]["error"]


@pytest.mark.asyncio
async def test_crossover_flow_revalidates_ack_before_playback(monkeypatch):
    from jasper.capture_relay import session as relay_session
    from jasper.web import correction_crossover_backend as be

    play_calls = []
    host_events = []

    def fake_run_capture(client, pi_session, *, on_armed, **kwargs):
        on_armed(SimpleNamespace(
            acknowledgement=None,
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260711.1",
            },
        ))

    monkeypatch.setattr(relay_session, "run_capture", fake_run_capture)
    monkeypatch.setattr(relay_session, "purge", lambda *_args: None)

    async def play(*_args, **_kwargs):
        play_calls.append(True)
        return {"status": "completed", "playback": {"audio_emitted": True}}

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda _sid, _token, payload: host_events.append(payload),
        **_relay_contract(),
    )

    with pytest.raises(RuntimeError, match="acknowledgement"):
        await run_and_consume(object(), _relay_pi_session("driver"))

    assert play_calls == []
    assert host_events[-1]["phase"] == "sweep_failed"


@pytest.mark.asyncio
async def test_stale_comparison_set_link_refuses_before_playback(monkeypatch):
    from jasper.web import correction_crossover_backend as be

    _fake_relay_transport(monkeypatch)
    play_calls = []
    host_events = []

    async def play(*_args, **_kwargs):
        play_calls.append(True)
        return {"status": "completed", "playback": {"audio_emitted": True}}

    monkeypatch.setattr(be, "play_driver_capture_sweep", play)
    run_and_consume = flow.build_crossover_relay_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=lambda _sid, _token, payload: host_events.append(payload),
        current_comparison_set=lambda: {
            **_COMPARISON_SET,
            "comparison_set_id": "9" * 32,
        },
        **_relay_contract(),
    )

    with pytest.raises(ValueError, match="level changed"):
        await run_and_consume(object(), _relay_pi_session("driver"))

    assert play_calls == []
    assert host_events[-1]["phase"] == "sweep_failed"


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


def test_phone_sweep_failed_maps_only_the_guard_refusal_to_household_copy():
    """Run-21 review S1 (defensive): ``on_armed``'s
    ``validate_current_context()`` can raise ``ServerOwnedNextStepMismatch``,
    and the phone capture page renders the ``sweep_failed`` host event's
    ``error`` field verbatim. That ONE exception routes through the same
    single-source household copy (its own ``user_message``); every other
    exception keeps ``str(exc)`` unchanged, so the mapping stays scoped to the
    observed-broken path."""

    from jasper.web import correction_setup

    guard_exc = correction_setup.ServerOwnedNextStepMismatch(
        "the requested driver capture is not the server-owned next step"
    )
    mapped = flow._phone_failure_text(guard_exc)
    assert mapped == guard_exc.user_message
    assert "server-owned next step" not in mapped
    # Single source: the phone-post copy is byte-identical to the wizard
    # status-line copy (_relay_failure_message reads the same attribute).
    assert mapped == correction_setup._relay_failure_message(guard_exc)

    # Unmapped: an unrelated failure still surfaces its own str(exc) — the
    # defensive routing does not swallow other errors' detail.
    assert flow._phone_failure_text(RuntimeError("camilla is unavailable")) == (
        "camilla is unavailable"
    )
    assert flow._phone_failure_text(ValueError("device mismatch")) == (
        "device mismatch"
    )


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
        correction_setup._handle_crossover_relay_capture(
            _json_handler({"kind": "driver"})
        )
    assert correction_setup._get_relay_capture() is None  # slot not claimed


def test_crossover_relay_endpoint_inert_when_unconfigured(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.delenv("JASPER_CAPTURE_RELAY_BASE", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        correction_setup._handle_crossover_relay_capture(
            _json_handler({"kind": "driver"})
        )


def _mid_sequence_sweep_status() -> dict:
    """Envelope-shaped status mid-sequence: anchored (blocked) + stash
    pending, with a locked level so the envelope's server-owned next step is
    the woofer driver sweep this endpoint is asked to run."""
    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    status["setup"]["reason"] = "active_speaker_commissioning_config_loaded"
    status["setup"]["applied_crossover"] = {
        "valid": True,
        "owner": "manual",
        "reason": None,
    }
    status["capture_entry_pending"] = True
    _locked_level(status)
    return status


def test_crossover_sweep_endpoint_admits_mid_sequence_anchor(monkeypatch):
    # Run-10 sweep-handler equivalent of the level-match repro: the first
    # driver sweep POST arrives while the persisted config is (by #1523
    # design) the all-muted staged anchor, so the raw `status != "ready"`
    # gate refused the very sweep the envelope was offering. Mid-sequence
    # (blocked + in-sequence reason + stash pending) the endpoint must
    # register the relay capture instead.
    #
    # Confirmed FAILING pre-fix: without the shared predicate at this gate,
    # this raises ValueError "protected speaker setup is no longer ready"
    # (verified by running this test against the pre-fix gate).
    import jasper.output_topology as output_topology
    from jasper.web import correction_setup

    status = _mid_sequence_sweep_status()
    monkeypatch.setattr(backend, "status_payload", lambda: status)
    monkeypatch.setattr(
        backend,
        "level_lease",
        lambda: SimpleNamespace(unresolved_volume_safety=None),
    )
    monkeypatch.setattr(
        output_topology,
        "load_output_topology",
        lambda: SimpleNamespace(topology_id="topology-1"),
    )
    monkeypatch.setattr(
        correction_setup, "_require_relay_base", lambda: "https://relay.test"
    )
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    registered = {}

    def run_relay(kind, relay_base, *, return_url):
        registered.update(label=kind.label, relay_base=relay_base)
        return {"status": "awaiting_phone"}

    monkeypatch.setattr(correction_setup, "_run_relay_capture", run_relay)

    response = correction_setup._handle_crossover_relay_capture(
        _json_handler(
            {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"}
        )
    )

    assert response["relay"]["status"] == "awaiting_phone"
    assert registered["label"] == "crossover_sweep:driver"


@pytest.mark.parametrize(
    ("reason", "capture_entry_pending"),
    (
        ("active_baseline_profile_unreadable", True),
        ("active_speaker_commissioning_config_loaded", False),
    ),
)
def test_crossover_sweep_endpoint_still_refuses_other_blocked_setups(
    monkeypatch, reason, capture_entry_pending
):
    # The carve-out is exactly (in-sequence reason AND stash pending). A
    # different blocked reason with the stash, or the in-sequence reason
    # without the stash, refuses at this endpoint exactly as before.
    import jasper.output_topology as output_topology
    from jasper.web import correction_setup

    status = _mid_sequence_sweep_status()
    status["setup"]["reason"] = reason
    status["capture_entry_pending"] = capture_entry_pending
    monkeypatch.setattr(backend, "status_payload", lambda: status)
    monkeypatch.setattr(
        backend,
        "level_lease",
        lambda: SimpleNamespace(unresolved_volume_safety=None),
    )
    monkeypatch.setattr(
        output_topology,
        "load_output_topology",
        lambda: SimpleNamespace(topology_id="topology-1"),
    )
    monkeypatch.setattr(
        correction_setup, "_require_relay_base", lambda: "https://relay.test"
    )
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(
        correction_setup,
        "_run_relay_capture",
        lambda *_args, **_kwargs: pytest.fail("must fail before relay registration"),
    )

    with pytest.raises(
        ValueError, match="protected speaker setup is no longer ready"
    ):
        correction_setup._handle_crossover_relay_capture(
            _json_handler(
                {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"}
            )
        )


def test_dispatch_maps_post_time_guard_refusal_to_household_copy(monkeypatch):
    """Run-21 review S1: the SYNCHRONOUS POST-time dispatch of
    ``/crossover/relay-capture`` must not leak the raw guard string. A stale
    wizard tab re-POSTing a driver capture the server no longer offers (here:
    reference_axis when near_field is the offered step) trips
    ``_assert_crossover_driver_action`` at POST time — inside
    ``_handle_crossover_relay_capture``, BEFORE any relay registration — and
    that ``ServerOwnedNextStepMismatch`` propagates into
    ``_dispatch_crossover``'s ``except``. The response body the wizard renders
    verbatim (``postJSON`` -> ``Error(body.error)`` -> ``setStatus``) must
    carry the plain household sentence, NOT "...is not the server-owned next
    step". The raw string stays in the structured
    ``event=capture_relay.server_owned_step_mismatch`` log only."""

    import jasper.output_topology as output_topology
    from jasper.web import correction_setup

    status = _mid_sequence_sweep_status()
    monkeypatch.setattr(backend, "status_payload", lambda: status)
    monkeypatch.setattr(
        backend,
        "level_lease",
        lambda: SimpleNamespace(unresolved_volume_safety=None),
    )
    monkeypatch.setattr(
        output_topology,
        "load_output_topology",
        lambda: SimpleNamespace(topology_id="topology-1"),
    )
    monkeypatch.setattr(
        correction_setup, "_require_relay_base", lambda: "https://relay.test"
    )
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    # The guard trips at POST time, before registration — prove it by failing
    # loudly if the relay is ever reached.
    monkeypatch.setattr(
        correction_setup,
        "_run_relay_capture",
        lambda *_a, **_k: pytest.fail("guard must refuse before relay registration"),
    )

    body = json.dumps(
        {
            "kind": "driver",
            "speaker_group_id": "mono",
            "role": "woofer",
            "capture_geometry": "reference_axis",  # near_field is what's offered
        }
    ).encode()
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/relay-capture")

    assert len(sent) == 1
    payload, http_status = sent[0]
    assert int(http_status) == 400
    assert payload["ok"] is False
    # The mapped household copy, NOT the raw guard string.
    assert payload["error"] == (
        "This measurement step changed on the speaker before the phone "
        "confirmed it. Reopen the phone link and try again."
    )
    assert "server-owned next step" not in payload["error"]
    # And it is the SAME single source the async surfacing uses.
    assert payload["error"] == correction_setup._relay_failure_message(
        correction_setup.ServerOwnedNextStepMismatch("x")
    )


@pytest.mark.parametrize(
    "route",
    (
        "/crossover/summed-capture",
        "/crossover/summed-capture-sweep",
    ),
)
def test_legacy_raw_summed_routes_refuse_before_body_or_side_effects(
    monkeypatch, route
):
    from jasper.web import correction_setup

    def unexpected(label):
        return lambda *_args, **_kwargs: pytest.fail(
            f"legacy summed route reached {label}"
        )

    monkeypatch.setattr(backend, "level_lease", unexpected("level lease"))
    monkeypatch.setattr(
        correction_setup, "_read_json_body", unexpected("JSON body read")
    )
    monkeypatch.setattr(
        correction_setup, "_read_wav_body", unexpected("WAV body read")
    )
    monkeypatch.setattr(correction_setup, "_camilla", unexpected("CamillaDSP"))
    monkeypatch.setattr(
        correction_setup, "_run_async", unexpected("async playback")
    )
    monkeypatch.setattr(
        correction_setup,
        "_get_or_create_session",
        unexpected("session allocation"),
    )
    monkeypatch.setattr(
        flow,
        "handle_summed_capture",
        unexpected("summed analysis"),
    )
    monkeypatch.setattr(
        flow,
        "handle_summed_capture_sweep",
        unexpected("summed playback"),
    )

    class PoisonBody:
        def read(self, *_args, **_kwargs):
            pytest.fail("legacy summed route read its poison body")

    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    handler.headers = {"Content-Length": str(1024 * 1024)}
    handler.rfile = PoisonBody()
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover(route)

    assert len(sent) == 1
    payload, status = sent[0]
    assert status == 409
    assert payload["status"] == "refused"
    assert payload["reason"] == "active_summed_persisted_admission_unavailable"
    assert payload["audio_emitted"] is False


def test_summed_relay_rejects_browser_owned_policy_before_side_effects(
    monkeypatch,
):
    from jasper import output_topology
    from jasper.web import correction_setup

    body = json.dumps({"kind": "summed", "speaker_group_id": "mono"}).encode()

    class ReadOnce:
        calls = 0

        def read(self, size):
            assert self.calls == 0, "summed relay body must be decoded only once"
            assert size == len(body)
            self.calls += 1
            return body

    def unexpected(label):
        return lambda *_args, **_kwargs: pytest.fail(
            f"summed relay reached {label}"
        )

    reader = ReadOnce()
    monkeypatch.setattr(backend, "level_lease", unexpected("level lease"))
    monkeypatch.setattr(backend, "status_payload", unexpected("status/graph"))
    monkeypatch.setattr(
        output_topology, "load_output_topology", unexpected("topology load")
    )
    monkeypatch.setattr(
        correction_setup, "_require_relay_base", unexpected("relay config")
    )
    monkeypatch.setattr(
        correction_setup, "_crossover_blocking_phase", unexpected("relay state")
    )
    monkeypatch.setattr(
        correction_setup, "_begin_relay_capture", unexpected("session allocation")
    )
    monkeypatch.setattr(
        correction_setup, "_run_relay_capture", unexpected("relay registration")
    )
    monkeypatch.setattr(correction_setup, "_camilla", unexpected("CamillaDSP"))
    monkeypatch.setattr(
        flow,
        "build_crossover_relay_run_and_consume",
        unexpected("playback/analysis"),
    )

    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = reader
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/relay-capture")

    assert reader.calls == 1
    assert len(sent) == 1
    payload, status = sent[0]
    assert status == 400
    assert payload == {
        "ok": False,
        "error": (
            "summed commissioning accepts no browser region, polarity, or delay fields"
        ),
    }


@pytest.mark.parametrize(
    ("relay_kind", "server_status", "backend_method", "label_fragment"),
    (
        (
            "summed",
            {
                "status": "collecting",
                "next_capture": {"evidence_kind": "server_selected"},
            },
            "capture_next_commissioning_region",
            "server-selected",
        ),
        (
            "verification",
            {
                "status": "applied_unverified",
                "verification": {
                    "next_target": {"speaker_group_id": "mono"},
                },
            },
            "capture_next_commissioning_verification",
            "post-apply",
        ),
    ),
)
@pytest.mark.asyncio
async def test_commissioning_relay_is_recorder_only_for_the_server_owned_host(
    monkeypatch,
    tmp_path,
    relay_kind,
    server_status,
    backend_method,
    label_fragment,
):
    from contextlib import asynccontextmanager

    from jasper.active_speaker.commissioning_capture_producer import RawCaptureResult
    from jasper.audio_measurement.playback import PlaybackResult
    from jasper.capture_relay import correction_adapter
    from jasper.capture_relay.session import CaptureResult
    from jasper.correction import coordinator
    from jasper.web import correction_setup

    calibration = SimpleNamespace(calibration_id="umik-2-calibration")
    lease = SimpleNamespace(
        unresolved_volume_safety=None,
        mic_calibration=None,
        input_device=None,
    )
    server_status = {
        **server_status,
        "run_id": "run-1",
        "owner_generation": 3,
        "plan_fingerprint": "a" * 64,
    }
    monkeypatch.setattr(backend, "level_lease", lambda: lease)
    monkeypatch.setattr(backend, "commissioning_region_status", lambda: server_status)
    monkeypatch.setattr(
        backend,
        "commissioning_recorder_binding",
        lambda: (
            calibration,
            hashlib.sha256(b"umik-2").hexdigest(),
        ),
    )
    monkeypatch.setattr(
        correction_setup, "_require_relay_base", lambda: "https://relay.test"
    )
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    monkeypatch.setattr(
        correction_setup, "_relay_device_calibration_block", lambda *_args: None
    )

    @asynccontextmanager
    async def measurement_window():
        yield

    monkeypatch.setattr(coordinator, "measurement_window", measurement_window)

    events = []
    monkeypatch.setattr(
        correction_setup,
        "log_event",
        lambda _logger, event, **_fields: events.append(event),
    )

    registered = {}

    def run_relay(kind, relay_base, *, return_url):
        registered.update(
            {"kind": kind, "relay_base": relay_base, "return_url": return_url}
        )
        return {"tap_link": "https://capture.test", "status": "awaiting_phone"}

    monkeypatch.setattr(correction_setup, "_run_relay_capture", run_relay)
    response = correction_setup._handle_crossover_relay_capture(
        _json_handler({"kind": relay_kind})
    )

    assert response["relay"]["status"] == "awaiting_phone"
    assert registered["kind"].label == "crossover_sweep:summed"
    assert registered["relay_base"] == "https://relay.test"
    assert registered["return_url"] == "http://jts.local/correction/crossover/"

    opened = {}

    def open_capture(_client, spec, **kwargs):
        opened.update({"spec": spec, **kwargs})
        return "registered"

    monkeypatch.setattr(correction_adapter, "open_capture", open_capture)
    assert (
        registered["kind"].open(
            object(),
            "https://relay.test",
            "https://capture.test",
            "http://jts.local/correction/crossover/",
        )
        == "registered"
    )
    spec = opened["spec"]
    assert spec.kind == "crossover_sweep"
    assert spec.acknowledgement is not None
    assert spec.acknowledgement.id == "summed_reference_axis_v1"
    assert label_fragment in spec.stimulus.label

    relay_result = CaptureResult(
        wav=b"real-recorder-wav",
        device={"label": "UMIK-2"},
        noise_floor={"dbfs": -72.0},
        setup={"source": "relay"},
    )
    transport_metadata = {}

    async def relay_transport(
        _client,
        pi_session,
        *,
        play_sequence,
        validate_playback,
        prepare_armed,
        validate_capture,
        **_kwargs,
    ):
        prepare_armed(
            SimpleNamespace(capture_page={"page": "fixed-axis"}),
            {
                "policy_id": pi_session.spec.acknowledgement.id,
                "binding_id": pi_session.spec.acknowledgement.binding_id,
            },
        )
        playback = PlaybackResult(tmp_path / "summed.wav", "measurement", 0)
        validate_playback(playback)
        validate_capture(relay_result)
        assert callable(play_sequence)
        return relay_result, playback

    monkeypatch.setattr(flow, "run_crossover_relay_transport", relay_transport)

    async def capture_next(raw_transport, *, camilla_factory):
        assert callable(camilla_factory)
        raw = await raw_transport(lambda: pytest.fail("host owns playback"))
        assert isinstance(raw, RawCaptureResult)
        assert raw.wav_bytes == b"real-recorder-wav"
        transport_metadata.update(raw.metadata)
        return {
            "status": "verified" if relay_kind == "verification" else "collecting",
            "speaker_group_id": "mono",
            "region_id": "woofer-tweeter",
            "evidence_kind": "normal",
            "capture_fingerprint": "b" * 64,
        }

    monkeypatch.setattr(backend, backend_method, capture_next)
    pi_session = SimpleNamespace(
        session_id="relay-session",
        pull_token="pull-token",
        spec=spec,
    )

    await registered["kind"].run_and_consume(object(), pi_session)

    assert transport_metadata["device"] == {"label": "UMIK-2"}
    assert transport_metadata["fixed_axis_acknowledgement"]["policy_id"] == (
        "summed_reference_axis_v1"
    )
    assert transport_metadata["fixed_axis_acknowledgement"][
        "acknowledgement_binding"
    ] == spec.acknowledgement.binding_id
    assert events == ["correction.crossover_region_capture_recorded"]


def test_crossover_relay_route_is_registered():
    from jasper.web import correction_setup

    assert "/crossover/relay-capture" in correction_setup._POST_ROUTES
    assert "/crossover/relay-cancel" in correction_setup._POST_ROUTES
    assert "/crossover/region-geometry" in correction_setup._POST_ROUTES
    assert "/crossover/candidate" in correction_setup._POST_ROUTES
    assert "/crossover/restore" in correction_setup._POST_ROUTES


def test_crossover_restore_route_dispatches_without_browser_policy(monkeypatch):
    from jasper.web import correction_setup

    seen = []
    monkeypatch.setattr(correction_setup, "_active_relay_phase", lambda: None)

    def restore(run_async, camilla_factory, *, blocking_phase):
        seen.append((run_async, camilla_factory, blocking_phase))
        return {"status": "rolled_back"}, 200

    monkeypatch.setattr(flow, "handle_restore", restore)
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/restore")

    assert sent == [({"status": "rolled_back"}, 200)]
    assert len(seen) == 1
    assert seen[0][0] is correction_setup._run_async
    assert seen[0][1] is correction_setup._camilla
    assert seen[0][2] is None


def test_region_geometry_route_accepts_only_the_server_target_and_signed_value(
    monkeypatch,
):
    from jasper.web import correction_setup

    seen = []
    monkeypatch.setattr(correction_setup, "_active_relay_phase", lambda: None)
    monkeypatch.setattr(
        backend,
        "attest_commissioning_region_geometry",
        lambda raw: seen.append(raw) or {"status": "accepted"},
    )
    raw = {
        "expected_target_fingerprint": "a" * 64,
        "signed_acoustic_path_difference_mm": -7.5,
    }
    assert correction_setup._handle_crossover_region_geometry(
        _json_handler(raw)
    ) == {"status": "accepted"}
    assert seen == [raw]

    with pytest.raises(ValueError, match="unsupported fields"):
        correction_setup._handle_crossover_region_geometry(
            _json_handler({**raw, "polarity": "reverse"})
        )


def test_candidate_recovery_route_accepts_no_browser_policy(monkeypatch):
    from jasper.web import correction_setup

    monkeypatch.setattr(correction_setup, "_active_relay_phase", lambda: None)
    monkeypatch.setattr(
        backend,
        "prepare_commissioning_candidate",
        lambda: {"status": "candidate_ready"},
    )

    assert correction_setup._handle_crossover_candidate(
        _json_handler({})
    ) == {"status": "candidate_ready"}
    with pytest.raises(ValueError, match="accepts no browser fields"):
        correction_setup._handle_crossover_candidate(
            _json_handler({"delay_ms": 1.0})
        )


def test_candidate_recovery_returns_persisted_refusal_as_current_state(
    monkeypatch,
):
    from jasper.active_speaker.commissioning_service import (
        CommissioningServiceError,
    )
    from jasper.web import correction_setup

    refused = {
        "status": "candidate_refused",
        "candidate_failure": {
            "reason": "candidate_polarity_inconclusive"
        },
    }

    class Service:
        def publish_candidate(self):
            raise CommissioningServiceError(
                "candidate_scoring_failed",
                "exact measured evidence could not authorize a candidate",
            )

        def status(self):
            return refused

    monkeypatch.setattr(backend, "_commissioning_capture_service", Service)
    monkeypatch.setattr(correction_setup, "_active_relay_phase", lambda: None)

    assert correction_setup._handle_crossover_candidate(
        _json_handler({})
    ) == refused


@pytest.mark.asyncio
async def test_final_region_capture_immediately_publishes_the_candidate(
    monkeypatch,
):
    statuses = iter(
        (
            {"status": "collecting"},
            {"status": "measured"},
            {"status": "candidate_ready"},
        )
    )
    calls = []

    class Service:
        def status(self):
            return next(statuses)

        async def capture_next(self, _port, **kwargs):
            calls.append(("capture", kwargs))
            return SimpleNamespace(
                fingerprint="a" * 64,
                evidence_kind="delay_null",
                speaker_group_id="mono",
                region_id="woofer_tweeter",
            )

        def publish_candidate(self):
            calls.append(("candidate",))
            return {"fingerprint": "b" * 64}

    monkeypatch.setattr(backend, "_commissioning_capture_service", Service)
    monkeypatch.setattr(
        backend,
        "_LEVEL_LEASE",
        SimpleNamespace(assert_volume_safety_resolved=lambda: None),
    )

    result = await backend.capture_next_commissioning_region(
        object(),
        camilla_factory=lambda: object(),
    )

    assert [call[0] for call in calls] == ["capture", "candidate"]
    assert result["next"] == {"status": "candidate_ready"}


@pytest.mark.asyncio
async def test_final_region_capture_returns_persisted_candidate_refusal(
    monkeypatch,
):
    from jasper.active_speaker.commissioning_service import (
        CommissioningServiceError,
    )

    statuses = iter(
        (
            {"status": "collecting"},
            {"status": "measured"},
            {
                "status": "candidate_refused",
                "candidate_failure": {
                    "reason": "candidate_polarity_inconclusive"
                },
            },
        )
    )

    class Service:
        def status(self):
            return next(statuses)

        async def capture_next(self, _port, **_kwargs):
            return SimpleNamespace(
                fingerprint="a" * 64,
                evidence_kind="delay_null",
                speaker_group_id="mono",
                region_id="woofer_tweeter",
            )

        def publish_candidate(self):
            raise CommissioningServiceError(
                "candidate_scoring_failed",
                "exact measured evidence could not authorize a candidate",
            )

    monkeypatch.setattr(backend, "_commissioning_capture_service", Service)
    monkeypatch.setattr(
        backend,
        "_LEVEL_LEASE",
        SimpleNamespace(assert_volume_safety_resolved=lambda: None),
    )

    result = await backend.capture_next_commissioning_region(
        object(),
        camilla_factory=lambda: object(),
    )

    assert result["next"] == {
        "status": "candidate_refused",
        "candidate_failure": {
            "reason": "candidate_polarity_inconclusive"
        },
    }


@pytest.mark.parametrize(
    "route",
    (
        "/crossover/level-match",
        "/crossover/apply",
        "/crossover/driver-test",
        "/crossover/summed-test",
        "/crossover/driver-capture-sweep",
    ),
)
def test_unresolved_volume_refuses_every_live_crossover_action_route(
    monkeypatch, tmp_path, route
):
    from jasper.web import correction_setup

    lease = backend.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    _latch_volume_safety(lease)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", lease)
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover(route)

    assert sent == [
        (
            {
                "status": "refused",
                "reason": "crossover_volume_safety_unresolved",
                "next_step": (
                    "Use Recover safe listening volume before another crossover action."
                ),
            },
            409,
        )
    ]


def test_unresolved_volume_refuses_driver_relay_after_bounded_decode(
    monkeypatch, tmp_path
):
    from jasper.web import correction_setup

    lease = backend.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    _latch_volume_safety(lease)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", lease)
    monkeypatch.setattr(
        correction_setup,
        "_require_relay_base",
        lambda: pytest.fail("volume safety must precede relay setup"),
    )
    body_handler = _json_handler({"kind": "driver"})
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    handler.headers = body_handler.headers
    handler.rfile = body_handler.rfile
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/relay-capture")

    assert sent == [
        (
            {
                "status": "refused",
                "reason": "crossover_volume_safety_unresolved",
                "next_step": (
                    "Use Recover safe listening volume before another crossover action."
                ),
            },
            409,
        )
    ]


def test_explicit_volume_recovery_route_confirms_readback(monkeypatch, tmp_path):
    import asyncio

    from jasper.web import correction_setup

    lease = backend.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    _latch_volume_safety(lease)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", lease)

    class FakeCamilla:
        def __init__(self):
            self.volume = -3.0

        async def set_volume_db(self, value, *, best_effort):
            assert best_effort is False
            self.volume = value
            return True

        async def get_volume_db(self, *, best_effort):
            assert best_effort is False
            return self.volume

    cam = FakeCamilla()
    monkeypatch.setattr(correction_setup, "_camilla", lambda: cam)
    monkeypatch.setattr(
        correction_setup,
        "_run_async",
        lambda coro, timeout=60.0: asyncio.run(coro),
    )
    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/recover-volume")

    assert sent == [
        (
            {
                "status": "recovered",
                "recovery": "exact_restored",
                "next_step": "Refresh and continue crossover commissioning.",
            },
            200,
        )
    ]
    assert cam.volume == -21.0
    assert lease.unresolved_volume_safety is None
    assert "/crossover/recover-volume" in correction_setup._POST_ROUTES


@pytest.mark.parametrize("failure", ("rejected", "timeout"))
def test_explicit_volume_recovery_route_fails_closed(
    monkeypatch, tmp_path, failure, caplog
):
    import asyncio
    import concurrent.futures

    from jasper.web import correction_setup

    lease = backend.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    _latch_volume_safety(lease)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", lease)

    class FakeCamilla:
        async def set_volume_db(self, _value, *, best_effort):
            assert best_effort is False
            return False

        async def get_volume_db(self, *, best_effort):
            assert best_effort is False
            return -3.0

    monkeypatch.setattr(correction_setup, "_camilla", FakeCamilla)
    if failure == "rejected":
        monkeypatch.setattr(
            correction_setup,
            "_run_async",
            lambda coro, timeout=60.0: asyncio.run(coro),
        )
    else:

        def time_out(coro, timeout=60.0):
            assert timeout == correction_setup._CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S
            coro.close()
            raise concurrent.futures.TimeoutError

        monkeypatch.setattr(correction_setup, "_run_async", time_out)

    handler_type = correction_setup._make_handler({"hostname": "jts.local"})
    handler = handler_type.__new__(handler_type)
    sent = []
    handler._send_json = lambda payload, status=200: sent.append((payload, status))

    handler._dispatch_crossover("/crossover/recover-volume")

    assert sent == [
        (
            {
                "status": "refused",
                "recovery": "failed",
                "next_step": (
                    "Stop playback and retry recovery when CamillaDSP is available."
                ),
            },
            409,
        )
    ]
    assert lease.unresolved_volume_safety is not None
    assert (
        "event=correction.crossover_level_volume_safety_recovery_timeout"
        in caplog.text
    ) is (failure == "timeout")


@pytest.mark.asyncio
async def test_relay_driver_sweep_uses_only_its_matching_prepared_volume_lease(
    monkeypatch, tmp_path
):
    from jasper.audio_measurement.ramp import RampState
    from jasper.web import correction_crossover_backend as backend_module

    lease = backend_module.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    lease._outcomes["near_field_driver:mono:woofer"] = SimpleNamespace(
        ramp=SimpleNamespace(
            state=RampState.LOCKED,
            locked_main_volume_db=-8.0,
        )
    )
    current = -27.0

    async def get_volume():
        return current

    async def set_volume(value):
        nonlocal current
        current = value
        return True

    assert await lease.acquire_driver_sweep_volume(
        "mono", "woofer", get_volume, set_volume
    )
    monkeypatch.setattr(backend_module, "_LEVEL_LEASE", lease)

    async def play(raw, **_kwargs):
        return {"status": "playing", "role": raw["role"]}

    monkeypatch.setattr(
        backend_module.web_commissioning,
        "play_driver_capture_sweep",
        play,
    )
    assert await backend_module.play_driver_capture_sweep(
        {"speaker_group_id": "mono", "role": "woofer"},
        camilla_factory=lambda: object(),
        volume_lease_prepared=True,
    ) == {"status": "playing", "role": "woofer"}
    with pytest.raises(RuntimeError, match="does not own"):
        await backend_module.play_driver_capture_sweep(
            {"speaker_group_id": "mono", "role": "tweeter"},
            camilla_factory=lambda: object(),
            volume_lease_prepared=True,
        )

    assert (await lease.finish_sweep_volume(set_volume, get_volume)).value == (
        "exact_restored"
    )


@pytest.mark.asyncio
async def test_unresolved_volume_refuses_direct_level_capture_and_apply_boundaries(
    monkeypatch, tmp_path
):
    lease = backend.CrossoverLevelLease(
        volume_safety_state_path=tmp_path / "volume-safety.json"
    )
    _latch_volume_safety(lease)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", lease)

    with pytest.raises(RuntimeError, match="not confirmed safe"):
        await lease.run_level_match("near_field_driver:mono:woofer")
    with pytest.raises(RuntimeError, match="not confirmed safe"):
        backend.record_driver_capture({}, b"")
    with pytest.raises(RuntimeError, match="not confirmed safe"):
        await backend.play_driver_capture_sweep({}, camilla_factory=lambda: object())
    with pytest.raises(RuntimeError, match="not confirmed safe"):
        await backend.apply_profile(
            tuning_owner="manual",
            expected_candidate_fingerprint="candidate",
            camilla_factory=lambda: object(),
        )


# --- v3 session-spanning capture plan — endpoint-level driver run (SPEC W2.3) --


def _v3_driver_plan_fixture(
    monkeypatch,
    tmp_path,
    *,
    capture_geometry: str = "near_field",
    target_fingerprint: str = "c" * 64,
):
    """The SAME analyzer-boundary mocking
    ``test_driver_capture_wires_three_repeats_before_one_durable_record`` uses
    for v2 (``jasper.active_speaker.commissioning_capture
    .record_driver_acoustic_capture`` faked; everything above it — the real
    ``repeat_admission`` ledger, ``web_measurement.record_driver_capture``,
    ``correction_crossover_backend.record_driver_capture``'s solve-correction
    wiring — genuinely runs) so a v3 session-spanning plan exercises the
    IDENTICAL per-capture analysis/record path, just driven through
    ``build_crossover_relay_plan_run_and_consume`` instead of one direct call
    per repeat.

    ``fixture.verdicts[attempt] = {"accepted": False, "estimated_snr_db": ...}``
    controls one attempt's per-capture acoustic verdict before driving it;
    ``fixture.finalize`` controls the WINNER's own re-analysis at set
    completion (``{"verdict": "insufficient", "snr_db": ...}`` drives the
    completed-insufficient correction path).
    """
    import jasper.active_speaker.bundles as active_speaker_bundles
    import jasper.active_speaker.calibration_level as calibration_level
    import jasper.active_speaker.commissioning_admission as commissioning_admission
    import jasper.active_speaker.commissioning_capture as capture
    import jasper.active_speaker.measurement as measurement
    from jasper.active_speaker import repeat_admission
    from jasper.active_speaker.capture_geometry import driver_repeat_binding

    topology = object()
    wav_path = tmp_path / "driver.wav"
    wav_path.write_bytes(b"wav")
    comparison_set = dict(_COMPARISON_SET)
    repeat_target_id, repeat_target_fingerprint = driver_repeat_binding(
        speaker_group_id="mono",
        role="woofer",
        target_fingerprint=target_fingerprint,
        capture_geometry=capture_geometry,
    )
    admission_path = tmp_path / "repeat-admission.json"
    # web_measurement.record_driver_capture's OWN deep calls into
    # repeat_admission.finish/complete/abort_ready never pass path= --  the
    # env var is the only way to redirect them off the real
    # /var/lib/jasper default in a hardware-free test (mirrors
    # test_driver_capture_wires_three_repeats_before_one_durable_record).
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_REPEAT_ADMISSION_STATE", str(admission_path)
    )

    monkeypatch.setattr(web_measurement, "load_output_topology", lambda: topology)
    monkeypatch.setattr(
        web_measurement, "capture_preset", lambda _topology, supplied=None: supplied
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_wav_path",
        lambda raw, kind, wav_bytes=None: wav_path,
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_sweep_meta",
        lambda raw: {
            "sample_rate": 48000,
            "n_samples": 48000,
            "f1": 20.0,
            "f2": 12000.0,
            "duration_s": 1.0,
            "amplitude_dbfs": -12.0,
        },
    )
    monkeypatch.setattr(
        web_measurement,
        "capture_calibration",
        lambda raw: (None, None, {"mode": "magnitude_only"}),
    )
    ambient = {
        "schema_version": 1,
        "domain": "deconvolved",
        "method": "deconvolved_band_difference",
        "bands": [],
    }
    monkeypatch.setattr(
        web_measurement, "_stored_ambient_report", lambda *a, **k: ambient
    )
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    monkeypatch.setattr(
        web_measurement,
        "_resolve_bundle_for_capture",
        lambda *a, **k: (bundle_dir, "captures/driver.wav"),
    )
    monkeypatch.setattr(
        web_measurement,
        "_driver_target_fingerprint",
        lambda *a, **k: target_fingerprint,
    )
    monkeypatch.setattr(calibration_level, "load_calibration_level_state", lambda: {})
    monkeypatch.setattr(
        commissioning_admission,
        "validate_capture_admission_handoff",
        lambda handoff, **_kwargs: (dict(handoff) if handoff else None),
    )
    monkeypatch.setattr(
        measurement,
        "load_measurement_state",
        lambda _topology: {"active_comparison_set": comparison_set},
    )
    monkeypatch.setattr(
        measurement,
        "current_driver_floor_evidence",
        lambda *_a, **_k: {
            "valid": True,
            "source": "durable_current_driver_measurement",
            "confirmation": {},
        },
    )
    monkeypatch.setattr(
        active_speaker_bundles,
        "append_repeat_capture",
        lambda *_a, **kwargs: {
            "artifact_path": f"captures/repeat-{kwargs['index']}.wav"
        },
    )
    monkeypatch.setattr(
        active_speaker_bundles,
        "record_repeat_progress",
        lambda *_a, **kwargs: dict(kwargs),
    )
    monkeypatch.setattr(active_speaker_bundles, "append_capture", lambda *a, **k: None)

    attempt_counter = {"n": 0}
    verdicts: dict = {}
    finalize: dict = {"verdict": "ok", "snr_db": 31.0}

    def fake_analyze(*_args, **kwargs):
        if kwargs.get("record") is not None:
            attempt_counter["n"] += 1
            attempt = attempt_counter["n"]
            verdict = verdicts.get(
                attempt, {"accepted": True, "estimated_snr_db": 31.0}
            )
            accepted = verdict.get("accepted", True)
            snr_verdict = "ok" if accepted else "insufficient"
            return {
                "recorded": True,
                "verdict": "present" if accepted else "unusable_capture",
                "outcome": "heard_correct_driver" if accepted else None,
                "acoustic": {
                    "verdict": "present" if accepted else "unusable_capture",
                    "capture_geometry": capture_geometry,
                    "observed_mic_dbfs": -30.0 + attempt / 10.0,
                    "mic_clipping": False,
                    "snr": {
                        "verdict": snr_verdict,
                        "worst_relevant": {
                            "band_id": "mid",
                            "estimated_snr_db": verdict.get(
                                "estimated_snr_db", 31.0
                            ),
                            "verdict": snr_verdict,
                        },
                    },
                },
                "excitation": {},
                "placement_proof": kwargs.get("placement_proof"),
            }
        repeats = kwargs["repeats"]
        return {
            "recorded": True,
            "verdict": "present",
            "acoustic": {
                "verdict": "present",
                "capture_geometry": capture_geometry,
                "snr": {
                    "verdict": finalize["verdict"],
                    "worst_relevant": {
                        "band_id": "mid",
                        "estimated_snr_db": finalize["snr_db"],
                        "verdict": finalize["verdict"],
                    },
                },
            },
            "measurement": {"repeats": repeats},
            "placement_proof": kwargs.get("placement_proof"),
        }

    monkeypatch.setattr(capture, "record_driver_acoustic_capture", fake_analyze)

    repeat_admission.activate(comparison_set, path=admission_path)
    monkeypatch.setattr(backend, "_LEVEL_LEASE", backend.CrossoverLevelLease())

    return SimpleNamespace(
        comparison_set=comparison_set,
        admission_path=admission_path,
        repeat_target_id=repeat_target_id,
        repeat_target_fingerprint=repeat_target_fingerprint,
        verdicts=verdicts,
        finalize=finalize,
        repeat_admission=repeat_admission,
    )


def _drive_v3_driver_plan(
    monkeypatch,
    fixture,
    *,
    ambient_duration_s: float = 0.0,
    play_fails_on_attempt: int | None = None,
    stop_after_reserve: bool = False,
):
    """Build and return (run_and_consume, client, session, phone,
    relay_backend, progress_calls, finish_failed_calls) wired against
    ``fixture`` (from :func:`_v3_driver_plan_fixture`).

    ``stop_after_reserve`` flips the runner's own ``stop_event`` the moment
    the first ``reserve_repeat_attempt`` succeeds — landing a Stop in the
    authorize→consume gap, before any tone can play."""
    import threading
    import urllib.parse

    from jasper.capture_relay import session as relay_session
    from jasper.capture_relay.client import RelayClient
    from jasper.capture_relay.session import mint_session, register_session
    from jasper.capture_relay.spec import CapturePlan, build_crossover_sweep_spec
    from jasper.correction import coordinator
    from tests.test_capture_relay_plan import _BINDING, FakePlanRelayBackend, PhonePlanDriver, _wav

    class _UploadingPhoneDriver(PhonePlanDriver):
        """``PhonePlanDriver`` plus the "upload once the sweep finishes"
        reaction the real capture page performs. ``test_capture_relay_plan
        .py``'s own suite sidesteps this by uploading straight from a
        TEST-ONLY ``on_armed`` (its ``_plan_callbacks`` helper) instead of the
        phone reacting to ``sweep_complete`` -- this test drives the REAL
        production ``on_armed`` (which only plays the sweep and posts
        ``sweep_complete``, never uploads), so the phone driver itself must
        react to it, exactly as the future capture page will."""

        def __init__(self, backend, session, *, page=None):
            super().__init__(backend, session, page=page)
            self._uploaded_for: set[tuple[int, int]] = set()

        def step(self):
            if not self.finished:
                host = (
                    self.backend.sessions[self.session.session_id]["host_event"]
                    or {}
                )
                if (
                    host.get("phase") == "sweep_complete"
                    and self.begun is not None
                    and self.begun not in self._uploaded_for
                ):
                    _index, attempt = self.begun
                    self.backend.phone_upload(
                        self.session.session_id,
                        self.session.content_key,
                        _wav(attempt),
                        index=attempt - 1,
                    )
                    self._uploaded_for.add(self.begun)
            super().step()

    class AlwaysActive:
        def __init__(self, *_args, **_kwargs):
            pass

        def assert_active(self):
            return None

    # Isolates the real coordinator.measurement_window() (a UDS call to
    # jasper-mux) hardware-free -- the same seam _fake_relay_transport
    # stubs for the v2 tests. CaptureActivityProbe also stubbed: its own
    # background watchdog polls client.status() concurrently with the main
    # loop, which would double-drive PhonePlanDriver's reactive step().
    async def acquire_measurement_gate():
        return None

    async def release_measurement_gate(**_kwargs):
        return None

    monkeypatch.setattr(
        coordinator, "_acquire_measurement_gate", acquire_measurement_gate
    )
    monkeypatch.setattr(
        coordinator, "_release_measurement_gate", release_measurement_gate
    )
    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AlwaysActive)

    play_calls: list[int] = []

    async def play(*_args, **_kwargs):
        play_calls.append(len(play_calls) + 1)
        if play_fails_on_attempt == len(play_calls):
            raise RuntimeError("simulated sweep playback failure")
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
            "playback_id": f"play-{len(play_calls)}",
            "test_level_dbfs": -72.0,
            "excitation": {},
        }

    monkeypatch.setattr(backend, "play_driver_capture_sweep", play)

    relay_backend = FakePlanRelayBackend()
    spec = build_crossover_sweep_spec(
        driver_label="Woofer driver",
        driver_role="woofer",
        acknowledgement_binding=_BINDING,
        stimulus_duration_ms=4000,
        capture_plan=CapturePlan(capture_target=3, max_attempts=4),
    )
    session = mint_session(
        spec, relay_base="https://relay.test", capture_origin="capture.test"
    )
    register_session(
        RelayClient("https://relay.test", transport=relay_backend), session
    )
    phone = _UploadingPhoneDriver(relay_backend, session)

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            phone.step()
        return relay_backend(method, url, headers, body)

    client = RelayClient("https://relay.test", transport=transport)

    stop_event = threading.Event()

    def reserve_repeat_attempt():
        reservation = fixture.repeat_admission.reserve(
            fixture.comparison_set,
            target_id=fixture.repeat_target_id,
            target_fingerprint=fixture.repeat_target_fingerprint,
            path=fixture.admission_path,
        )
        if stop_after_reserve:
            stop_event.set()
        return reservation

    finish_failed_calls: list[tuple[dict, str]] = []

    def finish_failed_repeat_attempt(reservation, failure_type):
        finish_failed_calls.append((dict(reservation), failure_type))
        fixture.repeat_admission.finish(
            fixture.comparison_set,
            target_id=fixture.repeat_target_id,
            target_fingerprint=fixture.repeat_target_fingerprint,
            token=str(reservation.get("token") or ""),
            result={"accepted": False, "reject_reason": "capture_failed"},
            status=fixture.repeat_admission.failure_status(
                reservation.get("attempt")
            ),
        )

    progress_calls: list[int] = []

    run_and_consume = flow.build_crossover_relay_plan_run_and_consume(
        {"kind": "driver", "speaker_group_id": "mono", "role": "woofer"},
        lambda coro, timeout=None: _run_coro(coro),
        lambda: object(),
        post_host_event=client.post_host_event,
        reserve_repeat_attempt=reserve_repeat_attempt,
        finish_failed_repeat_attempt=finish_failed_repeat_attempt,
        publish_progress=lambda n: progress_calls.append(n),
        comparison_set=fixture.comparison_set,
        target_fingerprint="c" * 64,
        ambient_duration_s=ambient_duration_s,
        stop_event=stop_event,
    )
    return (
        run_and_consume,
        client,
        session,
        phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    )


@pytest.mark.asyncio
async def test_v3_driver_plan_completes_a_full_repeat_set_and_advances(
    monkeypatch, tmp_path
):
    """The headline SPEC W2.3 acceptance shape: one relay session carries
    all 3 accepted captures of a driver's repeat set. Real
    ``repeat_admission`` ledger throughout — proves ``authorize_begin``'s
    ``reserve_repeat_attempt`` seam and ``consume_capture``'s
    ``record_driver_capture`` call finalize the SAME way the v2 per-HTTP-call
    path does, just driven by the phone's own begin/authorize/result loop
    instead of 3 separate wizard POSTs."""

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture)

    await run_and_consume(client, session)

    assert play_calls == [1, 2, 3]
    assert finish_failed_calls == []
    # Progress publishes once at session start (0) and once per capture.
    assert progress_calls == [0, 1, 2, 3]
    phases = relay_backend.phases(session.session_id)
    assert phases[-1] == "capture_set_complete"
    assert "sweep_failed" not in phases

    snapshot = fixture.repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["status"] == "completed"
    assert target_state["attempts"] == 3
    assert len(target_state["results"]) == 3
    assert all(r["accepted"] is True for r in target_state["results"])


@pytest.mark.asyncio
async def test_v3_driver_plan_rejection_mid_set_retries_same_index(
    monkeypatch, tmp_path
):
    """A rejected capture (SNR insufficient) retries the SAME measurement
    slot at the next admitted attempt — the set still reaches 3 accepted
    within the 4-attempt budget. Proves consume_capture's accepted/not
    signal drives run_capture_plan's own index/attempt bookkeeping
    correctly for a non-trivial (rejected-then-retried) sequence."""

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    fixture.verdicts[2] = {"accepted": False, "estimated_snr_db": 10.0}
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture)

    await run_and_consume(client, session)

    # 4 total admitted attempts (one rejected), all 4 played.
    assert play_calls == [1, 2, 3, 4]
    assert finish_failed_calls == []
    phases = relay_backend.phases(session.session_id)
    assert phases[-1] == "capture_set_complete"

    results = [
        e for e in relay_backend.host_events[session.session_id]
        if e.get("phase") == "capture_result"
    ]
    assert [r["accepted"] for r in results] == [True, False, True, True]
    # Rejected attempt (attempt 2) retries the SAME index (2), then the
    # successful retry (attempt 3) advances to index 3.
    assert [r["index"] for r in results] == [1, 2, 2, 3]
    assert [r["attempt"] for r in results] == [1, 2, 3, 4]

    snapshot = fixture.repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["status"] == "completed"
    assert target_state["attempts"] == 4


@pytest.mark.asyncio
async def test_v3_driver_plan_budget_enforcement_after_four_rejections(
    monkeypatch, tmp_path
):
    """Every one of the 4 admitted attempts rejects — the SAME
    repeat_admission budget v2 enforces refuses a 5th, and the SESSION ends
    via ``capture_set_exhausted`` (never an unhandled CaptureBeginRefused
    error) because run_capture_plan's own ``max_attempts=4`` matches the
    ledger's ``MAX_ATTEMPTS`` exactly."""

    from jasper.active_speaker import repeat_admission as repeat_admission_mod

    assert repeat_admission_mod.MAX_ATTEMPTS == 4

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    for attempt in (1, 2, 3, 4):
        fixture.verdicts[attempt] = {"accepted": False, "estimated_snr_db": 5.0}
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture)

    await run_and_consume(client, session)

    assert play_calls == [1, 2, 3, 4]
    assert finish_failed_calls == []
    phases = relay_backend.phases(session.session_id)
    assert phases[-1] == "capture_set_exhausted"
    assert progress_calls[-1] == 0  # never a single accepted capture

    snapshot = fixture.repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["status"] == "refused"
    assert target_state["attempts"] == 4

    # A 5th begin would be refused by the durable ledger, not silently
    # admitted -- pin the actual seam authorize_begin wraps. The target's
    # own status already moved from "active" to the terminal "refused"
    # (repeat_admission.finish's status=failure_status(4)), so THIS
    # rejection is the "already refused" branch, not the in-flight
    # "already used four attempts" one.
    with pytest.raises(ValueError, match="is refused"):
        fixture.repeat_admission.reserve(
            fixture.comparison_set,
            target_id=fixture.repeat_target_id,
            target_fingerprint=fixture.repeat_target_fingerprint,
            path=fixture.admission_path,
        )


@pytest.mark.asyncio
async def test_v3_driver_plan_writes_completed_insufficient_correction_per_capture(
    monkeypatch, tmp_path
):
    """#1555 extended to v3: every individual attempt is accepted (the
    per-attempt rejection path never fires), but the FINALIZING re-analysis
    (the winner's own acoustic.snr) reads "insufficient" -- the completion-
    time correction (record_solve_correction(trigger="completed_insufficient"))
    must still fire, exactly as it does through the v2 HTTP path, because
    consume_capture calls the IDENTICAL
    correction_crossover_backend.record_driver_capture wrapper."""

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    fixture.finalize.update({"verdict": "insufficient", "snr_db": 13.7})
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture)

    # The completion-time correction path (record_solve_correction) needs a
    # configured target on the module's level lease -- mirrors
    # test_correction_crossover_backend_level_solve.py's fixtures.
    backend._LEVEL_LEASE.configure_targets(
        [
            {
                "target_id": "mono:woofer",
                "speaker_group_id": "mono",
                "role": "woofer",
                "geometry": "near_field_driver:mono:woofer",
                "tone_frequency_hz": 250.0,
                "commissioning_gain_db": -3.0,
                "target_fingerprint": "c" * 64,
            }
        ]
    )

    await run_and_consume(client, session)

    assert play_calls == [1, 2, 3]
    phases = relay_backend.phases(session.session_id)
    assert phases[-1] == "capture_set_complete"

    from jasper.audio_measurement import level_solver
    from jasper.audio_measurement.quality_model import DRIVER as DRIVER_QUALITY_MODEL

    required_db = level_solver.driver_solve_requirement_db(DRIVER_QUALITY_MODEL)
    assert backend._LEVEL_LEASE._solve_adjustment_db == {
        "mono:woofer": pytest.approx(required_db - 13.7)
    }


@pytest.mark.asyncio
async def test_v3_driver_plan_transport_failure_finishes_the_reservation(
    monkeypatch, tmp_path
):
    """A mid-set TRANSPORT failure (the sweep itself fails to play) between
    authorize_begin succeeding and consume_capture running must not leave
    the reservation stuck ``active``/``inflight`` forever --
    finish_failed_repeat_attempt (the SAME escalation seam v2 uses) is
    called exactly once, and the session ends with the failure — never a
    silent hang."""

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture, play_fails_on_attempt=2)

    with pytest.raises(RuntimeError, match="simulated sweep playback failure"):
        await run_and_consume(client, session)

    assert play_calls == [1, 2]
    assert len(finish_failed_calls) == 1
    reservation, failure_type = finish_failed_calls[0]
    assert reservation["attempt"] == 2
    assert failure_type == "RuntimeError"

    snapshot = fixture.repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["inflight"] is None
    assert target_state["status"] == "active"  # first accepted capture stands
    assert target_state["attempts"] == 2


@pytest.mark.asyncio
async def test_v3_driver_plan_stop_between_authorize_and_consume_plays_no_tone(
    monkeypatch, tmp_path
):
    """A Stop landing in the authorize→consume gap (the reservation exists,
    the tone has not started) must end the session cleanly: the runner's own
    ``stop_requested`` polling raises ``CaptureStopped`` before ``on_armed``
    can reach the audio player, ``finish_failed_repeat_attempt`` (the SAME
    seam the transport-failure path above uses) settles the reservation
    exactly once, and the phone gets ``sweep_cancelled`` — never a tone,
    never an ``inflight`` reservation left behind."""

    from jasper.capture_relay import session as relay_session

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)
    (
        run_and_consume,
        client,
        session,
        _phone,
        relay_backend,
        progress_calls,
        finish_failed_calls,
        play_calls,
    ) = _drive_v3_driver_plan(monkeypatch, fixture, stop_after_reserve=True)

    with pytest.raises(relay_session.CaptureStopped, match="capture stopped"):
        await run_and_consume(client, session)

    assert play_calls == []  # the tone never played
    phases = relay_backend.phases(session.session_id)
    assert "sweep_started" not in phases
    assert phases[-1] == "sweep_cancelled"

    assert len(finish_failed_calls) == 1
    reservation, failure_type = finish_failed_calls[0]
    assert reservation["attempt"] == 1
    assert failure_type == "CaptureStopped"

    snapshot = fixture.repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["inflight"] is None
    assert target_state["status"] == "active"  # attempt 1 failing is not terminal
    assert target_state["attempts"] == 1


@pytest.mark.asyncio
async def test_v3_driver_plan_on_armed_guard_agrees_with_its_own_authorization(
    monkeypatch, tmp_path
):
    """Hardware run 21 (jts3 @ 62af5b206): every v3 driver capture
    deterministically failed. ``authorize_begin`` (the real
    ``repeat_admission`` ledger) admits index=1/attempt=1; ~3s later, when
    the phone's ``armed`` event arrives, ``on_armed``'s envelope-derivation
    guard (``_assert_crossover_driver_action`` via
    ``correction_setup._validate_current_context``) recomputes
    ``build_crossover_envelope`` with THIS SAME reservation now live in the
    ledger. Pre-fix, that recompute no longer offers ``measure_driver`` as
    ``next_action`` (the in-flight reservation, combined with the guard's
    own ``action_status["relay"] = None``, makes ``orphaned_inflight`` true
    in ``crossover_envelope._targets``/``orphaned_inflight`` — see
    ``jasper/active_speaker/crossover_envelope.py`` around
    ``orphaned_inflight``), so the guard raises "the requested driver
    capture is not the server-owned next step": the v2-shaped guard vetoes
    the v3 plan's own authorized capture. Two computations of "server-owned
    next step" (the plan's reservation vs. the guard's envelope recompute)
    disagree — an SSOT violation.

    Drives the REAL production entry point
    (``correction_setup._handle_crossover_relay_capture``) end to end: a
    real ``repeat_admission`` ledger, a real ``CrossoverLevelLease`` (only
    its CamillaDSP/volume-hardware methods are stubbed — orthogonal to the
    guard), and the real, unstubbed ``_assert_crossover_driver_action`` /
    ``build_crossover_envelope``. Only the acoustic-analysis boundary
    (``_v3_driver_plan_fixture``'s established mocks) and playback
    mechanics are faked, exactly as the rest of this v3 plan suite already
    does.

    Fixed, all three captures of the repeat set complete without the guard
    ever firing. Pre-fix (confirmed by running this test against the
    unpatched guard), the very first ``on_armed`` raised:

        ValueError: the requested driver capture is not the server-owned
        next step

    — the same failure line as the run-21 journal traceback
    (``correction_setup.py:5359``).
    """
    import urllib.parse

    from jasper.active_speaker import repeat_admission
    from jasper.capture_relay import session as relay_session
    from jasper.capture_relay.client import RelayClient
    from jasper.correction import coordinator
    from jasper.web import correction_setup
    import jasper.output_topology as output_topology
    from tests.test_capture_relay_plan import FakePlanRelayBackend, PhonePlanDriver, _wav

    fixture = _v3_driver_plan_fixture(monkeypatch, tmp_path)

    def fake_status_payload():
        status = _envelope_status()
        status["targets"]["drivers"] = [
            {
                "speaker_group_id": "mono",
                "role": "woofer",
                "target_fingerprint": "c" * 64,
            },
        ]
        status["setup"]["applied_crossover"] = {
            "valid": True,
            "owner": "manual",
            "reason": None,
        }
        status["measurements"]["active_comparison_set"] = fixture.comparison_set
        _locked_level(status)
        # The live ledger snapshot, re-read fresh on every call (exactly as
        # the real correction_crossover_backend.status_payload() does) —
        # this is what makes the in-flight reservation visible to the
        # guard's second (armed-time) envelope recompute.
        status["level_match"]["repeats"] = {
            "targets": {},
            "failures": {},
            "durable": repeat_admission.snapshot(
                fixture.comparison_set, path=fixture.admission_path
            ),
        }
        return status

    monkeypatch.setattr(backend, "status_payload", fake_status_payload)
    monkeypatch.setattr(
        output_topology,
        "load_output_topology",
        lambda: SimpleNamespace(topology_id="topology-1"),
    )

    # backend.level_lease() already resolves to the fixture's fresh
    # CrossoverLevelLease() (_v3_driver_plan_fixture patches the module
    # global _LEVEL_LEASE) — reuse the SAME instance record_driver_capture's
    # internals see, and stub only its hardware-facing volume methods.
    lease = backend.level_lease()
    monkeypatch.setattr(
        lease, "driver_sweep_locked_main_volume_db", lambda *_a, **_k: -12.0
    )

    async def fake_acquire(*_a, **_k):
        return True

    async def fake_finish(*_a, **_k):
        return backend.UnresolvedVolumeRecoveryResult.EXACT_RESTORED

    monkeypatch.setattr(lease, "acquire_driver_sweep_volume", fake_acquire)
    monkeypatch.setattr(lease, "finish_sweep_volume", fake_finish)

    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.test")
    monkeypatch.setattr(correction_setup, "_crossover_blocking_phase", lambda: None)
    correction_setup._set_relay_capture(None)

    async def acquire_measurement_gate():
        return None

    async def release_measurement_gate(**_kwargs):
        return None

    monkeypatch.setattr(
        coordinator, "_acquire_measurement_gate", acquire_measurement_gate
    )
    monkeypatch.setattr(
        coordinator, "_release_measurement_gate", release_measurement_gate
    )

    class AlwaysActive:
        def __init__(self, *_args, **_kwargs):
            pass

        def assert_active(self):
            return None

    monkeypatch.setattr(relay_session, "CaptureActivityProbe", AlwaysActive)

    play_calls: list[int] = []

    async def play(*_args, **_kwargs):
        play_calls.append(len(play_calls) + 1)
        return {
            "status": "completed",
            "playback": {"audio_emitted": True},
            "sweep_meta": {"sample_rate": 48000},
            "playback_id": f"play-{len(play_calls)}",
            "test_level_dbfs": -72.0,
            "excitation": {},
        }

    monkeypatch.setattr(backend, "play_driver_capture_sweep", play)

    # The production `_post_host_event` closure builds its OWN RelayClient
    # against the real (urllib) transport rather than reusing the caller's
    # client — route it through the same in-memory backend the phone driver
    # uses so host-event posts (capture_authorized, sweep_complete, ...)
    # never touch the network.
    relay_backend = FakePlanRelayBackend()
    register_client = RelayClient("https://relay.test", transport=relay_backend)

    def fake_post_host_event(_relay_base, session_id, pull_token, payload):
        return register_client.post_host_event(session_id, pull_token, payload)

    monkeypatch.setattr(
        correction_setup,
        "_post_crossover_relay_host_event",
        fake_post_host_event,
    )

    captured = {}

    def fake_run_relay_capture(kind, _relay_base, *, return_url):
        captured["kind"] = kind
        return {"status": "awaiting_phone"}

    monkeypatch.setattr(
        correction_setup, "_run_relay_capture", fake_run_relay_capture
    )

    response = correction_setup._handle_crossover_relay_capture(
        _json_handler(
            {
                "kind": "driver",
                "speaker_group_id": "mono",
                "role": "woofer",
                "capture_geometry": "near_field",
            }
        )
    )
    assert response["relay"]["status"] == "awaiting_phone"
    kind = captured["kind"]

    rc = kind.open(
        register_client,
        "https://relay.test",
        "capture.test",
        "http://jts.local/correction/crossover/",
    )

    class _UploadingPhoneDriver(PhonePlanDriver):
        """Reacts to the REAL production ``on_armed`` — which only plays the
        sweep and posts ``sweep_complete``, never uploads — by uploading the
        WAV itself once it sees that phase, exactly as the future capture
        page will (mirrors ``_drive_v3_driver_plan``'s identical helper)."""

        def __init__(self, backend_, session):
            super().__init__(backend_, session)
            self._uploaded_for: set[tuple[int, int]] = set()

        def step(self):
            if not self.finished:
                host = (
                    self.backend.sessions[self.session.session_id]["host_event"]
                    or {}
                )
                if (
                    host.get("phase") == "sweep_complete"
                    and self.begun is not None
                    and self.begun not in self._uploaded_for
                ):
                    _index, attempt = self.begun
                    self.backend.phone_upload(
                        self.session.session_id,
                        self.session.content_key,
                        _wav(attempt),
                        index=attempt - 1,
                    )
                    self._uploaded_for.add(self.begun)
            super().step()

    phone = _UploadingPhoneDriver(relay_backend, rc.pi_session)

    def transport(method, url, headers, body):
        if method == "GET" and urllib.parse.urlsplit(url).path.endswith("/status"):
            phone.step()
        return relay_backend(method, url, headers, body)

    run_client = RelayClient("https://relay.test", transport=transport)

    await kind.run_and_consume(run_client, rc.pi_session)

    assert play_calls == [1, 2, 3]
    phases = relay_backend.phases(rc.pi_session.session_id)
    assert phases[-1] == "capture_set_complete"
    assert "sweep_failed" not in phases

    snapshot = repeat_admission.snapshot(
        fixture.comparison_set, path=fixture.admission_path
    )
    target_state = snapshot["targets"][fixture.repeat_target_id]
    assert target_state["status"] == "completed"
    assert target_state["attempts"] == 3


def test_v3_capture_plan_progress_renders_in_the_wizard_envelope():
    """The write side (correction_setup._publish_crossover_capture_plan_progress)
    and the read side (crossover_envelope._plan_measuring_verdict, landed
    dormant with #1550) agree on the SAME shape:
    ``{role, capture_target, accepted}``."""

    from jasper.active_speaker import crossover_envelope
    from jasper.web import correction_setup

    correction_setup._begin_relay_capture("crossover_sweep:driver")
    correction_setup._set_relay_capture(
        {"status": "awaiting_phone", "kind": "crossover_sweep:driver", "tap_link": "x"}
    )
    try:
        correction_setup._publish_crossover_capture_plan_progress(
            "crossover_sweep:driver",
            {"role": "woofer", "capture_target": 3, "accepted": 1},
        )
        relay_snapshot = correction_setup._get_relay_capture_for(
            "crossover_sweep:", "level_ramp:crossover"
        )
        assert relay_snapshot is not None
        assert relay_snapshot["capture_plan"] == {
            "role": "woofer",
            "capture_target": 3,
            "accepted": 1,
        }
        verdict = crossover_envelope._plan_measuring_verdict(
            relay_snapshot["capture_plan"]
        )
        assert verdict == "Measuring the woofer — follow your phone. 1 of 3 done."
    finally:
        correction_setup._set_relay_capture(None)


def test_v3_capture_plan_progress_drops_silently_for_a_stale_owner():
    """A concurrent Stop (or the session already ending) has already moved
    the global relay slot on — the progress publish must not resurrect it or
    clobber whatever now owns the slot."""

    from jasper.web import correction_setup

    correction_setup._set_relay_capture(
        {"status": "stopped", "kind": "crossover_sweep:driver"}
    )
    try:
        correction_setup._publish_crossover_capture_plan_progress(
            "crossover_sweep:driver",
            {"role": "woofer", "capture_target": 3, "accepted": 2},
        )
        assert correction_setup._get_relay_capture() == {
            "status": "stopped",
            "kind": "crossover_sweep:driver",
        }
    finally:
        correction_setup._set_relay_capture(None)
