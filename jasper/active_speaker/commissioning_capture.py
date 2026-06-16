"""Bridge: mic-backed acoustic verdict -> commissioning measurement record.

[`driver_acoustics`](driver_acoustics.py)'s ``analyze_driver_capture`` /
``analyze_summed_crossover`` turn a phone-mic sweep capture into a real acoustic
verdict, but they had no caller (the runtime commissioning loop did not exist
yet). This module is that caller: per driver it derives the expected passband
from the compiled preset's crossover regions, runs the acoustic analysis on a
captured sweep WAV, maps the verdict to a
[`measurement`](measurement.py) outcome, and records it through
``record_driver_measurement`` / ``record_summed_validation`` with the *real*
``observed_mic_dbfs`` plus the acoustic verdict block as new evidence on the
same record (the gap ``driver_acoustics``'s module docstring describes).

It does no audio I/O and opens no hardware. The caller (the runtime
commissioning sequencer and its ``/sound/active-speaker/*`` endpoints) plays the
sweep through the active route under the existing safe-playback machinery,
records the phone mic with the shared browser recorder, and hands the captured
WAV path here. ``analyze`` / ``record`` are injected so the wire is hardware-free
unit-testable; the heavy numpy/scipy work stays lazy inside ``driver_acoustics``.

The acoustic verdict supplements, and never relaxes, the measurement-record
safety gates: a ``present`` verdict maps to ``heard_correct_driver``, which
``record_driver_measurement`` still gates on identity verification + the
operator floor confirmation before it counts as ``captured``. An
``unusable_capture`` (clipped / wrong-rate / too-short) records nothing — the
caller re-captures rather than persisting a fabricated result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.output_topology import OutputTopology

from .driver_acoustics import (
    ANALYSIS_HI_HZ,
    ANALYSIS_LO_HZ,
    DEFAULT_NULL_THRESHOLD_DB,
    DriverAcousticResult,
    SummedAcousticResult,
    analyze_driver_capture,
    analyze_summed_crossover,
)
from .measurement import record_driver_measurement, record_summed_validation
from .profile import ActiveSpeakerPreset, crossover_edges_for_role

# Acoustic verdict -> measurement outcome. ``unusable_capture`` has no entry:
# DRIVER_OUTCOMES / SUMMED_OUTCOMES have no "unusable" member, and recording a
# pass/fail from a capture we could not trust would fabricate evidence, so an
# unusable capture is reported back NOT recorded and the caller re-captures.
DRIVER_VERDICT_TO_OUTCOME = {
    "present": "heard_correct_driver",
    "out_of_band": "heard_wrong_driver",
    "silent": "silent",
}
SUMMED_VERDICT_TO_OUTCOME = {
    "blend_ok": "blend_ok",
    "polarity_or_delay_problem": "polarity_or_delay_problem",
}


def driver_passband_hz(preset: ActiveSpeakerPreset, role: str) -> tuple[float, float]:
    """The driver's expected acoustic passband for ``analyze_driver_capture``.

    Derived from the role's crossover edges. Open ends (a woofer's low side, a
    tweeter's high side) clamp to the trusted phone-mic analysis window
    ``[ANALYSIS_LO_HZ, ANALYSIS_HI_HZ]`` so the verdict's band comparison stays
    inside the range the deconvolved magnitude is meaningful in.
    """
    lower_edge, upper_edge = crossover_edges_for_role(preset, role)
    lo = float(lower_edge) if lower_edge and lower_edge > 0 else ANALYSIS_LO_HZ
    hi = float(upper_edge) if upper_edge and upper_edge > lo else ANALYSIS_HI_HZ
    if not 0 < lo < hi:
        # A degenerate/empty crossover set: fall back to the full trusted window
        # rather than raising — the verdict just becomes full-range presence.
        return float(ANALYSIS_LO_HZ), float(ANALYSIS_HI_HZ)
    return lo, hi


def primary_crossover_fc_hz(preset: ActiveSpeakerPreset) -> float | None:
    """The lowest crossover frequency for a group's summed-blend check.

    A 2-way has exactly one crossover; this returns it. A 3-way has two, and the
    lowest (woofer/mid) is the default summed-blend target unless the caller
    passes an explicit ``crossover_fc_hz``.
    """
    fcs = [
        float(region.fc_hz)
        for region in preset.crossover_regions
        if region.fc_hz and region.fc_hz > 0
    ]
    return min(fcs) if fcs else None


def record_driver_acoustic_capture(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    *,
    speaker_group_id: str,
    role: str,
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    playback_id: str | None = None,
    test_level_dbfs: float | None = None,
    has_mic_calibration: bool = False,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    safe_session: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    analyze: Callable[..., DriverAcousticResult] = analyze_driver_capture,
    record: Callable[..., dict[str, Any]] = record_driver_measurement,
) -> dict[str, Any]:
    """Analyze one driver's sweep capture and record the result.

    Runs ``analyze_driver_capture`` against the role's expected passband, maps
    the verdict to a measurement outcome, and persists it (with the real
    ``observed_mic_dbfs`` and the full acoustic block) through
    ``record_driver_measurement``. An ``unusable_capture`` records nothing.
    Returns ``{verdict, outcome, recorded, skipped_reason, passband_hz,
    acoustic, measurement}``.

    ``playback_id`` must be the **accepted floor test's** playback id and
    ``safe_session`` its armed session: a ``present`` verdict maps to
    ``heard_correct_driver``, which ``record_driver_measurement`` only counts as
    ``captured`` when that floor confirmation matches this target (see
    ``measurement._floor_confirmation_issues``). A missing or mismatched floor
    confirmation still records the acoustic evidence but leaves ``captured``
    False — the acoustic verdict never bypasses the operator floor gate.
    """
    passband = driver_passband_hz(preset, role)
    result = analyze(
        captured_wav,
        sweep_meta,
        passband_hz=passband,
        has_mic_calibration=has_mic_calibration,
    )
    acoustic = result.to_dict()
    outcome = DRIVER_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        return {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "passband_hz": list(passband),
            "acoustic": acoustic,
            "measurement": None,
        }
    raw = {
        "speaker_group_id": speaker_group_id,
        "role": role,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "playback_id": playback_id,
        "test_level_dbfs": test_level_dbfs,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        safe_session=safe_session,
        state_path=state_path,
        now=now,
    )
    return {
        "verdict": result.verdict,
        "outcome": outcome,
        "recorded": True,
        "skipped_reason": None,
        "passband_hz": list(passband),
        "acoustic": acoustic,
        "measurement": measurement,
    }


def record_summed_acoustic_capture(
    topology: OutputTopology,
    preset: ActiveSpeakerPreset,
    *,
    speaker_group_id: str,
    captured_wav: str | Path,
    sweep_meta: Mapping[str, Any],
    crossover_fc_hz: float | None = None,
    null_threshold_db: float = DEFAULT_NULL_THRESHOLD_DB,
    summed_test_id: str | None = None,
    playback_id: str | None = None,
    polarity: str | None = None,
    delay_ms: float | None = None,
    delay_target_role: str | None = None,
    has_mic_calibration: bool = False,
    notes: str | None = None,
    calibration_level: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
    analyze: Callable[..., SummedAcousticResult] = analyze_summed_crossover,
    record: Callable[..., dict[str, Any]] = record_summed_validation,
) -> dict[str, Any]:
    """Analyze a summed-driver sweep capture and record the crossover verdict.

    Runs ``analyze_summed_crossover`` at the group's crossover frequency
    (defaulting to the lowest crossover in the preset), maps the verdict to a
    summed outcome, and persists it through ``record_summed_validation``. An
    ``unusable_capture`` — or a preset with no crossover — records nothing.
    """
    fc = (
        float(crossover_fc_hz)
        if crossover_fc_hz and crossover_fc_hz > 0
        else primary_crossover_fc_hz(preset)
    )
    if not fc:
        return {
            "verdict": None,
            "outcome": None,
            "recorded": False,
            "skipped_reason": "no_crossover_region",
            "crossover_fc_hz": None,
            "acoustic": None,
            "measurement": None,
        }
    result = analyze(
        captured_wav,
        sweep_meta,
        crossover_fc_hz=fc,
        null_threshold_db=null_threshold_db,
        has_mic_calibration=has_mic_calibration,
    )
    acoustic = result.to_dict()
    outcome = SUMMED_VERDICT_TO_OUTCOME.get(result.verdict)
    if outcome is None:
        return {
            "verdict": result.verdict,
            "outcome": None,
            "recorded": False,
            "skipped_reason": result.verdict,
            "crossover_fc_hz": fc,
            "acoustic": acoustic,
            "measurement": None,
        }
    raw = {
        "speaker_group_id": speaker_group_id,
        "outcome": outcome,
        "observed_mic_dbfs": result.observed_mic_dbfs,
        "mic_clipping": result.mic_clipping,
        "acoustic": acoustic,
        "summed_test_id": summed_test_id,
        "playback_id": playback_id,
        "polarity": polarity,
        "delay_ms": delay_ms,
        "delay_target_role": delay_target_role,
        "notes": notes,
    }
    measurement = record(
        topology,
        raw,
        calibration_level=calibration_level,
        state_path=state_path,
        now=now,
    )
    return {
        "verdict": result.verdict,
        "outcome": outcome,
        "recorded": True,
        "skipped_reason": None,
        "crossover_fc_hz": fc,
        "acoustic": acoustic,
        "measurement": measurement,
    }
