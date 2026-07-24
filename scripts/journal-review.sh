#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Read-only journal-health digest, run ON the Pi. Summarizes the last
# `--since` window of the systemd journal into a human (or `--json`)
# report: journal disk usage + retention window, per-unit restart
# counts, warning+ volume by unit, the top `event=<domain.action>` keys
# with a week-over-week DELTA, never-seen-before keys (the generalized
# new-failure-mode detector), OOM/watchdog fingerprints, and repeated-
# message fingerprints.
#
# Usage:
#   sudo bash scripts/journal-review.sh                    # last 7 days, text
#   sudo bash scripts/journal-review.sh --since '24 hours ago'
#   sudo bash scripts/journal-review.sh --json             # machine-readable
#
# Contract (mirrors scripts/doc-freshness.sh's "informational, never a
# gate" posture):
#   - READ-ONLY. No config write, no restart, no systemctl mutation.
#   - ALWAYS exits 0 — this is a digest, not a health gate.
#   - The ONLY persistent write is its own state file
#     /var/lib/jasper/journal-review.state.json (atomic tempfile+rename),
#     read at start so the DELTA / never-seen columns are week-over-week.
#     A missing/garbled state file fails soft to an empty prior window —
#     every key then reads as "new" that run (loud, not silent).
#   - BOUNDED BY CONSTRUCTION for a 1 GB Pi: every journal read is a
#     WINDOWED `journalctl --since "$WINDOW"` (or `-b`/`-b -1` for the
#     boot-scoped OOM/watchdog fingerprints), streamed straight into awk
#     so only small aggregate maps live in memory — never an unbounded
#     full-journal scan, never the whole window materialized to disk.
#     Pure journalctl + awk/sort/grep; no python, no model loading.
#
# Reuses fetch-pi-logs.sh's `write_log_noise_summary()` awk fingerprinter
# and its previous-boot OOM/watchdog greps rather than re-implementing
# them (AGENTS.md anti-duplication rule); keep the two in sync.

set -u

# --- tunables (constants; the contract exposes only --since / --json) ---
STATE_FILE_DEFAULT="/var/lib/jasper/journal-review.state.json"
STATE_FILE="${JASPER_JOURNAL_REVIEW_STATE:-$STATE_FILE_DEFAULT}"
TOP_N="${JASPER_JOURNAL_REVIEW_TOP_N:-25}"   # event-key rows + fingerprint rows
SAMPLE_N=8                                    # OOM/watchdog sample lines per boot

SINCE="7 days ago"
JSON=0

