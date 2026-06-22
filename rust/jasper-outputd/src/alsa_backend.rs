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
use crate::types::CHANNELS;

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

pub struct AlsaBackend {
    content: PCM,
    dac: PCM,
    pub content_pcm: String,
    pub dac_pcm: String,
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
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
    linked: bool,
    delay_delta_baseline: Option<i64>,
    last_delay_delta: Option<i64>,
    last_delay_delta_error: Option<i64>,
    max_delay_delta_frames: i64,
    period_a: Vec<i16>,
    period_b: Vec<i16>,
}

impl AlsaBackend {
    pub fn new(config: &Config) -> Result<Self> {
        let content =
            PCM::new(&config.content_pcm, Direction::Capture, true).with_context(|| {
                format!("opening outputd content capture PCM {}", config.content_pcm)
            })?;
        let content_negotiated = configure_pcm(PcmConfig {
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

        let dac = PCM::new(&config.dac_pcm, Direction::Playback, false)
            .with_context(|| format!("opening outputd DAC PCM {}", config.dac_pcm))?;
        let dac_negotiated = configure_pcm(PcmConfig {
            role: "dac",
            pcm_name: &config.dac_pcm,
            pcm: &dac,
            sample_rate: config.sample_rate,
            period_frames: config.period_frames,
            channels: config.content_channels,
            buffer_frames: config.dac_buffer_frames,
            manual_start: true,
        })
        .with_context(|| format!("configuring outputd DAC PCM {}", config.dac_pcm))?;

        eprintln!(
            "event=outputd.alsa.opened content_pcm={} dac_pcm={} channels={} sample_rate={} content_period_frames={} content_buffer_frames={} dac_period_frames={} dac_buffer_frames={}",
            config.content_pcm,
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
        let requested_frames = out.len() / (self.channels as usize);
        match self.read_content_available(out)? {
            ContentRead::Frames(frames) => {
                if frames < requested_frames {
                    let active = frames * (self.channels as usize);
                    out[active..].fill(0);
                }
                Ok(frames)
            }
            ContentRead::NoData | ContentRead::XrunRecovered => {
                out.fill(0);
                Ok(0)
            }
        }
    }

    pub fn read_content_available(&mut self, out: &mut [i16]) -> Result<ContentRead> {
        let requested_frames = out.len() / (self.channels as usize);
        let io = self
            .content
            .io_i16()
            .context("getting i16 IO handle for outputd content input")?;
        match io.readi(out) {
            Ok(frames) => {
                self.counters.content_frames_read += frames as u64;
                if frames == 0 {
                    self.counters.content_empty_period_count += 1;
                    Ok(ContentRead::NoData)
                } else if frames < requested_frames {
                    self.counters.content_partial_period_count += 1;
                    Ok(ContentRead::Frames(frames))
                } else {
                    Ok(ContentRead::Frames(frames))
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EAGAIN {
                    self.counters.content_eagain_count += 1;
                    self.counters.content_empty_period_count += 1;
                    Ok(ContentRead::NoData)
                } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    self.counters.content_xrun_count += 1;
                    self.counters.content_empty_period_count += 1;
                    eprintln!(
                        "event=outputd.xrun source=content pcm={} count={} errno={} frames_read={} empty_periods={} partial_periods={} eagain_count={} dac_frames_written={} period_frames={} buffer_frames={}",
                        self.content_pcm,
                        self.counters.content_xrun_count,
                        errno,
                        self.counters.content_frames_read,
                        self.counters.content_empty_period_count,
                        self.counters.content_partial_period_count,
                        self.counters.content_eagain_count,
                        self.counters.dac_frames_written,
                        self.content_negotiated.period_frames,
                        self.content_negotiated.buffer_frames,
                    );
                    self.content
                        .try_recover(e, true)
                        .context("recovering outputd content xrun")?;
                    Ok(ContentRead::XrunRecovered)
                } else {
                    Err(e).context(format!("reading outputd content PCM {}", self.content_pcm))
                }
            }
        }
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

        let dac_a = PCM::new(&dac_a_pcm, Direction::Playback, false)
            .with_context(|| format!("opening outputd dual DAC A PCM {}", dac_a_pcm))?;
        let dac_a_negotiated = configure_pcm(PcmConfig {
            role: "dual_dac_a",
            pcm_name: &dac_a_pcm,
            pcm: &dac_a,
            sample_rate: config.sample_rate,
            period_frames: config.period_frames,
            channels: CHANNELS,
            buffer_frames: config.dac_buffer_frames,
            manual_start: true,
        })
        .with_context(|| format!("configuring outputd dual DAC A PCM {}", dac_a_pcm))?;

        let dac_b = PCM::new(&dac_b_pcm, Direction::Playback, false)
            .with_context(|| format!("opening outputd dual DAC B PCM {}", dac_b_pcm))?;
        let dac_b_negotiated = configure_pcm(PcmConfig {
            role: "dual_dac_b",
            pcm_name: &dac_b_pcm,
            pcm: &dac_b,
            sample_rate: config.sample_rate,
            period_frames: config.period_frames,
            channels: CHANNELS,
            buffer_frames: config.dac_buffer_frames,
            manual_start: true,
        })
        .with_context(|| format!("configuring outputd dual DAC B PCM {}", dac_b_pcm))?;

        if dac_a_negotiated != dac_b_negotiated {
            anyhow::bail!(
                "dual Apple DACs negotiated different shapes: A={:?} B={:?}",
                dac_a_negotiated,
                dac_b_negotiated
            );
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
        let requested_frames = out.len() / 4;
        let io = self
            .content
            .io_i16()
            .context("getting i16 IO handle for outputd active content input")?;
        match io.readi(out) {
            Ok(frames) => {
                self.counters.content_frames_read += frames as u64;
                if frames == 0 {
                    self.counters.content_empty_period_count += 1;
                    out.fill(0);
                } else if frames < requested_frames {
                    self.counters.content_partial_period_count += 1;
                    out[(frames * 4)..].fill(0);
                }
                Ok(frames)
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EAGAIN {
                    self.counters.content_eagain_count += 1;
                    self.counters.content_empty_period_count += 1;
                    out.fill(0);
                    Ok(0)
                } else if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    self.counters.content_xrun_count += 1;
                    self.counters.content_empty_period_count += 1;
                    eprintln!(
                        "event=outputd.xrun source=active_content pcm={} count={} errno={}",
                        self.content_pcm, self.counters.content_xrun_count, errno
                    );
                    self.content
                        .try_recover(e, true)
                        .context("recovering outputd active content xrun")?;
                    out.fill(0);
                    Ok(0)
                } else {
                    Err(e).context(format!(
                        "reading outputd active content PCM {}",
                        self.content_pcm
                    ))
                }
            }
        }
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
    fn deinterleaves_active_4ch_to_one_stereo_period_per_dac() {
        let mut a = vec![0; 4];
        let mut b = vec![0; 4];

        deinterleave_4ch_to_dual_stereo(&[10, 11, 20, 21, 12, 13, 22, 23], &mut a, &mut b).unwrap();

        assert_eq!(a, vec![10, 11, 12, 13]);
        assert_eq!(b, vec![20, 21, 22, 23]);
    }
}
