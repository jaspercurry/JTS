# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import time

import pytest

from jasper.conversation_history import CAPTURE_ALIAS_ENV
from jasper.research import DONE, RUNNING, ResearchJob, ResearchStartResult
from jasper.tools.research import make_research_tools


def _by_name(fns, name: str):
    for fn in fns:
        if getattr(fn, "__jasper_tool_name__", None) == name or fn.__name__ == name:
            return fn
    raise AssertionError(f"tool {name!r} not found")


def _job(
    job_id: str = "abc12345",
    *,
    status=RUNNING,
    result: str | None = None,
) -> ResearchJob:
    return ResearchJob(
        id=job_id,
        query="research induction ranges",
        status=status,
        result=result,
        error=None,
        created_at=time.time(),
        finished_at=None if status == RUNNING else time.time(),
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


class _DecisionScheduler:
    def __init__(self, job: ResearchJob) -> None:
        self.job = job
        self.announced: list[str] = []
        self.read: list[str] = []

    def submit(self, _query: str) -> ResearchStartResult:
        raise AssertionError("read_research_result must not submit research")

    def get(self, job_id: str) -> ResearchJob | None:
        return self.job if job_id == self.job.id else None

    def mark_announced(self, job_id: str) -> None:
        self.announced.append(job_id)

    def mark_read(self, job_id: str) -> None:
        self.read.append(job_id)


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


@pytest.mark.asyncio
async def test_read_research_result_yes_marks_read_and_records_delivery():
    job = _job(
        "read123",
        status=DONE,
        result="Induction is fast and efficient.",
    )
    sched = _DecisionScheduler(job)
    recorded: list[tuple[str, str | None, str]] = []
    read_result = _by_name(
        make_research_tools(
            sched,
            record_delivery=lambda j, text, decision: recorded.append(
                (j.id, text, decision)
            ),
        ),
        "read_research_result",
    )

    result = await read_result(job_id="read123", decision="yes")

    assert result == {
        "ok": True,
        "decision": "yes",
        "job_id": "read123",
        "text": "Induction is fast and efficient.",
    }
    assert sched.announced == ["read123"]
    assert sched.read == ["read123"]
    assert recorded == [("read123", "Induction is fast and efficient.", "yes")]


@pytest.mark.asyncio
async def test_read_research_result_no_uses_chat_log_line_when_capture_enabled(
    monkeypatch,
):
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1")
    job = _job("no123", status=DONE, result="Report text.")
    sched = _DecisionScheduler(job)
    recorded: list[tuple[str, str | None, str]] = []
    read_result = _by_name(
        make_research_tools(
            sched,
            record_delivery=lambda j, text, decision: recorded.append(
                (j.id, text, decision)
            ),
        ),
        "read_research_result",
    )

    result = await read_result(job_id="no123", decision="no")

    assert result["ok"] is True
    assert result["decision"] == "no"
    assert result["text"] == "Okay, you can find it in your chat log anytime."
    assert sched.announced == ["no123"]
    assert sched.read == []
    assert recorded == [
        ("no123", "Okay, you can find it in your chat log anytime.", "no"),
    ]


@pytest.mark.asyncio
async def test_read_research_result_no_uses_saved_line_when_capture_disabled(
    monkeypatch,
):
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "0")
    job = _job("no456", status=DONE, result="Report text.")
    sched = _DecisionScheduler(job)
    read_result = _by_name(make_research_tools(sched), "read_research_result")

    result = await read_result(job_id="no456", decision="no")

    assert result["text"] == "Okay, I've saved it for you."
    assert sched.announced == ["no456"]
    assert sched.read == []


# ---------------------------------------------------------------------------
# Single-constant dedup: empty-result fallback
# ---------------------------------------------------------------------------

def test_research_empty_result_text_single_source() -> None:
    """RESEARCH_EMPTY_RESULT_TEXT must be a single constant shared by both
    jasper.research (the canonical home) and jasper.tools.research (the tool
    that uses it).  Identity — not just equality — confirms no re-inline
    duplication.  If this test fails, someone re-inlined the string literal.
    """
    from jasper.research import RESEARCH_EMPTY_RESULT_TEXT as pkg_const
    from jasper.tools.research import RESEARCH_EMPTY_RESULT_TEXT as tool_const

    assert tool_const is pkg_const, (
        "jasper.tools.research.RESEARCH_EMPTY_RESULT_TEXT is not the same "
        "object as jasper.research.RESEARCH_EMPTY_RESULT_TEXT — "
        "the empty-result fallback string has been re-duplicated"
    )


@pytest.mark.asyncio
async def test_read_research_result_yes_uses_empty_result_constant_when_job_has_no_text():
    """When decision='yes' and job.result is empty/None, the tool must return
    RESEARCH_EMPTY_RESULT_TEXT — not a re-inlined copy of the same string.
    """
    from jasper.research import RESEARCH_EMPTY_RESULT_TEXT

    job = _job("empty99", status=DONE, result=None)
    sched = _DecisionScheduler(job)
    read_result = _by_name(make_research_tools(sched), "read_research_result")

    result = await read_result(job_id="empty99", decision="yes")

    assert result["ok"] is True
    assert result["text"] == RESEARCH_EMPTY_RESULT_TEXT
    assert sched.announced == ["empty99"]
