#!/usr/bin/env bash
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

# WS1 Phase 4a — move the high-value secrets OUT of the broad /var/lib/jasper
# StateDirectory into the jasper-secrets compartment. Guarded + idempotent: each
# move runs only when the old path exists and the new one does not, so a re-run
# (or a fresh install) is a no-op. mv on the same filesystem is an atomic rename
# — no token loss. See docs/HANDOFF-privilege-separation.md "Phase 4".
migrate_secrets_phase4a() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    ensure_secrets_dir

    local old_google="${STATE_DIR}/google"
    local new_google="${SECRETS_DIR}/google"
    if [[ -d "${old_google}" && ! -e "${new_google}" ]]; then
        mv "${old_google}" "${new_google}"
        # accounts.json bakes ABSOLUTE token_path values; rewrite the prefix so
        # google_creds.load_credentials() finds the moved per-account tokens.
        if [[ -f "${new_google}/accounts.json" ]]; then
            sed -i.bak "s#${STATE_DIR}/google/#${SECRETS_DIR}/google/#g" \
                "${new_google}/accounts.json"
            rm -f "${new_google}/accounts.json.bak"
        fi
        echo "  migrate_secrets_phase4a: moved Google token tree -> ${new_google}"
    fi

    local old_creds="${STATE_DIR}/google_credentials.env"
    local new_creds="${SECRETS_DIR}/google_credentials.env"
    if [[ -f "${old_creds}" && ! -e "${new_creds}" ]]; then
        mv "${old_creds}" "${new_creds}"
        echo "  migrate_secrets_phase4a: moved google_credentials.env -> ${SECRETS_DIR}"
    fi

    # (Re-)assert the tree layout + perms at the new location. mv preserves the
    # moved files' OLD owner:group (jasper-voice:jasper from the StateDirectory)
    # — setgid only affects NEW files — so an explicit recursive chown to
    # root:jasper-secrets is required (owner root = rollback-safe + matches the
    # tmpfiles spec; group-read is the access path). install -d is idempotent
    # (fresh install: just creates the subdirs).
    install -d -m 2770 -g jasper-secrets "${new_google}" "${new_google}/tokens"
    chown -R root:jasper-secrets "${SECRETS_DIR}" 2>/dev/null || true
    chmod 0640 "${new_creds}" 2>/dev/null || true
    chmod 0640 "${new_google}/accounts.json" 2>/dev/null || true
    find "${new_google}/tokens" -type f -name '*.json' \
        -exec chmod 0640 {} + 2>/dev/null || true

    migrate_voice_keys_split

    # Reconcile against the tmpfiles spec (also surfaces a syntax error early).
    systemd-tmpfiles --create --prefix="${SECRETS_DIR}" 2>/dev/null || true
}

