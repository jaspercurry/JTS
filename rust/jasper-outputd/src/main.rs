//! jasper-outputd - final-output owner.
//!
//! The default binary mode remains fake so `jasper-outputd --once` is
//! safe in a developer shell. The systemd unit opts into the real ALSA
//! transport with `JASPER_OUTPUTD_BACKEND=alsa`, reading
//! CamillaDSP's post-DSP loopback lane and writing the DAC directly.

use std::io;
use std::mem;
use std::net::{SocketAddr, UdpSocket};
use std::os::fd::RawFd;
use std::os::unix::net::UnixDatagram;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, SyncSender, TrySendError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use alsa::pcm::{State, PCM};
use anyhow::{Context, Result};
use jasper_outputd::alsa_backend::{
    open_playback_pcm, AlsaBackend, ContentRead, DualAppleBackend, IoCounters,
};
use jasper_outputd::config::{BackendMode, Config, ContentBridgeMode, SinkMode};
use jasper_outputd::content_bridge::ContentBridge;
use jasper_outputd::core::{OutputCore, PeriodReport};
use jasper_outputd::dac_content::DacContentSource;
use jasper_outputd::tts::{spawn_tts_server, tts_channels, TtsBridge};
use jasper_outputd::state::{OutputdState, StateServer};
use jasper_outputd::{CHANNELS, SAMPLE_RATE};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use signal_hook::flag;

const REF_OUTPUT_QUEUE_CAPACITY: usize = 32;
const MAX_CONTENT_BRIDGE_DRAIN_READS: usize = 8;

/// Exit code for a CONFIG-validation failure (sysexits.h EX_CONFIG).
/// The unit pairs it with `RestartPreventExitStatus=78`: a fail-closed
/// config rejection PARKS the unit failed (visible on /state + doctor)
/// instead of crash-looping — restarting cannot fix bad config, and on
/// this unit the loop escalates to StartLimitAction=reboot. Measured
/// incident (jts3, 2026-06-11): a grouping env + lab retune layered into
/// a guard-rejected combination; outputd crash-looped into THREE Pi
/// reboots before the T5.1 boot-loop guard contained it.
const EXIT_CONFIG: i32 = 78;

fn main() -> Result<()> {
    let config = match Config::from_env() {
        Ok(config) => config,
        Err(e) => {
            eprintln!("event=outputd.config_invalid detail={e:#}");
            eprintln!(
                "Error: invalid JASPER_OUTPUTD_* configuration (exit {EXIT_CONFIG}; \
                 the unit does not restart on config errors — fix the env and \
                 `systemctl restart jasper-outputd`)"
            );
            std::process::exit(EXIT_CONFIG);
        }
    };
    let once = std::env::args().any(|arg| arg == "--once");
    let shutdown = Arc::new(AtomicBool::new(false));
    flag::register(SIGTERM, Arc::clone(&shutdown)).context("registering SIGTERM handler")?;
    flag::register(SIGINT, Arc::clone(&shutdown)).context("registering SIGINT handler")?;

    let state = Arc::new(OutputdState::new(&config));

    if let Some(socket_path) = &config.control_socket_path {
        spawn_state_server(
            PathBuf::from(socket_path),
            Arc::clone(&state),
            Arc::clone(&shutdown),
        )?;
    }

    lock_memory();

    let result = match config.backend {
        BackendMode::Fake => {
            let mut core = OutputCore::new_for_daemon(config.period_frames, config.stream_id);
            run_fake(&config, &mut core, &state, once, &shutdown)
        }
        BackendMode::Alsa if config.sink_mode == SinkMode::DualApple => {
            run_alsa_dual_apple(&config, &state, once, &shutdown)
        }
        BackendMode::Alsa => run_alsa(&config, &state, once, &shutdown),
    };

    notify_systemd("STOPPING=1")?;
    result
}

