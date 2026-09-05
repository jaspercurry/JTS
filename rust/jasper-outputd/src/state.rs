// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Local control socket for outputd observability and cheap runtime knobs.
//!
//! The socket mirrors the fan-in daemon's shape: one command per
//! connection. `STATUS\n` returns a compact JSON snapshot, trim-only
//! pair-balance updates use `SET_DAC_CONTENT_TRIM_DB <db>\n`, and malformed
//! commands return a JSON error. `jasper-control /state`,
//! `jasper-doctor`, and an operator can all consume the same surface.

use std::io::{self, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};

use crate::aec_clock::SroEstimator;
use crate::alsa_backend::{CompositeStatus, IoCounters, NegotiatedPcm};
use crate::config::Config;
use crate::dac_clock::DacClockObserver;
use crate::dac_content::DacContentMetrics;
use crate::json::json_string;
use crate::tts::TtsMetrics;
use crate::types::SampleFormat;
use jasper_clock::DllSnapshot;
use jasper_ring::RingMetrics;
use std::sync::OnceLock;

const CONNECTION_READ_TIMEOUT: Duration = Duration::from_secs(2);
/// One local control command per connection. The longest production command is
/// a short trim update; 256 bytes leaves ample diagnostic margin while keeping
/// an idle/hostile local client from growing the state thread's buffer without
/// bound.
const MAX_COMMAND_BYTES: usize = 256;
const ACCEPT_POLL_INTERVAL: Duration = Duration::from_millis(500);
const NEVER_MS: u64 = u64::MAX;
const OPTIONAL_U64_NONE: u64 = u64::MAX;
const DAC_CONTENT_TRIM_DB_MIN_TENTHS: i32 = -240;
const DAC_CONTENT_TRIM_DB_MAX_TENTHS: i32 = 0;

#[derive(Debug, Clone, Copy, Default)]
pub struct ChipRefWrite {
    pub frames_written: u64,
    pub delay_frames: Option<u64>,
    pub reference_sequence: Option<u64>,
    pub underruns: u64,
    pub xruns: u64,
    pub recoveries: u64,
    pub write_failed: bool,
}

/// How many of the chip-reference writer's most recent completed writes STATUS
/// reports under `reference_outputs.chip_ref_writer.recent_writes`.
///
/// The reader is `jasper-aec-init`, which needs a RUN of per-write
/// `snd_pcm_delay` readings to resolve `SYS_DELAY = K - median(live queue)`.
/// It cannot get that run by polling: this state server is a single thread that
/// sleeps `ACCEPT_POLL_INTERVAL` between accepts and answers one command per
/// connection, which caps a sequential reader at about two reads a second
/// (measured on jts3: 11 reads in 5.14 s). Publishing the writer's own recent
/// observations decouples how fast the reader polls from how many readings it
/// sees, so one read carries seconds of history.
///
/// What the ring has to cover is the GAP BETWEEN two reads, since the reader
/// only counts writes made while it was watching. 256 covers it at both ends of
/// the fleet's mix cadences: 5.5 s at jts3's 1024-frame period (46.9 writes/s)
/// and 0.68 s at jts.local's 128-frame period (375 writes/s), against a poll
/// gap near 0.5 s. A reader that falls further behind than that thins its
/// window rather than corrupting it — the cumulative error counters it compares
/// read to read still span every gap. The ring is a fixed array inside the
/// state object (256 x 32 B = 8 KiB, allocated once at construction), because
/// outputd runs under `mlockall` and must not grow its resident set at runtime.
pub const CHIP_REF_RECENT_WRITES: usize = 256;

/// One completed chip-reference write, as the writer thread observed it.
///
/// Raw observations only: outputd reports what it saw and `jasper-aec-init`
/// owns every acceptance rule over them. Duplicating the statistics here would
/// give the window two definitions.
#[derive(Debug, Clone, Copy)]
struct ChipRefObservation {
    /// outputd uptime at write completion. Published as an age relative to the
    /// snapshot instant, which is the vocabulary the rest of this block uses.
    uptime_ms: u64,
    /// `snd_pcm_delay` read when this write returned.
    delay_frames: u64,
    /// Cumulative `frames_written` AFTER this write. Strictly increasing across
    /// entries, so it is the writer's own identity for an observation — the
    /// priming write at PCM open carries no reference sequence, so this is the
    /// key that is always present.
    frames_written: u64,
    /// The mix period this write carried, or `OPTIONAL_U64_NONE` for the
    /// priming write.
    reference_sequence: u64,
}

/// Bounded ring of the most recent [`ChipRefObservation`]s, oldest first.
struct ChipRefWriteRing {
    entries: [ChipRefObservation; CHIP_REF_RECENT_WRITES],
    len: usize,
    next: usize,
}

impl ChipRefWriteRing {
    fn new() -> Self {
        Self {
            entries: [ChipRefObservation {
                uptime_ms: 0,
                delay_frames: 0,
                frames_written: 0,
                reference_sequence: OPTIONAL_U64_NONE,
            }; CHIP_REF_RECENT_WRITES],
            len: 0,
            next: 0,
        }
    }

    fn push(&mut self, observation: ChipRefObservation) {
        self.entries[self.next] = observation;
        self.next = (self.next + 1) % CHIP_REF_RECENT_WRITES;
        if self.len < CHIP_REF_RECENT_WRITES {
            self.len += 1;
        }
    }

    /// Append the ring oldest-first. `out` must already own its capacity: this
    /// runs under the ring lock, and the writer thread must never wait on the
    /// state thread's heap allocator.
    fn copy_into(&self, out: &mut Vec<ChipRefObservation>) {
        let start = (self.next + CHIP_REF_RECENT_WRITES - self.len) % CHIP_REF_RECENT_WRITES;
        for offset in 0..self.len {
            out.push(self.entries[(start + offset) % CHIP_REF_RECENT_WRITES]);
        }
    }
}

