# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-wake-review — Build a listening-review package from scoring output.

Consumes a `jasper-wake-score` CSV + the corpus directory it scored,
produces a `review/` directory with:

  index.html         Sortable table with embedded HTML5 audio players
                     so the operator can listen in any browser without
                     hunting for WAV files in a Finder/Explorer window
  README.md          What to listen for; deliberately omits metric
                     claims to avoid biasing perception
  YOUR_VERDICT.md    Template for the listener to fill in (per the
                     methodology in docs/HANDOFF-wake-training-experiment.md)
  scores.csv         Copy of the input for reference
  clips/             Copies (or symlinks) of the WAVs the HTML references
                     via relative paths — keeps the package self-contained
                     when zipped or moved

The principle from `docs/HANDOFF-wake-training-experiment.md` §6
methodology principle #4: "metrics rank, ears select." The CSV
narrows 50 candidates to 5; the review package is what turns those
5 into a decision the operator's ear has signed off on.

Used at the human-in-the-loop checkpoints:
- Checkpoint 1 (Phase 0c): validate the gold corpus is fit for purpose
- Checkpoint 4 (Phase 1e): listen to new-model wins vs `jarvis_v2`
- Checkpoint 5 (Phase 1f): final sanity before Pi deploy

Usage:
  jasper-wake-review SCORES_CSV CORPUS_DIR --output review/
  jasper-wake-review SCORES_CSV CORPUS_DIR --title "v1 baseline" --symlink
"""
from __future__ import annotations

import argparse
import csv
import html
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("jasper-wake-review")


DEFAULT_OUTPUT_DIRNAME = "review"

# Cap the number of audio elements per HTML page. Browsers can handle
# more, but the page becomes sluggish + the operator can't realistically
# listen to >100 clips in one sitting. If the corpus is bigger than this,
# we render top-N and bottom-N sections (so worst-fires and best-fires
# are surfaced, not whichever happened to be alphabetically first).
DEFAULT_TOP_BOTTOM_N = 50


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreRow:
    """One row from the scores CSV with typed fields.

    Mirrors `jasper.cli.wake_score.ScoredClip` but loaded from CSV
    (so types are coerced from strings). Decoupling the review tool
    from the scoring tool via CSV means the two can be developed
    independently AND the operator can hand-edit the CSV before
    review (e.g. add a `notes` column manually) without breaking
    the renderer.
    """

    path: Path
    leg: str
    condition: str
    split: str
    peak_score: float
    mean_score: float
    frame_count: int
    fired: bool
    duration_sec: float


def load_scores(csv_path: Path) -> list[ScoreRow]:
    """Parse a scores.csv into typed ScoreRow records.

    Raises ValueError if the CSV is missing required columns — the
    review tool can't render anything meaningful without them, so
    better to fail loudly than render a blank table.
    """
    required_columns = {
        "path", "leg", "condition", "split",
        "peak_score", "mean_score", "frame_count",
        "fired", "duration_sec",
    }
    rows: list[ScoreRow] = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path}: empty or unreadable CSV")
        missing = required_columns - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"{csv_path}: missing required columns {sorted(missing)}"
            )
        for row in reader:
            rows.append(ScoreRow(
                path=Path(row["path"]),
                leg=row["leg"],
                condition=row["condition"],
                split=row["split"],
                peak_score=float(row["peak_score"]),
                mean_score=float(row["mean_score"]),
                frame_count=int(row["frame_count"]),
                fired=bool(int(row["fired"])),
                duration_sec=float(row["duration_sec"]),
            ))
    return rows


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


# CSS lives inline so the package is one HTML file + audio files, no
# additional static assets. Minimal styling, no JS. Reads fine in
# Safari/Chrome/Firefox without any build step.
_HTML_STYLE = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Roboto, sans-serif;
    max-width: 1200px;
    margin: 2em auto;
    padding: 0 1em;
    color: #222;
  }
  h1 { border-bottom: 2px solid #333; padding-bottom: 0.3em; }
  h2 { margin-top: 2em; color: #444; }
  table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
  th, td {
    padding: 0.35em 0.6em;
    text-align: left;
    border-bottom: 1px solid #ddd;
    vertical-align: middle;
  }
  th { background: #f4f4f4; font-weight: 600; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  td.fired-yes { color: #1f7a1f; font-weight: 600; }
  td.fired-no  { color: #888; }
  audio { width: 220px; height: 28px; }
  .intro {
    background: #f9f9f4;
    border-left: 4px solid #b8b86b;
    padding: 0.8em 1em;
    margin: 1em 0 1.5em;
  }
  .intro p { margin: 0.3em 0; }
  .filename { color: #666; font-size: 0.86em; }
"""


