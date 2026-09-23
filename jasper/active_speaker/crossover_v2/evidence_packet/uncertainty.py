# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.active_speaker.linearization_envelope import _MIC_TRUST_TABLE_HZ, MIC_TIERS
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.json_fields import finite_float

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

    Every accepted take carries the analysis's flat ``diagnostic`` block
    (:func:`~jasper.audio_measurement.program_analysis.analysis_diagnostic_summary`'s
    output, written on by ``bind_position_retention``); this publishes the SNR
    columns out of it, one row per take that carried one.

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

#: The air temperature :data:`DEFAULT_SOUND_SPEED_M_S` is the conventional
#: figure for, in degrees Celsius. Published beside the distance because it is
#: the ASSUMPTION the conversion rests on and nothing here measures room
#: temperature. Dry air's speed of sound is ``331.3 + 0.606*T`` m/s with ``T``
#: in Celsius, so 343.0 is the figure at 19.3 °C and 20 °C the round number it
#: is quoted for — the 0.4 m/s between them is smaller than a 1 K error.
_SPEED_OF_SOUND_AIR_TEMPERATURE_C = 20.0

#: The two numbers a capture's gate banks beside ``gate_disclosure``, as they
#: are spelled on a POSITION row. This set exists so
#: :func:`_gate_numbers_reason` can ask whether a round's records carry the
#: fields at all, which is a different question from whether their values are
#: null.
#:
#: ``gate_entanglement_floor_hz`` is deliberately NOT here: this set decides
#: the accuracy budget's ``gate_leakage.available``, whose subject is what the
#: gate DID to the spectrum, and the room's floor survives a capture that gated
#: nothing at all.
_POSITION_GATE_NUMBER_FIELDS = frozenset({
    "gate_moved_rms_db",
    "gate_reflection_delay_ms",
})

#: The same two facts as :data:`_POSITION_GATE_NUMBER_FIELDS`, as
#: :func:`~.capture_dispatch._gate_record` spells them inside ``verify.gate``:
#: the ``gate_`` prefix is dropped because the block is already the gate.
_VERIFY_GATE_NUMBER_FIELDS = frozenset({
    "moved_rms_db",
    "reflection_delay_ms",
})

#: Where the reflector-path conversion reads its delay from.
_REFLECTOR_PATH_SOURCE = "cloud_verify.json -> null_registry.tau_ladder_us"

#: Decimal places the reflector path length is published to — millimetres. A
#: millimetre of excess path is 2.9 us of delay, already finer than anything
#: this number supports: the fitted ladder tau and the directly measured
#: arrival tau disagree by up to 7.5 % on the S0 corpus (about 22 us, or 8 mm,
#: at the ~300 us those taus were), and the assumed speed of sound moves the
#: answer 1.8 % over a 10 K room.
_REFLECTOR_PATH_DECIMALS = 3


def _gate_numbers_present(
    rows: list[dict[str, Any]], gate: dict[str, Any]
) -> bool:
    """Does ANY banked record in the round carry a gate number?

    Over both carriers — the cloud's position rows and ``verify.gate`` —
    because either one answering settles it. The ONE spelling of the
    question :func:`_gate_numbers_reason` answers "no" to and the accuracy
    budget's ``gate_leakage.available`` answers "yes" to.
    """
    return any(_POSITION_GATE_NUMBER_FIELDS & set(row) for row in rows) or bool(
        _VERIFY_GATE_NUMBER_FIELDS & set(gate)
    )


def _gate_numbers_reason(
    positions: dict[str, Any], verify: dict[str, Any]
) -> str:
    """Why this round carries no gate numbers, or ``""`` when it does.

    The sentence names both readings because the two carriers have different
    absence rules: ``verify.gate`` always spells both keys, null or not, while
    a position row is filtered by
    :data:`~jasper.attribution.position_evidence._RECORD_FIELDS`, which drops a
    ``None``. So it states what is checkable and names ``gate_floor_source`` as
    the field separating "banked before the writers existed" from "every
    capture was ungateable".

    Silent when nothing COULD have carried them: those absences are already
    reported by their own blocks.
    """
    rows = [row for row in positions.get("positions") or [] if isinstance(row, dict)]
    gate = verify.get("gate")
    gate = gate if isinstance(gate, dict) else {}
    if not rows and not gate:
        return ""
    if _gate_numbers_present(rows, gate):
        return ""
    return (
        "no banked record in this round carries gate_moved_rms_db or "
        "gate_reflection_delay_ms, so its gate survives as a sentence only, and "
        "neither number can be recovered from that prose without parsing it — "
        "which this packet will not do. Two different rounds look like this and "
        "positions[].gate_floor_source separates them: one banked before the "
        "capture-time writers gained the fields, and one every capture of which "
        "was ungateable, where there was never a number to bank"
    )