pub struct OutputdState {
    started_at: Instant,
    backend: String,
    sink_mode: String,
    /// The DECLARED wire of the post-DSP content hop — the RING's wire, which
    /// `ShmRingSource` builds its geometry from (`Config::content_format`). The
    /// reconciler (`jasper-audio-hardware-reconcile`) emits `S32_LE` by
    /// default, since the ring wire's resolver defaults wide; `S16_LE` means an
    /// unreconciled box, or one pinned narrow
    /// (`JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`).
    ///
    /// It is the whole answer for `content.format`: nothing here negotiates,
    /// because the ring's own attach is what validates the declaration against
    /// the writer's header. `shm_ring.format` falls back to this value before
    /// the reader attaches for the same reason — one declaration, read at the
    /// other hop.
    declared_content_format: String,
    /// The DECLARED content-hop channel count (`Config::content_channels`) — the
    /// other axis of the same declaration as [`Self::declared_content_format`],
    /// and the pre-attach half of `shm_ring.channels` for the same reason. It
    /// lives here rather than pre-seeded into the wire cell so each declared
    /// axis has exactly one home, and the cell holds only what the reader
    /// reported.
    declared_content_channels: u64,
    dac_pcm: String,
    /// The registry-DECLARED format of the final hardware edge — what the
    /// reconciler says the DAC's `hw:` device is at. It is what `dac.format`
    /// reports only UNTIL outputd opens an edge of its own; the fake backend
    /// never does, so there it is the whole answer.
    declared_dac_format: String,
    /// The format outputd's ALSA sink actually NEGOTIATED, read back from the
    /// installed `hw_params` (`SampleFormat::as_str`, hence `&'static str`).
    /// Written exactly once, at open, and it is what `dac.format` reports from
    /// then on — the chip-AEC alignment identity must certify against the edge
    /// outputd is running, never against a declaration it could disagree with.
    negotiated_dac_format: OnceLock<&'static str>,
    dual_dac_a_pcm: Option<String>,
    dual_dac_b_pcm: Option<String>,
    dual_linked: AtomicBool,
    dual_delay_delta_frames: AtomicI64,
    dual_delay_delta_baseline_frames: AtomicI64,
    dual_delay_delta_error_frames: AtomicI64,
    dual_max_delay_delta_frames: AtomicI64,
    /// Per-child xrun attribution and the group's recovery bookkeeping, from
    /// `CompositeStatus`. Rendered only inside the already-conditional
    /// `dual_apple` block, so a single-DAC box's snapshot is unchanged.
    dual_dac_a_xruns: AtomicU64,
    dual_dac_b_xruns: AtomicU64,
    dual_group_recoveries: AtomicU64,
    dual_delay_baseline_relatches: AtomicU64,
    dual_reprime_alignment_failures: AtomicU64,
    chip_ref_pcm: Option<String>,
    chip_ref_diagnostic_tee_path: Option<String>,
    reference_udp_target: Option<String>,
    reference_udp_active: AtomicBool,
    reference_udp_error_count: AtomicU64,
    // Datagrams the socket refused with `WouldBlock` — the tap is
    // nonblocking BECAUSE it must never stall the DAC, so a full send
    // buffer drops the period. Counted, not recovered: the count is the
    // whole difference between a reference stream with holes in it and
    // one that looks complete (#3509).
    reference_udp_dropped_count: AtomicU64,
    sample_rate: AtomicU64,
    content_period_frames: AtomicU64,
    dac_period_frames: AtomicU64,
    content_buffer_frames: AtomicU64,
    dac_buffer_frames: AtomicU64,
    // The resolved outputd content source: `direct`, `shm_ring`, or
    // `dac_content_ring` (an armed round-trip marker, whose return lane is the
    // whole source — no central hop attaches beside it). Published as a plain
    // mode string; the ring's own health lives in the `shm_ring` block below and
    // the return lane's in `dac_content`. (The rate-matched bridge that once
    // published fill/ppm/lock counters here was deleted — see
    // `config::REMOVED_RATE_MATCH_BRIDGE_SPELLINGS`.)
    content_bridge_mode: String,
    // PROTOTYPE (latency/ring-proto-shm): SHM ping-pong ring reader health.
    // `shm_ring_path` is Some iff JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring; the
    // block is enabled:false with no further fields otherwise (default-off,
    // zero noise). Counters mirror jasper_ring::RingMetrics.
    shm_ring_path: Option<String>,
    shm_ring_slots: AtomicU64,
    shm_ring_slot_frames: AtomicU64,
    /// The attached ring's WIRE — the two axes ring v2 made per-box, published
    /// so an operator reads what the ring lane is running instead of inferring
    /// it from the env. Both come from the reader
    /// (`ShmRingSource::wire_format`/`channels`) via [`Self::mark_shm_ring_wire`],
    /// which is the point: the value is sourced from the ring outputd holds,
    /// not from a second copy of the declaration. They cannot disagree with
    /// that declaration — attach validates every geometry field against it and
    /// refuses any other outcome — so this is the wire, not a drift detector.
    ///
    /// ONE cell for the one fact-pair, and `OnceLock` because a wire cannot
    /// change while a reader lives (a re-attach is a new process). A pair in a
    /// single cell cannot half-publish: a reader that reached this block between
    /// two separate stores would otherwise have seen one axis attached and the
    /// other still declared. **Marking it twice is a programming error** — the
    /// second `set` is dropped, deliberately, because a spurious re-mark must
    /// not be able to REPLACE the wire a live reader is running; the one call
    /// site is beside the reader's construction in `run_alsa`.
    ///
    /// Before a reader exists, both axes report the DECLARATION —
    /// `declared_content_format` and [`Self::declared_content_channels`], the
    /// same two keys the ring geometry is built from, so what they report is the
    /// wire the reader will attach at or exit 78 trying. That window is real,
    /// not theoretical: the control socket is served before `run_alsa` builds
    /// the reader, and the fake backend never builds one at all.
    shm_ring_wire: OnceLock<(&'static str, u64)>,
    shm_ring_attached: AtomicBool,
    shm_ring_occupancy: AtomicU64,
    shm_ring_frames_read: AtomicU64,
    shm_ring_startup_empty_reads: AtomicU64,
    shm_ring_empty_reads: AtomicU64,
    shm_ring_epoch_resets: AtomicU64,
    shm_ring_reader_resyncs: AtomicU64,
    shm_ring_attach_resyncs: AtomicU64,
    shm_ring_writer_pid: AtomicU64,
    shm_ring_writer_heartbeat_age_ms: AtomicU64,
    shm_ring_writer_alive: AtomicBool,
    /// The armed round-trip lane's path, whichever transport carries it, and
    /// which transport that is. `None` = solo.
    dac_content_lane: Option<(&'static str, String)>,
    dac_content_channel: String,
    dac_content_trim_db_tenths: AtomicI32,
    dac_content_trim_gain_bits: AtomicU32,
    dac_content_serving_fifo: AtomicBool,
    dac_content_fifo_periods: AtomicU64,
    dac_content_starved_periods: AtomicU64,
    dac_content_staged_periods: AtomicU64,
    dac_content_overflow_dropped_periods: AtomicU64,
    dac_content_open_failures: AtomicU64,
    dac_content_read_failures: AtomicU64,
    chip_ref_sample_rate: AtomicU64,
    chip_ref_period_frames: AtomicU64,
    chip_ref_buffer_frames: AtomicU64,
    dac_snd_pcm_delay_frames: AtomicU64,
    dac_snd_pcm_delay_sample_ms: AtomicU64,
    chip_ref_queue_depth_periods: AtomicU64,
    chip_ref_queued_frames: AtomicU64,
    chip_ref_frames_written: AtomicU64,
    chip_ref_snd_pcm_delay_frames: AtomicU64,
    chip_ref_snd_pcm_delay_sample_ms: AtomicU64,
    chip_ref_write_underrun_count: AtomicU64,
    chip_ref_write_xrun_count: AtomicU64,
    chip_ref_write_recovery_count: AtomicU64,
    chip_ref_write_error_count: AtomicU64,
    chip_ref_writer_active: AtomicBool,
    chip_ref_terminal_failure: AtomicBool,
    chip_ref_open_error_count: AtomicU64,
    chip_ref_retry_count: AtomicU64,
    chip_ref_dropped_unavailable_periods: AtomicU64,
    chip_ref_dropped_full_periods: AtomicU64,
    chip_ref_dropped_disconnected_periods: AtomicU64,
    chip_ref_last_write_ms: AtomicU64,
    chip_ref_last_enqueued_reference_sequence: AtomicU64,
    chip_ref_last_written_reference_sequence: AtomicU64,
    chip_ref_tee_active: AtomicBool,
    chip_ref_tee_open_error_count: AtomicU64,
    chip_ref_tee_write_error_count: AtomicU64,
    // The writer's own recent per-write observations (see
    // `CHIP_REF_RECENT_WRITES`). Mutex on the same grounds as `sro_estimator`
    // below: a small fixed struct with one writer (the chip-ref writer thread)
    // and one reader (the state server), so the lock is uncontended and neither
    // side allocates while holding it.
    //
    // It is NOT the same duty cycle, and that is the part worth stating: the
    // SRO estimator is fed at ~1 Hz (decimated), this is pushed once per
    // completed write — 47 Hz at a 1024-frame mix period, 375 Hz at 128, i.e.
    // 50-375x more often. What keeps that fine is that the critical section is
    // a 32-byte store into a fixed array with no allocation and no syscall,
    // and the only contender is a /state read a couple of times a second which
    // copies out and releases before it formats anything.
    chip_ref_writes: Mutex<ChipRefWriteRing>,
    // Observe-only label (chip-AEC Layer 0): true when the reconciler armed
    // the chip-ref writer purely to MEASURE drift on the DAC playout clock (vs nominal)
    // (not for production chip-AEC). Set once at construction from config;
    // changes no behavior — surfaced in the aec_clock block so /state can
    // self-describe why the chip-ref writer is running.
    chip_ref_observe: bool,
    // Passive SRO (sample-rate-offset) drift estimator (chip-AEC Layer 0).
    // Ticked from `mark_chip_ref_write` — i.e. wherever the chip-ref delay is
    // already sampled — reading the already-stored DAC counters. Observe-only:
    // it never warps audio. Mutex (not atomics) because the estimate is a
    // small struct with a ring buffer; the chip-ref writer is the only writer
    // and the state server the only reader, so contention is negligible.
    sro_estimator: Mutex<SroEstimator>,
    // Last cumulative `chip_ref_frames_written` fed to the SRO estimator.
    // Decimates the ~50 Hz `mark_chip_ref_write` ticks down to ~1 Hz (the rate
    // the estimator's slope window is tuned for).
    sro_last_fed_chip_ref_frames: AtomicU64,
    // Observe-only DAC playout-clock drift observer (Inc 2). A
    // jasper-clock DLL fed the wall-clock-vs-DAC-playout frame error from
    // `mark_dac_delay` (where the DAC delay is already sampled). It NEVER warps
    // audio — it surfaces `dac_clock_ppm` + lock/verdict on /state.
    // Mutex for the same reason as `sro_estimator`: a small struct, single
    // writer (the playback loop) + single reader (the state server).
    dac_clock: Mutex<DacClockObserver>,
    content_frames_read: AtomicU64,
    content_empty_period_count: AtomicU64,
    // The live content source's CURRENT run of zero-filled periods and the
    // verdict `ContentFill` draws from it. The cumulative counters above and
    // in `shm_ring`/`dac_content` cannot say "right now", which is what a
    // deaf chain needs a surface to read (#3458).
    content_consecutive_empty_periods: AtomicU64,
    content_deaf: AtomicBool,
    content_partial_period_count: AtomicU64,
    content_eagain_count: AtomicU64,
    dac_frames_written: AtomicU64,
    content_xrun_count: AtomicU64,
    dac_xrun_count: AtomicU64,
    last_content_xrun_ms: AtomicU64,
    last_dac_xrun_ms: AtomicU64,
    total_clipped_samples: AtomicU64,
    last_period_clipped_samples: AtomicU64,
    reference_sequence: AtomicU64,
    last_progress_ms: AtomicU64,
    watchdog_pings_sent: AtomicU64,
    // Bonded-member TTS lane (PR-2). Set once at startup when the
    // socket env is configured; the state server may briefly read
    // enabled:false before run_alsa sets it — harmless.
    tts: OnceLock<(String, TtsMetrics)>,
}

fn trim_db_tenths(trim_db: f32) -> i32 {
    (trim_db * 10.0).round() as i32
}

fn validate_trim_db_tenths(trim_db: f32) -> Result<i32> {
    if !trim_db.is_finite() {
        anyhow::bail!("trim_db must be finite");
    }
    let tenths = trim_db_tenths(trim_db);
    if !(DAC_CONTENT_TRIM_DB_MIN_TENTHS..=DAC_CONTENT_TRIM_DB_MAX_TENTHS).contains(&tenths) {
        anyhow::bail!(
            "trim_db must be between {:.1} and {:.1} dB",
            DAC_CONTENT_TRIM_DB_MIN_TENTHS as f32 / 10.0,
            DAC_CONTENT_TRIM_DB_MAX_TENTHS as f32 / 10.0,
        );
    }
    Ok(tenths)
}

fn trim_gain_bits(trim_db_tenths: i32) -> u32 {
    let trim_db = trim_db_tenths as f32 / 10.0;
    let gain = if trim_db < 0.0 {
        10f32.powf(trim_db / 20.0)
    } else {
        1.0
    };
    gain.to_bits()
}

impl OutputdState {
    pub fn new(config: &Config) -> Self {
        Self {
            started_at: Instant::now(),
            backend: config.backend.as_str().to_string(),
            sink_mode: config.sink_mode.as_str().to_string(),
            declared_content_format: config.content_format.as_str().to_string(),
            declared_content_channels: u64::from(config.content_channels),
            dac_pcm: config.dac_pcm.clone(),
            declared_dac_format: config.declared_dac_format.as_str().to_string(),
            negotiated_dac_format: OnceLock::new(),
            dual_dac_a_pcm: config.dual_dac_a_pcm.clone(),
            dual_dac_b_pcm: config.dual_dac_b_pcm.clone(),
            dual_linked: AtomicBool::new(false),
            dual_delay_delta_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_delay_delta_baseline_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_delay_delta_error_frames: AtomicI64::new(pack_optional_i64(None)),
            dual_max_delay_delta_frames: AtomicI64::new(config.dual_max_delay_delta_frames),
            dual_dac_a_xruns: AtomicU64::new(0),
            dual_dac_b_xruns: AtomicU64::new(0),
            dual_group_recoveries: AtomicU64::new(0),
            dual_delay_baseline_relatches: AtomicU64::new(0),
            dual_reprime_alignment_failures: AtomicU64::new(0),
            chip_ref_pcm: config.chip_ref_pcm.clone(),
            chip_ref_diagnostic_tee_path: config.chip_ref_tee_path.clone(),
            reference_udp_target: config.reference_udp_target.clone(),
            reference_udp_active: AtomicBool::new(false),
            reference_udp_error_count: AtomicU64::new(0),
            reference_udp_dropped_count: AtomicU64::new(0),
            sample_rate: AtomicU64::new(config.sample_rate as u64),
            content_period_frames: AtomicU64::new(config.period_frames as u64),
            dac_period_frames: AtomicU64::new(config.period_frames as u64),
            // Seeded to the period, which is what `set_negotiated` installs at
            // open: there is no ALSA capture ring on the content side any more
            // (ADR-0100), so the honest depth is the SHM ring's own
            // `content.ring.capacity_frames`, published beside this.
            content_buffer_frames: AtomicU64::new(config.period_frames as u64),
            dac_buffer_frames: AtomicU64::new(config.dac_buffer_frames as u64),
            content_bridge_mode: config.content_bridge_mode.as_str().to_string(),
            // PROTOTYPE SHM ring reader health.
            shm_ring_path: config.shm_ring.as_ref().map(|r| r.path.clone()),
            shm_ring_slots: AtomicU64::new(
                config
                    .shm_ring
                    .as_ref()
                    .map(|r| r.n_slots as u64)
                    .unwrap_or(0),
            ),
            shm_ring_slot_frames: AtomicU64::new(
                config
                    .shm_ring
                    .as_ref()
                    .map(|_| config.period_frames as u64)
                    .unwrap_or(0),
            ),
            shm_ring_wire: OnceLock::new(),
            shm_ring_attached: AtomicBool::new(false),
            shm_ring_occupancy: AtomicU64::new(0),
            shm_ring_frames_read: AtomicU64::new(0),
            shm_ring_startup_empty_reads: AtomicU64::new(0),
            shm_ring_empty_reads: AtomicU64::new(0),
            shm_ring_epoch_resets: AtomicU64::new(0),
            shm_ring_reader_resyncs: AtomicU64::new(0),
            shm_ring_attach_resyncs: AtomicU64::new(0),
            shm_ring_writer_pid: AtomicU64::new(0),
            shm_ring_writer_heartbeat_age_ms: AtomicU64::new(0),
            shm_ring_writer_alive: AtomicBool::new(false),
            dac_content_lane: config
                .dac_content_ring
                .clone()
                .map(|path| ("ring", path))
                .or_else(|| config.dac_content_fifo.clone().map(|path| ("fifo", path))),
            dac_content_channel: config.dac_content_channel.as_str().to_string(),
            dac_content_trim_db_tenths: AtomicI32::new(trim_db_tenths(config.dac_content_trim_db)),
            dac_content_trim_gain_bits: AtomicU32::new(trim_gain_bits(trim_db_tenths(
                config.dac_content_trim_db,
            ))),
            dac_content_serving_fifo: AtomicBool::new(false),
            dac_content_fifo_periods: AtomicU64::new(0),
            dac_content_starved_periods: AtomicU64::new(0),
            dac_content_staged_periods: AtomicU64::new(0),
            dac_content_overflow_dropped_periods: AtomicU64::new(0),
            dac_content_open_failures: AtomicU64::new(0),
            dac_content_read_failures: AtomicU64::new(0),
            chip_ref_sample_rate: AtomicU64::new(config.chip_ref_sample_rate as u64),
            chip_ref_period_frames: AtomicU64::new(config.chip_ref_period_frames as u64),
            chip_ref_buffer_frames: AtomicU64::new(config.chip_ref_buffer_frames as u64),
            dac_snd_pcm_delay_frames: AtomicU64::new(OPTIONAL_U64_NONE),
            dac_snd_pcm_delay_sample_ms: AtomicU64::new(NEVER_MS),
            chip_ref_queue_depth_periods: AtomicU64::new(0),
            chip_ref_queued_frames: AtomicU64::new(0),
            chip_ref_frames_written: AtomicU64::new(0),
            chip_ref_snd_pcm_delay_frames: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_snd_pcm_delay_sample_ms: AtomicU64::new(NEVER_MS),
            chip_ref_write_underrun_count: AtomicU64::new(0),
            chip_ref_write_xrun_count: AtomicU64::new(0),
            chip_ref_write_recovery_count: AtomicU64::new(0),
            chip_ref_write_error_count: AtomicU64::new(0),
            chip_ref_writer_active: AtomicBool::new(false),
            chip_ref_terminal_failure: AtomicBool::new(false),
            chip_ref_open_error_count: AtomicU64::new(0),
            chip_ref_retry_count: AtomicU64::new(0),
            chip_ref_dropped_unavailable_periods: AtomicU64::new(0),
            chip_ref_dropped_full_periods: AtomicU64::new(0),
            chip_ref_dropped_disconnected_periods: AtomicU64::new(0),
            chip_ref_last_write_ms: AtomicU64::new(NEVER_MS),
            chip_ref_last_enqueued_reference_sequence: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_last_written_reference_sequence: AtomicU64::new(OPTIONAL_U64_NONE),
            chip_ref_tee_active: AtomicBool::new(false),
            chip_ref_tee_open_error_count: AtomicU64::new(0),
            chip_ref_tee_write_error_count: AtomicU64::new(0),
            chip_ref_writes: Mutex::new(ChipRefWriteRing::new()),
            chip_ref_observe: config.chip_ref_observe,
            sro_estimator: Mutex::new(SroEstimator::new()),
            sro_last_fed_chip_ref_frames: AtomicU64::new(0),
            dac_clock: Mutex::new(DacClockObserver::new(
                config.sample_rate,
                config.period_frames,
            )),
            content_frames_read: AtomicU64::new(0),
            content_empty_period_count: AtomicU64::new(0),
            content_consecutive_empty_periods: AtomicU64::new(0),
            content_deaf: AtomicBool::new(false),
            content_partial_period_count: AtomicU64::new(0),
            content_eagain_count: AtomicU64::new(0),
            dac_frames_written: AtomicU64::new(0),
            content_xrun_count: AtomicU64::new(0),
            dac_xrun_count: AtomicU64::new(0),
            last_content_xrun_ms: AtomicU64::new(NEVER_MS),
            last_dac_xrun_ms: AtomicU64::new(NEVER_MS),
            total_clipped_samples: AtomicU64::new(0),
            last_period_clipped_samples: AtomicU64::new(0),
            reference_sequence: AtomicU64::new(0),
            last_progress_ms: AtomicU64::new(0),
            watchdog_pings_sent: AtomicU64::new(0),
            tts: OnceLock::new(),
        }
    }

    pub fn set_tts(&self, socket: String, metrics: TtsMetrics) {
        let _ = self.tts.set((socket, metrics));
    }

    pub fn dac_content_trim_gain(&self) -> f32 {
        f32::from_bits(self.dac_content_trim_gain_bits.load(Ordering::Relaxed))
    }

    pub fn dac_content_trim_db(&self) -> f32 {
        self.dac_content_trim_db_tenths.load(Ordering::Relaxed) as f32 / 10.0
    }

    pub fn set_dac_content_trim_db(&self, trim_db: f32) -> Result<f32> {
        if self.dac_content_lane.is_none() {
            anyhow::bail!("dac_content lane is not enabled");
        }
        let tenths = validate_trim_db_tenths(trim_db)?;
        self.dac_content_trim_db_tenths
            .store(tenths, Ordering::Relaxed);
        self.dac_content_trim_gain_bits
            .store(trim_gain_bits(tenths), Ordering::Relaxed);
        Ok(tenths as f32 / 10.0)
    }

    pub fn set_negotiated(&self, content: NegotiatedPcm, dac: NegotiatedPcm) {
        self.sample_rate
            .store(dac.sample_rate as u64, Ordering::Relaxed);
        self.content_period_frames
            .store(content.period_frames as u64, Ordering::Relaxed);
        self.dac_period_frames
            .store(dac.period_frames as u64, Ordering::Relaxed);
        self.content_buffer_frames
            .store(content.buffer_frames as u64, Ordering::Relaxed);
        self.dac_buffer_frames
            .store(dac.buffer_frames as u64, Ordering::Relaxed);
    }

    /// Record the sample format the final sink negotiated at open.
    ///
    /// Set-once by construction (the ALSA run loop calls it once, right after
    /// the sink opens). A repeat call is ignored rather than fought over:
    /// there is no second negotiation, and `dac.format` is an identity input —
    /// a value that could change under a reader is worse than a stale one.
    pub fn set_dac_format(&self, format: SampleFormat) {
        let _ = self.negotiated_dac_format.set(format.as_str());
    }

    pub fn mark_period(&self, counters: IoCounters, reference_sequence: u64, clipped_samples: u32) {
        let uptime_ms = self.uptime_ms();
        self.content_frames_read
            .store(counters.content_frames_read, Ordering::Relaxed);
        self.content_empty_period_count
            .store(counters.content_empty_period_count, Ordering::Relaxed);
        self.content_partial_period_count
            .store(counters.content_partial_period_count, Ordering::Relaxed);
        self.content_eagain_count
            .store(counters.content_eagain_count, Ordering::Relaxed);
        self.dac_frames_written
            .store(counters.dac_frames_written, Ordering::Relaxed);
        let previous_content_xruns = self
            .content_xrun_count
            .swap(counters.content_xrun_count, Ordering::Relaxed);
        if counters.content_xrun_count > previous_content_xruns {
            self.last_content_xrun_ms
                .store(uptime_ms, Ordering::Relaxed);
        }
        let previous_dac_xruns = self
            .dac_xrun_count
            .swap(counters.dac_xrun_count, Ordering::Relaxed);
        if counters.dac_xrun_count > previous_dac_xruns {
            self.last_dac_xrun_ms.store(uptime_ms, Ordering::Relaxed);
        }
        self.reference_sequence
            .store(reference_sequence, Ordering::Relaxed);
        self.last_period_clipped_samples
            .store(clipped_samples as u64, Ordering::Relaxed);
        self.total_clipped_samples
            .fetch_add(clipped_samples as u64, Ordering::Relaxed);
        self.last_progress_ms.store(uptime_ms, Ordering::Relaxed);
    }

    /// Publish the live content source's zero-fill run (see
    /// [`crate::content_fill::ContentFill`]). Separate from `mark_period`
    /// because the sink's `IoCounters` cannot see the content source at all.
    pub fn mark_content_fill(&self, consecutive_empty_periods: u64, deaf: bool) {
        self.content_consecutive_empty_periods
            .store(consecutive_empty_periods, Ordering::Relaxed);
        self.content_deaf.store(deaf, Ordering::Relaxed);
    }

    pub fn mark_watchdog_ping(&self) {
        self.watchdog_pings_sent.fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_dual_apple_status(&self, status: &CompositeStatus) {
        self.dual_linked.store(status.linked, Ordering::Relaxed);
        self.dual_delay_delta_frames.store(
            pack_optional_i64(status.delay_delta_frames),
            Ordering::Relaxed,
        );
        self.dual_delay_delta_baseline_frames.store(
            pack_optional_i64(status.delay_delta_baseline_frames),
            Ordering::Relaxed,
        );
        self.dual_delay_delta_error_frames.store(
            pack_optional_i64(status.delay_delta_error_frames),
            Ordering::Relaxed,
        );
        self.dual_max_delay_delta_frames
            .store(status.max_delay_delta_frames, Ordering::Relaxed);
        self.dual_dac_a_xruns
            .store(status.dac_a_xruns, Ordering::Relaxed);
        self.dual_dac_b_xruns
            .store(status.dac_b_xruns, Ordering::Relaxed);
        self.dual_group_recoveries
            .store(status.group_recoveries, Ordering::Relaxed);
        self.dual_delay_baseline_relatches
            .store(status.delay_baseline_relatches, Ordering::Relaxed);
        self.dual_reprime_alignment_failures
            .store(status.reprime_alignment_failures, Ordering::Relaxed);
    }

    pub fn mark_dac_delay(&self, delay_frames: u64) {
        let uptime_ms = self.uptime_ms();
        self.dac_snd_pcm_delay_frames
            .store(delay_frames, Ordering::Relaxed);
        self.dac_snd_pcm_delay_sample_ms
            .store(uptime_ms, Ordering::Relaxed);
        // Observe-only (Inc 2): tick the DAC playout-clock drift observer
        // with the freshly-sampled DAC delay paired with the cumulative frames
        // written and the monotonic uptime. The estimator self-decimates to
        // ~1 Hz, so calling it every period is cheap and harmless. It NEVER
        // touches the audio path. `try_lock` so a /state reader (the only other
        // contender) can never make the playback loop wait; a skipped tick just
        // drops one ~1 Hz sample.
        let dac_written = self.dac_frames_written.load(Ordering::Relaxed);
        let elapsed_seconds = uptime_ms as f64 / 1000.0;
        if let Ok(mut clock) = self.dac_clock.try_lock() {
            clock.observe(dac_written, delay_frames, elapsed_seconds);
        }
    }

    pub fn mark_chip_ref_queue_admitted(&self, frames: u64) {
        self.chip_ref_queue_depth_periods
            .fetch_add(1, Ordering::Relaxed);
        self.chip_ref_queued_frames
            .fetch_add(frames, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_enqueued(&self, reference_sequence: u64) {
        self.chip_ref_last_enqueued_reference_sequence
            .store(reference_sequence, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_dequeued(&self, frames: u64) {
        subtract_saturating(&self.chip_ref_queue_depth_periods, 1);
        subtract_saturating(&self.chip_ref_queued_frames, frames);
    }

    pub fn mark_chip_ref_dropped_full(&self) {
        self.chip_ref_dropped_full_periods
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_dropped_disconnected(&self) {
        self.chip_ref_dropped_disconnected_periods
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_reference_udp_active(&self, active: bool) {
        self.reference_udp_active.store(active, Ordering::Relaxed);
    }

    /// One datagram dropped by a full send buffer. The tap stays ACTIVE: it is
    /// still publishing, just with a hole, and reporting it as inactive would
    /// point an operator at a socket that is working.
    pub fn mark_reference_udp_dropped(&self) {
        self.reference_udp_dropped_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_reference_udp_error(&self) {
        self.reference_udp_active.store(false, Ordering::Relaxed);
        self.reference_udp_error_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_writer_active(&self, active: bool) {
        self.chip_ref_writer_active.store(active, Ordering::Relaxed);
        if active {
            self.chip_ref_terminal_failure
                .store(false, Ordering::Relaxed);
        }
    }

    pub fn mark_chip_ref_terminal_failure(&self) {
        self.chip_ref_writer_active.store(false, Ordering::Relaxed);
        self.chip_ref_terminal_failure
            .store(true, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_open_error(&self) {
        self.chip_ref_writer_active.store(false, Ordering::Relaxed);
        self.chip_ref_open_error_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_retry(&self) {
        self.chip_ref_retry_count.fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_dropped_unavailable(&self) {
        self.chip_ref_dropped_unavailable_periods
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_write(&self, event: ChipRefWrite) {
        let ChipRefWrite {
            frames_written,
            delay_frames,
            reference_sequence,
            underruns,
            xruns,
            recoveries,
            write_failed,
        } = event;
        if frames_written > 0 {
            let uptime_ms = self.uptime_ms();
            self.chip_ref_frames_written
                .fetch_add(frames_written, Ordering::Relaxed);
            self.chip_ref_last_write_ms
                .store(uptime_ms, Ordering::Relaxed);
            if let Some(sequence) = reference_sequence {
                self.chip_ref_last_written_reference_sequence
                    .store(sequence, Ordering::Relaxed);
            }
            if let Some(delay_frames) = delay_frames {
                self.chip_ref_snd_pcm_delay_frames
                    .store(delay_frames, Ordering::Relaxed);
                self.chip_ref_snd_pcm_delay_sample_ms
                    .store(uptime_ms, Ordering::Relaxed);
                // Keep the observation itself, not just the latest value. A
                // reader capped at ~2 STATUS reads/s cannot otherwise see a run
                // of per-write readings on a box whose writer runs at 47-375 Hz
                // (see `CHIP_REF_RECENT_WRITES`). Only writes that produced a
                // delay reading are recorded — a write without one carries no
                // observation. Uncontended lock, fixed array, no allocation.
                if let Ok(mut ring) = self.chip_ref_writes.lock() {
                    ring.push(ChipRefObservation {
                        uptime_ms,
                        delay_frames,
                        frames_written: self.chip_ref_frames_written.load(Ordering::Relaxed),
                        reference_sequence: reference_sequence.unwrap_or(OPTIONAL_U64_NONE),
                    });
                }
                // Tick the passive SRO estimator here — the chip-ref delay is
                // freshly sampled. Read the already-stored DAC counters; this
                // never touches the audio path. A fresh chip-ref consumed
                // count (frames_written includes this write) pairs with the
                // most recent DAC snapshot. Both deltas are well below the
                // estimator's monotonicity/plausibility guards.
                let dac_written = self.dac_frames_written.load(Ordering::Relaxed);
                let dac_delay =
                    unpack_optional_u64(self.dac_snd_pcm_delay_frames.load(Ordering::Relaxed));
                let chip_ref_written = self.chip_ref_frames_written.load(Ordering::Relaxed);
                let chip_ref_rate = self.chip_ref_sample_rate.load(Ordering::Relaxed) as u32;
                // Only feed once the DAC has a real delay sample; otherwise the
                // pair is incomplete and the estimator should keep observing.
                if let Some(dac_delay) = dac_delay {
                    // Decimate to ~1 sample/sec. `mark_chip_ref_write` runs once
                    // per chip-ref period (~50 Hz); feeding the estimator that
                    // fast collapses its slope baseline to a fraction of a
                    // second, where snd_pcm_delay measurement jitter swamps the
                    // ppm estimate. The estimator's window is tuned for ~1 Hz,
                    // so only feed once ~1 s of chip-ref frames has elapsed.
                    // `mark_chip_ref_write` is called only from the single
                    // chip-ref writer thread, so this load/compare/store gate
                    // needs no compare-and-swap. (Single-DAC today; a future
                    // multi-clock-domain composite sink would need a per-child
                    // estimator + tick path, not one shared estimator.)
                    let interval = u64::from(chip_ref_rate.max(1));
                    let last_fed = self.sro_last_fed_chip_ref_frames.load(Ordering::Relaxed);
                    if chip_ref_written.saturating_sub(last_fed) >= interval {
                        self.sro_last_fed_chip_ref_frames
                            .store(chip_ref_written, Ordering::Relaxed);
                        if let Ok(mut est) = self.sro_estimator.lock() {
                            est.update(
                                dac_written,
                                dac_delay,
                                chip_ref_written,
                                delay_frames,
                                chip_ref_rate,
                            );
                        }
                    }
                }
            }
        }
        if underruns > 0 {
            self.chip_ref_write_underrun_count
                .fetch_add(underruns, Ordering::Relaxed);
        }
        if xruns > 0 {
            self.chip_ref_write_xrun_count
                .fetch_add(xruns, Ordering::Relaxed);
        }
        if recoveries > 0 {
            self.chip_ref_write_recovery_count
                .fetch_add(recoveries, Ordering::Relaxed);
        }
        if write_failed {
            self.chip_ref_write_error_count
                .fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn mark_chip_ref_tee_opened(&self) {
        self.chip_ref_tee_active.store(true, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_tee_open_error(&self) {
        self.chip_ref_tee_active.store(false, Ordering::Relaxed);
        self.chip_ref_tee_open_error_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_chip_ref_tee_write_error(&self) {
        self.chip_ref_tee_active.store(false, Ordering::Relaxed);
        self.chip_ref_tee_write_error_count
            .fetch_add(1, Ordering::Relaxed);
    }

    pub fn mark_dac_content(&self, metrics: DacContentMetrics) {
        self.dac_content_serving_fifo
            .store(metrics.serving_fifo, Ordering::Relaxed);
        self.dac_content_fifo_periods
            .store(metrics.fifo_periods, Ordering::Relaxed);
        self.dac_content_starved_periods
            .store(metrics.starved_periods, Ordering::Relaxed);
        self.dac_content_staged_periods
            .store(metrics.staged_periods, Ordering::Relaxed);
        self.dac_content_overflow_dropped_periods
            .store(metrics.overflow_dropped_periods, Ordering::Relaxed);
        self.dac_content_open_failures
            .store(metrics.open_failures, Ordering::Relaxed);
        self.dac_content_read_failures
            .store(metrics.read_failures, Ordering::Relaxed);
    }

    /// PROTOTYPE (latency/ring-proto-shm): publish the SHM ring reader's
    /// per-period metrics for `/state.shm_ring`. No-op cost when the ring is
    /// unconfigured (the caller only calls this in the ShmRing arm).
    pub fn mark_shm_ring(&self, metrics: RingMetrics) {
        self.shm_ring_attached
            .store(metrics.attached, Ordering::Relaxed);
        self.shm_ring_occupancy
            .store(metrics.occupancy, Ordering::Relaxed);
        self.shm_ring_frames_read
            .store(metrics.frames_read, Ordering::Relaxed);
        self.shm_ring_startup_empty_reads
            .store(metrics.startup_empty_reads, Ordering::Relaxed);
        self.shm_ring_empty_reads
            .store(metrics.empty_reads, Ordering::Relaxed);
        self.shm_ring_epoch_resets
            .store(metrics.epoch_resets, Ordering::Relaxed);
        self.shm_ring_reader_resyncs
            .store(metrics.reader_resyncs, Ordering::Relaxed);
        self.shm_ring_attach_resyncs
            .store(metrics.attach_resyncs, Ordering::Relaxed);
        self.shm_ring_writer_pid
            .store(metrics.writer_pid, Ordering::Relaxed);
        self.shm_ring_writer_heartbeat_age_ms
            .store(metrics.writer_heartbeat_age_ms, Ordering::Relaxed);
        self.shm_ring_writer_alive
            .store(metrics.writer_alive, Ordering::Relaxed);
        if metrics.slot_frames != 0 {
            self.shm_ring_slot_frames
                .store(metrics.slot_frames as u64, Ordering::Relaxed);
        }
        if metrics.n_slots != 0 {
            self.shm_ring_slots
                .store(metrics.n_slots as u64, Ordering::Relaxed);
        }
    }

    /// Publish the attached ring's wire — see the [`Self::shm_ring_wire`] field
    /// for where the two values come from, why they cannot disagree with the
    /// declaration, and why marking it a second time is a programming error
    /// rather than an update.
    ///
    /// Called ONCE, beside the first [`Self::mark_shm_ring`], rather than per
    /// period: the wire is fixed for the reader's lifetime, so re-publishing it
    /// 375 times a second would be a store that can never change a byte.
    pub fn mark_shm_ring_wire(&self, format: SampleFormat, channels: u16) {
        let _ = self
            .shm_ring_wire
            .set((format.as_str(), u64::from(channels)));
    }

    pub fn snapshot_json(&self) -> String {
        // Sized for the payload this actually builds: the chip-reference
        // sample ring alone is ~100 B an entry (~25.6 KiB at full capacity) on
        // a chip-AEC box, and 1 KiB meant a dozen reallocations per read.
        let mut buf = String::with_capacity(32 * 1024);
        let uptime_ms = self.uptime_ms();
        let sample_rate = self.sample_rate.load(Ordering::Relaxed);
        let content_xrun_count = self.content_xrun_count.load(Ordering::Relaxed);
        let dac_xrun_count = self.dac_xrun_count.load(Ordering::Relaxed);
        buf.push('{');
        push_kv_f64(&mut buf, "uptime_seconds", (uptime_ms as f64) / 1000.0, 2);
        buf.push(',');
        push_kv_str(&mut buf, "backend", &self.backend);
        buf.push(',');
        push_kv_str(&mut buf, "sink_mode", &self.sink_mode);
        buf.push(',');

        buf.push_str(r#""content":{"#);
        // The one upstream, and the key that says so — the ring is attached or
        // this daemon has already parked (ADR-0100). `alsa` names the window
        // before the ring attaches, not a second route.
        push_kv_str(
            &mut buf,
            "source",
            if self.shm_ring_path.is_some() {
                "shm_ring"
            } else {
                "alsa"
            },
        );
        buf.push(',');
        // The DECLARED wire of the content hop. Nothing negotiates it here: the
        // ring's own attach validates the declaration against the writer's
        // header, so this is the value that got the ring open.
        push_kv_str(&mut buf, "format", self.declared_content_format.as_str());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "period_frames",
            self.content_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "buffer_frames",
            self.content_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_read",
            self.content_frames_read.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "empty_periods",
            self.content_empty_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "consecutive_empty_periods",
            self.content_consecutive_empty_periods
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_bool(&mut buf, "deaf", self.content_deaf.load(Ordering::Relaxed));
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "partial_periods",
            self.content_partial_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "eagain_count",
            self.content_eagain_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "xrun_count", content_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_content_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            &mut buf,
            "xrun_rate_per_hour",
            rate_per_hour(content_xrun_count, uptime_ms),
            3,
        );
        // Ring B honesty contract (latency/ring-proto-shm): under the shm_ring
        // content source, outputd reads the post-DSP program from an n-slot SHM
        // ping-pong ring, NOT an ALSA capture PCM — so `content.buffer_frames`
        // above is a synthetic period-sized stand-in (neither sink opens a content
        // PCM at all — ADR-0100 leaves the ring as outputd's one upstream).
        // This sub-block reports the TRUE Ring B capacity that
        // outputd requires of the writer — n_slots x slot_frames — so the synthetic
        // is clearly labeled and jasper-doctor validates the ring geometry instead
        // of mis-applying the ALSA ">= 2x period" jitter floor (which a bounded
        // n-slot queue is not). Full runtime health (occupancy, empty reads, writer
        // liveness) stays in the top-level `shm_ring` block; this is the buffering
        // capacity contract that sits next to `content.buffer_frames`.
        if self.shm_ring_path.is_some() {
            let slots = self.shm_ring_slots.load(Ordering::Relaxed);
            let slot_frames = self.shm_ring_slot_frames.load(Ordering::Relaxed);
            buf.push(',');
            buf.push_str(r#""ring":{"#);
            push_kv_u64(&mut buf, "slots", slots);
            buf.push(',');
            push_kv_u64(&mut buf, "slot_frames", slot_frames);
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "capacity_frames",
                slots.saturating_mul(slot_frames),
            );
            buf.push('}');
        }
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""content_bridge":{"#);
        push_kv_str(&mut buf, "mode", &self.content_bridge_mode);
        buf.push('}');
        buf.push(',');

        // PROTOTYPE (latency/ring-proto-shm): SHM ping-pong ring reader health.
        // enabled:false with no further fields when unconfigured (default-off,
        // zero noise), full metrics when the flag armed it. `occupancy` is the
        // live W-R depth; empty_reads split startup vs steady like the local
        // pipe; writer_alive/pid/heartbeat_age surface the cross-process writer.
        buf.push_str(r#""shm_ring":{"#);
        match self.shm_ring_path.as_deref() {
            Some(path) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "path", path);
                buf.push(',');
                push_kv_bool(
                    &mut buf,
                    "attached",
                    self.shm_ring_attached.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "slots",
                    self.shm_ring_slots.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "slot_frames",
                    self.shm_ring_slot_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // The wire: the two axes ring v2 made per-box, read out of the
                // one cell together so the pair is always the SAME source. See
                // the `shm_ring_wire` field for their provenance.
                let (wire_format, wire_channels) = self.shm_ring_wire.get().copied().unwrap_or((
                    self.declared_content_format.as_str(),
                    self.declared_content_channels,
                ));
                push_kv_str(&mut buf, "format", wire_format);
                buf.push(',');
                push_kv_u64(&mut buf, "channels", wire_channels);
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "occupancy",
                    self.shm_ring_occupancy.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "frames_read",
                    self.shm_ring_frames_read.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "startup_empty_reads",
                    self.shm_ring_startup_empty_reads.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "empty_reads",
                    self.shm_ring_empty_reads.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "epoch_resets",
                    self.shm_ring_epoch_resets.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "reader_resyncs",
                    self.shm_ring_reader_resyncs.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "attach_resyncs",
                    self.shm_ring_attach_resyncs.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_bool(
                    &mut buf,
                    "writer_alive",
                    self.shm_ring_writer_alive.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "writer_pid",
                    self.shm_ring_writer_pid.load(Ordering::Relaxed),
                );
                buf.push(',');
                // u64::MAX = "writer never heartbeated" (see jasper_ring's
                // RingMetrics). Serialize the sentinel as JSON null rather than
                // 18446744073709551615, which exceeds JS Number.MAX_SAFE_INTEGER
                // and would deserialize lossily in the /state dashboard. Uses the
                // same OPTIONAL_U64_NONE convention as the pcm_delay fields.
                push_kv_u64_opt(
                    &mut buf,
                    "writer_heartbeat_age_ms",
                    unpack_optional_u64(
                        self.shm_ring_writer_heartbeat_age_ms
                            .load(Ordering::Relaxed),
                    ),
                );
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');

        // Multi-room round-trip lane (Increment 3) — DAEMON-TRUTH health
        // for /state + jasper-doctor (never a Python mirror of env
        // intent). enabled:false with no further fields when the lane is
        // not configured (solo — zero cost, zero noise).
        buf.push_str(r#""dac_content":{"#);
        match self.dac_content_lane.as_ref() {
            Some((transport, path)) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "transport", transport);
                buf.push(',');
                // The path under the key its transport owns. A FIFO box's block
                // is unchanged; a ring box says `ring` rather than lying with
                // `fifo`.
                push_kv_str(&mut buf, transport, path);
                buf.push(',');
                push_kv_str(&mut buf, "channel", &self.dac_content_channel);
                buf.push(',');
                buf.push_str(&format!("\"trim_db\":{:.1}", self.dac_content_trim_db()));
                buf.push(',');
                push_kv_bool(
                    &mut buf,
                    "serving_fifo",
                    self.dac_content_serving_fifo.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "fifo_periods",
                    self.dac_content_fifo_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "starved_periods",
                    self.dac_content_starved_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "staged_periods",
                    self.dac_content_staged_periods.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "overflow_dropped_periods",
                    self.dac_content_overflow_dropped_periods
                        .load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "open_failures",
                    self.dac_content_open_failures.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "read_failures",
                    self.dac_content_read_failures.load(Ordering::Relaxed),
                );
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');

        // Bonded-member TTS lane (PR-2) — daemon truth for /state +
        // doctor. enabled:false when the lane is off (solo: fanin owns
        // TTS) — zero noise, mirroring dac_content.
        buf.push_str(r#""tts":{"#);
        match self.tts.get() {
            Some((socket, m)) => {
                push_kv_bool(&mut buf, "enabled", true);
                buf.push(',');
                push_kv_str(&mut buf, "socket", socket);
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "pending_frames",
                    m.pending_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(&mut buf, "budget_frames", m.max_pending_frames);
                buf.push(',');
                push_kv_u64(&mut buf, "requests", m.requests.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "dropped_audio_frames",
                    m.dropped_audio_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "dropped_commands",
                    m.dropped_commands.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "flush_requests",
                    m.flush_requests.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    &mut buf,
                    "flushed_frames",
                    m.flushed_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // The same assistant_loudness object fan-in exposes, rendered
                // through the shared writer so the two /state shapes cannot
                // drift (pinned by ASSISTANT_LOUDNESS_STATUS_KEYS on both).
                buf.push_str(r#""assistant_loudness":"#);
                jasper_tts_protocol::loudness::render_assistant_loudness(
                    &mut buf,
                    &m.loudness_snapshot(),
                );
            }
            None => {
                push_kv_bool(&mut buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""dac":{"#);
        push_kv_str(&mut buf, "pcm", &self.dac_pcm);
        buf.push(',');
        // NEGOTIATED once outputd has opened its edge: the format read back off
        // the installed hw_params, not the declaration that asked for it.
        // Consumers that must know what edge is running (chiefly the chip-AEC
        // alignment identity, which records it for forensics — ADR-0190
        // excludes it from comparison) read it here.
        //
        // Falls back to the registry declaration whenever no edge is open yet —
        // which is the fake backend for its whole life, AND the ALSA backend for
        // its pre-open window: the state socket binds before the sink opens, so
        // a STATUS read in that window is answered with the declaration.
        push_kv_str(
            &mut buf,
            "format",
            self.negotiated_dac_format
                .get()
                .copied()
                .unwrap_or(self.declared_dac_format.as_str()),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "sample_rate", sample_rate);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "period_frames",
            self.dac_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "buffer_frames",
            self.dac_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_written",
            self.dac_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        let dac_delay_frames =
            unpack_optional_u64(self.dac_snd_pcm_delay_frames.load(Ordering::Relaxed));
        push_kv_u64_opt(&mut buf, "snd_pcm_delay_frames", dac_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            &mut buf,
            "snd_pcm_delay_ms",
            frames_to_ms_opt(dac_delay_frames, sample_rate),
            3,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "snd_pcm_delay_sample_age_ms",
            event_age_ms(
                uptime_ms,
                self.dac_snd_pcm_delay_sample_ms.load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "xrun_count", dac_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_dac_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            &mut buf,
            "xrun_rate_per_hour",
            rate_per_hour(dac_xrun_count, uptime_ms),
            3,
        );
        buf.push('}');
        buf.push(',');

        if self.sink_mode == "dual_apple" {
            buf.push_str(r#""dual_apple":{"#);
            push_kv_str_opt(&mut buf, "dac_a_pcm", self.dual_dac_a_pcm.as_deref());
            buf.push(',');
            push_kv_str_opt(&mut buf, "dac_b_pcm", self.dual_dac_b_pcm.as_deref());
            buf.push(',');
            push_kv_bool(&mut buf, "linked", self.dual_linked.load(Ordering::Relaxed));
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_frames",
                unpack_optional_i64(self.dual_delay_delta_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_baseline_frames",
                unpack_optional_i64(
                    self.dual_delay_delta_baseline_frames
                        .load(Ordering::Relaxed),
                ),
            );
            buf.push(',');
            push_kv_i64_opt(
                &mut buf,
                "delay_delta_error_frames",
                unpack_optional_i64(self.dual_delay_delta_error_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64(
                &mut buf,
                "max_delay_delta_frames",
                self.dual_max_delay_delta_frames.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "dac_a_xruns",
                self.dual_dac_a_xruns.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "dac_b_xruns",
                self.dual_dac_b_xruns.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "group_recoveries",
                self.dual_group_recoveries.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "delay_baseline_relatches",
                self.dual_delay_baseline_relatches.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "reprime_alignment_failures",
                self.dual_reprime_alignment_failures.load(Ordering::Relaxed),
            );
            buf.push('}');
            buf.push(',');
        }

        buf.push_str(r#""mix":{"#);
        push_kv_u64(
            &mut buf,
            "reference_sequence",
            self.reference_sequence.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "last_period_clipped_samples",
            self.last_period_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "clipped_samples",
            self.total_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""reference_outputs":{"#);
        push_kv_str(
            &mut buf,
            "speaker_reference_source",
            "outputd_final_electrical",
        );
        buf.push(',');
        push_kv_str(
            &mut buf,
            "chip_ref_transform",
            "stereo_mean_boxcar_decimate_dual_mono_v1",
        );
        buf.push(',');
        push_kv_bool(&mut buf, "speaker_reference_is_fallback", false);
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "speaker_reference_active",
            self.chip_ref_writer_active.load(Ordering::Relaxed)
                || self.reference_udp_active.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "speaker_reference_sample_rate", sample_rate);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "speaker_reference_channels",
            crate::types::CHANNELS as u64,
        );
        buf.push(',');
        push_kv_str_opt(&mut buf, "chip_ref_pcm", self.chip_ref_pcm.as_deref());
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_sample_rate",
            self.chip_ref_sample_rate.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_period_frames",
            self.chip_ref_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "chip_ref_buffer_frames",
            self.chip_ref_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        let chip_ref_sample_rate = self.chip_ref_sample_rate.load(Ordering::Relaxed);
        let chip_ref_delay_frames =
            unpack_optional_u64(self.chip_ref_snd_pcm_delay_frames.load(Ordering::Relaxed));
        let chip_ref_last_written_sequence = unpack_optional_u64(
            self.chip_ref_last_written_reference_sequence
                .load(Ordering::Relaxed),
        );
        let chip_ref_last_enqueued_sequence = unpack_optional_u64(
            self.chip_ref_last_enqueued_reference_sequence
                .load(Ordering::Relaxed),
        );
        let reference_sequence = self.reference_sequence.load(Ordering::Relaxed);
        let chip_ref_desired = self.chip_ref_pcm.is_some();
        let chip_ref_active = self.chip_ref_writer_active.load(Ordering::Relaxed);
        let chip_ref_terminal_failure = self.chip_ref_terminal_failure.load(Ordering::Relaxed);
        let chip_ref_open_error_count = self.chip_ref_open_error_count.load(Ordering::Relaxed);
        let chip_ref_write_error_count = self.chip_ref_write_error_count.load(Ordering::Relaxed);
        let chip_ref_status = if !chip_ref_desired {
            "disabled"
        } else if chip_ref_active {
            "active"
        } else if chip_ref_terminal_failure {
            "failed"
        } else if chip_ref_open_error_count > 0 || chip_ref_write_error_count > 0 {
            "degraded"
        } else {
            "connecting"
        };
        let chip_ref_sequence_lag = chip_ref_last_written_sequence
            .map(|written| reference_sequence.saturating_sub(written));
        // Reserve first, then copy under the lock: the chip-ref writer thread
        // must never wait on this thread's allocator (same rule the SRO block
        // below states for its own snapshot). The 8 KiB reservation is skipped
        // entirely when the writer is not desired, which is every box that
        // does not run chip AEC.
        let mut chip_ref_recent_writes: Vec<ChipRefObservation> = if chip_ref_desired {
            Vec::with_capacity(CHIP_REF_RECENT_WRITES)
        } else {
            Vec::new()
        };
        if chip_ref_desired {
            if let Ok(ring) = self.chip_ref_writes.lock() {
                ring.copy_into(&mut chip_ref_recent_writes);
            }
        }
        buf.push_str(r#""chip_ref_writer":{"#);
        push_kv_bool(&mut buf, "desired", chip_ref_desired);
        buf.push(',');
        // Compatibility: existing AEC policy consumers read `enabled` as the
        // live writer verdict. It now tells runtime truth instead of merely
        // echoing that a PCM name was configured.
        push_kv_bool(&mut buf, "enabled", chip_ref_active);
        buf.push(',');
        push_kv_bool(&mut buf, "active", chip_ref_active);
        buf.push(',');
        push_kv_str(&mut buf, "status", chip_ref_status);
        buf.push(',');
        push_kv_u64(&mut buf, "open_error_count", chip_ref_open_error_count);
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "retry_count",
            self.chip_ref_retry_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "queue_depth_periods",
            self.chip_ref_queue_depth_periods.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "queued_frames",
            self.chip_ref_queued_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "frames_written",
            self.chip_ref_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64_opt(&mut buf, "snd_pcm_delay_frames", chip_ref_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            &mut buf,
            "snd_pcm_delay_ms",
            frames_to_ms_opt(chip_ref_delay_frames, chip_ref_sample_rate),
            3,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "snd_pcm_delay_sample_age_ms",
            event_age_ms(
                uptime_ms,
                self.chip_ref_snd_pcm_delay_sample_ms
                    .load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_underrun_count",
            self.chip_ref_write_underrun_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_xrun_count",
            self.chip_ref_write_xrun_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_recovery_count",
            self.chip_ref_write_recovery_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "write_error_count",
            self.chip_ref_write_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_periods_due_to_full_queue",
            self.chip_ref_dropped_full_periods.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_periods_due_to_disconnected_writer",
            self.chip_ref_dropped_disconnected_periods
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "dropped_periods_while_unavailable",
            self.chip_ref_dropped_unavailable_periods
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_write_age_ms",
            event_age_ms(
                uptime_ms,
                self.chip_ref_last_write_ms.load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_enqueued_reference_sequence",
            chip_ref_last_enqueued_sequence,
        );
        buf.push(',');
        push_kv_u64_opt(
            &mut buf,
            "last_written_reference_sequence",
            chip_ref_last_written_sequence,
        );
        buf.push(',');
        push_kv_u64_opt(&mut buf, "reference_sequence_lag", chip_ref_sequence_lag);
        buf.push(',');
        push_kv_str_opt(
            &mut buf,
            "diagnostic_tee_path",
            self.chip_ref_diagnostic_tee_path.as_deref(),
        );
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "diagnostic_tee_active",
            self.chip_ref_tee_active.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "diagnostic_tee_open_error_count",
            self.chip_ref_tee_open_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "diagnostic_tee_write_error_count",
            self.chip_ref_tee_write_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "recent_writes_capacity",
            CHIP_REF_RECENT_WRITES as u64,
        );
        buf.push(',');
        // Oldest first. Ages are relative to THIS snapshot, so the reader can
        // place every observation on its own clock from one read.
        buf.push_str(r#""recent_writes":["#);
        for (index, observation) in chip_ref_recent_writes.iter().enumerate() {
            if index > 0 {
                buf.push(',');
            }
            buf.push('{');
            push_kv_u64(&mut buf, "frames_written", observation.frames_written);
            buf.push(',');
            push_kv_u64(&mut buf, "snd_pcm_delay_frames", observation.delay_frames);
            buf.push(',');
            push_kv_u64_opt(
                &mut buf,
                "reference_sequence",
                unpack_optional_u64(observation.reference_sequence),
            );
            buf.push(',');
            push_kv_u64(
                &mut buf,
                "age_ms",
                uptime_ms.saturating_sub(observation.uptime_ms),
            );
            buf.push('}');
        }
        buf.push(']');
        buf.push('}');
        buf.push(',');
        push_kv_str_opt(&mut buf, "udp_target", self.reference_udp_target.as_deref());
        buf.push(',');
        push_kv_bool(
            &mut buf,
            "udp_active",
            self.reference_udp_active.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "udp_error_count",
            self.reference_udp_error_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            &mut buf,
            "udp_dropped_count",
            self.reference_udp_dropped_count.load(Ordering::Relaxed),
        );
        buf.push(',');

        // Passive chip-AEC clock drift (Layer 0). Observe-only: SRO estimate,
        // a thin verdict, and the latency budget outputd already knows. No
        // audio path is affected by anything in this block.
        // Read the Copy snapshot under the lock, then build the human-readable
        // reason string AFTER releasing it — never allocate while the lock is
        // held, so a /state query can't make the chip-ref writer wait on a heap
        // allocation. (It is off the audio path either way, but this keeps the
        // lock hold to a few non-allocating reads.)
        let (sro_ppm, sro_status, verdict, poisoned) = match self.sro_estimator.lock() {
            Ok(est) => (est.sro_ppm(), est.status(), est.verdict(), false),
            // A poisoned lock should never happen (the estimator never panics),
            // but never surface a panic from /state — degrade to fallback.
            Err(_) => (
                None,
                crate::aec_clock::SroStatus::Untrusted,
                crate::aec_clock::AecClockVerdict::Fallback,
                true,
            ),
        };
        let verdict_reason = if poisoned {
            "sro estimator lock poisoned".to_string()
        } else {
            crate::aec_clock::verdict_reason_for(sro_status, sro_ppm)
        };
        let sro_status = sro_status.as_str();
        let verdict = verdict.as_str();
        // Observe-only DAC playout-clock drift (Inc 2). `try_lock` so a
        // /state read never blocks the playback loop's tick; on contention or a
        // (never-expected) poisoned lock, report a not-yet-locked idle snapshot
        // rather than waiting or panicking. We read BOTH the AEC-specific
        // snapshot (the named ppm + verdict) and the raw shared-DLL snapshot
        // (the Inc-4 rate_diff) under one lock acquisition.
        let (dac_clock, dac_clock_rate_diff) = match self.dac_clock.try_lock() {
            Ok(clock) => (clock.snapshot(), clock.dll_snapshot()),
            Err(_) => (
                crate::dac_clock::DacClockSnapshot {
                    locked: false,
                    sro_ppm: 0.0,
                    error_mean: 0.0,
                    error_var: 0.0,
                    updates: 0,
                    resync_count: 0,
                },
                DllSnapshot::idle(),
            ),
        };
        let dac_presentation_ms = frames_to_ms_opt(dac_delay_frames, sample_rate);
        let playback_queue_ms = frames_to_ms_opt(
            Some(self.dac_buffer_frames.load(Ordering::Relaxed)),
            sample_rate,
        );
        let chip_ref_queue_ms = frames_to_ms_opt(
            Some(self.chip_ref_queued_frames.load(Ordering::Relaxed)),
            chip_ref_sample_rate,
        );
        buf.push_str(r#""aec_clock":{"#);
        push_kv_f64_opt(&mut buf, "chip_ref_sro_ppm", sro_ppm, 3);
        buf.push(',');
        push_kv_str(&mut buf, "sro_estimator_status", sro_status);
        buf.push(',');
        push_kv_str(&mut buf, "verdict", verdict);
        buf.push(',');
        push_kv_str(&mut buf, "verdict_reason", &verdict_reason);
        buf.push(',');
        // Observe-only label: the chip-ref writer was armed purely to MEASURE
        // drift on the DAC playout clock vs nominal (not chip-AEC). Pure
        // self-description; no audio path reads it.
        push_kv_bool(&mut buf, "observe", self.chip_ref_observe);
        buf.push(',');
        // Observe-only DAC playout-clock drift (Inc 2): the shared
        // jasper-clock DLL locked onto the :9891-reference-vs-DAC-playout error,
        // surfaced as ppm + lock + verdict (the doctor-readable field). This
        // NEVER warps audio — it is the measure-before-fix signal for software
        // AEC, distinct from the chip-AEC `chip_ref_sro_ppm` above.
        buf.push_str(r#""dac_clock":{"#);
        // AEC-specific surface: the named ppm + the doctor-readable verdict.
        push_kv_f64(&mut buf, "dac_clock_ppm", dac_clock.sro_ppm, 3);
        buf.push(',');
        push_kv_bool(&mut buf, "locked", dac_clock.locked);
        buf.push(',');
        push_kv_str(&mut buf, "verdict", dac_clock.verdict());
        buf.push(',');
        // Shared rate_diff (Inc 4): the loop's full state in the one consistent
        // shape — error stats, bandwidth, DLL lock/resync counters — so this DLL
        // site reads identically to the content-bridge controller's. The named
        // ppm above and `rate_diff.ppm` are the same value (the AEC surface
        // keeps the named field for the doctor; the shape stays DRY).
        push_dll_rate_diff(&mut buf, "rate_diff", &dac_clock_rate_diff);
        buf.push('}');
        buf.push(',');
        buf.push_str(r#""latency":{"#);
        push_kv_f64_opt(&mut buf, "dac_presentation_ms", dac_presentation_ms, 3);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "playback_queue_ms", playback_queue_ms, 3);
        buf.push(',');
        push_kv_f64_opt(&mut buf, "chip_ref_queue_ms", chip_ref_queue_ms, 3);
        buf.push('}');
        buf.push('}');
        buf.push('}');
        buf.push(',');

        buf.push_str(r#""watchdog":{"#);
        let last_progress_ms = self.last_progress_ms.load(Ordering::Relaxed);
        let age_ms = uptime_ms.saturating_sub(last_progress_ms);
        push_kv_u64(
            &mut buf,
            "pings_sent",
            self.watchdog_pings_sent.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(&mut buf, "last_progress_age_ms", age_ms);
        buf.push('}');

        buf.push('}');
        buf
    }

    fn uptime_ms(&self) -> u64 {
        self.started_at.elapsed().as_millis() as u64
    }
}

pub struct StateServer {
    socket_path: PathBuf,
    state: Arc<OutputdState>,
    listener: UnixListener,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CommandReadError {
    TooLong,
    DeadlineExceeded,
    InvalidUtf8,
}

impl CommandReadError {
    fn response_json(self) -> String {
        match self {
            Self::TooLong => format!(
                r#"{{"error":"command too long","code":"command_too_long","max_bytes":{MAX_COMMAND_BYTES}}}"#
            ),
            Self::DeadlineExceeded => r#"{"error":"command read deadline exceeded","code":"command_read_deadline_exceeded"}"#.to_string(),
            Self::InvalidUtf8 => {
                r#"{"error":"command must be UTF-8","code":"command_not_utf8"}"#.to_string()
            }
        }
    }
}

impl StateServer {
    pub fn bind(socket_path: PathBuf, state: Arc<OutputdState>) -> Result<Self> {
        if let Some(parent) = socket_path.parent() {
            std::fs::create_dir_all(parent).with_context(|| {
                format!("creating outputd state socket parent {}", parent.display())
            })?;
        }
        let _ = std::fs::remove_file(&socket_path);
        let listener = UnixListener::bind(&socket_path)
            .with_context(|| format!("binding outputd STATUS socket {}", socket_path.display()))?;
        listener
            .set_nonblocking(true)
            .context("set_nonblocking on outputd state listener")?;
        eprintln!(
            "event=outputd.state_server.listening socket={}",
            socket_path.display()
        );

        Ok(Self {
            socket_path,
            state,
            listener,
        })
    }

    pub fn run(&self, shutdown: &AtomicBool) -> Result<()> {
        while !shutdown.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if let Err(e) = self.handle_connection(stream) {
                        eprintln!("event=outputd.state_server.handle_failed detail={e:#}");
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
                Err(e) => {
                    eprintln!("event=outputd.state_server.accept_failed detail={e}");
                    std::thread::sleep(ACCEPT_POLL_INTERVAL);
                }
            }
        }

        let _ = std::fs::remove_file(&self.socket_path);
        eprintln!("event=outputd.state_server.stopped");
        Ok(())
    }

    fn handle_connection(&self, stream: UnixStream) -> Result<()> {
        self.handle_connection_with_timeout(stream, CONNECTION_READ_TIMEOUT)
    }

    fn handle_connection_with_timeout(
        &self,
        mut stream: UnixStream,
        timeout: Duration,
    ) -> Result<()> {
        let mut response = match read_bounded_command(&mut stream, timeout) {
            Ok(Ok(command)) => self.response_for_command(command.trim()),
            Ok(Err(error)) => error.response_json(),
            Err(error) if is_client_disconnect(&error) => return Ok(()),
            Err(error) => return Err(error).context("reading outputd state command"),
        };
        response.push('\n');
        match stream.write_all(response.as_bytes()) {
            Ok(()) => Ok(()),
            Err(error) if is_client_disconnect(&error) => Ok(()),
            Err(error) => Err(error).context("writing outputd state response"),
        }
    }

    fn response_for_command(&self, command: &str) -> String {
        if command == "STATUS" {
            self.state.snapshot_json()
        } else if let Some(raw) = command.strip_prefix("SET_DAC_CONTENT_TRIM_DB ") {
            match raw.trim().parse::<f32>() {
                Ok(trim_db) => match self.state.set_dac_content_trim_db(trim_db) {
                    Ok(applied) => format!(r#"{{"ok":true,"trim_db":{applied:.1}}}"#),
                    Err(e) => format!(r#"{{"error":{}}}"#, json_string(&e.to_string())),
                },
                Err(_) => format!(
                    r#"{{"error":"trim_db must be a number","received":{}}}"#,
                    json_string(raw.trim())
                ),
            }
        } else {
            format!(
                r#"{{"error":"unknown command","received":{}}}"#,
                json_string(command)
            )
        }
    }
}

/// Read one newline-delimited command under both a byte cap and a total
/// monotonic deadline. Re-applying the *remaining* timeout before each read is
/// load-bearing: a client that trickles one byte per socket timeout cannot keep
/// outputd's single state-server thread occupied indefinitely.
fn read_bounded_command(
    stream: &mut UnixStream,
    timeout: Duration,
) -> io::Result<Result<String, CommandReadError>> {
    let started = Instant::now();
    let Some(deadline) = started.checked_add(timeout) else {
        return Ok(Err(CommandReadError::DeadlineExceeded));
    };
    let mut bytes = Vec::with_capacity(64);
    let mut chunk = [0u8; 64];

    loop {
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Ok(Err(CommandReadError::DeadlineExceeded));
        };
        if remaining.is_zero() {
            return Ok(Err(CommandReadError::DeadlineExceeded));
        }
        stream.set_read_timeout(Some(remaining))?;

        match stream.read(&mut chunk) {
            Ok(0) => {
                if Instant::now() >= deadline {
                    return Ok(Err(CommandReadError::DeadlineExceeded));
                }
                break;
            }
            Ok(read) => {
                if Instant::now() >= deadline {
                    return Ok(Err(CommandReadError::DeadlineExceeded));
                }
                let newline = chunk[..read].iter().position(|byte| *byte == b'\n');
                let command_bytes = &chunk[..newline.unwrap_or(read)];
                if bytes.len().saturating_add(command_bytes.len()) > MAX_COMMAND_BYTES {
                    return Ok(Err(CommandReadError::TooLong));
                }
                bytes.extend_from_slice(command_bytes);
                if newline.is_some() {
                    break;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) =>
            {
                return Ok(Err(CommandReadError::DeadlineExceeded));
            }
            Err(error) => return Err(error),
        }
    }

    match String::from_utf8(bytes) {
        Ok(command) => Ok(Ok(command)),
        Err(_) => Ok(Err(CommandReadError::InvalidUtf8)),
    }
}

fn is_client_disconnect(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::BrokenPipe
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::NotConnected
    )
}

fn push_kv_str(buf: &mut String, key: &str, value: &str) {
    push_key(buf, key);
    buf.push_str(&json_string(value));
}

fn push_kv_str_opt(buf: &mut String, key: &str, value: Option<&str>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&json_string(value)),
        None => buf.push_str("null"),
    }
}

fn push_kv_u64(buf: &mut String, key: &str, value: u64) {
    push_key(buf, key);
    buf.push_str(&value.to_string());
}

fn push_kv_u64_opt(buf: &mut String, key: &str, value: Option<u64>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&value.to_string()),
        None => buf.push_str("null"),
    }
}

fn push_kv_i64(buf: &mut String, key: &str, value: i64) {
    push_key(buf, key);
    buf.push_str(&value.to_string());
}

fn push_kv_i64_opt(buf: &mut String, key: &str, value: Option<i64>) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&value.to_string()),
        None => buf.push_str("null"),
    }
}

fn push_kv_bool(buf: &mut String, key: &str, value: bool) {
    push_key(buf, key);
    buf.push_str(if value { "true" } else { "false" });
}

fn push_kv_f64(buf: &mut String, key: &str, value: f64, decimals: usize) {
    push_key(buf, key);
    buf.push_str(&format!("{:.*}", decimals, value));
}

fn push_kv_f64_opt(buf: &mut String, key: &str, value: Option<f64>, decimals: usize) {
    push_key(buf, key);
    match value {
        Some(value) => buf.push_str(&format!("{:.*}", decimals, value)),
        None => buf.push_str("null"),
    }
}

fn push_key(buf: &mut String, key: &str) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":"#);
}

/// The ONE shared `clock.rate_diff` telemetry writer (Inc 4). Every DLL
/// instance in outputd publishes its loop state through this single shape, so
/// `/state` / doctor read every clock-domain boundary identically (mirrors
/// PipeWire's `clock.rate_diff`). Emits a nested object under `key`:
/// `{ppm, error_mean, error_var, bandwidth, locked, updates, lock_count,
/// unlock_count, resync_count}`.
fn push_dll_rate_diff(buf: &mut String, key: &str, snap: &DllSnapshot) {
    buf.push('"');
    buf.push_str(key);
    buf.push_str(r#"":{"#);
    push_kv_f64(buf, "ppm", snap.ratio_ppm, 3);
    buf.push(',');
    push_kv_f64(buf, "error_mean", snap.error_mean, 4);
    buf.push(',');
    push_kv_f64(buf, "error_var", snap.error_var, 4);
    buf.push(',');
    push_kv_f64(buf, "bandwidth", snap.bandwidth, 4);
    buf.push(',');
    push_kv_bool(buf, "locked", snap.locked);
    buf.push(',');
    push_kv_u64(buf, "updates", snap.updates);
    buf.push(',');
    push_kv_u64(buf, "lock_count", snap.lock_count);
    buf.push(',');
    push_kv_u64(buf, "unlock_count", snap.unlock_count);
    buf.push(',');
    push_kv_u64(buf, "resync_count", snap.resync_count);
    buf.push('}');
}

const PACKED_I64_NONE: i64 = i64::MIN;

fn pack_optional_i64(value: Option<i64>) -> i64 {
    value.unwrap_or(PACKED_I64_NONE)
}

fn unpack_optional_i64(value: i64) -> Option<i64> {
    if value == PACKED_I64_NONE {
        None
    } else {
        Some(value)
    }
}

fn unpack_optional_u64(value: u64) -> Option<u64> {
    if value == OPTIONAL_U64_NONE {
        None
    } else {
        Some(value)
    }
}

fn frames_to_ms_opt(frames: Option<u64>, sample_rate: u64) -> Option<f64> {
    if sample_rate == 0 {
        return None;
    }
    frames.map(|frames| (frames as f64) * 1000.0 / (sample_rate as f64))
}

fn subtract_saturating(value: &AtomicU64, delta: u64) {
    let _ = value.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
        Some(current.saturating_sub(delta))
    });
}

fn event_age_ms(uptime_ms: u64, event_ms: u64) -> Option<u64> {
    if event_ms == NEVER_MS {
        None
    } else {
        Some(uptime_ms.saturating_sub(event_ms))
    }
}

fn rate_per_hour(count: u64, uptime_ms: u64) -> f64 {
    if count == 0 || uptime_ms == 0 {
        return 0.0;
    }
    (count as f64) * 3_600_000.0 / (uptime_ms as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{BackendMode, Config, ContentBridgeMode, SinkMode};

    fn test_config() -> Config {
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
            period_frames: 1024,
            dac_buffer_frames: 3072,
            content_bridge_mode: ContentBridgeMode::Direct,
            shm_ring: None,
            chip_ref_pcm: None,
            chip_ref_sample_rate: 16_000,
            chip_ref_period_frames: 320,
            chip_ref_buffer_frames: 4096,
            chip_ref_observe: false,
            chip_ref_tee_path: None,
            reference_udp_target: None,
            control_socket_path: None,
            dac_content_fifo: None,
            dac_content_ring: None,
            dac_content_channel: crate::dac_content::ChannelPick::Stereo,
            dac_content_trim_db: 0.0,
            tts_socket_path: None,
            tts_max_pending_frames: crate::tts::DEFAULT_MAX_PENDING_FRAMES,
            tts_program_duck_db: -25.0,
            assistant_reference_path: "/var/lib/jasper/outputd_assistant_volume_reference.json"
                .to_string(),
            active_lane: false,
            // ACTIVE_LANE's pair. False is the passive/stereo default a FLAT box
            // runs. `dual_test_config` below overrides ring_active_endpoint rather
            // than inheriting it, because a composite sink CAN be an active-ring
            // endpoint.
            ring_active_endpoint: false,
        }
    }

    fn dual_test_config() -> Config {
        Config {
            sink_mode: SinkMode::Composite,
            // The armed composite shape: the ring endpoint, and NO content PCM.
            // A composite on the direct bridge refuses at parse, so a composite
            // that is running at all is a ring composite.
            ring_active_endpoint: true,
            content_channels: 4,
            dac_pcm: "dual_apple_usb_c_dac_4ch".to_string(),
            dual_dac_a_pcm: Some("hw:CARD=A,DEV=0".to_string()),
            dual_dac_b_pcm: Some("hw:CARD=B,DEV=0".to_string()),
            ..test_config()
        }
    }

    fn test_state_server(state: Arc<OutputdState>) -> StateServer {
        static NEXT_SOCKET_ID: AtomicU64 = AtomicU64::new(0);

        let id = NEXT_SOCKET_ID.fetch_add(1, Ordering::Relaxed);
        // Keep the AF_UNIX path short on macOS (sun_path is only 104 bytes).
        let path = PathBuf::from(format!("/tmp/jts-outputd-{}-{id}.sock", std::process::id()));
        let server = StateServer::bind(path.clone(), state).expect("bind test state server");
        // handle_connection only needs the state; unlink the otherwise-unused
        // listener path immediately so a test panic cannot leave filesystem
        // residue behind. The bound listener remains valid until server drops.
        std::fs::remove_file(path).expect("unlink test state socket");
        server
    }

    fn exchange_command(server: &StateServer, command: &[u8]) -> String {
        use std::net::Shutdown;

        let (mut client, server_stream) = UnixStream::pair().expect("socket pair");
        client.write_all(command).expect("write command");
        client
            .shutdown(Shutdown::Write)
            .expect("finish command write");
        server
            .handle_connection(server_stream)
            .expect("handle command");

        let mut response = String::new();
        client.read_to_string(&mut response).expect("read response");
        response
    }

    fn parse_snapshot_json(snapshot: &str) -> serde_json::Value {
        serde_json::from_str(snapshot)
            .unwrap_or_else(|error| panic!("complete STATUS snapshot must be valid JSON: {error}"))
    }

    #[test]
    fn state_server_wire_contract_returns_valid_json_for_status_trim_and_errors() {
        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Left,
            ..test_config()
        };
        let state = Arc::new(OutputdState::new(&cfg));
        let server = test_state_server(Arc::clone(&state));

        let status = parse_snapshot_json(exchange_command(&server, b"STATUS\n").trim());
        assert_eq!(status["backend"].as_str(), Some("alsa"));

        let applied: serde_json::Value = serde_json::from_str(
            exchange_command(&server, b"SET_DAC_CONTENT_TRIM_DB -3.54\n").trim(),
        )
        .expect("trim response must be valid JSON");
        assert_eq!(applied["ok"].as_bool(), Some(true));
        assert_eq!(applied["trim_db"].as_f64(), Some(-3.5));
        assert_eq!(state.dac_content_trim_db(), -3.5);

        let malformed: serde_json::Value = serde_json::from_str(
            exchange_command(&server, b"SET_DAC_CONTENT_TRIM_DB nope\n").trim(),
        )
        .expect("malformed trim response must be valid JSON");
        assert_eq!(
            malformed["error"].as_str(),
            Some("trim_db must be a number")
        );
        assert_eq!(malformed["received"].as_str(), Some("nope"));

        let unknown: serde_json::Value =
            serde_json::from_str(exchange_command(&server, b"NOT_A_COMMAND\n").trim())
                .expect("unknown-command response must be valid JSON");
        assert_eq!(unknown["error"].as_str(), Some("unknown command"));
        assert_eq!(unknown["received"].as_str(), Some("NOT_A_COMMAND"));
    }

    // Pre-existing macOS-only artifact, localized to the abandoned-client
    // block at the end of this test (the `drop(abandoned)` case): once a
    // UnixStream::pair() peer has both directions closed, XNU's
    // sosetopt(SO_RCVTIMEO) -- reached from read_bounded_command's
    // per-loop set_read_timeout call -- returns EINVAL. Every earlier
    // exchange in this test only closes the write half
    // (`shutdown(Write)`) and passes.
    // CI's `rust` job runs on ubuntu-latest and is the authority; the test
    // runs there unaffected. jasper-outputd's hard `alsa` dependency also
    // does not build on macOS at all via the usual Homebrew path; this
    // `ignore` therefore only bites when ALSA is available locally *and*
    // the run is lib-scoped (`cargo test --lib`) — a full `cargo test` on
    // macOS fails earlier compiling main.rs's test module
    // (`libc::SOCK_CLOEXEC` is Linux/BSD-only).
    #[test]
    #[cfg_attr(
        target_os = "macos",
        ignore = "pre-existing AF_UNIX EINVAL on macOS (abandoned-client SO_RCVTIMEO); CI (Linux) is authoritative -- see comment above"
    )]
    fn state_server_command_cap_is_exact_and_errors_stay_bounded() {
        let server = test_state_server(Arc::new(OutputdState::new(&test_config())));

        let mut at_cap = vec![b'X'; MAX_COMMAND_BYTES];
        at_cap.push(b'\n');
        let accepted: serde_json::Value =
            serde_json::from_str(exchange_command(&server, &at_cap).trim())
                .expect("at-cap unknown command must return JSON");
        assert_eq!(accepted["error"].as_str(), Some("unknown command"));
        assert_eq!(
            accepted["received"].as_str().map(str::len),
            Some(MAX_COMMAND_BYTES)
        );

        let mut over_cap = vec![b'X'; MAX_COMMAND_BYTES + 1];
        over_cap.push(b'\n');
        let rejected_response = exchange_command(&server, &over_cap);
        let rejected: serde_json::Value = serde_json::from_str(rejected_response.trim())
            .expect("oversized command must return JSON");
        assert_eq!(rejected["code"].as_str(), Some("command_too_long"));
        assert_eq!(
            rejected["max_bytes"].as_u64(),
            Some(MAX_COMMAND_BYTES as u64)
        );
        assert!(
            rejected_response.len() < 128,
            "oversized input must not be echoed into an oversized response"
        );

        let invalid_utf8: serde_json::Value =
            serde_json::from_str(exchange_command(&server, &[0xff, b'\n']).trim())
                .expect("invalid UTF-8 must return JSON");
        assert_eq!(invalid_utf8["code"].as_str(), Some("command_not_utf8"));

        // A client that disappears before sending a command is normal local
        // IPC churn, not a daemon fault that should emit handle_failed spam.
        let (abandoned, server_stream) = UnixStream::pair().expect("socket pair");
        drop(abandoned);
        server
            .handle_connection(server_stream)
            .expect("abandoned client is handled quietly");
    }

    #[test]
    fn state_server_total_deadline_rejects_a_slow_trickle() {
        let server = test_state_server(Arc::new(OutputdState::new(&test_config())));
        let (mut client, server_stream) = UnixStream::pair().expect("socket pair");

        let writer = std::thread::spawn(move || {
            client
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound test response read");
            for byte in b"STATUS\n" {
                if client.write_all(&[*byte]).is_err() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            let mut response = String::new();
            client
                .read_to_string(&mut response)
                .expect("read deadline response");
            response
        });

        let started = Instant::now();
        server
            .handle_connection_with_timeout(server_stream, Duration::from_millis(50))
            .expect("slow client receives a structured error");
        let elapsed = started.elapsed();
        let response = writer.join().expect("slow writer thread");
        let error: serde_json::Value =
            serde_json::from_str(response.trim()).expect("deadline response must be JSON");

        assert_eq!(
            error["code"].as_str(),
            Some("command_read_deadline_exceeded")
        );
        assert!(
            elapsed < Duration::from_millis(500),
            "total deadline must not reset for each trickled byte: {elapsed:?}"
        );
    }

    #[test]
    fn snapshot_json_contains_outputd_health_fields() {
        let state = OutputdState::new(&test_config());
        state.mark_period(
            IoCounters {
                content_frames_read: 2048,
                content_empty_period_count: 4,
                content_partial_period_count: 5,
                content_eagain_count: 6,
                dac_frames_written: 1024,
                content_xrun_count: 1,
                dac_xrun_count: 2,
            },
            42,
            3,
        );

        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""backend":"alsa""#,
            // The content lane's own declared/negotiated hop sits right after
            // its pcm, mirroring `dac`. Updated deliberately when
            // `content.format` was added: the block's prefix is the contract
            // consumers read, so a new field belongs IN this needle, not around
            // it.
            r#""content":{"source":"alsa","format":"S16_LE""#,
            r#""content_bridge":{"mode":"direct""#,
            r#""dac":{"pcm":"outputd_dac","format":"S16_LE""#,
            r#""sample_rate":48000"#,
            r#""period_frames":1024"#,
            r#""frames_read":2048"#,
            r#""empty_periods":4"#,
            r#""partial_periods":5"#,
            r#""eagain_count":6"#,
            r#""last_xrun_age_ms":"#,
            r#""xrun_rate_per_hour":"#,
            r#""frames_written":1024"#,
            r#""snd_pcm_delay_frames":null"#,
            r#""snd_pcm_delay_ms":null"#,
            r#""snd_pcm_delay_sample_age_ms":null"#,
            r#""reference_sequence":42"#,
            r#""last_period_clipped_samples":3"#,
            r#""clipped_samples":3"#,
            r#""reference_outputs":{"speaker_reference_source":"outputd_final_electrical""#,
            r#""chip_ref_transform":"stereo_mean_boxcar_decimate_dual_mono_v1""#,
            r#""chip_ref_pcm":null"#,
            r#""chip_ref_sample_rate":16000"#,
            r#""chip_ref_period_frames":320"#,
            r#""chip_ref_buffer_frames":4096"#,
            r#""chip_ref_writer":{"desired":false,"enabled":false,"active":false,"status":"disabled""#,
            r#""queue_depth_periods":0"#,
            r#""queued_frames":0"#,
            r#""dropped_periods_due_to_full_queue":0"#,
            r#""last_enqueued_reference_sequence":null"#,
            r#""last_written_reference_sequence":null"#,
            r#""reference_sequence_lag":null"#,
            r#""diagnostic_tee_path":null"#,
            r#""diagnostic_tee_active":false"#,
            r#""diagnostic_tee_open_error_count":0"#,
            r#""diagnostic_tee_write_error_count":0"#,
            r#""udp_target":null"#,
            r#""watchdog""#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        // PR-2 (Increment 5) UN-RETIRED the outputd TTS lane that 9102e13
        // removed — this assertion used to pin its absence; it now pins
        // the solo shape: present but disabled (fanin owns solo TTS).
        assert!(
            j.contains(r#""tts":{"enabled":false}"#),
            "solo tts block must be present-but-disabled in {j}"
        );
        assert!(
            !j.contains(r#""assistant_loudness":"#),
            "duplicate outputd loudness state present in {j}"
        );
    }

    #[test]
    fn snapshot_json_reports_the_content_deaf_verdict_and_the_taps_drops() {
        let cfg = Config {
            reference_udp_target: Some("127.0.0.1:9891".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        let idle = parse_snapshot_json(&state.snapshot_json());
        assert_eq!(idle["content"]["consecutive_empty_periods"], 0);
        assert_eq!(idle["content"]["deaf"], false);
        assert_eq!(idle["reference_outputs"]["udp_dropped_count"], 0);

        state.mark_content_fill(94, true);
        state.mark_reference_udp_dropped();
        let deaf = parse_snapshot_json(&state.snapshot_json());
        assert_eq!(deaf["content"]["consecutive_empty_periods"], 94);
        assert_eq!(deaf["content"]["deaf"], true);
        assert_eq!(deaf["reference_outputs"]["udp_dropped_count"], 1);
    }

    /// Every JSON key the STATUS payload exposes, at any nesting depth.
    fn snapshot_key_names(state: &OutputdState) -> std::collections::BTreeSet<String> {
        fn walk(value: &serde_json::Value, names: &mut std::collections::BTreeSet<String>) {
            match value {
                serde_json::Value::Object(fields) => {
                    for (name, child) in fields {
                        names.insert(name.clone());
                        walk(child, names);
                    }
                }
                serde_json::Value::Array(items) => {
                    for item in items {
                        walk(item, names);
                    }
                }
                _ => {}
            }
        }

        let mut names = std::collections::BTreeSet::new();
        walk(&parse_snapshot_json(&state.snapshot_json()), &mut names);
        names
    }

    #[test]
    fn snapshot_json_emits_every_key_the_python_status_consumers_read() {
        // jasper/audio_validation.py, jasper/cli/doctor/audio_runtime_outputd.py,
        // jasper/control/state_aggregate.py (/state) and
        // jasper/control/audio_health.py (the Audio view) read STATUS through
        // fail-soft `.get()` chains, so a renamed key never throws — it degrades
        // to null and quietly blanks a check. Names only: WHERE each key sits is
        // pinned on the Python side, by
        // `test_python_status_reads_match_the_rust_emitters_nesting` in
        // tests/test_wire_contracts.py. Built on the box shape
        // that emits every optional block (chip-reference writer armed and
        // observing, UDP reference target set) so no key is missing merely because
        // its section was not reached.
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            chip_ref_observe: true,
            reference_udp_target: Some("127.0.0.1:9891".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_chip_ref_writer_active(true);
        state.mark_chip_ref_write(ChipRefWrite {
            frames_written: 320,
            delay_frames: Some(400),
            reference_sequence: Some(1),
            ..ChipRefWrite::default()
        });

        let names = snapshot_key_names(&state);
        for key in [
            "backend",
            "sink_mode",
            "content",
            "deaf",
            "mix",
            "clipped_samples",
            "tts",
            "watchdog",
            "last_progress_age_ms",
            "dac",
            "pcm",
            "sample_rate",
            "period_frames",
            "buffer_frames",
            "frames_written",
            "xrun_count",
            "reference_outputs",
            "speaker_reference_source",
            "speaker_reference_active",
            "speaker_reference_channels",
            "udp_target",
            "chip_ref_pcm",
            "chip_ref_sample_rate",
            "chip_ref_period_frames",
            "chip_ref_buffer_frames",
            "chip_ref_writer",
            "desired",
            "enabled",
            "active",
            "status",
            "open_error_count",
            "retry_count",
            "queue_depth_periods",
            "queued_frames",
            "write_underrun_count",
            "write_xrun_count",
            "write_recovery_count",
            "write_error_count",
            "dropped_periods_due_to_full_queue",
            "dropped_periods_due_to_disconnected_writer",
            "dropped_periods_while_unavailable",
            "last_write_age_ms",
            "last_enqueued_reference_sequence",
            "last_written_reference_sequence",
            "reference_sequence_lag",
            "recent_writes",
            "recent_writes_capacity",
            "age_ms",
            "snd_pcm_delay_frames",
            "snd_pcm_delay_ms",
            "snd_pcm_delay_sample_age_ms",
            "diagnostic_tee_path",
            "diagnostic_tee_active",
            "diagnostic_tee_open_error_count",
            "diagnostic_tee_write_error_count",
            "aec_clock",
            "chip_ref_sro_ppm",
            "sro_estimator_status",
            "verdict",
            "verdict_reason",
            "observe",
            "latency",
            "dac_presentation_ms",
            "playback_queue_ms",
            "chip_ref_queue_ms",
        ] {
            assert!(
                names.contains(key),
                "outputd STATUS no longer emits {key:?}; emitted {names:?}"
            );
        }
    }

    #[test]
    fn snapshot_json_echoes_the_declared_dac_edge_format_before_the_sink_opens() {
        // The chip-AEC alignment identity reads dac.format out of STATUS, so a
        // declared S32_LE edge (the InnoMaker HiFi AMP Pro) must reach the wire
        // verbatim — never substituted by some other width outputd happens to
        // know about. (Its internal program is `ProgramSample`/i32 and has no
        // ALSA format at all, so there is nothing here for the declaration to
        // collapse to; this asserts the declaration is echoed, not derived.)
        // No sink has opened here (the fake backend never opens one), so the
        // declaration is all outputd knows.
        let state = OutputdState::new(&Config {
            declared_dac_format: SampleFormat::S32Le,
            ..test_config()
        });

        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""dac":{"pcm":"outputd_dac","format":"S32_LE""#),
            "declared S32_LE edge missing from {j}"
        );
    }

    #[test]
    fn snapshot_json_reports_the_negotiated_edge_format_once_the_sink_opens() {
        // The identity must certify against the edge outputd IS running, not
        // the one the registry declared. Where they disagree — the shape a
        // silently-converting ALSA plug would produce, or a device that
        // installed a width other than the one requested — the readback wins.
        let state = OutputdState::new(&Config {
            declared_dac_format: SampleFormat::S32Le,
            ..test_config()
        });

        state.set_dac_format(SampleFormat::S16Le);

        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""dac":{"pcm":"outputd_dac","format":"S16_LE""#),
            "negotiated S16_LE edge missing from {j}"
        );
    }

    #[test]
    fn snapshot_json_carries_the_packed_24_edge_on_both_the_declared_and_negotiated_paths() {
        // `dac.format` is a chip-AEC alignment identity input, so the new variant
        // has to reach the wire spelled `S24_3LE` through BOTH paths — the
        // declaration echoed before any sink opens, and the readback once one
        // has. Neither path may collapse an unfamiliar width to a familiar one.
        //
        // The single Apple USB-C dongle profile declares this edge, so the
        // spelling is live on real hardware rather than hypothetical. If it ever
        // prints `S243LE` (alsa-rs's own enum spelling) or `S24_LE`, every
        // commissioning run on that hardware certifies against a name nothing
        // else in the fleet uses.
        let declared = OutputdState::new(&Config {
            declared_dac_format: SampleFormat::S24_3Le,
            content_format: SampleFormat::S24_3Le,
            ..test_config()
        });
        let j = declared.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""dac":{"pcm":"outputd_dac","format":"S24_3LE""#),
            "declared S24_3LE edge missing from {j}"
        );
        assert!(
            j.contains(r#""format":"S24_3LE""#),
            "declared S24_3LE content lane missing from {j}"
        );

        let negotiated = OutputdState::new(&test_config());
        negotiated.set_dac_format(SampleFormat::S24_3Le);
        let j = negotiated.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""dac":{"pcm":"outputd_dac","format":"S24_3LE""#),
            "negotiated S24_3LE edge missing from {j}"
        );
    }

    #[test]
    fn negotiated_dac_format_is_recorded_once_and_never_moves_under_a_reader() {
        // `dac.format` is an identity input. There is exactly one negotiation,
        // so a later call is ignored rather than allowed to change the answer
        // a commissioning run already read.
        let state = OutputdState::new(&test_config());

        state.set_dac_format(SampleFormat::S32Le);
        state.set_dac_format(SampleFormat::S16Le);

        assert!(
            state.snapshot_json().contains(r#""format":"S32_LE""#),
            "first negotiated value must stand"
        );
    }

    #[test]
    fn snapshot_json_echoes_the_declared_content_lane_format_before_the_lane_opens() {
        // The content hop is declared independently of the DAC edge, so STATUS
        // must carry it verbatim — including the mixed shape this fixture pins
        // (wide content lane, S16 hardware edge), which is exactly what an
        // S16-only USB dongle build looks like once the lane goes wide. No lane
        // is open here (the fake backend never opens one).
        let state = OutputdState::new(&Config {
            content_format: SampleFormat::S32Le,
            declared_dac_format: SampleFormat::S16Le,
            ..test_config()
        });

        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""content":{"source":"alsa","format":"S32_LE""#),
            "declared S32_LE content lane missing from {j}"
        );
        assert!(
            j.contains(r#""dac":{"pcm":"outputd_dac","format":"S16_LE""#),
            "the DAC edge must not inherit the content lane's width in {j}"
        );
    }

    #[test]
    fn snapshot_json_contains_dual_apple_runtime_health() {
        let state = OutputdState::new(&dual_test_config());
        state.mark_dual_apple_status(&CompositeStatus {
            dac_a_pcm: "hw:CARD=A,DEV=0".to_string(),
            dac_b_pcm: "hw:CARD=B,DEV=0".to_string(),
            linked: true,
            delay_delta_frames: Some(7),
            delay_delta_baseline_frames: Some(5),
            delay_delta_error_frames: Some(2),
            max_delay_delta_frames: 2,
            dac_a_xruns: 31,
            dac_b_xruns: 17,
            group_recoveries: 43,
            delay_baseline_relatches: 29,
            reprime_alignment_failures: 11,
        });

        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""sink_mode":"dual_apple""#,
            r#""dual_apple":{"dac_a_pcm":"hw:CARD=A,DEV=0""#,
            r#""dac_b_pcm":"hw:CARD=B,DEV=0""#,
            r#""linked":true"#,
            r#""delay_delta_frames":7"#,
            r#""delay_delta_baseline_frames":5"#,
            r#""delay_delta_error_frames":2"#,
            r#""max_delay_delta_frames":2"#,
            // The #2255 recovery bookkeeping. Pairwise-distinct values, and
            // none of them a small integer that another field in this snapshot
            // also happens to hold: a wire-up that stored the same source into
            // two keys, or read the wrong atomic, has to show up as a wrong
            // NUMBER rather than coincide with a right one.
            r#""dac_a_xruns":31"#,
            r#""dac_b_xruns":17"#,
            r#""group_recoveries":43"#,
            r#""delay_baseline_relatches":29"#,
            r#""reprime_alignment_failures":11"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn dual_apple_recovery_counters_are_absent_from_a_single_dac_snapshot() {
        // The recovery bookkeeping rides the already-conditional `dual_apple`
        // block, so a single-DAC box's snapshot must not grow a byte of it.
        // Asserted rather than assumed: the block is conditional, and a key
        // pushed one `}` too late would land in every box's `/state`.
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            "dual_apple",
            "dac_a_xruns",
            "dac_b_xruns",
            "group_recoveries",
            "delay_baseline_relatches",
            "reprime_alignment_failures",
        ] {
            assert!(!j.contains(needle), "unexpected {needle} in {j}");
        }
    }

    #[test]
    fn a_composite_reports_the_width_its_children_negotiated_not_a_constant() {
        // STATUS honesty for the composite transport. Until PR-5,
        // `RuntimeAlsaSink::dac_format` returned a hardcoded `S16Le` for this
        // sink, so a composite that negotiated anything else reported S16_LE —
        // to `/state`, to the doctor, and to the chip-AEC alignment identity,
        // which certifies against `dac.format`. Both halves are asserted here:
        // the DECLARED echo before the sink opens, and the NEGOTIATED value
        // after, on the same composite config.
        let state = OutputdState::new(&Config {
            declared_dac_format: SampleFormat::S32Le,
            ..dual_test_config()
        });

        let declared = state.snapshot_json();
        let _ = parse_snapshot_json(&declared);
        assert!(
            declared.contains(r#""dac":{"pcm":"dual_apple_usb_c_dac_4ch","format":"S32_LE""#),
            "a composite's declared child width must reach the wire: {declared}"
        );

        // What the children actually came up at wins, exactly as it does for the
        // coherent sink — including this half-negotiated shape, where a wide
        // declaration met two dongles that only offer S16.
        state.set_dac_format(SampleFormat::S16Le);
        let negotiated = state.snapshot_json();
        let _ = parse_snapshot_json(&negotiated);
        assert!(
            negotiated.contains(r#""dac":{"pcm":"dual_apple_usb_c_dac_4ch","format":"S16_LE""#),
            "a composite's negotiated child width must win: {negotiated}"
        );
    }

    #[test]
    fn the_ring_content_hop_reports_one_shape_on_both_sinks() {
        // The /state `content` block is ONE vocabulary across both sinks —
        // never a second shape invented for the composite.
        //
        // What the ring hop means at this layer, on either sink: `source` is
        // shm_ring; `format` is the DECLARATION, because the ring's own attach
        // is what validates it (nothing here negotiates); and `buffer_frames`
        // is the period-sized SYNTHETIC, not a jitter buffer, with the true
        // ring capacity carried beside it in `content.ring`.
        //
        // Both states are driven identically here, so any field where the
        // composite answered differently would show up as a diff.
        let ring = |base: Config| Config {
            content_bridge_mode: ContentBridgeMode::ShmRing,
            shm_ring: Some(crate::config::ShmRingConfig {
                path: "/dev/shm/jts-ring/active.ring".to_string(),
                n_slots: 2,
            }),
            ..base
        };
        let coherent = OutputdState::new(&ring(test_config()));
        let composite = OutputdState::new(&ring(dual_test_config()));
        // The stand-in `AlsaBackend::new` and `PairedCompositeSink::new` both
        // report in place of a lane neither opens: `buffer_frames` ==
        // `period_frames` (see `synthetic_content_negotiated`, their shared
        // owner).
        let synthetic = NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 1024,
            buffer_frames: 1024,
        };
        let dac = NegotiatedPcm {
            sample_rate: 48_000,
            period_frames: 1024,
            buffer_frames: 3072,
        };
        coherent.set_negotiated(synthetic, dac);
        composite.set_negotiated(synthetic, dac);

        let a = parse_snapshot_json(&coherent.snapshot_json())["content"].clone();
        let b = parse_snapshot_json(&composite.snapshot_json())["content"].clone();

        assert_eq!(a, b, "the content hop must report ONE shape on both sinks");

        // And the shape is the ring's, not merely a matching pair of wrong
        // blocks: the source names the ring, the format is the declaration, and
        // buffer_frames is the period-sized synthetic sitting beside the ring's
        // true capacity.
        assert_eq!(b["source"], "shm_ring");
        assert_eq!(b["format"], "S16_LE");
        assert_eq!(b["period_frames"], 1024);
        assert_eq!(b["buffer_frames"], 1024);
        assert_eq!(b["ring"]["capacity_frames"], 2048);
    }

    #[test]
    fn snapshot_json_dac_content_disabled_is_quiet_and_enabled_is_full() {
        // Solo (lane unconfigured): just enabled:false — zero noise.
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""dac_content":{"enabled":false}"#),
            "missing quiet disabled block in {j}"
        );

        // Member (lane configured): full daemon-truth health block.
        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Left,
            dac_content_trim_db: -3.5,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_dac_content(DacContentMetrics {
            serving_fifo: true,
            fifo_periods: 100,
            starved_periods: 7,
            staged_periods: 3,
            overflow_dropped_periods: 1,
            open_failures: 4,
            read_failures: 5,
        });
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""dac_content":{"enabled":true"#,
            r#""transport":"fifo""#,
            r#""trim_db":-3.5"#,
            r#""fifo":"/run/jasper-grouping/member-content.fifo""#,
            r#""channel":"left""#,
            r#""serving_fifo":true"#,
            r#""fifo_periods":100"#,
            r#""starved_periods":7"#,
            r#""staged_periods":3"#,
            r#""overflow_dropped_periods":1"#,
            r#""open_failures":4"#,
            r#""read_failures":5"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    /// The ring transport's block names its own path key and keeps every field
    /// the pair-lock verdict reads — and the box says the return lane is where
    /// its content comes from, with no central ring beside it.
    ///
    /// `serving_fifo` is the load-bearing one: `jasper.multiroom.state` and
    /// `jasper.control.grouping_supervisor` read it to decide whether bytes are
    /// flowing, and they must keep working when a member moves onto the ring.
    #[test]
    fn snapshot_json_dac_content_ring_keeps_the_pair_lock_fields() {
        let cfg = Config {
            dac_content_ring: Some(crate::config::DEFAULT_DAC_CONTENT_RING_PATH.to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Right,
            content_bridge_mode: ContentBridgeMode::DacContentRing,
            shm_ring: None,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_dac_content(DacContentMetrics {
            serving_fifo: true,
            fifo_periods: 11,
            starved_periods: 2,
            staged_periods: 0,
            overflow_dropped_periods: 0,
            open_failures: 0,
            read_failures: 0,
        });
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""dac_content":{"enabled":true"#,
            r#""transport":"ring""#,
            r#""ring":"/dev/shm/jts-ring/dac-content.ring""#,
            r#""channel":"right""#,
            r#""serving_fifo":true"#,
            r#""starved_periods":2"#,
            // The content source, named — and the central hop reported OFF, so
            // the two blocks cannot both read as this box's upstream.
            r#""content_bridge":{"mode":"dac_content_ring"}"#,
            r#""shm_ring":{"enabled":false}"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        // A ring box must NOT claim a FIFO path it does not have.
        assert!(
            !j.contains(r#""fifo":"#),
            "ring block spelled a fifo key: {j}"
        );
    }

    #[test]
    fn snapshot_json_shm_ring_disabled_is_quiet_and_enabled_is_full() {
        // Default-off proof: no shm_ring config -> just enabled:false, zero
        // noise, and the content source stays "alsa".
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""shm_ring":{"enabled":false}"#),
            "missing quiet disabled shm_ring block in {j}"
        );
        assert!(j.contains(r#""content":{"source":"alsa""#), "{j}");

        // Enabled (flag armed): full daemon-truth block, content source flips.
        let cfg = Config {
            content_bridge_mode: ContentBridgeMode::ShmRing,
            shm_ring: Some(crate::config::ShmRingConfig {
                path: "/dev/shm/jts-ring/content.ring".to_string(),
                n_slots: 2,
            }),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        // Before the reader attaches, the wire is the declaration — the same
        // key `ShmRingSource` builds the ring geometry from. Read through the
        // PARSED block, not a substring: `"format":"S16_LE"` also appears in
        // `content` and `dac`, so a `contains` needle for it would pass on a
        // ring block that carried no wire at all.
        let ring = parse_snapshot_json(&state.snapshot_json())["shm_ring"].clone();
        assert_eq!(ring["format"], "S16_LE");
        assert_eq!(ring["channels"], 2);
        // Attach publishes the wire the reader actually holds.
        state.mark_shm_ring_wire(SampleFormat::S16Le, 2);
        // Empty-read (startup) period: silence path increments startup_empty.
        state.mark_shm_ring(RingMetrics {
            attached: true,
            occupancy: 0,
            frames_read: 0,
            startup_empty_reads: 4,
            empty_reads: 0,
            epoch_resets: 0,
            reader_resyncs: 0,
            attach_resyncs: 1,
            writer_pid: 0,
            writer_heartbeat_age_ms: u64::MAX,
            writer_alive: false,
            n_slots: 2,
            slot_frames: 1024,
            ..RingMetrics::default()
        });
        let j = state.snapshot_json();
        let parsed = parse_snapshot_json(&j);
        // The ring WIRE — ring v2's two per-box axes — read off the attached
        // reader, through the parsed block for the reason above.
        assert_eq!(parsed["shm_ring"]["format"], "S16_LE");
        assert_eq!(parsed["shm_ring"]["channels"], 2);
        for needle in [
            r#""content":{"source":"shm_ring""#,
            // Ring B honesty contract: content.ring reports the TRUE Ring B
            // capacity (n_slots x slot_frames = 2 x 1024 = 2048) next to the
            // synthetic content.buffer_frames, so no consumer is misled and the
            // doctor validates ring geometry instead of the ALSA jitter floor.
            r#""ring":{"slots":2,"slot_frames":1024,"capacity_frames":2048}"#,
            r#""shm_ring":{"enabled":true"#,
            r#""path":"/dev/shm/jts-ring/content.ring""#,
            r#""attached":true"#,
            r#""slots":2"#,
            r#""slot_frames":1024"#,
            r#""occupancy":0"#,
            r#""startup_empty_reads":4"#,
            r#""attach_resyncs":1"#,
            r#""writer_alive":false"#,
            // The u64::MAX "never heartbeated" sentinel serializes as
            // JSON null, not 18446744073709551615 (which exceeds JS safe-integer
            // range and would deserialize lossily in the /state dashboard).
            r#""writer_heartbeat_age_ms":null"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        assert!(
            !j.contains("18446744073709551615"),
            "raw u64::MAX sentinel must never leak into /state JSON: {j}"
        );

        // A filled period with a live writer: occupancy + frames + liveness.
        state.mark_shm_ring(RingMetrics {
            attached: true,
            occupancy: 1,
            frames_read: 2048,
            startup_empty_reads: 4,
            empty_reads: 3,
            epoch_resets: 1,
            reader_resyncs: 0,
            attach_resyncs: 1,
            writer_pid: 4242,
            writer_heartbeat_age_ms: 12,
            writer_alive: true,
            n_slots: 2,
            slot_frames: 1024,
            ..RingMetrics::default()
        });
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""occupancy":1"#,
            r#""frames_read":2048"#,
            r#""empty_reads":3"#,
            r#""epoch_resets":1"#,
            r#""writer_pid":4242"#,
            r#""writer_heartbeat_age_ms":12"#,
            r#""writer_alive":true"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    /// The ring wire is DATA, not two constants spelled into the emitter.
    ///
    /// The stereo case above cannot tell a published wire from a hardcoded one:
    /// `S16_LE` and `2` are also this crate's defaults everywhere else. A wide
    /// roleful shape shares neither value with the declaration it was built
    /// from, so it is what pins that both axes carry what the reader reports.
    #[test]
    fn snapshot_json_shm_ring_reports_a_wide_wire_as_the_reader_holds_it() {
        let cfg = Config {
            content_bridge_mode: ContentBridgeMode::ShmRing,
            content_channels: 6,
            content_format: SampleFormat::S32Le,
            shm_ring: Some(crate::config::ShmRingConfig {
                path: "/dev/shm/jts-ring/content.ring".to_string(),
                n_slots: 4,
            }),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_shm_ring_wire(SampleFormat::S32Le, 6);
        state.mark_shm_ring(RingMetrics {
            attached: true,
            n_slots: 4,
            slot_frames: 1024,
            ..RingMetrics::default()
        });
        let ring = parse_snapshot_json(&state.snapshot_json())["shm_ring"].clone();
        assert_eq!(ring["format"], "S32_LE");
        assert_eq!(ring["channels"], 6);
        assert_eq!(ring["slots"], 4);
        assert_eq!(ring["slot_frames"], 1024);

        // Marking a SECOND time is a programming error and is dropped — both
        // axes together, because they are one cell. A spurious re-mark must not
        // be able to replace the wire a live reader is running, and a pair in
        // two cells could land half of one.
        state.mark_shm_ring_wire(SampleFormat::S16Le, 2);
        let ring = parse_snapshot_json(&state.snapshot_json())["shm_ring"].clone();
        assert_eq!(ring["format"], "S32_LE");
        assert_eq!(ring["channels"], 6);
    }

    #[test]
    fn dac_content_trim_can_update_live_without_restarting_outputd() {
        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Right,
            dac_content_trim_db: 0.0,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);

        assert_eq!(state.dac_content_trim_db(), 0.0);
        assert_eq!(state.dac_content_trim_gain(), 1.0);

        assert_eq!(state.set_dac_content_trim_db(-3.54).unwrap(), -3.5);

        let expected_gain = 10f32.powf(-3.5 / 20.0);
        assert_eq!(state.dac_content_trim_db(), -3.5);
        assert!((state.dac_content_trim_gain() - expected_gain).abs() < 0.000_001);
        assert!(state.snapshot_json().contains(r#""trim_db":-3.5"#));
    }

    #[test]
    fn dac_content_trim_live_update_rejects_disabled_or_boosting_lane() {
        let state = OutputdState::new(&test_config());
        assert!(state.set_dac_content_trim_db(-1.0).is_err());

        let cfg = Config {
            dac_content_fifo: Some("/run/jasper-grouping/member-content.fifo".to_string()),
            dac_content_channel: crate::dac_content::ChannelPick::Left,
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        assert!(state.set_dac_content_trim_db(0.1).is_err());
        assert!(state.set_dac_content_trim_db(-24.1).is_err());
        assert!(state.set_dac_content_trim_db(f32::NAN).is_err());
        assert_eq!(state.dac_content_trim_db(), 0.0);
    }

    #[test]
    fn snapshot_json_tts_disabled_is_quiet_and_enabled_is_full() {
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""tts":{"enabled":false}"#),
            "missing quiet disabled tts block in {j}"
        );

        let state = OutputdState::new(&test_config());
        let metrics = TtsMetrics::new(96_000);
        metrics.pending_frames.store(123, Ordering::Relaxed);
        metrics.flushed_frames.store(7, Ordering::Relaxed);
        state.set_tts("/run/jasper-outputd/tts.sock".to_string(), metrics);
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        for needle in [
            r#""tts":{"enabled":true"#,
            r#""socket":"/run/jasper-outputd/tts.sock""#,
            r#""pending_frames":123"#,
            r#""budget_frames":96000"#,
            r#""flushed_frames":7"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
    }

    #[test]
    fn snapshot_json_assistant_loudness_satisfies_shared_key_contract() {
        // The mirror of fan-in's guard: outputd's assistant_loudness STATUS
        // object must carry every shared key (rendered through the one shared
        // writer), so the two daemons' /state shapes cannot drift.
        use jasper_tts_protocol::loudness::ASSISTANT_LOUDNESS_STATUS_KEYS;
        let state = OutputdState::new(&test_config());
        state.set_tts(
            "/run/jasper-outputd/tts.sock".to_string(),
            TtsMetrics::new(96_000),
        );
        let j = state.snapshot_json();
        let _ = parse_snapshot_json(&j);
        assert!(
            j.contains(r#""assistant_loudness":{"#),
            "block missing: {j}"
        );
        for key in ASSISTANT_LOUDNESS_STATUS_KEYS {
            assert!(
                j.contains(&format!("\"{key}\":")),
                "outputd assistant_loudness missing key {key}: {j}"
            );
        }
    }

    #[test]
    fn snapshot_json_stays_valid_json_when_the_loudness_snapshot_is_non_finite() {
        // End-to-end half of the shared writer's finite-or-null guarantee
        // (jasper_tts_protocol::loudness::push_json_finite_or_null). outputd
        // is the daemon that can carry a non-finite value this far: it copies
        // engine floats straight into the snapshot, where fan-in's packed
        // atomics cannot represent one. Proven here against the COMPLETE
        // STATUS document with a real parser, not a substring check — the
        // failure this guards is `inf` making a reader reject every other
        // fact in the document, and `NaN` slipping past a numeric check as a
        // value no comparison is ever true against.
        use jasper_tts_protocol::loudness::{HeldLoudnessReference, TtsLoudnessSnapshot};

        let state = OutputdState::new(&test_config());
        let metrics = TtsMetrics::new(96_000);
        let cell = metrics.loudness_cell();
        {
            let mut slot = cell.lock().expect("loudness cell");
            *slot = TtsLoudnessSnapshot {
                decision_seen: true,
                profile_confidence: f64::NAN,
                requested_gain_db: Some(f64::INFINITY),
                peak_cap_gain_db: Some(f64::NEG_INFINITY),
                final_gain_db: Some(f64::NAN),
                held_assistant: Some(HeldLoudnessReference {
                    speaker_lufs: f32::NAN,
                    canonical_db: f32::INFINITY,
                    calibration_offset_lu: f32::NEG_INFINITY,
                }),
                ..TtsLoudnessSnapshot::default()
            };
        } // drop before snapshot_json: the reader uses try_lock.
        state.set_tts("/run/jasper-outputd/tts.sock".to_string(), metrics);

        let j = state.snapshot_json();
        let parsed = parse_snapshot_json(&j);
        let loudness = &parsed["tts"]["assistant_loudness"];
        for key in [
            "profile_confidence",
            "requested_gain_db",
            "peak_cap_gain_db",
            "final_gain_db",
        ] {
            assert!(loudness[key].is_null(), "{key} should be null: {j}");
        }
        assert!(loudness["held_assistant"]["speaker_lufs"].is_null(), "{j}");
        // The poisoned floats did not cost the surrounding facts.
        assert_eq!(loudness["decision_seen"], serde_json::json!(true));
        assert_eq!(parsed["tts"]["budget_frames"], serde_json::json!(96_000));
    }

    #[test]
    fn snapshot_json_accumulates_clipping() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 2);
        state.mark_period(IoCounters::default(), 2, 5);

        let j = state.snapshot_json();
        assert!(j.contains(r#""last_period_clipped_samples":5"#));
        assert!(j.contains(r#""clipped_samples":7"#));
    }

    #[test]
    fn snapshot_json_reports_dac_delay_and_chip_ref_writer_counters() {
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            chip_ref_tee_path: Some("/tmp/outputd-chip-ref.s16le".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_period(IoCounters::default(), 12, 0);
        state.mark_dac_delay(240);
        state.mark_chip_ref_queue_admitted(320);
        state.mark_chip_ref_enqueued(10);
        state.mark_chip_ref_dequeued(320);
        state.mark_chip_ref_write(ChipRefWrite {
            frames_written: 320,
            delay_frames: Some(640),
            reference_sequence: Some(10),
            underruns: 1,
            xruns: 1,
            recoveries: 1,
            write_failed: false,
        });
        state.mark_chip_ref_writer_active(true);
        state.mark_chip_ref_retry();
        state.mark_chip_ref_dropped_full();
        state.mark_chip_ref_dropped_disconnected();
        state.mark_chip_ref_dropped_unavailable();
        state.mark_chip_ref_tee_open_error();
        state.mark_chip_ref_tee_opened();
        state.mark_chip_ref_tee_write_error();

        let j = state.snapshot_json();
        for needle in [
            r#""snd_pcm_delay_frames":240"#,
            r#""snd_pcm_delay_ms":5.000"#,
            r#""chip_ref_writer":{"desired":true,"enabled":true,"active":true,"status":"active""#,
            r#""retry_count":1"#,
            r#""queue_depth_periods":0"#,
            r#""queued_frames":0"#,
            r#""frames_written":320"#,
            r#""snd_pcm_delay_frames":640"#,
            r#""snd_pcm_delay_ms":40.000"#,
            r#""write_underrun_count":1"#,
            r#""write_xrun_count":1"#,
            r#""write_recovery_count":1"#,
            r#""write_error_count":0"#,
            r#""dropped_periods_due_to_full_queue":1"#,
            r#""dropped_periods_due_to_disconnected_writer":1"#,
            r#""dropped_periods_while_unavailable":1"#,
            r#""last_enqueued_reference_sequence":10"#,
            r#""last_written_reference_sequence":10"#,
            r#""reference_sequence_lag":2"#,
            r#""diagnostic_tee_path":"/tmp/outputd-chip-ref.s16le""#,
            r#""diagnostic_tee_active":false"#,
            r#""diagnostic_tee_open_error_count":1"#,
            r#""diagnostic_tee_write_error_count":1"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        assert!(!j.contains(r#""last_write_age_ms":null"#), "{j}");
    }

    #[test]
    fn snapshot_json_reports_the_chip_ref_writers_recent_observations() {
        // jasper-aec-init resolves SYS_DELAY from a RUN of per-write
        // snd_pcm_delay readings, and this state server answers about twice a
        // second — far slower than the writer writes. The ring is how one read
        // carries a run. Raw observations only: every acceptance rule over them
        // lives in jasper/cli/aec_init.py.
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        state.mark_chip_ref_writer_active(true);
        // The priming write at PCM open carries no reference sequence, which is
        // why `frames_written` and not the sequence is the entry's identity.
        state.mark_chip_ref_write(ChipRefWrite {
            frames_written: 128,
            delay_frames: Some(400),
            ..ChipRefWrite::default()
        });
        for sequence in 1..=3u64 {
            state.mark_chip_ref_write(ChipRefWrite {
                frames_written: 128,
                delay_frames: Some(400 + sequence),
                reference_sequence: Some(sequence),
                ..ChipRefWrite::default()
            });
        }
        // A write with no delay reading is no observation, so it must not
        // become a ring entry.
        state.mark_chip_ref_write(ChipRefWrite {
            frames_written: 128,
            delay_frames: None,
            reference_sequence: Some(4),
            ..ChipRefWrite::default()
        });

        let j = state.snapshot_json();
        assert!(
            j.contains(&format!(
                r#""recent_writes_capacity":{CHIP_REF_RECENT_WRITES}"#
            )),
            "{j}"
        );
        // Oldest first, cumulative frames_written strictly increasing.
        assert!(
            j.contains(
                r#""recent_writes":[{"frames_written":128,"snd_pcm_delay_frames":400,"reference_sequence":null,"age_ms":"#
            ),
            "{j}"
        );
        assert!(
            j.contains(r#""frames_written":256,"snd_pcm_delay_frames":401,"reference_sequence":1"#),
            "{j}"
        );
        assert!(
            j.contains(r#""frames_written":512,"snd_pcm_delay_frames":403,"reference_sequence":3"#),
            "{j}"
        );
        // Four writes produced a delay reading, so the ring holds four entries
        // — the fifth write had none and is therefore no observation.
        let entries = j.matches(r#","age_ms":"#).count();
        assert_eq!(entries, 4, "{j}");

        // `age_ms` is relative to THIS snapshot, and the reader places every
        // observation on its own clock from it. The load-bearing half of that
        // is WHICH instant it is relative to, and there is an independent
        // witness right beside it: the writer-level
        // `snd_pcm_delay_sample_age_ms` is the age of the last write that
        // produced a delay reading, which is the ring's newest entry (the
        // fifth write above produced none). Both are `uptime_ms` minus the
        // same stored instant, so they must be EQUAL — an age computed from
        // any other base breaks this and nothing else here would notice.
        //
        // The oldest-first ordering is also asserted, but honestly: these four
        // writes land inside one millisecond, so it is a shape check on the
        // emission order rather than a timing one.
        let ages = scan_u64_fields(&j, r#","age_ms":"#);
        assert_eq!(ages.len(), 4, "{j}");
        for pair in ages.windows(2) {
            assert!(pair[0] >= pair[1], "ages must run oldest-first: {ages:?}");
        }
        // Scoped to the writer block: the DAC reports a field of the same name
        // earlier in the snapshot, and it is `null` here because this test
        // never marks a DAC delay.
        let writer_block = &j[j.find(r#""chip_ref_writer":"#).unwrap()..];
        let writer_age = scan_u64_fields(writer_block, r#""snd_pcm_delay_sample_age_ms":"#);
        assert_eq!(writer_age.len(), 1, "{j}");
        assert_eq!(
            writer_age[0],
            *ages.last().unwrap(),
            "the newest ring entry and the writer-level delay sample are the \
             same write, so their ages share one base: {j}"
        );
    }

    /// Every `<needle><digits>` value in `text`, in order.
    ///
    /// Scans to the first non-digit rather than to a closing brace: a brace
    /// CHAR literal is not a string literal, so the panic-freedom scanner's
    /// brace counting would read one as ending this `#[cfg(test)]` module.
    fn scan_u64_fields(text: &str, needle: &str) -> Vec<u64> {
        text.match_indices(needle)
            .map(|(at, found)| {
                let rest = &text[at + found.len()..];
                let end = rest
                    .find(|c: char| !c.is_ascii_digit())
                    .unwrap_or(rest.len());
                rest[..end].parse::<u64>().unwrap()
            })
            .collect()
    }

    #[test]
    fn the_chip_ref_write_ring_keeps_the_newest_entries_and_never_grows() {
        // Fixed allocation is the contract: outputd runs under mlockall, so the
        // ring evicts rather than growing. Eviction is also what keeps one
        // outlier from sitting in the reader's window forever.
        let mut ring = ChipRefWriteRing::new();
        for index in 0..(CHIP_REF_RECENT_WRITES as u64 * 2 + 7) {
            ring.push(ChipRefObservation {
                uptime_ms: index,
                delay_frames: index,
                frames_written: index,
                reference_sequence: index,
            });
        }
        let mut out = Vec::with_capacity(CHIP_REF_RECENT_WRITES);
        ring.copy_into(&mut out);

        assert_eq!(out.len(), CHIP_REF_RECENT_WRITES);
        let newest = CHIP_REF_RECENT_WRITES as u64 * 2 + 6;
        assert_eq!(out[CHIP_REF_RECENT_WRITES - 1].frames_written, newest);
        assert_eq!(
            out[0].frames_written,
            newest - CHIP_REF_RECENT_WRITES as u64 + 1
        );
        for pair in out.windows(2) {
            assert!(pair[0].frames_written < pair[1].frames_written);
        }
    }

    #[test]
    fn chip_ref_writer_status_tracks_recoverable_and_terminal_failures() {
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        assert!(state.snapshot_json().contains(r#""status":"connecting""#));

        state.mark_chip_ref_write(ChipRefWrite {
            write_failed: true,
            ..ChipRefWrite::default()
        });
        assert!(state.snapshot_json().contains(r#""status":"degraded""#));

        state.mark_chip_ref_terminal_failure();
        assert!(state.snapshot_json().contains(r#""status":"failed""#));

        state.mark_chip_ref_writer_active(true);
        assert!(state.snapshot_json().contains(r#""status":"active""#));
    }

    #[test]
    fn snapshot_json_reports_never_for_missing_xruns() {
        let state = OutputdState::new(&test_config());
        state.mark_period(IoCounters::default(), 1, 0);

        let j = state.snapshot_json();
        assert_eq!(j.matches(r#""last_xrun_age_ms":null"#).count(), 2);
        assert_eq!(j.matches(r#""xrun_rate_per_hour":0.000"#).count(), 2);
    }

    #[test]
    fn snapshot_json_aec_clock_observing_by_default() {
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        for needle in [
            r#""aec_clock":{"chip_ref_sro_ppm":null"#,
            r#""sro_estimator_status":"observing""#,
            r#""verdict":"fallback""#,
            // Observe mode is off in test_config (default).
            r#""observe":false"#,
            // Inc 2: observe-only DAC playout-clock drift — fresh, not yet
            // locked, ppm 0, acquiring verdict.
            r#""dac_clock":{"dac_clock_ppm":0.000"#,
            r#""locked":false"#,
            // Inc 4: the shared rate_diff shape, idle placeholder values.
            r#""rate_diff":{"ppm":0.000"#,
            r#""bandwidth":0.1280"#,
            r#""updates":0"#,
            r#""resync_count":0"#,
            r#""latency":{"dac_presentation_ms":null"#,
            // test_config dac_buffer_frames=3072 / 48000 → 64 ms.
            r#""playback_queue_ms":64.000"#,
            r#""chip_ref_queue_ms":0.000"#,
        ] {
            assert!(j.contains(needle), "missing {needle} in {j}");
        }
        // The dac_clock block's verdict is "acquiring" before lock; it
        // sits between observe and latency.
        assert!(
            j.contains(r#""dac_clock":{"#) && j.contains(r#""verdict":"acquiring""#),
            "dac_clock block must be present with an acquiring verdict: {j}"
        );
    }

    #[test]
    fn every_dll_site_publishes_the_same_rate_diff_shape() {
        // Inc 4: every DLL instance publishes its loop state through the single
        // shared `rate_diff` writer, so /state and the doctor read every
        // clock-domain boundary identically. One site exists — the DAC-clock
        // observer. The count assertion is what keeps a future DLL site
        // from publishing a hand-rolled block instead of reusing
        // `push_dll_rate_diff`.
        let state = OutputdState::new(&test_config());
        let j = state.snapshot_json();
        assert_eq!(
            j.matches(r#""rate_diff":{"ppm":"#).count(),
            1,
            "exactly one rate_diff block (one per DLL site) in {j}"
        );
        // The full field set, in order — the shape every site must publish.
        // Keys, not values: the values are whatever the DLL happens to hold at
        // construction (`DllSnapshot::idle()` seeds `bandwidth` to `BW_MAX`, not
        // zero), and pinning them here would assert the loop's tuning rather
        // than the wire shape this test is about.
        let start = j
            .find(r#""rate_diff":{"#)
            .expect("a rate_diff block must be present");
        let block = &j[start..];
        // Bounding matters: `"locked"` is also emitted by the enclosing
        // `dac_clock` block, so an unbounded scan could match outside rate_diff.
        let end = block.find('}').expect("rate_diff block must close");
        let block = &block[..end];
        let mut cursor = 0usize;
        for key in [
            "ppm",
            "error_mean",
            "error_var",
            "bandwidth",
            "locked",
            "updates",
            "lock_count",
            "unlock_count",
            "resync_count",
        ] {
            let needle = format!(r#""{key}":"#);
            let at = block[cursor..].find(&needle).unwrap_or_else(|| {
                panic!("rate_diff is missing {key} at or after byte {cursor}: {block}")
            });
            cursor += at + needle.len();
        }
    }

    #[test]
    fn mark_dac_delay_ticks_dac_clock_without_panicking() {
        // Wiring test: the mark_dac_delay path (the playback loop's tick) feeds
        // the observe-only DAC-clock observer DLL. The tick reads REAL
        // uptime for its wall-clock, so a unit test can't drive it to a
        // deterministic lock in a tight loop — the convergence math is pinned by
        // the dac_clock module's own tests. Here we assert the tick
        // path is exercised and never panics, and the block stays present.
        let state = OutputdState::new(&test_config());
        for step in 1..=64u64 {
            state.mark_period(
                IoCounters {
                    dac_frames_written: step * 48_000,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
        }
        let j = state.snapshot_json();
        assert!(j.contains(r#""dac_clock":{"#), "block present: {j}");
    }

    #[test]
    fn snapshot_json_aec_clock_observe_reflects_config() {
        // The `observe` label is a pure passthrough of config.chip_ref_observe
        // (set by the reconciler when it armed the chip-ref writer for drift
        // MEASUREMENT on the DAC playout clock (vs nominal)). It changes no behavior; this
        // pins both polarities so the wire contract can't silently drop it.
        let off = OutputdState::new(&test_config());
        assert!(
            off.snapshot_json().contains(r#""observe":false"#),
            "observe must be false when config.chip_ref_observe is off"
        );
        let cfg_on = Config {
            chip_ref_observe: true,
            ..test_config()
        };
        let on = OutputdState::new(&cfg_on);
        assert!(
            on.snapshot_json().contains(r#""observe":true"#),
            "observe must be true when config.chip_ref_observe is on"
        );
    }

    #[test]
    fn snapshot_json_aec_clock_locks_and_classifies_drift() {
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        // Drive enough paired DAC + chip-ref snapshots for the estimator to
        // lock. The DAC runs ~50 ppm fast relative to the 16 kHz chip ref.
        // Round the CUMULATIVE DAC target each step so the integer counter
        // tracks the true ppm (per-step rounding would bias the slope).
        for step in 1..=40u64 {
            let dac_written = (48_000.0 * step as f64 * (1.0 + 50.0 / 1.0e6)).round() as u64;
            // The DAC counters land via the playback-loop marks...
            state.mark_period(
                IoCounters {
                    dac_frames_written: dac_written,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
            // ...and the chip-ref write ticks the estimator with both pairs.
            state.mark_chip_ref_write(ChipRefWrite {
                frames_written: 16_000,
                delay_frames: Some(320),
                ..ChipRefWrite::default()
            });
        }
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""sro_estimator_status":"locked""#),
            "estimator should lock in {j}"
        );
        assert!(
            j.contains(r#""verdict":"compensable""#),
            "50 ppm should classify compensable in {j}"
        );
        assert!(
            j.contains(r#""chip_ref_sro_ppm":50."#) || j.contains(r#""chip_ref_sro_ppm":49."#),
            "expected ~50 ppm estimate in {j}"
        );
    }

    #[test]
    fn aec_clock_decimates_sub_interval_chip_ref_writes() {
        // In production `mark_chip_ref_write` fires ~50 Hz, one ~320-frame
        // chip-ref period per call. The estimator must be fed only ~1 Hz, so
        // many sub-interval writes must NOT accumulate enough samples to lock:
        // without decimation 100 feeds would lock; with it ~2 feeds stay
        // observing. This pins the decimation gate.
        let cfg = Config {
            chip_ref_pcm: Some("plughw:CARD=Array,DEV=0".to_string()),
            ..test_config()
        };
        let state = OutputdState::new(&cfg);
        let mut dac: u64 = 0;
        for _ in 0..100u64 {
            dac += 960; // 48k/16k ratio, clock-coherent
            state.mark_period(
                IoCounters {
                    dac_frames_written: dac,
                    ..IoCounters::default()
                },
                1,
                0,
            );
            state.mark_dac_delay(1024);
            state.mark_chip_ref_write(ChipRefWrite {
                frames_written: 320,
                delay_frames: Some(320),
                ..ChipRefWrite::default()
            });
        }
        let j = state.snapshot_json();
        assert!(
            j.contains(r#""sro_estimator_status":"observing""#),
            "100 sub-interval (~320-frame) writes decimate to ~2 feeds and must \
             stay observing, not lock: {j}"
        );
    }
}
