#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Flip the Gemini Live model the daemon talks to. Use when 3.1 Live
# Preview is silently failing on Google's side and you need a working
# backend, or to switch back once 3.1 unsticks.
#
# Both supported models use the Live API (bidi WebSocket + audio I/O), use
# the same SDK code path (client.aio.live.connect), and accept the same
# SpeechConfig voice list. Their actual model IDs come from the installed
# jasper.voice.catalog on the Pi; the aliases here are only the stable
# operator-facing selection surface. See:
# https://ai.google.dev/gemini-api/docs/live
#
# Usage:
#   bash scripts/switch-gemini-model.sh 3.1     # catalog's tested default
#   bash scripts/switch-gemini-model.sh 2.5     # catalog's fallback
#   bash scripts/switch-gemini-model.sh         # show current
#
# Defaults: PI_HOST/PI_USER come from .env.local when present, then
# PI_HOST falls back to JASPER_HOSTNAME and jts.local.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

SSH=(ssh -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}")
OPERATOR_ENV="/etc/jasper/jasper.env"
PROVIDER_ENV="/var/lib/jasper/voice_provider.env"
CATALOG_PY="/opt/jasper/.venv/bin/python"

ALIAS="${1:-}"

case "$ALIAS" in
    3.1|3) CATALOG_ALIAS="3.1" ;;
    2.5|2) CATALOG_ALIAS="2.5" ;;
    "")
        echo "Current model on ${PI_HOST}:"
        "${SSH[@]}" "sudo sh -c 'grep -h \"^JASPER_GEMINI_MODEL=\" \"${OPERATOR_ENV}\" \"${PROVIDER_ENV}\" 2>/dev/null | tail -1 || true'"
        echo
        echo "Usage:  bash scripts/switch-gemini-model.sh [3.1|2.5]"
        exit 0
        ;;
    *)
        echo "error: unknown model alias '$ALIAS'. Use '3.1' or '2.5'." >&2
        exit 2
        ;;
esac

# Resolve the alias against the catalog installed on the target Pi. Validate
# BOTH aliases every time so a partially drifted catalog cannot make one path
# look safe while the recovery path is broken. The catalog owns model IDs; this
# script owns only the two long-standing operator aliases and their roles.
resolve_catalog_model() {
    local requested_alias="$1"
    "${SSH[@]}" "sudo ${CATALOG_PY} - ${requested_alias}" <<'PY'
import re
import sys

from jasper.voice.catalog import ModelStatus, PROVIDERS


def fail(message):
    print(f"catalog error: {message}", file=sys.stderr)
    raise SystemExit(2)


gemini_entries = [provider for provider in PROVIDERS if provider.id == "gemini"]
if len(gemini_entries) != 1:
    fail(f"expected exactly one Gemini provider, found {len(gemini_entries)}")

gemini = gemini_entries[0]
if gemini.model_env != "JASPER_GEMINI_MODEL":
    fail(f"Gemini model env is {gemini.model_env!r}, expected JASPER_GEMINI_MODEL")

models = tuple(gemini.models)
if not models:
    fail("Gemini has no catalog models")
for model in models:
    if not isinstance(model.id, str) or not model.id:
        fail("Gemini model id must be a non-empty string")
    if any(char in model.id for char in "\t\r\n"):
        fail(f"Gemini model id contains a control delimiter: {model.id!r}")
    if not isinstance(model.label, str):
        fail(f"Gemini model {model.id!r} has a non-string label")
    if not isinstance(model.status, ModelStatus):
        fail(f"Gemini model {model.id!r} has malformed status {model.status!r}")
    if type(model.default) is not bool:
        fail(f"Gemini model {model.id!r} has malformed default {model.default!r}")

contracts = {
    "3.1": (ModelStatus.TESTED, True),
    "2.5": (ModelStatus.FALLBACK, False),
}
resolved = {}
for alias, (expected_status, expected_default) in contracts.items():
    pattern = re.compile(rf"(?<![0-9]){re.escape(alias)}(?![0-9])")
    matches = [
        model for model in models
        if pattern.search(model.id) or pattern.search(model.label)
    ]
    if len(matches) != 1:
        fail(f"alias {alias!r} matched {len(matches)} Gemini models")
    model = matches[0]
    if model.status is not expected_status or model.default is not expected_default:
        fail(
            f"alias {alias!r} has status={model.status.value!r} "
            f"default={model.default!r}; expected "
            f"status={expected_status.value!r} default={expected_default!r}"
        )
    resolved[alias] = model

defaults = [model for model in models if model.default]
if len(defaults) != 1 or defaults[0] is not resolved["3.1"]:
    fail("Gemini must have exactly one default, owned by alias 3.1")
if resolved["3.1"].id == resolved["2.5"].id:
    fail("Gemini aliases 3.1 and 2.5 resolve to the same model id")

requested = sys.argv[1]
if requested not in resolved:
    fail(f"unsupported requested alias {requested!r}")
print(resolved[requested].id)
PY
}

