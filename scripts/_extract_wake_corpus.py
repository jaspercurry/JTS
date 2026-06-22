#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Extract a training-ready wake-word corpus from the wake_events store.

Reads `wake-events.sqlite3` + per-event WAVs (typically the snapshot
produced by `scripts/fetch-wake-events.sh`) and splits high-precision
positives into up to six quadrants defined by
(AEC leg) × (music_active):

  aec_on_nomusic/    AEC ON leg, music_active=0
  aec_on_music/      AEC ON leg, music_active=1
  aec_off_nomusic/   AEC OFF leg (raw chip-direct), music_active=0
  aec_off_music/     AEC OFF leg (raw chip-direct), music_active=1
  aec_dtln_nomusic/  DTLN-aec leg, music_active=0      (only when present)
  aec_dtln_music/    DTLN-aec leg, music_active=1      (only when present)

The DTLN leg was added to JTS in PR #253 (2026-05-23). Older corpus
snapshots predate it and only have aec_on + aec_off; newer snapshots
on Pis with `JASPER_WAKE_LEG_DTLN=1` also have audio_dtln_path
populated. The extractor handles both — each event contributes either
2 legs (aec_on + aec_off) or 3 legs (plus dtln) depending on what was
captured, and the DTLN quadrant dirs simply stay empty on 2-stream
corpora.

Each quadrant gets `train/` and `eval/` subdirectories. **The eval
split must never be fed back into training** — that would leak the
held-out set into the model's weights and inflate every measured recall
number. The script enforces this only by directory naming; the
downstream training pipeline has to honour it.

High-precision filter (default): wakes that opened a turn, saw
sustained speech, and completed normally — the cleanest positives in
the corpus. SQL shape:

    SELECT ... FROM wake_events
    WHERE outcome = 'completed'
      AND ts_speech_detected IS NOT NULL
      AND audio_on_path  IS NOT NULL AND audio_on_path  != 'rolled_off'
      AND audio_off_path IS NOT NULL AND audio_off_path != 'rolled_off'
      AND (mic_muted IS NULL OR mic_muted = 0)

`--require-label LABEL` adds `AND label = ?` for runs after a manual
labeling pass (typical: `real_attempt`).

Split policy: events are paired across legs by `event_id`, so the
split is applied per-music-state to whole events (not per-leg). If
event X is in the eval split for `aec_on_music`, its AEC OFF clip
also lands in eval for `aec_off_music`. This prevents within-event
correlation from leaking into training.

The split is deterministic given `--seed` (default 42). Re-running
with the same seed and the same source corpus produces the same
assignment — important when iterating on training without
shuffling the eval set out from under you.

Usage:
  python3 scripts/_extract_wake_corpus.py                            # ./wake-events/latest → ./data/real_positives
  python3 scripts/_extract_wake_corpus.py path/to/corpus path/to/out
  python3 scripts/_extract_wake_corpus.py --require-label real_attempt
  python3 scripts/_extract_wake_corpus.py --eval-fraction 0.15
  python3 scripts/_extract_wake_corpus.py --force                    # wipe + re-extract
