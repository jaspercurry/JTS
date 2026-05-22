// Laptop-only spike: WebRTC AEC3 binding with the deep
// EchoCanceller3Config knobs exposed, by vendoring & statically
// linking webrtc-audio-processing v2.1 from PipeWire's upstream
// fork.
//
// The pattern: build v2.1 with default_library=static (already done
// at /tmp/webrtc-aec3-vendor/builddir/), include the internal
// modules/audio_processing/aec3/echo_canceller3.h header from the
// SOURCE tree (the install only ships public api/audio/ headers),
// write our own EchoControlFactory subclass whose Create() returns
// an EchoCanceller3 with a custom EchoCanceller3Config, pass it to
// AudioProcessingBuilder via SetEchoControlFactory.
//
// Mirrors jasper_aec3/src/aec3_binding.cpp's API shape so the
// offline test scripts can swap engines transparently. Adds:
//   - rs_subband_nearend            (bool)
//   - rs_snr_threshold              (float)
//   - rs_hold_duration              (int, frames)
//   - rs_high_bands_max_gain        (float)
//   - filter_refined_length_blocks  (int)
//   - ep_strength_bounded_erl       (bool)

#include <pybind11/pybind11.h>

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "api/audio/echo_canceller3_config.h"
#include "api/audio/echo_control.h"
#include "modules/audio_processing/aec3/echo_canceller3.h"
#include "modules/audio_processing/include/audio_processing.h"
#include "api/scoped_refptr.h"

namespace py = pybind11;

namespace {

constexpr int kSampleRate = 16000;
constexpr int kNumChannels = 1;
constexpr int kFrameSamples10ms = 160;

// Our custom factory: takes a pre-built EchoCanceller3Config and
// returns a new EchoCanceller3 each time AudioProcessing requests
// one. AudioProcessing owns the returned pointer.
class JasperEchoControlFactory : public webrtc::EchoControlFactory {
public:
    explicit JasperEchoControlFactory(webrtc::EchoCanceller3Config cfg)
        : cfg_(std::move(cfg)) {}

    std::unique_ptr<webrtc::EchoControl> Create(
        int sample_rate_hz,
        int num_render_channels,
        int num_capture_channels) override {
        return std::make_unique<webrtc::EchoCanceller3>(
            cfg_,
            /*multichannel_config=*/std::nullopt,
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
           // Stock AudioProcessing::Config knobs (mirror jasper_aec3)
           bool ns_enabled,
           const std::string& ns_level,
           bool agc1_enabled,
           int agc1_target_dbfs,
           int agc1_max_gain_db,
           // NEW: deep EchoCanceller3Config knobs
           bool rs_subband_nearend,
           float rs_snr_threshold,
           int rs_hold_duration,
           float rs_high_bands_max_gain,
           int filter_refined_length_blocks,
           bool ep_strength_bounded_erl)
        : stream_cfg_(kSampleRate, kNumChannels),
          stream_delay_ms_(stream_delay_ms) {
        // Build the deep EchoCanceller3Config from kwargs.
        webrtc::EchoCanceller3Config aec3_cfg;
        aec3_cfg.filter.refined.length_blocks =
            static_cast<size_t>(filter_refined_length_blocks);
        aec3_cfg.ep_strength.bounded_erl = ep_strength_bounded_erl;
        aec3_cfg.suppressor.use_subband_nearend_detection = rs_subband_nearend;
        aec3_cfg.suppressor.dominant_nearend_detection.snr_threshold =
            rs_snr_threshold;
        aec3_cfg.suppressor.dominant_nearend_detection.hold_duration =
            rs_hold_duration;
        aec3_cfg.suppressor.high_bands_suppression.max_gain_during_echo =
            rs_high_bands_max_gain;

        // Build the AudioProcessing pipeline with our factory plugged in.
        webrtc::AudioProcessingBuilder builder;
        builder.SetEchoControlFactory(
            std::make_unique<JasperEchoControlFactory>(std::move(aec3_cfg)));
        apm_ = builder.Create();
        if (!apm_) {
            throw std::runtime_error(
                "AudioProcessingBuilder::Create() returned null");
        }

        // Top-level AudioProcessing::Config: still need to enable the
        // pre/post stages (HPF, NS, AGC1). echo_canceller.enabled is
        // implicit when an EchoControlFactory is set.
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
    // v2.1's AudioProcessingBuilder::Create() returns scoped_refptr,
    // not unique_ptr like v1.3. (Refcounted, automatic release.)
    rtc::scoped_refptr<webrtc::AudioProcessing> apm_;
    webrtc::StreamConfig stream_cfg_;
    int stream_delay_ms_;
};

}  // namespace

PYBIND11_MODULE(_aec3_v2_spike, m) {
    m.doc() = "AEC3 v2.1 spike binding with deep EchoCanceller3Config knobs";
    py::class_<Aec3V2>(m, "Aec3V2")
        .def(py::init<int, bool, std::string, bool, int, int,
                      bool, float, int, float, int, bool>(),
             py::arg("stream_delay_ms") = 40,
             py::arg("ns_enabled") = true,
             py::arg("ns_level") = std::string("low"),
             py::arg("agc1_enabled") = true,
             py::arg("agc1_target_dbfs") = 9,
             py::arg("agc1_max_gain_db") = 18,
             // Defaults below = the research-report-recommended starting
             // values from HANDOFF-aec.md line 2294-2297.
             py::arg("rs_subband_nearend") = true,
             py::arg("rs_snr_threshold") = 20.0f,
             py::arg("rs_hold_duration") = 50,
             py::arg("rs_high_bands_max_gain") = 1.0f,
             py::arg("filter_refined_length_blocks") = 30,
             py::arg("ep_strength_bounded_erl") = true)
        .def("process", &Aec3V2::process,
             py::arg("mic"), py::arg("ref"));
}
