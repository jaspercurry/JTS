# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure XVF3800 alignment contract and commissioning signal analysis.

This leaf performs no USB, ALSA, service, volume, or filesystem writes.  It
owns the small durable artifact schema, K-minus-live-queue math, global
four-microphone delay choice, and the two objective capture verdicts.
"""

from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.atomic_io import read_regular_bytes_nofollow
from jasper.audio_measurement.ramp import capped_gap_step_db
from jasper.mics import xvf3800

if TYPE_CHECKING:
    import numpy as np

ARTIFACT_PATH = Path("/var/lib/jasper/chip-aec-alignment.json")
ARTIFACT_KIND = "jts.xvf3800-chip-aec-alignment"
ARTIFACT_SCHEMA = 3
# The reference window: the precision K is defined against.  The median of
# QUEUE_SAMPLE_COUNT chip-reference readings whose range is at most
# QUEUE_MAX_SPREAD frames.  Windows at other mix cadences are held to the same
# median precision rather than to these literal numbers — `required_queue_samples`.
QUEUE_SAMPLE_COUNT = 8
QUEUE_MAX_SPREAD = 16
# How far the two halves of an accepted window may disagree on their median.
#
# `required_queue_samples` bounds the SAMPLING noise a window's median carries:
# it holds `spread / sqrt(readings)` at or under the reference window's
# QUEUE_MAX_SPREAD / sqrt(QUEUE_SAMPLE_COUNT).  It says nothing about the median
# having MOVED across the window, and a queue walking steadily away from its
# start holds any spread you like if you stop looking soon enough.  This is the
# bound on that movement, measured as the gap between the first half's median
# and the last half's.
#
# The value is three times the worst median-error scale the precision rule
# permits, so a still queue passes on noise alone: for a window of `n` readings
# spanning `s` frames the two half-medians differ with a standard deviation
# near `s / sqrt(n)`, which the precision rule caps at that same scale — jts3's
# realised window (235 readings, spread 86) sits at 3.03 sigma, so a boot
# false-rejects about one time in 400 and re-evaluates on the next read inside
# the same budget.  What it rejects is drift: across a window spanning T
# seconds it refuses anything past 2 * QUEUE_MAX_MEDIAN_DRIFT / T frames per
# second — 6.8 f/s at jts3's 5.0 s window, 17 f/s at jts.local's 2.0 s one.
# That is looser than the pre-#2253 rule's ~8 f/s on a fine cadence, because
# that number was the 16-frame spread bound doing double duty and no bound of
# that shape survives a box whose write jitter alone is 86 frames.
QUEUE_DRIFT_NOISE_SIGMAS = 3
QUEUE_MAX_MEDIAN_DRIFT = math.ceil(
    QUEUE_DRIFT_NOISE_SIGMAS * QUEUE_MAX_SPREAD / math.sqrt(QUEUE_SAMPLE_COUNT)
)
WINDOW_CENTER = 20.5
MAX_CENTER_ERROR = 2.0
MIN_EDGE_MARGIN = 8
# One trial per mic: the measured per-mic spread is <= 1 sample, and the final
# re-measure phase is the outlier check — it re-times at the chosen SYS_DELAY
# and `_final_timing` refuses an off-centre result.
TIMING_TRIALS = 1
MIC_COUNT = 4
# A candidate arrival is a correlation peak at least this much of the global
# maximum.  The sweep's cross-correlation oscillates at the band centre
# (~1950 Hz, ~8 samples at 16 kHz) and its side-lobes reach ~0.5 of their peak.
MIN_ARRIVAL_FRACTION = 0.65
# Past the chip's causal window a candidate is a distinct earlier arrival
# rather than a side-lobe of the strongest peak; inside it, the strongest peak
# is already a legal answer — see `_arrival_index`.
DISTINCT_ARRIVAL_MIN_SAMPLES = xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MAX
MIN_PEAK_RATIO = 1.10
MIN_TIMING_PEAK = 0.20
MIN_RAW_EXCESS_SNR_DB = 10.0
MIN_BEAM_ACQUISITION_DB = 8.0
MIN_BEAM_SUPPRESSION_DB = 10.0
# What the level probe aims the raw mics at: the stricter of the two level
# gates, plus a margin, so clearing it clears both.
LEVEL_TARGET_MARGIN_DB = 5.0
TARGET_RAW_EXCESS_SNR_DB = (
    max(MIN_RAW_EXCESS_SNR_DB, MIN_BEAM_ACQUISITION_DB) + LEVEL_TARGET_MARGIN_DB
)
MAX_RAW_LEVEL_DELTA_DB = 1.0
RATE = 16_000
PLAYBACK_RATE = 48_000
CAPTURE_CHANNELS = 6
GUARD = 5_600
# The same quiet guard expressed at the playback rate.
GUARD_48K = GUARD * PLAYBACK_RATE // RATE
ACTIVE = 4_800
CLIP = 32_767


def _round(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError("sample value must be finite")
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def median_samples(values: Sequence[int | float]) -> int:
    if not values or any(isinstance(v, bool) or not math.isfinite(float(v)) for v in values):
        raise ValueError("samples must be finite numbers")
    ordered = sorted(float(v) for v in values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return _round(median)


def required_queue_samples(spread: float) -> int:
    """Return how many chip-reference readings a window of ``spread`` frames needs.

    outputd samples ``snd_pcm_delay`` once per completed chip-reference write, so
    a window is a run of per-write readings and its spread is the writer's own
    jitter.  How wide that jitter runs is a property of the mix cadence: one mix
    period carries ``period_frames × chip_rate / dac_rate`` reference frames, and
    a burst wider than the writer's ALSA ring makes every write block, so where
    the reading lands at write completion moves with scheduling slack.  A fine
    cadence fits inside the ring and barely moves; a coarse one sweeps most of a
    burst.  jts.local (128-frame mix period) measures a spread of 11; jts3
    (1024-frame period) measures 86 with the same writer geometry.

    What has to hold across both is the precision of the MEDIAN, because K is
    ``commissioned SYS_DELAY + median(commissioned window)`` and boot applies
    ``K - median(live window)``.  The median's sampling error scales as
    ``spread / sqrt(samples)``, so pinning that ratio to the reference window's
    value gives every cadence the precision K was measured with — a wider spread
    buys back its precision by sampling longer, not by relaxing the bar.
    """

    if not math.isfinite(spread) or spread < 0:
        raise ValueError("queue spread must be a finite non-negative number")
    needed = math.ceil(spread * spread * QUEUE_SAMPLE_COUNT / (QUEUE_MAX_SPREAD**2))
    return max(QUEUE_SAMPLE_COUNT, needed)


def queue_median_drift(queue_samples: Sequence[int | float]) -> int:
    """Return how far a window's first and last halves disagree on their median.

    ``queue_samples`` must be in the order the writer produced them — this is
    the one place in the contract where that ordering is load-bearing, because
    a window is split in time here.  An odd-length window drops its middle
    reading so both halves are the same size.
    """

    half = len(queue_samples) // 2
    if half < 1:
        return 0
    return abs(
        median_samples(queue_samples[:half]) - median_samples(queue_samples[-half:])
    )


def queue_window_is_stable(queue_samples: Sequence[int | float]) -> bool:
    """Whether a window carries a median K can be measured against.

    Two independent conditions, both required: it estimates its median as
    precisely as the reference window did (`required_queue_samples`), and its
    median held still across the window (`QUEUE_MAX_MEDIAN_DRIFT`).  The first
    bounds sampling noise, the second bounds movement; neither implies the
    other.  The samples are time-ordered — see `queue_median_drift`.
    """

    if not queue_samples:
        return False
    spread = max(queue_samples) - min(queue_samples)
    if len(queue_samples) < required_queue_samples(spread):
        return False
    return queue_median_drift(queue_samples) <= QUEUE_MAX_MEDIAN_DRIFT


def runtime_sys_delay(
    k_samples: int,
    queue_samples: Sequence[int | float],
) -> int:
    """Return ``K - median(the live queue window)``; never clamp.

    ``K = commissioned SYS_DELAY + median(commissioned window)``, so subtracting
    the live median is what absorbs a reference queue that re-opened at a
    different fill: the queue term is real transport delay, and a moved median
    is the case K exists to answer, not a fault (ADR-0223).

    The chip's declared ``CHIP_AEC_SYS_DELAY_MIN..MAX`` range refuses outright:
    it is the driver cap, and nothing may be written outside it.
    """

    if type(k_samples) is not int:
        raise ValueError("K must be an integer")
    values = tuple(float(v) for v in queue_samples)
    queue = median_samples(values)
    if not queue_window_is_stable(values):
        raise ValueError("chip-reference queue is unstable")
    delay = k_samples - queue
    validate_banked_delays(k_samples, delay)
    return delay


@dataclass(frozen=True)
class AlignmentIdentity:
    xvf_variant: str
    xvf_serial: str
    xvf_firmware: str
    beam_plan: str
    fixed_profile: str
    output_id: str
    output_hardware_key: str
    output_pcm: str
    # outputd's negotiated hw_params sample format (STATUS ``dac.format``),
    # recorded for forensics only; excluded from comparison — ADR-0190.
    output_format: str
    output_rate: int
    output_channels: int
    output_period: int
    output_buffer: int

    def __post_init__(self) -> None:
        text = (
            self.xvf_variant,
            self.xvf_serial,
            self.xvf_firmware,
            self.beam_plan,
            self.fixed_profile,
            self.output_id,
            self.output_hardware_key,
            self.output_pcm,
            self.output_format,
        )
        if any(type(value) is not str or not value.strip() for value in text):
            raise ValueError("alignment identity text fields must be non-empty")
        numbers = (
            self.output_rate,
            self.output_channels,
            self.output_period,
            self.output_buffer,
        )
        if any(type(value) is not int or value <= 0 for value in numbers):
            raise ValueError("alignment identity geometry must be positive integers")


# The identity fields that name THIS physical box rather than its hardware
# class.  K is a property of the class, so a box whose only divergence is here
# is running a proof measured on a sibling of itself — worth saying out loud,
# not worth refusing (ADR-0101).
PER_UNIT_IDENTITY_FIELDS = frozenset({"xvf_serial", "output_hardware_key"})
# Recorded on every artifact for forensics only; excluded from comparison —
# ADR-0190.
RECORDED_ONLY_IDENTITY_FIELDS = frozenset({"xvf_variant", "beam_plan", "output_format"})
# What K was actually measured against: every field but the two sets above.
HARDWARE_CLASS_IDENTITY_FIELDS = tuple(
    name
    for name in AlignmentIdentity.__dataclass_fields__
    if name not in PER_UNIT_IDENTITY_FIELDS
    and name not in RECORDED_ONLY_IDENTITY_FIELDS
)
# `identity_divergence`'s default walk.
COMPARED_IDENTITY_FIELDS = tuple(
    name
    for name in AlignmentIdentity.__dataclass_fields__
    if name not in RECORDED_ONLY_IDENTITY_FIELDS
)


def hardware_class_identity(
    identity: AlignmentIdentity | Mapping[str, Any],
) -> AlignmentIdentity:
    """Return an identity carrying only what names the hardware CLASS.

    A mapping — a shipped registry row, which carries only
    `HARDWARE_CLASS_IDENTITY_FIELDS` — is held to `AlignmentIdentity`'s own
    field rules by filling the rest with a placeholder, so a malformed row
    fails where it is declared rather than at boot.  The placeholder is why
    the per-unit and recorded-only fields of the result mean nothing; only
    `HARDWARE_CLASS_IDENTITY_FIELDS` may be read off it.
    """

    if isinstance(identity, AlignmentIdentity):
        return identity
    if set(identity) != set(HARDWARE_CLASS_IDENTITY_FIELDS):
        raise ValueError("hardware class identity fields are incomplete")
    fields: dict[str, Any] = dict.fromkeys(
        PER_UNIT_IDENTITY_FIELDS | RECORDED_ONLY_IDENTITY_FIELDS, "unkeyed"
    )
    fields.update(identity)
    return AlignmentIdentity(**fields)


def hardware_class_key(
    identity: AlignmentIdentity | Mapping[str, Any],
) -> tuple[Any, ...]:
    """Return what an identity says about the hardware CLASS, in field order.

    Two boxes sharing this key share everything K was measured against, so a
    proof banked on one describes the other (ADR-0101).
    """

    resolved = hardware_class_identity(identity)
    return tuple(getattr(resolved, name) for name in HARDWARE_CLASS_IDENTITY_FIELDS)


def identity_divergence(
    commissioned: AlignmentIdentity,
    live: AlignmentIdentity,
    *,
    fields: Sequence[str] = COMPARED_IDENTITY_FIELDS,
) -> tuple[str, ...]:
    """Return the identity fields that differ, in declaration order.

    The default walk is `COMPARED_IDENTITY_FIELDS` — every field except
    `RECORDED_ONLY_IDENTITY_FIELDS`, which carry no timing story and so never
    diverge (ADR-0190).  The names are what a household needs to see, so they
    travel rather than a verdict; a caller splits them against
    `PER_UNIT_IDENTITY_FIELDS`.  ``fields`` narrows the walk further — a
    shipped class row compares only `HARDWARE_CLASS_IDENTITY_FIELDS`, because
    its per-unit fields are the placeholder `hardware_class_identity` filled
    in.
    """

    return tuple(
        name
        for name in fields
        if getattr(commissioned, name) != getattr(live, name)
    )


def validate_banked_delays(k_samples: int, sys_delay: int) -> None:
    """Apply the rules a banked K/SYS_DELAY pair passes wherever it is banked.

    ``CHIP_AEC_SYS_DELAY_MIN..MAX`` is the chip's DECLARED driver cap, so this
    refuses rather than clamps — for a commissioned artifact, a shipped
    hardware-class default, and the delay boot resolves from a live queue
    alike.  The message names no source for that reason, and carries the
    refused value: how far past the cap it landed is the whole diagnostic.
    """

    if type(k_samples) is not int or type(sys_delay) is not int:
        raise ValueError("K and SYS_DELAY must be integers")
    if not (
        xvf3800.CHIP_AEC_SYS_DELAY_MIN <= sys_delay <= xvf3800.CHIP_AEC_SYS_DELAY_MAX
    ):
        raise ValueError(
            f"SYS_DELAY {sys_delay} is out of range "
            f"({xvf3800.CHIP_AEC_SYS_DELAY_MIN}..{xvf3800.CHIP_AEC_SYS_DELAY_MAX})"
        )


@dataclass(frozen=True)
class AlignmentArtifact:
    identity: AlignmentIdentity
    k_samples: int
    # The SYS_DELAY the commissioner verified — the one that passed the causal
    # window, the convergence transition, and the >= 10 dB beam suppression.
    # A schema field held to the driver cap by `validate_banked_delays`, and
    # journalled at boot — but boot resolves its own delay from K and the live
    # queue and does not bound itself against this one (ADR-0223).
    sys_delay: int

    def __post_init__(self) -> None:
        validate_banked_delays(self.k_samples, self.sys_delay)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": ARTIFACT_KIND,
            "schema": ARTIFACT_SCHEMA,
            "identity": asdict(self.identity),
            "k_samples": self.k_samples,
            "sys_delay": self.sys_delay,
        }


def artifact_from_dict(value: object) -> AlignmentArtifact:
    if not isinstance(value, Mapping) or set(value) != {
        "kind", "schema", "identity", "k_samples", "sys_delay"
    }:
        raise ValueError("alignment artifact schema is invalid")
    if value["kind"] != ARTIFACT_KIND:
        raise ValueError("alignment artifact kind is unsupported")
    schema = value["schema"]
    if type(schema) is not int or schema != ARTIFACT_SCHEMA:
        # A newer schema is what a rollback leaves behind
        # (JASPER_DEPLOY_ALLOW_DOWNGRADE); an older one predates a key this
        # build requires to compare the commissioned identity. Either way this
        # build cannot apply the K it banks, so `resolve_banked_alignment`
        # treats it like any other unreadable artifact.
        raise ValueError("alignment artifact schema is unsupported")
    identity = value["identity"]
    fields = set(AlignmentIdentity.__dataclass_fields__)
    if not isinstance(identity, Mapping) or set(identity) != fields:
        raise ValueError("alignment artifact identity schema is invalid")
    return AlignmentArtifact(
        AlignmentIdentity(**{name: identity[name] for name in fields}),
        value["k_samples"],  # type: ignore[arg-type]
        value["sys_delay"],  # type: ignore[arg-type]
    )


def load_artifact(path: Path = ARTIFACT_PATH) -> AlignmentArtifact:
    try:
        raw = read_regular_bytes_nofollow(path, max_bytes=8_192)
        return artifact_from_dict(json.loads(raw.decode()))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"alignment artifact is unreadable: {exc}") from exc


@dataclass(frozen=True)
class MicTiming:
    mic: int
    current_delay: int
    trials: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.mic not in range(MIC_COUNT) or len(self.trials) != TIMING_TRIALS:
            raise ValueError(
                f"timing requires {TIMING_TRIALS} trials for one physical mic"
            )
        if any(type(value) is not int for value in self.trials):
            raise ValueError("timing trials must be integers")

    @property
    def lag(self) -> int:
        return median_samples(self.trials)

    def projected(self, delay: int) -> int:
        return self.lag - (delay - self.current_delay)


@dataclass(frozen=True)
class DelayChoice:
    sys_delay: int
    projected_lags: tuple[int, ...]
    worst_edge_margin: int


def choose_delay(evidence: Sequence[MicTiming]) -> DelayChoice:
    """Choose the global delay nearest the 20.5-sample causal bull's-eye."""

    if len(evidence) != MIC_COUNT or {
        item.mic for item in evidence
    } != set(range(MIC_COUNT)):
        raise ValueError("timing must cover physical microphones 0..3")
    if len({item.current_delay for item in evidence}) != 1:
        raise ValueError("timing trials must share one current SYS_DELAY")

    legal: list[tuple[tuple[float, int, int, int], int, tuple[int, ...], int]] = []
    current = evidence[0].current_delay
    for candidate in range(
        xvf3800.CHIP_AEC_SYS_DELAY_MIN,
        xvf3800.CHIP_AEC_SYS_DELAY_MAX + 1,
    ):
        lags = tuple(item.projected(candidate) for item in evidence)
        margins = tuple(
            min(
                lag - xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MIN,
                xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MAX - lag,
            )
            for lag in lags
        )
        margin = min(margins)
        if margin >= MIN_EDGE_MARGIN:
            center = (min(lags) + max(lags)) / 2
            legal.append(
                ((abs(center - WINDOW_CENTER), -margin, abs(candidate - current), candidate),
                 candidate, lags, margin)
            )
    if not legal:
        raise ValueError("no global SYS_DELAY has adequate causal-window margin")
    _, selected, lags, margin = min(legal)
    return DelayChoice(selected, lags, margin)


