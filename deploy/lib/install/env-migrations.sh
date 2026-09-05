#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# State-dir setup, secret-compartment enforcement, and voice-provider
# manifest rendering for deploy/install.sh.
#
# Extracted verbatim from install.sh (the installer remains the only
# caller; it sources this file REPO_DIR-relative from the rsync
# checkout). Functions assume install.sh's globals (ENV_DIR, STATE_DIR,
# INSTALL_DIR) and `set -euo pipefail` from the sourcing shell.
#
# These helpers create/heal the shared state and secret-compartment
# directories, re-assert their ownership and modes on every deploy,
# sweep operator-seeded provider/Routes keys out of jasper.env into the
# jasper-secrets compartment, seed the WiFi guardian stash, clean up
# retired state, and render the voice-provider id manifest. All are
# idempotent and safe on fresh installs.

ensure_state_dir() {
    # `install -d -m` re-chmods an EXISTING dir, so every call (10+ per
    # install) drops an already-widened 0770 dir to 0750 for the moment
    # between this line and the chmod 0770 below, EACCES-ing any concurrent
    # jasper-group writer (atomic_write_text's mkstemp/rename). Only create,
    # never re-chmod, an existing dir here.
    [[ -d "${STATE_DIR}" ]] || install -d -m 0750 "${STATE_DIR}"
    # WS1 Phase 3b: once the shared `jasper` group exists (created by
    # create_jasper_service_users earlier in install), widen the state dir to
    # root:jasper 0770 so the now-non-root jasper-voice/-mux (group jasper) can
    # write group-shared state here (speaker_volume.json via atomic
    # tempfile+rename, which needs dir write). Owner stays root (rollback-safe);
    # idempotent and a no-op before the group exists (pre-3b / fresh install
    # before users are created). Called repeatedly across install, so it lives
    # here rather than as a one-shot — any later `install -d -m 0750` above
    # would otherwise reset the mode/group.
    if getent group jasper >/dev/null 2>&1; then
        chgrp jasper "${STATE_DIR}" 2>/dev/null || true
        chmod 0770 "${STATE_DIR}" 2>/dev/null || true
    fi
    heal_shared_state_modes
}

