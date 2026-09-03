// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! jasper-outputd - final-output owner.
//!
//! The default binary mode remains fake so `jasper-outputd --once` is
//! safe in a developer shell. The systemd unit opts into the real ALSA
//! transport with `JASPER_OUTPUTD_BACKEND=alsa`, reading CamillaDSP's
//! post-DSP program off the SHM ring — outputd's one upstream (ADR-0100) —
//! and writing the DAC directly.

use std::io::{self, Write};
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
    open_playback_pcm, prime_periods, AlsaBackend, FinalSinkStartupConfigError, IoCounters,
    NegotiatedPcm, PairedCompositeSink,
};
use jasper_outputd::config::{BackendMode, Config, SinkMode, DAC_CONTENT_RING_SLOTS};
use jasper_outputd::content_fill::{ContentFill, ContentFillEdge};
use jasper_outputd::core::{OutputCore, PeriodReport};
use jasper_outputd::dac_content::DacContentSource;
use jasper_outputd::shm_ring_source::ShmRingSource;
use jasper_outputd::state::{ChipRefWrite, OutputdState, StateServer};
use jasper_outputd::tts::{spawn_tts_server, tts_channels, TtsBridge};
use jasper_outputd::types::{narrow_period, ProgramSample, SampleFormat};
use jasper_outputd::{CHANNELS, SAMPLE_RATE};
use signal_hook::consts::signal::{SIGINT, SIGTERM};
use signal_hook::flag;

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

/// Exit code for a CONFIG-validation failure (sysexits.h EX_CONFIG).
/// The unit pairs it with `RestartPreventExitStatus=78`: a fail-closed
/// config rejection PARKS the unit failed (visible on /state + doctor)
/// instead of crash-looping — restarting cannot fix bad config, and on
/// this unit the loop escalates to StartLimitAction=reboot. Measured
/// incident (jts3, 2026-06-11): a grouping env + lab retune layered into
/// a guard-rejected combination; outputd crash-looped into THREE Pi
/// reboots before the T5.1 boot-loop guard contained it.
const EXIT_CONFIG: i32 = 78;

/// Marker attached (via `anyhow::Context`) to a runtime error that is actually
/// a CONFIG-class fault surfacing after `Config::from_env`. `main` downcasts for
/// it and exits [`EXIT_CONFIG`] so the unit parks instead of reboot-looping.
///
/// ONE site attaches it — the SHM content ring's construction — and only to the
/// config-class half of what that can return: a ring geometry/format/channel
/// declaration this reader refuses (the `S24_3LE` wire the ring layout has no
/// format for, refused before the filesystem is touched) or one the writer's
/// existing ring disagrees with field-by-field, which is only visible once
/// outputd attaches at DAC-loop setup. Both are `InvalidInput`/`InvalidData`,
/// and restarting cannot repair either.
///
/// Everything ELSE attach can fail with — a missing ring directory, EACCES,
/// ENOSPC, ENOMEM, a bare OS error — stays an ordinary error and takes
/// `Restart=on-failure`, because those CAN clear without an operator editing
/// env. `alsa_backend::FinalSinkStartupConfigError` is the same treatment for
/// the DAC edge, and `main` maps both to [`EXIT_CONFIG`].
#[derive(Debug)]
struct ConfigClassError;

impl std::fmt::Display for ConfigClassError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "config-class startup fault (park, do not restart-loop)")
    }
}

impl std::error::Error for ConfigClassError {}

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
            let mut core = OutputCore::new_for_daemon(config.period_frames);
            run_fake(&config, &mut core, &state, once, &shutdown)
        }
        BackendMode::Alsa => run_alsa(&config, &state, once, &shutdown),
    };

    notify_systemd("STOPPING=1")?;
    // A config-class fault surfacing after startup exits EX_CONFIG so the unit
    // parks (RestartPreventExitStatus=78) rather than reboot-looping. This
    // includes both late SHM geometry validation and initial final-sink
    // open/negotiation; neither can be repaired by restarting the daemon.
    if let Err(e) = &result {
        if let Some(exit_code) = runtime_error_exit_code(e) {
            eprintln!("event=outputd.config_invalid_runtime detail={e:#}");
            std::process::exit(exit_code);
        }
    }
    result
}

/// Decide whether a failed SHM-ring attach parks the unit or lets it restart,
/// and log the outcome under an event that says which one happened.
///
/// The split is the error KIND, because that is what separates "an operator has
/// to change something" from "the environment can recover on its own":
///
/// * `InvalidInput` / `InvalidData` — the declaration is unusable (`S24_3LE`) or
///   disagrees with the writer's ring on a geometry field. Marked
///   [`ConfigClassError`] -> exit 78 -> `RestartPreventExitStatus=78` parks the
///   unit, because a restart re-reads the SAME env against the SAME ring.
/// * anything else — `WouldBlock`, `PermissionDenied`, `NotFound`,
///   `StorageFull`, `OutOfMemory`, a bare OS error — is left unmarked, so
///   `Restart=on-failure` (plus outputd's bounded `ExecStopPost` retry) gets to
///   try again. A ring directory that is not mounted yet, a tmpfs that is
///   momentarily full, a peer that has not created the ring: each clears
///   without an edit, and parking on them would need an operator to un-park a
///   box that fixed itself.
///
/// Two events rather than one, because the journal line is the readable surface
/// during a park and the two outcomes are not the same fault:
/// `outputd.<lane>.config_error` means parked-for-config, and
/// `outputd.<lane>.attach_error` carries `kind=` so a restart loop names what
/// it is retrying against.
///
/// `lane` names WHICH ring, since a box can carry more than one: `shm_ring` for
/// the central content hop, `dac_content` for a leader's return lane. The
/// central hop's event strings are unchanged by it. `EBUSY` — the SPSC guard
/// refusing a ring a live foreign reader still owns — lands in the restart arm
/// by this same kind split, which is right: the incumbent may exit.
fn classify_ring_attach_error(lane: &str, path: &str, e: io::Error) -> anyhow::Error {
    if matches!(
        e.kind(),
        io::ErrorKind::InvalidInput | io::ErrorKind::InvalidData
    ) {
        eprintln!("event=outputd.{lane}.config_error path={path} detail={e}");
        anyhow::Error::new(e).context(ConfigClassError)
    } else {
        eprintln!(
            "event=outputd.{lane}.attach_error path={path} kind={:?} detail={e}",
            e.kind()
        );
        anyhow::Error::new(e).context("attaching to an SHM content ring")
    }
}

