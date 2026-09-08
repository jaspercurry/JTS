# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — cue cache: catches a missing baked cue before AudioCueManager.play() silently WARNs instead of telling the operator."""
from __future__ import annotations

import urllib.parse
from collections import defaultdict
from typing import Any

from ...config import Config
from ...cues.factory import build_cue_tts_backend
from ...cues.manager import AudioCueManager
from ...cues.registry import CUES, CueDef
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult

# Machine-stable reason codes (AGENTS.md: tests pin status + reason, never detail prose).
REASON_CUE_CACHE_MISSING = "cue_cache_missing"
REASON_CUE_CACHE_FALLBACK_ONLY = "cue_cache_fallback_only"
REASON_CUE_CACHE_STALE = "cue_cache_stale"
REASON_CUE_DELIVERY_UNAVAILABLE = "cue_delivery_unavailable"
REASON_CUE_DELIVERY_FAILED = "cue_delivery_failed"

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
    manager: AudioCueManager, cue: CueDef, by_slug: dict[str, CueDef], memo: dict[str, str],
) -> str:
    """ok | stale | fallback_only | missing."""
    if cue.slug in memo:
        return memo[cue.slug]
    memo[cue.slug] = "missing"  # breaks a self-referential fallback cycle
    fallback = by_slug.get(cue.fallback) if cue.fallback else None
    if manager.is_cached(cue):
        memo[cue.slug] = "ok"
    elif manager.find_any_cached(cue) is not None:
        memo[cue.slug] = "stale"
    elif fallback and _cue_state(manager, fallback, by_slug, memo) != "missing":
        memo[cue.slug] = "fallback_only"
    return memo[cue.slug]

@doctor_check(label="cue cache", needs_cfg=True)
def check_cue_cache(cfg: Config) -> CheckResult:
    backend, voice = build_cue_tts_backend(cfg)
    hostname = urllib.parse.urlparse(cfg.management_url).hostname or "this speaker"
    manager = AudioCueManager(cfg.sounds_dir, hostname, voice, backend)
    by_slug = {cue.slug: cue for cue in CUES}
    memo: dict[str, str] = {}
    buckets: dict[str, list[str]] = defaultdict(list)
    for cue in CUES:
        buckets[_cue_state(manager, cue, by_slug, memo)].append(cue.slug)
    # speaker_silent stays default False (doctor_contract.CheckResult): the
    # assistant goes silent here, not the output chain.
    for bucket, status, reason, template in _SEVERITY:
        if slugs := buckets[bucket]:
            detail = template.format(
                n=len(slugs), total=len(CUES), slugs=", ".join(sorted(slugs)),
            )
            return CheckResult("cue cache", status, detail, reason=reason)
    return CheckResult("cue cache", "ok", f"{len(CUES)} cue(s) cached")


def _nested_dict(payload: Any, *keys: str) -> dict[str, Any] | None:
    """Drill a nested dict out of a jasper-control HTTP payload along
    ``keys``, fail-soft to None on any shape mismatch."""
    for key in keys:
        payload = payload.get(key) if isinstance(payload, dict) else None
    return payload if isinstance(payload, dict) else None


def _read_cue_delivery_state() -> dict[str, Any] | None:
    return _nested_dict(evidence.control_state().payload, "cues")


@doctor_check()
def check_cue_delivery() -> CheckResult:
    """Surface AudioCueManager delivery failures recorded on jasper-voice,
    otherwise visible only in the journal."""
    state = _read_cue_delivery_state()
    if state is None:
        return CheckResult(
            "cue delivery",
            "skipped",
            "jasper-control /state unavailable",
            reason=REASON_CUE_DELIVERY_UNAVAILABLE,
        )
    counts = state.get("counts")
    failed = counts.get("failed") if isinstance(counts, dict) else None
    if isinstance(failed, int) and failed > 0:
        last = state.get("last")
        last = last if isinstance(last, dict) else {}
        return CheckResult(
            "cue delivery",
            "warn",
            f"{failed} cue delivery failure(s); last reason="
            f"{last.get('reason')} slug={last.get('slug')}",
            reason=REASON_CUE_DELIVERY_FAILED,
        )
    return CheckResult("cue delivery", "ok", "no cue delivery failures")
