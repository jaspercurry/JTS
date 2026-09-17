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
    # The never-armed per-renderer SHM ring ingress. renderer_lanes.env is the
    # arm map nothing writes or reads any more, and the conf.d drop-in defines
    # the `*_ring_lane` PCMs it pointed at; alsa-lib parses that directory on
    # every PCM open on the box, so a definition naming a transport no code can
    # arm must not stay resolvable.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${STATE_DIR}/renderer_lanes.env /etc/alsa/conf.d/61-jts-renderer-lanes.conf|the per-renderer ring ingress arm map and lane PCMs"
    # v1.yml is the pre-outputd rollback graph (issue #2240); install.sh stopped
    # seeding it, but a copy left by an older install is not inert — camillagui's
    # config picker scans /etc/camilladsp/*.yml, and the install-time statefile
    # guard reads it as a flat-allowed graph that writes to the removed
    # pcm.jasper_out dmix.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${CAMILLA_CONF}/v1.yml|the pre-outputd CamillaDSP rollback graph"
    # PR #4333 deleted the Bluetooth role store's writer, its readers, both
    # jasper-doctor privsep rows and the heal pass that kept its mode correct,
    # so an already-deployed box carries {mac: handler_id} for every device it
    # ever paired with nothing left to touch it.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${STATE_DIR}/bt_roles.json|the retired Bluetooth device-role store"
    # ADR-0291 deleted the background research feature: its scheduler, its
    # job store and every reader of that store. A box that ran it still
    # carries the queued prompts and results the assistant was asked to look
    # up, with nothing left to read or expire them.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${STATE_DIR}/research_jobs.db|the retired background-research job store"
    # REMOVAL CONDITION: every box has taken one install after this lands —
    # the writer is gone, so a stuck latch would never self-resolve.
    "file|${STATE_DIR}/active_speaker_crossover_volume_safety.json ${STATE_DIR}/active_speaker_crossover_level_run.json ${STATE_DIR}/.active_speaker_crossover_level_run.json.lock|the retired per-step crossover level-run store and volume-safety latch"
    # ADR-0288 deleted the v1 commissioning lane's run store; install no
    # longer provisions the run record or its lock/mutation sidecars, so an
    # already-deployed box still carries whatever a prior run left on disk
    # with nothing left to read or write them.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "file|${STATE_DIR}/active_speaker_commissioning_run.json ${STATE_DIR}/.active_speaker_commissioning_run.json.lock ${STATE_DIR}/.active_speaker_commissioning_run.json.live-execution.lock ${STATE_DIR}/.active_speaker_commissioning_run.json.live-mutation.json|the retired v1 commissioning run record and its lock/mutation sidecars"
    # The AirPlay free-running toggle is deleted: shairport is always synced,
    # so nothing reads or writes the mode file any more. Its presence would
    # otherwise outlive every reader with a value that no longer means anything.
    # REMOVAL CONDITION: drops once every box has deployed a release after
    # 2026-09-17.
    "file|${STATE_DIR}/airplay_mode.env|the retired AirPlay free-running toggle"
    # capture-entry anchor stash: its writer was deleted with the module (issue #4942 wave 0); drop this row once every box has installed a build past it
    "file|${STATE_DIR}/active_speaker_capture_entry.json|the retired capture-entry anchor stash"
    # The dmix/fanin topology switcher and the device-name deriver, retired when
    # fan-in became the only supported renderer path. A stale installed copy is
    # what lets an operator reintroduce split-brain audio state by hand; the
    # switcher's config is a tree, so it needs the `dir` kind.
    # REMOVAL CONDITION: all three rows drop once every box has taken one
    # install after this lands.
    "file|${LOCAL_SBIN_DIR}/jasper-audio-topology ${LOCAL_SBIN_DIR}/jasper-derive-device-name|the retired topology switcher and device-name deriver"
    "dir|/etc/jasper/audio-topology|the retired topology switcher's config tree"
    # Keys an older .env.example seeded that the wizards now own in their
    # compartments: every unit loads jasper.env first, so the compartment
    # already wins — the stale jasper.env copy is a second, dead writer's
    # residue. JASPER_RESEARCH_* lost every reader when ADR-0291 deleted
    # background research, and the capture relay it registered against is gone.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "env|${ENV_DIR}/jasper.env SPOTIFY_CLIENT_ID SPOTIFY_OAUTH_MODE JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN JASPER_RESEARCH_*|the wizard-owned Spotify keys, the capture-relay token and the research keys"
    # jasper.env is a frozen first-install seed (never re-synced), so a box
    # seeded before these defaults changed keeps the stale value forever: the
    # 1 GiB wake-events cap the doctor's smaller threshold warns on, and a
    # candidate list that outranks the mic registry. Anchored on the exact
    # lines .env.example shipped, so any other value is a deliberate override
    # and survives.
    # REMOVAL CONDITION: every box has taken one install after this lands.
    "env|${ENV_DIR}/jasper.env JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=1073741824 JASPER_MIC_DEVICE_CANDIDATES=Array JASPER_MIC_DEVICE_CANDIDATES=Array,L16K6Ch|the stale frozen-seed wake-events cap and mic candidate lists"
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
                done < <(grep -o "^${key%\*}[A-Za-z0-9_]*=" "${file}" | sort -u)
                ;;
            *=*)
                if [[ "$(jasper_env_file_get "${file}" "${key%%=*}")" == "${key#*=}" ]]; then
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
