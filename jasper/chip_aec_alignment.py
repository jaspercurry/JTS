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
# that shape survives a box whose write jitter alone is 86 frames.  The
# end-to-end guard on the quantity K actually depends on is
# `runtime_sys_delay`'s MIN_EDGE_MARGIN bound against the commissioned
# SYS_DELAY, which no amount of drift escapes.
QUEUE_DRIFT_NOISE_SIGMAS = 3
QUEUE_MAX_MEDIAN_DRIFT = math.ceil(
    QUEUE_DRIFT_NOISE_SIGMAS * QUEUE_MAX_SPREAD / math.sqrt(QUEUE_SAMPLE_COUNT)
)
WINDOW_CENTER = 20.5
MAX_CENTER_ERROR = 2.0
MIN_EDGE_MARGIN = 8
TIMING_TRIALS = 3
MIN_PEAK_RATIO = 1.10
MIN_TIMING_PEAK = 0.20
MIN_RAW_EXCESS_SNR_DB = 10.0
MIN_BEAM_ACQUISITION_DB = 8.0
MIN_BEAM_SUPPRESSION_DB = 10.0
MAX_RAW_LEVEL_DELTA_DB = 1.0
RATE = 16_000
CAPTURE_CHANNELS = 6
GUARD = 5_600
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


class QueueMovedFromCommissioned(ValueError):
    """The live queue median no longer sits where the artifact measured it.

    A distinct type because the disposition is distinct: nothing is broken, the
    artifact simply stopped describing this box.  ``delay`` is the resolved
    SYS_DELAY, which has already passed the chip's own range check, so a caller
    may apply it and disclose (ADR-0101) rather than stop.  A subclass of
    ValueError so a caller that only knows the old contract still catches it.
    """

    def __init__(self, message: str, delay: int) -> None:
        super().__init__(message)
        self.delay = delay


def runtime_sys_delay(
    k_samples: int,
    queue_samples: Sequence[int | float],
    *,
    commissioned_sys_delay: int,
) -> int:
    """Return ``K - median(the live queue window)``; never clamp.

    ``commissioned_sys_delay`` is keyword-only and has no default on purpose:
    every caller has to supply the artifact's own value, so a boot cannot be
    silently ungated.  The bound it enables is the end-to-end one.  Since
    ``K = commissioned SYS_DELAY + median(commissioned window)``, the difference
    between the delay resolved here and the commissioned one IS the difference
    between the two windows' medians — the only error term standing between the
    commissioner's verified alignment and what boot applies.  ``choose_delay``
    reserves ``MIN_EDGE_MARGIN`` frames of causal-window margin on both edges,
    so that is exactly how far the live median may have moved before the
    projected first peak leaves the window commissioning proved.  Past it,
    `QueueMovedFromCommissioned` hands the delay out for a caller that
    discloses rather than one that stops.

    The chip's declared ``CHIP_AEC_SYS_DELAY_MIN..MAX`` range is checked FIRST
    and still refuses outright: it is the driver cap, nothing may be written
    outside it, and checking it ahead of the margin is what makes the margin
    safe to demote to a disclosure.
    """

    if type(k_samples) is not int or type(commissioned_sys_delay) is not int:
        raise ValueError("K and the commissioned SYS_DELAY must be integers")
    values = tuple(float(v) for v in queue_samples)
    queue = median_samples(values)
    if not queue_window_is_stable(values):
        raise ValueError("chip-reference queue is unstable")
    delay = k_samples - queue
    if not xvf3800.CHIP_AEC_SYS_DELAY_MIN <= delay <= xvf3800.CHIP_AEC_SYS_DELAY_MAX:
        raise ValueError(f"runtime SYS_DELAY {delay} is out of range")
    moved = delay - commissioned_sys_delay
    if abs(moved) > MIN_EDGE_MARGIN:
        raise QueueMovedFromCommissioned(
            f"live reference queue median has moved {moved:+d} frames from the "
            f"commissioned window (limit {MIN_EDGE_MARGIN}); SYS_DELAY {delay} "
            "is outside the causal margin commissioning reserved",
            delay,
        )
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
    # The sample format outputd's own client edge negotiated, read back from
    # the installed ``hw_params`` (outputd STATUS ``dac.format``).  That is the
    # hardware edge on a raw ``hw:`` device; through an ALSA ``plug`` it is not,
    # because a plug installs the client's own request client-side and converts
    # on the slave side.  A commissioned ``K`` is only valid for the electrical
    # edge it was measured against, so moving the edge — a DAC swap, or the
    # registry re-declaring one — is hardware-class divergence, which
    # `identity_divergence` names as the loud kind.
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
# not worth refusing (ADR-0101).  Everything else describes the class the
# alignment was measured against.
PER_UNIT_IDENTITY_FIELDS = frozenset({"xvf_serial", "output_hardware_key"})
HARDWARE_CLASS_IDENTITY_FIELDS = tuple(
    name
    for name in AlignmentIdentity.__dataclass_fields__
    if name not in PER_UNIT_IDENTITY_FIELDS
)


