"""Weekly review of the multi-leg wake-event corpus.

The OR-gate fires on any configured wake leg (AEC3, chip-direct raw,
DTLN-aec, and opt-in hardware AEC beams). This tool answers: which
legs are actually pulling weight, where do the legs disagree, and
which events are worth listening to?

Output:
  [1] Fire breakdown — Venn-style count of which legs crossed
      threshold at wake time, with mean per-leg score per pattern.
  [2] Per-leg score distribution — what scores does each leg
      typically reach? P10/P50/P90/Max across all events.
  [3] Distinct-leg contributions — events where exactly one leg
      saved the wake. Solo-save sets prove that leg's distinct value.
  [4] Listening playlist — N suggested events to audit by ear,
      sorted by category interest.
  [5] Funnel — how many wake events reached turn-open / speech /
      tool-call, broken down by fired_legs pattern. Surfaces
      whether DTLN-only fires are real attempts or false-positives.

Usage:
  python _analyze_three_leg.py wake-events/latest
  python _analyze_three_leg.py --top 10 wake-events/20260523T125330Z
"""
from __future__ import annotations

import argparse
import itertools
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegReviewSpec:
    """Review metadata for one wake-event leg.

    Tokens match jasper.wake_legs' frozen on-disk vocabulary and the
    fired_legs CSV. Score/audio column names are explicit because the
    original on/off/dtln columns predate the regular chip column shape.
    """

    token: str
    label: str
    score_col: str
    audio_col: str


# Review vocabulary. This intentionally mirrors the stable wake-event
# schema rather than importing jasper.voice_daemon; the review script
# should stay stdlib-only and usable against fetched corpora off-box.
LEG_SPECS: tuple[LegReviewSpec, ...] = (
    LegReviewSpec("on", "AEC3", "peak_score_aec_on", "audio_on_path"),
    LegReviewSpec("off", "Chip-direct", "peak_score_aec_off", "audio_off_path"),
    LegReviewSpec("dtln", "DTLN-aec", "peak_score_dtln_aec", "audio_dtln_path"),
    LegReviewSpec(
        "chip_aec_150",
        "Chip AEC 150",
        "peak_score_chip_aec_150",
        "audio_chip_aec_150_path",
    ),
    LegReviewSpec(
        "chip_aec_210",
        "Chip AEC 210",
        "peak_score_chip_aec_210",
        "audio_chip_aec_210_path",
    ),
)
SCORE_COLS = {spec.token: spec.score_col for spec in LEG_SPECS}
AUDIO_COLS = {spec.token: spec.audio_col for spec in LEG_SPECS}
BASE_SELECT_COLS = (
    "event_id", "ts_utc", "trigger_kind", "fired_legs",
    "outcome", "outcome_detail",
    "ts_turn_opened", "ts_speech_detected", "ts_tool_called",
    "music_active", "label",
)


