#!/bin/bash
# Verify code-token identity between origin/main and the working tree for the
# crossover_v2 modules named on the command line.
cd /home/user/JTS/.claude/worktrees/agent-a150b551e38ff3db2 || exit 1
mkdir -p .agenttmp/orig
for f in "$@"; do
  git show "origin/main:jasper/active_speaker/crossover_v2/$f.py" > ".agenttmp/orig/$f.py" || exit 1
  printf '%s: ' "$f"
  python3 .agenttmp/codeeq.py ".agenttmp/orig/$f.py" "jasper/active_speaker/crossover_v2/$f.py"
done
