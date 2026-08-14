# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end frame accounting for the phone-mic capture chain (issue #2094).

**The defect.** On 2026-08-03 a MEASURE session lost frames in discrete steps of
almost exactly 128 — one Web Audio render quantum — five times across two
captures, and the only reason anyone knows is that somebody ran sample-level WAV
forensics afterwards. Nothing in the chain said a word at the time. A capture
glitch is a SPLICE, never drift (#1765), so the loss is not recoverable by
fitting: the capture is simply wrong, and the household's only correct action is
to take it again. That decision needs the loss to be a REPORTED fact at capture
time, which is what this module makes it.

**The chain, and where a frame can be counted exactly.** Left to right, from the
microphone to the array the analyzer operates on:

===  ==========================================  ====================  ==========
hop  what happens                                counted by            exact?
===  ==========================================  ====================  ==========
A    mic → the browser's own capture FIFO →      nothing               **no**
     ``MediaStreamAudioSourceNode``
B    render graph hands quanta to the worklet    ``block_gap_frames``  yes
C    worklet quanta → one ``Float32Array``,      ``frames`` vs         yes
     transferred to the page                     ``encoded_frames``
D    ``float32ToWavBlob`` → 16-bit mono WAV      \\                     yes
E    AES-256-GCM → relay → Pi → decrypt          ``encoded_frames``    yes
                                                 vs ``received``
F    ``scipy.io.wavfile.read`` → samples         /                     yes
G    ``deconv.cap_capture_length`` truncation    NOT COMPARED          n/a
===  ==========================================  ====================  ==========

The count comparison at D–F spans all three as ONE bracket, and that is exactly
as strong as three separate ones would be: none of the three resamples, so a
frame in must be a frame out. D writes a 44-byte header plus ``frameCount * 2``
bytes; E is byte-transparent under an AES-GCM tag and a SHA-256 the relay client
verifies, so a truncated transfer raises rather than arriving short; F reads that
same frame count back and takes channel 0 of a multichannel file without changing
it. There is no counter to put between them because there is nothing between them
that could change a count without failing loudly first.

**Hop A is the one that cannot be closed, and saying so is the point.** A drop or
resync inside the browser's microphone FIFO delivers an unbroken run of quanta
whose CONTENT has a discontinuity; no Web Audio surface exposes it, so no counter
here can see it. That hop keeps the analyzer's own splice detectors as its
backstop, and a splice those find while this ledger balances is itself the
finding: it places the loss upstream of the audio graph.

**Hop B is where the 2026-08-03 shape lives, and it is exact.** The worklet checks
that ``currentFrame`` advances by exactly the previous quantum's length, so a
skipped quantum leaves an arithmetic hole. Note what this does NOT do to the
count comparison: the skipped frames were never handed to anyone, so ``frames``,
``encoded_frames`` and the host's received count all agree with each other while
being short of wall-clock. A ledger that only compared counts would have called
that capture balanced. It is reported separately for exactly that reason.

**Hop G is a legitimate transform and is deliberately outside the ledger.**
``analyze_program_capture`` truncates an over-long capture before any full-rate
FFT. The received count this module grades is taken BEFORE that truncation;
comparing after it would refuse every capture with a normal recording tail.

**No thresholds.** Any nonzero discrepancy is a fail. A 128-frame hole in a
1.4-million-frame capture is 0.009% of the recording and destroys phase coherence
for everything after it — there is no small amount of splice, so there is no
number to tune and no policy knob to get wrong.

This module owns arithmetic and vocabulary only. It never logs, never decides a
verdict's severity, and holds no pass/fail vocabulary of its own: the caller that
already owns an integrity vocabulary (``program_analysis._verify_capture_integrity``)
maps these facts into it.
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
    "REPORT_KEY_BLOCKS",
    "REPORT_KEY_ENCODED_FRAMES",
    "REPORT_KEY_FRAMES",
    "REPORT_KEY_RENDER_GAPS",
    "REPORT_KEY_RENDER_GAP_FRAMES",
    "reconcile_capture_frames",
]

