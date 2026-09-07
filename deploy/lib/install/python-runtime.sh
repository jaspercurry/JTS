#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Python runtime install steps for deploy/install.sh.
#
# Extracted from install.sh; functions assume install.sh globals and
# set -euo pipefail from the sourcing shell.

seed_speaker_name_env() {
    # The /speaker wizard owns this file after first creation.  Seed it before
    # install_renderers renders AirPlay + BlueZ so every first-boot consumer
    # (including later-started Librespot, dashboard, USB, and Avahi) sees one
    # canonical display name.  Any existing path is authoritative and remains
    # byte-for-byte untouched, including operator comments/formatting.
    local state_file="${STATE_DIR}/speaker_name.env"
    if [[ -e "${state_file}" || -L "${state_file}" ]]; then
        echo "  speaker name: preserving ${state_file}"
        return 0
    fi

    ensure_state_dir
    local system_hostname
    system_hostname="$(hostname 2>/dev/null || true)"
    local env_line
    env_line="$(
        PYTHONPATH="${REPO_DIR}" "${JASPER_SYSTEM_PYTHON:-python3}" - \
            "${ENV_DIR}/jasper.env" "${system_hostname}" <<'PY'
import os
import sys

from jasper.env_load import parse_env_file
from jasper.identity.speaker_name import initial_name_from_hostname, quote_env_value

env_path, system_hostname = sys.argv[1:]
configured = os.environ.get("JASPER_HOSTNAME", "").strip()
if not configured:
    configured = parse_env_file(env_path).get("JASPER_HOSTNAME", "").strip()
hostname = configured or system_hostname
name = initial_name_from_hostname(hostname)
print(f"JASPER_SPEAKER_NAME={quote_env_value(name)}")
PY
    )"

    # Publish only after the complete line and final mode are ready.  A failed
    # direct redirection would leave a partial canonical file that every later
    # deploy correctly preserves.  `link` is link(2) with no options to get
    # wrong: an atomic create-if-absent that refuses an existing name rather
    # than descending into it, so a wizard save landing after our first check
    # remains authoritative.  Not `ln -T` — `-T` is a GNU extension the
    # laptop-side test lane's macOS `ln` rejects.
    local tmp
    tmp="$(mktemp "${STATE_DIR}/.speaker_name.env.seed.XXXXXX")"
    if ! printf '%s\n' "${env_line}" > "${tmp}"; then
        rm -f -- "${tmp}"
        echo "  ERROR: could not write fresh speaker name" >&2
        return 1
    fi
    if ! chmod 0644 "${tmp}"; then
        rm -f -- "${tmp}"
        echo "  ERROR: could not set fresh speaker-name permissions" >&2
        return 1
    fi
    if link "${tmp}" "${state_file}" 2>/dev/null; then
        rm -f -- "${tmp}"
        echo "  speaker name: ${env_line#JASPER_SPEAKER_NAME=}"
        return 0
    fi
    rm -f -- "${tmp}"
    if [[ -e "${state_file}" || -L "${state_file}" ]]; then
        echo "  speaker name: preserving ${state_file}"
        return 0
    fi
    echo "  ERROR: could not publish fresh speaker name" >&2
    return 1
}

# Delete this function and both its calls once every box's JASPER_INSTALL_AT in /var/lib/jasper/build.txt is past the release that retired the ESP32 accessory stack.
retire_esp32_accessory_python_packages() {
    # Editable installs add/update requirements but pip does not prune
    # dependencies that disappear from pyproject.toml. Remove the retired
    # firmware/onboarding stack and the transitive packages that vanished
    # from uv.lock, including an operator-installed PlatformIO in JTS's own
    # venv. This never touches ~/.platformio or any system Python.
    local -a retired_packages=(
        platformio
        esptool
        pyserial
        bitarray
        bitstring
        click
        intelhex
        markdown-it-py
        mdurl
        reedsolo
        rich
        rich-click
        tibs
    )
    "${INSTALL_DIR:?}/.venv/bin/pip" uninstall -y \
        "${retired_packages[@]}" >/dev/null 2>&1
}

