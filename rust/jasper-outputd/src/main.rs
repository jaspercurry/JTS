//! jasper-outputd - final-output owner for the cutover branch.
//!
//! The default binary mode remains fake so `jasper-outputd --once` is
//! safe in a developer shell. The systemd unit opts into the real ALSA
//! transport with `JASPER_OUTPUTD_BACKEND=alsa`, reading
//! CamillaDSP's post-DSP loopback lane and writing the DAC directly.

use std::fs;
use std::io::{self, BufReader};
use std::mem;
use std::os::fd::RawFd;
use std::os::unix::net::{UnixDatagram, UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::TrySendError;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use jasper_outputd::alsa_backend::{AlsaBackend, IoCounters};
use jasper_outputd::config::{BackendMode, Config};
use jasper_outputd::core::{OutputCore, PeriodReport};
use jasper_outputd::mixer::MAX_TTS_GAIN_DB;
use jasper_outputd::protocol::{read_command, TtsCommand};
use jasper_outputd::state::{OutputdState, StateServer, TtsQueueMetrics};
use jasper_outputd::types::SegmentKind;
use jasper_outputd::{CHANNELS, SAMPLE_RATE};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use signal_hook::flag;

const TTS_COMMAND_QUEUE_CAPACITY: usize = 128;
const MAX_PENDING_ASSISTANT_FRAMES: u64 = SAMPLE_RATE as u64 * 2;
const TTS_QUEUE_LOG_STREAK_MS: u64 = 500;
const TTS_QUEUE_LOG_MARGIN_FRAMES: u64 = SAMPLE_RATE as u64 / 2;

fn main() -> Result<()> {
    let config = Config::from_env()?;
    let once = std::env::args().any(|arg| arg == "--once");
    let shutdown = Arc::new(AtomicBool::new(false));
    flag::register(SIGTERM, Arc::clone(&shutdown)).context("registering SIGTERM handler")?;
    flag::register(SIGINT, Arc::clone(&shutdown)).context("registering SIGINT handler")?;

    let mut core = OutputCore::new_for_daemon(config.period_frames, config.stream_id);
    let _aec = core.add_reference_consumer("aec", 128);
    let state = Arc::new(OutputdState::new(&config));

    if let Some(socket_path) = &config.control_socket_path {
        spawn_state_server(
            PathBuf::from(socket_path),
            Arc::clone(&state),
            Arc::clone(&shutdown),
        )?;
    }

    let (tts_tx, tts_rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    let (tts_flush_tx, tts_flush_rx) = mpsc::sync_channel(TTS_COMMAND_QUEUE_CAPACITY);
    let tts_epoch = Arc::new(AtomicU64::new(0));
    if let Some(socket_path) = &config.tts_socket_path {
        spawn_tts_server(
            PathBuf::from(socket_path),
            tts_tx,
            tts_flush_tx,
            Arc::clone(&tts_epoch),
        )?;
    }

    lock_memory();

    let result = match config.backend {
        BackendMode::Fake => run_fake(
            &config,
            &mut core,
            &tts_rx,
            &tts_flush_rx,
            &state,
            once,
            &shutdown,
        ),
        BackendMode::Alsa => run_alsa(
            &config,
            &mut core,
            &tts_rx,
            &tts_flush_rx,
            &state,
            once,
            &shutdown,
        ),
    };

    notify_systemd("STOPPING=1")?;
    result
}

fn run_fake(
    config: &Config,
    core: &mut OutputCore,
    tts_rx: &Receiver<QueuedTtsCommand>,
    tts_flush_rx: &Receiver<u64>,
    state: &OutputdState,
    once: bool,
    shutdown: &AtomicBool,
) -> Result<()> {
    let period = period_duration(config.period_frames);
    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    let mut current_gain_db = MAX_TTS_GAIN_DB;
    let mut tts_queue = TtsQueueTracker::new(MAX_PENDING_ASSISTANT_FRAMES, config.period_frames);
    let mut active_tts_epoch = 0u64;
    notify_ready(config)?;

    while !shutdown.load(Ordering::Relaxed) {
        drain_tts_commands(
            tts_rx,
            tts_flush_rx,
            core,
            &mut current_gain_db,
            &mut active_tts_epoch,
        );
        let report = core.step();
        let tts_metrics = tts_queue.mark_period(core.pending_assistant_frames());
        state.mark_period(
            fake_counters(core.frames_written()),
            report.reference_sequence,
            report.clipped_samples,
            tts_metrics,
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
    core: &mut OutputCore,
    tts_rx: &Receiver<QueuedTtsCommand>,
    tts_flush_rx: &Receiver<u64>,
    state: &OutputdState,
    once: bool,
    shutdown: &AtomicBool,
) -> Result<()> {
    let mut backend = AlsaBackend::new(config)?;
    state.set_negotiated(backend.content_negotiated, backend.dac_negotiated);
    let mut content_buf = vec![0i16; core.period_samples()];
    let zero_period = vec![0i16; core.period_samples()];
    backend
        .write_dac_period(&zero_period)
        .context("priming outputd DAC with silence")?;
    backend.start_dac()?;
    notify_ready(config)?;

    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    let mut current_gain_db = MAX_TTS_GAIN_DB;
    let mut tts_queue = TtsQueueTracker::new(MAX_PENDING_ASSISTANT_FRAMES, config.period_frames);
    let mut active_tts_epoch = 0u64;

    while !shutdown.load(Ordering::Relaxed) {
        drain_tts_commands(
            tts_rx,
            tts_flush_rx,
            core,
            &mut current_gain_db,
            &mut active_tts_epoch,
        );
        let _frames_read = backend.read_content_period(&mut content_buf)?;
        core.prepare_period_with_content(&content_buf);
        backend.write_dac_period(core.output_period())?;
        let report = core.commit_prepared_period();
        let tts_metrics = tts_queue.mark_period(core.pending_assistant_frames());
        state.mark_period(
            backend.counters(),
            report.reference_sequence,
            report.clipped_samples,
            tts_metrics,
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
    }
    Ok(())
}

fn notify_ready(config: &Config) -> Result<()> {
    notify_systemd("READY=1").context("notifying systemd READY=1")?;
    eprintln!(
        "event=outputd.ready backend={} period_frames={} stream_id={}",
        config.backend.as_str(),
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

fn log_once(report: PeriodReport) {
    eprintln!(
        "event=outputd.once frames_written={} reference_sequence={} clipped_samples={}",
        report.frames_written, report.reference_sequence, report.clipped_samples
    );
}

#[derive(Debug)]
struct QueuedTtsCommand {
    epoch: u64,
    command: TtsCommand,
}

fn drain_tts_commands(
    rx: &Receiver<QueuedTtsCommand>,
    flush_rx: &Receiver<u64>,
    core: &mut OutputCore,
    current_gain_db: &mut f32,
    active_epoch: &mut u64,
) {
    drain_tts_flushes(flush_rx, core, active_epoch);
    while core.pending_assistant_frames() < MAX_PENDING_ASSISTANT_FRAMES {
        let Ok(queued) = rx.try_recv() else {
            break;
        };
        if queued.epoch != *active_epoch {
            // A queued command from an older epoch is stale after a flush. A
            // future epoch has not had its flush observed by the audio loop yet,
            // so accepting it here would let post-flush audio bypass the flush
            // barrier.
            continue;
        }
        match queued.command {
            TtsCommand::GainDb(db) => {
                *current_gain_db = db;
            }
            TtsCommand::Audio(samples) => {
                if samples.is_empty() {
                    continue;
                }
                core.enqueue_assistant_segment(
                    None,
                    SegmentKind::Assistant,
                    *current_gain_db,
                    samples,
                );
            }
            TtsCommand::Flush => {
                flush_tts(core, 1);
            }
            TtsCommand::Close => {}
        }
    }
}

fn drain_tts_flushes(flush_rx: &Receiver<u64>, core: &mut OutputCore, active_epoch: &mut u64) {
    let mut requests = 0usize;
    let mut newest_epoch = *active_epoch;
    while let Ok(epoch) = flush_rx.try_recv() {
        requests += 1;
        newest_epoch = newest_epoch.max(epoch);
    }
    if requests > 0 {
        *active_epoch = newest_epoch;
        flush_tts(core, requests);
    }
}

fn flush_tts(core: &mut OutputCore, requests: usize) {
    let pending_before = core.pending_assistant_frames();
    let events = core.flush_assistant();
    let flushed_frames = events.iter().map(|event| event.flushed_frames).sum::<u64>();
    let played_ms = events
        .iter()
        .map(|event| event.audio_played_ms)
        .max()
        .unwrap_or(0);
    eprintln!(
        "event=outputd.tts_flush requests={} pending_frames={} segments={} flushed_frames={} max_audio_played_ms={}",
        requests,
        pending_before,
        events.len(),
        flushed_frames,
        played_ms
    );
}

struct TtsQueueTracker {
    metrics: TtsQueueMetrics,
    period_ms: u64,
    incident_logged: bool,
    incident_max_pending_frames: u64,
}

impl TtsQueueTracker {
    fn new(budget_frames: u64, period_frames: u32) -> Self {
        Self {
            metrics: TtsQueueMetrics {
                budget_frames,
                ..TtsQueueMetrics::default()
            },
            period_ms: ((period_frames as u64) * 1000 / (SAMPLE_RATE as u64)).max(1),
            incident_logged: false,
            incident_max_pending_frames: 0,
        }
    }

    fn mark_period(&mut self, pending_frames: u64) -> TtsQueueMetrics {
        self.metrics.pending_frames = pending_frames;
        self.metrics.max_pending_frames = self.metrics.max_pending_frames.max(pending_frames);
        let was_over_budget = self.metrics.over_budget;
        let over_budget = pending_frames >= self.metrics.budget_frames;
        if over_budget {
            if !was_over_budget {
                self.incident_max_pending_frames = pending_frames;
            } else {
                self.incident_max_pending_frames =
                    self.incident_max_pending_frames.max(pending_frames);
            }
            self.metrics.over_budget_periods = self.metrics.over_budget_periods.saturating_add(1);
            self.metrics.over_budget_ms =
                self.metrics.over_budget_ms.saturating_add(self.period_ms);
            self.metrics.over_budget_streak_ms = self
                .metrics
                .over_budget_streak_ms
                .saturating_add(self.period_ms);
            if !self.incident_logged && self.should_log_incident(pending_frames) {
                eprintln!(
                    "event=outputd.tts_queue_over_budget pending_frames={} budget_frames={} streak_ms={} max_pending_frames={} margin_frames={} total_over_budget_ms={}",
                    pending_frames,
                    self.metrics.budget_frames,
                    self.metrics.over_budget_streak_ms,
                    self.incident_max_pending_frames,
                    pending_frames.saturating_sub(self.metrics.budget_frames),
                    self.metrics.over_budget_ms
                );
                self.incident_logged = true;
            }
        } else if was_over_budget {
            if self.incident_logged {
                eprintln!(
                    "event=outputd.tts_queue_recovered pending_frames={} streak_ms={} max_pending_frames={} total_over_budget_ms={}",
                    pending_frames,
                    self.metrics.over_budget_streak_ms,
                    self.incident_max_pending_frames,
                    self.metrics.over_budget_ms
                );
            }
            self.metrics.over_budget_streak_ms = 0;
            self.incident_logged = false;
            self.incident_max_pending_frames = 0;
        }
        self.metrics.over_budget = over_budget;
        self.metrics
    }

    fn should_log_incident(&self, pending_frames: u64) -> bool {
        self.metrics.over_budget_streak_ms >= TTS_QUEUE_LOG_STREAK_MS
            || pending_frames
                >= self
                    .metrics
                    .budget_frames
                    .saturating_add(TTS_QUEUE_LOG_MARGIN_FRAMES)
    }
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

fn spawn_tts_server(
    path: PathBuf,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<u64>,
    epoch: Arc<AtomicU64>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating outputd TTS socket parent {}", parent.display()))?;
    }
    let _ = fs::remove_file(&path);
    let listener = UnixListener::bind(&path)
        .with_context(|| format!("binding outputd TTS socket {}", path.display()))?;
    eprintln!("event=outputd.tts_socket.listening path={}", path.display());
    thread::Builder::new()
        .name("outputd-tts-ipc".to_string())
        .spawn(move || {
            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => {
                        spawn_tts_client(stream, tx.clone(), flush_tx.clone(), Arc::clone(&epoch))
                            .unwrap_or_else(|e| {
                                eprintln!("event=outputd.tts_socket.spawn_failed detail={e}");
                            })
                    }
                    Err(e) => {
                        eprintln!("event=outputd.tts_socket.accept_failed detail={e}");
                    }
                }
            }
        })
        .context("spawning outputd TTS IPC accept thread")?;
    Ok(())
}

fn spawn_tts_client(
    stream: UnixStream,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<u64>,
    epoch: Arc<AtomicU64>,
) -> io::Result<()> {
    thread::Builder::new()
        .name("outputd-tts-client".to_string())
        .spawn(move || handle_tts_client(stream, tx, flush_tx, epoch))
        .map(|_| ())
}

fn handle_tts_client(
    stream: UnixStream,
    tx: SyncSender<QueuedTtsCommand>,
    flush_tx: SyncSender<u64>,
    epoch: Arc<AtomicU64>,
) {
    let mut reader = BufReader::new(stream);
    loop {
        match read_command(&mut reader) {
            Ok(Some(TtsCommand::Close)) | Ok(None) => return,
            Ok(Some(TtsCommand::Flush)) => {
                let next_epoch = epoch.fetch_add(1, Ordering::SeqCst) + 1;
                if flush_tx.send(next_epoch).is_err() {
                    return;
                }
            }
            Ok(Some(command)) => {
                let current_epoch = epoch.load(Ordering::SeqCst);
                if !try_enqueue_tts_command(
                    &tx,
                    QueuedTtsCommand {
                        epoch: current_epoch,
                        command,
                    },
                ) {
                    return;
                }
            }
            Err(e) => {
                eprintln!("event=outputd.tts_socket.protocol_error detail={e}");
                return;
            }
        }
    }
}

fn try_enqueue_tts_command(tx: &SyncSender<QueuedTtsCommand>, queued: QueuedTtsCommand) -> bool {
    match tx.try_send(queued) {
        Ok(()) => true,
        Err(TrySendError::Full(queued)) => {
            log_dropped_tts_command(&queued);
            true
        }
        Err(TrySendError::Disconnected(_)) => false,
    }
}

fn log_dropped_tts_command(queued: &QueuedTtsCommand) {
    match &queued.command {
        TtsCommand::Audio(samples) => {
            let frames = samples.len() / (CHANNELS as usize);
            eprintln!(
                "event=outputd.tts_command_dropped reason=queue_full command=audio epoch={} frames={}",
                queued.epoch, frames
            );
        }
        TtsCommand::GainDb(_) => {
            eprintln!(
                "event=outputd.tts_command_dropped reason=queue_full command=gain epoch={}",
                queued.epoch
            );
        }
        TtsCommand::Flush | TtsCommand::Close => {}
    }
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

    #[test]
    fn tts_queue_tracker_counts_over_budget_periods_and_recovery() {
        let mut tracker = TtsQueueTracker::new(96_000, 1024);

        let first = tracker.mark_period(95_999);
        assert!(!first.over_budget);
        assert_eq!(first.over_budget_ms, 0);

        let over = tracker.mark_period(96_000);
        assert!(over.over_budget);
        assert_eq!(over.over_budget_periods, 1);
        assert_eq!(over.over_budget_ms, 21);
        assert_eq!(over.over_budget_streak_ms, 21);
        assert_eq!(over.max_pending_frames, 96_000);
        assert!(!tracker.incident_logged);

        let recovered = tracker.mark_period(1_024);
        assert!(!recovered.over_budget);
        assert_eq!(recovered.over_budget_periods, 1);
        assert_eq!(recovered.over_budget_ms, 21);
        assert_eq!(recovered.over_budget_streak_ms, 0);
        assert_eq!(recovered.max_pending_frames, 96_000);
        assert!(!tracker.incident_logged);
    }

    #[test]
    fn tts_queue_tracker_logs_only_sustained_or_large_incidents() {
        let mut tracker = TtsQueueTracker::new(96_000, 1024);

        for _ in 0..20 {
            tracker.mark_period(96_001);
            tracker.mark_period(95_999);
        }
        assert_eq!(tracker.metrics.over_budget_periods, 20);
        assert_eq!(tracker.metrics.over_budget_ms, 420);
        assert_eq!(tracker.metrics.over_budget_streak_ms, 0);
        assert!(!tracker.incident_logged);

        for _ in 0..24 {
            tracker.mark_period(96_001);
        }
        assert_eq!(tracker.metrics.over_budget_streak_ms, 504);
        assert!(tracker.incident_logged);
        assert_eq!(tracker.incident_max_pending_frames, 96_001);

        tracker.mark_period(0);
        assert!(!tracker.incident_logged);
        assert_eq!(tracker.incident_max_pending_frames, 0);
    }

    #[test]
    fn tts_queue_tracker_logs_large_overshoot_without_waiting() {
        let mut tracker = TtsQueueTracker::new(96_000, 1024);

        let metrics = tracker.mark_period(120_000);

        assert!(metrics.over_budget);
        assert_eq!(metrics.over_budget_streak_ms, 21);
        assert!(tracker.incident_logged);
        assert_eq!(tracker.incident_max_pending_frames, 120_000);
    }

    #[test]
    fn tts_flush_bypasses_audio_backpressure() {
        let (_tx, rx) = mpsc::sync_channel::<QueuedTtsCommand>(1);
        let (flush_tx, flush_rx) = mpsc::sync_channel(1);
        let mut core = OutputCore::new(1024, 99);
        let mut gain = MAX_TTS_GAIN_DB;
        let mut active_epoch = 0u64;
        core.enqueue_assistant_segment(
            None,
            SegmentKind::Assistant,
            MAX_TTS_GAIN_DB,
            vec![0; (MAX_PENDING_ASSISTANT_FRAMES as usize) * (CHANNELS as usize)],
        );
        assert_eq!(
            core.pending_assistant_frames(),
            MAX_PENDING_ASSISTANT_FRAMES
        );

        flush_tx.send(1).unwrap();
        drain_tts_commands(&rx, &flush_rx, &mut core, &mut gain, &mut active_epoch);

        assert_eq!(core.pending_assistant_frames(), 0);
        assert_eq!(active_epoch, 1);
    }

    #[test]
    fn tts_flush_discards_stale_audio_already_in_command_queue() {
        let (tx, rx) = mpsc::sync_channel(4);
        let (flush_tx, flush_rx) = mpsc::sync_channel(1);
        let mut core = OutputCore::new(1024, 99);
        let mut gain = MAX_TTS_GAIN_DB;
        let mut active_epoch = 0u64;

        tx.send(QueuedTtsCommand {
            epoch: 0,
            command: TtsCommand::Audio(vec![1; 2 * (CHANNELS as usize)]),
        })
        .unwrap();
        flush_tx.send(1).unwrap();
        tx.send(QueuedTtsCommand {
            epoch: 1,
            command: TtsCommand::Audio(vec![2; 3 * (CHANNELS as usize)]),
        })
        .unwrap();

        drain_tts_commands(&rx, &flush_rx, &mut core, &mut gain, &mut active_epoch);

        assert_eq!(core.pending_assistant_frames(), 3);
        assert_eq!(active_epoch, 1);
    }

    #[test]
    fn tts_future_epoch_audio_does_not_advance_without_observed_flush() {
        let (tx, rx) = mpsc::sync_channel(4);
        let (_flush_tx, flush_rx) = mpsc::sync_channel(1);
        let mut core = OutputCore::new(1024, 99);
        let mut gain = MAX_TTS_GAIN_DB;
        let mut active_epoch = 0u64;

        tx.send(QueuedTtsCommand {
            epoch: 1,
            command: TtsCommand::Audio(vec![1; 2 * (CHANNELS as usize)]),
        })
        .unwrap();

        drain_tts_commands(&rx, &flush_rx, &mut core, &mut gain, &mut active_epoch);

        assert_eq!(core.pending_assistant_frames(), 0);
        assert_eq!(active_epoch, 0);
    }

    #[test]
    fn tts_audio_enqueue_is_drop_not_block_when_command_queue_is_full() {
        let (tx, rx) = mpsc::sync_channel(1);

        assert!(try_enqueue_tts_command(
            &tx,
            QueuedTtsCommand {
                epoch: 0,
                command: TtsCommand::Audio(vec![1; 2 * (CHANNELS as usize)]),
            },
        ));
        assert!(try_enqueue_tts_command(
            &tx,
            QueuedTtsCommand {
                epoch: 0,
                command: TtsCommand::Audio(vec![2; 2 * (CHANNELS as usize)]),
            },
        ));

        let first = rx.try_recv().unwrap();
        assert_eq!(first.epoch, 0);
        assert!(matches!(first.command, TtsCommand::Audio(_)));
        assert!(rx.try_recv().is_err());
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
