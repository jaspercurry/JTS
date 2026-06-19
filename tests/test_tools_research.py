from __future__ import annotations

import time

import pytest

from jasper.research import RUNNING, ResearchJob, ResearchStartResult
from jasper.tools.research import make_research_tools


def _by_name(fns, name: str):
    for fn in fns:
        if getattr(fn, "__jasper_tool_name__", None) == name or fn.__name__ == name:
            return fn
    raise AssertionError(f"tool {name!r} not found")


def _job(job_id: str = "abc12345") -> ResearchJob:
    return ResearchJob(
        id=job_id,
        query="research induction ranges",
        status=RUNNING,
        result=None,
        error=None,
        created_at=time.time(),
        finished_at=None,
        announced=False,
        read=False,
    )


class _Scheduler:
    def __init__(self, result: ResearchStartResult) -> None:
        self.result = result
        self.queries: list[str] = []

    def submit(self, query: str) -> ResearchStartResult:
        self.queries.append(query)
        return self.result


class _SpendCap:
    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed

    def allowed(self) -> bool:
        return self._allowed


@pytest.mark.asyncio
async def test_research_tool_accepts_and_returns_fast_confirm():
    sched = _Scheduler(ResearchStartResult(True, _job(), "unused"))
    research = _by_name(make_research_tools(sched), "research")

    result = await research(query="research induction ranges")

    assert result == {
        "ok": True,
        "confirm": "On it -- I'll let you know.",
        "job_id": "abc12345",
    }
    assert sched.queries == ["research induction ranges"]


@pytest.mark.asyncio
async def test_research_tool_returns_speakable_decline_when_scheduler_declines():
    sched = _Scheduler(
        ResearchStartResult(
            False,
            None,
            "I'm still working on other research. Try again in a moment.",
        ),
    )
    research = _by_name(make_research_tools(sched), "research")

    result = await research(query="research another thing")

    assert result == {
        "ok": False,
        "reason": "declined",
        "confirm": "I'm still working on other research. Try again in a moment.",
        "error": "I'm still working on other research. Try again in a moment.",
    }
    assert sched.queries == ["research another thing"]


@pytest.mark.asyncio
async def test_research_tool_blocks_before_submit_when_spend_cap_reached():
    sched = _Scheduler(ResearchStartResult(True, _job(), "unused"))
    research = _by_name(
        make_research_tools(sched, spend_cap=_SpendCap(False)),
        "research",
    )

    result = await research(query="research expensive thing")

    assert result["ok"] is False
    assert result["reason"] == "spend_cap_reached"
    assert "spend cap" in result["confirm"]
    assert sched.queries == []