fn run_fake(
    config: &Config,
    core: &mut OutputCore,
    state: &OutputdState,
    once: bool,
    shutdown: &Arc<AtomicBool>,
) -> Result<()> {
    let period = period_duration(config.period_frames);
    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    notify_ready(config)?;

    while !shutdown.load(Ordering::Relaxed) {
        let report = core.step();
        state.mark_period(
            fake_counters(core.frames_written()),
            report.reference_sequence,
            report.clipped_samples,
        );
        if once {
            log_once(report);
            return Ok(());
        }
        if last_watchdog.elapsed() >= watchdog_interval {
            notify_systemd("WATCHDOG=1")?;
            state.mark_watchdog_ping();
            last_watchdog = Instant::now();
        }
        thread::sleep(period);
    }
    Ok(())
}

fn run_alsa(
    config: &Config,
    state: &OutputdState,
    once: bool,
    shutdown: &Arc<AtomicBool>,
) -> Result<()> {
    let mut backend = AlsaBackend::new(config)?;
    let mut ref_outputs = ReferenceSideOutputs::new(config, shutdown)?;
    state.set_negotiated(backend.content_negotiated, backend.dac_negotiated);
    let period_samples = (config.period_frames as usize) * (CHANNELS as usize);
    let mut content_buf = vec![0i16; period_samples];
    let mut content_read_buf = vec![0i16; period_samples];
    let mut content_bridge = match config.content_bridge_mode {
        ContentBridgeMode::Direct => None,
        ContentBridgeMode::RateMatch => {
            eprintln!(
                "event=outputd.content_bridge.enabled mode=rate_match ring_frames={} target_fill_frames={} max_adjust_ppm={}",
                config.content_bridge.ring_frames,
                config.content_bridge.target_fill_frames,
                config.content_bridge.max_adjust_ppm,
            );
            Some(ContentBridge::new(
                config.content_bridge,
                config.period_frames,
                CHANNELS as usize,
            )?)
        }
    };
    // Multi-room round-trip lane (Increment 3, HANDOFF-multiroom.md §2):
    // when configured, the DAC is fed from the member-content FIFO with
    // an inv-B fallback to the direct content read below. Lazy + non-
    // blocking; None (solo) leaves this loop byte-identical to before.
    let mut dac_content = config.dac_content_fifo.as_deref().map(|path| {
        eprintln!(
            "event=outputd.dac_content.enabled fifo={} channel={}",
            path,
            config.dac_content_channel.as_str(),
        );
        DacContentSource::new(path, config.dac_content_channel, config.period_frames)
    });
    // Bonded-member TTS (Increment 5 PR-2): constructed ONLY when the
    // reconciler set the socket env — solo keeps fanin-owned TTS and
    // this loop stays byte-identical. The OutputCore engine (assistant
    // segments, loudness, saturating mix, the DAC-true PlayoutLedger)
    // mixes voice at the FINAL output stage: downstream of the
    // round-trip, upstream of the reference publish (inv-A).
    let mut tts: Option<(OutputCore, TtsBridge)> =
        if let Some(path) = &config.tts_socket_path {
            let (tx, rx, flush_tx, flush_rx, metrics, epoch) =
                tts_channels(config.tts_max_pending_frames);
            spawn_tts_server(
                PathBuf::from(path),
                tx,
                flush_tx,
                epoch,
                metrics.clone(),
            )?;
            state.set_tts(path.clone(), metrics.clone());
            eprintln!(
                "event=outputd.tts.enabled socket={} budget_frames={} program_duck_db={}",
                path, config.tts_max_pending_frames, config.tts_program_duck_db,
            );
            let core =
                OutputCore::new_for_daemon(config.period_frames, config.stream_id);
            let bridge =
                TtsBridge::new(rx, flush_rx, metrics, config.tts_program_duck_db);
            Some((core, bridge))
        } else {
            None
        };
    // Pair-balance trim: a fixed linear gain on the round-trip content
    // path (FIFO and inv-B fallback periods alike — no level jump on a
    // starvation transition). Precomputed once; <= 0 dB enforced at
    // config parse, so this can only attenuate.
    let dac_content_trim: Option<f32> = if dac_content.is_some()
        && config.dac_content_trim_db < 0.0
    {
        Some(10f32.powf(config.dac_content_trim_db / 20.0))
    } else {
        None
    };
    let zero_period = vec![0i16; period_samples];
    let prime_periods = prime_periods(
        backend.dac_negotiated.buffer_frames,
        backend.dac_negotiated.period_frames,
    );
    for _ in 0..prime_periods {
        backend
            .write_dac_period(&zero_period)
            .context("priming outputd DAC with silence")?;
    }
    backend.start_dac()?;
    eprintln!(
        "event=outputd.alsa.primed prime_periods={} buffer_frames={} period_frames={}",
        prime_periods, backend.dac_negotiated.buffer_frames, backend.dac_negotiated.period_frames,
    );
    notify_ready(config)?;

    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    let mut dac_delay_warning_logged = false;
    let mut content_drain_warning_logged = false;
    let mut reference_sequence = 0u64;
    let mut last_clipped_samples = 0u32;

    while !shutdown.load(Ordering::Relaxed) {
        let mut served_from_fifo = false;
        if let Some(src) = dac_content.as_mut() {
            served_from_fifo = src.try_fill_period(&mut content_buf);
            state.mark_dac_content(src.metrics());
            if served_from_fifo {
                // The FIFO served this period — still DRAIN the direct
                // content lane (bounded, non-blocking, discard) so an
                // upstream writer to the loopback can never stall on a
                // full ring while the round-trip is active. The drained
                // data is intentionally discarded; an inv-B fallback
                // period reads fresh direct content below.
                //
                // Best-effort: a hard error on this DISCARDED lane must
                // NOT crash the daemon while the FIFO audio is healthy
                // (inv-B — never silence the leader). Swallow it; if the
                // lane is genuinely broken, the inv-B fallback read below
                // surfaces it when we actually need the lane. Xruns are
                // already recovered inside read_content_available.
                for _ in 0..MAX_CONTENT_BRIDGE_DRAIN_READS {
                    match backend.read_content_available(&mut content_read_buf) {
                        Ok(ContentRead::Frames(frames)) if frames > 0 => {}
                        Ok(_) => break,
                        Err(e) => {
                            if !content_drain_warning_logged {
                                eprintln!(
                                    "event=outputd.dac_content.drain_failed detail={e:#}"
                                );
                                content_drain_warning_logged = true;
                            }
                            break;
                        }
                    }
                }
            }
        }
        if !served_from_fifo {
            if let Some(bridge) = content_bridge.as_mut() {
                read_content_bridge_period(
                    &mut backend,
                    bridge,
                    &mut content_read_buf,
                    &mut content_buf,
                )?;
                state.mark_content_bridge(bridge.metrics());
            } else {
                let _frames_read = backend.read_content_period(&mut content_buf)?;
            }
        }
        if let Some(trim) = dac_content_trim {
            // Before duck/mix/publish so the AEC reference carries the
            // trimmed program too (inv-A: reference == final DAC content).
            apply_linear_gain(&mut content_buf, trim);
        }
        if let Some((core, bridge)) = tts.as_mut() {
            // The TTS-enabled path: voice mixes via the engine. Duck is
            // applied to the CONTENT before the mix so the reference
            // carries the ducked program too (inv-A).
            bridge.drain(core);
            if let Some(gain) = bridge.content_duck_gain() {
                apply_linear_gain(&mut content_buf, gain);
            }
            core.prepare_period_with_content(&content_buf);
            backend.write_dac_period(core.output_period())?;
            let dac_delay_frames = match backend.dac_delay_frames() {
                Ok(frames) => frames,
                Err(e) => {
                    if !dac_delay_warning_logged {
                        eprintln!("event=outputd.dac_delay_unavailable detail={e:#}");
                        dac_delay_warning_logged = true;
                    }
                    backend.dac_negotiated.buffer_frames as u64
                }
            };
            // The ledger drains against ACTUAL DAC progress — the honest
            // max_audio_played_ms barge-in has never had from fanin.
            let report = core.commit_prepared_period_with_dac_delay(dac_delay_frames);
            ref_outputs.publish(core.output_period());
            reference_sequence = report.reference_sequence;
            last_clipped_samples = report.clipped_samples;
            state.mark_period(
                backend.counters(),
                reference_sequence,
                report.clipped_samples,
            );
        } else {
            backend.write_dac_period(&content_buf)?;
            let _dac_delay_frames = match backend.dac_delay_frames() {
                Ok(frames) => frames,
                Err(e) => {
                    if !dac_delay_warning_logged {
                        eprintln!("event=outputd.dac_delay_unavailable detail={e:#}");
                        dac_delay_warning_logged = true;
                    }
                    backend.dac_negotiated.buffer_frames as u64
                }
            };
            ref_outputs.publish(&content_buf);
            reference_sequence = reference_sequence.saturating_add(1);
            state.mark_period(backend.counters(), reference_sequence, 0);
        }
        if once {
            eprintln!(
                "event=outputd.once frames_written={} reference_sequence={} clipped_samples={}",
                backend.counters().dac_frames_written,
                reference_sequence,
                last_clipped_samples,
            );
            return Ok(());
        }
        if last_watchdog.elapsed() >= watchdog_interval {
            notify_systemd("WATCHDOG=1")?;
            state.mark_watchdog_ping();
            last_watchdog = Instant::now();
        }
    }
    Ok(())
}

