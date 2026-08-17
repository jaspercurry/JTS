#!/bin/bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Batch runner: N audible e0 runs back to back, each with a witness recording
# on the other microphone. Written for the 2026-07-22 honesty-guards session
# and kept as the worked example of an unattended series -- its mic names,
# placements, output dirs, and spacing are that session's, so read them as a
# template to edit, not as settings that fit the next series.
#
#   bash overnight_runs.sh smoke  [N]   # UMIK-2 merge-gate smoke (default 5)
#   bash overnight_runs.sh survey [N]   # iMM-6C survey + UMIK witness (default 12)
#
# Every audible run is gated on FLOOR_EPOCH: a run starting before that
# instant aborts the whole series. The shipped value is the 2026-07-22
# session's floor and is in the PAST, so the gate passes on every run today.
# Re-arm it for a quiet-hours series by exporting a future epoch:
#   FLOOR_EPOCH=$(date -j -f '%Y-%m-%d %H:%M' '2026-09-01 00:30' +%s) \
#     bash overnight_runs.sh smoke 5
set -euo pipefail

MODE="${1:?usage: overnight_runs.sh smoke|survey [count]}"
FLOOR_EPOCH="${FLOOR_EPOCH:-1784773000}"
# Self-locating, for the same reason e0_capture.py's WT is: this file used to
# hardcode an absolute path to the untracked captures/ working directory it
# was written in, which no longer drives anything.
DT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PY:-${DT}/../../.venv/bin/python}"
HOST="${HOST:-jts3.local}"

# Witnessing is symmetric (owner request, 22:20 check-in): whichever mic is
# NOT driving the pipeline witness-records the same sweeps for tomorrow's
# mic-vs-mic divergence study.
case "$MODE" in
  smoke)
    COUNT="${2:-5}"; MIC="UMIK-2"; WITNESS_DEV="iMM-6C"
    PLACEMENT="overnight_guard_smoke"
    OUTDIR="/private/tmp/xover-overnight"; SPACING_S=45 ;;
  survey)
    COUNT="${2:-12}"; MIC="iMM-6C"; WITNESS_DEV="UMIK-2"
    PLACEMENT="overnight_imm6c_survey"
    OUTDIR="/private/tmp/xover-overnight-survey"; SPACING_S=120 ;;
  *) echo "unknown mode: $MODE" >&2; exit 2 ;;
esac

mkdir -p "$OUTDIR"
echo "mode=$MODE count=$COUNT mic=$MIC placement=$PLACEMENT outdir=$OUTDIR"

for RUN in $(seq 1 "$COUNT"); do
  if [ "$(date +%s)" -ge "$FLOOR_EPOCH" ]; then echo "AUDIBLE_OK run=$RUN"; else
    echo "TOO_EARLY run=$RUN ($(date +%s) < FLOOR_EPOCH=$FLOOR_EPOCH) — aborting"; exit 3; fi

  RUN_T0="$(date '+%Y-%m-%d %H:%M:%S')"
  # Witness: independent recording on the non-pipeline mic spanning the whole
  # session. 420 s backstop duration; SIGINT after the session finalizes the
  # header.
  sox -q -t coreaudio "$WITNESS_DEV" -r 48000 -c 1 -b 16 \
    "$OUTDIR/witness_${WITNESS_DEV}_run${RUN}.wav" trim 0 420 &
  SOX_PID=$!
  sleep 1

  set +e
  "$PY" "$DT/e0_capture.py" --start-session --reset-first --host "$HOST" \
    --mic "$MIC" --placement "$PLACEMENT" --run "$RUN" --outdir "$OUTDIR" \
    --log "$OUTDIR/drive_run${RUN}.log"
  RC=$?
  set -e

  if [ -n "$SOX_PID" ]; then
    kill -INT "$SOX_PID" 2>/dev/null || true
    wait "$SOX_PID" 2>/dev/null || true
  fi
  echo "run=$RUN rc=$RC started='$RUN_T0' finished='$(date '+%Y-%m-%d %H:%M:%S')'"

  # Per-run diag pull — the numbers the morning tables are built from.
  ssh "pi@$HOST" "sudo journalctl -u jasper-correction-web --since '$RUN_T0' --no-pager" \
    | grep -E "check_diag|measure_diag|verify_diag|crossover_v2_result" \
    > "$OUTDIR/diag_run${RUN}.log" || true
  tail -3 "$OUTDIR/diag_run${RUN}.log" || true

  [ "$RUN" -lt "$COUNT" ] && sleep "$SPACING_S"
done
echo "ALL RUNS DONE mode=$MODE"
