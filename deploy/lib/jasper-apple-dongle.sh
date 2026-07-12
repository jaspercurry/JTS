#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Shared Apple USB-C dongle card resolution for the one-shot mixer pin and
# the long-running drift monitor. The sourcing script owns CONFIGURED_CARD.

APPLE_DONGLE_REGEX="usb-c to 3.5mm"

detect_apple_cards() {
    aplay -L 2>/dev/null \
        | grep -B1 -iE "$APPLE_DONGLE_REGEX" \
        | grep -oE 'CARD=[^,]+' \
        | sed 's/CARD=//' \
        || true
}

resolve_cards() {
    if [[ -n "$CONFIGURED_CARD" && "$CONFIGURED_CARD" != "auto" ]]; then
        printf '%s\n' "$CONFIGURED_CARD"
        return 0
    fi
    detect_apple_cards
}
