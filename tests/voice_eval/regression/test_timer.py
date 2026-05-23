"""Timer regression scenarios.

Pins the LLM-visible contract for the four timer tools — set, list,
cancel, **update**. The update scenario is the headline case: it
reproduces the 2026-05-23 incident where the model decomposed
"make it 2 minutes" into cancel + set and spoke a bogus preamble
between them.

The scenarios share state via the session-scoped harness — each
test resets the scheduler at entry so PASS_K trials don't pollute
each other.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. Each scenario function below runs
PASS_K (3) turns. The update scenario uses TWO turns per trial
(set the timer, then update it), so its total is PASS_K × 2 = 6
turns. Ballpark cost on OpenAI Realtime: ~$1.20 per full
scenario run.

DO NOT loop or increase PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

import pytest


PASS_K = 3


def _reset_scheduler(harness) -> "object":
    """Cancel every timer in the harness's scheduler so each trial
    starts clean. Returns the scheduler for direct assertions."""
    sched = harness.test_state.get("timer_scheduler")
    assert sched is not None, (
        "harness.test_state missing 'timer_scheduler' — registry "
        "wiring regressed. See _build_test_registry."
    )
    for t in list(sched.list_active()):
        sched.cancel(t.id)
    return sched


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_update_existing_timer_uses_update_tool(
    harness, trial: int,
) -> None:
    """The headline scenario: set a 5-minute pasta timer, then ask
    to make it 2 minutes. The model MUST:
      1. Call `update_timer` (NOT cancel_timer followed by set_timer)
      2. Leave exactly ONE timer running at the end (the 2-min one)

    The pre-fix behaviour was: cancel_timer + set_timer composition,
    with a hallucinated preamble between the two calls ("I'm setting
    a five-minute pasta timer"). This test pins the atomic path."""
    sched = _reset_scheduler(harness)

    # Turn 1: establish the original timer.
    setup = await harness.ask("set a 5 minute pasta timer")
    setup_call = setup.tool_call("set_timer")
    assert setup_call is not None, (
        f"[trial {trial}] setup turn did not call set_timer. "
        f"Tools observed: "
        f"{[r.name for r in setup.tool_call_records] or 'none'}. "
        f"Transcript: {setup.transcript_path}"
    )
    assert sched.list_active(), (  # type: ignore[union-attr]
        f"[trial {trial}] no timer active after set_timer. "
        f"Transcript: {setup.transcript_path}"
    )

    # Turn 2: ask for the update. THIS is the scenario.
    result = await harness.ask("actually, make it 2 minutes instead")

    # 1. Trajectory — the model called update_timer.
    update_call = result.tool_call("update_timer")
    cancel_call = result.tool_call("cancel_timer")
    set_call = result.tool_call("set_timer")

    assert update_call is not None, (
        f"[trial {trial}] model did not call update_timer. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"This is the pre-fix failure mode — the model is "
        f"decomposing 'update' into cancel+set. "
        f"Transcript: {result.transcript_path}"
    )

    # The composition path must NOT have fired. Both cancel and set
    # together would mean the model ignored the routing rule + the
    # tool docstrings. Either one alone on this turn is also wrong
    # (cancel_timer alone deletes the timer; set_timer alone leaves
    # two timers running). We assert both are absent.
    assert cancel_call is None, (
        f"[trial {trial}] model called cancel_timer despite "
        f"update_timer being the right tool for 'make it 2 minutes'. "
        f"Transcript: {result.transcript_path}"
    )
    assert set_call is None, (
        f"[trial {trial}] model called set_timer despite the "
        f"existing timer being the target of the update. "
        f"Transcript: {result.transcript_path}"
    )
    if update_call.error:
        pytest.fail(
            f"[trial {trial}] update_timer raised: {update_call.error}. "
            f"Transcript: {result.transcript_path}"
        )

    # 2. Outcome — update_timer returned ok=True with the new duration.
    payload = update_call.result or {}
    assert payload.get("ok") is True, (
        f"[trial {trial}] update_timer returned ok=False: "
        f"{payload!r}. Transcript: {result.transcript_path}"
    )
    assert payload.get("duration_seconds") == 120, (
        f"[trial {trial}] update_timer set the wrong duration: "
        f"expected 120, got {payload.get('duration_seconds')!r}. "
        f"Args passed: {update_call.args!r}. "
        f"Transcript: {result.transcript_path}"
    )

    # 3. Reality — scheduler state has exactly one timer at 120s.
    active = sched.list_active()  # type: ignore[union-attr]
    assert len(active) == 1, (
        f"[trial {trial}] expected exactly 1 active timer, got "
        f"{len(active)}: {[(t.label, t.total_seconds) for t in active]!r}. "
        f"Transcript: {result.transcript_path}"
    )
    assert active[0].total_seconds == 120, (
        f"[trial {trial}] active timer has wrong duration: "
        f"{active[0].total_seconds}s (expected 120). "
        f"Transcript: {result.transcript_path}"
    )
    assert active[0].label == "pasta", (
        f"[trial {trial}] active timer lost its label: "
        f"{active[0].label!r} (expected 'pasta'). "
        f"Transcript: {result.transcript_path}"
    )
