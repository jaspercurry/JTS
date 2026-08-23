# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The attribution stage's closed vocabularies.

Small closed sets — :data:`FIX_CLASSES`, :data:`CONFIDENCE_TIERS`,
:data:`PROBES` and :data:`EVIDENCE_TIERS` — kept in their own module so they
sit BELOW everything that needs a vocabulary, as a leaf with no attribution
imports of its own. Both the mechanism registry
(:mod:`jasper.attribution.mechanisms`) and the finding artifact
(:mod:`jasper.attribution.findings`) import from here directly; the artifact
also imports the registry, for ``mechanism_spec``, which is precisely why the
vocabularies are parked in neither of them. They are enumerated rather than
counted: a cardinality is a second claim about the same list, and it is the
half that goes stale when the list grows.

Every set here is **closed on purpose**. ``docs/attribution-stage-plan.md``
§3.3 names the fix classes as "closed set, v1"; §3.2 rules confidence to a
three-word tier ("never a bare number in household copy"); §5 tabulates the
probe primitives; §4's preamble rules the corpus evidence tiers. A value
outside these sets is a typo or a scope change, and both should fail loudly at
construction rather than reach a persisted finding.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Fix classes — plan §3.3, verbatim
# --------------------------------------------------------------------------- #
#
# ``refit`` is "the fit itself is wrong; re-run it under different
# constraints", distinct from ``eq`` ("add correction here"). ``carve`` names
# the shipped behaviour of excluding a band from fitting and/or spec
# evaluation. ``physical`` covers placement, geometry, driver, or operating
# point.
#
# Relation to the shipped correction vocabulary (plan §3.3): ``fix_class`` is
# an *internal routing* field, not a second copy vocabulary. Where a finding's
# consequence is a per-bin correction limit it must resolve to the one shipped
# closed enum, ``linearization_envelope.ReasonCode`` (``carve``, for example,
# resolves through ``LIMITED_BY_SPATIAL_EXCLUSION``). Pinning that mapping at
# the copy/envelope boundary is a WO-4 acceptance item and is deliberately NOT
# done here — WO-1 owns the schema, not the routing.
FIX_CLASSES: tuple[str, ...] = (
    "delay",
    "polarity",
    "eq",
    "refit",
    "carve",
    "physical",
    "document_as_physics",
    "measure_differently",
)

# --------------------------------------------------------------------------- #
# Confidence tiers — plan §3.2 (Q6 ruling)
# --------------------------------------------------------------------------- #
#
# Ordered weakest-last so a reader can see the ladder. ``unsure`` is not a
# failure state: it is the refuse-and-recommend-the-probe outcome, and it is
# the reason ``Finding`` requires a recommended probe whenever it is used.
CONFIDENCE_CONFIDENT = "confident"
CONFIDENCE_LIKELY = "likely"
CONFIDENCE_UNSURE = "unsure"
CONFIDENCE_TIERS: tuple[str, ...] = (
    CONFIDENCE_CONFIDENT,
    CONFIDENCE_LIKELY,
    CONFIDENCE_UNSURE,
)

# --------------------------------------------------------------------------- #
# Probe primitives — plan §5's table
# --------------------------------------------------------------------------- #
#
# Ids only. A probe's cost, its owner-attendance requirement, and its
# thresholds live in the plan and (from WO-2/WO-3) in the harness; nothing
# here executes a probe. They appear on a finding as ``probes_run`` (evidence
# that was actually gathered) and ``probes_recommended`` (what would decide an
# unsure finding).
PROBE_REVERSE_NULL = "P1"
PROBE_POSITION_VARIANCE = "P2"
PROBE_TWO_LEVEL = "P3"
PROBE_ROTATION = "P4"
PROBE_DESIGN_AXIS = "P5"
PROBE_FARINA_HARMONIC = "P6"
PROBE_REPEAT_VARIANCE = "P7"
PROBES: tuple[str, ...] = (
    PROBE_REVERSE_NULL,
    PROBE_POSITION_VARIANCE,
    PROBE_TWO_LEVEL,
    PROBE_ROTATION,
    PROBE_DESIGN_AXIS,
    PROBE_FARINA_HARMONIC,
    PROBE_REPEAT_VARIANCE,
)

# --------------------------------------------------------------------------- #
# Corpus evidence tiers — plan §4's preamble, verbatim
# --------------------------------------------------------------------------- #
#
# The tier of the *seed observation* a registry entry cites, not of an
# individual finding. Plan §10's first do-not — "No mechanism entry without a
# corpus citation carrying its evidence tier" — is enforced structurally by
# ``MechanismSpec`` requiring both fields.
EVIDENCE_TIER_ADJUDICATED = "adjudicated"
EVIDENCE_TIER_CORROBORATING = "corroborating"
EVIDENCE_TIER_MODEL_DERIVED = "model_derived"
EVIDENCE_TIER_REFUTED = "refuted"
EVIDENCE_TIERS: tuple[str, ...] = (
    EVIDENCE_TIER_ADJUDICATED,
    EVIDENCE_TIER_CORROBORATING,
    EVIDENCE_TIER_MODEL_DERIVED,
    EVIDENCE_TIER_REFUTED,
)

__all__ = [
    "CONFIDENCE_CONFIDENT",
    "CONFIDENCE_LIKELY",
    "CONFIDENCE_TIERS",
    "CONFIDENCE_UNSURE",
    "EVIDENCE_TIERS",
    "EVIDENCE_TIER_ADJUDICATED",
    "EVIDENCE_TIER_CORROBORATING",
    "EVIDENCE_TIER_MODEL_DERIVED",
    "EVIDENCE_TIER_REFUTED",
    "FIX_CLASSES",
    "PROBES",
    "PROBE_DESIGN_AXIS",
    "PROBE_FARINA_HARMONIC",
    "PROBE_POSITION_VARIANCE",
    "PROBE_REPEAT_VARIANCE",
    "PROBE_REVERSE_NULL",
    "PROBE_ROTATION",
    "PROBE_TWO_LEVEL",
]