def hardware_class_key(
    identity: AlignmentIdentity | Mapping[str, Any],
) -> tuple[Any, ...]:
    """Return what an identity says about the hardware CLASS, in field order.

    Two boxes sharing this key share everything K was measured against, so a
    proof banked on one describes the other (ADR-0101).  A mapping — a shipped
    registry row, which carries no per-unit fields — is held to
    `AlignmentIdentity`'s own field rules by filling those two with a
    placeholder that never leaves this call, so a malformed row fails where it
    is declared rather than at boot.
    """

    if not isinstance(identity, AlignmentIdentity):
        if set(identity) != set(HARDWARE_CLASS_IDENTITY_FIELDS):
            raise ValueError("hardware class identity fields are incomplete")
        fields: dict[str, Any] = dict.fromkeys(PER_UNIT_IDENTITY_FIELDS, "unkeyed")
        fields.update(identity)
        identity = AlignmentIdentity(**fields)
    return tuple(getattr(identity, name) for name in HARDWARE_CLASS_IDENTITY_FIELDS)


def identity_divergence(
    commissioned: AlignmentIdentity, live: AlignmentIdentity
) -> tuple[str, ...]:
    """Return the identity fields that differ, in declaration order.

    The names are what a household needs to see, so they travel rather than a
    verdict; a caller splits them against `PER_UNIT_IDENTITY_FIELDS`.
    """

    return tuple(
        name
        for name in AlignmentIdentity.__dataclass_fields__
        if getattr(commissioned, name) != getattr(live, name)
    )


def validate_banked_delays(k_samples: int, sys_delay: int) -> None:
    """Apply the rules a banked K/SYS_DELAY pair passes wherever it is banked.

    ``CHIP_AEC_SYS_DELAY_MIN..MAX`` is the chip's DECLARED driver cap, so this
    refuses rather than clamps — for a commissioned artifact, a superseded one,
    and a shipped hardware-class default alike.
    """

    if type(k_samples) is not int or type(sys_delay) is not int:
        raise ValueError("artifact K and SYS_DELAY must be integers")
    if not (
        xvf3800.CHIP_AEC_SYS_DELAY_MIN <= sys_delay <= xvf3800.CHIP_AEC_SYS_DELAY_MAX
    ):
        raise ValueError("artifact SYS_DELAY is out of range")


@dataclass(frozen=True)
class AlignmentArtifact:
    identity: AlignmentIdentity
    k_samples: int
    # The SYS_DELAY the commissioner verified — the one that passed the causal
    # window, the convergence transition, and the >= 10 dB beam suppression.
    # Stored so boot can bound how far it is allowed to resolve away from it
    # (`runtime_sys_delay`).  Without it the only boot-side check on the delay
    # is the chip's own -64..256 range, which is six times wider than the causal
    # window, so a live median tens of frames off the commissioned one would be
    # applied silently.
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


