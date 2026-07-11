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

from pathlib import Path

import pytest

from jasper.active_speaker.commissioning_capture import (
    DRIVER_VERDICT_TO_OUTCOME,
    SUMMED_VERDICT_TO_OUTCOME,
    driver_passband_hz,
    primary_crossover_fc_hz,
    record_driver_acoustic_capture,
    record_summed_acoustic_capture,
)
from jasper.active_speaker.driver_acoustics import (
    DRIVER_VERDICTS,
    SUMMED_VERDICTS,
    DriverAcousticResult,
    SummedAcousticResult,
)
from jasper.active_speaker.measurement import record_summed_validation
from jasper.active_speaker.profile import ActiveSpeakerPreset

# Canonical fixtures reused across the active-speaker suite.
from tests.test_active_speaker_measurement import _safe_session, _topology
from tests.test_active_speaker_profile import _three_way_preset, _two_way_preset


def _two_way() -> ActiveSpeakerPreset:
    # Mono 2-way matching _topology(): woofer=output 0, tweeter=output 1,
    # crossover at 1600 Hz.
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _three_way() -> ActiveSpeakerPreset:
    # crossovers at 350 (woofer/mid) and 2500 (mid/tweeter).
    return ActiveSpeakerPreset.from_mapping(_three_way_preset())


def _driver_result(
    verdict: str,
    *,
    present: bool = False,
    observed: float = -32.0,
    clipping: bool = False,
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
    )


def _summed_result(verdict: str, *, observed: float = -34.0) -> SummedAcousticResult:
    return SummedAcousticResult(
        verdict=verdict,
        null_depth_db=2.0 if verdict == "blend_ok" else 12.0,
        crossover_fc_hz=1600.0,
        observed_mic_dbfs=observed,
        mic_clipping=False,
        quality={"failed": False, "rms_dbfs": observed},
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
):
    seen: dict = {}

    def fake_analyze(
        wav, meta, *, passband_hz, overlap_fcs=(), has_mic_calibration,
        calibration=None,
    ):
        seen["passband_hz"] = passband_hz
        seen["overlap_fcs"] = tuple(overlap_fcs)
        seen["wav"] = wav
        seen["calibration"] = calibration
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
        analyze=fake_analyze,
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
