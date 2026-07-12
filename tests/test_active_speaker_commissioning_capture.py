# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The mic-backed acoustic verdict -> commissioning measurement wire (gap-1).

``driver_acoustics.analyze_driver_capture`` / ``analyze_summed_crossover`` had no
caller. ``commissioning_capture`` is that caller: it derives each driver's
expected passband from the preset crossovers, runs the acoustic analysis on a
captured sweep, maps the verdict to a measurement outcome, and records it (with
the real ``observed_mic_dbfs`` and the acoustic block) through
``measurement.record_*``. These tests pin the passband derivation, the
verdict->outcome mapping, the "unusable capture records nothing" rule, and that
the new acoustic evidence block round-trips into the persisted record.

The acoustic analysis itself (numpy/scipy deconvolution) is covered by
``test_active_speaker_driver_acoustics``; here ``analyze`` is injected so the
wire is exercised deterministically and hardware-free.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jasper.active_speaker import crossover_alignment as ca
from jasper.active_speaker.commissioning_capture import (
    RESERVED_CROSSOVER_EVENTS,
    DEFAULT_REPEAT_TARGET,
    DRIVER_VERDICT_TO_OUTCOME,
    REPEAT_OUTLIER_DB,
    SUMMED_VERDICT_TO_OUTCOME,
    _summed_alignment_snr,
    build_crossover_alignment_proposal as _build_crossover_alignment_proposal,
    aggregate_driver_repeats,
    driver_passband_hz,
    primary_crossover_fc_hz,
    record_driver_acoustic_capture,
    record_driver_repeat_aggregate,
    record_summed_acoustic_capture,
    region_for_fc,
)
from jasper.active_speaker.driver_acoustics import (
    DRIVER_VERDICTS,
    SUMMED_VERDICTS,
    DriverAcousticResult,
    SummedAcousticResult,
)
from jasper.active_speaker.measurement import (
    MAX_SUMMED_RECORDS,
    active_driver_targets,
    active_summed_targets,
    load_measurement_state,
    record_summed_test_artifact,
    record_summed_validation,
    start_active_comparison_set,
)
from jasper.active_speaker.profile import ActiveSpeakerPreset, required_driver_roles
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import mono_output_topology

# Canonical fixtures reused across the active-speaker suite.
from tests.test_active_speaker_measurement import (
    _safe_session,
    _three_way_topology,
)
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset


def _topology(**kwargs) -> OutputTopology:
    return mono_output_topology(topology_name="Bench mono", **kwargs)


def _two_way() -> ActiveSpeakerPreset:
    # Mono 2-way matching _topology(): woofer=output 0, tweeter=output 1,
    # crossover at 1600 Hz.
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _three_way() -> ActiveSpeakerPreset:
    # crossovers at 350 (woofer/mid) and 2500 (mid/tweeter).
    return ActiveSpeakerPreset.from_mapping(_three_way_preset())


def _alignment_applied_profile(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology | None = None,
    *,
    topology_id: str | None = None,
) -> dict:
    topology = topology or _topology()
    resolved_topology_id = topology_id or topology.topology_id
    corrections = {
        role: {"gain_db": 0.0, "delay_ms": 0.0, "inverted": False}
        for role in required_driver_roles(preset.way_count)
    }
    return {
        "status": "applied",
        "baseline_id": "baseline-test",
        "source": {"fingerprint": "protected-profile"},
        "recomposition_snapshot": {
            "topology_id": resolved_topology_id,
            "preset": preset.to_dict(),
            "corrections": corrections,
        },
    }


def build_crossover_alignment_proposal(
    preset: ActiveSpeakerPreset,
    measurements: dict,
    **kwargs,
) -> dict:
    comparison_set = measurements.get("active_comparison_set")
    comparison_set = comparison_set if isinstance(comparison_set, dict) else {}
    kwargs.setdefault(
        "expected_applied_profile",
        _alignment_applied_profile(
            preset,
            topology_id=str(comparison_set.get("topology_id") or "test-topology"),
        ),
    )
    return _build_crossover_alignment_proposal(preset, measurements, **kwargs)


def _alignment_excitation(
    preset: ActiveSpeakerPreset,
    topology: OutputTopology | None = None,
) -> dict:
    topology = topology or _topology()
    sweep_peak_dbfs = -12.0
    return {
        "schema_version": 1,
        "scope": "sweep_plus_applied_full_layer_a_graph",
        "sweep_peak_dbfs": sweep_peak_dbfs,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-test",
        "topology_id": topology.topology_id,
        "corrections": {
            role: {
                "gain_db": 0.0,
                "delay_ms": 0.0,
                "inverted": False,
                "effective_peak_dbfs": sweep_peak_dbfs,
            }
            for role in required_driver_roles(preset.way_count)
        },
    }


def _driver_result(
    verdict: str,
    *,
    present: bool = False,
    observed: float = -32.0,
    clipping: bool = False,
    snr: dict | None = None,
) -> DriverAcousticResult:
    return DriverAcousticResult(
        verdict=verdict,
        present=present,
        observed_mic_dbfs=observed,
        peak_dbfs=observed + 12.0,
        in_band_db=-30.0,
        out_of_band_db=-50.0,
        band_separation_db=20.0,
        passband_hz=(40.0, 1600.0),
        mic_clipping=clipping,
        quality={"failed": False, "rms_dbfs": observed},
        snr=snr,
    )


def _summed_result(
    verdict: str, *, observed: float = -34.0, snr: dict | None = None,
) -> SummedAcousticResult:
    return SummedAcousticResult(
        verdict=verdict,
        null_depth_db=2.0 if verdict == "blend_ok" else 12.0,
        crossover_fc_hz=1600.0,
        observed_mic_dbfs=observed,
        mic_clipping=False,
        quality={"failed": False, "rms_dbfs": observed},
        snr=snr,
    )


# --- passband / crossover derivation ----------------------------------------


def test_driver_passband_two_way():
    preset = _two_way()
    # Woofer is the lower driver -> low-passed at 1600; low side clamps to 40.
    assert driver_passband_hz(preset, "woofer") == (40.0, 1600.0)
    # Tweeter is the upper driver -> high-passed at 1600; high side clamps.
    assert driver_passband_hz(preset, "tweeter") == (1600.0, 18000.0)


def test_driver_passband_three_way_mid_is_bounded_both_sides():
    preset = _three_way()
    assert driver_passband_hz(preset, "woofer") == (40.0, 350.0)
    assert driver_passband_hz(preset, "mid") == (350.0, 2500.0)
    assert driver_passband_hz(preset, "tweeter") == (2500.0, 18000.0)


def test_primary_crossover_is_lowest():
    assert primary_crossover_fc_hz(_two_way()) == 1600.0
    assert primary_crossover_fc_hz(_three_way()) == 350.0


def test_verdict_maps_cover_the_full_recordable_vocabulary():
    # Guard the coupling to driver_acoustics' verdict vocabulary: every verdict
    # except the un-recordable `unusable_capture` maps to an outcome. A renamed
    # or added verdict then fails here instead of silently `.get()`->None->not
    # recorded.
    assert set(DRIVER_VERDICT_TO_OUTCOME) | {"unusable_capture"} == DRIVER_VERDICTS
    assert set(SUMMED_VERDICT_TO_OUTCOME) | {"unusable_capture"} == SUMMED_VERDICTS
    # The unusable verdict is never mapped (it must not record).
    assert "unusable_capture" not in DRIVER_VERDICT_TO_OUTCOME
    assert "unusable_capture" not in SUMMED_VERDICT_TO_OUTCOME


# --- driver-capture wire -----------------------------------------------------


def _capture_driver(
    tmp_path: Path,
    result: DriverAcousticResult,
    *,
    role: str = "woofer",
    output_index: int = 0,
    noise_band_report=None,
    capture_geometry: str = "near_field",
):
    seen: dict = {}

    def fake_analyze(
        wav, meta, *, passband_hz, overlap_fcs=(), has_mic_calibration,
        calibration=None, noise_band_report=None,
        capture_geometry="near_field",
    ):
        seen["passband_hz"] = passband_hz
        seen["overlap_fcs"] = tuple(overlap_fcs)
        seen["wav"] = wav
        seen["calibration"] = calibration
        seen["noise_band_report"] = noise_band_report
        seen["capture_geometry"] = capture_geometry
        return result

    out = record_driver_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        role=role,
        captured_wav=tmp_path / "cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        playback_id="pb1",
        safe_session=_safe_session(
            role=role, output_index=output_index, playback_id="pb1"
        ),
        state_path=tmp_path / "measurements.json",
        capture_geometry=capture_geometry,
        analyze=fake_analyze,
        noise_band_report=noise_band_report,
    )
    return out, seen


def test_present_records_heard_correct_driver_and_acoustic_block(tmp_path: Path):
    out, seen = _capture_driver(tmp_path, _driver_result("present", present=True))
    assert out["recorded"] is True
    assert out["outcome"] == "heard_correct_driver"
    # The woofer passband + its crossover Fc (for the overlap-band level match)
    # were derived from the preset and handed to analyze.
    assert seen["passband_hz"] == (40.0, 1600.0)
    assert seen["overlap_fcs"] == (1600.0,)
    record = out["measurement"]["driver_measurements"][-1]
    assert record["outcome"] == "heard_correct_driver"
    assert record["observed_mic_dbfs"] == -32.0
    # The acoustic verdict block is persisted as new evidence on the record.
    assert record["acoustic"]["verdict"] == "present"
    assert record["acoustic"]["kind"] == "jts_active_speaker_driver_acoustics"
    # Identity verified + floor-confirmed woofer + not clipping -> captured.
    assert record["captured"] is True