# WS1 Phase 4b — move Home Assistant + Spotify secrets OUT of the broad
# /var/lib/jasper StateDirectory into the jasper-intsecrets compartment.
# Spotify is read-write: voice, control, mux, and web can all refresh/persist
# spotipy token caches, so the compartment is writable by all four service
# users via systemd ReadWritePaths=. See docs/HANDOFF-privilege-separation.md.
migrate_secrets_phase4b() {
    getent group jasper-intsecrets >/dev/null 2>&1 || return 0
    ensure_intsecrets_dir

    local old_ha="${STATE_DIR}/home_assistant.env"
    local new_ha="${INTSECRETS_DIR}/home_assistant.env"
    if [[ -f "${old_ha}" && ! -e "${new_ha}" ]]; then
        mv "${old_ha}" "${new_ha}"
        echo "  migrate_secrets_phase4b: moved home_assistant.env -> ${INTSECRETS_DIR}"
    fi

    local old_spotify_creds="${STATE_DIR}/spotify_credentials.env"
    local new_spotify_creds="${INTSECRETS_DIR}/spotify_credentials.env"
    if [[ -f "${old_spotify_creds}" && ! -e "${new_spotify_creds}" ]]; then
        mv "${old_spotify_creds}" "${new_spotify_creds}"
        echo "  migrate_secrets_phase4b: moved spotify_credentials.env -> ${INTSECRETS_DIR}"
    fi

    local old_legacy_cache="${STATE_DIR}/.spotify-cache"
    local new_legacy_cache="${INTSECRETS_DIR}/.spotify-cache"
    if [[ -f "${old_legacy_cache}" && ! -e "${new_legacy_cache}" ]]; then
        mv "${old_legacy_cache}" "${new_legacy_cache}"
        echo "  migrate_secrets_phase4b: moved legacy Spotify cache -> ${new_legacy_cache}"
    fi

    local old_spotify="${STATE_DIR}/spotify"
    local new_spotify="${INTSECRETS_DIR}/spotify"
    if [[ -d "${old_spotify}" && ! -e "${new_spotify}" ]]; then
        mv "${old_spotify}" "${new_spotify}"
        echo "  migrate_secrets_phase4b: moved Spotify token tree -> ${new_spotify}"
    fi

    # accounts.json bakes ABSOLUTE cache_path values; rewrite the prefix so all
    # per-account caches remain reachable after the tree move. Run this
    # unconditionally when the moved registry exists so partial/re-run states
    # converge too.
    if [[ -f "${new_spotify}/accounts.json" ]]; then
        sed -i.bak "s#${STATE_DIR}/spotify/#${INTSECRETS_DIR}/spotify/#g" \
            "${new_spotify}/accounts.json"
        rm -f "${new_spotify}/accounts.json.bak"
    fi

    # (Re-)assert the tree layout + perms at the new location. mv preserves the
    # old StateDirectory owner:group; explicit recursive chown moves everything
    # to root:jasper-intsecrets. install -d is idempotent and also creates the
    # forward path on fresh installs.
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

# WS1 Phase 4a — split the three provider API keys out of the broad
# voice_provider.env (and any operator seed in /etc/jasper/jasper.env) into the
# group-jasper-secrets voice_keys.env. The non-secret JASPER_VOICE_PROVIDER +
# per-provider model/voice selectors stay in voice_provider.env so jasper-control
# keeps reading the active provider for /system/. Safe: never strips a key from
# the broad files until its value is confirmed written to voice_keys.env.
migrate_voice_keys_split() {
    getent group jasper-secrets >/dev/null 2>&1 || return 0
    local provider_env="${STATE_DIR}/voice_provider.env"
    local jasper_env="${ENV_DIR}/jasper.env"
    local keys_env="${SECRETS_DIR}/voice_keys.env"
    local key line val moved=0

    for key in GEMINI_API_KEY OPENAI_API_KEY XAI_API_KEY; do
        # Already split out (e.g. the wizard wrote it post-4a)? Just clean any
        # stale copy left on the broad files.
        if [[ -f "${keys_env}" ]] && grep -qE "^${key}=" "${keys_env}"; then
            _strip_key_from_broad "${key}" "${provider_env}" "${jasper_env}"
            continue
        fi
        # Find the value: wizard file first, then operator seed in jasper.env.
        val=""
        if [[ -f "${provider_env}" ]]; then
            line=$(grep -E "^${key}=" "${provider_env}" || true)
            val="${line#"${key}"=}"
        fi
        if [[ -z "${val}" && -f "${jasper_env}" ]]; then
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
            _strip_key_from_broad "${key}" "${provider_env}" "${jasper_env}"
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
    local key="$1" provider_env="$2" jasper_env="$3"
    if [[ -f "${provider_env}" ]]; then
        sed -i.bak "/^${key}=/d" "${provider_env}"
        rm -f "${provider_env}.bak"
    fi
    if [[ -f "${jasper_env}" ]]; then
        sed -i.bak "/^${key}=/d" "${jasper_env}"
        rm -f "${jasper_env}.bak"
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
# /var/lib/jasper/aec_mode.env. The /system "Wake detection" card
# owns these as booleans (JASPER_WAKE_LEG_RAW, _DTLN); the
# reconciler maps them back to the underlying device/enable vars
# the bridge + voice each read at startup.
#
# Previously AGENTS.md instructed operators to paste raw lines into
# /etc/jasper/jasper.env for opt-in legs:
#   JASPER_MIC_DEVICE_RAW=udp:9877        (dual-stream)
#   JASPER_MIC_DEVICE_DTLN=udp:9878       (triple-stream extras)
#   JASPER_AEC_DTLN_ENABLED=1
# This function preserves an operator's prior intent on upgrade by
# translating those values into the new boolean form, then strips
# the underlying vars so the reconciler is the only writer going
# forward. Fresh installs (no underlying vars set) are a no-op here
# — the new defaults seeded in reconcile_aec_state take effect
# (RAW=1, DTLN=0).
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

    # Chip-AEC beams: a hand-set chip device var OR the enabled flag is
    # enough signal to preserve intent. New in the chip-AEC promotion;
    # almost always a no-op (nobody hand-set these before), but mirrors
    # the raw/DTLN translation so the reconciler stays the sole writer.
    local want_chip_aec="0"
    if [[ -n "${chip_150_value}" || -n "${chip_210_value}" \
          || "${chip_enabled_value}" == "1" ]]; then
        want_chip_aec="1"
    fi

    local want_profile="custom"
    if [[ "${want_chip_aec}" == "1" ]]; then
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

    sed -i.bak '/^JASPER_MIC_DEVICE_RAW=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_MIC_DEVICE_DTLN=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_AEC_DTLN_ENABLED=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_MIC_DEVICE_CHIP_AEC_150=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_MIC_DEVICE_CHIP_AEC_210=/d' "${jasper_env}"
    sed -i.bak '/^JASPER_AEC_CHIP_AEC_ENABLED=/d' "${jasper_env}"
    rm -f "${jasper_env}.bak"
}

# Migrate the old OpenAI Realtime template default from far_field to auto.
#
# The far_field line originally shipped as part of the server-VAD/music
# experiment. Server VAD was later demoted back to opt-in, but existing
# installs may still carry the provider-side denoising default in
# /etc/jasper/jasper.env. Auto lets voice resolve provider preprocessing
# from the active input contract, while still allowing an operator to set
# far_field explicitly after migration if their custom raw-mic path wants it.
migrate_openai_noise_reduction_default() {
    local jasper_env="${ENV_DIR}/jasper.env"
    [[ -f "${jasper_env}" ]] || return 0
    if grep -qE '^JASPER_OPENAI_NOISE_REDUCTION=far_field$' "${jasper_env}"; then
        sed -i.bak \
            's/^JASPER_OPENAI_NOISE_REDUCTION=far_field$/JASPER_OPENAI_NOISE_REDUCTION=auto/' \
            "${jasper_env}"
        rm -f "${jasper_env}.bak"
        chmod 0640 "${jasper_env}"
        echo "  migrate_openai_noise_reduction_default: set JASPER_OPENAI_NOISE_REDUCTION=auto"
    fi
}

# Migrate the first outputd-cutover TTS socket default to the current
# fan-in-owned TTS socket. Existing installs that still carry the old
# /run/jasper-outputd/tts.sock value shadow jasper-voice.service's
# packaged Environment= line and make voice restart-loop, because
# outputd no longer owns a TTS IPC socket.
migrate_tts_outputd_socket_default() {
    local jasper_env="${ENV_DIR}/jasper.env"
    [[ -f "${jasper_env}" ]] || return 0
    if grep -qE '^JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts\.sock$' "${jasper_env}"; then
        sed -i.bak \
            's|^JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts\.sock$|JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock|' \
            "${jasper_env}"
        rm -f "${jasper_env}.bak"
        chmod 0640 "${jasper_env}"
        echo "  migrate_tts_outputd_socket_default: set JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock"
    fi
}

# Migrate stale transit env vars from /etc/jasper/jasper.env into the
# wizard-owned /var/lib/jasper/transit.env. The wizard at /transit
# owns every transit env variable; operators who paste those into
# jasper.env (CI bootstrap, headless imaging, SSH-driven setup) get
# them moved automatically so the wizard's file stays the single
# source of truth.
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
    # keys=() loop above (which mirrors transit.all_env_keys()). The daemon
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
# wizard (and jasper.multiroom.config) owns every JASPER_GROUPING_* key;
# an operator who pastes them into jasper.env (CI bootstrap, headless
# imaging, SSH-driven setup) gets them moved automatically so the wizard
# file stays the single source of truth — exactly like transit/weather.
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
        JASPER_GROUPING_ENABLED
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

# Seed the speaker's room label into the speaker-identity home
# (/var/lib/jasper/speaker_name.env, JASPER_SPEAKER_ROOM) from the
# legacy peering room (/var/lib/jasper/peering.env, JASPER_PEER_ROOM)
# so an existing household keeps its room label in the identity home where
# /rooms and control_advert now read it. One-time, non-destructive:
#
#   - If speaker_name.env already has a NON-EMPTY JASPER_SPEAKER_ROOM,
#     leave it untouched (don't overwrite an operator-set room).
#   - Otherwise (line absent OR present-but-empty), copy the explicit
#     JASPER_PEER_ROOM value from peering.env, if any.
#   - If peering.env has no explicit room (auto-derived default), do
#     nothing — the identity reader's legacy peering fallback keeps
#     /rooms consistent at runtime, so there is nothing to persist.
#
# SCOPE: JASPER_PEER_ROOM remains a data-compatibility fallback for older
# peering.env files; this only mirrors the value into the identity home.
#
# Fail-soft: any read/write hiccup is a warn-and-continue, never an
# install failure. Idempotent — a second run finds the room already set
# and no-ops. The value is written quoted to match the format
# jasper.speaker_name.write_state emits and read_state parses.
migrate_speaker_room() {
    local speaker_env="${STATE_DIR}/speaker_name.env"
    local peering_env="${STATE_DIR}/peering.env"

    # Nothing to seed into if the identity file isn't there yet. The
    # fresh-install seed in seed_env_defaults creates it before this
    # runs, so this guard only fires on an odd partial state.
    [[ -f "${speaker_env}" ]] || return 0

    # Already set (non-empty) -> respect the operator's choice, no-op.
    local cur_line cur_room
    cur_line=$(grep -E '^JASPER_SPEAKER_ROOM=' "${speaker_env}" 2>/dev/null || true)
    if [[ -n "${cur_line}" ]]; then
        cur_room="${cur_line#JASPER_SPEAKER_ROOM=}"
        cur_room="${cur_room%$'\r'}"
        cur_room="${cur_room%$'\n'}"
        # Strip surrounding double quotes (write_state quotes the value).
        cur_room="${cur_room#\"}"
        cur_room="${cur_room%\"}"
        if [[ -n "${cur_room}" ]]; then
            return 0
        fi
    fi

    # No legacy peering room to carry over -> no-op.
    [[ -f "${peering_env}" ]] || return 0
    local peer_line peer_room
    peer_line=$(grep -E '^JASPER_PEER_ROOM=' "${peering_env}" 2>/dev/null || true)
    [[ -z "${peer_line}" ]] && return 0
    peer_room="${peer_line#JASPER_PEER_ROOM=}"
    peer_room="${peer_room%$'\r'}"
    peer_room="${peer_room%$'\n'}"
    # peering.env writes the value bare, but tolerate quotes defensively.
    peer_room="${peer_room#\"}"
    peer_room="${peer_room%\"}"
    [[ -z "${peer_room}" ]] && return 0

    # Replace any present-but-empty room line, then append the seeded
    # value so a stale `JASPER_SPEAKER_ROOM=""` doesn't leave a duplicate.
    if ! sed -i.bak '/^JASPER_SPEAKER_ROOM=/d' "${speaker_env}" 2>/dev/null; then
        rm -f "${speaker_env}.bak"
        echo "  migrate_speaker_room: could not update ${speaker_env} (left unchanged)"
        return 0
    fi
    rm -f "${speaker_env}.bak"
    printf 'JASPER_SPEAKER_ROOM="%s"\n' "${peer_room}" >> "${speaker_env}"
    chmod 0644 "${speaker_env}" 2>/dev/null || true
    echo "  migrate_speaker_room: seeded JASPER_SPEAKER_ROOM=${peer_room}"
    echo "    into ${speaker_env} from ${peering_env}"
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

# Migrate stale JASPER_VOICE_PROVIDER from /etc/jasper/jasper.env to
# /var/lib/jasper/voice_provider.env. The wizard at /voice owns this
# variable; previously the install template also set a default
# (JASPER_VOICE_PROVIDER=gemini), which created stale-vs-runtime
# confusion when the wizard had written a different value.
#
# This function:
#  - reads any JASPER_VOICE_PROVIDER= line out of /etc/jasper/jasper.env
#  - if the wizard file (/var/lib/jasper/voice_provider.env) doesn't
#    already define the variable, moves the value there
#  - removes the line from /etc/jasper/jasper.env either way
#
# Idempotent: running multiple times produces the same end state.
# Safe on fresh installs (where neither file has the var, this is a
# no-op) and on long-lived installs (where the wizard file already
# has the var, this just cleans up the stale line).
migrate_voice_provider() {
    local jasper_env="${ENV_DIR}/jasper.env"
    local wizard_env="${STATE_DIR}/voice_provider.env"

    [[ -f "${jasper_env}" ]] || return 0
    local line
    line=$(grep -E '^JASPER_VOICE_PROVIDER=' "${jasper_env}" || true)
    [[ -z "${line}" ]] && return 0

    # value is everything after the first '='. Trim trailing CR/whitespace.
    local stale_value="${line#JASPER_VOICE_PROVIDER=}"
    stale_value="${stale_value%[$'\r\n ']*}"

    ensure_state_dir

    # If wizard file already declares the variable, just remove the
    # stale jasper.env line — the wizard's value wins per systemd's
    # EnvironmentFile load order regardless, this is just cleanup.
    if [[ -f "${wizard_env}" ]] && grep -qE '^JASPER_VOICE_PROVIDER=' "${wizard_env}"; then
        sed -i.bak '/^JASPER_VOICE_PROVIDER=/d' "${jasper_env}"
        rm -f "${jasper_env}.bak"
        echo "  migrate_voice_provider: removed stale JASPER_VOICE_PROVIDER"
        echo "    line from ${jasper_env} (wizard file already canonical)"
        return 0
    fi

    # Migrate the value to the wizard file. Empty stale value (we just
    # introduced this on a clean install) → don't write anything, just
    # remove the line from jasper.env. Non-empty → preserve the
    # operator's pre-cleanup choice so voice keeps working.
    if [[ -n "${stale_value}" ]]; then
        touch "${wizard_env}"
        # WS1 Phase 3b-2: 0640 (was 0600) so the now-non-root jasper-control +
        # the jasper-doctor it spawns (group jasper) can read voice_provider.env.
        # widen_control_secret_env_modes re-asserts group jasper on this file.
        chmod 0640 "${wizard_env}"
        echo "JASPER_VOICE_PROVIDER=${stale_value}" >> "${wizard_env}"
        echo "  migrate_voice_provider: moved JASPER_VOICE_PROVIDER=${stale_value}"
        echo "    from ${jasper_env} to ${wizard_env}"
    fi
    sed -i.bak '/^JASPER_VOICE_PROVIDER=/d' "${jasper_env}"
    rm -f "${jasper_env}.bak"
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
retire_audio_topology_switch() {
    # Fan-in is now the only supported renderer topology. Older builds
    # persisted mutable topology intent in /var/lib/jasper/audio_topology.env
    # and could leave that file saying `fanin` while install_alsa had just
    # re-rendered a dmix-era /etc/asound.conf. Remove the stale state and
    # backup files so deploy has one source of truth again: the shipped
    # fan-in asoundrc plus fixed renderer unit devices.
    local state="${STATE_DIR}/audio_topology.env"
    if [[ -f "${state}" ]]; then
        cp "${state}" "${state}.retired.$(date +%s)"
        rm -f "${state}"
        echo "  retire_audio_topology_switch: removed stale ${state} (backup kept with .retired.* suffix)"
    fi
    rm -f /etc/asound.conf.dmix-mode-backup
}

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
        chgrp jasper "${jasper_env}" 2>/dev/null || true
        chmod 0640 "${jasper_env}" 2>/dev/null || true
    fi

    # The wizard-written secret files (under /var/lib/jasper, already group
    # jasper via StateDirectory) only need the group-read MODE bit. control_token
    # is the Phase-2 mandatory gate: jasper-control reads it to verify, and
    # jasper-web embeds it via canonical_page() — _stored_token() FAILS SAFE to
    # gate-OFF on EACCES, so an unreadable token would SILENTLY DISABLE the gate.
    # It is owned jasper-voice (StateDirectory chown), so 0640 group read is the
    # only way the non-root jasper-control can read its own token.
    #
    # Two file classes, both read off disk by jasper-control's /state +
    # /system/diagnostics and so needing GROUP read (0640):
    #   - env/control: voice_provider.env (now keyless) + control_token.
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
    local f
    for f in voice_provider.env control_token \
             sound_profile.json sound_settings.json; do
        if [[ -f "${STATE_DIR}/${f}" ]]; then
            chgrp jasper "${STATE_DIR}/${f}" 2>/dev/null || true
            chmod 0640 "${STATE_DIR}/${f}" 2>/dev/null || true
        fi
    done
    echo "  widen_control_secret_env_modes: config jasper-control reads is group-jasper readable (0640)"
}