def _render_row(row: ScoreRow) -> str:
    fired_class = "fired-yes" if row.fired else "fired-no"
    fired_label = "YES" if row.fired else "no"
    return (
        "<tr>"
        f"<td>{html.escape(row.leg)}</td>"
        f"<td>{html.escape(row.condition)}</td>"
        f"<td>{html.escape(row.split)}</td>"
        f'<td class="num">{row.peak_score:.3f}</td>'
        f'<td class="num">{row.mean_score:.3f}</td>'
        f'<td class="{fired_class}">{fired_label}</td>'
        f'<td><audio controls preload="none" src="clips/{html.escape(row.path.name)}"></audio></td>'
        f'<td class="filename">{html.escape(str(row.path))}</td>'
        "</tr>"
    )


def _render_table(rows: Iterable[ScoreRow]) -> str:
    body = "\n      ".join(_render_row(r) for r in rows)
    return (
        "<table>\n"
        "  <thead><tr>"
        "<th>leg</th><th>condition</th><th>split</th>"
        '<th class="num">peak</th><th class="num">mean</th>'
        "<th>fired</th><th>audio</th><th>filename</th>"
        "</tr></thead>\n"
        f"  <tbody>\n      {body}\n  </tbody>\n"
        "</table>"
    )


def build_index_html(
    rows: list[ScoreRow], title: str, top_n: int = DEFAULT_TOP_BOTTOM_N,
) -> str:
    """Render the full index.html as a string.

    Layout strategy: if the corpus is small (≤ 2×top_n clips), show
    everything in one table sorted by peak score descending. If it's
    larger, split into "top N (highest scoring)" + "bottom N (lowest
    scoring)" sections — surfacing the most-and-least-confident
    detections, which are what the operator actually needs to listen
    to (the middle is uninteresting).
    """
    sorted_rows = sorted(rows, key=lambda r: r.peak_score, reverse=True)
    intro = (
        '<div class="intro">'
        "<p><strong>What to listen for:</strong> "
        "Click each clip's audio player to hear it. Note whether the "
        "audio actually contains a clear wake utterance, and whether "
        "the model's fire-or-not classification matches what your ear "
        "tells you. Patterns matter more than individual clips.</p>"
        "<p>Fill in <code>YOUR_VERDICT.md</code> in this directory "
        "when you're done.</p>"
        "</div>"
    )

    body_sections: list[str] = []
    if len(sorted_rows) <= top_n * 2:
        body_sections.append(
            "<h2>All clips (sorted by peak score, highest first)</h2>"
        )
        body_sections.append(_render_table(sorted_rows))
    else:
        body_sections.append(
            f"<h2>Top {top_n} (highest peak scores — what the model fires on)</h2>"
        )
        body_sections.append(_render_table(sorted_rows[:top_n]))
        body_sections.append(
            f"<h2>Bottom {top_n} (lowest peak scores — what the model misses)</h2>"
        )
        body_sections.append(_render_table(sorted_rows[-top_n:]))

    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_HTML_STYLE}</style>\n"
        "</head><body>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{intro}\n"
        + "\n".join(body_sections)
        + "\n</body></html>\n"
    )


# ---------------------------------------------------------------------------
# README + verdict template
# ---------------------------------------------------------------------------