def test_driver_capture_threads_noise_band_report_into_analyzer_and_record(
    tmp_path: Path,
):
    """noise_band_report threads from the record_* kwarg into the analyzer
    call AND the resulting acoustic['snr'] block lands, unchanged, in the
    persisted record — the SC-1 block round-trips through this layer exactly
    like every other acoustic field."""
    noise_report = [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0},
    ]
    fake_snr = {
        "schema_version": 1,
        "decision_class": "magnitude",
        "relevant_hz": [40.0, 1600.0],
        "bands": [],
        "worst_relevant": None,
        "verdict": "ok",
    }
    out, seen = _capture_driver(
        tmp_path,
        _driver_result("present", present=True, snr=fake_snr),
        noise_band_report=noise_report,
    )
    # Threaded INTO the analyzer call.
    assert seen["noise_band_report"] == noise_report
    # ...and the analyzer's snr block lands in the persisted acoustic block.
    record = out["measurement"]["driver_measurements"][-1]
    assert record["acoustic"]["snr"] == fake_snr


def test_driver_capture_without_noise_band_report_passes_none(tmp_path: Path):
    # The shipped no-noise-input flow: analyze still receives the kwarg
    # (always forwarded), but as None.
    out, seen = _capture_driver(tmp_path, _driver_result("present", present=True))
    assert seen["noise_band_report"] is None
    record = out["measurement"]["driver_measurements"][-1]
    assert record["acoustic"]["snr"] is None
def test_driver_capture_defaults_capture_geometry_to_near_field(tmp_path: Path):
    _out, seen = _capture_driver(tmp_path, _driver_result("present", present=True))
    assert seen["capture_geometry"] == "near_field"


def test_driver_capture_threads_capture_geometry_to_analyze(tmp_path: Path):
    out, seen = _capture_driver(
        tmp_path,
        _driver_result("present", present=True),
        capture_geometry="reference_axis",
    )
    assert seen["capture_geometry"] == "reference_axis"
    assert out["recorded"] is True
    record = out["measurement"]["driver_measurements"][-1]
    # The gating block rides the acoustic dict opaquely — always a key on
    # the persisted record (None when the injected result carries none).
    assert "gating" in record["acoustic"]


def test_out_of_band_records_heard_wrong_driver_not_captured(tmp_path: Path):
    out, _ = _capture_driver(tmp_path, _driver_result("out_of_band"))
    assert out["outcome"] == "heard_wrong_driver"
    record = out["measurement"]["driver_measurements"][-1]
    assert record["outcome"] == "heard_wrong_driver"
    assert record["captured"] is False


def test_silent_records_silent(tmp_path: Path):
    out, _ = _capture_driver(tmp_path, _driver_result("silent"))
    assert out["outcome"] == "silent"
    assert out["measurement"]["driver_measurements"][-1]["outcome"] == "silent"


def test_unusable_capture_records_nothing(tmp_path: Path):
    calls = {"n": 0}

    def spy_record(*args, **kwargs):
        calls["n"] += 1
        return {}

    out = record_driver_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        role="woofer",
        captured_wav=tmp_path / "cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        analyze=lambda *a, **k: _driver_result("unusable_capture"),
        record=spy_record,
    )
    assert out["recorded"] is False
    assert out["skipped_reason"] == "unusable_capture"
    assert out["measurement"] is None
    # An untrustworthy capture must not fabricate a measurement record.
    assert calls["n"] == 0
    # The acoustic block is still returned so the UI can ask for a re-capture.
    assert out["acoustic"]["verdict"] == "unusable_capture"


def test_mic_clipping_flows_through_to_record(tmp_path: Path):
    out, _ = _capture_driver(
        tmp_path, _driver_result("present", present=True, clipping=True)
    )
    record = out["measurement"]["driver_measurements"][-1]
    assert record["mic_clipping"] is True
    # Clipping demotes captured even for a "present" verdict.
    assert record["captured"] is False


def test_present_without_floor_confirmation_is_not_captured(tmp_path: Path):
    # The acoustic verdict must NEVER bypass the operator floor gate: a "present"
    # verdict with no armed/floor-confirmed safe session records the evidence but
    # leaves `captured` False (the same gate the operator quiet-test path uses).
    out = record_driver_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        role="woofer",
        captured_wav=tmp_path / "cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        playback_id="pb1",
        safe_session=None,  # no floor confirmation
        state_path=tmp_path / "measurements.json",
        analyze=lambda *a, **k: _driver_result("present", present=True),
    )
    assert out["recorded"] is True
    record = out["measurement"]["driver_measurements"][-1]
    assert record["outcome"] == "heard_correct_driver"
    assert record["captured"] is False
    assert any(issue["severity"] == "blocker" for issue in record["issues"])


def test_present_uses_durable_floor_confirmation_after_session_expiry(
    tmp_path: Path,
):
    confirmation = {
        "accepted": True,
        "playback_id": "pb1",
        "target": {
            "speaker_group_id": "mono",
            "role": "woofer",
            "output_index": 0,
        },
    }
    out = record_driver_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        role="woofer",
        captured_wav=tmp_path / "cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        playback_id="pb1",
        safe_session=None,
        durable_floor_confirmation=confirmation,
        state_path=tmp_path / "measurements.json",
        analyze=lambda *a, **k: _driver_result("present", present=True),
    )

    record = out["measurement"]["driver_measurements"][-1]
    assert record["captured"] is True
    assert record["floor_confirmation"] == confirmation


def test_driver_capture_persists_verified_played_excitation(tmp_path: Path):
    seen: dict = {}
    ledger = {
        "schema_version": 1,
        "scope": "sweep_plus_role_varying_commission_gain",
        "sweep_peak_dbfs": -12.0,
        "commissioning_gain_db": -9.0,
        "effective_peak_dbfs": -21.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-1",
        "topology_id": _topology().topology_id,
        "role": "woofer",
    }

    def spy_record(_topology, raw, **_kwargs):
        seen.update(raw)
        return {"driver_measurements": [dict(raw)]}

    out = record_driver_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        role="woofer",
        captured_wav=tmp_path / "cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096, "amplitude_dbfs": -12.0},
        playback_id="pb1",
        test_level_dbfs=-9.0,
        excitation=ledger,
        analyze=lambda *a, **k: _driver_result("present", present=True),
        record=spy_record,
    )

    assert out["recorded"] is True
    assert out["excitation"] == ledger
    assert seen["observed_mic_dbfs"] == -32.0
    assert seen["excitation"] == ledger


def test_driver_capture_rejects_excitation_that_does_not_match_played_sweep(
    tmp_path: Path,
):
    with pytest.raises(
        ValueError,
        match="excitation does not match the played sweep",
    ):
        record_driver_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            role="woofer",
            captured_wav=tmp_path / "cap.wav",
            sweep_meta={
                "sample_rate": 48000,
                "n_samples": 4096,
                "amplitude_dbfs": -18.0,
            },
            test_level_dbfs=-9.0,
            excitation={
                "schema_version": 1,
                "scope": "sweep_plus_role_varying_commission_gain",
                "sweep_peak_dbfs": -12.0,
                "commissioning_gain_db": -9.0,
                "effective_peak_dbfs": -21.0,
                "role": "woofer",
                "topology_id": _topology().topology_id,
            },
            analyze=lambda *a, **k: _driver_result("present", present=True),
        )


# --- summed-crossover wire ---------------------------------------------------


def test_summed_blend_ok_calls_record_with_outcome_and_acoustic():
    seen: dict = {}

    def spy_record(topology, raw, **kwargs):
        seen["raw"] = raw
        return {"summed_validations": [dict(raw)]}

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        summed_test_id="st1",
        analyze=lambda *a, **k: _summed_result("blend_ok"),
        record=spy_record,
    )
    assert out["recorded"] is True
    assert out["outcome"] == "blend_ok"
    # Defaulted to the (only) crossover frequency of the 2-way preset.
    assert out["crossover_fc_hz"] == 1600.0
    assert seen["raw"]["outcome"] == "blend_ok"
    assert seen["raw"]["observed_mic_dbfs"] == -34.0
    assert seen["raw"]["summed_test_id"] == "st1"
    assert seen["raw"]["acoustic"]["verdict"] == "blend_ok"


def test_summed_capture_threads_noise_band_report_into_analyzer_and_record():
    """noise_band_report (+ the existing noise_floor_dbfs scalar) thread from
    the record_* kwargs into the analyzer call AND the analyzer's snr /
    null_depth_capped fields land, unchanged, in the persisted record."""
    noise_report = [
        {"band_id": "mid", "band_hz": [1000.0, 4000.0], "level_dbfs": -80.0},
    ]
    fake_snr = {
        "schema_version": 1,
        "decision_class": "alignment",
        "relevant_hz": [800.0, 3200.0],
        "bands": [],
        "worst_relevant": {"band_id": "mid", "estimated_snr_db": 20.0, "verdict": "insufficient"},
        "verdict": "insufficient",
    }
    seen: dict = {}

    def fake_analyze(
        wav, meta, *, crossover_fc_hz, null_threshold_db, expect_null,
        has_mic_calibration, calibration=None, noise_band_report=None,
        noise_floor_dbfs=None, capture_geometry="near_field",
    ):
        seen["noise_band_report"] = noise_band_report
        seen["noise_floor_dbfs"] = noise_floor_dbfs
        return SummedAcousticResult(
            verdict="polarity_or_delay_problem",
            null_depth_db=10.0,
            crossover_fc_hz=crossover_fc_hz,
            observed_mic_dbfs=-34.0,
            mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            snr=fake_snr,
            null_depth_capped=True,
        )

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        noise_band_report=noise_report,
        noise_floor_dbfs=-70.0,
        analyze=fake_analyze,
        record=lambda topology, raw, **kw: {"summed_validations": [dict(raw)]},
    )
    assert seen["noise_band_report"] == noise_report
    assert seen["noise_floor_dbfs"] == -70.0
    assert out["acoustic"]["snr"] == fake_snr
    assert out["acoustic"]["null_depth_capped"] is True
    # The pre-existing scalar bolt-on is unaffected by the new SC-1 wiring.
    assert out["acoustic"]["noise_floor_dbfs"] == -70.0
    assert out["acoustic"]["signal_over_noise_db"] == pytest.approx(-34.0 - -70.0)


