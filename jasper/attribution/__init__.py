# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mechanism attribution — naming which physics owns a response feature.

The attribution stage, per ``docs/attribution-stage-plan.md`` (issue #1866).
JTS already has strong **instrument diagnosis** (is this measurement
trustworthy) and strong **prescription** (fit → predict → apply → verify); it
had almost no **mechanism attribution** — naming which physics owns a feature,
with evidence and confidence, *before* prescribing.

**What this package is, as of WO-1 (schema + persistence).**

* :mod:`~jasper.attribution.vocabulary` — the three closed sets: fix classes
  (§3.3), confidence tiers (§3.2), probe ids (§5).
* :mod:`~jasper.attribution.mechanisms` — the pure-data registry of
  declarations, the shipped ``REASON_REGISTRY`` shape (§2). Seeded with the
  two mechanisms WO-1 can reach; WO-4 seeds the rest alongside its detectors.
* :mod:`~jasper.attribution.findings` — §3.1's artifact, its validation, and
  its self-describing serialization.
* :mod:`~jasper.attribution.promotion` — the excluded-band promotion path:
  the carve-out records the pipeline already persists become findings with a
  mechanism and a fix class attached.
* :mod:`~jasper.attribution.position_evidence` — the per-position cloud
  members the pipeline computed and discarded (§6).
* :mod:`~jasper.attribution.session_identity` — the one identifier that
  survives every store hop (§6).
* :mod:`~jasper.attribution.storage` — bundle-lifetime persistence (Q-C).

**What it is not.** There is **no resident daemon** (§10) and there never will
be: attribution runs at analysis time, inside the process that already owns
the analysis. Nothing here holds state between calls, opens a socket, or
starts a thread. There is also **no detector** yet — WO-4 owns per-mechanism
signature functions — and **no UI**, which is WO-6's.

**Findings are optional evidence artifacts** (§3.4). Neither the deterministic
flow nor any future workbench is *gated* on a structured attribution verdict
existing. A session with no findings behaves exactly as it does today.
"""

from __future__ import annotations

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
    MECHANISM_REGISTRY,
    MechanismError,
    MechanismSpec,
    mechanism_spec,
)
from .position_evidence import (
    POSITION_EVIDENCE_SCHEMA,
    position_evidence_block,
)
from .promotion import promote_carve_outs
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
from .vocabulary import CONFIDENCE_TIERS, FIX_CLASSES, PROBES

__all__ = [
    "CONFIDENCE_TIERS",
    "EVIDENCE_STORE_BUNDLE",
    "EVIDENCE_STORE_CAPTURE_RING",
    "EVIDENCE_STORE_LAPTOP_ARCHIVE",
    "FINDING_SCHEMA",
    "FINDING_SET_SCHEMA",
    "FIX_CLASSES",
    "MECHANISM_BOUNDARY_SBIR",
    "MECHANISM_HF_REFLECTION",
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
    "publish_finding_set",
    "read_finding_set",
    "read_session_identity",
    "stamp_session_identity",
    "verify_finding_evidence",
]
