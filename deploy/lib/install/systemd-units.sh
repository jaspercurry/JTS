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
    jasper-dial-web
    jasper-system-web
    jasper-chat-web
)

cleanup_legacy_recovery_window_dropins() {
    # 2026-06-29: jts2 received these ad hoc drop-ins during an emergency
    # targeted recovery. The policy now lives in repo-owned unit files, so a
    # normal deploy should remove the temporary override before daemon-reload.
    local unit dropin dir
    local -a units=(
        librespot
        nqptp
        shairport-sync
        bt-agent
        jasper-mux
        "${WIZARD_UNITS[@]}"
    )
    for unit in "${units[@]}"; do
        dir="${SYSTEMD_DIR}/${unit}.service.d"
        dropin="${dir}/jts-recovery-window.conf"
        if [[ -e "${dropin}" ]]; then
            rm -f "${dropin}"
            rmdir "${dir}" 2>/dev/null || true
            echo "  removed legacy ad hoc recovery drop-in for ${unit}.service"
        fi
    done
}

install_jasper_support_files() {
    install -d -m 0755 /usr/local/lib/jasper /usr/local/sbin /usr/local/bin \
        "${SYSTEMD_DIR}"
    cleanup_legacy_recovery_window_dropins
    install -m 0644 \
        "${REPO_DIR}/deploy/lib/jasper-asound-render.sh" \
        /usr/local/lib/jasper/jasper-asound-render.sh
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
#     is a bonded active leader. docs/HANDOFF-distributed-active.md "Stage B".
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
    "0644 deploy/systemd/jasper-audio-hardware-reconcile.service ${SYSTEMD_DIR}/jasper-audio-hardware-reconcile.service"
    "0755 deploy/bin/jasper-audio-hardware-reconcile /usr/local/sbin/jasper-audio-hardware-reconcile"
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
            "${SYSTEMD_DIR}/librespot.service"
            "${SYSTEMD_DIR}/shairport-sync.service"
            "${SYSTEMD_DIR}/nqptp.service"
            "${SYSTEMD_DIR}/bt-agent.service"
            "${SYSTEMD_DIR}/jasper-mux.service"
            "${SYSTEMD_DIR}/jasper-usbgadget.service"
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
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-usbgadget.service" \
        "${SYSTEMD_DIR}/jasper-usbgadget.service"
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
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-up" \
        /usr/local/sbin/jasper-usbgadget-up
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-down" \
        /usr/local/sbin/jasper-usbgadget-down
    install -m 0755 \
        "${REPO_DIR}/deploy/usbsink/jasper-usbgadget-wanted" \
        /usr/local/sbin/jasper-usbgadget-wanted
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
    # NetworkManager keyfile owning usb0 (10.12.194.1/24, no default route) +
    # a device policy overriding Raspberry Pi OS's blanket "USB gadgets are
    # unmanaged" udev default + the scoped dnsmasq conf the device-activated
    # jasper-usbnet-dhcp.service reads. NM stays the box's single network owner. See
    # docs/HANDOFF-usb-gadget.md.
    install -d -m 0755 /etc/NetworkManager/system-connections
    install -d -m 0755 /etc/NetworkManager/conf.d
    install -m 0600 \
        "${REPO_DIR}/deploy/usb-network/jts-usb.nmconnection" \
        /etc/NetworkManager/system-connections/jts-usb.nmconnection
    install -m 0644 \
        "${REPO_DIR}/deploy/usb-network/90-jasper-usbnet.conf" \
        /etc/NetworkManager/conf.d/90-jasper-usbnet.conf
    install -m 0644 \
        "${REPO_DIR}/deploy/usb-network/usbnet-dnsmasq.conf" \
        /etc/jasper/usbnet-dnsmasq.conf
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
            /etc/NetworkManager/system-connections/jts-usb.nmconnection \
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

migrate_usbsink_init_to_usbgadget() {
    # The old jasper-usbsink-init.service (oneshot ConfigFS gadget owner, ships
    # disabled) is replaced by the hardware-gated composite jasper-usbgadget.service.
    # Disable + stop the old unit on upgrade before enabling the new one, and
    # remove its stale unit file so systemd-analyze / doctor don't trip on a
    # deleted-from-repo file that lingers under /etc/systemd/system. Idempotent
    # and safe on a fresh install (the old unit never existed → no-op).
    if systemctl list-unit-files jasper-usbsink-init.service >/dev/null 2>&1; then
        systemctl disable --now jasper-usbsink-init.service >/dev/null 2>&1 || true
    fi
    local stale="${SYSTEMD_DIR}/jasper-usbsink-init.service"
    if [[ -e "${stale}" ]]; then
        rm -f "${stale}"
        echo "  removed stale jasper-usbsink-init.service (replaced by jasper-usbgadget.service)"
    fi
    # Remove the renamed gadget scripts' old paths so a stale copy can't be
    # invoked by a lingering unit override.
    rm -f /usr/local/sbin/jasper-usbsink-gadget-up \
          /usr/local/sbin/jasper-usbsink-gadget-down 2>/dev/null || true
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
    # Install owns only a safe deployment baseline: derived USB audio Off and a
    # network-only gadget. It MUST NOT interpret canonical On or start the
    # source here. The later reapply_source_intent pass is the single owner of
    # the ordered On transition (arm fan-in direct capture, then advertise UAC2,
    # then start the process-free readiness marker). Advertising UAC2 from here first
    # creates a host-visible source with no live data plane.
    #
    # This baseline is also the upgrade/backcompat fence. Old releases may
    # arrive with jasper-usbsink enabled/active and a bound UAC2 descriptor.
    # Disable+stop the derived unit before touching the gadget. Recompose an
    # already-active gadget only when prior derived audio or the UAC2 card proves
    # stale audio may be present; a converged NCM-only gadget must not flap on
    # every deploy (the deploy itself may be using that management link).
    local audio_was_enabled=0
    local audio_was_active=0
    local uac2_was_present=0
    local uac2_present_after_enable=0
    local audio_enabled_now=0
    local audio_active_now=0
    local gadget_was_active=0
    local park_rc=0
    systemctl is-enabled --quiet jasper-usbsink.service 2>/dev/null && \
        audio_was_enabled=1
    systemctl is-active --quiet jasper-usbsink.service 2>/dev/null && \
        audio_was_active=1
    [[ -d "${JASPER_UAC2_CARD_PATH:-/proc/asound/UAC2Gadget}" ]] && \
        uac2_was_present=1
    systemctl is-active --quiet jasper-usbgadget.service 2>/dev/null && \
        gadget_was_active=1
    systemctl disable --now jasper-usbsink.service >/dev/null 2>&1 || park_rc=$?

    # A systemctl command can return non-zero after partially succeeding, so
    # observed state is authoritative. Fail closed if either half remains: the
    # gadget readiness gate reads enablement, while an active stale lifecycle
    # marker proves the old derived state did not park cleanly.
    systemctl is-enabled --quiet jasper-usbsink.service 2>/dev/null && \
        audio_enabled_now=1
    systemctl is-active --quiet jasper-usbsink.service 2>/dev/null && \
        audio_active_now=1
    if [[ "${audio_enabled_now}" == "1" || "${audio_active_now}" == "1" ]]; then
        echo "  ERROR: could not park USB Audio Input before composition; refusing to compose the USB gadget" >&2
        return 1
    fi
    if [[ "${park_rc}" != "0" ]]; then
        echo "  WARN: USB Audio Input park command returned ${park_rc}, but observed derived state is Off"
    fi
    echo "  event=install.usb_gadget_baseline audio=off next=source_intent_reapply"

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
    [[ -d "${JASPER_UAC2_CARD_PATH:-/proc/asound/UAC2Gadget}" ]] && \
        uac2_present_after_enable=1
    if [[ "${uac2_was_present}" == "1" || \
          "${uac2_present_after_enable}" == "1" || \
          ( "${gadget_was_active}" == "1" && ( \
            "${audio_was_enabled}" == "1" || \
            "${audio_was_active}" == "1" ) ) ]]; then
        systemctl restart jasper-usbgadget.service >/dev/null 2>&1 || {
            echo "  ERROR: jasper-usbgadget did not recompose to the network-only install baseline; refusing to continue with possibly stale UAC2 advertised. Check 'journalctl -u jasper-usbgadget'" >&2
            return 1
        }
    fi
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
    # must call this: jasper-doctor's check_oom_score_adj expects -450 on nginx
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

park_low_memory_build_units() {
    build_swap_required || return 0
    _build_sandbox_log "low_memory_build_park" \
        "stopping runtime units before constrained install/build steps"
    park_audio_clients_for_core_graph_restart
    local unit
    for unit in \
        jasper-fanin.service \
        jasper-camilla.service \
        jasper-camilla-crossover.service \
        jasper-control.service \
        jasper-system-web.service \
        jasper-input.service \
        jasper-accessory-reconcile.service \
        jasper-aec-init.service \
        jasper-aec-reconcile.service \
        bt-agent.service; do
        systemctl stop "${unit}" 2>/dev/null || true
        systemctl reset-failed "${unit}" 2>/dev/null || true
    done
}

park_streambox_brain_units() {
    # Converting from a full speaker to streambox must park local brain
    # surfaces while keeping renderer/DSP/source surfaces alive.
    #
    # Fan-in coupling is audio-data-plane ownership, not a voice-brain feature.
    # Both full and streambox profiles run the same renderer/fan-in graph and USB
    # Audio Input needs its direct lane on either profile, so coupling and its
    # bounded health timer deliberately remain outside this parking list.
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

reapply_source_intent() {
    # Re-converge the complete local-source lifecycle after active-only renderer
    # refreshes. Install never enables or starts an Off source as a temporary
    # baseline: the coordinator is the only writer of source enablement and the
    # only path that starts a desired-on source, including Bluetooth RF-kill.
    # install.sh runs as root and invokes the same reconciler directly. Keep a
    # process-level bound beyond the unit's 783-second contract so a
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
    # pass (bounded by the unit's 783 s ceiling); a failed/timeout path removes
    # the file once more so deploy health cannot accept an older generation.
    rm -f /run/jasper-source-intent/status.json
    if ! /usr/bin/timeout --foreground --kill-after=5s 793s \
        /opt/jasper/.venv/bin/jasper-source-intent-reconcile \
            --reason install --invalidate-status-before; then
        rm -f /run/jasper-source-intent/status.json
        echo "  WARN: source intent reconcile failed. Check logs with: journalctl -u jasper-source-intent-reconcile -e"
    fi
}

start_streambox_runtime_units() {
    systemctl enable jasper-camilla.service jasper-fanin.service \
        jasper-outputd.service jasper-audio-hardware-reconcile.service \
        jasper-control.service jasper-source-intent-reconcile.service
    park_audio_clients_for_core_graph_restart
    reset_failed_core_graph_restart_targets
    /usr/local/sbin/jasper-audio-hardware-reconcile --reason install || \
        echo "  WARN: audio hardware reconcile failed. Check logs with: journalctl -u jasper-audio-hardware-reconcile -e"
    systemctl restart jasper-fanin.service 2>/dev/null || true
    require_outputd_ready || \
        echo "  WARN: jasper-outputd is not ready. Check http://${JASPER_HOSTNAME:-jts.local}/system/ and 'journalctl -u jasper-outputd'. Continuing so the web UI and doctor remain available."
    ensure_outputd_camilla_statefile
    reconcile_sound_dsp_state
    # Restart CamillaDSP only after the DSP state reconcile has re-emitted the
    # active config for the current ring geometry. Restarting it in the gap after
    # fan-in/outputd recreate fresh ring files can load an old chunk-256 statefile
    # against the 2-slot ring before the reconcile gets a chance to heal it.
    systemctl try-restart jasper-camilla.service 2>/dev/null || true

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
    for unit in jasper-web jasper-bluetooth-web jasper-correction-web jasper-system-web; do
        systemctl stop "${unit}.service" 2>/dev/null || true
    done
    reconcile_grouping_state
    # Run the owner now, not only at next boot. Its install-profile gate keeps
    # streambox ring coupling on loopback while the independent USB decision
    # can still arm DIRECT capture from canonical source intent.
    resolve_fanin_coupling_default
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
    migrate_usbsink_init_to_usbgadget

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
    # jasper-input: third-party HID accessory bridge (Anticater VK-01
    # volume knob today; future macro pads / foot pedals). Reads
    # /dev/input/event* via python-evdev, translates known devices'
    # key events into HTTP calls against jasper-control. Always-on
    # like jasper-mux — idle cost is negligible if no accessory is
    # attached. See jasper/accessories/.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-input.service" \
        "${SYSTEMD_DIR}/jasper-input.service"
    # Optional accessory mic profiles are activated by this root oneshot:
    # it reads BlueZ's paired-device state, writes
    # /var/lib/jasper/accessory-mics.env for jasper-voice, and owns the
    # matching adapter unit state. This keeps rare remotes from imposing
    # resident cost on every speaker.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-accessory-reconcile.service" \
        "${SYSTEMD_DIR}/jasper-accessory-reconcile.service"
    # WiiM Remote 2 BLE microphone adapter. Button events still flow through
    # jasper-input; this companion daemon only decodes the remote's GATT voice
    # report into the wiim_remote_2 manual mic UDP source.
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/jasper-wiim-remote-mic.service" \
        "${SYSTEMD_DIR}/jasper-wiim-remote-mic.service"
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

    # jasper-usbgadget: composite ConfigFS gadget owner. It carries the
    # USB management network (ncm) when gadget hardware is available AND the wizard-toggled USB audio
    # function (uac2). jasper-usbsink is the disabled-by-default /sources audio
    # lifecycle/readiness marker; jasper-fanin DIRECT-captures the gadget and
    # the marker orders After= the gadget owner. jasper-usbnet-dhcp is the scoped,
    # device-activated dnsmasq for the USB network. The resolved peripheral
    # role must be active (handled by reconcile_usb_data_role above). See
    # docs/HANDOFF-usb-gadget.md.
    install_usbsink_unit_files

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
    # OPT-IN's job — now IMPLEMENTED (it used to be a comment with no code):
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

    # Retire the old jasper-usbsink-init.service (disable+stop+remove the stale
    # file) BEFORE the daemon-reload so systemd forgets it, then reload so the
    # new jasper-usbgadget.service / jasper-usbnet-dhcp.service are known.
    migrate_usbsink_init_to_usbgadget

    systemctl daemon-reload

    # Hardware-gated USB management network: enable the composite gadget (first
    # gadget unit we enable) and wire the device-activated DHCP. Its condition
    # skips cleanly when the resolved role cannot provide management transport
    # or no UDC exists yet. See docs/HANDOFF-usb-gadget.md.
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
    # CamillaDSP captures the fan-in output. Restart it after the DSP state
    # reconcile so a ring-default deploy cannot start Camilla on a stale
    # chunk-256 statefile against freshly-created 2-slot ring files.
    systemctl try-restart jasper-camilla.service 2>/dev/null || true

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
    # without one keep the BLE decoder stopped/disabled.
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
    # P3/P4 default-flip: resolve the SHIPPED default fan-in coupling (shm_ring on
    # a ring-eligible box, else loopback) and the USB combo (on a gadget box),
    # UNLESS the operator recorded an explicit choice. Runs AFTER grouping
    # reconcile so the coupling pass sees the settled active-leader state. A no-op
    # on an operator-frozen box and on an already-resolved box (confirm path, no
    # daemon bounce).
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
    systemctl start jasper-identity-reconcile.service || \
        echo "  (identity reconcile failed — non-fatal; doctor will flag)"
    echo
    echo "Units enabled. Start with: systemctl start jasper-fanin jasper-camilla jasper-outputd jasper-voice"
}