def test_summed_capture_threads_capture_geometry_to_analyze():
    seen: dict = {}

    def fake_analyze(
        wav, meta, *, crossover_fc_hz, null_threshold_db, expect_null,
        has_mic_calibration, calibration=None, capture_geometry="near_field",
        noise_band_report=None, noise_floor_dbfs=None,
    ):
        seen["capture_geometry"] = capture_geometry
        return _summed_result("blend_ok")

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        capture_geometry="reference_axis",
        analyze=fake_analyze,
        record=lambda topology, raw, **kw: {"summed_validations": [dict(raw)]},
    )
    assert seen["capture_geometry"] == "reference_axis"
    assert out["recorded"] is True


def test_summed_capture_below_validity_floor_records_nothing():
    """A reference-axis summed capture whose crossover Fc (or its lower
    shoulder) sits below the IR-gating validity floor comes back as
    unusable_capture with the gating block populated
    (driver_acoustics.analyze_summed_crossover's own contract, pinned in
    test_active_speaker_driver_acoustics.py); this pins that the wire here
    still records nothing for it, same as any other unusable_capture."""
    calls = {"n": 0}

    def spy_record(*a, **k):
        calls["n"] += 1
        return {}

    gated_below_floor = SummedAcousticResult(
        verdict="unusable_capture",
        null_depth_db=float("nan"),
        crossover_fc_hz=200.0,
        observed_mic_dbfs=-34.0,
        mic_clipping=False,
        quality={"failed": False, "rms_dbfs": -34.0},
        gating={
            "schema_version": 1,
            "applied": True,
            "exempt_reason": None,
            "direct_peak_ms": 5.0,
            "first_reflection_ms": 8.5,
            "window_ms": 6.67,
            "window": "half_hann_tail",
            "f_valid_floor_hz": 150.0,
            "floor_source": "measured_reflection",
        },
        above_validity_floor=False,
    )

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        crossover_fc_hz=200.0,
        capture_geometry="reference_axis",
        analyze=lambda *a, **k: gated_below_floor,
        record=spy_record,
    )
    assert out["recorded"] is False
    assert out["skipped_reason"] == "unusable_capture"
    assert calls["n"] == 0
    assert out["acoustic"]["gating"]["applied"] is True
    assert out["acoustic"]["above_validity_floor"] is False


def test_summed_capture_persists_verified_full_graph_excitation():
    seen = {}
    ledger = {
        "schema_version": 1,
        "scope": "sweep_plus_applied_full_layer_a_graph",
        "sweep_peak_dbfs": -12.0,
        "gain_source": "applied_baseline_recomposition_snapshot",
        "baseline_id": "baseline-full",
        "topology_id": _topology().topology_id,
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
    }

    def record(_topology_value, raw, **_kwargs):
        seen.update(raw)
        return {"summed_validations": [dict(raw)]}

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={
            "sample_rate": 48000,
            "n_samples": 4096,
            "amplitude_dbfs": -12.0,
        },
        excitation=ledger,
        analyze=lambda *a, **k: _summed_result("blend_ok"),
        record=record,
    )

    assert out["excitation"] == ledger
    assert seen["excitation"] == ledger


def test_summed_polarity_problem_maps_through():
    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        analyze=lambda *a, **k: _summed_result("polarity_or_delay_problem"),
        record=lambda topology, raw, **kw: {"summed_validations": [dict(raw)]},
    )
    assert out["outcome"] == "polarity_or_delay_problem"


def test_summed_unusable_records_nothing():
    calls = {"n": 0}

    def spy_record(*a, **k):
        calls["n"] += 1
        return {}

    out = record_summed_acoustic_capture(
        _topology(),
        _two_way(),
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        analyze=lambda *a, **k: _summed_result("unusable_capture"),
        record=spy_record,
    )
    assert out["recorded"] is False
    assert out["skipped_reason"] == "unusable_capture"
    assert calls["n"] == 0


class _NoCrossoverPreset:
    """A preset with no crossover regions (e.g. a single full-range driver)."""

    crossover_regions: tuple = ()


def test_summed_no_crossover_region_records_nothing():
    # No crossover means no blend to validate; the wire skips rather than
    # guessing a frequency, and never runs analyze or record.
    preset = _NoCrossoverPreset()
    assert primary_crossover_fc_hz(preset) is None

    out = record_summed_acoustic_capture(
        _topology(),
        preset,
        speaker_group_id="mono",
        captured_wav="cap.wav",
        sweep_meta={"sample_rate": 48000, "n_samples": 4096},
        analyze=lambda *a, **k: pytest.fail("analyze must not run without a crossover"),
        record=lambda *a, **k: pytest.fail("record must not run without a crossover"),
    )
    assert out["recorded"] is False
    assert out["skipped_reason"] == "no_crossover_region"


# --- measurement.py acoustic field round-trip (real record) ------------------


def test_record_summed_validation_persists_acoustic_block(tmp_path: Path):
    # The summed record always appends (gates only flip `validated`), so the
    # acoustic block round-trips even before the full gate set is satisfied.
    state = record_summed_validation(
        _topology(),
        {
            "speaker_group_id": "mono",
            "outcome": "blend_ok",
            "observed_mic_dbfs": -34.0,
            "acoustic": _summed_result("blend_ok").to_dict(),
            "excitation": {
                "schema_version": 1,
                "scope": "sweep_plus_applied_full_layer_a_graph",
                "sweep_peak_dbfs": -12.0,
            },
        },
        state_path=tmp_path / "measurements.json",
    )
    record = state["summed_validations"][-1]
    assert record["acoustic"]["verdict"] == "blend_ok"
    assert record["acoustic"]["kind"] == "jts_active_speaker_summed_acoustics"
    assert record["excitation"]["sweep_peak_dbfs"] == -12.0


# --- _summed_alignment_snr (B-1: scalar-only 'unknown' must not degrade) -----
#
# jasper/web/correction_crossover_flow.py bolts a scalar noise_floor_dbfs onto
# every summed record and never supplies noise_band_report, so the SC-1
# alignment-class snr block on every LIVE summed capture has an overall
# verdict of "unknown" with worst_relevant=None (see
# jasper.audio_measurement.snr_policy._band_verdict: a scalar_fallback method
# always reads "unknown" for the alignment decision class). That must resolve
# to alignment_snr_ok=None (no evidence, no degrade) — not False (confirmed
# insufficient) — or every real summed capture silently downgrades keep/
# aligned to review/unknown.


def test_summed_alignment_snr_no_record_or_no_acoustic_is_unknown_no_degrade():
    assert _summed_alignment_snr(None) == (None, False)
    assert _summed_alignment_snr("not a mapping") == (None, False)
    assert _summed_alignment_snr({}) == (None, False)
    assert _summed_alignment_snr({"acoustic": "not a mapping"}) == (None, False)


def test_summed_alignment_snr_no_snr_block_is_unknown_no_degrade():
    # No caller supplied any noise evidence at all.
    record = {"acoustic": {"verdict": "blend_ok"}}
    assert _summed_alignment_snr(record) == (None, False)


def test_summed_alignment_snr_scalar_only_worst_relevant_none_is_unknown_no_degrade():
    # The live shape: an snr block IS present (some evidence was supplied),
    # but its overall verdict is "unknown" because that evidence was
    # scalar-only (or covered no relevant band) — worst_relevant is None.
    # This must read as "no evidence" (None), never "confirmed bad" (False).
    record = {
        "acoustic": {
            "snr": {
                "schema_version": 1,
                "decision_class": "alignment",
                "relevant_hz": [800.0, 3200.0],
                "bands": [
                    {
                        "band_id": "mid",
                        "band_hz": [1000.0, 4000.0],
                        "estimated_snr_db": 45.0,
                        "verdict": "unknown",
                        "shortfall_db": None,
                        "method": "scalar_fallback",
                    },
                ],
                "worst_relevant": None,
                "verdict": "unknown",
            },
        },
    }
    alignment_snr_ok, null_depth_capped = _summed_alignment_snr(record)
    assert alignment_snr_ok is None
    assert null_depth_capped is False


def test_summed_alignment_snr_real_insufficient_reading_is_false():
    # A real per-band reading that did NOT clear the alignment bar is the
    # only case that may degrade the proposal.
    record = {
        "acoustic": {
            "snr": {
                "worst_relevant": {
                    "band_id": "mid",
                    "estimated_snr_db": 28.0,
                    "verdict": "insufficient",
                },
                "verdict": "insufficient",
            },
        },
    }
    alignment_snr_ok, _ = _summed_alignment_snr(record)
    assert alignment_snr_ok is False