# WS1 — group-writable heal for the shared, multi-writer state files.
#
# The dir mode (0770) + the daemons' UMask=0007 make NEW files group-writable,
# but files CREATED BEFORE that landed (or by a root writer) can be
# group-`jasper` yet mode 0644 — group-read-only. Because jasper-voice is the
# sole StateDirectory=jasper owner, its restart re-chowns the tree to its user;
# other writers in the same `jasper` group then cannot write a 0644 file they
# no longer own, and the voice DBs raise
# "attempt to write a readonly database" (the 2026-06-19 incident). This
# one-time heal fixes the EXISTING files on upgrade; UMask=0007 keeps new ones
# correct. It carries the same repair for the wizard units that later dropped
# from root to jasper-web: state their root incarnation created has to become
# readable — and, where a writer modifies it in place, owned — by the new uid.
#
# Deliberately an ALLOWLIST, not a recursive chmod: a blanket `chmod -R g+w`
# would also widen single-writer secrets that live in STATE_DIR — notably
# wifi_guardian.env (mode 0600, the WiFi PSK) — to group-readable. Only the
# known group-shared, multi-writer state is touched. Fresh installs no-op (the
# files don't exist until a daemon first creates them).
heal_shared_state_modes() {
    local group_line jasper_gid base sidecar web_uid
    group_line="$(getent group jasper 2>/dev/null || true)"
    [[ -n "${group_line}" ]] || return 0
    jasper_gid="$(printf '%s\n' "${group_line}" | awk -F: 'NR == 1 { print $3 }')"
    if [[ ! "${jasper_gid}" =~ ^[0-9]+$ ]]; then
        echo "  ERROR: could not resolve numeric jasper group id for shared-state heal" >&2
        return 1
    fi
    # Owner for the `w:` specs below (-1 = leave the uid alone). Absent only on
    # a fresh install before create_jasper_service_users, where those files do
    # not exist yet either, so the pass is a no-op.
    web_uid="$(getent passwd jasper-web 2>/dev/null \
        | awk -F: '$1 == "jasper-web" { print $3; exit }')"
    [[ "${web_uid}" =~ ^[0-9]+$ ]] || web_uid="-1"

    # Pass the complete allowlist through one descriptor-based helper. These
    # paths live below a group-writable directory, so a root deploy must never
    # use path-following chgrp/chmod: another group member could replace a name
    # with a symlink between the check and mutation. O_NOFOLLOW + fstat pins a
    # regular file/directory inode before fchown/fchmod. A symlink or unexpected
    # file type aborts install loudly without touching its target.
    #
    # Spec kinds: `f` regular file, `d` directory — both re-group only, because
    # every writer publishes them through jasper.atomic_io (tempfile + replace),
    # which needs write on the DIRECTORY rather than on the old inode. `w` is
    # the exception: a file its writer modifies IN PLACE, so the OWNER has to
    # move with the writer as well.
    local -a heal_specs=()
    for base in \
        "${STATE_DIR}/usage.db" \
        "${STATE_DIR}/timers.db" \
        "${STATE_DIR}/wake-events/wake-events.sqlite3"; do
        for sidecar in "${base}" "${base}-wal" "${base}-shm" "${base}-journal"; do
            heal_specs+=("f:0660:${sidecar}")
        done
    done
    heal_specs+=(
        "f:0660:${STATE_DIR}/speaker_volume.json"
        "f:0660:${STATE_DIR}/audio_health_incidents.json"
        "f:0660:${STATE_DIR}/mux_mode.json"
        "f:0640:${STATE_DIR}/output_topology.json"
        # The crossover-accept seam writes these two from the ROOT
        # jasper-correction-web process while /sound/ reads them as jasper-web.
        # Their writers now pass group_from_parent=True, but that only fixes
        # FUTURE writes -- a box that accepted a measured crossover before this
        # shipped still carries root:root 0640 and renders an empty design page
        # until something happens to rewrite them. Heal the ones already on disk.
        "f:0640:${STATE_DIR}/active_speaker_design_draft.json"
        "f:0640:${STATE_DIR}/active_speaker_crossover_preview.json"
        "f:0660:${STATE_DIR}/grouping.env"
        "f:0660:${STATE_DIR}/.grouping.env.lock"
        "f:0660:${STATE_DIR}/source_intent.env"
        "f:0660:${STATE_DIR}/.source_intent.env.lock"
        "f:0660:${STATE_DIR}/source_intent.env.request.lock"
        "f:0660:${STATE_DIR}/source_intent.env.reconcile.lock"
        "d:0770:${STATE_DIR}/wake-events"
        # The three wizard units that used to run as root now run as
        # jasper-web, so state they created root-owned has to move with them.
        # bt_roles.json is the dangerous one: RoleStore.set() LOADS before it
        # writes, so an unreadable map would republish an EMPTY one and forget
        # every device's handler. The measurement state is the same shape one
        # step milder (an unreadable file reads as "no measurements").
        "f:0640:${STATE_DIR}/bt_roles.json"
        "f:0640:${STATE_DIR}/active_speaker_measurements.json"
        # The Layer-A SSOT older root atomic writers published root:root 0640;
        # jasper-control reads it group `jasper` for the aggregate /state.
        # Future writes preserve the parent group in baseline_profile.py.
        "f:0640:${STATE_DIR}/active_speaker_baseline_profile.json"
        # Active run-record advisory locks. A root-run status poll used to
        # CREATE them root:root 0640, after which no service account could take
        # a lock it can only READ -- the ~3 s crossover_level_run_unavailable
        # ERROR storm and its repeat-admission twin (ADR-0196). The stores now
        # publish 0660 group-writable, but a non-owner cannot repair a lock it
        # cannot open, so the existing ones are healed here. `l` derives the
        # ".<record>.lock" name; only the LOCKS widen to write, the records
        # stay group-READ (published that way by their own atomic writers).
        "l:0660:${STATE_DIR}/active_speaker_commissioning_run.json"
        "l:0660:${STATE_DIR}/active_speaker_crossover_level_run.json"
        "l:0660:${STATE_DIR}/active_speaker_repeat_admission.json"
        "f:0660:${STATE_DIR}/.active_speaker_commissioning_run.json.live-execution.lock"
        "f:0640:${STATE_DIR}/active_speaker_commissioning_run.json"
        "f:0640:${STATE_DIR}/.active_speaker_commissioning_run.json.live-mutation.json"
        # The capture/sweep/tone trees the /correction/ and /sound/ commissioning
        # arms share. install.sh's install_camilladsp() now creates these at
        # install time (2770 group `jasper`, matching their
        # /var/lib/jasper/correction siblings); this heal stays for boxes
        # deployed before that landed, where whichever surface measured first
        # had already made them with a bare mkdir — root:root 0700 (its
        # UMask=0077), leaving the dropped writer unable to even traverse in.
        "d:2770:${STATE_DIR}/active_speaker"
        "d:2770:${STATE_DIR}/active_speaker/campaigns"
        "d:2770:${STATE_DIR}/active_speaker/sessions"
        "d:2770:${STATE_DIR}/active_speaker_captures"
        "d:2770:${STATE_DIR}/active_speaker_sweeps"
        "d:2770:${STATE_DIR}/active_speaker_stimuli"
        "d:2770:${STATE_DIR}/active_speaker_tone_artifacts"
    )
    # The tuning spend ledger is SQLite, written in place rather than
    # replaced, so a root-owned file left by the pre-drop jasper-correction-web
    # would raise "attempt to write a readonly database" for the new writer —
    # the 2026-06-19 class again, and here it would silently stop the paid
    # tuning calls counting against the household spend cap. 0644 is the mode
    # jasper.web.correction_tuning maintains for its group-`jasper` readers.
    for sidecar in \
        "${STATE_DIR}/usage-tuning.db" \
        "${STATE_DIR}/usage-tuning.db-wal" \
        "${STATE_DIR}/usage-tuning.db-shm" \
        "${STATE_DIR}/usage-tuning.db-journal"; do
        heal_specs+=("w:0644:${sidecar}")
    done
    /usr/bin/python3 - "${jasper_gid}" "${web_uid}" "${heal_specs[@]}" <<'PY'
import os
import stat
import sys

gid = int(sys.argv[1])
web_uid = int(sys.argv[2])
for spec in sys.argv[3:]:
    kind, mode_text, path = spec.split(":", 2)
    if kind == "l":
        # A record's advisory lock sibling, named the one way the stores name
        # it (jasper.atomic_io callers: ".<record>.lock"), so install does not
        # respell a filename Python owns. Group-WRITABLE, because taking an
        # advisory lock opens the file for write. See ADR-0196.
        head, base = os.path.split(path)
        path = os.path.join(head, "." + base + ".lock")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    if kind == "d":
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        continue
    except OSError as exc:
        raise SystemExit(
            f"ERROR: refusing unsafe shared-state path {path}: {exc}"
        ) from exc
    try:
        file_stat = os.fstat(fd)
        expected = stat.S_ISDIR(file_stat.st_mode) if kind == "d" else stat.S_ISREG(file_stat.st_mode)
        if not expected:
            raise SystemExit(
                f"ERROR: refusing unexpected shared-state file type at {path}"
            )
        # O_NOFOLLOW stops a SYMLINK redirect, but not a HARDLINK: a group
        # member could pre-create one of these names as a hardlink onto a
        # root-owned file, and fstat would see a plain regular file. A file we
        # own or created has st_nlink == 1; more than one name means the inode
        # is aliased elsewhere, so refuse rather than fchown/fchmod a target we
        # cannot see. Holds regardless of the fs.protected_hardlinks sysctl.
        if not stat.S_ISDIR(file_stat.st_mode) and file_stat.st_nlink != 1:
            raise SystemExit(
                f"ERROR: refusing hardlinked shared-state path {path}"
            )
        os.fchown(fd, web_uid if kind == "w" else -1, gid)
        os.fchmod(fd, int(mode_text, 8))
    finally:
        os.close(fd)
PY
}

