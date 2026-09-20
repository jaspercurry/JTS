#!/usr/bin/env bash
# INFORMAL overnight driver (#5405). No model in the loop: a fixed plan of rounds, run one after another.
# Stop it:  touch /home/pi/ab-5405/twomic/STOP     (it ends after the running round)
set -u
D=/home/pi/ab-5405/twomic; PLAN="$D/plan.txt"; LOG="$D/overnight.log"
exec >>"$LOG" 2>&1
echo "$(date -Is) overnight start, $(grep -c . "$PLAN") rounds planned"
fails=0
while read -r LABEL POSES REPEATS CANDS; do
  [ -z "${LABEL:-}" ] && continue
  [ -f "$D/STOP" ] && { echo "$(date -Is) STOP file seen, ending"; break; }
  [ -d "$D/rounds/$LABEL" ] && { echo "$(date -Is) $LABEL already ran, skipping"; continue; }
  REPEATS="$REPEATS" "$D/run_round.sh" "$LABEL" "$CANDS" "$POSES"; rc=$?
  res=$(grep -E '"result"' "$D/rounds/$LABEL/wait.json" 2>/dev/null | tr -d ' ",' | head -1)
  echo "$(date -Is) $LABEL rc=$rc $res"
  case $rc in 10|11|12|14) echo "$(date -Is) hard stop rc=$rc"; break;; esac
  if [ $rc -ne 0 ] || ! echo "$res" | grep -q "complete"; then fails=$((fails+1)); else fails=0; fi
  [ $fails -ge 3 ] && { echo "$(date -Is) 3 rounds in a row did not complete, ending"; break; }
  sleep 20
done < "$PLAN"
echo "$(date -Is) overnight end"