def test_summed_alignment_snr_both_real_ok_readings_are_true():
    record = {
        "acoustic": {
            "snr": {
                "worst_relevant": {
                    "band_id": "mid",
                    "estimated_snr_db": 40.0,
                    "verdict": "ok",
                },
                "verdict": "ok",
            },
        },
    }
    alignment_snr_ok, _ = _summed_alignment_snr(record, record)
    assert alignment_snr_ok is True


def test_summed_alignment_snr_null_depth_capped_passes_through_regardless_of_verdict():
    for snr_block in (
        None,
        {"worst_relevant": None, "verdict": "unknown"},
        {"worst_relevant": {"verdict": "ok"}, "verdict": "ok"},
    ):
        acoustic: dict = {"null_depth_capped": True}
        if snr_block is not None:
            acoustic["snr"] = snr_block
        _, null_depth_capped = _summed_alignment_snr({"acoustic": acoustic})
        assert null_depth_capped is True


def test_build_proposal_scalar_only_snr_never_degrades_keep_to_review():
    """End-to-end guard pinning the live scalar-injection path (B-1).

    Calibrated records plus a summed capture whose acoustic block carries a
    scalar-only 'unknown' snr block (worst_relevant=None — exactly what
    jasper/web/correction_crossover_flow.py produces today) and a deep
    reverse-polarity null must still authorize polarity_action='keep', never
    'review', and must never raise 'alignment_snr_insufficient'.
    """
    from tests.test_active_speaker_crossover_alignment import _state

    state = _state(calibrated=True, reverse_null=14.0)
    record = state["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"][
        "reverse"
    ]
    record["acoustic"].update({
        "null_depth_capped": False,
        "snr": {
            "schema_version": 1,
            "decision_class": "alignment",
            "relevant_hz": [800.0, 3200.0],
            "bands": [
                {
                    "band_id": "mid",
                    "band_hz": [1000.0, 4000.0],
                    "estimated_snr_db": 45.0,
                    "verdict": "unknown",
                    "shortfall_db": None,
                    "method": "scalar_fallback",
                },
            ],
            "worst_relevant": None,
            "verdict": "unknown",
        },
    })
    out = build_crossover_alignment_proposal(
        _two_way(), state, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    proposal = out["proposal"]
    assert proposal["polarity_action"] == ca.POLARITY_KEEP
    assert out["proposals"][0]["decision_quality"] == {
        "alignment_snr_ok": None,
        "null_depth_capped": False,
    }
    codes = {issue["code"] for issue in proposal["issues"]}
    assert "alignment_snr_insufficient" not in codes
# --- Step 2: three-repeat capture — aggregate_driver_repeats ----------------


def _repeat(
    level_dbfs: float,
    *,
    verdict: str = "present",
    clipping: bool = False,
    artifact_path: str | None = None,
    snr_verdict: str | None = None,
    above_validity_floor: bool | None = None,
) -> dict:
    """A minimal analyzer-result-shaped repeat item for aggregate_driver_repeats.

    Real callers pass the full ``DriverAcousticResult.to_dict()`` under
    "acoustic"; only ``observed_mic_dbfs`` / ``mic_clipping`` (and the
    optional lane A/B "snr"/"gating" blocks once they land) are read.
    """

    acoustic: dict = {"observed_mic_dbfs": level_dbfs, "mic_clipping": clipping}
    if snr_verdict is not None:
        acoustic["snr"] = {"verdict": snr_verdict}
    if above_validity_floor is not None:
        acoustic["gating"] = {"above_validity_floor": above_validity_floor}
    return {"verdict": verdict, "acoustic": acoustic, "artifact_path": artifact_path}


def test_aggregate_three_accepted_repeats_is_normal_confidence():
    repeats = [_repeat(-30.0), _repeat(-30.3), _repeat(-29.8)]

    result = aggregate_driver_repeats(repeats)

    assert result["target"] == DEFAULT_REPEAT_TARGET == 3
    assert result["accepted"] == 3
    assert result["rejected"] == 0
    assert result["aggregate"] == "median_magnitude"
    assert result["confidence"] == "normal"
    assert result["spread_db_p90"] is not None
    assert result["spread_db_p90"] <= 2.0
    assert result["needed_recapture"] is False
    assert result["recaptured"] is False
    assert len(result["per_repeat"]) == 3
    assert all(entry["accepted"] for entry in result["per_repeat"])
    assert all(entry["reject_reason"] is None for entry in result["per_repeat"])


def test_aggregate_one_outlier_needs_recapture_once():
    # Repeat 2 deviates from the running median (built from repeats 0-1,
    # ~ -30.1) by 9.9 dB, well past REPEAT_OUTLIER_DB (3.0).
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 2
    assert result["rejected"] == 1
    assert result["needed_recapture"] is True
    assert result["recaptured"] is False
    outlier = result["per_repeat"][2]
    assert outlier["accepted"] is False
    assert outlier["reject_reason"] == "level_outlier"


def test_aggregate_bounded_recapture_completes_at_three():
    first_pass = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]
    assert aggregate_driver_repeats(first_pass)["needed_recapture"] is True

    # The caller took the ONE bounded extra attempt and appends it.
    recaptured = [*first_pass, _repeat(-30.1)]

    result = aggregate_driver_repeats(recaptured)

    assert result["accepted"] == 3
    assert result["rejected"] == 1
    assert result["recaptured"] is True
    assert result["needed_recapture"] is False
    assert result["confidence"] == "normal"


def test_aggregate_refusing_recapture_proceeds_with_two_reduced_confidence():
    # Same 3-attempt, 1-rejected, 2-accepted list as the "needs recapture"
    # case — the caller simply stops here instead of trying a 4th time.
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-40.0)]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 2
    assert result["confidence"] == "reduced"


def test_aggregate_rejects_clipping_and_unusable_capture():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, clipping=True),
        _repeat(-29.9, verdict="unusable_capture"),
    ]

    result = aggregate_driver_repeats(repeats)

    reasons = [entry["reject_reason"] for entry in result["per_repeat"]]
    assert reasons == [None, "clipping", "unusable_capture"]
    assert result["accepted"] == 1


def test_aggregate_rejects_on_snr_insufficient_when_lane_b_block_present():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, snr_verdict="insufficient"),
        _repeat(-29.9),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["per_repeat"][1]["reject_reason"] == "snr_insufficient"
    assert result["accepted"] == 2


def test_aggregate_accepts_exact_warn_boundary_reduced_snr():
    repeats = [
        _repeat(-30.0, snr_verdict="reduced"),
        _repeat(-30.1, snr_verdict="reduced"),
        _repeat(-29.9, snr_verdict="reduced"),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 3
    assert all(entry["reject_reason"] is None for entry in result["per_repeat"])


def test_aggregate_rejects_below_validity_floor_when_lane_a_block_present():
    repeats = [
        _repeat(-30.0),
        _repeat(-30.1, above_validity_floor=False),
        _repeat(-29.9),
    ]

    result = aggregate_driver_repeats(repeats)

    assert result["per_repeat"][1]["reject_reason"] == "below_validity_floor"
    assert result["accepted"] == 2


def test_aggregate_woofer_bad_bottom_band_keeps_good_required_overlap():
    repeat = _repeat(-30.0, snr_verdict="insufficient")
    repeat["acoustic"]["overlap_levels"] = [
        {
            "fc_hz": 800.0,
            "usable": True,
            "snr_verdict": "ok",
            "above_validity_floor": True,
        }
    ]
    result = aggregate_driver_repeats([repeat])
    assert result["accepted"] == 1
    assert result["per_repeat"][0]["reject_reason"] is None


def test_aggregate_three_way_mid_accepts_one_of_two_usable_handoffs():
    repeat = _repeat(-30.0, snr_verdict="insufficient")
    repeat["acoustic"]["overlap_levels"] = [
        {
            "fc_hz": 250.0,
            "usable": False,
            "snr_verdict": "insufficient",
            "above_validity_floor": False,
        },
        {
            "fc_hz": 2400.0,
            "usable": True,
            "snr_verdict": "reduced",
            "above_validity_floor": True,
        },
    ]
    result = aggregate_driver_repeats([repeat])
    assert result["accepted"] == 1
    assert result["per_repeat"][0]["above_validity_floor"] is True


def test_aggregate_reads_real_driver_overlap_validity_shape_with_partial_pass():
    below = _repeat(-30.1)
    below["acoustic"]["overlap_levels"] = [
        {"fc_hz": 200.0, "above_validity_floor": False, "usable": False}
    ]
    partial = _repeat(-29.9)
    partial["acoustic"]["overlap_levels"] = [
        {"fc_hz": 200.0, "above_validity_floor": False, "usable": False},
        {"fc_hz": 2000.0, "above_validity_floor": True, "usable": True},
    ]

    result = aggregate_driver_repeats([_repeat(-30.0), below, partial])

    assert result["per_repeat"][1]["reject_reason"] == "no_usable_overlap"
    assert result["per_repeat"][1]["above_validity_floor"] is False
    assert result["per_repeat"][2]["reject_reason"] is None
    assert result["per_repeat"][2]["above_validity_floor"] is True
    assert result["accepted"] == 2


def test_aggregate_absent_snr_and_gating_blocks_do_not_reject_everything():
    """Lane A/B's snr/gating blocks are Slice 0 sibling work that may not
    have landed yet; their absence must degrade to level-outlier-only
    detection, never to rejecting every repeat outright."""

    repeats = [_repeat(-30.0), _repeat(-30.1), _repeat(-29.9)]
    assert "snr" not in repeats[0]["acoustic"]
    assert "gating" not in repeats[0]["acoustic"]

    result = aggregate_driver_repeats(repeats)

    assert result["accepted"] == 3
    assert result["rejected"] == 0


def test_aggregate_empty_repeats_degrades_gracefully():
    result = aggregate_driver_repeats([])

    assert result["accepted"] == 0
    assert result["rejected"] == 0
    assert result["aggregate_repeat"] is None
    assert result["spread_db_p90"] is None
    assert result["confidence"] == "reduced"
    assert result["needed_recapture"] is True
    assert result["recaptured"] is False


def test_aggregate_repeat_group_id_is_unique_per_call():
    repeats = [_repeat(-30.0), _repeat(-30.1), _repeat(-29.9)]

    first = aggregate_driver_repeats(repeats)
    second = aggregate_driver_repeats(repeats)

    assert first["repeat_group_id"] != second["repeat_group_id"]


def test_aggregate_target_is_configurable():
    repeats = [_repeat(-30.0), _repeat(-30.1)]

    result = aggregate_driver_repeats(repeats, target=2)

    assert result["target"] == 2
    assert result["accepted"] == 2
    assert result["needed_recapture"] is False
    assert result["confidence"] == "normal"


# --- Spec-promise guard: outlier rejection, never noise-floor reduction ----


def test_aggregate_spec_promise_no_complex_ir_input_and_no_averaged_curve():
    """aggregate_driver_repeats' only numeric input is a scalar magnitude
    (observed_mic_dbfs) per repeat -- there is no complex/IR parameter
    anywhere in its signature or in the per-repeat shape it reads, and the
    winning repeat's full acoustic block (including any SNR block) is
    reused byte-for-byte, never synthesized from an average across
    repeats."""

    import inspect

    sig = inspect.signature(aggregate_driver_repeats)
    assert "complex" not in str(sig).lower()
    assert "ir" not in {p.lower() for p in sig.parameters}

    repeats = [
        _repeat(-30.0, artifact_path="r0.wav"),
        _repeat(-30.05, artifact_path="r1.wav"),
        _repeat(-29.95, artifact_path="r2.wav"),
    ]
    # Give each a distinguishable SNR reading so an averaged/synthesized
    # value (e.g. the mean of 28/30/32 = 30) would be detectable.
    repeats[0]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 28.0}}
    repeats[1]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 30.0}}
    repeats[2]["acoustic"]["snr"] = {"worst_relevant": {"estimated_snr_db": 32.0}}

    result = aggregate_driver_repeats(repeats)

    winner = result["aggregate_repeat"]
    assert winner is not None
    # The winner is EXACTLY one of the input repeats (object equality on the
    # nested acoustic dict), not a new dict with blended/averaged fields.
    assert winner["acoustic"] in (
        repeats[0]["acoustic"],
        repeats[1]["acoustic"],
        repeats[2]["acoustic"],
    )
    winning_snr = winner["acoustic"]["snr"]["worst_relevant"]["estimated_snr_db"]
    assert winning_snr in (28.0, 30.0, 32.0)  # one repeat's real value
    assert winning_snr != sum([28.0, 30.0, 32.0]) / 3  # never the average


