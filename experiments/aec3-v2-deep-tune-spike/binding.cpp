// AEC3 v2.1 binding — REVISION 2 (2026-05-22 night)
//
// Adds knobs the research surfaced that round-1 V2tune missed:
//   - ep_strength.bounded_erl = false by default (was true, which
//     SILENTLY DISABLES Transparent Mode — the music-friendly mode
//     we actually want)
//   - suppressor.conservative_hf_suppression (bool) — the single-
//     line "less aggressive HF suppression" knob
//   - suppressor.normal_tuning.mask_hf.{enr_transparent,enr_suppress,
//     emr_transparent} — WebRTC's HF defaults are 4x more aggressive
//     than LF defaults (0.07 / 0.1 / 0.3 vs 0.3 / 0.4 / 0.3); we
//     can flatten to LF parity
//   - suppressor.normal_tuning.max_dec_factor_lf — gain attack rate
//     (default 0.25; lower = slower attack = less perceived pumping)
//   - erle.onset_detection (bool) — kills the onset transient
//   - erle.max_l / erle.max_h — caps on NLP depth per band
//   - echo_audibility.use_stationarity_properties (bool) — lets
//     music's stationarity gate engage, reducing suppression
//   - ep_strength.default_gain — prior on echo path strength
//     (default 1.0; lower = AEC3 assumes echo is weaker = less
//     over-suppression)

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

class JasperEchoControlFactory : public webrtc::EchoControlFactory {
public:
    explicit JasperEchoControlFactory(webrtc::EchoCanceller3Config cfg)
        : cfg_(std::move(cfg)) {}

    std::unique_ptr<webrtc::EchoControl> Create(
        int sample_rate_hz,
        int num_render_channels,
        int num_capture_channels) override {
        return std::make_unique<webrtc::EchoCanceller3>(
            cfg_, std::nullopt, sample_rate_hz,
            static_cast<size_t>(num_render_channels),
            static_cast<size_t>(num_capture_channels));
    }

private:
    webrtc::EchoCanceller3Config cfg_;
};

class Aec3V2 {
public:
    Aec3V2(int stream_delay_ms,
           // Top-level AudioProcessing::Config knobs
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
           // Erle (NEW)
           float erle_max_l,
           float erle_max_h,
           bool erle_onset_detection,
           // EchoAudibility (NEW)
           bool use_stationarity_properties,
           // Suppressor — round 1 knobs
           bool rs_subband_nearend,
           float rs_snr_threshold,
           int rs_hold_duration,
           // Suppressor — NEW knobs from research
           bool conservative_hf_suppression,
           float mask_hf_enr_transparent,
           float mask_hf_enr_suppress,
           float mask_hf_emr_transparent,
           float normal_max_dec_factor_lf)
        : stream_cfg_(kSampleRate, kNumChannels),
          stream_delay_ms_(stream_delay_ms) {
        webrtc::EchoCanceller3Config aec3_cfg;

        // Filter
        aec3_cfg.filter.refined.length_blocks =
            static_cast<size_t>(filter_refined_length_blocks);

        // EpStrength
        aec3_cfg.ep_strength.bounded_erl = ep_strength_bounded_erl;
        aec3_cfg.ep_strength.default_gain = ep_strength_default_gain;

        // Erle (caps how deep NLP can attenuate; lower = less pumping
        // but less cancellation)
        aec3_cfg.erle.max_l = erle_max_l;
        aec3_cfg.erle.max_h = erle_max_h;
        aec3_cfg.erle.onset_detection = erle_onset_detection;

        // EchoAudibility
        aec3_cfg.echo_audibility.use_stationarity_properties =
            use_stationarity_properties;

        // Suppressor — dominant nearend
        aec3_cfg.suppressor.use_subband_nearend_detection = rs_subband_nearend;
        aec3_cfg.suppressor.dominant_nearend_detection.snr_threshold =
            rs_snr_threshold;
        aec3_cfg.suppressor.dominant_nearend_detection.hold_duration =
            rs_hold_duration;

        // Suppressor — the new HF knobs
        aec3_cfg.suppressor.conservative_hf_suppression =
            conservative_hf_suppression;
        aec3_cfg.suppressor.normal_tuning.mask_hf.enr_transparent =
            mask_hf_enr_transparent;
        aec3_cfg.suppressor.normal_tuning.mask_hf.enr_suppress =
            mask_hf_enr_suppress;
        aec3_cfg.suppressor.normal_tuning.mask_hf.emr_transparent =
            mask_hf_emr_transparent;
        aec3_cfg.suppressor.normal_tuning.max_dec_factor_lf =
            normal_max_dec_factor_lf;

        // Build APM with our factory
        webrtc::AudioProcessingBuilder builder;
        builder.SetEchoControlFactory(
            std::make_unique<JasperEchoControlFactory>(std::move(aec3_cfg)));
        apm_ = builder.Create();
        if (!apm_) {
            throw std::runtime_error(
                "AudioProcessingBuilder::Create() returned null");
        }

        webrtc::AudioProcessing::Config cfg;
        cfg.echo_canceller.enabled = true;
        cfg.high_pass_filter.enabled = true;
        cfg.noise_suppression.enabled = ns_enabled;
        using NSLevel = webrtc::AudioProcessing::Config::NoiseSuppression::Level;
        if      (ns_level == "low")        cfg.noise_suppression.level = NSLevel::kLow;
        else if (ns_level == "moderate")   cfg.noise_suppression.level = NSLevel::kModerate;
        else if (ns_level == "high")       cfg.noise_suppression.level = NSLevel::kHigh;
        else if (ns_level == "very_high")  cfg.noise_suppression.level = NSLevel::kVeryHigh;
        else throw std::invalid_argument("ns_level invalid");
        cfg.gain_controller1.enabled = agc1_enabled;
        if (agc1_enabled) {
            cfg.gain_controller1.mode = webrtc::AudioProcessing::Config::
                GainController1::kAdaptiveDigital;
            cfg.gain_controller1.target_level_dbfs = agc1_target_dbfs;
            cfg.gain_controller1.compression_gain_db = agc1_max_gain_db;
            cfg.gain_controller1.enable_limiter = true;
        }
        apm_->ApplyConfig(cfg);
    }

