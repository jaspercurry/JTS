// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

// pybind11 binding: a spa_dll rate controller + a windowed-sinc resampler
// for jasper-usbsink's capture-follower rate matching.
//
// This file is the C++ MIRROR of two Rust crates:
//   - rust/jasper-clock/src/lib.rs   — the spa_dll second-order DLL plus the
//     operational hardening (max_error slew clamp, max_resync hard-jump,
//     variance-driven adaptive bandwidth, lock tracking, error statistics).
//   - rust/jasper-resampler/src/lib.rs — the Blackman-Harris windowed-sinc
//     interpolation table, the AudioRing streaming cursor, and the
//     RateController output-ppm clamp.
//
// Why duplicated, not bound through the Rust crate: this repo has ZERO
// PyO3/maturin/cdylib toolchain (verified by grep across *.toml/*.sh/*.py);
// adding one would introduce an entirely new build path. Instead the same
// algorithm is implemented once in Rust (consumed by the jasper-outputd
// daemon) and once here in C++ (consumed by Python/usbsink), exactly like
// jasper-fanin (Rust) and the usbsink Python FIFO writer already duplicate
// the FIFO writer shape. The two resampler implementations are pinned to
// ≤1 LSB byte-identity by tests/test_resampler_contract.py against a
// committed golden vector — so an edit to one side that is not mirrored is
// a hard CI failure.
//
// The math below is byte-for-byte the Rust f64 ops: the same sinc/window
// coefficients, the same table normalization, the same round-to-nearest
// i16 clamp, the same DLL coefficient formulas, the same negated-error sign
// (the capture-follower convention). Do NOT "clean up" any expression here
// without making the identical change in BOTH Rust crates in lockstep and
// regenerating the golden vector.
//
// Sample-width awareness (the architect's call, per the build spec's risk
// #4): the streaming resampler operates on int16 frames for the aloop lane
// (the queued payload is already S16) AND on int32 frames for the FIFO lean
// lane (the queued payload is full-width S32_LE). Rather than resample an
// S16 high-half view of the S32 stream and lose the low 16 bits, the binding
// is parametrized by bytes-per-sample (2 or 4) so both usbsink output modes
// share ONE exact resampling path. The 16-bit path is the cross-language
// contract reference (the Rust crate is i16-only); the 32-bit path is
// C++-only and pinned by the Python integration tests' frame-count +
// round-trip assertions.

#include <pybind11/pybind11.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace {

// ===========================================================================
// Windowed-sinc kernel constants — mirror rust/jasper-resampler/src/lib.rs.
// ===========================================================================

// Half-width of the interpolation kernel, in input frames.
constexpr std::int64_t kRadiusFrames = 16;
// Number of FIR taps per phase (2 * radius + 1).
constexpr std::size_t kTaps = static_cast<std::size_t>(kRadiusFrames) * 2 + 1;
// Number of precomputed sub-sample phases.
constexpr std::size_t kPhases = 2048;
// Sinc cutoff as a fraction of Nyquist.
constexpr double kCutoff = 0.97;

// ===========================================================================
// spa_dll bandwidth band + lock warmup — mirror rust/jasper-clock/src/lib.rs.
// ===========================================================================

constexpr double kBwMax = 0.128;
constexpr double kBwMin = 0.016;
// `update` calls a freshly-(re)initialised loop must run before its lock
// verdict is trusted (jasper-clock LOCK_WARMUP_UPDATES).
constexpr std::uint64_t kLockWarmupUpdates = 64;

// Default output-ppm safety clamp — mirrors content_bridge's
// DEFAULT_CONTENT_BRIDGE_MAX_ADJUST_PPM.
constexpr double kDefaultMaxAdjustPpm = 500.0;

constexpr double kPi = 3.14159265358979323846;

// ---------------------------------------------------------------------------
// Kernel math — lifted verbatim from content_bridge.rs / jasper-resampler so
// the daemon path and this binding agree bit-for-bit.
// ---------------------------------------------------------------------------

double sinc(double x) {
    if (std::fabs(x) < 1.0e-8) {
        return 1.0;
    }
    const double pix = kPi * x;
    return std::sin(pix) / pix;
}

