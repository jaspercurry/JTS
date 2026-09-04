# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — async research domain."""
from __future__ import annotations

from ._registry import doctor_check
from ._shared import CheckResult

# Machine-stable codes naming which branch of check_research produced a
# result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_DISABLED = "research_disabled"
REASON_STORE_UNAVAILABLE = "research_store_unavailable"
REASON_COUNTS_UNAVAILABLE = "research_counts_unavailable"


@doctor_check(order=24.85, group="research")
def check_research() -> CheckResult:
    """Report async research provider/store health without private text."""
    from ...research.state import snapshot

    label = "research"
    snap = snapshot()
    provider = snap.get("provider")
    if not isinstance(provider, dict) or provider.get("configured") is not True:
        # No provider configured is the operator's own choice and it WAS
        # observed: `ok` with a reason, not a skip (ADR-0228 rule 3).
        return CheckResult(
            label, "ok", "disabled (no provider configured)",
            reason=REASON_DISABLED,
        )

    provider_id = provider.get("id") or "unknown"
    model = provider.get("model") or "default"
    store = snap.get("store")
    if not isinstance(store, dict) or store.get("available") is not True:
        path = store.get("path") if isinstance(store, dict) else "unknown"
        error = store.get("error") if isinstance(store, dict) else None
        suffix = f": {error}" if isinstance(error, str) and error else ""
        return CheckResult(
            label,
            "warn",
            f"{provider_id} configured but research store unavailable at {path}{suffix}",
            reason=REASON_STORE_UNAVAILABLE,
        )

    counts = snap.get("counts")
    if not isinstance(counts, dict):
        return CheckResult(
            label,
            "warn",
            f"{provider_id} configured but research store counts unavailable",
            reason=REASON_COUNTS_UNAVAILABLE,
        )
    return CheckResult(
        label,
        "ok",
        (
            f"{provider_id} configured ({model}); "
            f"{counts.get('running', 0)} running, "
            f"{counts.get('pending', 0)} pending announcement, "
            f"{counts.get('done', 0)} done, "
            f"{counts.get('failed', 0)} failed"
        ),
    )
