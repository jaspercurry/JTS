"""Gmail regression scenarios — pins the LLM-visible contract for the
two gmail tools (`gmail_unread_summary`, `gmail_read_thread`) backed by
`jasper.tools.gmail.make_gmail_tools`.

Each scenario follows the same three-assertion shape as
`test_subway.py`:

  1. Trajectory: the model called the expected tool.
  2. Outcome: the tool returned ok=True with the expected fields.
  3. Reality: the response is internally consistent (count matches the
     messages list; the read thread echoes the requested thread_id).

The `gmail_read_thread` scenario is two turns by design: the tool
docstring tells the model to use a `thread_id` from a prior
`gmail_unread_summary` and "don't fabricate one", so the scenario
summarizes first to obtain a real id, then reads it — mirroring the
two-turn shape in `test_timer.py`.

We don't build an independent Gmail oracle — verifying "is this
message really unread" would re-run the tool. Reality checks are
consistency-based, the posture `test_spotify.py` documents.

============================================================
AUTH + COST NOTICE — read carefully before running
============================================================
These scenarios need a linked Google account (CLIENT_ID/SECRET set
AND at least one account OAuth-linked at jts.local/google). When
Google isn't configured the gmail tools aren't registered in the
harness, so each scenario **skips cleanly** — `pytest
tests/voice_eval/regression/test_gmail.py` is safe to run in any
environment. Read-only: no mail is sent, read-state is unchanged
(metadata/full reads don't mark messages read), no playback.

Plus the usual paid LLM API cost per turn. The read-thread scenario
uses TWO turns per trial (summary, then read), so its total is
PASS_K × 2 = 6 turns. DO NOT loop or increase PASS_K without explicit
human approval.
============================================================
"""
from __future__ import annotations

import pytest


PASS_K = 3