# WS1 Phase 4a — the group-`jasper-secrets` secret compartment (LLM API keys +
# Google client secret + OAuth token tree), narrowed to jasper-voice +
# jasper-web. A SIBLING of STATE_DIR on purpose: STATE_DIR is voice/mux's
# StateDirectory, whose recursive chown forces its contents' group back to
# `jasper` — which would re-expose these secrets to every jasper daemon. Installs
# the boot self-heal tmpfiles and creates the parent dir for an immediate
# (no-reboot) deploy. Idempotent; no-op before the group exists.
ensure_secrets_dir() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    if [[ -f "${REPO_DIR}/deploy/tmpfiles/jts-secrets.conf" ]]; then
        install -m 0644 "${REPO_DIR}/deploy/tmpfiles/jts-secrets.conf" \
            /etc/tmpfiles.d/ 2>/dev/null || true
    fi
    # 2770 setgid: voice+web (group jasper-secrets) rwx; other daemons none;
    # setgid → new files inherit group jasper-secrets. Owner root = rollback-safe.
    install -d -m 2770 -g jasper-secrets "${SECRETS_DIR}"
}

# WS1 Phase 4b — the group-`jasper-intsecrets` integration-secret compartment
# (Home Assistant token + Spotify credentials/OAuth token cache), narrowed to
# jasper-voice + jasper-control + jasper-mux + jasper-web. Also a sibling of
# STATE_DIR for the same StateDirectory recursive-chown reason as
# ensure_secrets_dir above. Idempotent; no-op before the group exists.
ensure_intsecrets_dir() {
    getent group jasper-intsecrets >/dev/null 2>&1 || return 0
    if [[ -f "${REPO_DIR}/deploy/tmpfiles/jts-intsecrets.conf" ]]; then
        install -m 0644 "${REPO_DIR}/deploy/tmpfiles/jts-intsecrets.conf" \
            /etc/tmpfiles.d/ 2>/dev/null || true
    fi
    # 2770 setgid: voice/control/mux/web (group jasper-intsecrets) rwx; input
    # none; setgid -> refreshed OAuth token files inherit jasper-intsecrets.
    install -d -m 2770 -g jasper-intsecrets "${INTSECRETS_DIR}"
}

