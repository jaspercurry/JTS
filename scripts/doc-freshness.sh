#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Report HANDOFF docs overdue for a freshness check.
#
# A doc is "stale" if its `Last verified: YYYY-MM-DD` footer (or, when
# the footer is absent, its most-recent git-commit date) is older than
# the threshold (default 90 days). Exit code is always 0 —
# informational, not a CI gate. Hook this into PR review as a
# reminder, not as enforcement.
#
# Usage:
#   bash scripts/doc-freshness.sh             # 90-day threshold, HANDOFFs only
#   bash scripts/doc-freshness.sh 60          # custom threshold
#   bash scripts/doc-freshness.sh 90 --all    # also include top-level + non-HANDOFF
#                                             # docs/, minus the archival trees
#                                             # (docs/research/, docs/historical/,
#                                             # docs/bass-extension-waves/,
#                                             # docs/correction-ux-wave3/)
#
# Output columns:
#   Date    last-verified (or last-touched) date
#   Days    days since that date
#   Source  "footer" if read from `Last verified:` line, "git" otherwise
#   Doc     repo-relative path

set -euo pipefail

usage() {
  awk '
    /^# SPDX-License-Identifier:/ { after_spdx = 1; next }
    !after_spdx { next }
    !in_docs {
      if ($0 ~ /^#/) in_docs = 1
      else next
    }
    /^#/ { sub(/^# ?/, ""); print; next }
    { exit }
  ' "$0"
}

# Help is valid in any position, including before the optional day threshold.
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
  esac
done

days=${1:-90}
shift || true
include_all=0
for arg in "$@"; do
  case "$arg" in
    --all) include_all=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"

# Cross-platform date math: BSD (macOS) vs GNU (Linux) `date`
epoch_days_ago() { date -v-"${1}"d +%s 2>/dev/null || date -d "${1} days ago" +%s; }
epoch_from_iso() { date -j -f "%Y-%m-%d" "$1" +%s 2>/dev/null || date -d "$1" +%s; }

threshold=$(epoch_days_ago "$days")
today=$(date +%s)
today_iso=$(date +%Y-%m-%d)

# Build doc list (portable to bash 3.2 — no mapfile)
docs=()
archival_excluded=0

# One classifier, both enumerations. An archival tree records WHEN something
# was learned, so the only remedy this report offers — "re-verify and bump the
# footer" — would falsify its provenance. A HANDOFF that has been archived is
# archival too, which is why this runs over the HANDOFF list as well.
classify_doc() {
  case "$1" in
    docs/research/*|docs/historical/*|docs/bass-extension-waves/*|docs/correction-ux-wave3/*)
      archival_excluded=$(( archival_excluded + 1 )) ;;
    *)
      docs+=("$1") ;;
  esac
}

while IFS= read -r d; do classify_doc "$d"; done \
  < <(find docs -maxdepth 2 -name 'HANDOFF-*.md' -type f 2>/dev/null | sort)
if (( include_all )); then
  while IFS= read -r d; do docs+=("$d"); done \
    < <(find . -maxdepth 1 -name '*.md' -type f 2>/dev/null | sed 's|^\./||' | sort)
  # No -maxdepth: nested non-HANDOFF docs were entirely invisible to this
  # report while this find was capped at depth 1. Being invisible, they could
  # never have tripped the threshold at ANY age; that is the defect, not their
  # age. `--all` already promises "top-level + non-HANDOFF docs/"; capping the
  # depth quietly broke that promise.
  #
  # Archival trees are pruned by `classify_doc` above (issue #2064, owner
  # ruling 2026-08-17). They are excluded by directory, not by per-file
  # opt-out, and the exclusion is PRINTED with its count: a silent prune would
  # let this report read as "everything was checked" when it was not.
  while IFS= read -r d; do classify_doc "$d"; done \
    < <(find docs -name '*.md' -type f ! -name 'HANDOFF-*.md' 2>/dev/null | sort)
fi

missing_footer_rows=()
stale_rows=()
fresh_count=0

for doc in "${docs[@]}"; do
  [[ -z "$doc" ]] && continue

  # 1. Try the `Last verified: YYYY-MM-DD` footer (take last match if multiple)
  verified=$(grep -hE '^Last verified: [0-9]{4}-[0-9]{2}-[0-9]{2}' "$doc" 2>/dev/null \
               | tail -1 | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 || true)
  source="footer"

  # 2. Fall back to most-recent git commit touching this doc
  if [[ -z "$verified" ]]; then
    verified=$(git log -1 --format='%cs' -- "$doc" 2>/dev/null || true)
    source="git"
    if [[ -n "$verified" && "$doc" == docs/HANDOFF-*.md ]]; then
      missing_footer_rows+=("${verified}|${doc}")
    fi
  fi

  [[ -z "$verified" ]] && continue  # untracked + no footer — skip silently

  doc_epoch=$(epoch_from_iso "$verified")
  age=$(( (today - doc_epoch) / 86400 ))

  if (( doc_epoch < threshold )); then
    stale_rows+=("${verified}|${age}|${source}|${doc}")
  else
    fresh_count=$(( fresh_count + 1 ))
  fi
done

stale_count=${#stale_rows[@]}
missing_footer_count=${#missing_footer_rows[@]}

printf 'HANDOFF docs missing a `Last verified:` footer:\n\n'
if (( missing_footer_count == 0 )); then
  printf '  (none)\n'
else
  printf '  %-12s  %s\n' 'Git date' 'Doc'
  printf '  %-12s  %s\n' '------------' '---'
  printf '%s\n' "${missing_footer_rows[@]}" | sort -t'|' -k1,1 | while IFS='|' read -r d doc; do
    printf '  %-12s  %s\n' "$d" "$doc"
  done
fi

printf '\n'

printf 'Docs not verified/touched in >%d days:\n\n' "$days"
if (( stale_count == 0 )); then
  # "all docs" would be the very over-claim the archival exclusion below is
  # printed to prevent: under `--all` some docs are deliberately not assessed.
  printf '  (none — all enumerated docs fresh)\n'
else
  printf '  %-12s %5s  %-6s  %s\n' 'Date' 'Days' 'Source' 'Doc'
  printf '  %-12s %5s  %-6s  %s\n' '------------' '-----' '------' '---'
  printf '%s\n' "${stale_rows[@]}" | sort -t'|' -k1,1 | while IFS='|' read -r d age src doc; do
    printf '  %-12s %5d  %-6s  %s\n' "$d" "$age" "$src" "$doc"
  done
fi

printf '\nSummary: %d missing footer, %d stale, %d fresh (threshold %d days).\n' \
  "$missing_footer_count" "$stale_count" "$fresh_count" "$days"
if (( archival_excluded > 0 )); then
  printf 'Excluded %d archival doc(s) under docs/research/, docs/historical/,\n' \
    "$archival_excluded"
  printf '  docs/bass-extension-waves/, and docs/correction-ux-wave3/:\n'
  printf '  a research bank or a spent delegation kit records when something was\n'
  printf '  learned, so bumping a freshness footer there would falsify its\n'
  printf '  provenance (issue #2064).\n'
fi
if (( stale_count > 0 )); then
  printf '\nAction: for each stale doc, re-read it against the current code and either\n'
  printf '  (a) bump the footer to `Last verified: %s`, or\n' "$today_iso"
  printf '  (b) update the content.\n'
fi
if (( missing_footer_count > 0 )); then
  printf '\nAction: add a final `Last verified: YYYY-MM-DD` footer to each missing\n'
  printf '  HANDOFF after checking whether it is operational, historical, or superseded.\n'
fi
