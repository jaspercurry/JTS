// pybind11 binding for WebRTC AEC3 vendored as webrtc-audio-processing v2.1
// (Pulseaudio's upstream fork: gitlab.freedesktop.org/pulseaudio/webrtc-audio-processing).
//
// This is the BEST_A-config binding: the system-installed
// libwebrtc-audio-processing-1 (v1.3-3, used by aec3_binding.cpp) doesn't
// expose EchoCanceller3Factory in its public headers, so the deep
// suppressor / filter / ERLE / stationarity knobs that BEST_A relies on
// are unreachable. We vendor v2.1 statically (built by install.sh into
// /opt/jasper/.cache/webrtc-2.1/), and write our own EchoControlFactory
// subclass that constructs EchoCanceller3 with a custom config.
//
// Default kwargs reflect the BEST_A config from the 2026-05-22 sweep
// campaign — see experiments/aec3-v2-deep-tune-spike/README.md for the
// methodology + per-knob rationale, and
// docs/HANDOFF-mic-quality-v2.md "Triple-stream architecture plan"
// for how this fits into the broader system.
//
// Production loader (jasper/cli/aec_bridge.py): tries `import _aec3_v2`
// first, falls back to `import _aec3` (v1.3-3 binding) if the v2 build
// is unavailable. The two share zero ABI; coexistence is fine.

#include <pybind11/pybind11.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "api/audio/echo_canceller3_config.h"
#include "api/audio/echo_control.h"
#include "api/scoped_refptr.h"
#include "modules/audio_processing/aec3/echo_canceller3.h"
#include "modules/audio_processing/include/audio_processing.h"

namespace py = pybind11;

namespace {

constexpr int kSampleRate = 16000;
constexpr int kNumChannels = 1;
constexpr int kFrameSamples10ms = 160;

// Our custom factory plugged into AudioProcessingBuilder. Constructs an
// EchoCanceller3 with the supplied (mutated) EchoCanceller3Config; the
// audio processor owns the returned pointer.
class JasperEchoControlFactory : public webrtc::EchoControlFactory {
public:
    explicit JasperEchoControlFactory(webrtc::EchoCanceller3Config cfg)
        : cfg_(std::move(cfg)) {}

