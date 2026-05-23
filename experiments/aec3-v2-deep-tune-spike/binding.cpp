// AEC3 v2.1 binding — REVISION 3 (2026-05-22 night)
// Full tuning surface: all knobs the research surfaced.

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
        int sample_rate_hz, int num_render_channels, int num_capture_channels) override {
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
    // Knobs are passed as a dict to keep the constructor manageable.
    Aec3V2(const py::dict& opts) : stream_cfg_(kSampleRate, kNumChannels) {
        stream_delay_ms_ = opts.contains("stream_delay_ms") ? opts["stream_delay_ms"].cast<int>() : 40;

        webrtc::EchoCanceller3Config c;

        auto get = [&](const char* key, auto def) {
            using T = decltype(def);
            if (opts.contains(key)) return opts[key].cast<T>();
            return def;
        };

        // Filter
        c.filter.refined.length_blocks = static_cast<size_t>(get("filter_refined_length_blocks", 13));

        // EpStrength
        c.ep_strength.bounded_erl = get("ep_strength_bounded_erl", false);
        c.ep_strength.default_gain = get("ep_strength_default_gain", 1.0f);
        c.ep_strength.default_len = get("ep_strength_default_len", 0.83f);
        c.ep_strength.nearend_len = get("ep_strength_nearend_len", 0.83f);
        c.ep_strength.echo_can_saturate = get("ep_strength_echo_can_saturate", true);

        // Erle
        c.erle.max_l = get("erle_max_l", 4.0f);
        c.erle.max_h = get("erle_max_h", 1.5f);
        c.erle.onset_detection = get("erle_onset_detection", true);

        // EchoAudibility
        c.echo_audibility.use_stationarity_properties = get("use_stationarity_properties", false);
        c.echo_audibility.audibility_threshold_hf = get("audibility_threshold_hf", 10.0f);
        c.echo_audibility.audibility_threshold_mf = get("audibility_threshold_mf", 10.0f);
        c.echo_audibility.audibility_threshold_lf = get("audibility_threshold_lf", 10.0f);

        // ComfortNoise
        c.comfort_noise.noise_floor_dbfs = get("comfort_noise_floor_dbfs", -96.03406f);

        // Suppressor — top-level
        c.suppressor.conservative_hf_suppression = get("conservative_hf_suppression", false);
        c.suppressor.nearend_average_blocks = static_cast<size_t>(get("nearend_average_blocks", 4));

        // Suppressor — normal tuning (when echo dominates)
        c.suppressor.normal_tuning.mask_hf.enr_transparent = get("normal_mask_hf_enr_transparent", 0.07f);
        c.suppressor.normal_tuning.mask_hf.enr_suppress    = get("normal_mask_hf_enr_suppress", 0.1f);
        c.suppressor.normal_tuning.mask_hf.emr_transparent = get("normal_mask_hf_emr_transparent", 0.3f);
        c.suppressor.normal_tuning.max_dec_factor_lf       = get("normal_max_dec_factor_lf", 0.25f);
        c.suppressor.normal_tuning.max_inc_factor          = get("normal_max_inc_factor", 2.0f);

        // Suppressor — nearend tuning (when speech dominates)
        c.suppressor.nearend_tuning.mask_hf.enr_transparent = get("nearend_mask_hf_enr_transparent", 0.1f);
        c.suppressor.nearend_tuning.mask_hf.enr_suppress    = get("nearend_mask_hf_enr_suppress", 0.3f);
        c.suppressor.nearend_tuning.mask_hf.emr_transparent = get("nearend_mask_hf_emr_transparent", 0.3f);
        c.suppressor.nearend_tuning.max_dec_factor_lf       = get("nearend_max_dec_factor_lf", 0.25f);
        c.suppressor.nearend_tuning.max_inc_factor          = get("nearend_max_inc_factor", 2.0f);

        // Suppressor — dominant nearend
        c.suppressor.use_subband_nearend_detection            = get("use_subband_nearend_detection", false);
        c.suppressor.dominant_nearend_detection.snr_threshold = get("dnd_snr_threshold", 30.0f);
        c.suppressor.dominant_nearend_detection.hold_duration = get("dnd_hold_duration", 50);
        c.suppressor.dominant_nearend_detection.enr_threshold = get("dnd_enr_threshold", 0.25f);

        webrtc::AudioProcessingBuilder builder;
        builder.SetEchoControlFactory(std::make_unique<JasperEchoControlFactory>(std::move(c)));
        apm_ = builder.Create();
        if (!apm_) throw std::runtime_error("APM::Create() returned null");

        webrtc::AudioProcessing::Config apc;
        apc.echo_canceller.enabled = true;
        apc.high_pass_filter.enabled = true;
        apc.noise_suppression.enabled = get("ns_enabled", true);
        std::string ns_level = opts.contains("ns_level") ? opts["ns_level"].cast<std::string>() : "low";
        using NSL = webrtc::AudioProcessing::Config::NoiseSuppression::Level;
        if (ns_level == "low") apc.noise_suppression.level = NSL::kLow;
        else if (ns_level == "moderate") apc.noise_suppression.level = NSL::kModerate;
        else if (ns_level == "high") apc.noise_suppression.level = NSL::kHigh;
        else if (ns_level == "very_high") apc.noise_suppression.level = NSL::kVeryHigh;
        else throw std::invalid_argument("bad ns_level");
        apc.gain_controller1.enabled = get("agc1_enabled", true);
        if (apc.gain_controller1.enabled) {
            apc.gain_controller1.mode = webrtc::AudioProcessing::Config::GainController1::kAdaptiveDigital;
            apc.gain_controller1.target_level_dbfs = get("agc1_target_dbfs", 9);
            apc.gain_controller1.compression_gain_db = get("agc1_max_gain_db", 18);
            apc.gain_controller1.enable_limiter = true;
        }
        apm_->ApplyConfig(apc);
    }

    py::bytes process(py::bytes mic_bytes, py::bytes ref_bytes) {
        const std::string mic_str = mic_bytes;
        const std::string ref_str = ref_bytes;
        if (mic_str.size() != ref_str.size())
            throw std::invalid_argument("mic + ref must be same length");
        const size_t total_bytes = mic_str.size();
        if (total_bytes % sizeof(int16_t) != 0)
            throw std::invalid_argument("must be int16-aligned");
        const size_t total_samples = total_bytes / sizeof(int16_t);
        if (total_samples % kFrameSamples10ms != 0)
            throw std::invalid_argument("must be 10 ms multiple");
        const auto* mic = reinterpret_cast<const int16_t*>(mic_str.data());
        const auto* ref = reinterpret_cast<const int16_t*>(ref_str.data());
        std::vector<int16_t> out(total_samples);
        std::vector<int16_t> rev(kFrameSamples10ms);
        for (size_t i = 0; i < total_samples; i += kFrameSamples10ms) {
            apm_->ProcessReverseStream(ref + i, stream_cfg_, stream_cfg_, rev.data());
            apm_->set_stream_delay_ms(stream_delay_ms_);
            apm_->ProcessStream(mic + i, stream_cfg_, stream_cfg_, out.data() + i);
        }
        return py::bytes(reinterpret_cast<const char*>(out.data()), total_samples * sizeof(int16_t));
    }

private:
    rtc::scoped_refptr<webrtc::AudioProcessing> apm_;
    webrtc::StreamConfig stream_cfg_;
    int stream_delay_ms_;
};

}  // namespace

PYBIND11_MODULE(_aec3_v2_spike, m) {
    m.doc() = "AEC3 v2.1 binding (rev 3) — full tuning surface via py::dict";
    py::class_<Aec3V2>(m, "Aec3V2")
        .def(py::init<const py::dict&>(),
             "All knobs passed as a single py::dict. Defaults = webrtc defaults.")
        .def("process", &Aec3V2::process, py::arg("mic"), py::arg("ref"));
}