# WS1 Phase 4a — re-assert ownership and modes across the jasper-secrets
# compartment (Google OAuth token tree + client secret + the LLM API keys) on
# every deploy, and run the two key relocations that still have a producer.
#
# This is a CONFIDENTIALITY re-assert, not a migration: nothing writes these
# files under the broad /var/lib/jasper StateDirectory any more, so there is no
# tree left to move. What remains is drift repair — a manual `chmod o+r`, a
# backup restore, or a file an older build created under the wrong owner:group
# would otherwise survive indefinitely, which is exactly what jasper-doctor's
# check_jasper_secrets_compartment FAILs on.
#
# Idempotent; no-op before the group exists.
reassert_secrets_compartment_perms() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    ensure_secrets_dir

    local new_google="${SECRETS_DIR}/google"
    local new_creds="${SECRETS_DIR}/google_credentials.env"

    # (Re-)assert the tree layout + perms. setgid only affects NEW files, so an
    # explicit recursive chown to root:jasper-secrets is required to repair
    # anything already on disk under another owner:group (owner root =
    # rollback-safe + matches the tmpfiles spec; group-read is the access
    # path). install -d is idempotent (fresh install: just creates the subdirs).
    install -d -m 2770 -g jasper-secrets "${new_google}" "${new_google}/tokens"
    chown -R root:jasper-secrets "${SECRETS_DIR}" 2>/dev/null || true
    chmod 0640 "${new_creds}" 2>/dev/null || true
    chmod 0640 "${new_google}/accounts.json" 2>/dev/null || true
    find "${new_google}/tokens" -type f -name '*.json' \
        -exec chmod 0640 {} + 2>/dev/null || true

    migrate_voice_keys_split
    migrate_google_routes_key

    # Re-tighten voice_keys.env's MODE on every deploy. migrate_voice_keys_split
    # chmods 0640 only when it WRITES the file; on the idempotent re-run (the key
    # already split) it doesn't, so a manual `chmod o+r voice_keys.env` (or a
    # backup restore) would survive — a silent CONFIDENTIALITY regression that puts
    # the LLM keys back within reach of any reader, exactly the drift
    # jasper-doctor's check_jasper_secrets_compartment FAILs on. The `chown -R`
    # above already re-groups the whole compartment to jasper-secrets every run;
    # this is the matching mode re-narrow (google_credentials.env / accounts.json /
    # tokens are re-chmod'd just above; voice_keys.env was the one gap because its
    # only chmod lived inside the conditional split). Owner left as-is.
    if [[ -f "${SECRETS_DIR}/voice_keys.env" ]]; then
        chmod 0640 "${SECRETS_DIR}/voice_keys.env" 2>/dev/null || true
    fi
    if [[ -f "${SECRETS_DIR}/google_routes.env" ]]; then
        chmod 0640 "${SECRETS_DIR}/google_routes.env" 2>/dev/null || true
    fi

    # Reconcile against the tmpfiles spec (also surfaces a syntax error early).
    systemd-tmpfiles --create --prefix="${SECRETS_DIR}" 2>/dev/null || true
}