fn run_alsa_dual_apple(
    config: &Config,
    state: &OutputdState,
    once: bool,
    shutdown: &Arc<AtomicBool>,
) -> Result<()> {
    let mut backend = DualAppleBackend::new(config)?;
    let mut ref_outputs = ReferenceSideOutputs::new(config, shutdown)?;
    state.set_negotiated(backend.content_negotiated, backend.dac_negotiated);
    state.mark_dual_apple_status(&backend.dual_status());
    let content_channels = config.content_channels as usize;
    let mut content_buf = vec![0i16; (config.period_frames as usize) * content_channels];
    let mut reference_buf = vec![0i16; (config.period_frames as usize) * (CHANNELS as usize)];
    let zero_period = vec![0i16; (config.period_frames as usize) * content_channels];
    let prime_periods = prime_periods(
        backend.dac_negotiated.buffer_frames,
        backend.dac_negotiated.period_frames,
    );
    for _ in 0..prime_periods {
        backend
            .write_dual_period(&zero_period)
            .context("priming dual Apple DACs with silence")?;
    }
    backend.start_dacs()?;
    eprintln!(
        "event=outputd.dual_apple.primed prime_periods={} buffer_frames={} period_frames={}",
        prime_periods, backend.dac_negotiated.buffer_frames, backend.dac_negotiated.period_frames,
    );
    notify_ready(config)?;

    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    let mut dac_delay_warning_logged = false;
    let mut reference_sequence = 0u64;

    while !shutdown.load(Ordering::Relaxed) {
        let _frames_read = backend.read_content_period(&mut content_buf)?;
        backend.write_dual_period(&content_buf)?;
        state.mark_dual_apple_status(&backend.dual_status());
        downmix_dual_active_reference(&content_buf, &mut reference_buf);
        ref_outputs.publish(&reference_buf);
        reference_sequence = reference_sequence.saturating_add(1);
        let _dac_delay_frames = match backend.dac_delay_frames() {
            Ok(frames) => frames,
            Err(e) => {
                if !dac_delay_warning_logged {
                    eprintln!("event=outputd.dual_apple.dac_delay_unavailable detail={e:#}");
                    dac_delay_warning_logged = true;
                }
                backend.dac_negotiated.buffer_frames as u64
            }
        };
        state.mark_period(backend.counters(), reference_sequence, 0);
        if once {
            eprintln!(
                "event=outputd.once frames_written={} reference_sequence={} clipped_samples=0",
                backend.counters().dac_frames_written,
                reference_sequence,
            );
            return Ok(());
        }
        if last_watchdog.elapsed() >= watchdog_interval {
            notify_systemd("WATCHDOG=1")?;
            state.mark_watchdog_ping();
            last_watchdog = Instant::now();
        }
    }
    Ok(())
}

