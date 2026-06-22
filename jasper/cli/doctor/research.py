# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — async research domain."""
from __future__ import annotations

from ._registry import doctor_check
from ._shared import CheckResult


@doctor_check(order=24.85, group="research")
def check_research() -> CheckResult:
    """Report async research provider/store health without private text."""
    from ...research.state import snapshot

    label = "research"
    snap = snapshot()
    provider = snap.get("provider")
    if not isinstance(provider, dict) or provider.get("configured") is not True:
        return CheckResult(label, "ok", "disabled (no provider configured)")

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
        )

    counts = snap.get("counts")
    if not isinstance(counts, dict):
        return CheckResult(
            label,
            "warn",
            f"{provider_id} configured but research store counts unavailable",
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