"""
from __future__ import annotations

import argparse
import csv
import random
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Quadrant model
# ---------------------------------------------------------------------------


QUADRANTS = (
    "aec_on_nomusic",
    "aec_on_music",
    "aec_off_nomusic",
    "aec_off_music",
    "aec_dtln_nomusic",
    "aec_dtln_music",
)


# Schema-canonical leg names. Match the `audio_<leg>_path` columns + the
# `.aec-<leg>.wav` filename suffix convention from jasper/wake_events.py.
# (The bridge env vars use "raw" for the "off" leg — JTS schema sticks
# with "off" for column-name continuity, so this script does too.)
LEGS = ("on", "off", "dtln")


def quadrant_for(leg: str, music_active: int) -> str:
    """Map (leg, music_active) to a quadrant directory name.

    `leg` is 'on', 'off', or 'dtln' (matching the .aec-on / .aec-off /
    .aec-dtln filename suffix); `music_active` is the SQLite integer
    (0/1).
    """
    if leg not in LEGS:
        raise ValueError(f"unknown leg {leg!r}; expected one of {LEGS}")
    state = "music" if music_active else "nomusic"
    return f"aec_{leg}_{state}"


# ---------------------------------------------------------------------------
# Event extraction from the DB + filesystem
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One wake event with the available leg WAVs verified on disk.

    Built by `select_events()` after the high-precision SQL filter +
    the per-row file-existence check. AEC ON + AEC OFF are always
    present (they're guaranteed by the production capture path); DTLN
    is optional — `audio_dtln_path` is None on events captured before
    PR #253 or on Pis with `JASPER_WAKE_LEG_DTLN=0`.
    """

    event_id: str
    ts_utc: str
    music_active: int
    audio_on_path: Path
    audio_off_path: Path
    audio_dtln_path: Path | None
    trigger_kind: str
    peak_score_aec_on: float | None
    peak_score_aec_off: float | None
    peak_score_dtln_aec: float | None
    outcome: str
    label: str | None
    wake_model: str | None


# DTLN is intentionally NOT in the WHERE clause — events without a DTLN
# leg are still valid 2-leg training samples. The per-row file-existence
# check in select_events() decides whether to include the dtln leg for
# each event.
_BASE_WHERE = """
    outcome = 'completed'
    AND ts_speech_detected IS NOT NULL
    AND audio_on_path  IS NOT NULL AND audio_on_path  != 'rolled_off'
    AND audio_off_path IS NOT NULL AND audio_off_path != 'rolled_off'
    AND (mic_muted IS NULL OR mic_muted = 0)
"""

# audio_dtln_path / peak_score_dtln_aec were added in PR #253. Reading
# them via SELECT is safe against pre-PR-253 snapshots because the
# WakeEventStore.open() migration adds the columns idempotently (NULL
# for old rows). If a really old snapshot somehow lacks the columns the
# SELECT raises sqlite3.OperationalError — caught at the top of
# select_events() so the script falls back to a 2-leg SELECT instead of
# erroring out entirely.
_SELECT_COLUMNS_V2 = (
    "event_id, ts_utc, music_active, "
    "audio_on_path, audio_off_path, audio_dtln_path, "
    "trigger_kind, peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec, "
    "outcome, label, wake_model"
)

# Fallback for pre-PR-253 snapshots where the DTLN columns literally
# don't exist (extreme edge case — the migration in WakeEventStore.open
# normally adds them).
_SELECT_COLUMNS_V1 = (
    "event_id, ts_utc, music_active, audio_on_path, audio_off_path, "
    "trigger_kind, peak_score_aec_on, peak_score_aec_off, "
    "outcome, label, wake_model"
)