@dataclass(frozen=True)
class TimingResult:
    """One timing capture's arrival, the peaks it was chosen over, and levels.

    ``competitor_*`` is the strongest peak AFTER the arrival — the reflection
    the chip's own tail models, recorded so the journal shows what was skipped.
    ``earlier_*`` is what ``peak_ratio`` gates on: only correlation BEFORE the
    arrival can disprove it.
    """

    lag: int
    peak: float
    peak_height: float
    competitor_lag: int
    competitor_height: float
    earlier_lag: int
    earlier_height: float
    mic_rms_dbfs: float
    reference_rms_dbfs: float
    clipped_samples: int

    @property
    def peak_ratio(self) -> float:
        return self.peak_height / max(self.earlier_height, math.ulp(1.0))

    @property
    def competitor_offset_ms(self) -> float:
        """Arrival separation; x 0.343 m/ms is the extra path a reflection ran."""
        return 1_000 * (self.competitor_lag - self.lag) / RATE

    def evidence(self) -> dict[str, Any]:
        return {
            "lag": self.lag,
            "peak": round(self.peak, 4),
            "min_timing_peak": MIN_TIMING_PEAK,
            "peak_height": round(self.peak_height, 4),
            "peak_ratio": round(self.peak_ratio, 4),
            "min_peak_ratio": MIN_PEAK_RATIO,
            "competitor_lag": self.competitor_lag,
            "competitor_offset_ms": round(self.competitor_offset_ms, 2),
            "competitor_height": round(self.competitor_height, 4),
            "earlier_lag": self.earlier_lag,
            "earlier_height": round(self.earlier_height, 4),
            "mic_rms_dbfs": round(self.mic_rms_dbfs, 2),
            "reference_rms_dbfs": round(self.reference_rms_dbfs, 2),
            "clipped_samples": self.clipped_samples,
        }


