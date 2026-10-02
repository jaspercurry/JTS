# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Durable crossover state."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.platform.json_fields import finite_float as _finite

from .topology_prescription import candidate_topology

logger = logging.getLogger(__name__)


DEFAULT_V2_STATE_PATH = Path("/var/lib/jasper/active_speaker_crossover_v2_state.json")

__all__ = [
    "DEFAULT_V2_STATE_PATH",
    "V2ConductorSnapshot",
    "build_conductor_state",
    "candidate_summary",
]


@dataclass(frozen=True)
class V2ConductorSnapshot:
    """The capture session a state document is bound to (§5.6)."""

    session_id: str


def _candidate_octave_summary(linearization: Any) -> dict[str, dict[str, float]]:
    """Per-role OBSERVE-layer octave deficits
    (``LinearizationFit.observe_octave_summary``, achieved-minus-target dB at
    each octave center), read off the candidate's ``linearization`` dict.

    A pure projection, never a derived curve. Empty for a role whose fit never
    ran. These are per-driver fit diagnostics from the design-axis capture, not
    the spec measurement.
    """
    out: dict[str, dict[str, float]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping):
            continue
        octaves = fit.get("observe_octave_summary")
        if not isinstance(octaves, Mapping) or not octaves:
            continue
        role_octaves: dict[str, float] = {}
        for hz, value in octaves.items():
            db = _finite(value)
            if db is not None:
                role_octaves[str(hz)] = db
        if role_octaves:
            out[str(role)] = role_octaves
    return out


def _candidate_octave_reasons(
    linearization: Any,
    octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, str]]:
    """Per-role octave-band reason codes (``LinearizationFit.reason_summary``),
    the sibling of :func:`_candidate_octave_summary`'s numbers.

    Keyed off the already-projected ``octaves`` rather than recomputed, because
    ``linearization_fit._empty_fit`` returns an EMPTY
    ``observe_octave_summary`` beside a fully populated ``reason_summary``: a
    role can honestly have verdicts and no numbers, and keying off the numbers
    makes the reason set a subset of the octave set by construction.

    The numbers need this (#2638): ``observe_octave_summary`` is
    ``working_db - frame_target_db`` across the WHOLE grid, so above a driver's
    radiating band the crossover target dives at 24 dB/oct while the measurement
    floor stays put and the difference explodes positive — stopband arithmetic,
    not performance. The fit engine labels those octaves
    ``envelope_out_of_band``; this carries the label to the surface showing the
    number.

    A SEPARATE key rather than a compound value, so an older candidate carrying
    numbers with no reasons renders as it always did.
    """
    out: dict[str, dict[str, str]] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        reasons = fit.get("reason_summary")
        if not isinstance(reasons, Mapping) or not reasons:
            continue
        role_reasons = {
            str(hz): code
            for hz, code in reasons.items()
            if isinstance(code, str) and code
        }
        if role_reasons:
            out[str(role)] = role_reasons
    return out


def _candidate_octave_driver_classes(
    linearization: Any,
    octaves: Mapping[str, Mapping[str, float]],
) -> dict[str, str]:
    """Declared driver classes for the roles with octave evidence."""
    out: dict[str, str] = {}
    for role, fit in (linearization or {}).items():
        if not isinstance(fit, Mapping) or str(role) not in octaves:
            continue
        driver_class = fit.get("driver_class")
        if isinstance(driver_class, str) and driver_class:
            out[str(role)] = driver_class
    return out


