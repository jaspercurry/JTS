// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// pybind11 binding for WebRTC AEC3 from libwebrtc-audio-processing-1.
//
// Exposes a single Aec3 class with a process(mic, ref) method.
// The bridge passes equal-sized mic and ref buffers of int16 mono PCM
// at 16 kHz; WebRTC's AEC3 API requires 10 ms frames (160 samples at
// 16 kHz), so the binding splits the bridge's larger frame internally
// and processes pairs of (reverse, capture) frames in render-then-
// capture order per the API contract.
//
// Config rationale:
//   - echo_canceller.enabled = true, mobile_mode = false → desktop AEC3
//     (the modern frequency-domain canceler with residual suppressor),
//     not AECM (legacy mobile variant).
//   - high_pass_filter.enabled = true → trims sub-80 Hz rumble that
//     wastes adaptive-filter capacity.
//   - noise_suppression.enabled = true at kModerate → cleans up post-
//     AEC residual noise without introducing the "musical noise"
//     artifacts kHigh/kVeryHigh produce. openWakeWord's training set
//     includes mild background noise but not aggressive NS gating.
//   - gain_controller2.enabled = false → the bridge consumes raw mic 0
//     (channel 2 of the 6-ch XVF firmware), which has no chip-side AGC.
//     We could enable AGC2 here, but it changes downstream level in a
//     way openWakeWord's training distribution may not cover. Default
//     off; revisit during tuning.

#include <pybind11/pybind11.h>

#include <memory>
#include <stdexcept>
#include <string>

#include <modules/audio_processing/include/audio_processing.h>

#include "process_10ms.h"

namespace py = pybind11;

namespace {

class Aec3 {
public:
    Aec3(int stream_delay_ms = 40, bool enable_agc2 = false,
         bool ns_enabled = true,
         const std::string& ns_level = "low",
         bool agc1_enabled = false,
         int agc1_target_dbfs = 9,
         int agc1_max_gain_db = 18)
        : stream_cfg_(jasper_aec3::kSampleRate, jasper_aec3::kNumChannels),
          stream_delay_ms_(stream_delay_ms),
          enable_agc2_(enable_agc2) {
        // libwebrtc-audio-processing-1-3 (Debian Trixie) doesn't expose
        // EchoCanceller3Factory in the public headers, but AEC3 is the
        // *default* echo controller when echo_canceller.enabled = true
        // and mobile_mode = false; the legacy AECM only kicks in when
        // mobile_mode = true. No SetEchoControlFactory call is needed.
        // AudioProcessingBuilder::Create() returns a raw pointer in this
        // version (newer upstream returns unique_ptr); wrap manually.
        apm_.reset(webrtc::AudioProcessingBuilder().Create());
        if (!apm_) {
            throw std::runtime_error(
                "AudioProcessingBuilder::Create() returned null — "
                "libwebrtc-audio-processing is broken or misconfigured");
        }

        webrtc::AudioProcessing::Config cfg;
        cfg.echo_canceller.enabled = true;
        cfg.echo_canceller.mobile_mode = false;  // → AEC3
        cfg.high_pass_filter.enabled = true;
        cfg.noise_suppression.enabled = ns_enabled;
        // Trixie's libwebrtc-audio-processing-1 v1.3-3 exposes only
        // 4 NS levels (kLow / kModerate / kHigh / kVeryHigh); no
        // kVeryLow despite upstream WebRTC having one.
        using NSLevel =
            webrtc::AudioProcessing::Config::NoiseSuppression::Level;
        if      (ns_level == "low")        cfg.noise_suppression.level = NSLevel::kLow;
        else if (ns_level == "moderate")  cfg.noise_suppression.level = NSLevel::kModerate;
        else if (ns_level == "high")       cfg.noise_suppression.level = NSLevel::kHigh;
        else if (ns_level == "very_high") cfg.noise_suppression.level = NSLevel::kVeryHigh;
        else throw std::invalid_argument(
            "ns_level must be one of: low, moderate, high, very_high");
        // AGC1 is the legacy (well-trusted, used by Home Assistant
        // Voice PE) gain controller. In kAdaptiveDigital mode it
        // dynamically brings each utterance toward target_level_dbfs
        // with up to compression_gain_db of gain, then limits peaks.
        // This addresses post-AEC level variance — static MIC_GAIN_DB
        // either over- or under-shoots depending on instantaneous
        // music level, whereas AGC1 adapts per-utterance.
        cfg.gain_controller1.enabled = agc1_enabled;
        if (agc1_enabled) {
            cfg.gain_controller1.mode =
                webrtc::AudioProcessing::Config::GainController1::
                    kAdaptiveDigital;
            cfg.gain_controller1.target_level_dbfs = agc1_target_dbfs;
            cfg.gain_controller1.compression_gain_db = agc1_max_gain_db;
            cfg.gain_controller1.enable_limiter = true;
        }
        // AGC2 is the modern alternative. Empirically in our build it
        // doesn't do much because adaptive_digital sub-config defaults
        // off and we don't expose it; off by default. Use AGC1.
        cfg.gain_controller2.enabled = enable_agc2_;
        apm_->ApplyConfig(cfg);
    }

