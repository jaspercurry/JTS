# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# Shared control plane for the laptop-side chip-AEC experiment scripts.
# Source scripts/_lib.sh before this file. That owner supplies PI_HOST,
# PI_USER, and shell_quote().

_CHIP_AEC_RESTORE_ARMED=0

chip_aec_ssh() {
    # The arguments are complete remote commands by design; their dynamic
    # fields are validated or shell_quote()'d at the owning call site.
    # shellcheck disable=SC2029
    ssh "${PI_USER}@${PI_HOST}" "$@"
}

chip_aec_prompt() {
    echo
    echo "------------------------------------------"
    echo "$@"
    echo "------------------------------------------"
    read -r -p "Press Enter when ready... " _ < /dev/tty
}

chip_aec_set_bypass() {
    local value="${1:-}"
    case "$value" in
        0|1) ;;
        *)
            echo "ERROR: SHF_BYPASS must be 0 or 1, got '${value}'." >&2
            return 2
            ;;
    esac

    local ssh_rc=0
    chip_aec_ssh \
        "sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS --values ${value} >/dev/null" \
        || ssh_rc=$?
    if (( ssh_rc != 0 )); then
        return "$ssh_rc"
    fi
    if [[ "$value" == "0" ]]; then
        echo "  SHF_BYPASS = 0 (chip AEC ON)"
    else
        echo "  SHF_BYPASS = 1 (chip AEC bypassed)"
    fi
}

_chip_aec_daemon_set_mode() {
    # Switch the experiment daemon between full mode (reference feeder +
    # UDP mic pump) and ref-only mode (reference feeder only).
    #
    # The XVF UAC2 capture endpoint has one substream. Direct arecord capture
    # therefore needs ref-only mode so the daemon's mic pump releases the
    # endpoint while its reference feeder preserves chip-AEC convergence.
    # Wake detection receives no UDP mic frames in ref-only mode; callers must
    # keep that interval bounded and the EXIT restoration trap armed.
    #
    # Stop/wait/start ordering matters: a new daemon can otherwise race the old
    # process while ALSA is still releasing the endpoint. A live PID is not
    # sufficient proof either, so the restart gate scans only newly appended
    # log lines for the daemon's PCM-open failure messages.
    local mode="${1:-}"
    local mic_channel="${MIC_CHANNEL:-}"
    local extra=""
    case "$mode" in
        full) ;;
        ref-only) extra="--ref-only" ;;
        *)
            echo "ERROR: chip-AEC daemon mode must be full or ref-only, got '${mode}'." >&2
            return 2
            ;;
    esac
    if [[ ! "$mic_channel" =~ ^[0-5]$ ]]; then
        echo "ERROR: MIC_CHANNEL must be an integer from 0 through 5, got '${mic_channel}'." >&2
        return 2
    fi

    local daemon_argv
    daemon_argv="/opt/jasper/.venv/bin/python -m \"\$module\""
    daemon_argv+=" --ref-delay-ms $(shell_quote "${REF_DELAY_MS:-0}")"
    daemon_argv+=" --mic-channel $(shell_quote "$mic_channel")"
    if [[ -n "$extra" ]]; then
        daemon_argv+=" $(shell_quote "$extra")"
    fi

    chip_aec_ssh "set -e
        # Keep the module token split in the controller command. A contiguous
        # copy would appear in this remote shell's argv and make pgrep/pkill -f
        # match the controller itself instead of only the Python daemon.
        module='jasper.chip_aec'_experiment
        module_re='^/opt/jasper/[.]venv/bin/python -m [j]asper[.]chip_aec_experiment( |$)'
        # Scope the startup scan to this daemon, not stale failures.
        boundary=\$(wc -l < /var/log/chip-aec-experiment.log 2>/dev/null || echo 0)

        sudo pkill -f \"\$module_re\" 2>/dev/null || true

        # Allow the daemon's 0.5 s loop wake-up and 3 s thread joins to finish.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            pgrep -f \"\$module_re\" >/dev/null || break
            sleep 0.4
        done
        if pgrep -f \"\$module_re\" >/dev/null; then
            echo '    old daemon did not exit after SIGTERM; sending SIGKILL'
            sudo pkill -9 -f \"\$module_re\" || true
            sleep 0.5
        fi

        sudo bash -c 'nohup \"\$@\" >> /var/log/chip-aec-experiment.log 2>&1 < /dev/null &' _ ${daemon_argv}
        sleep 2

        if ! pgrep -f \"\$module_re\" >/dev/null; then
            echo '    FAILED to restart daemon in ${mode} mode (process exited) — last 20 log lines:'
            sudo tail -20 /var/log/chip-aec-experiment.log
            exit 1
        fi

        new_lines=\$(sudo tail -n +\$((boundary + 1)) /var/log/chip-aec-experiment.log 2>/dev/null || true)
        if echo \"\$new_lines\" | grep -qE '(ref feeder|mic pump) open failed'; then
            echo '    daemon process alive but PCM open failed — log excerpt:'
            echo \"\$new_lines\" | grep -E 'open failed' | head -5
            exit 1
        fi

        echo \"    daemon now in ${mode} mode (PID \$(pgrep -f \"\$module_re\"))\"
    "
}

chip_aec_install_restore_trap() {
    trap _chip_aec_cleanup EXIT
    trap '_chip_aec_handle_signal HUP' HUP
    trap '_chip_aec_handle_signal INT' INT
    trap '_chip_aec_handle_signal TERM' TERM
}

chip_aec_enter_ref_only() {
    # Arm first: even a partial stop/restart failure must attempt restoration.
    _CHIP_AEC_RESTORE_ARMED=1
    _chip_aec_daemon_set_mode ref-only
}

chip_aec_restore_full() {
    if [[ "$_CHIP_AEC_RESTORE_ARMED" != "1" ]]; then
        return 0
    fi

    echo
    echo "==> Restoring full chip-AEC experiment state"
    local bypass_rc=0 daemon_rc=0
    chip_aec_set_bypass 0 || bypass_rc=$?
    _chip_aec_daemon_set_mode full || daemon_rc=$?

    if (( bypass_rc == 0 && daemon_rc == 0 )); then
        _CHIP_AEC_RESTORE_ARMED=0
        return 0
    fi

    echo "ERROR: chip-AEC experiment restoration failed (SHF_BYPASS rc=${bypass_rc}, daemon rc=${daemon_rc})." >&2
    if (( bypass_rc != 0 )); then
        return "$bypass_rc"
    fi
    return "$daemon_rc"
}

_chip_aec_cleanup() {
    local original_rc=$?
    local restore_rc=0
    trap - EXIT
    # Once cleanup begins, finish the bounded restoration instead of letting a
    # second signal interrupt it between the chip write and daemon restart.
    trap '' HUP INT TERM

    chip_aec_restore_full || restore_rc=$?
    if (( original_rc != 0 )); then
        exit "$original_rc"
    fi
    exit "$restore_rc"
}

_chip_aec_handle_signal() {
    local signal_name="$1"
    local signal_status
    case "$signal_name" in
        HUP) signal_status=129 ;;
        INT) signal_status=130 ;;
        TERM) signal_status=143 ;;
        *) signal_status=1 ;;
    esac
    # EXIT owns the one restoration implementation. Ignore repeat delivery
    # before exiting so a process-group signal cannot interrupt that cleanup.
    trap '' HUP INT TERM
    exit "$signal_status"
}
