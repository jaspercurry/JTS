#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Python runtime install steps for deploy/install.sh.
#
# Extracted from install.sh; functions assume install.sh globals and
# set -euo pipefail from the sourcing shell.

seed_capture_relay_env() {
    # Existing boxes have a frozen first-install jasper.env. Add the relay keys
    # if they predate the relay rollout, using the same public Jasper Tech relay
    # defaults as .env.example. This helper is shared by the full and streambox
    # profiles so every correction surface gets the same transport contract.
    # The relay exists because mobile browsers require a publicly trusted HTTPS
    # secure context for getUserMedia; the phone records on capture.jasper.tech
    # while the LAN-only Pi pulls encrypted blobs over outbound HTTPS.
    # Self-hosters can deploy the same Cloudflare code from relay/ and
    # capture-page/ (see their README files) and override these via deploy env or
    # by editing /etc/jasper/jasper.env. Existing non-empty custom values are
    # preserved. To keep the on-Pi fallback, set
    # JASPER_CAPTURE_RELAY_BASE=disabled (or off/0/none).
    if [[ -n "${JASPER_CAPTURE_RELAY_BASE:-}" ]]; then
        set_jasper_env_value JASPER_CAPTURE_RELAY_BASE "${JASPER_CAPTURE_RELAY_BASE}"
        echo "  capture relay: configured from deploy environment"
    elif ! grep -qE '^JASPER_CAPTURE_RELAY_BASE=[[:space:]]*[^[:space:]]' "${ENV_DIR}/jasper.env"; then
        set_jasper_env_value JASPER_CAPTURE_RELAY_BASE "https://relay.jasper.tech"
        echo "  capture relay: using Jasper Tech public relay"
    fi
    if [[ -n "${JASPER_CAPTURE_ORIGIN:-}" ]]; then
        set_jasper_env_value JASPER_CAPTURE_ORIGIN "${JASPER_CAPTURE_ORIGIN}"
        echo "  capture origin: configured from deploy environment"
    elif ! grep -qE '^JASPER_CAPTURE_ORIGIN=[[:space:]]*[^[:space:]]' "${ENV_DIR}/jasper.env"; then
        set_jasper_env_value JASPER_CAPTURE_ORIGIN "capture.jasper.tech"
        echo "  capture origin: using capture.jasper.tech"
    fi
    if [[ -n "${JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN:-}" ]]; then
        set_jasper_env_value \
            JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN \
            "${JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN}"
        echo "  capture relay registration token: configured from deploy environment"
    elif ! grep -qE '^JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=' "${ENV_DIR}/jasper.env"; then
        printf 'JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=\n' >> "${ENV_DIR}/jasper.env"
        echo "  capture relay registration token: unset"
    fi
    chmod 0640 "${ENV_DIR}/jasper.env"
}

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    ensure_state_dir
    install -d -m 0750 "${ENV_DIR}"
    # Non-secret, manually inspectable validation reports for mic/DAC/profile
    # readiness. Writers use atomic timestamped JSON files.
    install -d -m 0755 -o root -g root "${STATE_DIR}/audio-validation"
    # Active-speaker commissioning writes bounded tone artifacts from the
    # non-root jasper-web service before handing the same short file to the
    # protected commissioning graph. Keep the directory group-writable so a
    # root-created CLI artifact cannot wedge the web setup flow.
    install -d -m 2770 -o root -g jasper "${STATE_DIR}/active_speaker_tone_artifacts"

    # NOTE: the build manifest is intentionally NOT written here. It is the
    # verified-install success marker and is stamped as the FINAL mutation
    # in main() (write_build_manifest), so a failure anywhere downstream —
    # the WebRTC/Rust builds, unit install, nginx config — leaves the prior
    # good manifest rather than a SHA the box isn't cleanly running.
    # (Problem #4, docs/install-update-resilience-plan.md.)

    # WS1 Phase 4a — the per-account Google OAuth token tree + client secret now
    # live in the group-`jasper-secrets` compartment (jasper-voice + jasper-web
    # only), NOT here under the /var/lib/jasper StateDirectory (whose recursive
    # chown would force the group back to `jasper`, re-exposing the refresh
    # tokens to every jasper daemon). ensure_secrets_dir creates the compartment
    # parent + installs the boot self-heal tmpfiles; migrate_secrets_phase4a (in
    # the migrate list below) moves any existing tree out of /var/lib/jasper,
    # rewrites the absolute token_paths baked into accounts.json, re-groups the
    # tree to jasper-secrets, and splits the LLM API keys into voice_keys.env.
    ensure_secrets_dir

    # WS1 Phase 4b — Home Assistant + Spotify integration secrets live in the
    # sibling group-`jasper-intsecrets` compartment (voice/control/mux/web).
    # ensure_intsecrets_dir creates the forward path; migrate_secrets_phase4b
    # below moves any existing broad-state files into it.
    ensure_intsecrets_dir

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/jasper_aec3" \
        "${REPO_DIR}/pyproject.toml" \
        "${INSTALL_DIR}/"

    # Stage firmware/ next to the package so jasper-{dial,satellite}-onboard
    # find their respective bins (default --bin paths:
    # /opt/jasper/firmware/dial/jasper-dial.bin,
    # /opt/jasper/firmware/satellite-amoled/jasper-satellite-amoled.bin).
    # The .pio build dir is excluded — that's local to whoever ran the
    # per-firmware build.sh and contains absolute paths.
    #
    # NO --delete: build.sh writes each .bin INTO ${INSTALL_DIR}/firmware/
    # (not into the source repo), so --delete would silently remove the
    # staged .bin on every deploy. Verified failure mode: the /dial/
    # wizard's "Force flash" silently skipped flashing after re-deploy
    # because jasper-dial-onboard saw no bin and fell through to its
    # creds-only path. Instead we leave any locally-staged .bin in
    # place. Rebuilds are explicit accessory work: set
    # JASPER_BUILD_OPTIONAL_FIRMWARE=1 when intentionally refreshing
    # staged ESP32 firmware from source.
    if [[ -d "${REPO_DIR}/firmware" ]]; then
        rsync -a \
            --exclude='.pio' --exclude='.pioenvs' --exclude='.piolibdeps' \
            "${REPO_DIR}/firmware" "${INSTALL_DIR}/"

        if [[ "${JASPER_BUILD_OPTIONAL_FIRMWARE:-0}" == "1" ]]; then
            _build_firmware_if_stale "dial" "jasper-dial.bin"
            _build_firmware_if_stale "satellite-amoled" "jasper-satellite-amoled.bin"
        fi
    fi

    if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/.venv"
    fi
    # Pin the installer toolchain exactly. The previous unpinned
    # `--upgrade pip wheel` made every deploy pull whatever PyPI had
    # newest that morning — silent behavior drift (resolver changes,
    # build-isolation changes) on the highest-blast-radius script in
    # the repo. Bump these deliberately, with a deploy to verify.
    #
    # The application dependency tree (pyproject.toml) is open-ranged
    # for several packages (openai>=, scipy>=, onnxruntime>=, ...).
    # When the repo carries a Pi-generated constraints file (arm64 +
    # Python 3.13 resolve different wheels than a laptop, so the lock
    # must be produced on-platform — see
    # scripts/generate-pi-constraints.sh), the unpinned installs below
    # pass it via `-c` so every deploy replays the reviewed resolve.
    # No file → empty args → installs behave exactly as before.
    "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip==26.1.2 wheel==0.47.0

    local -a pip_constraints=()
    local constraints_file
    constraints_file="$(jasper_pip_constraints_file)"
    if [[ -n "${constraints_file}" ]]; then
        echo "  applying Pi-generated pip constraints: ${constraints_file}"
        pip_constraints=(-c "${constraints_file}")
    fi

    # openwakeword 0.6.0 hard-requires tflite-runtime on Linux, but
    # tflite-runtime has no Python 3.13 wheel (and PiOS Trixie ships
    # python3.13 only — no python3.12 in apt). We use ONNX models
    # exclusively (onnxruntime is already in pyproject.toml), so
    # tflite-runtime is never imported at runtime. Pre-install
    # openwakeword without its declared deps, then install its non-tflite
    # runtime deps explicitly. The subsequent editable install of
    # jasper-speaker sees openwakeword==0.6.0 already satisfied.
    "${INSTALL_DIR}/.venv/bin/pip" install --no-deps openwakeword==0.6.0
    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" \
        requests tqdm 'scipy>=1.3,<2' 'scikit-learn>=1,<2'

    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" -e "${INSTALL_DIR}[full]"

    # jasper_aec3 — pybind11 bindings for WebRTC AEC3. Two engines:
    #   - _aec3      → links against Debian Trixie's apt-installed
    #                  libwebrtc-audio-processing-1 (v1.3-3). Legacy
    #                  fallback engine.
    #   - _aec3_v2   → links statically against vendored
    #                  webrtc-audio-processing v2.1 (built by
    #                  build_webrtc_v2_for_aec3 below). Exposes the
    #                  deep EchoCanceller3Config knobs the v1
    #                  binding can't reach — required for the BEST_A
    #                  config. Built conditionally when the vendored
    #                  static archive exists.
    # See docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture
    # plan" and experiments/aec3-v2-deep-tune-spike/README.md for
    # the BEST_A canonical config + per-knob rationale.
    if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
        # Build vendored v2.1 first (cached after first run); exports
        # WEBRTC_AEC3_V2_PREFIX into the env that setup.py reads.
        build_webrtc_v2_for_aec3

        local marker="${INSTALL_DIR}/.cache/jasper_aec3.installed.fingerprint"
        local fingerprint
        fingerprint="$(jasper_aec3_source_fingerprint)"

        local needs_rebuild=1
        if [[ -f "${marker}" ]] \
           && [[ "$(cat "${marker}")" == "${fingerprint}" ]] \
           && jasper_aec3_import_probe; then
            echo "==> jasper_aec3 source + env unchanged, skipping rebuild"
            echo "    (delete ${marker} to force)"
            needs_rebuild=0
        elif [[ -f "${marker}" ]] \
             && ! grep -q '^content-v1:' "${marker}" \
             && jasper_aec3_import_probe; then
            echo "==> jasper_aec3 legacy cache marker imported cleanly; adopting content fingerprint"
            echo "    (delete ${marker} to force a rebuild)"
            mkdir -p "$(dirname "${marker}")"
            echo "${fingerprint}" > "${marker}"
            needs_rebuild=0
        fi

        if [[ "${needs_rebuild}" == "1" ]]; then
            # --force-reinstall: pip wheel cache only keys on source hash
            # + setuptools metadata, not on env vars. Without --force-reinstall,
            # a previously-cached wheel built without WEBRTC_AEC3_V2_PREFIX
            # (i.e. with only the v1 extension) would be reused even after
            # the vendored v2 build completes. Forcing a rebuild is the
            # simplest way to guarantee setup.py sees the env var and builds
            # both extensions.
            # cc1plus compiles the pybind wrapper translation units; contain
            # it so an OOM kills only this build, never a live daemon. The env
            # is passed via `env` (part of argv) so it
            # survives independently of systemd-run scope env inheritance.
            run_contained_build "jasper-aec3" -- \
                env "WEBRTC_AEC3_V2_PREFIX=${JASPER_WEBRTC_V2_PREFIX:-}" \
                "${INSTALL_DIR}/.venv/bin/pip" install --force-reinstall --no-deps \
                "${INSTALL_DIR}/jasper_aec3"
            mkdir -p "$(dirname "${marker}")"
            echo "${fingerprint}" > "${marker}"
        fi
    fi

    # Stage runtime model assets through jasper.model_downloads so the
    # exists/hash/download/failure-count logic stays unit-testable.
    stage_openwakeword_assets
    stage_wake_models
    stage_dtln_models
    seed_default_wake_model_env

    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        # Detect ReSpeaker XVF3800 card name. Default "Array" (PiOS literal
        # name for the legacy square USB firmware). ReSpeaker Flex linear
        # firmware enumerates as "L16K6Ch"; both are substrings of the
        # PortAudio device names sounddevice opens.
        # JASPER_MIC_DEVICE format is a PortAudio device name/substring,
        # NOT an ALSA pcm string — see jasper/config.py for the rationale.
        local mic_card
        mic_card=$(detect_card arecord 'xvf3800|respeaker.*(array|flex)|L16K6Ch' 'Array')
        echo "  ReSpeaker mic: ${mic_card}"
        # Derive JASPER_HOSTNAME from the OS hostname so a fresh Pi
        # named "jts2" in Raspberry Pi Imager ends up with
        # JASPER_HOSTNAME=jts2.local — otherwise other devices on the
        # LAN type jts2.local but Spotify/AirPlay setup URLs advertise
        # the wrong name. Override path stays clean: deploy-to-pi.sh
        # exports JASPER_HOSTNAME explicitly, which wins over the
        # autodetected fallback. Direct Pi-local install.sh reruns
        # that need a non-default identity must pass it in the sudo
        # environment, e.g.:
        #   sudo JASPER_HOSTNAME=jts2.local bash deploy/install.sh
        local hostname_value="${JASPER_HOSTNAME:-$(hostname).local}"
        echo "  hostname: ${hostname_value}"
        # .env.example is a frozen first-install seed. Keep any literals
        # that shadow Config defaults guarded by
        # tests/test_env_example_matches_config_defaults.py.
        sed \
            -e "s|JASPER_MIC_DEVICE=Array|JASPER_MIC_DEVICE=${mic_card}|" \
            -e "s|JASPER_AEC_MIC_DEVICE=Array|JASPER_AEC_MIC_DEVICE=${mic_card}|" \
            -e "s|^JASPER_HOSTNAME=.*|JASPER_HOSTNAME=${hostname_value}|" \
            "${REPO_DIR}/.env.example" > "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo
        echo "Created ${ENV_DIR}/jasper.env from template."
        echo "Pick a voice provider at http://${hostname_value}/voice before"
        echo "starting jasper-voice — there is no default."
        echo
    fi
    seed_capture_relay_env
    sed -i \
        -e '/^JASPER_SPOTIFY_DEVICE_NAME=/d' \
        -e '/^JASPER_AIRPLAY_DEVICE_NAME=/d' \
        -e '/^SPOTIFY_CLIENT_ID=/d' \
        -e '/^SPOTIFY_OAUTH_MODE=/d' \
        -e '/^SPOTIFY_REDIRECT_URI=/d' \
        -e '/^SPOTIPY_REDIRECT_URI=/d' \
        "${ENV_DIR}/jasper.env"
    if [[ -n "${OUTPUT_DAC_ID:-}" ]]; then
        sed -i.bak '/^JASPER_AUDIO_DAC_ID=/d' "${ENV_DIR}/jasper.env"
        rm -f "${ENV_DIR}/jasper.env.bak"
        printf 'JASPER_AUDIO_DAC_ID=%s\n' "${OUTPUT_DAC_ID}" >> "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo "  audio DAC id: ${OUTPUT_DAC_ID}"
    fi
    if [[ ! -e "${STATE_DIR}/speaker_name.env" ]]; then
        ensure_state_dir
        printf 'JASPER_SPEAKER_NAME="JTS"\n' > "${STATE_DIR}/speaker_name.env"
        chmod 0644 "${STATE_DIR}/speaker_name.env"
        echo "  speaker name: JTS"
    fi
    migrate_voice_provider
    # WS1 Phase 4a — runs AFTER migrate_voice_provider so JASPER_VOICE_PROVIDER
    # is already in voice_provider.env; this moves the Google tree + client
    # secret into the jasper-secrets compartment and splits the LLM API keys out
    # of voice_provider.env (+ any jasper.env seed) into voice_keys.env.
    migrate_secrets_phase4a
    # WS1 Phase 4b — runs after the 4a migration. Moves Home Assistant +
    # Spotify credentials/caches into jasper-intsecrets and rewrites Spotify's
    # absolute cache_path values baked into accounts.json.
    migrate_secrets_phase4b
    migrate_openai_noise_reduction_default
    migrate_tts_outputd_socket_default
    migrate_removed_output_dac_route
    render_voice_provider_ids_manifest
    migrate_transit_config
    migrate_weather_config
    migrate_wifi_guardian
    migrate_wake_legs_config
    migrate_grouping
    migrate_speaker_room
    migrate_control_host_bind_seed
    # Relocate JASPER_FANIN_CAMILLA_COUPLING out of jasper.env into the
    # reconciler-owned fanin.env (jasper.fanin.coupling_reconcile is its single
    # writer). No-op on a fresh box (the flag is unset) and on a box already
    # using fanin.env; only moves a hand-set experimental-phase value.
    migrate_fanin_coupling
}

