#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-core-graph-park-units.sh).
#
# THE COMPOSITION INTERFACE for the one composite USB gadget. Three consumers
# share it so they can never disagree about what the gadget should carry:
#
#   jasper-usbgadget-wanted    the unit's ExecCondition (is anything wanted?)
#   jasper-usbgadget-up        the ONLY ConfigFS writer
#   jasper-usbgadget-converge  the ONLY caller-facing entry point
#
# Two facts, one vocabulary:
#
#   jasper_usbgadget_desired  -> DESIRED composition, from live intent
#   jasper_usbgadget_live     -> LIVE composition, read back from ConfigFS
#
# Both publish the three fields individually AND as one
# `<network>/<audio>/<usb_mic>` token (COMPOSITION / LIVE_COMPOSITION), so
# "already converged" is a string equality and a rebind is a real difference.
# The token carries no spaces and no `=`, so it stays a single value inside a
# structured `k=v` event line. Producers express intent through the probes
# below — there is no separate desired-state file to drift.
#
# SCOPE: the token covers the FUNCTION SET only, because that is what a rebind
# changes. Descriptor STRINGS (the speaker name in product/mic labels) are not
# composition; their writer (jasper/web/speaker_setup.py) restarts the gadget
# unit explicitly, which tears down and rebuilds unconditionally.
#
# All probes are env-overridable (defaults are the real commands/paths) so
# pytest can drive every row hermetically — see tests/test_usbgadget_script.py.
# Overriding them does NOT change runtime behavior on a real Pi. The gadget
# unit strips every one of these names from its environment.

CONFIGFS="${JASPER_CONFIGFS_ROOT:-/sys/kernel/config}"
UDC_CLASS_DIR="${JASPER_UDC_CLASS_DIR:-/sys/class/udc}"
GADGET_NAME=jts-usb-audio
GADGET_DIR="${CONFIGFS}/usb_gadget/${GADGET_NAME}"

AUDIO_ALLOWED_CMD="${JASPER_USBGADGET_AUDIO_ALLOWED_CMD:-/opt/jasper/.venv/bin/jasper-local-source-allowed --source usbsink}"
AUDIO_READY_CMD="${JASPER_USBGADGET_AUDIO_READY_CMD:-systemctl is-enabled --quiet jasper-usbsink.service}"
AUDIO_DATA_READY_CMD="${JASPER_USBGADGET_AUDIO_DATA_READY_CMD:-/opt/jasper/.venv/bin/python -m jasper.fanin.status --usbsink-direct-armed}"
HARDWARE_ALLOWED_CMD="${JASPER_USBGADGET_HARDWARE_ALLOWED_CMD:-/opt/jasper/.venv/bin/python -m jasper.audio_hardware.usb_port_role --require-management-transport}"
USB_MIC_ENABLED_CMD="${JASPER_USBGADGET_USB_MIC_ENABLED_CMD:-/opt/jasper/.venv/bin/python -m jasper.usb_mic --check-intent}"

emit() {
    # Structured single-line event for journal grep (the wifi-guardian idiom).
    # `$1` is the outcome suffix on `event=usb_gadget.`; `$2` is the kv tail.
    if [[ -n "${2:-}" ]]; then
        printf 'event=usb_gadget.%s %s\n' "$1" "$2" >&2
    else
        printf 'event=usb_gadget.%s\n' "$1" >&2
    fi
}

