#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# What earlier releases left behind, for deploy/install.sh: the units, files,
# directories and env lines nothing in the tree writes any more, one row per
# thing in the table below, applied by retire_leftovers(). Adoptions of state an
# older install wrote elsewhere are migrations, and live in state-and-secrets.sh.
#
# Row format: "<kind>|<targets>|<what it retires>", targets space-separated.
#   unit -> disable --now, stop, and reset-failed after the daemon-reload
#   file -> rm -f
#   dir  -> rm -rf
#   env  -> "<env file> <key>...": KEY unsets that key, KEY=VALUE unsets it only
#           while the file's current value is exactly VALUE, KEY* unsets every
#           key the file states with that prefix. Every removal goes through
#           jasper_env_file_unset (deploy/lib/jasper-env-file.sh), the locked,
#           atomic owner of that edit.
# The table expands ENV_DIR, STATE_DIR, SYSTEMD_DIR, CAMILLA_CONF and
# LOCAL_SBIN_DIR when this file is SOURCED; install.sh sets all five above its
# source block.
: "${ENV_DIR:?}" "${STATE_DIR:?}" "${SYSTEMD_DIR:?}" "${CAMILLA_CONF:?}" "${LOCAL_SBIN_DIR:?}"
JASPER_RETIRED_LEFTOVERS=(
    # No backup, deliberately: nothing reads audio_topology.env for routing, so
    # a `.retired.*` copy would preserve ghost state under a name the doctor
    # does NOT warn about. jasper-doctor's check_fanin_asound_wiring WARNs on
    # the file's presence and names re-running the installer as the fix — this
    # row is the half that makes that sentence true.
    # REMOVAL CONDITION: that check drops its WARN AND no Pi still carries
    # /etc/asound.conf.dmix-mode-backup.
    "file|${STATE_DIR}/audio_topology.env /etc/asound.conf.dmix-mode-backup|the dmix/fanin topology switch state"
    # Library copies older installs published that nothing reads any more: the
    # installer libs beside build-sandbox.sh (deploy/bin/jasper-contained-build
    # sources that one alone), the core-graph park list and the Apple-dongle
    # helper. REMOVAL CONDITION: every box has taken one install after this lands.
    "file|/usr/local/lib/jasper/install/env-migrations.sh /usr/local/lib/jasper/install/first-party-runtime.sh /usr/local/lib/jasper/install/memory-resilience.sh /usr/local/lib/jasper/install/model-staging.sh /usr/local/lib/jasper/install/python-runtime.sh /usr/local/lib/jasper/install/renderers.sh /usr/local/lib/jasper/install/ring-platform.sh /usr/local/lib/jasper/install/rust-daemons.sh /usr/local/lib/jasper/install/service-users.sh /usr/local/lib/jasper/install/systemd-units.sh /usr/local/lib/jasper/install/web-assets.sh /usr/local/lib/jasper/jasper-core-graph-park-units.sh /usr/local/lib/jasper/jasper-apple-dongle.sh|the reader-less installer, park-list and Apple-dongle library copies"
)

# Apply `$2...` to every row of kind `$1`. Best-effort throughout: a fresh
# install carries none of these, and a box that never had one must not fail its
# deploy over it. Each applier silences its own expected noise rather than this
# loop silencing all of it.
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
        "$@" "${target_list[@]}" || true
    done
}

# systemctl is loud about units a box never had.
_retire_systemctl() {
    systemctl "$@" >/dev/null 2>&1
}

# A missing env file is a no-op: retire_leftovers runs after the step that seeds
# jasper.env, but must not break if that ever stops being true. 0640 is the mode
# widen_control_secret_env_modes holds jasper.env at, republished here because
# the unset rewrites the file.
_retire_env_lines() {
    local file="$1" key name
    shift
    [[ -f "${file}" ]] || return 0
    for key in "$@"; do
        case "${key}" in
            *\*)
                while read -r name; do
                    jasper_env_file_unset "${file}" "${name%=}" 0640
                done < <(grep -o "^[[:space:]]*${key%\*}[A-Za-z0-9_]*=" "${file}" | tr -d ' \t' | sort -u)
                ;;
            *=*)
                # Only a lone stale seed goes: a key stated twice is a hand
                # edit, and the unset would take the operator's line with it.
                if [[ "$(grep -c "^[[:space:]]*${key%%=*}=" "${file}")" == 1 \
                    && "$(jasper_env_file_get "${file}" "${key%%=*}")" == "${key#*=}" ]]; then
                    jasper_env_file_unset "${file}" "${key%%=*}" 0640
                fi
                ;;
            *)
                jasper_env_file_unset "${file}" "${key}" 0640
                ;;
        esac
    done
}

retire_leftovers() {
    _retire_apply unit _retire_systemctl disable --now
    _retire_apply unit _retire_systemctl stop
    _retire_apply file rm -f --
    _retire_apply dir rm -rf --
    _retire_apply env _retire_env_lines
    systemctl daemon-reload >/dev/null 2>&1 || true
    # A tick that raced the upgrade can leave a removed unit as a not-found
    # tombstone; that terminal state clears only once the reload has forgotten
    # the unit file, so reset-failed runs last.
    _retire_apply unit _retire_systemctl reset-failed
}
