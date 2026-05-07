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
)


def find(slug: str) -> CueDef | None:
    for c in CUES:
        if c.slug == slug:
            return c
    return None