# WS1 Phase 4b — re-assert ownership and modes across the jasper-intsecrets
# integration-secret compartment (Home Assistant token + Spotify
# credentials/OAuth token caches) on every deploy. Like the 4a re-assert above
# this is confidentiality drift repair, not a migration: nothing writes these
# files under the broad /var/lib/jasper StateDirectory any more.
#
# Spotify is read-write: voice, control, mux, and web can all refresh/persist
# spotipy token caches, so the compartment is writable by all four service
# users via systemd ReadWritePaths=. Idempotent; no-op before the group exists.
reassert_intsecrets_compartment_perms() {
    getent group jasper-intsecrets >/dev/null 2>&1 || return 0
    ensure_intsecrets_dir

    local new_ha="${INTSECRETS_DIR}/home_assistant.env"
    local new_spotify_creds="${INTSECRETS_DIR}/spotify_credentials.env"
    local new_legacy_cache="${INTSECRETS_DIR}/.spotify-cache"
    local new_spotify="${INTSECRETS_DIR}/spotify"

    # (Re-)assert the tree layout + perms. The recursive chown repairs anything
    # already on disk under another owner:group (setgid only affects NEW
    # files). install -d is idempotent and also creates the forward path on
    # fresh installs.
    install -d -m 2770 -g jasper-intsecrets \
        "${new_spotify}" "${new_spotify}/caches"
    chown -R root:jasper-intsecrets "${INTSECRETS_DIR}" 2>/dev/null || true
    chmod 0640 "${new_ha}" 2>/dev/null || true
    chmod 0640 "${new_spotify_creds}" 2>/dev/null || true
    chmod 0640 "${new_legacy_cache}" 2>/dev/null || true
    chmod 0640 "${new_spotify}/accounts.json" 2>/dev/null || true
    find "${new_spotify}/caches" -type f -name '*.json' \
        -exec chmod 0640 {} + 2>/dev/null || true

    # Reconcile against the tmpfiles spec (also surfaces a syntax error early).
    systemd-tmpfiles --create --prefix="${INTSECRETS_DIR}" 2>/dev/null || true
}

# Ongoing enforcement, not migration debt: non-negotiable #3 (secrets live
# only in their compartment) has a live producer here, so unlike this file's
# other relocations, this sweep is never gate-cleared by fleet deployment.

# WS1 Phase 4a — move an operator's hand-seeded provider API key out of the
# broad /etc/jasper/jasper.env into the group-jasper-secrets voice_keys.env.
# An operator seeding a key for headless/CI imaging is the remaining producer;
# the /voice wizard writes voice_keys.env directly. Safe: never strips a key
# from jasper.env until its value is confirmed written to voice_keys.env.
migrate_voice_keys_split() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    local jasper_env="${ENV_DIR}/jasper.env"
    local keys_env="${SECRETS_DIR}/voice_keys.env"
    local key line val moved=0

    for key in GEMINI_API_KEY OPENAI_API_KEY XAI_API_KEY; do
        # Already in the secret file (the wizard's normal path)? Just clean any
        # stale operator seed left in jasper.env.
        if [[ -f "${keys_env}" ]] && grep -qE "^${key}=" "${keys_env}"; then
            _strip_key_from_broad "${key}" "${jasper_env}"
            continue
        fi
        # Find the value: an operator seed in jasper.env.
        val=""
        if [[ -f "${jasper_env}" ]]; then
            line=$(grep -E "^${key}=" "${jasper_env}" || true)
            val="${line#"${key}"=}"
        fi
        val="${val%[$'\r\n ']*}"
        [[ -z "${val}" ]] && continue
        # Write to the secret file, then verify before stripping the source.
        touch "${keys_env}"
        chgrp jasper-secrets "${keys_env}" 2>/dev/null || true
        chmod 0640 "${keys_env}"
        printf '%s=%s\n' "${key}" "${val}" >> "${keys_env}"
        if grep -qE "^${key}=" "${keys_env}"; then
            _strip_key_from_broad "${key}" "${jasper_env}"
            moved=1
        fi
    done
    # NOTE: `if/then/fi`, NOT `[[ ... ]] && echo` — the latter returns the test's
    # exit status (1 when moved=0, the common re-deploy case), which under
    # install.sh's `set -e` would abort the whole install on every run after the
    # first migration. The function must end on a clean (zero) status.
    if [[ "${moved}" == "1" ]]; then
        echo "  migrate_voice_keys_split: provider API keys -> ${keys_env}"
    fi
}

_strip_key_from_broad() {
    local key="$1" jasper_env="$2"
    if [[ -f "${jasper_env}" ]]; then
        sed_inplace "${jasper_env}" "/^${key}=/d"
    fi
}

