#!/usr/bin/env bash
# Python runtime install steps for deploy/install.sh.
#
# Extracted from install.sh; functions assume install.sh globals and
# set -euo pipefail from the sourcing shell.

install_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    ensure_state_dir
    install -d -m 0750 "${ENV_DIR}"
    # Non-secret, manually inspectable validation reports for mic/DAC/profile
    # readiness. Writers use atomic timestamped JSON files.
    install -d -m 0755 -o root -g root "${STATE_DIR}/audio-validation"

    write_build_manifest

    # Per-account Google refresh tokens live under here at mode 0600.
    # Tighten the parent dirs too so non-root processes can't even
    # `ls` the per-household-member token filenames (the names are
    # PII-adjacent — they identify which household members linked
    # accounts). install -d resets perms on existing dirs, so this
    # also tightens any pre-existing 755 left from earlier installs.
    install -d -m 0700 "${STATE_DIR}/google" "${STATE_DIR}/google/tokens"

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

    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" -e "${INSTALL_DIR}"

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

        # Fingerprint-cache the C++ rebuild: skip pip install when
        # nothing the binding depends on has changed.
        #
        # Why this exists: --force-reinstall (kept below) forces a
        # full pip-side rebuild whose --no-cache-defeat is the only
        # way to guarantee setup.py sees WEBRTC_AEC3_V2_PREFIX (pip's
        # wheel cache doesn't key on env vars). But the actual C++
        # compile of aec3_binding_v2.cpp at -O3 takes 1-3 min on Pi 5
        # with ~430 MB peak RAM on cc1plus — wasteful on the ~80%
        # of deploys that don't touch jasper_aec3/.
        #
        # Fingerprint inputs (any change → rebuild):
        #   - mtime + name of every .cpp/.h/.py/pyproject.toml in
        #     jasper_aec3/
        #   - mtime of the vendored libwebrtc-audio-processing-2.a
        #     (rebuilt rarely by build_webrtc_v2_for_aec3)
        #   - Python version (ABI break → rebuild)
        #   - WEBRTC_AEC3_V2_PREFIX value (cache path change → rebuild)
        #
        # Defense-in-depth: even on cache hit, verify the module
        # imports cleanly — catches accidentally-deleted .so files
        # or partial installs between deploys.
        #
        # Escape hatch: `sudo rm /opt/jasper/.cache/jasper_aec3.installed.fingerprint`
        # then re-deploy → unconditional rebuild.
        local marker="${INSTALL_DIR}/.cache/jasper_aec3.installed.fingerprint"
        local fingerprint
        fingerprint=$(
            (
                find "${INSTALL_DIR}/jasper_aec3" -type f \
                    \( -name '*.cpp' -o -name '*.h' \
                       -o -name '*.py' -o -name 'pyproject.toml' \
                       -o -name 'setup.py' -o -name 'setup.cfg' \) \
                    -exec stat -c '%Y %n' {} \; 2>/dev/null | sort
                # Vendored static archive — null if build_webrtc_v2_for_aec3
                # didn't set the prefix, which means we'd be building
                # the v1-only binding (still want to fingerprint that).
                if [[ -n "${JASPER_WEBRTC_V2_PREFIX:-}" ]]; then
                    find "${JASPER_WEBRTC_V2_PREFIX}" -name 'libwebrtc-audio-processing-2.a' \
                        -exec stat -c '%Y %n' {} \; 2>/dev/null
                fi
                "${INSTALL_DIR}/.venv/bin/python" --version 2>&1
                echo "WEBRTC_PREFIX=${JASPER_WEBRTC_V2_PREFIX:-}"
            ) | sha256sum | awk '{print $1}'
        )

        local needs_rebuild=1
        if [[ -f "${marker}" ]] \
           && [[ "$(cat "${marker}")" == "${fingerprint}" ]] \
           && "${INSTALL_DIR}/.venv/bin/python" -c "import jasper_aec3" 2>/dev/null; then
            echo "==> jasper_aec3 source + env unchanged, skipping rebuild"
            echo "    (delete ${marker} to force)"
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
            WEBRTC_AEC3_V2_PREFIX="${JASPER_WEBRTC_V2_PREFIX:-}" \
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
        # name; product description matches and it's also a substring of
        # PortAudio's enumerated name "Array: USB Audio (hw:N,0)").
        # JASPER_MIC_DEVICE format is a PortAudio device name/substring,
        # NOT an ALSA pcm string — see jasper/config.py for the rationale.
        local mic_card
        mic_card=$(detect_card arecord 'xvf3800|respeaker.*array' 'Array')
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
        sed \
            -e "s|JASPER_MIC_DEVICE=Array|JASPER_MIC_DEVICE=${mic_card}|" \
            -e "s|^JASPER_HOSTNAME=.*|JASPER_HOSTNAME=${hostname_value}|" \
            "${REPO_DIR}/.env.example" > "${ENV_DIR}/jasper.env"
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo
        echo "Created ${ENV_DIR}/jasper.env from template."
        echo "Pick a voice provider at http://${hostname_value}/voice before"
        echo "starting jasper-voice — there is no default."
        echo
    fi
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
    migrate_openai_noise_reduction_default
    migrate_tts_outputd_socket_default
    render_voice_provider_ids_manifest
    migrate_transit_config
    migrate_weather_config
    migrate_wifi_guardian
    migrate_wake_legs_config
    migrate_grouping
    migrate_speaker_room
    migrate_control_host_bind_seed
}
