"""Pure ranking function for multi-device wake arbitration.

Given a list of WakeReport messages (one per peer that heard the
same utterance), `rank()` returns the peer_id of the winner. The
function is **deterministic** — every peer applying it to the same
input set reaches the same conclusion. That property is the safety
property of the whole arbitration: no consensus protocol, no leader
election, just identical pure functions running on identical inputs.

Ranking proceeds as a *cascade of tiers*. Each tier filters the
candidate pool down; the next tier only sees what survived. A peer
that "loses" a tier is gone for good. A tier with a single survivor
returns that survivor as the winner; a tier with multiple survivors
falls through to the next tier.

Tier 1: can_serve = True > can_serve = False
  A peer with a paused connection / spend cap reached can still
  bid (so the fleet knows the wake happened), but doesn't beat a
  peer that can actually serve. If NO peer can serve, the
  highest-confidence one still wins — exactly one peer plays the
  failure cue rather than all of them.

Tier 2: confidence band (within CONFIDENCE_TIE_EPS of the top)
  openWakeWord's per-frame probability score. Designed to be
  roughly gain-invariant across mics. A peer outside the band loses
  to the band's contents. We use a *band* rather than a strict
  ordering so that detection-jitter (0.83 vs 0.85 from the same
  audio) doesn't dominate over physical positioning.

Tier 3: primary flag (a single primary in the band wins)
  When the user has marked one peer as the household primary via
  `JASPER_PEER_PRIMARY=1`, that peer wins any tie *inside the
  confidence band*. This is the soft-affinity knob — meaningful in
  close calls (`primary` is set ⇒ it almost certainly heard the
  wake clearly), invisible when a different peer is clearly better
  positioned.

Tier 4: SNR (higher wins; tiebreaker eps SNR_TIE_EPS_DB)
  Gain-sensitive across heterogeneous mics, so it's a tiebreaker
  not a primary signal. Missing SNR ranks as worst.

Tier 5: RMS (higher wins, exact compare)
  Last numeric signal. Mostly only useful when the fleet is
  homogeneous (identical mic hardware).

Tier 6: peer_id (lowest UUID wins, lex compare)
  Final deterministic tiebreaker. Guarantees that even if every
  other signal is identical, every peer picks the same winner.

The design hub for these signals is docs/satellites.md "Proposed
approach for JTS" — this implements the multi-Pi version of those
rules (the doc describes the multi-mic-around-one-Pi case).
"""
from __future__ import annotations

from dataclasses import dataclass


# Two confidence scores within this distance are treated as tied for
# purposes of the next-tier tiebreakers. 0.05 is wide enough to absorb
# the per-frame jitter openWakeWord shows on the same audio + tight
# enough that a clearly-better-positioned peer (gap > 0.05) wins
# outright.
CONFIDENCE_TIE_EPS = 0.05

# SNR gap (in dB) considered "close enough" for the RMS tiebreaker
# to kick in. 3 dB ≈ 2× power; below that, the SNR estimate's own
# noise dominates.
SNR_TIE_EPS_DB = 3.0

# Historical name kept for tests / tooling that displays an
# "effective score". The current ranking uses primary as a Tier-3
# boolean filter (within the confidence band, an explicitly primary
# peer wins over non-primaries) rather than an additive score bonus.
# A boolean filter cleanly handles the "primary should win close
# calls but not override clearly better positioning" intent without
# the eps-boundary fiddliness of an additive bias.
PRIMARY_BIAS = 0.05


@dataclass(frozen=True, slots=True)
class WakeReport:
    """One peer's claim on a wake event.

    Immutable — these flow through the state machine as values, not
    as references that get mutated mid-arbitration.
    """

    peer_id: str
    score: float          # openWakeWord confidence, [0.0, 1.0]
    snr_db: float | None  # estimated SNR; None if not available
    rms_dbfs: float | None  # waveform RMS in dBFS; None if not available
    primary: bool         # JASPER_PEER_PRIMARY for this peer
    can_serve: bool       # True if this peer can open an LLM session right now

    def __post_init__(self) -> None:
        # Sanity: clamp out-of-range scores so a misbehaving peer can't
        # corrupt the ranking. Frozen dataclass → object.__setattr__.
        score = max(0.0, min(1.0, float(self.score)))
        if score != self.score:
            object.__setattr__(self, "score", score)


def rank(reports: list[WakeReport]) -> str:
    """Return the peer_id of the arbitration winner.

    Raises ValueError on empty input — empty arbitration is a bug at
    the caller (the local peer should always have contributed at
    least its own report).
    """
    if not reports:
        raise ValueError("rank(): no reports to arbitrate")

    # Tier 1: prefer servable peers. If none can serve, fall through
    # to the full pool so exactly one peer plays the failure cue.
    pool = [r for r in reports if r.can_serve] or list(reports)

    # Tier 2: confidence band (within eps of the top).
    top_score = max(r.score for r in pool)
    pool = [r for r in pool if r.score >= top_score - CONFIDENCE_TIE_EPS]
    if len(pool) == 1:
        return pool[0].peer_id

    # Tier 3: primary preference. If exactly one primary in the band,
    # it wins. If multiple primaries (rare — the user shouldn't mark
    # two), restrict subsequent tiers to the primaries.
    primaries = [r for r in pool if r.primary]
    if len(primaries) == 1:
        return primaries[0].peer_id
    if len(primaries) > 1:
        pool = primaries

    # Tier 4: SNR band.
    snrs = [_snr_or_neg_inf(r) for r in pool]
    top_snr = max(snrs)
    pool = [
        r for r in pool
        if _snr_or_neg_inf(r) >= top_snr - SNR_TIE_EPS_DB
    ]
    if len(pool) == 1:
        return pool[0].peer_id

    # Tier 5: RMS (exact, no eps — by now we're splitting hairs).
    rms = [_rms_or_neg_inf(r) for r in pool]
    top_rms = max(rms)
    pool = [r for r in pool if _rms_or_neg_inf(r) == top_rms]
    if len(pool) == 1:
        return pool[0].peer_id

    # Tier 6: deterministic final tiebreaker. Lexicographic peer_id.
    return min(pool, key=lambda r: r.peer_id).peer_id


def _snr_or_neg_inf(r: WakeReport) -> float:
    return r.snr_db if r.snr_db is not None else float("-inf")


def _rms_or_neg_inf(r: WakeReport) -> float:
    return r.rms_dbfs if r.rms_dbfs is not None else float("-inf")