double blackman_harris(double x) {
    constexpr double a0 = 0.35875;
    constexpr double a1 = 0.48829;
    constexpr double a2 = 0.14128;
    constexpr double a3 = 0.01168;
    const double phase = 2.0 * kPi * x;
    return a0 - a1 * std::cos(phase) + a2 * std::cos(2.0 * phase) -
           a3 * std::cos(3.0 * phase);
}

// Round-to-nearest, saturating to the i16 range — the exact rounding the Rust
// crate's clamp_i16 uses, so cross-language output matches at the LSB.
std::int16_t clamp_i16(double value) {
    const double r = std::round(value);
    const double lo = static_cast<double>(INT16_MIN);
    const double hi = static_cast<double>(INT16_MAX);
    return static_cast<std::int16_t>(std::min(std::max(r, lo), hi));
}

// Round-to-nearest, saturating to the i32 range — the S32 lean-lane analogue
// of clamp_i16. Same rounding rule; only the saturation bound differs. There
// is no Rust counterpart (the Rust crate is i16-only), so this path is
// C++-only and covered by the Python integration tests, not the byte-identity
// contract test.
std::int32_t clamp_i32(double value) {
    const double r = std::round(value);
    const double lo = static_cast<double>(INT32_MIN);
    const double hi = static_cast<double>(INT32_MAX);
    // INT32_MAX is not exactly representable as a double; the clamp uses the
    // double bound and the cast truncates toward it, which matches a saturating
    // round for every value the resampler can produce.
    const double clamped = std::min(std::max(r, lo), hi);
    if (clamped >= hi) {
        return INT32_MAX;
    }
    if (clamped <= lo) {
        return INT32_MIN;
    }
    return static_cast<std::int32_t>(clamped);
}

// A precomputed windowed-sinc interpolation table: kPhases rows of kTaps
// coefficients. Built ONCE per RateResampler (in the ctor), ~540 KB, never
// rebuilt per block. Mirrors SincTable::new / build_sinc_table.
class SincTable {
public:
    SincTable() {
        phases_.resize(kPhases);
        for (std::size_t phase = 0; phase < kPhases; ++phase) {
            const double frac =
                static_cast<double>(phase) / static_cast<double>(kPhases);
            std::array<double, kTaps>& coeffs = phases_[phase];
            double norm = 0.0;
            for (std::size_t tap = 0; tap < kTaps; ++tap) {
                const std::int64_t offset =
                    static_cast<std::int64_t>(tap) - kRadiusFrames;
                const double distance = frac - static_cast<double>(offset);
                const double window =
                    blackman_harris(static_cast<double>(tap) /
                                    static_cast<double>(kTaps - 1));
                coeffs[tap] = sinc(distance * kCutoff) * kCutoff * window;
                norm += coeffs[tap];
            }
            if (std::fabs(norm) > 1.0e-9) {
                for (std::size_t tap = 0; tap < kTaps; ++tap) {
                    coeffs[tap] /= norm;
                }
            }
        }
    }

    const std::array<double, kTaps>& phase(std::size_t idx) const {
        return phases_[idx];
    }

private:
    std::vector<std::array<double, kTaps>> phases_;
};

// A fixed-capacity interleaved-sample ring addressed by a monotonic frame
// counter. Mirrors rust/jasper-resampler AudioRing, but stores f64 samples so
// the SAME ring serves both the S16 and S32 paths (the caller scales raw
// integers in / clamps out per the sample width). The interpolation math is
// identical regardless of width because it runs in f64 either way.
//
// For the S16 path the stored f64 values are exact int16s, so this is
// bit-identical to the Rust i16 ring (whose sample() returns i16 promoted to
// f64 inside interpolate). For the S32 path it carries the full 32-bit value.
class AudioRing {
public:
    AudioRing(std::size_t capacity_frames, std::size_t channels)
        : channels_(channels),
          capacity_frames_(capacity_frames),
          read_frame_(0),
          write_frame_(0) {
        data_.assign(capacity_frames * channels, 0.0);
    }

    std::size_t fill_frames() const {
        return static_cast<std::size_t>(write_frame_ - read_frame_);
    }

