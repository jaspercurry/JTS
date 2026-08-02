// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

#pragma once

#include <pybind11/pybind11.h>

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include <modules/audio_processing/include/audio_processing.h>

namespace jasper_aec3 {

constexpr int kSampleRate = 16000;
constexpr int kNumChannels = 1;
constexpr int kFrameSamples10ms = 160;

// The wire validation and render-before-capture loop are identical for the
// distro AEC3 and vendored v2 bindings. Keeping them here prevents their Python
// surfaces from drifting while the constructors remain version-specific.
inline pybind11::bytes process_10ms(
    webrtc::AudioProcessing* apm,
    const webrtc::StreamConfig& stream_cfg,
    int stream_delay_ms,
    pybind11::bytes mic_bytes,
    pybind11::bytes ref_bytes) {
    const std::string mic_str = mic_bytes;
    const std::string ref_str = ref_bytes;
    if (mic_str.size() != ref_str.size()) {
        throw std::invalid_argument(
            "mic and ref byte buffers must be the same length");
    }
    const size_t total_bytes = mic_str.size();
    if (total_bytes == 0) {
        throw std::invalid_argument("empty buffer");
    }
    if (total_bytes % sizeof(int16_t) != 0) {
        throw std::invalid_argument(
            "buffer size must be a multiple of int16 (2 bytes)");
    }
    const size_t total_samples = total_bytes / sizeof(int16_t);
    if (total_samples % kFrameSamples10ms != 0) {
        throw std::invalid_argument(
            "buffer must be a multiple of 10 ms "
            "(160 samples @ 16 kHz mono = 320 bytes)");
    }

    const auto* mic = reinterpret_cast<const int16_t*>(mic_str.data());
    const auto* ref = reinterpret_cast<const int16_t*>(ref_str.data());
    std::vector<int16_t> output(total_samples);
    std::vector<int16_t> reverse_scratch(kFrameSamples10ms);
    for (size_t i = 0; i < total_samples; i += kFrameSamples10ms) {
        apm->ProcessReverseStream(
            ref + i, stream_cfg, stream_cfg, reverse_scratch.data());
        apm->set_stream_delay_ms(stream_delay_ms);
        apm->ProcessStream(
            mic + i, stream_cfg, stream_cfg, output.data() + i);
    }
    return pybind11::bytes(
        reinterpret_cast<const char*>(output.data()),
        total_samples * sizeof(int16_t));
}

}  // namespace jasper_aec3
