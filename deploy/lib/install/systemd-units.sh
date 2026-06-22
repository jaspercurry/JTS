#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# systemd unit install/enable steps for deploy/install.sh.
#
# Extracted from install.sh; functions assume install.sh globals and
# set -euo pipefail from the sourcing shell.

WIZARD_UNITS=(
    jasper-web
    jasper-bluetooth-web
    jasper-correction-web
    jasper-dial-web
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
        "${REPO_DIR}/deploy/lib/jasper-env-file.sh" \
        /usr/local/lib/jasper/jasper-env-file.sh
    install -d -m 0755 /usr/local/lib/jasper/install
    install -m 0644 \
        "${REPO_DIR}"/deploy/lib/install/*.sh \
        /usr/local/lib/jasper/install/
}

install_local_audio_graph_unit_files() {
    install -d -m 0755 /usr/local/lib/jasper /usr/local/sbin /usr/local/bin \
        "${SYSTEMD_DIR}"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla.service" \
        "${SYSTEMD_DIR}/jasper-camilla.service"
    # camilla#2 — endpoint-crossover instance (:1235). INERT: installed but
    # NOT enabled; a later reconciler PR arms it only on an active leader.
    # docs/HANDOFF-distributed-active.md "Stage B".
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla-crossover.service" \
        "${SYSTEMD_DIR}/jasper-camilla-crossover.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-fanin.service" \
        "${SYSTEMD_DIR}/jasper-fanin.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-outputd.service" \
        "${SYSTEMD_DIR}/jasper-outputd.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # WS1 Phase 3b-2: root oneshot that captures jasper-doctor --json at full
    # fidelity for /system/diagnostics (the non-root jasper-control triggers it
    # via polkit). On-demand only — not enabled.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-doctor-json.service" \
        "${SYSTEMD_DIR}/jasper-doctor-json.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-audio-hardware-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-audio-hardware-reconcile" \
        /usr/local/sbin/jasper-audio-hardware-reconcile
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-output-hardware-hotplug" \
        /usr/local/sbin/jasper-output-hardware-hotplug
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-outputd-failure-reconcile" \
        /usr/local/sbin/jasper-outputd-failure-reconcile
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-camilla-pipe-guard" \
        /usr/local/sbin/jasper-camilla-pipe-guard
    # camilla#2's crossover guard. Like the pipe-guard it breaks a dead-pipe
    # restart loop BEFORE launch, but repairs ONLY to the re-proven
    # driver-domain (Layer-A-intact) graph — never flat (a flat crossover
    # would send full-range to the tweeter). Shipped now alongside the
    # dormant unit so the later reconciler PR can arm the unit without also
    # adding its guard. docs/HANDOFF-distributed-active.md "Stage B".
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-camilla-crossover-guard" \
        /usr/local/sbin/jasper-camilla-crossover-guard
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
}

validate_streambox_web_socket() {
    local socket="${SYSTEMD_DIR}/jasper-web.socket"
    local -a expected_ports=(8765 8771 8773 8775 8783 8784 8785)
    local -a forbidden_ports=(8767 8768 8774 8776 8777 8778 8779 8782)
    local port
    for port in "${expected_ports[@]}"; do
        if ! grep -q "^ListenStream=127\\.0\\.0\\.1:${port}$" "${socket}"; then
            echo "  ERROR: streambox jasper-web.socket missing port ${port}" >&2
            return 1
        fi
    done
    for port in "${forbidden_ports[@]}"; do
        if grep -q "^ListenStream=127\\.0\\.0\\.1:${port}$" "${socket}"; then
            echo "  ERROR: streambox jasper-web.socket still binds full-brain port ${port}" >&2
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
            "${SYSTEMD_DIR}/jasper-camilla-crossover.service"
            "${SYSTEMD_DIR}/jasper-fanin.service"
            "${SYSTEMD_DIR}/jasper-outputd.service"
            "${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
            "${SYSTEMD_DIR}/jasper-snapclient.service"
            "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"
            "${SYSTEMD_DIR}/jasper-web.service"
            "${SYSTEMD_DIR}/jasper-web.socket"
            "${SYSTEMD_DIR}/jasper-bluetooth-web.service"
            "${SYSTEMD_DIR}/jasper-bluetooth-web.socket"
            "${SYSTEMD_DIR}/jasper-correction-web.service"
            "${SYSTEMD_DIR}/jasper-correction-web.socket"
            "${SYSTEMD_DIR}/jasper-system-web.service"
            "${SYSTEMD_DIR}/jasper-system-web.socket"
            "${SYSTEMD_DIR}/librespot.service"
            "${SYSTEMD_DIR}/shairport-sync.service"
            "${SYSTEMD_DIR}/nqptp.service"
            "${SYSTEMD_DIR}/bt-agent.service"
            "${SYSTEMD_DIR}/jasper-mux.service"
            "${SYSTEMD_DIR}/jasper-usbsink-init.service"
            "${SYSTEMD_DIR}/jasper-usbsink.service"
            "${SYSTEMD_DIR}/jts-audio.slice"
            "${SYSTEMD_DIR}/jasper-dongle-recover.service"
            "${SYSTEMD_DIR}/jasper-dac-init.service"
            "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
            "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
            "${SYSTEMD_DIR}/jasper-wifi-recover.service"
            "${SYSTEMD_DIR}/jasper-wifi-recover.timer"
            "${SYSTEMD_DIR}/jasper-bootloop-guard.service"
            "${SYSTEMD_DIR}/jasper-identity-reconcile.service"
            "${SYSTEMD_DIR}/jasper-identity-reconcile.timer"
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
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-init.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
        "${SYSTEMD_DIR}/jasper-usbsink.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-up" \
        /usr/local/sbin/jasper-usbsink-gadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-down" \
        /usr/local/sbin/jasper-usbsink-gadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-wait-card" \
        /usr/local/sbin/jasper-usbsink-wait-card
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-name-patch" \
        /usr/local/sbin/jasper-usbsink-name-patch
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/uac2_name_patch.py" \
        /usr/local/sbin/uac2_name_patch.py
}

install_grouping_unit_files() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapserver.service" \
        "${SYSTEMD_DIR}/jasper-snapserver.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapclient.service" \
        "${SYSTEMD_DIR}/jasper-snapclient.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"

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
        "${REPO_DIR}/deploy/systemd/bluealsa-aplay.service.d/jts-slice.conf" \
        "${SYSTEMD_DIR}/bluealsa-aplay.service.d/jts-slice.conf"
    install -d -m 0755 "${SYSTEMD_DIR}/bluealsa.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/bluealsa.service.d/jts-restart.conf" \
        "${SYSTEMD_DIR}/bluealsa.service.d/jts-restart.conf"
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
    udevadm control --reload-rules
    udevadm trigger --action=add --subsystem-match=sound 2>/dev/null || true
    udevadm trigger --action=add --subsystem-match=usb 2>/dev/null || true
}

install_streambox_audio_slices() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jts-audio.slice" \
        "${SYSTEMD_DIR}/jts-audio.slice"
    install -d -m 0755 "${SYSTEMD_DIR}/ssh.service.d"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/ssh.service.d/oom-protection.conf" \
        "${SYSTEMD_DIR}/ssh.service.d/oom-protection.conf"
}

park_streambox_brain_units() {
    # Converting from a full speaker to streambox must park local brain
    # surfaces while keeping renderer/DSP/source surfaces alive.
    for brain_unit in \
        jasper-voice.service jasper-aec-bridge.service jasper-aec-init.service \
        jasper-aec-reconcile.service jasper-input.service \
        jasper-dial-web.socket jasper-dial-web.service \
        camillagui.socket camillagui.service camillagui-proxy.service; do
        systemctl disable --now "${brain_unit}" >/dev/null 2>&1 || true
    done
    systemctl disable --now jasper-sources-web.socket jasper-sources-web.service \
        >/dev/null 2>&1 || true
}

enable_streambox_web_sockets() {
    local unit
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-system-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
        if systemctl is-enabled "${unit}.service" --quiet 2>/dev/null; then
            systemctl disable "${unit}.service" 2>/dev/null || true
        fi
        systemctl enable "${unit}.socket"
        systemctl restart "${unit}.socket" 2>/dev/null || true
    done
}

start_streambox_runtime_units() {
    systemctl enable jasper-camilla.service jasper-fanin.service \
        jasper-outputd.service jasper-audio-hardware-reconcile.service \
        jasper-control.service
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"
    systemctl restart jasper-fanin.service 2>/dev/null || true
    systemctl try-restart jasper-camilla.service 2>/dev/null || true
    require_outputd_ready || \
        echo "  WARN: jasper-outputd is not ready. Check http://${JASPER_HOSTNAME:-jts.local}/system/ and 'journalctl -u jasper-outputd'. Continuing so the web UI and doctor remain available."
    JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR=1 ensure_outputd_camilla_statefile

    systemctl enable nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service
    systemctl restart bluealsa-aplay.service 2>/dev/null || true
    systemctl restart nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service \
        2>/dev/null || true
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-system-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    reconcile_grouping_state
    systemctl enable jasper-wifi-guardian.service
    systemctl enable --now jasper-wifi-recover.timer
    systemctl enable jasper-bootloop-guard.service
    systemctl enable --now jasper-identity-reconcile.timer
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    systemctl restart jasper-control.service
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
    install_audio_output_recovery_unit_files
    park_streambox_brain_units

    validate_streambox_systemd_units
    systemctl daemon-reload
    systemctl enable --now jts-audio.slice >/dev/null 2>&1 || true
    enable_streambox_web_sockets
    start_streambox_runtime_units
    echo "Streambox units enabled. Local sources, DSP, /sound/, /system/, and grouping reconcile are live; voice/AEC remain parked."
}

install_systemd_units() {
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla.service" \
        "${SYSTEMD_DIR}/jasper-camilla.service"
    # camilla#2 — endpoint-crossover instance (:1235). INERT: installed but
    # NOT enabled; a later reconciler PR arms it only on an active leader.
    # docs/HANDOFF-distributed-active.md "Stage B".
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-camilla-crossover.service" \
        "${SYSTEMD_DIR}/jasper-camilla-crossover.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-voice.service" \
        "${SYSTEMD_DIR}/jasper-voice.service"
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
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.service" \
        "${SYSTEMD_DIR}/jasper-dial-web.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/jasper-dial-web.socket" \
        "${SYSTEMD_DIR}/jasper-dial-web.socket"
    # /correction/ wizard. Phase 0 = mic-permission verify only;
    # future phases pull in heavy deps (numpy / scipy / pyfar) so
    # this lives in its own process rather than colocating with
    # jasper-web (Spotify + voice settings). Mirrors jasper-dial-web.
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
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-control.service" \
        "${SYSTEMD_DIR}/jasper-control.service"
    # WS1 Phase 3b-2: root oneshot for /system/diagnostics (see full-path note).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-doctor-json.service" \
        "${SYSTEMD_DIR}/jasper-doctor-json.service"
    # jasper-input: third-party HID accessory bridge (Anticater VK-01
    # volume knob today; future macro pads / foot pedals). Reads
    # /dev/input/event* via python-evdev, translates known devices'
    # key events into HTTP calls against jasper-control. Always-on
    # like jasper-mux — idle cost is negligible if no accessory is
    # attached. See jasper/accessories/.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-input.service" \
        "${SYSTEMD_DIR}/jasper-input.service"
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
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-aec-reconcile" \
        /usr/local/sbin/jasper-aec-reconcile
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-audio-hardware-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
    install -d -m 0755 /usr/local/lib/jasper
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-asound-render.sh" \
        /usr/local/lib/jasper/jasper-asound-render.sh
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-env-file.sh" \
        /usr/local/lib/jasper/jasper-env-file.sh
    # Installer-only sourced libs (install.sh sources them REPO_DIR-
    # relative from the rsync checkout; the installed copies mirror the
    # other deploy/lib files for on-Pi inspection/consistency).
    install -d -m 0755 /usr/local/lib/jasper/install
    install -m 0644 \
        "${REPO_DIR}"/deploy/lib/install/*.sh \
        /usr/local/lib/jasper/install/
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-audio-hardware-reconcile" \
        /usr/local/sbin/jasper-audio-hardware-reconcile
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-output-hardware-hotplug" \
        /usr/local/sbin/jasper-output-hardware-hotplug
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-outputd-failure-reconcile" \
        /usr/local/sbin/jasper-outputd-failure-reconcile

    # jasper-fanin: per-renderer snd-aloop substream fan-in daemon.
    # **Production default** as of 2026-05-26 — replaces the
    # dmix-based topology that PR #214 introduced and that turned out
    # to cause periodic AirPlay drops via WiFi-burst + dmix-write-
    # timing interaction. This unit is mandatory for renderer audio;
    # enable/start happens below after daemon-reload. See
    # docs/HANDOFF-fan-in-daemon.md for the design + 2026-05-26
    # validation; docs/HANDOFF-airplay.md Pattern A3 for the dmix
    # failure mode that motivated the cutover.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-fanin.service" \
        "${SYSTEMD_DIR}/jasper-fanin.service"
    # jasper-outputd: mainline final-output owner.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-outputd.service" \
        "${SYSTEMD_DIR}/jasper-outputd.service"

    # WiFi profile guardian. Type=oneshot boot-time recreate of a lost
    # /etc/NetworkManager/system-connections/<SSID>.nmconnection from
    # the wizard-owned stash at /var/lib/jasper/wifi_guardian.env. See
    # docs/HANDOFF-resilience.md "WiFi profile recovery" for the
    # design and the 2026-05-23 incident this defends against.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-guardian.service" \
        "${SYSTEMD_DIR}/jasper-wifi-guardian.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.service" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wifi-recover.timer" \
        "${SYSTEMD_DIR}/jasper-wifi-recover.timer"
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-guardian" \
        /usr/local/sbin/jasper-wifi-guardian
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-wifi-recover" \
        /usr/local/sbin/jasper-wifi-recover

    # Camilla pipe guard. ExecStartPre= on jasper-camilla: when the
    # statefile points at a bonded multi-room pipe config but the
    # snapserver FIFO is dead, repair to the base config BEFORE camilla
    # launches — camilladsp exits clean on a dead File sink (measured),
    # and Restart=always + StartLimitAction=reboot would otherwise turn
    # that into a reboot loop. Fail-open by design. See
    # docs/HANDOFF-multiroom.md §2.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-camilla-pipe-guard" \
        /usr/local/sbin/jasper-camilla-pipe-guard
    # Camilla #2 crossover guard. ExecStartPre= on
    # jasper-camilla-crossover: same dead-pipe loop break as the pipe
    # guard, but its safe-repair target is the re-proven DRIVER-DOMAIN
    # (Layer-A-intact) graph — NEVER flat (a flat crossover would send
    # full-range to the tweeter, the hazard this increment prevents).
    # Installed now so the dormant unit is complete; the unit is not yet
    # enabled. docs/HANDOFF-distributed-active.md "Stage B".
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-camilla-crossover-guard" \
        /usr/local/sbin/jasper-camilla-crossover-guard

    # Identity reconciler. Type=oneshot snapshot of the speaker's
    # effective mDNS identity (OS hostname vs Avahi's post-collision
    # name vs JASPER_HOSTNAME) into /var/lib/jasper/identity.env, on a
    # 5-min timer because a collision rename lands when the OTHER
    # device joins the LAN. jasper.http_security reads the file so a
    # renamed speaker's management UI stays reachable instead of
    # 403ing. See docs/HANDOFF-identity.md.
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

    # jasper-usbsink: fourth music source (USB gadget audio in). The
    # init unit owns the ConfigFS gadget descriptor lifecycle; the
    # main service is the Python daemon that bridges gadget capture
    # into usbsink_substream. Both ship DISABLED — the /sources/ wizard
    # toggle owns enable/disable, and the dtoverlay must be set + Pi
    # rebooted first (handled by set_usb_gadget_mode above).
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink-init.service" \
        "${SYSTEMD_DIR}/jasper-usbsink-init.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbsink.service" \
        "${SYSTEMD_DIR}/jasper-usbsink.service"
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-up" \
        /usr/local/sbin/jasper-usbsink-gadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-gadget-down" \
        /usr/local/sbin/jasper-usbsink-gadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-wait-card" \
        /usr/local/sbin/jasper-usbsink-wait-card
    # Makes the host-visible USB device name track the Speaker Name by
    # patching the kernel's hardcoded UAC2 AudioStreaming string into a
    # `updates/` module override (configfs can't set it on 6.12). Pure
    # bash + stdlib python3 — no kernel headers / dkms. Run as
    # jasper-usbsink-init's best-effort ExecStartPre. See
    # docs/HANDOFF-usbsink.md "Device name".
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbsink-name-patch" \
        /usr/local/sbin/jasper-usbsink-name-patch
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/uac2_name_patch.py" \
        /usr/local/sbin/uac2_name_patch.py

    # jasper multi-room grouping (snapcast). snapserver is the timing
    # master; snapclient plays a single channel on each speaker. The
    # reconcile oneshot maps the wizard-owned /var/lib/jasper/grouping.env
    # role to which units run (leader => snapserver + snapclient;
    # follower => snapclient only; off/invalid => neither). All three ship
    # DISABLED — a solo speaker runs none of them, and the reconciler is
    # the only thing that enables/starts them on explicit opt-in. We do
    # NOT auto-enable grouping here. See docs/HANDOFF-multiroom.md and
    # jasper.multiroom.reconcile.
    #
    # Packages: we deliberately do NOT apt-install snapserver/snapclient
    # in the core install. The vast majority of speakers are solo, the
    # snapcast packages pull in extra runtime deps (libsoxr, libvorbis,
    # libflac, avahi client, etc.) and an enabled-by-default snapserver
    # daemon socket — pure dead weight + attack surface on a box that
    # will never group. Mirrors the off-by-default posture of the
    # usbsink dtoverlay (staged but inert until the wizard opts in) and
    # the optional ESP32 firmware (sources staged, build gated behind
    # JASPER_BUILD_OPTIONAL_FIRMWARE=1). The units reference
    # /usr/bin/snapserver and /usr/bin/snapclient (Trixie's `snapserver`
    # / `snapclient` apt packages); installing those is the grouping
    # opt-in's job, not every solo install's. The reconciler's plan is
    # fail-safe — if the binaries are absent the unit simply fails to
    # start and grouping stays off, never wedging a solo speaker.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapserver.service" \
        "${SYSTEMD_DIR}/jasper-snapserver.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-snapclient.service" \
        "${SYSTEMD_DIR}/jasper-snapclient.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-grouping-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-grouping-reconcile.service"

    # If the snapcast apt packages ARE present (the grouping opt-in
    # installed them), neutralise their DISTRO units: Trixie's snapserver
    # package ships an enabled-by-default snapserver.service that squats
    # :1704, advertises _snapcast._tcp on the LAN, and burns RAM on every
    # boot — a rogue second server JTS never manages (observed live on a
    # lab Pi 2026-06-11: a bare `snapclient` auto-discovered the rogue
    # instead of the JTS leader). JTS owns jasper-snapserver /
    # jasper-snapclient; the distro units must never run. Idempotent and
    # safe when the packages are absent (no unit files → no-op).
    for distro_unit in snapserver.service snapclient.service; do
        if systemctl list-unit-files "${distro_unit}" 2>/dev/null \
                | grep -q "^${distro_unit}"; then
            systemctl disable --now "${distro_unit}" >/dev/null 2>&1 || true
        fi
    done

    # Triggered by the udev rule installed below when the Apple dongle
    # re-enumerates: reset-failed, restart Camilla, then run the
    # mic/AEC reconciler so a hardware reconnect recovers without
    # manual intervention. See docs/HANDOFF-resilience.md.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-dongle-recover.service" \
        "${SYSTEMD_DIR}/jasper-dongle-recover.service"
    # Pin the Apple dongle's analog Headphone control to 100% at every
    # boot — the dynamic volume control happens in CamillaDSP (or the
    # source's own slider) and the dongle should never be limiting us.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-dac-init" \
        /usr/local/bin/jasper-dac-init
    # DONGLE_CARD was set above by install_alsa. Apple-only mixer helpers
    # receive APPLE_DONGLE_SERVICE_CARD, which is either the detected Apple
    # card or "auto" so they can no-op/wait safely when absent.
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-dac-init.service" \
        > "${SYSTEMD_DIR}/jasper-dac-init.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-dac-init.service"
    # Diagnostic monitor: 1Hz poll on the dongle's Headphone control,
    # logs every change to journald. Companion to jasper-dac-init —
    # if something moves the control after boot, this surfaces when
    # and how often. See deploy/bin/jasper-headphone-monitor.
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-headphone-monitor" \
        /usr/local/bin/jasper-headphone-monitor
    sed -e "s/__APPLE_DONGLE_CARD__/${APPLE_DONGLE_SERVICE_CARD}/g" \
        "${REPO_DIR}/deploy/systemd/jasper-headphone-monitor.service" \
        > "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
    chmod 0644 "${SYSTEMD_DIR}/jasper-headphone-monitor.service"
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
    udevadm control --reload-rules
    # Trigger the rule once for the currently-attached dongle so we
    # don't have to wait for the next replug. ATTR{} match is
    # idempotent: amixer setting Headphone=100% on an already-pinned
    # control is a no-op.
    udevadm trigger --action=add --subsystem-match=sound 2>/dev/null || true
    udevadm trigger --action=add --subsystem-match=usb 2>/dev/null || true

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
        jasper-voice.service \
        jasper-control.service \
        jasper-input.service

    # Stop the currently-running voice daemon before outputd claims the
    # direct DAC. On outputd deploys, the old voice process may still
    # hold a PortAudio stream to the legacy jasper_out path; if outputd
    # starts first, DAC ownership can fail with "device busy". The AEC
    # reconciler below restarts or parks voice once the output path is
    # coherent.
    systemctl stop jasper-voice.service 2>/dev/null || true
    systemctl reset-failed jasper-voice.service 2>/dev/null || true
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"

    systemctl restart jasper-fanin.service 2>/dev/null || true
    # CamillaDSP captures the fan-in output (`pcm.jasper_capture`).
    # Restart it after fan-in/asound wiring changes so it cannot keep
    # an old capture fd across topology updates.
    systemctl try-restart jasper-camilla.service 2>/dev/null || true
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
    JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR=1 ensure_outputd_camilla_statefile

    systemctl enable nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service
    systemctl restart bluealsa-aplay.service 2>/dev/null || true
    systemctl restart nqptp.service shairport-sync.service \
        librespot.service bt-agent.service jasper-mux.service \
        2>/dev/null || true
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

    # Reconcile software AEC against whatever mic hardware is actually
    # present right now. This replaces the old one-way "enable if
    # Array is 6-ch" install step: if a previous install left voice on
    # udp:9876 but the Array is currently absent, reconcile actively
    # clears that stale state and parks voice instead of letting it
    # watchdog-loop on an unfed UDP socket.
    reconcile_aec_state
    reconcile_grouping_state
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
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    echo
    echo "Units enabled. Start with: systemctl start jasper-fanin jasper-camilla jasper-outputd jasper-voice"
}
