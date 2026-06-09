"""Diagnostic-tool regression scenario — `flag_recent_issue`.

Pins the LLM-visible contract for the "flag that" surface: when the
user says the speaker misbehaved on the LAST interaction, the model
must call `flag_recent_issue` (not apologise conversationally, not
re-run a tool), passing the user's complaint as `reason`. The store
then marks the most-recent prior real event as `voice_flagged`.

Low side-effect: the only mutation is a SQLite row in a THROWAWAY
tmp store the harness builds per session (see
`_build_test_registry`). No playback, no smart-home action — so
this scenario has no playback skip-guard. It still costs one paid
LLM turn per trial.

Same three-assertion shape as the other scenarios:
  1. Trajectory: the model called `flag_recent_issue`.
  2. Outcome: the tool returned success=True with a flagged_event_id.
  3. Reality: the store row for the prior event is now labelled
     `voice_flagged` (read back via the side-channel store, no extra
     paid call).

============================================================
COST NOTICE — read tests/voice_eval/harness.py top docstring
============================================================
Paid LLM API calls per turn. PASS_K = 3 turns per scenario
function. DO NOT loop or increase PASS_K without explicit human
approval. This scenario is NOT playback-affecting — it writes one
row to a tmp SQLite store — so it has no JASPER_VOICE_EVAL_SKIP_
PLAYBACK guard.
============================================================
"""
from __future__ import annotations

import uuid

import pytest


PASS_K = 3


async def _seed_flaggable_events(store) -> str:
    """Seed two synthetic wake events so `record_flag` has a prior real
    event to flag, mirroring the daemon's runtime state.

    `record_flag` queries the two most-recent non-`flag_action` events:
    `events[0]` is the in-flight flag event (created by `begin_event`
    when the flag-utterance's wake fired) and `events[1]` is the prior
    real event the user is flagging. The harness bypasses the wake loop,
    so neither exists yet — we insert both here. The OLDER one (inserted
    first) is the prior real event; its id is returned so the test can
    read its label back after the turn.
    """
    prior_id = uuid.uuid4().hex
    inflight_id = uuid.uuid4().hex
    common = dict(
        trigger_kind="fire",
        peak_score_aec_on=0.9,
        peak_score_aec_off=None,
        threshold=0.3,
        wake_model="jarvis_v2",
    )
    # Insert oldest-first so the prior real event sorts BELOW the
    # in-flight event in record_flag's "2 most recent" query.
    await store.begin_event(event_id=prior_id, **common)
    await store.begin_event(event_id=inflight_id, **common)
    return prior_id


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_flag_that_calls_flag_recent_issue(harness, trial: int) -> None:
    """Asks 'flag that, you cut me off' — the model should call
    `flag_recent_issue` with a `reason` close to the user's words, and
    the prior wake event should end up labelled `voice_flagged`.

    Trajectory is the load-bearing assertion: the prompting playbook
    warns that absolute-ban phrasing makes models skip tools, so this
    catches a regression where the model apologises in prose ("Sorry I
    cut you off") instead of actually recording the flag — the failure
    mode the tool docstring's positive trigger-phrase list guards
    against."""
    store = harness.test_state.get("wake_event_store")
    assert store is not None, (
        "harness.test_state missing 'wake_event_store' — registry wiring "
        "regressed. See _build_test_registry in harness.py."
    )

    prior_id = await _seed_flaggable_events(store)

    result = await harness.ask("flag that, you cut me off")

    # 1. Trajectory — the model must call flag_recent_issue.
    call = result.tool_call("flag_recent_issue")
    assert call is not None, (
        f"[trial {trial}] model did not call flag_recent_issue. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"If the model apologised in prose instead, the docstring's "
        f"trigger-phrase routing regressed. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — the tool reported success with a flagged event id.
    res = call.result or {}
    assert res.get("success") is True, (
        f"[trial {trial}] flag_recent_issue returned success={res.get('success')!r} "
        f"(expected True). Result: {res!r}. The store had a seeded prior "
        f"event, so a False here means record_flag's lookup or the tool "
        f"wiring regressed. See transcript: {result.transcript_path}"
    )
    assert res.get("flagged_event_id"), (
        f"[trial {trial}] flag_recent_issue returned empty flagged_event_id: "
        f"{res!r}. See transcript: {result.transcript_path}"
    )

    # 3. Reality — the prior event row is now labelled voice_flagged.
    # Read back via the side-channel store (no extra paid LLM call). The
    # flagged id the tool reported must be the prior event we seeded.
    assert res.get("flagged_event_id") == prior_id, (
        f"[trial {trial}] flagged the wrong event: tool reported "
        f"{res.get('flagged_event_id')!r}, expected the seeded prior event "
        f"{prior_id!r}. See transcript: {result.transcript_path}"
    )
    row = await store.get_event(prior_id)
    assert row is not None and row.get("label") == "voice_flagged", (
        f"[trial {trial}] prior event {prior_id} label is "
        f"{(row or {}).get('label')!r}, expected 'voice_flagged'. The tool "
        f"reported success but the store row didn't get marked — a "
        f"record_flag persistence regression. "
        f"See transcript: {result.transcript_path}"
    )