# SC2034 (appears unused) — every name below is read by the sourcing script.
# shellcheck disable=SC2034
jasper_usbgadget_desired() {
    # Publishes: HARDWARE_OK, GADGET_UDC, WANT_NETWORK, WANT_AUDIO,
    # WANT_USB_MIC, NET_INTENT, AUDIO_REASON, COMPOSITION, WANTED,
    # WANTED_REASON. WANTED is the ExecCondition answer: 1 when a bound
    # descriptor is the desired end state, 0 when NO descriptor is.
    #
    # Fail-safe direction throughout: an unreadable or failing probe reads as
    # "not authorized / not ready", which withdraws a function rather than
    # advertising one whose consumer may not exist.
    #
    # Every `[[ ... ]] && x=1` in this file is written as an if-block on
    # purpose: jasper-usbgadget-up sources the fragment under `set -e`, where a
    # short-circuit that evaluates false is a failing command.
    HARDWARE_OK=0
    GADGET_UDC=""
    WANT_NETWORK=0
    WANT_AUDIO=0
    WANT_USB_MIC=0
    NET_INTENT=""
    AUDIO_REASON="hardware_unavailable"
    WANTED=0
    WANTED_REASON=""
    COMPOSITION="0/0/0"

    # The shared hardware resolver authorizes the management transport. Short
    # circuit: a box that cannot be a peripheral needs none of the probes below.
    if ! ${HARDWARE_ALLOWED_CMD}; then
        WANTED_REASON="hardware_unavailable"
        return 0
    fi
    HARDWARE_OK=1

    # One OTG controller on the BCM2712 (dwc2). No UDC is the fresh-install
    # pre-reboot case: nothing can be bound, so nothing is wanted.
    GADGET_UDC=$(ls "${UDC_CLASS_DIR}" 2>/dev/null | head -n1 || true)
    if [[ -z "${GADGET_UDC}" ]]; then
        AUDIO_REASON="no_udc"
        WANTED_REASON="no_udc"
        return 0
    fi

    # Network is on unless the kill switch is the exact literal `disabled`
    # (case-insensitive; any other value stays enabled — mirrors
    # JASPER_SHAIRPORT_SUPERVISOR). Lowercase via tr: ${var,,} is bash 4+ only
    # and the test harness runs on macOS bash 3.2.
    local net_raw="${JASPER_USB_NETWORK:-enabled}"
    NET_INTENT="$(printf '%s' "${net_raw}" | tr '[:upper:]' '[:lower:]')"
    WANT_NETWORK=1
    if [[ "${NET_INTENT}" == "disabled" ]]; then
        WANT_NETWORK=0
    fi

    # Audio is on only when the canonical source guard accepts BOTH current
    # household USB intent and role, the coordinator-derived lifecycle mirror is
    # enabled, AND live fan-in reports the direct USB lane armed. Canonical Off
    # dominates; derived enablement/readiness are never preference. The third
    # gate is what makes a stale advertised endpoint self-correcting: UAC2 whose
    # consumer is gone is not wanted, so the next converge withdraws it.
    AUDIO_REASON="intent_disabled_or_parked"
    if ${AUDIO_ALLOWED_CMD} >/dev/null 2>&1; then
        AUDIO_REASON="derived_unit_disabled"
        if ${AUDIO_READY_CMD} >/dev/null 2>&1; then
            AUDIO_REASON="direct_lane_unarmed"
            if ${AUDIO_DATA_READY_CMD} >/dev/null 2>&1; then
                WANT_AUDIO=1
                AUDIO_REASON="enabled_direct_ready"
            fi
        fi
    fi

    # The return microphone is a refinement of the existing UAC2 source, never
    # an audio function of its own: USB Audio Input must already be authorized,
    # armed, and ready above.
    if [[ "${WANT_AUDIO}" == "1" ]] && ${USB_MIC_ENABLED_CMD} >/dev/null 2>&1; then
        WANT_USB_MIC=1
    fi

    COMPOSITION="${WANT_NETWORK}/${WANT_AUDIO}/${WANT_USB_MIC}"
    if [[ "${WANT_NETWORK}" == "0" && "${WANT_AUDIO}" == "0" ]]; then
        # Restores the historic zero-RAM contract: libcomposite never loads,
        # the gadget never exists.
        WANTED_REASON="no_function_wanted"
        return 0
    fi
    WANTED=1
    return 0
}

# SC2034 (appears unused) — every name below is read by the sourcing script.
# shellcheck disable=SC2034
jasper_usbgadget_live() {
    # Publishes: LIVE_PRESENT, LIVE_BOUND, LIVE_UDC, LIVE_NETWORK, LIVE_AUDIO,
    # LIVE_USB_MIC, LIVE_COMPOSITION — the composition the kernel is actually
    # carrying, read straight out of
    # ConfigFS. LIVE_PRESENT is tracked separately from LIVE_BOUND so that an
    # UNBOUND leftover descriptor is still a difference from "no descriptor":
    # it is invisible to the host, but leaving it behind would strand the
    # kernel objects a later bring-up has to rebuild from.
    #
    # Function membership is tested with -L, not -e: `ln -s functions/<fn>
    # configs/c.1/` writes a link whose stored target only resolves inside real
    # ConfigFS, so -e would answer differently on a Pi and in a temp tree.
    LIVE_PRESENT=0
    LIVE_BOUND=0
    LIVE_UDC=""
    LIVE_NETWORK=0
    LIVE_AUDIO=0
    LIVE_USB_MIC=0
    local chmask=""
    if [[ -d "${GADGET_DIR}" ]]; then
        LIVE_PRESENT=1
        LIVE_UDC=$(cat "${GADGET_DIR}/UDC" 2>/dev/null || true)
        if [[ -n "${LIVE_UDC}" ]]; then
            LIVE_BOUND=1
        fi
        if [[ -L "${GADGET_DIR}/configs/c.1/ncm.usb0" ]]; then
            LIVE_NETWORK=1
        fi
        if [[ -L "${GADGET_DIR}/configs/c.1/uac2.usb0" ]]; then
            LIVE_AUDIO=1
            # p_chmask is the Pi-to-host direction: 0 disables it, 1 is the
            # mono host microphone gadget-up composes.
            chmask=$(cat "${GADGET_DIR}/functions/uac2.usb0/p_chmask" 2>/dev/null || true)
            if [[ "${chmask}" == "1" ]]; then
                LIVE_USB_MIC=1
            fi
        fi
    fi
    LIVE_COMPOSITION="${LIVE_NETWORK}/${LIVE_AUDIO}/${LIVE_USB_MIC}"
}