fn downmix_dual_active_reference(samples_4ch: &[i16], out_stereo: &mut [i16]) {
    assert_eq!(samples_4ch.len() % 4, 0);
    assert_eq!(out_stereo.len(), (samples_4ch.len() / 4) * (CHANNELS as usize));
    for (frame, out) in samples_4ch.chunks_exact(4).zip(out_stereo.chunks_exact_mut(2)) {
        out[0] = average_i16(frame[0], frame[1]);
        out[1] = average_i16(frame[2], frame[3]);
    }
}

fn average_i16(a: i16, b: i16) -> i16 {
    (((a as i32) + (b as i32)) / 2) as i16
}

/// In-place linear attenuation for the program duck (gain <= 1.0, so
/// no clipping is possible; the cast truncation is inaudible at duck
/// depths).
fn apply_linear_gain(samples: &mut [i16], gain: f32) {
    for s in samples.iter_mut() {
        *s = (*s as f32 * gain) as i16;
    }
}

fn prime_periods(buffer_frames: u32, period_frames: u32) -> u32 {
    if period_frames == 0 {
        return 1;
    }
    ((buffer_frames / period_frames).saturating_sub(1)).max(1)
}

fn read_content_bridge_period(
    backend: &mut AlsaBackend,
    bridge: &mut ContentBridge,
    read_buf: &mut [i16],
    out: &mut [i16],
) -> Result<()> {
    for _ in 0..MAX_CONTENT_BRIDGE_DRAIN_READS {
        match backend.read_content_available(read_buf)? {
            ContentRead::Frames(frames) => {
                if frames == 0 {
                    break;
                }
                let samples = frames * (CHANNELS as usize);
                bridge.push_input(&read_buf[..samples]);
            }
            ContentRead::NoData => break,
            ContentRead::XrunRecovered => {
                bridge.reset_after_discontinuity("content_xrun");
                break;
            }
        }
    }
    bridge.render_period(out);
    Ok(())
}

