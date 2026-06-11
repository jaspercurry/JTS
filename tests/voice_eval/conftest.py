"""Pytest fixtures for the voice-eval suite.

The harness is session-scoped so the `LiveConnection` is opened once
and reused across every scenario in the run. Cost matters; isolation
is fine today because the currently-tested tools are stateless. Add
a function-scoped variant when a stateful tool gets a scenario.

Every test in this suite runs on the SESSION event loop (the
collection hook below applies `loop_scope="session"`; the harness
fixture declares the same). Without it, pytest-asyncio gives each
test its own loop while the connection, its receive task, and the
turn queues live on the loop of whichever test opened them — which
pytest closes when that test ends. The 2026-06-11 on-Pi run showed
the result: every test after the first died with "a turn is already
active" against a connection whose loop no longer existed. One
session-long loop is also exactly how the daemon runs.

If the necessary env vars aren't set (no provider key, no
OPENAI_API_KEY for TTS, no subway/weather config), the whole suite
is skipped with a clear message. That way `pytest tests/voice_eval/`
is safe to run in any environment — it either runs end-to-end or
skips cleanly.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from jasper.config import Config


def pytest_collection_modifyitems(items) -> None:
    """Pin every voice_eval test to the session event loop.

    append=False is load-bearing: pytest-asyncio's auto mode has already
    put a bare ``asyncio`` marker (no loop_scope) on each item, and the
    plugin reads the CLOSEST marker — so an appended pin loses and the
    test silently runs on a per-function loop. That function loop's
    Runner.close() runs loop.shutdown_asyncgens() at test teardown,
    which finalizes google-genai's suspended connect() asyncgen and
    cleanly closes the live websocket between tests (the 2026-06-11
    "goodbye" — see PR #610's investigation). Prepending makes the pin
    the closest marker, so the whole suite genuinely shares the session
    loop, matching how the daemon runs.
    """
    suite_dir = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        if str(item.fspath).startswith(suite_dir):
            item.add_marker(
                pytest.mark.asyncio(loop_scope="session"), append=False,
            )


def _provider_key_present(cfg: Config) -> bool:
    """True iff the env var for the active provider is set. We don't
    parse it (might be a placeholder for tests), we just check
    non-empty — the provider will error sensibly if it's invalid."""
    by_provider = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "grok": "XAI_API_KEY",
    }
    var = by_provider.get(cfg.voice_provider, "")
    return bool(os.environ.get(var, "").strip())


@pytest.fixture(scope="session")
def voice_eval_config() -> Config:
    """Load Config from the environment. Skips the suite if loading
    fails (e.g. running on a laptop without /etc/jasper/jasper.env
    sourced)."""
    try:
        cfg = Config.from_env()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"voice-eval: Config.from_env() failed: {e!r}")

    if not _provider_key_present(cfg):
        pytest.skip(
            f"voice-eval: no API key set for active provider "
            f"({cfg.voice_provider}) — set the provider's key env var or "
            "switch the active provider via JASPER_VOICE_PROVIDER",
        )
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        # TTS uses OpenAI regardless of which provider drives the
        # voice loop. Cached audio is reused after first run; missing
        # key only matters when synthesizing a new prompt.
        pytest.skip(
            "voice-eval: OPENAI_API_KEY not set — needed for prompt-audio "
            "synthesis. (After all prompts are cached, this skip can be "
            "relaxed for offline runs.)",
        )
    return cfg


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def harness(voice_eval_config: Config):
    """The session-scoped harness. Opens the `LiveConnection` lazily
    on first `ask()` and tears down at session end — all on the
    session loop, so the connection's tasks outlive any one test."""
    # Local import so import-time of conftest stays light when the
    # suite is skipped.
    from .harness import VoiceEvalHarness

    h = VoiceEvalHarness(voice_eval_config)
    try:
        yield h
    finally:
        await h.aclose()
