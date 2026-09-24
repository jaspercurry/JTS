# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A take keeps the impulses its analysis measured, and readers get them back."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from jasper.active_speaker.crossover_v2.capture_prediction import read_diagnostic
from jasper.active_speaker.crossover_v2.gate_sweep import sweep_round
from jasper.active_speaker.crossover_v2.round_captures import RoundCapturesRefused, select_capture
from jasper.active_speaker.crossover_v2.take_impulses import (
    IMPULSES_KIND, TakeImpulsesUnreadable, analysis_impulses, impulse_for, take_impulses,
    write_take_impulses,
)
from jasper.audio_measurement.bundles import read_artifact_manifest
from jasper.audio_measurement.program import DEFAULT_VERIFY_TAIL_S, build_measure_program, build_verify_program
from jasper.audio_measurement.program_analysis import (
    DECONV_PRE_GUARD_S, MeasurementPriors, RecordedImpulse, analyze_program_capture,
)
from jasper.audio_measurement.program_analysis.response import recorded_impulse
from tests.crossover_v2_banked_round import bank_executor_take
from tests.crossover_v2_fixtures import _verify_analysis
from tests.test_audio_measurement_program_analysis import (
    FC_HZ, SR, _band_impulse, _roles, _synthesize,
)
from tests.test_crossover_v2_round_captures import PEAK_IDX, _bank_canonical, _write_round


def _arrival(impulse: RecordedImpulse) -> float:
    return impulse.peak_index - impulse.origin_index - impulse.clock_shift_samples


def test_every_sweep_keeps_its_impulse_on_the_recordings_one_clock():
    program = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 0.8, "tweeter": 0.6},
    )
    tweeter_later = 25
    capture = _synthesize(
        program, woofer_ir=_band_impulse(200, 150, 6000, 0.8),
        tweeter_ir=_band_impulse(200 + tweeter_later, 300, 20000, 0.7), epsilon=80e-6,
    )
    kept = analysis_impulses(analyze_program_capture(
        program, capture, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    ))

    for role in ("woofer", "tweeter"):
        repeats = sorted(one.repeat_index for one in kept if one.role == role)
        assert repeats == list(range(len(repeats))) and len(repeats) >= 2
        for index in repeats:
            assert _arrival(impulse_for(kept, role, index)) == pytest.approx(
                _arrival(impulse_for(kept, role)), abs=1.0)
    assert _arrival(impulse_for(kept, "tweeter")) - _arrival(impulse_for(kept, "woofer")) == pytest.approx(
        tweeter_later, abs=1.0)
    for one in kept:
        assert one.impulse.samples.dtype == np.float32
        assert one.impulse.samples.size <= one.impulse.origin_index + round(0.5 * SR) + 1


def test_a_capture_whose_loudest_sample_is_noise_keeps_no_more():
    full = np.zeros(5 * SR)
    full[4 * SR] = 1.0

    kept = recorded_impulse(full, 12_000, SimpleNamespace(segment_id="sweep_w"), SR)

    assert kept.samples.size == 12_000 + round(DEFAULT_VERIFY_TAIL_S * SR) + 1


def test_a_summed_sweep_keeps_its_impulse():
    program = build_verify_program(FC_HZ, sweep_s=1.5)
    capture = _synthesize(program, woofer_ir=_band_impulse(300, 20, 20000, 0.9),
                          tweeter_ir=_band_impulse(300, 20, 20000, 0.9))
    kept = analysis_impulses(analyze_program_capture(program, capture, SR))

    summed = impulse_for(kept, "summed")
    assert [one.role for one in kept] == ["summed"]
    assert summed.origin_index == round(DECONV_PRE_GUARD_S * SR)
    assert abs(_arrival(summed)) <= 2


def bank_kept_impulse_take(
    root: Path, monkeypatch, samples: np.ndarray, *, origin_index: int = 12_000,
    clock_shift_samples: float = 0.0,
) -> tuple[Path, dict]:
    """A take banked through the capture host, keeping ``samples`` as its summed impulse."""
    program = build_verify_program(2500, sweep_s=1.5, gain_db=-30, leading_pilot_gains_db=(-24, -14))
    impulse = RecordedImpulse(
        np.asarray(samples, dtype=np.float32), 48000, origin_index=origin_index,
        peak_index=int(np.argmax(np.abs(samples))), segment_id="sweep_verify",
        clock_shift_samples=clock_shift_samples,
    )
    doc = bank_executor_take(root, monkeypatch, program=program, analysis_fields={
        "summed_response": replace(_verify_analysis(program).summed_response, impulse=impulse),
    })
    return next((root / "sessions").iterdir()), doc


