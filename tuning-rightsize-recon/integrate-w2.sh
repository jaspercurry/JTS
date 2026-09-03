#!/usr/bin/env bash
# Dry-run integration: merge every claude/tuning-rightsize/* PR branch (except recon-reports)
# onto a fresh worktree of origin/main in dependency order; report conflicts; never push.
set -uo pipefail
cd /home/user/JTS
git fetch -q origin
W=/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/integration-wt
git worktree remove --force "$W" 2>/dev/null; git branch -D claude/tuning-rightsize/wave-2-integration 2>/dev/null
git worktree add -q -b claude/tuning-rightsize/wave-2-integration "$W" origin/main
cd "$W"
# order: docs → deletions → prose → tests (branches listed explicitly; add new ones at the right tier)
ORDER=(
  w2-retire-refactor-tuning w2-bass-plan-stays
  w2-preset-home w2-runtime-severance w2-doctor-handler-test w2-t1-low-findings w2-round-views-directivity
  w2-p-measurement-program w2-p-measurement-truth w2-p-as-fit-envelope w2-p-as-walk-capture
  ${EXTRA:-}
)
ok=(); missing=(); conflict=()
for s in "${ORDER[@]}"; do
  b="origin/claude/tuning-rightsize/$s"
  if ! git rev-parse -q --verify "$b" >/dev/null; then missing+=("$s"); continue; fi
  if git merge -q --no-edit "$b" >/dev/null 2>&1; then ok+=("$s");
  elif { git merge --abort; case "$s" in *-p-*|*-t1-*|w2-doctor-*) strat=ours;; *) strat=theirs;; esac; git merge -q --no-edit -X $strat "$b" >/dev/null 2>&1; }; then ok+=("$s(-X $strat)");
  else conflict+=("$s: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"); git merge --abort; fi
done
echo "MERGED (${#ok[@]}): ${ok[*]}"
echo "MISSING (${#missing[@]}): ${missing[*]}"
echo "CONFLICT (${#conflict[@]}):"; printf '  %s\n' "${conflict[@]}"
echo "--- integration stat vs origin/main ---"; git diff --shortstat origin/main