class ArtifactSchemaSuperseded(ValueError):
    """The artifact predates the current schema but still banks a usable K.

    ADR-0101: the schema moved, the measurement did not, so boot applies the
    banked values and discloses rather than parking on a version number.  They
    have passed the same integer and driver-cap checks `AlignmentArtifact`
    applies.  A subclass of ValueError so a caller that only knows the old
    contract still treats it as a bad artifact.
    """

    def __init__(self, message: str, k_samples: int, sys_delay: int) -> None:
        super().__init__(message)
        self.k_samples = k_samples
        self.sys_delay = sys_delay


def artifact_from_dict(value: object) -> AlignmentArtifact:
    if not isinstance(value, Mapping) or set(value) != {
        "kind", "schema", "identity", "k_samples", "sys_delay"
    }:
        raise ValueError("alignment artifact schema is invalid")
    if value["kind"] != ARTIFACT_KIND:
        raise ValueError("alignment artifact kind is unsupported")
    schema = value["schema"]
    if type(schema) is not int or schema > ARTIFACT_SCHEMA:
        # Only an OLDER schema is a proof this build knows how to read.  A
        # schema from the future is what a rollback leaves behind
        # (JASPER_DEPLOY_ALLOW_DOWNGRADE), and applying a K whose meaning this
        # build does not know is exactly what ADR-0101 does not license.
        raise ValueError("alignment artifact schema is unsupported")
    if schema < ARTIFACT_SCHEMA:
        # ADR-0101: the schema moved, the measurement did not.  The identity is
        # deliberately NOT inspected — an older shape is expected here.
        k_samples, sys_delay = value["k_samples"], value["sys_delay"]
        validate_banked_delays(k_samples, sys_delay)
        raise ArtifactSchemaSuperseded(
            f"alignment artifact schema {schema} predates schema "
            f"{ARTIFACT_SCHEMA}, so its commissioned identity cannot be "
            "compared against this box",
            k_samples,
            sys_delay,
        )
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
        if self.mic not in range(4) or len(self.trials) != TIMING_TRIALS:
            raise ValueError("timing requires three trials for one physical mic")
        if any(type(value) is not int for value in self.trials):
            raise ValueError("timing trials must be integers")

    @property
    def lag(self) -> int:
        return median_samples(self.trials)

    @property
    def uncertainty(self) -> int:
        return max(abs(value - self.lag) for value in self.trials)

    def projected(self, delay: int) -> int:
        return self.lag - (delay - self.current_delay)


@dataclass(frozen=True)
class DelayChoice:
    sys_delay: int
    projected_lags: tuple[int, ...]
    worst_edge_margin: int


def choose_delay(evidence: Sequence[MicTiming]) -> DelayChoice:
    """Choose the global delay nearest the 20.5-sample causal bull's-eye."""

    if len(evidence) != 4 or {item.mic for item in evidence} != set(range(4)):
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
                lag - item.uncertainty - xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MIN,
                xvf3800.CHIP_AEC_FIRST_PEAK_HARD_MAX - lag - item.uncertainty,
            )
            for item, lag in zip(evidence, lags, strict=True)
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
    lag: int
    peak: float
    peak_ratio: float
    mic_rms_dbfs: float
    reference_rms_dbfs: float
    clipped_samples: int


def _bandpass(values: np.ndarray) -> np.ndarray:
    import numpy as np
    from scipy import signal as scipy_signal

    sos = scipy_signal.butter(4, (250, 3_800), btype="bandpass", fs=RATE, output="sos")
    return scipy_signal.sosfiltfilt(sos, values.astype(np.float64))


def _dbfs_rms(values: np.ndarray) -> float:
    import numpy as np

    rms = float(np.sqrt(np.mean(np.square(values, dtype=np.float64))))
    return 20 * math.log10(rms / 32_768) if rms > 0 else -300.0


