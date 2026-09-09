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
# The table expands STATE_DIR, SYSTEMD_DIR and CAMILLA_CONF when this file is
# SOURCED; install.sh sets all three above its source block.
: "${STATE_DIR:?}" "${SYSTEMD_DIR:?}" "${CAMILLA_CONF:?}"
JASPER_RETIRED_LEFTOVERS=(
    # The removed endpoint tier served /sources/ from a standalone socket on
    # 8773, the port both profiles now serve from the combined jasper-web
    # bundle. Retire it before any jasper-web.socket enable; a
    # `systemctl disable --now` is never part of a unit-staging transaction.
    # The file row is what lets the unit row terminate: nothing else at HEAD
    # removes the unit files a pre-collapse install left in SYSTEMD_DIR.
    # REMOVAL CONDITION: both rows drop once every box has taken one install
    # after this lands — the file row leaves the disable nothing to find.
    "unit|jasper-sources-web.socket jasper-sources-web.service|the standalone /sources/ endpoint tier"
    "file|${SYSTEMD_DIR}/jasper-sources-web.socket ${SYSTEMD_DIR}/jasper-sources-web.service|the standalone /sources/ unit files"
    # The combo-health timer inferred capture failure from successful reopen
    # counters and could withdraw the entire UAC2 function. Its alternate
    # capture fallback no longer exists, so the destructive observer is retired
    # before the graph units are staged.
    # REMOVAL CONDITION: both rows drop once every box has taken one install
    # after this lands — the file row removes the unit files, so nothing can
    # re-register the timer.
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
    # v1.yml is the pre-outputd rollback graph (issue #2240); install.sh stopped
    # seeding it, but a copy left by an older install is not inert — camillagui's
    # config picker scans /etc/camilladsp/*.yml, and the install-time statefile
    # guard reads it as a flat-allowed graph that writes to the removed
    # pcm.jasper_out dmix.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${CAMILLA_CONF}/v1.yml|the pre-outputd CamillaDSP rollback graph"
)

# Apply `$2...` (systemctl verb or rm) to every row of kind `$1`. Best-effort
# throughout: a fresh install carries none of these, and a box that never had
# one must not fail its deploy over it.
_retire_apply() {
    local want="$1" row kind targets
    local -a target_list
    shift
    for row in "${JASPER_RETIRED_LEFTOVERS[@]}"; do
        IFS='|' read -r kind targets _ <<<"${row}"
        [[ "${kind}" == "${want}" ]] || continue
        # read -ra, not a bare ${targets}: word-split the target list without
        # also glob-expanding it against the installer's cwd.
        read -ra target_list <<<"${targets}"
        "$@" "${target_list[@]}" >/dev/null 2>&1 || true
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
