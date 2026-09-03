# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room correction v2 — targets and strategy, measurement flow, CamillaDSP YAML rewrite.

The pure measurement primitives (sweep generation, deconvolution, FR analysis,
mic calibration, capture-quality assessment, PEQ design) now live in the shared
kernel [`jasper.audio_measurement`](../audio_measurement/__init__.py); this
package consumes them and adds the correction-specific target and strategy
logic and the web flow.

Public surface (everything else is implementation detail):
  - jasper.audio_measurement.sweep.synchronized_swept_sine(...) → (sweep, meta)
  - jasper.audio_measurement.sweep.write_sweep_wav(path, sweep, sample_rate)
  - playback.play_sweep(wav_path, alsa_device) → completion
  - jasper.audio_measurement.deconv.deconvolve(captured, sweep, sr) → IR
  - jasper.audio_measurement.analysis.smooth_fractional_octave(...) → smoothed
  - target.flat_target(freqs) / target.harman_target(freqs)
  - strategy.design_correction(...) → bounded PEQ design + audit report
  - confidence.build_confidence_report(...) → measurement confidence summary
  - confidence.build_position_report(...) → per-band multi-position report
  - browser_audio.assess_browser_audio_path(...) → getUserMedia preflight report
  - jasper.audio_measurement.peq.design_peq(...) → list[PEQ]
  - apply path: jasper.sound.graph_carrier.carrier_for_loaded_config(...)
    .reemit(profile, room_peqs=..., out_path=..., profile_id=...) — what
    session.py emits before reloading CamillaDSP, width-matched to the
    loaded graph.
  - coordinator.measurement_window() — async context manager
  - session.MeasurementSession — state machine for the multi-step flow
"""