class Rejected(ValueError):
    """A capture an objective gate refused, carrying why."""

    def __init__(self, label: str, fields: dict[str, Any]) -> None:
        self.fields = fields
        super().__init__(
            f"{label} rejected: "
            + " ".join(f"{name}={value}" for name, value in fields.items())
        )


class TimingRejected(Rejected):
    def __init__(self, result: TimingResult, *, at_edge: bool) -> None:
        self.result = result
        super().__init__("timing", {**result.evidence(), "at_edge": at_edge})


def _bandpass(values: np.ndarray) -> np.ndarray:
    import numpy as np
    from scipy import signal as scipy_signal

    sos = scipy_signal.butter(4, (250, 3_800), btype="bandpass", fs=RATE, output="sos")
    return scipy_signal.sosfiltfilt(sos, values.astype(np.float64))


def _dbfs_power(power: float) -> float:
    return 10 * math.log10(power / 32_768**2) if power > 0 else -300.0


def dbfs_rms(values: np.ndarray) -> float:
    return _dbfs_power(_power(values))


def _arrival_index(corr: np.ndarray) -> int:
    """Index of the first arrival: the strongest peak, unless a candidate is
    more than DISTINCT_ARRIVAL_MIN_SAMPLES earlier, when the earliest wins.

    The XVF3800 needs its reference causal against the FIRST arrival only (User
    Guide v3.2.1 s4.2.4), so a louder later reflection must not take the lag.
    """

    import numpy as np
    from scipy import signal as scipy_signal

    strongest = int(np.argmax(corr))
    peaks, _ = scipy_signal.find_peaks(
        corr, height=float(corr[strongest]) * MIN_ARRIVAL_FRACTION
    )
    distinct = peaks[peaks < strongest - DISTINCT_ARRIVAL_MIN_SAMPLES]
    return int(distinct[0]) if distinct.size else strongest


