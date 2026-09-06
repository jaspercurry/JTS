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