    std::unique_ptr<webrtc::EchoControl> Create(
        int sample_rate_hz,
        int num_render_channels,
        int num_capture_channels) override {
        return std::make_unique<webrtc::EchoCanceller3>(
            cfg_, /*multichannel_config=*/std::nullopt,
            sample_rate_hz,
            static_cast<size_t>(num_render_channels),
            static_cast<size_t>(num_capture_channels));
    }

private:
    webrtc::EchoCanceller3Config cfg_;
};

class Aec3V2 {
public:
    Aec3V2(int stream_delay_ms,
           // Top-level AudioProcessing knobs (mirror v1 binding so the
           // existing JASPER_AEC_NS_*, _AGC1_*, _AGC2_ env vars work):
           bool enable_agc2,
           bool ns_enabled,
           const std::string& ns_level,
           bool agc1_enabled,
           int agc1_target_dbfs,
           int agc1_max_gain_db,
           // Filter
           int filter_refined_length_blocks,
           // EpStrength
           bool ep_strength_bounded_erl,
           float ep_strength_default_gain,
           // Erle (NLP depth caps + onset detection)
           float erle_max_l,
           float erle_max_h,
           bool erle_onset_detection,
           // EchoAudibility
           bool use_stationarity_properties,
           // Suppressor — HF + attack-rate knobs
           bool conservative_hf_suppression,
           float normal_mask_hf_enr_transparent,
           float normal_mask_hf_enr_suppress,
           float normal_mask_hf_emr_transparent,
           float normal_max_dec_factor_lf,
           int nearend_average_blocks,
           float nearend_mask_hf_enr_transparent,
           float nearend_mask_hf_enr_suppress,
           float nearend_mask_hf_emr_transparent,
           float nearend_max_dec_factor_lf,
           float nearend_max_inc_factor,
           float dominant_nearend_snr_threshold,
           int dominant_nearend_hold_duration,
           float dominant_nearend_enr_threshold,
           int dominant_nearend_trigger_threshold)
        : stream_cfg_(kSampleRate, kNumChannels),
          stream_delay_ms_(stream_delay_ms) {
        // Build the deep EchoCanceller3Config from kwargs.
        webrtc::EchoCanceller3Config aec3_cfg;
        aec3_cfg.filter.refined.length_blocks =
            static_cast<size_t>(filter_refined_length_blocks);
        aec3_cfg.ep_strength.bounded_erl = ep_strength_bounded_erl;
        aec3_cfg.ep_strength.default_gain = ep_strength_default_gain;
        aec3_cfg.erle.max_l = erle_max_l;
        aec3_cfg.erle.max_h = erle_max_h;
        aec3_cfg.erle.onset_detection = erle_onset_detection;
        aec3_cfg.echo_audibility.use_stationarity_properties =
            use_stationarity_properties;
        aec3_cfg.suppressor.conservative_hf_suppression =
            conservative_hf_suppression;
        aec3_cfg.suppressor.normal_tuning.mask_hf.enr_transparent =
            normal_mask_hf_enr_transparent;
        aec3_cfg.suppressor.normal_tuning.mask_hf.enr_suppress =
            normal_mask_hf_enr_suppress;
        aec3_cfg.suppressor.normal_tuning.mask_hf.emr_transparent =
            normal_mask_hf_emr_transparent;
        aec3_cfg.suppressor.normal_tuning.max_dec_factor_lf =
            normal_max_dec_factor_lf;
        aec3_cfg.suppressor.nearend_average_blocks =
            static_cast<size_t>(nearend_average_blocks);
        aec3_cfg.suppressor.nearend_tuning.mask_hf.enr_transparent =
            nearend_mask_hf_enr_transparent;
        aec3_cfg.suppressor.nearend_tuning.mask_hf.enr_suppress =
            nearend_mask_hf_enr_suppress;
        aec3_cfg.suppressor.nearend_tuning.mask_hf.emr_transparent =
            nearend_mask_hf_emr_transparent;
        aec3_cfg.suppressor.nearend_tuning.max_dec_factor_lf =
            nearend_max_dec_factor_lf;
        aec3_cfg.suppressor.nearend_tuning.max_inc_factor =
            nearend_max_inc_factor;
        aec3_cfg.suppressor.dominant_nearend_detection.snr_threshold =
            dominant_nearend_snr_threshold;
        aec3_cfg.suppressor.dominant_nearend_detection.hold_duration =
            dominant_nearend_hold_duration;
        aec3_cfg.suppressor.dominant_nearend_detection.enr_threshold =
            dominant_nearend_enr_threshold;
        aec3_cfg.suppressor.dominant_nearend_detection.trigger_threshold =
            dominant_nearend_trigger_threshold;

        webrtc::AudioProcessingBuilder builder;
        builder.SetEchoControlFactory(
            std::make_unique<JasperEchoControlFactory>(std::move(aec3_cfg)));
        apm_ = builder.Create();
        if (!apm_) {
            throw std::runtime_error(
                "AudioProcessingBuilder::Create() returned null — "
                "vendored libwebrtc-audio-processing-2 is broken or "
                "misconfigured");
        }

        // Top-level AudioProcessing::Config: still need to enable the
        // pre/post stages (HPF, NS, AGC1, AGC2). echo_canceller.enabled
        // is implicit when SetEchoControlFactory is used.
        webrtc::AudioProcessing::Config cfg;
        cfg.echo_canceller.enabled = true;
        cfg.high_pass_filter.enabled = true;
        cfg.noise_suppression.enabled = ns_enabled;
        using NSL = webrtc::AudioProcessing::Config::NoiseSuppression::Level;
        if      (ns_level == "low")        cfg.noise_suppression.level = NSL::kLow;
        else if (ns_level == "moderate")   cfg.noise_suppression.level = NSL::kModerate;
        else if (ns_level == "high")       cfg.noise_suppression.level = NSL::kHigh;
        else if (ns_level == "very_high")  cfg.noise_suppression.level = NSL::kVeryHigh;
        else throw std::invalid_argument(
            "ns_level must be one of: low, moderate, high, very_high");
        cfg.gain_controller1.enabled = agc1_enabled;
        if (agc1_enabled) {
            cfg.gain_controller1.mode = webrtc::AudioProcessing::Config::
                GainController1::kAdaptiveDigital;
            cfg.gain_controller1.target_level_dbfs = agc1_target_dbfs;
            cfg.gain_controller1.compression_gain_db = agc1_max_gain_db;
            cfg.gain_controller1.enable_limiter = true;
        }
        cfg.gain_controller2.enabled = enable_agc2;
        apm_->ApplyConfig(cfg);
    }