def analyze_timing(channels: np.ndarray, stimulus_16k: np.ndarray) -> TimingResult:
    """Analyze routed category-3 mic (ch0) against category-12 ref (ch1)."""

    import numpy as np

    from jasper.audio_measurement.alignment import (
        alignment_at,
        correlation,
        cross_correlation_alignment,
    )

    if channels.ndim != 2 or channels.shape[1] != CAPTURE_CHANNELS:
        raise ValueError("timing capture must be six-channel")
    marker = cross_correlation_alignment(channels[:, 1], stimulus_16k, sample_rate=RATE)
    start = marker.lag_samples + GUARD
    reference = channels[start : start + ACTIVE, 1]
    search_start = start - 512
    search = channels[search_start : start + ACTIVE + 512, 0]
    if search_start < 0 or len(reference) != ACTIVE or len(search) != ACTIVE + 1_024:
        raise ValueError("timing capture does not contain the measurement window")
    search_f, reference_f = _bandpass(search), _bandpass(reference)
    search_f -= search_f.mean()
    reference_f -= reference_f.mean()
    corr = correlation(search_f, reference_f, sample_rate=RATE)
    alignment = alignment_at(corr, _arrival_index(corr), exclude_radius=8)
    lag = alignment.lag_samples - 512
    paired = search[alignment.lag_samples : alignment.lag_samples + ACTIVE]
    paired_f, reference_f = _bandpass(paired), _bandpass(reference)
    denominator = float(np.linalg.norm(paired_f) * np.linalg.norm(reference_f))
    peak = abs(float(np.dot(paired_f, reference_f))) / denominator if denominator else 0
    clips = int(np.count_nonzero(np.abs(np.column_stack((paired, reference)).astype(np.int32)) >= CLIP))
    result = TimingResult(
        lag,
        peak,
        alignment.peak,
        alignment.later_lag_samples - 512,
        alignment.later,
        alignment.earlier_lag_samples - 512,
        alignment.earlier,
        dbfs_rms(paired),
        dbfs_rms(reference),
        clips,
    )
    at_edge = alignment.lag_samples <= 8 or alignment.lag_samples >= 1_016
    if at_edge or result.peak < MIN_TIMING_PEAK or result.peak_ratio < MIN_PEAK_RATIO or clips:
        raise TimingRejected(result, at_edge=at_edge)
    return result


