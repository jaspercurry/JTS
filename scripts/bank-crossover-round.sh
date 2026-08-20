#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Bank one crossover-v2 round's evidence from the Pi into a directory YOU
# name, then gate the run on its own dump-ring capture-integrity counters.
#
# Every measurement campaign has re-invented this as throwaway shell in
# captures/<campaign>/tools/ — most recently night_bank.sh + pull_dumps.sh +
# integrity_summary.py in
# captures/linearization-night-2026-08-19/tools/. This is the product
# version: same pulls, same clean-run definition, but the destination is an
# argument (never a hardcoded campaign path) and the integrity check is a
# tested product module
# (jasper/audio_measurement/capture_integrity.py) instead of a one-off
# script.
#
# Usage:
#   bash scripts/bank-crossover-round.sh <dest-dir>
#   SINCE='2026-08-20 21:00:00' bash scripts/bank-crossover-round.sh <dest-dir>
#   PI_HOST=jts3.local bash scripts/bank-crossover-round.sh <dest-dir>
#   PYTHON=/path/to/python bash scripts/bank-crossover-round.sh <dest-dir>
#
# A caller-exported PI_HOST / PI_USER always wins over whatever
# .env.local sets — captured before _lib.sh sources .env.local (which can
# otherwise clobber it, see the comment at the top of the script body) and
# re-applied after. <dest-dir> must not already exist non-empty: re-running
# into a used directory is refused rather than silently truncating a prior
# pull.
#
# Pulls into <dest-dir>/:
#   provenance.json         the host/user actually banked, in UTC, plus this
#                           script's own commit — written FIRST, so a banked
#                           tree names its source even if every other pull
#                           below fails
#   bundle/<session>/...    the newest active-speaker session bundle (evidence
#                           packet's info.json + evidence/v1/artifacts/...)
#   state.json              the crossover-v2 flow state
#                           (/var/lib/jasper/active_speaker_crossover_v2_state.json)
#   design-draft.json       the active-speaker design draft
#                           (/var/lib/jasper/active_speaker_design_draft.json)
#   journal/<unit>.log      journal window for the units that speak during a
#                           round, plus journal/combined.log
#   power.txt               vcgencmd get_throttled + under-voltage grep counts
#   prediction.json         the round's own verify_priors.predicted_sum /
#                           verify_priors.predicted_spec / fc_selection / etc.,
#                           copied out of state.json before the next round
#                           overwrites them
#   dumps/wav/*.wav         dump-ring captures (XOVER_CAPTURE_DUMP_DIR),
#   dumps/sidecar/*.json    split by extension — root-owned on the Pi, so this
#                           step needs sudo; empty when the operator never
#                           created the ENABLED marker for this round
#
# Every pull above is best-effort and independently reported to stderr, and
# a per-artifact summary prints at the end regardless of outcome — no silent
# failure paths. Two things can make this script refuse the run, and neither
# ever deletes a file that was already pulled (the refusal is the exit code
# plus the printed findings, forensics stay on disk):
#
#   * the round's own identity — the session BUNDLE or the flow STATE —
#     failed to pull. A bank that can't say which round it banked is not a
#     bank: exit 3 ("incomplete"), regardless of what the capture-integrity
#     check below found.
#   * capture-integrity over dumps/sidecar/ found the round dirty (or found
#     nothing to check). This script's own exit code IS that check's exit
#     code in this case — 0 clean / 1 nothing to check / 2 dirty — see
#     jasper/audio_measurement/capture_integrity.py.

set -uo pipefail

DEST="${1:?usage: bank-crossover-round.sh <dest-dir>}"
SINCE="${SINCE:-1 hour ago}"

# B1: _lib.sh sources .env.local with `set -a` (plain assignments), which
# unconditionally overwrites an already-exported PI_HOST/PI_USER — the
# `${PI_HOST:-...}` fallback below it never gets a chance to prefer the
# caller's value, because by then PI_HOST is already non-empty (issue
# #2689, repo-wide; not fixed here). Capture the caller's explicit exports
# now, before sourcing can clobber them, and re-apply below.
_caller_pi_host="${PI_HOST:-}"
_caller_pi_user="${PI_USER:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"
[[ -n "$_caller_pi_host" ]] && PI_HOST="$_caller_pi_host"
[[ -n "$_caller_pi_user" ]] && PI_USER="$_caller_pi_user"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_diagnostic_redaction.sh"

PYTHON="$(resolve_repo_python)"