usage() {
  awk '
    /^# SPDX-License-Identifier:/ { after_spdx = 1; next }
    !after_spdx { next }
    !in_docs { if ($0 ~ /^#/) in_docs = 1; else next }
    /^#/ { sub(/^# ?/, ""); print; next }
    { exit }
  ' "$0"
}

while (( $# )); do
  case "$1" in
    --since) SINCE="${2:-}"; shift 2 || { echo "--since needs a value" >&2; exit 0; } ;;
    --since=*) SINCE="${1#--since=}"; shift ;;
    --json) JSON=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 0 ;;
  esac
done

if ! command -v journalctl >/dev/null 2>&1; then
  if (( JSON )); then
    printf '{"schema":1,"error":"journalctl unavailable","window":"%s"}\n' "$SINCE"
  else
    echo "journal-review: journalctl not available on this host — nothing to review."
  fi
  exit 0
fi

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/journal-review.XXXXXX" 2>/dev/null)" || SCRATCH=""
if [[ -z "$SCRATCH" || ! -d "$SCRATCH" ]]; then
  echo "journal-review: could not create a scratch dir — aborting (no changes made)." >&2
  exit 0
fi
cleanup() { rm -rf "$SCRATCH"; }
trap cleanup EXIT

# Guaranteed-present aggregate files (touch so downstream readers never
# hit a missing path when a section produced no rows).
touch "$SCRATCH/prior_counts" "$SCRATCH/prior_seen" \
      "$SCRATCH/cur_events" "$SCRATCH/cur_restarts" "$SCRATCH/cur_fps" \
      "$SCRATCH/cur_warn" "$SCRATCH/events_table"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"

# --- JSON string escaper (awk lib, sourced into every json-emitting awk) ---
JESC_LIB='
function jesc(s,  r){ r=s;
  gsub(/\\/,"\\\\",r); gsub(/"/,"\\\"",r);
  gsub(/\t/,"\\t",r); gsub(/\r/,"\\r",r); gsub(/\n/,"\\n",r);
  return r }
'

# ---------------------------------------------------------------------------
# Read prior state (week-over-week baseline). Line-oriented parser tuned to
# THIS script's own one-key-per-line writer format (see write_state) — the only
# thing that ever writes this file. Any other/garbled structure yields an empty
# prior window, so every key reads as "new" — loud, never a crash. Keep the
# writer one-entry-per-line so this reader keeps round-tripping. Keys are
# constrained to a safe charset both here and when we write, so the two always
# agree and no escaping is needed.
# ---------------------------------------------------------------------------
if [[ -r "$STATE_FILE" ]]; then
  awk -v CFILE="$SCRATCH/prior_counts" -v SFILE="$SCRATCH/prior_seen" '
    /"counts"[[:space:]]*:[[:space:]]*\{/ { sec="counts"; next }
    /"seen"[[:space:]]*:[[:space:]]*\[/   { sec="seen";   next }
    sec=="counts" && /^[[:space:]]*\}/ { sec=""; next }
    sec=="seen"   && /^[[:space:]]*\]/ { sec=""; next }
    sec=="counts" {
      if (match($0, /"[A-Za-z0-9_.:-]+"/)) {
        k=substr($0,RSTART+1,RLENGTH-2); rest=substr($0,RSTART+RLENGTH)
        if (match(rest,/[0-9]+/)) print k "\t" substr(rest,RSTART,RLENGTH) > CFILE
      }
    }
    sec=="seen" {
      if (match($0, /"[A-Za-z0-9_.:-]+"/)) print substr($0,RSTART+1,RLENGTH-2) > SFILE
    }
  ' "$STATE_FILE" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# THE ONE HEAVY PASS: a single windowed, all-unit journal stream fed straight
# into one awk that simultaneously aggregates (a) event= keys, (b) auto-restart
# lines per unit, and (c) repeated-message fingerprints. Streaming keeps memory
# bounded to the small aggregate maps; combining the three avoids re-decompressing
# the window three times. The fingerprint() function is kept byte-for-byte in
# sync with fetch-pi-logs.sh's write_log_noise_summary() — pinned by
# tests/test_journal_review_fingerprint_contract.py.
# ---------------------------------------------------------------------------
journalctl --since "$SINCE" --no-pager -o short-iso 2>/dev/null \
  | awk -v EVENTS="$SCRATCH/cur_events" \
        -v RESTARTS="$SCRATCH/cur_restarts" \
        -v FPS="$SCRATCH/cur_fps" '
    function fingerprint(line) {
      sub(/^[0-9-]+T[0-9:.+-]+[[:space:]]+[^[:space:]]+[[:space:]]+/, "", line)
      gsub(/\[[0-9]+\]/, "[#]", line)
      gsub(/[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9:.+-]+/, "<ts>", line)
      gsub(/[0-9]+/, "#", line)
      gsub(/[[:space:]]+/, " ", line)
      return line
    }
    # Skip journalctl meta/hint lines ("-- No entries --", "-- Boot … --",
    # blanks): every real short-iso entry begins with its ISO timestamp digit.
    !/^[0-9]/ { next }
    {
      # (a) event=<domain.action> keys (safe charset; usually one per line)
      tmp = $0
      while (match(tmp, /event=[A-Za-z0-9_.:-]+/)) {
        ev[substr(tmp, RSTART+6, RLENGTH-6)]++
        tmp = substr(tmp, RSTART+RLENGTH)
      }
      # (b) systemd auto-restart lines: "<unit>.service: Scheduled restart job"
      if (index($0, "Scheduled restart job") > 0 &&
          match($0, /[A-Za-z0-9@:._-]+\.service: Scheduled restart job/)) {
        u = substr($0, RSTART, RLENGTH); sub(/\.service:.*/, "", u); restart[u]++
      }
      # (c) repeated-message fingerprints
      key = fingerprint($0); if (key != "") fp[key]++
    }
    END {
      for (k in ev)      print k "\t" ev[k]      > EVENTS
      for (u in restart) print u "\t" restart[u] > RESTARTS
      for (k in fp) if (fp[k] > 1) print fp[k] "\t" k > FPS
    }
  ' 2>/dev/null || true

# Warning+ volume, grouped by syslog identifier (≈ unit for our daemons).
# A separate, much smaller windowed pass (warning priority only).
journalctl --since "$SINCE" -p warning --no-pager -o short-iso 2>/dev/null \
  | awk -v OUT="$SCRATCH/cur_warn" '
      !/^[0-9]/ { next }   # skip journalctl meta lines ("-- No entries --" …)
      { id=$3; sub(/\[[0-9]+\]:?$/,"",id); sub(/:$/,"",id); if (id!="") c[id]++ }
      END { for (i in c) print c[i] "\t" i > OUT }
    ' 2>/dev/null || true

# Render the event delta / never-seen table (portable 3-file awk join:
# prior_counts, prior_seen, then this window's cur_events).
awk -F'\t' -v PC="$SCRATCH/prior_counts" -v PS="$SCRATCH/prior_seen" '
  FILENAME==PC { pc[$1]=$2; next }
  FILENAME==PS { seen[$1]=1; next }
  { key=$1; cnt=$2+0
    prior=(key in pc)?pc[key]+0:0
    isnew=(key in seen)?0:1
    print cnt "\t" key "\t" (cnt-prior) "\t" isnew }
' "$SCRATCH/prior_counts" "$SCRATCH/prior_seen" "$SCRATCH/cur_events" \
  > "$SCRATCH/events_table" 2>/dev/null || true

# --- metadata (cheap; no content scan) ---
DISK_USAGE="$(journalctl --disk-usage 2>/dev/null | tr -d '\n' || true)"
: "${DISK_USAGE:=unknown}"

# Oldest journal entry (µs epoch) via the metadata-only --list-boots, so we can
# flag a window that reaches further back than the journal actually retains.
OLDEST_US="$(journalctl --list-boots -o json 2>/dev/null \
  | grep -oE '"first_entry"[^0-9]*[0-9]+' | grep -oE '[0-9]+$' \
  | sort -n | head -1 || true)"
OLDEST_EPOCH=""; OLDEST_ISO=""
if [[ -n "$OLDEST_US" ]]; then
  OLDEST_EPOCH=$(( OLDEST_US / 1000000 ))
  OLDEST_ISO="$(date -u -d "@$OLDEST_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
fi
WINDOW_START_EPOCH="$(date -d "$SINCE" +%s 2>/dev/null || true)"
WINDOW_START_ISO=""
[[ -n "$WINDOW_START_EPOCH" ]] && \
  WINDOW_START_ISO="$(date -u -d "@$WINDOW_START_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
TRUNCATED=0
if [[ -n "$OLDEST_EPOCH" && -n "$WINDOW_START_EPOCH" && "$OLDEST_EPOCH" -gt "$WINDOW_START_EPOCH" ]]; then
  TRUNCATED=1
fi

# systemctl --failed (verbatim units), best-effort.
FAILED_RAW="$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | grep -F '.' || true)"

# OOM / watchdog fingerprints, boot-scoped (current + previous boot). Reuses the
# fetch-pi-logs.sh previous-boot grep vocabulary (kernel ring only, so it stays
# small): OOM, hung-task, and the "orphan cleanup on readonly fs" watchdog/unclean-
# reset signature.
OOM_PAT='out of memory|invoked oom-killer|killed process|oom-kill|hung task|blocked for more than|orphan cleanup on readonly fs|watchdog|kernel panic|segfault'
oom_scan() {  # $1 = boot selector (0 | -1); prints "count" then sample lines
  local boot="$1"
  journalctl -b "$boot" -k --no-pager -o short-iso 2>/dev/null \
    | grep -iE "$OOM_PAT" || true
}
OOM_CUR="$(oom_scan 0)"
OOM_PREV="$(oom_scan -1)"
OOM_CUR_N=0; [[ -n "$OOM_CUR" ]] && OOM_CUR_N="$(printf '%s\n' "$OOM_CUR" | grep -c . || true)"
OOM_PREV_N=0; [[ -n "$OOM_PREV" ]] && OOM_PREV_N="$(printf '%s\n' "$OOM_PREV" | grep -c . || true)"

# ---------------------------------------------------------------------------
# Persist this window's state ATOMICALLY (tempfile in the target dir + rename),
# BEFORE rendering so a render-time hiccup can't lose the baseline. counts =
# this window's key map (replaced each run); seen = monotone union of prior seen
# and current keys. Skips silently (still exits 0) if the dir is unwritable.
# ---------------------------------------------------------------------------
write_state() {
  local dir; dir="$(dirname "$STATE_FILE")"
  [[ -d "$dir" && -w "$dir" ]] || { echo "journal-review: $dir not writable — state not updated." >&2; return 0; }
  local tmp="${STATE_FILE}.tmp.$$"
  # seen = union(prior_seen, current keys)
  cut -f1 "$SCRATCH/cur_events" 2>/dev/null | cat - "$SCRATCH/prior_seen" 2>/dev/null \
    | sort -u > "$SCRATCH/new_seen" 2>/dev/null || true
  # Single-quoted awk body (no shell interpolation inside): values arrive via
  # -v, so keys/counts stay bare (safe-charset only) and the window string is
  # jesc()-escaped. Keys were constrained to a safe charset at extraction time,
  # so re-validating here is belt-and-suspenders, not the escaping guarantee.
  if awk -v WIN="$SINCE" -v WHEN="$NOW_ISO" \
         -v COUNTS="$SCRATCH/cur_events" -v SEEN="$SCRATCH/new_seen" \
         "$JESC_LIB"'
      BEGIN {
        print "{"
        print "  \"schema\": 1,"
        printf "  \"written_at\": \"%s\",\n", jesc(WHEN)
        printf "  \"window\": \"%s\",\n", jesc(WIN)
        print "  \"counts\": {"
        n=0
        while ((getline line < COUNTS) > 0) {
          split(line, a, "\t")
          if (a[1] ~ /^[A-Za-z0-9_.:-]+$/ && a[2] ~ /^[0-9]+$/) { key[++n]=a[1]; val[n]=a[2] }
        }
        for (i=1;i<=n;i++) printf "    \"%s\": %s%s\n", key[i], val[i], (i<n?",":"")
        print "  },"
        print "  \"seen\": ["
        m=0
        while ((getline s < SEEN) > 0) if (s ~ /^[A-Za-z0-9_.:-]+$/) sk[++m]=s
        for (i=1;i<=m;i++) printf "    \"%s\"%s\n", sk[i], (i<m?",":"")
        print "  ]"
        print "}"
      }' > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$STATE_FILE" 2>/dev/null || { rm -f "$tmp" 2>/dev/null; return 0; }
  else
    rm -f "$tmp" 2>/dev/null
  fi
  return 0
}
write_state

# ===========================================================================
# RENDER
# ===========================================================================
if (( JSON )); then
  # Assemble one JSON object from the aggregate files, escaping every
  # arbitrary-text field. Numbers/keys stay bare.
  {
    printf '{\n'
    printf '  "schema": 1,\n'
    awk "$JESC_LIB"'BEGIN{
      printf "  \"generated_at\": \"%s\",\n", jesc(ARGV[1])
      printf "  \"window\": \"%s\",\n", jesc(ARGV[2])
      printf "  \"window_start\": %s,\n", (ARGV[3]==""?"null":"\"" jesc(ARGV[3]) "\"")
      printf "  \"journal_oldest\": %s,\n", (ARGV[4]==""?"null":"\"" jesc(ARGV[4]) "\"")
      printf "  \"window_truncated\": %s,\n", (ARGV[5]=="1"?"true":"false")
      printf "  \"disk_usage\": \"%s\",\n", jesc(ARGV[6])
    }' "$NOW_ISO" "$SINCE" "$WINDOW_START_ISO" "$OLDEST_ISO" "$TRUNCATED" "$DISK_USAGE"

    # restarts {unit:count}
    printf '  "restarts": {'
    sort -t"$(printf '\t')" -k2,2nr "$SCRATCH/cur_restarts" 2>/dev/null \
      | awk -F'\t' "$JESC_LIB"'{ printf "%s\n    \"%s\": %d", (NR>1?",":""), jesc($1), $2 } END{ if(NR>0) printf "\n  " }'
    printf '},\n'

    # warn_by_unit {ident:count}
    printf '  "warn_by_unit": {'
    sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/cur_warn" 2>/dev/null \
      | awk -F'\t' "$JESC_LIB"'{ printf "%s\n    \"%s\": %d", (NR>1?",":""), jesc($2), $1 } END{ if(NR>0) printf "\n  " }'
    printf '},\n'

    # failed_units [ ... ]
    printf '  "failed_units": ['
    printf '%s' "$FAILED_RAW" | awk "$JESC_LIB"'NF{ printf "%s\n    \"%s\"", (n++>0?",":""), jesc($0) } END{ if(n>0) printf "\n  " }'
    printf '],\n'

    # events { top:[{key,count,delta,new}], never_seen:[...] }
    printf '  "events": {\n    "top": ['
    sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/events_table" 2>/dev/null | head -n "$TOP_N" \
      | awk -F'\t' "$JESC_LIB"'{ printf "%s\n      {\"key\": \"%s\", \"count\": %d, \"delta\": %d, \"new\": %s}", (NR>1?",":""), jesc($2), $1, $3, ($4=="1"?"true":"false") } END{ if(NR>0) printf "\n    " }'
    printf '],\n    "never_seen": ['
    awk -F'\t' '$4=="1"' "$SCRATCH/events_table" 2>/dev/null | sort -t"$(printf '\t')" -k1,1nr \
      | awk -F'\t' "$JESC_LIB"'{ printf "%s\n      \"%s\"", (NR>1?",":""), jesc($2) } END{ if(NR>0) printf "\n    " }'
    printf ']\n  },\n'

    # oom_watchdog
    printf '  "oom_watchdog": {"current_boot": %d, "previous_boot": %d},\n' "$OOM_CUR_N" "$OOM_PREV_N"

    # fingerprints [{count,sample}]
    printf '  "fingerprints": ['
    sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/cur_fps" 2>/dev/null | head -n "$TOP_N" \
      | awk -F'\t' "$JESC_LIB"'{ printf "%s\n    {\"count\": %d, \"sample\": \"%s\"}", (NR>1?",":""), $1, jesc($2) } END{ if(NR>0) printf "\n  " }'
    printf ']\n'
    printf '}\n'
  }
  exit 0