def _reflections_block(cloud: dict[str, Any], reason: str) -> dict[str, Any]:
    """How far the delayed copy travelled — the ladder's tau, converted.

    ``reflector_path_distance_m = tau_ladder_us * 1e-6 * c``, the whole
    computation: tau is ALREADY banked as
    ``honesty_mask.null_registry.tau_ladder_us``, and what was missing was the
    multiply.

    The LADDER's tau, not the arrival's: ``arrival_tau_us`` sits beside it on
    the same registry and still carries whatever a sub-minimum cluster held on
    a ``no_corroborating_arrivals`` refusal, so a distance built from it could
    be published from evidence the gate refused. The ladder's tau exists only
    after a frequency-domain and a time-domain estimator agreed within
    :data:`~jasper.audio_measurement.interference_nulls.LADDER_ARRIVAL_TOLERANCE`.

    Refuses BY NAME rather than publishing a zero: ``tau_ladder_us`` is 0.0
    when no ladder was fitted, and 0.0 metres would put the reflector at the
    microphone.
    """
    registry = _mapping(cloud.get("null_registry"))
    constants: dict[str, Any] = {
        "speed_of_sound_m_s": DEFAULT_SOUND_SPEED_M_S,
        "speed_of_sound_air_temperature_c": _SPEED_OF_SOUND_AIR_TEMPERATURE_C,
        "source": _REFLECTOR_PATH_SOURCE,
        "uncertainty": CONTRACT_COMMAND,
    }
    refusal = ""
    if not registry:
        refusal = (
            f"this round banked no interference-null registry ({reason}), so "
            "no fitted ladder delay exists to convert into a path length"
        )
    elif registry.get("reason"):
        refusal = (
            "the interference-null gate identified nothing in this round "
            f"(null_registry.reason={registry.get('reason')!r}), so its "
            "tau_ladder_us is the no-ladder sentinel rather than a delay"
        )
    tau_us = finite_float(registry.get("tau_ladder_us")) if not refusal else None
    if not refusal and (tau_us is None or tau_us <= 0.0):
        refusal = (
            "the interference-null registry reported no usable fitted ladder "
            "delay, so there is nothing to convert"
        )
    # ``tau_us is None`` cannot be reached with an empty ``refusal`` — the arm
    # above sets one for exactly that case. It is here to narrow the type for
    # the multiply below, not as a second guard.
    if refusal or tau_us is None:
        return {
            "available": False,
            "status": "not_evaluated",
            "reason": refusal,
            "tau_ladder_us": None,
            "reflector_path_distance_m": None,
            **constants,
            "note": (
                "no distance is published here and none is implied: a reader "
                "should not read the absent field as 'the reflector is close'"
            ),
        }
    return {
        "available": True,
        "tau_ladder_us": tau_us,
        "reflector_path_distance_m": round(
            tau_us * 1e-6 * DEFAULT_SOUND_SPEED_M_S, _REFLECTOR_PATH_DECIMALS
        ),
        **constants,
        "note": (
            "an EXCESS path length: how much further the delayed copy "
            "travelled than the direct sound, not a distance to a surface. "
            "Halving it for a mirror-image bounce is the reader's call and "
            "needs geometry this round does not bank. The per-capture gate "
            "delays on the positions rows are a DIFFERENT tau — one pose, one "
            "instrument, the time domain — and are published as times rather "
            "than converted, so two numbers about two reflectors cannot be "
            "read as one"
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
    reflections: dict[str, Any],
    verify: dict[str, Any],
    round_dir: Path | None,
    repeat_floor: dict[str, Any] | None,
    repeat_floor_reason: str,
) -> dict[str, Any]:
    """Random beside systematic (ADR-0202) — juxtaposed, never pooled.

    Assembled from fields the packet/bundle already carries: nothing measured
    fresh, and no two figures ever added together, so a 0.04 dB repeat floor
    cannot read as accuracy beside a systematic bound that dwarfs it.

    Four components, each labelled its own kind and each honest about absence:

    * ``cross_seat_position_spread`` — UNSEPARATED, pointing at
      ``positions.cross_seat_sigma`` rather than re-embedding its array.
    * ``in_capture_repeat_floor`` — RANDOM, from the banked repeat floor
      (:mod:`jasper.active_speaker.repeat_floor`), ``available=False`` when
      the rig has none. Unmeasured, never defaulted to 0.0.
    * ``gate_leakage`` — SYSTEMATIC: a bias one capture's window bakes in, so
      more captures at the SAME pose do not shrink it.
    * ``mic_calibration_tier`` — SYSTEMATIC, PER ROLE off ``candidate.json``'s
      ``linearization[*].mic_tier``. Roles fitted under different tiers are
      published as the disagreement they are.

    No score, no recommendation, no verdict: this juxtaposes, an LLM judges.
    """

    cross_seat = _mapping(positions.get("cross_seat_sigma"))
    cross_seat_available = bool(cross_seat.get("available"))

    rows = [row for row in positions.get("positions") or [] if isinstance(row, dict)]
    gate = _mapping(verify.get("gate"))
    gate_available = bool(reflections.get("available")) or _gate_numbers_present(
        rows, gate
    )

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
            "gate_leakage": {
                "kind": UNCERTAINTY_SYSTEMATIC,
                "available": gate_available,
                "source": (
                    "reflections.reflector_path_distance_m, "
                    "verify.gate.moved_rms_db, positions[].gate_moved_rms_db"
                ),
                "reason": (
                    "" if gate_available
                    else "no capture in this round carries a gate-disclosure "
                    "number"
                ),
            },
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
