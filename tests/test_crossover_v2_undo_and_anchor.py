# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The manual Undo endpoint's anchor preconditions, pinned at the endpoint.

What remains of this module is the /crossover/v2/restore door's own gate:
``rollback_anchor_refusal`` names why a durable state has no restorable
anchor, and ``handle_v2_restore`` declines with exactly that refusal's
sentence. The round's automatic restore no longer reads any of this — it
republishes the prior candidate through the normal path
(``bind_delta_probe_rollback``), whose pins live in
``tests/test_crossover_v2_pin_apply_rollback.py`` and
``tests/test_crossover_v2_round_wiring.py``.

The staging is :mod:`tests.crossover_v2_round_harness`, shared with the round
suite. The two autouse fixtures are imported here directly and under the
redundant-alias form: ``pytest`` activates an autouse fixture by its presence
in this module's namespace, nothing here calls one, and the alias says the
name is deliberate without spending a lint suppression.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jasper.active_speaker import baseline_profile as baseline_profile_mod
from jasper.web import correction_crossover_v2 as v2host

from tests.crossover_v2_round_harness import (
    _bg_run_async,
    _restored_ok,
    _seed_round_state,
)
from tests.test_crossover_v2_stage_bridge import (
    _isolated_v2_state as _isolated_v2_state,
    _production_host_seams as _production_host_seams,
)


def _topology_mismatched_state() -> dict[str, Any]:
    state = _seed_round_state()
    state["pre_apply_profile"] = {
        "candidate_fingerprint": "fp-previous",
        "source": {"topology_fingerprint": "a-fingerprint-from-another-speaker"},
    }
    v2host.save_v2_state(state)
    return state


@pytest.mark.parametrize(
    ("build_state", "code"),
    [
        (
            lambda: _seed_round_state(previous_candidate=False),
            v2host.ANCHOR_NO_PRE_APPLY_PROFILE,
        ),
        (_topology_mismatched_state, v2host.ANCHOR_TOPOLOGY_CHANGED),
    ],
    ids=["no-pre-apply-profile", "topology-changed"],
)
def test_each_anchor_refusal_is_a_refusal_undo_really_makes(
    monkeypatch, build_state, code,
):
    """The named refusal code and the endpoint's decline are one predicate.

    Each case asserts the SAME state produces the named refusal code and an
    endpoint that actually declines with that refusal's own sentence — the
    refusal is the endpoint's contract, not a description beside it.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    state = build_state()

    refusal = v2host.rollback_anchor_refusal(state)

    assert refusal is not None
    assert refusal.code == code
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())
    assert str(excinfo.value) == refusal.message


def test_the_not_applied_precondition_is_the_same_one_undo_refuses_on(monkeypatch):
    """The third precondition, separated because it has no anchor to build.

    "Nothing is applied" is reached by state that never applied AND by state a
    successful Undo has just cleared, which is why it must stay a refusal
    rather than a special case of the two above.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    state = _seed_round_state()
    state["applied"] = False
    v2host.save_v2_state(state)

    refusal = v2host.rollback_anchor_refusal(state)

    assert refusal is not None
    assert refusal.code == v2host.ANCHOR_NOT_APPLIED
    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())
    assert str(excinfo.value) == refusal.message


def test_a_valid_anchor_lets_the_endpoint_through(monkeypatch):
    """The positive control the refusals need.

    Without it, a predicate that refused unconditionally would satisfy every
    pin above — and would take Undo away from every household.
    """
    monkeypatch.setattr(
        baseline_profile_mod, "restore_applied_baseline_profile", _restored_ok,
    )
    _seed_round_state()

    assert v2host.rollback_anchor_refusal(v2host.load_v2_state()) is None

    payload = v2host.handle_v2_restore(_bg_run_async, lambda: SimpleNamespace())

    assert payload["status"] == "restored"
    # …and the anchor is gone afterwards, which is what makes a second Undo
    # refuse rather than replay: the endpoint is deliberately not idempotent.
    assert v2host.load_v2_state().get("pre_apply_profile") is None
    assert v2host.load_v2_state().get("applied") is False