#: Keys this module reads out of the capture page's own report
#: (``CaptureResult.capture_integrity`` — the object
#: ``capture-page/js/capture-integrity.js`` builds). Named constants because the
#: page and the host are separate releases: the wire spelling is a contract, not
#: an implementation detail, and it should be greppable from both sides.
REPORT_KEY_FRAMES = "frames"
REPORT_KEY_ENCODED_FRAMES = "encoded_frames"
REPORT_KEY_RENDER_GAPS = "block_gaps"
REPORT_KEY_RENDER_GAP_FRAMES = "block_gap_frames"
REPORT_KEY_BLOCKS = "blocks"

#: Where the frames went, named so a reader never has to infer the layer from a
#: number. Hop B's loss is a LAYER (the render graph never delivered them); the
#: others are SEGMENTS between two counters, spelled as the pair they lie
#: between so a new counter can be inserted without renaming an old finding.
LOST_AT_RENDER_GRAPH = "render_graph"
LOST_AT_WORKLET_TO_ENCODER = "worklet->encoder"
LOST_AT_ENCODER_TO_HOST = "encoder->host"
LOST_AT_WORKLET_TO_HOST = "worklet->host"

_STAGE_WORKLET = "worklet"
_STAGE_ENCODER = "encoder"
_STAGE_HOST = "host"


def _count(value: Any) -> int | None:
    """A non-negative frame count from an untrusted wire value, or ``None``.

    The report is phone-supplied JSON that the relay never parses, so every
    field arrives as whatever the page (or anything posing as it) chose to send.
    Anything that is not a plain non-negative ``int`` is treated as NOT REPORTED
    rather than coerced: a count this module cannot trust must leave its check
    unevaluated, never manufacture a discrepancy. ``bool`` is rejected
    explicitly because ``True`` is an ``int`` and would silently count as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class FrameLedger:
    """Every frame count the capture chain can state, and what they add up to.

    ``received_frames`` is the host's own count — the length of the decoded
    array the analyzer was handed — and is always present, because the host can
    always count what it holds. Every other field is ``None`` when the capture
    page did not report it: an older page build, or the single-capture (non-plan)
    runner, which posts no report at all. ``None`` means "not reported" and never
    zero, which is the same convention :class:`~.program_analysis.CaptureIntegrity`
    uses and for the same reason — a count nobody took must not read as a clean
    one.
    """

    #: Frames in the decoded capture as handed to the analyzer, taken BEFORE
    #: ``cap_capture_length`` truncates (hop G above).
    received_frames: int
    #: Frames the browser's recorder worklet accumulated (hop C's left edge).
    declared_frames: int | None = None
    #: Frames the page handed the WAV encoder (hop C's right edge / hop D's
    #: input).
    encoded_frames: int | None = None
    #: How many separate render-graph discontinuities the worklet saw.
    render_gaps: int | None = None
    #: How many frames those discontinuities account for (hop B).
    render_gap_frames: int | None = None

    @property
    def render_gap_evaluated(self) -> bool:
        """True when the page reported render-graph continuity at all."""
        return self.render_gap_frames is not None

    @property
    def balance_evaluated(self) -> bool:
        """True when the page declared at least one count to compare against.

        One is enough: a page that declares only what it encoded still lets the
        host check that everything it encoded arrived.
        """
        return self.declared_frames is not None or self.encoded_frames is not None

    @property
    def lost_at(self) -> tuple[str, ...]:
        """Every place this capture is short, earliest first; empty when clean.

        Both questions are reported, never collapsed: hop B's loss leaves the
        counts agreeing with each other (see the module docstring), so a ledger
        that returned one answer would have to choose which truth to hide.
        """
        found: list[str] = []
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

        Deliberately silent about hop B — a capture whose render graph dropped a
        quantum IS balanced by this measure, which is the whole reason
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
    """Reconcile the capture page's frame counters against what the host holds.

    ``report`` is ``CaptureResult.capture_integrity`` — the page's own per-take
    account, or ``None`` from any capture that carries none. ``received_frames``
    is the host's count of the decoded capture, taken before any truncation.

    Pure and total: no exception path, no I/O, and an unreadable or hostile
    report degrades to "nothing reported" rather than to a fabricated
    discrepancy. A diagnostic that can refuse a good capture by mis-reading its
    own metadata is worse than no diagnostic.
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