@dataclass(frozen=True)
class ProductResult:
    raw_level_delta_db: float
    minimum_raw_excess_snr_db: float
    beam_suppression_db: tuple[float, float]
    beam_acquisition_db: tuple[float, float]
    clipped_samples: int

    def evidence(self) -> dict[str, Any]:
        return {
            "raw_level_delta_db_abs": round(abs(self.raw_level_delta_db), 3),
            "max_raw_level_delta_db": MAX_RAW_LEVEL_DELTA_DB,
            "raw_excess_snr_db": round(self.minimum_raw_excess_snr_db, 2),
            "min_raw_excess_snr_db": MIN_RAW_EXCESS_SNR_DB,
            "beam_acquisition_db": tuple(
                round(value, 2) for value in self.beam_acquisition_db
            ),
            "min_beam_acquisition_db": MIN_BEAM_ACQUISITION_DB,
            "beam_suppression_db": tuple(
                round(value, 2) for value in self.beam_suppression_db
            ),
            "min_beam_suppression_db": MIN_BEAM_SUPPRESSION_DB,
            "clipped_samples": self.clipped_samples,
        }


def _power(values: np.ndarray) -> float:
    import numpy as np

    return float(np.mean(np.square(values.astype(np.float64))))


def _capture_windows(
    channels: np.ndarray, active: np.ndarray, *, quiet_from_pre_guard: bool = False
) -> tuple[int, list[float], list[float], int, tuple[float, float]]:
    """Per-raw-mic excitation and excess SNR, plus the worst mic's two guards.

    The post-guard begins at the sweep's last sample, so it holds the reverb
    tail, which rises with the fader; only the pre-guard is a level-independent
    noise floor.  ``quiet_from_pre_guard`` is for a caller sizing a fader move.
    The gate keeps the ``max`` of both: it judges a capture, not a fader.
    """

    import numpy as np

    from jasper.audio_measurement.alignment import cross_correlation_alignment

    starts = [
        cross_correlation_alignment(channels[:, index], active, sample_rate=RATE).lag_samples
        for index in range(2, 6)
    ]
    start = int(np.median(starts))
    if start < GUARD or start + ACTIVE + GUARD > len(channels):
        raise ValueError("product capture lacks quiet guards")
    echoes: list[float] = []
    snrs: list[float] = []
    guards: list[tuple[float, float]] = []
    clips = 0
    for index in range(2, 6):
        pre = _power(channels[start - GUARD : start, index])
        post = _power(channels[start + ACTIVE : start + ACTIVE + GUARD, index])
        quiet = pre if quiet_from_pre_guard else max(pre, post)
        signal = _power(channels[start : start + ACTIVE, index]) - quiet
        if signal <= 0 or quiet <= 0:
            raise ValueError("raw excitation is not above room noise")
        echoes.append(signal)
        snrs.append(10 * math.log10(signal / quiet))
        guards.append((pre, post))
        clips += int(np.count_nonzero(
            np.abs(channels[start : start + ACTIVE, index].astype(np.int32)) >= CLIP
        ))
    return start, echoes, snrs, clips, guards[snrs.index(min(snrs))]


