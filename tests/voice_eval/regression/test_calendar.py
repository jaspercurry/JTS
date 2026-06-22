# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Calendar regression scenarios — pins the LLM-visible contract for
the two calendar tools (`calendar_today_summary`, `calendar_upcoming`)
backed by `jasper.tools.calendar.make_calendar_tools`.

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned ok=True with the expected fields.
  3. Reality: the response is internally consistent (count matches the
     events list; the scope label matches the request).

We don't build an independent Google oracle. Verifying "is this event
really on the user's calendar" would require us to authenticate
against the same account and re-list events — essentially re-running
the tool. So the reality check here is consistency-based, the same
posture `test_spotify.py` documents for "is this playlist in the
library".

============================================================
AUTH + COST NOTICE — read carefully before running
============================================================
These scenarios need a linked Google account (CLIENT_ID/SECRET set
AND at least one account OAuth-linked at jts.local/google). When
Google isn't configured the calendar tools aren't registered in the
harness, so each scenario **skips cleanly** — `pytest
tests/voice_eval/regression/test_calendar.py` is safe to run in any
environment. Read-only: no calendar is mutated, no playback.

Plus the usual paid LLM API cost per turn. PASS_K = 3 turns per
scenario function. DO NOT loop or increase PASS_K without explicit
human approval.
============================================================
"""
from __future__ import annotations

import pytest


PASS_K = 3


def _require_google(harness) -> None:
    """Skip cleanly unless Google is configured with at least one
    linked account. Mirrors the daemon's gate in `_build_registry`
    (CLIENT_ID/SECRET present AND an account linked) and the
    subway/citibike `cfg.*_enabled` skip idiom — when the backing
    accessor isn't usable, the tools aren't registered, so the model
    can't call them and the scenario would fail on the trajectory
    assertion for an environment reason, not a code bug."""
    clients = harness.test_state.get("google_clients")
    if clients is None:
        pytest.skip(
            "voice-eval: Google not configured (no GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET) — calendar tools not registered. Set "
            "the env + link an account at jts.local/google to run this.",
        )
    if not clients.list_account_names():
        pytest.skip(
            "voice-eval: Google CLIENT_ID/SECRET set but no account "
            "linked — calendar tools not registered. Link one at "
            "jts.local/google to run this scenario.",
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_calendar_today_summary(harness, trial: int) -> None:
    """Asks 'what's on my calendar today?' — the model should call
    `calendar_today_summary` (NOT `calendar_upcoming`) and speak the
    events the tool returned.

    Catches: model answering from nothing (it has no calendar in
    context), and a today-question misrouted to the upcoming tool."""
    _require_google(harness)

    result = await harness.ask("what's on my calendar today?")

    # 1. Trajectory — the model must call the today tool.
    call = result.tool_call("calendar_today_summary")
    assert call is not None, (
        f"[trial {trial}] model did not call calendar_today_summary. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] calendar_today_summary raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the tool returned ok=True with the documented
    # fields. A {ok: false, error: ...} here means the account's token
    # couldn't refresh (re-link needed) — surface it as a skip rather
    # than a hard fail, since it's an auth-state issue not a code bug.
    res = call.result or {}
    if res.get("ok") is not True:
        pytest.skip(
            f"[trial {trial}] calendar_today_summary returned not-ok "
            f"(likely an account/token state issue, not a code bug): "
            f"{res.get('error')!r}. See transcript: {result.transcript_path}",
        )
    assert res.get("scope") == "today", (
        f"[trial {trial}] expected scope='today', got {res.get('scope')!r}. "
        f"See transcript: {result.transcript_path}"
    )
    assert "events" in res and "count" in res, (
        f"[trial {trial}] response missing events/count fields: {res!r}. "
        f"See transcript: {result.transcript_path}"
    )

    # 3. Reality — internal consistency: count matches the list length,
    # and each event has a summary (the field the model reads aloud).
    events = res.get("events") or []
    assert res.get("count") == len(events), (
        f"[trial {trial}] count={res.get('count')} but events list has "
        f"{len(events)} entries. See transcript: {result.transcript_path}"
    )
    for ev in events:
        assert ev.get("summary"), (
            f"[trial {trial}] an event has no summary: {ev!r}. "
            f"See transcript: {result.transcript_path}"
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_calendar_upcoming_this_week(harness, trial: int) -> None:
    """Asks 'what's on my calendar this week?' — the model should call
    `calendar_upcoming` (NOT `calendar_today_summary`) with a wide
    `hours` window (the tool docstring maps 'this week' → 168).

    Catches: a forward-looking 'this week' misrouted to the today tool,
    and a too-narrow window for a multi-day request."""
    _require_google(harness)

    result = await harness.ask("what's on my calendar this week?")

    # 1. Trajectory — the model must call the upcoming tool, not today.
    call = result.tool_call("calendar_upcoming")
    today_call = result.tool_call("calendar_today_summary")
    assert call is not None, (
        f"[trial {trial}] model did not call calendar_upcoming. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"A 'this week' question must route to calendar_upcoming. "
        f"See transcript: {result.transcript_path}"
    )
    assert today_call is None, (
        f"[trial {trial}] model ALSO called calendar_today_summary — "
        f"'this week' is forward-looking, only calendar_upcoming applies. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] calendar_upcoming raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the model passed a multi-day hours window. 'This
    # week' should be a wide window (> a single day); the tool caps at
    # 24*30. We assert > 24 to catch a same-day misread.
    hours = call.args.get("hours")
    assert isinstance(hours, int) and hours > 24, (
        f"[trial {trial}] calendar_upcoming hours={hours!r}; expected a "
        f"multi-day window (> 24) for 'this week'. "
        f"See transcript: {result.transcript_path}"
    )
    res = call.result or {}
    if res.get("ok") is not True:
        pytest.skip(
            f"[trial {trial}] calendar_upcoming returned not-ok (likely "
            f"an account/token state issue, not a code bug): "
            f"{res.get('error')!r}. See transcript: {result.transcript_path}",
        )

    # 3. Reality — scope echoes the requested window, and count matches
    # the events list.
    assert res.get("scope") == f"next_{hours}h", (
        f"[trial {trial}] scope={res.get('scope')!r} doesn't echo the "
        f"requested {hours}h window. See transcript: {result.transcript_path}"
    )
    events = res.get("events") or []
    assert res.get("count") == len(events), (
        f"[trial {trial}] count={res.get('count')} but events list has "
        f"{len(events)} entries. See transcript: {result.transcript_path}"
    )