    py::bytes process(py::bytes mic_bytes, py::bytes ref_bytes) {
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
        const auto* mic =
            reinterpret_cast<const int16_t*>(mic_str.data());
        const auto* ref =
            reinterpret_cast<const int16_t*>(ref_str.data());
        std::vector<int16_t> output(total_samples);
        std::vector<int16_t> reverse_scratch(kFrameSamples10ms);
        for (size_t i = 0; i < total_samples; i += kFrameSamples10ms) {
            apm_->ProcessReverseStream(
                ref + i, stream_cfg_, stream_cfg_, reverse_scratch.data());
            apm_->set_stream_delay_ms(stream_delay_ms_);
            apm_->ProcessStream(
                mic + i, stream_cfg_, stream_cfg_, output.data() + i);
        }
        return py::bytes(
            reinterpret_cast<const char*>(output.data()),
            total_samples * sizeof(int16_t));
    }

private:
    // v2.1's AudioProcessingBuilder::Create() returns scoped_refptr
    // (vs v1.3's raw unique_ptr). Refcounted, automatic release.
    rtc::scoped_refptr<webrtc::AudioProcessing> apm_;
    webrtc::StreamConfig stream_cfg_;
    int stream_delay_ms_;
};

}  // namespace

PYBIND11_MODULE(_aec3_v2, m) {
    m.doc() = "WebRTC AEC3 v2.1 binding (BEST_A config) for jasper-aec-bridge";

    py::class_<Aec3V2>(m, "Aec3V2")
        .def(py::init<int, bool, bool, std::string, bool, int, int,
                      int, bool, float, float, float, bool, bool, bool,
                      float, float, float, float, int, float, float, float,
                      float, float, float, int, float, int>(),
             py::arg("stream_delay_ms") = 40,
             // Top-level AudioProcessing (mirror v1 defaults)
             py::arg("enable_agc2") = false,
             py::arg("ns_enabled") = true,
             py::arg("ns_level") = std::string("low"),
             py::arg("agc1_enabled") = true,        // BEST_A default (v1 default was false)
             py::arg("agc1_target_dbfs") = 9,
             py::arg("agc1_max_gain_db") = 18,
             // === BEST_A canonical config below ===
             // Filter — BEST_A uses 30, default is 13
             py::arg("filter_refined_length_blocks") = 30,
             // EpStrength — bounded_erl=False restores Transparent Mode;
             // default_gain=0.3 (vs default 1.0)
             py::arg("ep_strength_bounded_erl") = false,
             py::arg("ep_strength_default_gain") = 0.3f,
             // Erle — the BEST_A headline knobs (defaults 4.0, 1.5; V2FIXED 2.0, 1.2)
             py::arg("erle_max_l") = 1.5f,
             py::arg("erle_max_h") = 1.0f,
             py::arg("erle_onset_detection") = false,
             // EchoAudibility
             py::arg("use_stationarity_properties") = true,
             // Suppressor — HF asymmetry fix, slow attack, and
             // corpus-only near-end tuning.
             py::arg("conservative_hf_suppression") = true,
             py::arg("normal_mask_hf_enr_transparent") = 0.3f,  // LF parity (default 0.07)
             py::arg("normal_mask_hf_enr_suppress") = 0.4f,     // LF parity (default 0.1)
             py::arg("normal_mask_hf_emr_transparent") = 0.3f,
             py::arg("normal_max_dec_factor_lf") = 0.05f,        // 5× slower than default 0.25
             py::arg("nearend_average_blocks") = 4,
             py::arg("nearend_mask_hf_enr_transparent") = 0.1f,
             py::arg("nearend_mask_hf_enr_suppress") = 0.3f,
             py::arg("nearend_mask_hf_emr_transparent") = 0.3f,
             py::arg("nearend_max_dec_factor_lf") = 0.25f,
             py::arg("nearend_max_inc_factor") = 2.0f,
             py::arg("dominant_nearend_snr_threshold") = 30.0f,
             py::arg("dominant_nearend_hold_duration") = 50,
             py::arg("dominant_nearend_enr_threshold") = 0.25f,
             py::arg("dominant_nearend_trigger_threshold") = 12,
             "BEST_A AEC3 binding via vendored webrtc-audio-processing v2.1. "
             "All knobs default to the BEST_A canonical config from the "
             "2026-05-22 tuning campaign — see "
             "experiments/aec3-v2-deep-tune-spike/README.md for rationale "
             "per knob. Override any kwarg to deviate from BEST_A.")
        .def("process", &Aec3V2::process,
             py::arg("mic"), py::arg("ref"),
             "Process one buffer of mic and ref bytes (equal-length int16 "
             "mono PCM @ 16 kHz, total samples must be a multiple of 10 ms "
             "= 160 samples). Returns AEC'd mic bytes of the same size.");
}
