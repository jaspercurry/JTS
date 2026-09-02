#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# This fragment is sourced, never executed. The shebang exists only so the
# static linter assumes bash (matches deploy/lib/jasper-core-graph-park-units.sh).
#
# THE COMPOSITION INTERFACE for the one composite USB gadget. Four consumers
# share it so they can never disagree about what the gadget should carry (or
# repeat a divergent copy of the post-rebuild refresh below):
#
#   jasper-usbgadget-wanted    the unit's ExecCondition (is anything wanted?)
#   jasper-usbgadget-up        the ONLY ConfigFS writer
#   jasper-usbgadget-converge  the ONLY caller-facing composition entry point
#   jasper-usbgadget-snapshot  the /system forensics repair action (a forced
#                              rebuild that bypasses converge -- see PHYSICS
#                              beside jasper_usbgadget_refresh_consumers)
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
JASPER_ENV_FILE="${JASPER_USBGADGET_ENV_FILE:-/etc/jasper/jasper.env}"

# The systemctl seam: every caller-facing consumer of this fragment (converge,
# the forensics repair action) needs to drive systemctl hermetically under
# pytest, so this is defined once here rather than once per caller. Production
# never sets the override; the units that run those callers strip it.
SYSTEMCTL="${JASPER_USBGADGET_SYSTEMCTL:-systemctl}"

# The verdict is a marker file the source coordinator publishes (ADR-0221):
# present = allowed, absent (including the whole directory) = blocked.
AUDIO_ALLOWED_CMD="${JASPER_USBGADGET_AUDIO_ALLOWED_CMD:-/usr/bin/test -e /run/jasper-source-intent/allowed/usbsink}"
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

_jasper_usbgadget_env_usb_network() {
    # JASPER_USB_NETWORK reaches the gadget UNIT through its EnvironmentFile=,
    # but the converger runs from install.sh and from jasper-usbmic-apply, and
    # neither loads /etc/jasper/jasper.env. Without this read a kill-switched
    # box computes desired network=1 here while the unit composes network=0, so
    # the two never agree and every deploy and every mic toggle rebuilds —
    # forever. Reading the ONE key restores the agreement.
    #
    # Grep-and-extract, never source/eval: this runs as root, and sourcing turns
    # an inert config value into a code path. The parse mirrors
    # jasper.env_load.parse_env_text, which is what unions the same key into the
    # doctor's os.environ, so the shell and Python readers cannot disagree about
    # a file value: comments skipped, split on the first `=`, surrounding
    # whitespace stripped, one layer of matching quotes dropped, last assignment
    # wins. Every failure (absent file, unreadable, key missing) yields an empty
    # value, which the caller reads as the default `enabled` — the fail-safe
    # direction, because withdrawing the management network is what strands a
    # deploy riding ncm.usb0.
    local line value
    [[ -r "${JASPER_ENV_FILE}" ]] || return 0
    line=$(grep -a -E '^[[:space:]]*JASPER_USB_NETWORK[[:space:]]*=' \
        "${JASPER_ENV_FILE}" 2>/dev/null | tail -n1 || true)
    [[ -n "${line}" ]] || return 0
    value="${line#*=}"
    # Trim surrounding whitespace without extglob (bash 3.2 runs the tests).
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${#value}" -ge 2 ]]; then
        case "${value}" in
            \"*\"|\'*\') value="${value:1:${#value}-2}" ;;
        esac
    fi
    printf '%s' "${value}"
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
    #
    # An assignment already in the environment wins over the file, exactly as
    # it does for the gadget unit (systemd's EnvironmentFile= never overrides an
    # inherited value) and for jasper.env_load's setdefault union. Only a caller
    # that inherited nothing falls through to the file read.
    local net_raw
    if [[ -n "${JASPER_USB_NETWORK+x}" ]]; then
        net_raw="${JASPER_USB_NETWORK:-enabled}"
    else
        net_raw="$(_jasper_usbgadget_env_usb_network)"
        net_raw="${net_raw:-enabled}"
    fi
    NET_INTENT="$(printf '%s' "${net_raw}" | tr '[:upper:]' '[:lower:]')"
    WANT_NETWORK=1
    if [[ "${NET_INTENT}" == "disabled" ]]; then
        WANT_NETWORK=0
    fi

    # Audio is on when the canonical source guard accepts current household USB
    # intent and role. Derived state — the lifecycle mirror unit, fan-in's
    # DIRECT consumer — is a CONSEQUENCE of that intent, disclosed by the
    # doctor and /sources, never a precondition that can withdraw the endpoint.
    # A composed UAC2 nobody is consuming is a silent device the household can
    # see and reason about; a withdrawn one is an invisible failure. See
    # ADR-0191.
    AUDIO_REASON="intent_disabled_or_parked"
    if ${AUDIO_ALLOWED_CMD} >/dev/null 2>&1; then
        WANT_AUDIO=1
        AUDIO_REASON="enabled"
    fi

    # The return microphone is a refinement of the existing UAC2 source, never
    # an audio function of its own: USB Audio Input must already be authorized
    # above.
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
    #
    # OBSERVABLE GATE MISMATCH, on purpose: this reads the CONFIG SYMLINK (is
    # the function composed onto c.1?), while jasper-usbsink.service and the
    # doctor's check_usbgadget_composition read the FUNCTION DIRECTORY (does
    # functions/uac2.usb0 exist at all?). gadget-up creates the directory before
    # linking it, so the two disagree for the width of a partial build: the
    # directory says "audio" while the symlink does not. That is the right split
    # here — a function the configuration does not carry is not advertised, so
    # membership, not existence, is what a rebind changes.
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

# jasper_usbgadget_refresh_consumers <timeout_sec>
#
# PHYSICS, stated once for both callers. A real gadget rebuild unbinds the
# UDC, so the host re-enumerates: the UAC2Gadget ALSA card is destroyed and
# recreated, and every handle on it goes stale. jasper-fanin (deliberately
# not PartOf= the gadget, it is the core mixer) and jasper-usbmic (whose
# ExecCondition can leave it inactive after PartOf= propagation) are
# refreshed here, in data-path order, fan-in before usbmic.
#
# Call this ONLY for a rebuild that is verified to have actually happened
# (converge reads ConfigFS back; the snapshot repair action does the same) --
# never for one that failed or that an ExecCondition skipped. There is no new
# card for the consumers to pick up otherwise.
#
# Every caller times out at its own bound rather than leaving these as an
# unbounded call in what is otherwise a fully-bounded chain -- see each
# caller for its own derivation. Publishes REFRESH_FANIN_RESULT and
# REFRESH_USBMIC_RESULT ("ok" or "failed") so the caller can report them in
# its own event line; never raises -- a try-restart failure is reported, not
# fatal, so the other unit's try-restart always still runs.
#
# SC2034 (appears unused) -- both REFRESH_* names are read by the caller.
# shellcheck disable=SC2034
jasper_usbgadget_refresh_consumers() {
    local timeout_sec="$1"
    REFRESH_FANIN_RESULT=ok
    REFRESH_USBMIC_RESULT=ok
    /usr/bin/timeout "${timeout_sec}s" ${SYSTEMCTL} try-restart jasper-fanin.service \
        >/dev/null 2>&1 || REFRESH_FANIN_RESULT=failed
    /usr/bin/timeout "${timeout_sec}s" ${SYSTEMCTL} try-restart jasper-usbmic.service \
        >/dev/null 2>&1 || REFRESH_USBMIC_RESULT=failed
}