fn notify_ready(config: &Config) -> Result<()> {
    notify_systemd("READY=1").context("notifying systemd READY=1")?;
    eprintln!(
        "event=outputd.ready backend={} sink_mode={} period_frames={} stream_id={}",
        config.backend.as_str(),
        config.sink_mode.as_str(),
        config.period_frames,
        config.stream_id
    );
    Ok(())
}

fn fake_counters(frames_written: u64) -> IoCounters {
    IoCounters {
        content_frames_read: 0,
        content_empty_period_count: 0,
        content_partial_period_count: 0,
        content_eagain_count: 0,
        dac_frames_written: frames_written,
        content_xrun_count: 0,
        dac_xrun_count: 0,
    }
}

struct ReferenceSideOutputs {
    udp_socket: Option<UdpSocket>,
    udp_target: Option<SocketAddr>,
    chip_tx: Option<SyncSender<Vec<i16>>>,
    chip_downsampler: Option<ChipRefDownsampler>,
}

impl ReferenceSideOutputs {
    fn new(config: &Config, shutdown: &Arc<AtomicBool>) -> Result<Self> {
        let udp_target =
            match config.reference_udp_target.as_deref() {
                Some(raw) => Some(raw.parse::<SocketAddr>().with_context(|| {
                    format!("parsing JASPER_OUTPUTD_REFERENCE_UDP_TARGET={raw:?}")
                })?),
                None => None,
            };
        let udp_socket = if udp_target.is_some() {
            let sock =
                UdpSocket::bind("127.0.0.1:0").context("binding outputd reference UDP sender")?;
            sock.set_nonblocking(true)
                .context("setting outputd reference UDP sender nonblocking")?;
            Some(sock)
        } else {
            None
        };

        let chip_tx = if let Some(pcm_name) = &config.chip_ref_pcm {
            Some(spawn_chip_ref_writer(
                pcm_name.clone(),
                config.chip_ref_sample_rate,
                config.chip_ref_period_frames,
                config.chip_ref_buffer_frames,
                Arc::clone(shutdown),
            )?)
        } else {
            None
        };

        if let Some(target) = udp_target {
            eprintln!("event=outputd.reference_udp.enabled target={target}");
        }
        if let Some(pcm) = &config.chip_ref_pcm {
            eprintln!("event=outputd.chip_ref.enabled pcm={pcm}");
        }

        Ok(Self {
            udp_socket,
            udp_target,
            chip_tx,
            chip_downsampler: if config.chip_ref_pcm.is_some() {
                Some(ChipRefDownsampler::new(
                    config.sample_rate,
                    config.chip_ref_sample_rate,
                )?)
            } else {
                None
            },
        })
    }

