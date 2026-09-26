# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.linearization_envelope import _MIC_TRUST_TABLE_HZ
from jasper.audio_measurement.mic_identity import MIC_TIERS

from ...repeat_floor import REPEAT_FLOOR_KIND, load_repeat_floor, stopping_thresholds
from ..contracts import POSITION_EVIDENCE_KIND
from ..feature_classification import (
    UNCERTAINTY_RANDOM,
    UNCERTAINTY_SYSTEMATIC,
    UNCERTAINTY_UNSEPARATED,
)
from ..prescription_contract import CONTRACT_COMMAND, snr_shape
from ..record_index import Measurement
from ..round_evidence import ITERATION_PLATEAU_DB, MEASURED_BENEFIT_MARGIN_DB
from .incumbent import _read_candidate
from .offline_reads import _exact_json_value, _mapping, _read_json
from .positions import _POSITIONS_SUBDIR, _banked_takes

#: What :func:`_capture_snr_block` reads off one banked take: the two
#: identities the packet's other take rows already carry, the digest of the
#: stimulus that was PLAYED (a different quantity from ``wav_sha256``, which
#: is the captured audio's), the phase that says which capture it was, and the
#: analysis block the SNR columns live in.
_TAKE_DIAGNOSTIC_FIELDS = (
    "take_id", "wav_sha256", "stimulus_wav_sha256", "phase", "diagnostic",
)

#: The substring that identifies a signal-to-noise field in a banked take's
#: flat ``diagnostic`` block. A substring rather than a name list because the
#: producer
#: (:func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`)
#: composes most names onto a ROLE the packet cannot know, so no allowlist here
#: could enumerate them.
_DIAGNOSTIC_SNR_MARKER = "snr"


def _read_take_diagnostic(path: Path) -> dict[str, Any] | None:
    """One banked take narrowed to its identity and its analysis, or ``None``.

    Takes every phase, because an SNR is an SNR whichever capture produced it.
    """
    raw, _ = _read_json(path)
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") != POSITION_EVIDENCE_KIND:
        return None
    return {field: raw.get(field) for field in _TAKE_DIAGNOSTIC_FIELDS}


def _capture_snr_block(
    session_dir: Path, rows: Sequence[Measurement],
) -> dict[str, Any]:
    """Per-capture signal-to-noise, off the round's own banked takes.

    A banked take may carry the analysis's flat ``diagnostic`` block
    (:func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`'s
    output); this publishes the SNR columns out of it, one row per take that
    carried one.

    Read from the BUNDLE, so there is nothing to attribute: a take under this
    bundle's own artifacts root is this bundle's by construction. Each capture
    is named by ``take_id`` and ``wav_sha256``, the identities the
    ``lateral_poses`` and ``positions`` rows carry, so a reader can join them.
    """
    captures: list[dict[str, Any]] = []
    non_finite: set[str] = set()
    undeclared: set[str] = set()
    declared_as: dict[str, str] = {}
    seen = 0
    for take in _banked_takes(session_dir, rows, None, _read_take_diagnostic):
        seen += 1
        diagnostic = _mapping(take.get("diagnostic"))
        if not diagnostic:
            continue
        snr = {}
        for column, value in sorted(diagnostic.items()):
            if _DIAGNOSTIC_SNR_MARKER not in column and column != "pilot_ambient":
                continue
            shape = snr_shape(column)
            if shape is None:
                undeclared.add(column)
            else:
                declared_as[column] = shape
            snr[column] = _exact_json_value(value, column, non_finite)
        captures.append({
            "take_id": take.get("take_id"),
            "wav_sha256": take.get("wav_sha256"),
            "stimulus_wav_sha256": take.get("stimulus_wav_sha256"),
            "phase": take.get("phase"),
            "snr": snr,
        })
    absent: dict[str, Any] = {}
    if not captures:
        absent = {
            "status": "not_evaluated",
            "reason": (
                f"this round banked {seen} take(s) and none of them carries a "
                "diagnostic block — the round was banked before a take carried "
                "its own analysis, or every analysis it ran produced none"
            ),
        }
    return {
        "available": bool(captures),
        **absent,
        "n_captures": len(captures),
        "n_takes_seen": seen,
        "captures": captures,
        "non_finite_fields": sorted(non_finite),
        "undeclared_fields": sorted(undeclared),
        "declared_as": dict(sorted(declared_as.items())),
        "source": f"{_POSITIONS_SUBDIR}/<take_id>.json, the diagnostic block",
        "uncertainty": CONTRACT_COMMAND,
        "note": (
            "one row per banked take that carried an analysis, named by the "
            "same take_id and wav_sha256 the lateral_poses and positions rows "
            "carry so a reader can join them. n_takes_seen is every take this "
            "round banked; the difference is takes whose record carries no "
            "diagnostic block at all"
        ),
    }