    std::uint64_t read_frame() const { return read_frame_; }
    std::uint64_t write_frame() const { return write_frame_; }

    // Push interleaved f64 frames, dropping oldest-first on overflow. Mirrors
    // AudioRing::push_interleaved.
    void push_interleaved(const double* samples, std::size_t frame_count) {
        for (std::size_t frame = 0; frame < frame_count; ++frame) {
            if (fill_frames() == capacity_frames_) {
                read_frame_ += 1;
            }
            const std::size_t dst =
                (static_cast<std::size_t>(write_frame_) % capacity_frames_) *
                channels_;
            const std::size_t src = frame * channels_;
            for (std::size_t c = 0; c < channels_; ++c) {
                data_[dst + c] = samples[src + c];
            }
            write_frame_ += 1;
        }
    }

    void clear() { read_frame_ = write_frame_; }

    // Advance read_frame up to (but not past) `frame`. Mirrors drop_before.
    void drop_before(std::int64_t frame) {
        if (frame <= 0) {
            return;
        }
        const std::uint64_t f = static_cast<std::uint64_t>(frame);
        if (f > read_frame_) {
            read_frame_ = std::min(f, write_frame_);
        }
    }

    // Read one channel of one frame, 0 outside [read_frame, write_frame).
    // Mirrors AudioRing::sample.
    double sample(std::int64_t frame, std::size_t channel) const {
        if (frame < 0) {
            return 0.0;
        }
        const std::uint64_t f = static_cast<std::uint64_t>(frame);
        if (f < read_frame_ || f >= write_frame_) {
            return 0.0;
        }
        const std::size_t idx =
            (static_cast<std::size_t>(f) % capacity_frames_) * channels_ +
            channel;
        return data_[idx];
    }

private:
    std::vector<double> data_;
    std::size_t channels_;
    std::size_t capacity_frames_;
    std::uint64_t read_frame_;
    std::uint64_t write_frame_;
};

// Interpolate one channel of `ring` at fractional frame position `pos` using
// `table`. Mirrors SincTable::interpolate but returns the raw f64 accumulator
// (the caller applies the width-appropriate clamp). The phase selection and
// tap loop are byte-identical to the Rust path.
double interpolate_raw(const SincTable& table, const AudioRing& ring,
                       double pos, std::size_t channel) {
    const std::int64_t center = static_cast<std::int64_t>(std::floor(pos));
    const double frac = pos - static_cast<double>(center);
    std::size_t phase =
        static_cast<std::size_t>(std::floor(frac * static_cast<double>(kPhases)));
    if (phase > kPhases - 1) {
        phase = kPhases - 1;
    }
    const std::array<double, kTaps>& coeffs = table.phase(phase);
    double acc = 0.0;
    for (std::size_t tap = 0; tap < kTaps; ++tap) {
        const std::int64_t offset =
            static_cast<std::int64_t>(tap) - kRadiusFrames;
        const std::int64_t frame = center + offset;
        acc += ring.sample(frame, channel) * coeffs[tap];
    }
    return acc;
}

// ===========================================================================
// SpaDll — the bare second-order DLL, lifted verbatim from
// spa/include/spa/utils/dll.h (via jasper-clock's SpaDll). Holds ONLY loop
// state; the hardening lives in Dll.
// ===========================================================================
class SpaDll {
public:
    SpaDll() : z1_(0.0), z2_(0.0), z3_(0.0), w0_(0.0), w1_(0.0), w2_(0.0) {}

    void set_bw(double bw, double period, double rate) {
        const double w = 2.0 * kPi * bw * period / rate;
        w0_ = 1.0 - std::exp(-20.0 * w);
        w1_ = w * 1.5 / period;
        w2_ = w / 1.5;
    }

    double update(double err) {
        z1_ += w0_ * (w1_ * err - z1_);
        z2_ += w0_ * (z1_ - z2_);
        z3_ += w2_ * z2_;
        return 1.0 - (z2_ + z3_);
    }

    double w0() const { return w0_; }

private:
    double z1_, z2_, z3_;
    double w0_, w1_, w2_;
};