def test_a_banked_take_keeps_its_impulses_beside_the_recording(tmp_path, monkeypatch):
    samples = np.sin(np.arange(2000) / 7.0).astype(np.float32)
    bundle, doc = bank_kept_impulse_take(tmp_path, monkeypatch, samples, origin_index=100,
                                         clock_shift_samples=0.25)

    read = impulse_for(take_impulses(bundle, doc), "summed")
    np.testing.assert_array_equal(read.samples, samples)
    assert (read.origin_index, read.peak_index, read.clock_shift_samples, read.segment_id) == (
        100, int(np.argmax(np.abs(samples))), 0.25, "sweep_verify")
    entry, = (row for row in read_artifact_manifest(bundle)["artifacts"] if row["path"] == doc["impulses"]["path"])
    assert (entry["kind"], entry["sha256"], entry["dependencies"]) == (
        IMPULSES_KIND, doc["impulses"]["sha256"], [doc["wav_path"]])

    (bundle / doc["impulses"]["path"]).write_bytes(b"not the file that was banked")
    with pytest.raises(TakeImpulsesUnreadable):
        take_impulses(bundle, doc)


def test_a_take_without_impulses_records_none(tmp_path, monkeypatch):
    assert "impulses" not in bank_executor_take(tmp_path, monkeypatch)


def _response(role: str, peak: int) -> SimpleNamespace:
    samples = np.zeros(4800, dtype=np.float32)
    samples[peak] = 1.0
    return SimpleNamespace(role=role, repeat_index=None, repeat_responses=(), impulse=RecordedImpulse(
        samples, 48000, origin_index=240, peak_index=peak, segment_id=f"sweep_{role[0]}"))


def test_readers_take_each_role_from_the_kept_impulses(tmp_path):
    round_dir = _write_round(tmp_path)
    record, doc = _bank_canonical(tmp_path)
    bundle = tmp_path / "bundle" / "b0"
    (bundle / "info.json").write_text(json.dumps({"bundle_schema_version": 1}))
    analysis = SimpleNamespace(summed_response=None,
                               driver_responses=(_response("woofer", 300), _response("tweeter", 310)))
    doc["impulses"] = write_take_impulses(bundle, doc["take_id"], analysis, recording=doc["wav_path"])
    record.write_text(json.dumps(doc))
    second = bundle / "summed" / "summed_cloud_verify_01.json"
    sidecar = json.loads(second.read_text())
    sidecar["impulses"] = write_take_impulses(bundle, "cloud_verify_01", analysis, recording=None)
    sidecar["candidate_id"] = doc["candidate_id"]
    second.write_text(json.dumps(sidecar))

    tweeter = select_capture(round_dir, capture_id=doc["take_id"], role="tweeter")
    assert (tweeter.peak_idx, tweeter.preprocessing["impulse_source"],
            tweeter.preprocessing["pre_guard_samples"]) == (310, "kept", 240)
    summed = select_capture(round_dir, capture_id=doc["take_id"], role="summed")
    assert "impulse_source" not in summed.preprocessing
    assert abs(summed.peak_idx - PEAK_IDX) <= 1
    with pytest.raises(RoundCapturesRefused) as refused:
        select_capture(round_dir, capture_id=doc["take_id"], role="mid")
    assert (refused.value.reason, refused.value.detail["roles"]) == (
        "round_role_not_recorded", ["tweeter", "woofer"])
    ladder = sweep_round(round_dir, role="tweeter")
    assert [pose["direct_peak_ms"] for pose in ladder["poses"]] == [pytest.approx(1000 * 310 / 48000)] * 2
    # The rebuilt sum stands off the take's recording clock, so the forecast refuses it by name.
    with pytest.raises(RoundCapturesRefused) as unclocked:
        read_diagnostic(round_dir, doc["take_id"], 5.0)
    assert (unclocked.value.reason, unclocked.value.detail["roles"]) == ("round_branch_diagnostic_missing", ["summed"])
