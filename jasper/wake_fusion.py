# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wake-leg fire-decision seam (Phase 1.2).

A single thin place that decides the threshold a leg must clear to fire, so
per-condition thresholds (Phase 1.3) and any future cross-leg
corroboration/veto land *here* rather than scattered across the parallel
leg loops in `jasper.voice_daemon`. This is the hybrid: keep the legs firing
independently (`effective_threshold(leg, condition)` is the mechanism), but
route every threshold decision through one component so the upgrade path is
"grow this object," not "re-architect the loops."

**Behavior-preserving today.** With no offsets configured, the effective
threshold *is* the base threshold — i.e. the historical global-threshold
OR-gate, unchanged. Phase 1.3 fills `offsets` from the corpus (the
`condition` argument is already threaded so that's a data change, not a
signature change). Phase 1.4 adds the `verify()` stage — the *precision*
half of recall → verify, corroborating a proposed fire before the turn
opens — which also defaults to a no-op (always fire), so it stays
behavior-preserving until corroboration rules are configured.
"""
from __future__ import annotations


class WakeFuser:
    """Owns both halves of the recall → verify wake decision: the per-leg
    fire *threshold* (recall, `effective_threshold`) and the cross-leg
    *corroboration* (precision, `verify`). Stateless apart from its config;
    safe to share across the parallel leg loops (each call is a pure
    lookup)."""

    def __init__(
        self, offsets: "dict[tuple[str, str], float] | None" = None,
    ) -> None:
        # (leg_token, condition) -> additive threshold offset, in score units
        # [0,1]. Empty today => pure OR-gate (today's behavior). Phase 1.3
        # fills it from the corpus, e.g. {("off", "music"): +0.10} raises the
        # chip-direct leg's bar during music to cut sung-vocal false fires;
        # an absent (leg, condition) pair contributes 0.0 (base threshold),
        # so adding a condition can never make a leg fire *more* eagerly than
        # today until a value is set for it.
        self._offsets: dict[tuple[str, str], float] = dict(offsets or {})

    def effective_threshold(
        self, leg_token: str, condition: str, base_threshold: float,
    ) -> float:
        """The score `leg_token` must reach to fire under `condition`:
        the base threshold plus the configured offset (0.0 when unset)."""
        return base_threshold + self._offsets.get((leg_token, condition), 0.0)

    def verify(
        self, fired_leg: str, fired_set: "set[str]", condition: str,
    ) -> bool:
        """Precision stage of recall → verify. The OR-gate is RECALL: any leg
        crossing its `effective_threshold` *proposes* a fire. `verify()` then
        decides whether to actually fire (True) or suppress it (False), once
        the firing leg has won the OR-gate race and `fired_set` (the legs that
        corroborated within the staleness window) is known.

        Default: **always fire** — with no rules configured this is
        byte-identical to a plain OR-gate, so it is behavior-preserving until
        Phase 1.4 fills in corroboration rules (a shared VAD veto; require ≥2
        legs for the raw / chip-direct FP-prone classes; require the AEC-on
        leg to confirm during TTS to kill `tts_bleed`).

        **Fail-open contract (load-bearing).** This runs on the wake path, so
        any rule that cannot evaluate — missing signal, exception, genuine
        uncertainty — MUST resolve to True (fire). The verifier may only ever
        suppress a *confident* false fire; it must never make the speaker go
        deaf because verification was unsure (AGENTS.md no-silent-failure
        rule)."""
        return True