fi

# ---- human text report ----
hr() { printf '%s\n' '----------------------------------------------------------------------'; }

echo "JTS journal-health review"
echo "generated: $NOW_ISO   window: --since '$SINCE'"
hr

echo "1) Journal usage & retention window"
echo "   disk usage : $DISK_USAGE"
[[ -n "$WINDOW_START_ISO" ]] && echo "   window from: $WINDOW_START_ISO (resolved from '$SINCE')"
[[ -n "$OLDEST_ISO" ]] && echo "   journal from: $OLDEST_ISO (oldest retained entry)"
if (( TRUNCATED )); then
  echo "   NOTE: the journal only reaches back to $OLDEST_ISO — the requested window"
  echo "         is TRUNCATED (older entries were vacuumed at SystemMaxUse). Treat a"
  echo "         missing line as 'rolled off', not 'never happened'."
fi
echo "   boots:"
journalctl --list-boots --no-pager 2>/dev/null | sed 's/^/     /' || echo "     (unavailable)"
hr

echo "2) Auto-restart counts over the window (systemd 'Scheduled restart job')"
if [[ -s "$SCRATCH/cur_restarts" ]]; then
  sort -t"$(printf '\t')" -k2,2nr "$SCRATCH/cur_restarts" \
    | awk -F'\t' '{ printf "   %6d  %s\n", $2, $1 }'