jasper_aec3_import_probe() {
    local require_v2=0
    if [[ -n "${JASPER_WEBRTC_V2_PREFIX:-}" ]]; then
        require_v2=1
    fi
    JASPER_AEC3_REQUIRE_V2="${require_v2}" "${INSTALL_DIR}/.venv/bin/python" - <<'PY' 2>/dev/null
import importlib
import os

import jasper_aec3

importlib.import_module("jasper_aec3._aec3")
if os.environ.get("JASPER_AEC3_REQUIRE_V2") == "1":
    importlib.import_module("jasper_aec3._aec3_v2")
PY
}

jasper_aec3_source_fingerprint() {
    # Content, ABI, and vendored-source identity fingerprint for the compiled
    # jasper_aec3 extensions. The old cache used mtimes after rsync, so every
    # deploy could force a pybind rebuild on 1 GB Pis even when AEC3 source
    # bytes were unchanged. Keep setup.py out of this key: build-policy edits
    # such as lower optimization flags should not invalidate an already
    # importable runtime binary.
    (
        if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
            find "${INSTALL_DIR}/jasper_aec3" -type f \
                \( -path '*/jasper_aec3/*.py' \
                   -o -path '*/src/*.cpp' \
                   -o -path '*/src/*.h' \
                   -o -name 'pyproject.toml' \) \
                -print 2>/dev/null \
                | LC_ALL=C sort \
                | while IFS= read -r path; do
                    sha256sum "${path}"
                done || true
        fi
        "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
import sys
import sysconfig

print(f"python={sys.version_info.major}.{sys.version_info.minor}")
print(f"ext_suffix={sysconfig.get_config_var('EXT_SUFFIX') or ''}")
PY
        pkg-config --modversion webrtc-audio-processing-1 2>/dev/null \
            | sed 's/^/webrtc1=/' || true
        echo "webrtc2=${WEBRTC_AEC3_COMMIT:-}:${WEBRTC_AEC3_SHA256:-}"
        echo "webrtc2_prefix=${JASPER_WEBRTC_V2_PREFIX:-}"
    ) | sha256sum | awk '{print "content-v1:" $1}'
}

