// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The chip-reference leg of the reference taps: the playout thread's
//! [`ChipRefDownsampler`] and the writer thread [`spawn_chip_ref_writer`]
//! starts, which owns the chip's reference PCM. A missing or failing device
//! degrades this leg (background retry, STATUS counters), never the DAC path.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use alsa::pcm::{State, PCM};
use anyhow::{Context, Result};

use crate::alsa_backend::open_playback_pcm;
use crate::state::{ChipRefWrite, OutputdState};
use crate::types::i16_bytes;
use crate::CHANNELS;

const REF_OUTPUT_QUEUE_CAPACITY: usize = 32;
const CHIP_REF_RETRY_INITIAL: Duration = Duration::from_secs(1);
const CHIP_REF_RETRY_MAX: Duration = Duration::from_secs(30);
const CHIP_REF_WORKER_POLL: Duration = Duration::from_millis(200);
/// Minimum spacing between chip-ref degraded (`reason=write_failed`) log lines.
/// A sustained open-OK -> write-fail flap would otherwise emit one WARN per
/// failed period — issue #1245 observed 4382 lines in ~4 days, the single most
/// frequent event on the box. The first failure of an episode logs
/// immediately; subsequent failures within this window are suppressed and
/// counted, then folded (as `suppressed=N`) into the next line.
const CHIP_REF_DEGRADED_LOG_INTERVAL: Duration = Duration::from_secs(300);

#[derive(Clone, Copy)]
struct ChipRefWorkerTiming {
    retry_initial: Duration,
    retry_max: Duration,
    poll: Duration,
    degraded_log_interval: Duration,
}

const CHIP_REF_WORKER_TIMING: ChipRefWorkerTiming = ChipRefWorkerTiming {
    retry_initial: CHIP_REF_RETRY_INITIAL,
    retry_max: CHIP_REF_RETRY_MAX,
    poll: CHIP_REF_WORKER_POLL,
    degraded_log_interval: CHIP_REF_DEGRADED_LOG_INTERVAL,
};

#[derive(Debug)]
pub struct ChipRefPacket {
    pub samples: Vec<i16>,
    pub reference_sequence: u64,
}

#[derive(Debug)]
pub struct ChipRefDownsampler {
    input_frames_per_output: u32,
    accum: i64,
    count: u32,
}

impl ChipRefDownsampler {
    pub fn new(input_sample_rate: u32, output_sample_rate: u32) -> Result<Self> {
        if input_sample_rate % output_sample_rate != 0 {
            anyhow::bail!(
                "chip-reference sample rate {} must divide outputd sample rate {}",
                output_sample_rate,
                input_sample_rate
            );
        }
        Ok(Self {
            input_frames_per_output: input_sample_rate / output_sample_rate,
            accum: 0,
            count: 0,
        })
    }

    /// Allocates one ~107-sample `Vec` per period on the playout thread (moved
    /// to the writer thread inside `ChipRefPacket`), and only where the
    /// chip-reference leg is armed (`chip_ref_pcm` set). Removing it means a
    /// pool or pre-sized ring on the writer side — a different ownership model.
    pub fn process(&mut self, stereo_samples: &[i16]) -> Vec<i16> {
        let input_frames = stereo_samples.len() / (CHANNELS as usize);
        let output_frames =
            (input_frames + self.count as usize) / (self.input_frames_per_output as usize);
        let mut out = Vec::with_capacity(output_frames * (CHANNELS as usize));
        for frame in stereo_samples.chunks_exact(CHANNELS as usize) {
            self.accum += frame[0] as i64 + frame[1] as i64;
            self.count += 1;
            if self.count == self.input_frames_per_output {
                let divisor = (self.input_frames_per_output as i64) * (CHANNELS as i64);
                let mixed = (self.accum / divisor) as i16;
                out.push(mixed);
                out.push(mixed);
                self.accum = 0;
                self.count = 0;
            }
        }
        out
    }
}

#[derive(Debug)]
struct ChipRefWriterConfig<'a> {
    pcm_name: &'a str,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
    tee_path: Option<&'a str>,
}

