"""Research-tool regression scenario.

Pins the LLM-visible routing for the async "research X and let me know"
shape. The harness exposes a fake ResearchScheduler, so this scenario
does NOT call the background OpenAI research provider; it only pays for
the realtime voice turn needed to prove the model calls `research`.

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. PASS_K = 3 turns for this scenario.
The tool itself uses a fake scheduler in the harness and has no network
side effect.
DO NOT loop or increase PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

import pytest


PASS_K = 3


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_research_and_let_me_know_calls_research_tool(
    harness,
    trial: int,
) -> None:
    scheduler = harness.test_state.get("research_scheduler")
    assert scheduler is not None, (
        "harness.test_state missing 'research_scheduler' — registry "
        "wiring regressed. See _build_test_registry."
    )

    result = await harness.ask(
        "research the best induction range under two thousand dollars "
        "and let me know",
    )

    call = result.tool_call("research")
    assert call is not None, (
        f"[trial {trial}] model did not call research. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"Transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] research tool raised: {call.error}. "
            f"Transcript: {result.transcript_path}",
        )

    payload = call.result or {}
    assert payload.get("ok") is True, (
        f"[trial {trial}] research returned non-ok payload: {payload!r}. "
        f"Transcript: {result.transcript_path}"
    )
    assert payload.get("confirm") == "On it -- I'll let you know."
    assert payload.get("job_id"), (
        f"[trial {trial}] research response omitted job_id: {payload!r}. "
        f"Transcript: {result.transcript_path}"
    )