def _unmeasured_repeat_floor(absence: str, reason: str) -> dict[str, Any]:
    """The shared shape for every absence — thresholds falling back to the two
    ``round_evidence`` constants that self-describe as assumptions. ``absence``
    is the closed vocabulary a reader keys on; ``reason`` is for a human."""
    return {
        "kind": UNCERTAINTY_RANDOM,
        "available": False,
        "absence": absence,
        "reason": reason,
        "thresholds": {
            "source": "codified_assumption",
            "margin_db": MEASURED_BENEFIT_MARGIN_DB,
            "plateau_db": ITERATION_PLATEAU_DB,
            "note": (
                "both self-described assumptions in round_evidence.py, "
                "awaiting exactly this measurement"
            ),
        },
    }

#: Why the repeat floor is not available: never banked, a file that is not
#: a readable record, or a record whose aggregate row cannot yield thresholds.
#: The packet names which rather than one shared reason.
REPEAT_FLOOR_UNMEASURED = "unmeasured"

REPEAT_FLOOR_UNREADABLE = "unreadable"

REPEAT_FLOOR_UNUSABLE = "unusable"


def _repeat_floor_source(path: Path | None) -> tuple[dict[str, Any] | None, str]:
    """The banked floor, or why there is none — ``source_absent`` when no file
    was there to read, the read failure otherwise (same rule as
    :func:`applied_profile_source`)."""
    if path is None:
        return None, "source_absent"
    record = load_repeat_floor(state_path=path)
    if record is not None:
        return record, ""
    _, reason = _read_json(path)
    return None, reason or f"not a {REPEAT_FLOOR_KIND} record"


def _repeat_floor_component(
    record: dict[str, Any] | None, read_reason: str
) -> dict[str, Any]:
    """The RANDOM repeat floor as banked, or one of three honest absences."""
    if record is None and read_reason == "source_absent":
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNMEASURED,
            "unmeasured -- no banked repeat floor (calibration experiment E2); "
            "jasper-round-views repeat reads mark-take spread within and "
            "between rounds (ADR-0341)",
        )
    if record is None:
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNREADABLE,
            f"banked repeat floor could not be read ({read_reason}); re-copy it",
        )
    thresholds = stopping_thresholds(record)
    if thresholds is None:
        return _unmeasured_repeat_floor(
            REPEAT_FLOOR_UNUSABLE,
            f"banked repeat floor carries no usable {record.get('aggregate_metric')} "
            "row (a finite, positive pairwise_abs_delta_p95_db and a finite "
            "pairwise_abs_delta_median_db)",
        )
    rows = [row for row in record.get("rounds") or [] if isinstance(row, Mapping)]
    return {
        "kind": UNCERTAINTY_RANDOM,
        "available": True,
        "absence": None,
        "source": "repeat-floor.json (jts_active_speaker_repeat_floor)",
        "n_repeats": record.get("n_repeats"),
        "measured_at": record.get("measured_at"),
        "bundle_session_ids": [row.get("bundle_session_id") for row in rows],
        "graph_fingerprints": sorted(
            {
                str(row["graph_fingerprint"])
                for row in rows
                if row.get("graph_fingerprint") is not None
            }
        ),
        "aggregate_metric": record.get("aggregate_metric"),
        "metrics": record.get("metrics"),
        "thresholds": {"source": "banked_repeat_floor", **thresholds},
        "reason": "",
    }


