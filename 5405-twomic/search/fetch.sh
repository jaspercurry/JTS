#!/usr/bin/env bash
# usage: fetch.sh <label> <ROUND>     -- copy one finished round off jts3 and analyse it.
# Run only when the round is finished (runner.log says "== done").
set -eu
LABEL="$1"; ROUND="$2"
SP=/private/tmp/claude-501/-Users-jaspercurry-Code-JTS--claude-worktrees-speaker-tuning-llm-arch-bb1ff5/f447b743-f8a9-4f10-bce1-5ed46c07c2ff/scratchpad/twomic
PI=pi@192.168.1.92
D="$SP/round-$ROUND"
mkdir -p "$D/side" "$SP/search/$LABEL"
ssh "$PI" "sudo tar -C /var/lib/jasper/active_speaker/campaigns -cf - $ROUND" < /dev/null | tar -C "$D" --strip-components=1 -xf -
ssh "$PI" "tar -C /home/pi/ab-5405/twomic/rounds/$LABEL -cf - ." < /dev/null | tar -C "$D/side" -xf -
cd "$SP"
PYTHONPATH=/Users/jaspercurry/Code/JTS/.claude/worktrees/deploy-jts3-cardioid \
/Users/jaspercurry/Code/JTS/.venv/bin/python twomic_analyse.py \
  --round-dir "$D" --journal "$D/side/journal-takes.txt" \
  --side-wav "$D/side/side-dayton.wav" \
  --side-start-epoch "$(cat "$D/side/side-start-epoch.txt")" \
  --side-cal "$SP/dayton-CMM31555.txt" \
  --main-cal "$SP/calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt" \
  --muted 0caaa048 --out "$SP/search/$LABEL/result.json"
