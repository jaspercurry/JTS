"""Static registry of all audio cues. Add new cues here.

A cue is a named, pre-rendered audio file the daemon plays when it
hits a known failure state and would otherwise fall silent. Each
cue's text is a .format() template with {hostname}-style placeholders
that get filled at generation time from the current management URL.

Adding a new cue:
  1. Append a CueDef below.
  2. Run `jasper-cues regenerate` (or just restart jasper-voice — its
     startup task will detect the missing file and bake it).
  3. Wire `manager.play("<slug>")` into the failure path that should
     trigger it (see jasper/voice_daemon.py for examples).

Cues must be PROVIDER-AGNOSTIC. Don't say "Google" or "Gemini" — the
project may switch voice backends and audio files baked with
provider names would mislead users post-switch.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CueDef:
    """Definition of a single audio cue.

    `template` is a .format() string. The only variable currently
    supported is `{hostname}` (resolved from JASPER_MANAGEMENT_URL).
    Add more variables as they're needed; just remember each addition
    must also flow through `cue_hash` so cache busting stays correct.
    """
    slug: str
    template: str
    description: str


CUES: tuple[CueDef, ...] = (
    CueDef(
        slug="spend_cap_reached",
        template=(
            "Hey, I've reached today's spend cap. "
            "Visit {hostname} to manage."
        ),
        description=(
            "Played when wake fires after JASPER_DAILY_SPEND_CAP_USD "
            "is hit and voice is disabled until UTC rollover."
        ),
    ),
    CueDef(
        slug="cant_connect",
        template=(
            "Hey, sorry, I can't connect right now. I'll keep trying."
        ),
        description=(
            "Played when wake fires while the voice backend is in "
            "reconnect / paused-for-backoff state."
        ),
    ),
    CueDef(
        slug="internal_error",
        template=(
            "Sorry, something went wrong on my end. Please try again."
        ),
        description=(
            "Played when wake fires and turn-open hits an UNEXPECTED "
            "local/internal error that is NOT a connectivity problem — "
            "e.g. a failed state write. Distinguished from cant_connect: "
            "that cue is truthful only when the live backend is genuinely "
            "down/paused (its own gate handles that). Reaching this means "
            "the connection looked healthy and something else broke, so "
            "claiming 'I can't connect' would be a false alarm (the "
            "2026-06-19 incident). Deliberately honest and low-alarm: it "
            "makes no false promise to 'keep trying' and names no cause "
            "it can't stand behind."
        ),
    ),
    CueDef(
        slug="cant_reach_cloud",
        template=(
            "Heads up — I'm having trouble reaching the cloud and "
            "I'll keep trying. You might want to check on me at "
            "{hostname}."
        ),
        description=(
            "Proactive cue fired by the connection supervisor after "
            "5 consecutive identical reconnect failures (~30 s of "
            "sustained outage on the default backoff schedule). "
            "Distinguished from cant_connect: that one is reactive "
            "to a wake event during a paused window. This one fires "
            "without a wake event so the user knows the speaker is "
            "broken even when they haven't tried to use it. "
            "Rate-limited to once per hour to avoid spamming."
        ),
    ),
    CueDef(
        slug="research_failed",
        template=(
            "Sorry, I couldn't finish that research. Please ask me again."
        ),
        description=(
            "Provider-agnostic proactive cue text for async research jobs "
            "that fail after the user has already been promised a later "
            "answer. WakeLoop rate-limits failed research announcements to "
            "once per hour to avoid nagging during bursts."
        ),
    ),
)


def find(slug: str) -> CueDef | None:
    for c in CUES:
        if c.slug == slug:
            return c
    return None
