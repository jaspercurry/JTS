# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""End-to-end frame accounting for the capture chain (issue #2094).

**What was silent, in one paragraph.** On 2026-08-03 a MEASURE session lost
frames in five discrete steps of almost exactly 128 — one Web Audio render
quantum — across two captures, and the only reason anyone knows is that somebody
ran sample-level WAV forensics days later. The capture page could already
*measure* the render-graph half of that loss (#2151 shipped the counters) and
nothing on the host ever read what it said; the host could always count the
frames it received and never compared them to anything. This module's subject is
the ledger that closes both, and the first test below is the witness for what
"silent" meant.

Read :mod:`jasper.audio_measurement.frame_ledger` for the per-hop exactness
argument — which hops can be counted exactly, which one structurally cannot, and
why the truncation in ``analyze_program_capture`` is deliberately outside the
comparison.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.active_speaker.crossover_v2.contracts import CaptureValidity
from jasper.active_speaker.crossover_v2.verification import (
    CAPTURE_INTEGRITY_CLEAN,
    CAPTURE_INTEGRITY_FAILED,
    evaluate_capture_validity,
)
from jasper.audio_measurement.frame_ledger import (
    LOST_AT_ENCODER_TO_HOST,
    LOST_AT_RENDER_GRAPH,
    LOST_AT_WORKLET_TO_ENCODER,
    LOST_AT_WORKLET_TO_HOST,
    FrameLedger,
    reconcile_capture_frames,
)
from jasper.audio_measurement.program import (
    build_measure_program,
    build_verify_program,
    render_program_pcm,
)
from jasper.audio_measurement.program_analysis import (
    CAPTURE_BOUND_MARGIN_S,
    INTEGRITY_CHECK_FRAME_LEDGER,
    INTEGRITY_CHECK_RENDER_GAP,
    INTEGRITY_NOT_EVALUATED,
    INTEGRITY_PASS,
    MeasurementPriors,
    analysis_diagnostic_summary,
    analyze_program_capture,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import RoleBand

SR = 48_000
FC_HZ = 1_800.0
GLOBAL_OFFSET = 3_000
#: The Web Audio render quantum, and the exact size of the 2026-08-03 losses.
RENDER_QUANTUM = 128
_ANALYSIS_LOGGER = "jasper.audio_measurement.program_analysis"
_REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _driver_ir(seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ir = rng.normal(0.0, 1.0, 256) * np.exp(-np.arange(256) / 40.0)
    ir[0] += 1.0
    return ir / np.max(np.abs(ir))


def _synthesize(program, seed: int = 3) -> np.ndarray:
    pcm = render_program_pcm(program)
    mono = pcm.sum(axis=1) if pcm.ndim > 1 else pcm
    body = fftconvolve(mono, _driver_ir(seed))[: mono.size]
    out = np.zeros(GLOBAL_OFFSET + body.size)
    out[GLOBAL_OFFSET:] = body * 0.4
    return out


def _verify_program():
    return build_verify_program(
        FC_HZ, sweep_s=1.5, leading_pilot_gains_db=(-22.0, -12.0),
        pilot_duration_s=0.5,
    )


def _measure_program():
    return build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0},
        [
            RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
            RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
        ],
        sweep_durations={"woofer": 1.0, "tweeter": 0.8},
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )


def _excise(cap: np.ndarray, at: int, samples: int) -> np.ndarray:
    """Remove ``samples`` frames — the 2026-08-03 loss shape.

    #1971's fixture INSERTS silence, so everything after it arrives LATE. The
    forensics found the opposite sign: frames that were never delivered at all.
    """
    return np.concatenate([cap[:at], cap[at + samples:]])


def _page_report(frames: int, **overrides) -> dict:
    """What the capture page posts about a take it believes was clean."""
    report = {
        "focus_lost": False,
        "focus_losses": 0,
        "frames": frames,
        "encoded_frames": frames,
        "blocks": frames // RENDER_QUANTUM,
        "block_gaps": 0,
        "block_gap_frames": 0,
        "silent_blocks": 0,
    }
    report.update(overrides)
    return report


def _analyze(program, capture, report=None):
    return analyze_program_capture(
        program, capture, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        capture_report=report,
    )


# --------------------------------------------------------------------------- #
# the bound that makes the page counter necessary
# --------------------------------------------------------------------------- #


def test_the_analyzer_alone_cannot_see_one_lost_render_quantum():
    """THE WITNESS for what #2094 fixes, and a permanent bound.

    A 128-frame hole inside the summed sweep does not move the sweep's located
    START, so the schedule residual — the only splice instrument a one-sweep
    VERIFY program has — reads clean. This assertion is expected to keep passing
    forever: the ledger does not make the analyzer see this, it makes the
    BROWSER's report of it count. If this ever starts failing, a signal-side
    detector grew that reaches inside a sweep, and the check below is no longer
    the only thing standing between this loss and a silent adoption.
    """
    prog = _verify_program()
    sweep = prog.segment("sweep_verify")
    lossy = _excise(
        _synthesize(prog),
        GLOBAL_OFFSET + sweep.start_sample + 20_000,
        RENDER_QUANTUM,
    )
    res = _analyze(prog, lossy)
    assert res.capture_integrity is not None
    assert res.capture_integrity.failed == ()


# --------------------------------------------------------------------------- #
# the ledger itself
# --------------------------------------------------------------------------- #


def test_a_render_gap_is_reported_even_though_every_count_agrees():
    """The 2026-08-03 shape. The skipped quantum was never handed to anyone, so
    worklet, encoder and host all agree — a counts-only ledger would call this
    balanced, which is exactly why the gap is a separate question."""
    ledger = reconcile_capture_frames(
        _page_report(480_000, block_gaps=1, block_gap_frames=RENDER_QUANTUM),
        received_frames=480_000,
    )
    assert ledger.balanced is True
    assert ledger.lost_at == (LOST_AT_RENDER_GRAPH,)
    assert ledger.render_gap_frames == RENDER_QUANTUM


def test_each_broken_link_names_the_hop_it_lies_between():
    lost_in_page = reconcile_capture_frames(
        _page_report(480_000, encoded_frames=480_000 - RENDER_QUANTUM),
        received_frames=480_000 - RENDER_QUANTUM,
    )
    assert lost_in_page.lost_at == (LOST_AT_WORKLET_TO_ENCODER,)

    lost_in_transport = reconcile_capture_frames(
        _page_report(480_000), received_frames=480_000 - RENDER_QUANTUM,
    )
    assert lost_in_transport.lost_at == (LOST_AT_ENCODER_TO_HOST,)

    # A page that declares only what its worklet assembled still gets a
    # comparison — spanning both hops, and named for the span it can prove.
    worklet_only = reconcile_capture_frames(
        {"frames": 480_000}, received_frames=480_000 - RENDER_QUANTUM,
    )
    assert worklet_only.lost_at == (LOST_AT_WORKLET_TO_HOST,)


def test_both_losses_are_reported_together_earliest_first():
    ledger = reconcile_capture_frames(
        _page_report(
            480_000, block_gaps=2, block_gap_frames=256,
            encoded_frames=480_000 - 8,
        ),
        received_frames=480_000 - 8,
    )
    assert ledger.lost_at == (LOST_AT_RENDER_GRAPH, LOST_AT_WORKLET_TO_ENCODER)
    assert ledger.balanced is False


def test_a_clean_take_names_nothing():
    ledger = reconcile_capture_frames(_page_report(480_000), received_frames=480_000)
    assert ledger.lost_at == ()
    assert ledger.balanced is True
    assert ledger.render_gap_evaluated is True
    assert ledger.balance_evaluated is True


def test_an_absent_report_is_unreported_never_zero():
    """``None`` means nobody counted, and must not read as a clean count."""
    ledger = reconcile_capture_frames(None, received_frames=480_000)
    assert ledger.declared_frames is None
    assert ledger.encoded_frames is None
    assert ledger.render_gap_frames is None
    assert ledger.render_gap_evaluated is False
    assert ledger.balance_evaluated is False
    assert ledger.lost_at == ()


@pytest.mark.parametrize(
    "hostile",
    [
        {"frames": "480000"},          # a string the page never sends
        {"frames": 480_000.0},         # a float — not a frame count
        {"frames": True},              # bool is an int; would count as 1
        {"frames": -1},                # negative
        {"frames": None},
    ],
)
def test_an_untrustworthy_count_leaves_the_check_unevaluated_never_failed(hostile):
    """The report is phone-supplied JSON the capture never parses. A value this
    module cannot trust must not be coerced into a discrepancy — a diagnostic
    that refuses a good capture by misreading its own metadata is worse than no
    diagnostic at all."""
    ledger = reconcile_capture_frames(hostile, received_frames=480_000)
    assert ledger.declared_frames is None
    assert ledger.lost_at == ()


def test_a_non_mapping_report_degrades_to_unreported():
    for junk in ("not a dict", 7, [1, 2, 3]):
        ledger = reconcile_capture_frames(junk, received_frames=10)
        assert ledger.balance_evaluated is False
        assert ledger.lost_at == ()


def test_to_dict_is_json_safe_and_carries_every_count():
    ledger = reconcile_capture_frames(
        _page_report(480_000, block_gaps=1, block_gap_frames=RENDER_QUANTUM),
        received_frames=480_000,
    )
    record = json.loads(json.dumps(ledger.to_dict()))
    assert record == {
        "received_frames": 480_000,
        "declared_frames": 480_000,
        "encoded_frames": 480_000,
        "render_gaps": 1,
        "render_gap_frames": RENDER_QUANTUM,
        "lost_at": [LOST_AT_RENDER_GRAPH],
    }


def test_a_bare_ledger_reports_only_what_the_host_can_always_count():
    bare = FrameLedger(received_frames=99)
    assert bare.lost_at == ()
    assert bare.balance_evaluated is False
    assert bare.render_gap_evaluated is False


# --------------------------------------------------------------------------- #
# the verdict — the whole point
# --------------------------------------------------------------------------- #


def test_a_reported_render_gap_makes_the_capture_unusable():
    """RED→GREEN. Before #2094 this exact take graded USABLE /
    ``capture_integrity_clean``: the page had measured the lost quantum and the
    host read nothing it said."""
    prog = _verify_program()
    cap = _synthesize(prog)
    res = _analyze(prog, cap, _page_report(
        cap.size, block_gaps=1, block_gap_frames=RENDER_QUANTUM,
    ))
    integrity = res.capture_integrity
    assert integrity is not None
    assert integrity.failed == (INTEGRITY_CHECK_RENDER_GAP,)
    # The one-bit projection every durable position record already carries.
    assert res.glitch_detected is True
    verdict = evaluate_capture_validity(integrity)
    assert verdict.status is CaptureValidity.UNUSABLE
    assert verdict.reason == CAPTURE_INTEGRITY_FAILED
    assert INTEGRITY_CHECK_RENDER_GAP in verdict.evidence["failed"]


def test_frames_that_never_arrived_make_the_capture_unusable():
    """The other half: the page's count and the host's disagree."""
    prog = _verify_program()
    cap = _synthesize(prog)
    res = _analyze(prog, cap, _page_report(cap.size + RENDER_QUANTUM))
    integrity = res.capture_integrity
    assert integrity is not None
    assert integrity.failed == (INTEGRITY_CHECK_FRAME_LEDGER,)
    verdict = evaluate_capture_validity(integrity)
    assert verdict.status is CaptureValidity.UNUSABLE
    assert res.frame_ledger.lost_at == (LOST_AT_ENCODER_TO_HOST,)


def test_a_balanced_report_passes_both_checks_and_stays_usable():
    """The no-false-positive bar at the verdict."""
    prog = _verify_program()
    cap = _synthesize(prog)
    res = _analyze(prog, cap, _page_report(cap.size))
    integrity = res.capture_integrity
    assert integrity is not None
    statuses = {c.name: c.status for c in integrity.checks}
    assert statuses[INTEGRITY_CHECK_RENDER_GAP] == INTEGRITY_PASS
    assert statuses[INTEGRITY_CHECK_FRAME_LEDGER] == INTEGRITY_PASS
    assert integrity.failed == ()
    assert evaluate_capture_validity(integrity).reason == CAPTURE_INTEGRITY_CLEAN


def test_a_page_that_reports_nothing_is_not_refused_for_it():
    """A pre-#2094 bundle, and the single-capture runner, report no counts. That
    is a fact about the phone's build, never about the recording — so both
    checks say ``not_evaluated`` by name, and the shipped comparability rule
    (not-evaluated is still usable) leaves the take alone."""
    prog = _verify_program()
    res = _analyze(prog, _synthesize(prog), None)
    integrity = res.capture_integrity
    assert integrity is not None
    statuses = {c.name: c.status for c in integrity.checks}
    assert statuses[INTEGRITY_CHECK_RENDER_GAP] == INTEGRITY_NOT_EVALUATED
    assert statuses[INTEGRITY_CHECK_FRAME_LEDGER] == INTEGRITY_NOT_EVALUATED
    reasons = {c.name: c.reason for c in integrity.checks}
    assert reasons[INTEGRITY_CHECK_RENDER_GAP]
    assert reasons[INTEGRITY_CHECK_FRAME_LEDGER]
    assert evaluate_capture_validity(integrity).status is CaptureValidity.USABLE


def test_the_frame_checks_are_asked_before_any_signal_question():
    """Ordering is the module's documented routing rule, and it is load-bearing:
    a capture missing frames is not a worse measurement of the speaker, it is a
    measurement of something that was never recorded."""
    prog = _verify_program()
    res = _analyze(prog, _synthesize(prog), _page_report(1))
    names = [c.name for c in res.capture_integrity.checks]
    assert names[:2] == [INTEGRITY_CHECK_RENDER_GAP, INTEGRITY_CHECK_FRAME_LEDGER]


# --------------------------------------------------------------------------- #
# the no-false-positive bar against the chain's real transforms
# --------------------------------------------------------------------------- #


def test_the_analyzers_own_truncation_is_never_read_as_loss():
    """``analyze_program_capture`` caps an over-long capture before any
    full-rate FFT (frame_ledger's hop G). The received count is taken BEFORE
    that cap; taking it after would refuse every capture with a normal
    recording tail, which is most of them."""
    prog = _verify_program()
    cap = _synthesize(prog)
    tail = np.zeros(int((CAPTURE_BOUND_MARGIN_S + 5.0) * SR))
    long_capture = np.concatenate([cap, tail])
    res = _analyze(prog, long_capture, _page_report(long_capture.size))

    assert res.frame_ledger.received_frames == long_capture.size
    assert res.frame_ledger.lost_at == ()
    assert res.capture_integrity.failed == ()
    # The cap really did fire, so this test is not passing vacuously.
    assert long_capture.size > prog.total_samples + CAPTURE_BOUND_MARGIN_S * SR


def test_the_ledger_counts_frames_not_wav_bytes():
    """The page's WAV is 16-bit mono with a 44-byte header, so bytes and frames
    differ by a factor the ledger must never confuse. Counting the decoded
    array is what keeps the two ends of the comparison in the same unit."""
    prog = _verify_program()
    cap = _synthesize(prog)
    res = _analyze(prog, cap, _page_report(cap.size))
    assert res.frame_ledger.received_frames == cap.size
    wav_bytes = 44 + cap.size * 2
    assert res.frame_ledger.received_frames != wav_bytes


def test_a_multichannel_decode_still_compares_one_channels_frames():
    """``decode_wav_to_mono`` keeps channel 0 of a multichannel WAV, and the
    frame count is unchanged by that — the ledger must compare frames, never
    interleaved samples."""
    prog = _verify_program()
    cap = _synthesize(prog)
    stereo = np.stack([cap, cap], axis=1)
    res = _analyze(prog, stereo[:, 0], _page_report(cap.size))
    assert res.frame_ledger.lost_at == ()


# --------------------------------------------------------------------------- #
# every phase reports; only VERIFY refuses
# --------------------------------------------------------------------------- #


def test_a_measure_capture_carries_the_ledger_too():
    """The 2026-08-03 loss happened in MEASURE. A record that existed only on
    VERIFY would have been absent exactly where the defect was."""
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        capture_report=_page_report(
            cap.size, block_gaps=1, block_gap_frames=RENDER_QUANTUM,
        ),
    )
    assert res.frame_ledger is not None
    assert res.frame_ledger.lost_at == (LOST_AT_RENDER_GRAPH,)


