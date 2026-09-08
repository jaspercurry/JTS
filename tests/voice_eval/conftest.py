# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid voice fixtures share one connection and one session event loop.

The shared loop keeps connection tasks and turn queues alive across tests.
Provider history and tool state can persist between trials. Missing provider
or prompt-TTS keys skip tests using these fixtures; weather and transit
prerequisites are checked by individual scenarios.

Announce the scenario count, estimated cost and live tool side effects before
running. Never loop or auto-retry paid sessions. See README.md for run rules.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from jasper.config import Config


#: Seconds per collected test item; parametrized trials have separate budgets.
VOICE_EVAL_TIMEOUT_S = 900


def pytest_collection_modifyitems(items) -> None:
    """Pin paid tests to the loop that owns the connection tasks.

    append=False makes the session asyncio marker closest. Auto mode can add
    an unscoped marker, and pytest-asyncio reads the closest one. A function
    loop would close connection async generators at test teardown.
    The timeout applies to each collected test item.
    """
    suite_dir = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        if str(item.fspath).startswith(suite_dir):
            item.add_marker(
                pytest.mark.asyncio(loop_scope="session"), append=False,
            )
            item.add_marker(pytest.mark.timeout(VOICE_EVAL_TIMEOUT_S))


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
    """Load provider config; missing provider or prompt-TTS keys skip consumers."""
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
        # Prompt TTS uses OpenAI for every voice provider. This fixture
        # requires its key even when prompt audio is cached.
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
