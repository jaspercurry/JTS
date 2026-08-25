#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Right-size baseline report: per-zone line census, Python prose-to-code
# ratio, and a dead-code gauge. Laptop-side report tool, NOT a CI gate --
# budgets land only at campaign end (docs/REFACTOR-2026-08.md, wave 0.5).
#
# Usage:
#   bash scripts/right-size-report.sh
#
# Zone membership mirrors the "Ownership boundaries" section of
# docs/REFACTOR-2026-08.md, which is the source of truth for the
# tuning/platform split; keep zone_of() below in sync with it. The per-zone
# rows are also the tuning program's net-lines evidence.
#
# Needs git + python3 only. vulture, cargo and a JS analyzer are optional;
# each section prints RAN or SKIPPED so an absent tool is never silent.
# Deleted with the campaign, same lifecycle as docs/REFACTOR-2026-08.md.

set -euo pipefail
export LC_ALL=C

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

printf 'JTS right-size report\n'
printf 'commit: %s\n' "$(git rev-parse HEAD)"

cat >"$tmpdir/census.py" <<'PY'
import io
import os
import sys
import tokenize

ZONES = [
    "tuning-product",
    "tuning-tests",
    "product",
    "tests",
    "rust",
    "c",
    "deploy",
    "scripts",
    "web-assets",
    "docs",
    "other",
]

TUNING_PKGS = (
    "jasper/active_speaker/",
    "jasper/audio_measurement/",
    "jasper/correction/",
    "jasper/bass_extension/",
)
TUNING_CLI = {
    "seat_level.py",
    "correction_bundle.py",
    "measurement_mic.py",
    "crossover_prescriber.py",
    "driver_trim.py",
    "angle_capture.py",
    "arm_walk.py",
    "noise_capture.py",
    "read_distortion.py",
    "round_views.py",
    "classify_features.py",
}
TUNING_TEST_PREFIXES = (
    "test_active_speaker",
    "test_audio_measurement",
    "test_correction",
    "test_crossover",
    "test_bass_extension",
    "test_seat_level",
    "test_spatial",
)
PLAIN_TOPS = ("rust", "c", "deploy", "scripts", "docs")


def zone_of(path):
    base = os.path.basename(path)
    if path.startswith(TUNING_PKGS):
        return "tuning-product"
    if path.startswith("jasper/web/correction_") and path.endswith(".py"):
        return "tuning-product"
    if path.startswith("jasper/cli/") and path.endswith(".py"):
        if base.startswith("active_speaker") or base in TUNING_CLI:
            return "tuning-product"
    if path.startswith("tests/"):
        if base == "crossover_v2_fixtures.py":
            return "tuning-tests"
        if base.startswith(TUNING_TEST_PREFIXES):
            return "tuning-tests"
        return "tests"
    if path.startswith("deploy/assets/") or path.startswith("capture-page/"):
        return "web-assets"
    if path.startswith("jasper/"):
        return "product"
    for top in PLAIN_TOPS:
        if path.startswith(top + "/"):
            return top
    return "other"


