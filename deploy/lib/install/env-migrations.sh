#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Wizard-env migrations + manifest rendering for deploy/install.sh.
#
# Extracted verbatim from install.sh (the installer remains the only
# caller; it sources this file REPO_DIR-relative from the rsync
# checkout). Functions assume install.sh's globals (ENV_DIR, STATE_DIR,
# INSTALL_DIR) and `set -euo pipefail` from the sourcing shell.
#
# These helpers move operator-set keys out of /etc/jasper/jasper.env
# into the wizard-owned /var/lib/jasper/*.env files so each wizard file
# stays the single source of truth, and render the voice-provider id
# manifest. All are idempotent and safe on fresh installs.

ensure_state_dir() {
    install -d -m 0750 "${STATE_DIR}"
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
# correct.
#
# Deliberately an ALLOWLIST, not a recursive chmod: a blanket `chmod -R g+w`
# would also widen single-writer secrets that live in STATE_DIR — notably
# wifi_guardian.env (mode 0600, the WiFi PSK) — to group-readable. Only the
# known group-shared, multi-writer state is touched. Fresh installs no-op (the
# files don't exist until a daemon first creates them).
heal_shared_state_modes() {
    local group_line jasper_gid base sidecar
    group_line="$(getent group jasper 2>/dev/null || true)"
    [[ -n "${group_line}" ]] || return 0
    jasper_gid="$(printf '%s\n' "${group_line}" | awk -F: 'NR == 1 { print $3 }')"
    if [[ ! "${jasper_gid}" =~ ^[0-9]+$ ]]; then
        echo "  ERROR: could not resolve numeric jasper group id for shared-state heal" >&2
        return 1
    fi

    # Pass the complete allowlist through one descriptor-based helper. These
    # paths live below a group-writable directory, so a root deploy must never
    # use path-following chgrp/chmod: another group member could replace a name
    # with a symlink between the check and mutation. O_NOFOLLOW + fstat pins a
    # regular file/directory inode before fchown/fchmod. A symlink or unexpected
    # file type aborts install loudly without touching its target.
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
        "f:0660:${STATE_DIR}/grouping.env"
        "f:0660:${STATE_DIR}/.grouping.env.lock"
        "f:0660:${STATE_DIR}/source_intent.env"
        "f:0660:${STATE_DIR}/.source_intent.env.lock"
        "f:0660:${STATE_DIR}/source_intent.env.request.lock"
        "f:0660:${STATE_DIR}/source_intent.env.reconcile.lock"
        "d:0770:${STATE_DIR}/wake-events"
    )
    /usr/bin/python3 - "${jasper_gid}" "${heal_specs[@]}" <<'PY'
import os
import stat
import sys

gid = int(sys.argv[1])
for spec in sys.argv[2:]:
    kind, mode_text, path = spec.split(":", 2)
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
        os.fchown(fd, -1, gid)
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
# See docs/HANDOFF-privilege-separation.md "Phase 4".
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
# See docs/HANDOFF-privilege-separation.md.
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
        sed -i.bak "/^${key}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
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

# Migrate hand-set wake-detection leg env vars from
# /etc/jasper/jasper.env into the wizard-owned
# /var/lib/jasper/aec_mode.env. The /wake "Wake detection" card
# owns these as booleans (JASPER_WAKE_LEG_RAW, _DTLN,
# _CHIP_AEC, _CHIP_AEC_150, _CHIP_AEC_210); the
# reconciler maps them back to the underlying device/enable vars
# the bridge + voice each read at startup.
#
# Previously AGENTS.md instructed operators to paste raw lines into
# /etc/jasper/jasper.env for opt-in legs:
#   JASPER_MIC_DEVICE_RAW=udp:9877        (dual-stream)
#   JASPER_MIC_DEVICE_DTLN=udp:9878       (triple-stream extras)
#   JASPER_AEC_DTLN_ENABLED=1
# This function preserves an operator's prior intent on upgrade by
# translating those values into the new boolean form. Fresh installs
# (no underlying vars set) are a no-op here — the new defaults seeded
# in reconcile_aec_state take effect (RAW=1, DTLN=0).
#
# It deliberately does NOT strip the underlying vars from jasper.env:
# jasper-aec-reconcile owns those keys there and rewrites all six on
# every run, so removing them here would only delete lines the
# reconciler immediately recreates.
#
# Idempotent — already-translated installs find nothing to migrate.
migrate_wake_legs_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/aec_mode.env"

    [[ -f "${jasper_env}" ]] || return 0

    local raw_line dtln_line dtln_enabled_line
    local chip_150_line chip_210_line chip_enabled_line
    raw_line=$(grep -E '^JASPER_MIC_DEVICE_RAW=' "${jasper_env}" || true)
    dtln_line=$(grep -E '^JASPER_MIC_DEVICE_DTLN=' "${jasper_env}" || true)
    dtln_enabled_line=$(grep -E '^JASPER_AEC_DTLN_ENABLED=' "${jasper_env}" || true)
    chip_150_line=$(grep -E '^JASPER_MIC_DEVICE_CHIP_AEC_150=' "${jasper_env}" || true)
    chip_210_line=$(grep -E '^JASPER_MIC_DEVICE_CHIP_AEC_210=' "${jasper_env}" || true)
    chip_enabled_line=$(grep -E '^JASPER_AEC_CHIP_AEC_ENABLED=' "${jasper_env}" || true)

    if [[ -z "${raw_line}${dtln_line}${dtln_enabled_line}${chip_150_line}${chip_210_line}${chip_enabled_line}" ]]; then
        return 0
    fi

    ensure_state_dir

    local raw_value dtln_value dtln_enabled_value
    local chip_150_value chip_210_value chip_enabled_value
    raw_value="${raw_line#JASPER_MIC_DEVICE_RAW=}"
    raw_value="${raw_value%[$'\r\n ']*}"
    dtln_value="${dtln_line#JASPER_MIC_DEVICE_DTLN=}"
    dtln_value="${dtln_value%[$'\r\n ']*}"
    dtln_enabled_value="${dtln_enabled_line#JASPER_AEC_DTLN_ENABLED=}"
    dtln_enabled_value="${dtln_enabled_value%[$'\r\n ']*}"
    chip_150_value="${chip_150_line#JASPER_MIC_DEVICE_CHIP_AEC_150=}"
    chip_150_value="${chip_150_value%[$'\r\n ']*}"
    chip_210_value="${chip_210_line#JASPER_MIC_DEVICE_CHIP_AEC_210=}"
    chip_210_value="${chip_210_value%[$'\r\n ']*}"
    chip_enabled_value="${chip_enabled_line#JASPER_AEC_CHIP_AEC_ENABLED=}"
    chip_enabled_value="${chip_enabled_value%[$'\r\n ']*}"

    # An operator running the dual-stream setup had RAW set to a
    # udp:* device. Empty value means they had explicitly cleared
    # it — treat as off so we don't silently turn things on.
    local want_raw="0"
    [[ -n "${raw_value}" ]] && want_raw="1"

    # An operator running DTLN had both MIC_DEVICE_DTLN and
    # AEC_DTLN_ENABLED=1. Either alone is enough signal to preserve.
    local want_dtln="0"
    if [[ -n "${dtln_value}" || "${dtln_enabled_value}" == "1" ]]; then
        want_dtln="1"
    fi

    # Chip-AEC: the low-level enabled flag preserves the base chip-AEC
    # profile. Hand-set per-beam device vars additionally preserve the
    # advanced extra wake-detector opt-ins. If a wizard-owned file already
    # says chip-AEC is off, trust that higher-level intent and do not
    # resurrect per-beam detectors from stale /etc/jasper/jasper.env lines.
    local want_chip_aec="0"
    if [[ -n "${chip_150_value}" || -n "${chip_210_value}" \
          || "${chip_enabled_value}" == "1" ]]; then
        want_chip_aec="1"
    fi
    local want_chip_aec_150="0"
    [[ -n "${chip_150_value}" ]] && want_chip_aec_150="1"
    local want_chip_aec_210="0"
    [[ -n "${chip_210_value}" ]] && want_chip_aec_210="1"
    local existing_chip_aec_line existing_chip_aec_value
    existing_chip_aec_line=$(grep -E '^JASPER_WAKE_LEG_CHIP_AEC=' "${wizard_env}" 2>/dev/null || true)
    existing_chip_aec_value="${existing_chip_aec_line#JASPER_WAKE_LEG_CHIP_AEC=}"
    existing_chip_aec_value="${existing_chip_aec_value%[$'\r\n ']*}"
    if [[ -n "${existing_chip_aec_line}" && "${existing_chip_aec_value}" != "1" ]]; then
        want_chip_aec_150="0"
        want_chip_aec_210="0"
    fi

    local want_profile="custom"
    if [[ "${want_chip_aec_150}" == "1" || "${want_chip_aec_210}" == "1" ]]; then
        # Extra chip beams are advanced opt-ins. Named chip-AEC profiles reset
        # them to the one-detector default, so legacy installs carrying
        # hand-set beam device vars must migrate to custom to preserve intent.
        want_profile="custom"
    elif [[ "${want_chip_aec}" == "1" ]]; then
        want_profile="xvf_chip_aec"
    elif [[ "${want_raw}" == "1" && "${want_dtln}" == "0" ]]; then
        want_profile="xvf_software_aec3"
    fi

    touch "${wizard_env}"
    chmod 0644 "${wizard_env}"

    if ! grep -qE '^JASPER_AUDIO_INPUT_PROFILE=' "${wizard_env}"; then
        echo "JASPER_AUDIO_INPUT_PROFILE=${want_profile}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_AUDIO_INPUT_PROFILE=${want_profile}"
        echo "    from prior low-level wake/AEC leg vars"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_RAW=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_RAW=${want_raw}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_RAW=${want_raw}"
        echo "    from prior JASPER_MIC_DEVICE_RAW=${raw_value:-<unset>}"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_DTLN=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_DTLN=${want_dtln}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_DTLN=${want_dtln}"
        echo "    from prior JASPER_MIC_DEVICE_DTLN=${dtln_value:-<unset>}, JASPER_AEC_DTLN_ENABLED=${dtln_enabled_value:-<unset>}"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_CHIP_AEC=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_CHIP_AEC=${want_chip_aec}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_CHIP_AEC=${want_chip_aec}"
        echo "    from prior JASPER_MIC_DEVICE_CHIP_AEC_150=${chip_150_value:-<unset>}, _210=${chip_210_value:-<unset>}, JASPER_AEC_CHIP_AEC_ENABLED=${chip_enabled_value:-<unset>}"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_CHIP_AEC_150=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_CHIP_AEC_150=${want_chip_aec_150}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_CHIP_AEC_150=${want_chip_aec_150}"
        echo "    from prior JASPER_MIC_DEVICE_CHIP_AEC_150=${chip_150_value:-<unset>}"
    fi
    if ! grep -qE '^JASPER_WAKE_LEG_CHIP_AEC_210=' "${wizard_env}"; then
        echo "JASPER_WAKE_LEG_CHIP_AEC_210=${want_chip_aec_210}" >> "${wizard_env}"
        echo "  migrate_wake_legs_config: set JASPER_WAKE_LEG_CHIP_AEC_210=${want_chip_aec_210}"
        echo "    from prior JASPER_MIC_DEVICE_CHIP_AEC_210=${chip_210_value:-<unset>}"
    fi
}

