#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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
    if [[ "${OUTPUT_DAC_ID:-}" == "innomaker_hifi_amp_pro" ]]; then
        # The Merus amp's kernel DAI (ma120x0p.c) advertises only S24_LE|
        # S32_LE at continuous 44.1-192 kHz rates -- a driver-advertisement
        # limit, not a documented silicon one. JTS pins 48 kHz/2ch. outputd now
        # opens this alias at S32_LE ITSELF (widening its i16 program at the
        # final write), so the plug converts nothing -- and the pinned S32_LE
        # slave below is what still guarantees the hardware edge, since a plug
        # is invisible to outputd's own client-side format readback.
        # Belt-and-braces: the live gate is the registry's
        # supports_active_outputd_lane=False; this just fails loudly if that
        # ever drifts.
        if [[ "${OUTPUTD_ACTIVE_MODE:-0}" != "0" || -n "${OUTPUTD_ACTIVE_CHANNELS:-}" ]]; then
            echo "jasper-asound-render: InnoMaker HiFi AMP Pro is passive stereo only" >&2
            return 64
        fi
        cat <<EOF
pcm.outputd_dac {
    type plug
    slave {
        pcm {
            type hw
            card ${OUTPUT_DAC_CARD}
            device 0
        }
        rate 48000
        channels 2
        format S32_LE
    }
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