# Pre-PR .env.example seeded this exact value uncommented; jasper.env is a
# frozen first-install seed (never re-synced — see the comment above its
# creation), so every existing Pi would keep the retired 1 GiB cap forever,
# and the doctor's new smaller warn threshold would warn on it permanently.
# Anchored on the full stale line so a deliberate non-default override
# (any other value) survives untouched.
migrate_wake_events_cap_seed() {
    sed_inplace "${ENV_DIR}/jasper.env" \
        '/^JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=1073741824$/d'
}

# A seeded value outranks the mic registry; only the two shapes
# .env.example shipped are removed — an operator's own bare `=Array`
# is indistinguishable from the seed and goes too.
migrate_mic_device_candidates_seed() {
    sed_inplace "${ENV_DIR}/jasper.env" \
        -e '/^JASPER_MIC_DEVICE_CANDIDATES=Array$/d' \
        -e '/^JASPER_MIC_DEVICE_CANDIDATES=Array,L16K6Ch$/d'
}

# The Python tree publishes from a staging path (See ADR-0252). The checkout
# rsyncs into ${INSTALL_DIR}/.staging — the live tree's own filesystem by
# construction, so --link-dest makes an unchanged file a second link to the live
# inode rather than a copy — and each top-level entry is renamed into place from
# there. The guarantee is per entry: an entry is absent for the instant between
# its two renames, is otherwise wholly old or wholly new, and any failure rolls
# the already-published entries back, so a re-deploy or a reboot finds the whole
# old tree or the whole new one.
publish_staged_install_tree() {
    # The glob never matches a dotfile, so .deps.txt — and any dot-name the
    # checkout ships at its top level — stays behind in the staging tree.
    local staging="${INSTALL_DIR}/.staging" staged name
    for staged in "${staging}"/*; do
        [[ -e "${staged}" ]] || continue
        name="${staged##*/}"
        if [[ -e "${INSTALL_DIR}/${name}" ]]; then
            mv "${INSTALL_DIR}/${name}" "${staged}.prev" || return 1
        fi
        mv "${staged}" "${INSTALL_DIR}/${name}" || return 1
    done
    # The rollback globs ${INSTALL_DIR}/.staging/*.prev, so renaming the tree
    # takes it out of that namespace: a delete cut off partway then leaves a
    # truncated .prev nothing reads, rather than one the rollback would take
    # for a complete old copy. Its own `rm -rf .done` is hygiene.
    mv "${staging}" "${staging}.done" || return 1
    rm -rf -- "${staging}.done"
}

stage_install_tree() {
    remove_staged_install_tree
    install -d -m 0755 "${INSTALL_DIR}/.staging"
}

# Roll an unfinished publish back, then drop the staging tree. The EXIT trap
# calls this through _call_if_defined, which disarms errexit: the explicit
# return is what stops a failed restore from reaching the rm below and deleting
# a live entry's only copy.
remove_staged_install_tree() {
    local staging="${INSTALL_DIR}/.staging" prev name restored
    rm -rf -- "${staging}.done"
    for prev in "${staging}"/*.prev; do
        [[ -e "${prev}" ]] || continue
        name="${prev##*/}"
        name="${name%.prev}"
        restored=no
        if [[ ! -e "${INSTALL_DIR}/${name}" ]] \
           || mv "${INSTALL_DIR}/${name}" "${staging}/${name}"; then
            if mv "${prev}" "${INSTALL_DIR}/${name}"; then
                restored=yes
            fi
        fi
        jasper_install_log \
            "event=install.staging_rollback entry=${name} restored=${restored}"
        [[ "${restored}" == yes ]] || return 1
    done
    rm -rf -- "${staging}"
}