# Migrate the supported legacy provider keys, Google Routes travel-mode
# default, and city-pack selection from /etc/jasper/jasper.env into the
# wizard-owned /var/lib/jasper/transit.env. Wizard-only geocoding fields have
# no legacy jasper.env migration path. The wizard file remains the runtime
# source of truth because services load it after jasper.env.
#
# Idempotent. Safe on fresh installs (no-op) and on long-lived ones
# (already-migrated keys just clean up the jasper.env residue).
migrate_transit_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/transit.env"

    local keys=(
        JASPER_SUBWAY_STATION_ID
        JASPER_SUBWAY_DEFAULT_DIRECTION
        JASPER_MTA_BUSTIME_KEY
        JASPER_BUS_STOPS
        JASPER_CITIBIKE_STATIONS
        JASPER_CITIBIKE_EBIKE_ONLY
        JASPER_TRAVEL_DEFAULT_MODE
    )

    [[ -f "${jasper_env}" ]] || return 0

    ensure_state_dir

    local k line stale_value
    for k in "${keys[@]}"; do
        line=$(grep -E "^${k}=" "${jasper_env}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#${k}=}"
        # Trim ONLY CR/LF — NOT spaces. JASPER_BUS_STOPS labels
        # contain spaces (e.g. "39 ST/4 AV SE"); a `%[ \t\r\n]*`
        # glob would shred them at the first space.
        stale_value="${stale_value%$'\r'}"
        stale_value="${stale_value%$'\n'}"

        if [[ -f "${wizard_env}" ]] && grep -qE "^${k}=" "${wizard_env}"; then
            sed -i.bak "/^${k}=/d" "${jasper_env}"
            rm -f "${jasper_env}.bak"
            echo "  migrate_transit_config: removed stale ${k} line from ${jasper_env}"
            continue
        fi

        if [[ -n "${stale_value}" ]]; then
            touch "${wizard_env}"
            chmod 0640 "${wizard_env}"
            echo "${k}=${stale_value}" >> "${wizard_env}"
            echo "  migrate_transit_config: moved ${k}=${stale_value}"
            echo "    from ${jasper_env} to ${wizard_env}"
        fi
        sed -i.bak "/^${k}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
    done

    # Migrate an operator-set JASPER_TRANSIT_CITIES out of jasper.env. It's the
    # pack-level toggle — NOT a provider env key, so deliberately not in the
    # keys=() loop above (which is a superset of transit.all_env_keys() because
    # it also carries Google Routes' JASPER_TRAVEL_DEFAULT_MODE). The daemon
    # reads it via os.environ so it works in either file, but leaving it in
    # jasper.env shadows the wizard, which reads transit.env and would render
    # the wrong toggle. Migrate even an EMPTY value: present-empty means "no
    # cities", which must be preserved (dropping it would read as absent -> all
    # packs). Runs before the seed below, so a migrated value makes the seed
    # skip. Mirrors the per-key loop's "wizard value wins" precedence.
    if grep -qE "^JASPER_TRANSIT_CITIES=" "${jasper_env}"; then
        local cities_value
        cities_value=$(grep -E "^JASPER_TRANSIT_CITIES=" "${jasper_env}" | tail -n1)
        cities_value="${cities_value#JASPER_TRANSIT_CITIES=}"
        cities_value="${cities_value%$'\r'}"
        cities_value="${cities_value%$'\n'}"
        if [[ -f "${wizard_env}" ]] && grep -qE "^JASPER_TRANSIT_CITIES=" "${wizard_env}"; then
            echo "  migrate_transit_config: removed stale JASPER_TRANSIT_CITIES" \
                 "from ${jasper_env} (wizard value wins)"
        else
            touch "${wizard_env}"
            chmod 0640 "${wizard_env}"
            echo "JASPER_TRANSIT_CITIES=${cities_value}" >> "${wizard_env}"
            echo "  migrate_transit_config: moved JASPER_TRANSIT_CITIES=${cities_value}"
            echo "    from ${jasper_env} to ${wizard_env}"
        fi
        sed -i.bak "/^JASPER_TRANSIT_CITIES=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
    fi

    # Seed the city-pack toggle for existing households. JASPER_TRANSIT_CITIES
    # (comma-separated CityPack ids, wizard-owned) gates which city's transit
    # providers are eligible. It is intentionally optional — jasper.transit's
    # enabled_pack_ids() treats "unset" as "all packs", so installs predating
    # the toggle keep working untouched — but seeding an explicit value when
    # the household ALREADY uses NYC transit (a) makes the /transit/ wizard
    # render the right toggle state and (b) follows the codify-don't-memorise
    # rule. Only the NYC pack ships today, so "nyc" is the only value to seed.
    # Idempotent: never overwrites an explicit (wizard-written) value, and
    # never presumes transit for a household that has configured none.
    if [[ -f "${wizard_env}" ]] && ! grep -qE "^JASPER_TRANSIT_CITIES=" "${wizard_env}"; then
        local cfgkey has_nyc_transit=""
        for cfgkey in JASPER_SUBWAY_STATION_ID JASPER_BUS_STOPS JASPER_CITIBIKE_STATIONS; do
            # A non-empty value (`.+` after `=`) means that NYC mode is set up.
            if grep -qE "^${cfgkey}=.+" "${wizard_env}"; then
                has_nyc_transit=1
                break
            fi
        done
        if [[ -n "${has_nyc_transit}" ]]; then
            echo "JASPER_TRANSIT_CITIES=nyc" >> "${wizard_env}"
            echo "  migrate_transit_config: seeded JASPER_TRANSIT_CITIES=nyc"
            echo "    (existing NYC transit detected; explicit city-pack toggle)"
        fi
    fi
}

