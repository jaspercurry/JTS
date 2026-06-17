"""Mic-backed acoustic analysis for active-speaker driver checks.

The driver-check step in the active-crossover flow has, until now, recorded
only operator confirmation plus a hand-passed ``observed_mic_dbfs`` number
([`measurement.py`](measurement.py) says it deliberately "does not ... infer
acoustic truth"). This module is the missing acoustic half: it turns a phone-
mic sweep capture into a real per-driver verdict (is this driver producing
sound, and in its expected band?) and a summed-crossover verdict (does the
crossover region sum without a cancellation null?).

It reuses the room-correction sweep / deconvolution / analysis primitives in
[`jasper.correction`](../correction/__init__.py) rather than reinventing the
DSP — the only thing correction can't do is target one physical output, so
``write_driver_sweep_wav`` builds a channel-targeted multichannel WAV (sweep on
one channel, silence elsewhere). numpy/scipy and the correction modules are
imported lazily inside functions so the socket-activated ``/sound/`` wizard
stays light until a measurement actually runs (mirrors
[`jasper/web/correction_setup.py`](../web/correction_setup.py)).

This module does no audio I/O and holds no state. The caller plays the WAV
through the active route under the existing safe-playback machinery, records
the phone mic with the shared browser recorder
([`measurement-audio.js`](../../deploy/assets/shared/js/measurement-audio.js)),
and hands the captured bytes here. The returned ``observed_mic_dbfs`` is what
[`measurement.record_driver_measurement`](measurement.py) already consumes;
the ``acoustic`` verdict block is new evidence for the same record.

Playback *safety* (level, ramp, tweeter protection) stays owned by
``safe_playback`` / ``calibration_level`` / ``driver_protection`` — this module
only chooses the digital sweep amplitude; the real SPL is governed by the
system volume and CamillaDSP at play time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# Default swept-sine parameters. Shorter than room correction's 10 s — a single
# driver needs far less SNR than a multi-position room average — but the same
# band and sample rate so the correction deconvolution path is reused verbatim.
DEFAULT_F1_HZ = 20.0
DEFAULT_F2_HZ = 20000.0
DEFAULT_DURATION_S = 6.0
DEFAULT_SAMPLE_RATE = 48000
# Conservative digital amplitude; the caller may lower it. This is signal
# amplitude only — acoustic level is governed by the volume/DSP chain.
DEFAULT_AMPLITUDE_DBFS = -18.0

# Frequency window we trust for a phone-mic + speaker sweep. Below ~40 Hz and
# above ~18 kHz, room modes, mic roll-off, and sweep fade dominate.
ANALYSIS_LO_HZ = 40.0
ANALYSIS_HI_HZ = 18000.0
DEFAULT_SMOOTHING_FRACTION = 24

# Verdict thresholds (all differential, so the unknown absolute calibration of
# the deconvolved magnitude cancels out).
SILENT_PEAK_DBFS = -45.0  # at/below this the capture is effectively silent
PRESENT_MIN_SEPARATION_DB = 0.0  # in-band must be at least as strong as out
OUT_OF_BAND_SEPARATION_DB = -3.0  # clearly more energy outside the band
DEFAULT_NULL_THRESHOLD_DB = 6.0  # crossover suckout that flags polarity/delay

DRIVER_ACOUSTIC_KIND = "jts_active_speaker_driver_acoustics"
SUMMED_ACOUSTIC_KIND = "jts_active_speaker_summed_acoustics"

# The complete verdict vocabulary each analyzer can return. Exported as the
# single source so callers that MAP verdicts (commissioning_capture's
# verdict->outcome maps) can guard-test that they cover the full set: a renamed
# or added verdict then fails a test loudly instead of silently skipping a
# capture (`.get()` -> None -> not recorded). Keep these in lockstep with the
# verdict literals returned by analyze_driver_capture / analyze_summed_crossover.
DRIVER_VERDICTS = frozenset(
    {"present", "out_of_band", "silent", "unusable_capture"}
)
SUMMED_VERDICTS = frozenset(
    {"blend_ok", "polarity_or_delay_problem", "unusable_capture"}
)


class DriverAcousticsError(ValueError):
    """Raised for malformed inputs (bad channel index, unreadable sweep meta)."""


@dataclass(frozen=True)
class DriverSweep:
    """Describes a channel-targeted sweep WAV written to disk."""

    sample_rate: int
    channel_count: int
    target_channel: int
    sweep_meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "channel_count": self.channel_count,
            "target_channel": self.target_channel,
            "sweep_meta": self.sweep_meta,
        }


@dataclass(frozen=True)
class DriverAcousticResult:
    """Per-driver acoustic verdict computed from a phone-mic capture."""

    verdict: str  # present | out_of_band | silent | unusable_capture
    present: bool
    observed_mic_dbfs: float
    peak_dbfs: float
    in_band_db: float
    out_of_band_db: float
    band_separation_db: float
    passband_hz: tuple[float, float]
    mic_clipping: bool
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": DRIVER_ACOUSTIC_KIND,
            "verdict": self.verdict,
            "present": self.present,
            "observed_mic_dbfs": self.observed_mic_dbfs,
            "peak_dbfs": self.peak_dbfs,
            "in_band_db": self.in_band_db,
            "out_of_band_db": self.out_of_band_db,
            "band_separation_db": self.band_separation_db,
            "passband_hz": list(self.passband_hz),
            "mic_clipping": self.mic_clipping,
            "quality": self.quality,
        }


@dataclass(frozen=True)
class SummedAcousticResult:
    """Summed-crossover verdict: is there a cancellation null at the crossover?"""

    verdict: str  # blend_ok | polarity_or_delay_problem | unusable_capture
    null_depth_db: float
    crossover_fc_hz: float
    observed_mic_dbfs: float
    mic_clipping: bool
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": SUMMED_ACOUSTIC_KIND,
            "verdict": self.verdict,
            "null_depth_db": self.null_depth_db,
            "crossover_fc_hz": self.crossover_fc_hz,
            "observed_mic_dbfs": self.observed_mic_dbfs,
            "mic_clipping": self.mic_clipping,
            "quality": self.quality,
        }


def write_driver_sweep_wav(
    path: str | Path,
    *,
    target_channel: int,
    channel_count: int,
    f1_hz: float = DEFAULT_F1_HZ,
    f2_hz: float = DEFAULT_F2_HZ,
    duration_s: float = DEFAULT_DURATION_S,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    amplitude_dbfs: float = DEFAULT_AMPLITUDE_DBFS,
) -> DriverSweep:
    """Write a multichannel sweep WAV with the ESS on one channel, silence else.

    ``jasper.correction.sweep.write_sweep_wav`` only emits mono; an active
    speaker needs the sweep routed to exactly one physical output so a single
    driver is excited. The returned ``DriverSweep.sweep_meta`` carries the exact
    synchronization parameters the analysis side must regenerate the reference
    sweep from (regenerating beats reloading the int16 WAV, which would add
    quantization error to the deconvolution reference).
    """
    if channel_count < 1:
        raise DriverAcousticsError(f"channel_count must be >= 1, got {channel_count}")
    if not 0 <= target_channel < channel_count:
        raise DriverAcousticsError(
            f"target_channel {target_channel} out of range for "
            f"{channel_count} channels"
        )

    import numpy as np
    from scipy.io import wavfile

    from jasper.correction import sweep as sweep_mod

    mono, meta = sweep_mod.synchronized_swept_sine(
        f1=f1_hz,
        f2=f2_hz,
        duration_approx_s=duration_s,
        sample_rate=sample_rate,
        amplitude_dbfs=amplitude_dbfs,
    )
    frame = np.zeros((meta.n_samples, channel_count), dtype=np.float32)
    frame[:, target_channel] = mono
    pcm = np.clip(frame, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    wavfile.write(str(path), meta.sample_rate, pcm16)
    return DriverSweep(
        sample_rate=meta.sample_rate,
        channel_count=channel_count,
        target_channel=target_channel,
        sweep_meta=meta.to_dict(),
    )


def _capture_to_magnitude(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    has_mic_calibration: bool,
):
    """Shared capture → (quality, freqs, smoothed_magnitude_db) pipeline.

    Returns ``(quality, None, None)`` when the capture fails quality gating —
    deconvolving an unsafe (clipped / too short / wrong rate) capture would
    fabricate a curve, so we stop and report the failure instead.
    """
    import numpy as np

    from jasper.correction import analysis, deconv, quality
    from jasper.correction import sweep as sweep_mod

    sample_rate = int(sweep_meta["sample_rate"])
    n_samples = int(sweep_meta["n_samples"])

    captured, sr = sweep_mod.read_wav_mono(captured_wav)
    # Bound the capture before assess + deconv (mirrors the /correction
    # session path) so quality and the IR describe the same signal and an
    # over-long capture can't drive the FFT to OOM on the 1 GB Pi.
    raw_capture_samples = len(captured)
    captured = deconv.cap_capture_length(
        captured, sweep_len=n_samples, sample_rate=sr,
    )
    report = quality.assess_capture(
        captured,
        sample_rate=sr,
        expected_sample_rate=sample_rate,
        sweep_n_samples=n_samples,
        has_mic_calibration=has_mic_calibration,
        truncated_from_samples=raw_capture_samples,
    )
    if report.failed:
        return report, None, None

    reference, _ = sweep_mod.synchronized_swept_sine(
        f1=float(sweep_meta["f1"]),
        f2=float(sweep_meta["f2"]),
        duration_approx_s=float(sweep_meta["duration_s"]),
        sample_rate=sample_rate,
        amplitude_dbfs=float(sweep_meta["amplitude_dbfs"]),
    )
    ir = deconv.deconvolve(
        captured.astype(np.float64),
        reference.astype(np.float64),
        sample_rate=sr,
    )
    freqs, mag_db = deconv.magnitude_response(ir, sr, normalize=False)
    smoothed = analysis.smooth_fractional_octave(
        freqs, mag_db, DEFAULT_SMOOTHING_FRACTION
    )
    return report, freqs, smoothed


def _band_mean_db(freqs, mag_db, lo_hz: float, hi_hz: float) -> float | None:
    import numpy as np

    mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not bool(np.any(mask)):
        return None
    return float(np.mean(mag_db[mask]))


def analyze_driver_capture(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    passband_hz: tuple[float, float],
    has_mic_calibration: bool = False,
) -> DriverAcousticResult:
    """Classify whether a driver is producing sound in its expected band.

    ``passband_hz`` is the driver's intended pass band (e.g. a woofer's
    ``(40, 400)``). A correct driver has more energy inside its band than
    outside it; a silent capture is flagged ``silent``; a driver whose energy
    sits clearly outside its band (mis-wired output, swapped driver) is flagged
    ``out_of_band``. ``observed_mic_dbfs`` is the capture RMS — the value
    ``measurement.record_driver_measurement`` already consumes.
    """
    import numpy as np

    lo, hi = float(passband_hz[0]), float(passband_hz[1])
    if not (0 < lo < hi):
        raise DriverAcousticsError(f"invalid passband_hz: {passband_hz!r}")

    report, freqs, mag_db = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration
    )
    quality_dict = report.to_dict()
    mic_clipping = report.clipped_fraction >= 1e-4

    if freqs is None:
        return DriverAcousticResult(
            verdict="unusable_capture",
            present=False,
            observed_mic_dbfs=report.rms_dbfs,
            peak_dbfs=report.peak_dbfs,
            in_band_db=float("nan"),
            out_of_band_db=float("nan"),
            band_separation_db=float("nan"),
            passband_hz=(lo, hi),
            mic_clipping=mic_clipping,
            quality=quality_dict,
        )

    band_lo = max(lo, ANALYSIS_LO_HZ)
    band_hi = min(hi, ANALYSIS_HI_HZ)
    in_band = _band_mean_db(freqs, mag_db, band_lo, band_hi)

    # Out-of-band reference: trusted analysis window minus the passband.
    out_mask = ((freqs >= ANALYSIS_LO_HZ) & (freqs <= ANALYSIS_HI_HZ)) & ~(
        (freqs >= band_lo) & (freqs <= band_hi)
    )
    out_of_band = (
        float(np.mean(mag_db[out_mask])) if bool(np.any(out_mask)) else None
    )

    if in_band is None:
        in_band = float("nan")
    if out_of_band is None:
        # Passband spans the whole trusted window (e.g. a full-range driver):
        # there is nothing to compare against, so separation is not meaningful.
        out_of_band = in_band
    separation = in_band - out_of_band

    if report.peak_dbfs <= SILENT_PEAK_DBFS:
        verdict, present = "silent", False
    elif separation < OUT_OF_BAND_SEPARATION_DB:
        verdict, present = "out_of_band", False
    elif separation >= PRESENT_MIN_SEPARATION_DB:
        verdict, present = "present", True
    else:
        # Slightly negative separation but audible: weak/marginal, not clearly
        # wrong. Treat as present so a real-but-quiet driver isn't rejected.
        verdict, present = "present", True

    return DriverAcousticResult(
        verdict=verdict,
        present=present,
        observed_mic_dbfs=report.rms_dbfs,
        peak_dbfs=report.peak_dbfs,
        in_band_db=in_band,
        out_of_band_db=out_of_band,
        band_separation_db=separation,
        passband_hz=(lo, hi),
        mic_clipping=mic_clipping,
        quality=quality_dict,
    )


def analyze_summed_crossover(
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    *,
    crossover_fc_hz: float,
    null_threshold_db: float = DEFAULT_NULL_THRESHOLD_DB,
    has_mic_calibration: bool = False,
) -> SummedAcousticResult:
    """Detect a cancellation null at the crossover in a summed-speaker capture.

    A correct crossover sums flat through the crossover region. A polarity flip
    or gross delay error cancels at the crossover and shows as a suckout: the
    magnitude at ``crossover_fc_hz`` drops well below its shoulders. The null
    depth is measured against the mean of the octave-away shoulders, which
    cancels the unknown absolute reference.
    """
    import numpy as np

    if crossover_fc_hz <= 0:
        raise DriverAcousticsError(
            f"crossover_fc_hz must be positive, got {crossover_fc_hz}"
        )

    report, freqs, mag_db = _capture_to_magnitude(
        captured_wav, sweep_meta, has_mic_calibration=has_mic_calibration
    )
    quality_dict = report.to_dict()
    mic_clipping = report.clipped_fraction >= 1e-4

    if freqs is None:
        return SummedAcousticResult(
            verdict="unusable_capture",
            null_depth_db=float("nan"),
            crossover_fc_hz=crossover_fc_hz,
            observed_mic_dbfs=report.rms_dbfs,
            mic_clipping=mic_clipping,
            quality=quality_dict,
        )

    at_fc = float(np.interp(crossover_fc_hz, freqs, mag_db))
    lower_shoulder = float(np.interp(crossover_fc_hz / 2.0, freqs, mag_db))
    upper_shoulder = float(np.interp(crossover_fc_hz * 2.0, freqs, mag_db))
    shoulder_mean = (lower_shoulder + upper_shoulder) / 2.0
    null_depth = shoulder_mean - at_fc

    verdict = (
        "polarity_or_delay_problem"
        if null_depth >= null_threshold_db
        else "blend_ok"
    )
    return SummedAcousticResult(
        verdict=verdict,
        null_depth_db=null_depth,
        crossover_fc_hz=crossover_fc_hz,
        observed_mic_dbfs=report.rms_dbfs,
        mic_clipping=mic_clipping,
        quality=quality_dict,
    )