// ===========================================================================
// Dll — the SpaDll plus jasper-clock's hardening: max_error slew clamp,
// max_resync hard-jump, variance-driven adaptive bandwidth, lock tracking,
// error statistics. Mirrors rust/jasper-clock/src/lib.rs Dll + DllConfig.
// ===========================================================================
class Dll {
public:
    // Mirror DllConfig::for_rate's derivations: max_error = max(256, period/2),
    // max_resync = max(period, max_error), initial bw = BW_MAX, retune every
    // ~rate/period cycles.
    //
    // `max_error_override` / `max_resync_override`: a NEGATIVE value (the
    // default) means "use the for_rate derivation" — so the default
    // construction is byte-identical to the Rust DllConfig::for_rate and the
    // cross-language contract test stays valid. A non-negative value overrides
    // that single threshold. usbsink uses this because its control signal is
    // the QUEUE DEPTH, whose natural quantum is one full block (480 frames) ==
    // the for_rate max_resync; a per-cycle resync would then fire on ordinary
    // queue jitter. usbsink passes max_resync_override = 0 (disable per-cycle
    // resync — its prime gate keeps the engaged error small and a daemon-side
    // guard owns true discontinuities) while keeping the for_rate max_error
    // slew clamp. content_bridge passes neither and gets the exact Rust
    // derivation.
    Dll(double period, double rate, double initial_bw,
        double max_error_override, double max_resync_override)
        : period_(period < 1.0 ? 1.0 : period),
          rate_(rate < 1.0 ? 1.0 : rate) {
        bw_retune_period_ = static_cast<std::uint64_t>(
            std::max(1.0, std::round(rate_ / period_)));
        max_error_ = max_error_override >= 0.0 ? max_error_override
                                               : std::max(256.0, period_ / 2.0);
        max_resync_ = max_resync_override >= 0.0
                          ? max_resync_override
                          : std::max(period_, max_error_);
        // The acquire bandwidth (DllConfig::initial_bw in Rust; defaults to
        // BW_MAX). Clamped to [BW_MIN, BW_MAX]; the adaptive retune then
        // narrows toward BW_MIN once locked.
        initial_bw_ = clamp_bw(initial_bw);
        reset_internal(/*count_unlock=*/false);
    }

    void reset() { reset_internal(/*count_unlock=*/true); }

    // Feed one error sample and return the corrected ratio. Mirrors
    // Dll::update exactly: non-finite ignored, resync hard-jump on a big
    // RAW error, slew clamp on the integrator input, RAW error drives stats
    // and the lock verdict.
    double update(double err) {
        if (!std::isfinite(err)) {
            return ratio_;
        }
        if (is_resync(err)) {
            reset_internal(/*count_unlock=*/true);
            resync_count_ += 1;
            ratio_ = 1.0;
            return ratio_;
        }
        const double clamped = clamp_error(err);
        ratio_ = dll_.update(clamped);
        accumulate_error(err);
        updates_ += 1;
        updates_since_retune_ += 1;
        maybe_retune_bandwidth();
        update_lock();
        return ratio_;
    }

    double ratio() const { return ratio_; }
    double ratio_ppm() const { return (ratio_ - 1.0) * 1.0e6; }
    double error_mean() const { return err_avg_; }
    double error_variance() const { return err_var_ < 0.0 ? 0.0 : err_var_; }
    double bandwidth() const { return bandwidth_; }
    bool is_locked() const { return locked_; }
    std::uint64_t resync_count() const { return resync_count_; }

private:
    static double clamp_bw(double bw) {
        if (!std::isfinite(bw)) {
            return kBwMin;
        }
        return std::min(std::max(bw, kBwMin), kBwMax);
    }

    void reset_internal(bool count_unlock) {
        const double initial_bw = clamp_bw(initial_bw_);
        dll_ = SpaDll();
        dll_.set_bw(initial_bw, period_, rate_);
        // Track the error statistics on the loop's own settling timescale.
        avg_coeff_ = std::min(std::max(dll_.w0(), 1.0e-4), 1.0);
        err_avg_ = 0.0;
        err_var_ = 0.0;
        ratio_ = 1.0;
        bandwidth_ = initial_bw;
        if (count_unlock && locked_) {
            unlock_count_ += 1;
        }
        locked_ = false;
        updates_ = 0;
        updates_since_retune_ = 0;
    }