# Migrate stale multi-room grouping env vars from /etc/jasper/jasper.env
# into the wizard-owned /var/lib/jasper/grouping.env. The /grouping
# wizard owns JASPER_GROUPING_* config; an operator who pastes the core
# bootstrap subset below into jasper.env (CI bootstrap, headless imaging,
# SSH-driven setup) gets them moved automatically so the wizard file wins
# on conflicts — like transit/weather. This is NOT the full key set
# jasper.multiroom.config reads (it also parses ROSTER, PEER_ADDR/NAME,
# TRIM_DB, CLIENT_LATENCY_MS, LEFT/RIGHT_DELAY_MS, CROSSOVER_HZ,
# MAINS_HIGHPASS, SUBWOOFER_PRESENT); jasper.multiroom.config.load_config
# parses only grouping.env — never jasper.env, never the process
# environment (confirmed: jasper-grouping-reconcile.service stopped
# loading grouping.env as an EnvironmentFile at c3ea20e1b) — so an
# unmigrated hand-set value among those is silently INERT, not merely
# uncleaned: it never reaches the daemon at all.
#
# Grouping is OFF BY DEFAULT on a solo speaker: absence of grouping.env
# means off (jasper.multiroom.config fail-safes to enabled=False). So we
# only create the file when an operator actually referenced a grouping
# key — a fresh solo install never grows the file, and this NEVER enables
# any unit (the reconciler does that on explicit opt-in).
#
# Idempotent. Safe on fresh installs (no-op) and on long-lived ones
# (already-migrated keys just clean up the jasper.env residue).
migrate_grouping() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/grouping.env"

    # Mirror jasper.multiroom.config's env keys. Duplicated here because
    # install.sh runs before the venv Python is guaranteed importable.
    local keys=(
        JASPER_GROUPING
        JASPER_GROUPING_ROLE
        JASPER_GROUPING_CHANNEL
        JASPER_GROUPING_BOND_ID
        JASPER_GROUPING_LEADER_ADDR
        JASPER_GROUPING_BUFFER_MS
        JASPER_GROUPING_CODEC
    )

    [[ -f "${jasper_env}" ]] || return 0

    ensure_state_dir

    local k line stale_value
    for k in "${keys[@]}"; do
        line=$(grep -E "^${k}=" "${jasper_env}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#${k}=}"
        stale_value="${stale_value%$'\r'}"
        stale_value="${stale_value%$'\n'}"

        if [[ -f "${wizard_env}" ]] && grep -qE "^${k}=" "${wizard_env}"; then
            sed -i.bak "/^${k}=/d" "${jasper_env}"
            rm -f "${jasper_env}.bak"
            echo "  migrate_grouping: removed stale ${k} line from ${jasper_env}"
            continue
        fi

        if [[ -n "${stale_value}" ]]; then
            touch "${wizard_env}"
            chmod 0644 "${wizard_env}"
            echo "${k}=${stale_value}" >> "${wizard_env}"
            echo "  migrate_grouping: moved ${k}=${stale_value}"
            echo "    from ${jasper_env} to ${wizard_env}"
        fi
        sed -i.bak "/^${k}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
    done
}

