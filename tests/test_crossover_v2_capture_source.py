# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-source seam's contract (#2662, decision 13).

Pins the promises :mod:`jasper.active_speaker.crossover_v2.capture_source`
makes: the relay's shipped answer type satisfies :class:`CaptureAnswer`; an
answer carrying ONLY the contract's four attributes is sufficient for the
production analyze seam (what makes the seam wired-ready — a second source
mints exactly this shape); the integrity-counter tuple is the frame ledger's
real read set, not a parallel spelling; and the host/provider module pair
keeps the import shape that makes the strangler safe (host re-publishes the
provider's names; the provider never imports the host at module scope).
"""
from __future__ import annotations

import io
import subprocess
import sys
import wave
from dataclasses import dataclass
from typing import Any, Mapping

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
    CaptureAnswer,
)
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.capture_relay.session import CaptureResult
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_relay as v2relay


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


def test_the_relays_shipped_answer_satisfies_the_contract():
    """``CaptureResult`` IS a ``CaptureAnswer`` as it stands.

    The relay provider passes through what the phone sent; the seam names
    that existing shape rather than wrapping it. Structural, so this holds
    for any conformant object — the ``isinstance`` is the runtime-checkable
    protocol's attribute-presence check.
    """
    assert isinstance(CaptureResult(wav=b""), CaptureAnswer)
    assert isinstance(_WiredShapedAnswer(wav=b""), CaptureAnswer)


def test_a_contract_only_answer_satisfies_the_production_analyze_seam(
    monkeypatch,
):
    """The analyze seam reads NOTHING beyond the contract's four attributes.

    This is the wired-readiness pin: a provider that mints an answer with
    exactly ``wav``/``device``/``setup``/``capture_integrity`` flows through
    ``bind_production_analyze`` — resolver, integrity report and all — so the
    next source needs no relay-shaped extras. The analysis itself is stubbed;
    the subject is what the binding consumes off the answer.
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


def test_the_host_republishes_the_providers_names():
    """The host's published surface survives the extraction (#2662 slice 1).

    ``correction_setup``, the preparers, and the whole existing test surface
    address these names at the host module; the strangler keeps them there as
    the SAME objects, so a patch or a call through either module is one
    binding, not two.
    """
    for name in (
        "TERMINAL_FAILURE_PURGE_GRACE_S",
        "PlaybackStartSignal",
        "build_v2_run_and_consume",
        "program_phase_schedule",
        "relay_link_ttl_s",
        "start_program_phase_ladder",
    ):
        assert getattr(v2host, name) is getattr(v2relay, name), name


def test_the_provider_reaches_the_host_only_at_call_time():
    """Importing the provider does not import the host — behavioral pin.

    The host imports the provider EAGERLY (to re-publish its names), so the
    pair stays import-safe only while the provider's host reach-backs are
    call-time. Asserted on the real interpreter rather than by scanning
    import statements: a fresh process imports the provider and checks
    ``sys.modules`` for the host — which also catches an INDIRECT route (a
    dependency of the provider growing a host import) that a source scan of
    the one file cannot see. The ``find_spec`` line is the probe's positive
    control: it proves the name asserted absent is a real, loadable module,
    so a renamed host cannot make this pass vacuously.
    """
    probe = (
        "import importlib.util, sys\n"
        "import jasper.web.correction_crossover_v2_relay\n"
        "assert 'jasper.web.correction_crossover_v2' not in sys.modules, "
        "'the provider imported the host at module scope'\n"
        "assert importlib.util.find_spec("
        "'jasper.web.correction_crossover_v2') is not None\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
