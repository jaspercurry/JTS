#!/usr/bin/env bash
# Shared ALSA template rendering helpers for JTS final-output routing.
#
# Inputs are the already-detected role variables owned by install.sh and
# jasper-audio-hardware-reconcile:
#   DONGLE_CARD, OUTPUT_DAC_CARD, OUTPUT_DAC_ID
#
# Keep this narrow. It renders the outputd_dac PCM/ctl blocks and simple
# placeholders in deploy/alsa/asoundrc.jasper; it is not a DAC abstraction.

jasper_asound_log_token() {
    local value="${1:-}"
    if [[ -z "$value" ]]; then
        printf 'direct'
        return
    fi
    printf '%s' "$value" | tr -c 'A-Za-z0-9_.:,-' '_'
}

jasper_asound_require_output_dac_card() {
    if [[ -n "${OUTPUT_DAC_CARD:-}" ]]; then
        return 0
    fi
    echo "jasper-asound-render: OUTPUT_DAC_CARD is required for ${OUTPUT_DAC_ID:-unknown}" >&2
    return 64
}

jasper_asound_outputd_dac_parked() {
    if [[ "${OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch" ]]; then
        return 0
    fi
    if [[ "${OUTPUT_DAC_RECOGNIZED:-1}" != "1" ]]; then
        return 0
    fi
    return 1
}

jasper_asound_outputd_dac_pcm_block() {
    if jasper_asound_outputd_dac_parked; then
        cat <<'EOF'
pcm.outputd_dac {
    type null
}
EOF
        return
    fi
    jasper_asound_require_output_dac_card || return $?
    cat <<EOF
pcm.outputd_dac {
    type hw
    card ${OUTPUT_DAC_CARD}
    device 0
}
EOF
}

jasper_asound_outputd_dac_ctl_block() {
    if jasper_asound_outputd_dac_parked; then
        return
    fi
    jasper_asound_require_output_dac_card || return $?
    cat <<EOF
ctl.outputd_dac {
    type hw
    card ${OUTPUT_DAC_CARD}
}
EOF
}

jasper_asound_render_template() {
    local source="$1" dest="$2"
    local ctl_block line pcm_block
    pcm_block="$(jasper_asound_outputd_dac_pcm_block)" || return $?
    ctl_block="$(jasper_asound_outputd_dac_ctl_block)" || return $?
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "__OUTPUTD_DAC_PCM_BLOCK__" ]]; then
            printf '%s\n' "$pcm_block"
            continue
        fi
        if [[ "$line" == "__OUTPUTD_DAC_CTL_BLOCK__" ]]; then
            if [[ -n "$ctl_block" ]]; then
                printf '%s\n' "$ctl_block"
            fi
            continue
        fi
        line="${line//__DONGLE_CARD__/${DONGLE_CARD}}"
        printf '%s\n' "$line"
    done < "$source" > "$dest"
}