def analyze_product(
    aec_on: np.ndarray,
    aec_off: np.ndarray,
    active_stimulus: np.ndarray,
) -> ProductResult:
    """Require raw excitation, zero clipping, and suppression on both beams."""

    import numpy as np

    on_start, on_raw, on_snr, on_clips, _ = _capture_windows(aec_on, active_stimulus)
    off_start, off_raw, off_snr, off_clips, _ = _capture_windows(
        aec_off, active_stimulus
    )
    raw_ratio = float(np.median(np.asarray(on_raw) / np.asarray(off_raw)))
    raw_delta = 10 * math.log10(raw_ratio)
    suppression: list[float] = []
    acquisition: list[float] = []
    clips = on_clips + off_clips
    for beam in (0, 1):
        on_active = aec_on[on_start : on_start + ACTIVE, beam]
        off_active = aec_off[off_start : off_start + ACTIVE, beam]
        quiet = max(
            _power(aec_off[off_start - GUARD : off_start, beam]),
            _power(aec_off[off_start + ACTIVE : off_start + ACTIVE + GUARD, beam]),
        )
        echo = _power(off_active) - quiet
        if echo <= 0 or quiet <= 0:
            raise ValueError("AEC-off beam did not acquire the excitation")
        suppression.append(10 * math.log10(echo * raw_ratio / _power(on_active)))
        acquisition.append(10 * math.log10(echo / quiet))
        clips += sum(
            int(np.count_nonzero(np.abs(values.astype(np.int32)) >= CLIP))
            for values in (on_active, off_active)
        )
    result = ProductResult(
        raw_delta,
        min(*on_snr, *off_snr),
        (suppression[0], suppression[1]),
        (acquisition[0], acquisition[1]),
        clips,
    )
    if (
        abs(result.raw_level_delta_db) > MAX_RAW_LEVEL_DELTA_DB
        or result.minimum_raw_excess_snr_db < MIN_RAW_EXCESS_SNR_DB
        or min(result.beam_acquisition_db) < MIN_BEAM_ACQUISITION_DB
        or min(result.beam_suppression_db) < MIN_BEAM_SUPPRESSION_DB
        or result.clipped_samples
    ):
        raise Rejected("product", result.evidence())
    return result


