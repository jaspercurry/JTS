#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# One-shot retirements for deploy/install.sh: the units and files earlier
# releases left on a box that nothing in the tree writes any more. One row per
# retired thing, applied by retire_leftovers().
#
# Row format: "<kind>|<targets>|<what it retires>", targets space-separated.
#   unit -> disable --now, stop, and reset-failed after the daemon-reload
#   file -> rm -f
# The table expands STATE_DIR and SYSTEMD_DIR when this file is SOURCED;
# install.sh sets both above its source block.
JASPER_RETIRED_LEFTOVERS=(
    # The removed endpoint tier served /sources/ from a standalone socket on
    # 8773, the port both profiles now serve from the combined jasper-web
    # bundle. Retire it before any jasper-web.socket enable; a
    # `systemctl disable --now` is never part of a unit-staging transaction.
    "unit|jasper-sources-web.socket jasper-sources-web.service|the standalone /sources/ endpoint tier"
    # The combo-health timer inferred capture failure from successful reopen
    # counters and could withdraw the entire UAC2 function. Its alternate
    # capture fallback no longer exists, so the destructive observer is retired
    # before the graph units are staged.
    "unit|jasper-fanin-combo-health.timer jasper-fanin-combo-health.service|the destructive USB combo-health watcher"
    "file|${SYSTEMD_DIR}/jasper-fanin-combo-health.timer ${SYSTEMD_DIR}/jasper-fanin-combo-health.service ${STATE_DIR}/usb_combo_fallback.json ${STATE_DIR}/combo_health_tick.json|the combo-health unit files and its persisted override state"
    # No backup, deliberately: nothing reads audio_topology.env for routing, so
    # a `.retired.*` copy would preserve ghost state under a name the doctor
    # does NOT warn about. jasper-doctor's check_fanin_asound_wiring WARNs on
    # the file's presence and names re-running the installer as the fix — this
    # row is the half that makes that sentence true.
    # REMOVAL CONDITION: that check drops its WARN AND no Pi still carries
    # /etc/asound.conf.dmix-mode-backup.
    "file|${STATE_DIR}/audio_topology.env /etc/asound.conf.dmix-mode-backup|the dmix/fanin topology switch state"
)

# Apply `$2...` (systemctl verb or rm) to every row of kind `$1`. Best-effort
# throughout: a fresh install carries none of these, and a box that never had
# one must not fail its deploy over it.
_retire_apply() {
    local want="$1" row kind targets
    shift
    for row in "${JASPER_RETIRED_LEFTOVERS[@]}"; do
        IFS='|' read -r kind targets _ <<<"${row}"
        [[ "${kind}" == "${want}" ]] || continue
        # shellcheck disable=SC2086  # targets is a deliberate word split
        "$@" ${targets} >/dev/null 2>&1 || true
    done
}

retire_leftovers() {
    _retire_apply unit systemctl disable --now
    _retire_apply unit systemctl stop
    _retire_apply file rm -f
    systemctl daemon-reload >/dev/null 2>&1 || true
    # A tick that raced the upgrade can leave a removed unit as a not-found
    # tombstone; that terminal state clears only once the reload has forgotten
    # the unit file, so reset-failed runs last.
    _retire_apply unit systemctl reset-failed
}
