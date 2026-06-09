#!/usr/bin/env bash
# Shared ALSA template rendering helpers for JTS final-output routing.
#
# Inputs are the already-detected role variables owned by install.sh and
# jasper-audio-hardware-reconcile:
#   DONGLE_CARD, OUTPUT_DAC_CARD, OUTPUT_DAC_ID, OUTPUT_DAC_ROUTE
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

jasper_asound_route_ignored() {
    local reason="$1" route="${2:-}"
    echo \
        "  Ignoring JASPER_OUTPUT_DAC_ROUTE=$(jasper_asound_log_token "$route"): reason=${reason} output_dac_id=${OUTPUT_DAC_ID:-unknown}" \
        >&2
}

jasper_asound_direct_outputd_dac_pcm_block() {
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

jasper_asound_routed_outputd_dac_pcm_block() {
    local route="$1"
    local left right left_idx right_idx mono_idx

    if [[ "$OUTPUT_DAC_ID" != "hifiberry_dac8x" && "$OUTPUT_DAC_ID" != "hifiberry_dac8x_studio" ]]; then
        jasper_asound_route_ignored "unsupported_dac" "$route"
        jasper_asound_direct_outputd_dac_pcm_block
        return
    fi

    if [[ "$route" =~ ^mono:([1-8])$ ]]; then
        mono_idx=$((BASH_REMATCH[1] - 1))
        cat <<EOF
pcm.outputd_dac {
    type route
    slave {
        pcm "hw:CARD=${OUTPUT_DAC_CARD},DEV=0"
        channels 8
    }
    ttable {
        0.${mono_idx} 0.5
        1.${mono_idx} 0.5
    }
}
EOF
        return
    fi

    if [[ "$route" =~ ^stereo:([1-8]),([1-8])$ ]]; then
        left="${BASH_REMATCH[1]}"
        right="${BASH_REMATCH[2]}"
        if [[ "$left" == "$right" ]]; then
            jasper_asound_route_ignored "duplicate_stereo_channel" "$route"
            jasper_asound_direct_outputd_dac_pcm_block
            return
        fi
        left_idx=$((left - 1))
        right_idx=$((right - 1))
        cat <<EOF
pcm.outputd_dac {
    type route
    slave {
        pcm "hw:CARD=${OUTPUT_DAC_CARD},DEV=0"
        channels 8
    }
    ttable {
        0.${left_idx} 1.0
        1.${right_idx} 1.0
    }
}
EOF
        return
    fi

    jasper_asound_route_ignored "invalid_route" "$route"
    jasper_asound_direct_outputd_dac_pcm_block
}

jasper_asound_outputd_dac_pcm_block() {
    case "${OUTPUT_DAC_ROUTE:-}" in
        ""|direct|passthrough)
            jasper_asound_direct_outputd_dac_pcm_block
            ;;
        *)
            jasper_asound_routed_outputd_dac_pcm_block "${OUTPUT_DAC_ROUTE}"
            ;;
    esac
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
