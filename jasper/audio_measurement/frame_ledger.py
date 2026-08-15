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

**Hop A cannot be closed by COUNTERS — but since 2026-08-15 its DATA witnesses
itself.** A drop or resync inside the browser's microphone FIFO delivers an
unbroken run of quanta whose CONTENT has a discontinuity; no Web Audio surface
exposes it, so no counter in this ledger can see it, and every count above goes
on agreeing with every other. That much is unchanged. What changed is that the
discontinuity no longer has to be inferred: issue #2557's verdict (2026-08-15)
found that all 13 testable glitch events of that campaign carried exactly one run
of 128 consecutive digital-zero samples beginning at an index ≡ 0 mod 128 — one
render quantum, block-aligned, written into a live room's noise floor — while 0
of 3 clean controls carried any such run (their longest natural zero run: 13
samples). The capture page now scans its assembled buffer for that shape before
upload (``scanZeroFillRuns`` in ``capture-page/js/capture-integrity.js``) and
reports ``zero_run_count`` / ``zero_runs`` alongside the counts this module
reconciles, so hop A's own signature arrives WITH the capture instead of being
reconstructed from WAV forensics weeks later.

This module reads none of those keys, and that is a boundary rather than an
oversight: they are DISCLOSURE, and nothing here grades them. The analyzer's own
splice detectors stay the thing that refuses such a take, and they still earn
their place — a FIFO that
drops samples rather than inserting silence splices the content without writing
any zeros, so no scan of the data can witness that half. A splice those detectors
find while this ledger balances AND the reported zero-run count is 0 therefore
remains exactly the finding it always was: the loss is upstream of the audio
graph, and it did not arrive as a silent quantum.

**Hop B is the LEADING CANDIDATE for the 2026-08-03 shape, and is now counted
exactly.** The worklet checks that ``currentFrame`` advances by exactly the
previous quantum's length, so a skipped quantum leaves an arithmetic hole. What
the record does not support is calling hop B the located cause: #2094's issue body
and the #2083 forensics both say the browser audio graph and the mic's USB link
"cannot be separated" by signal analysis, and these counters postdate the
incident, so no capture from that session was ever counted. The unseparated rival
is hop A — the one hop above that no counter reaches. That asymmetry is usable
rather than merely regrettable: a FUTURE capture whose ledger balances, whose
render gap is zero, and in which the analyzer still finds a splice is evidence
FOR hop A, because every hop this module can count has just said it lost nothing.

That prediction was tested on 2026-08-15 and returned hop A — by direct data
witness (the 128-sample zero run above), which is stronger than the elimination
argument sketched here. It settles that campaign's events, NOT this paragraph's:
the 2026-08-03 captures predate the scan, their audio has since rotated out of
the retained dump, and nothing bit-exact was ever run on them. So hop B stays the
leading candidate for the 2026-08-03 shape on the same evidence as before, and
"leading candidate" stays the strongest word the record supports.

Note what a hop-B loss does NOT do to the count comparison: the skipped frames
were never handed to anyone, so ``frames``, ``encoded_frames`` and the host's
received count all agree with each other while being short of wall-clock. A ledger
that only compared counts would have called that capture balanced. It is reported
separately for exactly that reason.

**Hop G is a legitimate transform and is deliberately outside the ledger.**
``analyze_program_capture`` truncates an over-long capture before any full-rate
FFT. The received count this module grades is taken BEFORE that truncation;
comparing after it would refuse every capture with a normal recording tail.

**No thresholds, and this is STRICTER than the repo's own prior art — stated, not
silent.** Any nonzero discrepancy is a fail. The researched recommendation
(``docs/research/2026-05-27-room-correction-research/raw/mobile-browser-audio-reliability.md``,
"Dropout / glitch detection") is "any dropped frames → WARN; > 1 % → FAIL", and
that is right for what it grades: a pre-flight PROBE TONE, where a scattered lost
frame degrades a level/SNR estimate roughly in proportion to how much was lost, so
a percentage budget buys tolerance cheaply. This instrument grades the measurement
sweep itself, which is consumed by a deconvolution — a single missing quantum is a
phase discontinuity that corrupts the impulse response for everything after it,
and the resulting error has no relation to the fraction of frames lost. A 128-frame
hole in a 1.4-million-frame capture is 0.009% of the recording and would pass a 1%
budget while making the crossover derived from it wrong. That is #1765's banked
lesson exactly: a capture glitch is a SPLICE, never drift, and an epsilon fitted on
a glitch is an artefact. So there is no small amount of splice, no number to tune,
and no policy knob to get wrong — at the cost of refusing takes a percentage budget
would have kept.

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
#:
#: This is the READ set, not the whole report. The page also sends ``blocks``,
#: ``silent_blocks`` and the zero-run keys (``zero_run_count``, ``zero_runs``,
#: ``zero_run_quantum``); none is reconciled here — ``blocks`` is ``frames`` over
#: the render quantum, a redundant restatement rather than an independent count;
#: ``silent_blocks`` is already inside ``block_gap_frames`` (see
#: :attr:`FrameLedger.lost_at`), and counts only the render callbacks handed NO
#: input array, so it says nothing about the zero-FILLED array a FIFO resync
#: delivers (#2557); and the zero-run keys are hop-A DISCLOSURE that nothing has
#: chosen to grade yet. A constant for a key nothing reads would make this
#: comment false, so none has one until something reconciles it.
REPORT_KEY_FRAMES = "frames"
REPORT_KEY_ENCODED_FRAMES = "encoded_frames"
REPORT_KEY_RENDER_GAPS = "block_gaps"
REPORT_KEY_RENDER_GAP_FRAMES = "block_gap_frames"

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
        # POOLED, and the name is the coarser of the two things it books. The
        # worklet's arithmetic hole opens for a skipped render quantum AND for
        # input starvation (a `process()` call with no input channel leaves the
        # expected-next frame where it was, so the next real quantum reads as a
        # gap). Both are frames that never reached the recording, so the verdict
        # is right either way — but the page reports ONE pooled frame count for
        # them, so a second location name here would be a distinction this
        # number cannot support. `silent_blocks` rides the sidecar untouched for
        # a reader who needs to tell them apart.
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