def percentiles(values: list[float], pcts: tuple[int, ...]) -> dict[int, float]:
    """Pure-stdlib percentile (avoid pulling numpy into this tool —
    `_audit_wake_events.py` already needs numpy/scipy for xcorr,
    keep this one as light as possible)."""
    if not values:
        return {p: 0.0 for p in pcts}
    s = sorted(values)
    n = len(s)
    out = {}
    for p in pcts:
        idx = (p / 100.0) * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        out[p] = s[lo] + frac * (s[hi] - s[lo])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path,
                    help="wake-events corpus dir (e.g. wake-events/latest)")
    ap.add_argument("--top", type=int, default=5,
                    help="how many events to list per interesting category (default: 5)")
    args = ap.parse_args()

    corpus = args.corpus
    if not corpus.is_dir():
        print(f"ERROR: {corpus} is not a directory", file=sys.stderr)
        return 1
    db_path = corpus / "wake-events.sqlite3"
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(wake_events)")}
    if "fired_legs" not in cols:
        print("ERROR: this DB predates multi-leg migration; no fired_legs column",
              file=sys.stderr)
        print("Pull a fresher corpus from the Pi (bash scripts/fetch-wake-events.sh)",
              file=sys.stderr)
        return 1

    legs = tuple(spec for spec in LEG_SPECS if spec.score_col in cols)
    if not legs:
        print("ERROR: wake_events DB has no recognized per-leg score columns",
              file=sys.stderr)
        return 1
    select_cols = list(BASE_SELECT_COLS)
    for spec in legs:
        select_cols.append(spec.score_col)
        if spec.audio_col in cols:
            select_cols.append(spec.audio_col)
    rows = list(conn.execute(
        f"SELECT {', '.join(select_cols)} FROM wake_events ORDER BY ts_utc"
    ))
    if not rows:
        print(f"  (empty DB at {db_path})")
        return 0

    triple = [r for r in rows if r["fired_legs"] is not None]
    legacy = [r for r in rows if r["fired_legs"] is None]

    print("=" * 72)
    leg_tokens = tuple(spec.token for spec in legs)
    print(f"Multi-leg wake-event analysis: {corpus}")
    print("=" * 72)
    print(f"  Time range: {rows[0]['ts_utc']} ... {rows[-1]['ts_utc']}")
    print(f"  Total events:        {len(rows)}")
    print(f"  Multi-leg events:    {len(triple)} (with fired_legs)")
    print(f"  Legacy events:       {len(legacy)} (pre-migration)")
    print(f"  Analyzed legs:       {', '.join(spec.label for spec in legs)}")
    if not triple:
        print("\n  No multi-leg events yet. Use the speaker, then re-fetch.")
        return 0

    pattern_count = Counter(r["fired_legs"] for r in triple)

    # ---------- [1] Fire breakdown ----------
    print()
    print("[1] Fire breakdown — which legs crossed threshold at fire time")
    print()
    # fired_legs stores CSV-joined tokens sorted alphabetically by
    # voice_daemon. Print every possible pattern while the leg set is
    # small; once chip beams are included, print observed patterns plus
    # all zero-count solo rows so the table stays readable.
    canonical_order = _canonical_patterns(leg_tokens, observed=tuple(pattern_count))
    by_pattern: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in triple:
        by_pattern[r["fired_legs"]].append(r)

    total = len(triple)
    score_header = " / ".join(spec.token for spec in legs)
    print(f"  {'Legs fired':<42} {'Count':>6} {'%':>6}  Mean peak score ({score_header})")
    print(f"  {'-'*42} {'-'*6} {'-'*6}  {'-'*36}")
    for pat in canonical_order:
        n = pattern_count.get(pat, 0)
        if n == 0:
            # Still print zero-count rows so the reader sees every possible
            # pattern. Useful for "we shipped DTLN but it never fired alone".
            zero_scores = " / ".join("0.000" for _ in legs)
            print(f"  {pat:<42} {n:>6} {0.0:>5.0f}%  {zero_scores}")
            continue
        evs = by_pattern[pat]
        pct = 100.0 * n / total
        means = []
        for spec in legs:
            scores = [_score(e, spec.token) or 0.0 for e in evs]
            means.append(sum(scores) / n)
        mean_text = " / ".join(f"{m:.3f}" for m in means)
        print(f"  {pat:<42} {n:>6} {pct:>5.1f}%  {mean_text}")
    # Catch any patterns we didn't account for (paranoia — shouldn't happen
    # given voice_daemon sorts alphabetically into the canonical set).
    unknown = set(pattern_count) - set(canonical_order)
    for pat in sorted(unknown):
        evs = by_pattern[pat]
        n = len(evs)
        pct = 100.0 * n / total
        print(f"  ! {pat:<40} {n:>6} {pct:>5.1f}%  (unexpected pattern)")

    # ---------- [2] Per-leg score distribution ----------
    print()
    print("[2] Per-leg score distribution across ALL multi-leg events")
    print("    (the distribution shows what each leg 'sees', "
          "fired-or-not — useful for spotting a leg that's silently dead)")
    print()
    label_width = max(12, max(len(spec.label) for spec in legs))
    print(f"  {'Leg':<{label_width}} {'P10':>7} {'P50':>7} {'P90':>7} {'Max':>7}   {'n_nonnull':>10}")
    print(f"  {'-'*label_width} {'-'*7} {'-'*7} {'-'*7} {'-'*7}   {'-'*10}")
    for spec in legs:
        scores = [_score(r, spec.token) for r in triple if _score(r, spec.token) is not None]
        pcs = percentiles(scores, (10, 50, 90, 100))
        print(f"  {spec.label:<{label_width}} {pcs[10]:>7.3f} {pcs[50]:>7.3f} "
              f"{pcs[90]:>7.3f} {pcs[100]:>7.3f}   {len(scores):>10}")

    # ---------- [3] Distinct-leg contributions ----------
    print()
    print("[3] Distinct-leg contributions — events where exactly one leg fired")
    print("    (each leg's 'solo saves' prove its independent value)")
    print()
    for spec in legs:
        solo = by_pattern.get(spec.token, [])
        pct = 100.0 * len(solo) / total if total else 0
        print(
            f"    Only {spec.label} fired: {len(solo):>4} events "
            f"({pct:.1f}% of multi-leg)"
        )

    # ---------- [4] Listening playlist ----------
    print()
    print(f"[4] Listening playlist — top {args.top} per category")
    print()
    categories: list[tuple[str, list[sqlite3.Row], str | None]] = []
    for spec in legs:
        categories.append((f"Only {spec.label} fired", by_pattern.get(spec.token, []), spec.token))
    all_pattern = _pattern(leg_tokens)
    categories.append(("All analyzed legs agreed", by_pattern.get(all_pattern, []), None))
    for title, evs, sort_leg in categories:
        if not evs:
            continue
        # Sort by score of the leg that fired alone (or by max score for "all three")
        if sort_leg is not None:
            evs = sorted(evs, key=lambda r: -(_score(r, sort_leg) or 0))
        else:
            evs = sorted(evs, key=lambda r: -max(_score(r, spec.token) or 0 for spec in legs))
        print(f"  {title}:")
        for r in evs[:args.top]:
            audio = _audio_path(r, sort_leg, legs)
            if audio == "rolled_off" or audio is None:
                audio = "<audio rolled off>"
            scores = " ".join(
                f"{spec.token}={_score(r, spec.token) or 0:.2f}"
                for spec in legs
            )
            music = "music" if r["music_active"] else "quiet"
            print(f"    {r['ts_utc'][:19]}  evt={r['event_id'][:8]}  "
                  f"{scores}  [{music}]  outcome={r['outcome'] or '(open)'}")
            if audio != "<audio rolled off>":
                print(f"      → afplay {corpus}/{audio}")
        print()

    # ---------- [5] Funnel ----------
    print("[5] Funnel — % of fires that reached each stage, by pattern")
    print("    (a fired pattern that NEVER reaches 'speech_detected' is")
    print("     a false-fire indicator)")
    print()
    print(f"  {'Pattern':<24} {'Fired':>6} {'Turn':>6} {'Speech':>7} {'Tool':>6}")
    print(f"  {'-'*24} {'-'*6} {'-'*6} {'-'*7} {'-'*6}")
    for pat in canonical_order:
        evs = by_pattern.get(pat, [])
        if not evs:
            continue
        n = len(evs)
        turn = sum(1 for r in evs if r["ts_turn_opened"])
        speech = sum(1 for r in evs if r["ts_speech_detected"])
        tool = sum(1 for r in evs if r["ts_tool_called"])
        print(f"  {pat:<24} {n:>6} {turn:>6} {speech:>7} {tool:>6}")
    print()

    # ---------- [6] Threshold-tuning hints ----------
    # For each solo-leg pattern, look at the OTHER legs' peak scores
    # — these are the scores legs HAD at fire time but didn't act on
    # because they were sub-threshold. If the distribution clusters
    # just below threshold (e.g., 0.40-0.49 on a 0.50 threshold), a
    # small threshold reduction on that leg would have brought it
    # into the solo-save population. If the distribution is uniformly
    # near zero, that leg is genuinely silent on these events and no
    # threshold change would help.
    #
    # Operates on existing peak_score_* columns — no near-miss event
    # capture needed. "Sub-threshold" here means "the leg didn't
    # fire, but it still scored something during the fire window."
    print("[6] Sub-threshold scores on non-firing legs — would lowering")
    print("    a threshold catch more wakes? Per solo-fire pattern, the")
    print("    distribution of the OTHER legs' peak scores. A cluster")
    print("    in 0.40-0.49 (just under 0.50) suggests threshold tuning;")
    print("    a distribution near zero means the leg is genuinely silent.")
    print()
    for solo_spec in legs:
        solo_evs = by_pattern.get(solo_spec.token, [])
        if not solo_evs:
            continue
        print(f"  When {solo_spec.label} fired ALONE ({len(solo_evs)} events):")
        for other_spec in legs:
            if other_spec.token == solo_spec.token:
                continue
            other_scores = [
                _score(r, other_spec.token) for r in solo_evs
                if _score(r, other_spec.token) is not None
            ]
            if not other_scores:
                print(f"    {other_spec.label:<{label_width}} (no scores recorded)")
                continue
            pcs = percentiles(other_scores, (10, 50, 90, 100))
            # Highlight the "just under threshold" zone — assume
            # threshold ~0.5; flag P90 if it's 0.30-0.49.
            tunable = "← tunable" if 0.30 <= pcs[90] < 0.50 else ""
            print(f"    {other_spec.label:<{label_width}} P10={pcs[10]:.3f} "
                  f"P50={pcs[50]:.3f} P90={pcs[90]:.3f} Max={pcs[100]:.3f}  "
                  f"{tunable}")
        print()

    # ---------- end ----------
    print("=" * 72)
    return 0