def test_a_measure_capture_is_reported_but_not_refused_by_the_ledger():
    """The stated boundary. MEASURE's ``glitch_detected`` is a one-bit
    projection of ``DriftEstimate``, assigned at one site so the bit and the
    record can never disagree; folding a second owner in would break exactly
    that invariant. So MEASURE self-reports the loss and does not act on it.

    Changing this is a refusal-semantics decision on the capture path, not a
    plumbing change — see the issue #2094 PR body.
    """
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        capture_report=_page_report(
            cap.size, block_gaps=1, block_gap_frames=RENDER_QUANTUM,
        ),
    )
    assert res.frame_ledger.lost_at == (LOST_AT_RENDER_GRAPH,)
    assert res.capture_integrity is None
    assert res.glitch_detected is False
    assert res.drift is not None


# --------------------------------------------------------------------------- #
# it says so out loud
# --------------------------------------------------------------------------- #


def test_every_analyzed_capture_emits_the_ledger_event(caplog):
    """A clean capture logs too, at INFO: "no loss was reported here" and "no
    capture was analysed" are different facts, and only a line on the clean path
    tells them apart."""
    prog = _verify_program()
    cap = _synthesize(prog)
    with caplog.at_level(logging.INFO, logger=_ANALYSIS_LOGGER):
        _analyze(prog, cap, _page_report(cap.size))
    assert "event=program_analysis.frame_ledger" in caplog.text
    assert f"received_frames={cap.size}" in caplog.text
    assert "lost_at=" in caplog.text