def test_record_driver_repeat_aggregate_emits_lifecycle_event(caplog):
    repeats = [_repeat(-30.0), _repeat(-30.2), _repeat(-29.8)]

    with caplog.at_level(logging.INFO):
        result = record_driver_repeat_aggregate(
            speaker_group_id="mono",
            role="woofer",
            repeats=repeats,
            session_id="sess-1",
        )

    assert result["accepted"] == 3
    assert "event=correction.crossover_repeats_aggregated" in caplog.text
    assert "session=sess-1" in caplog.text
    assert "group=mono" in caplog.text
    assert "role=woofer" in caplog.text
    assert "accepted=3" in caplog.text
    assert "rejected=0" in caplog.text


def test_repeat_outlier_threshold_constant_is_positive_and_documented():
    assert REPEAT_OUTLIER_DB > 0


# --- lifecycle events (lane E, docs/active-crossover-information-design.md
# "Structured events") -------------------------------------------------------

_LOGGER_NAME = "jasper.active_speaker.commissioning_capture"


def _events(caplog, name: str) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.getMessage().startswith(f"event={name}")
    ]


class _FakeAcousticWithSnrAndGating:
    """Duck-types DriverAcousticResult with a snr/gating block (SC-1/SC-2).

    Those blocks are lane B's (snr) and lane A's (gating) — not shipped yet on
    this branch — so this fake proves the capture-event wiring extracts them
    correctly once they exist, without depending on either lane's code.
    """

    verdict = "present"
    mic_clipping = False
    observed_mic_dbfs = -32.0

    def to_dict(self):
        return {
            "kind": "jts_active_speaker_driver_acoustics",
            "verdict": "present",
            "mic_clipping": False,
            "snr": {
                "schema_version": 1,
                "decision_class": "magnitude",
                "worst_relevant": {
                    "band_id": "mid",
                    "estimated_snr_db": 27.5,
                    "verdict": "ok",
                },
                "verdict": "ok",
            },
            "gating": {
                "schema_version": 1,
                "applied": True,
                "f_valid_floor_hz": 240.0,
            },
        }


def test_driver_capture_accepted_emits_exactly_one_lifecycle_event(
    tmp_path: Path, caplog,
):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out, _ = _capture_driver(tmp_path, _driver_result("present", present=True))
    assert out["recorded"] is True
    accepted = _events(caplog, "correction.crossover_capture_accepted")
    assert len(accepted) == 1
    assert "group=mono" in accepted[0]
    assert "role=woofer" in accepted[0]
    assert "verdict=present" in accepted[0]
    assert "outcome=heard_correct_driver" in accepted[0]
    assert _events(caplog, "correction.crossover_capture_rejected") == []


def test_driver_capture_accepted_surfaces_snr_and_floor_when_present(
    tmp_path: Path, caplog,
):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_driver_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            role="woofer",
            captured_wav=tmp_path / "cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            playback_id="pb1",
            safe_session=_safe_session(role="woofer", output_index=0, playback_id="pb1"),
            state_path=tmp_path / "measurements.json",
            analyze=lambda *a, **k: _FakeAcousticWithSnrAndGating(),
        )
    assert out["recorded"] is True
    accepted = _events(caplog, "correction.crossover_capture_accepted")
    assert len(accepted) == 1
    assert "snr_db=27.5" in accepted[0]
    assert "floor_hz=240.0" in accepted[0]


def test_driver_capture_accepted_includes_session_when_bundle_known(
    tmp_path: Path, caplog,
):
    # Real persisted shape: the bundle session belongs to the active comparison
    # set, not a fabricated top-level measurement-state key.
    def spy_record(*_args, **_kwargs):
        return {
            "driver_measurements": [],
            "active_comparison_set": {"bundle_session_id": "sess-abc123"},
        }

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_driver_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            role="woofer",
            captured_wav=tmp_path / "cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            analyze=lambda *a, **k: _driver_result("present", present=True),
            record=spy_record,
        )
    assert out["recorded"] is True
    accepted = _events(caplog, "correction.crossover_capture_accepted")
    assert len(accepted) == 1
    assert "session=sess-abc123" in accepted[0]


def test_driver_capture_unusable_emits_exactly_one_rejected_event(
    tmp_path: Path, caplog,
):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_driver_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            role="woofer",
            captured_wav=tmp_path / "cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            analyze=lambda *a, **k: _driver_result("unusable_capture"),
            record=lambda *a, **k: pytest.fail("record must not run"),
        )
    assert out["recorded"] is False
    rejected = _events(caplog, "correction.crossover_capture_rejected")
    assert len(rejected) == 1
    assert "reason=unusable_capture" in rejected[0]
    assert "group=mono" in rejected[0]
    assert "role=woofer" in rejected[0]
    assert _events(caplog, "correction.crossover_capture_accepted") == []


def test_summed_capture_accepted_emits_exactly_one_lifecycle_event(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_summed_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            captured_wav="cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            summed_test_id="st1",
            analyze=lambda *a, **k: _summed_result("blend_ok"),
            record=lambda topology, raw, **kw: {"summed_validations": [dict(raw)]},
        )
    assert out["recorded"] is True
    accepted = _events(caplog, "correction.crossover_capture_accepted")
    assert len(accepted) == 1
    assert "group=mono" in accepted[0]
    assert "verdict=blend_ok" in accepted[0]
    assert "outcome=blend_ok" in accepted[0]
    # Summed captures have no per-driver role.
    assert "role=" not in accepted[0]