pub fn spawn_chip_ref_writer(
    pcm_name: String,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
    tee_path: Option<String>,
    shutdown: Arc<AtomicBool>,
    state: Arc<OutputdState>,
) -> Result<SyncSender<ChipRefPacket>> {
    let (tx, rx) = mpsc::sync_channel(REF_OUTPUT_QUEUE_CAPACITY);
    thread::Builder::new()
        .name("outputd-chip-ref".to_string())
        .stack_size(crate::HELPER_STACK_BYTES)
        .spawn(move || {
            run_chip_ref_writer(
                ChipRefWriterConfig {
                    pcm_name: &pcm_name,
                    sample_rate,
                    period_frames,
                    buffer_frames,
                    tee_path: tee_path.as_deref(),
                },
                &rx,
                &shutdown,
                &state,
            );
        })
        .context("spawning outputd chip-ref writer")?;
    Ok(tx)
}

fn run_chip_ref_writer(
    config: ChipRefWriterConfig<'_>,
    rx: &Receiver<ChipRefPacket>,
    shutdown: &AtomicBool,
    state: &OutputdState,
) {
    run_chip_ref_writer_with(
        config,
        rx,
        shutdown,
        state,
        CHIP_REF_WORKER_TIMING,
        open_chip_ref_pcm,
        write_playback_period,
    );
}

/// Rate limiter for the chip-ref degraded (`reason=write_failed`) log line.
/// See [`CHIP_REF_DEGRADED_LOG_INTERVAL`]. Pure and clock-injected so the
/// suppression policy is unit-tested without a live worker or sleeping.
struct ChipRefDegradedLog {
    last_emit: Option<Instant>,
    suppressed: u64,
}

impl ChipRefDegradedLog {
    fn new() -> Self {
        Self {
            last_emit: None,
            suppressed: 0,
        }
    }

    /// Record a degraded occurrence at `now`. Returns `Some(n)` — where `n` is
    /// how many occurrences were suppressed since the last emit — when a line
    /// should be printed, or `None` when this one is folded into the running
    /// suppressed count (the previous emit is < `interval` old).
    fn note(&mut self, now: Instant, interval: Duration) -> Option<u64> {
        match self.last_emit {
            Some(last) if now.duration_since(last) < interval => {
                self.suppressed += 1;
                None
            }
            _ => {
                let suppressed = self.suppressed;
                self.suppressed = 0;
                self.last_emit = Some(now);
                Some(suppressed)
            }
        }
    }

    /// Return to the healthy baseline so the next failure logs immediately.
    fn reset(&mut self) {
        self.last_emit = None;
        self.suppressed = 0;
    }
}

/// What the chip-ref writer should log for one write result. Pure so the full
/// anti-spam policy — `recovery=1` once per real outage plus the rate-limited
/// failure line — is unit-tested as a whole, not just the limiter primitive
/// (#1245). The worker owns the `eprintln!`, the PCM teardown, and STATUS;
/// this owns the degraded-state transition and the log decision.
#[derive(Debug, PartialEq, Eq)]
enum ChipRefWriteLog {
    /// Healthy write within a healthy episode — nothing to log.
    Silent,
    /// First good write after a degraded episode — emit `recovery=1`.
    Recovered,
    /// Write failed and this occurrence should log (`suppressed` folded in).
    Failed { suppressed: u64 },
    /// Write failed but the line is rate-limited away.
    FailedSuppressed,
}

/// Decide what to log for one write outcome (`failed`), advancing the degraded
/// flag and the rate-limiter in lockstep. This is the SINGLE owner of the
/// chip-ref write-path degraded-state transition. See [`ChipRefWriteLog`].
fn chip_ref_write_log(
    failed: bool,
    degraded: &mut bool,
    log: &mut ChipRefDegradedLog,
    now: Instant,
    interval: Duration,
) -> ChipRefWriteLog {
    if failed {
        *degraded = true;
        match log.note(now, interval) {
            Some(suppressed) => ChipRefWriteLog::Failed { suppressed },
            None => ChipRefWriteLog::FailedSuppressed,
        }
    } else if *degraded {
        // First good write after a degraded episode: a real recovery. Reset the
        // limiter so a later, unrelated outage logs immediately.
        *degraded = false;
        log.reset();
        ChipRefWriteLog::Recovered
    } else {
        ChipRefWriteLog::Silent
    }
}

