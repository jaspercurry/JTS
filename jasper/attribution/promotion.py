# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The excluded-band promotion path — the embryo becomes a finding.

``docs/attribution-stage-plan.md`` §3.1: "The excluded-band tau records are
the embryo: [attribution] promotes them from 'reason to refuse EQ' to findings
with mechanism and fix class attached."

That is exactly and only what this module does. It reads the carve-out records
the cloud pipeline **already persists** — each one already carrying its band,
its tau, its r, its depth, its position-variance classification, and its
household sentence — and attaches the three things a finding adds: a
``mechanism``, a ``fix_class``, and a ``confidence`` tier with the probe that
would raise it.

**This is not a detector** (that is WO-4). No signal is analysed here, no
threshold is applied, no classification is computed. Every number and every
word comes from a record the shipped pipeline produced; promotion is a
translation, and it can only ever name a mechanism the shipped
``InterferenceNullReport`` already classified.

**Three rules the plan binds this path to, each pinned by test:**

1. **Every promoted finding is ``unsure``.** §5: "Any finding whose only
   support is P2 stays ``unsure`` with P4 as its recommended probe." The cloud
   is a P2 instrument — the shipped ``interference_nulls`` docstring says so
   itself, that position-invariance within one session "is consistent with an
   origin that travels with the speaker **or** with a path through the room
   that did not change while the session ran, and a single session cannot
   separate the two". P4 (rotation) is the adjudicator. So a promoted finding
   is never ``likely`` and never ``confident``, no matter how clean the tau.
2. **``eq`` is never routed.** §3.3's hard rule: ``eq`` is never the routed
   class for a position-variant null or a source-fixed interference ripple.
   The physics warrant is the load-bearing one (§11.3 X22) — energy added into
   a cancellation is itself cancelled, so the null is an interference zero,
   not a deficit the drive can fill. M5's registry entry permits ``eq`` for
   boundary *loading*; this path never sees loading, only identified
   interference nulls, so it never routes it.
3. **The household sentence is copied, never rewritten.** The carve-out
   record's own ``reason`` is the shipped SSOT for what a household is told
   about an excluded band (``crossover_v2_flow._carve_out_records`` composed
   with ``_null_classification_copy``). Minting a second sentence here would
   be the second computation of one verdict that §3.1 forbids — and would put
   the hardware-noun prohibition in two places instead of one.

A record this path cannot promote honestly is **left alone**, not guessed at:
a position-screen carve has no tau and no classification, and a null the
shipped gate classified ``insufficient_evidence`` has, by that gate's own
verdict, nothing to attribute. §10's "No speculative mechanisms" applies to
the promoter as much as to the registry.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Mapping, Sequence

from jasper.log_event import log_event

from .findings import EvidenceRef, Finding, FindingError
from .mechanisms import MECHANISM_BOUNDARY_SBIR, MECHANISM_HF_REFLECTION
from .session_identity import SessionIdentity
from .vocabulary import (
    CONFIDENCE_UNSURE,
    PROBE_POSITION_VARIANCE,
    PROBE_ROTATION,
)

logger = logging.getLogger(__name__)

#: Producer id written into the finding set's provenance marker.
PRODUCED_BY = "jasper.attribution.promotion.promote_carve_outs"

#: The one carve-out source that carries attributable evidence. Mirrors
#: ``crossover_v2_flow.CARVE_OUT_SOURCE_IDENTIFIED_NULL``; the sibling
#: ``position_screen`` source has no tau and no classification by
#: construction, so it is skipped rather than promoted with invented fields.
SOURCE_IDENTIFIED_NULL = "identified_null"

#: Position-variance classification -> (mechanism, routed fix class). Mirrors
#: ``interference_nulls.CLASSIFICATION_*``. ``insufficient_evidence`` is
#: deliberately absent: the shipped gate already said it could not tell.
_CLASSIFICATION_ROUTES: Mapping[str, tuple[str, str]] = {
    # Source-fixed: sat at the same frequencies at every position. §4 M2's
    # fix classes are document_as_physics + carve; `carve` is what the
    # pipeline actually did to these bins, so it is what the finding asserts.
    "position_invariant": (MECHANISM_HF_REFLECTION, "carve"),
    # Position-variant interference null -> `physical`, never `eq` (§4 M5,
    # library panel 2026-07-29: the split is a detector requirement, and the
    # discriminating move is physical).
    "position_dependent": (MECHANISM_BOUNDARY_SBIR, "physical"),
}

_EVIDENCE_KEYS = ("f_center_hz", "n", "tau_us", "r_time", "r_freq", "depth_db")