install_streambox_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    install -d -m 0750 "${STATE_DIR}"
    install -d -m 0750 "${ENV_DIR}"
    install -d -m 0755 -o root -g root "${STATE_DIR}/audio-validation"

    # Build manifest is written as the FINAL mutation in main(), not here —
    # see install_jasper's note and write_build_manifest for why (problem #4).

    rsync -a --delete \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" \
        "${REPO_DIR}/pyproject.toml" \
        "${REPO_DIR}/README.md" \
        "${REPO_DIR}/docs" \
        "${INSTALL_DIR}/"

    if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/.venv"
    fi
    "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip==26.1.2 wheel==0.47.0

    local -a pip_constraints=()
    local constraints_file
    constraints_file="$(jasper_pip_constraints_file)"
    if [[ -n "${constraints_file}" ]]; then
        echo "  applying Pi-generated pip constraints: ${constraints_file}"
        pip_constraints=(-c "${constraints_file}")
    fi
    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" \
        -e "${INSTALL_DIR}[streambox]"

    local hostname_value="${JASPER_HOSTNAME:-$(hostname).local}"
    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        cat > "${ENV_DIR}/jasper.env" <<EOF
JASPER_HOSTNAME=${hostname_value}
JASPER_CONTROL_HOST=0.0.0.0
JASPER_INSTALL_PROFILE=streambox
EOF
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo "  streambox env: created ${ENV_DIR}/jasper.env"
    else
        set_jasper_env_value JASPER_CONTROL_HOST "0.0.0.0"
        set_jasper_env_value JASPER_INSTALL_PROFILE "streambox"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo "  streambox env: refreshed streambox defaults"
    fi
    seed_capture_relay_env

    if [[ ! -e "${STATE_DIR}/speaker_name.env" ]]; then
        printf 'JASPER_SPEAKER_NAME="JTS"\n' > "${STATE_DIR}/speaker_name.env"
        chmod 0644 "${STATE_DIR}/speaker_name.env"
        echo "  speaker name: JTS"
    fi
}