# Move an operator's hand-seeded Google Routes key out of the broad
# /etc/jasper/jasper.env into the group-jasper-secrets google_routes.env. Same
# remaining producer as migrate_voice_keys_split: headless/CI seeding. The
# /transit wizard writes google_routes.env directly.
migrate_google_routes_key() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    ensure_secrets_dir
    local jasper_env="${ENV_DIR}/jasper.env"
    local routes_env="${SECRETS_DIR}/google_routes.env"
    local key="GOOGLE_ROUTES_API_KEY"
    local line val moved=0

    if [[ -f "${routes_env}" ]] && grep -qE "^${key}=" "${routes_env}"; then
        _strip_key_from_broad "${key}" "${jasper_env}"
        chmod 0640 "${routes_env}" 2>/dev/null || true
        return 0
    fi

    val=""
    if [[ -f "${jasper_env}" ]]; then
        line=$(grep -E "^${key}=" "${jasper_env}" || true)
        val="${line#"${key}"=}"
    fi
    val="${val%[$'\r\n ']*}"

    if [[ -n "${val}" ]]; then
        touch "${routes_env}"
        chgrp jasper-secrets "${routes_env}" 2>/dev/null || true
        chmod 0640 "${routes_env}"
        printf '%s=%s\n' "${key}" "${val}" >> "${routes_env}"
        if grep -qE "^${key}=" "${routes_env}"; then
            _strip_key_from_broad "${key}" "${jasper_env}"
            moved=1
        fi
    else
        _strip_key_from_broad "${key}" "${jasper_env}"
    fi

    if [[ "${moved}" == "1" ]]; then
        echo "  migrate_google_routes_key: Google Routes API key -> ${routes_env}"
    fi
}

render_voice_provider_ids_manifest() {
    local provider_ids_file="${STATE_DIR}/voice_provider_ids"
    local python_bin="${JASPER_INSTALL_PYTHON:-${INSTALL_DIR}/.venv/bin/python}"
    local tmp

    ensure_state_dir
    tmp="$(mktemp "${STATE_DIR}/.voice_provider_ids.XXXXXX")"
    if ! "${python_bin}" - <<'PY' > "${tmp}"
from jasper.voice.catalog import provider_ids_manifest_text

print(provider_ids_manifest_text(), end="")
PY
    then
        rm -f "${tmp}" "${provider_ids_file}"
        echo "  warning: could not generate ${provider_ids_file}"
        echo "  jasper-voice will remain parked until a successful install regenerates it"
        return 0
    fi
    chmod 0644 "${tmp}"
    mv "${tmp}" "${provider_ids_file}"
    echo "  voice provider id manifest: ${provider_ids_file}"
}

# Remove the retired dmix/fanin topology switch's state file.
#
# WHY THIS ONE LINE SURVIVED THE #2285 DELETION when the migration around it did
# not: `jasper-doctor`'s `check_fanin_asound_wiring` WARNs on this file's
# presence and names re-running the installer as the fix, and this is the only
# thing in the tree that makes that sentence true. Deleting the remover with the
# rest of the migration would have left a WARN no operator action could ever
# clear -- the paired "doctor warns on presence + install cleans" mechanism with
# its cleaning half cut. Pinned by
# tests/test_install_helpers.py::test_install_removes_the_retired_audio_topology_state.
#
# No backup, deliberately, unlike the migration this replaces: nothing reads the
# file for routing (only the doctor inspects it), so a `.retired.*` copy would
# preserve ghost state under a name the doctor does NOT warn about -- trading a
# clearable warning for a silent one.
remove_retired_audio_topology_state() {
    rm -f "${STATE_DIR}/audio_topology.env" /etc/asound.conf.dmix-mode-backup
}

