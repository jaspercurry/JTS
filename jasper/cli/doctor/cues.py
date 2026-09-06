# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — cue cache domain.

Catches "no baked WAV -> the user gets silence" before a failure path ever
calls ``AudioCueManager.play()`` — the daemon's own fallback there degrades
silently to a WARNING log, never to the operator-visible doctor report.
"""
from __future__ import annotations

import urllib.parse

from ...config import Config
from ...cues.factory import build_cue_tts_backend
from ...cues.manager import AudioCueManager
from ...cues.registry import CUES, CueDef
from ._registry import doctor_check
from ._shared import CheckResult

# Machine-stable codes naming which branch of a cue-cache check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_CUE_CACHE_MISSING = "cue_cache_missing"
REASON_CUE_CACHE_FALLBACK_ONLY = "cue_cache_fallback_only"
REASON_CUE_CACHE_STALE = "cue_cache_stale"

_REMEDY = "Run `jasper-cues regenerate`."


def _cue_manager_for_doctor(cfg: Config) -> AudioCueManager:
    """Read-only manager for cache inspection — mirrors ``jasper-cues list``
    (jasper/cues/cli.py:_make_manager): same hostname/voice/model resolution
    as the daemon, with no TTS backend required, so "cached" here agrees
    with what a live ``AudioCueManager.play()`` would find."""
    backend, voice = build_cue_tts_backend(cfg)
    hostname = urllib.parse.urlparse(cfg.management_url).hostname or "this speaker"
    return AudioCueManager(cfg.sounds_dir, hostname, voice, backend)


def _cue_state(
    manager: AudioCueManager,
    cue: CueDef,
    by_slug: dict[str, CueDef],
    *,
    seen: frozenset[str] = frozenset(),
) -> str:
    """ok | stale | fallback_only | missing, walking the registry's fallback
    chain the same way ``AudioCueManager.play()`` does: current hash, then
    any cached version under the slug, then ``cue.fallback`` (recursively —
    a fallback may itself only resolve through a further fallback).
    ``by_slug`` is built from the same iterable this check classifies, so a
    fallback naming a slug outside it is correctly unresolved rather than
    silently checked against the full production registry."""
    if manager.is_cached(cue):
        return "ok"
    if manager._find_any_cached(cue) is not None:
        return "stale"
    fallback = by_slug.get(cue.fallback) if cue.fallback else None
    if (
        fallback is not None
        and cue.slug not in seen
        and _cue_state(manager, fallback, by_slug, seen=seen | {cue.slug})
        in ("ok", "stale")
    ):
        return "fallback_only"
    return "missing"


@doctor_check(label="cue cache", needs_cfg=True)
def check_cue_cache(cfg: Config) -> CheckResult:
    manager = _cue_manager_for_doctor(cfg)
    by_slug = {cue.slug: cue for cue in CUES}
    missing: list[str] = []
    fallback_only: list[str] = []
    stale: list[str] = []
    for cue in CUES:
        state = _cue_state(manager, cue, by_slug)
        if state == "missing":
            missing.append(cue.slug)
        elif state == "fallback_only":
            fallback_only.append(cue.slug)
        elif state == "stale":
            stale.append(cue.slug)
    # speaker_silent stays default False: a missing cue means the ASSISTANT
    # falls silent on that failure path, not the music output chain
    # (doctor_contract.py's speaker_silent is scoped to the latter).
    if missing:
        return CheckResult(
            "cue cache", "fail",
            f"{len(missing)}/{len(CUES)} cue(s) have no cached WAV and no "
            f"fallback resolves, so the assistant will be silent on these "
            f"failure paths: {', '.join(sorted(missing))}. {_REMEDY}",
            reason=REASON_CUE_CACHE_MISSING,
        )
    if fallback_only:
        return CheckResult(
            "cue cache", "warn",
            f"{len(fallback_only)} cue(s) play only via their fallback cue, "
            f"not their own text: {', '.join(sorted(fallback_only))}. "
            f"{_REMEDY}",
            reason=REASON_CUE_CACHE_FALLBACK_ONLY,
        )
    if stale:
        return CheckResult(
            "cue cache", "warn",
            f"{len(stale)} cue(s) are cached under a stale "
            f"hostname/voice/model hash: {', '.join(sorted(stale))}. "
            f"{_REMEDY}",
            reason=REASON_CUE_CACHE_STALE,
        )
    return CheckResult("cue cache", "ok", f"{len(CUES)} cue(s) cached")
