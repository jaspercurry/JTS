# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
This file now has two scenario families:
  - research(...) routing: PASS_K turns.
  - read_research_result(...) confirmation routing:
    2 decisions × PASS_K turns.
DO NOT loop or increase PASS_K without explicit human approval.
============================================================
"""
from __future__ import annotations

import time

import pytest

from jasper.research import DONE, ResearchJob


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


@pytest.mark.parametrize("decision", ["yes", "no"])
@pytest.mark.parametrize("trial", range(PASS_K))
async def test_research_ready_confirmation_calls_read_research_result(
    harness,
    decision: str,
    trial: int,
) -> None:
    scheduler = harness.test_state.get("research_scheduler")
    assert scheduler is not None, (
        "harness.test_state missing 'research_scheduler' — registry "
        "wiring regressed. See _build_test_registry."
    )
    job_id = f"eval{decision}{trial}"
    _seed_done_research_job(
        scheduler,
        job_id=job_id,
        query="research induction cooktops",
        result_text="Induction is fast and efficient.",
    )

    result = await harness.ask(
        "The speaker just asked: 'Your research is ready — want me to "
        f"read it now?' I am answering {decision} for research result "
        f"{job_id}.",
    )

    call = result.tool_call("read_research_result")
    assert call is not None, (
        f"[trial {trial} {decision}] model did not call "
        "read_research_result. Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"Transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial} {decision}] read_research_result raised: "
            f"{call.error}. Transcript: {result.transcript_path}",
        )

    assert call.args.get("job_id") == job_id
    assert call.args.get("decision") == decision
    payload = call.result or {}
    assert payload.get("ok") is True
    assert payload.get("decision") == decision
    if decision == "yes":
        assert payload.get("text") == "Induction is fast and efficient."
    else:
        assert payload.get("text") in {
            "Okay, you can find it in your chat log anytime.",
            "Okay, I've saved it for you.",
        }


def _seed_done_research_job(
    scheduler,
    *,
    job_id: str,
    query: str,
    result_text: str,
) -> None:
    now = time.time()
    job = ResearchJob(
        id=job_id,
        query=query,
        status=DONE,
        result=result_text,
        error=None,
        created_at=now,
        finished_at=now,
        announced=False,
        read=False,
    )
    scheduler._jobs[job.id] = job
    scheduler._store.add(job)