# Seed /var/lib/jasper/wifi_guardian.env from the currently-active WiFi
# profile if no stash exists yet. This migration hook for the WiFi profile
# guardian covers the SSH-driven setup case where the
# operator brought up WiFi via raspi-config / nmcli before ever
# opening the /wifi/ wizard.
#
# Idempotent:
#   - stash already exists       -> no-op
#   - nmcli missing              -> no-op (no NM, nothing to recover)
#   - no active WiFi connection  -> no-op (Ethernet-only Pi)
#   - active profile is WPA-EAP  -> no-op (enterprise out of scope)
#
# PSK redaction: the stash file is mode 0600 (root-only). The PSK lands
# in it because NM's own keyfile is also plaintext at 0600 — encrypting
# our copy while NM's stays plaintext is theatre against a root-equiv
# attacker. The PSK does NOT appear in any `echo` from this function.
migrate_wifi_guardian() {
    local stash="${STATE_DIR}/wifi_guardian.env"

    # Stash already exists — wizard or a previous migrate seeded it.
    # Nothing to do.
    [[ -f "${stash}" ]] && return 0

    # No nmcli means no NetworkManager; the guardian is a no-op on this
    # host. Don't bother seeding.
    command -v nmcli >/dev/null 2>&1 || return 0

    # Find the active wifi profile NAME. `nmcli` field "TYPE" reports
    # `802-11-wireless` for wifi connections.
    local active
    active=$(nmcli -t -f NAME,TYPE connection show --active 2>/dev/null \
             | awk -F: '$2 ~ /wifi|wireless/ { print $1; exit }')
    [[ -z "${active}" ]] && return 0

    # Pull SSID + PSK + key-mgmt for the active profile. `-s` is
    # "show secrets" — requires root, which install.sh always has.
    # We parse with awk to keep the PSK off any intermediate
    # variable trace (this whole helper runs without `set -x`).
    local ssid="" psk="" key_mgmt=""
    while IFS=: read -r key value; do
        case "${key}" in
            "802-11-wireless.ssid")              ssid="${value}" ;;
            "802-11-wireless-security.psk")      psk="${value}" ;;
            "802-11-wireless-security.key-mgmt") key_mgmt="${value}" ;;
        esac
    done < <(
        nmcli -s -t -f \
            802-11-wireless.ssid,\
802-11-wireless-security.psk,\
802-11-wireless-security.key-mgmt \
            connection show "${active}" 2>/dev/null
    )

    [[ -z "${ssid}" ]] && return 0

    # Enterprise auth is out of scope — the guardian can't recreate it
    # (no cert/identity in our stash). Skip silently rather than write
    # a stash that the guardian itself would refuse.
    [[ "${key_mgmt}" == "wpa-eap" ]] && return 0

    # Default key-mgmt to `none` when nmcli reported nothing (open
    # network). Matches the wizard's behavior.
    [[ -z "${key_mgmt}" ]] && key_mgmt="none"

    # Write atomically: tempfile in same dir, chmod 0600, mv. We're
    # in bash, not Python, so no fsync — the wizard does fsync on
    # its own writes, and seeding from install.sh is a one-time event
    # whose durability matters less than its idempotency.
    ensure_state_dir
    local tmp
    tmp=$(mktemp "${STATE_DIR}/.wifi_guardian.XXXXXX")
    # umask + mode dance: write the file with the PSK never visible to
    # other processes via `ls`. The `chmod 0600` after write is the
    # belt; `umask 077` on the tempfile creation is the suspenders.
    (
        umask 077
        cat > "${tmp}" <<EOF
JASPER_WIFI_SSID=${ssid}
JASPER_WIFI_PSK=${psk}
JASPER_WIFI_KEY_MGMT=${key_mgmt}
EOF
    )
    chmod 0600 "${tmp}"
    mv "${tmp}" "${stash}"

    # PSK redaction: the SSID is fine to log (visible in every nmcli
    # output) but the PSK never appears in this echo or any other.
    echo "  migrate_wifi_guardian: seeded ${stash} from active profile (SSID=${ssid}, key-mgmt=${key_mgmt})"
}

