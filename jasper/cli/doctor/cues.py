# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — cue cache: catches a missing baked cue before AudioCueManager.play() silently WARNs instead of telling the operator."""
from __future__ import annotations

import os
import urllib.parse
from collections import defaultdict

from ...config import Config
from ...cues.factory import build_cue_tts_backend
from ...cues.manager import AudioCueManager
from ...cues.registry import CUES, CueDef
from ._registry import doctor_check
from ._shared import CheckResult

# Machine-stable reason codes (AGENTS.md: tests pin status + reason, never detail prose).
REASON_CUE_CACHE_MISSING = "cue_cache_missing"
REASON_CUE_CACHE_FALLBACK_ONLY = "cue_cache_fallback_only"
REASON_CUE_CACHE_STALE = "cue_cache_stale"

_REMEDY = "Run `jasper-cues regenerate`."
# (bucket, status, reason, detail template) — worst-first.
_SEVERITY: tuple[tuple[str, str, str, str], ...] = (
    ("missing", "fail", REASON_CUE_CACHE_MISSING,
     "{n}/{total} cue(s) have no cached WAV and no fallback — the assistant "
     "will be silent: {slugs}. " + _REMEDY),
    ("fallback_only", "warn", REASON_CUE_CACHE_FALLBACK_ONLY,
     "{n} cue(s) play only via their fallback, not their own text: {slugs}. "
     + _REMEDY),
    ("stale", "warn", REASON_CUE_CACHE_STALE,
     "{n} cue(s) are cached under a stale hostname/voice/model hash: "
     "{slugs}. " + _REMEDY),
)

def _cue_state(
    manager: AudioCueManager, cue: CueDef, by_slug: dict[str, CueDef], memo: dict[str, str], cached: set[str],
) -> str:
    """ok | stale | fallback_only | missing. ``cached`` is every slug with SOME
    cached WAV (any hash) found by ONE directory listing — mirrors
    AudioCueManager._find_any_cached's prefix/suffix match without that
    method's per-slug os.listdir."""
    if cue.slug in memo:
        return memo[cue.slug]
    memo[cue.slug] = "missing"  # breaks a self-referential fallback cycle
    fallback = by_slug.get(cue.fallback) if cue.fallback else None
    if manager.is_cached(cue):
        memo[cue.slug] = "ok"
    elif cue.slug in cached:
        memo[cue.slug] = "stale"
    elif fallback and _cue_state(manager, fallback, by_slug, memo, cached) != "missing":
        memo[cue.slug] = "fallback_only"
    return memo[cue.slug]

@doctor_check(label="cue cache", needs_cfg=True)
def check_cue_cache(cfg: Config) -> CheckResult:
    backend, voice = build_cue_tts_backend(cfg)
    hostname = urllib.parse.urlparse(cfg.management_url).hostname or "this speaker"
    manager = AudioCueManager(cfg.sounds_dir, hostname, voice, backend)
    by_slug = {cue.slug: cue for cue in CUES}
    entries = os.listdir(cfg.sounds_dir) if os.path.isdir(cfg.sounds_dir) else []
    cached = {s for s in by_slug if any(e.startswith(f"{s}-") and e.endswith(".wav") for e in entries)}
    memo: dict[str, str] = {}
    buckets: dict[str, list[str]] = defaultdict(list)
    for cue in CUES:
        buckets[_cue_state(manager, cue, by_slug, memo, cached)].append(cue.slug)
    # speaker_silent stays default False (doctor_contract.CheckResult): the
    # assistant goes silent here, not the output chain.
    for bucket, status, reason, template in _SEVERITY:
        if slugs := buckets[bucket]:
            detail = template.format(
                n=len(slugs), total=len(CUES), slugs=", ".join(sorted(slugs)),
            )
            return CheckResult("cue cache", status, detail, reason=reason)
    return CheckResult("cue cache", "ok", f"{len(CUES)} cue(s) cached")