else
  echo "   (no auto-restarts in the window — good)"
fi
echo
echo "   systemctl --failed:"
if [[ -n "$FAILED_RAW" ]]; then
  printf '%s\n' "$FAILED_RAW" | sed 's/^/     /'
else
  echo "     (none)"
fi
hr

echo "3) Warning+ line volume by unit/identifier"
if [[ -s "$SCRATCH/cur_warn" ]]; then
  sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/cur_warn" \
    | awk -F'\t' '{ printf "   %6d  %s\n", $1, $2 }'
else
  echo "   (no warning+ lines in the window)"
fi
hr

echo "4) Top event= keys (with week-over-week delta vs the prior run's window)"
if [[ -s "$SCRATCH/events_table" ]]; then
  printf '   %8s  %8s  %s\n' 'count' 'delta' 'event'
  sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/events_table" | head -n "$TOP_N" \
    | awk -F'\t' '{
        d = ($3>0 ? "+" $3 : $3 "")
        printf "   %8d  %8s  %s%s\n", $1, d, $2, ($4=="1" ? "  (new)" : "")
      }'
else
  echo "   (no event= keys seen in the window)"
fi
hr

echo "5) Never-seen-before event keys (present now, absent from the prior state)"
if [[ -s "$SCRATCH/events_table" ]]; then
  nseen="$(awk -F'\t' '$4=="1"' "$SCRATCH/events_table" | sort -t"$(printf '\t')" -k1,1nr)"
  if [[ -n "$nseen" ]]; then
    printf '%s\n' "$nseen" | awk -F'\t' '{ printf "   %8d  %s\n", $1, $2 }'
    echo "   (a brand-new key is the generalized new-failure-mode signal — triage first)"
  else
    echo "   (none — every event key was seen in a prior window)"
  fi
