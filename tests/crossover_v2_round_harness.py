# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

from typing import Any


from tests.crossover_v2_fixtures import (
    _seed_applied_stage_1_state,
)


_PREVIOUS_CANDIDATE_FINGERPRINT = "fp-previous"


def _seed_round_state(*, previous_candidate: bool = True) -> dict[str, Any]:
    state = _seed_applied_stage_1_state()
    state["verify_priors"]["entry_baseline"] = None
    if previous_candidate:
        state["previous_candidate_fingerprint"] = _PREVIOUS_CANDIDATE_FINGERPRINT
        state["previous_candidate_displaced_by"] = "fp-stage-1"
    v2state.save_v2_state(state)
    return state


__all__ = ["_PREVIOUS_CANDIDATE_FINGERPRINT", "_seed_round_state"]