fn runtime_error_exit_code(error: &anyhow::Error) -> Option<i32> {
    if error.downcast_ref::<ConfigClassError>().is_some()
        || error
            .downcast_ref::<FinalSinkStartupConfigError>()
            .is_some()
    {
        Some(EXIT_CONFIG)
    } else {
        None
    }
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

enum RuntimeAlsaSink {
    Single(AlsaBackend),
    Composite(PairedCompositeSink),
}

impl RuntimeAlsaSink {
    fn open(config: &Config) -> Result<Self> {
        match config.sink_mode {
            SinkMode::SingleAlsa => Ok(Self::Single(AlsaBackend::new(config)?)),
            SinkMode::Composite => Ok(Self::Composite(PairedCompositeSink::new(config)?)),
        }
    }

    fn content_channels(&self) -> u16 {
        match self {
            Self::Single(sink) => sink.channels(),
            Self::Composite(_) => CHANNELS * 2,
        }
    }

    fn content_negotiated(&self) -> NegotiatedPcm {
        match self {
            Self::Single(sink) => sink.content_negotiated,
            Self::Composite(sink) => sink.content_negotiated,
        }
    }

    fn dac_negotiated(&self) -> NegotiatedPcm {
        match self {
            Self::Single(sink) => sink.dac_negotiated,
            Self::Composite(sink) => sink.dac_negotiated,
        }
    }

    /// The sample format this sink's final edge actually negotiated.
    ///
    /// Both transports answer from their own readback-verified state — the
    /// composite from its `ChildPeriods`, which is the width its write path
    /// actually writes. There is no hardcoded arm left here: while there was
    /// one, a composite that negotiated any other width would have reported
    /// `S16_LE` to STATUS and to the chip-AEC alignment identity regardless.
    fn dac_format(&self) -> SampleFormat {
        match self {
            Self::Single(sink) => sink.dac_format(),
            Self::Composite(sink) => sink.dac_format(),
        }
    }

    fn counters(&self) -> IoCounters {
        match self {
            Self::Single(sink) => sink.counters(),
            Self::Composite(sink) => sink.counters(),
        }
    }

    fn write_period(&mut self, samples: &[ProgramSample]) -> Result<()> {
        match self {
            Self::Single(sink) => sink.write_dac_period(samples),
            Self::Composite(sink) => sink.write_dual_period(samples),
        }
    }

    fn start(&self) -> Result<()> {
        match self {
            Self::Single(sink) => sink.start_dac(),
            Self::Composite(sink) => sink.start_dacs(),
        }
    }

    fn dac_delay_frames(&self) -> Result<u64> {
        match self {
            Self::Single(sink) => sink.dac_delay_frames(),
            Self::Composite(sink) => sink.dac_delay_frames(),
        }
    }

    fn mark_runtime_status(&self, state: &OutputdState) {
        if let Self::Composite(sink) = self {
            state.mark_dual_apple_status(&sink.dual_status());
        }
    }

    fn prime_context(&self) -> &'static str {
        match self {
            Self::Single(_) => "priming outputd DAC with silence",
            Self::Composite(_) => "priming dual Apple DACs with silence",
        }
    }

    fn primed_event(&self) -> &'static str {
        match self {
            Self::Single(_) => "outputd.alsa.primed",
            Self::Composite(_) => "outputd.dual_apple.primed",
        }
    }

    fn dac_delay_unavailable_event(&self) -> &'static str {
        match self {
            Self::Single(_) => "outputd.dac_delay_unavailable",
            Self::Composite(_) => "outputd.dual_apple.dac_delay_unavailable",
        }
    }

    fn is_composite(&self) -> bool {
        matches!(self, Self::Composite(_))
    }
}

fn state_counters(sink: &RuntimeAlsaSink) -> IoCounters {
    sink.counters()
}