def test_summed_capture_unusable_emits_exactly_one_rejected_event(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_summed_acoustic_capture(
            _topology(),
            _two_way(),
            speaker_group_id="mono",
            captured_wav="cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            analyze=lambda *a, **k: _summed_result("unusable_capture"),
            record=lambda *a, **k: pytest.fail("record must not run"),
        )
    assert out["recorded"] is False
    rejected = _events(caplog, "correction.crossover_capture_rejected")
    assert len(rejected) == 1
    assert "reason=unusable_capture" in rejected[0]
    assert _events(caplog, "correction.crossover_capture_accepted") == []


def test_summed_capture_no_crossover_region_emits_exactly_one_rejected_event(caplog):
    preset = _NoCrossoverPreset()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        out = record_summed_acoustic_capture(
            _topology(),
            preset,
            speaker_group_id="mono",
            captured_wav="cap.wav",
            sweep_meta={"sample_rate": 48000, "n_samples": 4096},
            analyze=lambda *a, **k: pytest.fail("analyze must not run"),
            record=lambda *a, **k: pytest.fail("record must not run"),
        )
    assert out["recorded"] is False
    rejected = _events(caplog, "correction.crossover_capture_rejected")
    assert len(rejected) == 1
    assert "reason=no_crossover_region" in rejected[0]
    # No verdict at all for this early-out path.
    assert "verdict=" not in rejected[0]


def test_reserved_crossover_events_are_never_emitted():
    # Spec-pinned (docs/active-crossover-information-design.md "Structured
    # events"): correction.crossover_proposal_ready / _verification_passed /
    # _verification_failed / _level_locked / _level_failed are documented as
    # future work and MUST NOT have a call site yet. A static grep over the
    # source tree is the guard: no jasper/ file may pass one of these literal
    # strings to log_event.
    root = Path(__file__).resolve().parents[1] / "jasper"
    assert set(RESERVED_CROSSOVER_EVENTS) == {
        "correction.crossover_proposal_ready",
        "correction.crossover_verification_passed",
        "correction.crossover_verification_failed",
        "correction.crossover_level_locked",
        "correction.crossover_level_failed",
    }
    for name in RESERVED_CROSSOVER_EVENTS:
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # Ignore the reserved-names tuple's own declaration.
            if path.name == "commissioning_capture.py":
                text = text.replace(f'"{name}"', "", 1)
            if f'"{name}"' in text or f"'{name}'" in text:
                offenders.append(str(path.relative_to(root.parent)))
        assert offenders == [], f"{name} has a call site: {offenders}"


# --- Paired summed evidence + multi-region proposals (lane E, Slice 2) ------


def _capture_summed(
    tmp_path: Path,
    preset: ActiveSpeakerPreset,
    result: SummedAcousticResult,
    *,
    topology: OutputTopology | None = None,
    crossover_fc_hz: float | None = None,
    state_path: Path | None = None,
    now: str | None = None,
    wav_name: str = "cap.wav",
    placement_proof: dict | None = None,
    start_new_comparison_set: bool = False,
) -> dict:
    """Run the REAL record_summed_acoustic_capture -> record_summed_validation
    wire (region stamping included), unlike most tests above which spy on
    ``record``."""
    from jasper.active_speaker.capture_geometry import (
        SUMMED_PLACEMENT_POLICY_ID,
        normalized_placement_proof,
    )

    topology = topology or _topology()
    state_path = state_path or (tmp_path / "measurements.json")
    state = load_measurement_state(topology, state_path=state_path)
    comparison_set = state.get("active_comparison_set")
    if start_new_comparison_set or not isinstance(comparison_set, dict):
        comparison_set = start_active_comparison_set(
            topology,
            profile_context_id="protected-profile",
            setup_sha256="a" * 64,
            device_sha256="b" * 64,
            calibration_id="test-calibration",
            driver_level_locks={
                target["target_id"]: {
                    "target_id": target["target_id"],
                    "speaker_group_id": target["speaker_group_id"],
                    "role": target["role"],
                    "tone_frequency_hz": (
                        250.0 if target["role"] == "woofer" else 6250.0
                    ),
                    "tone_peak_dbfs": -12.0,
                    "commissioning_gain_db": 0.0,
                    "locked_main_volume_db": -12.0,
                }
                for target in active_driver_targets(topology)
            },
            state_path=state_path,
            now=now or "2026-07-11T11:59:00Z",
        )
    state = load_measurement_state(topology, state_path=state_path)
    playback_id = f"summed-playback-{len(state.get('summed_tests') or []) + 1}"
    output_indices = sorted({
        int(channel.physical_output_index)
        for group in topology.speaker_groups
        if group.id == "mono"
        for channel in group.channels
        if channel.physical_output_index is not None
    })
    record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": "mono",
            "playback": {
                "status": "completed",
                "backend": "aplay",
                "playback_id": playback_id,
                "audio_emitted": True,
                "artifact": {
                    "wav_basename": "summed.wav",
                    "metadata_basename": "summed.json",
                    "target_output_indices": output_indices,
                    "channel_count": len(output_indices),
                },
            },
        },
        state_path=state_path,
        now=now,
    )
    if placement_proof is None:
        group_target = next(
            target
            for target in active_summed_targets(topology)
            if target["speaker_group_id"] == "mono"
        )
        placement_proof = normalized_placement_proof(
            policy_id=SUMMED_PLACEMENT_POLICY_ID,
            acknowledgement_binding=f"binding-{playback_id}-abcdefghijkl",
            relay_session_id=f"relay-{playback_id}",
            capture_page={
                "capture_protocol_version": 2,
                "capture_page_build": "20260712.2",
            },
            speaker_group_id="mono",
            role="summed",
            target_fingerprint=group_target["group_fingerprint"],
            comparison_set=comparison_set,
        )
    return record_summed_acoustic_capture(
        topology,
        preset,
        speaker_group_id="mono",
        captured_wav=tmp_path / wav_name,
        sweep_meta={
            "sample_rate": 48000,
            "n_samples": 4096,
            "amplitude_dbfs": -12.0,
        },
        summed_test_id=playback_id,
        crossover_fc_hz=crossover_fc_hz,
        expect_null=result.expect_null,
        excitation=_alignment_excitation(preset, topology),
        placement_proof=placement_proof,
        analyze=lambda *a, **k: result,
        record=lambda topology, raw, **kwargs: record_summed_validation(
            topology,
            raw,
            driver_target_proof_complete=True,
            **kwargs,
        ),
        state_path=state_path,
        now=now,
    )


def test_record_summed_acoustic_capture_stamps_region_matching_analyzed_fc(
    tmp_path: Path,
) -> None:
    out = _capture_summed(
        tmp_path, _two_way(), _summed_result("blend_ok"),
    )
    record = out["measurement"]["summed_validations"][-1]
    assert record["region"] == {
        "lower_role": "woofer",
        "upper_role": "tweeter",
        "fc_hz": 1600.0,
    }


def test_record_summed_acoustic_capture_stamps_region_per_three_way_crossover(
    tmp_path: Path,
) -> None:
    preset = _three_way()
    topology = _three_way_topology()

    lower_out = _capture_summed(
        tmp_path,
        preset,
        _summed_result("blend_ok", observed=-34.0),
        topology=topology,
        crossover_fc_hz=350.0,
        wav_name="lower.wav",
    )
    lower_record = lower_out["measurement"]["summed_validations"][-1]
    assert lower_record["region"] == {
        "lower_role": "woofer", "upper_role": "mid", "fc_hz": 350.0,
    }

    upper_out = _capture_summed(
        tmp_path,
        preset,
        _summed_result("blend_ok", observed=-34.0),
        topology=topology,
        crossover_fc_hz=2500.0,
        wav_name="upper.wav",
    )
    upper_record = upper_out["measurement"]["summed_validations"][-1]
    assert upper_record["region"] == {
        "lower_role": "mid", "upper_role": "tweeter", "fc_hz": 2500.0,
    }


def test_record_summed_acoustic_capture_unresolvable_fc_stamps_region_none(
    tmp_path: Path,
) -> None:
    # A caller-supplied fc that matches no preset region: region_for_fc has
    # nothing to resolve, so the persisted record stamps region=None rather
    # than guessing.
    out = _capture_summed(
        tmp_path,
        _two_way(),
        _summed_result("blend_ok"),
        crossover_fc_hz=999.0,
    )
    record = out["measurement"]["summed_validations"][-1]
    assert record["region"] is None
    assert region_for_fc(_two_way(), 999.0) is None


