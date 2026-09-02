// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! ALSA transport for the outputd topology.
//!
//! The DAC playback stream is blocking and owns timing. Camilla's post-DSP
//! program arrives over the SHM ring — outputd's one upstream (ADR-0100) — and
//! this module opens no content PCM at all; absent content becomes silence.
//! This keeps the final output loop alive even when renderers are idle.

use alsa::pcm::{Access, Format, HwParams, State, IO, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};

use crate::config::Config;
use crate::types::{narrow_period, narrow_period_i24_le, ProgramSample, SampleFormat, CHANNELS};

const MAX_RECOVERIES_PER_PERIOD: u32 = 3;

/// What the write loop does after one recovery attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum XrunAction {
    /// Stay in the period and write again.
    Continue,
    /// The budget is spent — bail, which takes the unit's ordinary exit-1
    /// restart ladder.
    GiveUp,
}

/// **The one recovery budget, shared by the coherent single sink and the
/// composite.** One function so the two write paths cannot drift into
/// different answers to "how many times may one period recover?".
///
/// The parameter name carries the calling convention, because the off-by-one
/// it prevents is the whole bug class: the caller has ALREADY attempted the
/// recovery and ALREADY incremented, so `1` means "one recovery has happened".
/// A `>` against `MAX_RECOVERIES_PER_PERIOD` therefore permits exactly
/// `MAX_RECOVERIES_PER_PERIOD` recoveries and no further write past them:
///
/// | `recoveries_so_far_including_this_one` | Action |
/// |---|---|
/// | 1 | `Continue` |
/// | 2 | `Continue` |
/// | 3 | `Continue` |
/// | 4 | `GiveUp` |
///
/// Written check-AFTER-increment rather than check-before, which is what the
/// single path shipped and what a fresh reading of "budget of 3" would get
/// wrong in the permissive direction (a fourth `writei` on a device that has
/// already failed four times).
fn xrun_policy(recoveries_so_far_including_this_one: u32) -> XrunAction {
    if recoveries_so_far_including_this_one > MAX_RECOVERIES_PER_PERIOD {
        XrunAction::GiveUp
    } else {
        XrunAction::Continue
    }
}

/// How many periods of silence to queue before an explicit `snd_pcm_start`.
///
/// `(buffer / period) - 1`, floored at one period: deliberately ONE PERIOD OF
/// BUFFER HEADROOM below a full buffer, because `manual_start` sets
/// `start_threshold = buffer_frames`. Filling the buffer would make ALSA START
/// the stream by itself — and on a linked composite that auto-start fires when
/// the FIRST child's buffer fills, before the second child has been primed at
/// all, baking a period of permanent A/B skew into the pair. Priming below the
/// threshold is what makes the following `snd_pcm_start` the only thing that
/// starts either child.
///
/// Used by BOTH the startup prime (`main.rs`) and the composite's post-recovery
/// re-prime, from here, so the depth that makes the startup path safe is the
/// same number the recovery path re-establishes alignment with. Pinned by
/// `prime_periods_leave_one_period_of_buffer_headroom`.
pub fn prime_periods(buffer_frames: u32, period_frames: u32) -> u32 {
    if period_frames == 0 {
        return 1;
    }
    ((buffer_frames / period_frames).saturating_sub(1)).max(1)
}

/// The steady-state pairwise-divergence arithmetic, lifted out of
/// [`PairedCompositeSink::check_delay_delta`] so it can be tested without two
/// live USB DACs.
///
/// `error_frames` is the drift AWAY FROM the latched baseline, not the raw A/B
/// delay difference: a composite's two children sit at some fixed offset from
/// each other and that offset is the zero. `diverged` is `error > max`, so an
/// error EXACTLY at the tolerance is accepted — the same inclusive boundary the
/// post-recovery re-latch uses, because they are the same tolerance with one
/// meaning.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DelayDeltaCheck {
    error_frames: i64,
    diverged: bool,
}

fn delay_delta_check(delta: i64, baseline: i64, max_error_frames: i64) -> DelayDeltaCheck {
    let error_frames = (delta - baseline).abs();
    DelayDeltaCheck {
        error_frames,
        diverged: error_frames > max_error_frames,
    }
}

/// Verdict on re-latching the pairwise delay baseline after a group recovery.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BaselineRelatch {
    /// The re-primed pair landed within tolerance of where it was before the
    /// fault — bless the new offset as the zero.
    Accept,
    /// The re-primed pair is somewhere else. Refuse, and let the sink fail
    /// closed rather than laundering the fault the divergence guard exists to
    /// see.
    Refuse,
}

/// **The magnitude bound on the re-latch — bounded by how far the pair moved,
/// never by how many times it moved.**
///
/// A count threshold cannot see this harm: at 48 kHz one 128-frame period is
/// 2.667 ms, and on an active 2-way that offset IS the woofer/tweeter time
/// alignment. A single bad re-latch bakes it in permanently, so the harm is
/// unbounded at count = 1.
///
/// `max_delta_frames` is `dual_max_delay_delta_frames` — the SAME constant the
/// steady-state divergence guard enforces, one meaning in both places. Note the
/// arithmetic that makes it the right number: at the default 48 frames
/// (~1.0 ms), a full-period skew is ~2.7x the tolerance, i.e. the steady-state
/// guard would already catch it if the baseline were not re-latched. This bound
/// is precisely what stops the re-latch from hiding that.
///
/// `pre_fault_baseline == None` accepts: the fault arrived before the pair ever
/// latched a steady-state zero, so there is no prior alignment to violate and
/// nothing to launder. Refusing there would fail closed on a fact nobody knows.
fn baseline_relatch_decision(
    pre_fault_baseline: Option<i64>,
    post_reprime_delta: i64,
    max_delta_frames: i64,
) -> BaselineRelatch {
    match pre_fault_baseline {
        None => BaselineRelatch::Accept,
        Some(pre_fault) => {
            if (post_reprime_delta - pre_fault).abs() <= max_delta_frames {
                BaselineRelatch::Accept
            } else {
                BaselineRelatch::Refuse
            }
        }
    }
}

