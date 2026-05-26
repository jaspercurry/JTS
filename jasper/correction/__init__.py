"""Room correction v2 — sweep generation, deconvolution, PEQ design,
and CamillaDSP YAML rewrite.

Public surface (everything else is implementation detail):
  - sweep.synchronized_swept_sine(f1, f2, duration, sample_rate) → (sweep, meta)
  - sweep.write_sweep_wav(path, sweep, sample_rate)
  - playback.play_sweep(wav_path, alsa_device) → completion
  - deconv.deconvolve(captured_signal, sweep, sample_rate) → impulse response
  - analysis.smooth_fractional_octave(magnitude_db, freqs, fraction) → smoothed
  - target.flat_target(freqs) / target.harman_target(freqs)
  - strategy.design_correction(...) → bounded PEQ design + audit report
  - peq.design_peq(measured_db, target_db, freqs, **constraints) → list[PEQ]
  - camilla_yaml.emit_correction_config(base_yaml_path, peqs, out_path)
  - coordinator.measurement_window() — async context manager
  - session.MeasurementSession — state machine for the multi-step flow

See docs/HANDOFF-correction.md for the architecture and phase plan.
"""