def test_build_proposal_three_way_returns_two_region_proposals(
    tmp_path: Path,
) -> None:
    """Spec: 'Support every crossover region in a three-way system.' Paired
    evidence for BOTH regions of a 3-way yields two independent proposals,
    each with its own region roles/fc and its own margin; the backward-
    compatible top-level 'proposal' equals the lowest-fc region's dict."""
    preset = _three_way()
    topology = _three_way_topology()
    state_path = tmp_path / "measurements.json"

    # Region 1 (woofer/mid, fc=350): strong keep evidence (reverse null much
    # deeper than in-phase).
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=2.0, crossover_fc_hz=350.0,
            observed_mic_dbfs=-34.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=False, calibrated=True,
        ),
        topology=topology, crossover_fc_hz=350.0, state_path=state_path,
        wav_name="r1_inphase.wav", now="2026-07-11T12:00:00Z",
    )
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=28.0, crossover_fc_hz=350.0,
            observed_mic_dbfs=-50.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -50.0},
            expect_null=True, calibrated=True,
        ),
        topology=topology, crossover_fc_hz=350.0, state_path=state_path,
        wav_name="r1_reverse.wav", now="2026-07-11T12:01:00Z",
    )

    # Region 2 (mid/tweeter, fc=2500): strong invert evidence (in-phase null
    # much deeper than reverse).
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="polarity_or_delay_problem", null_depth_db=30.0,
            crossover_fc_hz=2500.0, observed_mic_dbfs=-50.0,
            mic_clipping=False, quality={"failed": False, "rms_dbfs": -50.0},
            expect_null=False, calibrated=True,
        ),
        topology=topology, crossover_fc_hz=2500.0, state_path=state_path,
        wav_name="r2_inphase.wav", now="2026-07-11T12:02:00Z",
    )
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="polarity_or_delay_problem", null_depth_db=3.0,
            crossover_fc_hz=2500.0, observed_mic_dbfs=-34.0,
            mic_clipping=False, quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=True, calibrated=True,
        ),
        topology=topology, crossover_fc_hz=2500.0, state_path=state_path,
        wav_name="r2_reverse.wav", now="2026-07-11T12:03:00Z",
    )

    measurements = load_measurement_state(topology, state_path=state_path)
    out = build_crossover_alignment_proposal(
        preset, measurements, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    assert out["status"] == "ok"
    assert len(out["proposals"]) == 2

    region1 = out["proposals"][0]
    assert region1["region"] == {
        "lower_role": "woofer", "upper_role": "mid", "fc_hz": 350.0,
    }
    proposal1 = region1["proposal"]
    assert proposal1["polarity_action"] == ca.POLARITY_KEEP
    assert proposal1["polarity_margin_db"] == pytest.approx(28.0 - 2.0)

    region2 = out["proposals"][1]
    assert region2["region"] == {
        "lower_role": "mid", "upper_role": "tweeter", "fc_hz": 2500.0,
    }
    proposal2 = region2["proposal"]
    assert proposal2["polarity_action"] == ca.POLARITY_INVERT
    assert proposal2["polarity"] == "invert_tweeter"
    assert proposal2["polarity_margin_db"] == pytest.approx(3.0 - 30.0)

    # Backward compat: the flat top-level fields mirror the LOWEST region.
    assert out["proposal"] == proposal1
    assert out["mode"]["mode"] == ca.PHASE_AWARE


def test_build_proposal_per_region_calibration_gate_is_independent(
    tmp_path: Path,
) -> None:
    """An uncalibrated capture in region 2 downgrades ONLY region 2's
    proposal to magnitude_only/unauthorized; region 1 (fully calibrated)
    stays phase_aware."""
    preset = _three_way()
    topology = _three_way_topology()
    state_path = tmp_path / "measurements.json"

    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=2.0, crossover_fc_hz=350.0,
            observed_mic_dbfs=-34.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=False, calibrated=True,
        ),
        topology=topology, crossover_fc_hz=350.0, state_path=state_path,
        wav_name="r1.wav", now="2026-07-11T12:00:00Z",
    )
    # Region 2's summed capture is UNCALIBRATED (a phone, not a calibrated
    # measurement mic).
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=2.0, crossover_fc_hz=2500.0,
            observed_mic_dbfs=-34.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=False, calibrated=False,
        ),
        topology=topology, crossover_fc_hz=2500.0, state_path=state_path,
        wav_name="r2.wav", now="2026-07-11T12:01:00Z",
    )

    measurements = load_measurement_state(topology, state_path=state_path)
    out = build_crossover_alignment_proposal(
        preset, measurements, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    proposal1 = out["proposals"][0]["proposal"]
    proposal2 = out["proposals"][1]["proposal"]

    assert proposal1["mode"] == ca.PHASE_AWARE
    assert proposal1["authorized"] is True

    assert proposal2["mode"] == ca.MAGNITUDE_ONLY
    assert proposal2["authorized"] is False


def test_build_proposal_reaches_both_captures_margin_through_persisted_pairs(
    tmp_path: Path,
) -> None:
    """Paired persistence guard (spec: 'Retain both normal- and reverse-
    polarity summed evidence per crossover region'). Recording an in-phase
    then a reverse summed capture for one region leaves BOTH readable in
    latest_summed_pairs_by_group, and the proposal computes polarity from the
    reverse-vs-in-phase margin -- the shipped proposer's both-captures
    branch, now reachable end-to-end through persisted state rather than a
    hand-built dict."""
    preset = _two_way()
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=2.0, crossover_fc_hz=1600.0,
            observed_mic_dbfs=-34.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=False, calibrated=True,
        ),
        topology=topology, state_path=state_path,
        wav_name="inphase.wav", now="2026-07-11T12:00:00Z",
    )
    _capture_summed(
        tmp_path, preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=18.0, crossover_fc_hz=1600.0,
            observed_mic_dbfs=-50.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -50.0},
            expect_null=True, calibrated=True,
        ),
        topology=topology, state_path=state_path,
        wav_name="reverse.wav", now="2026-07-11T12:01:00Z",
    )

    measurements = load_measurement_state(topology, state_path=state_path)
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["in_phase"]["acoustic"]["expect_null"] is False
    assert pair["reverse"]["acoustic"]["expect_null"] is True

    out = build_crossover_alignment_proposal(
        preset, measurements, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    proposal = out["proposal"]
    assert proposal["in_phase_null_depth_db"] == 2.0
    assert proposal["reverse_null_depth_db"] == 18.0
    assert proposal["polarity_margin_db"] == pytest.approx(18.0 - 2.0)
    assert proposal["polarity_action"] == ca.POLARITY_KEEP


def test_build_proposal_never_combines_polarities_from_different_runs(
    tmp_path: Path,
) -> None:
    """Consumer-level guard for the Lane-E review finding.

    A reverse capture from a new fixed-position run must stay a reverse-only
    proposal. It may not borrow the prior run's in-phase capture and invent a
    cross-run null margin.
    """
    preset = _two_way()
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    _capture_summed(
        tmp_path,
        preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=2.0, crossover_fc_hz=1600.0,
            observed_mic_dbfs=-34.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=False, calibrated=True,
        ),
        topology=topology,
        state_path=state_path,
        wav_name="run_a_inphase.wav",
        now="2026-07-11T12:00:00Z",
    )
    _capture_summed(
        tmp_path,
        preset,
        SummedAcousticResult(
            verdict="blend_ok", null_depth_db=24.0, crossover_fc_hz=1600.0,
            observed_mic_dbfs=-50.0, mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -50.0},
            expect_null=True, calibrated=True,
        ),
        topology=topology,
        state_path=state_path,
        start_new_comparison_set=True,
        wav_name="run_b_reverse.wav",
        now="2026-07-11T12:01:00Z",
    )

    measurements = load_measurement_state(topology, state_path=state_path)
    out = build_crossover_alignment_proposal(
        preset, measurements, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    proposal = out["proposal"]
    assert proposal["in_phase_null_depth_db"] is None
    assert proposal["reverse_null_depth_db"] == 24.0
    assert proposal["polarity_margin_db"] is None


@pytest.mark.parametrize("malformed_expect_null", [False, True])
def test_build_proposal_never_falls_back_around_malformed_modern_proof(
    tmp_path: Path,
    malformed_expect_null: bool,
) -> None:
    """An authoritative invalid region blocks the 2-way legacy fallback."""
    preset = _two_way()
    topology = _topology()
    state_path = tmp_path / "measurements.json"

    for expect_null, depth, created_at in (
        (False, 2.0, "2026-07-11T12:00:00Z"),
        (True, 24.0, "2026-07-11T12:01:00Z"),
    ):
        _capture_summed(
            tmp_path,
            preset,
            SummedAcousticResult(
                verdict="blend_ok",
                null_depth_db=depth,
                crossover_fc_hz=1600.0,
                observed_mic_dbfs=-34.0,
                mic_clipping=False,
                quality={"failed": False, "rms_dbfs": -34.0},
                expect_null=expect_null,
                calibrated=True,
            ),
            topology=topology,
            state_path=state_path,
            wav_name=f"legacy_{expect_null}.wav",
            now=created_at,
        )

    _capture_summed(
        tmp_path,
        preset,
        SummedAcousticResult(
            verdict="blend_ok",
            null_depth_db=7.0,
            crossover_fc_hz=1600.0,
            observed_mic_dbfs=-34.0,
            mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -34.0},
            expect_null=malformed_expect_null,
            calibrated=True,
        ),
        topology=topology,
        state_path=state_path,
        placement_proof={"comparison_set_id": "bad"},
        wav_name="malformed.wav",
        now="2026-07-11T12:02:00Z",
    )

    measurements = load_measurement_state(topology, state_path=state_path)
    out = build_crossover_alignment_proposal(
        preset, measurements, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    proposal = out["proposal"]
    assert proposal["in_phase_null_depth_db"] is None
    assert proposal["reverse_null_depth_db"] is None
    assert proposal["polarity_margin_db"] is None


def _valid_alignment_pair(tmp_path: Path) -> tuple[ActiveSpeakerPreset, dict]:
    preset = _two_way()
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    for expect_null, depth, created_at in (
        (False, 2.0, "2026-07-12T12:00:00Z"),
        (True, 24.0, "2026-07-12T12:01:00Z"),
    ):
        _capture_summed(
            tmp_path,
            preset,
            SummedAcousticResult(
                verdict="blend_ok",
                null_depth_db=depth,
                crossover_fc_hz=1600.0,
                observed_mic_dbfs=-34.0,
                mic_clipping=False,
                quality={"failed": False, "rms_dbfs": -34.0},
                expect_null=expect_null,
                calibrated=True,
            ),
            topology=topology,
            state_path=state_path,
            wav_name=f"valid_{expect_null}.wav",
            now=created_at,
        )
    return preset, load_measurement_state(topology, state_path=state_path)


@pytest.mark.parametrize(
    "corruption",
    (
        "blocker",
        "incomplete_proof",
        "stale_comparison_set",
        "polarity_slot",
        "acoustic_fc",
        "too_loud",
        "missing_meter_status",
        "unknown_meter_status",
        "missing_excitation",
        "excitation_graph_mismatch",
        "malformed_clipping",
        "missing_applied_graph",
    ),
)
def test_build_proposal_rejects_unadmitted_summed_decision_evidence(
    tmp_path: Path,
    corruption: str,
) -> None:
    """Regression: a valid-looking null never bypasses the record/run gates."""
    import copy

    from jasper.active_speaker.capture_geometry import comparison_set_fingerprint

    preset, measurements = _valid_alignment_pair(tmp_path)
    measurements = copy.deepcopy(measurements)
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    if corruption == "blocker":
        for record in pair.values():
            record["validated"] = False
            record["issues"].append({
                "severity": "blocker",
                "code": "summed_validation_test_missing",
                "message": "no proven playback",
            })
    elif corruption == "incomplete_proof":
        for record in pair.values():
            record["placement_proof"] = {"comparison_set_id": "a" * 32}
    elif corruption == "polarity_slot":
        pair["reverse"]["acoustic"]["expect_null"] = False
    elif corruption == "acoustic_fc":
        for record in pair.values():
            record["acoustic"]["crossover_fc_hz"] = 1200.0
    elif corruption == "too_loud":
        for record in pair.values():
            record["mic_meter"]["status"] = "too_loud"
    elif corruption == "missing_meter_status":
        for record in pair.values():
            record["mic_meter"] = {}
    elif corruption == "unknown_meter_status":
        for record in pair.values():
            record["mic_meter"]["status"] = "mystery"
    elif corruption == "missing_excitation":
        for record in pair.values():
            record["excitation"] = None
    elif corruption == "excitation_graph_mismatch":
        for record in pair.values():
            record["excitation"]["corrections"]["tweeter"]["gain_db"] = -1.0
    elif corruption == "malformed_clipping":
        for record in pair.values():
            record["mic_clipping"] = "yes"
            record["acoustic"]["mic_clipping"] = "yes"
    elif corruption == "missing_applied_graph":
        pass
    else:
        current = dict(measurements["active_comparison_set"])
        current["comparison_set_id"] = "f" * 32
        current["fingerprint"] = comparison_set_fingerprint(current)
        measurements["active_comparison_set"] = current

    proposal_kwargs = (
        {"expected_applied_profile": None}
        if corruption == "missing_applied_graph"
        else {}
    )
    out = build_crossover_alignment_proposal(
        preset,
        measurements,
        requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
        **proposal_kwargs,
    )
    if corruption == "polarity_slot":
        # The still-valid in-phase half remains visible as tentative evidence,
        # but the malformed reverse slot cannot form a paired margin.
        assert out["proposal"]["in_phase_null_depth_db"] == 2.0
        assert out["proposal"]["reverse_null_depth_db"] is None
        assert out["proposal"]["polarity_margin_db"] is None
    else:
        assert out["proposal"]["authorized"] is False
        assert out["proposal"]["in_phase_null_depth_db"] is None
        assert out["proposal"]["reverse_null_depth_db"] is None
    assert {
        issue["code"] for issue in out["proposal"]["issues"]
    } >= {"summed_decision_evidence_rejected"}
    if corruption == "polarity_slot":
        assert out["proposals"][0]["evidence"]["reverse"]["reason"] == (
            "summed_evidence_polarity_slot_mismatch"
        )
    elif corruption == "acoustic_fc":
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_acoustic_fc_mismatch"
        )
    elif corruption in {"too_loud", "missing_meter_status", "unknown_meter_status"}:
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_mic_level_invalid"
        )
    elif corruption == "missing_excitation":
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_excitation_missing"
        )
    elif corruption == "excitation_graph_mismatch":
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_excitation_graph_mismatch"
        )
    elif corruption == "malformed_clipping":
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_clipped"
        )
    elif corruption == "missing_applied_graph":
        assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
            "summed_evidence_applied_graph_missing"
        )