/// The single mapping from outputd's format vocabulary into ALSA's own.
///
/// Everything else in the crate speaks [`SampleFormat`]; this is the one place
/// that knows what ALSA calls it. `S16Le` maps to the same `Format::S16LE` the
/// retired module-level `FORMAT` const held, so an S16 edge asks ALSA for
/// exactly what it always asked for.
///
/// `S24_3Le` maps to `Format::S243LE` — alsa-rs's spelling of
/// `SND_PCM_FORMAT_S24_3LE`, 24 bits in three packed bytes. **Not**
/// `Format::S24LE`, which is the same 24 bits inside a 4-byte word and is a
/// different wire; that pair is one typo apart, so the mapping is pinned by test.
fn alsa_format(format: SampleFormat) -> Format {
    match format {
        SampleFormat::S16Le => Format::S16LE,
        SampleFormat::S24_3Le => Format::S243LE,
        SampleFormat::S32Le => Format::S32LE,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NegotiatedPcm {
    pub sample_rate: u32,
    pub period_frames: u32,
    pub buffer_frames: u32,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct IoCounters {
    pub content_frames_read: u64,
    pub content_empty_period_count: u64,
    pub content_partial_period_count: u64,
    pub content_eagain_count: u64,
    pub dac_frames_written: u64,
    pub content_xrun_count: u64,
    pub dac_xrun_count: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompositeStatus {
    pub dac_a_pcm: String,
    pub dac_b_pcm: String,
    pub linked: bool,
    pub delay_delta_frames: Option<i64>,
    pub delay_delta_baseline_frames: Option<i64>,
    pub delay_delta_error_frames: Option<i64>,
    pub max_delay_delta_frames: i64,
    /// Per-child xrun attribution and the group's recovery bookkeeping. Only
    /// ever rendered inside the already-conditional `dual_apple` block, so a
    /// single-DAC box's `/state` does not change by a byte.
    pub dac_a_xruns: u64,
    pub dac_b_xruns: u64,
    pub group_recoveries: u64,
    pub delay_baseline_relatches: u64,
    pub reprime_alignment_failures: u64,
}

pub struct AlsaBackend {
    dac: PCM,
    pub dac_pcm: String,
    /// The stand-in geometry `/state` reports for the content hop. No ALSA
    /// content lane is opened (ADR-0100 — the ring is the one upstream), so
    /// there is nothing to negotiate; see `synthetic_content_negotiated`.
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
    /// Runtime DAC/content width carried as data — a coherent single DAC reads
    /// and writes this many channels end-to-end. `2` is byte-identical to the
    /// previous compile-time `CHANNELS`; the reconciler emits wider values
    /// (DAC8x = 8) via `JASPER_OUTPUTD_ACTIVE_CHANNELS`. The reference/chip-ref
    /// width stays `CHANNELS=2` (the published reference is always stereo).
    channels: u16,
    /// The format OUTPUTD'S OWN CLIENT EDGE negotiated — requested from the
    /// registry declaration and checked against the installed `hw_params` by
    /// `configure_pcm`'s dac readback, so this is what outputd is running, not
    /// an intention. On a raw `hw:` device it is also the hardware edge;
    /// through a `plug` it is not (see that readback's comment). It selects the
    /// write path in `write_dac_period` and is what `/state` reports as
    /// `dac.format`.
    dac_format: SampleFormat,
    /// Reused NARROWING staging for an `S16Le` edge — allocated once, at open,
    /// and never on the audio path. Empty (zero bytes) on an `S32Le` edge, which
    /// takes the program spine straight through with no staging at all, and on an
    /// `S24_3Le` edge, which stages into [`Self::dac_pack_buf`] instead.
    ///
    /// Only the mismatched edge pays: the program spine is wide, so the S16 edge
    /// is the one that converts.
    dac_narrow_buf: Vec<i16>,
    /// Reused PACKING staging for an `S24_3Le` edge: one period as packed
    /// little-endian 24-bit BYTES, allocated once at open. Empty (zero bytes) at
    /// every other edge width, so a box that does not declare `S24_3LE` pays
    /// nothing for this field's existence — the same conditional-allocation rule
    /// [`ChildPeriods`] follows, and the reason this is a second buffer rather
    /// than a widened `dac_narrow_buf`.
    ///
    /// A separate field from `dac_narrow_buf` rather than one buffer reinterpreted,
    /// because the element TYPE is what makes the two edges different: an i16
    /// staging can be handed to `io_i16()` and a byte staging cannot. Keeping them
    /// distinct means an edge can only ever be handed the buffer its own arm
    /// filled. Exactly one of the two is non-empty on any box.
    ///
    /// **Non-empty on every single-Apple-dongle box** — that profile declares an
    /// `S24_3Le` edge, so this holds one period there and `dac_narrow_buf` is the
    /// empty one instead. It stays `Vec::new()` at the `S16Le` and `S32Le` edges.
    dac_pack_buf: Vec<u8>,
}

/// Paired-composite transport: two clock-independent child DACs driven as one
/// 4-channel sink (the dual-Apple shape). Renamed from `DualAppleBackend` — the
/// transport dispatches on the composite SHAPE, not the DAC's identity. Stays
/// exactly two children (a pairwise drift guard cannot be half-vectorized).
pub struct PairedCompositeSink {
    dac_a: PCM,
    dac_b: PCM,
    pub dac_a_pcm: String,
    pub dac_b_pcm: String,
    /// The stand-in geometry `/state` reports for the content hop — the
    /// coherent single sink's field, same contract. No ALSA content lane is
    /// opened (ADR-0100).
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
    linked: bool,
    /// Per-child cumulative xrun counts. The sink-level `dac_xrun_count` in
    /// `counters` stays exactly what it was (the doctor's existing consumer);
    /// these two are the attribution that says WHICH dongle is burping, which
    /// on a two-dongle-on-one-bus box is the difference between "replace a
    /// cable" and "replace a hub".
    dac_a_xrun_count: u64,
    dac_b_xrun_count: u64,
    /// Cumulative GROUP recoveries. One per recovered xrun, not two: on a
    /// linked pair `snd_pcm_recover` prepares both children, so a per-child
    /// recovery counter would state a fact that is not true.
    group_recoveries: u64,
    /// How many times the pairwise baseline was re-latched after a recovery —
    /// an observable, never the bound. The bound is
    /// [`baseline_relatch_decision`]'s magnitude test.
    delay_baseline_relatches: u64,
    /// How many re-latches were REFUSED because the re-primed pair landed
    /// outside `max_delay_delta_frames` of its pre-fault baseline. This is the
    /// number that says "this pair is losing alignment on every burp" — each
    /// one is also a fail-closed stop, so a nonzero value here is always paired
    /// with a restart.
    reprime_alignment_failures: u64,
    delay_delta_baseline: Option<i64>,
    last_delay_delta: Option<i64>,
    last_delay_delta_error: Option<i64>,
    max_delay_delta_frames: i64,
    /// The two child period buffers, at the children's declared edge width —
    /// and the ONE place that width is represented at runtime. See
    /// [`ChildPeriods`] for why the width is the variant rather than a separate
    /// field beside two typed buffer pairs.
    periods: ChildPeriods,
}

/// One sample at a composite CHILD's edge width, and the conversion from the
/// program spine that produces it.
///
/// A CLOSED trait — exactly two implementations, both in this module — rather
/// than a conversion closure passed in by the caller. The difference matters:
/// with a closure, a call site could hand the split loop
/// `jasper_resampler::s32_high_word_to_s16` (UAC2 *capture* truncation) and put
/// a half-LSB downward bias on every sample at the speaker edge, and nothing
/// about the split would look wrong. Here the conversion is a property of the
/// output type, so the wrong one is unreachable rather than merely untested.
trait ChildEdgeSample: Copy {
    fn from_program(sample: ProgramSample) -> Self;
}

impl ChildEdgeSample for i16 {
    /// The composite's quantization: round-to-nearest, saturating — the SAME
    /// primitive the coherent sink's `S16Le` edge narrows through, so the two
    /// final edges cannot drift into rounding differently.
    fn from_program(sample: ProgramSample) -> Self {
        jasper_resampler::narrow_i32_to_i16_round(sample)
    }
}

impl ChildEdgeSample for ProgramSample {
    /// An `S32Le` child IS the spine's own width, so there is nothing to
    /// convert — the identity, exactly as the coherent sink's `S32Le` edge
    /// writes the spine straight through.
    fn from_program(sample: ProgramSample) -> Self {
        sample
    }
}

/// The composite's two child period buffers, at the children's declared edge
/// width.
///
/// An enum rather than two typed buffer pairs plus a `SampleFormat` field,
/// because the buffers' element type and the width the children negotiated are
/// ONE fact: a struct holding both could drift into an `S32Le` format field over
/// i16 buffers, and the write path would then hand `io_i32()` a slice of
/// half-samples — full-scale garbage at the speaker. Here the width IS the
/// variant, and [`Self::format`] reads it back off the same value the buffers
/// live in, so `/state` cannot report a width the buffers are not.
///
/// Both variants are allocated once, at open, and reused — the audio path fills
/// them in the split and then hands them to the child writes, never resizing or
/// replacing them. Unlike the coherent sink's `dac_narrow_buf` — EMPTY on the
/// edge that converts nothing — a composite always needs a scratch pair whatever
/// the width, because the 4-channel program period has to be SPLIT into two
/// stereo periods before either child can be written. Only the element type
/// moves.
enum ChildPeriods {
    S16 {
        a: Vec<i16>,
        b: Vec<i16>,
    },
    S32 {
        a: Vec<ProgramSample>,
        b: Vec<ProgramSample>,
    },
}

impl ChildPeriods {
    /// One stereo period per child, at `format`. `CHANNELS` (not the sink's
    /// 4-channel content width): each child is a stereo DAC.
    ///
    /// **Fallible, and only because of `S24_3Le`.** There is no packed-24
    /// variant, because this transport has no packed-24 child write path: the
    /// arms below hold an i16 pair or an i32 pair, and
    /// [`deinterleave_4ch_to_dual_stereo`] is generic over the sample TYPE —
    /// Rust has no 3-byte integer for it to be. So an `S24_3Le` declaration
    /// reaching a composite means the registry and this transport disagree, and
    /// the ONLY safe answer is to refuse. The registry holds every composite off
    /// this width, pinned by
    /// tests/test_dac_profiles.py::test_a_composite_never_declares_a_width_its_transport_refuses;
    /// jaspercurry/JTS#2257 tracks adding the packed child write path that would
    /// let a composite declare it.
    ///
    /// Refuse rather than fall back to `S16`, which is what a `_ =>` arm would
    /// have done: outputd would then have asked BOTH children for `S24_3LE` at
    /// open (`configure_pcm` reads the same declaration), passed their readbacks,
    /// and written i16 half-samples into a 3-byte wire — full-scale garbage at two
    /// speakers, with `/state` reporting `S24_3LE` because
    /// [`Self::format`] reads the buffers. Loud beats plausible.
    fn new(format: SampleFormat, period_frames: u32) -> Result<Self> {
        let samples = (period_frames as usize) * (CHANNELS as usize);
        Ok(match format {
            SampleFormat::S16Le => Self::S16 {
                a: vec![0i16; samples],
                b: vec![0i16; samples],
            },
            SampleFormat::S32Le => Self::S32 {
                a: vec![0 as ProgramSample; samples],
                b: vec![0 as ProgramSample; samples],
            },
            SampleFormat::S24_3Le => anyhow::bail!(
                "outputd composite sink cannot drive an {} child edge: the paired \
                 transport has no packed-24 child write path, so a composite must \
                 declare S16_LE or S32_LE (a child profile may still declare the \
                 packed edge for its own single-DAC case; see jaspercurry/JTS#2257)",
                format.as_str()
            ),
        })
    }

    /// The children's edge width, read off the buffers themselves.
    fn format(&self) -> SampleFormat {
        match self {
            Self::S16 { .. } => SampleFormat::S16Le,
            Self::S32 { .. } => SampleFormat::S32Le,
        }
    }

    /// Split one 4-channel program period into the two child buffers, at
    /// whichever width this pair negotiated.
    ///
    /// The width arm lives here, next to the buffers, so the split and the
    /// buffers it fills can only ever be the same width — the reason this is an
    /// enum at all. Re-run on every retry of a period rather than cached,
    /// because a recovery overwrites these buffers with silence for the
    /// re-prime.
    fn deinterleave(&mut self, samples_4ch: &[ProgramSample]) -> Result<()> {
        match self {
            Self::S16 { a, b } => deinterleave_4ch_to_dual_stereo(samples_4ch, a, b),
            Self::S32 { a, b } => deinterleave_4ch_to_dual_stereo(samples_4ch, a, b),
        }
    }

    /// How many frames one child period holds — the buffers' own length, not a
    /// negotiated value read from somewhere else.
    fn frames(&self) -> usize {
        let samples = match self {
            Self::S16 { a, .. } => a.len(),
            Self::S32 { a, .. } => a.len(),
        };
        samples / (CHANNELS as usize)
    }

    /// Overwrite both child buffers with silence, in place.
    ///
    /// The post-recovery re-prime's payload. In place rather than a fresh
    /// buffer because the re-prime runs on the SCHED_FIFO playout thread, where
    /// an allocation is a priority-inversion path — and because a re-prime that
    /// wrote anything but this pair's own buffers could write the wrong width.
    fn fill_silence(&mut self) {
        match self {
            Self::S16 { a, b } => {
                a.fill(0);
                b.fill(0);
            }
            Self::S32 { a, b } => {
                a.fill(0);
                b.fill(0);
            }
        }
    }
}

/// Marker for a startup fault that a restart cannot repair, attached via
/// `anyhow::Context` and downcast by `main` into the EX_CONFIG 78 park.
///
/// Named for its first user — a final sink that could not be opened or
/// negotiate outputd's geometry, where a restart cannot change the PCM alias or
/// its hardware capabilities. A box that declared no ring carries it for the
/// same reason (`main`'s run loop): the declaration is what it is on every
/// restart, so the unit must park where an operator can see it instead of
/// restart-looping into `StartLimitAction=reboot`.
#[derive(Debug)]
pub struct FinalSinkStartupConfigError;

impl std::fmt::Display for FinalSinkStartupConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "final sink startup configuration fault")
    }
}

impl std::error::Error for FinalSinkStartupConfigError {}

fn final_sink_startup<T>(result: Result<T>) -> Result<T> {
    result.context(FinalSinkStartupConfigError)
}

/// The `NegotiatedPcm` reported in place of a content lane that was never opened
/// — one stand-in for both sinks, so `/state` keeps one vocabulary whatever the
/// transport.
///
/// `buffer_frames = period_frames` is deliberate: there is no ALSA capture ring
/// here at all, so this is a period-sized stand-in, **NOT** a real jitter buffer.
/// The honest buffering depth of the non-ALSA source is reported separately in
/// /state so no consumer is misled: the SHM ring's TRUE capacity (n_slots x
/// period) rides `content.ring.capacity_frames` (see
/// `OutputdState::snapshot_json`). jasper-doctor validates that instead of
/// applying the ALSA ">= 2x period" floor to this synthetic.
fn synthetic_content_negotiated(config: &Config) -> NegotiatedPcm {
    NegotiatedPcm {
        sample_rate: config.sample_rate,
        period_frames: config.period_frames,
        buffer_frames: config.period_frames,
    }
}

impl AlsaBackend {
    pub fn new(config: &Config) -> Result<Self> {
        // No ALSA content lane exists to negotiate: the ring is the one upstream
        // (ADR-0100), so `/state`'s content geometry is the stand-in below.
        let content_negotiated = synthetic_content_negotiated(config);
        let dac = final_sink_startup(
            PCM::new(&config.dac_pcm, Direction::Playback, false)
                .with_context(|| format!("opening outputd DAC PCM {}", config.dac_pcm)),
        )?;
        let dac_negotiated = final_sink_startup(
            configure_pcm(PcmConfig {
                role: "dac",
                pcm_name: &config.dac_pcm,
                pcm: &dac,
                sample_rate: config.sample_rate,
                period_frames: config.period_frames,
                channels: config.content_channels,
                // The one role that asks for the DECLARED edge rather than a
                // constant. `configure_pcm`'s dac readback checks the installed
                // client-side format against it before this returns.
                format: config.declared_dac_format,
                buffer_frames: config.dac_buffer_frames,
                manual_start: true,
            })
            .with_context(|| format!("configuring outputd DAC PCM {}", config.dac_pcm)),
        )?;

        // `format` is what outputd's OWN CLIENT EDGE negotiated: what
        // `configure_pcm` asked ALSA for (the registry declaration), checked
        // against the installed hw_params. That equals the hardware edge on a
        // raw `hw:` device, not through a `plug` — see the dac readback's
        // comment. It also selects the write path: outputd's internal program is
        // `ProgramSample` (i32), so an `S32Le` edge takes the spine STRAIGHT
        // THROUGH and converts nothing, while an `S16Le` edge is where the one
        // output-path quantization happens (`write_dac_period`).
        //
        // `format=` is the DAC edge and keeps its bare name. Not because
        // anything would break: no machine consumer parses this line today (the
        // only reference is prose — docs/AEC-DIAG-01-baseline.md). It stays
        // by convention, because operators and journal-grep recipes read it and
        // a stable key costs nothing. `content_format=` names the OTHER hop —
        // the ring's wire — so one line still names both.
        eprintln!(
            "event=outputd.alsa.opened content_source=shm_ring content_format={} dac_pcm={} channels={} sample_rate={} content_period_frames={} content_buffer_frames={} dac_period_frames={} dac_buffer_frames={} format={}",
            config.content_format.as_str(),
            config.dac_pcm,
            config.content_channels,
            dac_negotiated.sample_rate,
            content_negotiated.period_frames,
            content_negotiated.buffer_frames,
            dac_negotiated.period_frames,
            dac_negotiated.buffer_frames,
            config.declared_dac_format.as_str()
        );

        Ok(Self {
            dac,
            dac_pcm: config.dac_pcm.clone(),
            content_negotiated,
            dac_negotiated,
            counters: IoCounters::default(),
            channels: config.content_channels,
            // `configure_pcm`'s dac readback checked the installed client-side
            // format against this requested one, so storing the request stores
            // what outputd is running — it never reached here otherwise.
            dac_format: config.declared_dac_format,
            dac_narrow_buf: s16_staging(
                config.declared_dac_format,
                config.period_frames,
                config.content_channels,
            ),
            // The `S24_3Le` twin of the line above, and mutually exclusive with
            // it: at most one of the two edge stagings is ever non-empty.
            dac_pack_buf: i24_packed_staging(
                config.declared_dac_format,
                config.period_frames,
                config.content_channels,
            ),
        })
    }

    /// The runtime DAC/content channel width this backend reads and writes.
    pub fn channels(&self) -> u16 {
        self.channels
    }

    /// The format outputd's own client edge negotiated (readback-checked at
    /// open; equals the hardware edge on a raw `hw:` device, not through a
    /// `plug`).
    pub fn dac_format(&self) -> SampleFormat {
        self.dac_format
    }

    pub fn counters(&self) -> IoCounters {
        self.counters
    }

    pub fn start_dac(&self) -> Result<()> {
        if self.dac.state() != State::Running {
            self.dac.start().context("starting outputd DAC PCM")?;
        }
        Ok(())
    }

    pub fn write_dac_period(&mut self, samples: &[ProgramSample]) -> Result<()> {
        let channels = self.channels as usize;
        let frames_total = samples.len() / channels;
        match self.dac_format {
            // DEFAULT PATH — every S16_LE edge, which is every box whose DAC
            // profile declares no wider one (the dual-Apple composite, DAC8x
            // Studio, and any unrecognized card, which resolves to this
            // default). The one place on the output path where resolution is
            // deliberately given up, and it is given up ONCE: round-to-nearest,
            // not the truncating capture conversion. For content that arrived as
            // S16 at unity gain this is bit-identical to the pre-spine path (see
            // `jasper_resampler::s16_widened_then_narrowed_is_bit_identical` and
            // `core::the_content_path_is_bit_transparent_from_spine_ingress_to_sink`).
            SampleFormat::S16Le => {
                if self.dac_narrow_buf.len() != samples.len() {
                    // Steady state never resizes: `new` sized this for the run
                    // loop's period. A geometry that ever differs reallocates
                    // once and then stays put — it must not silently write a
                    // short or stale period.
                    self.dac_narrow_buf.resize(samples.len(), 0);
                }
                narrow_period(samples, &mut self.dac_narrow_buf)?;
                let io = self
                    .dac
                    .io_i16()
                    .context("getting i16 IO handle for outputd DAC")?;
                write_dac_frames(
                    &io,
                    &self.dac,
                    &self.dac_pcm,
                    &self.dac_narrow_buf,
                    channels,
                    &mut self.counters.dac_xrun_count,
                )?;
            }
            // THE PACKED EDGE — live wherever a single Apple USB-C dongle is the
            // armed DAC. Quantize to 24 significant bits and pack three
            // little-endian bytes per sample, then hand ALSA the BYTES.
            //
            // `io_bytes()` is alsa-rs's documented mechanism for exactly this
            // case: "Call this if you have an unusual format, not supported by the
            // regular access methods (io_i16 etc)" (alsa 0.12.1 `src/pcm.rs`). It
            // returns `IO<'_, u8>`, and `IO::writei` computes its frame count via
            // `snd_pcm_bytes_to_frames` on the slice's BYTE length — so on a PCM
            // whose installed format is `S24_3LE` the frame arithmetic is
            // alsa-lib's own, and the shared writer's xrun accounting, recovery
            // budget and `event=outputd.xrun` line are byte-for-byte the ones the
            // S16 and S32 edges get.
            //
            // Two honest differences from the typed handles, both deliberate:
            //
            // * `io_bytes()` does NOT verify the installed format (the typed
            //   `io_i16`/`io_i32` do, via `io_checked`). That per-period check is
            //   not what this path relies on and never was the real proof: the
            //   OPEN-time `verify_dac_format` readback compares the installed
            //   `hw_params` format against the request, names both, and parks at
            //   EX_CONFIG 78 — strictly stronger than an opaque per-period
            //   `unsupported io_xx`. An `S24_3LE` box cannot reach this arm without
            //   having passed that readback.
            // * The writer advances by ELEMENTS, and one element here is a byte,
            //   not a sample — so it is handed `channels * 3`, not `channels`. That
            //   is the whole reason `write_dac_frames`'s parameter is named
            //   `elements_per_frame`; passing `channels` would advance the slice at
            //   a third of the right rate and re-send most of every period.
            SampleFormat::S24_3Le => {
                let packed_len = samples.len() * jasper_resampler::I24_LE_BYTES_PER_SAMPLE;
                if self.dac_pack_buf.len() != packed_len {
                    // Steady state never resizes: `new` sized this for the run
                    // loop's period. A geometry that ever differs reallocates
                    // once and then stays put — it must not silently write a
                    // short or stale period.
                    self.dac_pack_buf.resize(packed_len, 0);
                }
                narrow_period_i24_le(samples, &mut self.dac_pack_buf)?;
                let io = self.dac.io_bytes();
                write_dac_frames(
                    &io,
                    &self.dac,
                    &self.dac_pcm,
                    &self.dac_pack_buf,
                    channels * jasper_resampler::I24_LE_BYTES_PER_SAMPLE,
                    &mut self.counters.dac_xrun_count,
                )?;
            }
            SampleFormat::S32Le => {
                let io = self
                    .dac
                    .io_i32()
                    .context("getting i32 IO handle for outputd DAC")?;
                write_dac_frames(
                    &io,
                    &self.dac,
                    &self.dac_pcm,
                    samples,
                    channels,
                    &mut self.counters.dac_xrun_count,
                )?;
            }
        }
        self.counters.dac_frames_written += frames_total as u64;
        Ok(())
    }

    pub fn dac_delay_frames(&self) -> Result<u64> {
        let delay = self
            .dac
            .delay()
            .context("reading outputd DAC playback delay")?;
        Ok(delay.max(0) as u64)
    }
}

impl PairedCompositeSink {
    pub fn new(config: &Config) -> Result<Self> {
        let dac_a_pcm = config
            .dual_dac_a_pcm
            .as_ref()
            .context("dual Apple sink missing DAC A PCM")?
            .clone();
        let dac_b_pcm = config
            .dual_dac_b_pcm
            .as_ref()
            .context("dual Apple sink missing DAC B PCM")?
            .clone();

        // The children's period buffers are built FIRST, before any PCM is
        // opened, because building them is also the check that this transport can
        // drive the declared child width at all (see `ChildPeriods::new`). A width
        // it cannot drive is a permanent registry/transport disagreement, so it
        // parks at EX_CONFIG 78 — and it does so without first opening two USB
        // DACs for a configuration that was never going to run. It reads
        // `config.period_frames`, not a negotiated value, so nothing here
        // depends on the opens below.
        let periods = final_sink_startup(ChildPeriods::new(
            config.declared_dac_format,
            config.period_frames,
        ))?;

        // No ALSA content lane exists to negotiate — see `AlsaBackend::new`.
        let content_negotiated = synthetic_content_negotiated(config);

        let dac_a = final_sink_startup(
            PCM::new(&dac_a_pcm, Direction::Playback, false)
                .with_context(|| format!("opening outputd dual DAC A PCM {}", dac_a_pcm)),
        )?;
        let dac_a_negotiated = final_sink_startup(
            configure_pcm(PcmConfig {
                role: "dual_dac_a",
                pcm_name: &dac_a_pcm,
                pcm: &dac_a,
                sample_rate: config.sample_rate,
                period_frames: config.period_frames,
                channels: CHANNELS,
                // BOTH children request the registry-declared edge format, the
                // same declaration the coherent single DAC reads. Per-child
                // divergence is deliberately not modelled: the one registered
                // composite profile pairs two of the SAME dongle
                // (`child_profile_ids = (apple_usb_c_dongle, apple_usb_c_dongle)`),
                // so one declaration describes both edges, and
                // `configure_pcm`'s final-edge readback proves each child
                // installed it.
                //
                // A future composite of unlike children needs a per-child
                // declaration in the registry first, and the thing that would
                // catch its absence is the PER-CHILD READBACK — one child
                // installing a width other than the requested one parks at exit
                // 78 naming that child. NOT the `dac_a_negotiated !=
                // dac_b_negotiated` check below: `NegotiatedPcm` carries
                // sample_rate/period_frames/buffer_frames and no format at all,
                // so two children running different widths compare equal there.
                // The registry-side guards are
                // tests/test_dac_profiles.py::test_a_composite_never_declares_a_width_its_transport_refuses
                // and ::test_a_composite_may_diverge_from_its_child_only_at_that_capability_gap,
                // which fail at build time instead of on a household's speaker.
                // They no longer require a composite to match its child profile:
                // the Apple dongle declares `S24_3LE` for its own single-DAC
                // case while this composite stays `S16_LE`, because the emitted
                // format is resolved from the ARMED profile's id.
                format: config.declared_dac_format,
                buffer_frames: config.dac_buffer_frames,
                manual_start: true,
            })
            .with_context(|| format!("configuring outputd dual DAC A PCM {}", dac_a_pcm)),
        )?;

        let dac_b = final_sink_startup(
            PCM::new(&dac_b_pcm, Direction::Playback, false)
                .with_context(|| format!("opening outputd dual DAC B PCM {}", dac_b_pcm)),
        )?;
        let dac_b_negotiated = final_sink_startup(
            configure_pcm(PcmConfig {
                role: "dual_dac_b",
                pcm_name: &dac_b_pcm,
                pcm: &dac_b,
                sample_rate: config.sample_rate,
                period_frames: config.period_frames,
                channels: CHANNELS,
                // Same declaration as child A — see that call's comment.
                format: config.declared_dac_format,
                buffer_frames: config.dac_buffer_frames,
                manual_start: true,
            })
            .with_context(|| format!("configuring outputd dual DAC B PCM {}", dac_b_pcm)),
        )?;

        if dac_a_negotiated != dac_b_negotiated {
            return final_sink_startup(Err(anyhow::anyhow!(
                "dual Apple DACs negotiated different shapes: A={:?} B={:?}",
                dac_a_negotiated,
                dac_b_negotiated
            )));
        }

        let mut linked = false;
        match dac_a.link(&dac_b) {
            Ok(()) => {
                linked = true;
                eprintln!("event=outputd.dual_apple.link status=ok");
            }
            Err(err) => {
                eprintln!("event=outputd.dual_apple.link status=failed detail={err}");
            }
        }

        // `link=ok` is a precondition for driving a composite at all: the
        // recovery model that makes the pair safe is built on `snd_pcm_link`
        // (group prepare, group re-prime, one atomic group start). Without it
        // the pair's post-recovery A/B skew is unbounded and unverifiable, and
        // on an active 2-way that skew IS the woofer/tweeter time alignment.
        //
        // It used to gate only the RING ARM, because an unlinked pair could
        // stay on the snd-aloop transport instead. Under one audio transport
        // (ADR-0100) there is no other transport to keep, so the gate is
        // unconditional and the box parks rather than running a recovery model
        // that does not hold.
        //
        // PARK-class (EX_CONFIG 78), like every other refusal in this
        // constructor: whether two devices will link is a property of the
        // devices and their drivers, so it answers identically on every restart.
        // Walking the restart ladder would only spend the box's start budget on
        // its way to `StartLimitAction=reboot`. Parking puts it where an
        // operator — and jasper-doctor — can see it.
        if !linked {
            eprintln!("event=outputd.dual_apple.unlinked_pair_refused reason=link_required");
            return final_sink_startup(Err(anyhow::anyhow!(
                "outputd composite sink refuses to drive an unlinked child pair \
                 ({dac_a_pcm} + {dac_b_pcm}): snd_pcm_link is what makes the post-xrun group \
                 re-prime and atomic group start able to re-establish A/B alignment, so an \
                 unlinked pair has no safe recovery."
            )));
        }

        // `content_source=`, `content_format=` and `format=` spelled exactly as
        // the single sink's `event=outputd.alsa.opened` line spells them, so one
        // grep recipe reads both hops and a half-flipped box is visible at open
        // rather than only in STATUS. `format=` is the CHILDREN's edge (both
        // children, one declaration); it is bare for the same reason it is bare
        // over there — operators and journal recipes read that key.
        eprintln!(
            "event=outputd.dual_apple.opened content_source=shm_ring content_format={} dac_a_pcm={} dac_b_pcm={} sample_rate={} period_frames={} content_buffer_frames={} dac_buffer_frames={} linked={} max_delay_delta_frames={} format={}",
            config.content_format.as_str(),
            dac_a_pcm,
            dac_b_pcm,
            dac_a_negotiated.sample_rate,
            dac_a_negotiated.period_frames,
            content_negotiated.buffer_frames,
            dac_a_negotiated.buffer_frames,
            linked,
            config.dual_max_delay_delta_frames,
            config.declared_dac_format.as_str(),
        );

        Ok(Self {
            dac_a,
            dac_b,
            dac_a_pcm,
            dac_b_pcm,
            content_negotiated,
            dac_negotiated: dac_a_negotiated,
            counters: IoCounters::default(),
            linked,
            dac_a_xrun_count: 0,
            dac_b_xrun_count: 0,
            group_recoveries: 0,
            delay_baseline_relatches: 0,
            reprime_alignment_failures: 0,
            delay_delta_baseline: None,
            last_delay_delta: None,
            last_delay_delta_error: None,
            max_delay_delta_frames: config.dual_max_delay_delta_frames,
            // `configure_pcm`'s final-edge readback checked the installed
            // client-side format on BOTH children against this requested one, so
            // buffers built at the request are buffers at what outputd is running
            // — neither child reached here otherwise. Built at the top of this
            // function (see there for why it happens before the opens).
            periods,
        })
    }

    pub fn counters(&self) -> IoCounters {
        self.counters
    }

    /// The format BOTH children's edges negotiated (readback-checked at open by
    /// `configure_pcm`'s final-edge readback, per child).
    ///
    /// The composite twin of [`AlsaBackend::dac_format`], and what `/state`
    /// reports as `dac.format` for this transport. One value for the pair: both
    /// children request one declaration, and their buffers are one
    /// [`ChildPeriods`] — so there is no per-child answer to give, and no second
    /// place this could disagree with what the write path actually writes.
    pub fn dac_format(&self) -> SampleFormat {
        self.periods.format()
    }

    pub fn dual_status(&self) -> CompositeStatus {
        CompositeStatus {
            dac_a_pcm: self.dac_a_pcm.clone(),
            dac_b_pcm: self.dac_b_pcm.clone(),
            linked: self.linked,
            delay_delta_frames: self.last_delay_delta,
            delay_delta_baseline_frames: self.delay_delta_baseline,
            delay_delta_error_frames: self.last_delay_delta_error,
            max_delay_delta_frames: self.max_delay_delta_frames,
            dac_a_xruns: self.dac_a_xrun_count,
            dac_b_xruns: self.dac_b_xrun_count,
            group_recoveries: self.group_recoveries,
            delay_baseline_relatches: self.delay_baseline_relatches,
            reprime_alignment_failures: self.reprime_alignment_failures,
        }
    }

    pub fn start_dacs(&self) -> Result<()> {
        if self.linked {
            if self.dac_a.state() != State::Running || self.dac_b.state() != State::Running {
                self.dac_a
                    .start()
                    .context("starting linked outputd dual Apple DACs")?;
            }
            let state_a = self.dac_a.state();
            let state_b = self.dac_b.state();
            if state_a != State::Running || state_b != State::Running {
                anyhow::bail!(
                    "linked outputd dual Apple DACs did not both enter Running state: dac_a={:?} dac_b={:?}",
                    state_a,
                    state_b
                );
            }
            return Ok(());
        }
        if self.dac_a.state() != State::Running {
            self.dac_a
                .start()
                .context("starting outputd dual Apple DAC A")?;
        }
        if self.dac_b.state() != State::Running {
            self.dac_b
                .start()
                .context("starting outputd dual Apple DAC B")?;
        }
        Ok(())
    }

    /// Split one 4-channel program period to the two children and write both —
    /// **the composite's single quantization, at the children's edge** — with a
    /// bounded linked-group recovery around the write.
    ///
    /// The split's width arm is chosen by [`ChildPeriods`], which is the
    /// children's negotiated width: an `S16Le` pair narrows once per sample
    /// during the split (round-to-nearest, the same primitive the coherent
    /// sink's `S16Le` edge uses), an `S32Le` pair carries the spine's own
    /// samples and converts nothing. Every buffer either arm touches was sized
    /// at open.
    ///
    /// **Every exit from this period is either the write completing or a bail**
    /// (#2255) — there is no arm that returns `Ok` having silently given up, and
    /// no arm that retries without spending budget. An xrun is the one fault
    /// that gets a bounded second chance: the recovery ladder is
    /// recover → re-prime → group start → re-latch, and it bails at any rung it
    /// cannot complete (budget exhausted, an unlinked pair, an xrun on the
    /// just-prepared pair, a group start that does not reach Running, a
    /// re-latch outside the magnitude bound) as well as on the steady-state
    /// divergence guard. Every other error — a non-`EPIPE` `writei`, an IO
    /// handle, a `snd_pcm_delay` read — propagates as it always did.
    ///
    /// What it does NOT do is bail on the FIRST xrun, which is what used to
    /// exit 1 and ride `Restart=on-failure` → `StartLimitBurst=5` →
    /// `StartLimitAction=reboot` on a USB dongle burp.
    ///
    /// **The loop is bounded by the budget, and the budget is declared here,
    /// outside it, on purpose.** Each recovery spends one unit, and the
    /// re-prime's own writes spend from the same unit — so a child that xruns
    /// forever bails after `MAX_RECOVERIES_PER_PERIOD` instead of spinning on
    /// outputd's SCHED_FIFO playout thread. Moving the declaration inside the
    /// loop re-zeroes it every pass and makes the recovery unbounded; the
    /// position is pinned by
    /// `test_the_composite_recovery_budget_is_per_period_not_per_attempt`.
    pub fn write_dual_period(&mut self, samples_4ch: &[ProgramSample]) -> Result<()> {
        // ONE budget per period, shared by both children and by the re-prime —
        // the same shape the coherent single sink's per-period `recoveries` has,
        // through the same `xrun_policy`. A pair that burps four times inside
        // one period is not recovering, it is failing, and the bail is what puts
        // that on the restart ladder rather than in an unbounded loop.
        let mut recoveries = 0u32;
        loop {
            self.periods.deinterleave(samples_4ch)?;
            match self.write_children(&mut recoveries)? {
                ChildWriteOutcome::Complete => break,
                // The group was recovered, so BOTH children are prepared and
                // empty: re-establish alignment, then write this period again
                // from the top. Writing only "the rest" would be wrong — the
                // prepare dropped whatever either child had queued.
                ChildWriteOutcome::GroupRecovered => {
                    self.reprime_after_group_recovery(&mut recoveries)?
                }
            }
        }
        self.counters.dac_frames_written += (samples_4ch.len() / 4) as u64;
        if self.dac_a.state() == State::Running && self.dac_b.state() == State::Running {
            self.check_delay_delta()?;
        }
        Ok(())
    }

    /// Write the two child period buffers — whatever they hold at this point —
    /// to A, then B.
    ///
    /// The per-period write body, and the reason it is its own function: the
    /// re-prime needs the identical A-then-B interleave over a silent payload,
    /// and a second copy of this is exactly where "prime A fully, then prime B"
    /// would creep back in — which is the skew hazard the whole recovery model
    /// exists to avoid.
    ///
    /// The width arm is chosen by [`ChildPeriods`], which is the children's
    /// negotiated width. Every buffer either arm touches was sized at open.
    ///
    /// The `.context(...)` strings are `&'static str` rather than `format!`-ed
    /// PCM names, exactly as `AlsaBackend::write_dac_period`'s io-handle context
    /// is: this body is period-hot and
    /// `test_outputd_period_hot_functions_do_not_allocate` covers it. Both
    /// children are named literally so the message still says which one failed;
    /// the PCM aliases are on the open-time `event=outputd.dual_apple.opened`
    /// line, in STATUS, and on each child's `event=outputd.xrun` line.
    fn write_children(&mut self, recoveries: &mut u32) -> Result<ChildWriteOutcome> {
        let linked = self.linked;
        match &mut self.periods {
            ChildPeriods::S16 { a, b } => {
                let io_a = self
                    .dac_a
                    .io_i16()
                    .context("getting i16 IO handle for outputd dual Apple DAC A")?;
                if write_dac_fail_closed(
                    &io_a,
                    ChildWrite {
                        pcm: &self.dac_a,
                        pcm_name: &self.dac_a_pcm,
                        source: "dual_dac_a",
                        linked,
                    },
                    a,
                    ChildWriteLedger {
                        recoveries,
                        xrun_count: &mut self.counters.dac_xrun_count,
                        child_xrun_count: &mut self.dac_a_xrun_count,
                        group_recoveries: &mut self.group_recoveries,
                    },
                )? == ChildWriteOutcome::GroupRecovered
                {
                    return Ok(ChildWriteOutcome::GroupRecovered);
                }
                let io_b = self
                    .dac_b
                    .io_i16()
                    .context("getting i16 IO handle for outputd dual Apple DAC B")?;
                write_dac_fail_closed(
                    &io_b,
                    ChildWrite {
                        pcm: &self.dac_b,
                        pcm_name: &self.dac_b_pcm,
                        source: "dual_dac_b",
                        linked,
                    },
                    b,
                    ChildWriteLedger {
                        recoveries,
                        xrun_count: &mut self.counters.dac_xrun_count,
                        child_xrun_count: &mut self.dac_b_xrun_count,
                        group_recoveries: &mut self.group_recoveries,
                    },
                )
            }
            ChildPeriods::S32 { a, b } => {
                let io_a = self
                    .dac_a
                    .io_i32()
                    .context("getting i32 IO handle for outputd dual Apple DAC A")?;
                if write_dac_fail_closed(
                    &io_a,
                    ChildWrite {
                        pcm: &self.dac_a,
                        pcm_name: &self.dac_a_pcm,
                        source: "dual_dac_a",
                        linked,
                    },
                    a,
                    ChildWriteLedger {
                        recoveries,
                        xrun_count: &mut self.counters.dac_xrun_count,
                        child_xrun_count: &mut self.dac_a_xrun_count,
                        group_recoveries: &mut self.group_recoveries,
                    },
                )? == ChildWriteOutcome::GroupRecovered
                {
                    return Ok(ChildWriteOutcome::GroupRecovered);
                }
                let io_b = self
                    .dac_b
                    .io_i32()
                    .context("getting i32 IO handle for outputd dual Apple DAC B")?;
                write_dac_fail_closed(
                    &io_b,
                    ChildWrite {
                        pcm: &self.dac_b,
                        pcm_name: &self.dac_b_pcm,
                        source: "dual_dac_b",
                        linked,
                    },
                    b,
                    ChildWriteLedger {
                        recoveries,
                        xrun_count: &mut self.counters.dac_xrun_count,
                        child_xrun_count: &mut self.dac_b_xrun_count,
                        group_recoveries: &mut self.group_recoveries,
                    },
                )
            }
        }
    }

    /// Re-establish A/B alignment after a group recovery: re-prime BOTH children
    /// to the startup depth, interleaved, then start the group explicitly, then
    /// re-latch the pairwise baseline under a magnitude bound.
    ///
    /// **Why a re-prime and not "resume writing".** `snd_pcm_recover` prepared
    /// the group and dropped whatever either child had queued, so the pair is at
    /// a known-empty state — the only state from which alignment can be
    /// re-established rather than assumed. What must NOT happen is what a
    /// full-buffer refill would do: `manual_start` sets
    /// `start_threshold = buffer_frames`, so filling the buffer AUTO-STARTS the
    /// linked group the moment child A's buffer fills — before child B has been
    /// primed at all — baking in up to a full period of PERMANENT skew. That is
    /// the exact hazard the re-prime exists to remove, so it must not be
    /// re-created by the removal.
    ///
    /// Hence the shipped startup path, reused verbatim rather than re-derived:
    /// prime to [`prime_periods`] depth (one period of buffer headroom,
    /// deliberately BELOW `start_threshold`, so nothing auto-starts), fill those
    /// periods through the same A-then-B [`Self::write_children`] the run loop
    /// uses (so the two children's fill states are equal at the moment of
    /// start), and only THEN call [`Self::start_dacs`], which on a linked pair
    /// starts both atomically and already asserts both reach Running.
    ///
    /// An xrun DURING the re-prime fails closed rather than recursing. A
    /// prepared, un-started stream has no playback position to underrun from, so
    /// an EPIPE here means the pair is not in the state the recovery just put it
    /// in — and a second recovery layered on an unexplained one would be
    /// blessing a state nobody has verified. It is also what keeps this bounded:
    /// one recovery, one re-prime, no nesting.
    fn reprime_after_group_recovery(&mut self, recoveries: &mut u32) -> Result<()> {
        let pre_fault_baseline = self.delay_delta_baseline;
        let depth = prime_periods(
            self.dac_negotiated.buffer_frames,
            self.dac_negotiated.period_frames,
        );
        self.periods.fill_silence();
        let period_frames = self.periods.frames() as u64;
        for _ in 0..depth {
            if self.write_children(recoveries)? == ChildWriteOutcome::GroupRecovered {
                self.reprime_alignment_failures += 1;
                eprintln!("event=outputd.dual_apple.reprime status=xrun_during_reprime");
                anyhow::bail!(
                    "outputd dual Apple re-prime hit an xrun on a prepared child pair: \
                     refusing to layer a second recovery on a group state that has not \
                     been re-established"
                );
            }
            // These frames really were handed to the children, so they count —
            // the STARTUP prime's identical silence already does (it goes
            // through `write_dual_period` from the run loop), and one counter
            // that means "frames written to the DACs" beats one that means it
            // except during recovery.
            self.counters.dac_frames_written += period_frames;
        }
        self.start_dacs()
            .context("starting outputd dual Apple DAC group after xrun recovery")?;
        self.relatch_delay_baseline(pre_fault_baseline)
    }

    /// Bless the re-primed pair's A/B offset as the new zero — but only if the
    /// pair came back to within `max_delay_delta_frames` of where it was.
    ///
    /// The re-latch is necessary: `snd_pcm_recover` resets the children's stream
    /// positions, a real and permanent step. Without it, the very next
    /// [`Self::check_delay_delta`] would measure the post-recovery offset
    /// against a pre-recovery baseline and bail — turning "bail on the first
    /// xrun" into "bail on the first period after the first xrun", with a
    /// scarier diagnosis (`delay_diverged` instead of `xrun`).
    ///
    /// The bound is what keeps the re-latch from laundering the fault: see
    /// [`baseline_relatch_decision`] for why the bound is a MAGNITUDE and not a
    /// count.
    fn relatch_delay_baseline(&mut self, pre_fault_baseline: Option<i64>) -> Result<()> {
        let delay_a = self
            .dac_a
            .delay()
            .context("reading outputd dual DAC A delay after re-prime")?;
        let delay_b = self
            .dac_b
            .delay()
            .context("reading outputd dual DAC B delay after re-prime")?;
        let delta = delay_a - delay_b;
        match baseline_relatch_decision(pre_fault_baseline, delta, self.max_delay_delta_frames) {
            BaselineRelatch::Accept => {
                self.delay_delta_baseline = Some(delta);
                self.last_delay_delta = Some(delta);
                self.last_delay_delta_error = Some(0);
                self.delay_baseline_relatches += 1;
                eprintln!(
                    "event=outputd.dual_apple.reprime status=ok baseline_delta_frames={} \
                     prior_baseline_frames={} relatches={} max_delay_delta_frames={}",
                    delta,
                    pre_fault_baseline.unwrap_or(delta),
                    self.delay_baseline_relatches,
                    self.max_delay_delta_frames,
                );
                Ok(())
            }
            BaselineRelatch::Refuse => {
                self.reprime_alignment_failures += 1;
                let prior = pre_fault_baseline.unwrap_or(delta);
                eprintln!(
                    "event=outputd.dual_apple.reprime status=alignment_refused \
                     baseline_delta_frames={} prior_baseline_frames={} \
                     baseline_step_frames={} max_delay_delta_frames={}",
                    delta,
                    prior,
                    (delta - prior).abs(),
                    self.max_delay_delta_frames,
                );
                anyhow::bail!(
                    "outputd dual Apple re-prime did not restore alignment: baseline moved \
                     {} frames (from {} to {}), beyond the {}-frame tolerance",
                    (delta - prior).abs(),
                    prior,
                    delta,
                    self.max_delay_delta_frames
                );
            }
        }
    }

    pub fn dac_delay_frames(&self) -> Result<u64> {
        let a = self
            .dac_a
            .delay()
            .context("reading outputd dual DAC A delay")?
            .max(0) as u64;
        let b = self
            .dac_b
            .delay()
            .context("reading outputd dual DAC B delay")?
            .max(0) as u64;
        Ok(a.max(b))
    }

    fn check_delay_delta(&mut self) -> Result<()> {
        let delay_a = self
            .dac_a
            .delay()
            .context("reading outputd dual DAC A delay")?;
        let delay_b = self
            .dac_b
            .delay()
            .context("reading outputd dual DAC B delay")?;
        let delta = delay_a - delay_b;
        let baseline = *self.delay_delta_baseline.get_or_insert(delta);
        let check = delay_delta_check(delta, baseline, self.max_delay_delta_frames);
        let error = check.error_frames;
        self.last_delay_delta = Some(delta);
        self.last_delay_delta_error = Some(error);
        if check.diverged {
            eprintln!(
                "event=outputd.dual_apple.delay_diverged current_delta_frames={} baseline_frames={} error_frames={} max_error_frames={}",
                delta,
                baseline,
                error,
                self.max_delay_delta_frames
            );
            anyhow::bail!(
                "outputd dual Apple delay divergence: current_delta={} baseline={} error={} max={}",
                delta,
                baseline,
                error,
                self.max_delay_delta_frames
            );
        }
        if self.dac_a.state() != State::Running || self.dac_b.state() != State::Running {
            anyhow::bail!(
                "outputd dual Apple bad PCM state: dac_a={:?} dac_b={:?}",
                self.dac_a.state(),
                self.dac_b.state()
            );
        }
        Ok(())
    }
}

pub fn open_playback_pcm(
    role: &str,
    pcm_name: &str,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
) -> Result<(PCM, NegotiatedPcm)> {
    let pcm = PCM::new(pcm_name, Direction::Playback, false)
        .with_context(|| format!("opening outputd {role} playback PCM {pcm_name}"))?;
    let negotiated = configure_pcm(PcmConfig {
        role,
        pcm_name,
        pcm: &pcm,
        sample_rate,
        period_frames,
        channels: CHANNELS,
        // The chip-AEC reference leg's only caller; its native contract is
        // 16 kHz/2ch/S16_LE and `validate_chip_ref_geometry` enforces it.
        format: SampleFormat::S16Le,
        buffer_frames,
        manual_start: true,
    })
    .with_context(|| format!("configuring outputd {role} playback PCM {pcm_name}"))?;
    Ok((pcm, negotiated))
}

struct PcmConfig<'a> {
    role: &'a str,
    pcm_name: &'a str,
    pcm: &'a PCM,
    sample_rate: u32,
    period_frames: u32,
    channels: u16,
    /// The format to REQUEST from ALSA for this PCM. Every role that varies it
    /// reads one of TWO independent declarations: the final-edge roles
    /// ([`is_final_edge_role`] — `dac` and both composite children) take the
    /// registry-declared edge (`Config::declared_dac_format`), and the `content`
    /// / `active_content` lanes take `Config::content_format`. Both default to
    /// `S16Le`. The one remaining role, `chip_ref`, passes `S16Le` explicitly by
    /// contract (16 kHz/2ch/S16, `validate_chip_ref_geometry` exact), which is
    /// byte-identical to the retired module-level `FORMAT` const it used to
    /// inherit.
    format: SampleFormat,
    buffer_frames: u32,
    manual_start: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct PcmGeometry {
    sample_rate: u32,
    channels: u32,
    format: Format,
    access: Access,
    period_frames: u32,
    buffer_frames: u32,
}

fn configure_pcm(config: PcmConfig<'_>) -> Result<NegotiatedPcm> {
    let PcmConfig {
        role,
        pcm_name,
        pcm,
        sample_rate,
        period_frames,
        channels,
        format,
        buffer_frames,
        manual_start,
    } = config;
    let requested_format = alsa_format(format);
    let negotiated;
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(channels as u32)
            .with_context(|| format!("set_channels({})", channels))?;
        hwp.set_rate(sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", sample_rate))?;
        hwp.set_format(requested_format)
            .with_context(|| format!("set_format({:?})", requested_format))?;
        hwp.set_access(Access::RWInterleaved)
            .context("set_access(RWInterleaved)")?;
        hwp.set_period_size(period_frames as i64, ValueOr::Nearest)
            .with_context(|| format!("set_period_size({})", period_frames))?;
        hwp.set_buffer_size(buffer_frames as i64)
            .with_context(|| format!("set_buffer_size({})", buffer_frames))?;
        negotiated = NegotiatedPcm {
            sample_rate: hwp.get_rate().context("get_rate")?,
            period_frames: hwp.get_period_size().context("get_period_size")? as u32,
            buffer_frames: hwp.get_buffer_size().context("get_buffer_size")? as u32,
        };
        pcm.hw_params(&hwp).context("installing HwParams")?;
    }
    if is_final_edge_role(role) {
        // Prove what OUTPUTD'S OWN CLIENT EDGE ended up at, and fail closed if
        // it is not what was requested. Read the scope of that claim honestly,
        // because it depends on what sits between outputd and the card:
        //
        // `hw_params_current` returns the CLIENT-side params of the PCM outputd
        // opened. On a raw `hw:` device the client edge IS the hardware edge, so
        // this readback IS a hardware-edge proof. Through a `plug` it would NOT
        // be: a plug installs the client's own request client-side and converts
        // on the slave side, so the readback would agree BY CONSTRUCTION and
        // could never see what the DAC is really doing.
        //
        // Post-plug truth: every registered single DAC profile opens its
        // `outputd_dac` PCM as a raw `hw:` device now, InnoMaker included —
        // deploy/lib/jasper-asound-render.sh's per-profile plug was deleted in
        // PR-4 (format-foundation), so there is no conversion layer left
        // anywhere in front of this role's PCM. This readback therefore fully
        // participates in the hardware-edge proof for every DAC, where before
        // it was narrower than it looked. What keeps a plug from returning is a
        // Python-side structural test, not this Rust check:
        // tests/test_outputd_wiring.py::test_every_single_dac_profile_renders_raw_hw_with_no_plug
        // drives the render script for every registered single DAC profile and
        // asserts a raw `type hw` block with no `plug` in the output.
        //
        // The COMPOSITE CHILDREN reach here on the same terms and with the same
        // scope: `jasper.output_hardware` hands the reconciler each child as a
        // raw `hw:CARD=<card>,DEV=<n>` alias, so a child's client edge is its
        // hardware edge too, and each child is checked rather than trusted to
        // have honoured the S16 it asked for.
        let current = pcm
            .hw_params_current()
            .context("reading installed outputd DAC HwParams")?;
        let installed = current
            .get_format()
            .context("get installed outputd DAC format")?;
        verify_dac_format(role, pcm_name, format, installed)?;
    }
    if role == "chip_ref" {
        let current = pcm
            .hw_params_current()
            .context("reading installed outputd chip_ref HwParams")?;
        validate_chip_ref_geometry(
            pcm_name,
            PcmGeometry {
                sample_rate: current.get_rate().context("get installed chip_ref rate")?,
                channels: current
                    .get_channels()
                    .context("get installed chip_ref channels")?,
                format: current
                    .get_format()
                    .context("get installed chip_ref format")?,
                access: current
                    .get_access()
                    .context("get installed chip_ref access")?,
                period_frames: current
                    .get_period_size()
                    .context("get installed chip_ref period size")?
                    as u32,
                buffer_frames: current
                    .get_buffer_size()
                    .context("get installed chip_ref buffer size")?
                    as u32,
            },
        )?;
    }
    if manual_start {
        let swp = pcm
            .sw_params_current()
            .with_context(|| format!("reading outputd {role} SwParams"))?;
        swp.set_start_threshold(negotiated.buffer_frames as i64)
            .with_context(|| format!("setting outputd {role} start_threshold"))?;
        pcm.sw_params(&swp)
            .with_context(|| format!("installing outputd {role} SwParams"))?;
    }
    validate_negotiated(role, pcm_name, negotiated, sample_rate, period_frames)?;
    Ok(negotiated)
}

/// The roles whose PCM is a FINAL HARDWARE EDGE: the coherent single DAC, and
/// each child of a paired composite.
///
/// A named predicate rather than an inline `matches!` inside `configure_pcm`,
/// because `configure_pcm` cannot be called without a live PCM and so cannot be
/// unit-tested at all — pulling the role set out here makes "which roles get the
/// edge readback" a pure function a test can pin, which is the whole content of
/// the branch. Deliberately an ALLOWLIST: a future role that is an edge has to
/// be added here on purpose, and one that is not (the content lanes, `chip_ref`)
/// cannot fall in by resembling a name.
fn is_final_edge_role(role: &str) -> bool {
    matches!(role, "dac" | "dual_dac_a" | "dual_dac_b")
}

/// Fail closed unless the format ALSA installed on OUTPUTD'S OWN CLIENT EDGE
/// is exactly the one requested.
///
/// Scope: the client edge equals the hardware edge only on a raw `hw:` device
/// — true for every registered single DAC today, InnoMaker included, since
/// PR-4 (format-foundation) deleted the last per-profile `plug`, and true for
/// both composite children, which the reconciler passes as raw `hw:CARD=…`
/// aliases. See `configure_pcm`'s dac readback comment for why a `plug` would
/// defeat this check, and for the regression test that keeps one from coming
/// back.
///
/// Separate from `validate_negotiated` because the failure is a different kind:
/// a rate or period mismatch means the device could not serve outputd's
/// geometry, while this means it served a DIFFERENT sample format than the one
/// the alignment identity would be certified against. Called only from
/// `configure_pcm`'s final-edge roles ([`is_final_edge_role`]), every one of
/// whose callers wraps it in `final_sink_startup` — so a mismatch parks the unit
/// at EX_CONFIG 78 rather than restart-looping against hardware that cannot
/// change.
///
/// `role` is a parameter, not the constant `"dac"` it was before the composite
/// children joined: with three edges in play, "which edge moved" is only
/// answerable if the message says which one it was.
///
/// It is NOT redundant with `alsa`'s own `io_i16`/`io_i32` format check. That
/// one runs per period, deep on the audio path, and reports an opaque
/// `unsupported io_xx`; this runs once, at startup, names both formats, and
/// reaches the operator as a park rather than as a restart loop.
fn verify_dac_format(
    role: &str,
    pcm_name: &str,
    requested: SampleFormat,
    installed: Format,
) -> Result<()> {
    verify_installed_format(role, pcm_name, requested, installed)
}

/// The one request-vs-installed comparison. Names BOTH formats: the operator
/// has to know which end moved.
fn verify_installed_format(
    role: &str,
    pcm_name: &str,
    requested: SampleFormat,
    installed: Format,
) -> Result<()> {
    let want = alsa_format(requested);
    if installed != want {
        anyhow::bail!(
            "outputd {role} PCM {pcm_name} installed format {:?} but outputd requested {} ({:?})",
            installed,
            requested.as_str(),
            want
        );
    }
    Ok(())
}

/// The i16 staging one period needs at `format` — for BOTH directions.
///
/// The program spine is i32, so only an `S16Le` hop needs an i16 staging buffer:
/// an `S16Le` DAC edge narrows into one before writing, and an `S16Le` content
/// lane reads into one before widening. An `S32Le` hop moves the spine's own
/// samples and holds an EMPTY vec — zero bytes. So does an `S24_3Le` hop, whose
/// staging is BYTES, not i16 — [`i24_packed_staging`] owns that one.
///
/// One function for both directions because the shape is identical (period ×
/// channels of i16) and because a second copy would be a twin that drifts. It is
/// called once per hop, at open, never on the audio path.
fn s16_staging(format: SampleFormat, period_frames: u32, channels: u16) -> Vec<i16> {
    match format {
        SampleFormat::S16Le => vec![0i16; (period_frames as usize) * (channels as usize)],
        SampleFormat::S24_3Le | SampleFormat::S32Le => Vec::new(),
    }
}

/// The PACKED-BYTE staging one period needs at `format`: `period × channels × 3`
/// bytes at an `S24_3Le` edge, an EMPTY vec at every other width.
///
/// The byte-shaped sibling of [`s16_staging`], and separate from it for the same
/// reason the two backend fields are separate — the element type is the whole
/// difference, and a single function cannot return both. Same discipline
/// otherwise: called once, at open, never on the audio path, and it reserves
/// nothing at a width that does not use it.
///
/// The 3 comes from `jasper_resampler::I24_LE_BYTES_PER_SAMPLE`, the same
/// constant the packing primitive's length contract reads, so the staging and the
/// conversion cannot disagree about the stride.
fn i24_packed_staging(format: SampleFormat, period_frames: u32, channels: u16) -> Vec<u8> {
    match format {
        SampleFormat::S24_3Le => vec![
            0u8;
            (period_frames as usize)
                * (channels as usize)
                * jasper_resampler::I24_LE_BYTES_PER_SAMPLE
        ],
        SampleFormat::S16Le | SampleFormat::S32Le => Vec::new(),
    }
}

fn validate_chip_ref_geometry(pcm_name: &str, actual: PcmGeometry) -> Result<()> {
    let required = PcmGeometry {
        sample_rate: 16_000,
        channels: 2,
        format: Format::S16LE,
        access: Access::RWInterleaved,
        period_frames: 128,
        buffer_frames: 256,
    };
    if actual != required {
        anyhow::bail!(
            "outputd chip_ref PCM {pcm_name} installed geometry {:?} but requires exact {:?}",
            actual,
            required
        );
    }
    Ok(())
}

fn validate_negotiated(
    role: &str,
    pcm_name: &str,
    negotiated: NegotiatedPcm,
    sample_rate: u32,
    period_frames: u32,
) -> Result<()> {
    if negotiated.sample_rate != sample_rate {
        anyhow::bail!(
            "outputd {role} PCM {pcm_name} negotiated sample_rate={} but outputd requires {}",
            negotiated.sample_rate,
            sample_rate
        );
    }
    if negotiated.period_frames != period_frames {
        anyhow::bail!(
            "outputd {role} PCM {pcm_name} negotiated period_frames={} but outputd requires {}",
            negotiated.period_frames,
            period_frames
        );
    }
    if negotiated.buffer_frames < period_frames.saturating_mul(2) {
        anyhow::bail!(
            "outputd {role} PCM {pcm_name} negotiated buffer_frames={} but requires at least 2 x period_frames={}",
            negotiated.buffer_frames,
            period_frames
        );
    }
    Ok(())
}

/// Split one 4-channel program period into the composite's two stereo child
/// periods, at the children's declared edge width.
///
/// **This is where the composite meets its children's width — the composite
/// sink's counterpart to `AlsaBackend::write_dac_period`, and its ONLY
/// conversion site.** The width is chosen by the output slices' element type,
/// through the closed [`ChildEdgeSample`] trait: `i16` children quantize here,
/// round-to-nearest and saturating, the same primitive the coherent sink's
/// `S16Le` edge narrows through; `i32` children are already the spine's own
/// width and are copied unconverted.
///
/// ONE loop over both widths, not one loop each: the frame arithmetic and the
/// two bounds checks are the fail-closed part (a short scratch would write a
/// partial period to a live DAC), and a second copy of them would be a twin that
/// drifts. Only the per-sample conversion varies, and it varies by type.
///
/// The channel map is width-independent and unchanged: child A takes program
/// channels 0/1, child B takes 2/3.
fn deinterleave_4ch_to_dual_stereo<T: ChildEdgeSample>(
    samples_4ch: &[ProgramSample],
    out_a: &mut [T],
    out_b: &mut [T],
) -> Result<()> {
    if samples_4ch.len() % 4 != 0 {
        anyhow::bail!("active content period does not contain whole 4-channel frames");
    }
    let frames = samples_4ch.len() / 4;
    if out_a.len() < frames * 2 || out_b.len() < frames * 2 {
        anyhow::bail!("dual output scratch buffers are smaller than content period");
    }
    for frame in 0..frames {
        let src = frame * 4;
        let dst = frame * 2;
        out_a[dst] = T::from_program(samples_4ch[src]);
        out_a[dst + 1] = T::from_program(samples_4ch[src + 1]);
        out_b[dst] = T::from_program(samples_4ch[src + 2]);
        out_b[dst + 1] = T::from_program(samples_4ch[src + 3]);
    }
    Ok(())
}

/// The coherent single DAC's period write loop, over whatever element type the
/// negotiated edge takes.
///
/// One loop, not one per format: the xrun accounting, the bounded recovery
/// budget, and the `event=outputd.xrun` line are the audio path's fail-closed
/// behaviour and must not be able to drift between an S16, an S24_3LE and an S32
/// edge. Monomorphised for `i16` this is instruction-for-instruction the
/// pre-native-format writer.
///
/// `elements_per_frame` is the stride of ONE FRAME in units of `S`, and it is
/// deliberately not called `channels`:
///
/// * At a TYPED edge (`IO<i16>`, `IO<i32>`) one element is one sample, so the
///   stride IS the channel count — every existing caller passes exactly that and
///   is unchanged.
/// * At the PACKED `S24_3LE` edge the handle is `IO<u8>` and one element is one
///   BYTE, so the stride is `channels * 3`. Passing `channels` there would slice
///   forward at a third of the right rate and re-send most of every period as the
///   loop chased `frames_total`.
///
/// The parameter carries the difference so the loop body does not have to know
/// which kind of edge it is serving — which is what keeps ONE xrun policy for all
/// three widths.
fn write_dac_frames<S: Copy>(
    io: &IO<'_, S>,
    pcm: &PCM,
    pcm_name: &str,
    samples: &[S],
    elements_per_frame: usize,
    xrun_count: &mut u64,
) -> Result<()> {
    let frames_total = samples.len() / elements_per_frame;
    let mut frames_done = 0usize;
    let mut recoveries = 0u32;

    while frames_done < frames_total {
        let offset = frames_done * elements_per_frame;
        match io.writei(&samples[offset..]) {
            Ok(n) => {
                frames_done += n;
                if n == 0 {
                    recoveries += 1;
                    if xrun_policy(recoveries) == XrunAction::GiveUp {
                        anyhow::bail!("outputd DAC writei returned 0 frames repeatedly");
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    *xrun_count += 1;
                    let pending = frames_total - frames_done;
                    eprintln!(
                        "event=outputd.xrun source=dac pcm={} count={} frames_pending={}",
                        pcm_name, *xrun_count, pending
                    );
                    pcm.try_recover(e, true)
                        .context("recovering outputd DAC xrun")?;
                    recoveries += 1;
                    if xrun_policy(recoveries) == XrunAction::GiveUp {
                        anyhow::bail!(
                            "outputd DAC xrun recovery exceeded {} attempts in one period",
                            MAX_RECOVERIES_PER_PERIOD
                        );
                    }
                } else {
                    return Err(e).context(format!("writing outputd DAC PCM {}", pcm_name));
                }
            }
        }
    }
    Ok(())
}

/// What one composite child's period write did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ChildWriteOutcome {
    /// The whole period reached the child.
    Complete,
    /// An xrun was recovered. On a linked pair `snd_pcm_recover` is a GROUP
    /// prepare, so BOTH children are now prepared and empty — the caller owes
    /// the group re-prime, the explicit group start and the bounded baseline
    /// re-latch before any further program audio is written.
    GroupRecovered,
}

/// Everything one composite child write needs to know about the child it is
/// writing. A struct rather than four more parameters, because clippy's
/// argument-count lint is the honest signal here: the loop below takes a
/// buffer, an identity and a ledger, and those are three things.
struct ChildWrite<'a> {
    pcm: &'a PCM,
    pcm_name: &'a str,
    /// The `source=` token on this child's `event=outputd.xrun` line —
    /// `dual_dac_a` or `dual_dac_b`. Per-child attribution is the diagnostic
    /// that says WHICH dongle burped; the recovery it triggers is the group's.
    source: &'static str,
    linked: bool,
}

