"""Pair-balance measurement core — hardware-free math for the
phone-mic SPL auto-match flow (#23 P2).

The flow this module serves (orchestrated by the wizard layer, not
here): the leader plays an interleaved left/right/left sequence of
band-limited noise bursts through the NORMAL bonded music chain
(``correction_substream`` → fan-in → CamillaDSP → snapserver → every
member's snapclient → outputd). Each burst lives on exactly one
channel of a stereo WAV, so each member's outputd channel pick emits
it from exactly one physical speaker — the measurement exercises the
member's real output path including its current
``JASPER_GROUPING_TRIM_DB``. A phone at the listening position
records ONE continuous capture of the whole sequence; this module
turns that capture into a per-member trim recommendation.

Design choices (research-backed, see HANDOFF-multiroom "PAIR TRIM"):

- **500 Hz–2 kHz band.** Above the room-mode region where position
  dominates level, below tweeter directivity and phone-mic HF
  rolloff. Out-of-band energy (traffic rumble, HVAC) is ignored by
  both the burst synthesis and the analysis filter.
- **A/B/A interleave with a drift gate.** The left speaker plays
  first AND last; if those two measurements disagree by more than
  ``DRIFT_GATE_DB`` the phone moved or the level changed mid-take,
  and the whole session is rejected rather than averaged into a
  wrong answer.
- **Template alignment, not onset detection.** The burst timing is
  locked by the WAV we played, so the only unknown is the capture's
  start offset. Correlating the capture's band-limited envelope
  against the schedule's on/off template solves for that one scalar
  robustly; independent per-burst onset hunting fails on noise
  blips and quiet rooms.
- **The gaps are the noise floor.** Per-burst SNR is measured
  against the inter-burst silences of the same capture — no separate
  noise-capture step for the user.
- **Attenuate-only, renormalized.** Trims only ever attenuate
  (hearing safety + headroom, enforced again by validate_grouping
  and outputd's config parse); after balancing, trims are lifted
  together so the quieter side rides at 0 dB and no loudness is
  wasted.

Everything here is numpy-pure and deterministic (seeded synthesis)
so the whole pipeline is testable by rendering a synthetic "room"
capture and closing the loop in CI. scipy is imported lazily and
only for WAV I/O, mirroring jasper/correction/sweep.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import TRIM_DB_MAX, TRIM_DB_MIN

# Measurement band (Hz). See module docstring.
BURST_F_LO = 500.0
BURST_F_HI = 2000.0

# Burst/sequence timing. 0.8 s of band-limited noise integrates to a
# stable RMS on a phone mic; 0.6 s gaps give clean noise-floor windows
# and keep the whole take under ~6 s of holding the phone still.
BURST_S = 0.8
GAP_S = 0.6
LEAD_IN_S = 1.0
TAIL_S = 0.5

# Peak level of the synthesized burst. -12 dBFS mirrors the correction
# sweep: comfortable headroom through fan-in/CamillaDSP, loud enough to
# clear a domestic noise floor at normal listening volume.
BURST_AMPLITUDE_DBFS = -12.0

# |left(first) - left(again)| above this rejects the take: the phone
# moved, someone changed the volume, or a noise event landed on one of
# the anchor bursts.
DRIFT_GATE_DB = 1.0

# Each burst must clear the capture's own gap noise floor by this much
# in-band, or the take is too quiet/noisy to trust.
SNR_GATE_DB = 10.0

# Sample magnitude at/above this anywhere inside a burst window means
# the phone input clipped; levels measured from a clipped take lie.
CLIP_THRESHOLD = 0.99

# Analyze only the central fraction of each burst window — dodges the
# synthesis fades, alignment slop, and room decay tails at the edges.
ANALYSIS_WINDOW_FRACTION = 0.6

# Envelope hop for alignment (s). Coarse is fine: bursts are 0.8 s.
ENVELOPE_HOP_S = 0.010

# At the best alignment, mean envelope during template-on (bursts)
# must beat template-off (gaps/lead-in) by this factor, or the bursts
# aren't audible in the capture. Scale-free and local to the chosen
# offset, so it works for any capture length. ~10 dB SNR gives ≈3.2×.
ALIGNMENT_CONTRAST_MIN = 2.0

_DBFS_FLOOR = -120.0

CHANNELS = ("left", "right")


def _dbfs(value: float) -> float:
    if value <= 0 or not math.isfinite(value):
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(value))


@dataclass(frozen=True)
class BurstSpec:
    """One scheduled burst: which channel, where in the played WAV."""

    channel: str  # "left" | "right"
    start_s: float
    end_s: float


@dataclass(frozen=True)
class BalanceSchedule:
    """Timing contract between the played WAV and the analysis."""

    sample_rate: int
    bursts: tuple[BurstSpec, ...]
    total_s: float

    def to_dict(self) -> dict:
        return {
            "sample_rate": self.sample_rate,
            "total_s": round(self.total_s, 3),
            "bursts": [
                {
                    "channel": b.channel,
                    "start_s": round(b.start_s, 3),
                    "end_s": round(b.end_s, 3),
                }
                for b in self.bursts
            ],
        }


@dataclass(frozen=True)
class BalanceResult:
    """Outcome of evaluating one capture against a schedule.

    ``ok=False`` carries a machine-readable ``reason`` (one of
    "capture_short", "no_alignment", "clipped", "low_snr", "drift")
    plus whatever numbers were computable — the wizard turns reasons
    into user-facing guidance ("hold the phone still", "turn the
    volume up").
    """

    ok: bool
    reason: str
    left_dbfs: float
    right_dbfs: float
    delta_db: float  # left - right; positive = left louder
    drift_db: float
    snr_db: float
    noise_dbfs: float

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "left_dbfs": round(self.left_dbfs, 2),
            "right_dbfs": round(self.right_dbfs, 2),
            "delta_db": round(self.delta_db, 2),
            "drift_db": round(self.drift_db, 2),
            "snr_db": round(self.snr_db, 2),
            "noise_dbfs": round(self.noise_dbfs, 2),
        }


def _failed(reason: str, **levels: float) -> BalanceResult:
    return BalanceResult(
        ok=False,
        reason=reason,
        left_dbfs=levels.get("left_dbfs", _DBFS_FLOOR),
        right_dbfs=levels.get("right_dbfs", _DBFS_FLOOR),
        delta_db=levels.get("delta_db", 0.0),
        drift_db=levels.get("drift_db", 0.0),
        snr_db=levels.get("snr_db", 0.0),
        noise_dbfs=levels.get("noise_dbfs", _DBFS_FLOOR),
    )


def synth_balance_burst(
    sample_rate: int = 48000,
    duration_s: float = BURST_S,
    f_lo: float = BURST_F_LO,
    f_hi: float = BURST_F_HI,
    amplitude_dbfs: float = BURST_AMPLITUDE_DBFS,
    seed: int = 0x4A5453,  # "JTS" — fixed so the burst is reproducible
) -> np.ndarray:
    """Band-limited noise burst, float32 mono, peak at amplitude_dbfs.

    Gaussian noise → rFFT → hard mask outside [f_lo, f_hi] → irFFT,
    peak-normalized then faded. Deterministic for a given seed so the
    played WAV (and every test fixture) is byte-stable.
    """
    if not 0 < f_lo < f_hi < sample_rate / 2:
        raise ValueError(
            f"band [{f_lo}, {f_hi}] must sit inside (0, {sample_rate / 2})"
        )
    n = int(round(duration_s * sample_rate))
    if n < 64:
        raise ValueError(f"duration_s={duration_s} too short")
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    burst = np.fft.irfft(spectrum, n)

    peak = float(np.max(np.abs(burst)))
    if peak <= 0:
        raise ValueError("degenerate burst (all-zero after band mask)")
    amp = 10 ** (amplitude_dbfs / 20.0)
    burst = burst * (amp / peak)

    # Same squared-linspace fade as the correction sweep — kills the
    # band-edge click without measurably denting the RMS window.
    fade = max(8, int(0.005 * sample_rate))
    if fade * 2 < n:
        burst[:fade] *= np.linspace(0.0, 1.0, fade) ** 2
        burst[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2
    return burst.astype(np.float32)


def build_balance_schedule(
    sample_rate: int = 48000,
    channel_order: tuple[str, ...] = ("left", "right", "left"),
) -> BalanceSchedule:
    """The canonical A/B/A timing: lead-in, then burst/gap alternation."""
    for ch in channel_order:
        if ch not in CHANNELS:
            raise ValueError(f"unknown channel {ch!r}")
    bursts = []
    cursor = LEAD_IN_S
    for ch in channel_order:
        bursts.append(BurstSpec(channel=ch, start_s=cursor,
                                end_s=cursor + BURST_S))
        cursor += BURST_S + GAP_S
    total = cursor - GAP_S + TAIL_S
    return BalanceSchedule(sample_rate=sample_rate,
                           bursts=tuple(bursts), total_s=total)


def write_balance_wav(path: str | Path,
                      schedule: BalanceSchedule) -> None:
    """Render the schedule as a 16-bit stereo WAV.

    Channel mapping is the grouping contract's: index 0 = left,
    index 1 = right (the member outputd's channel pick selects its
    side). Every burst is the SAME seeded noise — only its channel
    placement differs — so left/right measurements compare identical
    program material.
    """
    from scipy.io import wavfile

    sr = schedule.sample_rate
    total_n = int(round(schedule.total_s * sr))
    stereo = np.zeros((total_n, 2), dtype=np.float32)
    burst = synth_balance_burst(sample_rate=sr)
    for spec in schedule.bursts:
        start = int(round(spec.start_s * sr))
        seg = burst[: total_n - start]
        stereo[start:start + seg.size, CHANNELS.index(spec.channel)] = seg
    int16 = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
    wavfile.write(str(path), sr, int16)


def band_rms_dbfs(
    samples: np.ndarray,
    sample_rate: int,
    f_lo: float = BURST_F_LO,
    f_hi: float = BURST_F_HI,
) -> float:
    """In-band RMS of a chunk, in dB (same Hann/rFFT-power shape as
    correction's _band_levels_dbfs, parameterized to one band).

    Convention caveat: the normalization is window-length-dependent
    (values scale as rms/sqrt(N)), so levels are directly comparable
    only between equal-length windows — which is all the balance flow
    does for its delta (burst windows share one length). The SNR gate
    compares burst windows against shorter gap windows, which
    understates SNR by a constant ~2 dB for this schedule — a
    conservative bias, pinned by the synthetic-capture tests."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 1 or sample_rate <= 0 or x.size < 8:
        return _DBFS_FLOOR
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    freqs = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    power = np.abs(spectrum) ** 2
    mask = (freqs >= f_lo) & (freqs < f_hi)
    if not np.any(mask):
        return _DBFS_FLOOR
    rms_like = math.sqrt(float(np.mean(power[mask]))) / max(1, x.size)
    return _dbfs(rms_like)


def _band_filter(samples: np.ndarray, sample_rate: int,
                 f_lo: float, f_hi: float) -> np.ndarray:
    """Zero-phase band-limit via rFFT mask round-trip (analysis only)."""
    spectrum = np.fft.rfft(np.asarray(samples, dtype=np.float64))
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / sample_rate)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, samples.size)