    py::bytes process(py::bytes mic_bytes, py::bytes ref_bytes) {
        return jasper_aec3::process_10ms(
            apm_.get(), stream_cfg_, stream_delay_ms_, mic_bytes, ref_bytes);
    }

private:
    std::unique_ptr<webrtc::AudioProcessing> apm_;
    webrtc::StreamConfig stream_cfg_;
    int stream_delay_ms_;
    bool enable_agc2_;
};

}  // namespace

PYBIND11_MODULE(_aec3, m) {
    m.doc() = "WebRTC AEC3 binding for jasper-aec-bridge "
              "(wraps libwebrtc-audio-processing-1 from Debian Trixie)";

    py::class_<Aec3>(m, "Aec3")
        .def(py::init<int, bool, bool, std::string, bool, int, int>(),
             py::arg("stream_delay_ms") = 40,
             py::arg("enable_agc2") = false,
             py::arg("ns_enabled") = true,
             py::arg("ns_level") = std::string("low"),
             py::arg("agc1_enabled") = false,
             py::arg("agc1_target_dbfs") = 9,
             py::arg("agc1_max_gain_db") = 18,
             "Construct an AEC3 instance (16 kHz mono). stream_delay_ms "
             "hints AEC3's delay estimator with the expected ref-to-mic "
             "delay; default 40 ms is the measured value for the JTS "
             "build. ns_enabled / ns_level toggle and tune the post-AEC "
             "noise-suppression stage (low / moderate / high / "
             "very_high — Trixie v1.3-3 lacks kVeryLow). Empirically "
             "ns_level='low' is the sweet spot for wake-word detection "
             "in our pipeline. agc1_enabled turns on WebRTC AGC1 in "
             "kAdaptiveDigital mode for post-AEC level normalization. "
             "agc1_target_dbfs maps to target_level_dbfs (positive "
             "value = dBFS-below-zero; range 0-31; 9 → -9 dBFS "
             "target). agc1_max_gain_db maps to compression_gain_db "
             "(soft-knee compressor parameter, range 0-90; NOT a "
             "'max gain ceiling' despite the param name). Empirically "
             "these knobs have minimal effect on Trixie's libwebrtc "
             "v1.3-3 (limiter dominates); the shipped benefit is "
             "consistent ~RMS 1213 across utterances regardless of "
             "music level. WebRTC AGC1 has no public attack/release "
             "parameter. Use this instead of enable_agc2 (which has "
             "no useful effect in our build).")
        .def("process", &Aec3::process,
             py::arg("mic"), py::arg("ref"),
             "Process one buffer of mic and ref bytes (equal-length "
             "int16 mono PCM @ 16 kHz, total samples must be a multiple "
             "of 10 ms = 160 samples). Returns AEC'd mic bytes of the "
             "same size. Internally splits into 10 ms windows and calls "
             "ProcessReverseStream + ProcessStream per window.");
}
