#!/usr/bin/env bash
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
#   bash scripts/doc-freshness.sh 90 --all    # also include top-level + non-HANDOFF docs/
#
# Output columns:
#   Date    last-verified (or last-touched) date
#   Days    days since that date
#   Source  "footer" if read from `Last verified:` line, "git" otherwise
#   Doc     repo-relative path

set -euo pipefail

days=${1:-90}
shift || true
include_all=0
for arg in "$@"; do
  case "$arg" in
    --all) include_all=1 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \?//'; exit 0 ;;
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
while IFS= read -r d; do docs+=("$d"); done \
  < <(find docs -maxdepth 2 -name 'HANDOFF-*.md' -type f 2>/dev/null | sort)
if (( include_all )); then
  while IFS= read -r d; do docs+=("$d"); done \
    < <(find . -maxdepth 1 -name '*.md' -type f 2>/dev/null | sed 's|^\./||' | sort)
  while IFS= read -r d; do docs+=("$d"); done \
    < <(find docs -maxdepth 1 -name '*.md' -type f ! -name 'HANDOFF-*.md' 2>/dev/null | sort)
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
  printf '  (none — all docs fresh)\n'
else
  printf '  %-12s %5s  %-6s  %s\n' 'Date' 'Days' 'Source' 'Doc'
  printf '  %-12s %5s  %-6s  %s\n' '------------' '-----' '------' '---'
  printf '%s\n' "${stale_rows[@]}" | sort -t'|' -k1,1 | while IFS='|' read -r d age src doc; do
    printf '  %-12s %5d  %-6s  %s\n' "$d" "$age" "$src" "$doc"
  done
fi

printf '\nSummary: %d missing footer, %d stale, %d fresh (threshold %d days).\n' \
  "$missing_footer_count" "$stale_count" "$fresh_count" "$days"
if (( stale_count > 0 )); then
  printf '\nAction: for each stale doc, re-read it against the current code and either\n'
  printf '  (a) bump the footer to `Last verified: %s`, or\n' "$today_iso"
  printf '  (b) update the content. See AGENTS.md "Documentation paradigm".\n'
fi
if (( missing_footer_count > 0 )); then
  printf '\nAction: add a final `Last verified: YYYY-MM-DD` footer to each missing\n'
  printf '  HANDOFF after checking whether it is operational, historical, or superseded.\n'
fi