    bool is_resync(double err) const {
        return std::isfinite(max_resync_) && max_resync_ > 0.0 &&
               std::fabs(err) > max_resync_;
    }

    double clamp_error(double err) const {
        if (std::isfinite(max_error_) && max_error_ > 0.0) {
            return std::min(std::max(err, -max_error_), max_error_);
        }
        return err;
    }

    void accumulate_error(double err) {
        const double delta = err - err_avg_;
        err_avg_ += avg_coeff_ * delta;
        err_var_ =
            (1.0 - avg_coeff_) * (err_var_ + avg_coeff_ * delta * delta);
    }

    void maybe_retune_bandwidth() {
        if (bw_retune_period_ == 0 ||
            updates_since_retune_ < bw_retune_period_) {
            return;
        }
        updates_since_retune_ = 0;
        const double var = err_var_ < 0.0 ? 0.0 : err_var_;
        const double target_bw =
            clamp_bw((std::fabs(err_avg_) + std::sqrt(var)) / 1000.0);
        if (std::fabs(target_bw - bandwidth_) >
            std::numeric_limits<double>::epsilon()) {
            bandwidth_ = target_bw;
            dll_.set_bw(target_bw, period_, rate_);
        }
    }

    void update_lock() {
        const bool was_locked = locked_;
        const double lock_threshold = std::max(period_ * 1.0e-3, 1.0e-6);
        const bool acquired = updates_ >= kLockWarmupUpdates;
        const double var = err_var_ < 0.0 ? 0.0 : err_var_;
        const bool low_error = std::fabs(err_avg_) < lock_threshold &&
                               std::sqrt(var) < lock_threshold;
        locked_ = acquired && low_error;
        if (locked_ && !was_locked) {
            lock_count_ += 1;
        } else if (!locked_ && was_locked) {
            unlock_count_ += 1;
        }
    }

    SpaDll dll_;
    double period_;
    double rate_;
    double initial_bw_ = kBwMax;
    std::uint64_t bw_retune_period_ = 1;
    double max_error_ = 256.0;
    double max_resync_ = 256.0;

    double err_avg_ = 0.0;
    double err_var_ = 0.0;
    double avg_coeff_ = 1.0;
    double ratio_ = 1.0;
    double bandwidth_ = kBwMax;
    bool locked_ = false;
    std::uint64_t updates_ = 0;
    std::uint64_t updates_since_retune_ = 0;
    std::uint64_t lock_count_ = 0;
    std::uint64_t unlock_count_ = 0;
    std::uint64_t resync_count_ = 0;
};

// ===========================================================================
// RateResampler — the Python-facing class. Composes a Dll (with the output
// ppm clamp = jasper-resampler's RateController) and a streaming windowed-sinc
// resampler (jasper-resampler's BlockResampler), keeping a fractional read
// cursor across resample_block calls so successive 10 ms blocks are phase-
// continuous.
// ===========================================================================
class RateResampler {
public:
    RateResampler(double bw, unsigned period_frames, unsigned rate,
                  unsigned channels, double max_adjust_ppm,
                  unsigned bytes_per_sample, double max_error,
                  double max_resync)
        : dll_(static_cast<double>(period_frames), static_cast<double>(rate),
               bw, max_error, max_resync),
          max_adjust_ppm_(max_adjust_ppm),
          channels_(channels),
          bytes_per_sample_(bytes_per_sample),
          // The ring must hold a generous window of input so a brief
          // producer/consumer imbalance never drops a frame mid-stream. The
          // usbsink queue is at most QUEUE_MAXBLOCKS (8) * block, so a few
          // hundred ms of headroom is ample; size it to many blocks.
          ring_(std::max<std::size_t>(period_frames * 64,
                                      static_cast<std::size_t>(kTaps) + 1),
                channels),
          next_input_frame_(0.0),
          primed_(false) {
        if (channels == 0) {
            throw std::invalid_argument("channels must be > 0");
        }
        if (bytes_per_sample != 2 && bytes_per_sample != 4) {
            throw std::invalid_argument(
                "bytes_per_sample must be 2 (S16_LE) or 4 (S32_LE)");
        }
        // `bw` is the DLL's acquire bandwidth (passed through to the Dll ctor,
        // which clamps it to [BW_MIN, BW_MAX]); the loop then narrows toward
        // BW_MIN once locked. Record the requested value for the telemetry
        // accessor. The default (0.128 == BW_MAX) is the fast-acquire start.
        requested_bw_ = std::min(std::max(bw, kBwMin), kBwMax);
        // table_ is default-constructed (built once here).
    }