# Install what the STAGED manifest declares, before anything in the live tree
# moves: a dependency that will not resolve or build fails the deploy with the
# box still on its old source and its old venv. This IS the dependency install —
# the editable install after the publish takes --no-deps — so a manifest that is
# unreadable, or that does not declare the extra this profile installs, fails
# here rather than publishing a tree whose dependencies nothing resolved.
install_staged_dependencies() {
    local extra="$1" staging="${INSTALL_DIR}/.staging"
    shift
    "${INSTALL_DIR}/.venv/bin/python" - "${staging}/pyproject.toml" \
        "${extra}" >"${staging}/.deps.txt" <<'PY' &&
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]
optional = project.get("optional-dependencies", {})
if not optional.get(sys.argv[2]):
    raise SystemExit(f"manifest declares no {sys.argv[2]} extra")
print("\n".join(project.get("dependencies", []) + optional[sys.argv[2]]))
PY
    "${INSTALL_DIR}/.venv/bin/pip" install "$@" -r "${staging}/.deps.txt"
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

    # The build manifest is intentionally NOT written here. It is the verified-install success
    # marker and is stamped as the FINAL mutation in main() (write_build_manifest), so a
    # failure anywhere downstream — the WebRTC/Rust builds, unit install, nginx config —
    # leaves the prior good manifest rather than a SHA the box isn't cleanly running. (See
    # ADR-0172.)

    # The per-account Google OAuth token tree + client secret
    # live in the group-`jasper-secrets` compartment (jasper-voice + jasper-web
    # only), NOT here under the /var/lib/jasper StateDirectory (whose recursive
    # chown would force the group back to `jasper`, re-exposing the refresh
    # tokens to every jasper daemon). ensure_secrets_dir creates the compartment
    # parent + installs the boot self-heal tmpfiles; a later main() step,
    # reassert_secrets_compartment_perms, re-narrows the tree's ownership/modes
    # and splits an operator-seeded LLM API key into voice_keys.env.
    ensure_secrets_dir

    # Home Assistant + Spotify integration secrets live in the
    # sibling group-`jasper-intsecrets` compartment (voice/control/mux/web).
    # ensure_intsecrets_dir creates the forward path; a later main() step,
    # reassert_intsecrets_compartment_perms, re-narrows its ownership/modes.
    ensure_intsecrets_dir

    # Stop optional work before mutating the live source/venv. Its compiler
    # children remain in the oneshot cgroup, so a cgroup-wide KILL drains the
    # whole tree without making a core deploy wait for nonessential work. The
    # final manifest PathChanged event retries the still-durable intent.
    local enhanced_aec_state
    enhanced_aec_state="$(
        systemctl is-active jasper-enhanced-aec-install.service 2>/dev/null \
            || true
    )"
    case "${enhanced_aec_state}" in
        active|activating|reloading|deactivating)
            echo "  pausing optional enhanced-AEC work for core deploy"
            systemctl kill --kill-whom=all --signal=KILL \
                jasper-enhanced-aec-install.service 2>/dev/null || true
            systemctl stop --no-block jasper-enhanced-aec-install.service \
                >/dev/null 2>&1 || true
            ;;
    esac

    # Serialize the remaining short source/package mutation with enhanced-AEC
    # snapshot/activation. The optional build itself never holds this lock.
    # This lock authorizes root package mutation, so its parent is deliberately
    # outside group-writable /var/lib/jasper. Creating/healing the root-only
    # parent before shell redirection prevents a jasper-group process from
    # substituting a symlink or holding the deploy lock.
    install -d -m 0755 -o root -g root /var/lib/jasper-enhanced-aec
    local enhanced_aec_lock_fd
    exec {enhanced_aec_lock_fd}>"/var/lib/jasper-enhanced-aec/.install.lock"
    chmod 0600 /var/lib/jasper-enhanced-aec/.install.lock
    if ! flock -n "${enhanced_aec_lock_fd}"; then
        echo "  waiting for enhanced-AEC activation to finish stopping"
    fi
    if ! flock -w 2 "${enhanced_aec_lock_fd}"; then
        echo "  ERROR: unmanaged root process still holds the enhanced-AEC package mutation lock" >&2
        return 1
    fi

    local staging="${INSTALL_DIR}/.staging" extra=full
    stage_install_tree
    rsync -a --link-dest="${INSTALL_DIR}" \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" "${REPO_DIR}/jasper_aec3" \
        "${REPO_DIR}/pyproject.toml" \
        "${staging}/"
    install -d -m 0755 "${staging}/experiments"
    rsync -a --link-dest="${INSTALL_DIR}/experiments" \
        --exclude='__pycache__' --exclude='*.pyc' \
        "${REPO_DIR}/experiments/usb-turntable" \
        "${staging}/experiments/"

    # The three operator docs (ADR-0204 tier 2): methodology, runbook and
    # doctrine load on demand from the box rather than riding in an agent's
    # resident context. install_streambox_jasper below already carries all of
    # docs/ (its rsync predates this ticket); named individually here instead
    # of widening this profile's rsync to the whole tree, which would also
    # ship the dev-process corpus (ADRs, research, historical/) this profile
    # has never installed.
    install -d -m 0755 "${staging}/docs"
    install -m 0644 \
        "${REPO_DIR}/docs/tuning-methodology.md" \
        "${REPO_DIR}/docs/tuning-operator-runbook.md" \
        "${REPO_DIR}/docs/measurement-loop-doctrine.md" \
        "${staging}/docs/"

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
    retire_esp32_accessory_python_packages

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

    install_staged_dependencies "${extra}" "${pip_constraints[@]}"
    publish_staged_install_tree
    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" --no-deps \
        -e "${INSTALL_DIR}[${extra}]"

    # jasper_aec3 — pybind11 bindings for WebRTC AEC3. Two engines:
    #   - _aec3      → links against Debian Trixie's apt-installed
    #                  libwebrtc-audio-processing-1 (v1.3-3). Legacy
    #                  fallback engine.
    #   - _aec3_v2   → optional enhanced engine installed later by
    #                  jasper-enhanced-aec-install. A normal deploy never
    #                  downloads or compiles it.
    # See experiments/aec3-v2-deep-tune-spike/README.md for the BEST_A
    # canonical config + per-knob rationale.
    if [[ -d "${INSTALL_DIR}/jasper_aec3" ]]; then
        local marker="${INSTALL_DIR}/.cache/jasper_aec3.installed.fingerprint"
        local fingerprint
        fingerprint="$(jasper_aec3_source_fingerprint)"

        # Upgrade migration: v2 used to be mandatory. Preserve that implicit
        # preference as durable opt-in before a necessary v1 package rebuild
        # can remove the old extension. The new root job will re-verify it
        # against the current fingerprint; runtime uses v1 until that marker
        # lands.
        if [[ ! -f /var/lib/jasper/enhanced-aec-intent.json ]] \
           && find "${INSTALL_DIR}/.venv/lib" -path \
                '*/site-packages/jasper_aec3/_aec3_v2*.so' \
                -type f -print -quit 2>/dev/null | grep -q .; then
            "${INSTALL_DIR}/.venv/bin/python" - <<'PY'