# N5: a bare `>` redirect per pulled file means re-running into an existing
# $DEST silently truncates whatever was banked there before. Refuse instead
# of guessing the operator meant to overwrite — pick a fresh directory, or
# remove the old one on purpose.
if [[ -d "$DEST" ]] && [[ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 2>/dev/null)" ]]; then
    echo "bank-crossover-round: refusing -- $DEST already exists and is not empty (re-running into it would truncate the prior pull); remove it or pick a fresh directory" >&2
    exit 1
fi
mkdir -p "$DEST"

# B1: provenance manifest, written before any Pi round-trip so a banked
# tree always names its own source even if every pull below fails.
UTC_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SCRIPT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [[ "$SCRIPT_SHA" != "unknown" ]] && ! git -C "$REPO_ROOT" diff-index --quiet HEAD -- 2>/dev/null; then
    SCRIPT_SHA="${SCRIPT_SHA}-dirty"
fi
cat > "$DEST/provenance.json" <<EOF
{
  "pi_host": "${PI_HOST}",
  "pi_user": "${PI_USER}",
  "banked_at_utc": "${UTC_NOW}",
  "script_commit": "${SCRIPT_SHA}"
}
EOF
echo "provenance -> $DEST/provenance.json (host=$PI_HOST user=$PI_USER banked_at=$UTC_NOW)" >&2

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "$@"
}

echo "Banking crossover-v2 round from ${PI_USER}@${PI_HOST} -> ${DEST}/" >&2

# --------------------------------------------------------------------- #
# 1. Evidence bundle — the newest session bundle by mtime, whole tree.
#    Gates exit 3 below: this IS the round's identity.
# --------------------------------------------------------------------- #
bundle_ok=0
bundle_status="no session bundles found on the Pi"
BUNDLE="$(remote "sudo ls -t /var/lib/jasper/active_speaker/sessions 2>/dev/null | head -1")"
if [[ -n "$BUNDLE" ]]; then
    mkdir -p "$DEST/bundle"
    if remote "sudo tar -C /var/lib/jasper/active_speaker/sessions -cf - '$BUNDLE'" \
            | tar -C "$DEST/bundle" -xf -; then
        bundle_ok=1
        bundle_status="ok ($BUNDLE)"
        echo "bundle -> $DEST/bundle/$BUNDLE" >&2
    else
        bundle_status="FAILED to pull $BUNDLE"
        echo "bundle: FAILED to pull $BUNDLE" >&2
    fi
else
    echo "bundle: no session bundles found on the Pi" >&2
fi

# --------------------------------------------------------------------- #
# 2. Crossover-v2 flow state — per-claim verdicts live only here.
#    Gates exit 3 below: this IS the round's identity.
# --------------------------------------------------------------------- #
state_ok=0
state_status="FAILED or not present"
if remote "sudo cat /var/lib/jasper/active_speaker_crossover_v2_state.json 2>/dev/null" \
        > "$DEST/state.json" && [[ -s "$DEST/state.json" ]]; then
    state_ok=1
    state_status="ok ($(wc -c < "$DEST/state.json") bytes)"
    echo "state -> $DEST/state.json ($(wc -c < "$DEST/state.json") bytes)" >&2
else
    rm -f "$DEST/state.json"
    echo "state: FAILED or not present" >&2
fi

# --------------------------------------------------------------------- #
# 3. Active-speaker design draft — the confirmed driver-safety profile.
#    NOT part of the round's identity — reported, not gated.
# --------------------------------------------------------------------- #
design_draft_status="FAILED or not present"
if remote "sudo cat /var/lib/jasper/active_speaker_design_draft.json 2>/dev/null" \
        > "$DEST/design-draft.json" && [[ -s "$DEST/design-draft.json" ]]; then
    design_draft_status="ok ($(wc -c < "$DEST/design-draft.json") bytes)"
    echo "design-draft -> $DEST/design-draft.json ($(wc -c < "$DEST/design-draft.json") bytes)" >&2
else
    rm -f "$DEST/design-draft.json"
    echo "design-draft: FAILED or not present" >&2
fi

# --------------------------------------------------------------------- #
# 4. Journal window — the units that speak during a crossover-v2 round.
#    Same per-unit + combined shape as fetch-pi-logs.sh, scoped to this
#    round's units instead of the whole install.
# --------------------------------------------------------------------- #
units=(jasper-correction-web jasper-control jasper-camilla jasper-outputd)
journal_ok_count=0
mkdir -p "$DEST/journal"
for u in "${units[@]}"; do
    out="$DEST/journal/${u}.log"
    if remote "journalctl -u $u --since '$SINCE' --no-pager --output=short-iso 2>/dev/null" \
            | redact_jasper_diagnostics > "$out"; then
        journal_ok_count=$((journal_ok_count + 1))
        echo "  journal/${u}.log: $(wc -l < "$out") lines" >&2
    else
        echo "  journal/${u}.log: failed" >&2
        rm -f "$out"
    fi
done
combined_flags=()
for u in "${units[@]}"; do
    combined_flags+=(-u "$u")
done
combined_ok="failed"
if remote "journalctl --since '$SINCE' --no-pager --output=short-iso ${combined_flags[*]} 2>/dev/null" \
        | redact_jasper_diagnostics > "$DEST/journal/combined.log"; then
    combined_ok="ok"
    echo "  journal/combined.log: $(wc -l < "$DEST/journal/combined.log") lines" >&2
else
    echo "  journal/combined.log: failed" >&2
    rm -f "$DEST/journal/combined.log"
fi
journal_status="${journal_ok_count}/${#units[@]} unit logs, combined=${combined_ok}"