else
  echo "   (no event= keys to compare)"
fi
hr

echo "6) OOM / watchdog fingerprints (kernel ring, current & previous boot)"
echo "   current boot : $OOM_CUR_N match(es)"
if [[ -n "$OOM_CUR" ]]; then
  printf '%s\n' "$OOM_CUR" | head -n "$SAMPLE_N" | sed 's/^/     /'
fi
echo "   previous boot: $OOM_PREV_N match(es)"
if [[ -n "$OOM_PREV" ]]; then
  printf '%s\n' "$OOM_PREV" | head -n "$SAMPLE_N" | sed 's/^/     /'
fi
if [[ "$OOM_CUR_N" == "0" && "$OOM_PREV_N" == "0" ]]; then
  echo "   (no OOM/watchdog/unclean-reset signatures — good)"
fi
hr

echo "7) Top repeated-message fingerprints (timestamps/PIDs/numbers normalized)"
if [[ -s "$SCRATCH/cur_fps" ]]; then
  sort -t"$(printf '\t')" -k1,1nr "$SCRATCH/cur_fps" | head -n "$TOP_N" \
    | awk -F'\t' '{ printf "   %8d  %s\n", $1, $2 }'
else
  echo "   (no message repeated more than once in the window)"
fi
hr

echo "state: $STATE_FILE"
exit 0
