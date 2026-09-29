# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The H2/H3 reading a take banks at capture, and the distortion view over it.

Harmonic images precede the linear IR by L·ln(order), so the distortion
kernel uses a wider pre-guard than the normal response analysis. The reading
deconvolves each per-driver sweep on the samples, anchors, drift and
calibration its take's analysis read, and the view opens no recording
(ADR-0394). The id leaves the fader out (#5012), so a MEASURE take's drive
rests on the session volume it recorded, labelled so and checked against
the fader readback it banked.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from jasper.audio_measurement.program import (
    ExcitationProgram,
    KIND_SWEEP,
    is_level_probe,
)
from jasper.platform.json_fields import CodedFieldError, JsonFields, finite_float
from jasper.platform.log_event import log_event
from jasper.platform.volume_latch import fader_matches

from jasper.audio_measurement import deconv
from jasper.audio_measurement.calibration import (
    CalibrationCurve,
)
from jasper.audio_measurement.distortion import read_segment_distortion, worst_clear_of_floor
from jasper.audio_measurement.evidence_reasons import (
    REASON_COVERAGE_SHORT,
    REASON_HARMONIC_WINDOW_OUT_OF_RANGE,
    REASON_SWEEP_GRIDS_DISAGREE,
    TAKE_CURVES_NOT_BANKED,
    EvidenceUnavailable,
    unavailable,
)

from .journey import PHASE_MEASURE
from .record_index import measurement_documents, record_path

logger = logging.getLogger(__name__)
_FIELDS = JsonFields(CodedFieldError)

#: The artifact's own kind tag, so a file found loose says what it is.
HARMONICS_ARTIFACT_KIND = "jts_crossover_v2_harmonic_distortion"

#: The orders read. Not imported from
#: :data:`~jasper.audio_measurement.deconv.DEFAULT_HARMONIC_ORDERS`
#: even though it holds the same pair today: that constant bounds what the
#: ANALYSIS kernel will separate, this one is the product's choice of what
#: to publish.
HARMONIC_ORDERS: tuple[int, ...] = (2, 3)

#: Excitation frequencies the rows are sampled at. A fixed ladder rather than
#: the full FFT grid, because this document is read by an LLM operator and
#: by a human at a terminal. Roughly third-octave from 150 Hz. Points
#: outside a role's own band are omitted rather than published as null, so
#: a row that exists is a row that was measured.
PROBE_FREQUENCIES_HZ: tuple[float, ...] = (
    150.0, 200.0, 300.0, 400.0, 600.0, 800.0, 1000.0,
    1500.0, 2000.0, 3000.0, 4000.0, 6000.0, 8000.0,
)

#: Decimal places the published dB figures carry. One, because the pooling
#: below is a median over sweeps whose own scatter is tenths of a dB.
#: Digits past it would be arithmetic noise in a document that is
#: content-fingerprinted downstream.
_DB_DECIMALS = 1

#: Decimal places for THD percent, which is a small number where the first
#: significant digit often sits three places in.
_PERCENT_DECIMALS = 3

#: The round banks no MEASURE or branch take this view can read.
NO_ADMISSIBLE_CAPTURES = "no_admissible_captures"


class SweepGridsDisagree(ValueError):
    """One role's sweeps were read on different grids, which pooling by index would hide."""


def distortion_evidence(program: ExcitationProgram, analysis: Any, samples: np.ndarray,
                        calibration: CalibrationCurve | None) -> dict[str, Any] | None:
    """Each role's H2/H3 rows, pooled over its per-driver sweeps in the
    ``samples`` ``analysis`` read, at its anchors and drift, or their coded
    gap, so the take banks its curves whatever this can read. A program with
    no per-driver sweep, and a level probe, have none (ADR-0394)."""
    sweeps = [segment for segment in program.stimulus_segments() if segment.kind == KIND_SWEEP]
    if not sweeps or is_level_probe(program):
        return None
    anchors = {location.segment_id: location.scheduled_start for location in analysis.locations}
    epsilon = analysis.drift.epsilon_ppm / 1e6 if analysis.drift else 0.0
    by_role: dict[str, list] = {}
    try:
        for segment in sweeps:
            by_role.setdefault(str(segment.role), []).append(read_segment_distortion(
                program, samples, segment.segment_id, anchors[segment.segment_id],
                orders=HARMONIC_ORDERS, calibration=calibration, epsilon=epsilon))
        roles = [_role_block(role, readings, HARMONIC_ORDERS) for role, readings in sorted(by_role.items())]
    except ValueError as exc:
        reason = (REASON_HARMONIC_WINDOW_OUT_OF_RANGE if isinstance(exc, deconv.HarmonicWindowOutOfRange)
                  else REASON_SWEEP_GRIDS_DISAGREE if isinstance(exc, SweepGridsDisagree) else REASON_COVERAGE_SHORT)
        log_event(logger, "active_speaker.distortion_not_banked", level=logging.WARNING,
                  stimulus_id=program.stimulus_id, reason=reason, error_type=type(exc).__name__)
        return unavailable(reason)
    return {"orders": list(HARMONIC_ORDERS), "roles": roles}


def _median(values: Sequence[float]) -> float:
    """Median, or NaN over nothing — never a zero standing in for no data."""
    real = [value for value in values if math.isfinite(value)]
    return statistics.median(real) if real else float("nan")


def _spread(values: Sequence[float]) -> float | None:
    """Sample standard deviation across in-capture repeats, or ``None``.

    ``None`` below two real values rather than 0.0: a sample standard
    deviation is UNDEFINED at n=1 and a zero would say
    the repeats agreed. ``statistics.stdev`` RAISES at n < 2 instead of
    returning a silent NaN, and the ``len < 2`` guard stands in front of it.
    """
    real = [value for value in values if math.isfinite(value)]
    if len(real) < 2:
        return None
    try:
        return round(statistics.stdev(real), _DB_DECIMALS)
    except OverflowError:
        return None


def _nullable(value: float, decimals: int = _DB_DECIMALS) -> float | None:
    """One rounded number, or ``None`` where the reading is not real.

    NaN means "past this order's own band edge" and reaches JSON as ``null`` —
    never as a number, because a very negative float would read as a
    preternaturally clean driver exactly where nothing was measured.
    """
    return round(float(value), decimals) if math.isfinite(value) else None


def _role_block(role: str, readings: list, orders: tuple[int, ...]) -> dict:
    """One (capture, role)'s rows, pooled over that role's in-capture sweeps.

    **Pooling is per capture on purpose, and it is what makes the spread below
    one kind.** A MEASURE capture is one pose, so the sweeps of one role
    inside it are that pose's repeats and their scatter is the RANDOM
    repeatability term — the statistic
    ``linearization_envelope.compute_sigma_curve`` owns. Pooling across
    CAPTURES would mix it with whatever differs between takes, which is the
    unseparated case, so a round with two MEASURE captures publishes two
    blocks. Pooling below is BY GRID INDEX, valid because every sweep of one
    role shares one ``SweepMeta`` — asserted rather than assumed, because
    pooling by index lies silently otherwise.
    """
    first = readings[0]
    if not all(np.array_equal(r.freqs_hz, first.freqs_hz) for r in readings):
        raise SweepGridsDisagree(role)

    fund_pool = np.median(np.stack([r.fundamental_db for r in readings]), axis=0)
    fund_delta = fund_pool - float(np.median(fund_pool))

    rows: list[dict[str, Any]] = []
    for probe in PROBE_FREQUENCIES_HZ:
        if not (first.band_hz[0] <= probe <= first.band_hz[1]):
            continue
        index = int(np.argmin(np.abs(first.freqs_hz - probe)))
        row: dict[str, Any] = {
            "hz": probe,
            "fundamental_re_band_median_db": _nullable(fund_delta[index]),
        }
        for order in orders:
            values, floors, limited = [], [], []
            for reading in readings:
                at = int(np.argmin(np.abs(reading.freqs_hz - probe)))
                values.append(float(reading.relative_db[order][at]))
                floors.append(float(reading.floor_relative_db[order][at]))
                limited.append(bool(reading.floor_limited(order)[at]))
            row[f"h{order}_below_fundamental_db"] = _nullable(_median(values))
            row[f"h{order}_floor_below_fundamental_db"] = _nullable(_median(floors))
            # Majority vote, so one sweep's noise spike cannot flag a point the
            # others read as clear — the same rule the summary below pools by.
            row[f"h{order}_floor_limited"] = (
                None if not math.isfinite(_median(values))
                else sum(limited) > len(limited) / 2
            )
            row[f"h{order}_repeat_spread_db"] = _spread(values)
        row["thd_percent"] = _nullable(
            _median([
                float(r.thd_percent[int(np.argmin(np.abs(r.freqs_hz - probe)))])
                for r in readings
            ]),
            _PERCENT_DECIMALS,
        )
        rows.append(row)

    worst: dict[str, Any] = {}
    floor_fraction: dict[str, float] = {}

    for order in orders:
        pooled = np.median(
            np.stack([r.relative_db[order] for r in readings]), axis=0
        )
        limited_mask = np.stack(
            [r.floor_limited(order) for r in readings]
        ).sum(axis=0) > len(readings) / 2
        floor_fraction[f"h{order}"] = round(float(np.mean(limited_mask)), 3)
        hz, value = worst_clear_of_floor(first.freqs_hz, pooled, limited_mask)
        worst[f"h{order}"] = (
            None if not math.isfinite(value)
            else {"hz": round(float(hz), 1), "below_fundamental_db": round(value, 1)}
        )

    drives = [r.drive for r in readings]
    return {
        "role": role,
        "n_sweeps": len(readings),
        "sweep": {
            "f1_hz": round(float(first.sweep.f1), 1),
            "f2_hz": round(float(first.sweep.f2), 1),
            "L_s": round(float(first.sweep.L), 4),
            "read_band_hz": [
                round(float(first.band_hz[0]), 1),
                round(float(first.band_hz[1]), 1),
            ],
        },
        "drive": {
            "stimulus_peak_dbfs": _nullable(
                _median([d.stimulus_peak_dbfs for d in drives]), 2
            ),
            "effective_peak_dbfs": _nullable(
                _median([d.effective_peak_dbfs for d in drives]), 2
            ),
            "capture_peak_dbfs": _nullable(
                _median([d.capture_peak_dbfs for d in drives]), 2
            ),
            "capture_rms_dbfs": _nullable(
                _median([d.capture_rms_dbfs for d in drives]), 2
            ),
        },
        "images_clean": all(r.images_clean for r in readings),
        "worst_clearance_s": round(min(r.clearance_s for r in readings), 3),
        "worst": worst,
        "floor_limited_fraction": floor_fraction,
        "rows": rows,
    }


def _session_volume_db(record: Mapping[str, Any]) -> float | None:
    """The session volume a take recorded playing at, or ``None``."""
    provenance = record.get("provenance")
    return finite_float(provenance.get("session_volume_db")) if isinstance(provenance, Mapping) else None


def _recorded_volume_drive(record: Mapping[str, Any], volume_db: float | None) -> dict[str, Any]:
    """A MEASURE take's drive status: the id does not prove its recorded volume (#5012),
    so it is labelled as recorded and checked against the fader readback the take banked."""
    if volume_db is None:
        return {"status": "unknown", "effective_peak_dbfs": None, "reason": "session_volume_unrecorded"}
    provenance = record.get("provenance")
    readback = provenance.get("main_volume_db") if isinstance(provenance, Mapping) else None
    if readback is None:
        return {"status": "recorded_session_volume", "session_volume_readback": "unrecorded"}
    if not fader_matches(readback, volume_db):
        return {"status": "unknown", "effective_peak_dbfs": None, "reason": "session_volume_readback_mismatch",
                "session_volume_readback": "mismatched"}
    return {"status": "recorded_session_volume", "session_volume_readback": "matched"}


def read_round_harmonics(bundle_dir: Path) -> dict[str, Any]:
    """The reading every MEASURE and candidate-branch take of this bundle
    banked, whatever its verdict, with no recording read. A take whose analysis
    failed, or whose reading banked a gap, is refused by its reason; a take
    banked before the reading refuses the view (ADR-0394)."""
    blocks: list[dict[str, Any]] = []
    read: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    phases: Counter[str] = Counter()
    for row, document in measurement_documents(bundle_dir):
        phases[row.phase] += 1
        branch = row.graph_scope == "candidate_branches"
        if row.phase != PHASE_MEASURE and not branch:
            continue
        take = {"take_id": document.get("take_id"), "record": record_path(row),
                "wav_sha256_12": str(document.get("wav_sha256") or "")[:12], "position_deg": row.position_deg}
        if "analysis_error" in document:
            refused.append({**take, "reason": TAKE_CURVES_NOT_BANKED, "analysis_error": document["analysis_error"]})
            continue
        analysis = _FIELDS.mapping(document.get("analysis", {}), f"{take['record']} analysis")
        if "distortion" not in analysis:
            raise EvidenceUnavailable(TAKE_CURVES_NOT_BANKED, {"record": take["record"], "field": "analysis.distortion"})
        reading = analysis["distortion"]
        if reading is None:
            # Its program plays no per-driver sweep, or is a level probe.
            continue
        if _FIELDS.mapping(reading, f"{take['record']} analysis.distortion").get("status") == "unavailable":
            refused.append({**take, "reason": reading["reason"]})
            continue
        read.append({**take, "calibration": document.get("capture_calibration")})
        for block in reading["roles"]:
            drive = {**block["drive"], **({"status": "program_declared"} if branch else
                                          _recorded_volume_drive(document, _session_volume_db(document)))}
            blocks.append({**block, "wav_sha256_12": take["wav_sha256_12"], "drive": drive})
    if not blocks:
        # A round whose takes all refused for one reason refuses by it.
        reasons = {take["reason"] for take in refused}
        raise EvidenceUnavailable(reasons.pop() if len(reasons) == 1 else NO_ADMISSIBLE_CAPTURES,
                                  {"phases_seen": dict(phases), "refused": refused})
    calibrations = [take["calibration"] for take in read]
    return {
        "artifact_kind": HARMONICS_ARTIFACT_KIND,
        "orders": list(HARMONIC_ORDERS),
        "captures": {"n_read": len(read), "n_refused": len(refused), "read": read, "refused": refused},
        # The one calibration every take read was captured through, else none.
        "calibration": calibrations[0] if calibrations.count(calibrations[0]) == len(calibrations) else None,
        "roles": blocks,
    }
