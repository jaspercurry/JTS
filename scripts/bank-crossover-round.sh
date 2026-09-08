#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Bank a named session, or the newest when no session ID is supplied.
# Usage: bank-crossover-round.sh <dest-dir> [bundle-session-id]
# Optional state belongs to the captured round; other configuration is bank-time context.
# Exit 4: nonempty destination. Exit 3: bundle unavailable. Partial evidence is retained.

set -uo pipefail

DEST="${1:?usage: bank-crossover-round.sh <dest-dir> [bundle-session-id]}"
SINCE="${SINCE:-1 hour ago}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_lib.sh"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/_diagnostic_redaction.sh"

# N5: a bare `>` redirect per pulled file means re-running into an existing
# $DEST silently truncates whatever was banked there before. Refuse instead
# of guessing the operator meant to overwrite — pick a fresh directory, or
# remove the old one on purpose. Own exit code (4), never 1: a retry loop
# that just hit exit 1 from the capture-integrity check's "nothing to
# check" meaning and retries into the SAME $DEST must be able to tell that
# apart from "this destination is unusable" without parsing stderr.
if [[ -d "$DEST" ]] && [[ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 2>/dev/null)" ]]; then
    echo "bank-crossover-round: refusing -- $DEST already exists and is not empty (re-running into it would truncate the prior pull); remove it or pick a fresh directory" >&2
    exit 4
fi
mkdir -p "$DEST"

# B1: provenance manifest, written before any Pi round-trip so a banked
# tree always names its own source even if every pull below fails. The key set
# a banked round carries is owned by jasper/active_speaker/round_bank.py.
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
BUNDLE="${2:-$(remote "sudo ls -t /var/lib/jasper/active_speaker/sessions 2>/dev/null | head -1")}"
if [[ -n "$BUNDLE" && ! "$BUNDLE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "bundle: invalid session ID" >&2
    exit 3
fi
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

# Resolve the state after the bundle pull, so a later live state cannot label it.
remote "sudo cat /var/lib/jasper/active_speaker_crossover_v2_state.json 2>/dev/null" \
    > "$DEST/.current-state.json"
state_status="unavailable"
python_bin="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin=python3
if resolved="$(PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" - "$DEST" "$BUNDLE" <<'PYTHON'
import json
import shutil
import sys
from pathlib import Path
from jasper.active_speaker.crossover_v2.round_inputs import matching_state_path

destination = Path(sys.argv[1])
state, reason = matching_state_path(
    destination / "bundle" / sys.argv[2], destination / ".current-state.json",
)
if state is not None:
    shutil.copy2(state, destination / "state.json")
provenance = destination / "provenance.json"
record = json.loads(provenance.read_text())
record["missing"] = [] if state is not None else ["state.json"]
record["state_reason"] = reason
provenance.write_text(json.dumps(record, indent=2) + "\n")
print("ok" if state is not None else reason or "source_absent")
PYTHON
)"; then
    state_status="$resolved"
fi
rm -f "$DEST/.current-state.json"
echo "state: $state_status" >&2

# Pull one optional on-Pi artifact into $DEST/<name>; reported, never gated.
# Prints the status line the summary shows.
pull_optional() {  # <label> <remote-path> <local-name>
    local label="$1" src="$2" name="$3" bytes
    if remote "sudo cat $src 2>/dev/null" > "$DEST/$name" && [[ -s "$DEST/$name" ]]; then
        bytes="$(wc -c < "$DEST/$name")"
        echo "$label -> $DEST/$name ($bytes bytes)" >&2
        echo "ok ($bytes bytes)"
    else
        rm -f "$DEST/$name"
        echo "$label: FAILED or not present" >&2
        echo "FAILED or not present"
    fi
}

# --------------------------------------------------------------------- #
# 3. Active-speaker design draft — the confirmed driver-safety profile.
#    NOT part of the round's identity — reported, not gated.
# --------------------------------------------------------------------- #
design_draft_status="$(pull_optional design-draft /var/lib/jasper/active_speaker_design_draft.json design-draft.json)"

# --------------------------------------------------------------------- #
# 3b. Applied baseline profile — what the speaker is PLAYING. The flow
#     state above cannot answer that: its pre_apply_profile is the Undo
#     stash, one apply behind. NOT part of the round's identity —
#     reported, not gated.
# --------------------------------------------------------------------- #
applied_profile_status="$(pull_optional applied-profile /var/lib/jasper/active_speaker_baseline_profile.json applied-profile.json)"

# --------------------------------------------------------------------- #
# 3c. Banked repeat floor — the rig's measured touched-nothing repeat
#     spread, which the packet's in_capture_repeat_floor reads and derives
#     the stopping plateau/benefit margin from. NOT part of the round's
#     identity — reported, not gated.
# --------------------------------------------------------------------- #
repeat_floor_status="$(pull_optional repeat-floor /var/lib/jasper/active_speaker_repeat_floor.json repeat-floor.json)"

# --------------------------------------------------------------------- #
# 3d. Declared rig geometry — the household's own tape measure, the only
#     viable source for the room's entanglement floor on this rig class.
#     Frozen HERE because the packet is rebuilt by every reader: a round
#     read on another machine must report the room the SPEAKER declared,
#     not that machine's. NOT part of the round's identity — reported,
#     not gated.
# --------------------------------------------------------------------- #
declared_geometry_status="$(pull_optional declared-geometry /var/lib/jasper/measurement_geometry.json declared-geometry.json)"

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
# 5. Power diagnostics.
# --------------------------------------------------------------------- #
remote 'vcgencmd get_throttled 2>&1; \
    echo -n "dmesg under-voltage: "; sudo dmesg -T 2>/dev/null | grep -ci "under-voltage"; \
    echo -n "journal under-voltage: "; sudo journalctl -b 0 --no-pager 2>/dev/null | grep -ci "under-voltage"' \
    | tee "$DEST/power.txt" | sed 's/^/  power: /' >&2

# --------------------------------------------------------------------- #
# 6. Per-artifact summary. Missing state does not discard valid captures.
# --------------------------------------------------------------------- #
echo "" >&2
echo "=== artifact pull summary ===" >&2
echo "  bundle:          $bundle_status" >&2
echo "  state:           $state_status" >&2
echo "  design-draft:    $design_draft_status" >&2
echo "  applied-profile: $applied_profile_status" >&2
echo "  repeat-floor:    $repeat_floor_status" >&2
echo "  declared-geom:   $declared_geometry_status" >&2
echo "  journal:         $journal_status" >&2

if (( bundle_ok == 0 )); then
    echo "" >&2
    echo "bank-crossover-round: INCOMPLETE (exit 3) -- the round bundle could not be pulled. Every pulled file is kept under $DEST for forensics." >&2
    exit 3
fi

echo "" >&2
echo "bank-crossover-round: CLEAN -> $DEST" >&2
exit 0