    fn publish(&mut self, stereo_samples: &[i16]) {
        if let (Some(sock), Some(target)) = (&self.udp_socket, self.udp_target) {
            if let Err(e) = sock.send_to(bytemuck_i16(stereo_samples), target) {
                if e.kind() != io::ErrorKind::WouldBlock {
                    eprintln!("event=outputd.reference_udp.send_failed detail={e}");
                }
            }
        }
        if let Some(tx) = &self.chip_tx {
            let dual_mono = self
                .chip_downsampler
                .as_mut()
                .expect("chip ref downsampler is present when chip_tx is present")
                .process(stereo_samples);
            if dual_mono.is_empty() {
                return;
            }
            match tx.try_send(dual_mono) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => {
                    eprintln!("event=outputd.chip_ref.queue_full action=drop_period");
                }
                Err(TrySendError::Disconnected(_)) => {
                    eprintln!("event=outputd.chip_ref.disconnected action=drop_period");
                }
            }
        }
    }
}

#[derive(Debug)]
struct ChipRefDownsampler {
    input_frames_per_output: u32,
    accum: i64,
    count: u32,
}

impl ChipRefDownsampler {
    fn new(input_sample_rate: u32, output_sample_rate: u32) -> Result<Self> {
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

    fn process(&mut self, stereo_samples: &[i16]) -> Vec<i16> {
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

fn bytemuck_i16(samples: &[i16]) -> &[u8] {
    unsafe {
        std::slice::from_raw_parts(
            samples.as_ptr() as *const u8,
            std::mem::size_of_val(samples),
        )
    }
}

fn spawn_chip_ref_writer(
    pcm_name: String,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
    shutdown: Arc<AtomicBool>,
) -> Result<SyncSender<Vec<i16>>> {
    let (tx, rx) = mpsc::sync_channel(REF_OUTPUT_QUEUE_CAPACITY);
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    thread::Builder::new()
        .name("outputd-chip-ref".to_string())
        .spawn(move || {
            let result = run_chip_ref_writer(
                &pcm_name,
                sample_rate,
                period_frames,
                buffer_frames,
                &rx,
                &shutdown,
                ready_tx,
            );
            if let Err(e) = result {
                eprintln!("event=outputd.chip_ref.failed detail={e:#}");
            }
        })
        .context("spawning outputd chip-ref writer")?;
    match ready_rx.recv_timeout(Duration::from_secs(5)) {
        Ok(Ok(())) => Ok(tx),
        Ok(Err(detail)) => anyhow::bail!("outputd chip-ref writer failed to start: {detail}"),
        Err(_) => anyhow::bail!("outputd chip-ref writer did not report readiness"),
    }
}

fn run_chip_ref_writer(
    pcm_name: &str,
    sample_rate: u32,
    period_frames: u32,
    buffer_frames: u32,
    rx: &Receiver<Vec<i16>>,
    shutdown: &AtomicBool,
    ready_tx: SyncSender<Result<(), String>>,
) -> Result<()> {
    let startup = (|| -> Result<PCM> {
        let (pcm, negotiated) = open_playback_pcm(
            "chip_ref",
            pcm_name,
            sample_rate,
            period_frames,
            buffer_frames,
        )?;
        eprintln!(
            "event=outputd.chip_ref.opened pcm={} sample_rate={} period_frames={} buffer_frames={}",
            pcm_name, negotiated.sample_rate, negotiated.period_frames, negotiated.buffer_frames
        );
        let zero = vec![0i16; (period_frames as usize) * (CHANNELS as usize)];
        write_playback_period(&pcm, pcm_name, &zero)?;
        if pcm.state() != State::Running {
            pcm.start().context("starting outputd chip-ref PCM")?;
        }
        Ok(pcm)
    })();
    let pcm = match startup {
        Ok(pcm) => {
            let _ = ready_tx.send(Ok(()));
            pcm
        }
        Err(e) => {
            let detail = format!("{e:#}");
            let _ = ready_tx.send(Err(detail));
            return Err(e);
        }
    };
    while !shutdown.load(Ordering::Relaxed) {
        match rx.recv_timeout(Duration::from_millis(500)) {
            Ok(samples) => {
                if let Err(e) = write_playback_period(&pcm, pcm_name, &samples) {
                    eprintln!("event=outputd.chip_ref.write_failed detail={e:#}");
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    Ok(())
}

fn write_playback_period(pcm: &PCM, pcm_name: &str, samples: &[i16]) -> Result<()> {
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
    Ok(())
}

fn log_once(report: PeriodReport) {
    eprintln!(
        "event=outputd.once frames_written={} reference_sequence={} clipped_samples={}",
        report.frames_written, report.reference_sequence, report.clipped_samples
    );
}

fn spawn_state_server(
    path: PathBuf,
    state: Arc<OutputdState>,
    shutdown: Arc<AtomicBool>,
) -> Result<()> {
    let server = StateServer::bind(path, state)?;
    thread::Builder::new()
        .name("outputd-state".to_string())
        .spawn(move || {
            if let Err(e) = server.run(&shutdown) {
                eprintln!("event=outputd.state_server.failed detail={e:#}");
            }
        })
        .context("spawning outputd state thread")?;
    Ok(())
}

fn lock_memory() {
    let rc = unsafe { libc::mlockall(libc::MCL_CURRENT | libc::MCL_FUTURE) };
    if rc == 0 {
        eprintln!("event=outputd.mlockall_ok");
    } else {
        let err = io::Error::last_os_error();
        eprintln!(
            "event=outputd.mlockall_failed errno={} detail={}",
            err.raw_os_error().unwrap_or(0),
            err
        );
    }
}

fn period_duration(period_frames: u32) -> Duration {
    Duration::from_nanos((period_frames as u64) * 1_000_000_000u64 / (SAMPLE_RATE as u64))
}

fn watchdog_interval() -> Duration {
    let watchdog_usec = std::env::var("WATCHDOG_USEC")
        .ok()
        .and_then(|raw| raw.parse::<u64>().ok())
        .unwrap_or(30_000_000);
    let thirds = Duration::from_micros(watchdog_usec / 3);
    thirds
        .min(Duration::from_secs(10))
        .max(Duration::from_secs(1))
}

fn notify_systemd(message: &str) -> io::Result<()> {
    let Ok(socket_path) = std::env::var("NOTIFY_SOCKET") else {
        return Ok(());
    };
    if socket_path.starts_with('@') {
        return notify_systemd_abstract(&socket_path, message);
    }

    let sock = UnixDatagram::unbound()?;
    sock.connect(socket_path)?;
    sock.send(message.as_bytes())?;
    Ok(())
}

fn notify_systemd_abstract(socket_path: &str, message: &str) -> io::Result<()> {
    let name = socket_path
        .strip_prefix('@')
        .expect("abstract notify socket must start with @");
    let name_bytes = name.as_bytes();
    if name_bytes.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "empty abstract NOTIFY_SOCKET",
        ));
    }

    // Linux abstract Unix sockets encode the leading "@" from
    // NOTIFY_SOCKET as a NUL byte in sun_path. std::os::unix::net
    // deliberately exposes only filesystem paths, so keep the libc
    // bridge tiny and local to systemd notify.
    let probe: libc::sockaddr_un = unsafe { mem::zeroed() };
    let sun_path_len = probe.sun_path.len();
    if name_bytes.len() + 1 > sun_path_len {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "abstract NOTIFY_SOCKET is too long",
        ));
    }

    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let result = notify_systemd_abstract_fd(fd, name_bytes, message.as_bytes());
    let close_result = unsafe { libc::close(fd) };
    if result.is_ok() && close_result < 0 {
        return Err(io::Error::last_os_error());
    }
    result
}

fn notify_systemd_abstract_fd(fd: RawFd, name: &[u8], message: &[u8]) -> io::Result<()> {
    let mut addr: libc::sockaddr_un = unsafe { mem::zeroed() };
    addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
    addr.sun_path[0] = 0;
    for (dst, src) in addr.sun_path[1..].iter_mut().zip(name.iter().copied()) {
        *dst = src as libc::c_char;
    }

    let sockaddr_len = (mem::size_of_val(&addr.sun_family) + 1 + name.len()) as libc::socklen_t;
    let rc = unsafe {
        libc::connect(
            fd,
            (&addr as *const libc::sockaddr_un).cast::<libc::sockaddr>(),
            sockaddr_len,
        )
    };
    if rc < 0 {
        return Err(io::Error::last_os_error());
    }

    let sent = unsafe {
        libc::send(
            fd,
            message.as_ptr().cast(),
            message.len(),
            libc::MSG_NOSIGNAL,
        )
    };
    if sent < 0 {
        return Err(io::Error::last_os_error());
    }
    if sent as usize != message.len() {
        return Err(io::Error::new(
            io::ErrorKind::WriteZero,
            "short write to NOTIFY_SOCKET",
        ));
    }
    Ok(())
}

#[allow(dead_code)]
fn _period_samples(period_frames: u32) -> usize {
    (period_frames as usize) * (CHANNELS as usize)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::FromRawFd;
    use std::time::{SystemTime, UNIX_EPOCH};

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
    fn dual_active_reference_downmixes_driver_lanes_to_stereo_monitor() {
        let mut out = vec![0; 4];

        downmix_dual_active_reference(
            &[
                100, 300, 1000, 3000,
                -100, -300, -1000, -3000,
            ],
            &mut out,
        );

        assert_eq!(out, vec![200, 2000, -200, -2000]);
    }

    #[test]
    fn prime_periods_leave_one_period_of_buffer_headroom() {
        assert_eq!(prime_periods(3072, 1024), 2);
        assert_eq!(prime_periods(4096, 1024), 3);
        assert_eq!(prime_periods(1024, 1024), 1);
        assert_eq!(prime_periods(0, 1024), 1);
        assert_eq!(prime_periods(3072, 0), 1);
    }

    #[test]
    fn notify_systemd_supports_abstract_notify_socket() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let name = format!("jasper-outputd-notify-test-{}-{nonce}", std::process::id());
        let listener = bind_abstract_datagram(name.as_bytes()).unwrap();
        listener
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();

        notify_systemd_abstract(&format!("@{name}"), "READY=1").unwrap();

        let mut buf = [0u8; 64];
        let n = listener.recv(&mut buf).unwrap();
        assert_eq!(&buf[..n], b"READY=1");
    }

    #[test]
    fn notify_systemd_rejects_empty_abstract_socket_name() {
        let err = notify_systemd_abstract("@", "READY=1").unwrap_err();

        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
    }

    fn bind_abstract_datagram(name: &[u8]) -> io::Result<UnixDatagram> {
        let probe: libc::sockaddr_un = unsafe { mem::zeroed() };
        if name.len() + 1 > probe.sun_path.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "abstract socket name is too long",
            ));
        }

        let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_DGRAM | libc::SOCK_CLOEXEC, 0) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }

        let mut addr: libc::sockaddr_un = unsafe { mem::zeroed() };
        addr.sun_family = libc::AF_UNIX as libc::sa_family_t;
        addr.sun_path[0] = 0;
        for (dst, src) in addr.sun_path[1..].iter_mut().zip(name.iter().copied()) {
            *dst = src as libc::c_char;
        }
        let sockaddr_len = (mem::size_of_val(&addr.sun_family) + 1 + name.len()) as libc::socklen_t;
        let rc = unsafe {
            libc::bind(
                fd,
                (&addr as *const libc::sockaddr_un).cast::<libc::sockaddr>(),
                sockaddr_len,
            )
        };
        if rc < 0 {
            let err = io::Error::last_os_error();
            let _ = unsafe { libc::close(fd) };
            return Err(err);
        }

        Ok(unsafe { UnixDatagram::from_raw_fd(fd) })
    }
}