# Migrate stale weather env vars from /etc/jasper/jasper.env into the
# wizard-owned /var/lib/jasper/weather.env, and seed missing weather /
# transit coordinates from each other. Weather and transit remain
# separate after seeding: if both files already have coordinates, this
# helper leaves both alone.
migrate_weather_config() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local weather_env="${STATE_DIR}/weather.env"
    local transit_env="${STATE_DIR}/transit.env"

    local keys=(
        JASPER_DEFAULT_LOCATION
        JASPER_WEATHER_LAT
        JASPER_WEATHER_LON
        JASPER_WEATHER_DISPLAY_NAME
        JASPER_WEATHER_UNITS
    )

    [[ -f "${jasper_env}" ]] || return 0

    ensure_state_dir

    local k line stale_value
    for k in "${keys[@]}"; do
        line=$(grep -E "^${k}=" "${jasper_env}" || true)
        [[ -z "${line}" ]] && continue
        stale_value="${line#${k}=}"
        stale_value="${stale_value%$'\r'}"
        stale_value="${stale_value%$'\n'}"

        if [[ -f "${weather_env}" ]] && grep -qE "^${k}=" "${weather_env}"; then
            sed -i.bak "/^${k}=/d" "${jasper_env}"
            rm -f "${jasper_env}.bak"
            echo "  migrate_weather_config: removed stale ${k} line from ${jasper_env}"
            continue
        fi

        if [[ -n "${stale_value}" ]]; then
            touch "${weather_env}"
            chmod 0640 "${weather_env}"
            echo "${k}=${stale_value}" >> "${weather_env}"
            echo "  migrate_weather_config: moved ${k}=${stale_value}"
            echo "    from ${jasper_env} to ${weather_env}"
        fi
        sed -i.bak "/^${k}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
    done

    local weather_lat weather_lon weather_display weather_default
    local transit_lat transit_lon transit_display
    weather_lat=$(grep -E '^JASPER_WEATHER_LAT=' "${weather_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    weather_lon=$(grep -E '^JASPER_WEATHER_LON=' "${weather_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    weather_display=$(grep -E '^JASPER_WEATHER_DISPLAY_NAME=' "${weather_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    weather_default=$(grep -E '^JASPER_DEFAULT_LOCATION=' "${weather_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    transit_lat=$(grep -E '^JASPER_TRANSIT_LAT=' "${transit_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    transit_lon=$(grep -E '^JASPER_TRANSIT_LON=' "${transit_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)
    transit_display=$(grep -E '^JASPER_TRANSIT_DISPLAY_NAME=' "${transit_env}" 2>/dev/null | tail -n1 | cut -d= -f2- || true)

    if [[ -z "${weather_lat}" && -z "${weather_lon}" && -n "${transit_lat}" && -n "${transit_lon}" ]]; then
        touch "${weather_env}"
        chmod 0640 "${weather_env}"
        echo "JASPER_WEATHER_LAT=${transit_lat}" >> "${weather_env}"
        echo "JASPER_WEATHER_LON=${transit_lon}" >> "${weather_env}"
        if [[ -n "${transit_display}" && -z "${weather_display}" ]]; then
            echo "JASPER_WEATHER_DISPLAY_NAME=${transit_display}" >> "${weather_env}"
        fi
        if [[ -n "${transit_display}" && -z "${weather_default}" ]]; then
            echo "JASPER_DEFAULT_LOCATION=${transit_display}" >> "${weather_env}"
        fi
        echo "  migrate_weather_config: seeded weather location from transit.env"
    fi

    if [[ -z "${transit_lat}" && -z "${transit_lon}" && -n "${weather_lat}" && -n "${weather_lon}" ]]; then
        touch "${transit_env}"
        chmod 0640 "${transit_env}"
        echo "JASPER_TRANSIT_LAT=${weather_lat}" >> "${transit_env}"
        echo "JASPER_TRANSIT_LON=${weather_lon}" >> "${transit_env}"
        if [[ -n "${weather_display}" ]]; then
            echo "JASPER_TRANSIT_DISPLAY_NAME=${weather_display}" >> "${transit_env}"
        elif [[ -n "${weather_default}" ]]; then
            echo "JASPER_TRANSIT_DISPLAY_NAME=${weather_default}" >> "${transit_env}"
        fi
        echo "  migrate_weather_config: seeded transit location from weather.env"
    fi
}

# Move JASPER_FANIN_CAMILLA_COUPLING out of jasper.env into the
# reconciler-owned /var/lib/jasper/fanin.env. The coupling reconciler
# (jasper.fanin.coupling_reconcile) is the single writer of this key in
# fanin.env (the same file jasper-fanin + jasper-mux load, fanin.env
# winning over the unit Environment= defaults). During the experimental
# phase the flag may have been hand-set in jasper.env; this relocates it
# so there is one owner and no shadowing. Same shape as the other
# wizard-file relocations (migrate_transit_config, migrate_grouping): move
# the value only when the wizard file does not already declare it, then
# strip the jasper.env line either way.
# fanin.env carries no secrets (buffer frames + coupling mode), so 0644.
migrate_fanin_coupling() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/fanin.env"

    [[ -f "${jasper_env}" ]] || return 0
    local line
    line=$(grep -E '^JASPER_FANIN_CAMILLA_COUPLING=' "${jasper_env}" || true)
    [[ -z "${line}" ]] && return 0

    local stale_value="${line#JASPER_FANIN_CAMILLA_COUPLING=}"
    stale_value="${stale_value%[$'\r\n ']*}"

    ensure_state_dir

    if [[ -f "${wizard_env}" ]] && grep -qE '^JASPER_FANIN_CAMILLA_COUPLING=' "${wizard_env}"; then
        sed -i.bak '/^JASPER_FANIN_CAMILLA_COUPLING=/d' "${jasper_env}"
        rm -f "${jasper_env}.bak"
        echo "  migrate_fanin_coupling: removed stale JASPER_FANIN_CAMILLA_COUPLING"
        echo "    line from ${jasper_env} (wizard file already canonical)"
        return 0
    fi

    if [[ -n "${stale_value}" ]]; then
        touch "${wizard_env}"
        chmod 0644 "${wizard_env}"
        echo "JASPER_FANIN_CAMILLA_COUPLING=${stale_value}" >> "${wizard_env}"
        echo "  migrate_fanin_coupling: moved JASPER_FANIN_CAMILLA_COUPLING=${stale_value}"
        echo "    from ${jasper_env} to ${wizard_env}"
    fi
    sed -i.bak '/^JASPER_FANIN_CAMILLA_COUPLING=/d' "${jasper_env}"
    rm -f "${jasper_env}.bak"
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
# profile if no stash exists yet. This is the migration hook for the
# WiFi profile guardian (docs/HANDOFF-resilience.md "Hardware-event
# recovery" sidebar) — it covers the SSH-driven setup case where the
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

# Drop the seeded-default `JASPER_CONTROL_HOST=0.0.0.0` line from
# /etc/jasper/jasper.env. The var is the control server's *bind*
# address, and 0.0.0.0 is already the server-side code default
# (jasper/control/server.py), so the seeded line adds nothing — but
# jasper.control.client used to misread it as its *connect* host,
# sending `Host: 0.0.0.0:8780`, which the management-host guard
# rejects: the 2026-06-11 regression where every /system/ dashboard
# poll 403ed on Pis seeded with the line. The client now maps
# unspecified → loopback, so the line is harmless going forward; prune
# it anyway so the frozen first-install seed stops shadowing the code
# default (the HEADROOM bug class). Any value other than exactly
# `0.0.0.0` is an operator's deliberate bind override — left alone.
migrate_control_host_bind_seed() {
    local jasper_env="${ENV_DIR}/jasper.env"
    [[ -f "${jasper_env}" ]] || return 0
    local line value
    line=$(grep -E '^JASPER_CONTROL_HOST=' "${jasper_env}" 2>/dev/null || true)
    [[ -z "${line}" ]] && return 0
    value="${line#JASPER_CONTROL_HOST=}"
    value="${value%$'\r'}"
    if [[ "${value}" != "0.0.0.0" ]]; then
        return 0
    fi
    if ! sed -i.bak '/^JASPER_CONTROL_HOST=/d' "${jasper_env}" 2>/dev/null; then
        rm -f "${jasper_env}.bak"
        echo "  migrate_control_host_bind_seed: could not update ${jasper_env} (left unchanged)"
        return 0
    fi
    rm -f "${jasper_env}.bak"
    echo "  migrate_control_host_bind_seed: removed seeded JASPER_CONTROL_HOST=0.0.0.0"
    echo "    (server bind default is already 0.0.0.0; on-Pi clients connect via loopback)"
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
    #     config the /state sound card reads). These carry no secret.
    # NOTE: the WiFi guardian PSK stash is DELIBERATELY NOT widened here — it
    # holds the WiFi password, which jasper-control does not need the value of
    # (only the SSID, which it derives from nmcli/the journal), so it stays
    # owner-only 0600. Least privilege over blanket widening. See
    # docs/HANDOFF-privilege-separation.md.
    #
    # WS1 Phase 4a/4b — google_credentials.env moved to jasper-secrets, while
    # spotify_credentials.env + home_assistant.env moved to jasper-intsecrets.
    # Those compartment migrations own their perms now. voice_provider.env stays
    # here (now keyless; control reads the provider name for /system/).
    local f path
    for f in voice_provider.env control_token household_secret \
             sound_profile.json sound_settings.json; do
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