def test_a_lossy_capture_warns_and_names_where(caplog):
    prog = _verify_program()
    cap = _synthesize(prog)
    with caplog.at_level(logging.WARNING, logger=_ANALYSIS_LOGGER):
        _analyze(prog, cap, _page_report(
            cap.size, block_gaps=1, block_gap_frames=RENDER_QUANTUM,
        ))
    assert "event=program_analysis.frame_ledger" in caplog.text
    assert f"lost_at={LOST_AT_RENDER_GRAPH}" in caplog.text
    assert f"render_gap_frames={RENDER_QUANTUM}" in caplog.text


def test_a_clean_capture_does_not_warn(caplog):
    prog = _verify_program()
    cap = _synthesize(prog)
    with caplog.at_level(logging.WARNING, logger=_ANALYSIS_LOGGER):
        _analyze(prog, cap, _page_report(cap.size))
    assert "event=program_analysis.frame_ledger" not in caplog.text


def test_the_retained_sidecar_summary_carries_the_ledger_flat():
    """``analysis_diagnostic_summary`` is what a retained forensic clip is
    described by. Flat, one field per fact, like every other block there."""
    prog = _verify_program()
    cap = _synthesize(prog)
    summary = analysis_diagnostic_summary(_analyze(prog, cap, _page_report(
        cap.size, block_gaps=1, block_gap_frames=RENDER_QUANTUM,
    )))
    assert summary["frames_received"] == cap.size
    assert summary["frames_declared"] == cap.size
    assert summary["frames_encoded"] == cap.size
    assert summary["frames_render_gaps"] == 1
    assert summary["frames_render_gap_frames"] == RENDER_QUANTUM
    assert summary["frames_lost_at"] == LOST_AT_RENDER_GRAPH


