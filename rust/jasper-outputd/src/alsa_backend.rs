//! ALSA transport for the outputd cutover.
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

pub struct AlsaBackend {
    content: PCM,
    dac: PCM,
    pub content_pcm: String,
    pub dac_pcm: String,
    pub content_negotiated: NegotiatedPcm,
    pub dac_negotiated: NegotiatedPcm,
    counters: IoCounters,
}

impl AlsaBackend {
    pub fn new(config: &Config) -> Result<Self> {
        let content =
            PCM::new(&config.content_pcm, Direction::Capture, true).with_context(|| {
                format!("opening outputd content capture PCM {}", config.content_pcm)
            })?;
        let content_negotiated = configure_pcm(
            "content",
            &config.content_pcm,
            &content,
            config.sample_rate,
            config.period_frames,
            config.content_buffer_frames,
        )
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
        let dac_negotiated = configure_pcm(
            "dac",
            &config.dac_pcm,
            &dac,
            config.sample_rate,
            config.period_frames,
            config.dac_buffer_frames,
        )
        .with_context(|| format!("configuring outputd DAC PCM {}", config.dac_pcm))?;

        eprintln!(
            "event=outputd.alsa.opened content_pcm={} dac_pcm={} sample_rate={} content_period_frames={} content_buffer_frames={} dac_period_frames={} dac_buffer_frames={}",
            config.content_pcm,
            config.dac_pcm,
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
        })
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
        let requested_frames = out.len() / (CHANNELS as usize);
        let io = self
            .content
            .io_i16()
            .context("getting i16 IO handle for outputd content input")?;
        match io.readi(out) {
            Ok(frames) => {
                self.counters.content_frames_read += frames as u64;
                if frames == 0 {
                    self.counters.content_empty_period_count += 1;
                    out.fill(0);
                } else if frames < requested_frames {
                    self.counters.content_partial_period_count += 1;
                    let active = frames * (CHANNELS as usize);
                    out[active..].fill(0);
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
                    out.fill(0);
                    Ok(0)
                } else {
                    Err(e).context(format!("reading outputd content PCM {}", self.content_pcm))
                }
            }
        }
    }

    pub fn write_dac_period(&mut self, samples: &[i16]) -> Result<()> {
        let frames_total = samples.len() / (CHANNELS as usize);
        let io = self
            .dac
            .io_i16()
            .context("getting i16 IO handle for outputd DAC")?;
        let mut frames_done = 0usize;
        let mut recoveries = 0u32;

        while frames_done < frames_total {
            let offset = frames_done * (CHANNELS as usize);
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

fn configure_pcm(
    role: &str,
    pcm_name: &str,
    pcm: &PCM,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
) -> Result<NegotiatedPcm> {
    let negotiated;
    {
        let hwp = HwParams::any(pcm).context("creating HwParams::any")?;
        hwp.set_channels(CHANNELS as u32)
            .with_context(|| format!("set_channels({})", CHANNELS))?;
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
}