    py::bytes process(py::bytes mic_bytes, py::bytes ref_bytes) {
        const std::string mic_str = mic_bytes;
        const std::string ref_str = ref_bytes;
        if (mic_str.size() != ref_str.size()) {
            throw std::invalid_argument("mic + ref must be same length");
        }
        const size_t total_bytes = mic_str.size();
        if (total_bytes % sizeof(int16_t) != 0) {
            throw std::invalid_argument("must be int16 (2-byte) aligned");
        }
        const size_t total_samples = total_bytes / sizeof(int16_t);
        if (total_samples % kFrameSamples10ms != 0) {
            throw std::invalid_argument(
                "must be a multiple of 10 ms (160 samples)");
        }
        const auto* mic = reinterpret_cast<const int16_t*>(mic_str.data());
        const auto* ref = reinterpret_cast<const int16_t*>(ref_str.data());
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
    rtc::scoped_refptr<webrtc::AudioProcessing> apm_;
    webrtc::StreamConfig stream_cfg_;
    int stream_delay_ms_;
};

}  // namespace

PYBIND11_MODULE(_aec3_v2_spike, m) {
    m.doc() = "AEC3 v2.1 binding (rev 2) with full deep-tune surface";
    py::class_<Aec3V2>(m, "Aec3V2")
        .def(py::init<int, bool, std::string, bool, int, int,
                      int, bool, float,
                      float, float, bool,
                      bool,
                      bool, float, int,
                      bool, float, float, float, float>(),
             py::arg("stream_delay_ms") = 40,
             py::arg("ns_enabled") = true,
             py::arg("ns_level") = std::string("low"),
             py::arg("agc1_enabled") = true,
             py::arg("agc1_target_dbfs") = 9,
             py::arg("agc1_max_gain_db") = 18,
             // Filter
             py::arg("filter_refined_length_blocks") = 13,    // webrtc default
             // EpStrength
             py::arg("ep_strength_bounded_erl") = false,       // FIXED: was true (kills Transparent Mode)
             py::arg("ep_strength_default_gain") = 1.0f,       // webrtc default
             // Erle
             py::arg("erle_max_l") = 4.0f,                     // webrtc default
             py::arg("erle_max_h") = 1.5f,                     // webrtc default
             py::arg("erle_onset_detection") = true,           // webrtc default
             // EchoAudibility
             py::arg("use_stationarity_properties") = false,   // webrtc default
             // Suppressor — dominant nearend
             py::arg("rs_subband_nearend") = false,            // webrtc default
             py::arg("rs_snr_threshold") = 30.0f,              // webrtc default
             py::arg("rs_hold_duration") = 50,                 // webrtc default
             // Suppressor — NEW knobs
             py::arg("conservative_hf_suppression") = false,   // webrtc default
             py::arg("mask_hf_enr_transparent") = 0.07f,       // webrtc default (4x more aggressive than LF!)
             py::arg("mask_hf_enr_suppress") = 0.1f,           // webrtc default
             py::arg("mask_hf_emr_transparent") = 0.3f,        // webrtc default
             py::arg("normal_max_dec_factor_lf") = 0.25f       // webrtc default
            )
        .def("process", &Aec3V2::process,
             py::arg("mic"), py::arg("ref"));
}
