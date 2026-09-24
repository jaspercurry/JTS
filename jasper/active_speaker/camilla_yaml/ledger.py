# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from jasper.biquad import PeqFilter, total_positive_boost_db

from ..profile import ActiveSpeakerPreset

if TYPE_CHECKING:
    from ..branch_chain import CrossoverSection
from .topology import _ordered_regions

BASELINE_HEADROOM_DB = 0.0


def _correction_value(
    corrections: Mapping[str, Mapping[str, float | bool]],
    role: str,
    field: str,
    default: float,
) -> float:
    value: Any = corrections.get(role, {}).get(field)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _correction_bool(
    corrections: Mapping[str, Mapping[str, float | bool]],
    role: str,
    field: str,
) -> bool:
    return bool(corrections.get(role, {}).get(field))


# Never silently mute the program through headroom absorption (ADR-0219).
MAX_PROGRAM_HEADROOM_DB = 40.0


def program_headroom_db(
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *, branch_context: Mapping[str, tuple[Sequence["CrossoverSection"], float]],
    room_peqs: Sequence[PeqFilter] = (),
    baseline_headroom_db: float = BASELINE_HEADROOM_DB,
    output_trim_db: float = 0.0,
    rear_calibration: Mapping[str, Any] | None = None,
) -> float:
    """Total program attenuation in dB, including shared gains and branch peaks."""
    rear_headroom_db = 0.0
    if rear_calibration:
        from ..branch_chain import (
            rear_branch_sum_headroom_db,  # lazy: numpy import cost (fanin imports this module for one constant)
        )

        rear_headroom_db = rear_branch_sum_headroom_db(rear_calibration)
    return (baseline_headroom_db + total_positive_boost_db(room_peqs)
            + linearization_headroom_db(linearization, branch_context=branch_context)
            + rear_headroom_db
            + max(0.0, output_trim_db))


def boost_headroom_by_role(
    *, branch_context: Mapping[str, tuple[Sequence[CrossoverSection], float]],
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    room_peqs: Sequence[PeqFilter] = (),
    session_volume_db: float | None = None,
    spl_headroom_db: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Disclose playback's program headroom cost, in dB, using the emitter's charge.

    Full-scale branch peak is session volume + trim + crossover/linearization
    peak - program absorption (dBFS). Absorption includes the largest positive
    branch peak plus its margin, so boost spends maximum SPL without raising
    the branch above the fader. Measurement excitation caps do not apply here;
    session volume and measured SPL headroom are disclosures only.
    """
    from ..branch_chain import (
        branch_chain_peak_db,  # lazy: numpy import cost (fanin imports this module for one constant)
    )

    spent = program_headroom_db(linearization, branch_context=branch_context, room_peqs=room_peqs)
    return {role: {
        "composed_boost_db": max(0.0, branch_chain_peak_db((linearization or {}).get(role, ()))),
        "program_headroom_spent_db": spent,
        "program_headroom_remaining_db": max(0.0, MAX_PROGRAM_HEADROOM_DB - spent),
        "max_program_headroom_db": MAX_PROGRAM_HEADROOM_DB,
        "session_volume_db": session_volume_db,
        "spl_headroom_db": spl_headroom_db,
        "binding": "program_headroom" if spent >= MAX_PROGRAM_HEADROOM_DB else None,
    } for role in branch_context}


def linearization_headroom_db(
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    *,
    branch_context: Mapping[str, tuple[Sequence["CrossoverSection"], float]],
) -> float:
    """Program-domain attenuation the emitted linearization boost needs, dB.

    The WORST branch's REALIZED PEAK — the largest gain any one branch chain
    (``crossover ⊗ linearization ⊗ trim``) applies to the program, plus
    :data:`jasper.active_speaker.branch_chain.HEADROOM_MARGIN_DB`. Worst branch
    rather than the sum across branches because the driver chains run in
    PARALLEL after the split, so no sample path ever sees two branches' boosts.
    A per-branch SUM of positive gains is a valid but badly loose bound: it once
    charged 22.458 dB against a branch peaking at +4.00 dB, leaving the speaker
    8.3 dB below the household's listening level at maximum volume.

    ``branch_context`` maps role to ``(crossover_sections, trim_db)`` and is
    REQUIRED: omitting it would charge the linearization cascade alone — safe
    for a charge, but wrong for any reader comparing two corrections, and wrong
    in the loud direction for a delta. :func:`_branch_context` builds it from
    the same preset and corrections the graph is emitted from.

    Public because the runtime contract's prover must agree with the emitter
    about this number and the candidate payload discloses it. The evaluation
    lives in :mod:`jasper.active_speaker.branch_chain` — one implementation.
    0.0 for a cut-only linearization.
    """
    # A branch with no positive gain cannot reach unity through a crossover and
    # a non-positive trim, so a cut-only graph is charged 0.0 without evaluating
    # anything — and without importing numpy, kept lazy on a 1 GB Pi.
    if not linearization_has_boost(linearization):
        return 0.0
    from ..branch_chain import (
        branch_headroom_db,  # lazy: numpy import cost (fanin imports this module for one constant)
    )

    worst = 0.0
    for role, filters in (linearization or {}).items():
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            continue
        sections, trim_db = branch_context.get(str(role), ((), 0.0))
        worst = max(worst, branch_headroom_db(
            [entry for entry in filters if isinstance(entry, Mapping)],
            sections=sections,
            trim_db=float(trim_db),
        ))
    return worst


def linearization_has_boost(
    linearization: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> bool:
    """Does any emitted linearization filter carry positive gain?

    The guard that keeps a cut-only graph off the chain-evaluation path
    entirely, so neither this emitter nor the runtime contract imports numpy for
    it. Sound because a cut cascade, a Linkwitz-Riley section and a non-positive
    trim are each <= 0 dB everywhere.

    Public because the adoption table asks the same question of the APPLIED
    candidate (a boosted intervention whose measured benefit is indeterminate
    fails closed): "does this graph put energy in" has one definition here.
    """
    for filters in (linearization or {}).values():
        if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
            continue
        for entry in filters:
            if not isinstance(entry, Mapping):
                continue
            gain = entry.get("gain")
            if isinstance(gain, (int, float)) and not isinstance(gain, bool) and gain > 0.0:
                return True
    return False


def _branch_context(
    preset: ActiveSpeakerPreset,
    corrections: Mapping[str, Mapping[str, float | bool]],
) -> dict[str, tuple[tuple[CrossoverSection, ...], float]]:
    """Per-role ``(crossover sections, trim_db)`` for the headroom charge.

    Built from the same two sources the graph itself is — the preset's crossover
    regions and ``corrections``' per-driver ``gain_db`` — so the chain this
    charge is computed over IS the chain the next few lines emit. The role ->
    sections half is :func:`jasper.active_speaker.branch_chain.sections_by_role`,
    shared with the session that stamps the disclosed ``headroom_cost_db``.

    Deliberately omits the bass-management and protective tweeter high-passes,
    which attenuate further still: crediting less attenuation over-charges
    rather than under-charges, and keeps this identical to what the runtime
    contract can re-derive without walking optional filters.
    """
    from ..branch_chain import (
        sections_by_role,  # lazy: numpy import cost (fanin imports this module for one constant)
    )

    return {
        role: (
            role_sections,
            float(_correction_value(corrections, role, "gain_db", 0.0)),
        )
        for role, role_sections in sections_by_role(
            _ordered_regions(preset)
        ).items()
    }