from jasper.enhanced_aec import request_install
request_install()
PY
            echo "  migrated existing enhanced AEC engine to durable opt-in intent"
        fi

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
            # Build only mandatory v1. The optional root job builds a v2-only
            # wheel in staging and atomically installs its one extension, so a
            # failed enhancement can never uninstall or damage this fallback.
            run_contained_build "jasper-aec3" -- \
                env "JASPER_AEC3_BUILD_MODE=v1-only" \
                "${INSTALL_DIR}/.venv/bin/pip" install --force-reinstall --no-deps \
                "${INSTALL_DIR}/jasper_aec3"
            mkdir -p "$(dirname "${marker}")"
            echo "${fingerprint}" > "${marker}"
        fi
    fi

    flock -u "${enhanced_aec_lock_fd}"
    exec {enhanced_aec_lock_fd}>&-

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
    sed_inplace "${ENV_DIR}/jasper.env" \
        -e '/^SPOTIFY_CLIENT_ID=/d' \
        -e '/^SPOTIFY_OAUTH_MODE=/d' \
        -e '/^SPOTIFY_REDIRECT_URI=/d' \
        -e '/^JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=/d'
    migrate_wake_events_cap_seed
    migrate_mic_device_candidates_seed
    if [[ -n "${OUTPUT_DAC_ID:-}" ]]; then
        jasper_env_file_set "${ENV_DIR}/jasper.env" \
            JASPER_AUDIO_DAC_ID "${OUTPUT_DAC_ID}" 0640 0750
        echo "  audio DAC id: ${OUTPUT_DAC_ID}"
    fi
    render_voice_provider_ids_manifest
}

