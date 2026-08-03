#!/usr/bin/env python3
"""Severed-twin replay: re-fit a banked crossover-v2 session with the cloud
verdict cut, and diff the two fits.

**The question it answers, and the one it cannot.** Replaying a banked session
with exactly one input severed — ``excluded_bands_hz=None``, the production
``cloud is None`` branch in
:meth:`jasper.active_speaker.crossover_v2_flow.CrossoverV2Conductor._fit_linearization`
— shows whether the cloud's null evidence actually *bound* the fit, and what
the fit does without it. It cannot show that the wired answer was *right*:
the corpus carries no ground truth. That boundary is the validation ladder's
("Validation ladder — what each instrument can prove" in
``docs/HANDOFF-audio-measurement-core.md``), and this tool is the corpus-replay
rung of it, not the synthetic-scenario rung.

**Why a capture is bound to a session by its diagnostics, not its filename.**
A raw-ring sidecar carries no session id — only ``phase``, ``device_label``,
``wav_bytes``, and a ``diagnostic`` block. Selecting by the filename's phase
token or timestamp is the mistake that produced two false claims in PR #2070's
review: phase tokens changed era to era. So :func:`bind_measure_capture` matches
the sidecar's recorded ``diagnostic`` values against the banked
``candidate.json``'s ``analysis`` block and requires a unique hit. That is a
content binding; the filename is never read for anything but globbing.

**Why the replay validates itself.** The same sidecar ``diagnostic`` block is
the analysis AS PERFORMED, so it is ground truth for the replay's own fidelity
(not for the prescription). :func:`replay_measure` re-derives it and refuses to
report a fit unless the reproduced values match. An era-drifted reconstruction
therefore fails loudly instead of quietly re-reading the evidence — the failure
mode ``tests/_flat_lin_corpus.py``'s ``sweep_anchor`` docstring and issue #1879
record.

Nothing here is a fixture or a framework: the program is rebuilt from the
session's own ``check.json`` (its recorded ``gain_plan_db`` and per-role
``role_solves[*].band_hz``), and every analysis call is the shipped one.

Usage (laptop, from the repo root)::

    .venv/bin/python scripts/severed-twin-replay.py \\
        --bank captures/wo0-retrospective-20260729/reanalysis-data/pi-pull \\
        --ring captures/ring-snapshot-20260730 \\
        --calibration captures/flat-linearization-20260725/umik2-cal/umik2-b7343c0c625b.txt

Exit code is 1 if any session fails its fidelity check, so a drifted replay
cannot be mistaken for a finding.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# The sidecar diagnostics compared against the replay. Each is a value the
# banked analysis recorded about ITSELF, so a mismatch means the reconstruction
# reads different bytes than the session did — not that the speaker changed.
# `delay_us` is deliberately absent: the banked value is post-T2-refinement and
# the seed is what an offline replay reproduces (see FIDELITY_NOTE).
FIDELITY_FIELDS = (
    "epsilon_ppm",
    "woofer_repeat_epsilon_ppm",
    "tweeter_repeat_epsilon_ppm",
    "max_residual_samples",
    "repeat_level_delta_db",
    "alignment_confidence",
    "anchor_delay_us",
    "polarity",
    "alignment_status",
    "glitch_detected",
    "glitch_inputs",
    "linearity_ok",
    "pilot_snr_ok",
    "channel_map_ok",
    "woofer_pilot_snr_db",
    "woofer_captured_delta_db",
    "woofer_gate_window_ms",
    "tweeter_gate_window_ms",
    "woofer_validity_floor_hz",
    "tweeter_validity_floor_hz",
)
FIDELITY_TOLERANCE = 5e-3

FIDELITY_NOTE = """\
Two things this gate does NOT cover, stated because a gate whose reach is
assumed is worse than one whose reach is known:

  1. delay_us / snap_* / predicted_ripple_db are excluded deliberately. The
     banked values are post-T2-refinement; this replay reproduces the aligner
     SEED, which matches the sidecar's own `alignment_seed_delay_us` to every
     digit. The linearization fit reads magnitude curves, not the applied
     delay, so the seam does not reach this tool's question — but anyone
     asking a DELAY question here must close it first.

  2. The calibration SIGN CONVENTION is unpinned, and measurably so: running
     this tool under `sign_convention="response"` changes the fitted filter
     set and every one of the checked diagnostics still passes. The 20 fields
     below pin the capture/timing/deconvolution chain; no value banked in this
     era is a magnitude-domain quantity independent of the fit engine (which
     has moved several commits since), so none can pin the convention. The
     calibration FILE is bound by identity against the sidecar's
     `setup_calibration_id`; the convention rides the same era decision
     tests/_flat_lin_corpus.py records (jaspercurry/JTS#1774)."""


@dataclass(frozen=True)
class Session:
    """One banked session, and the raw-ring capture bound to it."""

    relay_id: str
    artifacts: Path
    candidate: dict[str, Any]
    check: dict[str, Any]
    capture_wav: Path
    sidecar: dict[str, Any]


@dataclass(frozen=True)
class Fit:
    """One role's fit, reduced to what a diff needs."""

    role: str
    filters: tuple[dict[str, float], ...]
    lift_suppressed_reason: str
    reason_at: dict[float, str]


def _load_mono(path: Path) -> np.ndarray:
    with wave.open(str(path)) as handle:
        channels, frames = handle.getnchannels(), handle.getnframes()
        raw = handle.readframes(frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    return samples[::channels] if channels > 1 else samples


def _regions(preset: dict[str, Any]) -> list[types.SimpleNamespace]:
    """``crossover_regions`` as the duck-typed objects ``sections_by_role``
    reads (it takes attributes, and the bank stores dicts)."""
    return [types.SimpleNamespace(**region)
            for region in preset.get("crossover_regions") or ()]


def bind_measure_capture(
    artifacts: Path, ring: Path, candidate: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """The raw-ring MEASURE capture this banked candidate was computed from.

    Matched on the sidecar's recorded ``diagnostic`` values against the
    candidate's own ``analysis`` block — a content binding. Raises when the
    match is not unique, because a replay reading the wrong capture is exactly
    the silent-wrong-answer this tool exists to avoid.
    """
    analysis = candidate["analysis"]
    keys = ("epsilon_ppm", "predicted_ripple_db", "alignment_confidence")
    hits: list[tuple[Path, dict[str, Any]]] = []
    for sidecar_path in sorted(ring.glob("*.json")):
        try:
            meta = json.loads(sidecar_path.read_text())
        except (OSError, ValueError):
            continue
        diagnostic = meta.get("diagnostic") or {}
        if meta.get("phase") != "measure":
            continue
        if any(
            not isinstance(diagnostic.get(key), (int, float))
            or not isinstance(analysis.get(key), (int, float))
            or abs(float(diagnostic[key]) - float(analysis[key])) > 1e-3
            for key in keys
        ):
            continue
        wav = sidecar_path.with_suffix(".wav")
        if wav.is_file():
            hits.append((wav, meta))
    if len(hits) != 1:
        raise SystemExit(
            f"{artifacts.name}: expected exactly one raw-ring MEASURE capture "
            f"matching {keys} in {ring}, found {len(hits)}"
        )
    return hits[0]


def discover(bank: Path, ring: Path) -> list[Session]:
    """Every banked session under ``bank`` that carries a candidate, a check,
    and a cloud verdict — by structure, never by a filename token."""
    sessions: list[Session] = []
    for candidate_path in sorted(bank.rglob("candidate.json")):
        artifacts = candidate_path.parent
        check_path = artifacts / "check.json"
        cloud_path = artifacts / "cloud_measure.json"
        if not (check_path.is_file() and cloud_path.is_file()):
            continue
        candidate = json.loads(candidate_path.read_text())
        if not candidate.get("linearization"):
            continue
        wav, sidecar = bind_measure_capture(artifacts, ring, candidate)
        sessions.append(
            Session(
                relay_id=artifacts.name,
                artifacts=artifacts,
                candidate=candidate,
                check=json.loads(check_path.read_text()),
                capture_wav=wav,
                sidecar=sidecar,
            )
        )
    return sessions


def replay_measure(
    session: Session, calibration_text: str, calibration_stem: str,
) -> tuple[Any, list[str]]:
    """Re-run the shipped MEASURE analysis over the bound capture.

    Returns ``(analysis, failures)``; ``failures`` is empty when every
    :data:`FIDELITY_FIELDS` value was reproduced.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS,
        COURTESY_PRELUDE_ENABLED,
        PILOT_LEVEL_DELTA_DB,
    )
    from jasper.audio_measurement.calibration import parse_calibration_text
    from jasper.audio_measurement.program import (
        FrequencyBand,
        RoleBand,
        build_measure_program,
    )
    from jasper.audio_measurement.program_analysis import (
        MeasurementGeometry,
        MeasurementPriors,
        analyze_program_capture,
    )

    check, candidate = session.check, session.candidate
    # The calibration FILE is bound by identity — the sidecar records which
    # one the session used, and reading a different mic's curve would move
    # every magnitude the fit reads without moving one checked diagnostic.
    calibration_id = str(session.sidecar.get("setup_calibration_id") or "")
    if calibration_id and calibration_stem not in calibration_id:
        raise SystemExit(
            f"{session.relay_id}: session used calibration {calibration_id!r}, "
            f"but {calibration_stem!r} was supplied"
        )
    solves, gains = check["role_solves"], check["gain_plan_db"]
    roles = [
        RoleBand("woofer", 0, FrequencyBand(*solves["woofer"]["band_hz"])),
        RoleBand("tweeter", 1, FrequencyBand(*solves["tweeter"]["band_hz"])),
    ]
    program = build_measure_program(
        {role: float(gains[role]) for role in ("woofer", "tweeter")},
        roles,
        leading_pilot_gains_db=(
            float(gains["woofer"]) - PILOT_LEVEL_DELTA_DB, float(gains["woofer"]),
        ),
        leading_pilot_role="woofer",
        courtesy_prelude=COURTESY_PRELUDE_ENABLED,
    )
    region = (candidate["source_preset"].get("crossover_regions") or [{}])[0]
    lo_ms, hi_ms = region.get("delay_range_ms") or (0.0, 0.0)
    bounds_us = (
        max(0.0, lo_ms - ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS) * 1000.0,
        (hi_ms + ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS) * 1000.0,
    )
    analysis = analyze_program_capture(
        program,
        _load_mono(session.capture_wav),
        program.sample_rate_hz,
        calibration=parse_calibration_text(
            calibration_text,
            # Era-exact: the banked analysis read this UMIK-2 file on the
            # parser's "correction" default. The file is really a RESPONSE
            # curve and the product negates it since 2026-07-27; flipping the
            # corpus to match is jaspercurry/JTS#1774, and doing it here alone
            # would re-read archived evidence under a convention the session
            # never used. Same decision, and same reasoning, as
            # tests/_flat_lin_corpus.py's CORPUS_CALIBRATION_SIGN_CONVENTION.
            sign_convention="correction",
        ),
        # No declared spacing: this session's info.json records
        # `placement.acknowledged=False`, and the banked `anchor_delay_us`
        # reproduces EXACTLY at parallax 0 (a declared 0.254 m / 1.0 m pair
        # offsets it by 92.5771 us on both banked sessions — the measurement
        # that settled it, rather than a guess at the rig).
        geometry=MeasurementGeometry(),
        priors=MeasurementPriors(
            crossover_fc_hz=float(region.get("fc_hz") or 0.0) or None,
            ambient_report=check.get("ambient_report"),
            alignment_delay_bounds_us=bounds_us if hi_ms else None,
            mic_tier=str(candidate["linearization"]["woofer"]["mic_tier"]),
        ),
    )
    return analysis, _fidelity_failures(analysis, session.sidecar["diagnostic"])


def _fidelity_failures(analysis: Any, want: dict[str, Any]) -> list[str]:
    drift, alignment = analysis.drift, analysis.alignment
    responses = {r.role: r for r in analysis.driver_responses}
    per_role = dict(getattr(drift, "per_role_epsilon_ppm", {}) or {})
    pilots = {p.role: p for p in analysis.pilots}
    gates = {
        role: (getattr(responses.get(role), "gating", None) or {}).get("window_ms")
        for role in ("woofer", "tweeter")
    }
    woofer_pilot = pilots.get("woofer")
    got: dict[str, Any] = {
        "epsilon_ppm": getattr(drift, "epsilon_ppm", None),
        "woofer_repeat_epsilon_ppm": per_role.get("woofer"),
        "tweeter_repeat_epsilon_ppm": per_role.get("tweeter"),
        "max_residual_samples": getattr(drift, "max_residual_samples", None),
        "repeat_level_delta_db": getattr(drift, "repeat_level_delta_db", None),
        "glitch_detected": getattr(drift, "glitch_detected", None),
        "glitch_inputs": ",".join(getattr(drift, "glitch_inputs", ()) or ()),
        "alignment_confidence": getattr(alignment, "confidence", None),
        "anchor_delay_us": getattr(alignment, "anchor_delay_us", None),
        "polarity": getattr(alignment, "polarity", None),
        "alignment_status": getattr(alignment, "status", None),
        "linearity_ok": analysis.linearity_ok,
        "pilot_snr_ok": analysis.pilot_snr_ok,
        "channel_map_ok": analysis.channel_map_ok,
        "woofer_pilot_snr_db": getattr(woofer_pilot, "snr_db", None),
        "woofer_captured_delta_db": getattr(woofer_pilot, "captured_delta_db", None),
        "woofer_gate_window_ms": gates["woofer"],
        "tweeter_gate_window_ms": gates["tweeter"],
        "woofer_validity_floor_hz": getattr(
            responses.get("woofer"), "validity_floor_hz", None),
        "tweeter_validity_floor_hz": getattr(
            responses.get("tweeter"), "validity_floor_hz", None),
    }
    failures = []
    for field in FIDELITY_FIELDS:
        if field not in want:
            continue
        mine, theirs = got.get(field), want[field]
        if isinstance(mine, float) and isinstance(theirs, (int, float)):
            if abs(mine - float(theirs)) > FIDELITY_TOLERANCE:
                failures.append(f"{field}: replay={mine!r} banked={theirs!r}")
        elif mine != theirs:
            failures.append(f"{field}: replay={mine!r} banked={theirs!r}")
    return failures


def refit(session: Session, analysis: Any, *, arm: str) -> dict[str, Fit]:
    """Fit both drivers through the shipped path, under one of three arms.

    Three, not two, because the production ``cloud is None`` branch changes
    TWO things at once and they answer different questions:

    * ``wired`` — as recorded.
    * ``severed_exclusion`` — the cloud terms cut
      (``excluded_bands_hz``/``band_spread``/``n_positions`` all ``None``,
      together, because ``compose_envelope`` requires them as a set) while
      boost permission is LEFT ON. This isolates what the null evidence
      contributed to the envelope, for cuts as well as boosts.
    * ``severed_cloud`` — additionally ``allow_boost=False``. This is the
      production branch verbatim: no cloud reached the envelope, so the
      conductor withholds the lift vocabulary too.

    Reporting only the last would credit the exclusion term with a change
    the permission flag caused, which is the confound this split removes.
    """
    assert arm in ("wired", "severed_exclusion", "severed_cloud"), arm
    wired = arm == "wired"
    from jasper.active_speaker.branch_chain import sections_by_role
    from jasper.active_speaker.branch_target import branch_target, radiating_band_hz
    from jasper.active_speaker.crossover_v2_flow import _compose_sigma_db
    from jasper.active_speaker.linearization_envelope import compose_envelope
    from jasper.active_speaker.linearization_fit import (
        FitVocabulary,
        driver_core_level_db,
        fit_driver_linearization,
        solve_shared_level_frame,
    )
    from jasper.audio_measurement.spatial_combine import BandSpread

    evidence = session.candidate["exclusion_evidence"]
    # The bank stores BandSpread as plain dicts; the envelope's
    # position-stability term reads attributes.
    band_spread = tuple(BandSpread(**row) for row in evidence["band_spread"])
    program_bands = {
        role: tuple(session.check["role_solves"][role]["band_hz"])
        for role in ("woofer", "tweeter")
    }
    responses = {r.role: r for r in analysis.driver_responses}
    siblings = {"woofer": responses["tweeter"], "tweeter": responses["woofer"]}
    sections = sections_by_role(_regions(session.candidate["source_preset"]))
    mic_tier = str(analysis.mic_tier)
    classes = {
        role: str(session.candidate["linearization"][role]["driver_class"])
        for role in ("woofer", "tweeter")
    }

    cloud_bands = tuple(
        (float(lo), float(hi)) for lo, hi in evidence["excluded_bands_hz"]
    )
    vocabulary = FitVocabulary(
        # `_post_apply_verifies and cloud is not None`. These sessions declared
        # a post-apply check, so the only term that can turn this off is the
        # severed cloud — which is what `severed_cloud` reproduces.
        allow_boost=arm != "severed_cloud",
        boost_excluded_bands_hz=(),
    )

    role_sections = {role: sections.get(role, ()) for role in ("woofer", "tweeter")}
    radiating = {role: radiating_band_hz(s) for role, s in role_sections.items()}

    # Compose BOTH envelopes before fitting either — the conductor's PR-L5
    # ordering. The session's one shared level frame is read off them, so a
    # per-role fit-then-move loop would fit the first driver to a frame the
    # second had not yet contributed to.
    envelopes = {
        role: compose_envelope(
            role, responses[role],
            excited_band_hz=program_bands[role],
            mic_tier=mic_tier,
            driver_class=classes[role],
            sigma_db=_compose_sigma_db(
                responses[role], siblings[role],
                tier=mic_tier, valid_band_hz=program_bands[role],
            ),
            excluded_bands_hz=cloud_bands if wired else None,
            band_spread=band_spread if wired else None,
            n_positions=int(evidence["n_positions"]) if wired else None,
        )
        for role in ("woofer", "tweeter")
    }
    # The frame is recomputed per arm ON PURPOSE: severing the cloud moves the
    # envelope, which moves each driver's core level, which moves the frame.
    # Reusing the wired frame in the severed arm would sever one input and
    # silently keep a second derived from it.
    core_levels_db = {
        role: level
        for role in ("woofer", "tweeter")
        if (level := driver_core_level_db(
            responses[role], envelopes[role], radiating_band_hz=radiating[role],
        )) is not None
    }
    analysis_block = session.candidate["analysis"]
    frame_trims_db = dict(
        analysis_block.get("trim_band_average_db") or analysis_block["trim_db"]
    )
    level_frame = (
        solve_shared_level_frame(core_levels_db, frame_trims_db)
        if core_levels_db else None
    )

    out: dict[str, Fit] = {}
    for role in ("woofer", "tweeter"):
        response, envelope = responses[role], envelopes[role]
        fit = fit_driver_linearization(
            response, envelope,
            vocabulary=vocabulary,
            level_frame=level_frame,
            radiating_band_hz=radiating[role],
            target=branch_target(role_sections[role], envelope.freqs_hz),
        )
        out[role] = Fit(
            role=role,
            filters=tuple(
                {"freq": float(f.freq), "gain": float(f.gain), "q": float(f.q)}
                for f in fit.filters
            ),
            lift_suppressed_reason=str(getattr(fit, "lift_suppressed_reason", "")),
            reason_at=dict(getattr(fit, "reason_summary", {}) or {}),
        )
    return out


def _inside(freq: float, bands: tuple[tuple[float, float], ...]) -> tuple[float, float] | None:
    for lo, hi in bands:
        if lo <= freq <= hi:
            return (lo, hi)
    return None


def report(session: Session, arms: dict[str, dict[str, Fit]]) -> None:
    nulls = tuple(
        (float(lo), float(hi))
        for lo, hi in session.candidate["exclusion_evidence"]["excluded_bands_hz"]
    )
    print(f"\n### {session.relay_id}")
    print(f"  capture   : {session.capture_wav.name}  (bound on diagnostics)")
    print(f"  nulls     : {[(round(lo,1), round(hi,1)) for lo, hi in nulls]}")
    for role in ("woofer", "tweeter"):
        print(f"  -- {role} --")
        for label, fits in arms.items():
            fit = fits[role]
            boosts = [f for f in fit.filters if f["gain"] > 0]
            inside = [f for f in fit.filters if _inside(f["freq"], nulls)]
            print(f"     {label:<18}: {len(fit.filters)} filters, "
                  f"{len(boosts)} boost, {len(inside)} INSIDE a null "
                  f"(lift_suppressed_reason={fit.lift_suppressed_reason!r})")
            for f in fit.filters:
                hit = _inside(f["freq"], nulls)
                kind = "BOOST" if f["gain"] > 0 else "cut"
                flag = (f"   <-- {kind} INSIDE null "
                        f"{tuple(round(v, 1) for v in hit)}") if hit else ""
                print(f"        {f['freq']:>10.1f} Hz  {f['gain']:+7.3f} dB  "
                      f"Q={f['q']:.3f}{flag}")
        exclusion_reasons = {
            hz: why for hz, why in arms["wired"][role].reason_at.items()
            if "spatial_exclusion" in str(why)
        }
        print(f"     wired envelope reasons naming spatial exclusion: "
              f"{exclusion_reasons or '{}'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, required=True,
                        help="directory to search for banked sessions")
    parser.add_argument("--ring", type=Path, required=True,
                        help="raw capture ring holding the MEASURE captures")
    parser.add_argument("--calibration", type=Path, required=True)
    args = parser.parse_args(argv)

    calibration_text = args.calibration.read_text()
    sessions = discover(args.bank, args.ring)
    if not sessions:
        print(f"no banked sessions with candidate+check+cloud under {args.bank}")
        return 1
    print(f"{len(sessions)} banked session(s)\n{FIDELITY_NOTE}")

    failed = False
    for session in sessions:
        analysis, failures = replay_measure(
            session, calibration_text, args.calibration.stem,
        )
        if failures:
            failed = True
            print(f"\n### {session.relay_id}\n  FIDELITY FAILED — not reporting a fit:")
            for line in failures:
                print(f"    {line}")
            continue
        print(f"\n[{session.relay_id}] fidelity OK "
              f"({len(FIDELITY_FIELDS)} banked diagnostics reproduced)")
        report(session, {
            arm: refit(session, analysis, arm=arm)
            for arm in ("wired", "severed_exclusion", "severed_cloud")
        })
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
