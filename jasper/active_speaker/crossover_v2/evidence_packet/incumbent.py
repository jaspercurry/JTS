# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jasper.active_speaker.baseline_profile import (
    applied_profile_displacement,
    load_applied_baseline_profile_state,
    profile_blend_correction,
    profile_driver_corrections,
    profile_linearization,
)
from jasper.json_fields import finite_float

from ..round_inputs import recent_round_sessions, round_artifact_dir
from .offline_reads import _absence, _mapping, _read_json


def applied_profile_source(path: Path | None) -> tuple[dict[str, Any] | None, str]:
    """The applied-profile SSOT, and why there is none when there is none.

    One owner for "what is this speaker playing":
    :func:`~jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state`.
    It collapses every failure into ``None``, so the REASON is read separately
    on that path. A file that parsed but the loader rejected has three causes,
    and rather than re-derive that verdict here the reason ECHOES the
    document's own three self-describing fields.
    """

    if path is None:
        return None, "no applied baseline profile was supplied"
    profile = load_applied_baseline_profile_state(path)
    if profile is not None:
        return profile, ""
    raw, reason = _read_json(path)
    if reason:
        return None, reason
    document = _mapping(raw)
    return None, (
        "the file is not an applied baseline profile this install can read "
        f"(kind={document.get('kind')!r}, "
        f"artifact_schema_version={document.get('artifact_schema_version')!r}, "
        f"status={document.get('status')!r})"
    )


def _read_candidate(round_dir: Path) -> dict[str, Any]:
    """One round's ``candidate.json`` as a plain mapping, without revalidation."""
    raw, _reason = _read_json(round_dir / "candidate.json")
    return _mapping(raw)

#: How many rounds of structural history are carried.
STRUCTURAL_HISTORY_MAX_ROUNDS = 8

#: How many recent bundles :func:`_structural_history_block` looks at before
#: giving up on finding :data:`STRUCTURAL_HISTORY_MAX_ROUNDS` rounds that
#: banked a candidate. Wider than the round count: commissioning and
#: calibration bundles carry no ``candidate.json`` and are skipped rather than
#: counted against the budget.
_STRUCTURAL_HISTORY_BUNDLE_SCAN_LIMIT = 32

#: EVERY structural axis a round re-derives, in report order — the axes the
#: three prescription classes exist to pin (:mod:`.driver_prescription`'s
#: ``pinned_trim_db``, :mod:`.alignment_prescription`'s ``delay_us`` and
#: ``polarity``, :mod:`.topology_prescription`'s corner). Declared once so the
#: history below is a loop: an axis named here appears on every row, and an
#: axis left out is a silent re-derivation.
STRUCTURAL_HISTORY_AXES: tuple[str, ...] = (
    "trim_db",
    "delay_us",
    "polarity",
    "crossover_fc_hz",
)


