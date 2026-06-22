# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard: cue registry and cue play sites must match exactly, both ways.

AGENTS.md ("No silent failure paths"): a failure path that would leave
the speaker silent must play a registered cue — append a CueDef to
jasper/cues/registry.py AND wire a play call into the failure handler.
Both halves are easy to ship without the other, and the result is only
observable in production failure modes (rarely-executed code):

  * Orphan cue — a CueDef nobody plays. The failure path it was baked
    for either never got wired or lost its play call in a refactor; the
    WAV is regenerated (and re-billed against the TTS endpoint) on
    every provider switch for nothing.
  * Phantom play — a play call naming a slug the registry doesn't
    know. CueManager has no file to play, so the speaker falls silent
    on exactly the failure the cue existed to announce.

This is a static cross-check: the set of slugs registered in CUES must
equal the set of slug literals at play sites in jasper/ (outside
jasper/cues/ itself). Play sites are `*.play("slug")` /
`_play_cue("slug")` calls and `..._CUE_SLUG = "slug"` constants (the
indirection jasper/voice/_supervisor.py uses). A play call routed
through a *new* kind of indirection won't be seen — extend
_PLAY_SITE if you add one.

Set equality makes the guard two-sided with no allowlist to go stale.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.cues.registry import CUES

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "jasper"

# A slug literal in playing position: a `.play(`/`_play_cue(` call or a
# `<NAME>_CUE_SLUG = ` constant that a play call consumes.
_PLAY_SITE = re.compile(
    r"(?:\bplay\(|_play_cue\(|[A-Z0-9_]*_CUE_SLUG\s*=)\s*['\"]([a-z0-9_]+)['\"]"
)


def _played_slugs() -> dict[str, list[str]]:
    """slug -> repo-relative files that play it (jasper/cues/ excluded —
    the registry/manager package defines cues, it doesn't consume them)."""
    found: dict[str, list[str]] = {}
    for py in sorted(PKG.rglob("*.py")):
        if PKG / "cues" in py.parents:
            continue
        for slug in _PLAY_SITE.findall(py.read_text(encoding="utf-8")):
            found.setdefault(slug, []).append(str(py.relative_to(ROOT)))
    return found


def test_every_registered_cue_is_played_somewhere():
    played = _played_slugs()
    orphans = [c.slug for c in CUES if c.slug not in played]
    assert not orphans, (
        f"CueDef(s) registered in jasper/cues/registry.py with no play "
        f"site anywhere in jasper/: {orphans}. Wire `cues.play(\"<slug>\")` "
        "into the failure path the cue announces (see AGENTS.md 'No silent "
        "failure paths' + docs/HANDOFF-audible-feedback.md), or delete the "
        "CueDef — an unplayed cue is regenerated on every provider switch "
        "for nothing."
    )


def test_every_played_slug_is_registered():
    registered = {c.slug for c in CUES}
    phantoms = {
        slug: files
        for slug, files in _played_slugs().items()
        if slug not in registered
    }
    assert not phantoms, (
        f"play site(s) reference cue slug(s) missing from CUES in "
        f"jasper/cues/registry.py: {phantoms}. The manager has no baked WAV "
        "for an unregistered slug, so the speaker stays silent on exactly "
        "the failure the cue was meant to announce. Register a CueDef (or "
        "fix the typo)."
    )


def test_registry_slugs_are_unique():
    slugs = [c.slug for c in CUES]
    assert len(slugs) == len(set(slugs)), f"duplicate cue slug in CUES: {slugs}"


def test_cues_are_provider_agnostic():
    """AGENTS.md + the registry docstring: cue text must not name a voice
    backend. A WAV baked with "Gemini"/"OpenAI"/"Grok" would mislead the
    household after a provider switch. Pin it so a new cue can't quietly
    reintroduce a brand name (the internal_error cue added 2026-06-19 is
    deliberately generic — "something went wrong on my end")."""
    forbidden = (
        "google", "gemini", "openai", "chatgpt", "grok", "xai",
        "anthropic", "claude",
    )
    offenders = {
        c.slug: w
        for c in CUES
        for w in forbidden
        if w in c.template.lower()
    }
    assert not offenders, (
        "cue template(s) name a voice provider (must be provider-agnostic "
        f"per AGENTS.md / registry docstring): {offenders}"
    )
