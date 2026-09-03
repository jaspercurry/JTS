#!/usr/bin/env bash
# Dry-run integration: merge every claude/tuning-rightsize/* PR branch (except recon-reports)
# onto a fresh worktree of origin/main in dependency order; report conflicts; never push.
set -uo pipefail
cd /home/user/JTS
git fetch -q origin
W=/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/integration-wt
git worktree remove --force "$W" 2>/dev/null; git branch -D claude/tuning-rightsize/wave-1-integration 2>/dev/null
git worktree add -q -b claude/tuning-rightsize/wave-1-integration "$W" origin/main
cd "$W"
# order: docs → deletions → prose → tests (branches listed explicitly; add new ones at the right tier)
ORDER=(
  1-1a-doc-tier 1-1b-runbook-fixes 1-1d-adr-surfaced-rulings
  1-2c-am-dead 1-2d-correction-dead 1-2e-cli-dups 1-6-ghosts 1-2a-as-dead 1-2b-runtime-severance
  1-3-p1-flow 1-3-p2-spatial-plan-state 1-3-p3-packet-coord-refusal 1-3-p4-doors 1-3-p5-truth-layer
  1-3-p6-measure-half 1-3-p7-runtime-half 1-3-p8-verify-organs 1-3-p9a-records-rounds 1-3-p9b-capture-session
  1-3-p10-commissioning 1-3-p11-candidates-profile 1-3-p12a-audio-measurement 1-3-p12b-as-measurement-side 1-3-p12-substrate
  1-3-p13-cli-correction 1-3-p13a-cli 1-3-p13b-correction-attribution-calagent
  1-4-t1-source-pins 1-4-t2-heavy-tests
)
ok=(); missing=(); conflict=()
for s in "${ORDER[@]}"; do
  b="origin/claude/tuning-rightsize/$s"
  if ! git rev-parse -q --verify "$b" >/dev/null; then missing+=("$s"); continue; fi
  if git merge -q --no-edit "$b" >/dev/null 2>&1; then ok+=("$s");
  elif { git merge --abort; case "$s" in 1-3-*|1-4-*) strat=ours;; *) strat=theirs;; esac; git merge -q --no-edit -X $strat "$b" >/dev/null 2>&1; }; then ok+=("$s(-X $strat)");
  else conflict+=("$s: $(git diff --name-only --diff-filter=U | tr '\n' ' ')"); git merge --abort; fi
done
echo "MERGED (${#ok[@]}): ${ok[*]}"
echo "MISSING (${#missing[@]}): ${missing[*]}"
echo "CONFLICT (${#conflict[@]}):"; printf '  %s\n' "${conflict[@]}"
echo "--- integration stat vs origin/main ---"; git diff --shortstat origin/main
