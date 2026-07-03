# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared amplitude-threshold impulse detector for the click/capture harness.

Both ends of a route-latency measurement need to find "where did the click
land" in a stream of S16 samples: the Rust ingress tap
(``rust/jasper-usbsink-audio``, out of this module's scope but built to the
same peak/hysteresis/refractory design) and this package's egress (mic)
detector. Keeping the Python detection logic in exactly one place — rather
than reimplementing it per caller — means a threshold/hysteresis/refractory
tuning change has one home on this side.

The two implementations share the same *design* but differ in one intentional
detail: the Rust tap reports the offset of the period's **peak** sample, while
this detector reports the **first** sample that crosses ``threshold``. On a
~5 ms click burst that is a ~1-2 ms systematic anchor skew — within the
harness's documented per-impulse error budget, but "same design," not
"byte-identical offset." It is not a defect: each side needs only a
consistent, sub-period anchor for its own timeline; the harness re-anchors and
subtracts.

Algorithm (peak + hysteresis + refractory), mirroring the pinned Rust tap
contract in spirit:

* Track a running ``armed`` state per detector instance (starts armed).
* A sample's absolute peak (as ``abs(sample) / 32768.0``, i.e. normalized to
  0..1 for S16) crossing ``threshold`` while armed fires a detection at that
  sample offset, then disarms the detector for ``refractory_samples`` more
  samples (suppresses re-firing within one impulse's decay tail).
* The detector re-arms once the peak has dropped below
  ``threshold - hysteresis`` (hysteresis keeps a slowly-decaying transient
  from re-triggering right at the threshold edge) *and* the refractory
  window has elapsed. Both conditions gate re-arming; either alone can
  produce a spurious extra fire on a signal that decays slowly through the
  threshold band.

This module has no I/O and no audio-device dependency — it operates on
in-memory ``int16`` sample buffers so it is trivially unit-testable. Its sole
runtime consumer today is the harness's mic-egress capture loop
(``jasper.cli.route_latency_harness.capture_mic_detections``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


DEFAULT_THRESHOLD = 0.2
DEFAULT_HYSTERESIS = 0.05
DEFAULT_REFRACTORY_MS = 250.0

_S16_FULL_SCALE = 32768.0


@dataclass(frozen=True)
class Detection:
    """One impulse detection within a buffer passed to ``feed``.

    ``sample_offset`` is the index within the buffer just fed to ``feed``
    (0-based), not a cumulative/global sample count — callers that need a
    global position add their own running offset (see
    ``StreamingDetector.total_samples_before_buffer`` usage in callers).
    """

    sample_offset: int
    peak: float


@dataclass
class StreamingDetector:
    """Stateful peak/hysteresis/refractory detector over successive buffers.

    Mirrors the Rust tap's per-period detector (same design; see module
    docstring for the one intentional anchor difference) so both ends of a
    measurement agree on what counts as "the click." Call :meth:`feed` once
    per arriving chunk of ``int16`` samples (a UDP packet or an ALSA period);
    it returns zero or more :class:`Detection` for that chunk.
    """

    threshold: float = DEFAULT_THRESHOLD
    hysteresis: float = DEFAULT_HYSTERESIS
    refractory_samples: int = 0
    _armed: bool = field(default=True, init=False, repr=False)
    _refractory_remaining: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError(f"threshold must be in (0, 1], got {self.threshold!r}")
        if self.hysteresis < 0.0 or self.hysteresis >= self.threshold:
            raise ValueError(
                f"hysteresis must be in [0, threshold), got {self.hysteresis!r} "
                f"(threshold={self.threshold!r})"
            )
        if self.refractory_samples < 0:
            raise ValueError("refractory_samples must be non-negative")

    def feed(self, samples: Sequence[int]) -> list[Detection]:
        """Scan one buffer of ``int16`` samples, returning any detections."""

        detections: list[Detection] = []
        rearm_level = self.threshold - self.hysteresis
        for i, raw in enumerate(samples):
            peak = abs(int(raw)) / _S16_FULL_SCALE
            if self._refractory_remaining > 0:
                self._refractory_remaining -= 1
            if not self._armed:
                if peak < rearm_level and self._refractory_remaining == 0:
                    self._armed = True
                continue
            if peak >= self.threshold:
                detections.append(Detection(sample_offset=i, peak=peak))
                self._armed = False
                self._refractory_remaining = self.refractory_samples
        return detections


def refractory_samples_for(refractory_ms: float, sample_rate_hz: int) -> int:
    """Convert a refractory window in milliseconds to whole samples."""

    if refractory_ms < 0.0:
        raise ValueError("refractory_ms must be non-negative")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    return round(refractory_ms * sample_rate_hz / 1000.0)


__all__ = [
    "DEFAULT_HYSTERESIS",
    "DEFAULT_REFRACTORY_MS",
    "DEFAULT_THRESHOLD",
    "Detection",
    "StreamingDetector",
    "refractory_samples_for",
]
