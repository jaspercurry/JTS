# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Home Assistant regression scenario — the `home_assistant` relay tool.

Pins the LLM-visible routing for smart-home control: a device-control
utterance ("turn on the living room lights") must reach the
`home_assistant` tool, which relays the phrase verbatim to HA's
conversation pipeline. The trajectory assertion is load-bearing — the
documented failure mode is the model answering smart-home requests
conversationally ("I can't control your lights") instead of calling
the tool, or mis-routing to spotify/transport. The tool's docstring
uses conditional WHEN/WHEN-NOT framing precisely to keep this routing
sticky (per docs/HANDOFF-prompting.md); this scenario is its guard.

============================================================
SIDE-EFFECT + COST NOTICE — read carefully before running
============================================================
The `home_assistant` tool performs a REAL action against the
configured Home Assistant: when the model calls
`home_assistant("turn on the living room lights")`, HA will TURN
ON THE LIGHTS (or whichever entity matches). This is a physical
side-effect on the household. Skip via
`JASPER_VOICE_EVAL_SKIP_PLAYBACK=1` (the same env that silences
playback scenarios — one switch to disable everything that touches
the physical world) AND it auto-skips when HA isn't configured.

The prompt deliberately uses a low-stakes "turn on the living room
lights" command rather than a lock/garage/scene so a stray run is
recoverable by eye. If your HA has no `light` entity in a "living
room" area the command will no_intent_match — still a valid tool
call, so the trajectory assertion passes and the outcome assertion
tolerates the documented error shape.

PASS_K = 3 turns per scenario. DO NOT loop or increase PASS_K
without explicit human approval. Per-turn cost: read harness.py
top docstring.
============================================================

Three-assertion shape:
  1. Trajectory: the model called `home_assistant` (not a deflection).
  2. Outcome: the tool returned HA's documented result shape
     (spoken_response / success / response_type / error_code).
  3. Reality: the model passed a query that carries the smart-home
     intent through to HA (the relay didn't drop the request), and
     the spoken response isn't the "smart-home isn't set up"
     conversational fallback.
"""
from __future__ import annotations

import os

import pytest


PASS_K = 3


def _ha_action_skip() -> bool:
    """True if the user has opted out of physical-side-effect tests for
    this run. Reuses `JASPER_VOICE_EVAL_SKIP_PLAYBACK` so a single env
    var silences both playback control AND smart-home actions — the
    operator rarely wants one but not the other while the household is
    home."""
    return os.environ.get("JASPER_VOICE_EVAL_SKIP_PLAYBACK", "").strip() == "1"


@pytest.mark.parametrize("trial", range(PASS_K))
async def test_lights_command_routes_to_home_assistant(harness, trial: int) -> None:
    """Asks 'turn on the living room lights' — the model must call
    `home_assistant` with the phrase relayed through, NOT answer
    conversationally and NOT route to a music tool.

    KNOWN ENVIRONMENT DEPENDENCY: requires a configured Home Assistant
    (JASPER_HA_URL + JASPER_HA_TOKEN) — the tool is gated on
    `cfg.ha_enabled` in the harness registry exactly as in the daemon,
    so without HA the model never sees the tool and this scenario
    skips. The outcome assertion tolerates HA's no_intent_match /
    no_valid_targets error (e.g. no 'living room' light entity) because
    that's still a correct relay — the trajectory is what we're
    locking."""
    if not harness.cfg.ha_enabled:
        pytest.skip(
            "voice-eval: Home Assistant not configured "
            "(JASPER_HA_URL + JASPER_HA_TOKEN required) — set them to run "
            "this scenario",
        )
    if _ha_action_skip():
        pytest.skip(
            "voice-eval: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1 set — "
            "skipping Home Assistant scenario (it performs a real "
            "smart-home action)",
        )

    result = await harness.ask("turn on the living room lights")

    # 1. Trajectory — the model must call home_assistant, not deflect.
    call = result.tool_call("home_assistant")
    assert call is not None, (
        f"[trial {trial}] model did not call home_assistant. "
        f"Tool calls observed: "
        f"{[r.name for r in result.tool_call_records] or 'none'}. "
        f"If it answered conversationally ('I can't control lights'), "
        f"the docstring's device-control routing regressed. "
        f"See transcript: {result.transcript_path}"
    )
    if call.error:
        pytest.fail(
            f"[trial {trial}] tool raised: {call.error}. "
            f"See transcript: {result.transcript_path}",
        )

    # 2. Outcome — HA's documented result shape is present. We don't
    # require success=True because the eval HA may legitimately lack a
    # matching 'living room' light entity; an error response with a code
    # is still a valid relay. What we assert is the shape, so a future
    # change to HAResponse.as_tool_result that drops a key is caught.
    res = call.result or {}
    for key in ("spoken_response", "success", "response_type", "error_code"):
        assert key in res, (
            f"[trial {trial}] home_assistant result missing '{key}': "
            f"{res!r}. HAResponse.as_tool_result shape regressed. "
            f"See transcript: {result.transcript_path}"
        )

    # 3. Reality — the model relayed the smart-home intent (the args
    # carry a lights/living-room reference) rather than passing an empty
    # or unrelated query. The HA tool's docstring says to pass the
    # phrase close to verbatim; this catches a regression where the
    # model strips the request down to nothing.
    query = (call.args.get("query") or "").lower()
    assert ("light" in query or "living room" in query), (
        f"[trial {trial}] home_assistant called with query={call.args.get('query')!r}, "
        f"which doesn't carry the lights/living-room intent — the relay "
        f"dropped or rewrote the request. "
        f"See transcript: {result.transcript_path}"
    )

    # The spoken response must not be the "smart-home isn't set up"
    # conversational fallback (which only fires when the tool ISN'T
    # registered). Since the tool WAS called, a setup-redirect in the
    # speech means the model is confused about its own capability.
    if result.spoken_text:
        spoken = result.spoken_text.lower()
        assert "jts.local/ha" not in spoken and "isn't set up" not in spoken, (
            f"[trial {trial}] model spoke a smart-home-setup redirect even "
            f"though it called home_assistant — capability confusion. "
            f"Spoken text: {result.spoken_text!r}. "
            f"See transcript: {result.transcript_path}"
        )
