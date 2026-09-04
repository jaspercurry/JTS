#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# systemd unit install/enable steps for deploy/install.sh.
#
# Extracted from install.sh; functions assume install.sh globals and
# set -euo pipefail from the sourcing shell.

# Single canonical core-graph park list (JASPER_CORE_GRAPH_PARK_UNITS),
# shared with the runtime recovery handler deploy/bin/jasper-camilla-recover.
# Sourced REPO_DIR-relative from the rsync checkout (REPO_DIR is an assumed
# install.sh global). park_audio_clients_for_core_graph_restart() iterates it.
# shellcheck source=deploy/lib/jasper-core-graph-park-units.sh
source "${REPO_DIR}/deploy/lib/jasper-core-graph-park-units.sh"

WIZARD_UNITS=(
    jasper-web
    jasper-bluetooth-web
    jasper-correction-web
    jasper-system-web
    jasper-chat-web
)

install_jasper_support_files() {
    install -d -m 0755 /usr/local/lib/jasper /usr/local/sbin /usr/local/bin \
        "${SYSTEMD_DIR}"
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-asound-render.sh" \
        /usr/local/lib/jasper/jasper-asound-render.sh
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-alsa-card.sh" \
        /usr/local/lib/jasper/jasper-alsa-card.sh
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-env-file.sh" \
        /usr/local/lib/jasper/jasper-env-file.sh
    # Single canonical core-graph park list, sourced at runtime by
    # /usr/local/sbin/jasper-camilla-recover (../lib has no sibling there).
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-core-graph-park-units.sh" \
        /usr/local/lib/jasper/jasper-core-graph-park-units.sh
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-apple-dongle.sh" \
        /usr/local/lib/jasper/jasper-apple-dongle.sh
    install -d -m 0755 /usr/local/lib/jasper/install
    install -m 0644 \
        "${REPO_DIR}"/deploy/lib/install/*.sh \
        /usr/local/lib/jasper/install/
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-contained-build" \
        /usr/local/sbin/jasper-contained-build
}

# Core audio-graph unit + helper-binary install table. One row per file:
# "<mode> <src-relative-to-REPO_DIR> <dst>". Driven by a transactional loop
# (install_local_audio_graph_unit_files) so a single failed `install` cannot
# abort the sequence and silently skip a LATER unit — the 2026-06 deploy hazard
# where a newly-added unit never landed on the first deploy because an earlier
# step failed under `set -euo pipefail`. The loop attempts EVERY row, then
# fails at the end if any row failed (so a genuine error still surfaces), and a
# daemon-reload is guaranteed by the caller regardless.
#
# Annotations preserved from the prior flat form:
#   jasper-camilla-crossover.service — camilla#2 endpoint-crossover (:1235),
#     not globally boot-enabled; grouping reconcile arms it only while the box
#     is a bonded active leader.
#   jasper-doctor-json.service — WS1 Phase 3b-2 root oneshot capturing
#     jasper-doctor --json for /system/diagnostics (non-root jasper-control
#     triggers it via polkit). On-demand only — not enabled.
#   jasper-camilla-pipe-guard — ExecStartPre chain-breaker: re-points the
#     statefile off a dead Snapcast PLAYBACK pipe before camilla launches.
#   jasper-camilla-crossover-guard — like the pipe-guard but repairs ONLY to the
#     re-proven driver-domain graph (never flat — a flat crossover would send
#     full-range to the tweeter). Live only on a reconciled active leader.
JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS=(
    "0644 deploy/systemd/jasper-camilla.service ${SYSTEMD_DIR}/jasper-camilla.service"
    "0644 deploy/systemd/jasper-camilla-recover.service ${SYSTEMD_DIR}/jasper-camilla-recover.service"
    "0644 deploy/systemd/jasper-camilla-crossover.service ${SYSTEMD_DIR}/jasper-camilla-crossover.service"
    "0644 deploy/systemd/jasper-fanin.service ${SYSTEMD_DIR}/jasper-fanin.service"
    "0644 deploy/systemd/jasper-fanin-coupling-auto.service ${SYSTEMD_DIR}/jasper-fanin-coupling-auto.service"
    "0644 deploy/systemd/jasper-source-intent-reconcile.service ${SYSTEMD_DIR}/jasper-source-intent-reconcile.service"
    "0644 deploy/systemd/jasper-outputd.service ${SYSTEMD_DIR}/jasper-outputd.service"
    "0644 deploy/systemd/jasper-control.service ${SYSTEMD_DIR}/jasper-control.service"
    "0644 deploy/systemd/jasper-doctor-json.service ${SYSTEMD_DIR}/jasper-doctor-json.service"
    "0644 deploy/systemd/jasper-xvf-firmware-update.service ${SYSTEMD_DIR}/jasper-xvf-firmware-update.service"
    "0644 deploy/systemd/jasper-aec-commission.service ${SYSTEMD_DIR}/jasper-aec-commission.service"
    # The script lands before its unit: the unit's ExecCondition= names it,
    # and a systemd exec failure (203) is in the SKIP band, so a deploy
    # interrupted between these two rows would silently stop reconciling.
    "0755 deploy/bin/jasper-audio-hardware-reconcile /usr/local/sbin/jasper-audio-hardware-reconcile"
    "0644 deploy/systemd/jasper-audio-hardware-reconcile.service ${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
    "0755 deploy/bin/jasper-output-hardware-hotplug /usr/local/sbin/jasper-output-hardware-hotplug"
    "0755 deploy/bin/jasper-outputd-failure-reconcile /usr/local/sbin/jasper-outputd-failure-reconcile"
    "0755 deploy/bin/jasper-camilla-pipe-guard /usr/local/sbin/jasper-camilla-pipe-guard"
    "0755 deploy/bin/jasper-camilla-recover /usr/local/sbin/jasper-camilla-recover"
    "0755 deploy/bin/jasper-camilla-crossover-guard /usr/local/sbin/jasper-camilla-crossover-guard"
    "0755 deploy/bin/jasper-fanin-pitch-neutralize /usr/local/sbin/jasper-fanin-pitch-neutralize"
)

install_local_audio_graph_unit_files() {
    install -d -m 0755 /usr/local/lib/jasper /usr/local/sbin /usr/local/bin \
        "${SYSTEMD_DIR}"
    # The former combo-health timer inferred capture failure from successful
    # reopen counters and could withdraw the entire UAC2 function. Its alternate
    # capture fallback no longer exists, so upgrades retire the destructive
    # observer before staging the remaining graph units. Fresh installs no-op.
    systemctl disable --now jasper-fanin-combo-health.timer \
        >/dev/null 2>&1 || true
    systemctl stop jasper-fanin-combo-health.service \
        >/dev/null 2>&1 || true
    rm -f "${SYSTEMD_DIR}/jasper-fanin-combo-health.timer" \
          "${SYSTEMD_DIR}/jasper-fanin-combo-health.service"
    rm -f /var/lib/jasper/usb_combo_fallback.json \
          /var/lib/jasper/combo_health_tick.json 2>/dev/null || true
    # The guards below are a coupled runtime set. Do not continue to overwrite
    # either consumer when its required library could not be staged.
    if ! install -m 0644 \
            "${REPO_DIR}/deploy/lib/jasper-camilla-guard-common.sh" \
            /usr/local/lib/jasper/jasper-camilla-guard-common.sh; then
        echo "  ERROR: failed to install Camilla guard common library" >&2
        return 1
    fi
    # Transactional: attempt EVERY row even if one fails, so a newly-added unit
    # at the END of the table still lands on the first deploy. Failures are
    # collected and re-raised after the loop; the caller's daemon-reload runs
    # regardless. Each `install` is guarded with `|| failed=...` so `set -e`
    # from the sourcing shell cannot short-circuit the loop.
    local failed="" row mode src dst
    for row in "${JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS[@]}"; do
        read -r mode src dst <<<"${row}"
        if ! install -m "${mode}" "${REPO_DIR}/${src}" "${dst}"; then
            echo "  ERROR: failed to install ${dst} (from ${src})" >&2
            failed="${failed}${failed:+, }${dst}"
        fi
    done
    # Guaranteed daemon-reload: even if a row failed and `set -e` later aborts
    # the caller before its central daemon-reload, the units that DID land are
    # now known to systemd — so a newly-added unit takes effect on this deploy
    # rather than waiting for the next reboot. Best-effort (the caller reloads
    # again centrally; a transient reload miss here must not mask a row failure).
    systemctl daemon-reload 2>/dev/null || true
    # A tick that raced the upgrade can finish after the stop and leave the now
    # removed service as a not-found/failed tombstone. Clear that terminal state
    # only after daemon-reload has forgotten the old unit files.
    systemctl reset-failed jasper-fanin-combo-health.service \
        jasper-fanin-combo-health.timer >/dev/null 2>&1 || true
    if [[ -n "${failed}" ]]; then
        echo "  ERROR: core audio-graph unit install failed for: ${failed}" >&2
        return 1
    fi
}

_snapshot_full_unit_install_destination() {
    local destination="$1" seen
    if (( ${#install_transaction_paths[@]} > 0 )); then
        for seen in "${install_transaction_paths[@]}"; do
            if [[ "${seen}" == "${destination}" ]]; then
                return 0
            fi
        done
    fi
    if [[ -e "${destination}" || -L "${destination}" ]]; then
        command install -d -m 0700 \
            "${install_transaction_dir}/existing$(dirname "${destination}")"
        cp -a -- \
            "${destination}" \
            "${install_transaction_dir}/existing${destination}"
        install_transaction_paths+=("${destination}")
        install_transaction_existed+=(1)
    else
        install_transaction_paths+=("${destination}")
        install_transaction_existed+=(0)
    fi
}

_transactional_full_unit_install() {
    # Directory creation is harmless to retain on rollback. Every file
    # promotion is snapshotted exactly once before the ordinary atomic
    # `install` replacement, including installs performed by nested helpers.
    local arg destination
    for arg in "$@"; do
        if [[ "${arg}" == "-d" ]]; then
            command install "$@"
            return
        fi
    done
    destination="${!#}"
    _snapshot_full_unit_install_destination "${destination}"
    command install "$@"
}

_rollback_full_unit_install_transaction() {
    local index destination backup restore
    # A rollback failure must terminate normally, not recursively re-enter the
    # ERR trap with a half-consumed backup set.
    trap - ERR
    # Stop intercepting `install` before recovery so rollback cannot snapshot
    # its own restoration work.
    unset -f install
    for ((index=${#install_transaction_paths[@]} - 1; index >= 0; index--)); do
        destination="${install_transaction_paths[index]}"
        backup="${install_transaction_dir}/existing${destination}"
        if [[ "${install_transaction_existed[index]}" == "1" ]]; then
            restore="${destination}.jasper-rollback.$$"
            rm -f -- "${restore}"
            cp -a -- "${backup}" "${restore}"
            mv -f -- "${restore}" "${destination}"
        else
            rm -f -- "${destination}"
        fi
    done
    systemctl daemon-reload 2>/dev/null || true
    rm -rf -- "${install_transaction_dir:?}"
    echo "  ERROR: rolled back the incomplete full-profile unit generation" >&2
}

# Bluetooth accessory units, shared by BOTH install profiles. A paired remote
# is renderer-side: its buttons reach jasper-control, which runs on either
# profile, and its optional mic rides the accessory path (udp:9892) rather
# than the AEC bridge's local-array path. The AEC units below stay
# full-profile only, deliberately.
install_hid_accessory_unit_files() {
    # The WiiM mic adapter is a task inside jasper-input now (ADR-0225). An
    # upgraded box still has the old daemon running and its unit enabled, and
    # both producers would subscribe to the same GATT voice report and send to
    # the same UDP mic source. Retire it before staging the host. Fresh
    # installs no-op.
    systemctl disable --now jasper-wiim-remote-mic.service \
        >/dev/null 2>&1 || true
    rm -f "${SYSTEMD_DIR}/jasper-wiim-remote-mic.service" \
          "${SYSTEMD_DIR}/multi-user.target.wants/jasper-wiim-remote-mic.service"
    # jasper-input: every accessory bridge in one interpreter (ADR-0225).
    # Reads /dev/input/event* via python-evdev and translates known devices'
    # key events into HTTP calls against jasper-control; also runs the BLE mic
    # adapter task for whichever accessory mic the reconciler below publishes.
    # Always-on like jasper-mux — idle cost is negligible if no accessory is
    # attached. See jasper/accessories/.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-input.service" \
        "${SYSTEMD_DIR}/jasper-input.service"
    # Optional accessory mic profiles are activated by this root oneshot:
    # it reads BlueZ's paired-device state and writes
    # /var/lib/jasper/accessory-mics.env, which is both jasper-voice's source
    # list and jasper-input's instruction about which mic adapter to run. This
    # keeps rare remotes from imposing resident cost on every speaker.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-accessory-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-accessory-reconcile.service"
    # How every unprivileged requester (the Bluetooth wizard, the source-intent
    # coordinator) asks for a pass: touch a request file, no systemd privilege.
    # Without this watcher every request is silently dropped until reboot.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-accessory-reconcile.path" \
        "${SYSTEMD_DIR}/jasper-accessory-reconcile.path"
    # Root oneshot the adapter kicks (through jasper-control's restart broker)
    # on every reconnect: it reserves BLE connection-event length on the live
    # remote link. BlueZ hardcodes that to 0, which starves the mic to ~24% of
    # realtime on a Pi Zero 2 W. Never enabled; started on demand only.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wiim-remote-ce.service" \
        "${SYSTEMD_DIR}/jasper-wiim-remote-ce.service"
}


# Staged on streambox but never boot-enabled: jasper-accessory-reconcile starts
# and stops jasper-voice as a mic-bearing remote pairs and unpairs, and
# `systemctl start` on a unit whose file was never installed exits 1. The full
# profile calls this from inside its rollback transaction below, so `install`
# here resolves to the transactional wrapper.
# See docs/adr/0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md
install_voice_unit_files() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-voice.service" \
        "${SYSTEMD_DIR}/jasper-voice.service"
}


install_streambox_web_unit_files() {
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web-streambox.service" \
        "${SYSTEMD_DIR}/jasper-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web-streambox.socket" \
        "${SYSTEMD_DIR}/jasper-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.service" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.socket" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.service" \
        "${SYSTEMD_DIR}/jasper-correction-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.socket" \
        "${SYSTEMD_DIR}/jasper-correction-web.socket"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.service" \
        "${SYSTEMD_DIR}/jasper-system-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.socket" \
        "${SYSTEMD_DIR}/jasper-system-web.socket"
    # /chat/ is an ASSISTANT surface, so it ships wherever the assistant
    # wizards do. Same unit as the full tier: socket-activated, stdlib +
    # SQLite in the shared venv, idle-exits after 30 min.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-chat-web.service" \
        "${SYSTEMD_DIR}/jasper-chat-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-chat-web.socket" \
        "${SYSTEMD_DIR}/jasper-chat-web.socket"
}

# Renderer/DSP + assistant wizard ports; the assistant ones are bound
# whether or not the tier currently holds Capability.ASSISTANT, because a
# static socket cannot follow the grant table. Forbidden = the WAKE_DETECTION
# wizards, which a Zero-2-W-class board never runs. Kept in step with
# deploy/jasper-web-streambox.socket and nginx-jasper-streambox.conf by
# tests/test_web_main_imports.py.
validate_streambox_web_socket() {
    local socket="${SYSTEMD_DIR}/jasper-web.socket"
    local -a expected_ports=(
        8765 8767 8768 8771 8773 8775 8777 8778 8779 8783 8784 8785 8786
    )
    local -a forbidden_ports=(8774 8782)
    local port
    for port in "${expected_ports[@]}"; do
        if ! grep -q "^ListenStream=127\\.0\\.0\\.1:${port}$" "${socket}"; then
            echo "  ERROR: streambox jasper-web.socket missing port ${port}" >&2
            return 1
        fi
    done
    for port in "${forbidden_ports[@]}"; do
        if grep -q "^ListenStream=127\\.0\\.0\\.1:${port}$" "${socket}"; then
            echo "  ERROR: streambox jasper-web.socket still binds wake-side port ${port}" >&2
            return 1
        fi
    done
}

validate_streambox_systemd_units() {
    validate_streambox_web_socket || return 1
    if command -v systemd-analyze >/dev/null 2>&1; then
        local -a verify_units=(
            "${SYSTEMD_DIR}/jasper-control.service"
            "${SYSTEMD_DIR}/jasper-camilla.service"
            "${SYSTEMD_DIR}/jasper-camilla-recover.service"
            "${SYSTEMD_DIR}/jasper-camilla-crossover.service"
            "${SYSTEMD_DIR}/jasper-fanin.service"
            "${SYSTEMD_DIR}/jasper-outputd.service"
            "${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
            "${SYSTEMD_DIR}/jasper-snapclient.service"
            "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"
            "${SYSTEMD_DIR}/jasper-grouping-reconcile-trailing.service"
            "${SYSTEMD_DIR}/jasper-source-intent-reconcile.service"
            "${SYSTEMD_DIR}/jasper-fanin-coupling-auto.service"
            "${SYSTEMD_DIR}/jasper-web.service"
            "${SYSTEMD_DIR}/jasper-web.socket"
            "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
            "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
            "${SYSTEMD_DIR}/jasper-correction-web.service"
            "${SYSTEMD_DIR}/jasper-correction-web.socket"
            "${SYSTEMD_DIR}/jasper-system-web.service"
            "${SYSTEMD_DIR}/jasper-system-web.socket"
            "${SYSTEMD_DIR}/jasper-chat-web.service"
            "${SYSTEMD_DIR}/jasper-chat-web.socket"
            "${SYSTEMD_DIR}/librespot.service"
            "${SYSTEMD_DIR}/shairport-sync.service"
            "${SYSTEMD_DIR}/nqptp.service"
            "${SYSTEMD_DIR}/bt-agent.service"
            "${SYSTEMD_DIR}/jasper-mux.service"
            "${SYSTEMD_DIR}/jasper-usbgadget.service"
            "${SYSTEMD_DIR}/jasper-usbmic.service"
            "${SYSTEMD_DIR}/jasper-usbsink.service"
            "${SYSTEMD_DIR}/jasper-usbsink-volume.service"
            "${SYSTEMD_DIR}/jasper-usbnet-dhcp.service"
            "${SYSTEMD_DIR}/jts-audio.slice"
            "${SYSTEMD_DIR}/jasper-dongle-recover.service"
            "${SYSTEMD_DIR}/jasper-dac-init.service"
            "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
            "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
            "${SYSTEMD_DIR}/jasper-wifi-recover.service"
            "${SYSTEMD_DIR}/jasper-wifi-recover.timer"
            "${SYSTEMD_DIR}/jasper-wifi-scan-repair.service"
            "${SYSTEMD_DIR}/jasper-bootloop-guard.service"
            "${SYSTEMD_DIR}/jasper-identity-reconcile.service"
            "${SYSTEMD_DIR}/jasper-identity-reconcile.timer"
            "${SYSTEMD_DIR}/jasper-accessory-reconcile.path"
            "${SYSTEMD_DIR}/jasper-voice.service"
            "${SYSTEMD_DIR}/jasper-input.service"
            "${SYSTEMD_DIR}/jasper-accessory-reconcile.service"
            "${SYSTEMD_DIR}/jasper-wiim-remote-ce.service"
        )
        if [[ -x /usr/bin/snapserver ]]; then
            verify_units+=("${SYSTEMD_DIR}/jasper-snapserver.service")
        fi
        systemd-analyze verify "${verify_units[@]}" || {
            echo "  ERROR: streambox systemd units failed systemd-analyze verify" >&2
            return 1
        }
    fi
}

install_resilience_identity_unit_files() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-guardian.service" \
        "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.service" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.timer" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.timer"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-scan-repair.service" \
        "${SYSTEMD_DIR}/jasper-wifi-scan-repair.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-guardian" \
        /usr/local/sbin/jasper-wifi-guardian
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-recover" \
        /usr/local/sbin/jasper-wifi-recover
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.timer" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.timer"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-identity-reconcile" \
        /usr/local/sbin/jasper-identity-reconcile
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-bootloop-guard.service" \
        "${SYSTEMD_DIR}/jasper-bootloop-guard.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-bootloop-guard" \
        /usr/local/sbin/jasper-bootloop-guard
}

install_usbsink_unit_files() {
    # Root-owned, bounded USB incident artifacts. Creating it here makes the
    # opt-in sampler's narrow ReadWritePaths= contract valid even when the
    # gadget has never bound (and therefore never emitted a startup snapshot).
    install -d -m 0750 /var/lib/jasper/usb-gadget-incidents
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbgadget.service" \
        "${SYSTEMD_DIR}/jasper-usbgadget.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbgadget-forensics.service" \
        "${SYSTEMD_DIR}/jasper-usbgadget-forensics.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbgadget-forensics.path" \
        "${SYSTEMD_DIR}/jasper-usbgadget-forensics.path"
    # Kick-only (no [Install]): jasper-usbgadget's name-patch ExecStartPre
    # starts it with `systemctl --no-block` so the 10.3 s depmod runs in its
    # own cgroup instead of the gadget's 5 s start budget (#2176).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-name-index.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-name-index.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
        "${SYSTEMD_DIR}/jasper-usbsink.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-volume.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-volume.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbmic.service" \
        "${SYSTEMD_DIR}/jasper-usbmic.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbmic-apply.service" \
        "${SYSTEMD_DIR}/jasper-usbmic-apply.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbnet-dhcp.service" \
        "${SYSTEMD_DIR}/jasper-usbnet-dhcp.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usb-network-plan.service" \
        "${SYSTEMD_DIR}/jasper-usb-network-plan.service"
    install -d -m 0755 "${SYSTEMD_DIR}/NetworkManager.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/NetworkManager.service.d/jasper-usb-network-plan.conf" \
        "${SYSTEMD_DIR}/NetworkManager.service.d/jasper-usb-network-plan.conf"
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-up" \
        /usr/local/sbin/jasper-usbgadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-down" \
        /usr/local/sbin/jasper-usbgadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-snapshot" \
        /usr/local/sbin/jasper-usbgadget-snapshot
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-wanted" \
        /usr/local/sbin/jasper-usbgadget-wanted
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-converge" \
        /usr/local/sbin/jasper-usbgadget-converge
    # Sourced (not executed) by -wanted/-up/-converge, which find it beside
    # themselves — so it lives in sbin with them rather than /usr/local/lib.
    install -m 0644 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-compose.sh" \
        /usr/local/sbin/jasper-usbgadget-compose.sh
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbmic-apply-result" \
        /usr/local/sbin/jasper-usbmic-apply-result
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-wait-card" \
        /usr/local/sbin/jasper-usbsink-wait-card
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-name-patch" \
        /usr/local/sbin/jasper-usbsink-name-patch
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/uac2_name_patch.py" \
        /usr/local/sbin/uac2_name_patch.py
    install_usb_network_files
}

install_usb_network_files() {
    # One immutable-hardware-derived plan owns both the NetworkManager keyfile
    # and dnsmasq projection. A live interface on a different address means an
    # install may itself be riding that link: promote leaves BOTH old files
    # untouched for this run, and the boot gate applies the pair before NM and
    # the gadget next start. A same-install gadget recompose therefore returns
    # on its current address instead of severing the deploy mid-flight.
    install -d -m 0755 /etc/NetworkManager/system-connections
    install -d -m 0755 /etc/NetworkManager/conf.d
    install -m 0644 \
        "${REPO_DIR}/deploy/usb-network/90-jasper-usbnet.conf" \
        /etc/NetworkManager/conf.d/90-jasper-usbnet.conf
    local usb_network_state_dir="/var/lib/jasper-usb-network"
    install -d -o root -g root -m 0755 "${usb_network_state_dir}"
    local plan_python="${INSTALL_DIR:-/opt/jasper}/.venv/bin/python"
    if [[ ! -x "${plan_python}" ]]; then
        plan_python=python3
    fi
    local plan_path="${usb_network_state_dir}/plan.json"
    local pending_path="${usb_network_state_dir}/migration_pending"
    local nm_path="/etc/NetworkManager/system-connections/jts-usb.nmconnection"
    local dnsmasq_path="/etc/jasper/usbnet-dnsmasq.conf"
    # Full-profile installs already own a rollback domain for the gate unit and
    # NetworkManager drop-in. Include the two generated consumers before the
    # plan owner replaces either one, so any later catchable staging failure
    # restores the complete prior generation rather than old ungated units with
    # a partially promoted address pair. Streambox installs have no surrounding
    # transaction and converge directly.
    if declare -p install_transaction_paths >/dev/null 2>&1; then
        _snapshot_full_unit_install_destination "${nm_path}"
        _snapshot_full_unit_install_destination "${dnsmasq_path}"
    fi
    local plan_output
    if ! plan_output="$(PYTHONPATH="${REPO_DIR}" "${plan_python}" \
            -m jasper.usb_network converge \
            --plan "${plan_path}" \
            --nm "${nm_path}" \
            --dnsmasq "${dnsmasq_path}" \
            --pending "${pending_path}" 2>&1)"; then
        echo "  ERROR: USB network plan generation/promotion failed; refusing a fleet-wide fallback address. The USB gadget will remain boot-blocked while NetworkManager/Wi-Fi remain available: ${plan_output}" >&2
        return 1
    fi
    echo "  ${plan_output}"
    if [[ -e "${pending_path}" ]]; then
        # Deliberately skip every live NM/dnsmasq action. The files and their
        # running consumers remain one legacy generation until next boot.
        echo "  event=install.usb_network_migration_pending activation=next_boot live_files=preserved"
        return 0
    fi
    # Reload both policy and profile. On an upgrade usb0 may already exist, so
    # also make NM reconsider that device and activate the static profile once.
    # Bounds keep the install path finite. Load only JTS's keyfile: a global
    # connection reload invokes slow netplan regeneration and can take about
    # 20 seconds on a loaded Pi Zero 2. Future usb0 recreation is handled by
    # NM's normal autoconnect lifecycle with no poller or resident helper.
    # Failures remain non-fatal (nmcli is absent on non-Pi CI boxes and NM may
    # not be running yet), but are loud so a real speaker is diagnosable.
    if command -v nmcli >/dev/null 2>&1; then
        nmcli --wait 10 general reload conf >/dev/null 2>&1 || \
            echo "  WARN: NetworkManager did not reload USB network device policy"
        nmcli --wait 10 connection load \
            "${nm_path}" \
            >/dev/null 2>&1 || \
            echo "  WARN: NetworkManager did not reload jts-usb profile"
        if [[ -e /sys/class/net/usb0 ]]; then
            nmcli --wait 10 device set usb0 managed yes >/dev/null 2>&1 || \
                echo "  WARN: NetworkManager did not take ownership of usb0"
            if ! nmcli --wait 10 -t -f NAME,DEVICE connection show --active 2>/dev/null | \
                    grep -Fxq 'jts-usb:usb0'; then
                if nmcli --wait 10 connection up jts-usb ifname usb0 \
                        >/dev/null 2>&1; then
                    echo "  event=install.usb_network_converged profile=jts-usb interface=usb0"
                else
                    echo "  WARN: NetworkManager did not activate jts-usb on usb0; jasper-doctor reports the actionable state"
                fi
            fi
        fi
    fi
}

enable_usbgadget() {
    # The composite gadget is the FIRST gadget unit we enable — it carries the
    # default-on USB management network where the resolved transport permits.
    # `enable --now` arms it at boot; its ExecCondition skips cleanly when
    # hardware is stable host/unsupported or no UDC exists yet. During the
    # pending-host/current-peripheral migration it retains NCM so an install
    # running over USB can finish before reboot. jasper-usbnet-dhcp is
    # device-activated via its
    # [Install] WantedBy=sys-subsystem-net-devices-usb0.device, so `enable`
    # wires the pull without starting it until usb0 appears.
    #
    # Install expresses NO composition intent of its own. It MUST NOT interpret
    # canonical On or start the source here; the later reapply_source_intent
    # pass is the single owner of the ordered On transition (arm fan-in direct
    # capture, then advertise UAC2, then start the process-free readiness
    # marker).
    #
    # The old baseline parked jasper-usbsink first and then recomposed the
    # gadget "to network-only". That was itself a composition intent, and a
    # false one: it flipped the desired shape Off and the coordinator flipped it
    # back seconds later, which IS the deploy-time double bind that wedged the
    # dwc2 ISO data path in #3194. The upgrade/backcompat fence it stood for now
    # lives in the composer's own truth table, whose third gate refuses UAC2
    # unless live fan-in reports the DIRECT lane armed — so a stale endpoint
    # from an old release is withdrawn by the converge below, and a converged
    # one is left exactly where it is.
    #
    # The path watcher is always cheap/inert; the sampler starts only while the
    # persistent operator marker exists. try-restart picks up helper updates on
    # deploy without turning a disabled sampler on.
    systemctl enable jasper-usb-network-plan.service >/dev/null 2>&1 || {
        echo "  ERROR: could not enable USB network address-plan boot gate" >&2
        return 1
    }
    systemctl enable --now jasper-usbgadget-forensics.path >/dev/null 2>&1 || \
        echo "  WARN: could not arm opt-in USB gadget forensics watcher"
    systemctl try-restart jasper-usbgadget-forensics.service >/dev/null 2>&1 || true

    # NOTE the failure branch below only fires on a REAL failure (modprobe
    # failure, gadget-up exit >=255, masked unit): the ExecCondition
    # (jasper-usbgadget-wanted) skip is a condition-not-met outcome, which
    # `systemctl enable --now` reports as a SUCCESSFUL job (rc=0), so the
    # benign pre-reboot no-UDC case does NOT reach here. Point the operator at
    # the unit's journal rather than mislabeling every failure as "no UDC yet".
    # The relay is dependency-enabled (not independently boot-started). Its
    # ExecCondition keeps it inert unless the wizard intent + descriptor are
    # both On; the wants links let either gadget or AEC recovery bring it back.
    systemctl enable jasper-usbmic.service >/dev/null 2>&1 || \
        echo "  WARN: could not enable jasper-usbmic dependency links"
    systemctl enable --now jasper-usbgadget.service >/dev/null 2>&1 || \
        echo "  WARN: jasper-usbgadget failed to enable/compose — check 'systemctl status jasper-usbgadget' and 'journalctl -u jasper-usbgadget' (pre-reboot no-UDC is a clean skip, not this error)"
    systemctl enable jasper-usbnet-dhcp.service >/dev/null 2>&1 || true

    # One converge. It compares the live ConfigFS composition against the truth
    # table and does nothing at all when they already agree, so a deploy over a
    # converged link — NCM-only or a UAC2 endpoint with its live consumer —
    # performs zero ConfigFS writes and never re-enumerates the host. A real
    # difference costs exactly one teardown+rebuild plus the ordered
    # fan-in→usbmic refresh, both owned by the converger.
    #
    # Fail closed: a converge that could not reach the desired composition may
    # have left UAC2 advertised without its consumer, so it stops the install.
    "${JASPER_USBGADGET_CONVERGE:-/usr/local/sbin/jasper-usbgadget-converge}" install || {
        echo "  ERROR: jasper-usbgadget did not converge to the desired composition; refusing to continue with possibly stale UAC2 advertised. Check 'journalctl -u jasper-usbgadget'" >&2
        return 1
    }
}

install_grouping_unit_files() {
    local distro_unit
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapserver.service" \
        "${SYSTEMD_DIR}/jasper-snapserver.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapclient.service" \
        "${SYSTEMD_DIR}/jasper-snapclient.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile-trailing.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile-trailing.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-grouping-reconcile-trailing" \
        /usr/local/sbin/jasper-grouping-reconcile-trailing
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-grouping-reconcile-kick" \
        /usr/local/sbin/jasper-grouping-reconcile-kick

    for distro_unit in snapserver.service snapclient.service; do
        if systemctl list-unit-files "${distro_unit}" 2>/dev/null \
                | grep -q "^${distro_unit}"; then
            systemctl disable --now "${distro_unit}" >/dev/null 2>&1 || true
        fi
    done
}

install_renderer_source_unit_files() {
    if [[ -e "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf" ]]; then
        rm -f "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"
        rmdir "${SYSTEMD_DIR}/shairport-sync.service.d" 2>/dev/null || true
        echo "  removed stale shairport drop-in from a previous install"
    fi
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/librespot.service" \
        "${SYSTEMD_DIR}/librespot.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/shairport-sync.service" \
        "${SYSTEMD_DIR}/shairport-sync.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/nqptp.service" \
        "${SYSTEMD_DIR}/nqptp.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bt-agent.service" \
        "${SYSTEMD_DIR}/bt-agent.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-mux.service" \
        "${SYSTEMD_DIR}/jasper-mux.service"
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa-aplay.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-output.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-output.conf"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-restart.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-restart.conf"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-slice.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-slice.conf"
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa.service.d/jts-restart.conf" \
        "${SYSTEMD_DIR}/bluealsa.service.d/jts-restart.conf"
    install -d -m 0755 "${SYSTEMD_DIR}/bluetooth.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluetooth.service.d/jts-timeout.conf" \
        "${SYSTEMD_DIR}/bluetooth.service.d/jts-timeout.conf"
}

install_audio_output_recovery_unit_files() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-dongle-recover.service" \
        "${SYSTEMD_DIR}/jasper-dongle-recover.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-dac-init" \
        /usr/local/bin/jasper-dac-init
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-dac-init.service" \
        > "${SYSTEMD_DIR}/jasper-dac-init.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-dac-init.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-headphone-monitor" \
        /usr/local/bin/jasper-headphone-monitor
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-headphone-monitor.service" \
        > "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    install -d -m 0755 /etc/udev/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-apple-dongle.rules" \
        /etc/udev/rules.d/99-jasper-apple-dongle.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-audio-hardware-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-audio-hardware-reconcile.rules
    reload_audio_recovery_udev_rules_for_install
}

pin_attached_apple_dongle_power_control() {
    # Preserve the udev rule's autosuspend-off side effect for already-attached
    # Apple dongles without synthesizing a full USB/sound hotplug event during
    # deploy. The explicit output-hardware reconciler run below owns mixer
    # pinning and service restarts after the live graph has been parked.
    local device vendor product control
    for device in /sys/bus/usb/devices/*; do
        [[ -d "${device}" ]] || continue
        [[ -r "${device}/idVendor" && -r "${device}/idProduct" ]] || continue
        read -r vendor < "${device}/idVendor" || continue
        read -r product < "${device}/idProduct" || continue
        [[ "${vendor}" == "05ac" && "${product}" == "110a" ]] || continue
        control="${device}/power/control"
        [[ -w "${control}" ]] || continue
        printf 'on\n' 2>/dev/null > "${control}" || true
    done
}

reload_audio_recovery_udev_rules_for_install() {
    udevadm control --reload-rules
    pin_attached_apple_dongle_power_control
}

install_nginx_recovery_dropin() {
    # nginx is the package-owned management front door. This drop-in gives it
    # Restart=always + OOMScoreAdjust=-450 so a transient OOM cannot leave
    # http://<speaker>.local dark until someone SSHes in. Both install profiles
    # must call this: the doctor's installed-settings drift check expects -450 on nginx
    # regardless of profile, and without Restart=always an OOM-killed nginx
    # stays down.
    install -d -m 0755 "${SYSTEMD_DIR}/nginx.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/nginx.service.d/jts-recovery.conf" \
        "${SYSTEMD_DIR}/nginx.service.d/jts-recovery.conf"
}

install_streambox_audio_slices() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-audio.slice" \
        "${SYSTEMD_DIR}/jts-audio.slice"
    install -d -m 0755 "${SYSTEMD_DIR}/ssh.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/ssh.service.d/oom-protection.conf" \
        "${SYSTEMD_DIR}/ssh.service.d/oom-protection.conf"

    install_nginx_recovery_dropin
}

park_audio_clients_for_core_graph_restart() {
    # Deploy updates can rewrite asound/Camilla/outputd state while local
    # renderers are actively playing. Park the units that can hold fan-in,
    # Camilla, or outputd endpoints before restarting the core graph, then
    # let the existing restart/reconcile steps below restore the profile-
    # appropriate runtime state. The list is the single canonical
    # JASPER_CORE_GRAPH_PARK_UNITS sourced at the top of this file.
    local unit
    for unit in "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"; do
        systemctl stop "${unit}" 2>/dev/null || true
        systemctl reset-failed "${unit}" 2>/dev/null || true
    done
}

restart_core_camilla_after_dsp_reconcile() {
    # CamillaDSP captures the fan-in output. Restart it only after the DSP state
    # reconcile so a ring-default deploy cannot start Camilla on a stale
    # chunk-256 statefile against freshly-created 2-slot ring files.
    systemctl try-restart jasper-camilla.service 2>/dev/null || true
}

# The two always-on core-graph units the install path RESTARTS in place (never
# parked, because they ARE the graph being restarted). Both carry a
# StartLimitBurst guard; jasper-fanin escalates to StartLimitAction=reboot.
# JASPER_CORE_GRAPH_PARK_UNITS already reset-failed the PARKED clients (incl.
# outputd); these are the restart TARGETS it deliberately omits.
JASPER_CORE_GRAPH_RESTART_TARGETS=(
    jasper-fanin.service
    jasper-camilla.service
)

reset_failed_core_graph_restart_targets() {
    # Deploy-churn guard: a prior deploy can leave jasper-fanin (or camilla) in
    # a `failed` state with its StartLimit counter at/near the burst — e.g. a
    # transient EBUSY/config error during the previous install window. A bare
    # `systemctl restart` then immediately re-trips the burst, and
    # jasper-fanin's StartLimitAction=reboot would REBOOT THE PI mid-deploy.
    # reset-failed clears the failed state and the start-limit counter so the
    # restart below starts from a clean slate. Best-effort; never fatal.
    local unit
    for unit in "${JASPER_CORE_GRAPH_RESTART_TARGETS[@]}"; do
        systemctl reset-failed "${unit}" 2>/dev/null || true
    done
}

# Second phase of the low-memory build park: the core graph itself plus the
# control plane. Deliberately disjoint from JASPER_CORE_GRAPH_PARK_UNITS (phase
# one, the graph's CLIENTS) — an ordinary deploy restarts these in place rather
# than parking them, and only the constrained build window stops them outright.
JASPER_LOW_MEMORY_BUILD_PARK_UNITS=(
    jasper-fanin.service
    jasper-camilla.service
    jasper-camilla-crossover.service
    jasper-control.service
    jasper-system-web.service
    jasper-input.service
    jasper-accessory-reconcile.service
    jasper-aec-init.service
    jasper-aec-reconcile.service
    bt-agent.service
)

# Units this install actually STOPPED for the constrained build window, so an
# aborted install can put back exactly what it took away — no more (a unit the
# profile deliberately keeps parked must stay parked) and no less.
JASPER_LOW_MEMORY_PARK_RECORD=()

# The subset of the record that was ALREADY `disabled`/`masked` when it was
# parked. Restore skips a unit whose enablement CHANGED to off during this
# install, which is not the same question as whether it is off right now:
# several units run while permanently disabled because a reconciler starts
# them and systemd never does. See _unpark_one_low_memory_unit.
JASPER_LOW_MEMORY_PARK_OFF_AT_PARK=()

# Restore order after an aborted install: the units' own declared After= chain.
# jasper-camilla is After=jasper-outputd jasper-fanin; jasper-mux is
# After=jasper-fanin. Bringing the output owner and the graph back before the
# renderers that attach to them avoids handing a renderer a graph that has no
# sink yet. Everything not named here is restored afterwards, in park order.
# Entries that were not parked are simply absent from the record and skipped.
JASPER_LOW_MEMORY_UNPARK_FIRST=(
    jasper-outputd.service
    jasper-fanin.service
    jasper-camilla.service
    jasper-camilla-crossover.service
    jasper-mux.service
)

_jasper_unit_in_list() {
    # $1 = unit, $2.. = list to search. Linear scan over ~11 entries; bash 3.2
    # safe (no associative arrays — the test harness runs on macOS bash).
    local needle="$1" candidate
    shift
    for candidate in "$@"; do
        [[ "${candidate}" == "${needle}" ]] && return 0
    done
    return 1
}

_jasper_unit_was_off_at_park() {
    # $1 = unit. True when the unit was already `disabled`/`masked` at snapshot
    # time. The emptiness check is load-bearing, not defensive: this array is
    # empty on an ordinary install, and bash 3.2 under `set -u` treats
    # "${empty[@]}" as an unbound variable (the test harness runs on macOS
    # bash; verified 3.2.57 errors, 5.2.37 does not).
    (( ${#JASPER_LOW_MEMORY_PARK_OFF_AT_PARK[@]} )) || return 1
    _jasper_unit_in_list "$1" "${JASPER_LOW_MEMORY_PARK_OFF_AT_PARK[@]}"
}

_record_low_memory_parked_unit() {
    # $1 = unit the park is about to stop. Records it only if it is RUNNING,
    # together with whether its enablement was already off, so restore can tell
    # "this install turned it off" from "it was always off and something other
    # than systemd runs it".
    local unit="$1" enablement
    systemctl is-active --quiet "${unit}" 2>/dev/null || return 0
    JASPER_LOW_MEMORY_PARK_RECORD+=("${unit}")
    enablement="$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
    # `masked-runtime` is systemd's documented transient counterpart to
    # `masked` (a /run rather than /etc mask) and is just as much an off
    # state. Kept in step with the identical case in
    # _unpark_one_low_memory_unit: if one site calls a state off and the
    # other does not, a unit that was ALWAYS off reads as one this install
    # turned off, and the restore skips it as deliberate instead of
    # reporting a unit it could not put back. No in-repo path produces the
    # state today, so this is vocabulary completeness, not a fixed defect.
    case "${enablement}" in
        disabled|masked|masked-runtime)
            JASPER_LOW_MEMORY_PARK_OFF_AT_PARK+=("${unit}")
            ;;
    esac
    return 0
}

park_low_memory_build_units() {
    build_swap_required || return 0
    _build_sandbox_log "low_memory_build_park" \
        "stopping runtime units before constrained install/build steps"
    JASPER_LOW_MEMORY_PARK_RECORD=()
    JASPER_LOW_MEMORY_PARK_OFF_AT_PARK=()
    local unit
    # Snapshot BOTH phases before anything is stopped. Recording after the
    # phase-one park would miss every renderer plus the output owner and mux —
    # precisely the units whose absence the household notices ("it's gone from
    # my AirPlay list"), while the control plane came back and made the box
    # look healthy.
    #
    # Record only what was RUNNING. Restoring a unit that was already stopped
    # would start something this box had deliberately off.
    for unit in "${JASPER_CORE_GRAPH_PARK_UNITS[@]}"; do
        _record_low_memory_parked_unit "${unit}"
    done
    for unit in "${JASPER_LOW_MEMORY_BUILD_PARK_UNITS[@]}"; do
        # The two lists overlap by jasper-camilla-crossover today; skip so the
        # record cannot hold a unit twice regardless of future edits.
        _jasper_unit_in_list "${unit}" "${JASPER_CORE_GRAPH_PARK_UNITS[@]}" && continue
        _record_low_memory_parked_unit "${unit}"
    done

    # jasper-fanin must stop before park_audio_clients_for_core_graph_restart
    # stops jasper-outputd below: with outputd gone and CamillaDSP
    # free-running, fanin's mixer loop loses its downstream pacer and its RT
    # thread trips RLIMIT_RTTIME (SIGKILL) within ~1s. fanin is a restart
    # target (JASPER_CORE_GRAPH_RESTART_TARGETS), not parked here, so it is
    # stopped explicitly rather than reordered into either park list.
    systemctl stop jasper-fanin.service 2>/dev/null || true
    systemctl reset-failed jasper-fanin.service 2>/dev/null || true
    park_audio_clients_for_core_graph_restart
    for unit in "${JASPER_LOW_MEMORY_BUILD_PARK_UNITS[@]}"; do
        systemctl stop "${unit}" 2>/dev/null || true
        systemctl reset-failed "${unit}" 2>/dev/null || true
    done
}

# Outcome counters for one unpark pass. Module-scope because the per-unit
# helper below is called from two ordered loops and bash 3.2 has no nameref.
_JASPER_LOW_MEMORY_UNPARK_RESTORED=0
_JASPER_LOW_MEMORY_UNPARK_FAILED=0

_unpark_one_low_memory_unit() {
    local unit="$1" enablement
    # Already back — the normal success path restarted it. The trap must not
    # churn a live graph.
    systemctl is-active --quiet "${unit}" 2>/dev/null && return 0

    # Intent can change AFTER the snapshot, and a recovery path must not undo a
    # deliberate decision: park_streambox_brain_units `disable --now`s the brain
    # surfaces on a full->streambox conversion, well after the park. Re-checking
    # at restore time covers that site and anything added later, where filtering
    # the recorded array at each disable site would not.
    #
    # The question is whether intent CHANGED during this install, NOT whether
    # the unit is off right now. Several units run while permanently disabled
    # because a reconciler starts them and systemd never does: on a bonded
    # speaker jasper-grouping-reconcile starts jasper-snapclient/-snapserver,
    # which ship disabled and are never `enable`d. Skipping every off unit
    # strands exactly those — and that reconciler is a WantedBy=multi-user
    # oneshot with no timer and no path unit, so nothing re-runs it before the
    # next boot. So: skip only a unit that is off NOW and was NOT off when it
    # was parked.
    #
    # `is-enabled` reports static/indirect/generated for units that legitimately
    # carry no [Install] directives; none of those are off states and all are
    # restored. That is what the socket-activated wizard .service units
    # report — their [Install] sections are comment-only, so the `disable` in
    # enable_streambox_web_sockets cannot move them to `disabled` and this
    # branch never sees them.
    enablement="$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
    case "${enablement}" in
        disabled|masked|masked-runtime)
            if ! _jasper_unit_was_off_at_park "${unit}"; then
                _build_sandbox_log "low_memory_build_unpark_skip" \
                    "unit=${unit} state=${enablement} left off on purpose"
                return 0
            fi
            ;;
    esac

    if systemctl start "${unit}" 2>/dev/null; then
        _JASPER_LOW_MEMORY_UNPARK_RESTORED=$(( _JASPER_LOW_MEMORY_UNPARK_RESTORED + 1 ))
        return 0
    fi
    _JASPER_LOW_MEMORY_UNPARK_FAILED=$(( _JASPER_LOW_MEMORY_UNPARK_FAILED + 1 ))
    # A masked unit reaches here precisely because it was masked and running
    # when the park stopped it (the branch above sends a newly-masked one down
    # the deliberate-off skip). systemd refuses to start it, so handing the
    # operator a bare `systemctl start` hands them the command that just
    # failed — it has to be unmasked first, and re-masked afterwards. Stopping
    # at `unmask && start` leaves the unit unmasked + running, which is NOT the
    # state the install found: it silently drops a mask the operator set on
    # purpose. A mask is the only thing that makes a unit un-startable
    # whatever its enablement says, so dropping it hands back a unit that
    # anything can start again — a boot, a reconciler, another unit's `Wants=`.
    # Same rule the skip branch above follows: a recovery path must not undo a
    # deliberate decision. Masking does not stop a running unit, so the re-mask
    # chains safely, and `--runtime` is carried through so a /run-scoped mask is
    # not promoted to a permanent /etc one.
    #
    # `case` rather than a `[[ ]] &&` one-liner because a `case` with no
    # matching branch returns 0 while a false `[[ ]] &&` list returns 1, so
    # these lines stay safe even if the trap's guard is ever dropped. That is
    # defence in depth, NOT what keeps the recovery alive: the trap calls this
    # via `unpark_low_memory_build_units || true`, and a caller's guard
    # suspends `set -e` for the whole call tree beneath it — which is also why
    # the unguarded `_build_sandbox_log` below cannot abort the loop.
    local recover="systemctl start ${unit}"
    local recover_sudo="sudo systemctl start ${unit}"
    local remask=""
    case "${enablement}" in
        masked)         remask="systemctl mask ${unit}" ;;
        masked-runtime) remask="systemctl mask --runtime ${unit}" ;;
    esac
    case "${remask}" in
        ?*)
            recover="systemctl unmask ${unit} && ${recover} && ${remask}"
            recover_sudo="sudo systemctl unmask ${unit} && ${recover_sudo} && sudo ${remask}"
            ;;
    esac
    _build_sandbox_log "low_memory_build_unpark_failed" \
        "unit=${unit} recover=${recover}"
    echo "  WARN: could not restart ${unit} after a failed install;" >&2
    echo "  recover with: ${recover_sudo}" >&2
}

unpark_low_memory_build_units() {
    # Recovery for an install that dies between the park above and the systemd
    # step that would normally restart these. Without it, ANY abort in that
    # window leaves the speaker silently dead: every daemon exited cleanly, so
    # nothing looks broken, the box just vanishes from AirPlay and stays gone
    # with no cue, no alert and nothing retrying. A failed install is expected;
    # a speaker that stays dead afterwards is not.
    #
    # Called from install.sh's EXIT trap. Idempotent and best-effort: a unit
    # that is already back is skipped, and a failure to restore one unit must
    # not stop the others.
    (( ${#JASPER_LOW_MEMORY_PARK_RECORD[@]} )) || return 0
    local unit
    _JASPER_LOW_MEMORY_UNPARK_RESTORED=0
    _JASPER_LOW_MEMORY_UNPARK_FAILED=0
    for unit in "${JASPER_LOW_MEMORY_UNPARK_FIRST[@]}"; do
        _jasper_unit_in_list "${unit}" "${JASPER_LOW_MEMORY_PARK_RECORD[@]}" || continue
        _unpark_one_low_memory_unit "${unit}"
    done
    for unit in "${JASPER_LOW_MEMORY_PARK_RECORD[@]}"; do
        _jasper_unit_in_list "${unit}" "${JASPER_LOW_MEMORY_UNPARK_FIRST[@]}" && continue
        _unpark_one_low_memory_unit "${unit}"
    done
    # Unconditional: restored=0 is the normal success path (the trap ran and
    # found nothing to do) and needs to be distinguishable in the journal from
    # the trap never running at all.
    _build_sandbox_log "low_memory_build_unpark" \
        "parked=${#JASPER_LOW_MEMORY_PARK_RECORD[@]} restored=${_JASPER_LOW_MEMORY_UNPARK_RESTORED} failed=${_JASPER_LOW_MEMORY_UNPARK_FAILED}"
    JASPER_LOW_MEMORY_PARK_RECORD=()
    JASPER_LOW_MEMORY_PARK_OFF_AT_PARK=()
}

park_streambox_brain_units() {
    # Converting from a full speaker to streambox must park local brain
    # surfaces while keeping renderer/DSP/source surfaces alive.
    #
    # Fan-in coupling is audio-data-plane ownership, not a voice-brain feature.
    # Both full and streambox profiles run the same renderer/fan-in graph and USB
    # Audio Input needs its direct lane on either profile, so coupling and its
    # bounded health timer deliberately remain outside this parking list.
    #
    # jasper-input is out for the same reason: it translates HID key events into
    # jasper-control HTTP calls, and jasper-control runs on both profiles. A
    # streambox with a paired volume remote must keep its buttons.
    local brain_unit
    for brain_unit in \
        jasper-voice.service jasper-aec-bridge.service jasper-aec-init.service \
        jasper-aec-reconcile.service \
        jasper-enhanced-aec-install.service jasper-enhanced-aec-reconcile.path \
        camillagui.socket camillagui.service camillagui-proxy.service; do
        systemctl disable --now "${brain_unit}" >/dev/null 2>&1 || true
    done
    systemctl disable --now jasper-sources-web.socket jasper-sources-web.service \
        >/dev/null 2>&1 || true
    # Both markers' only writer (jasper-aec-reconcile) is parked above, so
    # neither can be corrected until the next boot, and each fails in its own
    # direction. A stale voice-input-absent would condition-fail every
    # jasper-voice start the accessory reconciler issues for a paired mic
    # remote; a stale aec-bridge-ready would admit the bridge that voice's
    # reconciler-owned Wants= drop-in pulls with it (ADR-0224), on a box with
    # no XVF and nothing left to withdraw the verdict.
    # See docs/adr/0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md
    rm -f "${STATE_DIR}/voice-input-absent"
    rm -f /run/jasper-aec-reconcile/aec-bridge-ready
}

enable_streambox_web_sockets() {
    local unit
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web \
                jasper-system-web jasper-chat-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
        if systemctl is-enabled "${unit}.service" --quiet 2>/dev/null; then
            systemctl disable "${unit}.service" 2>/dev/null || true
        fi
        systemctl enable "${unit}.socket"
        systemctl restart "${unit}.socket" 2>/dev/null || true
    done
}

reapply_source_intent() {
    # Re-converge the complete local-source lifecycle after active-only renderer
    # refreshes. Install never enables or starts an Off source as a temporary
    # baseline: the coordinator is the only writer of source enablement and the
    # only path that starts a desired-on source, including Bluetooth RF-kill.
    # install.sh runs as root and invokes the same reconciler directly. Keep a
    # process-level bound beyond the unit's 2693-second contract so a
    # manual/stale lock holder or child regression cannot pin deploy forever.
    # The reconciler is the single authority for the source allowlist and
    # runtime ordering. A failed apply WARNs; boot or the next toggle retries.
    # A short-lived pre-merge build used JASPER_SOURCE_INTENT_BLUETOOTH. That
    # name lives inside an older release's strict owned namespace, so leaving it
    # behind would make a rollback reject the whole intent file. Migrate it to
    # the deliberately rollback-ignorable key before either reconciler reads.
    if [[ -e "${STATE_DIR}/source_intent.env" || -L "${STATE_DIR}/source_intent.env" ]]; then
        /opt/jasper/.venv/bin/python - "${STATE_DIR}/source_intent.env" <<'PY'
import sys

from jasper.atomic_io import locked_transform_env_file

path = sys.argv[1]
legacy_key = "JASPER_SOURCE_INTENT_BLUETOOTH"
canonical_key = "JASPER_BLUETOOTH_SOURCE_INTENT"

def migrate(state: dict[str, str]) -> dict[str, str]:
    legacy_value = state.pop(legacy_key, None)
    if legacy_value is not None and canonical_key not in state:
        state[canonical_key] = legacy_value
    return state

locked_transform_env_file(
    path,
    migrate,
    mode=0o660,
    group_from_parent=True,
    lock_mode=0o660,
    max_bytes=64 * 1024,
    lock_timeout_sec=2.0,
)
PY
    fi
    # Remove the old acknowledgement immediately, then again under the
    # coordinator lock. The long lock wait drains any legitimate in-flight
    # pass (bounded by the unit's 2693 s ceiling); a failed/timeout path removes
    # the file once more so deploy health cannot accept an older generation.
    rm -f /run/jasper-source-intent/status.json
    if ! /usr/bin/timeout --foreground --kill-after=5s 2703s \
        /opt/jasper/.venv/bin/jasper-source-intent-reconcile \
            --reason install --invalidate-status-before; then
        rm -f /run/jasper-source-intent/status.json
        echo "  WARN: source intent reconcile failed. Check logs with: journalctl -u jasper-source-intent-reconcile -e"
    fi
}

start_streambox_runtime_units() {
    local unit
    systemctl enable jasper-camilla.service jasper-fanin.service \
        jasper-outputd.service jasper-audio-hardware-reconcile.service \
        jasper-control.service jasper-source-intent-reconcile.service \
        jasper-accessory-reconcile.service jasper-input.service
    # --now: enable alone only arms the watcher for the NEXT boot, and every
    # accessory refresh requested before then would be dropped on the floor.
    systemctl enable --now jasper-accessory-reconcile.path
    park_audio_clients_for_core_graph_restart
    reset_failed_core_graph_restart_targets
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"
    systemctl restart jasper-fanin.service 2>/dev/null || true
    require_outputd_ready || \
        echo "  WARN: jasper-outputd is not ready. Check http://${JASPER_HOSTNAME:-jts.local}/system/ and 'journalctl -u jasper-outputd'. Continuing so the web UI and doctor remain available."
    ensure_outputd_camilla_statefile
    reconcile_sound_dsp_state
    restart_core_camilla_after_dsp_reconcile

    # Hardware-gated USB management network (composite gadget +
    # device-activated DHCP). Skips cleanly when the resolved role cannot
    # provide management transport or no UDC exists yet.
    enable_usbgadget
    # Mux is core arbitration infrastructure, not a user-selectable source.
    # Keep it available (role guard still parks followers); its ~1 Hz idle loop
    # is cheaper and safer than coupling output policy to aggregate source state.
    systemctl enable --now jasper-mux.service
    systemctl enable jasper-fanin-coupling-auto.service
    systemctl try-restart bluealsa-aplay.service nqptp.service \
        shairport-sync.service librespot.service bt-agent.service \
        2>/dev/null || true
    reapply_source_intent
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web \
                jasper-system-web jasper-chat-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    reconcile_grouping_state
    # Run the owner now, not only at next boot. Since #2285 there is no
    # install-profile gate: a streambox resolves its coupling on the same
    # per-box ring evidence as any other box, and the independent USB decision
    # still arms DIRECT capture from canonical source intent either way.
    resolve_fanin_coupling_default
    systemctl enable jasper-wifi-guardian.service
    systemctl enable --now jasper-wifi-recover.timer
    systemctl enable jasper-bootloop-guard.service
    systemctl enable --now jasper-identity-reconcile.timer
    # The oneshot itself must be enabled, not only started: daemons that
    # resolve the hostname order themselves After= it, and an un-enabled
    # unit is never pulled into the boot transaction.
    systemctl enable jasper-identity-reconcile.service
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    systemctl restart jasper-control.service
    # Enabling only arms these for the NEXT boot; deploy health checks this
    # boot. Mirrors the full path: restart the bridge so an already-paired
    # remote picks up new code, then let the reconciler publish the mic source
    # a paired remote needs. Ordered after jasper-control, which is what the
    # bridge posts key events to.
    systemctl restart jasper-input.service 2>/dev/null || true
    /opt/jasper/.venv/bin/jasper-accessory-reconcile --reason install || \
        echo "  WARN: accessory reconcile failed; optional remote mics may stay inactive until next boot"
}

mask_distro_background_units() {
    # Distro housekeeping a single-purpose speaker never benefits from. On a
    # 415 MB Zero 2 W an apt-daily or man-db run evicts the audio path's page
    # cache; cloud-init costs 6.9 s of boot re-deciding a host identity JTS
    # already owns. Masking the timers leaves the services runnable by hand
    # (`systemctl start apt-daily.service`) — only the schedule goes away.
    # apt-daily-upgrade.timer is what drives unattended-upgrades, so security
    # updates become the owner's `apt upgrade`, not the box's.
    # To undo on a box that already ran this: `systemctl unmask --now <unit>`.
    # A mask outlives the installer, so deleting this function does not
    # restore the timers.
    local unit
    for unit in apt-daily.timer apt-daily-upgrade.timer man-db.timer \
                dpkg-db-backup.timer e2scrub_all.timer; do
        # `mask --now` also stops the unit, and stopping an absent one fails:
        # these five ship in different packages and no image carries them all.
        if systemctl list-unit-files "${unit}" 2>/dev/null \
                | grep -q "^${unit}"; then
            systemctl mask --now "${unit}" >/dev/null 2>&1 || true
            # `mask --now` on a still-active timer records Result=resources
            # (the mask severs the trigger link before the stop resolves it)
            # and the unit sits in `failed` state forever even though the
            # mask itself succeeded — clear that stale record.
            systemctl reset-failed "${unit}" >/dev/null 2>&1 || true
        fi
    done
    # The sentinel file is cloud-init's own opt-out: the package stays
    # installed, and deleting the file restores it on the next boot.
    local cloud_dir="${JTS_CLOUD_INIT_DIR:-/etc/cloud}"
    if [[ -d "${cloud_dir}" ]]; then
        touch "${cloud_dir}/cloud-init.disabled" \
            || echo "  WARN: could not write ${cloud_dir}/cloud-init.disabled"
    fi
}

install_streambox_systemd_units() {
    install_jasper_support_files
    install_local_audio_graph_unit_files
    install_streambox_web_unit_files
    install_resilience_identity_unit_files
    install_usbsink_unit_files
    install_grouping_unit_files
    install_renderer_source_unit_files
    install_streambox_audio_slices
    install_hid_accessory_unit_files
    install_voice_unit_files
    install_audio_output_recovery_unit_files
    park_streambox_brain_units
    mask_distro_background_units

    validate_streambox_systemd_units
    systemctl daemon-reload
    systemctl enable --now jts-audio.slice >/dev/null 2>&1 || true
    enable_streambox_web_sockets
    start_streambox_runtime_units
    echo "Streambox units enabled. Local sources, DSP, /sound/, /system/, and grouping reconcile are live; voice/AEC remain parked."
}

install_systemd_units() {
    # Full speakers and streamboxes consume the same support-file and core-graph
    # owners before any profile-specific units are staged or started.
    install_jasper_support_files
    install_local_audio_graph_unit_files
    # Stage the remaining full-profile generation as one rollback domain. A
    # failed copy/render restores every destination touched since this point,
    # reloads systemd against that prior generation, and exits before any unit
    # enable/start mutation below. The wrapper is removed at commit; a staging
    # failure exits the installer immediately after rollback.
    local install_transaction_dir
    install_transaction_dir="$(mktemp -d /tmp/jasper-full-unit-install.XXXXXX)"
    local -a install_transaction_paths=()
    local -a install_transaction_existed=()
    local install_transaction_errtrace_was_set=0
    [[ $- == *E* ]] && install_transaction_errtrace_was_set=1
    set -E
    trap '_rollback_full_unit_install_transaction' ERR
    install() { _transactional_full_unit_install "$@"; }
    # The enhanced-AEC installed marker authorizes loading native code. Keep
    # its containing directory outside group-writable StateDirectory=jasper so
    # a non-root service in the shared `jasper` group cannot replace the proof.
    install -d -m 0755 -o root -g root /var/lib/jasper-enhanced-aec
    install_voice_unit_files
    # The wizard daemons are SOCKET-ACTIVATED (each .service is paired
    # with a .socket unit that holds the port and re-spawns the daemon
    # on demand). systemd binds the listener; the daemon adopts the fd
    # via LISTEN_FDS and exits after 10 min idle, saving ~60-90 MB Pss
    # while no one is using a setup page. See jasper/web/_systemd.py.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.service" \
        "${SYSTEMD_DIR}/jasper-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-web.socket" \
        "${SYSTEMD_DIR}/jasper-web.socket"
    # /correction/ wizard. Phase 0 = mic-permission verify only;
    # future phases pull in heavy deps (numpy / scipy / pyfar) so
    # this lives in its own process rather than colocating with
    # jasper-web (Spotify + voice settings).
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.service" \
        "${SYSTEMD_DIR}/jasper-correction-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-correction-web.socket" \
        "${SYSTEMD_DIR}/jasper-correction-web.socket"
    # /bluetooth/ control panel — generic BT scan/pair/forget for
    # phones, knobs, headphones. Drives bluez via dbus-next; per-class
    # post-pair behaviour lives in jasper/bluetooth/handlers/.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.service" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-bluetooth-web.socket" \
        "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
    # /system/ dashboard — RAM/CPU/temp sparklines + restart/diagnostics
    # actions. Socket-activated like the other wizards.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.service" \
        "${SYSTEMD_DIR}/jasper-system-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-system-web.socket" \
        "${SYSTEMD_DIR}/jasper-system-web.socket"
    # /chat/ conversation-history dashboard. Read-only; socket-activated
    # like /system/ so opening the history page does not keep a resident
    # web process forever.
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-chat-web.service" \
        "${SYSTEMD_DIR}/jasper-chat-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-chat-web.socket" \
        "${SYSTEMD_DIR}/jasper-chat-web.socket"
    install_hid_accessory_unit_files
    # AEC bridge + boot-time chip init + reconciler. The reconciler is
    # the policy layer that keeps JASPER_MIC_DEVICE, AEC services, and
    # the currently attached mic hardware in sync.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-bridge.service" \
        "${SYSTEMD_DIR}/jasper-aec-bridge.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-init.service" \
        "${SYSTEMD_DIR}/jasper-aec-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-aec-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-aec-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-enhanced-aec-install.service" \
        "${SYSTEMD_DIR}/jasper-enhanced-aec-install.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-enhanced-aec-reconcile.path" \
        "${SYSTEMD_DIR}/jasper-enhanced-aec-reconcile.path"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-aec-reconcile" \
        /usr/local/sbin/jasper-aec-reconcile
    # WiFi profile guardian. Type=oneshot boot-time recreate of a lost
    # /etc/NetworkManager/system-connections/<SSID>.nmconnection from
    # the wizard-owned stash at /var/lib/jasper/wifi_guardian.env. This
    # defends against the 2026-05-23 incident.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-guardian.service" \
        "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.service" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.timer" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.timer"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-scan-repair.service" \
        "${SYSTEMD_DIR}/jasper-wifi-scan-repair.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-guardian" \
        /usr/local/sbin/jasper-wifi-guardian
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-recover" \
        /usr/local/sbin/jasper-wifi-recover

    # Identity reconciler. Type=oneshot snapshot of the speaker's
    # effective mDNS identity (OS hostname vs Avahi's post-collision
    # name vs JASPER_HOSTNAME) into /var/lib/jasper/identity.env, on a
    # 5-min timer because a collision rename lands when the OTHER
    # device joins the LAN. jasper.http_security reads the file so a
    # renamed speaker's management UI stays reachable instead of
    # 403ing.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-identity-reconcile.timer" \
        "${SYSTEMD_DIR}/jasper-identity-reconcile.timer"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-identity-reconcile" \
        /usr/local/sbin/jasper-identity-reconcile

    # Boot-loop guard. Type=oneshot cross-boot circuit breaker for the
    # T5.1 StartLimitAction=reboot ladder: on the Nth boot inside the
    # window it writes runtime drop-ins (StartLimitAction=none) so a
    # PERMANENT daemon failure parks the sick unit failed (visible to
    # systemctl/doctor; systemctl reset-failed + start to recover) but
    # leaves the Pi reachable instead of rebooting forever. Runtime
    # drop-ins live in /run and self-clear on the next healthy boot.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-bootloop-guard.service" \
        "${SYSTEMD_DIR}/jasper-bootloop-guard.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-bootloop-guard" \
        /usr/local/sbin/jasper-bootloop-guard

    # jasper-usbgadget: composite ConfigFS gadget owner. It carries the
    # USB management network (ncm) when gadget hardware is available AND the wizard-toggled USB audio
    # function (uac2). jasper-usbsink is the disabled-by-default /sources audio
    # lifecycle/readiness marker; jasper-fanin DIRECT-captures the gadget and
    # the marker orders After= the gadget owner. jasper-usbnet-dhcp is the scoped,
    # device-activated dnsmasq for the USB network. The resolved peripheral
    # role must be active (handled by reconcile_usb_data_role above).
    install_usbsink_unit_files

    # jasper multi-room grouping (snapcast). snapserver is the timing
    # master; snapclient plays a single channel on each speaker. The
    # reconcile oneshot maps the wizard-owned /var/lib/jasper/grouping.env
    # role to which units run (leader => snapserver + snapclient;
    # follower => snapclient only; off/invalid => neither). All three ship
    # DISABLED — a solo speaker runs none of them, and the reconciler is
    # the only thing that enables/starts them on explicit opt-in. We do
    # NOT auto-enable grouping here. See jasper.multiroom.reconcile.
    #
    # Packages: we deliberately do NOT apt-install snapserver/snapclient
    # in the core install. The vast majority of speakers are solo, the
    # snapcast packages pull in extra runtime deps (libsoxr, libvorbis,
    # libflac, avahi client, etc.) and an enabled-by-default snapserver
    # daemon socket — pure dead weight + attack surface on a box that
    # will never group. Mirrors the off-by-default posture of the
    # usbsink dtoverlay (staged but inert until the wizard opts in). The units reference
    # /usr/bin/snapserver and /usr/bin/snapclient (Trixie's `snapserver`
    # / `snapclient` apt packages); installing those is the grouping
    # OPT-IN's job:
    # the grouping reconciler apt-installs them the first time grouping is
    # enabled (jasper.multiroom.provision.ensure_snapcast_installed), surfacing
    # "Installing Snapcast…" in /rooms via /state.grouping.provision. So a solo
    # install stays binary-free, a grouping box self-heals if the binaries are
    # missing, and jasper-doctor's check_grouping_snapcast_installed surfaces the
    # gap regardless. The reconciler's plan is also fail-safe — if the binaries
    # are absent the unit simply fails to start and grouping stays off, never
    # wedging a solo speaker.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapserver.service" \
        "${SYSTEMD_DIR}/jasper-snapserver.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapclient.service" \
        "${SYSTEMD_DIR}/jasper-snapclient.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile-trailing.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile-trailing.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-grouping-reconcile-trailing" \
        /usr/local/sbin/jasper-grouping-reconcile-trailing
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-grouping-reconcile-kick" \
        /usr/local/sbin/jasper-grouping-reconcile-kick

    # If the snapcast apt packages ARE present (the grouping opt-in
    # installed them), neutralise their DISTRO units: Trixie's snapserver
    # package ships an enabled-by-default snapserver.service that squats
    # :1704, advertises _snapcast._tcp on the LAN, and burns RAM on every
    # boot — a rogue second server JTS never manages (observed live on a
    # lab Pi 2026-06-11: a bare `snapclient` auto-discovered the rogue
    # instead of the JTS leader). JTS owns jasper-snapserver /
    # jasper-snapclient; the distro units must never run. Idempotent and
    # safe when the packages are absent (no unit files → no-op).
    local distro_unit
    for distro_unit in snapserver.service snapclient.service; do
        if systemctl list-unit-files "${distro_unit}" 2>/dev/null \
                | grep -q "^${distro_unit}"; then
            systemctl disable --now "${distro_unit}" >/dev/null 2>&1 || true
        fi
    done

    # Triggered by the udev rule installed below when the Apple dongle
    # re-enumerates: reset-failed, restart Camilla, then run the
    # mic/AEC reconciler so a hardware reconnect recovers without
    # manual intervention.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-dongle-recover.service" \
        "${SYSTEMD_DIR}/jasper-dongle-recover.service"
    # Experimental microphone-rig guard: a CH340 tty hot-plug starts one
    # bounded identity-check-and-stop attempt. It is never enabled or polled.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-turntable-autostop@.service" \
        "${SYSTEMD_DIR}/jasper-turntable-autostop@.service"
    # Pin the Apple dongle's analog Headphone control to 100% at every
    # boot — the dynamic volume control happens in CamillaDSP (or the
    # source's own slider) and the dongle should never be limiting us.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-dac-init" \
        /usr/local/bin/jasper-dac-init
    # Apple-only mixer helpers receive APPLE_DONGLE_SERVICE_CARD, always
    # "auto": they resolve the card on every start, so a card id sampled
    # during a re-enumeration cannot be frozen into these units.
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-dac-init.service" \
        > "${install_transaction_dir}/jasper-dac-init.service"
    install -m 0644 \
        "${install_transaction_dir}/jasper-dac-init.service" \
        "${SYSTEMD_DIR}/jasper-dac-init.service"
    # Diagnostic monitor: 1Hz poll on the dongle's Headphone control,
    # logs every change to journald. Companion to jasper-dac-init —
    # if something moves the control after boot, this surfaces when
    # and how often. See deploy/bin/jasper-headphone-monitor.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-headphone-monitor" \
        /usr/local/bin/jasper-headphone-monitor
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-headphone-monitor.service" \
        > "${install_transaction_dir}/jasper-headphone-monitor.service"
    install -m 0644 \
        "${install_transaction_dir}/jasper-headphone-monitor.service" \
        "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    # Custom udev rule: re-pins the dongle's Headphone control to 100%
    # on every USB (re-)enumeration AND disables autosuspend on the
    # device. Compensates for two upstream issues:
    #   * Trixie's alsa-utils 1.2.14-1 ships a broken
    #     /usr/lib/udev/rules.d/90-alsa-restore.rules where a GOTO
    #     points at the wrong label, so `alsactl restore` never fires
    #     on hotplug (Debian bug #1093057, still open).
    #   * The Apple dongle's UAC firmware default for the Headphone
    #     control is 80/120 (-20 dB), surfaced via UAC GET_CUR each
    #     time the device probes. Without our rule, every speaker
    #     re-plug or USB resume that triggers re-enumeration costs
    #     the user 20 dB of analog attenuation until they reboot or
    #     run `systemctl start jasper-dac-init` manually.
    # Active reset is also done by jasper-headphone-monitor (1 Hz
    # poller); this rule is the fast path on hotplug, the monitor
    # catches anything the rule doesn't.
    install -d -m 0755 /etc/udev/rules.d
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-apple-dongle.rules" \
        /etc/udev/rules.d/99-jasper-apple-dongle.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-aec-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-aec-reconcile.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-audio-hardware-reconcile.rules" \
        /etc/udev/rules.d/99-jasper-audio-hardware-reconcile.rules
    install -m 0644 \
        "${REPO_DIR}/deploy/udev/99-jasper-turntable-autostop.rules" \
        /etc/udev/rules.d/99-jasper-turntable-autostop.rules
    reload_audio_recovery_udev_rules_for_install

    # We own the full systemd units for each renderer + nqptp + the
    # no-code Bluetooth pairing agent.
    #
    # Defense in depth: a Pi installed against an older codepath could
    # still have /etc/systemd/system/shairport-sync.service.d/jts-output.conf
    # on disk, which would override our ExecStart with
    # /usr/bin/shairport-sync (the apt-package path) — that binary doesn't
    # exist on this stack and the service crash-loops. Actively remove
    # the drop-in on every install so it can't reappear after rsync.
    if [[ -e "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf" ]]; then
        rm -f "${SYSTEMD_DIR}/shairport-sync.service.d/jts-output.conf"
        rmdir "${SYSTEMD_DIR}/shairport-sync.service.d" 2>/dev/null || true
        echo "  removed stale shairport drop-in from a previous install"
    fi
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/librespot.service" \
        "${SYSTEMD_DIR}/librespot.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/shairport-sync.service" \
        "${SYSTEMD_DIR}/shairport-sync.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/nqptp.service" \
        "${SYSTEMD_DIR}/nqptp.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bt-agent.service" \
        "${SYSTEMD_DIR}/bt-agent.service"
    # jasper-mux: latest-source-wins preemption between Spotify,
    # AirPlay, and Bluetooth.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-mux.service" \
        "${SYSTEMD_DIR}/jasper-mux.service"
    # Drop-in routing bluealsa-aplay's output into the JTS loopback
    # instead of ALSA default (HDMI on a fresh Pi).
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa-aplay.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-output.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-output.conf"
    # Drop-in flipping bluealsa.service (the apt-installed system unit)
    # to Restart=always with a StartLimit guard. Same logic as the
    # source-built renderers' service files: a clean exit (status=0)
    # silently disables Bluetooth audio under the apt default.
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa.service.d/jts-restart.conf" \
        "${SYSTEMD_DIR}/bluealsa.service.d/jts-restart.conf"
    install -d -m 0755 "${SYSTEMD_DIR}/bluetooth.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluetooth.service.d/jts-timeout.conf" \
        "${SYSTEMD_DIR}/bluetooth.service.d/jts-timeout.conf"

    # sshd OOM-protection drop-in: Debian's openssh-server package
    # ships ssh.service WITHOUT an OOMScoreAdjust= directive. JTS
    # gives sshd a moderate negative bias so it remains a good recovery
    # path, but keeps it killable because SSH-launched diagnostics
    # inherit this value. Heavy Pi-side diagnostics should run through
    # scripts/pi-run-diagnostic.sh. Operators on distros whose sshd
    # unit is named differently (sshd.service on RHEL/Fedora) should
    # rename. See the file's header comment.
    install -d -m 0755 "${SYSTEMD_DIR}/ssh.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/ssh.service.d/oom-protection.conf" \
        "${SYSTEMD_DIR}/ssh.service.d/oom-protection.conf"

    # nginx recovery drop-in — full-profile parity with the streambox path
    # (see install_nginx_recovery_dropin for the rationale).
    install_nginx_recovery_dropin

    # Stage 2 audio-protection slices: MemorySwapMax=0 on jts-audio.slice
    # (camilla + shairport-sync + librespot + bluealsa-aplay) and
    # jts-mic.slice (aec-bridge). Pages in these slices can NEVER be
    # swapped to zram — direct fix for the 2026-05-24 stress test that
    # caused audible audio glitches because aec-bridge accumulated 42 MB
    # of VmSwap. Requires cgroup memory controller enabled in
    # /boot/firmware/cmdline.txt (handled by migrate_cgroup_memory_enabled).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-audio.slice" \
        "${SYSTEMD_DIR}/jts-audio.slice"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-mic.slice" \
        "${SYSTEMD_DIR}/jts-mic.slice"
    # bluealsa-aplay's Slice= assignment lands as a drop-in (we don't
    # own that unit file fully — the package ships it). The 4 services
    # we DO own (jasper-camilla, jasper-aec-bridge, shairport-sync,
    # librespot) have Slice= directly in the .service file installed
    # above; no separate drop-in needed for them.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-slice.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-slice.conf"

    systemctl daemon-reload
    # Commit only after systemd accepted the complete generation. Runtime
    # mutations below are deliberately outside the staging transaction.
    trap - ERR
    if [[ "${install_transaction_errtrace_was_set}" == "0" ]]; then
        set +E
    fi
    unset -f install
    rm -rf -- "${install_transaction_dir:?}"

    # Exercise both cgroup protection slices now as well as enabling them for
    # boot — both carry [Install], so a copy alone is not enough.
    systemctl enable --now jts-audio.slice jts-mic.slice

    mask_distro_background_units

    # Hardware-gated USB management network: enable the composite gadget (first
    # gadget unit we enable) and wire the device-activated DHCP. Its condition
    # skips cleanly when the resolved role cannot provide management transport
    # or no UDC exists yet.
    enable_usbgadget

    # Legacy migration cleanup: an old endpoint-tier box (the removed third
    # install tier) served /sources/ from a tiny standalone socket on 8773.
    # Full speakers serve /sources/ from the combined jasper-web bundle, so
    # disable that lingering legacy socket before enabling jasper-web.socket
    # on the same port. No-op on a box that never had it (idempotent).
    systemctl disable --now jasper-sources-web.socket jasper-sources-web.service \
        >/dev/null 2>&1 || true

    # Migrate wizard services from always-on to socket-activated.
    # Older installs had jasper-X-web.service enabled directly; the new
    # topology enables the .socket instead, which pulls in the .service
    # on demand. Idempotent: re-running install.sh after migration is
    # already done is a no-op.
    local unit
    for unit in "${WIZARD_UNITS[@]}"; do
        if systemctl is-enabled "${unit}.service" --quiet 2>/dev/null; then
            # First time through this socket-activation migration —
            # disable the always-on
            # service. Stop it explicitly so the next request comes up
            # with the new socket-activated code rather than the
            # still-running old-process binding the port.
            systemctl stop "${unit}.service" 2>/dev/null || true
            systemctl disable "${unit}.service" 2>/dev/null || true
            echo "  migrated ${unit} to socket activation"
        fi
        systemctl enable "${unit}.socket"
        # Restart (not just start) so a deploy that adds or moves a
        # ListenStream= port — e.g. a new wizard sharing this socket
        # like /sources/ on 8773 — actually re-binds. A bare `start`
        # is a no-op when the socket is already active and silently
        # leaves the old port set live; nginx then 502s on the new
        # route until the next reboot. Restart cascades through the
        # Requires=.socket service too, which the later wizard-stop
        # loop also covers, so the order is safe.
        systemctl restart "${unit}.socket" 2>/dev/null || true
    done

    systemctl enable jasper-camilla.service jasper-fanin.service \
        jasper-outputd.service \
        jasper-audio-hardware-reconcile.service \
        jasper-source-intent-reconcile.service \
        jasper-accessory-reconcile.service \
        jasper-voice.service \
        jasper-control.service \
        jasper-input.service
    # --now: enable alone only arms the watcher for the NEXT boot, and every
    # accessory refresh requested before then would be dropped on the floor.
    systemctl enable --now jasper-accessory-reconcile.path
    # Boot retries durable opt-in once. The path unit handles later successful
    # deploys asynchronously after build.txt is published; neither is awaited
    # by install.sh, so an enhancement failure cannot fail the core deploy.
    systemctl enable jasper-enhanced-aec-install.service \
        >/dev/null 2>&1 || \
        echo "  WARN: enhanced-AEC boot retry could not be enabled"
    systemctl enable --now jasper-enhanced-aec-reconcile.path \
        >/dev/null 2>&1 || \
        echo "  WARN: enhanced-AEC deploy reconcile path could not start"

    # Stop currently-running audio clients before outputd/Camilla claim the
    # direct DAC and fan-in graph. On outputd deploys, old voice/renderers may
    # still hold legacy or current graph endpoints; if the core graph starts
    # first, DAC or Camilla ownership can fail with "device busy". The AEC,
    # grouping, and renderer restart steps below restore the appropriate
    # runtime state once the graph is coherent.
    park_audio_clients_for_core_graph_restart
    reset_failed_core_graph_restart_targets
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"

    systemctl restart jasper-fanin.service 2>/dev/null || true
    # outputd owns the final DAC loop on current main. If it is not active
    # and answering STATUS, the voice daemon's outputd TTS socket points at a
    # silent path. Surface that LOUDLY, but do NOT abort the install: nginx,
    # TLS, cues, and the doctor summary are the operator's recovery surface
    # and must always be set up. A transient 3 s STATUS-probe miss or a slow
    # service settle on a loaded 1 GB Pi must not strand the box with no web
    # UI to diagnose it through. The systemd Wants=/After=jasper-outputd
    # dependency is the real runtime guard, and run_doctor_summary re-checks
    # outputd (check_outputd_service) at the end of the install. Mirrors the
    # non-fatal jasper-audio-hardware-reconcile handling a few lines above.
    require_outputd_ready || \
        echo "  WARN: jasper-outputd is not ready (see the STATUS-probe error above). Voice TTS may be silent until outputd recovers; check http://${JASPER_HOSTNAME:-jts.local}/system/ and 'journalctl -u jasper-outputd'. Continuing install so the web UI and doctor remain available."
    ensure_outputd_camilla_statefile
    reconcile_sound_dsp_state
    restart_core_camilla_after_dsp_reconcile

    # Mux is core arbitration infrastructure, not a selectable source. Start it
    # on a fresh install as well as enabling boot; its role ExecCondition skips
    # a bonded follower safely.
    systemctl enable --now jasper-mux.service
    systemctl try-restart bluealsa-aplay.service nqptp.service \
        shairport-sync.service librespot.service bt-agent.service \
        2>/dev/null || true
    reapply_source_intent
    # The wizard services are socket-activated now. Any currently-
    # running instance is on the old code; stop it so the next incoming
    # request brings up the new code via the .socket. Idempotent: if the
    # service is already inactive (post-idle-exit or never started), the
    # stop is a no-op.
    for unit in "${WIZARD_UNITS[@]}"; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    # jasper-input is always-on (HID accessory bridge) — restart so any
    # already-plugged-in knob picks up new code without waiting for boot.
    systemctl restart jasper-input.service 2>/dev/null || true
    # Optional adapter-backed mic sources are profile-gated. Reconcile after
    # code deploy so a paired WiiM Remote 2 starts immediately, while speakers
    # without one never load the BLE decoder at all.
    /opt/jasper/.venv/bin/jasper-accessory-reconcile --reason install || \
        echo "  WARN: accessory reconcile failed; optional remote mics may stay inactive until next boot"

    # Reconcile software AEC against whatever mic hardware is actually
    # present right now. This replaces the old one-way "enable if
    # Array is 6-ch" install step: if a previous install left voice on
    # udp:9876 but the Array is currently absent, reconcile actively
    # clears that stale state and parks voice instead of letting it
    # watchdog-loop on an unfed UDP socket.
    reconcile_aec_state
    reconcile_grouping_state
    # Converge the fan-in ring coupling (the only central transport) and resolve
    # the USB combo (on a gadget box). Runs AFTER grouping reconcile so the pass
    # sees the settled active-leader state. A no-op on an already-converged box
    # (confirm path, no daemon bounce).
    resolve_fanin_coupling_default
    # WiFi profile guardian: oneshot at boot, gated by
    # ConditionPathExists= on the wizard's stash file. Enabling is safe
    # on fresh installs because the unit silently no-ops until the
    # wizard saves once. See migrate_wifi_guardian (called from
    # ensure_env_file above) for the SSH-driven-setup seed path.
    systemctl enable jasper-wifi-guardian.service
    # WiFi recover timer: no resident RAM. Every few minutes it runs a tiny
    # oneshot that exits after one NM active-connection read when WiFi is
    # healthy; when WiFi is down it can run the Pi 5 scan-suppression
    # repair and then delegate profile activation/recreation to the
    # guardian. `--now` makes the first-deploy recovery loop live.
    systemctl enable --now jasper-wifi-recover.timer
    # Boot-loop guard: oneshot at boot; records the boot timestamp and
    # disarms StartLimitAction=reboot via runtime drop-ins only when
    # boots are looping. Safe on fresh installs (first boots never trip).
    systemctl enable jasper-bootloop-guard.service
    # Identity reconciler: boot + 5-min timer; pure observer (writes
    # only /var/lib/jasper/identity.env). `enable --now`, NOT bare
    # `enable`: enable alone arms the timer for the NEXT boot but
    # leaves it inactive until then — the same enable-vs-start trap as
    # the wizard-socket lesson above. Caught on hardware 2026-06-11
    # (timer inactive after first deploy; doctor's snapshot-staleness
    # warn was the backstop). --now is idempotent on redeploys. The
    # one-shot service `start` keeps identity fresh immediately so the
    # allowlist/doctor don't wait for the first timer tick.
    systemctl enable --now jasper-identity-reconcile.timer
    # The oneshot itself must be enabled, not only started: daemons that
    # resolve the hostname order themselves After= it, and an un-enabled
    # unit is never pulled into the boot transaction.
    systemctl enable jasper-identity-reconcile.service
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    echo
    echo "Units enabled. Start with: systemctl start jasper-fanin jasper-camilla jasper-outputd jasper-voice"
}