# :mod:`~jasper.active_speaker.linearization_envelope` is the ONE place the
# mic-tier trust ceiling is defined, so it is imported rather than restated.
# The table is private there because this is the only reader outside that
# module needing the raw breakpoints rather than the composed per-bin curve
# :func:`~.linearization_envelope.mic_trust_limit` returns.
def _accuracy_budget_block(
    *,
    positions: dict[str, Any],
    round_dir: Path | None,
    repeat_floor: dict[str, Any] | None,
    repeat_floor_reason: str,
) -> dict[str, Any]:
    """Random beside systematic (ADR-0202) — juxtaposed, never pooled.

    Assembled from fields the packet/bundle already carries: nothing measured
    fresh, and no two figures ever added together, so a 0.04 dB repeat floor
    cannot read as accuracy beside a systematic bound that dwarfs it.

    Three components, each labelled its own kind and each honest about absence:

    * ``cross_seat_position_spread`` — UNSEPARATED, pointing at
      ``positions.cross_seat_sigma`` rather than re-embedding its array.
    * ``in_capture_repeat_floor`` — RANDOM, from the banked repeat floor
      (:mod:`jasper.active_speaker.repeat_floor`), ``available=False`` when
      the rig has none. Unmeasured, never defaulted to 0.0.
    * ``mic_calibration_tier`` — SYSTEMATIC, PER ROLE off ``candidate.json``'s
      ``linearization[*].mic_tier``. Roles fitted under different tiers are
      published as the disagreement they are.

    No score, no recommendation, no verdict: this juxtaposes, an LLM judges.
    """

    cross_seat = _mapping(positions.get("cross_seat_sigma"))
    cross_seat_available = bool(cross_seat.get("available"))

    candidate = _read_candidate(round_dir) if round_dir is not None else {}
    linearization = _mapping(candidate.get("linearization"))
    # Per role, never elected: two roles fitted under different tiers is a
    # fact this block discloses, not a tie one entry silently wins.
    tier_by_role = {
        str(role): str(entry["mic_tier"])
        for role, entry in linearization.items()
        if isinstance(entry, Mapping) and isinstance(entry.get("mic_tier"), str)
    }
    # dict.fromkeys, not set: dedupe with a run-stable order, since this
    # document is content-fingerprinted.
    trust_ceiling_hz_by_tier: dict[str, dict[str, float]] = {
        tier: {"full_to_hz": bp[0], "taper_zero_hz": bp[1]}
        for tier in dict.fromkeys(tier_by_role.values())
        if (bp := _MIC_TRUST_TABLE_HZ.get(tier)) is not None
    }

    return {
        "note": (
            "juxtaposes this round's RANDOM terms against the standing "
            "SYSTEMATIC bounds (ADR-0202); built from fields the "
            "packet/bundle already carries, nothing measured fresh and "
            "nothing pooled. Every component labels its OWN kind"
        ),
        "components": {
            "cross_seat_position_spread": {
                "kind": UNCERTAINTY_UNSEPARATED,
                "available": cross_seat_available,
                "n_seats": cross_seat.get("n_seats"),
                "source": "positions.cross_seat_sigma",
                "reason": (
                    "" if cross_seat_available
                    else str(cross_seat.get("reason") or "")
                ),
                "note": (
                    "the per-bin array is "
                    "positions.cross_seat_sigma.per_bin_sigma_db; not "
                    "duplicated here"
                ),
            },
            "in_capture_repeat_floor": _repeat_floor_component(
                repeat_floor, repeat_floor_reason
            ),
            "mic_calibration_tier": {
                "kind": UNCERTAINTY_SYSTEMATIC,
                "available": bool(tier_by_role),
                "tier_by_role": tier_by_role,
                "tier_vocabulary": list(MIC_TIERS),
                "trust_ceiling_hz_by_tier": trust_ceiling_hz_by_tier,
                "source": "candidate.json linearization[*].mic_tier",
                "reason": (
                    "" if tier_by_role
                    else "no banked candidate names a mic tier for this round"
                ),
            },
        },
    }
