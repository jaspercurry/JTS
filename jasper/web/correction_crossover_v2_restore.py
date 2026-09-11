# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The excess-boost stop restores through the normal republish and apply doors."""

from __future__ import annotations

import threading
from typing import Any, Callable

from jasper.active_speaker import baseline_profile
from jasper.active_speaker.boost_protection import config_graph_fingerprint


def current_graph_fingerprint() -> str:
    profile = baseline_profile.load_applied_baseline_profile_state()
    if profile is None or baseline_profile.applied_profile_displacement(profile) == baseline_profile.APPLIED_PROFILE_DISPLACED:
        return ""
    return config_graph_fingerprint(profile)


def bind_boost_restore(run_async: Any, camilla_factory: Any) -> Callable[[str], dict[str, Any]]:
    from jasper.web import correction_crossover_v2 as host  # lazy: host binds this seam
    from jasper.web import correction_crossover_backend as backend
    from jasper.web import correction_crossover_v2_republish as republish
    from jasper.web import correction_crossover_v2_status as status

    lock = threading.Lock()
    outcome: dict[str, Any] = {}

    def restore(graph_fingerprint: str) -> dict[str, Any]:
        with lock:
            if outcome:
                return dict(outcome)
            outcome.update(status="restore_failed", restored=False)
            state = host.load_v2_state()
            previous = status._previous_candidate_fingerprint(state)
            if not graph_fingerprint or current_graph_fingerprint() != graph_fingerprint:
                outcome["status"] = "graph_displaced"
            elif previous is None or not host._previous_candidate_paired(state):
                outcome["status"] = "previous_profile_unavailable"
            else:
                try:
                    republish.handle_v2_republish({"fingerprint": previous})
                    result = host.handle_v2_apply(
                        {"expected_candidate_fingerprint": previous}, run_async, camilla_factory,
                        status=backend.status_payload(),
                    )
                    if result.get("status") == "applied":
                        outcome.update(status="restored", restored=True)
                except host.CrossoverV2Refused as exc:
                    outcome["refusal_code"] = exc.code
            return dict(outcome)

    return restore