def _require_google(harness) -> None:
    """Skip cleanly unless Google is configured with at least one
    linked account. Mirrors the daemon's gate in `_build_registry`
    (CLIENT_ID/SECRET present AND an account linked) — when the
    backing accessor isn't usable the tools aren't registered, so the
    model can't call them and the scenario would fail on trajectory
    for an environment reason, not a code bug."""
    clients = harness.test_state.get("google_clients")
    if clients is None:
        pytest.skip(
            "voice-eval: Google not configured (no GOOGLE_CLIENT_ID / "
            "GOOGLE_CLIENT_SECRET) — gmail tools not registered. Set the "
            "env + link an account at jts.local/google to run this.",
        )
    if not clients.list_account_names():
        pytest.skip(
            "voice-eval: Google CLIENT_ID/SECRET set but no account "
            "linked — gmail tools not registered. Link one at "
            "jts.local/google to run this scenario.",
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_gmail_unread_summary(harness, trial: int) -> None:
    """Asks 'any new emails?' — the model should call
    `gmail_unread_summary` and speak a count.

    Catches: model answering from nothing (it has no inbox in
    context), and the tool's count/messages shape drifting."""
    _require_google(harness)

    result = await harness.ask("any new emails?")

    # 1. Trajectory — the model must call the unread-summary tool.
    call = result.tool_call("gmail_unread_summary")
    assert call is not None, (
        f"[trial {trial}] model did not call gmail_unread_summary. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] gmail_unread_summary raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — ok=True with the documented fields. A not-ok here is
    # an account/token state issue (re-link needed), surfaced as a skip
    # rather than a hard fail.
    res = call.result or {}
    if res.get("ok") is not True:
        pytest.skip(
            f"[trial {trial}] gmail_unread_summary returned not-ok (likely "
            f"an account/token state issue, not a code bug): "
            f"{res.get('error')!r}. See transcript: {result.transcript_path}",
        )
    assert "messages" in res and "count" in res, (
        f"[trial {trial}] response missing messages/count fields: {res!r}. "
        f"See transcript: {result.transcript_path}"
    )

    # 3. Reality — count matches the list length, and each message
    # carries the from/subject fields the model reads aloud.
    messages = res.get("messages") or []
    assert res.get("count") == len(messages), (
        f"[trial {trial}] count={res.get('count')} but messages list has "
        f"{len(messages)} entries. See transcript: {result.transcript_path}"
    )
    for msg in messages:
        assert "from" in msg and msg.get("subject"), (
            f"[trial {trial}] a message is missing from/subject: {msg!r}. "
            f"See transcript: {result.transcript_path}"
        )


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_gmail_read_thread_uses_prior_id(harness, trial: int) -> None:
    """Two turns: 'any new emails?' to populate a thread list, then
    'read me the first one'. The model MUST call `gmail_read_thread`
    with a `thread_id` that came from the summary (not a fabricated
    one), and the read response must echo that id.

    Catches: the model fabricating a thread_id, and the read tool's
    thread_id/subject/messages shape drifting. Skips if the inbox has
    no unread messages to read (an environment condition, not a bug)."""
    _require_google(harness)

    # Turn 1: summary to obtain a real thread_id.
    summary = await harness.ask("any new emails?")
    summary_call = summary.tool_call("gmail_unread_summary")
    assert summary_call is not None, (
        f"[trial {trial}] setup turn did not call gmail_unread_summary. "
        f"Tools observed: "
        f"{[r.name for r in summary.tool_call_records] or 'none'}. "
        f"See transcript: {summary.transcript_path}"
    )
    summary_res = summary_call.result or {}
    if summary_res.get("ok") is not True:
        pytest.skip(
            f"[trial {trial}] setup gmail_unread_summary returned not-ok "
            f"(account/token state): {summary_res.get('error')!r}. "
            f"See transcript: {summary.transcript_path}",
        )
    messages = summary_res.get("messages") or []
    if not messages:
        pytest.skip(
            f"[trial {trial}] inbox has no unread messages to read — "
            f"nothing for gmail_read_thread to open. "
            f"See transcript: {summary.transcript_path}",
        )
    known_thread_ids = {m.get("thread_id") for m in messages if m.get("thread_id")}
    assert known_thread_ids, (
        f"[trial {trial}] unread summary returned messages with no "
        f"thread_id — gmail_read_thread has nothing to reference. "
        f"See transcript: {summary.transcript_path}"
    )

    # Turn 2: read the first one. THIS is the scenario.
    result = await harness.ask("read me the first one")

    # 1. Trajectory — the model must call gmail_read_thread.
    call = result.tool_call("gmail_read_thread")
    assert call is not None, (
        f"[trial {trial}] model did not call gmail_read_thread. "
        f"Tools observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] gmail_read_thread raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the thread_id the model passed came from the summary
    # (not fabricated). This is the load-bearing assertion: the tool
    # docstring forbids inventing a thread_id.
    passed_id = call.args.get("thread_id")
    assert passed_id in known_thread_ids, (
        f"[trial {trial}] gmail_read_thread called with thread_id="
        f"{passed_id!r}, which was NOT in the summary's thread ids "
        f"{known_thread_ids!r} — the model fabricated an id. "
        f"See transcript: {result.transcript_path}"
    )
    res = call.result or {}
    if res.get("ok") is not True:
        pytest.skip(
            f"[trial {trial}] gmail_read_thread returned not-ok (likely "
            f"account/token state, not a code bug): {res.get('error')!r}. "
            f"See transcript: {result.transcript_path}",
        )

    # 3. Reality — the response echoes the requested thread_id and its
    # message_count matches the messages list.
    assert res.get("thread_id") == passed_id, (
        f"[trial {trial}] read response thread_id={res.get('thread_id')!r} "
        f"doesn't echo the requested {passed_id!r}. "
        f"See transcript: {result.transcript_path}"
    )
    thread_messages = res.get("messages") or []
    assert res.get("message_count") == len(thread_messages), (
        f"[trial {trial}] message_count={res.get('message_count')} but "
        f"messages list has {len(thread_messages)} entries. "
        f"See transcript: {result.transcript_path}"
    )
