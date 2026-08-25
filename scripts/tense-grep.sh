#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Advisory sweep for roadmap-dated comment/doc phrasing.
#
# A comment or doc line that describes a not-yet-landed state ("not
# yet wired", "until the reconciler lands", "today X happens", "no
# writer for Y yet") is a claim about a moment in time. Once a later
# PR lands the described change, the sentence goes stale silently —
# nobody's task is "notice this comment quietly became false." See
# the 2026-08 wide-output-path incident that
# motivated this script and the rule it backs — the story lives
# there, not here, so it can't drift out of sync between the two.
#
# This is advisory, not a CI gate: a normal run always exits 0, and a
# match is a hypothesis to check by hand, not a confirmed defect —
# the pattern is deliberately loose (no allow-list) because false
# positives are cheap to skim past and false negatives are the
# failure mode that matters.
#
# Two scopes, same pattern set:
#
#   bash scripts/tense-grep.sh          Files changed on this branch (the
#                                        default — run before publishing a
#                                        PR). Scope is the merge base with
#                                        origin/main (never a two-dot diff —
#                                        origin/main moves under a branch in
#                                        this repo, and a two-dot diff
#                                        against a moving target renders
#                                        phantom changes).
#
#   bash scripts/tense-grep.sh --all    Every repo-tracked file, grouped by
#                                        file with a per-file count. A
#                                        changed-files sweep can only
#                                        falsify prose in files ITS OWN diff
#                                        touches — a cutover PR that deletes
#                                        or supersedes a subsystem can leave
#                                        a now-false "nothing produces this
#                                        yet" sitting in a module the diff
#                                        never opens (#2325). Run this once
#                                        per deletion PR: capture a baseline
#                                        before the cut, run again after, and
#                                        diff the two by hand — the grouping
#                                        is what makes that diff readable.
#
# Usage:
#   bash scripts/tense-grep.sh
#   bash scripts/tense-grep.sh --all

set -uo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "${repo_root}" || exit 0

all_mode=0
for arg in "$@"; do
    case "${arg}" in
        --all) all_mode=1 ;;
        *)
            echo "tense-grep: unknown argument: ${arg}" >&2
            echo "usage: bash scripts/tense-grep.sh [--all]" >&2
            exit 2
            ;;
    esac
done

# Roadmap-dated phrasing: language that describes what does NOT exist
# yet, rather than what the code currently does. Case-insensitive.
# Loosely anchored on purpose — see the header comment above. Shared by
# both scopes so a full-repo sweep can never drift from what the
# default changed-files sweep looks for.
pattern='\bnot yet\b'
pattern+='|until (it|that|this) lands\b'
pattern+='|later step'
pattern+='|will (add|land)\b'
pattern+='|\btoday\b'
pattern+='|\bcurrently\b'
pattern+='|no writer'
pattern+='|nothing (writes|refuses)'

if (( all_mode )); then
    # core.quotePath=false so a non-ASCII path comes back literal instead
    # of C-quoted — a quoted path wouldn't match `-f "${f}"` below and
    # would silently drop out of scope.
    files=()
    while IFS= read -r f; do
        [[ -n "${f}" && -f "${f}" ]] && files+=("${f}")
    done < <(git -c core.quotePath=false ls-files 2>/dev/null)

    if (( ${#files[@]} == 0 )); then
        echo "tense-grep --all: 0 repo-tracked file(s) in scope."
        exit 0
    fi

    # `--` guards a dash-leading filename from being read as a grep
    # option (silent false negative otherwise: grep exits 2, no
    # output). `-I` skips binary files rather than printing a
    # content-free "Binary file … matches" line.
    matches="$(grep -HinIE "${pattern}" -- "${files[@]}")"

    if [[ -z "${matches}" ]]; then
        echo "tense-grep --all: no matches in ${#files[@]} repo-tracked file(s)."
        exit 0
    fi

    echo "tense-grep --all: advisory full-repo sweep of ${#files[@]} repo-tracked file(s) for roadmap-dated phrasing."
    echo "Same hypothesis-not-verdict contract as the default changed-files sweep — read each match by hand."
    echo "Grouped by file below, each header's (N) is that file's match count."
    echo

    # Group matches by file instead of the default mode's flat dump — a
    # full-repo match count runs an order of magnitude higher, and
    # "which file, how many" is the useful first read before drilling
    # into any one file's lines. This relies on grep emitting one
    # file's matches contiguously when given multiple file operands
    # (true of both GNU and BSD grep, and observable from the `-H`
    # prefix on every line), so a single pass suffices — no per-file
    # re-grep, no associative array (bash 3.2 on macOS has neither
    # `mapfile` nor `declare -A`; see the portability note in
    # doc-freshness.sh for the same constraint). `files[]` itself comes
    # from `git ls-files`, which is already path-sorted, so groups land
    # in the same order run to run — what makes a before/after baseline
    # diff meaningful (see the header comment's Phase-F use case).
    current_file=""
    current_count=0
    current_body=""
    file_count=0
    total=0

    flush_group() {
        if [[ -n "${current_file}" ]]; then
            printf '%s (%d)\n' "${current_file}" "${current_count}"
            printf '%s' "${current_body}"
            file_count=$(( file_count + 1 ))
        fi
    }

    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        f="${line%%:*}"
        rest="${line#*:}"
        if [[ "${f}" != "${current_file}" ]]; then
            flush_group
            current_file="${f}"
            current_count=0
            current_body=""
        fi
        current_count=$(( current_count + 1 ))
        current_body+="  ${rest}"$'\n'
        total=$(( total + 1 ))
    done <<< "${matches}"
    flush_group

    echo
    printf 'Total: %d match(es) in %d of %d repo-tracked file(s).\n' \
        "${total}" "${file_count}" "${#files[@]}"

    exit 0
fi

# Default scope: files changed on the current branch. Everything below
# is the original changed-files sweep, unchanged — a no-arg invocation
# must produce byte-identical output to before `--all` existed.
merge_base="$(git merge-base origin/main HEAD 2>/dev/null)" || {
    echo "tense-grep: no merge base with origin/main found — skipping" >&2
    exit 0
}

# core.quotePath=false so a non-ASCII path comes back literal instead of
# C-quoted — a quoted path wouldn't match `-f "${f}"` below and would
# silently drop out of scope.
files=()
while IFS= read -r f; do
    [[ -n "${f}" && -f "${f}" ]] && files+=("${f}")
done < <(git -c core.quotePath=false diff --name-only --diff-filter=ACMR "${merge_base}" 2>/dev/null)

if (( ${#files[@]} == 0 )); then
    echo "tense-grep: 0 changed files in scope."
    exit 0
fi

# `--` guards a dash-leading changed filename from being read as a grep
# option (silent false negative otherwise: grep exits 2, no output).
# `-I` skips binary files rather than printing a content-free "Binary
# file … matches" line.
matches="$(grep -HinIE "${pattern}" -- "${files[@]}")"

if [[ -n "${matches}" ]]; then
    echo "tense-grep: advisory sweep of ${#files[@]} changed file(s) for roadmap-dated phrasing."
    echo "Comments and docs must state what IS — every match below is a hypothesis that the"
    echo "claim went stale once a later PR landed. Read each one; most will be fine."
    echo
    echo "${matches}"
else
    echo "tense-grep: no matches in ${#files[@]} changed file(s)."
fi

exit 0