def _intervals(carve_outs: Any) -> list[Mapping[str, Any]]:
    """Flatten the persisted per-band carve-out structure, de-duplicated.

    ``carve_outs_by_band`` lists a null under **every** spec band it overlaps
    — correct for disclosure, since the null removes bins from both — but a
    straddling null is one physical feature and must become one finding.
    De-duplicate on the interval's own identity (its edges plus the
    instrument that carved it), which is stable because a registry row's
    interval is the null's own unclipped half-depth width.
    """

    if not isinstance(carve_outs, Sequence) or isinstance(carve_outs, (str, bytes)):
        return []
    seen: set[tuple[Any, Any, Any]] = set()
    flat: list[Mapping[str, Any]] = []
    for band in carve_outs:
        if not isinstance(band, Mapping):
            continue
        rows = band.get("intervals")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = (row.get("f_lo_hz"), row.get("f_hi_hz"), row.get("source"))
            if key in seen:
                continue
            seen.add(key)
            flat.append(row)
    return flat


def _band_bounds(row: Mapping[str, Any]) -> tuple[float, float] | None:
    """This record's ``(f_lo_hz, f_hi_hz)`` as real floats, or ``None``.

    ``Finding`` validates the band itself, so this is not a second validator
    — it is the narrowing that lets the call site pass a genuine
    ``tuple[float, float]`` instead of two ``Any | None`` values that only
    happen to be numbers. Persisted carve-out records come from JSON, where
    any field can be absent or the wrong type, and relying on the
    constructor to catch that made the call site statically dishonest about
    what it was passing.

    ``bool`` is excluded explicitly: ``isinstance(True, int)`` is true in
    Python, so a band edge of ``True`` would otherwise become 1.0 Hz.
    Ordering and non-negativity stay :class:`Finding`'s to enforce — one
    owner per rule.
    """

    bounds: list[float] = []
    for key in ("f_lo_hz", "f_hi_hz"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        bounds.append(number)
    return bounds[0], bounds[1]


def promote_carve_outs(
    carve_outs: Any,
    *,
    session: SessionIdentity,
    cites: Iterable[EvidenceRef],
) -> tuple[Finding, ...]:
    """Promote every attributable carve-out record to a finding.

    Args:
      carve_outs: the persisted ``carve_outs`` block from the cloud
        pipeline's result — a list of ``{band_hz, intervals, ...}``. Taking
        the *persisted dict* rather than the live ``InterferenceNullReport``
        is deliberate: it is what a bundle actually holds, so the same call
        promotes a live close and a replayed archive, and this package stays
        free of the analysis stack.
      session: the session these records belong to.
      cites: the evidence pointers every produced finding carries. At least
        one must be a commissioning-bundle citation (``Finding`` enforces it)
        — normally the cloud artifact these records were read from.

    Returns:
      Findings in ascending band order. Empty is a legitimate and common
      answer — a clean speaker carves nothing.
    """

    pointers = tuple(cites)
    out: list[Finding] = []
    for row in _intervals(carve_outs):
        if row.get("source") != SOURCE_IDENTIFIED_NULL:
            continue
        route = _CLASSIFICATION_ROUTES.get(str(row.get("classification") or ""))
        if route is None:
            continue
        mechanism, fix_class = route
        evidence: dict[str, Any] = {
            key: row[key] for key in _EVIDENCE_KEYS if row.get(key) is not None
        }
        evidence["classification"] = str(row.get("classification"))
        band_hz = _band_bounds(row)
        if band_hz is None:
            # A record that IS attributable but whose band is unusable. This
            # is a REFUSAL, not one of the two skips above, and the
            # distinction is deliberate: those skip records that are
            # correctly not attributable (§10's "no speculative mechanisms"),
            # whereas a null the shipped gate classified but whose own
            # frequency edges are missing or non-finite is a malformed
            # record, and a silent drop would hide it. Same event and same
            # continue as the ``FindingError`` arm below, which this simply
            # reaches earlier and with a better message.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=(
                    "carve-out record has no usable band: "
                    f"f_lo_hz={row.get('f_lo_hz')!r} f_hi_hz={row.get('f_hi_hz')!r}"
                ),
            )
            continue
        try:
            out.append(
                Finding(
                    mechanism=mechanism,
                    band_hz=band_hz,
                    evidence=evidence,
                    # Rule 1 — P2-only support never rises above `unsure`.
                    confidence=CONFIDENCE_UNSURE,
                    fix_class=fix_class,
                    # Rule 3 — the shipped sentence, copied.
                    household_copy=str(row.get("reason") or ""),
                    probes_run=(PROBE_POSITION_VARIANCE,),
                    probes_recommended=(PROBE_ROTATION,),
                    cites=pointers,
                )
            )
        except FindingError as exc:
            # A malformed record must not take the session's whole findings
            # set with it. It is not dropped silently either: the record
            # stays in the persisted null registry and the carve-out
            # disclosure (where a reader asking "why was this band excluded"
            # already goes), and the reason it could not be promoted is
            # named here. In practice this fires on a household sentence that
            # broke its own contract — the one failure the schema is designed
            # to catch before a household sees it.
            log_event(
                logger,
                "attribution.carve_out_promotion_refused",
                level=logging.WARNING,
                mechanism=mechanism,
                classification=str(row.get("classification")),
                error=str(exc),
            )
            continue
    out.sort(key=lambda finding: finding.band_hz)
    return tuple(out)


__all__ = ["PRODUCED_BY", "SOURCE_IDENTIFIED_NULL", "promote_carve_outs"]
