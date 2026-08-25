# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared loaders for the flat-linearization capture corpora.

The 2026-07-25 S0 session and the 2026-07-24/25 cdhorn corpus are
laptop-durable and **gitignored**, so every test that reads them is
env-gated and skips cleanly in CI. Several test modules now need the same
session directories — :mod:`tests.test_spatial_combine` (the detector and
geometry acceptance), :mod:`tests.test_interference_nulls` (the null gate's)
and :mod:`tests.test_active_speaker_linearization_envelope` (the correction
doctrine's cloud terms) — so the loading lives here rather than in any of
them: one deconvolution chain, one place for it to be era-exact.

**Era-exact** is the load-bearing property. These WAVs were captured by
``captures/flat-linearization-20260725/s0-kit/s0_capture.py`` against a
program the Pi rendered at the time; the analysis kit
(``s0-kit/analyze_s0.py``) is the authority on what that program was, and
:func:`s0_position_irs` mirrors it rather than re-deriving the chain — same
``build_verify_program``, same pilot/prelude constants read **live** from
``crossover_v2_flow`` (so this tracks the shipped defaults instead of
freezing a copy), same ``fc_hz`` from the session's own ``session.json``,
same ``_deconvolve_window`` on the ``sweep_verify`` segment.

The ALIGNMENT is deliberately no longer "same as the kit" (2026-07-27):
every reader here anchors on the sweep it deconvolves
(:func:`sweep_anchor`) rather than cross-correlating the WHOLE composed
program, because the latter makes an archived reading depend on where every
other segment sits — so an unrelated composer edit silently re-reads
historical evidence. See that function for the measurement that motivated
the change and what it moved.

The same shape SURVIVED, deliberately, in ``tests/test_spatial_combine``'s
bespoke cdhorn loader until 2026-07-29, where it registered against the
archived program WAV but took ``start_sample`` from a freshly composed one.
That was measured benign at the time (PR-L3 review, 2026-07-27, within
**2 samples** of the sweep anchor) with an explicit caveat that "a future
composer edit that DOES disturb it has somewhere to be traced from".

**That edit arrived the next day** (#1816, beeps-first + a 1.0 s pre-pilot
ambient window) and moved ``sweep_verify.start_sample`` 369324 -> 417324,
which placed that loader's deconvolution window 1.0 s late into a 6.0 s
sweep and silently moved two pinned corpus readings. The caveat did its
job — issue #1879 is the trace — and the conclusion is that the exemption
was not worth its cost: **every** reader of these corpora now anchors on
:func:`sweep_anchor`, and there is no second registration convention left
to reason about. The larger, program-global form of the same hazard is
:func:`sweep_anchored_global_offset`.

**Re-deriving a pinned reading?** The procedure (reproduce, classify
detector-vs-reading, bisect, record era) is documented once, in
``tests.test_spatial_combine.test_band_deficit_separates_honest_captures_from_stopband_residue``'s
docstring -- it governs every population read through this module,
not just that test's own.

The 2026-08-02 re-pin era (#2045)
---------------------------------

Every corpus reading that moved **since the 2026-07-27 re-pin** (PR-U1's
corpus-reader alignment fix, the era recorded in :func:`sweep_anchor`) moved
for ONE reason, established by ablation rather than assumed: **PR #1991**
(``540d169f5``, "a crossing must stand out to become a window bound") added
the prominence vote to the first-reflection detector.

The window matters. These pins were originally set at PR #1753
(``2a7060ab1``), but PR-U1 already re-derived a subset of them on 2026-07-27,
so "everything since #1753" would cover two separate causes. This section is
scoped to the later one; a value carrying a ``RE-PINNED 2026-07-27`` note
beside it belongs to the earlier.

On this corpus that vote changes exactly one capture. ``cloud_04`` alone used
to report ``floor_source="measured_reflection"`` at a **1777.8 Hz** validity
floor -- a reflection "found" 3 samples past the search-window open, which is
precisely the field instance #1991 names as its motivation (**#1790**). Under
the vote that crossing is voted down, no later crossing qualifies, and
``cloud_04`` gates like its nine neighbours: ``floor_source="search_span_bound"``
at **142.857 Hz**. So these pins were holding a detector artifact in place, and
the fix -- not a regression -- is what moved them.

**The ablation is the evidence, and it is reproducible.** Disabling only the
vote (``prominence_db=0``, which :func:`jasper.audio_measurement.gating.detect_first_reflection`
documents as an exact recovery of the pre-vote detector) restores every
pre-#1991 reading with the rest of the tree at HEAD. The control rides in the
data itself: groupings that do NOT contain ``cloud_04`` -- ``hand_width_low``
(cloud_07-10), ``desk_edge``, ``ground_plane`` -- are byte-identical across the
change, and only ``main`` and ``tweeter_height`` moved.

Reproduce (both roots must be reachable; these tests skip otherwise)::

    JTS_FLAT_LIN_S0=<captures>/flat-linearization-20260725 \\
    JTS_FLAT_LIN_CORPUS=<captures>/flat-linearization-20260725/cdhorn-live-session \\
      .venv/bin/pytest tests/test_spatial_combine.py \\
        tests/test_interference_nulls.py \\
        tests/test_active_speaker_linearization_envelope.py \\
        tests/test_crossover_v2_cloud_pipeline.py \\
        tests/test_flat_spec_ssot.py tests/test_flat_spec.py

These readings are **deterministic**: three runs (a repeat in the same session,
and one under ``OMP_NUM_THREADS=1``) agree bit-for-bit, so the pins keep their
original decimal precision rather than widening into a tolerance.

A re-pin from this era is tagged ``RE-PINNED 2026-08-02 (#2045)`` at its call
site. That tag is deliberate: jaspercurry/JTS#1774's cal-sign re-baseline will
re-derive these same numbers again, and it needs to tell this pass's values
from the 2026-07-27 (PR-U1) ones sitting beside them.
"""
from __future__ import annotations

import dataclasses
import json
import os
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jasper.audio_measurement.spatial_combine import PositionCapture

# --------------------------------------------------------------------------- #
# Root
#
# Resolved repo-root-relative by default so a normal clone needs no setup; a
# *worktree* checkout has no captures/ of its own and points at the main
# checkout's copy with ``JTS_FLAT_LIN_S0=<dir>``. No absolute path is
# committed — one machine's home directory is not a contract, and a stale one
# skips silently instead of failing.
#
# The 2026-07-24/25 cdhorn corpus (``JTS_FLAT_LIN_CORPUS``) gained its second
# reader on 2026-07-27 (PR-L3's level-match frame regression, in
# test_audio_measurement_program_analysis.py, alongside test_spatial_combine's
# detector acceptance). Its ROOT and SKIPIF live here now — the two consumers
# must agree on where the corpus is and when to skip — while each keeps its
# own loader, because they read different phases with different program
# parameters. That split is the point: shared identity, per-consumer reading.
# --------------------------------------------------------------------------- #

_S0_ENV = os.environ.get("JTS_FLAT_LIN_S0", "").strip()
S0_ROOT = (
    Path(_S0_ENV)
    if _S0_ENV
    else Path(__file__).resolve().parents[1] / "captures" / "flat-linearization-20260725"
)
S0_GROUND_PLANE = S0_ROOT / "s0-session-groundplane"
S0_MAIN = S0_ROOT / "s0-session-main"
S0_DESK_EDGE = S0_ROOT / "s0-session-deskedge"
S0_LOOPBACK = S0_ROOT / "s0-analysis" / "loopback"
S0_CALIBRATION = S0_ROOT / "umik2-cal" / "umik2-b7343c0c625b.txt"

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
# Governs BOTH corpora: S0_CALIBRATION and CDHORN_CALIBRATION resolve to the
# same umik2-b7343c0c625b.txt, which is byte-identical to the raw miniDSP
# 0-degree file, i.e. the MIC'S RESPONSE, and the product negates it
# (jasper.audio_measurement.calibration.SUPPORTED_MODELS, fixed 2026-07-27).
# The 2026-07-24/25 analyses were performed on the parser's "correction"
# default, so every pinned number downstream of this module is that analysis
# -- including PR-L3's L3_RUN5_* pins in
# tests/test_audio_measurement_program_analysis.py.
# Correcting the sign fails 19 tests across five files (measured 2026-07-27,
# both roots reachable) and is not all numeric drift: the S0 desk-edge rung 4
# flips position_dependent -> position_invariant, silently retiring the corpus
# instance test_the_report_roll_up_is_the_conservative_one is built on, and
# the cloud-pipeline carve-out grows an extra identified_null row (a SHAPE
# change, test_crossover_v2_cloud_pipeline.py:1151). That
# re-baseline is a deliberate change of its own, taken with the whole
# linearization ladder's analysis fixes in view rather than as a side effect
# of the input fix -- tracked as jaspercurry/JTS#1774, which carries the full
# cost list. Named here, once, so flipping it later is one edit and so no call
# site inherits a convention silently.
#
# Note for anyone comparing outputs: the laptop-side S0 kit
# (captures/flat-linearization-20260725/s0-kit/analyze_s0.py) WAS fixed and now
# analyzes these same sessions under the OPPOSITE convention. A kit-vs-corpus
# discrepancy in the tweeter band is that difference, not a bug in either.
CORPUS_CALIBRATION_SIGN_CONVENTION = "correction"

requires_s0 = pytest.mark.skipif(
    not (S0_GROUND_PLANE.is_dir() and S0_MAIN.is_dir() and S0_LOOPBACK.is_dir()),
    reason=(
        f"laptop-durable S0 session absent under {S0_ROOT} "
        "(set JTS_FLAT_LIN_S0 to point at it)"
    ),
)
# The magnitude path needs the UMIK-2 calibration the session was captured
# with, and the desk-edge leg the null gate's four-way calibration reads —
# and does *not* need the electrical loopback. A separate gate rather than an
# extra condition on ``requires_s0``, so each test skips on exactly what it
# would have used and a tree missing one artifact does not silently take out
# tests that never touch it.
requires_s0_curves = pytest.mark.skipif(
    not (
        S0_GROUND_PLANE.is_dir()
        and S0_MAIN.is_dir()
        and S0_DESK_EDGE.is_dir()
        and S0_CALIBRATION.is_file()
    ),
    reason=(
        f"S0 sessions or the UMIK-2 calibration absent under {S0_ROOT} "
        "(set JTS_FLAT_LIN_S0 to point at it)"
    ),
)

# The declared passbands the S0 legs would carry from a driver contract: a
# summed VERIFY capture spans the whole swept system band, the electrical
# loopback's two branches their own crossover halves. Supplied by the tests
# because the modules under test are product-blind and never guess one.
S0_SUMMED_PASSBAND_HZ = (150.0, 20_000.0)
LOOPBACK_WOOFER_PASSBAND_HZ = (200.0, 2000.0)
LOOPBACK_TWEETER_PASSBAND_HZ = (2500.0, 20_000.0)
# The leg-B protocol window S0 actually searched (analyze_s0.py's
# LEG_B_ECHO_SEARCH_US). It is NOT the module default, and the difference is
# the point of two of the tests in test_spatial_combine.py.
S0_PROTOCOL_SEARCH_US = (150.0, 1000.0)

# The S0 main leg's ten positions split by mic height. The S0 report's own
# split (REPORT.md § Q2): 01-06 at tweeter height, 07-10 a hand-width lower.
# The measurement confirms it independently — the 1.8 kHz dip reads
# 10.2-14.6 dB on 01-06 and 4.0-4.6 dB on 07-10, "a clean break at 07".
S0_MAIN_TWEETER_HEIGHT = tuple(f"cloud_0{i}" for i in range(1, 7))
S0_MAIN_HAND_WIDTH_LOW = ("cloud_07", "cloud_08", "cloud_09", "cloud_10")


def _load_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path)) as handle:
        channels = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return samples[::channels] if channels > 1 else samples


def _session_program(session_dir: Path) -> tuple[Any, Any, int, float]:
    """``(program, sweep_verify segment, rate, fc_hz)``.

    No rendered reference PCM: every reader anchors on the sweep alone
    (:func:`sweep_anchor`), so rendering the whole ~681k-sample program on
    each call bought nothing but time.
    """
    from jasper.active_speaker.crossover_v2.programs import (
        PILOT_LEVEL_DELTA_DB,
        courtesy_prelude_for_phase,
    )
    from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_MEASURE
    from jasper.audio_measurement.program import BASE_STIMULUS_PEAK_DBFS
    from jasper.audio_measurement.program import build_verify_program

    session = json.loads((session_dir / "session.json").read_text())
    fc_hz = float(session["fc_hz"])
    program = build_verify_program(
        fc_hz,
        leading_pilot_gains_db=(
            BASE_STIMULUS_PEAK_DBFS - PILOT_LEVEL_DELTA_DB,
            BASE_STIMULUS_PEAK_DBFS,
        ),
        # The corpus is CLOUD POSITIONS, so it asks the shipped rule for that
        # phase. Which answer comes back does not move a reading either way:
        # ``sweep_anchor`` locates the sweep by cross-correlating the stimulus
        # and ignores its declared schedule position entirely — pinned by
        # ``test_sweep_anchor_owes_nothing_to_the_composers_schedule``.
        courtesy_prelude=courtesy_prelude_for_phase(PHASE_CLOUD_MEASURE),
    )
    return (
        program,
        program.segment("sweep_verify"),
        int(session["sample_rate_hz"]),
        fc_hz,
    )


def _session_groups(session_dir: Path) -> dict[str, list[tuple[int, Path]]]:
    """``{position_id: [(capture_index, wav_path), ...]}``.

    Driven off the per-capture sidecar JSONs, which is also how ``.retaken``
    WAVs are skipped: a retaken attempt has no sidecar pointing at it.
    """
    groups: dict[str, list[tuple[int, Path]]] = {}
    for sidecar in sorted(session_dir.glob("*.json")):
        if sidecar.name == "session.json":
            continue
        meta = json.loads(sidecar.read_text())
        if not meta.get("wav_path"):
            continue
        wav = session_dir / Path(meta["wav_path"]).name
        if wav.is_file():
            groups.setdefault(str(meta["position_id"]), []).append(
                (int(meta.get("capture_index", 1)), wav)
            )
    return groups


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
    after it. That is not hypothetical: it is what happened to
    ``test_spatial_combine``'s cdhorn loader, and issue #1879 is its trace.
    Anchoring here is immune by construction — the sweep is located by its own
    waveform, so where the composer puts it cannot matter.

    A 1-5 sample disagreement is mostly harmless, and the control for that is
    the pattern of which positions actually moved: a position whose two
    repeats shifted SYMMETRICALLY (``cloud_05``, ``cloud_07``, ``cloud_10``
    — the same offset on both) still averages to a bit-identical IR, while a
    position whose repeats shifted ASYMMETRICALLY averages two differently
    registered IRs together and smears the echo. Exactly the two asymmetric
    positions are the two that changed — which is the strongest evidence that
    this is a registration fix rather than the reader reading something else.

    What they changed to, on the S0 main leg::

        cloud_04   319.3 us @ 0.294   ->   315.7 us @ 0.949
        cloud_09   322.6 us @ 0.851   ->   333.4 us @ 0.852

    ``cloud_04`` is the decisive one: it was the single weakest detection in
    the S0 report's own table, low enough that ``test_interference_nulls``
    pinned it as sitting below the corroboration confidence floor, and
    de-smeared it corroborates like its neighbours. ``cloud_09``'s confidence
    is flat (0.851 → 0.852) — its tau moved without its confidence improving,
    so it is evidence that the registration changed, not that it got better.
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


def s0_position_irs(session_dir: Path) -> dict[str, np.ndarray]:
    """Era-exact deconvolution of one S0 session directory's positions.

    A position's repeats are averaged in the **time domain** — they share a
    deconvolution anchor, so the mean is valid and quieter, which is what the
    kit does too.
    """
    from jasper.audio_measurement import program_analysis as pa

    _program, segment, sample_rate, _fc = _session_program(session_dir)
    groups = _session_groups(session_dir)
    irs: dict[str, np.ndarray] = {}
    for position_id in sorted(groups):
        per_repeat = [
            pa._deconvolve_window(
                captured,
                segment,
                sweep_anchor(captured, segment),
                sample_rate,
            )[0]
            for _index, wav in sorted(groups[position_id])
            for captured in (_load_wav_mono(wav),)
        ]
        shortest = min(ir.size for ir in per_repeat)
        irs[position_id] = np.mean(
            np.vstack([ir[:shortest] for ir in per_repeat]), axis=0
        )
    return irs


def s0_position_captures(
    session_dir: Path, *, only: tuple[str, ...] | None = None
) -> list[PositionCapture]:
    """One session directory as a cloud of :class:`PositionCapture`.

    The same deconvolution as :func:`s0_position_irs`, carried one step
    further into the calibrated, gated magnitude response the kit's
    ``summed_response_db`` produces (``program_analysis._driver_response``
    with ``role="summed"``, the session's own ``fc_hz``, and the UMIK-2
    calibration the session was captured with).

    Per-position reduction matches the kit exactly and deliberately differs
    between the two domains: repeat **magnitudes** are combined as a power
    mean, repeat **IRs** as a time-domain mean. Both are the plan's "existing
    repeat/drift machinery within each position" (fundamental 1) reduced to
    its simplest honest form — average, do not cherry-pick.

    ``only`` selects a subset of ``position_id``s, which is how the S0
    report's own tweeter-height / hand-width-low split is reproduced.
    """
    from jasper.audio_measurement import program_analysis as pa
    from jasper.audio_measurement.calibration import parse_calibration_text

    calibration = parse_calibration_text(
        S0_CALIBRATION.read_text(),
        sign_convention=CORPUS_CALIBRATION_SIGN_CONVENTION,
    )
    _program, segment, sample_rate, fc_hz = _session_program(session_dir)
    groups = _session_groups(session_dir)

    captures: list[PositionCapture] = []
    for position_id in sorted(groups):
        if only is not None and position_id not in only:
            continue
        irs: list[np.ndarray] = []
        magnitudes: list[np.ndarray] = []
        freqs_ref: np.ndarray | None = None
        for _index, wav in sorted(groups[position_id]):
            captured = _load_wav_mono(wav)
            full_ir, _pre_guard = pa._deconvolve_window(
                captured,
                segment,
                sweep_anchor(captured, segment),
                sample_rate,
            )
            response = pa._driver_response(
                "summed",
                full_ir,
                sample_rate,
                calibration=calibration,
                ambient_report=None,
                fc_hz=fc_hz,
                n_fft=pa._n_fft_for(full_ir),
            )
            freqs = np.asarray(response.freqs_hz, dtype=float)
            magnitude = np.asarray(response.magnitude_db, dtype=float)
            if freqs_ref is None:
                freqs_ref = freqs
            elif not np.allclose(freqs, freqs_ref):
                magnitude = np.interp(freqs_ref, freqs, magnitude)
            irs.append(full_ir)
            magnitudes.append(magnitude)
        assert freqs_ref is not None
        shortest = min(ir.size for ir in irs)
        captures.append(
            PositionCapture(
                position_id=position_id,
                freqs_hz=freqs_ref,
                magnitude_db=10.0
                * np.log10(np.mean(np.power(10.0, np.vstack(magnitudes) / 10.0), axis=0)),
                sample_rate=sample_rate,
                ir=np.mean(np.vstack([ir[:shortest] for ir in irs]), axis=0),
            )
        )
    return captures


def s0_position_driver_response(
    session_dir: Path, position_id: str
) -> tuple[Any, float]:
    """``(DriverResponse, fc_hz)`` for ONE S0 position, repeats attached.

    The same deconvolution + calibrated gated magnitude chain
    :func:`s0_position_captures` uses, stopped one step earlier: instead of
    power-mean-averaging a position's repeat captures into a single curve,
    each capture becomes its own
    :class:`~jasper.audio_measurement.program_analysis.DriverResponse` and the
    non-first ones are attached to the first as ``repeat_responses``. That is
    the shape the correction envelope reads for its in-position repeat sigma
    (:func:`jasper.active_speaker.linearization_envelope.compute_sigma_curve`),
    which needs the occurrences separately — the averaged view cannot supply
    it.

    Every S0 position holds exactly two captures, so the returned primary
    carries one repeat. ``role`` is ``"summed"`` because that is what the
    session captured: a summed system sweep, not a per-driver one. A caller
    fitting a single driver's band out of it is making a declaration about
    the crossover, and ``fc_hz`` (read from the session's own
    ``session.json``, not assumed) is returned so it can make that
    declaration from the session rather than from a literal.
    """
    from jasper.audio_measurement import program_analysis as pa
    from jasper.audio_measurement.calibration import parse_calibration_text

    calibration = parse_calibration_text(
        S0_CALIBRATION.read_text(),
        sign_convention=CORPUS_CALIBRATION_SIGN_CONVENTION,
    )
    _program, segment, sample_rate, fc_hz = _session_program(session_dir)
    occurrences = []
    for _index, wav in sorted(_session_groups(session_dir)[position_id]):
        captured = _load_wav_mono(wav)
        full_ir, _pre_guard = pa._deconvolve_window(
            captured,
            segment,
            sweep_anchor(captured, segment),
            sample_rate,
        )
        occurrences.append(
            pa._driver_response(
                "summed",
                full_ir,
                sample_rate,
                calibration=calibration,
                ambient_report=None,
                fc_hz=fc_hz,
                n_fft=pa._n_fft_for(full_ir),
            )
        )
    primary = dataclasses.replace(
        occurrences[0],
        repeat_responses=tuple(
            dataclasses.replace(occurrence, repeat_index=index + 1)
            for index, occurrence in enumerate(occurrences[1:])
        ),
    )
    return primary, fc_hz


def loopback_irs() -> dict[str, np.ndarray]:
    """The electrical loopback's per-branch IRs, as the loopback run left
    them (``np.load``, no reconstruction — these are already impulse
    responses, one per stimulus x branch)."""
    return {
        f"{stimulus}_{branch}": np.load(S0_LOOPBACK / f"ir_{stimulus}_{branch}.npy")
        for stimulus in ("impulse", "sweep", "mls")
        for branch in ("woofer", "tweeter")
    }
