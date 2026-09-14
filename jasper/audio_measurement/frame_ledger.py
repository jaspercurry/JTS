# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end frame accounting for a measurement capture chain (#2094): reconciles the counters
a capture source reports against the frames the host decoded. No thresholds — any nonzero
discrepancy is a fail, since one missing quantum is a phase discontinuity that corrupts the
whole deconvolution regardless of what fraction of frames it is (#1765). The received count is
taken before ``deconv.cap_capture_length`` truncates. Arithmetic and vocabulary only, no
logging: ``program_analysis._verify_capture_integrity`` owns the pass/fail vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FrameLedger",
    "LOST_AT_ENCODER_TO_HOST",
    "LOST_AT_CAPTURE_OVERRUN",
    "LOST_AT_WORKLET_TO_ENCODER",
    "LOST_AT_WORKLET_TO_HOST",
    "REPORT_KEY_ENCODED_FRAMES",
    "REPORT_KEY_FRAMES",
    "REPORT_KEY_CAPTURE_GAPS",
    "REPORT_KEY_CAPTURE_GAP_FRAMES",
    "reconcile_capture_frames",
]

#: Keys read from ``CaptureResult.capture_integrity`` (cross-release wire contract); other keys
#: it may send (``blocks``, ``silent_blocks``, the zero-run keys) are unreconciled here (#2557).
REPORT_KEY_FRAMES = "frames"
REPORT_KEY_ENCODED_FRAMES = "encoded_frames"
REPORT_KEY_CAPTURE_GAPS = "capture_gaps"
REPORT_KEY_CAPTURE_GAP_FRAMES = "capture_gap_frames"

LOST_AT_CAPTURE_OVERRUN = "capture_overrun"
LOST_AT_WORKLET_TO_ENCODER = "worklet->encoder"
LOST_AT_ENCODER_TO_HOST = "encoder->host"
LOST_AT_WORKLET_TO_HOST = "worklet->host"

_STAGE_WORKLET = "worklet"
_STAGE_ENCODER = "encoder"
_STAGE_HOST = "host"


def _count(value: Any) -> int | None:
    """``None`` if untrustworthy (``bool`` included — it subclasses ``int``)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class FrameLedger:
    """Every frame count the capture chain can state. ``None`` means "not reported", never zero,
    matching :class:`~.program_analysis.CaptureIntegrity`'s convention."""

    #: Taken BEFORE ``cap_capture_length`` truncates.
    received_frames: int
    declared_frames: int | None = None
    encoded_frames: int | None = None
    capture_gaps: int | None = None
    capture_gap_frames: int | None = None

    @property
    def capture_gap_evaluated(self) -> bool:
        return self.capture_gap_frames is not None

    @property
    def balance_evaluated(self) -> bool:
        return self.declared_frames is not None or self.encoded_frames is not None

    @property
    def lost_at(self) -> tuple[str, ...]:
        """Places this capture is short, earliest first; empty when clean."""
        found: list[str] = []
        if self.capture_gap_frames:
            found.append(LOST_AT_CAPTURE_OVERRUN)
        stages = [
            (name, count)
            for name, count in (
                (_STAGE_WORKLET, self.declared_frames),
                (_STAGE_ENCODER, self.encoded_frames),
                (_STAGE_HOST, self.received_frames),
            )
            if count is not None
        ]
        for (left_name, left), (right_name, right) in zip(stages, stages[1:]):
            if left != right:
                found.append(f"{left_name}->{right_name}")
        return tuple(found)

    @property
    def balanced(self) -> bool:
        """Frame transfer balance, independent of microphone overruns."""
        return not any(name != LOST_AT_CAPTURE_OVERRUN for name in self.lost_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "received_frames": self.received_frames,
            "declared_frames": self.declared_frames,
            "encoded_frames": self.encoded_frames,
            "capture_gaps": self.capture_gaps,
            "capture_gap_frames": self.capture_gap_frames,
            "lost_at": list(self.lost_at),
        }


def reconcile_capture_frames(
    report: Mapping[str, Any] | None,
    *,
    received_frames: int,
) -> FrameLedger:
    """Pure and total: an unreadable or hostile ``report`` degrades to "nothing reported" rather
    than a fabricated discrepancy."""
    if not isinstance(report, Mapping):
        report = {}
    return FrameLedger(
        received_frames=max(0, int(received_frames)),
        declared_frames=_count(report.get(REPORT_KEY_FRAMES)),
        encoded_frames=_count(report.get(REPORT_KEY_ENCODED_FRAMES)),
        capture_gaps=_count(report.get(REPORT_KEY_CAPTURE_GAPS)),
        capture_gap_frames=_count(report.get(REPORT_KEY_CAPTURE_GAP_FRAMES)),
    )