def _envelope(samples: np.ndarray, sample_rate: int,
              hop_s: float = ENVELOPE_HOP_S) -> np.ndarray:
    """Hop-RMS envelope of an already band-limited signal."""
    hop = max(1, int(round(hop_s * sample_rate)))
    usable = (samples.size // hop) * hop
    if usable == 0:
        return np.zeros(0)
    frames = samples[:usable].reshape(-1, hop)
    return np.sqrt(np.mean(frames * frames, axis=1))


def align_capture(
    capture: np.ndarray,
    sample_rate: int,
    schedule: BalanceSchedule,
) -> int | None:
    """Find the sample offset of the schedule's t=0 inside the capture.

    Correlates the capture's band-limited envelope against the
    schedule's boolean on/off template at envelope resolution. Returns
    None when the best alignment doesn't convincingly beat the
    baseline (ALIGNMENT_CONTRAST_MIN) — i.e. the bursts aren't
    audible in this capture.
    """
    hop = max(1, int(round(ENVELOPE_HOP_S * sample_rate)))
    filtered = _band_filter(capture, sample_rate, BURST_F_LO, BURST_F_HI)
    env = _envelope(filtered, sample_rate)
    template_len = int(round(schedule.total_s * sample_rate)) // hop
    if env.size < template_len or template_len == 0:
        return None
    template = np.zeros(template_len)
    for spec in schedule.bursts:
        a = int(round(spec.start_s * sample_rate)) // hop
        b = int(round(spec.end_s * sample_rate)) // hop
        template[a:b] = 1.0

    # Sliding dot product picks the offset; the accept gate is local to
    # that offset (see ALIGNMENT_CONTRAST_MIN): a global baseline like
    # the median score fails on short captures, where EVERY candidate
    # offset overlaps the bursts substantially.
    n_offsets = env.size - template_len + 1
    scores = np.empty(n_offsets)
    for i in range(n_offsets):
        scores[i] = float(np.dot(env[i:i + template_len], template))
    best = int(np.argmax(scores))
    aligned = env[best:best + template_len]
    on = aligned[template > 0]
    off = aligned[template == 0]
    if on.size == 0 or off.size == 0:
        return None
    on_mean = float(np.mean(on))
    off_mean = float(np.mean(off))
    if off_mean <= 0 or on_mean < ALIGNMENT_CONTRAST_MIN * off_mean:
        return None
    return best * hop


def evaluate_capture(
    capture: np.ndarray,
    sample_rate: int,
    schedule: BalanceSchedule,
) -> BalanceResult:
    """Turn one continuous capture into a left/right delta (or a
    machine-readable rejection). See module docstring for the gates."""
    capture = np.asarray(capture, dtype=np.float64)
    need = int(round(schedule.total_s * sample_rate))
    if capture.ndim != 1 or capture.size < need:
        return _failed("capture_short")

    offset = align_capture(capture, sample_rate, schedule)
    if offset is None:
        return _failed("no_alignment")

    # Per-burst windows: central ANALYSIS_WINDOW_FRACTION of each
    # scheduled burst, shifted by the alignment offset.
    per_burst: list[tuple[str, float]] = []
    margin_frac = (1.0 - ANALYSIS_WINDOW_FRACTION) / 2.0
    for spec in schedule.bursts:
        dur = spec.end_s - spec.start_s
        a = offset + int(round((spec.start_s + dur * margin_frac)
                               * sample_rate))
        b = offset + int(round((spec.end_s - dur * margin_frac)
                               * sample_rate))
        window = capture[a:b]
        if window.size == 0:
            return _failed("capture_short")
        if float(np.max(np.abs(window))) >= CLIP_THRESHOLD:
            return _failed("clipped")
        per_burst.append((spec.channel,
                          band_rms_dbfs(window, sample_rate)))

    # Noise floor from the inter-burst gaps of the SAME capture (the
    # central halves, away from room decay after each burst).
    gap_levels: list[float] = []
    for prev, nxt in zip(schedule.bursts, schedule.bursts[1:]):
        gap_dur = nxt.start_s - prev.end_s
        a = offset + int(round((prev.end_s + gap_dur * 0.25)
                               * sample_rate))
        b = offset + int(round((nxt.start_s - gap_dur * 0.25)
                               * sample_rate))
        if b > a:
            gap_levels.append(band_rms_dbfs(capture[a:b], sample_rate))
    noise_dbfs = max(gap_levels) if gap_levels else _DBFS_FLOOR

    lefts = [lvl for ch, lvl in per_burst if ch == "left"]
    rights = [lvl for ch, lvl in per_burst if ch == "right"]
    left_dbfs = float(np.mean(lefts)) if lefts else _DBFS_FLOOR
    right_dbfs = float(np.mean(rights)) if rights else _DBFS_FLOOR
    drift_db = (max(lefts) - min(lefts)) if len(lefts) >= 2 else 0.0
    snr_db = min(lvl for _, lvl in per_burst) - noise_dbfs
    delta_db = left_dbfs - right_dbfs
    common = {
        "left_dbfs": left_dbfs, "right_dbfs": right_dbfs,
        "delta_db": delta_db, "drift_db": drift_db,
        "snr_db": snr_db, "noise_dbfs": noise_dbfs,
    }
    if snr_db < SNR_GATE_DB:
        return _failed("low_snr", **common)
    if drift_db > DRIFT_GATE_DB:
        return _failed("drift", **common)
    return BalanceResult(ok=True, reason="", **common)


@dataclass(frozen=True)
class TrimRecommendation:
    left_trim_db: float
    right_trim_db: float
    clamped: bool  # True when the -24 dB floor prevented full balance

    def to_dict(self) -> dict:
        return {
            "left_trim_db": self.left_trim_db,
            "right_trim_db": self.right_trim_db,
            "clamped": self.clamped,
        }


def recommend_trims(
    delta_db: float,
    current_left_trim_db: float = 0.0,
    current_right_trim_db: float = 0.0,
) -> TrimRecommendation:
    """Map a measured delta (left - right, dB, measured WITH the
    current trims in effect) to new absolute trims.

    Attenuate-only and loudness-maximizing: the louder side comes
    down by the residual imbalance, then both are lifted together so
    the higher one sits at 0 dB. If the -24 dB floor truncates the
    correction, the best achievable pair is returned with
    ``clamped=True`` so the wizard can say so instead of silently
    under-correcting.
    """
    new_left = float(current_left_trim_db)
    new_right = float(current_right_trim_db)
    if delta_db >= 0:
        new_left -= delta_db
    else:
        new_right += delta_db

    lift = -max(new_left, new_right)  # bring the louder trim to 0
    new_left += lift
    new_right += lift

    clamped = new_left < TRIM_DB_MIN or new_right < TRIM_DB_MIN
    new_left = min(TRIM_DB_MAX, max(TRIM_DB_MIN, new_left))
    new_right = min(TRIM_DB_MAX, max(TRIM_DB_MIN, new_right))
    return TrimRecommendation(
        left_trim_db=round(new_left, 1),
        right_trim_db=round(new_right, 1),
        clamped=clamped,
    )