def _score(row: sqlite3.Row, leg: str) -> float | None:
    """Look up a row's peak_score for a leg by canonical leg name."""
    val = row[SCORE_COLS[leg]]
    return float(val) if val is not None else None


def _pattern(tokens: tuple[str, ...] | list[str]) -> str:
    """fired_legs' canonical storage order: CSV of lexically sorted tokens."""
    return ",".join(sorted(tokens))


def _canonical_patterns(legs: tuple[str, ...], *, observed: tuple[str, ...]) -> list[str]:
    """Patterns to print in the fire/funnel tables.

    The original three-leg corpus printed all seven combinations. With
    chip beams enabled the full powerset is 31 rows, which drowns out the
    useful signal on small corpora. Preserve exhaustive output for three
    or fewer legs; otherwise show every observed pattern plus all solo
    rows so missing solo-save value stays visible.
    """
    if len(legs) <= 3:
        patterns = [
            _pattern(list(combo))
            for n in range(1, len(legs) + 1)
            for combo in itertools.combinations(legs, n)
        ]
        return sorted(patterns, key=lambda p: (p.count(","), p))
    patterns = {_pattern([leg]) for leg in legs}
    patterns.update(observed)
    return sorted(patterns, key=lambda p: (p.count(","), p))


def _audio_path(
    row: sqlite3.Row,
    preferred_leg: str | None,
    legs: tuple[LegReviewSpec, ...],
) -> str | None:
    """Pick the best WAV path for a playlist row.

    For solo-save rows we prefer that leg. For aggregate rows, or older
    DBs missing a leg's audio column, fall back to the first available
    leg audio path in review order.
    """
    if preferred_leg is not None:
        col = AUDIO_COLS.get(preferred_leg)
        if col is not None and col in row.keys():
            val = row[col]
            if val:
                return str(val)
    for spec in legs:
        if spec.audio_col in row.keys():
            val = row[spec.audio_col]
            if val:
                return str(val)
    return None


if __name__ == "__main__":
    sys.exit(main())
