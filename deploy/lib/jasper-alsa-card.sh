#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared ALSA hint parser for install-time and hotplug-time hardware detection.

jasper_find_alsa_card() {
    # jasper_find_alsa_card <aplay-or-arecord-command> <descriptor-regex>
    local tool="$1" regex="$2"
    local card
    card="$(
        "$tool" -L 2>/dev/null \
            | grep -B1 -iE "$regex" \
            | grep -oE 'CARD=[^,]+' \
            | head -1 \
            | sed 's/CARD=//' \
            || true
    )"
    if [[ -n "$card" ]]; then
        printf '%s\n' "$card"
    fi
}
