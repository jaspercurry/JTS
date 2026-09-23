# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared root, skip gate and sweep anchors for the cdhorn capture corpus.

The 2026-07-24/25 cdhorn corpus is laptop-durable and **gitignored**, so every
test that reads it is env-gated and skips cleanly in CI.

Every reader anchors on the sweep it deconvolves (:func:`sweep_anchor`) rather
than cross-correlating the WHOLE composed program, because the latter makes an
archived reading depend on where every other segment sits — so an unrelated
composer edit silently re-reads historical evidence (#1879). The larger,
program-global form of the same hazard is :func:`sweep_anchored_global_offset`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest


# Resolved repo-root-relative by default so a normal clone needs no setup; a
# *worktree* checkout has no captures/ of its own and points at the main
# checkout's copy with ``JTS_FLAT_LIN_CORPUS=<dir>``. No absolute path is
# committed — one machine's home directory is not a contract, and a stale one
# skips silently instead of failing.
_CDHORN_ENV = os.environ.get("JTS_FLAT_LIN_CORPUS", "").strip()
CDHORN_ROOT = (
    Path(_CDHORN_ENV)
    if _CDHORN_ENV
    else Path(__file__).resolve().parents[1]
    / "captures"
    / "flat-linearization-20260725"
    / "cdhorn-live-session"
)
CDHORN_CALIBRATION = CDHORN_ROOT.parent / "umik2-cal" / "umik2-b7343c0c625b.txt"

requires_cdhorn = pytest.mark.skipif(
    not CDHORN_ROOT.is_dir(),
    reason=(
        f"laptop-durable capture corpus absent: {CDHORN_ROOT} "
        "(set JTS_FLAT_LIN_CORPUS to point at it)"
    ),
)

# DELIBERATELY "correction", which is NOT what that file is.
#
# CDHORN_CALIBRATION resolves to umik2-b7343c0c625b.txt, which is
# byte-identical to the raw miniDSP 0-degree file, i.e. the MIC'S RESPONSE, and
# the product negates it (jasper.audio_measurement.calibration.SUPPORTED_MODELS,
# fixed 2026-07-27). The 2026-07-24/25 analyses were performed on the parser's
# "correction" default, so every pinned number downstream of this module is
# that analysis -- including PR-L3's L3_RUN5_* pins in
# tests/test_audio_measurement_program_analysis.py. The re-baseline is tracked
# as jaspercurry/JTS#1774. Named here, once, so flipping it later is one edit
# and so no call site inherits a convention silently.
CORPUS_CALIBRATION_SIGN_CONVENTION = "correction"


def _offset_of(captured: np.ndarray, reference: np.ndarray) -> int:
    n_fft = 1 << (captured.size + reference.size - 1).bit_length()
    cross = np.fft.rfft(captured, n_fft) * np.conj(np.fft.rfft(reference, n_fft))
    window = max(1, captured.size - reference.size // 2)
    return int(np.argmax(np.abs(np.fft.irfft(cross, n_fft)[:window])))


def sweep_anchor(captured: np.ndarray, segment: Any) -> int:
    """Where ``segment``'s own stimulus sits inside an archived capture.

    **Why not the whole rendered program** (which is what this reader used
    until 2026-07-27): the deconvolution only ever needs the SWEEP's position,
    but correlating the ENTIRE composed program against the capture makes the
    registration depend on every other segment's placement — so an unrelated
    edit to the composer silently re-reads archived evidence.

    Note precisely what the §2.5 courtesy-tone move did and did not do. It did
    NOT move the sweep: ``sweep_verify.start_sample`` was 369324 under both of
    THOSE compositions, because relocating the prelude from the head of the
    program to just before the sweep is a PERMUTATION of the head block, not a
    shift. What it changed is the shape the whole-program correlation matches
    against, and under that old anchor the registration disagreed with the
    sweep-only one by 1-5 samples on 8 of the corpus's 26 captures.

    **Later composer edits did shift it, which is the case this function
    exists for.** #1816's beeps-first reorder (2026-07-28) inserted a 1.0 s
    pre-pilot ``ambient`` window ahead of the pilots — a genuine
    48000-sample INSERTION, not a permutation — and the 2026-08-18 prelude
    trim then took the 172800-sample prelude back off this phase, because a
    prompted position no longer opens a session
    (``crossover_v2.programs.courtesy_prelude_for_phase``). That prelude length
    is fixed and crossover-independent, so ``sweep_verify`` starts at
    417324 − 172800 = **244524** under today's composer against 369324 in every
    archived 2026-07-24/25 capture. A reader that registers on the archived program but takes
    ``start_sample`` from a freshly composed one therefore deconvolves into the
    wrong part of a 6.0 s sweep — 1.0 s late before the trim, 2.6 s early
    after it; issue #1879 is the trace of a reader that did. Anchoring here is
    immune by construction — the sweep is located by its own waveform, so
    where the composer puts it cannot matter.
    """
    from jasper.audio_measurement.program import segment_stimulus

    stimulus = np.asarray(segment_stimulus(segment), dtype=np.float64)
    return _offset_of(captured, stimulus)


def sweep_anchored_global_offset(captured: np.ndarray, segment: Any) -> int:
    """A PROGRAM-GLOBAL offset for an archived capture, derived from one
    sweep's own registration — what a whole-program analysis needs when the
    composer has moved on.

    A single-segment reader wants :func:`sweep_anchor` (the sweep's absolute
    position). A reader that hands the capture to
    ``program_analysis.analyze_program_capture`` needs the offset that
    function's own ``_global_offset`` would have produced, because every
    segment is then located as ``global_offset + segment.start_sample``.

    Those two are not the same hazard, and the difference is large. Production
    ``_global_offset`` locates the program's FIRST STIMULUS SEGMENT and
    subtracts its schedule position — and 2026-07-27's courtesy-tone move
    (#1771, ``_insert_courtesy_prelude``) relocated the prelude from the head
    of the program to just ahead of the first sweep, which moves the PILOT
    PAIR (the first stimulus) by the prelude's whole length while leaving the
    sweeps exactly where they were. Registering a cross-era MEASURE capture
    that way lands **172 781 samples (3.6 s)** off on this corpus, and the
    analysis degrades to ``residual_desync``. This is a REPLAY hazard only:
    live analysis composes and plays the same program, so its first stimulus
    is where it says it is.

    Anchoring on a sweep and subtracting that sweep's (unchanged) schedule
    position recovers the offset the capture was recorded under, and every
    segment downstream still shares one anchor — so branch-to-branch relative
    timing is untouched.
    """
    return sweep_anchor(captured, segment) - segment.start_sample