    // Feed one fill error and return the bounded resampler ratio. Mirrors
    // jasper-resampler RateController::next_ratio: negate the error (capture-
    // follower negative feedback — a too-full buffer drains by reading FASTER,
    // ratio > 1), then clamp the output ppm to +-max_adjust_ppm.
    double update(double error_frames) {
        const double raw_ppm = (dll_.update(-error_frames) - 1.0) * 1.0e6;
        double clamped_ppm = std::min(std::max(raw_ppm, -max_adjust_ppm_),
                                      max_adjust_ppm_);
        if (std::fabs(raw_ppm - clamped_ppm) >
            std::numeric_limits<double>::epsilon()) {
            clamp_count_ += 1;
        }
        ratio_ppm_ = clamped_ppm;
        return 1.0 + clamped_ppm / 1.0e6;
    }

    // Push interleaved PCM bytes and emit resampled interleaved PCM bytes at
    // `ratio`. Mirrors BlockResampler::resample_block: buffer into the ring,
    // emit floor(available / ratio) output frames by advancing the cursor by
    // `ratio` per frame, drop consumed history keeping kRadiusFrames + 1
    // behind. Sample-width-aware: reads/writes int16 or int32 per
    // bytes_per_sample, all interpolation in f64.
    py::bytes resample_block(py::bytes pcm, double ratio) {
        const std::string in = pcm;
        const std::size_t frame_bytes = channels_ * bytes_per_sample_;
        if (frame_bytes == 0) {
            throw std::invalid_argument("zero frame size");
        }
        if (in.size() % frame_bytes != 0) {
            throw std::invalid_argument(
                "pcm length must be a multiple of channels * bytes_per_sample");
        }
        const std::size_t in_frames = in.size() / frame_bytes;

        // Defense in depth: the loop owns real clamping; a non-finite or
        // non-positive ratio falls back to unity so the cursor never stalls
        // or runs backwards (mirrors BlockResampler's guard).
        double r = ratio;
        if (!std::isfinite(r) || r <= 0.0) {
            r = 1.0;
        }

        // Decode the input into f64 frames and push into the ring.
        if (in_frames > 0) {
            std::vector<double> scratch(in_frames * channels_);
            decode_to_f64(in.data(), in_frames, scratch.data());
            ring_.push_interleaved(scratch.data(), in_frames);
        }

        // Prime the cursor RADIUS_FRAMES into the buffered input on the first
        // block (mirrors BlockResampler's one-shot/streaming edge convention).
        if (!primed_) {
            if (ring_.fill_frames() == 0) {
                return py::bytes(std::string());
            }
            next_input_frame_ = static_cast<double>(ring_.read_frame()) +
                                static_cast<double>(kRadiusFrames);
            primed_ = true;
        }

        const double write_frame = static_cast<double>(ring_.write_frame());
        double pos = next_input_frame_;

        // Emit while the kernel's rightmost tap is still a written frame.
        // The boundary pos + RADIUS + 1 <= write_frame keeps that tap strictly
        // inside [read_frame, write_frame) (mirrors BlockResampler exactly).
        std::vector<double> out_frames;  // interleaved f64
        while (pos + static_cast<double>(kRadiusFrames) + 1.0 <= write_frame) {
            for (std::size_t c = 0; c < channels_; ++c) {
                out_frames.push_back(interpolate_raw(table_, ring_, pos, c));
            }
            pos += r;
        }
        next_input_frame_ = pos;

        // Free history the cursor has passed, keeping RADIUS_FRAMES + 1 behind.
        const std::int64_t keep_from =
            static_cast<std::int64_t>(std::floor(pos)) - kRadiusFrames - 1;
        ring_.drop_before(keep_from);

        return encode_from_f64(out_frames);
    }

