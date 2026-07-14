# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Secure correction crossover measurement flow."""

from __future__ import annotations

import json
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
    ("final_write_fails", "abort_write_fails", "complete_write_fails"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (False, False, True),
        (False, True, True),
    ],
)
@pytest.mark.parametrize("capture_geometry", ("near_field", "reference_axis"))
def test_driver_capture_wires_three_repeats_before_one_durable_record(
    monkeypatch,
    tmp_path,
    final_write_fails,
    abort_write_fails,
    complete_write_fails,
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

    def capture_attempt(attempt):
        reservation = repeat_admission.reserve(
            comparison_set,
            target_id=repeat_target_id,
            target_fingerprint=repeat_target_fingerprint,
            path=admission_path,
        )
        assert reservation["attempt"] == attempt
        return backend.record_driver_capture(
            {
                **raw,
                "repeat_reservation": reservation,
            },
            b"wav",
            placement_proof=placement_proof,
            repeat_store=store,
        )

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


def test_terminal_transport_failure_finalizes_two_existing_accepted_repeats(
    monkeypatch, tmp_path
):
    from jasper.active_speaker import (
        bundles as active_speaker_bundles,
        commissioning_capture,
        repeat_admission,
    )

    comparison = dict(_COMPARISON_SET)
    target_fingerprint = "c" * 64
    placement_proof = _placement_proof(
        "driver_same_distance_v1", "woofer", target_fingerprint
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
        "capture_geometry": "near_field",
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
        capture_geometry="near_field",
        reservation=fourth,
        failure_type="CaptureAborted",
        repeat_store=store,
    )
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

    payload = backend.status_payload()

    assert payload["commission"] == {"ramp": {"pending": None}}


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
        "session_id": "session-1",
        "run_id": handle.run_id,
        "owner_generation": 1,
        "lifecycle_state": "unconfigured",
        "attempt_count": 0,
        "last_transition": None,
        "updated_at": current["updated_at"],
        "state_fingerprint": store.snapshot()["fingerprint"],
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
    assert "schedulePoll(relayActive ? POLL_MS : null)" in source
    assert "renderActions(null);" in source
    assert "env.alternate_actions" in source
    assert "baseline_candidate_fingerprint_mismatch" in source
    assert "candidateChanged" in source
    assert "await refresh();" in source


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
    assert env["schema_version"] == 3


def test_crossover_envelope_requires_protected_setup_first():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    status["setup"]["status"] = "blocked"
    env = crossover_envelope.build_crossover_envelope(status)
    assert env["screen"] == "speaker_setup"
    assert env["next_action"]["href"] == "/sound/"
    assert env["next_action"]["id"] == "speaker_setup"


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
    assert env["progress"] == {"position": 4, "total": 4}


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
    assert env["next_action"]["label"] == "Measure fixed-axis woofer — repeat 2"


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
    assert "Repeat 3; 2 of 3 accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure woofer — repeat 3"


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
    assert "Repeat 3; 2 of 3 accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure fixed-axis woofer — repeat 3"


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
    assert "Repeat 4; 1 of 3 accepted" in env["verdict_text"]
    assert env["next_action"]["label"] == "Measure woofer — repeat 4"


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


def test_live_relay_does_not_misclassify_its_inflight_repeat_as_orphaned():
    from jasper.active_speaker import crossover_envelope

    status = _envelope_status()
    _locked_level(status)
    status["relay"] = {"status": "awaiting_phone", "kind": "crossover_sweep:driver"}
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
    assert not any(
        nudge["code"] == "crossover_repeat_persistence_interrupted"
        for nudge in env["nudges"]
    )


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
    assert env["schema_version"] == 3


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
        measurements = {
            "summary": {
                "latest_driver_measurements": {
                    "mono:woofer": {
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
    assert host_events == ["sweep_started", "sweep_complete"]


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


@pytest.mark.parametrize(
    "route",
    (
        "/crossover/level-match",
        "/crossover/relay-capture",
        "/crossover/apply",
        "/crossover/driver-test",
        "/crossover/summed-test",
        "/crossover/driver-capture-sweep",
        "/crossover/summed-capture-sweep",
        "/crossover/summed-capture",
    ),
)
def test_unresolved_volume_refuses_every_crossover_action_route(
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
