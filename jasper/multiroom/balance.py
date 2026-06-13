"""Pair-balance measurement core — equal-loudness ramp method (#23 P2).

The flow this serves (orchestrated by jasper/web/balance_flow.py): one
speaker at a time, the leader plays a band-limited noise ramp that
starts near-silent and rises at a fixed rate through that member's
NORMAL bonded chain (``correction_substream`` → fan-in → CamillaDSP →
snapserver → the member's snapclient → outputd channel pick → DAC,
including the member's current ``JASPER_GROUPING_TRIM_DB``). The phone
at the listening position watches its own in-band mic level and locks
the moment the speaker crosses a fixed target above the room's noise
floor. Repeat for the other speaker; the difference between the two
DRIVE levels at lock is the imbalance.

Why ramp-to-equal-loudness instead of fixed-level bursts (the v1
design this replaced): a fixed-level burst fails exactly on the case
the tool exists for — a badly mismatched pair puts the quiet
speaker's bursts under the noise floor and the whole take dies with
an unhelpful rejection. The ramp measures each speaker at MATCHED
RECEIVED loudness, so a quiet speaker simply locks later instead of
failing, and per-speaker failure ("ramped to maximum unheard") is
actionable. Every systematic latency — subprocess spawn, ALSA/fan-in
buffering, the snapclient buffer, LAN round-trip, the phone's
detection smoothing — is identical across the two passes and cancels
in the drive-level difference; what doesn't cancel is bounded by the
ramp rate (0.3 s of asymmetric delay at 1.5 dB/s ≈ 0.45 dB, under the
wizard's 0.5 dB step). The one latency that is per-MEMBER rather than
shared is the snapclient playout buffer (``JASPER_GROUPING_BUFFER_MS``,
operator-tuned per member, not set by the bond flow): a difference
between the two members feeds straight into the delta at the ramp
rate. In practice both default to 400 ms and an asymmetry large enough
to matter (≈100 ms → ≈0.15 dB) is itself a sync misconfiguration the
household would hear as an imaging smear before it dented a trim.

Hearing safety is structural: the ramp STARTS 30 dB below its
ceiling, the ceiling is the same -12 dBFS program level as the
correction sweep (never louder than music at the current volume), the
test noise has a fixed crest factor, and the WAV is bounded — playback
ends on its own even if every control path dies.

Everything here is numpy-pure and deterministic (seeded noise);
scipy is imported lazily and only for WAV I/O, mirroring
jasper/correction/sweep.py. The wizard-layer trim write composition
(:func:`recommend_trims`) is unchanged from v1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import TRIM_DB_MAX, TRIM_DB_MIN

# Measurement band (Hz): above the room-mode region where listening
# position dominates level, below tweeter directivity and phone-mic HF
# rolloff. The phone meters the same band, so out-of-band noise
# (HVAC, traffic rumble) moves neither the stimulus nor the meter.
BURST_F_LO = 500.0
BURST_F_HI = 2000.0

# Ramp shape. 30 dB of travel at 1.5 dB/s = 20 s, plus a hold at the
# ceiling so a marginal speaker gets a final chance to cross the
# target before "ramped unheard". Slower rate = finer precision but a
# longer walkthrough; 1.5 dB/s keeps lock-timing jitter (~0.3 s
# asymmetric worst case) under the 0.5 dB display step.
RAMP_START_DBFS = -42.0
RAMP_CEIL_DBFS = -12.0
RAMP_RATE_DB_S = 1.5
RAMP_HOLD_S = 4.0

# Silence at the head of the WAV: absorbs the subprocess-spawn /
# pipeline-fill transient so the audible ramp truly starts at the
# bottom, and gives the phone meter a beat of floor before stimulus.
RAMP_LEAD_IN_S = 0.5

# A lock arriving before the ramp has meaningfully started is a noise
# transient (door slam, cough), not the speaker — the wizard tells the
# phone to keep listening.
MIN_LOCK_OFFSET_S = RAMP_LEAD_IN_S + 1.0

CHANNELS = ("left", "right")


def ramp_duration_s() -> float:
    """Total WAV length: lead-in + linear ramp + ceiling hold."""
    travel = (RAMP_CEIL_DBFS - RAMP_START_DBFS) / RAMP_RATE_DB_S
    return RAMP_LEAD_IN_S + travel + RAMP_HOLD_S


def ramp_emission_dbfs(offset_s: float) -> float | None:
    """Emitted level (dBFS) at ``offset_s`` after playback start, or
    None while silent (lead-in) / after the WAV has ended. This pure
    function is the timing contract between the played WAV and the
    lock-time math — both are derived from the same constants."""
    if offset_s < RAMP_LEAD_IN_S or offset_s > ramp_duration_s():
        return None
    db = RAMP_START_DBFS + RAMP_RATE_DB_S * (offset_s - RAMP_LEAD_IN_S)
    return min(db, RAMP_CEIL_DBFS)


def _band_limited_noise(
    n: int, sample_rate: int, f_lo: float, f_hi: float, seed: int,
) -> np.ndarray:
    """Gaussian noise FFT-masked to the band, peak-normalized to 1.0
    (so an amplitude envelope expresses peak dBFS directly)."""
    if not 0 < f_lo < f_hi < sample_rate / 2:
        raise ValueError(
            f"band [{f_lo}, {f_hi}] must sit inside (0, {sample_rate / 2})"
        )
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n)
    spectrum = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    out = np.fft.irfft(spectrum, n)
    peak = float(np.max(np.abs(out)))
    if peak <= 0:
        raise ValueError("degenerate noise (all-zero after band mask)")
    return out / peak


def write_ramp_wav(
    path,
    channel: str,
    sample_rate: int = 48000,
    seed: int = 0x4A5453,  # "JTS" — deterministic, byte-stable output
) -> None:
    """Render the ramp as a 16-bit stereo WAV with the OTHER channel
    silent. Channel mapping is the grouping contract's: index 0 =
    left, index 1 = right (each member's outputd channel pick selects
    its side, so exactly one physical speaker emits)."""
    from scipy.io import wavfile

    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    total_s = ramp_duration_s()
    n_total = int(round(total_s * sample_rate))
    n_lead = int(round(RAMP_LEAD_IN_S * sample_rate))

    noise = _band_limited_noise(
        n_total - n_lead, sample_rate, BURST_F_LO, BURST_F_HI, seed)

    t = np.arange(noise.size, dtype=np.float64) / sample_rate
    env_db = np.minimum(
        RAMP_START_DBFS + RAMP_RATE_DB_S * t, RAMP_CEIL_DBFS)
    signal = noise * 10 ** (env_db / 20.0)

    # Edge fades (same squared-linspace idiom as the correction
    # sweep): the start fade kills the band-mask onset click, the end
    # fade keeps a natural WAV end from popping if nobody locked.
    fade = max(8, int(0.005 * sample_rate))
    if fade * 2 < signal.size:
        signal[:fade] *= np.linspace(0.0, 1.0, fade) ** 2
        signal[-fade:] *= np.linspace(1.0, 0.0, fade) ** 2

    stereo = np.zeros((n_total, 2), dtype=np.float64)
    stereo[n_lead:, CHANNELS.index(channel)] = signal
    int16 = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
    wavfile.write(str(path), sample_rate, int16)


def drive_delta_db(
    left_drive_dbfs: float, right_drive_dbfs: float,
) -> float:
    """Channel loudness delta (left − right; positive = left louder)
    from the two lock-point drive levels.

    The speaker that needed LESS drive to reach the same received
    loudness is the louder one, so the delta is right − left in drive
    terms. Because each ramp played through that member's CURRENT
    trim, this delta is residual — exactly what
    :func:`recommend_trims` composes with the existing trims."""
    return right_drive_dbfs - left_drive_dbfs


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