def select_events(
    db_path: Path,
    corpus_dir: Path,
    *,
    require_label: str | None = None,
) -> list[Event]:
    """Run the high-precision filter and verify both WAVs exist on disk.

    Returns the rows as `Event` objects with absolute paths. A row that
    passed the SQL filter but is missing one of its WAVs on disk is
    skipped silently here; the caller logs the count separately so the
    operator notices large discrepancies (typically a partial fetch).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Try the v2 (3-leg) SELECT first, fall back to v1 (2-leg) if
        # the DTLN columns literally don't exist. Either should work
        # post-PR-253 even on pre-#253 row data because of the
        # idempotent ALTER TABLE migration in WakeEventStore.open().
        sql_v2 = f"SELECT {_SELECT_COLUMNS_V2} FROM wake_events WHERE {_BASE_WHERE}"
        sql_v1 = f"SELECT {_SELECT_COLUMNS_V1} FROM wake_events WHERE {_BASE_WHERE}"
        params: tuple = ()
        if require_label:
            sql_v2 += " AND label = ?"
            sql_v1 += " AND label = ?"
            params = (require_label,)
        sql_v2 += " ORDER BY ts_utc"
        sql_v1 += " ORDER BY ts_utc"
        try:
            rows = list(conn.execute(sql_v2, params))
            schema_v2 = True
        except sqlite3.OperationalError:
            rows = list(conn.execute(sql_v1, params))
            schema_v2 = False
    finally:
        conn.close()

    events: list[Event] = []
    for r in rows:
        on_path = corpus_dir / r["audio_on_path"]
        off_path = corpus_dir / r["audio_off_path"]
        if not on_path.is_file() or not off_path.is_file():
            continue
        # DTLN is optional. When the schema has the column and the row
        # populated it AND the file is on disk, include it; otherwise
        # the event is a 2-leg sample.
        dtln_path: Path | None = None
        peak_dtln: float | None = None
        if schema_v2 and r["audio_dtln_path"] and r["audio_dtln_path"] != "rolled_off":
            candidate = corpus_dir / r["audio_dtln_path"]
            if candidate.is_file():
                dtln_path = candidate
                peak_dtln = r["peak_score_dtln_aec"]
        events.append(Event(
            event_id=r["event_id"],
            ts_utc=r["ts_utc"],
            music_active=int(r["music_active"] or 0),
            audio_on_path=on_path,
            audio_off_path=off_path,
            audio_dtln_path=dtln_path,
            trigger_kind=r["trigger_kind"],
            peak_score_aec_on=r["peak_score_aec_on"],
            peak_score_aec_off=r["peak_score_aec_off"],
            peak_score_dtln_aec=peak_dtln,
            outcome=r["outcome"],
            label=r["label"],
            wake_model=r["wake_model"],
        ))
    return events


# ---------------------------------------------------------------------------
# Per-music-state split — deterministic given seed
# ---------------------------------------------------------------------------


def split_events(
    events: Iterable[Event],
    *,
    eval_fraction: float,
    seed: int,
) -> dict[str, dict[str, list[Event]]]:
    """Split events into train/eval per music_active state.

    Returns nested dict: `{music_state: {"train": [...], "eval": [...]}}`
    where music_state is "music" or "nomusic". Each event appears in
    exactly one split for its music_state — both legs (ON and OFF)
    follow the same split assignment so the held-out event's other
    leg can't leak into the model.

    Eval fraction floor: at least one event lands in eval per
    music_state if any events exist for that state. Below ~5 events
    the math says "0 in eval"; we round up because zero-eval ships
    a model that's literally untestable for that quadrant.
    """
    if not 0.0 < eval_fraction < 1.0:
        raise ValueError(
            f"eval_fraction must be in (0, 1); got {eval_fraction}"
        )

    rng = random.Random(seed)
    by_state: dict[str, list[Event]] = {"music": [], "nomusic": []}
    for ev in events:
        state = "music" if ev.music_active else "nomusic"
        by_state[state].append(ev)

    out: dict[str, dict[str, list[Event]]] = {}
    for state, evs in by_state.items():
        # Sort by event_id first so the rng.shuffle order is reproducible
        # regardless of SQL row order. (sqlite ordering is stable but
        # not contractually so across versions.)
        evs_sorted = sorted(evs, key=lambda e: e.event_id)
        rng.shuffle(evs_sorted)
        if not evs_sorted:
            out[state] = {"train": [], "eval": []}
            continue
        n_eval = max(1, round(len(evs_sorted) * eval_fraction))
        n_eval = min(n_eval, len(evs_sorted) - 1) if len(evs_sorted) > 1 else 1
        out[state] = {
            "eval": evs_sorted[:n_eval],
            "train": evs_sorted[n_eval:],
        }
    return out


# ---------------------------------------------------------------------------
# Copy + manifest writing
# ---------------------------------------------------------------------------


def write_corpus(
    splits: dict[str, dict[str, list[Event]]],
    output_dir: Path,
) -> list[dict]:
    """Copy the selected WAVs into the quadrant tree and return manifest rows.

    Manifest row is one dict per (event, leg) pair — so each event
    contributes two rows. Caller writes them to CSV.
    """
    # Create the full directory tree up front so missing-quadrant
    # consumers don't have to handle FileNotFoundError per quadrant.
    for q in QUADRANTS:
        (output_dir / q / "train").mkdir(parents=True, exist_ok=True)
        (output_dir / q / "eval").mkdir(parents=True, exist_ok=True)

    # Per-leg peak-score lookup keeps the manifest's peak_score column
    # honest about which leg's score it reports.
    def _peak_for(ev: Event, leg: str) -> float | None:
        if leg == "on":
            return ev.peak_score_aec_on
        if leg == "off":
            return ev.peak_score_aec_off
        if leg == "dtln":
            return ev.peak_score_dtln_aec
        raise AssertionError(f"unhandled leg {leg!r}")

    manifest: list[dict] = []
    for state, by_split in splits.items():
        for split, events in by_split.items():
            for ev in events:
                # AEC ON + AEC OFF are always present (guaranteed by
                # the SQL filter); DTLN is conditional on the event
                # having audio_dtln_path populated + on disk.
                legs_for_event: list[tuple[str, Path]] = [
                    ("on", ev.audio_on_path),
                    ("off", ev.audio_off_path),
                ]
                if ev.audio_dtln_path is not None:
                    legs_for_event.append(("dtln", ev.audio_dtln_path))

                for leg, src in legs_for_event:
                    quadrant = quadrant_for(leg, ev.music_active)
                    dst = output_dir / quadrant / split / src.name
                    shutil.copyfile(src, dst)
                    manifest.append({
                        "event_id": ev.event_id,
                        "leg": leg,
                        "quadrant": quadrant,
                        "split": split,
                        "music_active": ev.music_active,
                        "ts_utc": ev.ts_utc,
                        "trigger_kind": ev.trigger_kind,
                        "peak_score": _peak_for(ev, leg),
                        "outcome": ev.outcome,
                        "label": ev.label or "",
                        "wake_model": ev.wake_model or "",
                        "src_path": str(src),
                        "dst_path": str(dst),
                    })
    return manifest


_MANIFEST_FIELDNAMES = (
    "event_id", "leg", "quadrant", "split", "music_active",
    "ts_utc", "trigger_kind", "peak_score", "outcome",
    "label", "wake_model", "src_path", "dst_path",
)


def write_manifest_csv(manifest: list[dict], output_dir: Path) -> Path:
    path = output_dir / "manifest.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_MANIFEST_FIELDNAMES)
        w.writeheader()
        for row in manifest:
            w.writerow(row)
    return path


def summarize(
    manifest: list[dict],
    *,
    source_db: Path,
    eval_fraction: float,
    seed: int,
    require_label: str | None,
    skipped_missing: int,
) -> str:
    """Human-readable summary for stdout + summary.txt."""
    lines: list[str] = []
    lines.append("Wake corpus extraction")
    lines.append("=" * 60)
    lines.append(f"  source DB         : {source_db}")
    lines.append(f"  eval fraction     : {eval_fraction}")
    lines.append(f"  random seed       : {seed}")
    lines.append(f"  label filter      : {require_label or '(none)'}")
    if skipped_missing:
        lines.append(
            f"  skipped (file missing): {skipped_missing}  "
            "(rows passed SQL filter but at least one WAV not on disk)"
        )

    # Per-quadrant counts.
    counts: dict[tuple[str, str], int] = {}
    for row in manifest:
        key = (row["quadrant"], row["split"])
        counts[key] = counts.get(key, 0) + 1

    lines.append("")
    lines.append(f"  {'quadrant':<22} {'train':>8} {'eval':>8} {'total':>8}")
    lines.append(f"  {'-' * 22} {'-' * 8} {'-' * 8} {'-' * 8}")
    grand_train = grand_eval = 0
    for q in QUADRANTS:
        t = counts.get((q, "train"), 0)
        e = counts.get((q, "eval"), 0)
        lines.append(f"  {q:<22} {t:>8} {e:>8} {t + e:>8}")
        grand_train += t
        grand_eval += e
    lines.append(f"  {'-' * 22} {'-' * 8} {'-' * 8} {'-' * 8}")
    lines.append(
        f"  {'TOTAL clips':<22} "
        f"{grand_train:>8} {grand_eval:>8} {grand_train + grand_eval:>8}"
    )

    if grand_train + grand_eval == 0:
        lines.append("")
        lines.append("  ⚠  No clips extracted. Check the source corpus has")
        lines.append("     completed-with-speech events, and that the WAV")
        lines.append("     files exist alongside the SQLite DB.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        default="wake-events/latest",
        type=Path,
        help=(
            "Source corpus directory. Expected layout: "
            "<dir>/wake-events.sqlite3 plus <event_id>.aec-on.wav / "
            "<event_id>.aec-off.wav files alongside. "
            "Default: ./wake-events/latest (the symlink "
            "scripts/fetch-wake-events.sh maintains)."
        ),
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default="data/real_positives",
        type=Path,
        help=(
            "Output directory. Will contain aec_{on,off}_{nomusic,music}/"
            "{train,eval}/ trees plus manifest.csv + summary.txt. "
            "Default: ./data/real_positives"
        ),
    )
    parser.add_argument(
        "--eval-fraction",
        type=float,
        default=0.2,
        help="Fraction of events held out per music_state (default 0.2)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for the deterministic split (default 42)",
    )
    parser.add_argument(
        "--require-label",
        default=None,
        help=(
            "Only extract events whose `label` column equals this value. "
            "Typical: --require-label real_attempt after a manual labeling "
            "pass. Default: no label filter."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Wipe the output_dir before extracting. Without this, the "
            "script refuses to overwrite an existing non-empty output_dir "
            "to avoid mixing extractions from different DB snapshots."
        ),
    )
    args = parser.parse_args(argv)

    corpus_dir: Path = args.corpus_dir
    output_dir: Path = args.output_dir
    db_path = corpus_dir / "wake-events.sqlite3"

    if not db_path.is_file():
        print(f"ERROR: {db_path} not found.", file=sys.stderr)
        print(
            "       Run `bash scripts/fetch-wake-events.sh` first to "
            "pull the corpus from the Pi, or pass an explicit corpus "
            "directory as the first argument.",
            file=sys.stderr,
        )
        return 2

    # Refuse-or-wipe on existing output. Loud failure better than silent
    # overwrite — different DB snapshots produce different per-event
    # selection and merging them would invalidate the held-out eval set.
    if output_dir.exists() and any(output_dir.iterdir()):
        if not args.force:
            print(
                f"ERROR: {output_dir} is not empty. Re-run with --force "
                "to wipe and re-extract, or pick a different output dir.",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run the extraction. select_events() is the only place that touches
    # the source SQLite (read-only); everything else is filesystem.
    events = select_events(
        db_path, corpus_dir, require_label=args.require_label,
    )
    # Diagnose row-passed-SQL-but-file-missing separately so the operator
    # notices a large discrepancy.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sql_count = conn.execute(
            f"SELECT COUNT(*) FROM wake_events WHERE {_BASE_WHERE}"
            + (" AND label = ?" if args.require_label else ""),
            (args.require_label,) if args.require_label else (),
        ).fetchone()[0]
    finally:
        conn.close()
    skipped_missing = sql_count - len(events)

    splits = split_events(
        events, eval_fraction=args.eval_fraction, seed=args.seed,
    )
    manifest = write_corpus(splits, output_dir)
    write_manifest_csv(manifest, output_dir)

    summary = summarize(
        manifest,
        source_db=db_path,
        eval_fraction=args.eval_fraction,
        seed=args.seed,
        require_label=args.require_label,
        skipped_missing=skipped_missing,
    )
    (output_dir / "summary.txt").write_text(summary + "\n")
    print(summary)
    print(f"\n  manifest          : {output_dir / 'manifest.csv'}")
    print(f"  summary           : {output_dir / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