def analyze_timing(channels: np.ndarray, stimulus_16k: np.ndarray) -> TimingResult:
    """Analyze routed category-3 mic (ch0) against category-12 ref (ch1)."""

    import numpy as np

    from jasper.capture_relay.alignment import cross_correlation_alignment

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
    alignment = cross_correlation_alignment(
        search_f, reference_f, sample_rate=RATE, exclude_radius=8
    )
    lag = alignment.lag_samples - 512
    paired = search[alignment.lag_samples : alignment.lag_samples + ACTIVE]
    paired_f, reference_f = _bandpass(paired), _bandpass(reference)
    denominator = float(np.linalg.norm(paired_f) * np.linalg.norm(reference_f))
    peak = abs(float(np.dot(paired_f, reference_f))) / denominator if denominator else 0
    ratio = alignment.peak / max(alignment.secondary, np.finfo(float).eps)
    clips = int(np.count_nonzero(np.abs(np.column_stack((paired, reference)).astype(np.int32)) >= CLIP))
    result = TimingResult(
        lag, peak, ratio, _dbfs_rms(paired), _dbfs_rms(reference), clips
    )
    at_edge = alignment.lag_samples <= 8 or alignment.lag_samples >= 1_016
    if at_edge or result.peak < MIN_TIMING_PEAK or result.peak_ratio < MIN_PEAK_RATIO or clips:
        raise ValueError(
            f"timing rejected: lag={lag} peak={peak:.4f} "
            f"ratio={ratio:.4f} clips={clips} edge={at_edge}"
        )
    return result


@dataclass(frozen=True)
class ProductResult:
    raw_level_delta_db: float
    minimum_raw_excess_snr_db: float
    beam_suppression_db: tuple[float, float]
    beam_acquisition_db: tuple[float, float]
    clipped_samples: int


def _power(values: np.ndarray) -> float:
    import numpy as np

    return float(np.mean(np.square(values.astype(np.float64))))


def _capture_windows(channels: np.ndarray, active: np.ndarray) -> tuple[int, list[float], list[float], int]:
    import numpy as np

    from jasper.capture_relay.alignment import cross_correlation_alignment

    starts = [
        cross_correlation_alignment(channels[:, index], active, sample_rate=RATE).lag_samples
        for index in range(2, 6)
    ]
    start = int(np.median(starts))
    if start < GUARD or start + ACTIVE + GUARD > len(channels):
        raise ValueError("product capture lacks quiet guards")
    echoes: list[float] = []
    snrs: list[float] = []
    clips = 0
    for index in range(2, 6):
        quiet = max(
            _power(channels[start - GUARD : start, index]),
            _power(channels[start + ACTIVE : start + ACTIVE + GUARD, index]),
        )
        signal = _power(channels[start : start + ACTIVE, index]) - quiet
        if signal <= 0 or quiet <= 0:
            raise ValueError("raw excitation is not above room noise")
        echoes.append(signal)
        snrs.append(10 * math.log10(signal / quiet))
        clips += int(np.count_nonzero(
            np.abs(channels[start : start + ACTIVE, index].astype(np.int32)) >= CLIP
        ))
    return start, echoes, snrs, clips


def analyze_product(
    aec_on: np.ndarray,
    aec_off: np.ndarray,
    active_stimulus: np.ndarray,
) -> ProductResult:
    """Require raw excitation, zero clipping, and suppression on both beams."""

    import numpy as np

    on_start, on_raw, on_snr, on_clips = _capture_windows(aec_on, active_stimulus)
    off_start, off_raw, off_snr, off_clips = _capture_windows(aec_off, active_stimulus)
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
        raise ValueError("product AEC evidence does not meet the fixed thresholds")
    return result


def commissioning_stimulus() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return fixed -14 dBFS 48 kHz stereo, exact writer reference, active 16 kHz."""

    import numpy as np

    from jasper.audio_measurement.sweep import synchronized_swept_sine

    sweep, _ = synchronized_swept_sine(
        f1=300, f2=3_600, duration_approx_s=0.30, sample_rate=48_000,
        amplitude_dbfs=-14,
    )
    mono = np.pad(sweep, (16_800, 16_800))
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