def prose_rows(text):
    """Rows carrying only a comment or a standalone docstring, or None."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    comment, doc, code = set(), set(), set()
    skip = (tokenize.NL, tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER)
    starters = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    sig = []
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            comment.update(range(tok.start[0], tok.end[0] + 1))
        if tok.type not in skip:
            sig.append(tok)
    for i, tok in enumerate(sig):
        if tok.type == tokenize.STRING:
            before = sig[i - 1].type if i else tokenize.NEWLINE
            after = sig[i + 1].type if i + 1 < len(sig) else tokenize.NEWLINE
            if before in starters and after == tokenize.NEWLINE:
                doc.update(range(tok.start[0], tok.end[0] + 1))
                continue
        if tok.type in starters:
            continue
        code.update(range(tok.start[0], tok.end[0] + 1))
    return (comment | doc) - code


class Tally:
    fields = ("files", "lines", "code", "py_code", "prose")

    def __init__(self):
        for name in self.fields:
            setattr(self, name, 0)

    def add(self, lines, code, py_code, prose):
        self.files += 1
        self.lines += lines
        self.code += code
        self.py_code += py_code
        self.prose += prose

    def absorb(self, other):
        for name in self.fields:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def ratio(num, den):
    return "%.3f" % (num / den) if den else "-"


def count(value):
    return "{:,}".format(value)


def render(row, label, tally):
    print(
        row
        % (
            label,
            count(tally.files),
            count(tally.lines),
            count(tally.code),
            count(tally.py_code),
            count(tally.prose),
            ratio(tally.prose, tally.py_code),
        )
    )


def main():
    paths = [p for p in sys.stdin.buffer.read().split(b"\0") if p]
    tally = {name: Tally() for name in ZONES}
    binary = 0
    unreadable = 0
    untokenized = []

    for raw_path in sorted(paths):
        path = raw_path.decode("utf-8", "surrogateescape")
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            unreadable += 1
            continue
        zone = tally[zone_of(path)]
        try:
            text = None if b"\0" in raw else raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            binary += 1
            zone.add(0, 0, 0, 0)
            continue
        lines = text.splitlines()
        nonblank = {i for i, line in enumerate(lines, 1) if line.strip()}
        if not path.endswith((".py", ".pyi")):
            zone.add(len(lines), len(nonblank), 0, 0)
            continue
        rows = prose_rows(text)
        if rows is None:
            untokenized.append(path)
            zone.add(len(lines), len(nonblank), len(nonblank), 0)
            continue
        prose = len(rows & nonblank)
        zone.add(len(lines), len(nonblank) - prose, len(nonblank) - prose, prose)

    row = "%-15s %7s %10s %10s %10s %10s %11s"
    print("")
    print(row % ("zone", "files", "lines", "code", "py_code", "prose", "prose_ratio"))
    print("-" * 79)
    total = Tally()
    for name in ZONES:
        total.absorb(tally[name])
        render(row, name, tally[name])
    print("-" * 79)
    render(row, "TOTAL", total)
    print("")
    print("legend: lines = code + prose + blank, over git-tracked UTF-8 text files.")
    print("        prose = Python comments + standalone docstrings, blanks excluded.")
    print("        code  = every other non-blank line; py_code is its Python subset.")
    print("        prose_ratio = prose / py_code.")
    print("")
    print("binary or non-UTF-8 files (counted, lines not counted): %d" % binary)
    print("unreadable paths: %d" % unreadable)
    print("Python files tokenize refused (counted as code): %d" % len(untokenized))
    for path in sorted(untokenized)[:10]:
        print("    %s" % path)

    jasper_code = tally["product"].py_code + tally["tuning-product"].py_code
    jasper_prose = tally["product"].prose + tally["tuning-product"].prose
    test_lines = tally["tests"].lines + tally["tuning-tests"].lines
    product_lines = tally["product"].lines + tally["tuning-product"].lines

    head = "  %-44s %13s  %13s"
    print("")
    print("HEADLINE METRICS")
    print(head % ("", "this report", "audit \xa71"))
    print(head % ("total tracked lines", count(total.lines), "~1,420,000"))
    print(head % ("test lines (tests + tuning-tests)", count(test_lines), "~617,000"))
    print(head % ("product lines (all of jasper/)", count(product_lines), "~490,000"))
    print(head % ("jasper/ code lines", count(jasper_code), "274,456"))
    print(head % ("jasper/ prose lines", count(jasper_prose), "135,886"))
    print(head % ("jasper/ prose ratio", ratio(jasper_prose, jasper_code), "0.50"))

    pair = "  %-44s %13s"
    print("")
    print("TEST-TO-PRODUCT RATIOS (lines)")
    print(pair % ("whole repo", ratio(test_lines, product_lines)))
    print(
        pair
        % (
            "tuning program (tuning-tests/tuning-product)",
            ratio(tally["tuning-tests"].lines, tally["tuning-product"].lines),
        )
    )
    print(
        pair
        % (
            "platform (tests/product)",
            ratio(tally["tests"].lines, tally["product"].lines),
        )
    )


main()
PY

git ls-files -z | python3 "$tmpdir/census.py"

printf '\n'
printf 'DEAD CODE\n'
printf -- '---------\n'

# Python. A gauge, not a worklist: count plus the heaviest files only.
vulture=()
if [ -x "$repo_root/.venv/bin/python" ] &&
  "$repo_root/.venv/bin/python" -c 'import vulture' >/dev/null 2>&1; then
  vulture=("$repo_root/.venv/bin/python" -m vulture)
elif command -v uvx >/dev/null 2>&1 &&
  uvx --from vulture vulture --version >/dev/null 2>&1; then
  vulture=(uvx --from vulture vulture)
fi

if [ "${#vulture[@]}" -eq 0 ]; then
  printf 'vulture: SKIPPED (not importable from .venv and no working uvx)\n'
else
  rc=0
  "${vulture[@]}" --min-confidence 90 jasper >"$tmpdir/vulture.txt" \
    2>"$tmpdir/vulture.err" || rc=$?
  # 0 = clean, 3 = dead code found; anything else is a tool failure.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
    printf 'vulture: SKIPPED (exit %d)\n' "$rc"
    sed -n '1,3p' "$tmpdir/vulture.err" | sed 's/^/    /'
  else
    printf 'vulture: RAN (--min-confidence 90 over jasper/)\n'
    printf '    findings: %s\n' "$(grep -c 'confidence)$' "$tmpdir/vulture.txt" || true)"
    printf '    heaviest files:\n'
    awk -F: 'NF >= 2 { print $1 }' "$tmpdir/vulture.txt" |
      sort | uniq -c | sort -k1,1nr -k2,2 | head -10 |
      awk '{ printf "    %6d  %s\n", $1, $2 }'
  fi
fi

# Rust: the rustc dead_code lint family, per crate (no workspace root).
printf '\n'
if ! command -v cargo >/dev/null 2>&1; then
  printf 'cargo: SKIPPED (cargo not on PATH)\n'
else
  printf 'cargo: RAN (cargo check, dead-code lint family per crate)\n'
  export CARGO_TARGET_DIR="$repo_root/rust/target"
  while IFS= read -r manifest; do
    crate="$(dirname "$manifest")"
    rc=0
    cargo check --manifest-path "$manifest" --message-format=short \
      >"$tmpdir/cargo.txt" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
      printf '    %-22s check FAILED (exit %d)\n' "${crate#rust/}" "$rc"
      continue
    fi
    # `^[^.]` keeps this crate's own diagnostics: rustc reports a sibling
    # path dependency as ../<crate>/src/..., which the next crate's own run
    # would otherwise count a second time.
    loc='^[^.].*:[0-9]+:[0-9]+: warning: '
    dead=$(grep -cE "$loc.*never (read|used|constructed)" "$tmpdir/cargo.txt" || true)
    warn=$(grep -cE "$loc" "$tmpdir/cargo.txt" || true)
    printf '    %-22s dead-code: %-5s all warnings: %s\n' \
      "${crate#rust/}" "$dead" "$warn"
  done < <(git ls-files 'rust/*/Cargo.toml' | sort)
fi

printf '\n'
printf 'javascript: SKIPPED (no vendored analyzer; adding a node dependency for\n'
printf '            knip is out of scope for a report script)\n'