def test_build_proposal_rejects_another_profile_context(tmp_path: Path) -> None:
    """A setting change invalidates otherwise-current acoustic evidence."""
    preset, measurements = _valid_alignment_pair(tmp_path)

    out = build_crossover_alignment_proposal(
        preset,
        measurements,
        requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="new-protected-profile",
    )

    assert out["proposal"]["authorized"] is False
    assert out["proposal"]["in_phase_null_depth_db"] is None
    assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
        "summed_evidence_profile_context_stale"
    )


def test_build_proposal_rejects_pair_measured_at_another_fc(tmp_path: Path) -> None:
    """Role identity alone cannot relabel a 1200 Hz null as a 1600 Hz result."""
    import copy

    preset, measurements = _valid_alignment_pair(tmp_path)
    measurements = copy.deepcopy(measurements)
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    for record in pair.values():
        record["region"]["fc_hz"] = 1200.0

    out = build_crossover_alignment_proposal(
        preset,
        measurements,
        requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    assert out["proposal"]["authorized"] is False
    assert out["proposal"]["in_phase_null_depth_db"] is None
    assert out["proposals"][0]["evidence"]["in_phase"]["reason"] == (
        "summed_evidence_region_mismatch"
    )


def test_build_proposal_rejects_old_listening_position_policy(
    tmp_path: Path,
) -> None:
    """Pre-reference-axis summed captures remain visible history only."""
    import copy

    preset, measurements = _valid_alignment_pair(tmp_path)
    measurements = copy.deepcopy(measurements)
    pair = measurements["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    for record in pair.values():
        record["placement_proof"]["policy_id"] = "summed_listening_position_v1"

    out = build_crossover_alignment_proposal(
        preset,
        measurements,
        requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    assert out["proposal"]["authorized"] is False
    assert out["proposals"][0]["evidence"]["reverse"]["reason"] == (
        "summed_evidence_comparison_or_placement_invalid"
    )


def test_max_summed_records_eviction_degrades_to_single_capture_fallback(
    tmp_path: Path,
) -> None:
    """MAX_SUMMED_RECORDS headroom: a 3-way needs 2 regions x 2 kinds = 4 live
    slots; MAX_SUMMED_RECORDS is far above that, so no bump is needed. This
    pins the degrade-gracefully behavior when the ring evicts one side of a
    pair anyway (many repeat captures over a long session): the proposer
    falls back to its existing single-capture path rather than raising or
    mispairing a stale evicted record back in."""
    preset = _two_way()
    topology = _topology()
    state_path = tmp_path / "measurements.json"
    # The reverse capture that will fall off the ring.
    _capture_summed(
        tmp_path,
        preset,
        SummedAcousticResult(
            verdict="blend_ok",
            null_depth_db=22.0,
            crossover_fc_hz=1600.0,
            observed_mic_dbfs=-50.0,
            mic_clipping=False,
            quality={"failed": False, "rms_dbfs": -50.0},
            expect_null=True,
            calibrated=True,
        ),
        topology=topology,
        state_path=state_path,
        wav_name="evicted_reverse.wav",
        now="2026-07-11T12:00:00Z",
    )

    # MAX_SUMMED_RECORDS more in-phase fillers push the reverse capture above
    # out of the retained [-MAX_SUMMED_RECORDS:] window.
    state = {}
    for i in range(MAX_SUMMED_RECORDS):
        out = _capture_summed(
            tmp_path,
            preset,
            SummedAcousticResult(
                verdict="blend_ok",
                null_depth_db=2.0,
                crossover_fc_hz=1600.0,
                observed_mic_dbfs=-34.0,
                mic_clipping=False,
                quality={"failed": False, "rms_dbfs": -34.0},
                expect_null=False,
                calibrated=True,
            ),
            topology=topology,
            state_path=state_path,
            wav_name=f"filler_{i}.wav",
            now=f"2026-07-11T13:{i:02d}:00Z",
        )
        state = out["measurement"]

    assert len(state["summed_validations"]) == MAX_SUMMED_RECORDS
    pair = state["latest_summed_pairs_by_group"]["mono"]["woofer:tweeter"]
    assert pair["reverse"] is None
    assert pair["in_phase"] is not None

    out = build_crossover_alignment_proposal(
        preset, state, requested_mode=ca.PHASE_AWARE,
        expected_profile_context_id="protected-profile",
    )
    assert out["status"] == "ok"
    proposal = out["proposal"]
    # Single-capture (in-phase-only) fallback -- not a raised error, and not
    # a margin computed against the evicted reverse record.
    assert proposal["reverse_null_depth_db"] is None
    assert proposal["in_phase_null_depth_db"] == 2.0
    assert proposal["polarity_action"] == ca.POLARITY_KEEP


# --- _summed_alignment_snr — conservative pair combination -------------------


def test_summed_alignment_snr_pair_both_none_is_unknown():
    assert _summed_alignment_snr(None, None) == (None, False)


def test_summed_alignment_snr_pair_one_ok_one_no_evidence_is_unknown():
    ok_record = {
        "acoustic": {
            "snr": {"worst_relevant": {"verdict": "ok"}, "verdict": "ok"},
        },
    }
    assert _summed_alignment_snr(ok_record, None) == (None, False)
    assert _summed_alignment_snr(None, ok_record) == (None, False)
    assert _summed_alignment_snr(ok_record, ok_record) == (True, False)


def test_summed_alignment_snr_pair_either_insufficient_is_false():
    ok_record = {
        "acoustic": {
            "snr": {"worst_relevant": {"verdict": "ok"}, "verdict": "ok"},
        },
    }
    bad_record = {
        "acoustic": {
            "snr": {
                "worst_relevant": {"verdict": "insufficient"},
                "verdict": "insufficient",
            },
        },
    }
    assert _summed_alignment_snr(ok_record, bad_record) == (False, False)
    assert _summed_alignment_snr(bad_record, ok_record) == (False, False)


def test_summed_alignment_snr_pair_either_capped_is_capped():
    ok_record = {
        "acoustic": {
            "snr": {"worst_relevant": {"verdict": "ok"}, "verdict": "ok"},
            "null_depth_capped": False,
        },
    }
    capped_record = {
        "acoustic": {
            "snr": {"worst_relevant": {"verdict": "ok"}, "verdict": "ok"},
            "null_depth_capped": True,
        },
    }
    _, capped = _summed_alignment_snr(ok_record, capped_record)
    assert capped is True
    _, capped = _summed_alignment_snr(capped_record, ok_record)
    assert capped is True