fn run_chip_ref_writer_with<P, Open, WritePeriod>(
    config: ChipRefWriterConfig<'_>,
    rx: &Receiver<ChipRefPacket>,
    shutdown: &AtomicBool,
    state: &OutputdState,
    timing: ChipRefWorkerTiming,
    mut open_pcm: Open,
    mut write_period: WritePeriod,
) where
    Open: FnMut(&ChipRefWriterConfig<'_>, &OutputdState) -> Result<P>,
    WritePeriod: FnMut(&P, &str, &[i16], &mut PlaybackWriteReport) -> Result<()>,
{
    let mut tee = open_chip_ref_tee(config.tee_path);
    let mut pcm: Option<P> = None;
    let mut retry_delay = timing.retry_initial;
    let mut retry_at = Instant::now();
    let mut degraded_logged = false;
    let mut degraded_log = ChipRefDegradedLog::new();

    while !shutdown.load(Ordering::Relaxed) {
        if pcm.is_none() && Instant::now() >= retry_at {
            if degraded_logged {
                state.mark_chip_ref_retry();
            }
            match open_pcm(&config, state) {
                Ok(opened) => {
                    state.mark_chip_ref_writer_active(true);
                    if !degraded_logged {
                        // First open of a healthy episode (startup, or a clean
                        // reopen not preceded by a write failure): confirm the
                        // writer came up. A reopen DURING a degraded
                        // open-OK -> write-fail flap stays silent here — the
                        // `recovery=1` line is emitted on the first successful
                        // WRITE below, so a persistent flap cannot spam a false
                        // "recovered" once per cycle (#1245).
                        eprintln!(
                            "event=outputd.chip_ref.active pcm={} recovery=0",
                            config.pcm_name,
                        );
                    }
                    pcm = Some(opened);
                    retry_delay = timing.retry_initial;
                }
                Err(e) => {
                    state.mark_chip_ref_open_error();
                    if !degraded_logged {
                        eprintln!(
                            "event=outputd.chip_ref.unavailable action=retry_background pcm={} retry_ms={} detail={e:#}",
                            config.pcm_name,
                            retry_delay.as_millis(),
                        );
                        degraded_logged = true;
                    }
                    retry_at = Instant::now() + retry_delay;
                    retry_delay = next_chip_ref_retry_delay(retry_delay, timing.retry_max);
                }
            }
        }

        match rx.recv_timeout(timing.poll) {
            Ok(packet) => {
                let frames = (packet.samples.len() / (CHANNELS as usize)) as u64;
                state.mark_chip_ref_dequeued(frames);
                write_chip_ref_tee(&mut tee, &packet.samples);
                if let Some(opened) = pcm.as_ref() {
                    let mut report = PlaybackWriteReport::default();
                    let result =
                        write_period(opened, config.pcm_name, &packet.samples, &mut report);
                    let failed = result.is_err();
                    state.mark_chip_ref_write(ChipRefWrite {
                        frames_written: report.frames_written,
                        delay_frames: report.delay_frames,
                        reference_sequence: Some(packet.reference_sequence),
                        underruns: report.underruns,
                        xruns: report.xruns,
                        recoveries: report.recoveries,
                        write_failed: failed,
                    });
                    // Anti-spam decision + degraded-state transition live in one
                    // pure place (unit-tested); the worker owns only the PCM
                    // teardown, STATUS, and the eprintln! IO.
                    let log_action = chip_ref_write_log(
                        failed,
                        &mut degraded_logged,
                        &mut degraded_log,
                        Instant::now(),
                        timing.degraded_log_interval,
                    );
                    if failed {
                        state.mark_chip_ref_writer_active(false);
                        pcm = None;
                        retry_delay = timing.retry_initial;
                        retry_at = Instant::now() + retry_delay;
                    }
                    match log_action {
                        ChipRefWriteLog::Recovered => {
                            // A genuine recovery: the sink reopened AND a period
                            // actually landed — emitted once per real outage,
                            // never once per open-OK -> write-fail flap cycle.
                            eprintln!(
                                "event=outputd.chip_ref.active pcm={} recovery=1",
                                config.pcm_name,
                            );
                        }
                        ChipRefWriteLog::Failed { suppressed } => {
                            if let Err(e) = &result {
                                eprintln!(
                                    "event=outputd.chip_ref.unavailable action=retry_background reason=write_failed pcm={} suppressed={suppressed} detail={e:#}",
                                    config.pcm_name,
                                );
                            }
                        }
                        ChipRefWriteLog::Silent | ChipRefWriteLog::FailedSuppressed => {}
                    }
                } else {
                    state.mark_chip_ref_dropped_unavailable();
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    state.mark_chip_ref_writer_active(false);
}

fn open_chip_ref_pcm(config: &ChipRefWriterConfig<'_>, state: &OutputdState) -> Result<PCM> {
    let (pcm, negotiated) = open_playback_pcm(
        "chip_ref",
        config.pcm_name,
        config.sample_rate,
        config.period_frames,
        config.buffer_frames,
    )?;
    eprintln!(
        "event=outputd.chip_ref.opened pcm={} access=RWInterleaved channels={} format=S16_LE sample_rate={} period_frames={} buffer_frames={}",
        config.pcm_name,
        CHANNELS,
        negotiated.sample_rate,
        negotiated.period_frames,
        negotiated.buffer_frames
    );
    let zero = vec![0i16; (config.period_frames as usize) * (CHANNELS as usize)];
    let mut report = PlaybackWriteReport::default();
    let result = write_playback_period(&pcm, config.pcm_name, &zero, &mut report);
    state.mark_chip_ref_write(ChipRefWrite {
        frames_written: report.frames_written,
        delay_frames: report.delay_frames,
        underruns: report.underruns,
        xruns: report.xruns,
        recoveries: report.recoveries,
        write_failed: result.is_err(),
        ..ChipRefWrite::default()
    });
    result?;
    if pcm.state() != State::Running {
        pcm.start().context("starting outputd chip-ref PCM")?;
    }
    Ok(pcm)
}

fn next_chip_ref_retry_delay(current: Duration, maximum: Duration) -> Duration {
    current.saturating_mul(2).min(maximum)
}

#[derive(Debug, Default)]
struct PlaybackWriteReport {
    frames_written: u64,
    delay_frames: Option<u64>,
    underruns: u64,
    xruns: u64,
    recoveries: u64,
}

fn write_playback_period(
    pcm: &PCM,
    pcm_name: &str,
    samples: &[i16],
    report: &mut PlaybackWriteReport,
) -> Result<()> {
    let frames_total = samples.len() / (CHANNELS as usize);
    let io = pcm
        .io_i16()
        .context("getting i16 IO handle for outputd chip-ref")?;
    let mut frames_done = 0usize;
    let mut recoveries = 0u32;
    while frames_done < frames_total {
        let offset = frames_done * (CHANNELS as usize);
        match io.writei(&samples[offset..]) {
            Ok(n) => {
                frames_done += n;
                report.frames_written += n as u64;
                if n == 0 {
                    recoveries += 1;
                    if recoveries > 3 {
                        anyhow::bail!("outputd chip-ref writei returned 0 frames repeatedly");
                    }
                }
            }
            Err(e) => {
                let errno = e.errno();
                if errno == libc::EPIPE || errno == libc::ESTRPIPE {
                    report.xruns += 1;
                    if errno == libc::EPIPE {
                        report.underruns += 1;
                    }
                    report.recoveries += 1;
                    pcm.try_recover(e, true)
                        .context("recovering outputd chip-ref xrun")?;
                    recoveries += 1;
                    if recoveries > 3 {
                        anyhow::bail!("outputd chip-ref xrun recovery exceeded retries");
                    }
                } else {
                    return Err(e).context(format!("writing outputd chip-ref PCM {pcm_name}"));
                }
            }
        }
    }
    if let Ok(delay) = pcm.delay() {
        report.delay_frames = Some(delay.max(0) as u64);
    }
    Ok(())
}

fn open_chip_ref_tee(path: Option<&str>) -> Option<std::fs::File> {
    let path = path?;
    match std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
    {
        Ok(file) => {
            eprintln!("event=outputd.chip_ref.tee.enabled path={path}");
            Some(file)
        }
        Err(e) => {
            eprintln!("event=outputd.chip_ref.tee.open_failed path={path} detail={e}");
            None
        }
    }
}

fn write_chip_ref_tee(tee: &mut Option<std::fs::File>, samples: &[i16]) {
    let Some(file) = tee.as_mut() else {
        return;
    };
    if let Err(e) = file.write_all(i16_bytes(samples)) {
        eprintln!("event=outputd.chip_ref.tee.write_failed detail={e}");
        *tee = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use crate::state::tests::test_config;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn chip_ref_downsampler_downmixes_and_decimates_exact_ratio() {
        let mut downsampler = ChipRefDownsampler::new(48_000, 16_000).unwrap();

        let out = downsampler.process(&[
            3, 9, // mono average: 6
            6, 12, // mono average: 9
            9, 15, // mono average: 12
            12, 18, // carried into the next output
        ]);

        assert_eq!(out, vec![9, 9]);

        let out = downsampler.process(&[
            15, 21, // mono average: 18
            18, 24, // mono average: 21
        ]);

        assert_eq!(out, vec![18, 18]);
    }

    #[test]
    fn chip_ref_downsampler_rejects_fractional_ratios() {
        let err = ChipRefDownsampler::new(48_000, 22_050).unwrap_err();

        assert!(err.to_string().contains("must divide"));
    }

    #[test]
    fn chip_ref_degraded_log_rate_limits_a_write_fail_flap() {
        // #1245: an open-OK -> write-fail flap must not emit one line per
        // failed period. The first failure of an episode logs; the rest are
        // suppressed and counted until the interval elapses, then summarised.
        let mut log = ChipRefDegradedLog::new();
        let t0 = Instant::now();
        let interval = Duration::from_secs(300);

        // First failure logs immediately, nothing suppressed yet.
        assert_eq!(log.note(t0, interval), Some(0));
        // A rapid flap within the window is suppressed and counted, not logged.
        assert_eq!(log.note(t0 + Duration::from_secs(1), interval), None);
        assert_eq!(log.note(t0 + Duration::from_secs(2), interval), None);
        assert_eq!(log.note(t0 + Duration::from_secs(60), interval), None);
        // Once the window elapses, one summary line carries the suppressed count
        // and the counter resets.
        assert_eq!(log.note(t0 + Duration::from_secs(300), interval), Some(3));
        assert_eq!(log.note(t0 + Duration::from_secs(301), interval), None);

        // A genuine recovery resets the limiter: the next failure logs at once.
        log.reset();
        assert_eq!(log.note(t0 + Duration::from_secs(600), interval), Some(0));
    }

    #[test]
    fn chip_ref_write_log_recovers_once_and_rate_limits_failures() {
        // #1245: pin the full write-result wiring, not just the limiter —
        // `recovery=1` fires once per real outage and the failure line is
        // rate-limited across a sustained open-OK -> write-fail flap.
        let mut degraded = false;
        let mut log = ChipRefDegradedLog::new();
        let t0 = Instant::now();
        let interval = Duration::from_secs(300);
        let s = |n| Duration::from_secs(n);

        // Healthy writes log nothing and keep us out of a degraded episode.
        assert_eq!(
            chip_ref_write_log(false, &mut degraded, &mut log, t0, interval),
            ChipRefWriteLog::Silent
        );
        assert!(!degraded);

        // First failure opens the episode and logs immediately.
        assert_eq!(
            chip_ref_write_log(true, &mut degraded, &mut log, t0 + s(1), interval),
            ChipRefWriteLog::Failed { suppressed: 0 }
        );
        assert!(degraded);

        // A flap (reopen succeeds, next write fails) within the window is
        // suppressed and counted — no per-cycle spam.
        assert_eq!(
            chip_ref_write_log(true, &mut degraded, &mut log, t0 + s(2), interval),
            ChipRefWriteLog::FailedSuppressed
        );
        assert_eq!(
            chip_ref_write_log(true, &mut degraded, &mut log, t0 + s(60), interval),
            ChipRefWriteLog::FailedSuppressed
        );

        // Once the interval elapses, one summary line carries the count.
        assert_eq!(
            chip_ref_write_log(true, &mut degraded, &mut log, t0 + s(301), interval),
            ChipRefWriteLog::Failed { suppressed: 2 }
        );

        // A good write is a genuine recovery: logged once, and it resets the
        // limiter so the NEXT outage logs immediately rather than suppressed.
        assert_eq!(
            chip_ref_write_log(false, &mut degraded, &mut log, t0 + s(302), interval),
            ChipRefWriteLog::Recovered
        );
        assert!(!degraded);
        assert_eq!(
            chip_ref_write_log(false, &mut degraded, &mut log, t0 + s(303), interval),
            ChipRefWriteLog::Silent
        );
        assert_eq!(
            chip_ref_write_log(true, &mut degraded, &mut log, t0 + s(304), interval),
            ChipRefWriteLog::Failed { suppressed: 0 }
        );
    }

    #[test]
    fn chip_ref_retry_backoff_is_bounded() {
        assert_eq!(
            next_chip_ref_retry_delay(Duration::from_secs(1), CHIP_REF_RETRY_MAX),
            Duration::from_secs(2)
        );
        assert_eq!(
            next_chip_ref_retry_delay(Duration::from_secs(16), CHIP_REF_RETRY_MAX),
            CHIP_REF_RETRY_MAX
        );
        assert_eq!(
            next_chip_ref_retry_delay(CHIP_REF_RETRY_MAX, CHIP_REF_RETRY_MAX),
            CHIP_REF_RETRY_MAX
        );
    }

    #[test]
    fn chip_ref_worker_degrades_then_recovers_without_exiting() {
        let config = Config {
            chip_ref_pcm: Some("test-unavailable-chip-ref".to_string()),
            ..test_config()
        };
        let state = Arc::new(OutputdState::new(&config));
        let shutdown = Arc::new(AtomicBool::new(false));
        let attempts = Arc::new(AtomicUsize::new(0));
        let writes = Arc::new(AtomicUsize::new(0));
        let (tx, rx) = mpsc::sync_channel(4);

        let worker_state = Arc::clone(&state);
        let worker_shutdown = Arc::clone(&shutdown);
        let worker_attempts = Arc::clone(&attempts);
        let worker_writes = Arc::clone(&writes);
        let handle = thread::spawn(move || {
            run_chip_ref_writer_with(
                ChipRefWriterConfig {
                    pcm_name: "test-unavailable-chip-ref",
                    sample_rate: 16_000,
                    period_frames: 320,
                    buffer_frames: 1280,
                    tee_path: None,
                },
                &rx,
                &worker_shutdown,
                &worker_state,
                ChipRefWorkerTiming {
                    retry_initial: Duration::from_millis(5),
                    retry_max: Duration::from_millis(10),
                    poll: Duration::from_millis(1),
                    degraded_log_interval: Duration::from_millis(50),
                },
                move |_, _| {
                    if worker_attempts.fetch_add(1, Ordering::Relaxed) == 0 {
                        anyhow::bail!("synthetic missing chip-reference device");
                    }
                    Ok(())
                },
                move |_, _, samples, report| {
                    report.frames_written = (samples.len() / CHANNELS as usize) as u64;
                    worker_writes.fetch_add(1, Ordering::Relaxed);
                    Ok(())
                },
            );
        });

        tx.send(ChipRefPacket {
            samples: vec![0; 640],
            reference_sequence: 1,
        })
        .unwrap();
        let deadline = Instant::now() + Duration::from_millis(250);
        while attempts.load(Ordering::Relaxed) < 2 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert!(attempts.load(Ordering::Relaxed) >= 2);

        tx.send(ChipRefPacket {
            samples: vec![0; 640],
            reference_sequence: 2,
        })
        .unwrap();
        let deadline = Instant::now() + Duration::from_millis(250);
        while writes.load(Ordering::Relaxed) < 1 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert!(writes.load(Ordering::Relaxed) >= 1);

        let snapshot = state.snapshot_json();
        assert!(snapshot.contains(r#""status":"active""#), "{snapshot}");
        assert!(snapshot.contains(r#""retry_count":1"#), "{snapshot}");
        assert!(
            snapshot.contains(r#""dropped_periods_while_unavailable":1"#),
            "{snapshot}"
        );

        shutdown.store(true, Ordering::Relaxed);
        drop(tx);
        handle.join().unwrap();
    }
}