def _candidate_pinned_trims(
    candidate: Any,
) -> dict[str, dict[str, float | None]]:
    """Each pinned role's shipped trim, the value it displaced, and the gap.

    Read off the candidate's ``trim_pinned`` and ``displaced_trim_db``, rather
    than asked of the session.

    The program-analysis ``trim_db`` is deliberately NOT read: on the fitted
    lane it is the pre-commit number, a different value from the
    giveback-and-normalized trim the pin displaced, so a delta against it would
    misstate what the pin changed. ``None`` means the candidate carries no
    displaced value, never a substituted zero.
    """
    out: dict[str, dict[str, float | None]] = {}
    for role, entry in (candidate.linearization or {}).items():
        if not isinstance(entry, Mapping) or entry.get("trim_pinned") is not True:
            continue
        shipped = candidate.role_attenuations_db.get(str(role))
        if shipped is None:
            continue
        raw = entry.get("displaced_trim_db")
        displaced = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        out[str(role)] = {
            "pinned_db": float(shipped),
            "displaced_db": displaced,
            "delta_db": None if displaced is None else float(shipped) - displaced,
        }
    return out


def candidate_summary(
    candidate: Any,
    *,
    topology_pinned: bool = False,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    analysis = candidate.analysis if isinstance(candidate.analysis, Mapping) else {}
    octaves = _candidate_octave_summary(candidate.linearization)
    return {
        "fingerprint": candidate.fingerprint,
        "trims_db": dict(candidate.role_attenuations_db),
        "trims_pinned": _candidate_pinned_trims(candidate),
        "crossover": candidate_topology(candidate),
        "crossover_pinned": bool(topology_pinned),
        "alignment": candidate.alignment.to_dict(),
        "alignment_confidence": analysis.get("alignment_confidence"),
        "predicted_ripple_db": analysis.get("predicted_ripple_db"),
        "alignment_objective": analysis.get("alignment_objective"),
        **{
            key: analysis.get(key)
            for key in (
                "timing_verdict",
                "timing_saved",
                "timing_verification",
                "repeat_count",
            )
        },
        "polarity_pinned": bool(analysis.get("polarity_pinned")),
        "left_anchor_lobe": analysis.get("left_anchor_lobe"),
        "linearization_outcome": str(
            getattr(candidate, "linearization_outcome", "") or ""
        ),
        "linearization_octaves": octaves,
        "linearization_octave_reasons": _candidate_octave_reasons(
            candidate.linearization, octaves
        ),
        "linearization_driver_class": _candidate_octave_driver_classes(
            candidate.linearization, octaves
        ),
    }


def build_conductor_state(
    conductor: Any,
    prior: Mapping[str, Any],
    *,
    failure_code: str | None,
    evidence: Mapping[str, Any] | None = None,
    failure_refusals: Sequence[str] = (),
    failure_detail: str = "",
    failure_roles: Sequence[str] = (),
) -> dict[str, Any]:
    """The whole document one persist writes, over the one it is replacing.

    ``prior`` is the state currently on disk (``{}`` when there is none); the
    carry-forward rules below read it, which is why this is a document builder
    rather than a projection of the conductor. This function touches no file.

    ``failure_refusals`` are the underlying admission-refusal slugs behind a
    program failure — FORENSICS, never household copy: the envelope renders
    ``failure["code"]`` through the reason registry and ignores this key.
    """

    snap = conductor.snapshot()
    same_session = prior.get("session_id") == snap.session_id
    state: dict[str, Any] = {
        "session_id": snap.session_id,
        "candidate": None,
        "failure": (
            {
                "code": failure_code,
                **({"detail": failure_detail} if failure_detail else {}),
                "at": time.time(),
                **(
                    {"refusals": [str(slug) for slug in failure_refusals]}
                    if failure_refusals
                    else {}
                ),
                **({"failed_roles": [str(role) for role in failure_roles]} if failure_roles else {}),
            }
            if failure_code
            else None
        ),
        "evidence": dict(evidence) if evidence else None,
    }
    if isinstance(prior.get("candidate"), Mapping) and same_session:
        state["candidate"] = dict(prior["candidate"])
    if state["evidence"] is None and isinstance(prior.get("evidence"), Mapping) and same_session:
        state["evidence"] = dict(prior["evidence"])
    return state