_README_TEMPLATE = """# Listening review: {title}

Generated at {timestamp}. This is the review package for one wake-word
scoring run — open `index.html` in a browser to listen to the clips
alongside their metadata, then fill in `YOUR_VERDICT.md` with your
read.

## What to listen for

The HTML table is sorted by peak score (highest first) so you can
focus on what matters:

- **Top of the list** = clips the model fired on most confidently.
  Listen and confirm: was it actually a clear wake-word utterance?
  Or is the model firing on something it shouldn't (an artifact,
  a partial phoneme, background music)?
- **Bottom of the list** = clips the model failed to detect. Listen
  and confirm: was the utterance actually unclear (mumbled, far
  away, drowned out)? Or did the model miss something the operator's
  ear can clearly hear?
- **Middle ground** (around the threshold) = the borderline cases.
  These are where threshold tuning has the most impact.

## What's in this package

- `index.html` — the main review interface (open in a browser)
- `YOUR_VERDICT.md` — fill this in with your read; the experiment
  tracking is the better for having a written record
- `scores.csv` — full per-clip scores, for spreadsheet analysis if
  you want to slice the data
- `clips/` — the WAVs the HTML references via relative paths
  (so the whole directory can be zipped, moved, or shared as one
  self-contained artifact)

## Methodology reminder

Per `docs/HANDOFF-wake-training-experiment.md` §6: **metrics rank,
ears select.** Don't merge or ship anything based on numbers alone.
If the numbers say one thing but listening tells you the opposite,
your ears win. The reason the verdict template exists is to make
that judgment explicit + recorded.
"""


_VERDICT_TEMPLATE = """# Verdict: {title}

Fill this in after listening to a representative subset of clips
in `index.html`. Don't worry about being exhaustive — capture the
patterns you noticed, not every clip.

## High-scoring clips (top of the list)

Did the model fire on actual wake utterances? Or are there false
positives (artifacts, partial phonemes, music)?

> _Your read here. E.g. "Top 10 all sounded like clear 'Jarvis' to
> me. Top 11-20 included 3 that fired on TV background noise; the
> rest were clean."_

## Low-scoring clips (bottom of the list)

Did the model miss clips you could clearly hear? Or do the misses
have obvious reasons (heavy reverb, far distance, mumbled)?

> _Your read here. E.g. "Bottom 10 were all far+music — couldn't
> blame the model for missing them. Bottom 11-20 included some
> clear 'Jarvis' that I'd have expected to fire; flagged below."_

## Specific clips worth a second look

List anything notable — false positives that surprised you, false
negatives that shouldn't have been missed, anything that doesn't
match expectations.

> _Your notes here._

## Overall verdict

- [ ] Approve as-is (results match my ears)
- [ ] Approve with caveats (note them above)
- [ ] Reject: metric says A, ears say B (explain above)
- [ ] Need to investigate further

## Free-form notes

Anything else worth recording for the next iteration.

> _Your notes here._

---

Verdict recorded: {timestamp}
"""