    // Discard buffered input and re-prime on the next block (the hard-resync
    // path). Mirrors BlockResampler::reset; the DLL has its OWN reset accessor
    // below so the daemon can resync the loop independently of the cursor.
    void reset() {
        ring_.clear();
        next_input_frame_ = 0.0;
        primed_ = false;
    }

    // Reset BOTH the cursor and the DLL loop — the daemon-side hard-resync used
    // on a true underrun/overflow discontinuity the per-cycle error may
    // under-represent at 10 ms granularity (the spec's extra usbsink resync
    // trigger). The DLL's own max_resync still fires on a single big error.
    void reset_loop() {
        dll_.reset();
        ratio_ppm_ = 0.0;
        reset();
    }

    double ratio_ppm() const { return ratio_ppm_; }
    bool is_locked() const { return dll_.is_locked(); }
    std::uint64_t resync_count() const { return dll_.resync_count(); }
    std::uint64_t clamp_count() const { return clamp_count_; }
    double error_mean() const { return dll_.error_mean(); }
    double error_var() const { return dll_.error_variance(); }
    double bandwidth() const { return dll_.bandwidth(); }
    unsigned channels() const { return channels_; }
    unsigned bytes_per_sample() const { return bytes_per_sample_; }
    double requested_bw() const { return requested_bw_; }

private:
    void decode_to_f64(const char* data, std::size_t frame_count,
                       double* out) const {
        const std::size_t n = frame_count * channels_;
        if (bytes_per_sample_ == 2) {
            const auto* p = reinterpret_cast<const std::int16_t*>(data);
            for (std::size_t i = 0; i < n; ++i) {
                out[i] = static_cast<double>(p[i]);
            }
        } else {
            const auto* p = reinterpret_cast<const std::int32_t*>(data);
            for (std::size_t i = 0; i < n; ++i) {
                out[i] = static_cast<double>(p[i]);
            }
        }
    }

    py::bytes encode_from_f64(const std::vector<double>& frames) const {
        if (bytes_per_sample_ == 2) {
            std::vector<std::int16_t> out(frames.size());
            for (std::size_t i = 0; i < frames.size(); ++i) {
                out[i] = clamp_i16(frames[i]);
            }
            return py::bytes(reinterpret_cast<const char*>(out.data()),
                             out.size() * sizeof(std::int16_t));
        }
        std::vector<std::int32_t> out(frames.size());
        for (std::size_t i = 0; i < frames.size(); ++i) {
            out[i] = clamp_i32(frames[i]);
        }
        return py::bytes(reinterpret_cast<const char*>(out.data()),
                         out.size() * sizeof(std::int32_t));
    }

    Dll dll_;
    double max_adjust_ppm_;
    unsigned channels_;
    unsigned bytes_per_sample_;
    SincTable table_;
    AudioRing ring_;
    double next_input_frame_;
    bool primed_;
    double ratio_ppm_ = 0.0;
    std::uint64_t clamp_count_ = 0;
    double requested_bw_ = kBwMax;
};

}  // namespace

