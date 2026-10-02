# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.web import correction_crossover_v2_state as v2state

from typing import Any


from tests.crossover_v2_fixtures import (
    _seed_applied_stage_1_state,
)


def _seed_round_state() -> dict[str, Any]:
    state = _seed_applied_stage_1_state()
    v2state.save_v2_state(state)
    return state


__all__ = ["_seed_round_state"]
