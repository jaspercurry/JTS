// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! ALSA transport for the outputd topology.
//!
//! The DAC playback stream is blocking and owns timing. Camilla's
//! post-DSP content lane is read nonblocking from snd-aloop; absent
//! content becomes silence. This keeps the final output loop alive
//! even when renderers are idle.

use alsa::pcm::{Access, Format, HwParams, State, IO, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};

use crate::config::Config;
use crate::types::{
    narrow_period, widen_period, ProgramSample, SampleFormat, CHANNELS, SAMPLE_RATE,
};

const MAX_RECOVERIES_PER_PERIOD: u32 = 3;

/// The single mapping from outputd's format vocabulary into ALSA's own.
///
/// Everything else in the crate speaks [`SampleFormat`]; this is the one place
/// that knows what ALSA calls it. `S16Le` maps to the same `Format::S16LE` the
/// retired module-level `FORMAT` const held, so an S16 edge asks ALSA for
/// exactly what it always asked for.
fn alsa_format(format: SampleFormat) -> Format {
    match format {
        SampleFormat::S16Le => Format::S16LE,
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

/// Journal cooldown for `event=outputd.content_fill`, measured in DAC frames.
///
/// `dac_frames_written` is the only monotonic clock this hot path already
/// has, and it advances at exactly the DAC rate — so a frame budget is a
/// wall-clock cooldown without a syscall. One second: outputd's core is fixed
/// at `SAMPLE_RATE` (config.rs bails on any other
/// `JASPER_OUTPUTD_SAMPLE_RATE`), so this derives from that one constant
/// rather than restating 48000.
const FILL_LOG_COOLDOWN_FRAMES: u64 = SAMPLE_RATE as u64;

/// Rate limiter for the content-fill journal line (issue #1768).
///
/// A short content read is zero-filled and still written to the DAC, which
/// INSERTS `requested - frames` samples into the emitted timeline — audible as
/// a brief tear, and it displaces everything downstream in time. Before this,
/// only the `EPIPE`/`ESTRPIPE` branch ever printed, so the partial-period and
/// `EAGAIN` paths — the ones that actually insert — were structurally silent:
/// their counters surfaced only as fields on a line some OTHER condition
/// happened to emit. That is how a corrupt measurement capture (#1765) and
/// audible sweep tears both went unattributed.
///
/// The first fill after a quiet second always prints; the rest are counted and
/// reported as `suppressed=` on the next line, so a pathological storm costs
/// at most one journal line per second and still cannot hide.
#[derive(Debug, Clone, Copy, Default)]
struct FillLogGate {
    next_log_frames: u64,
    suppressed: u64,
}

impl FillLogGate {
    /// `Some(suppressed_since_the_last_line)` when this fill should print.
    fn admit(&mut self, dac_frames_written: u64) -> Option<u64> {
        if dac_frames_written < self.next_log_frames {
            self.suppressed += 1;
            return None;
        }
        let suppressed = self.suppressed;
        self.suppressed = 0;
        self.next_log_frames = dac_frames_written.saturating_add(FILL_LOG_COOLDOWN_FRAMES);
        Some(suppressed)
    }
}

/// Journal a zero-fill that INSERTED `frames_short` samples into the emitted
/// timeline (issue #1768).
///
/// Free function, not a method: BOTH transports zero-fill on exactly the same
/// paths, and `kind="composite"` is explicitly open to further shapes — a
/// per-transport copy would be a twin that drifts. Takes the pieces it needs
/// so callers pass disjoint `&mut`/`&` field borrows.
fn log_content_fill(
    gate: &mut FillLogGate,
    counters: &IoCounters,
    content_pcm: &str,
    source: &str,
    frames_short: usize,
) {
    if frames_short == 0 {
        return;
    }
    let Some(suppressed) = gate.admit(counters.dac_frames_written) else {
        return;
    };
    eprintln!(
        "event=outputd.content_fill source={} pcm={} frames_short={} empty_periods={} \
partial_periods={} eagain_count={} xrun_count={} frames_read={} dac_frames_written={} \
suppressed={}",
        source,
        content_pcm,
        frames_short,
        counters.content_empty_period_count,
        counters.content_partial_period_count,
        counters.content_eagain_count,
        counters.content_xrun_count,
        counters.content_frames_read,
        counters.dac_frames_written,
        suppressed,
    );
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
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentRead {
    Frames(usize),
    NoData,
    XrunRecovered,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ContentPcmRole {
    Content,
    ActiveContent,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ClassifiedContentRead {
    read: ContentRead,
    fill_source: &'static str,
}

#[derive(Debug, Clone, Copy)]
struct ContentPcmReadSpec<'a> {
    channels: usize,
    pcm_name: &'a str,
    negotiated: NegotiatedPcm,
    role: ContentPcmRole,
}

/// Read and classify one nonblocking content-capture period.
///
/// Both the coherent-DAC and paired-composite transports consume the same
/// content PCM contract. Keep the errno mapping, counters, and xrun recovery
/// here so a new read outcome cannot drift between the two sinks. The caller
/// still owns transport-specific zero-fill journaling.
/// Generic over the SAMPLE TYPE because the content lane's width is now
/// declared config: an `S16Le` lane reads through an `io_i16()` handle into an
/// i16 staging buffer, an `S32Le` lane reads through `io_i32()` straight into the
/// program period. Everything this function owns — the errno mapping, the
/// counters, the xrun recovery, the journal line — is identical either way and
/// must stay identical, which is exactly what one generic body guarantees.
fn read_content_pcm<S, Read, Recover>(
    out: &mut [S],
    spec: ContentPcmReadSpec<'_>,
    counters: &mut IoCounters,
    read: Read,
    recover: Recover,
) -> Result<ClassifiedContentRead>
where
    S: Copy,
    Read: FnOnce(&mut [S]) -> alsa::Result<usize>,
    Recover: FnOnce(alsa::Error) -> alsa::Result<()>,
{
    let requested_frames = out.len() / spec.channels;
    match read(out) {
        Ok(frames) => {
            counters.content_frames_read += frames as u64;
            if frames == 0 {
                counters.content_empty_period_count += 1;
                Ok(ClassifiedContentRead {
                    read: ContentRead::NoData,
                    fill_source: "empty",
                })
            } else {
                if frames < requested_frames {
                    counters.content_partial_period_count += 1;
                }
                Ok(ClassifiedContentRead {
                    read: ContentRead::Frames(frames),
                    fill_source: "partial",
                })
            }
        }
        Err(error) => {
            let errno = error.errno();
            if errno == libc::EAGAIN {
                counters.content_eagain_count += 1;
                counters.content_empty_period_count += 1;
                return Ok(ClassifiedContentRead {
                    read: ContentRead::NoData,
                    fill_source: "eagain",
                });
            }
            if errno != libc::EPIPE && errno != libc::ESTRPIPE {
                let context = match spec.role {
                    ContentPcmRole::Content => {
                        format!("reading outputd content PCM {}", spec.pcm_name)
                    }
                    ContentPcmRole::ActiveContent => {
                        format!("reading outputd active content PCM {}", spec.pcm_name)
                    }
                };
                return Err(error).context(context);
            }

            counters.content_xrun_count += 1;
            counters.content_empty_period_count += 1;
            match spec.role {
                ContentPcmRole::Content => eprintln!(
                    "event=outputd.xrun source=content pcm={} count={} errno={} frames_read={} empty_periods={} partial_periods={} eagain_count={} dac_frames_written={} period_frames={} buffer_frames={}",
                    spec.pcm_name,
                    counters.content_xrun_count,
                    errno,
                    counters.content_frames_read,
                    counters.content_empty_period_count,
                    counters.content_partial_period_count,
                    counters.content_eagain_count,
                    counters.dac_frames_written,
                    spec.negotiated.period_frames,
                    spec.negotiated.buffer_frames,
                ),
                ContentPcmRole::ActiveContent => eprintln!(
                    "event=outputd.xrun source=active_content pcm={} count={} errno={}",
                    spec.pcm_name, counters.content_xrun_count, errno
                ),
            }
            let recovery_context = match spec.role {
                ContentPcmRole::Content => "recovering outputd content xrun",
                ContentPcmRole::ActiveContent => "recovering outputd active content xrun",
            };
            recover(error).context(recovery_context)?;
            Ok(ClassifiedContentRead {
                read: ContentRead::XrunRecovered,
                fill_source: "xrun_recovered",
            })
        }
    }
}

/// Apply the content-lane silence contract and return `(frames_read,
/// frames_short)` for the transport-specific fill journal line.
///
/// Generic over the sample type for the same reason as `read_content_pcm`: it
/// zero-fills whichever buffer the lane's declared width read into. Digital
/// silence is `Default::default()` at every width — and it is worth naming that
/// the widening of zero is zero, so a zero-filled tail sounds identical on the
/// spine as it did at i16.
fn zero_fill_content_period<S: Copy + Default>(
    out: &mut [S],
    channels: usize,
    read: ContentRead,
) -> (usize, usize) {
    let requested_frames = out.len() / channels;
    let frames_read = match read {
        ContentRead::Frames(frames) => frames,
        ContentRead::NoData | ContentRead::XrunRecovered => 0,
    };
    let frames_short = requested_frames.saturating_sub(frames_read);
    if frames_short > 0 {
        out[(frames_read * channels)..].fill(S::default());
    }
    (frames_read, frames_short)
}

pub struct AlsaBackend {
    content: Option<PCM>,
    dac: PCM,
    pub content_pcm: String,
    pub dac_pcm: String,
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
    fill_log: FillLogGate,
    /// Runtime DAC/content width carried as data — a coherent single DAC reads
    /// and writes this many channels end-to-end. `2` is byte-identical to the
    /// previous compile-time `CHANNELS`; the reconciler emits wider values
    /// (DAC8x = 8) via `JASPER_OUTPUTD_ACTIVE_CHANNELS`. The reference/chip-ref
    /// width stays `CHANNELS=2` (the published reference is always stereo).
    channels: u16,
    /// The format the CONTENT capture PCM negotiated — requested from
    /// `Config::content_format` and checked against the installed `hw_params`
    /// by `configure_pcm`'s content readback. `None` means no ALSA content PCM
    /// was opened at all, which is the (PROTOTYPE) SHM-ring source: there is no
    /// lane to have negotiated anything, so `/state` keeps reporting the
    /// declaration rather than claiming a readback that never ran.
    ///
    /// Read the `None` case honestly: under the SHM-ring source the declared
    /// value STATUS reports can disagree with the ring's real width, because
    /// nothing here reads or verifies the ring (the ring wire is S16 by
    /// contract, and `jasper_ring::Geometry::validate_self` hard-rejects
    /// anything else). What WILL keep the pair coherent is upstream, not here:
    /// wide-output-path PR-1 (#2226) adds a coupling-reconciler gate that refuses
    /// to arm `shm_ring` when the emitted lane format is wider than the ring
    /// wire. Until that lands, NOTHING refuses the pairing —
    /// `default_ring_gates()` has no format axis and `RING_WIRE_FORMAT` is never
    /// compared against anything — and the unattended auto path can arm the ring
    /// on an eligible stereo box by itself, so reaching the disagreement takes
    /// one hand-set `JASPER_OUTPUTD_CONTENT_FORMAT` rather than a fully
    /// hand-built lab config. `content.source` sits immediately before
    /// `content.format` in STATUS so a reader can always see which source the
    /// format describes.
    content_format: Option<SampleFormat>,
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
    /// takes the program spine straight through with no staging at all.
    ///
    /// This is the inverse of what stood here before the i32 spine: the staging
    /// used to be an `S32Le` edge's WIDENING buffer, because the program was i16
    /// and the wide edge was the exception. Now the program is wide and the S16
    /// edge is the one that converts — the same "only the mismatched edge pays"
    /// rule, with the mismatch on the other side.
    dac_narrow_buf: Vec<i16>,
    /// Reused i16 staging for an `S16Le` CONTENT lane — allocated once, at open.
    /// Empty (zero bytes) on an `S32Le` lane, and empty under the SHM-ring source
    /// (no ALSA content PCM is opened at all, so nothing ever reads through here).
    ///
    /// The ingest twin of `dac_narrow_buf`. alsa-rs's IO handles are typed, so an
    /// S16 lane physically cannot read into an i32 slice; it reads here and
    /// `widen_period` lifts the period onto the spine. `read_content_available`
    /// borrows it as a disjoint field alongside `content` and `counters` — never
    /// via `mem::take`, so no early return on that path can drop it and leave the
    /// next period to re-allocate.
    content_widen_buf: Vec<i16>,
}

/// Paired-composite transport: two clock-independent child DACs driven as one
/// 4-channel sink (the dual-Apple shape). Renamed from `DualAppleBackend` — the
/// transport dispatches on the composite SHAPE, not the DAC's identity. Stays
/// exactly two children (a pairwise drift guard cannot be half-vectorized).
pub struct PairedCompositeSink {
    content: PCM,
    dac_a: PCM,
    dac_b: PCM,
    pub content_pcm: String,
    pub dac_a_pcm: String,
    pub dac_b_pcm: String,
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
    fill_log: FillLogGate,
    linked: bool,
    delay_delta_baseline: Option<i64>,
    last_delay_delta: Option<i64>,
    last_delay_delta_error: Option<i64>,
    max_delay_delta_frames: i64,
    /// The two child period buffers, at the children's declared edge width —
    /// and the ONE place that width is represented at runtime. See
    /// [`ChildPeriods`] for why the width is the variant rather than a separate
    /// field beside two typed buffer pairs.
    periods: ChildPeriods,
    /// The format the ACTIVE content capture PCM negotiated — same contract as
    /// [`AlsaBackend::content_format`], never `Option` here because the
    /// composite sink always opens its content lane (the SHM-ring source is
    /// single-ALSA only).
    content_format: SampleFormat,
    /// Reused i16 ingest staging for an `S16Le` active content lane. Same
    /// contract as [`AlsaBackend::content_widen_buf`]; empty on an `S32Le` lane.
    content_widen_buf: Vec<i16>,
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
/// Both variants are allocated once, at open, and reused; the audio path only
/// ever writes into them. Unlike the coherent sink's `dac_narrow_buf` — EMPTY on
/// the edge that converts nothing — a composite always needs a scratch pair
/// whatever the width, because the 4-channel program period has to be SPLIT into
/// two stereo periods before either child can be written. Only the element type
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
    fn new(format: SampleFormat, period_frames: u32) -> Self {
        let samples = (period_frames as usize) * (CHANNELS as usize);
        match format {
            SampleFormat::S16Le => Self::S16 {
                a: vec![0i16; samples],
                b: vec![0i16; samples],
            },
            SampleFormat::S32Le => Self::S32 {
                a: vec![0 as ProgramSample; samples],
                b: vec![0 as ProgramSample; samples],
            },
        }
    }

    /// The children's edge width, read off the buffers themselves.
    fn format(&self) -> SampleFormat {
        match self {
            Self::S16 { .. } => SampleFormat::S16Le,
            Self::S32 { .. } => SampleFormat::S32Le,
        }
    }
}

/// Marker for a startup fault that a restart cannot repair, attached via
/// `anyhow::Context` and downcast by `main` into the EX_CONFIG 78 park.
///
/// Named for its first user — a final sink that could not be opened or
/// negotiate outputd's geometry, where a restart cannot change the PCM alias or
/// its hardware capabilities. The content lane's format readback carries it for
/// the same reason: a device that installs a format other than the one outputd
/// requested will install exactly that format again on the next start, so the
/// unit must park where an operator can see it instead of restart-looping into
/// `StartLimitAction=reboot`. It is deliberately NOT attached to ordinary
/// content-lane open failures, which ARE transient — those mean CamillaDSP has
/// not yet opened its half of the snd-aloop pair, and restarting is how outputd
/// waits that out.
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

impl AlsaBackend {
    pub fn new(config: &Config) -> Result<Self> {
        // The content ALSA PCM is NOT opened when a non-ALSA content source
        // owns the program: (PROTOTYPE) the SHM ring. It feeds `content_buf`
        // directly in the run loop; opening snd-aloop here would leave an unread
        // capture lane. A synthetic NegotiatedPcm stands in for the /state
        // contract, with `buffer_frames = period_frames`: there is no ALSA capture
        // ring here, so this is a period-sized stand-in, NOT a real jitter buffer.
        // The honest buffering depth of the non-ALSA source is reported separately
        // in /state so no consumer is misled: the SHM ring's TRUE capacity
        // (n_slots x period) rides `content.ring.capacity_frames` (see
        // OutputdState::snapshot_json). jasper-doctor validates that instead of
        // applying the ALSA ">= 2x period" floor to this synthetic.
        let skip_content_pcm =
            config.content_bridge_mode == crate::config::ContentBridgeMode::ShmRing;
        let (content, content_negotiated) = if skip_content_pcm {
            (
                None,
                NegotiatedPcm {
                    sample_rate: config.sample_rate,
                    period_frames: config.period_frames,
                    buffer_frames: config.period_frames,
                },
            )
        } else {
            let content =
                PCM::new(&config.content_pcm, Direction::Capture, true).with_context(|| {
                    format!("opening outputd content capture PCM {}", config.content_pcm)
                })?;
            let negotiated = configure_pcm(PcmConfig {
                role: "content",
                pcm_name: &config.content_pcm,
                pcm: &content,
                sample_rate: config.sample_rate,
                period_frames: config.period_frames,
                channels: config.content_channels,
                // Camilla's post-DSP loopback lane — its own declared hop, NOT
                // the hardware edge and not outputd's internal program width.
                // Defaults to S16_LE (every box today); `configure_pcm`'s
                // content readback proves what the lane installed.
                format: config.content_format,
                buffer_frames: config.content_buffer_frames,
                manual_start: false,
            })
            .with_context(|| {
                format!(
                    "configuring outputd content capture PCM {}",
                    config.content_pcm
                )
            })?;
            content
                .start()
                .with_context(|| format!("starting capture PCM {}", config.content_pcm))?;
            (Some(content), negotiated)
        };

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
        // only references are prose — docs/AEC-DIAG-01-baseline.md,
        // docs/CHIP-AEC-EXPERIMENT.md, scripts/ring-proto/README.md). It stays
        // by convention, because operators and journal-grep recipes read it and
        // a stable key costs nothing. The content lane rides the
        // explicitly-named `content_format=` beside it, so one line names both
        // hops and a half-flipped box is visible at open rather than only in
        // STATUS.
        eprintln!(
            "event=outputd.alsa.opened content_pcm={} content_source={} content_format={} dac_pcm={} channels={} sample_rate={} content_period_frames={} content_buffer_frames={} dac_period_frames={} dac_buffer_frames={} format={}",
            config.content_pcm,
            if config.content_bridge_mode == crate::config::ContentBridgeMode::ShmRing {
                "shm_ring"
            } else {
                "alsa"
            },
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
            content,
            dac,
            content_pcm: config.content_pcm.clone(),
            dac_pcm: config.dac_pcm.clone(),
            content_negotiated,
            dac_negotiated,
            counters: IoCounters::default(),
            fill_log: FillLogGate::default(),
            channels: config.content_channels,
            // `Some` only when an ALSA content lane was actually opened and its
            // readback passed. Under the SHM-ring source `skip_content_pcm`
            // short-circuits the open, so there is nothing to report.
            content_format: if skip_content_pcm {
                None
            } else {
                Some(config.content_format)
            },
            // `configure_pcm`'s dac readback checked the installed client-side
            // format against this requested one, so storing the request stores
            // what outputd is running — it never reached here otherwise.
            dac_format: config.declared_dac_format,
            dac_narrow_buf: s16_staging(
                config.declared_dac_format,
                config.period_frames,
                config.content_channels,
            ),
            // No ALSA content lane opened ⇒ no ingest staging, whatever the
            // declaration says. The SHM-ring source feeds `content_buf` directly
            // and the ring wire is S16 by contract (D5), so its widening happens
            // in the run loop, not here.
            content_widen_buf: if skip_content_pcm {
                Vec::new()
            } else {
                s16_staging(
                    config.content_format,
                    config.period_frames,
                    config.content_channels,
                )
            },
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

    /// The format the content lane negotiated, or `None` when no ALSA content
    /// PCM was opened (the SHM-ring source). See the field's doc.
    pub fn content_format(&self) -> Option<SampleFormat> {
        self.content_format
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

    pub fn read_content_period(&mut self, out: &mut [ProgramSample]) -> Result<usize> {
        let read = self.read_content_available(out)?;
        // Stay EXHAUSTIVE over `ContentRead` — a catch-all here would let a
        // future variant silently inherit the "empty" label on the audio path
        // instead of failing the build. `XrunRecovered` is labelled distinctly
        // because it already printed its own `outputd.xrun` line; the pair
        // should read as one event, not two unexplained ones.
        let source = match read {
            ContentRead::Frames(_) => "partial",
            ContentRead::NoData => "empty",
            ContentRead::XrunRecovered => "xrun_recovered",
        };
        let (frames, frames_short) = zero_fill_content_period(out, self.channels as usize, read);
        log_content_fill(
            &mut self.fill_log,
            &self.counters,
            &self.content_pcm,
            source,
            frames_short,
        );
        Ok(frames)
    }

    /// Read one nonblocking content period onto the PROGRAM SPINE, converting
    /// from the lane's declared width as it reads.
    ///
    /// This is the wide path's S16 INGRESS, and it retired the temporary
    /// i16-only-ingest guard `configure_pcm`'s content branch used to run (now
    /// deleted, along with its tests). Before this, ingest fetched an `io_i16()`
    /// handle unconditionally, so a wide declaration opened cleanly and then
    /// failed on the FIRST period read — deep on the audio path, which on this
    /// unit escalates to `StartLimitAction=reboot`; the guard refused the
    /// declaration at startup instead. Every value the config parser accepts is
    /// now a value ingest can actually read, so there is nothing left to refuse.
    ///
    /// The `S16Le` arm widens through the pre-sized `content_widen_buf`; the
    /// `S32Le` arm reads the lane's native samples straight into `out` with no
    /// conversion at all.
    pub fn read_content_available(&mut self, out: &mut [ProgramSample]) -> Result<ContentRead> {
        // Refuse the no-ALSA-lane case FIRST, before anything touches the staging
        // buffer. Under the SHM-ring source `content` is `None` and `content_format`
        // is `None` with it; the old i16-only ingest reached this same refusal on
        // its first line, and keeping it first is what stops the S16 arm below from
        // resizing the staging and then bailing — which would allocate and drop a
        // whole period per call, on the audio path.
        let content = self
            .content
            .as_ref()
            .context("outputd ALSA content PCM is disabled in local-pipe mode")?;
        let spec = ContentPcmReadSpec {
            channels: self.channels as usize,
            pcm_name: &self.content_pcm,
            negotiated: self.content_negotiated,
            role: ContentPcmRole::Content,
        };
        // `content_format` is `Some` here by construction: `new` sets it to `None`
        // only on the path that leaves `content` `None` too, and the `?` above
        // already returned in that case. Refuse rather than default, because the
        // wrong arm is worse than no arm: silently choosing S16 on a lane that is
        // actually S32 hands `io_i16()` a buffer of half-samples and the speaker
        // plays loud garbage. There is no width to guess.
        //
        // `final_sink_startup` is load-bearing on this line, not decoration. A
        // bare bail here would be ordinary-error class — exit 1, which on this
        // unit means restart, and eventually `StartLimitAction=reboot`. That is
        // the exact chain the retired i16-only ingest guard existed to prevent, so
        // refusing a format outputd cannot resolve must PARK at EX_CONFIG 78,
        // where an operator sees it, rather than reboot-loop against a condition
        // no restart can change.
        //
        // Park-class is right even though this is a PER-PERIOD call, not a startup
        // one, and that is the whole justification for borrowing a marker named
        // for startup: the condition is a static property of the struct (set once
        // in `new`, never mutated), so it cannot be transient. If it were ever
        // true it would be true on every period and on every restart alike —
        // precisely the case the marker exists to park rather than retry. The
        // transient content-lane failures on this path stay unmarked, as before.
        // A `&'static str` context, not a `format!`-ed one naming the PCM. Two
        // reasons: it matches the idiom of every other error this function
        // produces (the io-handle contexts are static too), and it keeps the
        // period-hot body free of allocating tokens —
        // `test_outputd_period_hot_functions_do_not_allocate` forbids `format!(`
        // here, and although a `with_context` closure is lazy and would never run
        // per period, a guard that has to reason about laziness is a guard that
        // gets argued with. The PCM name is already on the open-time
        // `event=outputd.alsa.opened` line and in STATUS.
        let lane_format = final_sink_startup(self.content_format.context(
            "outputd content PCM is open but its negotiated format is unknown; \
             refusing to guess the ingest width",
        ))?;
        match lane_format {
            SampleFormat::S32Le => {
                let io = content
                    .io_i32()
                    .context("getting i32 IO handle for outputd content input")?;
                let classified = read_content_pcm(
                    out,
                    spec,
                    &mut self.counters,
                    |samples| io.readi(samples),
                    |error| content.try_recover(error, true),
                )?;
                Ok(classified.read)
            }
            SampleFormat::S16Le => {
                if self.content_widen_buf.len() != out.len() {
                    // Steady state never resizes: `new` sized this for the run
                    // loop's period. A geometry that ever differs reallocates
                    // once and then stays put.
                    self.content_widen_buf.resize(out.len(), 0);
                }
                let io = content
                    .io_i16()
                    .context("getting i16 IO handle for outputd content input")?;
                // Disjoint field borrows: `io`/`try_recover` hold `self.content`,
                // the reader writes `self.content_widen_buf`, the counters are a
                // third field. No `mem::take` dance, so no path can drop the
                // staging buffer on the way out.
                let classified = read_content_pcm(
                    &mut self.content_widen_buf,
                    spec,
                    &mut self.counters,
                    |samples| io.readi(samples),
                    |error| content.try_recover(error, true),
                )?;
                widen_period(&self.content_widen_buf, out)?;
                Ok(classified.read)
            }
        }
    }

    /// Write one already-mixed PROGRAM period to the final edge — **the single
    /// quantization on outputd's output path.**
    ///
    /// The period arriving here IS the reference the caller publishes to the
    /// chip/software AEC (inv-A: reference == final DAC content). `samples` is
    /// never mutated, so the published reference and what the DAC receives are the
    /// same audio; the taps narrow their own copy to S16 (D8).
    ///
    /// An `S32Le` edge takes the spine STRAIGHT THROUGH — this is the payoff of
    /// the wide spine, and the reason the widening staging that used to live here
    /// is gone: there is nothing left to convert. An `S16Le` edge narrows once,
    /// round-to-nearest, into `dac_narrow_buf`.
    pub fn write_dac_period(&mut self, samples: &[ProgramSample]) -> Result<()> {
        let channels = self.channels as usize;
        let frames_total = samples.len() / channels;
        match self.dac_format {
            // DEFAULT PATH — every S16_LE edge, which is every commissioned
            // box today. The one place on the output path where resolution is
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

        let content =
            PCM::new(&config.content_pcm, Direction::Capture, true).with_context(|| {
                format!(
                    "opening outputd active content capture PCM {}",
                    config.content_pcm
                )
            })?;
        let content_negotiated = configure_pcm(PcmConfig {
            role: "active_content",
            pcm_name: &config.content_pcm,
            pcm: &content,
            sample_rate: config.sample_rate,
            period_frames: config.period_frames,
            channels: config.content_channels,
            // The active lane's own declared hop — same axis the single-ALSA
            // content role reads, and independent of what the composite's
            // children run at their edges (those take the DECLARED edge format
            // below, from the other declaration).
            format: config.content_format,
            buffer_frames: config.content_buffer_frames,
            manual_start: false,
        })
        .with_context(|| {
            format!(
                "configuring outputd active content capture PCM {}",
                config.content_pcm
            )
        })?;
        content
            .start()
            .with_context(|| format!("starting capture PCM {}", config.content_pcm))?;

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
                // installed it. A future composite of unlike children needs a
                // per-child declaration in the registry first — the pair's
                // negotiated-shape equality check below would catch the
                // mismatch, but it would catch it as a startup park, which is
                // the wrong place to discover a registry gap.
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
            Err(err) if config.dual_require_link => {
                anyhow::bail!(
                    "dual Apple snd_pcm_link failed and JASPER_OUTPUTD_DUAL_REQUIRE_LINK=1: {}",
                    err
                );
            }
            Err(err) => {
                eprintln!("event=outputd.dual_apple.link status=failed detail={err}");
            }
        }

        // `content_format=` and `format=` spelled exactly as the single sink's
        // `event=outputd.alsa.opened` line spells them, so one grep recipe reads
        // both hops on either transport and a half-flipped box is visible at
        // open rather than only in STATUS. `format=` is the CHILDREN's edge (both
        // children, one declaration); it is bare for the same reason it is bare
        // over there — operators and journal recipes read that key.
        eprintln!(
            "event=outputd.dual_apple.opened content_pcm={} content_format={} dac_a_pcm={} dac_b_pcm={} sample_rate={} period_frames={} content_buffer_frames={} dac_buffer_frames={} linked={} max_delay_delta_frames={} format={}",
            config.content_pcm,
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
            content,
            dac_a,
            dac_b,
            content_pcm: config.content_pcm.clone(),
            dac_a_pcm,
            dac_b_pcm,
            content_negotiated,
            dac_negotiated: dac_a_negotiated,
            counters: IoCounters::default(),
            fill_log: FillLogGate::default(),
            linked,
            delay_delta_baseline: None,
            last_delay_delta: None,
            last_delay_delta_error: None,
            max_delay_delta_frames: config.dual_max_delay_delta_frames,
            // `configure_pcm`'s final-edge readback checked the installed
            // client-side format on BOTH children against this requested one, so
            // building the buffers at the request builds them at what outputd is
            // running — neither child reached here otherwise.
            periods: ChildPeriods::new(config.declared_dac_format, config.period_frames),
            content_format: config.content_format,
            // The active content lane is 4-channel, so its ingest staging is
            // wider than the per-child period buffers above.
            content_widen_buf: s16_staging(
                config.content_format,
                config.period_frames,
                config.content_channels,
            ),
        })
    }

    pub fn counters(&self) -> IoCounters {
        self.counters
    }

    /// The format the active content lane negotiated (readback-checked at open
    /// by `configure_pcm`'s content readback).
    pub fn content_format(&self) -> SampleFormat {
        self.content_format
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

    pub fn read_content_period(&mut self, out: &mut [ProgramSample]) -> Result<usize> {
        // Same declared-width ingest as the single-ALSA lane; see
        // `AlsaBackend::read_content_available` for why the S16 arm stages.
        // `io` borrows `self.content` and has a Drop impl, so nothing inside
        // those scopes may take `&mut self`. Decide the fill here, journal it
        // after the borrow ends.
        let spec = ContentPcmReadSpec {
            channels: 4,
            pcm_name: &self.content_pcm,
            negotiated: self.content_negotiated,
            role: ContentPcmRole::ActiveContent,
        };
        let classified = match self.content_format {
            SampleFormat::S32Le => {
                let io = self
                    .content
                    .io_i32()
                    .context("getting i32 IO handle for outputd active content input")?;
                read_content_pcm(
                    out,
                    spec,
                    &mut self.counters,
                    |samples| io.readi(samples),
                    |error| self.content.try_recover(error, true),
                )?
            }
            SampleFormat::S16Le => {
                if self.content_widen_buf.len() != out.len() {
                    self.content_widen_buf.resize(out.len(), 0);
                }
                let io = self
                    .content
                    .io_i16()
                    .context("getting i16 IO handle for outputd active content input")?;
                // Disjoint field borrows, same as the single-ALSA arm — no
                // `mem::take`, so no early return can drop the staging buffer.
                let classified = read_content_pcm(
                    &mut self.content_widen_buf,
                    spec,
                    &mut self.counters,
                    |samples| io.readi(samples),
                    |error| self.content.try_recover(error, true),
                )?;
                widen_period(&self.content_widen_buf, out)?;
                classified
            }
        };
        let (frames, frames_short) = zero_fill_content_period(out, 4, classified.read);
        log_content_fill(
            &mut self.fill_log,
            &self.counters,
            &self.content_pcm,
            classified.fill_source,
            frames_short,
        );
        Ok(frames)
    }

    /// Split one 4-channel program period to the two children and write both —
    /// **the composite's single quantization, at the children's edge.**
    ///
    /// The width arm is chosen by [`ChildPeriods`], which is the children's
    /// negotiated width: an `S16Le` pair narrows once per sample during the
    /// split (round-to-nearest, the same primitive the coherent sink's `S16Le`
    /// edge uses), an `S32Le` pair carries the spine's own samples and converts
    /// nothing. Every buffer either arm touches was sized at open.
    ///
    /// The `.context(...)` strings are `&'static str` rather than `format!`-ed
    /// PCM names, exactly as `AlsaBackend::write_dac_period`'s io-handle context
    /// is: this body is period-hot and
    /// `test_outputd_period_hot_functions_do_not_allocate` covers it. Both
    /// children are named literally so the message still says which one failed;
    /// the PCM aliases are on the open-time `event=outputd.dual_apple.opened`
    /// line and in STATUS.
    pub fn write_dual_period(&mut self, samples_4ch: &[ProgramSample]) -> Result<()> {
        match &mut self.periods {
            ChildPeriods::S16 { a, b } => {
                deinterleave_4ch_to_dual_stereo(samples_4ch, a, b)?;
                let io_a = self
                    .dac_a
                    .io_i16()
                    .context("getting i16 IO handle for outputd dual Apple DAC A")?;
                write_dac_fail_closed(
                    &io_a,
                    &self.dac_a_pcm,
                    a,
                    &mut self.counters.dac_xrun_count,
                )?;
                let io_b = self
                    .dac_b
                    .io_i16()
                    .context("getting i16 IO handle for outputd dual Apple DAC B")?;
                write_dac_fail_closed(
                    &io_b,
                    &self.dac_b_pcm,
                    b,
                    &mut self.counters.dac_xrun_count,
                )?;
            }
            ChildPeriods::S32 { a, b } => {
                deinterleave_4ch_to_dual_stereo(samples_4ch, a, b)?;
                let io_a = self
                    .dac_a
                    .io_i32()
                    .context("getting i32 IO handle for outputd dual Apple DAC A")?;
                write_dac_fail_closed(
                    &io_a,
                    &self.dac_a_pcm,
                    a,
                    &mut self.counters.dac_xrun_count,
                )?;
                let io_b = self
                    .dac_b
                    .io_i32()
                    .context("getting i32 IO handle for outputd dual Apple DAC B")?;
                write_dac_fail_closed(
                    &io_b,
                    &self.dac_b_pcm,
                    b,
                    &mut self.counters.dac_xrun_count,
                )?;
            }
        }
        self.counters.dac_frames_written += (samples_4ch.len() / 4) as u64;
        if self.dac_a.state() == State::Running && self.dac_b.state() == State::Running {
            self.check_delay_delta()?;
        }
        Ok(())
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
        let error = (delta - baseline).abs();
        self.last_delay_delta = Some(delta);
        self.last_delay_delta_error = Some(error);
        if error > self.max_delay_delta_frames {
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
        // hardware edge too, and until this PR neither child was checked at all
        // — both simply asked for S16 and believed it.
        let current = pcm
            .hw_params_current()
            .context("reading installed outputd DAC HwParams")?;
        let installed = current
            .get_format()
            .context("get installed outputd DAC format")?;
        verify_dac_format(role, pcm_name, format, installed)?;
    }
    if role == "content" || role == "active_content" {
        // Prove what outputd's own client edge ended up at, exactly as the dac
        // role does — and read the scope of THIS claim just as honestly, because
        // the content lane's plumbing differs per build:
        //
        // The ACTIVE lane (`outputd_active_content_*`) is a raw `type hw`
        // snd-aloop pair, so the client edge IS the lane: a format the lane
        // cannot serve fails at `hw_params` install above, and this readback is
        // a genuine second proof of what got installed.
        //
        // The PASSIVE lane (`outputd_content_*`) is `type plug` over a slave
        // that pins `format S16_LE` today, so this readback proves the CLIENT
        // EDGE ONLY: a plug installs the client's own request client-side and
        // converts on the slave side, so it agrees BY CONSTRUCTION and cannot
        // see the slave's width. Re-pinning those slaves is a later step of the
        // wide-output-path program; the slave-edge proof is the asound wiring
        // test over there, not this Rust check.
        //
        // ONE `final_sink_startup` covers the WHOLE readback — the hw_params and
        // format reads as well as the comparison. Marking only the comparison
        // would leave a failed `hw_params_current()` exiting 1 into the very
        // restart-loop-to-reboot class the guard above exists to prevent.
        final_sink_startup(read_back_content_format(
            || {
                let current = pcm
                    .hw_params_current()
                    .with_context(|| format!("reading installed outputd {role} HwParams"))?;
                current
                    .get_format()
                    .with_context(|| format!("get installed outputd {role} format"))
            },
            role,
            pcm_name,
            format,
        ))?;
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

/// Fail closed unless the format ALSA installed on the CONTENT lane's client
/// edge is exactly the one requested.
///
/// The content sibling of `verify_dac_format`, and the scope of what it proves
/// depends on the lane — raw `hw` on the active lane (a real second proof),
/// client-edge-only through the passive lane's `plug`. `configure_pcm`'s content
/// readback comment carries the full argument; the failure is handled the same
/// way (parked at EX_CONFIG 78, never restart-looped) because a device that
/// substituted a format once will substitute it again.
fn verify_content_format(
    role: &str,
    pcm_name: &str,
    requested: SampleFormat,
    installed: Format,
) -> Result<()> {
    verify_installed_format(role, pcm_name, requested, installed)
}

/// The CONTENT lane's whole readback: read the installed format, then compare.
///
/// One function so ONE `final_sink_startup` at the call site marks EVERY error
/// the readback can produce, not just the comparison. The `dac` role gets that
/// for free — its caller wraps the entire `configure_pcm` call — but the content
/// call sites deliberately do NOT wrap the whole call, because an ordinary
/// content-lane OPEN failure is transient (CamillaDSP has not opened its half of
/// the snd-aloop pair yet) and restart-looping is how outputd waits that out.
/// Without this single mark point, a failed `hw_params_current()` would exit 1
/// into that restart loop instead of parking.
///
/// `read_installed` is injected rather than read here so the failure path is
/// testable without a live PCM — the same shape as `read_content_pcm`'s injected
/// reader/recover closures.
fn read_back_content_format(
    read_installed: impl FnOnce() -> Result<Format>,
    role: &str,
    pcm_name: &str,
    requested: SampleFormat,
) -> Result<()> {
    let installed = read_installed()?;
    verify_content_format(role, pcm_name, requested, installed)
}

/// The one request-vs-installed comparison, shared by both readbacks so the two
/// lanes can never drift into disagreeing about what a mismatch is or how it
/// reads. Names BOTH formats: the operator has to know which end moved.
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
/// The program spine is i32, so only an `S16Le` hop needs a staging buffer at
/// all: an `S16Le` DAC edge narrows into one before writing, and an `S16Le`
/// content lane reads into one before widening. An `S32Le` hop moves the spine's
/// own samples and holds an EMPTY vec — zero bytes.
///
/// One function for both because the shape is identical (period × channels of
/// i16) and because a second copy would be a twin that drifts. It is called
/// once per hop, at open, never on the audio path.
fn s16_staging(format: SampleFormat, period_frames: u32, channels: u16) -> Vec<i16> {
    match format {
        SampleFormat::S16Le => vec![0i16; (period_frames as usize) * (channels as usize)],
        SampleFormat::S32Le => Vec::new(),
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

/// The coherent single DAC's period write loop, over whatever sample type the
/// negotiated edge takes.
///
/// One loop, not one per format: the xrun accounting, the bounded recovery
/// budget, and the `event=outputd.xrun` line are the audio path's fail-closed
/// behaviour and must not be able to drift between an S16 and an S32 edge.
/// Monomorphised for `i16` this is instruction-for-instruction the pre-native-
/// format writer.
fn write_dac_frames<S: Copy>(
    io: &IO<'_, S>,
    pcm: &PCM,
    pcm_name: &str,
    samples: &[S],
    channels: usize,
    xrun_count: &mut u64,
) -> Result<()> {
    let frames_total = samples.len() / channels;
    let mut frames_done = 0usize;
    let mut recoveries = 0u32;

    while frames_done < frames_total {
        let offset = frames_done * channels;
        match io.writei(&samples[offset..]) {
            Ok(n) => {
                frames_done += n;
                if n == 0 {
                    recoveries += 1;
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
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
                    if recoveries > MAX_RECOVERIES_PER_PERIOD {
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

/// One composite CHILD's period write, over whatever sample type its negotiated
/// edge takes.
///
/// Generic for the same reason `write_dac_frames` is: the abort-on-xrun policy
/// that makes this "fail closed" — a composite must not paper over a child's
/// xrun, because a recovered child would silently be one buffer out of phase
/// with its sibling and the pair's whole promise is that they stay aligned —
/// must not be able to drift between an S16 and an S32 child. Monomorphised for
/// `i16` this is instruction-for-instruction the pre-wide-composite writer.
///
/// The caller fetches the IO handle (`io_i16`/`io_i32` per its `ChildPeriods`
/// arm) rather than this function taking the `PCM` and choosing, which is what
/// makes the width the caller's single decision — and lets the caller use
/// `&'static str` contexts on the period-hot path.
fn write_dac_fail_closed<S: Copy>(
    io: &IO<'_, S>,
    pcm_name: &str,
    samples: &[S],
    xrun_count: &mut u64,
) -> Result<()> {
    let frames_total = samples.len() / (CHANNELS as usize);
    let mut frames_done = 0usize;
    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&samples[offset..]) {
            Ok(0) => {
                anyhow::bail!("outputd dual Apple DAC {pcm_name} writei returned 0 frames");
            }
            Ok(n) => frames_done += n,
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    *xrun_count += 1;
                    anyhow::bail!(
                        "outputd dual Apple DAC {pcm_name} aborted on xrun/suspend errno={errno}"
                    );
                }
                return Err(e).context(format!("writing outputd dual Apple DAC {pcm_name}"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

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

    fn test_negotiated_pcm() -> NegotiatedPcm {
        NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 4,
            buffer_frames: 8,
        }
    }

    fn test_content_read_spec(role: ContentPcmRole) -> ContentPcmReadSpec<'static> {
        ContentPcmReadSpec {
            channels: 2,
            pcm_name: "test_content",
            negotiated: test_negotiated_pcm(),
            role,
        }
    }

    #[test]
    fn content_pcm_read_counts_full_success_without_recovery() {
        let mut out = [1i16; 8];
        let mut counters = IoCounters::default();
        let classified = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::Content),
            &mut counters,
            |_| Ok(4),
            |_| -> alsa::Result<()> { panic!("full read must not recover") },
        )
        .unwrap();

        assert_eq!(classified.read, ContentRead::Frames(4));
        assert_eq!(counters.content_frames_read, 4);
        assert_eq!(counters.content_partial_period_count, 0);
        assert_eq!(counters.content_empty_period_count, 0);
        let (frames, frames_short) = zero_fill_content_period(&mut out, 2, classified.read);
        assert_eq!((frames, frames_short), (4, 0));
        assert_eq!(out, [1; 8]);
    }

    #[test]
    fn content_pcm_read_counts_partial_and_zero_fills_unread_frames() {
        let mut out = [1, 2, 3, 4, 9, 9, 9, 9];
        let mut counters = IoCounters::default();
        let classified = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::ActiveContent),
            &mut counters,
            |_| Ok(2),
            |_| -> alsa::Result<()> { panic!("partial read must not recover") },
        )
        .unwrap();

        assert_eq!(classified.read, ContentRead::Frames(2));
        assert_eq!(classified.fill_source, "partial");
        assert_eq!(counters.content_frames_read, 2);
        assert_eq!(counters.content_partial_period_count, 1);
        let (frames, frames_short) = zero_fill_content_period(&mut out, 2, classified.read);
        assert_eq!((frames, frames_short), (2, 2));
        assert_eq!(out, [1, 2, 3, 4, 0, 0, 0, 0]);
    }

    #[test]
    fn content_pcm_read_counts_empty_and_zero_fills_the_period() {
        let mut out = [7i16; 8];
        let mut counters = IoCounters::default();
        let classified = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::Content),
            &mut counters,
            |_| Ok(0),
            |_| -> alsa::Result<()> { panic!("empty read must not recover") },
        )
        .unwrap();

        assert_eq!(classified.read, ContentRead::NoData);
        assert_eq!(classified.fill_source, "empty");
        assert_eq!(counters.content_frames_read, 0);
        assert_eq!(counters.content_empty_period_count, 1);
        let (frames, frames_short) = zero_fill_content_period(&mut out, 2, classified.read);
        assert_eq!((frames, frames_short), (0, 4));
        assert_eq!(out, [0; 8]);
    }

    #[test]
    fn content_pcm_read_classifies_eagain_without_recovery() {
        let mut out = [7i16; 8];
        let mut counters = IoCounters::default();
        let classified = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::ActiveContent),
            &mut counters,
            |_| Err(alsa::Error::new("readi", libc::EAGAIN)),
            |_| -> alsa::Result<()> { panic!("EAGAIN must not recover") },
        )
        .unwrap();

        assert_eq!(classified.read, ContentRead::NoData);
        assert_eq!(classified.fill_source, "eagain");
        assert_eq!(counters.content_eagain_count, 1);
        assert_eq!(counters.content_empty_period_count, 1);
        let (frames, frames_short) = zero_fill_content_period(&mut out, 2, classified.read);
        assert_eq!((frames, frames_short), (0, 4));
        assert_eq!(out, [0; 8]);
    }

    #[test]
    fn content_pcm_read_recovers_epipe_and_returns_xrun_semantics() {
        let mut out = [7i16; 8];
        let mut counters = IoCounters::default();
        let recovered_errno = Cell::new(None);
        let classified = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::Content),
            &mut counters,
            |_| Err(alsa::Error::new("readi", libc::EPIPE)),
            |error| {
                recovered_errno.set(Some(error.errno()));
                Ok(())
            },
        )
        .unwrap();

        assert_eq!(classified.read, ContentRead::XrunRecovered);
        assert_eq!(classified.fill_source, "xrun_recovered");
        assert_eq!(recovered_errno.get(), Some(libc::EPIPE));
        assert_eq!(counters.content_xrun_count, 1);
        assert_eq!(counters.content_empty_period_count, 1);
        let (frames, frames_short) = zero_fill_content_period(&mut out, 2, classified.read);
        assert_eq!((frames, frames_short), (0, 4));
        assert_eq!(out, [0; 8]);
    }

    #[test]
    fn content_pcm_read_propagates_other_errno_without_touching_counters() {
        let mut out = [7i16; 8];
        let mut counters = IoCounters::default();
        let error = read_content_pcm(
            &mut out,
            test_content_read_spec(ContentPcmRole::Content),
            &mut counters,
            |_| Err(alsa::Error::new("readi", libc::EBADF)),
            |_| -> alsa::Result<()> { panic!("fatal read must not recover") },
        )
        .unwrap_err();

        assert!(error
            .to_string()
            .contains("reading outputd content PCM test_content"));
        assert_eq!(counters, IoCounters::default());
    }

    #[test]
    fn fill_log_gate_prints_the_first_fill_immediately() {
        // A fill must never wait for a cooldown to be seen — the whole defect
        // in #1768 was that the inserting paths printed nothing at all.
        let mut gate = FillLogGate::default();
        assert_eq!(gate.admit(0), Some(0));
    }

    #[test]
    fn fill_log_gate_suppresses_within_the_cooldown_and_reports_the_count() {
        let mut gate = FillLogGate::default();
        assert_eq!(gate.admit(0), Some(0));
        // Same second: suppressed, but counted.
        assert_eq!(gate.admit(1), None);
        assert_eq!(gate.admit(FILL_LOG_COOLDOWN_FRAMES - 1), None);
        // Cooldown elapsed — prints, and surfaces what it swallowed so a storm
        // is bounded to one line per second WITHOUT being able to hide.
        assert_eq!(gate.admit(FILL_LOG_COOLDOWN_FRAMES), Some(2));
        // The suppressed tally resets after it is reported.
        assert_eq!(gate.admit(2 * FILL_LOG_COOLDOWN_FRAMES), Some(0));
    }

    #[test]
    fn fill_log_gate_paces_by_dac_frames_not_call_count() {
        // The gate's clock is `dac_frames_written`, so a quiet run of fills
        // spread across real time each print, however few calls there were.
        let mut gate = FillLogGate::default();
        let mut frames = 0u64;
        for _ in 0..5 {
            assert_eq!(gate.admit(frames), Some(0));
            frames += FILL_LOG_COOLDOWN_FRAMES;
        }
    }

    #[test]
    fn zero_short_fill_is_a_no_op_that_does_not_spend_gate_budget() {
        // The shared logger is called from four sites; a degenerate
        // `frames_short == 0` must neither print nor consume the one-per-second
        // budget a REAL fill needs.
        let mut gate = FillLogGate::default();
        let counters = IoCounters::default();
        log_content_fill(&mut gate, &counters, "pcm", "partial", 0);
        assert_eq!(gate.admit(0), Some(0));
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
    }

    #[test]
    fn only_the_s16_hop_stages_and_it_stages_one_whole_period() {
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
        let err = verify_dac_format("dac", "outputd_dac", SampleFormat::S16Le, Format::S24LE)
            .unwrap_err();
        assert!(err.to_string().contains("S24LE"), "{err}");
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
        // The content lanes have their OWN readback (`verify_content_format`,
        // whose park scope and plug caveats differ), `chip_ref` has an exact
        // whole-geometry check, and the fake backend opens nothing. None of them
        // may fall into the edge branch — nor may a name that merely resembles a
        // child role.
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
    fn content_format_readback_accepts_the_installed_format_it_requested() {
        // Both content roles, both vocabulary values. The passive lane's `plug`
        // makes this agree by construction today (see `configure_pcm`'s content
        // readback comment); the active lane's raw `hw` makes it a real proof.
        verify_content_format(
            "content",
            "outputd_content_capture",
            SampleFormat::S16Le,
            Format::S16LE,
        )
        .unwrap();
        verify_content_format(
            "active_content",
            "outputd_active_content_capture",
            SampleFormat::S32Le,
            Format::S32LE,
        )
        .unwrap();
    }

    #[test]
    fn content_format_readback_rejects_an_honestly_reported_mismatch() {
        // The lane installed something other than the request and reported it.
        // Both formats and the ROLE must be named: with two content roles and
        // two hops in play, "which end moved" is only answerable if the message
        // says which lane it was.
        //
        // The role assertion must match the SENTENCE POSITION, not the bare
        // substring: `outputd_active_content_capture` contains both "content"
        // and "active_content", so `text.contains(role)` was satisfied by the
        // PCM name alone and stayed green even with the role hardcoded to the
        // wrong lane. `"outputd <role> PCM"` can only come from the message's
        // own role slot.
        let err = verify_content_format(
            "active_content",
            "outputd_active_content_capture",
            SampleFormat::S32Le,
            Format::S16LE,
        )
        .unwrap_err();
        let text = err.to_string();
        assert!(text.contains("outputd active_content PCM"), "{text}");
        assert!(text.contains("outputd_active_content_capture"), "{text}");
        assert!(text.contains("S16LE"), "{text}");
        assert!(text.contains("S32_LE"), "{text}");
    }

    #[test]
    fn content_format_readback_rejects_an_edge_outside_the_vocabulary() {
        let err = verify_content_format(
            "content",
            "outputd_content_capture",
            SampleFormat::S16Le,
            Format::S24LE,
        )
        .unwrap_err();
        assert!(err.to_string().contains("S24LE"), "{err}");
    }

    #[test]
    fn the_content_readback_compares_the_format_it_read_not_the_one_requested() {
        // Wiring guard against a self-satisfying readback: the comparison must
        // consume what the READ returned. If it compared the request against
        // itself it would agree always — green, and blind.
        read_back_content_format(
            || Ok(Format::S32LE),
            "content",
            "outputd_content_capture",
            SampleFormat::S32Le,
        )
        .unwrap();

        let err = read_back_content_format(
            || Ok(Format::S16LE),
            "content",
            "outputd_content_capture",
            SampleFormat::S32Le,
        )
        .unwrap_err();
        assert!(err.to_string().contains("S16LE"), "{err}");
        assert!(err.to_string().contains("S32_LE"), "{err}");
    }

    #[test]
    fn content_format_mismatch_parks_the_unit_instead_of_restart_looping() {
        // `configure_pcm` wraps the content readback in `final_sink_startup`, so
        // a mismatch must carry the marker main() turns into EX_CONFIG 78 — a
        // lane that substituted a format once will substitute it again, and this
        // unit's restart loop escalates to StartLimitAction=reboot.
        let error = final_sink_startup(verify_content_format(
            "content",
            "outputd_content_capture",
            SampleFormat::S32Le,
            Format::S16LE,
        ))
        .unwrap_err();

        assert!(error
            .downcast_ref::<FinalSinkStartupConfigError>()
            .is_some());
    }

    #[test]
    fn a_content_park_survives_the_callers_own_context_layer() {
        // The dac marker is attached OUTERMOST (`final_sink_startup(configure_pcm
        // (..).with_context(..))`); the content markers are attached INSIDE
        // `configure_pcm` and the caller then wraps them in its own
        // `.with_context("configuring outputd content capture PCM ...")`. So the
        // park only works if `main`'s downcast walks the whole context chain
        // rather than inspecting the outermost layer — pin that, because the
        // alternative is a mismatch that silently restart-loops instead of
        // parking. Mirrors both call sites' wrapping exactly.
        //
        // Second element covers the readback's INFRASTRUCTURE error — a failed
        // `hw_params_current()` / `get_format()`. That path reaches the marker
        // only because `read_back_content_format` owns the reads, so one
        // `final_sink_startup` covers them; marking the comparison alone would
        // let it exit 1 into the restart loop.
        //
        // There were THREE elements while a temporary i16-only ingest guard also
        // rode this call site. The wide ingest deleted that guard, and its
        // element with it — the deleted guard's own checklist named this test
        // precisely because it is not named for the guard.
        for inner in [
            final_sink_startup(verify_content_format(
                "content",
                "outputd_content_capture",
                SampleFormat::S32Le,
                Format::S16LE,
            )),
            final_sink_startup(read_back_content_format(
                || anyhow::bail!("reading installed outputd content HwParams failed"),
                "content",
                "outputd_content_capture",
                SampleFormat::S16Le,
            )),
        ] {
            let wrapped = inner
                .with_context(|| {
                    "configuring outputd content capture PCM outputd_content_capture".to_string()
                })
                .unwrap_err();

            assert!(
                wrapped
                    .downcast_ref::<FinalSinkStartupConfigError>()
                    .is_some(),
                "marker lost under the caller's context: {wrapped:#}"
            );
        }
    }

    #[test]
    fn both_readbacks_report_a_mismatch_in_one_shape() {
        // One comparison behind both entry points: the dac and content lanes
        // cannot drift into disagreeing about what a mismatch is or how it
        // reads. Same requested/installed pair, same message but for the role.
        let dac = verify_dac_format("dac", "some_pcm", SampleFormat::S32Le, Format::S16LE)
            .unwrap_err()
            .to_string();
        let content =
            verify_content_format("content", "some_pcm", SampleFormat::S32Le, Format::S16LE)
                .unwrap_err()
                .to_string();

        assert_eq!(
            dac.replacen("dac", "content", 1),
            content,
            "{dac} vs {content}"
        );
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
        let narrow = ChildPeriods::new(SampleFormat::S16Le, 1024);
        match &narrow {
            ChildPeriods::S16 { a, b } => {
                assert_eq!(a.len(), 2048);
                assert_eq!(b.len(), 2048);
                assert!(a.iter().chain(b.iter()).all(|&s| s == 0));
            }
            ChildPeriods::S32 { .. } => panic!("S16Le children must hold i16 buffers"),
        }

        let wide = ChildPeriods::new(SampleFormat::S32Le, 1024);
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
            assert_eq!(ChildPeriods::new(format, 64).format(), format);
        }
    }
}