if ! MODEL="$(resolve_catalog_model "$CATALOG_ALIAS")"; then
    echo "error: could not resolve Gemini alias ${CATALOG_ALIAS} from the installed catalog on ${PI_HOST}." >&2
    echo "       Re-run deploy/install so ${CATALOG_PY} can import a valid jasper.voice.catalog." >&2
    exit 3
fi
if [[ -z "$MODEL" || "$MODEL" == *$'\n'* || ! "$MODEL" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "error: installed Gemini catalog returned an unsafe model id for alias ${CATALOG_ALIAS}." >&2
    exit 3
fi

echo "Switching ${PI_HOST}:JASPER_GEMINI_MODEL → ${MODEL}"
"${SSH[@]}" "sudo sh -s -- ${MODEL} ${OPERATOR_ENV} ${PROVIDER_ENV}" <<'REMOTE'
set -eu
model="$1"
operator_env="$2"
provider_env="$3"
proc_root="${4:-/proc}"
expected="JASPER_GEMINI_MODEL=${model}"

# The wizard-owned provider file is sourced after jasper.env, so it is the
# effective non-secret selector owner. Preserve every unrelated selector and
# replace this one atomically with the same root:jasper/0640 posture as the
# provider switcher and voice wizard.
state_dir="$(dirname "$provider_env")"
install -d -m 0770 "$state_dir"
chown root:jasper "$state_dir"
chmod 0770 "$state_dir"
tmp="$(mktemp "${provider_env}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
if [ -f "$provider_env" ]; then
    awk '!/^JASPER_GEMINI_MODEL=/' "$provider_env" > "$tmp"
fi
printf '%s\n' "$expected" >> "$tmp"
chown root:jasper "$tmp"
chmod 0640 "$tmp"
mv "$tmp" "$provider_env"
trap - EXIT

effective_file_value="$({
    grep -h '^JASPER_GEMINI_MODEL=' "$operator_env" "$provider_env" 2>/dev/null \
        || true
} | tail -1)"
if [ "$effective_file_value" != "$expected" ]; then
    echo "error: effective Gemini model file value is ${effective_file_value:-unset}, expected ${expected}" >&2
    exit 1
fi

systemctl restart jasper-voice
sleep 2
systemctl is-active jasper-voice
main_pid="$(systemctl show jasper-voice --property=MainPID --value)"
case "$main_pid" in
    ''|0|*[!0-9]*)
        echo "error: jasper-voice has no valid MainPID after restart" >&2
        exit 1
        ;;
esac
if ! tr '\0' '\n' < "${proc_root}/${main_pid}/environ" | grep -Fqx "$expected"; then
    echo "error: restarted jasper-voice does not have ${expected}" >&2
    exit 1
fi

printf '%s\n' "$expected"
journalctl -u jasper-voice -n 5 --no-pager 2>&1 \
    | grep -v -E 'GetGpuDevices|device_discovery' \
    | tail -5
REMOTE