fn run_alsa(
    config: &Config,
    state: &Arc<OutputdState>,
    once: bool,
    shutdown: &Arc<AtomicBool>,
) -> Result<()> {
    let mut sink = RuntimeAlsaSink::open(config)?;
    // Reference publication is a side output of the final DAC path. A missing
    // AEC consumer must degrade that consumer, never prevent the speaker from
    // reaching READY or writing the physical DAC.
    let mut ref_outputs = ReferenceSideOutputs::new(config, shutdown, Arc::clone(state));
    state.set_negotiated(sink.content_negotiated(), sink.dac_negotiated());
    // The final edge is open and its format is readback-verified: STATUS
    // `dac.format` stops echoing the declaration and reports what is running.
    state.set_dac_format(sink.dac_format());
    sink.mark_runtime_status(state);
    // Content/DAC width is carried as data (coherent single DAC of any width);
    // the published reference is always stereo (a wide sink folds to L == R).
    let content_channels = sink.content_channels() as usize;
    let content_period_samples = (config.period_frames as usize) * content_channels;
    // The program spine: one i32 period, ingested wide and written wide, with
    // the single quantization happening inside the sink at the DAC edge.
    let mut content_buf = vec![0 as ProgramSample; content_period_samples];
    let mut reference_buf =
        vec![0 as ProgramSample; (config.period_frames as usize) * (CHANNELS as usize)];
    // Ring B: the SHM ping-pong ring content source — the one central
    // transport from CamillaDSP to the DAC (ADR-0100).
    // Constructed ONLY when the CENTRAL ring is the resolved content source —
    // JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring, or an undeclared bridge on a box
    // whose round-trip marker is not armed;
    // None leaves the loop byte-identical. A declaration this reader refuses, or
    // a geometry/version/size disagreement with the writer's existing ring, is a
    // hard error here — mapped to the config-class exit 78 so the unit parks
    // (RestartPreventExitStatus=78) instead of reboot-looping on a mismatched
    // writer. Any OTHER attach failure keeps Restart=on-failure (see
    // `ConfigClassError`).
    //
    // The ring's wire is a per-box DECLARATION, taken from the same
    // `JASPER_OUTPUTD_CONTENT_*` names the reconciler renders the ring's other
    // ends from — so outputd declares one tuple and the crate's field-by-field
    // attach is the whole negotiation. The reader owns any staging its wire
    // needs (see `ShmRingSource`), so a box that resolved some OTHER source —
    // a `direct` declaration, or an armed round-trip marker — allocates nothing
    // for it, which matters under `mlockall`.
    let mut shm_ring = match config.shm_ring.as_ref() {
        Some(ring) => {
            eprintln!(
                "event=outputd.shm_ring.enabled path={} slots={} slot_frames={} channels={} format={} sample_rate={}",
                ring.path,
                ring.n_slots,
                config.period_frames,
                config.content_channels,
                config.content_format.as_str(),
                config.sample_rate,
            );
            let src = ShmRingSource::new(
                &ring.path,
                config.period_frames,
                config.content_channels,
                config.content_format,
                ring.n_slots,
            )
            .map_err(|e| classify_ring_attach_error("shm_ring", &ring.path, e))?;
            Some(src)
        }
        None => None,
    };
    if let Some(src) = shm_ring.as_ref() {
        state.mark_shm_ring(src.metrics());
        state.mark_shm_ring_wire(src.wire_format(), src.channels());
    }
    // The multi-room round-trip lane: on a grouping LEADER the DAC is fed from
    // the bond's shared program coming back out of the sync engine, so the
    // leader is sample-locked with its members. An armed lane IS the content
    // source — it never falls back, and a period it cannot fill is silence
    // (D4). `Config::from_env` has already refused both transports at once, so
    // at most one arm is built here; neither armed (solo) leaves this loop
    // byte-identical to before.
    let dac_content_lane = config
        .dac_content_ring
        .as_deref()
        .map(|path| ("ring", path))
        .or_else(|| {
            config
                .dac_content_fifo
                .as_deref()
                .map(|path| ("fifo", path))
        });
    let mut dac_content = match dac_content_lane {
        Some((transport, path)) => {
            eprintln!(
                "event=outputd.dac_content.enabled transport={} path={} channel={} \
                 main_highpass_hz={}",
                transport,
                path,
                config.dac_content_channel.as_str(),
                config
                    .dac_content_highpass_hz
                    .map(|v| format!("{v:.1}"))
                    .unwrap_or_else(|| "none".to_string()),
            );
            Some(if transport == "ring" {
                DacContentSource::ring(
                    path,
                    config.dac_content_channel,
                    config.period_frames,
                    DAC_CONTENT_RING_SLOTS,
                    config.dac_content_highpass_hz,
                )
                .map_err(|e| classify_ring_attach_error("dac_content", path, e))?
            } else {
                DacContentSource::fifo(
                    path,
                    config.dac_content_channel,
                    config.period_frames,
                    config.dac_content_highpass_hz,
                )
            })
        }
        None => None,
    };
    // Bonded-member TTS (Increment 5 PR-2): constructed ONLY when the
    // reconciler set the socket env — solo keeps fanin-owned TTS and
    // this loop stays byte-identical. The OutputCore engine (assistant
    // segments, loudness, saturating mix, the DAC-true PlayoutLedger)
    // mixes voice at the FINAL output stage: downstream of the
    // round-trip, upstream of the reference publish (inv-A).
    // Kept alive for the run so the assistant-reference writer thread's channel
    // sender (held inside OutputCore) has a live receiver; dropped at shutdown.
    let mut _assistant_reference_writer: Option<thread::JoinHandle<()>> = None;
    let mut tts: Option<(OutputCore, TtsBridge)> = if let Some(path) = &config.tts_socket_path {
        let (tx, rx, flush_tx, flush_rx, metrics, epoch) =
            tts_channels(config.tts_max_pending_frames);
        spawn_tts_server(PathBuf::from(path), tx, flush_tx, epoch, metrics.clone())?;
        state.set_tts(path.clone(), metrics.clone());
        eprintln!(
            "event=outputd.tts.enabled socket={} budget_frames={} program_duck_db={}",
            path, config.tts_max_pending_frames, config.tts_program_duck_db,
        );
        let mut core = OutputCore::new_for_daemon(config.period_frames);
        // Publish the assistant-loudness snapshot into the shared STATUS cell
        // each period (the same shape fan-in exposes under tts.assistant_loudness).
        core.set_loudness_sink(metrics.loudness_cell());
        // Learned quiet-room assistant reference: load the last value and arm
        // the persistence writer, mirroring fan-in. Best-effort — a writer
        // failure degrades to no learning and never blocks final output.
        let reference = jasper_outputd::assistant_reference::load(std::path::Path::new(
            &config.assistant_reference_path,
        ));
        core.set_held_assistant_reference(reference);
        match jasper_outputd::assistant_reference::spawn_writer(
            PathBuf::from(&config.assistant_reference_path),
            reference,
        ) {
            Ok((reference_tx, handle)) => {
                core.set_assistant_reference_sink(reference_tx);
                _assistant_reference_writer = Some(handle);
            }
            Err(e) => eprintln!(
                "event=outputd.assistant_reference.writer_unavailable path={} detail={e}",
                config.assistant_reference_path,
            ),
        }
        let bridge = TtsBridge::new(rx, flush_rx, metrics, config.tts_program_duck_db);
        Some((core, bridge))
    } else {
        None
    };
    // Pair-balance trim: a runtime-adjustable linear gain on the round-trip
    // content path (FIFO and inv-B fallback periods alike — no level jump on a
    // starvation transition). <= 0 dB is enforced at config/control parse, so
    // this can only attenuate.
    let zero_period = vec![0 as ProgramSample; content_period_samples];
    let dac_negotiated = sink.dac_negotiated();
    // Which source is live is fixed for the run (the loop below consults
    // exactly one). A box with NEITHER parks on its first period, before this
    // ever observes anything.
    let mut content_fill = ContentFill::new(
        if dac_content.is_some() {
            "dac_content"
        } else {
            "shm_ring"
        },
        dac_negotiated,
    );
    let prime_periods = prime_periods(dac_negotiated.buffer_frames, dac_negotiated.period_frames);
    for _ in 0..prime_periods {
        sink.write_period(&zero_period)
            .context(sink.prime_context())?;
    }
    sink.start()?;
    eprintln!(
        "event={} prime_periods={} buffer_frames={} period_frames={}",
        sink.primed_event(),
        prime_periods,
        dac_negotiated.buffer_frames,
        dac_negotiated.period_frames,
    );
    notify_ready(config)?;

    let watchdog_interval = watchdog_interval();
    let mut last_watchdog = Instant::now();
    let mut dac_delay_warning_logged = false;
    let mut reference_sequence = 0u64;

    while !shutdown.load(Ordering::Relaxed) {
        let period_clipped_samples: u32;
        // Did THIS period carry content, or did the live source zero-fill it?
        // Whichever arm below runs answers, in its own vocabulary (#3458).
        let mut period_served = false;
        // An armed round-trip lane owns the content period outright: it fills
        // `content_buf` with the bond's program or with silence, so there is no
        // second source to consult and the arms below belong to a box with no
        // lane at all. Metrics are published BEFORE the fatal-fault `?` can
        // propagate, so `/state`'s last sample stays honest.
        let served_from_dac_content = match dac_content.as_mut() {
            Some(src) => {
                let filled = src.fill_period(&mut content_buf);
                let metrics = src.metrics();
                state.mark_dac_content(metrics);
                filled?;
                period_served = metrics.serving_fifo;
                true
            }
            None => false,
        };
        if !served_from_dac_content {
            if let Some(src) = shm_ring.as_mut() {
                // Try-read one slot from the SHM ping-pong ring;
                // empty -> silence (read_period zero-fills). Never blocks, and a
                // ring fault is never a runtime error (it degrades to silence +
                // counters — StartLimitAction=reboot discipline).
                //
                // The reader delivers onto the spine at its own wire's width —
                // an S16 ring widens through the same shared door every other
                // S16 ingress uses, an S32 ring is already the spine's width and
                // copies straight in. The `?` is the staging-length contract the
                // widening always carried, unmoved — and, as before, the ring's
                // counters for THIS period are published before it can
                // propagate, so a fatal staging fault still leaves `/state`'s
                // last sample honest.
                let read = src.read_period(&mut content_buf);
                state.mark_shm_ring(src.metrics());
                period_served = read? > 0;
            } else {
                // ADR-0100: the ring-fed CamillaDSP chain is the only upstream
                // outputd serves. A box that reaches here declared
                // `JASPER_OUTPUTD_CONTENT_BRIDGE=direct`, so no ring was
                // attached and there is nothing else to read — it PARKS rather
                // than playing silence. Park-class (EX_CONFIG 78) because no
                // restart can change a declaration. Which NAMED park it is
                // comes from `jasper.control.transport_park` (ADR-0178).
                return Err(anyhow::anyhow!(
                    "PARKED: no content upstream. \
                     JASPER_OUTPUTD_CONTENT_BRIDGE=direct names the retired \
                     snd-aloop route, which no longer exists, so outputd \
                     attached no ring and has nothing to read. The SHM ring is \
                     the one transport (ADR-0100): remove the stale \
                     JASPER_OUTPUTD_CONTENT_BRIDGE line from \
                     /var/lib/jasper/outputd.env — an UNDECLARED bridge is the \
                     ring — or set it to shm_ring."
                )
                .context(FinalSinkStartupConfigError));
            }
        }
        match content_fill.observe(period_served) {
            Some(ContentFillEdge::Deaf) => eprintln!(
                "event=outputd.content.deaf source={} threshold_periods={} \
                 action=emit_silence",
                content_fill.source(),
                content_fill.deaf_threshold_periods(),
            ),
            Some(ContentFillEdge::Recovered { empty_periods }) => eprintln!(
                "event=outputd.content.recovered source={} empty_periods={}",
                content_fill.source(),
                empty_periods,
            ),
            None => {}
        }
        state.mark_content_fill(
            content_fill.consecutive_empty_periods(),
            content_fill.deaf(),
        );
        let trim = if dac_content.is_some() {
            state.dac_content_trim_gain()
        } else {
            1.0
        };
        if trim < 1.0 {
            // Before duck/mix/publish so the AEC reference carries the
            // trimmed program too (inv-A: reference == final DAC content).
            apply_linear_gain(&mut content_buf, f64::from(trim));
        }
        if let Some((core, bridge)) = tts.as_mut() {
            // The TTS-enabled path: voice mixes via the engine. Duck is
            // applied to the CONTENT before the mix so the reference
            // carries the ducked program too (inv-A).
            bridge.drain(core);
            if let Some(gain) = bridge.content_duck_gain() {
                apply_linear_gain(&mut content_buf, f64::from(gain));
            }
            core.prepare_period_with_content(&content_buf);
            sink.write_period(core.output_period())?;
            sink.mark_runtime_status(state);
            let dac_delay_frames = match sink.dac_delay_frames() {
                Ok(frames) => {
                    state.mark_dac_delay(frames);
                    frames
                }
                Err(e) => {
                    if !dac_delay_warning_logged {
                        eprintln!("event={} detail={e:#}", sink.dac_delay_unavailable_event());
                        dac_delay_warning_logged = true;
                    }
                    sink.dac_negotiated().buffer_frames as u64
                }
            };
            // The ledger drains against ACTUAL DAC progress — the honest
            // max_audio_played_ms barge-in has never had from fanin.
            let report = core.commit_prepared_period_with_dac_delay(dac_delay_frames);
            reference_sequence = report.reference_sequence;
            // NO FOLD HERE, and none is needed (#2627). The `else` branch below
            // folds a wide sink's program down to the stereo :9891 wire because
            // its period can be wider than two channels; this period never can.
            // Local TTS and a wide/roleful sink are MUTUALLY EXCLUSIVE BY
            // CONFIG, enforced loudly at startup: `config.rs` bails when
            // `tts_socket_path.is_some() && !is_full_range_stereo_lr_sink`, and
            // that predicate is `sink_mode == SinkMode::SingleAlsa
            // && content_channels == 2 && !active_lane`. So reaching this line
            // at all proves the sink is a full-range stereo L/R single-ALSA one
            // and `core.output_period()` is already the wire's own width —
            // exactly like the `content_channels == CHANNELS` byte-identical
            // arm below. A width mismatch would be a config-gate regression,
            // not a case to handle here.
            ref_outputs.publish(core.output_period(), reference_sequence);
            period_clipped_samples = report.clipped_samples;
            state.mark_period(
                state_counters(&sink),
                reference_sequence,
                report.clipped_samples,
            );
        } else {
            sink.write_period(&content_buf)?;
            sink.mark_runtime_status(state);
            let _dac_delay_frames = match sink.dac_delay_frames() {
                Ok(frames) => {
                    state.mark_dac_delay(frames);
                    frames
                }
                Err(e) => {
                    if !dac_delay_warning_logged {
                        eprintln!("event={} detail={e:#}", sink.dac_delay_unavailable_event());
                        dac_delay_warning_logged = true;
                    }
                    sink.dac_negotiated().buffer_frames as u64
                }
            };
            // Real clip accounting (replaces the hardwired 0): the passthrough
            // never clips, so a full-scale sample means CamillaDSP hit the
            // ceiling upstream — the honest signal the Stage-6 no-clip gate
            // needs (it was vacuously green against a hardwired 0).
            let clipped = count_full_scale_samples(&content_buf);
            let next_reference_sequence = reference_sequence.saturating_add(1);
            if content_channels == CHANNELS as usize {
                // Byte-identical stereo path: the content IS the reference.
                ref_outputs.publish(&content_buf, next_reference_sequence);
            } else if sink.is_composite() {
                // Existing composite monitor contract: pairwise child averages.
                fold_reference_pairwise_composite(&content_buf, &mut reference_buf);
                ref_outputs.publish(&reference_buf, next_reference_sequence);
            } else {
                // Wide sink: fold the driven lanes to the stereo reference.
                fold_reference(&content_buf, content_channels, &mut reference_buf);
                ref_outputs.publish(&reference_buf, next_reference_sequence);
            }
            reference_sequence = next_reference_sequence;
            period_clipped_samples = clipped;
            state.mark_period(state_counters(&sink), reference_sequence, clipped);
        }
        if once {
            eprintln!(
                "event=outputd.once frames_written={} reference_sequence={} clipped_samples={}",
                sink.counters().dac_frames_written,
                reference_sequence,
                period_clipped_samples,
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

fn fold_reference_pairwise_composite(
    samples_4ch: &[ProgramSample],
    out_stereo: &mut [ProgramSample],
) {
    assert_eq!(samples_4ch.len() % 4, 0);
    assert_eq!(
        out_stereo.len(),
        (samples_4ch.len() / 4) * (CHANNELS as usize)
    );
    for (frame, out) in samples_4ch
        .chunks_exact(4)
        .zip(out_stereo.chunks_exact_mut(2))
    {
        out[0] = average_pair(frame[0], frame[1]);
        out[1] = average_pair(frame[2], frame[3]);
    }
}

/// Clip-proof average of two program samples: accumulate in **i64**, halve, back
/// to the spine.
///
/// Renamed from `average_i16` because the width moved. i64 is load-bearing at
/// this width in a way it was not at i16: two i32 samples sum past the i32 rail,
/// so an i32 accumulator would WRAP a correlated full-scale pair to the opposite
/// rail — the loudest defect a fold can produce, and inaudible in a small
/// fixture. `pairwise_composite_average_cannot_overflow_at_the_spine_rails`
/// pins it.
fn average_pair(a: ProgramSample, b: ProgramSample) -> ProgramSample {
    (((a as i64) + (b as i64)) / 2) as ProgramSample
}

/// Fold an N-channel coherent-single DAC content period into the published
/// stereo reference (L == R) as a clip-proof 1/N mono sum of all driven lanes.
///
/// Both AEC consumers collapse the reference to mono (software AEC3 sums
/// L+R -> mono; the chip USB-IN producer downmixes), so a mono fold loses
/// nothing. Scale by 1/N (NOT 1/sqrt(N)): N correlated full-scale lanes sum to
/// N x full-scale, so 1/N keeps the result in range for ANY correlation. A
/// clipped reference is uniquely harmful — the linear AEC cannot model the
/// nonlinearity — so the conservative 1/N is deliberate; band-splitting keeps
/// the real reference well below the N x worst case, so the conservatism costs
/// no SNR.
///
/// Accumulate in **i64** to avoid intermediate overflow. At the i32 program spine
/// this is not a formality: N full-scale i32 lanes sum to N x the i32 rail, which
/// overflows i32 for every N >= 2, so an i32 accumulator would wrap the exact
/// worst case this function's 1/N scaling exists to survive. i64 holds N x i32
/// for any channel count this daemon can be configured with (the fold is bounded
/// by `content_channels`, at most 8 today, and i64 tolerates ~4e9 lanes).
fn fold_reference(
    content_nch: &[ProgramSample],
    channels: usize,
    out_stereo: &mut [ProgramSample],
) {
    debug_assert!(channels >= 1);
    debug_assert_eq!(content_nch.len() % channels, 0);
    debug_assert_eq!(
        out_stereo.len(),
        (content_nch.len() / channels) * (CHANNELS as usize)
    );
    for (frame, out) in content_nch
        .chunks_exact(channels)
        .zip(out_stereo.chunks_exact_mut(CHANNELS as usize))
    {
        let sum: i64 = frame.iter().map(|&s| s as i64).sum();
        let mono = (sum / (channels as i64)) as ProgramSample;
        out[0] = mono;
        out[1] = mono;
    }
}

/// One S16 LSB expressed at the program spine's scale — the width of the
/// full-scale detection band below.
const SPINE_S16_LSB: ProgramSample = 1 << 16;

/// Count samples at digital full-scale in a written period. outputd's active path
/// is a passthrough (CamillaDSP owns gain), so a healthy commissioned config
/// reports ~0; a full-scale sample is a saturation proxy meaning the content hit
/// the ceiling upstream.
///
/// "Full scale" is a band one S16 LSB wide at each rail, NOT equality with the
/// i32 rails, and that is the whole subtlety of moving this to the spine. A
/// widened S16 full-scale sample is `0x7FFF_0000` — one S16 LSB BELOW `i32::MAX`
/// — so an `s == i32::MAX` test would report 0 forever on every S16-content box
/// and quietly restore the vacuously-green Stage-6 no-clip gate this function was
/// written to fix. The band is symmetric by construction
/// (`[i32::MAX - 65535, i32::MAX]` and `[i32::MIN, i32::MIN + 65535]`) and
/// contains both the widened-S16 rails and the native-S32 rails, so the count is
/// unchanged on today's fleet and correct once the lane goes wide. The nearest
/// non-clipping S16 value, widened, is 65536 below the band and is not counted.
fn count_full_scale_samples(samples: &[ProgramSample]) -> u32 {
    samples
        .iter()
        .filter(|&&s| {
            s >= ProgramSample::MAX - (SPINE_S16_LSB - 1)
                || s <= ProgramSample::MIN + (SPINE_S16_LSB - 1)
        })
        .count() as u32
}

/// In-place linear attenuation for the program duck and the pair-balance trim
/// (gain <= 1.0, enforced at config/control parse, so no clipping is possible).
///
/// **f64, not f32 — this is not a style choice.** An f32 mantissa is 24 bits, so
/// `sample as f32` on an i32 sample discards the bottom 7 bits BEFORE the
/// multiply; every ducked or trimmed period would lose more resolution than the
/// wide spine was introduced to keep, and it would lose it on the two paths a
/// grouped member uses constantly. f64 represents every i32 exactly.
///
/// Rounds rather than truncating (the old i16 version's `as i16` cast truncated
/// toward zero). At duck depths either is inaudible; rounding is chosen because
/// the whole point of this change is that resolution is given up in exactly one
/// place, and this is not that place. The clamp is belt: with gain <= 1.0 the
/// product's magnitude cannot exceed the input's.
fn apply_linear_gain(samples: &mut [ProgramSample], gain: f64) {
    for s in samples.iter_mut() {
        *s = ((*s as f64) * gain)
            .round()
            .clamp(ProgramSample::MIN as f64, ProgramSample::MAX as f64)
            as ProgramSample;
    }
}

fn notify_ready(config: &Config) -> Result<()> {
    notify_systemd("READY=1").context("notifying systemd READY=1")?;
    eprintln!(
        "event=outputd.ready backend={} sink_mode={} period_frames={}",
        config.backend.as_str(),
        config.sink_mode.as_str(),
        config.period_frames
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
    chip_tx: Option<SyncSender<ChipRefPacket>>,
    chip_downsampler: Option<ChipRefDownsampler>,
    state: Arc<OutputdState>,
    /// The reference taps' S16 staging — narrowed ONCE per period from the
    /// program spine and shared by BOTH consumers.
    ///
    /// Both taps are **S16 by contract** (D8): the :9891 datagrams are what
    /// `jasper-aec-bridge` parses as `period_frames * 2ch * 2 bytes`, and the
    /// chip-reference leg is pinned 16 kHz / 2ch / S16_LE exactly by
    /// `validate_chip_ref_geometry`. One staging buffer, one narrow, so the UDP
    /// datagram and the chip-ref downsampler cannot diverge into two different
    /// quantizations of the same reference — inv-A is that the reference IS the
    /// final DAC content, and two roundings of it would be two references.
    ///
    /// Pre-sized at `new` from the configured period, so the audio path never
    /// allocates.
    narrow_staging: Vec<i16>,
}

impl ReferenceSideOutputs {
    fn new(config: &Config, shutdown: &Arc<AtomicBool>, state: Arc<OutputdState>) -> Self {
        let udp_target = match config.reference_udp_target.as_deref() {
            Some(raw) => match raw.parse::<SocketAddr>() {
                Ok(target) => Some(target),
                Err(e) => {
                    state.mark_reference_udp_error();
                    eprintln!(
                        "event=outputd.reference_udp.unavailable action=continue_without_reference reason=invalid_target target={raw:?} detail={e}"
                    );
                    None
                }
            },
            None => None,
        };
        let udp_socket = udp_target.and_then(|target| {
            let opened = (|| -> Result<UdpSocket> {
                let sock = UdpSocket::bind("127.0.0.1:0")
                    .context("binding outputd reference UDP sender")?;
                sock.set_nonblocking(true)
                    .context("setting outputd reference UDP sender nonblocking")?;
                Ok(sock)
            })();
            match opened {
                Ok(sock) => {
                    state.mark_reference_udp_active(true);
                    Some(sock)
                }
                Err(e) => {
                    state.mark_reference_udp_error();
                    eprintln!(
                        "event=outputd.reference_udp.unavailable action=continue_without_reference target={target} detail={e:#}"
                    );
                    None
                }
            }
        });

        let chip_downsampler = if config.chip_ref_pcm.is_some() {
            // Config::from_env has already validated this exact divisibility
            // invariant. Keep the side-output boundary non-fatal even if that
            // contract changes in a future build.
            match ChipRefDownsampler::new(config.sample_rate, config.chip_ref_sample_rate) {
                Ok(downsampler) => Some(downsampler),
                Err(e) => {
                    state.mark_chip_ref_open_error();
                    state.mark_chip_ref_terminal_failure();
                    eprintln!(
                        "event=outputd.chip_ref.unavailable action=continue_without_reference reason=downsampler_config detail={e:#}"
                    );
                    None
                }
            }
        } else {
            None
        };

        let chip_tx = if let (Some(_), Some(pcm_name)) =
            (&chip_downsampler, config.chip_ref_pcm.as_ref())
        {
            match spawn_chip_ref_writer(
                pcm_name.clone(),
                config.chip_ref_sample_rate,
                config.chip_ref_period_frames,
                config.chip_ref_buffer_frames,
                config.chip_ref_tee_path.clone(),
                Arc::clone(shutdown),
                Arc::clone(&state),
            ) {
                Ok(tx) => Some(tx),
                Err(e) => {
                    state.mark_chip_ref_open_error();
                    state.mark_chip_ref_terminal_failure();
                    eprintln!(
                        "event=outputd.chip_ref.unavailable action=continue_without_reference reason=worker_spawn_failed pcm={pcm_name} detail={e:#}"
                    );
                    None
                }
            }
        } else {
            None
        };

        if let (Some(_), Some(target)) = (&udp_socket, udp_target) {
            eprintln!("event=outputd.reference_udp.enabled target={target}");
        }
        if let Some(pcm) = &config.chip_ref_pcm {
            eprintln!("event=outputd.chip_ref.desired pcm={pcm}");
        }

        // Only pay for the tap staging when a tap will actually consume it. Same
        // reasoning as `run_alsa`'s `s16_lane_scratch`: outputd runs `mlockall`,
        // so an unconditional allocation is resident forever even on a box with
        // no AEC consumer and no chip-reference leg. `publish` returns before
        // touching this when both taps are absent, so the empty vec is never
        // resized either. Decided BEFORE the struct literal, which moves both.
        let narrow_staging = if udp_socket.is_some() || chip_tx.is_some() {
            vec![0i16; (config.period_frames as usize) * (CHANNELS as usize)]
        } else {
            Vec::new()
        };

        Self {
            udp_socket,
            udp_target,
            chip_tx,
            chip_downsampler,
            state,
            narrow_staging,
        }
    }

    /// Publish one program period to the reference taps, narrowing to their S16
    /// contract exactly once.
    ///
    /// A conversion failure here must NOT reach the audio path: the taps are side
    /// outputs, and "a missing AEC consumer degrades that consumer, never the
    /// speaker" is the rule this whole struct is built around (see `run_alsa`'s
    /// construction comment). So a staging-length mismatch logs and skips the
    /// period rather than propagating — see the comment at that branch for why it
    /// is unreachable and why it is kept anyway.
    fn publish(&mut self, stereo_samples: &[ProgramSample], reference_sequence: u64) {
        if self.udp_socket.is_none() && self.chip_tx.is_none() {
            return;
        }
        if self.narrow_staging.len() != stereo_samples.len() {
            // Steady state never resizes: `new` sized this for the configured
            // period, and the reference is always stereo whatever the sink's
            // width. A geometry that ever differs reallocates once, then stays.
            self.narrow_staging.resize(stereo_samples.len(), 0);
        }
        // Unreachable by construction: the resize immediately above makes the two
        // lengths equal, and that is the ONLY thing `narrow_period` rejects. Kept
        // rather than `expect()`-ed because this runs on outputd's realtime
        // playout thread — a panic there is a dead speaker, while a skipped
        // reference period only degrades the AEC consumer.
        //
        // It does NOT mark a UDP error. A length mismatch is not a UDP fault: it
        // would fail both taps identically, so pinning it to `reference_udp` would
        // point an operator at the wrong leg (and at a socket that is working).
        // The `event=` line is the whole observable, deliberately.
        if let Err(e) = narrow_period(stereo_samples, &mut self.narrow_staging) {
            eprintln!(
                "event=outputd.reference.narrow_failed action=skip_reference_period \
                 samples={} staging={} detail={e:#}",
                stereo_samples.len(),
                self.narrow_staging.len(),
            );
            return;
        }
        // ONE staging slice feeds BOTH taps below — see the field's doc.
        if let (Some(sock), Some(target)) = (&self.udp_socket, self.udp_target) {
            match sock.send_to(i16_bytes(&self.narrow_staging), target) {
                Ok(_) => self.state.mark_reference_udp_active(true),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    // Keep dropping — a blocking retry here would stall the
                    // playout thread and starve the DAC. The counter is the
                    // observable; a reader that trusts the tap's continuity
                    // has to see it (#3509).
                    self.state.mark_reference_udp_active(true);
                    self.state.mark_reference_udp_dropped();
                }
                Err(e) => {
                    self.state.mark_reference_udp_error();
                    eprintln!("event=outputd.reference_udp.send_failed detail={e}");
                }
            }
        }
        if let Some(tx) = &self.chip_tx {
            let dual_mono = self
                .chip_downsampler
                .as_mut()
                .expect("chip ref downsampler is present when chip_tx is present")
                .process(&self.narrow_staging);
            if dual_mono.is_empty() {
                return;
            }
            let frames = (dual_mono.len() / (CHANNELS as usize)) as u64;
            let packet = ChipRefPacket {
                samples: dual_mono,
                reference_sequence,
            };
            self.state.mark_chip_ref_queue_admitted(frames);
            let writer_disconnected = match tx.try_send(packet) {
                Ok(()) => {
                    self.state.mark_chip_ref_enqueued(reference_sequence);
                    false
                }
                Err(TrySendError::Full(_packet)) => {
                    self.state.mark_chip_ref_dequeued(frames);
                    self.state.mark_chip_ref_dropped_full();
                    false
                }
                Err(TrySendError::Disconnected(_packet)) => {
                    self.state.mark_chip_ref_dequeued(frames);
                    self.state.mark_chip_ref_dropped_disconnected();
                    self.state.mark_chip_ref_terminal_failure();
                    eprintln!("event=outputd.chip_ref.disconnected action=disable_reference_sink");
                    true
                }
            };
            if writer_disconnected {
                self.chip_tx = None;
                self.chip_downsampler = None;
            }
        }
    }
}

#[derive(Debug)]
struct ChipRefPacket {
    samples: Vec<i16>,
    reference_sequence: u64,
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

    /// KNOWN ALLOCATION EXCEPTION on the playout thread — pre-existing, unchanged
    /// by the i32 spine, and deliberately left alone here.
    ///
    /// This allocates one `Vec` per period (and `ChipRefPacket` then moves it to
    /// the writer thread), so it is the one place `test_outputd_wiring.py`'s
    /// no-allocation guard cannot cover. It costs a ~107-sample allocation per
    /// period, and ONLY on boxes with the chip-reference leg armed
    /// (`chip_ref_pcm` set) — the same boxes that already pay a channel send and a
    /// second thread for it. Fixing it means giving the writer a pool or a
    /// pre-sized ring, which changes the queue's ownership model: out of scope for
    /// the spine widening, and it must not be silently folded in.
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

/// Reinterpret an S16 slice as its little-endian wire bytes.
///
/// **Deliberately monomorphic in `i16`, and that is the point.** It replaces a
/// type-adaptive `bytemuck_i16<T>`-shaped helper whose name promised i16 while
/// its body would happily accept `&[i32]` and emit TWICE the bytes — with the
/// program spine now i32, calling that helper on a spine slice would have sent
/// 2x-length datagrams to `jasper-aec-bridge` and written 2x bytes to the
/// chip-ref tee, silently, with every counter still reporting success. The
/// signature is the guard: a spine slice does not compile here.
/// `the_reference_datagram_is_exactly_one_s16_stereo_period` pins the resulting
/// wire length.
fn i16_bytes(samples: &[i16]) -> &[u8] {
    unsafe {
        std::slice::from_raw_parts(
            samples.as_ptr() as *const u8,
            std::mem::size_of_val(samples),
        )
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

fn spawn_chip_ref_writer(
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
        .stack_size(jasper_outputd::HELPER_STACK_BYTES)
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
    let mut tee = open_chip_ref_tee(config.tee_path, state);
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
                write_chip_ref_tee(&mut tee, &packet.samples, state);
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

fn open_chip_ref_tee(path: Option<&str>, state: &OutputdState) -> Option<std::fs::File> {
    let path = path?;
    match std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)
    {
        Ok(file) => {
            eprintln!("event=outputd.chip_ref.tee.enabled path={path}");
            state.mark_chip_ref_tee_opened();
            Some(file)
        }
        Err(e) => {
            eprintln!("event=outputd.chip_ref.tee.open_failed path={path} detail={e}");
            state.mark_chip_ref_tee_open_error();
            None
        }
    }
}

fn write_chip_ref_tee(tee: &mut Option<std::fs::File>, samples: &[i16], state: &OutputdState) {
    let Some(file) = tee.as_mut() else {
        return;
    };
    if let Err(e) = file.write_all(i16_bytes(samples)) {
        eprintln!("event=outputd.chip_ref.tee.write_failed detail={e}");
        state.mark_chip_ref_tee_write_error();
        *tee = None;
    }
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
        .stack_size(jasper_outputd::HELPER_STACK_BYTES)
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

#[cfg(test)]
mod tests {
    use super::*;
    // Only the test `Config` literal names it now that the run loop's ring
    // staging moved into `ShmRingSource`; importing it at module scope would
    // be an unused import in a non-test build.
    use jasper_outputd::config::ContentBridgeMode;
    use std::os::fd::FromRawFd;
    use std::sync::atomic::AtomicUsize;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn final_sink_startup_fault_uses_configuration_exit() {
        let error =
            anyhow::anyhow!("synthetic sink open failure").context(FinalSinkStartupConfigError);

        assert_eq!(runtime_error_exit_code(&error), Some(EXIT_CONFIG));
        assert_eq!(
            runtime_error_exit_code(&anyhow::anyhow!("transient runtime fault")),
            None
        );
    }

    /// A ring WIRE mismatch parks the unit; it does not restart-loop it.
    ///
    /// Ring v2 gave the ring geometry two per-box axes (format, channels), so a
    /// writer and this reader can now disagree on a field that no slot/period
    /// check would notice. This walks a REAL such disagreement — an S16 ring
    /// file against an S32 declaration — through the SAME
    /// `classify_shm_ring_attach_error` the run loop applies to it, and pins the
    /// exit code at 78, which is what `RestartPreventExitStatus=78` in the unit
    /// turns into a park. Only the classifier is exercised here, not `run_alsa`
    /// (which needs ALSA) — the same bound the sink-startup test above has.
    #[test]
    fn a_ring_wire_mismatch_classifies_as_a_config_fault_not_a_restart() {
        use jasper_ring::{Geometry, TestRingWriter, SAMPLE_FORMAT_S16LE};

        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "jasper-outputd-ring-wire-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("content.ring").to_string_lossy().into_owned();

        // A narrow ring on disk...
        let _writer = TestRingWriter::create_or_attach(
            &path,
            Geometry {
                rate: 48_000,
                channels: 2,
                sample_format: SAMPLE_FORMAT_S16LE,
                period_frames: 128,
                n_slots: 2,
            },
        )
        .unwrap();
        // ...and a wide declaration meeting it.
        let io_error = match ShmRingSource::new(&path, 128, 2, SampleFormat::S32Le, 2) {
            Ok(_) => panic!("an S32 declaration must not attach to an S16 ring"),
            Err(e) => e,
        };
        assert_eq!(io_error.kind(), io::ErrorKind::InvalidData);
        let error = classify_ring_attach_error("shm_ring", &path, io_error);

        assert_eq!(runtime_error_exit_code(&error), Some(EXIT_CONFIG));

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_dir(&dir);
    }

    /// An attach failure the ENVIRONMENT can clear is NOT a park.
    ///
    /// The other half of the classifier, and the reason it is a kind check
    /// rather than "anything `ShmRingSource::new` returns". Parking on these
    /// would strand a box that fixed itself: `WouldBlock` is a ring the peer has
    /// not finished creating, `PermissionDenied` is a `/dev/shm` mode that a
    /// tmpfiles/udev pass repairs — a restart re-attaches, an operator un-park
    /// is needed only for a bad declaration. `StorageFull`/`OutOfMemory`/a raw
    /// OS errno ride the same arm; two kinds are enough to pin the boundary, and
    /// a raw errno is included because it is the shape that arrives with NO
    /// mapped kind at all.
    ///
    /// It also pins that they take the other EVENT: a non-config fault logging
    /// `config_error` would read, in the journal, as a park that never happened.
    #[test]
    fn a_recoverable_attach_failure_restarts_rather_than_parking() {
        for kind in [
            io::ErrorKind::WouldBlock,
            io::ErrorKind::PermissionDenied,
            io::ErrorKind::StorageFull,
        ] {
            let error = classify_ring_attach_error(
                "shm_ring",
                "/dev/shm/jts-ring/content.ring",
                io::Error::new(kind, "synthetic attach failure"),
            );
            assert_eq!(
                runtime_error_exit_code(&error),
                None,
                "{kind:?} must not park the unit"
            );
        }

        // A bare errno with NO mapped `ErrorKind` (EIO): the shape a raw syscall
        // failure arrives in, and the one an `Uncategorized`-defaults-to-park
        // classifier would get wrong.
        let raw = classify_ring_attach_error(
            "shm_ring",
            "/dev/shm/jts-ring/content.ring",
            io::Error::from_raw_os_error(5),
        );
        assert_eq!(runtime_error_exit_code(&raw), None);

        // ...and the config-class kinds still do, so the assertion above is a
        // boundary rather than "the classifier marks nothing".
        for kind in [io::ErrorKind::InvalidInput, io::ErrorKind::InvalidData] {
            let error = classify_ring_attach_error(
                "shm_ring",
                "/dev/shm/jts-ring/content.ring",
                io::Error::new(kind, "synthetic declaration fault"),
            );
            assert_eq!(
                runtime_error_exit_code(&error),
                Some(EXIT_CONFIG),
                "{kind:?} must park the unit"
            );
        }
    }

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

    /// **The :9891 reference-datagram wire guard.**
    ///
    /// The :9891 reference datagram is what `jasper-aec-bridge` parses as one
    /// stereo S16 period — `period_frames * 2 channels * 2 bytes`. A
    /// type-adaptive byte helper handed the i32 program period would emit
    /// silently DOUBLE-length datagrams, with `mark_reference_udp_active(true)` and
    /// every counter still reporting success. The AEC consumer would
    /// mis-frame every period and the only symptom would be echo
    /// cancellation quietly getting worse.
    ///
    /// Two layers now stop that: `i16_bytes` is monomorphic so a spine slice does
    /// not compile, and this test binds a real loopback socket and measures what
    /// `publish` actually put on the wire.
    #[test]
    fn the_reference_datagram_is_exactly_one_s16_stereo_period() {
        const PERIOD_FRAMES: u32 = 128;
        let receiver = UdpSocket::bind("127.0.0.1:0").expect("bind reference receiver");
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let target = receiver.local_addr().unwrap();

        let config = Config {
            period_frames: PERIOD_FRAMES,
            reference_udp_target: Some(target.to_string()),
            // Keep this to the UDP tap alone: the chip-ref leg spawns a writer
            // thread against a PCM that does not exist here.
            chip_ref_pcm: None,
            ..chip_ref_test_config()
        };
        let state = Arc::new(OutputdState::new(&config));
        let shutdown = Arc::new(AtomicBool::new(false));
        let mut refs = ReferenceSideOutputs::new(&config, &shutdown, Arc::clone(&state));

        // A full-scale-ish program period at the SPINE's width.
        let period: Vec<ProgramSample> = (0..(PERIOD_FRAMES as usize) * (CHANNELS as usize))
            .map(|i| w((i as i16).wrapping_mul(211)))
            .collect();
        refs.publish(&period, 7);

        let mut buf = vec![0u8; 64 * 1024];
        let (len, _) = receiver.recv_from(&mut buf).expect("reference datagram");
        let expected = (PERIOD_FRAMES as usize) * (CHANNELS as usize) * 2;
        assert_eq!(
            len, expected,
            "the :9891 wire must stay one S16 stereo period: {len} bytes vs {expected}"
        );
        // Not merely the right LENGTH — the right SAMPLES. Decode the wire and
        // compare against the period narrowed to S16, so a datagram that happened
        // to be the correct size but carried the wrong bytes still fails.
        let decoded: Vec<i16> = buf[..len]
            .chunks_exact(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .collect();
        let mut want = vec![0i16; period.len()];
        narrow_period(&period, &mut want).unwrap();
        assert_eq!(decoded, want);
        // And the spine period the caller handed in is untouched (inv-A: the
        // published reference and the DAC content are the same audio).
        let unchanged: Vec<ProgramSample> = (0..(PERIOD_FRAMES as usize) * (CHANNELS as usize))
            .map(|i| w((i as i16).wrapping_mul(211)))
            .collect();
        assert_eq!(period, unchanged);
    }

    /// The chip-reference tap must consume the SAME S16 staging the UDP tap does,
    /// not a second narrowing of the spine — two roundings of one period would be
    /// two different references, and inv-A says there is one.
    #[test]
    fn both_reference_taps_consume_one_narrowing_of_the_same_period() {
        const PERIOD_FRAMES: u32 = 128;
        let receiver = UdpSocket::bind("127.0.0.1:0").expect("bind reference receiver");
        receiver
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let config = Config {
            period_frames: PERIOD_FRAMES,
            reference_udp_target: Some(receiver.local_addr().unwrap().to_string()),
            chip_ref_pcm: None,
            ..chip_ref_test_config()
        };
        let state = Arc::new(OutputdState::new(&config));
        let shutdown = Arc::new(AtomicBool::new(false));
        let mut refs = ReferenceSideOutputs::new(&config, &shutdown, Arc::clone(&state));

        // Values deliberately OFF the S16 grid — halfway between two S16 steps —
        // so a second, differently-rounded narrowing would be visible.
        let period: Vec<ProgramSample> = (0..(PERIOD_FRAMES as usize) * (CHANNELS as usize))
            .map(|i| w(((i as i16) % 1000) * 17) + 32_768)
            .collect();
        refs.publish(&period, 1);

        let mut buf = vec![0u8; 64 * 1024];
        let (len, _) = receiver.recv_from(&mut buf).unwrap();
        let decoded: Vec<i16> = buf[..len]
            .chunks_exact(2)
            .map(|b| i16::from_le_bytes([b[0], b[1]]))
            .collect();
        // Feed the SAME staging through the chip downsampler and confirm its
        // output is derivable from the wire bytes rather than from the spine.
        let mut downsampler = ChipRefDownsampler::new(48_000, 16_000).unwrap();
        let from_wire = downsampler.process(&decoded);
        let mut fresh = ChipRefDownsampler::new(48_000, 16_000).unwrap();
        let mut staging = vec![0i16; period.len()];
        narrow_period(&period, &mut staging).unwrap();
        assert_eq!(
            from_wire,
            fresh.process(&staging),
            "the two taps must see identical S16 samples"
        );
    }

    fn chip_ref_test_config() -> Config {
        Config {
            backend: BackendMode::Alsa,
            sink_mode: SinkMode::SingleAlsa,
            content_channels: 2,
            content_format: SampleFormat::S16Le,
            dac_pcm: "outputd_dac".to_string(),
            declared_dac_format: SampleFormat::S16Le,
            dual_dac_a_pcm: None,
            dual_dac_b_pcm: None,
            dual_max_delay_delta_frames: 2,
            sample_rate: 48_000,
            period_frames: 128,
            dac_buffer_frames: 256,
            content_bridge_mode: ContentBridgeMode::Direct,
            shm_ring: None,
            chip_ref_pcm: Some("test-unavailable-chip-ref".to_string()),
            chip_ref_sample_rate: 16_000,
            chip_ref_period_frames: 320,
            chip_ref_buffer_frames: 1280,
            chip_ref_observe: false,
            chip_ref_tee_path: None,
            reference_udp_target: None,
            control_socket_path: None,
            dac_content_fifo: None,
            dac_content_ring: None,
            dac_content_channel: jasper_outputd::dac_content::ChannelPick::Stereo,
            dac_content_highpass_hz: None,
            dac_content_trim_db: 0.0,
            tts_socket_path: None,
            tts_max_pending_frames: jasper_outputd::tts::DEFAULT_MAX_PENDING_FRAMES,
            tts_program_duck_db: -25.0,
            assistant_reference_path: "/var/lib/jasper/outputd_assistant_volume_reference.json"
                .to_string(),
            active_lane: false,
            // ACTIVE_LANE's pair — false is the passive default, which is what
            // this chip-ref fixture wants (it is not an active-ring endpoint).
            ring_active_endpoint: false,
        }
    }

    #[test]
    fn chip_ref_worker_degrades_then_recovers_without_exiting() {
        let config = chip_ref_test_config();
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

    /// One S16 sample at the program spine's scale.
    fn w(sample: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(sample)
    }

    /// A slice of S16 values at the spine's scale.
    fn wv(samples: &[i16]) -> Vec<ProgramSample> {
        samples.iter().copied().map(w).collect()
    }

    #[test]
    fn fold_reference_pairwise_composite_matches_dual_active_reference_contract() {
        let mut out = vec![0 as ProgramSample; 4];

        fold_reference_pairwise_composite(
            &wv(&[100, 300, 1000, 3000, -100, -300, -1000, -3000]),
            &mut out,
        );

        // Same monitor contract as before the spine widened — the pairwise child
        // averages, now exact at the spine's resolution instead of rounded onto
        // the S16 grid (these particular averages are whole numbers either way).
        assert_eq!(out, wv(&[200, 2000, -200, -2000]));
    }

    #[test]
    fn pairwise_composite_average_cannot_overflow_at_the_spine_rails() {
        // Why `average_pair` accumulates in i64. Two i32 samples sum past the i32
        // rail, so an i32 accumulator wraps a correlated full-scale pair to the
        // OPPOSITE rail — a full-scale polarity flip in the AEC reference, which
        // would both mis-train the canceller and be inaudible in a small fixture.
        // Untestable at i16, where two samples always fit the i32 accumulator;
        // this is a guard the widening created the need for.
        let mut out = vec![0 as ProgramSample; 2];
        fold_reference_pairwise_composite(
            &[
                ProgramSample::MAX,
                ProgramSample::MAX,
                ProgramSample::MIN,
                ProgramSample::MIN,
            ],
            &mut out,
        );
        assert_eq!(out, vec![ProgramSample::MAX, ProgramSample::MIN]);

        // Anti-correlated full scale folds to silence, not to a rail.
        fold_reference_pairwise_composite(
            &[
                ProgramSample::MAX,
                ProgramSample::MIN,
                ProgramSample::MIN,
                ProgramSample::MAX,
            ],
            &mut out,
        );
        assert_eq!(out, vec![0, 0]);
    }

    #[test]
    fn fold_reference_sums_lanes_to_mono_clip_proof() {
        // 8-channel content -> stereo reference: mono = sum/8, L == R.
        let mut out = vec![0 as ProgramSample; 2 * 2]; // 2 frames, stereo
        fold_reference(
            &wv(&[
                800, 1600, 2400, 3200, 4000, 4800, 5600, 6400, // sum 28800 / 8 = 3600
                -800, -1600, -2400, -3200, -4000, -4800, -5600, -6400, // -3600
            ]),
            8,
            &mut out,
        );
        assert_eq!(out, wv(&[3600, 3600, -3600, -3600]));
    }

    #[test]
    fn fold_reference_one_over_n_cannot_clip_at_full_scale() {
        // The clip-proof claim, RE-PROVEN at the spine's width: N full-scale
        // lanes sum to N x, and 1/N keeps the result exactly at full-scale (never
        // beyond) for any width. This is where the i64 accumulator earns its
        // keep — at i32 the 8-lane sum is 8 x i32::MAX, which overflows i32 by
        // three bits, so an i32 accumulator would wrap this exact worst case
        // instead of surviving it.
        for channels in [2usize, 4, 8] {
            let mut content = vec![ProgramSample::MAX; channels];
            let mut out = vec![0 as ProgramSample; 2];
            fold_reference(&content, channels, &mut out);
            assert_eq!(
                out,
                vec![ProgramSample::MAX, ProgramSample::MAX],
                "max width {channels}"
            );

            content.fill(ProgramSample::MIN);
            fold_reference(&content, channels, &mut out);
            assert_eq!(
                out,
                vec![ProgramSample::MIN, ProgramSample::MIN],
                "min width {channels}"
            );
        }
    }

    #[test]
    fn count_full_scale_samples_counts_saturation_only() {
        // The pre-spine vectors, widened. The counts must be IDENTICAL, because
        // every commissioned box feeds this function widened S16 content and the
        // clip proxy must not change meaning under it.
        assert_eq!(
            count_full_scale_samples(&wv(&[0, 100, -100, 32766, -32767])),
            0
        );
        assert_eq!(
            count_full_scale_samples(&wv(&[i16::MAX, 5, i16::MIN, i16::MAX, -3])),
            3
        );
        assert_eq!(count_full_scale_samples(&[]), 0);
    }

    #[test]
    fn count_full_scale_samples_sees_both_the_widened_s16_and_native_s32_rails() {
        // The trap this band exists for: a widened S16 full-scale sample is
        // `0x7FFF_0000`, one S16 LSB BELOW `i32::MAX`. An `s == i32::MAX` test
        // would score every S16-content box 0 forever and quietly restore the
        // vacuously-green no-clip gate.
        assert_ne!(w(i16::MAX), ProgramSample::MAX, "the trap must be real");
        assert_eq!(count_full_scale_samples(&[w(i16::MAX)]), 1);
        assert_eq!(count_full_scale_samples(&[w(i16::MIN)]), 1);
        // A native S32 lane's own rails count too (what PR-6 will feed it).
        assert_eq!(count_full_scale_samples(&[ProgramSample::MAX]), 1);
        assert_eq!(count_full_scale_samples(&[ProgramSample::MIN]), 1);

        // And the band is TIGHT: the nearest non-clipping S16 value, widened, is
        // one S16 LSB below the band and must not be counted. Without this the
        // "counts saturation ONLY" half of the contract is unpinned.
        assert_eq!(count_full_scale_samples(&[w(i16::MAX - 1)]), 0);
        assert_eq!(count_full_scale_samples(&[w(i16::MIN + 1)]), 0);
        // The band edges themselves, exactly: symmetric, one S16 LSB wide.
        assert_eq!(count_full_scale_samples(&[ProgramSample::MAX - 65_535]), 1);
        assert_eq!(count_full_scale_samples(&[ProgramSample::MAX - 65_536]), 0);
        assert_eq!(count_full_scale_samples(&[ProgramSample::MIN + 65_535]), 1);
        assert_eq!(count_full_scale_samples(&[ProgramSample::MIN + 65_536]), 0);
    }

    #[test]
    fn the_program_duck_gain_carries_the_low_bits_f32_would_have_dropped() {
        // `apply_linear_gain` is f64 for the same mantissa reason `apply_gain` is:
        // at unity it must be the exact identity even near full scale, which an
        // f32 product cannot be.
        let mut samples = [ProgramSample::MAX - 3, ProgramSample::MIN + 3, 12_345, 0];
        let before = samples;
        apply_linear_gain(&mut samples, 1.0);
        assert_eq!(samples, before, "unity duck must be the identity");
        // Show f32 would have failed it, so the guard is not vacuous.
        let via_f32 = ((before[0] as f32) * 1.0f32).round() as i64;
        assert_ne!(via_f32, i64::from(before[0]));

        // A real duck attenuates and stays in range.
        let mut samples = [ProgramSample::MAX, ProgramSample::MIN];
        apply_linear_gain(&mut samples, 0.5);
        assert_eq!(samples[0], (ProgramSample::MAX as f64 * 0.5).round() as i32);
        assert_eq!(samples[1], (ProgramSample::MIN as f64 * 0.5).round() as i32);
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
