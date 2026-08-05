// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! ALSA transport for the outputd topology.
//!
//! The DAC playback stream is blocking and owns timing. Camilla's
//! post-DSP content lane is read nonblocking from snd-aloop; absent
//! content becomes silence. This keeps the final output loop alive
//! even when renderers are idle.

use alsa::pcm::{Access, Format, HwParams, State, PCM};
use alsa::{Direction, ValueOr};
use anyhow::{Context, Result};

use crate::config::Config;
use crate::types::{CHANNELS, SAMPLE_RATE};

const FORMAT: Format = Format::S16LE;
const MAX_RECOVERIES_PER_PERIOD: u32 = 3;

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
fn read_content_pcm<Read, Recover>(
    out: &mut [i16],
    spec: ContentPcmReadSpec<'_>,
    counters: &mut IoCounters,
    read: Read,
    recover: Recover,
) -> Result<ClassifiedContentRead>
where
    Read: FnOnce(&mut [i16]) -> alsa::Result<usize>,
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
fn zero_fill_content_period(out: &mut [i16], channels: usize, read: ContentRead) -> (usize, usize) {
    let requested_frames = out.len() / channels;
    let frames_read = match read {
        ContentRead::Frames(frames) => frames,
        ContentRead::NoData | ContentRead::XrunRecovered => 0,
    };
    let frames_short = requested_frames.saturating_sub(frames_read);
    if frames_short > 0 {
        out[(frames_read * channels)..].fill(0);
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
    period_a: Vec<i16>,
    period_b: Vec<i16>,
}

/// Marker for a final sink that could not be opened or negotiate the required
/// outputd geometry during initial startup. This is configuration-class: a
/// restart cannot change the PCM alias or its hardware capabilities.
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
                buffer_frames: config.dac_buffer_frames,
                manual_start: true,
            })
            .with_context(|| format!("configuring outputd DAC PCM {}", config.dac_pcm)),
        )?;

        eprintln!(
            "event=outputd.alsa.opened content_pcm={} content_source={} dac_pcm={} channels={} sample_rate={} content_period_frames={} content_buffer_frames={} dac_period_frames={} dac_buffer_frames={}",
            config.content_pcm,
            if config.content_bridge_mode == crate::config::ContentBridgeMode::ShmRing {
                "shm_ring"
            } else {
                "alsa"
            },
            config.dac_pcm,
            config.content_channels,
            dac_negotiated.sample_rate,
            content_negotiated.period_frames,
            content_negotiated.buffer_frames,
            dac_negotiated.period_frames,
            dac_negotiated.buffer_frames
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
        })
    }

    /// The runtime DAC/content channel width this backend reads and writes.
    pub fn channels(&self) -> u16 {
        self.channels
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

    pub fn read_content_period(&mut self, out: &mut [i16]) -> Result<usize> {
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

    pub fn read_content_available(&mut self, out: &mut [i16]) -> Result<ContentRead> {
        let content = self
            .content
            .as_ref()
            .context("outputd ALSA content PCM is disabled in local-pipe mode")?;
        let io = content
            .io_i16()
            .context("getting i16 IO handle for outputd content input")?;
        let classified = read_content_pcm(
            out,
            ContentPcmReadSpec {
                channels: self.channels as usize,
                pcm_name: &self.content_pcm,
                negotiated: self.content_negotiated,
                role: ContentPcmRole::Content,
            },
            &mut self.counters,
            |samples| io.readi(samples),
            |error| content.try_recover(error, true),
        )?;
        Ok(classified.read)
    }

    pub fn write_dac_period(&mut self, samples: &[i16]) -> Result<()> {
        let frames_total = samples.len() / (self.channels as usize);
        let io = self
            .dac
            .io_i16()
            .context("getting i16 IO handle for outputd DAC")?;
        let mut frames_done = 0usize;
        let mut recoveries = 0u32;

        while frames_done < frames_total {
            let offset = frames_done * (self.channels as usize);
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
                        self.counters.dac_xrun_count += 1;
                        let pending = frames_total - frames_done;
                        eprintln!(
                            "event=outputd.xrun source=dac pcm={} count={} frames_pending={}",
                            self.dac_pcm, self.counters.dac_xrun_count, pending
                        );
                        self.dac
                            .try_recover(e, true)
                            .context("recovering outputd DAC xrun")?;
                        recoveries += 1;
                        if recoveries > MAX_RECOVERIES_PER_PERIOD {
                            anyhow::bail!(
                                "outputd DAC xrun recovery exceeded {} attempts in one period",
                                MAX_RECOVERIES_PER_PERIOD
                            );
                        }
                    } else {
                        return Err(e).context(format!("writing outputd DAC PCM {}", self.dac_pcm));
                    }
                }
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

        eprintln!(
            "event=outputd.dual_apple.opened content_pcm={} dac_a_pcm={} dac_b_pcm={} sample_rate={} period_frames={} content_buffer_frames={} dac_buffer_frames={} linked={} max_delay_delta_frames={}",
            config.content_pcm,
            dac_a_pcm,
            dac_b_pcm,
            dac_a_negotiated.sample_rate,
            dac_a_negotiated.period_frames,
            content_negotiated.buffer_frames,
            dac_a_negotiated.buffer_frames,
            linked,
            config.dual_max_delay_delta_frames,
        );

        let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
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
            period_a: vec![0i16; period_samples],
            period_b: vec![0i16; period_samples],
        })
    }

    pub fn counters(&self) -> IoCounters {
        self.counters
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

    pub fn read_content_period(&mut self, out: &mut [i16]) -> Result<usize> {
        // `io` borrows `self.content` and has a Drop impl, so nothing inside
        // this scope may take `&mut self`. Decide the fill here, journal it
        // after the borrow ends.
        let classified = {
            let io = self
                .content
                .io_i16()
                .context("getting i16 IO handle for outputd active content input")?;
            read_content_pcm(
                out,
                ContentPcmReadSpec {
                    channels: 4,
                    pcm_name: &self.content_pcm,
                    negotiated: self.content_negotiated,
                    role: ContentPcmRole::ActiveContent,
                },
                &mut self.counters,
                |samples| io.readi(samples),
                |error| self.content.try_recover(error, true),
            )?
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

    pub fn write_dual_period(&mut self, samples_4ch: &[i16]) -> Result<()> {
        deinterleave_4ch_to_dual_stereo(samples_4ch, &mut self.period_a, &mut self.period_b)?;
        write_dac_fail_closed(
            &self.dac_a,
            &self.dac_a_pcm,
            &self.period_a,
            &mut self.counters.dac_xrun_count,
        )?;
        write_dac_fail_closed(
            &self.dac_b,
            &self.dac_b_pcm,
            &self.period_b,
            &mut self.counters.dac_xrun_count,
        )?;
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
        buffer_frames,
        manual_start,
    } = config;
    let negotiated;
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(channels as u32)
            .with_context(|| format!("set_channels({})", channels))?;
        hwp.set_rate(sample_rate, ValueOr::Nearest)
            .with_context(|| format!("set_rate({})", sample_rate))?;
        hwp.set_format(FORMAT)
            .with_context(|| format!("set_format({:?})", FORMAT))?;
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

fn deinterleave_4ch_to_dual_stereo(
    samples_4ch: &[i16],
    out_a: &mut [i16],
    out_b: &mut [i16],
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
        out_a[dst] = samples_4ch[src];
        out_a[dst + 1] = samples_4ch[src + 1];
        out_b[dst] = samples_4ch[src + 2];
        out_b[dst + 1] = samples_4ch[src + 3];
    }
    Ok(())
}

fn write_dac_fail_closed(
    pcm: &PCM,
    pcm_name: &str,
    samples: &[i16],
    xrun_count: &mut u64,
) -> Result<()> {
    let frames_total = samples.len() / (CHANNELS as usize);
    let io = pcm
        .io_i16()
        .with_context(|| format!("getting i16 IO handle for outputd DAC {pcm_name}"))?;
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

    #[test]
    fn deinterleaves_active_4ch_to_one_stereo_period_per_dac() {
        let mut a = vec![0; 4];
        let mut b = vec![0; 4];

        deinterleave_4ch_to_dual_stereo(&[10, 11, 20, 21, 12, 13, 22, 23], &mut a, &mut b).unwrap();

        assert_eq!(a, vec![10, 11, 12, 13]);
        assert_eq!(b, vec![20, 21, 22, 23]);
    }
}