def _structural_axes_of(candidate: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """One candidate's committed value for each :data:`STRUCTURAL_HISTORY_AXES`.

    Every axis answers with the same two keys — ``value`` and ``pinned``
    (``None`` where the candidate banks no such bit) — so a reader walks the
    axes rather than learning a shape per axis.

    ONE frame per axis, never across artifacts: every value is read off
    ``candidate.json``, so ``polarity`` stays the candidate's own action word,
    a flip RELATIVE to the declared ``upper_polarity``. The applied profile's
    ABSOLUTE per-role ``inverted`` flags would put two rows in two frames on
    any speaker whose draft declares an inverted branch; that conversion's one
    owner is ``commanded.profile_graph_summation``.
    """
    linearization = _mapping(candidate.get("linearization"))
    alignment = _mapping(candidate.get("alignment"))
    analysis = _mapping(candidate.get("analysis"))
    region = next(
        iter(
            _mapping(candidate.get("source_preset")).get("crossover_regions") or ()
        ),
        None,
    )
    polarity = alignment.get("polarity")
    values: dict[str, tuple[Any, Any]] = {
        "trim_db": (
            {
                str(role): float(value)
                for role, value in _mapping(
                    candidate.get("role_attenuations_db")
                ).items()
                if finite_float(value) is not None
            },
            {
                str(role): bool(_mapping(entry).get("trim_pinned") is True)
                for role, entry in linearization.items()
            },
        ),
        "delay_us": (finite_float(alignment.get("delay_us")), None),
        "polarity": (
            polarity if isinstance(polarity, str) and polarity else None,
            (
                bool(analysis.get("polarity_pinned"))
                if "polarity_pinned" in analysis
                else None
            ),
        ),
        "crossover_fc_hz": (
            finite_float(_mapping(region).get("fc_hz")), None,
        ),
    }
    return {
        axis: {"value": values[axis][0], "pinned": values[axis][1]}
        for axis in STRUCTURAL_HISTORY_AXES
    }


def _structural_history_block(session_dir: Path) -> dict[str, Any]:
    """Candidate axes across recent live or banked rounds, oldest first."""

    try:
        bundles = recent_round_sessions(
            session_dir, limit=_STRUCTURAL_HISTORY_BUNDLE_SCAN_LIMIT
        )
    except OSError:
        bundles = []

    newest_first: list[dict[str, Any]] = []
    for bundle_dir in bundles:
        round_dir, _reason = round_artifact_dir(bundle_dir)
        if round_dir is None:
            continue
        candidate = _read_candidate(round_dir)
        if _mapping(candidate.get("analysis")).get("measurement_status") == "unmeasured":
            continue
        axes = _structural_axes_of(candidate)
        # Emptiness, not falsiness: a committed delay of exactly 0.0 µs and a
        # polarity of ``keep`` are both readings.
        if all(
            entry["value"] is None or entry["value"] == {}
            for entry in axes.values()
        ):
            continue
        newest_first.append({"round_id": round_dir.name, "axes": axes})
        if len(newest_first) >= STRUCTURAL_HISTORY_MAX_ROUNDS:
            break

    oldest_first = list(reversed(newest_first))
    return {
        "available": bool(oldest_first),
        "max_rounds": STRUCTURAL_HISTORY_MAX_ROUNDS,
        "axes": list(STRUCTURAL_HISTORY_AXES),
        "rounds_covered": len(oldest_first),
        "rounds": [
            {"ordinal": index + 1, **entry}
            for index, entry in enumerate(oldest_first)
        ],
        "source": (
            "candidate.json role_attenuations_db / alignment / source_preset "
            "corner, across recent live or banked rounds"
        ),
        "note": (
            "oldest first, so a monotonic walk reads left to right. Values "
            "only -- no drift verdict. Every row answers for every axis; a "
            "null is 'this candidate banks none', never a substituted "
            "default, and 'pinned': null is 'the candidate banks no pin bit "
            "for this axis'. polarity is the candidate's own action word, a "
            "flip relative to the DECLARED polarity, so two rows compare and "
            "neither states an absolute wiring. History legitimately starts "
            "wherever the household's retained bundles do; rounds_covered "
            "states how many this reading actually found, bounded at "
            "max_rounds"
        ),
    }


def _incumbent_block(
    receipt: dict[str, Any],
    reason: str,
    profile: dict[str, Any] | None,
    profile_reason: str,
    state: Mapping[str, Any],
    statefile_path: Path | None,
) -> dict[str, Any]:
    """What the speaker is PLAYING — three records, two questions.

    The BLEND correction is recorded in two places and reported side by side
    rather than reconciled: the receipt's
    ``round_measurements.blend.incumbent`` (what the round said it derived
    from) and the applied profile's ``blend_correction`` (what the graph
    carried). They should agree, and reconciling them is a judgement this
    module does not make.

    ``linearization`` is the same question asked of the other prescription
    class, read through
    :func:`~jasper.active_speaker.baseline_profile.profile_linearization`,
    which owns WHICH copy of that field is authoritative.

    The applied-profile SSOT answers both halves and the flow state does not:
    what the flow state records names the graph live BEFORE the last v2 apply,
    so it is one apply behind after any v2 apply and arbitrarily behind after
    an apply through a door that never touches v2 state.

    That is load-bearing because a per-driver prescription is a TOTAL for every
    role it names, so a role's incumbent filters are DELETED by any document
    that names the role and does not repeat them.

    ``identity`` says WHICH profile the answer describes. ``config.path`` is
    not among its fields — the packet excludes absolute paths, and
    ``config.sha256`` names the same graph. Its ``applied_profile_displacement``
    is the question one layer up (#2537, #3316): is this record still what the
    speaker is PLAYING, answered against ``statefile_path`` — a CamillaDSP
    statefile banked at the SAME time as the profile, never a live read, so a
    packet rebuilt away from the box it describes reports what was true at
    bank time and not the reading machine's own state. ``None`` (no statefile
    supplied) reads as unknown, not as agreement.

    ``trim`` is a fourth record, LEVEL rather than shape: see
    :func:`_incumbent_trim_block`.
    """

    blend = _mapping(_mapping(receipt.get("round_measurements")).get("blend"))
    from_receipt = blend.get("incumbent")
    # ``profile_blend_correction`` and not an attribute read: it owns the same
    # snapshot-first authority rule ``profile_linearization`` owns, and it
    # keeps ``None`` (no readable profile) apart from ``()`` (a profile that
    # applied none) — the distinction this block's two consumers both need.
    from_profile = profile_blend_correction(profile)
    linearization = profile_linearization(profile)
    trim = _incumbent_trim_block(profile, state)
    return {
        "from_round_receipt": (
            from_receipt
            if from_receipt is not None
            else _absence(reason, False, "round_measurements.blend.incumbent")
        ),
        "from_applied_profile": (
            list(from_profile)
            if from_profile is not None
            # ``profile_blend_correction`` returns ``()`` for a profile that
            # applied no blend, so ``None`` beside a READABLE profile can only
            # be a malformed record — a different fact from a missing one, and
            # ``_absence``'s bare ``field_null`` would spell them the same.
            else _absence(
                profile_reason
                or "the profile is readable but its blend_correction is not a list",
                False,
                "applied_baseline_profile.blend_correction",
            )
        ),
        "identity": (
            {
                "candidate_fingerprint": profile.get("candidate_fingerprint"),
                "applied_at": profile.get("applied_at"),
                "config_sha256": _mapping(profile.get("config")).get("sha256"),
                "applied_profile_displacement": (
                    applied_profile_displacement(
                        profile, statefile_path=statefile_path
                    )
                    if statefile_path is not None
                    else _absence(
                        "no CamillaDSP statefile was supplied",
                        False,
                        "camilla_statefile",
                    )
                ),
                "note": (
                    "which applied profile the filters below describe. A "
                    "packet built from a bank names the profile that was live "
                    "when the bank was pulled, not the one live now"
                ),
            }
            if profile
            else _absence(profile_reason, False, "applied_baseline_profile")
        ),
        "note": (
            "a prescription is a TOTAL, not a delta: prescribe the whole "
            "correction the next round should apply, incumbent included"
        ),
        "linearization": {
            # Keyed on the PROFILE, not on what it holds: an empty
            # linearization says the branches carry nothing, and only a
            # missing profile leaves the question unanswered. Filters are
            # copied VERBATIM — the profile stores exactly
            # `{biquad_type, freq, q, gain}`, and rounding would cost a reader
            # the ability to reproduce the cascade the speaker is playing.
            "from_applied_profile": (
                {
                    str(role): list(filters)
                    for role, filters in sorted(linearization.items())
                    if isinstance(role, str) and role.strip()
                }
                if profile
                else _absence(
                    profile_reason, False, "applied_baseline_profile.linearization"
                )
            ),
            "source": (
                "applied_baseline_profile.recomposition_snapshot.linearization, "
                "falling back to applied_baseline_profile.linearization"
            ),
            "note": (
                "the per-driver correction each branch is already carrying. A "
                "driver prescription is a TOTAL for every role it names: every "
                "filter listed here for a role you name and do not repeat is "
                "DELETED from the graph. A document may carry any filter listed "
                "here, shelves included, so repeat what you mean to keep. A "
                "shelf must LEAD its role's chain (or, a Highshelf taper, end "
                "it after a Lowshelf lead); the door refuses any other "
                "placement by name"
            ),
        },
        "trim": trim,
    }


def _incumbent_trim_block(
    profile: dict[str, Any] | None, state: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per role: what is APPLIED now, and what THIS round's own solve wants.

    ``applied_db`` reads the applied-profile SSOT's ``corrections`` (never the
    flow state's Undo stash — one apply behind, same as :func:`_incumbent_block`
    everywhere else). ``round_resolved_db`` reads the flow state's own
    ``candidate.trims_db`` — the trim this round's measurement produced —
    except for a role a prescription pinned this round, where that field holds
    the PIN rather than the solve it displaced; the solve is what
    ``candidate.trims_pinned[role].displaced_db`` banks instead
    (``durable_state._candidate_pinned_trims``).

    A role missing either half reports ``None``, never a substituted 0.0.
    """

    applied = profile_driver_corrections(profile)
    candidate = _mapping(state.get("candidate"))
    resolved = _mapping(candidate.get("trims_db"))
    pinned = _mapping(candidate.get("trims_pinned"))
    out: dict[str, dict[str, Any]] = {}
    for role in sorted(set(applied) | set(resolved)):
        applied_db = finite_float(_mapping(applied.get(role)).get("gain_db"))
        resolved_db = (
            finite_float(_mapping(pinned[role]).get("displaced_db"))
            if role in pinned
            else finite_float(resolved.get(role))
        )
        out[role] = {
            "applied_db": applied_db,
            "round_resolved_db": resolved_db,
            "delta_db": (
                None if applied_db is None or resolved_db is None
                else resolved_db - applied_db
            ),
            "pinned_this_round": role in pinned,
        }
    return out
