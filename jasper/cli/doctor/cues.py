# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks over the cue subsystem.

`check_cue_cache` catches a missing baked cue before `AudioCueManager.play()`
silently WARNs instead of telling the operator; `check_cue_delivery` judges
the delivery record the running daemon publishes at /state.cues.
"""
from __future__ import annotations

import urllib.parse
from collections import defaultdict

from ...config import Config
from ...cues.factory import build_cue_tts_backend
from ...cues.manager import AudioCueManager, OUTCOME_FAILED
from ...cues.registry import CUES, CueDef
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult

# Machine-stable reason codes (AGENTS.md: tests pin status + reason, never detail prose).
REASON_CUE_CACHE_MISSING = "cue_cache_missing"
REASON_CUE_CACHE_FALLBACK_ONLY = "cue_cache_fallback_only"
REASON_CUE_CACHE_STALE = "cue_cache_stale"
REASON_CUE_DELIVERY_UNAVAILABLE = "cue_delivery_unavailable"
REASON_CUE_DELIVERY_NO_MANAGER = "cue_delivery_no_manager"
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


@doctor_check()
def check_cue_delivery() -> CheckResult:
    """Judge the cue-delivery record jasper-voice publishes at /state.cues,
    whose failures are otherwise visible only in the journal.

    Every state a snapshot cannot be read from is `skipped`: an unknown
    record must never render as a healthy one. The `warn` is the LAST
    outcome, not the lifetime counter — those counters are monotonic, so
    alarming on them would never clear after one historical failure.
    """
    payload = evidence.control_state().payload
    if not isinstance(payload, dict):
        return CheckResult(
            "cue delivery",
            "skipped",
            "jasper-control /state unavailable — cue delivery unknown",
            reason=REASON_CUE_DELIVERY_UNAVAILABLE,
        )
    state = payload.get("cues")
    if state is None:
        if evidence.install_profile_is_streambox():
            return CheckResult(
                "cue delivery",
                "skipped",
                "the streambox profile runs no cue manager",
                reason=REASON_CUE_DELIVERY_NO_MANAGER,
            )
        return CheckResult(
            "cue delivery",
            "warn",
            "jasper-voice is running with no cue manager: every failure cue "
            "is silent for this daemon run. Check the cue TTS backend and "
            "API key in the voice startup logs.",
            reason=REASON_CUE_DELIVERY_NO_MANAGER,
        )
    counts = state.get("counts") if isinstance(state, dict) else None
    failed = counts.get("failed") if isinstance(counts, dict) else None
    # bool is an int subclass, and a bool here means the shape drifted.
    if not isinstance(failed, int) or isinstance(failed, bool):
        return CheckResult(
            "cue delivery",
            "skipped",
            "/state.cues has an unreadable shape — cue delivery unknown",
            reason=REASON_CUE_DELIVERY_UNAVAILABLE,
        )
    last = state.get("last")
    if isinstance(last, dict) and last.get("outcome") == OUTCOME_FAILED:
        return CheckResult(
            "cue delivery",
            "warn",
            f"last cue delivery failed {last.get('age_seconds')}s ago: "
            f"reason={last.get('reason')} slug={last.get('slug')}. " + _REMEDY,
            reason=REASON_CUE_DELIVERY_FAILED,
        )
    return CheckResult(
        "cue delivery", "ok",
        f"last cue delivery did not fail ({failed} failure(s) this daemon run)",
    )
