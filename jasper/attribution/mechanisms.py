# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The mechanism registry — pure data, read by an engine with no per-entry
knowledge.

**Shape.** This is the repo's shipped **registry-of-declarations**: a mapping
from a stable id to a frozen spec, exactly the
``REASON_REGISTRY: dict[str, ReasonSpec]`` shape in
:mod:`jasper.active_speaker.crossover_v2_flow`. ``docs/attribution-stage-plan.md``
§2 rules out the transit-provider pattern for this deliberately — that
pattern's defining property is each plugin parsing its own env from a plain
``Mapping`` (attribution has no analogue), and its flattening step *raises* on
a duplicate id, which a library whose whole point is that one mechanism can be
selected by two lines cannot mirror.

**What WO-1 seeds, and what it does not.** Plan §7 gives WO-4 "Registry +
detectors v1 for the frozen seed set" — the eight seed mechanisms of §4, each
with the three callables §3.2 names (``signature``, ``confidence``,
``household_copy``) plus its detector module. WO-1 ships the *declaration
shape* and only the entries its own promotion path can actually produce:
**M2 and M5**. That is not tidiness, it is the §10 do-not applied to itself —
"No mechanism entry without a corpus citation carrying its evidence tier".
Several §4 rows state their tier in prose that does not reduce to one tier
word without a judgment call (M6's "now measured", for one), and guessing at
those tiers here would launder a claim into code for no benefit: nothing in
WO-1 can reach them.

**Who adds the rest, and when — because it is NOT all WO-4.** §8's critical
path is WO-1 → WO-2 → WO-3, and **WO-3 ships before WO-4**: its acceptance is
"the M1/M3 decision recorded as a finding", which this registry would refuse
today, since :func:`mechanism_spec` raises on an unregistered id. That is the
correct order rather than an obstacle. WO-3 is the reverse-null probe (P1),
and P1 is precisely the probe §4 names as load-bearing for separating M1 from
M3 — a separation **no measurement in the corpus has ever made**, which is why
both rows currently read `model-derived`. So WO-3 adds M1 and M3 *with the
evidence that fixes their tiers in hand*, and states the tier its own probe
earned rather than inheriting a guess made here. WO-4 then seeds M4, M6, and
M8 alongside the detectors that make those reachable. Adding an entry is a
one-tuple edit to :data:`_SEED`; the engine needs no change.

**M7 arrived early, and the reason is worth stating rather than inferring.**
The paragraph above once listed M7 among WO-4's four, on the sound assumption
that a mechanism becomes reachable when its *detector* does. The owner's
2026-07-30 frame-gate ruling (#1866) produced a second way to reach one: the
shipped level-frame gate ALREADY computes the comparison M7 is about — two
measured estimates of where the drivers sit — and the ruling banks that
comparison as a finding when the estimators disagree while the independent
realized-level check corroborates one of them. So M7 is registered here with
**no detector**, because none is needed for that one site: the numbers come
from a gate the flow already runs, exactly as the carve-out promotion path
takes numbers from a pipeline that already ran. WO-4 still owns M7's
*detector* — the general per-driver realized-passband comparator §4 names —
and that work adds signature/confidence callables around this same id rather
than a second entry.

**The two required fields are the do-not, made structural.**
:class:`MechanismSpec` cannot be constructed without both
``corpus_evidence_tier`` and ``corpus_citation``, so a speculative entry
fails at import rather than at review.

**``discriminating_probes`` is advisory and per-mechanism** (§3.2/§3.4): "if
this mechanism is unsure, this probe decides it". It is not, and must not
become, a fixed global discriminator chain — the workbench plan's §12
supersedes that idea and this plan does not reinstate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .vocabulary import (
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

    Args:
      id: the stable §4 seed id (``"M2"``…). Persisted on every finding.
      title: the **internal** taxonomy name. Plan §3.1's "two vocabularies,
        one artifact": this names physics, it may name hardware, and it
        appears on ops/forensic surfaces. It is never household copy — see
        :func:`jasper.attribution.findings.Finding` for the separate,
        hardware-noun-free field the household sees.
      fix_classes: every class this mechanism may route to, from the closed
        §3.3 set. More than one is normal and is not vagueness: M5's routing
        is *mechanism-conditional* (a position-variant interference null
        routes ``physical`` and never ``eq``; boundary **loading** — broad,
        minimum-phase-ish, tracking solid angle — is legitimately EQ-able),
        and M2 is both ``document_as_physics`` and ``carve``. A finding picks
        exactly one and is validated against this set.
      discriminating_probes: advisory ordering — the probe that would decide
        this mechanism when a finding is unsure. Never a global chain.
      corpus_evidence_tier: the tier of the *seed observation*, in §4's own
        vocabulary. Not the tier of any individual finding.
      corpus_citation: where in the corpus that seed observation lives, so a
        reader can check the claim rather than trust it.
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
        # Plan §4: "document_as_physics + carve". Both are real: the band is
        # carved out of the fit and the grading, AND the residue is disclosed
        # as physics rather than corrected. A finding names whichever one it
        # is actually asserting.
        fix_classes=("document_as_physics", "carve"),
        # §5: P2 corroborates ("rides with the speaker" vs "walks with the
        # mic") but CANNOT name a source; P4 (rotation) is the adjudicator
        # that makes a source-fixed claim, and would name the reflector.
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
        # Mechanism-conditional, and the split is a DETECTOR requirement, not
        # a doctrine choice (plan §4 M5, library panel 2026-07-29): a
        # position-variant interference null (narrow, tracks path difference)
        # routes `physical` and NEVER `eq`; boundary loading (broad,
        # minimum-phase-ish, tracks solid angle) permits `eq`. WO-1's
        # promotion path only ever sees identified interference nulls, so it
        # only ever routes `physical` — pinned by test.
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
        # Plan §4 M7: "`eq` (level) — and `refit` when the level error is
        # upstream in the fit's own frame". Both are real and a finding picks
        # one. The frame-gate site only ever produces the second: a
        # disagreement BETWEEN two estimates of the frame is by construction
        # upstream of any trim derived from them, so adding level cannot be
        # the answer — re-solving the frame is. Pinned by test.
        fix_classes=("eq", "refit"),
        # **A gap, declared as one — no §5 probe DECIDES M7 today.** This
        # field's contract is the strong one ("the probe that would decide
        # this mechanism when a finding is unsure"), and neither entry below
        # meets it. The probe that would is the one §4's M7 row names — a
        # per-driver passband comparison against a declared-sensitivity prior —
        # and it is not a member of §5's P1-P7 table. PROBES is closed, so
        # inventing an id for it here would be a scope change made silently in
        # the wrong file; adding it to the table is a plan change and belongs
        # in the plan.
        #
        # So this field carries the best available RAISERS rather than a
        # decider, and the weaker standing is stated rather than papered over:
        # P5 reads the inter-driver balance on a second axis, separating a real
        # level relationship from a property of where the mic was; P7 bounds
        # how much of a disagreement could be measurement spread at all.
        # Neither settles which of the two frames is right. An empty tuple was
        # the alternative and is worse — it would say "nothing helps", which is
        # false, and it would leave an ``unsure`` M7 finding unable to
        # construct at all (``Finding`` requires a recommended probe).
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

#: The registry. Keyed by mechanism id; the engine iterates it with zero
#: per-mechanism knowledge.
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
