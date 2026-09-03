# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-source seam's contract (#2662, decision 13).

Pins the promises :mod:`jasper.active_speaker.crossover_v2.capture_source`
makes: the wired provider's shipped answer type satisfies
:class:`CaptureAnswer`; an answer carrying ONLY the contract's four
attributes is sufficient for the production analyze seam (what makes the
seam wired-ready — a second source mints exactly this shape); and the
integrity-counter tuple is the frame ledger's real read set, not a parallel
spelling.
"""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
    CaptureAnswer,
)
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.web import correction_crossover_v2 as v2host
from jasper.web.correction_crossover_v2_wired import WiredCaptureAnswer


def _mono_wav_bytes(n: int = 4800) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x00\x00" * n)
    return buf.getvalue()


@dataclass(frozen=True)
class _WiredShapedAnswer:
    """Exactly the contract, nothing else — the shape a second source mints."""

    wav: bytes
    device: Mapping[str, Any] | None = None
    setup: Mapping[str, Any] | None = None
    capture_integrity: Mapping[str, Any] | None = None


def test_the_wired_providers_shipped_answer_satisfies_the_contract():
    """``WiredCaptureAnswer`` IS a ``CaptureAnswer`` as it stands.

    Structural, so this holds for any conformant object — the ``isinstance``
    is the runtime-checkable protocol's attribute-presence check.
    """
    assert isinstance(WiredCaptureAnswer(wav=b""), CaptureAnswer)
    assert isinstance(_WiredShapedAnswer(wav=b""), CaptureAnswer)


def test_a_contract_only_answer_satisfies_the_production_analyze_seam(
    monkeypatch,
):
    """The analyze seam reads NOTHING beyond the contract's four attributes.

    This is the wired-readiness pin: a provider that mints an answer with
    exactly ``wav``/``device``/``setup``/``capture_integrity`` flows through
    ``bind_production_analyze`` — resolver, integrity report and all — so the
    next source needs no provider-shaped extras. The analysis itself is
    stubbed; the subject is what the binding consumes off the answer.
    """
    from jasper.audio_measurement import program_analysis as pa_mod
    from jasper.audio_measurement.program import build_verify_program
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
    )

    seen: dict[str, Any] = {}

    def spy(program, samples, rate, *, calibration=None, geometry=None,
            priors=None, capture_report=None):
        seen.update(rate=rate, capture_report=capture_report)
        return "analysis"

    monkeypatch.setattr(pa_mod, "analyze_program_capture", spy)

    resolved: list = []

    def resolver(setup, device):
        resolved.append((setup, device))
        return None

    report = {key: 7 for key in INTEGRITY_COUNTER_KEYS}
    answer = _WiredShapedAnswer(
        wav=_mono_wav_bytes(),
        device={"label": "UMIK-2"},
        setup={"calibration": {"mode": "serial"}},
        capture_integrity=report,
    )
    assert isinstance(answer, CaptureAnswer)

    analyze = v2host.bind_production_analyze(
        resolve_calibration=resolver, meta={},
    )
    out = analyze(
        build_verify_program(2000.0, sweep_s=0.5),
        answer,
        MeasurementPriors(crossover_fc_hz=2000.0),
        MeasurementGeometry(driver_spacing_m=0.15, mic_distance_m=1.0),
        phase="verify",
    )

    assert out == "analysis"
    assert resolved == [(answer.setup, answer.device)]
    assert seen["rate"] == 48000
    assert seen["capture_report"] is report


def test_the_integrity_counter_keys_are_the_ledgers_real_read_set():
    """The contract's tuple is what ``reconcile_capture_frames`` actually reads.

    Behavioral in both directions: a report built from exactly these keys
    populates every page-side ledger slot, and a report with none of them
    populates none — so the tuple can neither miss a key the ledger reads nor
    carry one it ignores.
    """
    report = {
        key: index + 1 for index, key in enumerate(INTEGRITY_COUNTER_KEYS)
    }
    ledger = reconcile_capture_frames(report, received_frames=4)
    assert ledger.declared_frames == 1
    assert ledger.encoded_frames == 2
    assert ledger.render_gaps == 3
    assert ledger.render_gap_frames == 4

    unrelated = reconcile_capture_frames(
        {"blocks": 9, "silent_blocks": 1}, received_frames=4
    )
    assert unrelated.declared_frames is None
    assert unrelated.encoded_frames is None
    assert unrelated.render_gaps is None
    assert unrelated.render_gap_frames is None