def render_readme(title: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return _README_TEMPLATE.format(
        title=title,
        timestamp=now.strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_verdict_template(title: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return _VERDICT_TEMPLATE.format(
        title=title,
        timestamp=now.strftime("%Y-%m-%d %H:%M UTC"),
    )


# ---------------------------------------------------------------------------
# Package writing
# ---------------------------------------------------------------------------


def _place_clip(src: Path, dst: Path, *, symlink: bool) -> None:
    """Place a single clip in the package. Symlink or copy.

    Symlink is faster + uses no extra disk, but breaks if the
    package is moved off the host. Copy is slower + uses ~400 KB
    per clip but produces a self-contained package safe to zip + share.
    Default is copy (correctness > speed).
    """
    if dst.exists():
        dst.unlink()
    if symlink:
        # Use absolute src path so symlinks survive cwd changes when
        # the package is opened. Relative symlinks would break.
        os.symlink(src.resolve(), dst)
    else:
        shutil.copyfile(src, dst)


def write_review_package(
    rows: list[ScoreRow],
    corpus_dir: Path,
    output_dir: Path,
    *,
    title: str = "Wake-word listening review",
    symlink: bool = False,
    scores_csv: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Write the full review package to `output_dir`.

    If `output_dir` exists and is non-empty, the script raises rather
    than overwriting — protects against accidentally clobbering a
    previous review's verdict file. Use a fresh directory per run
    (the CLI does this automatically by stamping the timestamp into
    the default output path).

    Clips referenced by `rows` but missing on disk are logged at
    WARNING and skipped — the HTML simply omits the audio element
    for that row (would render a broken control otherwise).
    """
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(
            f"{output_dir} is non-empty; pick a fresh dir to avoid "
            "clobbering a previous review's notes"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = output_dir / "clips"
    clips_dir.mkdir()

    # Resolve corpus_dir once for the defense-in-depth path-containment
    # check below. Symlinks are resolved so we compare canonical paths.
    corpus_resolved = corpus_dir.resolve()
    placed_rows: list[ScoreRow] = []
    for row in rows:
        # row.path may be absolute (from scoring with an absolute
        # corpus_dir) or relative; either way, resolve it against
        # the corpus_dir if it's not already absolute.
        src = row.path if row.path.is_absolute() else corpus_dir / row.path
        # Defense-in-depth: refuse paths that escape corpus_dir. CSV
        # output from jasper-wake-score never produces escaping paths
        # (walk_corpus only emits paths inside corpus_dir), but a
        # hand-edited or untrusted CSV could try to copy files from
        # outside the corpus. Better to fail noisily than to silently
        # bundle /etc/passwd into a review package.
        try:
            src.resolve().relative_to(corpus_resolved)
        except ValueError:
            logger.warning(
                "clip path escapes corpus_dir, skipping: %s "
                "(resolved=%s, corpus=%s)",
                src, src.resolve(), corpus_resolved,
            )
            continue
        if not src.is_file():
            logger.warning("clip not found, skipping: %s", src)
            continue
        dst = clips_dir / src.name
        _place_clip(src, dst, symlink=symlink)
        placed_rows.append(row)

    html_content = build_index_html(placed_rows, title)
    (output_dir / "index.html").write_text(html_content)
    (output_dir / "README.md").write_text(render_readme(title, now=now))
    (output_dir / "YOUR_VERDICT.md").write_text(
        render_verdict_template(title, now=now),
    )

    # Copy the scores CSV verbatim into the package for reference.
    if scores_csv is not None and scores_csv.is_file():
        shutil.copyfile(scores_csv, output_dir / "scores.csv")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _default_output_dir() -> Path:
    """`review/<UTC-timestamp>/` — stamped so re-runs don't clash."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(DEFAULT_OUTPUT_DIRNAME) / ts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-wake-review",
        description=__doc__.split("\n\n")[0] if __doc__ else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "scores_csv",
        type=Path,
        help="Per-clip scores CSV from jasper-wake-score.",
    )
    parser.add_argument(
        "corpus_dir",
        type=Path,
        help="Corpus directory the CSV was scored against (so this "
             "tool can resolve relative paths in the CSV). Same dir "
             "you passed to jasper-wake-score.",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=None,
        help="Output directory for the review package. Default: "
             f"./{DEFAULT_OUTPUT_DIRNAME}/<UTC-timestamp>/ (timestamp "
             "makes re-runs not clash).",
    )
    parser.add_argument(
        "--title",
        type=str,
        default="Wake-word listening review",
        help="Page + verdict-template title.",
    )
    parser.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink clips into the package instead of copying. "
             "Faster + no extra disk, but the package breaks if "
             "moved or zipped. Default: copy (self-contained).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.scores_csv.is_file():
        print(f"ERROR: {args.scores_csv} not found", file=sys.stderr)
        return 2
    if not args.corpus_dir.is_dir():
        print(f"ERROR: {args.corpus_dir} is not a directory", file=sys.stderr)
        return 2

    output_dir = args.output or _default_output_dir()
    if output_dir.exists() and any(output_dir.iterdir()):
        print(
            f"ERROR: {output_dir} is non-empty; pick a fresh dir to "
            "avoid clobbering a previous review's notes",
            file=sys.stderr,
        )
        return 2

    try:
        rows = load_scores(args.scores_csv)
    except (ValueError, OSError) as e:
        print(f"ERROR loading scores CSV: {e}", file=sys.stderr)
        return 1

    if not rows:
        print(
            f"ERROR: {args.scores_csv} has no data rows", file=sys.stderr,
        )
        return 1

    try:
        write_review_package(
            rows,
            args.corpus_dir,
            output_dir,
            title=args.title,
            symlink=args.symlink,
            scores_csv=args.scores_csv,
        )
    except ValueError as e:
        print(f"ERROR writing package: {e}", file=sys.stderr)
        return 2

    print(f"Review package ready: {output_dir}/")
    print(f"  Open: {output_dir / 'index.html'}")
    print(f"  Fill in: {output_dir / 'YOUR_VERDICT.md'}")
    print(f"  Clips placed: {len(rows)} (symlink={args.symlink})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