# --------------------------------------------------------------------- #
# 5. Power re-check. Any sign of under-voltage VOIDS the attestation.
# --------------------------------------------------------------------- #
remote 'vcgencmd get_throttled 2>&1; \
    echo -n "dmesg under-voltage: "; sudo dmesg -T 2>/dev/null | grep -ci "under-voltage"; \
    echo -n "journal under-voltage: "; sudo journalctl -b 0 --no-pager 2>/dev/null | grep -ci "under-voltage"' \
    | tee "$DEST/power.txt" | sed 's/^/  power: /' >&2

# --------------------------------------------------------------------- #
# 6. Dump-ring captures — root-owned on the Pi, split WAV vs sidecar.
#    Empty when the operator never created the ENABLED marker for this
#    round; that is reported below by the integrity check, not here.
# --------------------------------------------------------------------- #
mkdir -p "$DEST/dumps/wav" "$DEST/dumps/sidecar"
if remote "sudo tar -C /var/lib/jasper/xover-capture-dump -cf - --exclude=ENABLED . 2>/dev/null" \
        | tar -C "$DEST/dumps" -xf - 2>/dev/null; then
    find "$DEST/dumps" -maxdepth 1 -name '*.wav' -exec mv {} "$DEST/dumps/wav/" \; 2>/dev/null
    find "$DEST/dumps" -maxdepth 1 -name '*.json' -exec mv {} "$DEST/dumps/sidecar/" \; 2>/dev/null
fi
n_wav=$(find "$DEST/dumps/wav" -name '*.wav' 2>/dev/null | wc -l | tr -d ' ')
n_sidecar=$(find "$DEST/dumps/sidecar" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
dumps_status="wav=$n_wav, sidecar=$n_sidecar"
echo "dumps -> $DEST/dumps ($dumps_status)" >&2

# --------------------------------------------------------------------- #
# 7. Prediction snapshot — copied out of state.json before the next round
#    overwrites verify_priors.predicted_sum / verify_priors.predicted_spec /
#    fc_selection / etc. Pure local read; no second Pi round-trip.
# --------------------------------------------------------------------- #
"$PYTHON" - "$DEST/state.json" "$DEST/prediction.json" 1>&2 <<'PY'
import json
import sys
from pathlib import Path

state_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])

PREDICTION_KEYS = (
    "predicted_sum", "predicted_spec", "expected_post_apply_offset_db",
    "commanded_delta", "gain_plan_db", "fc_selection", "candidate",
)


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            yield p, v
            yield from walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


if not state_path.exists() or state_path.stat().st_size == 0:
    print("prediction: skipped (no state.json pulled)")
    raise SystemExit(0)

state = json.loads(state_path.read_text())
found = {}
for path, value in walk(state):
    leaf = path.split(".")[-1].split("[")[0]
    if leaf in PREDICTION_KEYS:
        found[path] = value

out_path.write_text(json.dumps({
    "source_state": str(state_path),
    "prediction_paths_found": sorted(found.keys()),
    "prediction": found,
}, indent=1, sort_keys=True))
print(f"prediction -> {out_path} ({len(found)} paths found)")
if not found:
    print("  NOTE: no prediction fields found in state.json")
PY

# --------------------------------------------------------------------- #
# 8. Capture-integrity over the dump-ring sidecars. Its own 0/1/2 contract
#    is unaffected by anything below — the bank-level exit-3 override
#    (step 9) can still supersede it.
# --------------------------------------------------------------------- #
echo "" >&2
echo "=== capture integrity (dumps/sidecar) ===" >&2
PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}" "$PYTHON" \
    -m jasper.audio_measurement.capture_integrity "$DEST/dumps/sidecar"
checker_rc=$?

# --------------------------------------------------------------------- #
# 9. Per-artifact summary, then the final verdict. A bank that never
#    pulled its own round identity (bundle and/or flow state) is not a
#    bank, no matter how clean the dump-ring is — exit 3 overrides
#    whatever the checker said. Otherwise this script's exit code IS the
#    checker's.
# --------------------------------------------------------------------- #
echo "" >&2
echo "=== artifact pull summary ===" >&2
echo "  bundle:       $bundle_status" >&2
echo "  state:        $state_status" >&2
echo "  design-draft: $design_draft_status" >&2
echo "  journal:      $journal_status" >&2
echo "  dumps:        $dumps_status" >&2

if (( bundle_ok == 0 )) || (( state_ok == 0 )); then
    echo "" >&2
    echo "bank-crossover-round: INCOMPLETE (exit 3) -- the round bundle and/or flow state could not be pulled; a bank without its own round identity is not a bank. Every pulled file is kept under $DEST for forensics." >&2
    exit 3
fi

echo "" >&2
if (( checker_rc == 0 )); then
    echo "bank-crossover-round: CLEAN -> $DEST" >&2
else
    echo "bank-crossover-round: REFUSED (exit $checker_rc) -- every pulled file is kept under $DEST for forensics" >&2
fi
exit "$checker_rc"
