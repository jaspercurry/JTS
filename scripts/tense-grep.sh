#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Advisory sweep for roadmap-dated comment/doc phrasing in the files
# changed on the current branch.
#
# A comment or doc line that describes a not-yet-landed state ("not
# yet wired", "until the reconciler lands", "today X happens", "no
# writer for Y yet") is a claim about a moment in time. Once a later
# PR lands the described change, the sentence goes stale silently —
# nobody's task is "notice this comment quietly became false." The
# 2026-08 wide-output-path program found a nine-site instance of
# exactly this shape (pre-flip comments describing a not-yet-landed
# state); commit 2787623bf trued up six of them. See AGENTS.md
# "Documentation paradigm" for the rule this script backs.
#
# This is advisory, not a CI gate: it always exits 0, and a match is
# a hypothesis to check by hand, not a confirmed defect — the pattern
# is deliberately loose (no allow-list) because false positives are
# cheap to skim past and false negatives are the failure mode that
# matters. Run it before publishing a PR.
#
# Scope is the merge base with origin/main (never a two-dot diff —
# origin/main moves under a branch in this repo, and a two-dot diff
# against a moving target renders phantom changes).
#
# Usage:
#   bash scripts/tense-grep.sh

set -uo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "${repo_root}" || exit 0

merge_base="$(git merge-base origin/main HEAD 2>/dev/null)" || {
    echo "tense-grep: no merge base with origin/main found — skipping" >&2
    exit 0
}

files=()
while IFS= read -r f; do
    [[ -n "${f}" && -f "${f}" ]] && files+=("${f}")
done < <(git diff --name-only --diff-filter=ACMR "${merge_base}" 2>/dev/null)

if (( ${#files[@]} == 0 )); then
    exit 0
fi

# Roadmap-dated phrasing: language that describes what does NOT exist
# yet, rather than what the code currently does. Case-insensitive.
# Loosely anchored on purpose — see the header comment above.
pattern='\bnot yet\b'
pattern+='|until (it|that|this) lands\b'
pattern+='|later step'
pattern+='|will (add|land)\b'
pattern+='|\btoday\b'
pattern+='|\bcurrently\b'
pattern+='|no writer'
pattern+='|nothing (writes|refuses)'

echo "tense-grep: advisory sweep of ${#files[@]} changed file(s) for roadmap-dated phrasing."
echo "Comments and docs must state what IS — every match below is a hypothesis that the"
echo "claim went stale once a later PR landed. Read each one; most will be fine."
echo

if grep -HinE "${pattern}" "${files[@]}" 2>/dev/null; then
    :
else
    echo "tense-grep: no matches."
fi

exit 0
