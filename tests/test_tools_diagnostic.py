# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for `jasper.tools.diagnostic.make_diagnostic_tools`.

Covers the LLM-facing tool shape (factory gating, spoken-response
strings, success/failure paths). The store-side semantics live in
`jasper/wake_events.py::WakeEventStore.record_flag` and are covered
in `tests/test_wake_events.py`."""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.tools.diagnostic import make_diagnostic_tools
from jasper.wake_events import WakeEventStore


@pytest.fixture
def store(tmp_path: Path):
    s = WakeEventStore(tmp_path)
    s.open()
    yield s
    s.close()


def _get_tool(fns):
    """Resolve the registered tool callable to invoke directly. The
    factory returns the function wrapped by `@tool()` which keeps the
    original async fn invocable."""
    assert len(fns) == 1, (
        "make_diagnostic_tools should expose exactly one tool today "
        "(flag_recent_issue). If/when this grows, update this fixture."
    )
    fn = fns[0]
    # The @tool decorator stores the registry entry but leaves the
    # callable invocable. Just call it.
    assert callable(fn)
    return fn


# ---------------------------------------------------------------------------
# Factory gating
# ---------------------------------------------------------------------------


def test_factory_returns_empty_when_store_is_none():
    """When wake-event telemetry is disabled (store failed to open),
    the factory MUST return [] so the model never sees the tool.
    Without this, the model would try to flag something that the
    backend can't actually persist, and the user would get false-
    positive 'Got it, I flagged it' confirmations against a no-op."""
    assert make_diagnostic_tools(None) == []


def test_factory_returns_tool_when_store_is_open(store: WakeEventStore):
    """Happy path: store is open, factory returns the tool list."""
    fns = make_diagnostic_tools(store)
    assert len(fns) == 1
    assert fns[0].__name__ == "flag_recent_issue"


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


async def _seed_events(store: WakeEventStore, ids_and_ts: list[tuple[str, str]]):
    """Helper: insert N events with controllable timestamps via the
    same direct-SQL pattern as `tests/test_wake_events.py::_seed_event`.
    Avoids the millisecond-collision risk of back-to-back begin_event
    calls."""
    for event_id, ts in ids_and_ts:
        await store.begin_event(
            event_id=event_id,
            trigger_kind="fire_aec_on",
            peak_score_aec_on=0.85,
            peak_score_aec_off=0.10,
            threshold=0.5,
            wake_model="jarvis_v2.onnx",
        )
        store._conn.execute(  # noqa: SLF001
            "UPDATE wake_events SET ts_utc = ? WHERE event_id = ?",
            (ts, event_id),
        )


async def test_flag_tool_returns_success_with_canonical_spoken_response(
    store: WakeEventStore,
):
    """The success spoken_response is the intentional short
    confirmation 'Got it, I flagged it.' The user picked this exact
    wording — changing it without permission would be an unwanted
    UX shift, so this is a tight assertion on the literal string."""
    await _seed_events(store, [
        ("evt-prior", "2026-05-23T19:00:00+00:00"),
        ("evt-flag",  "2026-05-23T19:00:05+00:00"),
    ])
    fn = _get_tool(make_diagnostic_tools(store))
    result = await fn(reason="cut me off")
    assert result == {
        "spoken_response": "Got it, I flagged it.",
        "success": True,
        "flagged_event_id": "evt-prior",
    }


async def test_flag_tool_triggers_flight_recorder_dump_on_success(
    store: WakeEventStore, monkeypatch,
):
    """On a successful flag, the voice flight recorder is dumped to the
    journal so the DEBUG context the user just noticed is captured (Tier
    C). Best-effort — only on the success path."""
    from jasper import flight_recorder
    dumps: list[str] = []
    monkeypatch.setattr(
        flight_recorder, "dump",
        lambda reason="manual": dumps.append(reason) or 0,
    )
    await _seed_events(store, [
        ("evt-prior", "2026-05-23T19:00:00+00:00"),
        ("evt-flag",  "2026-05-23T19:00:05+00:00"),
    ])
    fn = _get_tool(make_diagnostic_tools(store))
    result = await fn(reason="cut me off")
    assert result["success"] is True
    assert dumps == ["voice_flagged"]


async def test_flag_tool_returns_failure_when_no_prior_event(
    store: WakeEventStore,
):
    """Only the in-flight flag event exists (fresh boot / first
    wake). Tool returns success=false with a short user-facing
    explanation that the model speaks. Don't crash."""
    await _seed_events(store, [
        ("evt-only", "2026-05-23T19:00:00+00:00"),
    ])
    fn = _get_tool(make_diagnostic_tools(store))
    result = await fn(reason="testing")
    assert result["success"] is False
    assert result["flagged_event_id"] == ""
    # Spoken response is short, no apology, no offer to retry.
    assert result["spoken_response"] == "There's no recent event to flag yet."


async def test_flag_tool_fail_soft_on_store_error(store: WakeEventStore):
    """If record_flag raises (DB locked, disk full, etc.), the tool
    MUST NOT propagate — it returns success=false with a user-
    facing message. Telemetry failures are not allowed to silence
    the speaker (AGENTS.md: 'no silent failure paths')."""
    # Force a record_flag exception by monkey-patching it onto the store.
    async def _boom(_reason):
        raise RuntimeError("disk full")
    store.record_flag = _boom  # type: ignore[assignment]

    fn = _get_tool(make_diagnostic_tools(store))
    result = await fn(reason="anything")
    assert result["success"] is False
    assert result["flagged_event_id"] == ""
    assert "isn't available" in result["spoken_response"]


async def test_flag_tool_passes_reason_through_to_store(
    store: WakeEventStore,
):
    """The user's complaint text must reach the SQLite row verbatim —
    that's the whole point of the feature (offline review needs the
    user's actual words to disambiguate failure modes)."""
    await _seed_events(store, [
        ("evt-prior", "2026-05-23T19:00:00+00:00"),
        ("evt-flag",  "2026-05-23T19:00:05+00:00"),
    ])
    fn = _get_tool(make_diagnostic_tools(store))
    await fn(reason="you fired on the TV again")
    row = await store.get_event("evt-prior")
    assert row["label"] == "voice_flagged"
    assert "you fired on the TV again" in row["label_notes"]
