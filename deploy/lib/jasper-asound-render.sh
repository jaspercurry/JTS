#!/usr/bin/env bash
# Shared ALSA template rendering helpers for JTS final-output routing.
#
# Inputs are the already-detected role variables owned by install.sh and
# jasper-audio-hardware-reconcile:
#   DONGLE_CARD, OUTPUT_DAC_CARD, OUTPUT_DAC_ID
#
# Keep this narrow. It renders the outputd_dac PCM block and simple
# placeholders in deploy/alsa/asoundrc.jasper; it is not a DAC abstraction.

jasper_asound_log_token() {
    local value="${1:-}"
    if [[ -z "$value" ]]; then
        printf 'direct'
        return
    fi
    printf '%s' "$value" | tr -c 'A-Za-z0-9_.:,-' '_'
}

jasper_asound_outputd_dac_pcm_block() {
    if [[ "${OUTPUT_DAC_ID:-}" == "dual_apple_usb_c_dac_4ch" ]]; then
        cat <<'EOF'
pcm.outputd_dac {
    type null
}
EOF
        return
    fi
    cat <<EOF
pcm.outputd_dac {
    type hw
    card ${OUTPUT_DAC_CARD}
    device 0
}
EOF
}

jasper_asound_render_template() {
    local source="$1" dest="$2"
    local block line
    block="$(jasper_asound_outputd_dac_pcm_block)"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "__OUTPUTD_DAC_PCM_BLOCK__" ]]; then
            printf '%s\n' "$block"
            continue
        fi
        line="${line//__DONGLE_CARD__/${DONGLE_CARD}}"
        line="${line//__OUTPUT_DAC_CARD__/${OUTPUT_DAC_CARD}}"
        printf '%s\n' "$line"
    done < "$source" > "$dest"
}
