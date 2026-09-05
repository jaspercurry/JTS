# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    # Played instead while this cue has no baked WAV: cues are
    # synthesised through the provider whose outage they announce.
    # Remove once cues are baked by a local TTS that needs no provider.
    fallback: str | None = None


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
            "reconnect / paused-for-backoff state for a TRANSIENT "
            "reason. A terminal outage plays the provider_* cue naming "
            "its remedy instead — 'I'll keep trying' is a false promise "
            "there (ADR-0215)."
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
            "e.g. a failed state write — and again at end of turn when a "
            "question was asked and no answer came back at all. "
            "Distinguished from cant_connect: "
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
        slug="provider_out_of_credit",
        template=(
            "My AI service is out of credit. Please check me at "
            "{hostname}."
        ),
        description=(
            "Names the remedy for a terminal connection failure the "
            "household fixes by topping up. Chosen when the rejection "
            "body names credit, quota or billing (ADR-0215)."
        ),
        fallback="cant_connect",
    ),
    CueDef(
        slug="provider_needs_attention",
        template=(
            "My AI service needs attention. Please check me at "
            "{hostname}."
        ),
        description=(
            "Names the remedy for a terminal connection failure needing "
            "a look at the setup — a rejected key, a missing model, a "
            "malformed config. Chosen for every terminal failure the "
            "rejection body does not blame on credit (ADR-0215)."
        ),
        fallback="cant_connect",
    ),
    CueDef(
        slug="network_down",
        template=(
            "I can't reach the network. Please check the Wi-Fi or "
            "troubleshoot at {hostname}."
        ),
        description=(
            "Names the remedy when the household's own link is down — a "
            "DNS or route failure carrying no HTTP status. Names the URL "
            "by the owner's call even though the page may be unreachable; "
            "\"or\" keeps it an option rather than an instruction "
            "(ADR-0215)."
        ),
        fallback="cant_connect",
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
    CueDef(
        slug="no_room_microphone",
        template=(
            "I don't have a microphone of my own. Hold the button on your "
            "remote to talk to me."
        ),
        description=(
            "Played when something asks this speaker to open a room-mic turn "
            "but it has no always-listening microphone — a streambox whose "
            "only voice input is a paired push-to-talk remote (issue #2205). "
            "Without it that request ducks the music, chirps, forwards no "
            "audio at all, and dies to the idle watchdog in silence. Wired "
            "from WakeLoop.manual_session_start via NO_ROOM_MIC_CUE_SLUG. "
            "Names the remedy, not the cause: the household can act on "
            "'hold the button', not on 'no primary leg was planned'."
        ),
    ),
    CueDef(
        slug="audition_reduced_graph",
        template=(
            "Heads up — I'm playing the crossover-only tuning for a listen. "
            "I'll put the full one back within half an hour."
        ),
        description=(
            "Played by jasper.active_speaker.audition right after it swaps the "
            "running graph down to the crossover-only layer. Plays THROUGH the "
            "new graph, so it is also the liveness proof. It prevents the "
            "silent failure: a household listening to a deliberately reduced "
            "tuning with nothing on the speaker having said so."
        ),
    ),
    CueDef(
        slug="audition_full_graph",
        template="Back to the full tuning.",
        description=(
            "Played by jasper.active_speaker.audition after the durable graph "
            "is proven back — on an explicit stop, on the deadline, or on the "
            "owner being interrupted. Deliberately terse: it confirms a return "
            "to normal and promises nothing else."
        ),
    ),
)


def find(slug: str) -> CueDef | None:
    for c in CUES:
        if c.slug == slug:
            return c
    return None