jasper_aec3_import_probe() {
    "${INSTALL_DIR}/.venv/bin/python" - <<'PY' 2>/dev/null
import importlib

import jasper_aec3

importlib.import_module("jasper_aec3._aec3")
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
        sha256sum "${INSTALL_DIR}/jasper_aec3/enhanced-aec-source.env" \
            2>/dev/null || true
    ) | sha256sum | awk '{print "content-v1:" $1}'
}

install_streambox_jasper() {
    install -d -m 0755 "${INSTALL_DIR}"
    ensure_state_dir
    install -d -m 0750 "${ENV_DIR}"
    install -d -m 0755 -o root -g root "${STATE_DIR}/audio-validation"

    # Build manifest is written as the FINAL mutation in main(), not here —
    # see install_jasper's note and write_build_manifest for why (ADR-0172).

    local staging="${INSTALL_DIR}/.staging" extra=streambox
    stage_install_tree
    rsync -a --link-dest="${INSTALL_DIR}" \
        --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        --exclude='tests' --exclude='deploy' \
        --exclude='build' --exclude='*.egg-info' \
        "${REPO_DIR}/jasper" \
        "${REPO_DIR}/pyproject.toml" \
        "${REPO_DIR}/README.md" \
        "${REPO_DIR}/docs" \
        "${staging}/"

    if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/.venv"
    fi
    "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip==26.1.2 wheel==0.47.0
    retire_esp32_accessory_python_packages

    local -a pip_constraints=()
    local constraints_file
    constraints_file="$(jasper_pip_constraints_file)"
    if [[ -n "${constraints_file}" ]]; then
        echo "  applying Pi-generated pip constraints: ${constraints_file}"
        pip_constraints=(-c "${constraints_file}")
    fi
    install_staged_dependencies "${extra}" "${pip_constraints[@]}"
    publish_staged_install_tree
    "${INSTALL_DIR}/.venv/bin/pip" install "${pip_constraints[@]}" --no-deps \
        -e "${INSTALL_DIR}[${extra}]"

    local hostname_value="${JASPER_HOSTNAME:-$(hostname).local}"
    if [[ ! -f "${ENV_DIR}/jasper.env" ]]; then
        cat > "${ENV_DIR}/jasper.env" <<EOF
JASPER_HOSTNAME=${hostname_value}
JASPER_INSTALL_PROFILE=streambox
EOF
        chmod 0640 "${ENV_DIR}/jasper.env"
        echo "  streambox env: created ${ENV_DIR}/jasper.env"
    else
        jasper_env_file_set "${ENV_DIR}/jasper.env" \
            JASPER_INSTALL_PROFILE streambox 0640 0750
        echo "  streambox env: refreshed streambox defaults"
    fi
    # Streambox writes its own env rather than seeding from .env.example, so it
    # never reaches the full profile's retirement list, and the retired capture
    # relay's token line can hold a real self-hosted secret.
    sed_inplace "${ENV_DIR}/jasper.env" \
        '/^JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=/d'
}
