# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve completed captures of an authored candidate without inheriting parent proof."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jasper.audio_measurement.bundles import (
    BundleError,
    read_artifact_manifest,
    relative_artifact_path,
    sha256_file,
)
from jasper.json_fields import finite_float

from .candidate_bank import CandidateBankRefusal, find_banked_candidate
from .measured_crossover_candidate import candidate_trial_scope


def require_candidate_trial(candidate: Any, *, root: Path | None = None) -> dict[str, Any] | None:
    """Require a captured full graph for authored candidates; legacy fits need no new proof.

    A trial establishes that these exact settings were captured, not that their
    acoustic result passed. The graph digest is the installed graph's recorded
    identity; rebuilding it here would substitute current device configuration.
    """
    if candidate.analysis.get("measurement_status") != "unmeasured":
        return None
    from .commissioning_evidence_store import EVIDENCE_ROOT  # lazy: apply-only evidence reader
    from .crossover_v2.record_index import bundle_measurements  # lazy: pulls the tuning engine
    from .crossover_v2.round_inputs import iter_round_sessions  # lazy: pulls the tuning engine

    scope = candidate_trial_scope(candidate)
    source = find_banked_candidate(candidate.fingerprint, root=root).path.parents[5]
    for bundle in iter_round_sessions(source):
        for row in bundle_measurements(bundle, candidate_id=candidate.fingerprint):
            path = bundle / EVIDENCE_ROOT / "artifacts" / row.path
            try:
                record = json.loads(path.read_text())
                if (
                    record.get("measurement_status") != "captured"
                    or record.get("candidate_id") != candidate.fingerprint
                    or record.get("graph_scope") != scope
                    or record.get("incident") != ""
                    or finite_float(record.get("level_db")) is None
                    or re.fullmatch(r"[0-9a-f]{16}", str(record.get("graph_fingerprint") or "")) is None
                ):
                    continue
                wav = relative_artifact_path(bundle, record.get("wav_path") or "")
                artifacts = read_artifact_manifest(bundle).get("artifacts", [])
                identities = {item.get("path"): item for item in artifacts}
                record_path = relative_artifact_path(bundle, path)
                if wav not in identities.get(record_path, {}).get("dependencies", []):
                    continue
                intact = True
                for relative in (record_path, wav):
                    identity = identities.get(relative, {})
                    raw = bundle / relative
                    if (
                        not identity.get("sha256") or raw.stat().st_size <= 0
                        or raw.stat().st_size != identity.get("byte_size")
                        or sha256_file(raw) != identity["sha256"]
                    ):
                        intact = False
                        break
                if not intact:
                    continue
            except (OSError, ValueError, TypeError, AttributeError, BundleError):
                continue
            return {**record, "record_path": str(path)}
    raise CandidateBankRefusal(
        "candidate_trial_required",
        "Capture this complete candidate before applying it; no intact trial of this fingerprint was found.",
    )
