"""Weekly review of the triple-stream wake-event corpus.

The OR-gate fires on any of three legs (AEC ON, AEC OFF, DTLN-aec).
This tool answers: which legs are actually pulling weight, where do
the legs disagree, and which events are worth listening to?

Output:
  [1] Fire breakdown — Venn-style count of which legs crossed
      threshold at wake time, with mean per-leg score per pattern.
  [2] Per-leg score distribution — what scores does each leg
      typically reach? P10/P50/P90/Max across all events.
  [3] Distinct-leg contributions — events where exactly one leg
      saved the wake. The "Only DTLN fired" set is the most
      interesting: those events prove the third leg's distinct value.
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
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


# Canonical leg names per jasper/voice_daemon.py:1863-1874.
# fired_legs stores these CSV-joined and sorted alphabetically:
#   single: 'dtln', 'off', 'on'
#   double: 'dtln,off', 'dtln,on', 'off,on'
#   triple: 'dtln,off,on'
LEGS = ("on", "off", "dtln")
LEG_LABELS = {"on": "AEC ON", "off": "AEC OFF", "dtln": "DTLN-aec"}
SCORE_COLS = {
    "on":   "peak_score_aec_on",
    "off":  "peak_score_aec_off",
    "dtln": "peak_score_dtln_aec",
}
AUDIO_COLS = {
    "on":   "audio_on_path",
    "off":  "audio_off_path",
    "dtln": "audio_dtln_path",
}


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
    has_dtln_schema = "fired_legs" in cols and "peak_score_dtln_aec" in cols
    if not has_dtln_schema:
        print("ERROR: this DB predates triple-stream migration; no fired_legs column",
              file=sys.stderr)
        print("Pull a fresher corpus from the Pi (bash scripts/fetch-wake-events.sh)",
              file=sys.stderr)
        return 1

    rows = list(conn.execute(
        "SELECT event_id, ts_utc, trigger_kind, fired_legs, "
        "peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec, "
        "audio_on_path, audio_off_path, audio_dtln_path, "
        "outcome, outcome_detail, "
        "ts_turn_opened, ts_speech_detected, ts_tool_called, "
        "music_active, label "
        "FROM wake_events ORDER BY ts_utc"
    ))
    if not rows:
        print(f"  (empty DB at {db_path})")
        return 0

    triple = [r for r in rows if r["fired_legs"] is not None]
    legacy = [r for r in rows if r["fired_legs"] is None]

    print("=" * 72)
    print(f"Triple-stream wake-event analysis: {corpus}")
    print("=" * 72)
    print(f"  Time range: {rows[0]['ts_utc']} ... {rows[-1]['ts_utc']}")
    print(f"  Total events:        {len(rows)}")
    print(f"  Triple-stream events: {len(triple)} (with fired_legs)")
    print(f"  Legacy events:        {len(legacy)} (pre-migration, no DTLN score)")
    if not triple:
        print("\n  No triple-stream events yet. Use the speaker, then re-fetch.")
        return 0

    # ---------- [1] Fire breakdown ----------
    print()
    print("[1] Fire breakdown — which legs crossed threshold at fire time")
    print()
    # Order so single-leg patterns appear first, then doubles, then triple.
    # Lexically-sorted CSV from voice_daemon ensures canonical strings.
    canonical_order = ["on", "off", "dtln",
                       "off,on", "dtln,on", "dtln,off",
                       "dtln,off,on"]
    pattern_count = Counter(r["fired_legs"] for r in triple)
    by_pattern: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in triple:
        by_pattern[r["fired_legs"]].append(r)

    total = len(triple)
    print(f"  {'Legs fired':<24} {'Count':>6} {'%':>6}  Mean peak score (on / off / dtln)")
    print(f"  {'-'*24} {'-'*6} {'-'*6}  {'-'*36}")
    for pat in canonical_order:
        n = pattern_count.get(pat, 0)
        if n == 0:
            # Still print zero-count rows so the reader sees every possible
            # pattern. Useful for "we shipped DTLN but it never fired alone".
            zeros = (0.0, 0.0, 0.0)
            print(f"  {pat:<24} {n:>6} {0.0:>5.0f}%  {zeros[0]:.3f} / {zeros[1]:.3f} / {zeros[2]:.3f}")
            continue
        evs = by_pattern[pat]
        pct = 100.0 * n / total
        mean_on = sum(_score(e, "on") for e in evs) / n
        mean_off = sum(_score(e, "off") for e in evs) / n
        mean_dtln = sum(_score(e, "dtln") for e in evs) / n
        print(f"  {pat:<24} {n:>6} {pct:>5.1f}%  {mean_on:.3f} / {mean_off:.3f} / {mean_dtln:.3f}")
    # Catch any patterns we didn't account for (paranoia — shouldn't happen
    # given voice_daemon sorts alphabetically into the canonical set).
    unknown = set(pattern_count) - set(canonical_order)
    for pat in sorted(unknown):
        evs = by_pattern[pat]
        n = len(evs)
        pct = 100.0 * n / total
        print(f"  ⚠ {pat:<22} {n:>6} {pct:>5.1f}%  (unexpected pattern)")

    # ---------- [2] Per-leg score distribution ----------
    print()
    print("[2] Per-leg score distribution across ALL triple-stream events")
    print("    (the distribution shows what each leg 'sees', "
          "fired-or-not — useful for spotting a leg that's silently dead)")
    print()
    print(f"  {'Leg':<10} {'P10':>7} {'P50':>7} {'P90':>7} {'Max':>7}   {'n_nonnull':>10}")
    print(f"  {'-'*10} {'-'*7} {'-'*7} {'-'*7} {'-'*7}   {'-'*10}")
    for leg in LEGS:
        scores = [_score(r, leg) for r in triple if _score(r, leg) is not None]
        pcs = percentiles(scores, (10, 50, 90, 100))
        print(f"  {LEG_LABELS[leg]:<10} {pcs[10]:>7.3f} {pcs[50]:>7.3f} "
              f"{pcs[90]:>7.3f} {pcs[100]:>7.3f}   {len(scores):>10}")

    # ---------- [3] Distinct-leg contributions ----------
    print()
    print("[3] Distinct-leg contributions — events where exactly one leg fired")
    print("    (each leg's 'solo saves' prove its independent value)")
    print()
    for leg in LEGS:
        solo = by_pattern.get(leg, [])
        emoji = "★" if leg == "dtln" and solo else " "  # DTLN-only = headline
        pct = 100.0 * len(solo) / total if total else 0
        print(f"  {emoji} Only {LEG_LABELS[leg]} fired: {len(solo):>4} events ({pct:.1f}% of triple)")

    # ---------- [4] Listening playlist ----------
    print()
    print(f"[4] Listening playlist — top {args.top} per category")
    print()
    categories = [
        ("Only DTLN fired (proves DTLN's value)", by_pattern.get("dtln", []), "dtln"),
        ("Only AEC ON fired", by_pattern.get("on", []), "on"),
        ("Only AEC OFF fired (the dominant kind)", by_pattern.get("off", []), "off"),
        ("All three legs agreed", by_pattern.get("dtln,off,on", []), None),
    ]
    for title, evs, sort_leg in categories:
        if not evs:
            continue
        # Sort by score of the leg that fired alone (or by max score for "all three")
        if sort_leg is not None:
            evs = sorted(evs, key=lambda r: -(_score(r, sort_leg) or 0))
        else:
            evs = sorted(evs, key=lambda r: -max(
                _score(r, "on") or 0, _score(r, "off") or 0, _score(r, "dtln") or 0
            ))
        print(f"  {title}:")
        for r in evs[:args.top]:
            audio_for_leg = AUDIO_COLS.get(sort_leg, "audio_off_path")
            audio = r[audio_for_leg] if sort_leg else r["audio_off_path"]
            if audio == "rolled_off" or audio is None:
                audio = "<audio rolled off>"
            scores = (f"on={r['peak_score_aec_on'] or 0:.2f} "
                      f"off={r['peak_score_aec_off'] or 0:.2f} "
                      f"dtln={r['peak_score_dtln_aec'] or 0:.2f}")
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
    for solo_leg in LEGS:
        solo_evs = by_pattern.get(solo_leg, [])
        if not solo_evs:
            continue
        print(f"  When {LEG_LABELS[solo_leg]} fired ALONE ({len(solo_evs)} events):")
        for other_leg in LEGS:
            if other_leg == solo_leg:
                continue
            other_scores = [
                _score(r, other_leg) for r in solo_evs
                if _score(r, other_leg) is not None
            ]
            if not other_scores:
                print(f"    {LEG_LABELS[other_leg]:<10} (no scores recorded)")
                continue
            pcs = percentiles(other_scores, (10, 50, 90, 100))
            # Highlight the "just under threshold" zone — assume
            # threshold ~0.5; flag P90 if it's 0.30-0.49.
            tunable = "← tunable" if 0.30 <= pcs[90] < 0.50 else ""
            print(f"    {LEG_LABELS[other_leg]:<10} P10={pcs[10]:.3f} "
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


if __name__ == "__main__":
    sys.exit(main())