# WS1 Phase 3b-2 — widen the config/secret env files jasper-control reads OFF
# DISK so a non-root jasper-control (and the jasper-doctor it spawns) can read
# them. This is the deliberate, documented group-`jasper` secret-exposure that
# the jasper-control drop requires; per-daemon isolation is Phase 4
# (LoadCredential). Mirrors the Google-token-tree widening (3b-1, python-runtime.sh).
#
# Two distinct surfaces fresh-read these as the jasper-control uid:
#   - /system/diagnostics spawns `jasper-doctor --json`, which loads EVERY
#     env_load.ENV_FILES path and (full profile) Config.from_env → reads the
#     provider API keys + integration secrets it is allowed to see.
#   - /state + /system/snapshot fresh-read home_assistant.env (the HA bearer
#     token) from the jasper-intsecrets compartment and voice_provider.env
#     directly (jasper-control is not restarted on a wizard save, so it can't
#     rely on systemd EnvironmentFile injection).
#
# The wizards themselves now WRITE these at 0640 group jasper (the forward fix);
# this migration is the UPGRADE PATH — it widens files an older build wrote at
# 0600 so the drop doesn't silently break /state + the doctor on existing Pis
# that never re-save a wizard. Idempotent, [[ -f ]]-guarded, no-op before the
# `jasper` group exists. Owner is left as-is (StateDirectory recursive-chown
# may have set it to jasper-voice); cross-daemon reads rely on GROUP, not owner.
widen_control_secret_env_modes() {
    getent group jasper >/dev/null 2>&1 || return 0

    # /etc/jasper/jasper.env: the load-bearing config file the doctor reads. It
    # is created 0640 but owned root:root (group root grants the jasper group
    # nothing) and lives OUTSIDE the StateDirectory recursive-chown, so it needs
    # an EXPLICIT chgrp jasper. The /etc/jasper dir must also be group-traversable
    # — it can be created 0750 root:root (python-runtime.sh), which would block a
    # jasper-group traverse. Set it 0755 (the dir listing is not sensitive — only
    # jasper.env + the cert backups, which keep their own 0640/0600 modes; and
    # nothing non-jasper reads here — the correction CA lives in /var/lib/jasper/ca,
    # nginx certs in /etc/nginx/ssl). 0755 also keeps nginx/www-data traversal,
    # avoiding any TLS-read surprise.
    local jasper_env="${ENV_DIR}/jasper.env"
    if [[ -d "${ENV_DIR}" ]]; then
        chmod 0755 "${ENV_DIR}" 2>/dev/null || true
    fi
    if [[ -f "${jasper_env}" ]]; then
        if ! chgrp jasper "${jasper_env}" 2>/dev/null; then
            echo "  ERROR: failed to chgrp ${jasper_env} to jasper" >&2
            return 1
        fi
        if ! chmod 0640 "${jasper_env}" 2>/dev/null; then
            echo "  ERROR: failed to chmod ${jasper_env} to 0640" >&2
            return 1
        fi
    fi

    # The wizard-written secret files (under /var/lib/jasper, already group
    # jasper via StateDirectory) only need the group-read MODE bit. control_token
    # is the Phase-2 mandatory gate: jasper-control reads it to verify, and
    # jasper-web embeds it via canonical_page() — _stored_token() FAILS SAFE to
    # gate-OFF on EACCES, so an unreadable token would SILENTLY DISABLE the gate.
    # It is owned jasper-voice (StateDirectory chown), so 0640 group read is the
    # only way the non-root jasper-control can read its own token.
    #
    # Three file classes needing GROUP read (0640):
    #   - env/control: voice_provider.env (now keyless) + control_token.
    #   - device-to-device auth: household_secret, minted by jasper-web and
    #     verified/adopted/cleared by jasper-control. Older Phase-C deploys could
    #     have created it owner-only before the non-root reality was accounted
    #     for, and ensure()/adopt() deliberately never overwrite an existing
    #     secret, so the migration must fix the upgrade path.
    #   - non-secret state: sound_profile.json / sound_settings.json (the EQ
    #     config the /state sound card reads); transit.env / weather.env (the
    #     BusTime/location wizards' state, read by jasper-doctor). These carry
    #     no secret.
    # NOTE: the WiFi guardian PSK stash is DELIBERATELY NOT widened here — it
    # holds the WiFi password, which jasper-control does not need the value of
    # (only the SSID, which it derives from nmcli/the journal), so it stays
    # owner-only 0600. Least privilege over blanket widening.
    #
    # WS1 Phase 4a/4b — google_credentials.env moved to jasper-secrets, while
    # spotify_credentials.env + home_assistant.env moved to jasper-intsecrets.
    # Those compartment migrations own their perms now. voice_provider.env stays
    # here (now keyless; control reads the provider name for /system/).
    local f path
    for f in voice_provider.env control_token household_secret \
             sound_profile.json sound_settings.json transit.env weather.env; do
        path="${STATE_DIR}/${f}"
        if [[ -L "${path}" ]]; then
            echo "  widen_control_secret_env_modes: skipping symlink ${path}"
            continue
        fi
        if [[ -f "${path}" ]]; then
            chgrp jasper "${path}" 2>/dev/null || true
            chmod 0640 "${path}" 2>/dev/null || true
        fi
    done
    echo "  widen_control_secret_env_modes: config jasper-control reads is group-jasper readable (0640)"
}