/// The counters one child write may move. All borrowed separately because they
/// live in different owners: the recovery budget is per-period and stack-local,
/// the xrun counts are the sink's, and the group-recovery count is the pair's.
struct ChildWriteLedger<'a> {
    /// Per-PERIOD recovery budget, shared with the `Ok(0)` arm exactly as the
    /// coherent single sink shares it. Reset by the caller once per period.
    recoveries: &'a mut u32,
    /// The sink-level cumulative xrun count — the existing doctor consumer,
    /// unchanged, and what `count=` on the event line reports (same meaning as
    /// the single path's `count=`).
    xrun_count: &'a mut u64,
    /// This child's own cumulative xrun count, reported in `/state` only.
    child_xrun_count: &'a mut u64,
    /// Cumulative group recoveries ATTEMPTED. Incremented with the xrun
    /// counters, before `try_recover`, so the event line can report it — a
    /// `try_recover` that fails propagates and stops the sink, so the count can
    /// over-report by at most the one terminal period.
    group_recoveries: &'a mut u64,
}

/// One composite CHILD's period write, over whatever sample type its negotiated
/// edge takes.
///
/// Generic for the same reason `write_dac_frames` is: whatever this function's
/// xrun policy is, it must not be able to DIFFER between an S16 and an S32
/// child. The caller fetches the IO handle (`io_i16`/`io_i32` per its
/// `ChildPeriods` arm) rather than this function taking the `PCM` and choosing,
/// which is what makes the width the caller's single decision — and lets the
/// caller use `&'static str` contexts on the period-hot path.
///
/// **The recovery is the GROUP's, and this function does exactly its own half of
/// it.** It attributes the xrun to the reporting child, calls `try_recover`
/// ONCE (on a linked pair that call IS the group prepare — calling it again for
/// the sibling would double-prepare), spends one unit of the SHARED
/// [`xrun_policy`] budget, and hands the re-prime/re-start/re-latch back to
/// [`PairedCompositeSink::write_dual_period`]. It cannot do that half itself:
/// re-priming means writing to BOTH children interleaved, and this function
/// holds one.
///
/// **An UNLINKED pair keeps the pre-change bail, deliberately.** With
/// `linked=false` there is no atomic group restart to re-establish alignment
/// with, so a recovered pair's A/B skew is unbounded and unverifiable — a
/// recovery that cannot be made safe is not a recovery, and the honest answer is
/// the one this path already gave. That is also why `link=ok` is a precondition
/// this sink refuses to start without ([`PairedCompositeSink::new`]); the
/// unlinked composite is not made worse than it was, it is simply not given a
/// recovery it cannot make safe.
///
/// `Ok(0)` now rides the same budget the coherent single sink gives it instead
/// of bailing on the first occurrence: a spurious zero-frame return is not an
/// xrun, moves no stream position, and needs no group action — leaving it a hard
/// bail kept it on the restart ladder this whole change exists to get off.
fn write_dac_fail_closed<S: Copy>(
    io: &IO<'_, S>,
    child: ChildWrite<'_>,
    samples: &[S],
    ledger: ChildWriteLedger<'_>,
) -> Result<ChildWriteOutcome> {
    let frames_total = samples.len() / (CHANNELS as usize);
    let mut frames_done = 0usize;
    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&samples[offset..]) {
            Ok(0) => {
                *ledger.recoveries += 1;
                if xrun_policy(*ledger.recoveries) == XrunAction::GiveUp {
                    anyhow::bail!(
                        "outputd dual Apple DAC {} writei returned 0 frames repeatedly",
                        child.pcm_name
                    );
                }
            }
            Ok(n) => frames_done += n,
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    *ledger.xrun_count += 1;
                    *ledger.child_xrun_count += 1;
                    let pending = frames_total - frames_done;
                    if child.linked {
                        *ledger.group_recoveries += 1;
                    }
                    // ONE event line for both outcomes. `linked=` is what says
                    // which one this is, so a journal recipe reads the recovered
                    // and the refused case with the same grep — and the two
                    // cannot drift into reporting different fields.
                    eprintln!(
                        "event=outputd.xrun source={} pcm={} count={} frames_pending={} group_recoveries={} linked={}",
                        child.source,
                        child.pcm_name,
                        *ledger.xrun_count,
                        pending,
                        *ledger.group_recoveries,
                        child.linked
                    );
                    if !child.linked {
                        anyhow::bail!(
                            "outputd dual Apple DAC {} aborted on xrun/suspend errno={errno}: \
                             the pair is not snd_pcm_link'ed, so no atomic group restart can \
                             re-establish A/B alignment after a recovery",
                            child.pcm_name
                        );
                    }
                    child
                        .pcm
                        .try_recover(e, true)
                        .context("recovering outputd dual Apple DAC group xrun")?;
                    *ledger.recoveries += 1;
                    if xrun_policy(*ledger.recoveries) == XrunAction::GiveUp {
                        anyhow::bail!(
                            "outputd dual Apple DAC group xrun recovery exceeded {} attempts in one period",
                            MAX_RECOVERIES_PER_PERIOD
                        );
                    }
                    return Ok(ChildWriteOutcome::GroupRecovered);
                }
                return Err(e)
                    .context(format!("writing outputd dual Apple DAC {}", child.pcm_name));
            }
        }
    }
    Ok(ChildWriteOutcome::Complete)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES;

    /// One S16 sample at the program spine's scale.
    ///
    /// Fixtures that feed a spine-width parameter MUST go through this. A bare
    /// integer literal type-infers happily into `[ProgramSample]` and then means
    /// something ~65536x quieter than the author intended — which is how
    /// `deinterleaves_active_4ch_to_one_stereo_period_per_dac` compiled green
    /// locally while asserting the wrong audio.
    fn w(sample: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(sample)
    }

    #[test]
    fn final_sink_startup_attaches_configuration_marker() {
        let error = final_sink_startup::<()>(Err(anyhow::anyhow!(
            "synthetic final sink negotiation failure"
        )))
        .unwrap_err();

        assert!(error
            .downcast_ref::<FinalSinkStartupConfigError>()
            .is_some());
    }

    #[test]
    fn negotiated_pcm_accepts_exact_contract() {
        let negotiated = NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 1024,
            buffer_frames: 3072,
        };

        validate_negotiated("dac", "outputd_dac", negotiated, 48_000, 1024).unwrap();
    }

    #[test]
    fn negotiated_pcm_rejects_sample_rate_drift() {
        let negotiated = NegotiatedPcm {
            sample_rate: 44_100,
            period_frames: 1024,
            buffer_frames: 3072,
        };

        let err = validate_negotiated("dac", "outputd_dac", negotiated, 48_000, 1024).unwrap_err();
        assert!(err.to_string().contains("sample_rate=44100"));
    }

    #[test]
    fn negotiated_pcm_rejects_period_drift() {
        let negotiated = NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 960,
            buffer_frames: 3072,
        };

        let err = validate_negotiated("dac", "outputd_dac", negotiated, 48_000, 1024).unwrap_err();
        assert!(err.to_string().contains("period_frames=960"));
    }

    #[test]
    fn negotiated_pcm_rejects_tiny_buffer() {
        let negotiated = NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 1024,
            buffer_frames: 1024,
        };

        let err = validate_negotiated("dac", "outputd_dac", negotiated, 48_000, 1024).unwrap_err();
        assert!(err.to_string().contains("buffer_frames=1024"));
    }

    #[test]
    fn alsa_format_maps_s16_to_the_constant_the_default_path_always_used() {
        // The retired module-level `const FORMAT: Format = Format::S16LE` is
        // now one arm of this mapping. If it ever stops being S16LE, every
        // S16 box silently changes its electrical edge — so pin the literal.
        assert_eq!(alsa_format(SampleFormat::S16Le), Format::S16LE);
        assert_eq!(alsa_format(SampleFormat::S32Le), Format::S32LE);
        // `S243LE` is 24 bits in THREE PACKED BYTES. `S24LE` — one character
        // apart in alsa-rs's spelling — is the same 24 bits inside a 4-byte word,
        // a different wire and NOT in this vocabulary. Requesting the wrong one
        // would make ALSA's own frame arithmetic disagree with the packed staging
        // by 4/3, so the literal is pinned and the wrong neighbour is named.
        assert_eq!(alsa_format(SampleFormat::S24_3Le), Format::S243LE);
        assert_ne!(alsa_format(SampleFormat::S24_3Le), Format::S24LE);
    }

    #[test]
    fn only_the_s16_hop_stages_i16_and_it_stages_one_whole_period() {
        // The INVERSE of what this asserted before the i32 program spine, and
        // that inversion is the payoff: an `S32Le` hop moves the spine's own
        // samples, so it stages nothing at all, while an `S16Le` hop is the one
        // that converts and therefore the one that pays. Sized once at open so
        // the audio path never allocates. Applies to both directions — an S16
        // DAC edge narrows into this shape, an S16 content lane reads into it.
        let wide = s16_staging(SampleFormat::S32Le, 1024, 2);
        assert!(wide.is_empty(), "a wide hop needs no conversion buffer");
        assert_eq!(wide.capacity(), 0, "and must not reserve bytes for one");

        let narrow = s16_staging(SampleFormat::S16Le, 1024, 2);
        assert_eq!(narrow.len(), 2048);
        assert!(narrow.iter().all(|&s| s == 0));

        // Width is carried as data: a wide sink's period is channels x frames.
        assert_eq!(s16_staging(SampleFormat::S16Le, 1024, 8).len(), 8192);

        // An `S24_3Le` hop converts too, but into BYTES — so it takes no i16
        // staging either. If this ever returns a non-empty vec, an S24_3LE box
        // holds two full period buffers where one is used.
        let packed = s16_staging(SampleFormat::S24_3Le, 1024, 2);
        assert!(packed.is_empty(), "a packed hop stages bytes, not i16");
        assert_eq!(packed.capacity(), 0, "and must not reserve bytes for one");
    }

    #[test]
    fn only_the_packed_24_edge_stages_bytes_and_it_stages_three_per_sample() {
        // The byte-staging twin of the test above, and the size is the point: the
        // packed period is `frames x channels x 3`. At the 4-byte `S24_LE` stride
        // it would be 4/3 too long and ALSA would be handed a third of a period
        // more than the frame count claims; at a 2-byte stride it would be a
        // partial period every time.
        let packed = i24_packed_staging(SampleFormat::S24_3Le, 1024, 2);
        assert_eq!(packed.len(), 1024 * 2 * 3);
        assert!(packed.iter().all(|&b| b == 0));

        // Width is carried as data here too, exactly as it is for the i16 hop.
        assert_eq!(
            i24_packed_staging(SampleFormat::S24_3Le, 1024, 8).len(),
            1024 * 8 * 3
        );

        // And EVERY other edge width reserves nothing — a box that does not
        // declare S24_3LE must not pay mlocked bytes for this field.
        for format in [SampleFormat::S16Le, SampleFormat::S32Le] {
            let unused = i24_packed_staging(format, 1024, 8);
            assert!(
                unused.is_empty(),
                "{} needs no byte staging",
                format.as_str()
            );
            assert_eq!(unused.capacity(), 0, "{}", format.as_str());
        }
    }

    #[test]
    fn exactly_one_edge_staging_is_allocated_at_any_declared_width() {
        // The two edge stagings are mutually exclusive by construction, and that
        // is what makes a second buffer cheap rather than a doubling: at most one
        // of them is ever non-empty on a given box. Enumerated over the whole
        // vocabulary so a future width cannot quietly allocate both, or neither
        // while still converting.
        for (format, wants_staging) in [
            (SampleFormat::S16Le, true),
            (SampleFormat::S24_3Le, true),
            (SampleFormat::S32Le, false),
        ] {
            let i16_bytes = s16_staging(format, 1024, 2).len() * 2;
            let byte_bytes = i24_packed_staging(format, 1024, 2).len();
            assert!(
                i16_bytes == 0 || byte_bytes == 0,
                "{} allocates BOTH edge stagings",
                format.as_str()
            );
            assert_eq!(
                (i16_bytes + byte_bytes) > 0,
                wants_staging,
                "{} staging presence",
                format.as_str()
            );
        }
    }

    #[test]
    fn dac_format_readback_accepts_the_installed_format_it_requested() {
        verify_dac_format("dac", "outputd_dac", SampleFormat::S16Le, Format::S16LE).unwrap();
        verify_dac_format("dac", "outputd_dac", SampleFormat::S32Le, Format::S32LE).unwrap();
    }

    #[test]
    fn dac_format_readback_rejects_an_honestly_reported_mismatch() {
        // A device that installed something other than the request AND reported
        // it honestly. This is NOT the plug shape: a plug installs the client's
        // own request client-side, so a plug can never produce this
        // disagreement (see `configure_pcm`'s dac readback comment). Both
        // formats must be named — the operator needs to know which end moved.
        let err = verify_dac_format("dac", "outputd_dac", SampleFormat::S32Le, Format::S16LE)
            .unwrap_err();
        let text = err.to_string();
        assert!(text.contains("outputd_dac"), "{text}");
        assert!(text.contains("S16LE"), "{text}");
        assert!(text.contains("S32_LE"), "{text}");
    }

    #[test]
    fn dac_format_readback_rejects_an_edge_outside_the_vocabulary() {
        // The sentinel is `FloatLE`, not `S24LE`. `S24_3LE` is IN the
        // vocabulary now, and a reader meeting `S24LE` beside it would
        // reasonably wonder whether the test was asserting the accepted 24-bit
        // width is rejected. `FloatLE` is unambiguously outside — outputd's
        // vocabulary is integer-only — so the assertion says what it means.
        let err = verify_dac_format("dac", "outputd_dac", SampleFormat::S16Le, Format::FloatLE)
            .unwrap_err();
        assert!(err.to_string().contains("FloatLE"), "{err}");

        // The 4-byte 24-bit spelling is also still outside it, and THAT is now the
        // interesting case: it is one character from the accepted `S243LE`, so a
        // device installing it against an `S24_3LE` request has to be caught.
        let err = verify_dac_format("dac", "outputd_dac", SampleFormat::S24_3Le, Format::S24LE)
            .unwrap_err();
        let text = err.to_string();
        assert!(text.contains("S24LE"), "{text}");
        assert!(text.contains("S24_3LE"), "{text}");
    }

    #[test]
    fn dac_format_readback_accepts_a_packed_24_edge_that_installed_what_it_asked_for() {
        // The readback round-trips the new variant: `alsa_format` maps it to
        // `Format::S243LE`, and an honest device reporting that must pass. This is
        // the only format proof the packed write path has — `io_bytes()` does no
        // per-period verification, unlike the typed `io_i16`/`io_i32` handles — so
        // it has to hold for both the single DAC and both composite children.
        for role in ["dac", "dual_dac_a", "dual_dac_b"] {
            verify_dac_format(
                role,
                "hw:CARD=A,DEV=0",
                SampleFormat::S24_3Le,
                Format::S243LE,
            )
            .unwrap();
        }
    }

    #[test]
    fn dac_format_mismatch_parks_the_unit_instead_of_restart_looping() {
        // configure_pcm's dac call is wrapped in `final_sink_startup`, so a
        // readback mismatch must carry the configuration marker main() turns
        // into EX_CONFIG 78. A restart cannot change what the device installs.
        let error = final_sink_startup(verify_dac_format(
            "dac",
            "outputd_dac",
            SampleFormat::S32Le,
            Format::S16LE,
        ))
        .unwrap_err();

        assert!(error
            .downcast_ref::<FinalSinkStartupConfigError>()
            .is_some());
    }

    #[test]
    fn the_final_edge_readback_covers_the_single_dac_and_both_composite_children() {
        // The role set `configure_pcm`'s edge readback branches on. Before this
        // PR the branch was a bare `role == "dac"`, so BOTH composite children
        // opened with no format proof at all — they asked for S16 and believed
        // it. Every role that requests a declared HARDWARE width has to be in
        // here, and nothing else may be.
        for role in ["dac", "dual_dac_a", "dual_dac_b"] {
            assert!(is_final_edge_role(role), "{role} is a final hardware edge");
        }
        // The content roles have no lane to read back at all, `chip_ref` has an
        // exact whole-geometry check, and the fake backend opens nothing. None
        // of them may fall into the edge branch — nor may a name that merely
        // resembles a child role.
        for role in [
            "content",
            "active_content",
            "chip_ref",
            "dual_dac",
            "dual_dac_c",
            "",
        ] {
            assert!(!is_final_edge_role(role), "{role} is not a hardware edge");
        }
    }

    #[test]
    fn child_dac_format_readback_accepts_and_rejects_per_child_naming_the_role() {
        // Each child proves its own edge, and its message says WHICH child: a
        // composite whose A came up S16 and whose B came up S32 is a real
        // failure mode (two independently-negotiating USB devices), and an
        // operator cannot act on "a DAC installed the wrong format".
        //
        // The role assertion matches the SENTENCE POSITION, not the bare
        // substring — the same trap PR-3's content readback test recorded: the
        // child PCM alias could itself contain the role token, and
        // `text.contains(role)` would then be satisfied by the PCM name alone
        // and stay green with the role hardcoded to the wrong child.
        for (role, pcm) in [
            ("dual_dac_a", "hw:CARD=A,DEV=0"),
            ("dual_dac_b", "hw:CARD=B,DEV=0"),
        ] {
            verify_dac_format(role, pcm, SampleFormat::S16Le, Format::S16LE).unwrap();
            verify_dac_format(role, pcm, SampleFormat::S32Le, Format::S32LE).unwrap();

            let err = verify_dac_format(role, pcm, SampleFormat::S32Le, Format::S16LE).unwrap_err();
            let text = err.to_string();
            assert!(text.contains(&format!("outputd {role} PCM")), "{text}");
            assert!(text.contains(pcm), "{text}");
            assert!(text.contains("S16LE"), "{text}");
            assert!(text.contains("S32_LE"), "{text}");
        }
    }

    #[test]
    fn a_child_format_mismatch_parks_the_unit_instead_of_restart_looping() {
        // Both children's `configure_pcm` calls are already wrapped in
        // `final_sink_startup` (they were, for negotiation faults), so the new
        // readback inherits the park class: a dongle that installs a width other
        // than the one requested will install it again on the next start, and
        // this unit's restart loop escalates to StartLimitAction=reboot.
        for role in ["dual_dac_a", "dual_dac_b"] {
            let error = final_sink_startup(verify_dac_format(
                role,
                "hw:CARD=A,DEV=0",
                SampleFormat::S32Le,
                Format::S16LE,
            ))
            .unwrap_err();

            assert!(
                error
                    .downcast_ref::<FinalSinkStartupConfigError>()
                    .is_some(),
                "{role} mismatch must park, not restart-loop"
            );
        }
    }

    #[test]
    fn chip_ref_geometry_accepts_exact_native_contract() {
        let actual = PcmGeometry {
            sample_rate: 16_000,
            channels: 2,
            format: Format::S16LE,
            access: Access::RWInterleaved,
            period_frames: 128,
            buffer_frames: 256,
        };

        validate_chip_ref_geometry("hw:CARD=Array,DEV=0", actual).unwrap();
    }

    #[test]
    fn chip_ref_geometry_rejects_installed_buffer_mismatch() {
        let actual = PcmGeometry {
            sample_rate: 16_000,
            channels: 2,
            format: Format::S16LE,
            access: Access::RWInterleaved,
            period_frames: 128,
            buffer_frames: 512,
        };

        let err = validate_chip_ref_geometry("hw:CARD=Array,DEV=0", actual).unwrap_err();
        assert!(err.to_string().contains("buffer_frames: 512"));
        assert!(err.to_string().contains("buffer_frames: 256"));
    }

    /// The 4-channel program period every split fixture below feeds, so the two
    /// widths are compared on the SAME audio rather than on two hand-written
    /// vectors that could quietly differ.
    ///
    /// The INPUT is a program period (i32 spine), so it has to be written at that
    /// scale — bare `10` here is 10/65536 of an S16 LSB and narrows to silence.
    /// CI caught exactly that once: type inference accepted the unwidened
    /// literals, so the test compiled and asserted the wrong audio.
    fn split_fixture() -> [ProgramSample; 8] {
        [w(10), w(11), w(20), w(21), w(12), w(13), w(22), w(23)]
    }

    #[test]
    fn deinterleaves_active_4ch_to_one_stereo_period_per_s16_child() {
        // THE TRANSPARENCY PROOF for every live composite box: `S16Le` children
        // are what the registry declares today, so this arm must be exactly the
        // pre-PR-5 path. Same fixture, same expected samples as before the split
        // became width-dispatching.
        let mut a = vec![0i16; 4];
        let mut b = vec![0i16; 4];

        deinterleave_4ch_to_dual_stereo(&split_fixture(), &mut a, &mut b).unwrap();

        // Child A takes program channels 0/1, child B takes 2/3.
        assert_eq!(a, vec![10, 11, 12, 13]);
        assert_eq!(b, vec![20, 21, 22, 23]);
    }

    #[test]
    fn deinterleaves_active_4ch_to_one_stereo_period_per_s32_child() {
        // The dormant arm (nothing in the tree declares an S32 composite yet).
        // The channel map is identical — the SAME assertion as the S16 arm, at
        // the spine's own scale — because only the element width moved. If the
        // map ever diverged between arms, one child would play the other's audio
        // on a wide box and no S16 test would notice.
        let mut a = vec![0 as ProgramSample; 4];
        let mut b = vec![0 as ProgramSample; 4];

        deinterleave_4ch_to_dual_stereo(&split_fixture(), &mut a, &mut b).unwrap();

        assert_eq!(a, vec![w(10), w(11), w(12), w(13)]);
        assert_eq!(b, vec![w(20), w(21), w(22), w(23)]);
    }

    #[test]
    fn the_s16_child_edge_quantizes_by_rounding_and_saturates_at_the_rails() {
        // The composite's own single quantization. It must be the SAME
        // round-to-nearest the coherent sink's S16 edge uses, not a truncation —
        // a widened fixture cannot tell those apart, so this off-grid vector is
        // what pins it.
        let mut a = vec![0i16; 2];
        let mut b = vec![0i16; 2];

        deinterleave_4ch_to_dual_stereo(
            // ch0: half an S16 LSB up  -> rounds to 1
            // ch1: half an S16 LSB down -> rounds to 0 (halves go toward +inf)
            // ch2/ch3: both i32 rails   -> saturate, never wrap
            &[32_768, -32_768, ProgramSample::MAX, ProgramSample::MIN],
            &mut a,
            &mut b,
        )
        .unwrap();

        assert_eq!(a, vec![1, 0], "the child edge must round, not truncate");
        assert_eq!(
            b,
            vec![i16::MAX, i16::MIN],
            "and must saturate at the rails"
        );
    }

    /// **LOAD-BEARING — do not weaken.** This is the ONLY test in the tree that
    /// catches an `S32Le` child converting when it must copy — a 65536×
    /// resolution loss on the wide composite arm.
    ///
    /// Its on-grid sibling `deinterleaves_active_4ch_to_one_stereo_period_per_s32_child`
    /// CANNOT catch that class, and this is measured, not assumed: wiring
    /// `ChildEdgeSample for ProgramSample` to
    /// `widen_i16_to_i32(narrow_i32_to_i16_round(s))` leaves that test GREEN,
    /// because its fixture is built from widened S16 values and a widened S16
    /// value round-trips through narrow-then-widen exactly. Only an OFF-GRID
    /// vector — samples that are not multiples of 65536 — can tell a copy from a
    /// round trip.
    ///
    /// So the tempting cleanup is the dangerous one: consolidating the two S32
    /// tests and keeping the channel-map one would disarm the guard while
    /// leaving the suite green. If they are ever merged, the surviving test must
    /// carry THIS off-grid vector.
    #[test]
    fn an_s32_child_edge_converts_nothing_at_all() {
        // The payoff, and the one thing that could silently be wrong on a wide
        // composite: an `S32Le` child IS the spine's width, so the split must
        // COPY. Fed the exact vector the S16 arm above rounds and saturates,
        // every sample has to survive bit-for-bit — a stray narrow-then-widen
        // here would cost 16 bits per sample and read as "quieter, grainier" with
        // nothing in any counter to show for it.
        let off_grid = [32_768, -32_768, ProgramSample::MAX, ProgramSample::MIN];
        let mut a = vec![0 as ProgramSample; 2];
        let mut b = vec![0 as ProgramSample; 2];

        deinterleave_4ch_to_dual_stereo(&off_grid, &mut a, &mut b).unwrap();

        assert_eq!(a, vec![32_768, -32_768], "an S32 child must not round");
        assert_eq!(
            b,
            vec![ProgramSample::MAX, ProgramSample::MIN],
            "an S32 child must not clamp to the S16 rails"
        );
    }

    #[test]
    fn the_composite_edge_rejects_scratch_buffers_smaller_than_the_period() {
        // Unchanged contract, asserted at BOTH child widths: a short scratch
        // would otherwise write a partial period to a live DAC, and the bounds
        // check lives in the shared generic body — so if one width ever skipped
        // it, that would be a width-specific hole in a fail-closed guard.
        let mut a16 = vec![0i16; 2];
        let mut b16 = vec![0i16; 2];
        let mut a32 = vec![0 as ProgramSample; 2];
        let mut b32 = vec![0 as ProgramSample; 2];

        for err in [
            deinterleave_4ch_to_dual_stereo(&[w(1); 8], &mut a16, &mut b16).unwrap_err(),
            deinterleave_4ch_to_dual_stereo(&[w(1); 8], &mut a32, &mut b32).unwrap_err(),
        ] {
            assert!(
                err.to_string().contains("smaller than content period"),
                "{err}"
            );
        }

        for err in [
            deinterleave_4ch_to_dual_stereo(&[w(1); 7], &mut a16, &mut b16).unwrap_err(),
            deinterleave_4ch_to_dual_stereo(&[w(1); 7], &mut a32, &mut b32).unwrap_err(),
        ] {
            assert!(err.to_string().contains("whole 4-channel frames"), "{err}");
        }
    }

    #[test]
    fn child_periods_allocate_one_stereo_pair_at_the_declared_width() {
        // Sized once, at open, per child: `CHANNELS` (2) x period frames, NOT the
        // sink's 4-channel content width. A pair sized at 4 channels would be
        // twice the mlocked bytes it needs; a pair sized at 1 would let the split
        // hit its own bounds check every period.
        let narrow = ChildPeriods::new(SampleFormat::S16Le, 1024).unwrap();
        match &narrow {
            ChildPeriods::S16 { a, b } => {
                assert_eq!(a.len(), 2048);
                assert_eq!(b.len(), 2048);
                assert!(a.iter().chain(b.iter()).all(|&s| s == 0));
            }
            ChildPeriods::S32 { .. } => panic!("S16Le children must hold i16 buffers"),
        }

        let wide = ChildPeriods::new(SampleFormat::S32Le, 1024).unwrap();
        match &wide {
            ChildPeriods::S32 { a, b } => {
                assert_eq!(a.len(), 2048);
                assert_eq!(b.len(), 2048);
                assert!(a.iter().chain(b.iter()).all(|&s| s == 0));
            }
            ChildPeriods::S16 { .. } => panic!("S32Le children must hold i32 buffers"),
        }
    }

    #[test]
    fn child_periods_report_the_width_their_buffers_actually_are() {
        // `PairedCompositeSink::dac_format` — and through it `/state.dac.format`
        // and the chip-AEC alignment identity — reads this. It has to be derived
        // from the buffers rather than stored beside them: a second copy of the
        // width could disagree with what the write path writes, which is exactly
        // the lie the old hardcoded `SampleFormat::S16Le` in
        // `RuntimeAlsaSink::dac_format` was.
        for format in [SampleFormat::S16Le, SampleFormat::S32Le] {
            assert_eq!(ChildPeriods::new(format, 64).unwrap().format(), format);
        }
    }

    #[test]
    fn a_composite_child_edge_of_packed_24_is_refused_not_silently_narrowed() {
        // The composite has no packed-24 child write path, so this declaration
        // means the registry and this transport disagree. A CHILD PROFILE may
        // legitimately declare the packed edge for its own single-DAC case — the
        // Apple dongle does — because the emitted format is resolved from the
        // ARMED profile's id; what must never reach here is a COMPOSITE
        // declaring it. jaspercurry/JTS#2257 is the unlock.
        //
        // What a `_ => Self::S16 {..}` fallback would have done instead is the
        // whole reason this is fallible: `configure_pcm` reads the SAME
        // declaration, so both children would have been asked for `S24_3LE` and
        // passed their readbacks, and then the write path would have handed a
        // 3-byte wire i16 half-samples — full-scale garbage at two speakers, while
        // `/state` reported `S24_3LE` because `format()` reads the buffers.
        // `match` rather than `unwrap_err()`: `ChildPeriods` deliberately has no
        // `Debug` (it holds two whole period buffers), and adding one just so a
        // test could print them is the tail wagging the dog.
        let err = match ChildPeriods::new(SampleFormat::S24_3Le, 1024) {
            Ok(_) => panic!("a packed-24 child edge must be refused, not built"),
            Err(err) => err,
        };
        let text = err.to_string();
        assert!(text.contains("S24_3LE"), "{text}");
        assert!(text.contains("no packed-24 child write path"), "{text}");
        // The other two widths still build, so the refusal is not a blanket one.
        assert!(ChildPeriods::new(SampleFormat::S16Le, 1024).is_ok());
        assert!(ChildPeriods::new(SampleFormat::S32Le, 1024).is_ok());
    }

    #[test]
    fn a_packed_24_composite_declaration_parks_the_unit_instead_of_restart_looping() {
        // `PairedCompositeSink::new` wraps the construction in
        // `final_sink_startup`, so the refusal above has to carry the marker
        // `main()` turns into EX_CONFIG 78. A restart cannot change a registry
        // declaration, and this unit's restart loop escalates to
        // `StartLimitAction=reboot`.
        let error = match final_sink_startup(ChildPeriods::new(SampleFormat::S24_3Le, 1024)) {
            Ok(_) => panic!("a packed-24 child edge must be refused, not built"),
            Err(error) => error,
        };
        assert!(error
            .downcast_ref::<FinalSinkStartupConfigError>()
            .is_some());
    }

    // ---- #2255: the composite's bounded, linked-group xrun recovery ----

    #[test]
    fn prime_periods_leave_one_period_of_buffer_headroom() {
        assert_eq!(prime_periods(3072, 1024), 2);
        assert_eq!(prime_periods(4096, 1024), 3);
        assert_eq!(prime_periods(1024, 1024), 1);
        assert_eq!(prime_periods(0, 1024), 1);
        assert_eq!(prime_periods(3072, 0), 1);
    }

    #[test]
    fn the_prime_depth_always_stays_below_the_auto_start_threshold() {
        // The property the recovery model rests on, asserted as a property
        // rather than as three example rows: `manual_start` sets
        // `start_threshold = buffer_frames`, so a prime that reaches the buffer
        // AUTO-STARTS the linked group when the FIRST child fills — before the
        // second is primed. Every geometry with room for two periods must leave
        // headroom.
        for buffer in [256u32, 512, 1024, 2048, 3072, 4096, 8192] {
            for period in [64u32, 128, 256, 512, 1024] {
                if buffer < 2 * period {
                    // Degenerate geometry: one period is already the buffer, so
                    // there is no headroom to leave and the floor of 1 applies.
                    // outputd rejects these upstream; nothing to assert here.
                    continue;
                }
                let primed = prime_periods(buffer, period) * period;
                assert!(
                    primed < buffer,
                    "prime of {primed} frames reaches start_threshold {buffer} \
                     (buffer={buffer} period={period}) — the group would auto-start \
                     before both children are primed"
                );
            }
        }
    }

    #[test]
    fn the_recovery_budget_is_increment_then_check_and_allows_exactly_three() {
        // The table in `xrun_policy`'s doc IS the spec; this is that table.
        // `1` means "one recovery has already happened", so a `GiveUp` at 4 is
        // three recoveries followed by no fourth write.
        assert_eq!(xrun_policy(0), XrunAction::Continue);
        assert_eq!(xrun_policy(1), XrunAction::Continue);
        assert_eq!(xrun_policy(2), XrunAction::Continue);
        assert_eq!(xrun_policy(3), XrunAction::Continue);
        assert_eq!(xrun_policy(4), XrunAction::GiveUp);
        assert_eq!(xrun_policy(5), XrunAction::GiveUp);
        assert_eq!(xrun_policy(u32::MAX), XrunAction::GiveUp);
        // And the boundary is the shared constant, not a literal that could
        // drift away from it.
        assert_eq!(xrun_policy(MAX_RECOVERIES_PER_PERIOD), XrunAction::Continue);
        assert_eq!(
            xrun_policy(MAX_RECOVERIES_PER_PERIOD + 1),
            XrunAction::GiveUp
        );
    }

    #[test]
    fn the_divergence_check_measures_drift_from_the_baseline_not_the_raw_offset() {
        // A pair sitting at a large but STABLE offset is aligned, not diverged
        // — the baseline is the zero. This arithmetic had no test at all before
        // #2255 (only the `/state` wire shape was pinned).
        let check = delay_delta_check(500, 500, 48);
        assert_eq!(check.error_frames, 0);
        assert!(!check.diverged);

        // Sign-independent: the guard is a magnitude.
        assert_eq!(delay_delta_check(470, 500, 48).error_frames, 30);
        assert_eq!(delay_delta_check(530, 500, 48).error_frames, 30);
        assert!(!delay_delta_check(470, 500, 48).diverged);
        assert!(!delay_delta_check(530, 500, 48).diverged);

        // Negative baselines (child B ahead of child A) behave the same.
        assert_eq!(delay_delta_check(-500, -530, 48).error_frames, 30);
        assert!(!delay_delta_check(-500, -530, 48).diverged);
    }

    #[test]
    fn the_divergence_boundary_is_inclusive_at_the_tolerance() {
        // `error > max` bails, so error == max is accepted. Asserted because
        // the re-latch bound below uses the SAME constant and must agree with
        // it at the boundary — one tolerance, one meaning.
        assert!(!delay_delta_check(548, 500, 48).diverged);
        assert!(delay_delta_check(549, 500, 48).diverged);
        assert!(!delay_delta_check(452, 500, 48).diverged);
        assert!(delay_delta_check(451, 500, 48).diverged);
    }

    #[test]
    fn a_within_tolerance_reprime_relatches_the_baseline() {
        // The recovery reset both children's stream positions, so the offset
        // legitimately steps. Within tolerance the new offset becomes the zero
        // — without this the very next steady-state check would bail with
        // `delay_diverged` on the period after the xrun.
        assert_eq!(
            baseline_relatch_decision(Some(500), 500, 48),
            BaselineRelatch::Accept
        );
        assert_eq!(
            baseline_relatch_decision(Some(500), 540, 48),
            BaselineRelatch::Accept
        );
        assert_eq!(
            baseline_relatch_decision(Some(500), 460, 48),
            BaselineRelatch::Accept
        );
        // Inclusive at the tolerance, exactly as the steady-state guard is.
        assert_eq!(
            baseline_relatch_decision(Some(500), 548, 48),
            BaselineRelatch::Accept
        );
        assert_eq!(
            baseline_relatch_decision(Some(500), 452, 48),
            BaselineRelatch::Accept
        );
    }

    #[test]
    fn a_full_period_of_baked_in_skew_is_refused_not_relatched() {
        // THE case the magnitude bound exists for. At 48 kHz one 128-frame
        // period is 2.667 ms; on an active 2-way that offset IS the
        // woofer/tweeter time alignment, and a re-latch would bless it
        // permanently. 128 frames is ~2.7x the 48-frame default tolerance, so
        // the steady-state guard WOULD have caught it — the bound is what stops
        // the re-latch from laundering it first.
        assert_eq!(
            baseline_relatch_decision(Some(500), 500 + 128, DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES),
            BaselineRelatch::Refuse
        );
        assert_eq!(
            baseline_relatch_decision(Some(500), 500 - 128, DEFAULT_DUAL_MAX_DELAY_DELTA_FRAMES),
            BaselineRelatch::Refuse
        );
        // And the first frame past the tolerance, in both directions.
        assert_eq!(
            baseline_relatch_decision(Some(500), 549, 48),
            BaselineRelatch::Refuse
        );
        assert_eq!(
            baseline_relatch_decision(Some(500), 451, 48),
            BaselineRelatch::Refuse
        );
    }

    #[test]
    fn a_fault_before_the_first_latch_has_no_alignment_to_violate() {
        // No steady-state zero was ever latched, so there is nothing to launder
        // and nothing to compare against. Refusing here would fail closed on a
        // fact nobody knows. Accepts at any magnitude, deliberately.
        assert_eq!(
            baseline_relatch_decision(None, 0, 48),
            BaselineRelatch::Accept
        );
        assert_eq!(
            baseline_relatch_decision(None, 100_000, 48),
            BaselineRelatch::Accept
        );
    }

    #[test]
    fn a_zero_tolerance_pair_may_only_relatch_at_exactly_its_old_offset() {
        // `JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES=0` is a valid setting
        // (config only refuses negatives). It must mean "no step at all", not
        // "no bound".
        assert_eq!(
            baseline_relatch_decision(Some(7), 7, 0),
            BaselineRelatch::Accept
        );
        assert_eq!(
            baseline_relatch_decision(Some(7), 8, 0),
            BaselineRelatch::Refuse
        );
    }

    #[test]
    fn the_reprime_writes_silence_at_whichever_width_the_children_negotiated() {
        // The re-prime's payload comes from the children's OWN buffers, so it
        // cannot be the wrong width — and it is written in place, because the
        // re-prime runs on the SCHED_FIFO playout thread where an allocation is
        // a priority-inversion path.
        let mut s16 = ChildPeriods::new(SampleFormat::S16Le, 2).expect("s16 pair");
        s16.deinterleave(&[w(1), w(2), w(3), w(4), w(5), w(6), w(7), w(8)])
            .expect("split");
        match &s16 {
            ChildPeriods::S16 { a, b } => {
                assert_eq!(a.as_slice(), &[1, 2, 5, 6]);
                assert_eq!(b.as_slice(), &[3, 4, 7, 8]);
            }
            ChildPeriods::S32 { .. } => panic!("built S16, got S32"),
        }
        s16.fill_silence();
        match &s16 {
            ChildPeriods::S16 { a, b } => {
                assert!(a.iter().all(|s| *s == 0), "child A not silent: {a:?}");
                assert!(b.iter().all(|s| *s == 0), "child B not silent: {b:?}");
                // Silence FILLS the period; a shortened buffer would prime a
                // different depth than `prime_periods` computed.
                assert_eq!(a.len(), 4);
                assert_eq!(b.len(), 4);
            }
            ChildPeriods::S32 { .. } => panic!("built S16, got S32"),
        }

        let mut s32 = ChildPeriods::new(SampleFormat::S32Le, 2).expect("s32 pair");
        s32.deinterleave(&[w(1), w(2), w(3), w(4), w(5), w(6), w(7), w(8)])
            .expect("split");
        s32.fill_silence();
        match &s32 {
            ChildPeriods::S32 { a, b } => {
                assert!(a.iter().all(|s| *s == 0), "child A not silent: {a:?}");
                assert!(b.iter().all(|s| *s == 0), "child B not silent: {b:?}");
                assert_eq!(a.len(), 4);
                assert_eq!(b.len(), 4);
            }
            ChildPeriods::S16 { .. } => panic!("built S32, got S16"),
        }
    }
}
