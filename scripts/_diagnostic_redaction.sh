# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# Shared redaction for diagnostics that may be copied off the Pi.
#
# Source from bash scripts before writing logs/config snapshots to disk:
#
#   . "$(dirname "$0")/_diagnostic_redaction.sh"
#   some_command | redact_jasper_diagnostics > out.txt
#
# Redacts current known credentials and future env vars that follow the
# project's secret naming convention: *_API_KEY, *_TOKEN, *_SECRET, *_PSK,
# *_PASSWORD, *_PASSPHRASE. `JASPER_MTA_BUSTIME_KEY` predates that
# convention, so it is listed explicitly.
#
# Pure sed on purpose: this is the only guard on the secret-compartment
# `cat` in fetch-pi-logs.sh and pi-bundle.sh, and it has to work when the
# Python venv is the broken thing being diagnosed. Its shapes are pinned
# side by side with jasper/secret_redaction.py's in
# tests/test_secret_redaction.py.

JASPER_SECRET_ENV_NAME_RE='([A-Za-z_][A-Za-z0-9_]*(_API_KEY|_TOKEN|_SECRET|_PSK|_PASSWORD|_PASSPHRASE)|JASPER_MTA_BUSTIME_KEY)'

# The one list of KEY=value config files a laptop/Pi bundle script may
# collect. Only KEY=value files belong here: this redactor keys on names
# (JASPER_SECRET_ENV_NAME_RE above), so a raw-value file with no such shape
# — e.g. /var/lib/jasper/control_token, .../household_secret — cannot be
# scrubbed by it and must never be added. fetch-pi-logs.sh (laptop) and
# pi-bundle.sh (Pi) both iterate this array so the two stay in lockstep
# with each other and with jasper/cli/doctor/secret_compartments.py's file
# inventory instead of drifting independently.
# shellcheck disable=SC2034  # consumed by the sourcing scripts, not here
JASPER_SECRET_ENV_FILES=(
    /etc/jasper/jasper.env
    /var/lib/jasper/voice_provider.env
    /var/lib/jasper-secrets/voice_keys.env
    /var/lib/jasper-secrets/google_credentials.env
    /var/lib/jasper-secrets/google_routes.env
    /var/lib/jasper-intsecrets/spotify_credentials.env
    /var/lib/jasper-intsecrets/home_assistant.env
    /var/lib/jasper/transit.env
    /var/lib/jasper/wifi_guardian.env
)

redact_jasper_diagnostics() {
    sed -E \
        -e "s/^([[:space:]]*${JASPER_SECRET_ENV_NAME_RE}[[:space:]]*[=:][[:space:]]*).*/\1<redacted>/" \
        -e "s/(Environment=|[[:space:]])\"(${JASPER_SECRET_ENV_NAME_RE})=[^\"]*\"/\1\"\2=<redacted>\"/g" \
        -e "s/(Environment=|[[:space:]])'(${JASPER_SECRET_ENV_NAME_RE})=[^']*'/\1'\2=<redacted>'/g" \
        -e "s/(Environment=|[[:space:]])(${JASPER_SECRET_ENV_NAME_RE})=\"[^\"]*\"/\1\2=<redacted>/g" \
        -e "s/(Environment=|[[:space:]])(${JASPER_SECRET_ENV_NAME_RE})='[^']*'/\1\2=<redacted>/g" \
        -e "s/(Environment=|[[:space:]])(${JASPER_SECRET_ENV_NAME_RE})=[^[:space:]]+/\1\2=<redacted>/g" \
        -e "s/([?&][Kk][Ee][Yy]=)[^&[:space:]\"'<>]+/\1<redacted>/g"
}
