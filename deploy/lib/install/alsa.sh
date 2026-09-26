#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# ALSA card detection, audio-hardware roles and the /etc/asound.conf install for deploy/install.sh.

# `-L` prints each PCM name (with its CARD=) on the line above the description
# the regex is matched against.
detect_card() {
    local tool="$1" regex="$2" fallback="$3" card
    card="$("${tool}" -L 2>/dev/null | grep -B1 -iE "${regex}" \
        | grep -oE 'CARD=[^,]+' | head -1 | sed 's/CARD=//' || true)"
    printf '%s\n' "${card:-${fallback}}"
}

select_audio_hardware_roles() {
    # Hardware roles are intentionally separate. The reconciler owns
    # detection so install, boot, and udev-triggered changes share one
    # policy surface.
    eval "$(bash "${REPO_DIR}/deploy/bin/jasper-audio-hardware-reconcile" --print-env)"
    if [[ "${APPLE_DONGLE_PRESENT}" == "1" ]]; then
        echo "  Apple dongle: CARD=${DONGLE_CARD}"
    else
        echo "  Apple dongle: not detected"
    fi
    echo "  Output DAC: CARD=${OUTPUT_DAC_CARD}"
    echo "  Output DAC id: ${OUTPUT_DAC_ID}"
    export DONGLE_CARD APPLE_DONGLE_PRESENT APPLE_DONGLE_SERVICE_CARD
    export OUTPUT_DAC_CARD OUTPUT_DAC_ID OUTPUT_DAC_RECOGNIZED
}

# snd-aloop binds index/pcm_substreams/pcm_notify at module load, and on a
# full-RAM box jasper-fanin holds the Loopback capture sides, so the unload is
# EBUSY (#4027) and changed options wait for the next boot. The low-RAM
# profiles park fanin first; there the reload succeeds, gated by #4218.
install_snd_aloop_options() {
    local shipped="${REPO_DIR}/deploy/modprobe.d/snd-aloop.conf"
    local installed=/etc/modprobe.d/snd-aloop.conf
    local changed=0 module_busy=0 sysfs="$1"
    cmp -s "${shipped}" "${installed}" || changed=1
    install -m 0644 "${shipped}" "${installed}"
    if [[ -d "${sysfs}" ]] && ! rmmod snd_aloop 2>/dev/null; then
        module_busy=1
    fi
    modprobe snd-aloop
    if (( ! module_busy )); then
        _set_reboot_required_reason snd_aloop ""
    elif (( changed )); then
        echo "  snd-aloop: options changed but module busy; deferred to reboot"
        _set_reboot_required_reason snd_aloop \
            "snd-aloop options changed, module busy — reboot to apply"
    fi
}

install_alsa() {
    install -d -m 0755 /etc/modules-load.d /etc/alsa/conf.d /etc/modprobe.d
    install -m 0644 \
        "${REPO_DIR}/deploy/modules-load.d/snd-aloop.conf" \
        /etc/modules-load.d/snd-aloop.conf
    install_snd_aloop_options /sys/module/snd_aloop

    select_audio_hardware_roles

    # /etc/asound.conf provides the system-wide ALSA PCM definitions; its own
    # header owns what they are and why (deploy/alsa/asoundrc.jasper).
    # It MUST stay world-readable: renderers run as non-root users
    # (shairport-sync, librespot as `pi`) and cannot otherwise resolve the
    # user-space PCM names it declares. The backup's grep guard keys on our
    # own content; a symlink is ours (created below) and is never backed up.
    if [[ -f /etc/asound.conf && ! -L /etc/asound.conf ]] \
            && ! grep -q "shairport_substream" /etc/asound.conf 2>/dev/null; then
        cp /etc/asound.conf "/etc/asound.conf.pre-jasper.$(date +%s)"
        echo "  Backed up pre-existing /etc/asound.conf (.pre-jasper.*)."
    fi
    install -d -m 0755 "${ENV_DIR}"
    ensure_state_dir
    install -m 0755 \
        "${REPO_DIR}/deploy/bin/jasper-render-asound-conf" \
        /usr/local/sbin/jasper-render-asound-conf
    if [[ ! -e "${STATE_DIR}/audio_quality.env" ]]; then
        jasper_env_file_set "${STATE_DIR}/audio_quality.env" \
            JASPER_ALSA_RATE_CONVERTER samplerate_medium 0644 0770
        echo "  /var/lib/jasper/audio_quality.env defaulted to samplerate_medium."
    fi
    # 2775 root:jasper (setgid): jasper-control renders here; o+rx stays so pi-run renderers can read the /etc/asound.conf symlink.
    install -d -m 2775 -o root -g jasper /var/lib/jasper-asound
    install -m 0644 \
        "${REPO_DIR}/deploy/alsa/asoundrc.jasper" \
        "${ENV_DIR}/asoundrc.jasper.source"
    jasper_asound_render_template \
        "${ENV_DIR}/asoundrc.jasper.source" \
        "${ENV_DIR}/asoundrc.jasper.template"
    chmod 0644 "${ENV_DIR}/asoundrc.jasper.template"
    /usr/local/sbin/jasper-render-asound-conf
    ln -sfn /var/lib/jasper-asound/asound.conf /etc/asound.conf
    chmod 0644 /var/lib/jasper-asound/asound.conf
    echo "  Wrote /etc/asound.conf with fan-in and outputd lanes"
}
