# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The mechanism registry — pure data, read by an engine with no per-entry
knowledge.

A mapping from a stable id to a frozen spec, mirroring the ``REASON_REGISTRY``
shape in :mod:`jasper.active_speaker.crossover_v2_flow`. Only mechanisms a
shipped path can actually produce are registered, per plan §10 ("no mechanism
entry without a corpus citation carrying its evidence tier"), which
:class:`MechanismSpec`'s two required fields make structural. Adding an entry
is a one-tuple edit to :data:`_SEED`. ``discriminating_probes`` is advisory and
per-mechanism (§3.2/§3.4), never a fixed global discriminator chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .closed_sets import (
    EVIDENCE_TIERS,
    EVIDENCE_TIER_ADJUDICATED,
    EVIDENCE_TIER_CORROBORATING,
    FIX_CLASSES,
    PROBES,
    PROBE_DESIGN_AXIS,
    PROBE_POSITION_VARIANCE,
    PROBE_REPEAT_VARIANCE,
    PROBE_ROTATION,
)

#: Plan §4's seed ids. Declared here as strings so a finding produced by a
#: WO-1 path and a finding produced by a WO-4 detector name the same thing.
MECHANISM_HF_REFLECTION = "M2"
MECHANISM_BOUNDARY_SBIR = "M5"
MECHANISM_LEVEL_FRAME = "M7"


class MechanismError(ValueError):
    """A mechanism declaration or lookup is malformed."""


@dataclass(frozen=True)
class MechanismSpec:
    """One mechanism's declaration. Pure data — no callables, no I/O.

    ``title`` is the INTERNAL taxonomy name: it may name hardware and appears
    on ops surfaces, never as household copy (plan §3.1's two vocabularies).
    ``fix_classes`` is every class this mechanism may route to, from the closed
    §3.3 set; more than one is normal because routing is mechanism-conditional,
    and a finding picks exactly one and is validated against this set.
    ``corpus_evidence_tier`` is the tier of the SEED observation, not of any
    individual finding, and ``corpus_citation`` says where it lives.
    """

    id: str
    title: str
    fix_classes: tuple[str, ...]
    discriminating_probes: tuple[str, ...]
    corpus_evidence_tier: str
    corpus_citation: str

    def __post_init__(self) -> None:
        if not self.id or not self.title or not self.corpus_citation:
            raise MechanismError("id, title, and corpus_citation are required")
        if not self.fix_classes:
            raise MechanismError(f"{self.id}: at least one fix class is required")
        unknown_fix = set(self.fix_classes) - set(FIX_CLASSES)
        if unknown_fix:
            raise MechanismError(
                f"{self.id}: fix classes outside the closed §3.3 set: "
                f"{sorted(unknown_fix)}"
            )
        unknown_probes = set(self.discriminating_probes) - set(PROBES)
        if unknown_probes:
            raise MechanismError(
                f"{self.id}: probes outside the §5 table: {sorted(unknown_probes)}"
            )
        if self.corpus_evidence_tier not in EVIDENCE_TIERS:
            raise MechanismError(
                f"{self.id}: corpus_evidence_tier must be one of {EVIDENCE_TIERS}"
            )


_SEED: tuple[MechanismSpec, ...] = (
    MechanismSpec(
        id=MECHANISM_HF_REFLECTION,
        title="Source-fixed HF reflection",
        # Plan §4: "document_as_physics + carve". Both are real; a finding
        # names whichever one it is actually asserting.
        fix_classes=("document_as_physics", "carve"),
        # §5: P2 corroborates but cannot name a source; P4 (rotation) is the
        # adjudicator that makes a source-fixed claim.
        discriminating_probes=(PROBE_ROTATION, PROBE_POSITION_VARIANCE),
        corpus_evidence_tier=EVIDENCE_TIER_ADJUDICATED,
        corpus_citation=(
            "plan §4 M2 — adjudicated once by S0's three-geometry physical "
            "relocation (desk / desk-edge / speaker-on-floor); corroborated "
            "by WO-0's per-position pass, tau 310 +/- 8 us (CV 2.13%) over 21 "
            "hand-screened positions in two sessions"
        ),
    ),
    MechanismSpec(
        id=MECHANISM_BOUNDARY_SBIR,
        title="Boundary/SBIR interference",
        # Mechanism-conditional, and the split is a DETECTOR requirement (plan
        # §4 M5): a position-variant interference null routes `physical` and
        # NEVER `eq`; boundary loading permits `eq`. The promotion path only
        # ever sees identified interference nulls.
        fix_classes=("physical", "eq"),
        discriminating_probes=(PROBE_POSITION_VARIANCE, PROBE_ROTATION),
        corpus_evidence_tier=EVIDENCE_TIER_CORROBORATING,
        corpus_citation=(
            "plan §4 M5 — observed in two sessions via P2 (corroborating for "
            "SBIR specifically: position-variance proves 'not source-fixed', "
            "not 'boundary'); adjudicated only as a positive control (S0 "
            "ground-plane leg); refuted for S0's 1.8 kHz dip"
        ),
    ),
    MechanismSpec(
        id=MECHANISM_LEVEL_FRAME,
        title="Inter-driver level-frame error",
        # Plan §4 M7 declares `eq` (the committed pair's REALIZED levels
        # landing apart) and `refit`. Ruling S8 added `document_as_physics` for
        # the two level DEFINITIONS differing: the handover level and the
        # passband average were never estimates of one quantity, so a re-solve
        # cannot close that gap. `refit` stays declared because the mechanism
        # can still carry a genuine upstream frame error.
        fix_classes=("eq", "refit", "document_as_physics"),
        # A gap, declared as one: no §5 probe DECIDES M7 today. The probe that
        # would — a per-driver passband comparison against a declared-sensitivity
        # prior — is not in §5's closed P1-P7 table, and adding it is a plan
        # change. These two are the best available RAISERS: P5 reads the
        # inter-driver balance on a second axis, P7 bounds how much of a
        # disagreement could be measurement spread.
        discriminating_probes=(PROBE_DESIGN_AXIS, PROBE_REPEAT_VARIANCE),
        corpus_evidence_tier=EVIDENCE_TIER_ADJUDICATED,
        corpus_citation=(
            "plan §4 M7 — adjudicated under that section's stated extension "
            "of the tier (a known intervention applied and the feature "
            "responding), NOT by a probe from the §5 table: on a 7-11 dB dark "
            "tweeter an independent hand correction moved every band "
            "300 Hz-16 kHz to within +/-0.9 dB of the reference. Corpus's "
            "largest measured defect and its only before/after listening "
            "verdict; #1667's 1.7-6.3 dB trim bias is the same row"
        ),
    ),
)

#: The registry, keyed by mechanism id.
MECHANISM_REGISTRY: Mapping[str, MechanismSpec] = MappingProxyType(
    {spec.id: spec for spec in _SEED}
)

if len({spec.id for spec in _SEED}) != len(_SEED):  # pragma: no cover - import guard
    raise MechanismError("duplicate mechanism id in the seed set")


def mechanism_spec(mechanism_id: str) -> MechanismSpec:
    """Look one up, or raise :class:`MechanismError` naming what is registered."""

    try:
        return MECHANISM_REGISTRY[mechanism_id]
    except KeyError:
        raise MechanismError(
            f"unregistered mechanism {mechanism_id!r}; registered: "
            f"{sorted(MECHANISM_REGISTRY)}"
        ) from None


__all__ = [
    "MECHANISM_BOUNDARY_SBIR",
    "MECHANISM_HF_REFLECTION",
    "MECHANISM_LEVEL_FRAME",
    "MECHANISM_REGISTRY",
    "MechanismError",
    "MechanismSpec",
    "mechanism_spec",
]
