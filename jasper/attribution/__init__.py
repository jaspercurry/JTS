# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mechanism attribution — naming which physics owns a response feature.

Per ``docs/historical/attribution-stage-plan.md`` (#1866). There is no resident
daemon and no detector yet: attribution runs at analysis time, inside the
process that already owns the analysis, and holds no state between calls.
Findings are optional evidence artifacts — nothing in the deterministic flow is
gated on one existing.
"""

from __future__ import annotations

from .closed_sets import CONFIDENCE_TIERS, EVIDENCE_TIERS, FIX_CLASSES, PROBES
from .findings import (
    EVIDENCE_STORE_BUNDLE,
    EVIDENCE_STORE_CAPTURE_RING,
    EVIDENCE_STORE_LAPTOP_ARCHIVE,
    FINDING_SCHEMA,
    FINDING_SET_SCHEMA,
    EvidenceRef,
    Finding,
    FindingError,
    FindingSet,
)
from .mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
    MECHANISM_LEVEL_FRAME,
    MECHANISM_REGISTRY,
    MechanismError,
    MechanismSpec,
    mechanism_spec,
)
from .position_evidence import (
    POSITION_EVIDENCE_SCHEMA,
    position_evidence_block,
)
from .promotion import promote_carve_outs, promote_level_frame_disagreement
from .session_identity import (
    SESSION_IDENTITY_KEY,
    SESSION_IDENTITY_SCHEME,
    SessionIdentity,
    SessionIdentityError,
    read_session_identity,
    stamp_session_identity,
)
from .storage import (
    FindingEvidenceMissing,
    FindingStorageError,
    bundle_evidence_ref,
    findings_relative_path,
    publish_finding_set,
    read_finding_set,
    verify_finding_evidence,
)

__all__ = [
    "CONFIDENCE_TIERS",
    "EVIDENCE_STORE_BUNDLE",
    "EVIDENCE_STORE_CAPTURE_RING",
    "EVIDENCE_STORE_LAPTOP_ARCHIVE",
    "EVIDENCE_TIERS",
    "FINDING_SCHEMA",
    "FINDING_SET_SCHEMA",
    "FIX_CLASSES",
    "MECHANISM_BOUNDARY_SBIR",
    "MECHANISM_HF_REFLECTION",
    "MECHANISM_LEVEL_FRAME",
    "MECHANISM_REGISTRY",
    "POSITION_EVIDENCE_SCHEMA",
    "PROBES",
    "SESSION_IDENTITY_KEY",
    "SESSION_IDENTITY_SCHEME",
    "EvidenceRef",
    "Finding",
    "FindingError",
    "FindingEvidenceMissing",
    "FindingSet",
    "FindingStorageError",
    "MechanismError",
    "MechanismSpec",
    "SessionIdentity",
    "SessionIdentityError",
    "bundle_evidence_ref",
    "findings_relative_path",
    "mechanism_spec",
    "position_evidence_block",
    "promote_carve_outs",
    "promote_level_frame_disagreement",
    "publish_finding_set",
    "read_finding_set",
    "read_session_identity",
    "stamp_session_identity",
    "verify_finding_evidence",
]
