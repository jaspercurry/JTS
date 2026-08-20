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
#
# Pulls into <dest-dir>/:
#   bundle/<session>/...   the newest active-speaker session bundle (evidence
#                          packet's info.json + evidence/v1/artifacts/...)
#   state.json             the crossover-v2 flow state
#                          (/var/lib/jasper/active_speaker_crossover_v2_state.json)
#   design-draft.json      the active-speaker design draft
#                          (/var/lib/jasper/active_speaker_design_draft.json)
#   journal/<unit>.log     journal window for the units that speak during a
#                          round, plus journal/combined.log
#   power.txt              vcgencmd get_throttled + under-voltage grep counts
#   prediction.json         the round's own priors.predicted_sum /
#                          predicted_spec / fc_selection / etc., copied out of
#                          state.json before the next round overwrites them
#   dumps/wav/*.wav        dump-ring captures (XOVER_CAPTURE_DUMP_DIR),
#   dumps/sidecar/*.json   split by extension — root-owned on the Pi, so this
#                          step needs sudo; empty when the operator never
#                          created the ENABLED marker for this round
#
# Every pull is best-effort and independently reported — one failed pull
# (or an empty dump-ring because the operator never enabled it) does not
# stop the others. The ONLY gate is the last step: capture-integrity is
# checked over dumps/sidecar/, and this script's own exit code IS that
# check's exit code (0 clean / 1 nothing to check / 2 dirty — see
# jasper/audio_measurement/capture_integrity.py). A dirty or unreadable
# verdict never deletes anything that was pulled — every file stays on disk
# for forensics; the refusal is the exit code plus the printed findings.

set -uo pipefail

DEST="${1:?usage: bank-crossover-round.sh <dest-dir>}"
SINCE="${SINCE:-1 hour ago}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_diagnostic_redaction.sh"

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "$@"
}

mkdir -p "$DEST"
echo "Banking crossover-v2 round from ${PI_USER}@${PI_HOST} -> ${DEST}/" >&2

# --------------------------------------------------------------------- #
# 1. Evidence bundle — the newest session bundle by mtime, whole tree.
# --------------------------------------------------------------------- #
BUNDLE="$(remote "sudo ls -t /var/lib/jasper/active_speaker/sessions 2>/dev/null | head -1")"
if [[ -n "$BUNDLE" ]]; then
    mkdir -p "$DEST/bundle"
    if remote "sudo tar -C /var/lib/jasper/active_speaker/sessions -cf - '$BUNDLE'" \
            | tar -C "$DEST/bundle" -xf -; then
        echo "bundle -> $DEST/bundle/$BUNDLE" >&2
    else
        echo "bundle: FAILED to pull $BUNDLE" >&2
    fi
else
    echo "bundle: no session bundles found on the Pi" >&2
fi

# --------------------------------------------------------------------- #
# 2. Crossover-v2 flow state — per-claim verdicts live only here.
# --------------------------------------------------------------------- #
if remote "sudo cat /var/lib/jasper/active_speaker_crossover_v2_state.json 2>/dev/null" \
        > "$DEST/state.json" && [[ -s "$DEST/state.json" ]]; then
    echo "state -> $DEST/state.json ($(wc -c < "$DEST/state.json") bytes)" >&2
else
    rm -f "$DEST/state.json"
    echo "state: FAILED or not present" >&2
fi

# --------------------------------------------------------------------- #
# 3. Active-speaker design draft — the confirmed driver-safety profile.
# --------------------------------------------------------------------- #
if remote "sudo cat /var/lib/jasper/active_speaker_design_draft.json 2>/dev/null" \
        > "$DEST/design-draft.json" && [[ -s "$DEST/design-draft.json" ]]; then
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
mkdir -p "$DEST/journal"
for u in "${units[@]}"; do
    out="$DEST/journal/${u}.log"
    if remote "journalctl -u $u --since '$SINCE' --no-pager --output=short-iso 2>/dev/null" \
            | redact_jasper_diagnostics > "$out"; then
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
if remote "journalctl --since '$SINCE' --no-pager --output=short-iso ${combined_flags[*]} 2>/dev/null" \
        | redact_jasper_diagnostics > "$DEST/journal/combined.log"; then
    echo "  journal/combined.log: $(wc -l < "$DEST/journal/combined.log") lines" >&2
else
    echo "  journal/combined.log: failed" >&2
    rm -f "$DEST/journal/combined.log"
fi

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
echo "dumps -> $DEST/dumps (wav=$n_wav, sidecar=$n_sidecar)" >&2

# --------------------------------------------------------------------- #
# 7. Prediction snapshot — copied out of state.json before the next round
#    overwrites priors.predicted_sum / predicted_spec / fc_selection / etc.
#    Pure local read; no second Pi round-trip.
# --------------------------------------------------------------------- #
python3 - "$DEST/state.json" "$DEST/prediction.json" 1>&2 <<'PY'
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
# 8. The gate. Capture-integrity over the dump-ring sidecars is the ONLY
#    thing this script refuses a run on; every earlier pull above is
#    best-effort and already reported. Exit code is exactly the check's.
# --------------------------------------------------------------------- #
PYTHON="$(resolve_repo_python)"
echo "" >&2
echo "=== capture integrity (dumps/sidecar) ===" >&2
PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}" "$PYTHON" \
    -m jasper.audio_measurement.capture_integrity "$DEST/dumps/sidecar"
rc=$?

echo "" >&2
if (( rc == 0 )); then
    echo "bank-crossover-round: CLEAN -> $DEST" >&2
else
    echo "bank-crossover-round: REFUSED (exit $rc) -- every pulled file is kept under $DEST for forensics" >&2
fi
exit "$rc"
