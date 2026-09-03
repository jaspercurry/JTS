# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end frame accounting for a measurement capture chain (#2094).

Reconciles the four counters a capture source reports (the wired recorder, or
the browser measurement page) against the frames the host actually decoded, and
names where a short capture went short.

**No thresholds: any nonzero discrepancy is a fail.** These captures feed a
deconvolution, where one missing quantum is a phase discontinuity that corrupts
the impulse response for everything after it — an error with no relation to the
fraction of frames lost, so a percentage budget (the research recommendation for
a probe TONE) would pass a 128-frame hole in a 1.4-million-frame capture, 0.009%
of the recording, while making the crossover derived from it wrong. A capture
glitch is a SPLICE, never drift (#1765): there is no small amount of splice.

Render-graph loss and count imbalance are reported SEPARATELY: frames a graph
never delivered were never handed to anyone, so every count agrees with every
other while the recording is short of wall-clock. A ledger comparing only counts
would call that capture balanced.

The received count is taken BEFORE ``deconv.cap_capture_length`` truncates;
comparing after it would refuse every capture with a normal recording tail.

This module owns arithmetic and vocabulary only. It never logs and holds no
pass/fail vocabulary of its own: the caller that already owns an integrity
vocabulary (``program_analysis._verify_capture_integrity``) maps these facts
into it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FrameLedger",
    "LOST_AT_ENCODER_TO_HOST",
    "LOST_AT_RENDER_GRAPH",
    "LOST_AT_WORKLET_TO_ENCODER",
    "LOST_AT_WORKLET_TO_HOST",
    "REPORT_KEY_ENCODED_FRAMES",
    "REPORT_KEY_FRAMES",
    "REPORT_KEY_RENDER_GAPS",
    "REPORT_KEY_RENDER_GAP_FRAMES",
    "reconcile_capture_frames",
]

#: Keys this module reads out of a capture source's own report
#: (``CaptureResult.capture_integrity``). Named constants because the source
#: and the host are separate releases: the wire spelling is a contract.
#:
#: This is the READ set, not the whole report. A source may also send
#: ``blocks``, ``silent_blocks`` and the zero-run keys (``zero_run_count``,
#: ``zero_runs``, ``zero_run_quantum``); none is reconciled here — ``blocks`` is
#: ``frames`` over the render quantum, a restatement rather than an independent
#: count; ``silent_blocks`` is already inside ``block_gap_frames`` and counts
#: only the callbacks handed NO input array, so it says nothing about the
#: zero-FILLED array a FIFO resync delivers (#2557); and the zero-run keys are
#: disclosure nothing has chosen to grade yet.
REPORT_KEY_FRAMES = "frames"
REPORT_KEY_ENCODED_FRAMES = "encoded_frames"
REPORT_KEY_RENDER_GAPS = "block_gaps"
REPORT_KEY_RENDER_GAP_FRAMES = "block_gap_frames"

#: Where the frames went. The render-graph loss is a LAYER (they were never
#: delivered); the others are SEGMENTS between two counters, spelled as the pair
#: they lie between so a new counter can be inserted without renaming an old
#: finding.
LOST_AT_RENDER_GRAPH = "render_graph"
LOST_AT_WORKLET_TO_ENCODER = "worklet->encoder"
LOST_AT_ENCODER_TO_HOST = "encoder->host"
LOST_AT_WORKLET_TO_HOST = "worklet->host"

_STAGE_WORKLET = "worklet"
_STAGE_ENCODER = "encoder"
_STAGE_HOST = "host"


def _count(value: Any) -> int | None:
    """A non-negative frame count from an untrusted wire value, or ``None``.

    Anything that is not a plain non-negative ``int`` is treated as NOT REPORTED
    rather than coerced, so a count this module cannot trust leaves its check
    unevaluated instead of manufacturing a discrepancy. ``bool`` is rejected
    explicitly because ``True`` is an ``int`` and would count as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class FrameLedger:
    """Every frame count the capture chain can state, and what they add up to.

    ``received_frames`` is the host's own count and is always present. Every
    other field is ``None`` when the source did not report it; ``None`` means
    "not reported", never zero — the same convention
    :class:`~.program_analysis.CaptureIntegrity` uses, so a count nobody took
    cannot read as a clean one.
    """

    #: Frames in the decoded capture as handed to the analyzer, taken BEFORE
    #: ``cap_capture_length`` truncates.
    received_frames: int
    #: Frames the source's recorder accumulated.
    declared_frames: int | None = None
    #: Frames the source handed the WAV encoder.
    encoded_frames: int | None = None
    #: How many separate render-graph discontinuities the recorder saw.
    render_gaps: int | None = None
    #: How many frames those discontinuities account for.
    render_gap_frames: int | None = None

    @property
    def render_gap_evaluated(self) -> bool:
        """True when the source reported render-graph continuity at all."""
        return self.render_gap_frames is not None

    @property
    def balance_evaluated(self) -> bool:
        """True when the source declared at least one count to compare against.

        One is enough: a source that declares only what it encoded still lets
        the host check that everything it encoded arrived.
        """
        return self.declared_frames is not None or self.encoded_frames is not None

    @property
    def lost_at(self) -> tuple[str, ...]:
        """Every place this capture is short, earliest first; empty when clean.

        Both questions are reported, never collapsed — see the module docstring.
        """
        found: list[str] = []
        # POOLED: the recorder's arithmetic hole opens both for a skipped render
        # quantum and for input starvation, and the source reports ONE frame
        # count for the pair, so a second location name here would be a
        # distinction this number cannot support.
        if self.render_gap_frames:
            found.append(LOST_AT_RENDER_GRAPH)
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
        """True when every reported count agrees with its neighbour.

        Deliberately silent about render-graph loss: a capture whose graph
        dropped a quantum IS balanced by this measure, which is why
        :attr:`lost_at` reports the two separately.
        """
        return not any(name != LOST_AT_RENDER_GRAPH for name in self.lost_at)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe record for a sidecar / durable evidence payload."""
        return {
            "received_frames": self.received_frames,
            "declared_frames": self.declared_frames,
            "encoded_frames": self.encoded_frames,
            "render_gaps": self.render_gaps,
            "render_gap_frames": self.render_gap_frames,
            "lost_at": list(self.lost_at),
        }


def reconcile_capture_frames(
    report: Mapping[str, Any] | None,
    *,
    received_frames: int,
) -> FrameLedger:
    """Reconcile a capture source's frame counters against what the host holds.

    ``report`` is ``CaptureResult.capture_integrity`` — the source's own
    per-take account, or ``None``. ``received_frames`` is the host's count of
    the decoded capture, taken before any truncation.

    Pure and total: an unreadable or hostile report degrades to "nothing
    reported" rather than to a fabricated discrepancy.
    """
    if not isinstance(report, Mapping):
        report = {}
    return FrameLedger(
        received_frames=max(0, int(received_frames)),
        declared_frames=_count(report.get(REPORT_KEY_FRAMES)),
        encoded_frames=_count(report.get(REPORT_KEY_ENCODED_FRAMES)),
        render_gaps=_count(report.get(REPORT_KEY_RENDER_GAPS)),
        render_gap_frames=_count(report.get(REPORT_KEY_RENDER_GAP_FRAMES)),
    )