def test_the_worklets_frame_count_is_the_buffer_it_transfers():
    """``frames`` is read off the assembled buffer, never accumulated beside it.

    A separate counter can drift from the array it claims to describe, and a
    ledger whose left edge is wrong reports loss that did not happen — worse
    than no ledger. ``total`` is the same expression that SIZES the transferred
    buffer, so the two cannot disagree.
    """
    src = (_REPO / "deploy/assets/shared/js/measurement-audio.js").read_text(
        encoding="utf-8"
    )
    assert "'var out=new Float32Array(total);var pos=0;'" in src
    assert "'frames:total,blocks:this.blocks,block_gaps:this.gaps,'" in src


def test_silent_blocks_counts_only_the_input_starved_callbacks():
    """``silent_blocks`` is not "blocks that were silent" (issue #2557).

    The 2026-08-15 verdict turned on that distinction. A resync in the browser's
    own capture FIFO hands the worklet a perfectly ordinary input array that
    happens to be full of zeros; this counter never looks at content, so it read
    0 on all 13 glitch events while a whole zero-filled render quantum sat in
    each capture. Its ONE increment lives in the else-branch of "was I handed an
    input channel at all", and pinning that is what keeps three prose claims
    honest at once — this module's read-set comment, the capture page's report
    comment, and the field list all say the counter
    cannot see a zero-FILLED block, and all three become wrong together the day
    someone starts counting content here without renaming the field.
    """
    src = (_REPO / "deploy/assets/shared/js/measurement-audio.js").read_text(
        encoding="utf-8"
    )
    assert "'}else{this.silent++;}'" in src
    assert src.count("this.silent++") == 1


def test_the_measure_sidecar_summary_carries_it_too():
    prog = _measure_program()
    cap = _synthesize(prog)
    summary = analysis_diagnostic_summary(analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        capture_report=_page_report(cap.size),
    ))
    assert summary["frames_received"] == cap.size
    assert summary["frames_lost_at"] == ""