@dataclass(frozen=True)
class LevelProbe:
    raw_excess_snr_db: float
    clipped_samples: int
    pre_guard_dbfs: float
    post_guard_dbfs: float

    def offset_db(self, step_cap_db: float) -> float:
        """One step toward `TARGET_RAW_EXCESS_SNR_DB`, saturated upward.

        ASSUMES the fader moves raw excess SNR dB for dB — valid while the
        pre-guard is noise-dominated.  A clipped capture understates its own
        excitation, so it caps at zero and `analyze_product` refuses it.
        """

        return capped_gap_step_db(
            measured_db=self.raw_excess_snr_db,
            target_db=TARGET_RAW_EXCESS_SNR_DB,
            cap_db=0.0 if self.clipped_samples else step_cap_db,
        )

    def reaches_target(self, step_cap_db: float) -> bool:
        """Whether ONE capped step lands on the target.

        A clipped capture is never asked to: its step caps at zero, so it can
        only fail this, and `analyze_product`'s clip gate is the right refusal.
        """

        return bool(self.clipped_samples) or (
            self.raw_excess_snr_db + self.offset_db(step_cap_db)
            >= TARGET_RAW_EXCESS_SNR_DB
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "warmup_raw_excess_snr_db": round(self.raw_excess_snr_db, 2),
            "target_raw_excess_snr_db": TARGET_RAW_EXCESS_SNR_DB,
            "warmup_clipped_samples": self.clipped_samples,
            "pre_guard_dbfs": round(self.pre_guard_dbfs, 2),
            "post_guard_dbfs": round(self.post_guard_dbfs, 2),
        }