PYBIND11_MODULE(_resampler, m) {
    m.doc() =
        "Windowed-sinc resampler + spa_dll rate controller for jasper-usbsink "
        "(C++ mirror of the Rust jasper-resampler crate; byte-identity pinned "
        "by tests/test_resampler_contract.py).";

    py::class_<RateResampler>(m, "RateResampler")
        .def(py::init<double, unsigned, unsigned, unsigned, double, unsigned,
                      double, double>(),
             py::arg("bw") = kBwMax,
             py::arg("period_frames") = 480,
             py::arg("rate") = 48000,
             py::arg("channels") = 2,
             py::arg("max_adjust_ppm") = kDefaultMaxAdjustPpm,
             py::arg("bytes_per_sample") = 2,
             py::arg("max_error") = -1.0,
             py::arg("max_resync") = -1.0,
             "Construct a capture-follower rate controller + streaming "
             "windowed-sinc resampler. `bw` is the acquire bandwidth (clamped "
             "to [0.016, 0.128]); `period_frames` and `rate` set the DLL loop "
             "timescale (use the capture block size and sample rate, e.g. 480 "
             "@ 48000 = 10 ms); `channels` is the interleaved channel count; "
             "`max_adjust_ppm` is the OUTPUT ppm clamp bounding how far the "
             "resampler may ever warp pitch (default 500, matching "
             "content_bridge); `bytes_per_sample` is 2 for S16_LE (the aloop "
             "lane / cross-language contract) or 4 for S32_LE (the FIFO lean "
             "lane) so both usbsink output modes share one exact path. "
             "`max_error` / `max_resync` default to -1 = the Rust "
             "DllConfig::for_rate derivation (max_error = max(256, period/2), "
             "max_resync = max(period, max_error)); pass a non-negative value "
             "to override one threshold. usbsink passes max_resync=0 to "
             "disable the per-cycle hard-resync because its control signal is "
             "the queue depth, whose quantum is one block == the default "
             "max_resync — ordinary queue jitter would otherwise trip it; its "
             "prime gate + a daemon-side guard own true discontinuities "
             "instead. The ~540 KB sinc table is built ONCE in this "
             "constructor — never per block.")
        .def("update", &RateResampler::update, py::arg("error_frames"),
             "Feed one buffer-fill error in frames (error_frames = fill - "
             "target) and return the bounded resampler ratio. CAPTURE-FOLLOWER "
             "SIGN: the error is negated internally (negative feedback), so a "
             "too-full buffer (error_frames > 0) settles to ratio > 1 — the "
             "same sign as content_bridge.rs RateController::next_ratio "
             "(dll.update(-error_frames)). The result is clamped to "
             "+-max_adjust_ppm.")
        .def("resample_block", &RateResampler::resample_block,
             py::arg("pcm"), py::arg("ratio"),
             "Resample one interleaved PCM block at `ratio` (the value "
             "`update` returned). Keeps the fractional read cursor + ring "
             "history across calls, so successive 10 ms blocks are phase-"
             "continuous (no seam click). ratio > 1 emits FEWER output frames "
             "than input (consume host faster, drain the queue); ratio < 1 "
             "emits more. Sample width follows bytes_per_sample (S16_LE or "
             "S32_LE); len(pcm) must be a multiple of channels * "
             "bytes_per_sample.")
        .def("reset", &RateResampler::reset,
             "Discard buffered input and re-prime the cursor on the next "
             "block (a fresh phase). Leaves the DLL loop state intact; use "
             "reset_loop() to also re-lock the controller.")
        .def("reset_loop", &RateResampler::reset_loop,
             "Reset BOTH the resampler cursor AND the DLL loop (zero the "
             "integrators, drop lock, return ratio to unity). The daemon-side "
             "hard-resync for a true underrun/overflow discontinuity that the "
             "per-cycle error may under-represent at 10 ms granularity.")
        .def("ratio_ppm", &RateResampler::ratio_ppm,
             "The last bounded ratio in ppm ((ratio - 1) * 1e6).")
        .def("is_locked", &RateResampler::is_locked,
             "Whether the underlying DLL loop is currently locked.")
        .def("resync_count", &RateResampler::resync_count,
             "Times a max_resync hard-jump re-initialised the loop (a "
             "discontinuity — e.g. a host pause/seek that steps the fill).")
        .def("clamp_count", &RateResampler::clamp_count,
             "Times the output ppm clamp engaged (the loop wanted to warp "
             "past max_adjust_ppm).")
        .def("error_mean", &RateResampler::error_mean,
             "Running mean of the (negated) fill error fed to the loop.")
        .def("error_var", &RateResampler::error_var,
             "Running variance of the fill error fed to the loop.")
        .def("bandwidth", &RateResampler::bandwidth,
             "Current adaptive loop bandwidth (acquires at bw, narrows toward "
             "0.016 once locked).")
        .def_property_readonly("channels", &RateResampler::channels)
        .def_property_readonly("bytes_per_sample",
                               &RateResampler::bytes_per_sample);
}