def analyze_level(capture: np.ndarray, active_stimulus: np.ndarray) -> LevelProbe:
    """`analyze_product`'s own raw excess SNR, against the pre-guard alone."""

    _, _, snrs, clips, guards = _capture_windows(
        capture, active_stimulus, quiet_from_pre_guard=True
    )
    return LevelProbe(min(snrs), clips, *(_dbfs_power(power) for power in guards))


def commissioning_stimulus() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return fixed -14 dBFS 48 kHz stereo, exact writer reference, active 16 kHz."""

    import numpy as np

    from jasper.audio_measurement.sweep import synchronized_swept_sine

    sweep, _ = synchronized_swept_sine(
        f1=300, f2=3_600, duration_approx_s=0.30, sample_rate=PLAYBACK_RATE,
        amplitude_dbfs=-14,
    )
    mono = np.pad(sweep, (GUARD_48K, GUARD_48K))
    pcm = (np.clip(mono, -1, 1) * 32_767).astype("<i2")
    stereo = np.column_stack((pcm, pcm))
    blocks = stereo.astype(np.int64).reshape(-1, 3, 2)
    reference = np.trunc(blocks.sum(axis=(1, 2)) / 6).astype("<i2")
    return stereo, reference, reference[GUARD : GUARD + ACTIVE]


def read_capture(path: Path) -> np.ndarray:
    import numpy as np

    try:
        with wave.open(str(path), "rb") as wav:
            params = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
            raw = wav.readframes(wav.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"capture WAV is unreadable: {exc}") from exc
    if params != (RATE, CAPTURE_CHANNELS, 2):
        raise ValueError("capture must be 16 kHz, six-channel, S16_LE")
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size % CAPTURE_CHANNELS:
        raise ValueError("capture has a partial frame")
    return samples.reshape(-1, CAPTURE_CHANNELS)
