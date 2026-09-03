// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! USB-direct capture's hot-path state machine, drain pipeline, and recovery.
//!
//! The surrounding mixer owns lane construction and shared ALSA primitives;
//! this module owns the direct lane's period-by-period behavior.

use super::*;

/// Shared USB DIRECT counters for the STATUS `direct{}` block. The mixer work
/// thread writes them from the direct-capture state machine; the
/// state-server thread reads them lock-free. Cloned into
/// [`crate::state::InputSnapshotSource`] at construction.
#[derive(Clone)]
pub struct DirectObservability {
    /// The capture device the direct lane opens (`hw:UAC2Gadget` or override).
    pub device: String,
    /// The gadget open period this lane negotiated (frames). Default 256 unless
    /// `JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES` overrides it — surfaced in STATUS
    /// so an operator can confirm the running geometry.
    pub period_frames: u32,
    /// The gadget capture buffer this lane ACTUALLY negotiated (frames) — the
    /// live `hwp.get_buffer_size()` from the open, not the requested
    /// `resolve_direct_buffer_frames(period)`. The kernel may round the
    /// `set_buffer_size_near` request up (still period-aligned + ≥ floor, so the
    /// open is accepted with a `buffer_near` warn), and this field reports what
    /// the PCM is really running so the STATUS geometry can't overclaim. Atomic
    /// (not a plain `u32`) so a reopen after unplug can re-store the freshly
    /// negotiated size, mirroring the `opens`/`retries`/`present` idiom.
    pub buffer_frames: Arc<AtomicU64>,
    /// Whether the gadget capture is currently open (`Present`) — the live
    /// "is the USB host attached and captured" gauge.
    pub present: Arc<AtomicBool>,
    /// Edge-detected host-frame flow used by mux's fast USB wake path. A helper
    /// thread samples the already-published input counter; the audio thread does
    /// no notification I/O and never reads this field.
    pub streaming: Arc<AtomicBool>,
    /// Since-boot false→true / true→false streaming edges.
    pub stream_starts: Arc<AtomicU64>,
    pub stream_stops: Arc<AtomicU64>,
    /// Best-effort mux wake delivery counters. A failure is safe because mux's
    /// fixed patrol observes the published ``streaming`` state.
    pub notify_attempts: Arc<AtomicU64>,
    pub notify_failures: Arc<AtomicU64>,
    /// Cumulative successful opens of the gadget capture (climbs on first open
    /// and on every reopen after an unplug/loss).
    pub opens: Arc<AtomicU64>,
    /// Cumulative reopen attempts QUEUED while Absent (a growing value with
    /// `present=false` means the gadget is not attachable — bridge holding it,
    /// or no host). An attempt is counted when handed to the
    /// `fanin-direct-opener` thread (#2533), including the handover that
    /// retires a dead handle; the ~2 s latch governs the next attempt.
    pub retries: Arc<AtomicU64>,
    /// Whether a device open is currently QUEUED on the `fanin-direct-opener`
    /// thread (#2533). A value stuck `true` alongside a climbing `retries`
    /// means the open itself is hanging, which costs silence on this lane
    /// rather than an audio glitch.
    pub reopen_pending: Arc<AtomicBool>,
    /// Cumulative zombie-handle forced reopens: a run of
    /// `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS` consecutive zero-avail drains while
    /// Present tripped a close + bounded re-open of the gadget capture. A growing
    /// value means the gadget function is being rebuilt underneath fan-in (UDC
    /// rebind / usbsink stop-start) and this lane is self-healing the deaf handle
    /// instead of needing a manual fan-in restart. Surfaced via STATUS.
    pub reopens: Arc<AtomicU64>,
    /// Consecutive zero-avail drain count. Incremented each drain entry where
    /// `avail_update()` reported exactly 0 while Present; reset to 0 the moment any
    /// avail > 0 is seen or the handle transitions to Absent/reopened. When it
    /// reaches `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS` the zombie-reopen fires — but ONLY
    /// once `frames_flowed_since_open` latched (see below), so an attached-idle host
    /// that never streamed on this handle can accumulate the streak but never trip.
    pub zero_avail_streak: Arc<AtomicU64>,
    /// Flowing→dead edge latch guarding the zombie detector against an ordinary
    /// attached-but-silent host, which can hold `avail≈0` indefinitely with no
    /// gadget rebuild. Set `true` the first time a drain sees `avail > 0` on the
    /// current handle; reset to `false` on every open/reopen and on going Absent.
    /// The zombie-reopen only fires once this is `true` — the handle demonstrably
    /// fed this lane and then went deaf — bounding reopens to one per real
    /// rebuild. Lock-free atomic, same mixer-writes / state-reads idiom.
    pub frames_flowed_since_open: Arc<AtomicBool>,
    /// Render-period index of the LAST handle-liveness probe, so the probe fires
    /// on the `DIRECT_LIVENESS_PROBE_EVERY_PERIODS` (~1 s) cadence off the drain
    /// count rather than every period (a `snd_pcm_status` ioctl is a real
    /// syscall). Mixer-thread only.
    pub liveness_last_checked_drain: Arc<AtomicU64>,
    /// Cumulative liveness-probe forced reopens: the count of times the ~1 s
    /// `snd_pcm_status` probe found the open handle dead (ioctl `-ENODEV` or
    /// `State::Disconnected`) and this lane self-healed with a bounded reopen.
    /// This is the signal the frozen-mmap `avail_update` fast path structurally
    /// cannot raise — a rebuilt gadget leaves `avail_update` returning `Ok(0)`
    /// forever with no errno. Kept a DISTINCT counter from `reopens` (the
    /// flowing→dead zero-avail zombie latch) so an operator can tell which
    /// signal caught the rebuild. Which signal fires first is timing-dependent,
    /// so read these two as "which probe caught it," not as a clean
    /// live-vs-idle partition. Surfaced via STATUS alongside `reopens`.
    pub card_gen_reopens: Arc<AtomicU64>,
    /// Drain-entry avail dwell stats. SINCE-BOOT cumulative (matches the
    /// `opens`/`retries` idiom in this block — no reset-on-read state to carry,
    /// and a monotonic denominator makes the STATUS `mean` a lifetime average
    /// rather than a since-last-poll one). Written lock-free by the mixer work
    /// thread on each drain entry; read lock-free by the state-server thread
    /// for the STATUS `drain_avail{}` sub-block.
    pub drain_stats: DrainStats,
}

/// Since-boot drain-entry avail dwell accumulators. One sample per
/// `drain_direct_capture` call: the `avail_update()` reading at drain entry,
/// which is the standing gadget-capture dwell the ~186-frame symptom measures.
/// All fields are lock-free atomics so the mixer work thread can record without
/// a mutex and the state-server thread can read a consistent-enough snapshot for
/// STATUS (each field is independently monotonic; a torn read across fields at
/// most skews one poll's mean by one sample — acceptable for observability).
#[derive(Clone)]
pub struct DrainStats {
    /// Number of drain-entry samples recorded (the histogram/mean denominator).
    pub count: Arc<AtomicU64>,
    /// Running sum of drain-entry avail (frames). `mean = sum / count`.
    pub sum: Arc<AtomicU64>,
    /// Maximum drain-entry avail observed (frames).
    pub max: Arc<AtomicU64>,
    /// Fixed 64-frame-step histogram of drain-entry avail (see
    /// [`drain_avail_bucket`]). Index i counts samples in that bucket.
    pub hist: [Arc<AtomicU64>; DRAIN_AVAIL_BUCKETS],
}

impl Default for DrainStats {
    fn default() -> Self {
        DrainStats::new()
    }
}

impl DrainStats {
    /// Fresh zeroed accumulators. `pub` so the state-server fixtures can build a
    /// direct-lane snapshot without reaching into the atomics field-by-field.
    pub fn new() -> Self {
        DrainStats {
            count: Arc::new(AtomicU64::new(0)),
            sum: Arc::new(AtomicU64::new(0)),
            max: Arc::new(AtomicU64::new(0)),
            hist: std::array::from_fn(|_| Arc::new(AtomicU64::new(0))),
        }
    }

    /// Record one drain-entry avail sample (frames). Lock-free, allocation-free,
    /// syscall-free — safe to call every render cycle on the hot path. Negative
    /// avail (never seen at a real `Ok` reading) is clamped to 0. Returns the
    /// post-increment count so the caller can rate-limit its INFO log off it
    /// without a second load.
    pub(super) fn record(&self, avail: i64) -> u64 {
        let a = avail.max(0) as u64;
        let count = self.count.fetch_add(1, Ordering::Relaxed) + 1;
        self.sum.fetch_add(a, Ordering::Relaxed);
        self.max.fetch_max(a, Ordering::Relaxed);
        self.hist[drain_avail_bucket(avail)].fetch_add(1, Ordering::Relaxed);
        count
    }
}

/// One direct USB capture lane's runtime state (DEFAULT-OFF; only the usbsink
/// lane when `JASPER_FANIN_USB_DIRECT=enabled`). Owns the `hw:UAC2Gadget`
/// S32_LE capture PCM directly — the usbsink bridge hop + aloop cable are gone
/// on this lane. On a narrow wire the lane's audio is narrowed to S16 and fed
/// the SAME `LaneResampler` the aloop path would use; on a wide wire it is fed
/// that resampler unnarrowed (#2223).
///
/// Presence is dynamic (a UAC2 gadget comes and goes with the host cable), so
/// this is a small state machine: `Present` while the capture is open and
/// reading, `Absent` while the device is unplugged/held-by-the-bridge with a
/// bounded reopen retry counted in periods. No wall clock in the hot loop —
/// the retry cadence is measured in render periods like the auto-trim latch.
pub(super) enum DirectCapture {
    /// The gadget capture is open; the lane reads it every period.
    Present(PCM),
    /// The gadget is absent (never opened, unplugged, or a runtime loss). Reopen
    /// is retried at most once per `DIRECT_REOPEN_RETRY_PERIODS`; `periods_until_retry`
    /// counts down each period the lane renders (silence).
    Absent { periods_until_retry: u64 },
}

/// One open (and, when a handle is being retired, one close) for the
/// `fanin-direct-opener` thread to perform OFF the render thread.
struct DirectOpenRequest {
    /// The dead handle to close. `Drop` on a `PCM` is `snd_pcm_close`, a real
    /// blocking device call, so the retiring lane hands it over rather than
    /// dropping it inside `step()`.
    retire: Option<PCM>,
    device: String,
    open_period: u32,
}

/// The result of one queued open: the live PCM and its negotiated buffer, or the
/// errno + rendered detail the lane logs. `alsa::Error` is not carried across the
/// channel — only the two fields the caller actually reports.
enum DirectOpenOutcome {
    Opened { pcm: PCM, negotiated_buffer: u32 },
    Failed { errno: i32, detail: String },
}

/// The DIRECT lane's deferred device-open channel (#2533): `snd_pcm_open` +
/// `hw_params` + `prepare` + `start` on the UAC2 gadget, and the
/// `snd_pcm_close` that retires a dead handle, run here instead of INLINE in
/// the mixer's render loop. One `Sender::send` and one `try_recv` per
/// affected period; nothing blocks, and the lane renders silence until a
/// handle comes back.
///
/// The render loop's budget is one period (5.33 ms at the shipped 256 frames)
/// and the downstream pipeline holds two 128-frame slots of cushion, so a
/// device open that takes longer than ~2.7 ms costs a whole slot: CamillaDSP
/// reads an empty Ring A (a 128-frame silence INSERTION) or fan-in
/// free-run-drops a slot it could not publish in time (a 128-frame
/// DELETION) — measured in the field with a USB host attached. The `Absent`
/// retry fires every `DIRECT_REOPEN_RETRY_PERIODS` (~2 s) for as long as the
/// gadget is unattachable, and the zombie / card-generation recoveries
/// close-and-reopen on the spot, so this is not a rare path.
pub(super) struct DirectOpener {
    req_tx: Sender<DirectOpenRequest>,
    res_rx: std::sync::mpsc::Receiver<DirectOpenOutcome>,
    /// A request is queued and its result has not been collected. At most one is
    /// ever outstanding, so a slow/hung open cannot build a backlog.
    in_flight: bool,
    /// The STATUS mirror of `in_flight` (`direct.reopen_pending`), shared with the
    /// state-server thread. A value stuck at `true` means the device open itself
    /// is hanging — which no longer costs audio, which is the point of deferring it.
    pending_gauge: Arc<AtomicBool>,
    /// Retired handles this opener could not hand over yet. `Drop` on a `PCM` is
    /// `snd_pcm_close` — a blocking device call — so a handle that cannot be
    /// queued is PARKED here rather than dropped on the render thread; the next
    /// successful request carries one away.
    ///
    /// In practice this is empty or holds one: a handle is only ever retired on a
    /// `Present` → `Absent` transition, and `Present` implies no open is in
    /// flight (an open is requested only from the `Absent` arm, and adopting its
    /// result is what makes the lane `Present` again), so a live opener always
    /// accepts a retiring handle. It is a `Vec` rather than an `Option` precisely
    /// so that parking can never displace — and therefore never silently close —
    /// a handle already parked, even if that reasoning is one day wrong. If the
    /// opener thread is gone entirely these live until the daemon exits: one or
    /// two descriptors, never a stall.
    parked: Vec<PCM>,
}

impl DirectOpener {
    /// Spawn the opener thread. Called at lane construction (`Mixer::new`, which
    /// `main` runs before `mlockall`, like every other fan-in helper thread).
    pub(super) fn spawn(pending_gauge: Arc<AtomicBool>) -> std::io::Result<Self> {
        let (req_tx, req_rx) = std::sync::mpsc::channel::<DirectOpenRequest>();
        let (res_tx, res_rx) = std::sync::mpsc::channel::<DirectOpenOutcome>();
        std::thread::Builder::new()
            .name("fanin-direct-opener".to_string())
            .stack_size(crate::HELPER_STACK_BYTES)
            .spawn(move || {
                while let Ok(req) = req_rx.recv() {
                    // Close the retired handle before opening a replacement:
                    // some gadget rebuilds refuse a second open while the old
                    // fd is still open.
                    drop(req.retire);
                    let outcome = match open_direct_capture(&req.device, req.open_period) {
                        Ok((pcm, negotiated_buffer)) => DirectOpenOutcome::Opened {
                            pcm,
                            negotiated_buffer,
                        },
                        Err(e) => DirectOpenOutcome::Failed {
                            errno: errno_of(&e),
                            detail: format!("{e:#}"),
                        },
                    };
                    if res_tx.send(outcome).is_err() {
                        break;
                    }
                }
            })?;
        Ok(Self {
            req_tx,
            res_rx,
            in_flight: false,
            pending_gauge,
            parked: Vec::new(),
        })
    }

    /// Mirror `in_flight` into the STATUS gauge. One relaxed store.
    fn publish_pending(&self) {
        self.pending_gauge.store(self.in_flight, Ordering::Relaxed);
    }

    /// Queue one retire+open. Returns whether it was queued: `false` when a
    /// request is already outstanding or the thread is gone.
    ///
    /// **Never drops a `PCM`.** A handle it cannot hand over is parked (see
    /// [`parked`](Self::parked)) and offered again on the next call — dropping it
    /// here would run `snd_pcm_close` on the render thread, which is the whole
    /// class of call this type exists to move away. Never blocks.
    fn request(&mut self, retire: Option<PCM>, device: &str, open_period: u32) -> bool {
        if let Some(pcm) = retire {
            self.parked.push(pcm);
        }
        if self.in_flight {
            return false;
        }
        // Carry one parked handle per request; any second one waits for the next.
        let carried = self.parked.pop();
        match self.req_tx.send(DirectOpenRequest {
            retire: carried,
            device: device.to_string(),
            open_period,
        }) {
            Ok(()) => {
                self.in_flight = true;
                self.publish_pending();
                true
            }
            Err(std::sync::mpsc::SendError(request)) => {
                // The thread is gone and `SendError` owns the request — take the
                // handle back out and re-park it rather than letting the returned
                // request drop it here.
                if let Some(pcm) = request.retire {
                    self.parked.push(pcm);
                }
                self.publish_pending();
                false
            }
        }
    }

    /// Collect a finished open, if one is ready. Never blocks.
    fn poll(&mut self) -> Option<DirectOpenOutcome> {
        if !self.in_flight {
            return None;
        }
        let collected = match self.res_rx.try_recv() {
            Ok(outcome) => {
                self.in_flight = false;
                Some(outcome)
            }
            Err(std::sync::mpsc::TryRecvError::Empty) => None,
            Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                // The opener thread is gone; stop expecting a result so the lane
                // can re-request (which will fail fast) rather than waiting forever.
                self.in_flight = false;
                None
            }
        };
        self.publish_pending();
        collected
    }
}

/// Read the USB DIRECT lane: drain everything the gadget capture
/// reports ready into the lane resampler (narrowing S32→S16 on a narrow wire and
/// tapping the converted slice on the way; carrying the gadget's i32 untouched
/// on a wide one), then render exactly one DAC-paced period into `read_buf` —
/// or into `read_buf_wide` when this lane is spine-scale. Returns the number of
/// real (non-silence) frames rendered —
/// `period_frames` when the resampler is locked, `0` while priming/absent.
///
/// Never returns `Err`: a device loss (ENODEV on unplug, or a rejected reopen)
/// transitions the lane to `Absent` and renders silence with a bounded reopen
/// retry, so the daemon keeps running. Xruns recover exactly like the
/// aloop resampler lane (`recover_resampler_input_xrun`, but device-open aware).
pub(super) fn read_direct_and_render(
    input: &mut Input,
    period_frames: usize,
    tap: &mut DirectTapHook,
    xrun_tx: &Sender<XrunEvent>,
) -> usize {
    // The lane's resampler fill BEFORE this period's push — the diagnostic
    // `ring_fill_frames` the tap records (not added to harness latency). Read via
    // the single-atomic gauge, NOT observability() (which clones Arcs — never on
    // the hot path).
    let ring_fill_before = input
        .resampler
        .as_ref()
        .map(|r| r.fill_frames_gauge())
        .unwrap_or(0);

    // Take ownership of the state machine so we can mutate `input` (resampler,
    // counters) inside the read without a double borrow. Restored at the end.
    let mut direct = input
        .direct
        .take()
        .expect("read_direct_and_render only called on a direct lane");

    match &direct {
        DirectCapture::Present(_) => {
            let outcome = drain_direct_capture(
                &direct,
                input,
                period_frames,
                tap,
                ring_fill_before,
                xrun_tx,
            );
            // Every non-`Ok` outcome retires this handle. Take it OUT of the state
            // machine so the opener thread performs the `snd_pcm_close` (#2533);
            // dropping it here would run a blocking device call in the render loop.
            let retire = match outcome {
                DirectDrainOutcome::Ok => None,
                _ => match std::mem::replace(
                    &mut direct,
                    DirectCapture::Absent {
                        periods_until_retry: 0,
                    },
                ) {
                    DirectCapture::Present(pcm) => Some(pcm),
                    absent => {
                        direct = absent;
                        None
                    }
                },
            };
            match outcome {
                DirectDrainOutcome::Ok => {}
                DirectDrainOutcome::DeviceLost => {
                    // Runtime loss (errno-driven): close the PCM, reset the
                    // resampler, go Absent — the reopen retry re-establishes it.
                    if let Some(r) = input.resampler.as_mut() {
                        r.reset();
                    }
                    if let Some(obs) = &input.direct_obs {
                        obs.present.store(false, Ordering::Relaxed);
                        obs.zero_avail_streak.store(0, Ordering::Relaxed);
                        obs.frames_flowed_since_open.store(false, Ordering::Relaxed);
                        warn!(
                            "event=fanin.usb_direct.absent device={} reason=runtime_loss (will retry ~every {} periods)",
                            obs.device, DIRECT_REOPEN_RETRY_PERIODS,
                        );
                    }
                    direct = DirectCapture::Absent {
                        periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
                    };
                    hand_retired_handle_to_opener(input, retire);
                }
                DirectDrainOutcome::ZombieReopen => {
                    // periods_until_retry=0: a zombie is a live rebuild, not a
                    // truly-absent host — no need to wait on top of the ~2 s
                    // already spent detecting it.
                    if let Some(r) = input.resampler.as_mut() {
                        r.reset();
                    }
                    if let Some(obs) = &input.direct_obs {
                        obs.present.store(false, Ordering::Relaxed);
                        obs.zero_avail_streak.store(0, Ordering::Relaxed);
                        obs.frames_flowed_since_open.store(false, Ordering::Relaxed);
                        let reopens = obs.reopens.fetch_add(1, Ordering::Relaxed) + 1;
                        warn!(
                            "event=fanin.usb_direct.reopen device={} reason=zombie_handle reopens={} (avail=0 for ~{} periods after frames flowed; gadget rebuilt underneath — closing + re-opening the capture)",
                            obs.device, reopens, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS,
                        );
                    }
                    direct = DirectCapture::Absent {
                        periods_until_retry: 0,
                    };
                    hand_retired_handle_to_opener(input, retire);
                }
                DirectDrainOutcome::CardGenerationReopen => {
                    // periods_until_retry=0: same immediate-reopen reasoning as
                    // the zombie arm above; counted separately (card_gen_reopens)
                    // so the two signals stay distinguishable in STATUS.
                    if let Some(r) = input.resampler.as_mut() {
                        r.reset();
                    }
                    if let Some(obs) = &input.direct_obs {
                        obs.present.store(false, Ordering::Relaxed);
                        obs.zero_avail_streak.store(0, Ordering::Relaxed);
                        obs.frames_flowed_since_open.store(false, Ordering::Relaxed);
                        let reopens = obs.card_gen_reopens.fetch_add(1, Ordering::Relaxed) + 1;
                        warn!(
                            "event=fanin.usb_direct.reopen device={} reason=card_generation card_gen_reopens={} (snd_pcm_status reported the open handle dead — ENODEV/Disconnected — while avail_update still returned Ok(0); gadget function rebuilt with no frames flowed — closing + re-opening the capture)",
                            obs.device, reopens,
                        );
                    }
                    direct = DirectCapture::Absent {
                        periods_until_retry: 0,
                    };
                    hand_retired_handle_to_opener(input, retire);
                }
            }
        }
        DirectCapture::Absent { .. } => {
            // Collect a finished open / queue the next attempt. Never blocks.
            direct = maybe_reopen_direct(direct, input);
        }
    }

    // Render one DAC-paced period from whatever the resampler holds (silence
    // while Absent / priming). Advance the tap's capture cursor only by frames
    // actually read this period (done inside drain_direct_capture).
    // Render into whichever buffer this lane's width owns. `read_buf_wide` is
    // non-empty on a wide wire; on a narrow box this is the same
    // `render_period` into the same `read_buf`.
    let real_frames = if input.read_buf_wide.is_empty() {
        match input.resampler.as_mut() {
            Some(r) => r.render_period(&mut input.read_buf),
            None => {
                input.read_buf.fill(0);
                0
            }
        }
    } else {
        // Keep the narrow buffer digitally silent on a wide lane so nothing
        // downstream can read a stale i16 period from it.
        input.read_buf.fill(0);
        match input.resampler.as_mut() {
            Some(r) => r.render_period_wide(&mut input.read_buf_wide),
            None => {
                input.read_buf_wide.fill(0);
                0
            }
        }
    };
    input.direct = Some(direct);
    real_frames
}

/// The outcome of one direct-capture drain. All non-`Ok` outcomes drive the SAME
/// close→Absent→bounded-reopen recovery; they differ only in the log line and
/// which counter increments, so an operator can tell the three distinct causes
/// apart:
///   - `DeviceLost` — an `avail_update`/read errno (ENODEV on a clean unplug, etc.)
///     the drain classified as a device loss.
///   - `ZombieReopen` — Present but `avail_update` returned exactly 0 for
///     `DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS` consecutive drains AFTER frames had
///     flowed (the flowing→dead latch: a gadget rebuilt underneath a LIVE stream).
///   - `CardGenerationReopen` — the ~1 s `snd_pcm_status` liveness probe found the
///     open handle dead (ioctl `-ENODEV` / `State::Disconnected`) under a handle
///     that `avail_update` still reported `Ok(0)` for: the gadget function was
///     rebuilt even though no frame ever flowed on this handle (the window the
///     flowing→dead latch structurally cannot catch). Named for the
///     card-generation change it detects (STATUS counter `card_gen_reopens`),
///     not the ioctl that detects it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DirectDrainOutcome {
    Ok,
    DeviceLost,
    ZombieReopen,
    CardGenerationReopen,
}

/// Drain all currently-available frames from the gadget capture into the lane
/// resampler — narrowing S32→S16 on a narrow wire, carrying the samples
/// untouched on a wide one — and tapping each read. Bounded by
/// `RESAMPLER_MAX_READ_PERIODS`. EAGAIN stops the drain; EPIPE/ESTRPIPE recovers
/// the PCM + resets the resampler; any other errno is a device loss.
fn drain_direct_capture(
    direct: &DirectCapture,
    input: &mut Input,
    period_frames: usize,
    tap: &mut DirectTapHook,
    ring_fill_before: u64,
    xrun_tx: &Sender<XrunEvent>,
) -> DirectDrainOutcome {
    let DirectCapture::Present(pcm) = direct else {
        return DirectDrainOutcome::Ok;
    };
    let channels = CHANNELS as usize;
    // Preallocated i32 scratch (256×2) — no allocation in the hot path. Same
    // length as `narrow_scratch` below: the i32 read fills `scratch[..samples]`
    // and the narrow fills `narrow_scratch[..got]` with `got == samples`.
    let mut scratch = [0i32; direct_narrow_scratch_samples()];
    // Dedicated i16 narrowing scratch, sized to match the i32 scratch. MUST NOT
    // reuse `input.read_buf` (sized `period_frames × CHANNELS`) — see
    // `direct_narrow_scratch_samples` for the OOB-on-small-period hazard. A
    // single chunk read is capped at DIRECT_PERIOD_FRAMES frames (`to_read`
    // below), so this fixed size always bounds `got`.
    let mut narrow_scratch = [0i16; direct_narrow_scratch_samples()];
    let mut read_budget_remaining =
        period_frames.saturating_mul(RESAMPLER_MAX_READ_PERIODS as usize);
    let armed = tap.state.armed();
    // This lane's width, read from the ONE place that decides it: whether the
    // lane allocated a spine-scale period buffer at construction. No second
    // flag, so "which buffer holds the period" and "was the capture narrowed"
    // cannot disagree.
    let wide = !input.read_buf_wide.is_empty();
    // Sample the drain-ENTRY avail exactly once per drain call. The first
    // `avail_update()` reading is the standing gadget-capture dwell — the
    // frames sitting readable when the mixer render cycle reaches this lane
    // (a ~186-frame / 3.9 ms latency). Later in-loop `avail_update`s reflect
    // drain progress, not the standing dwell, so they are NOT recorded
    // (recording every iteration would multi-count).
    let mut drain_entry_recorded = false;

    while read_budget_remaining > 0 {
        let avail = match pcm.avail_update() {
            Ok(a) => a,
            Err(e) => match classify_pcm_errno(e.errno()) {
                PcmIoFate::WouldBlock => break,
                PcmIoFate::Xrun => {
                    recover_direct_xrun(pcm, input, e, period_frames, xrun_tx, "avail_update");
                    break;
                }
                PcmIoFate::Fatal => return DirectDrainOutcome::DeviceLost,
            },
        };
        if !drain_entry_recorded {
            drain_entry_recorded = true;
            record_drain_entry(input, avail);
            // Track the zero-avail streak on the drain-ENTRY sample only (once
            // per drain call, like the dwell stats); see
            // `frames_flowed_since_open` and `zero_avail_streak` for the gate.
            if let Some(obs) = &input.direct_obs {
                if avail == 0 {
                    let streak = obs.zero_avail_streak.fetch_add(1, Ordering::Relaxed) + 1;
                    let flowed = obs.frames_flowed_since_open.load(Ordering::Relaxed);
                    if zombie_handle_suspected(flowed, streak, DIRECT_ZOMBIE_ZERO_AVAIL_PERIODS) {
                        return DirectDrainOutcome::ZombieReopen;
                    }
                } else {
                    obs.zero_avail_streak.store(0, Ordering::Relaxed);
                    obs.frames_flowed_since_open.store(true, Ordering::Relaxed);
                }
                // Liveness probe: on the ~1 s housekeeping cadence (not every
                // period — a `snd_pcm_status` ioctl is a real syscall) issue ONE
                // STATUS ioctl on the open handle. Orthogonal to the zero-avail
                // latch above: it catches a rebuild on a handle that never
                // carried a frame (where `frames_flowed_since_open` is false and
                // that latch can never fire) and cannot false-fire on an
                // attached-idle host, which keeps reporting a live PCM state
                // regardless of silence. An `avail_update` errno (ENODEV on a
                // hard unplug) is matched above and returns DeviceLost before
                // this runs, so a clean unplug never reaches the probe.
                let drains = obs.drain_stats.count.load(Ordering::Relaxed);
                let last = obs.liveness_last_checked_drain.load(Ordering::Relaxed);
                if drains.saturating_sub(last) >= DIRECT_LIVENESS_PROBE_EVERY_PERIODS {
                    obs.liveness_last_checked_drain
                        .store(drains, Ordering::Relaxed);
                    if liveness_probe_dead(probe_direct_liveness(pcm)) {
                        return DirectDrainOutcome::CardGenerationReopen;
                    }
                }
            }
        }
        let want = resampler_read_budget_frames(avail, period_frames).min(read_budget_remaining);
        if want == 0 {
            break;
        }
        // Read in ≤256-frame chunks (the scratch size) via io_i32().readi.
        let mut remaining = want;
        let mut stop = false;
        while remaining > 0 && !stop {
            let to_read = remaining.min(DIRECT_PERIOD_FRAMES as usize);
            let samples = to_read * channels;
            let read_result = {
                let io = match pcm.io_i32() {
                    Ok(io) => io,
                    Err(_) => return DirectDrainOutcome::DeviceLost,
                };
                io.readi(&mut scratch[..samples])
            };
            match read_result {
                Ok(0) => {
                    stop = true;
                }
                Ok(n) => {
                    let got = n * channels;
                    // The tap's marker detector is an S16 contract by design, so
                    // an ARMED tap gets a narrowed view of the chunk whatever the
                    // lane's width. On the narrow route that view is the SAME
                    // slice the resampler is fed (one conversion, not two); on
                    // the wide route it is a diagnostic branch OFF the audio
                    // path, computed only while armed so a disarmed wide lane
                    // pays for no conversion at all.
                    if armed || !wide {
                        let converted = &mut narrow_scratch[..got];
                        let _ = jasper_resampler::convert_s32_to_s16(&scratch[..got], converted);
                    }
                    if armed {
                        // read_ns is taken immediately after readi returned above.
                        let read_ns = monotonic_ns();
                        tap.tap_over_read(&narrow_scratch[..got], n, read_ns, ring_fill_before);
                    }
                    tap.capture_frames_cursor = tap.capture_frames_cursor.saturating_add(n as u64);
                    input.frames_read.fetch_add(n as u64, Ordering::Relaxed);
                    // `Some` exactly when the view above was computed.
                    let narrow_view = (armed || !wide).then(|| &narrow_scratch[..got] as &[i16]);
                    push_capture_chunk(
                        input.resampler.as_mut(),
                        wide,
                        &scratch[..got],
                        narrow_view,
                    );
                    remaining = remaining.saturating_sub(n);
                    read_budget_remaining = read_budget_remaining.saturating_sub(n);
                    if n < to_read {
                        stop = true;
                    }
                }
                Err(e) => match classify_pcm_errno(e.errno()) {
                    PcmIoFate::WouldBlock => stop = true,
                    PcmIoFate::Xrun => {
                        recover_direct_xrun(pcm, input, e, period_frames, xrun_tx, "readi");
                        stop = true;
                    }
                    PcmIoFate::Fatal => return DirectDrainOutcome::DeviceLost,
                },
            }
        }
        if stop {
            break;
        }
    }
    // Reset the tap detector across a disarm transition (mirrors the aloop tap's
    // arm-boundary reset) so a fresh arm starts clean.
    if !armed && tap.detector.is_some() {
        tap.detector = None;
    }
    DirectDrainOutcome::Ok
}

/// Hand ONE just-read gadget chunk to the lane resampler at this lane's width —
/// the width fork, and the one place the gadget's low word lives or dies.
///
/// * NARROW wire (the shipped default): push the S16 view that
///   `convert_s32_to_s16` already produced. The narrowing happens BEFORE the
///   resampler; the byte-identity golden tests pin exactly this order.
/// * WIDE wire: push the gadget's `i32` untouched. There is no `>> 16` anywhere
///   on this route (#2223), so a hi-res host's low bits reach the sum.
///
/// **The ORDER is the contract, not an implementation detail.** Pushing the raw
/// `i32` on the narrow route would not merely widen it — the lane would then
/// resample at spine scale and narrow at its render instead, i.e.
/// resample-then-narrow. That is a better-rounded signal and a DIFFERENT one,
/// and it would silently change what every shipped box emits.
///
/// Extracted from the drain loop so that contract is reachable from a
/// hardware-free test: the loop around it needs an open ALSA capture, this does
/// not.
///
/// `narrow` is `Some` exactly when a narrowed view of this chunk was actually
/// computed — always on the narrow route, and on the wide route only while the
/// tap is armed. It is an `Option` rather than a slice the wide arm quietly
/// ignores because those are two different facts: "a narrow view exists and I
/// am not using it" and "no narrow view was computed" would otherwise read the
/// same at this signature, and only one of them is true on a disarmed wide lane.
/// When `Some`, it MUST be the `convert_s32_to_s16` of `raw`, same length.
pub(super) fn push_capture_chunk(
    resampler: Option<&mut LaneResampler>,
    wide: bool,
    raw: &[i32],
    narrow: Option<&[i16]>,
) {
    debug_assert!(narrow.map_or(true, |n| n.len() == raw.len()));
    let Some(r) = resampler else {
        return;
    };
    if wide {
        r.push_input_wide(raw);
    } else {
        // The narrow route always computes the view before calling.
        let Some(narrow) = narrow else {
            debug_assert!(false, "the narrow route must supply its narrowed view");
            return;
        };
        r.push_input(narrow);
    }
}

/// Record one drain-ENTRY avail sample into the lane's since-boot drain stats
/// and, every [`DRAIN_STATS_LOG_EVERY`] drains, emit a rate-limited
/// summary INFO line. Lock-free, allocation-free, syscall-free apart from the
/// throttled log — safe on the hot path. A `None` `direct_obs` (never true on a
/// direct lane) is a silent no-op.
fn record_drain_entry(input: &Input, avail: i64) {
    let Some(obs) = &input.direct_obs else {
        return;
    };
    let stats = &obs.drain_stats;
    let count = stats.record(avail);
    // The counter itself is the rate limiter: log only on the exact multiple so
    // there is no separate "last logged" state and the cadence is O(1).
    if count % DRAIN_STATS_LOG_EVERY == 0 {
        let sum = stats.sum.load(Ordering::Relaxed);
        let max = stats.max.load(Ordering::Relaxed);
        let mean = (sum as f64) / (count as f64);
        info!(
            "event=fanin.direct.drain_stats device={} drains={} mean_avail={:.1} max_avail={} \
             hist=[{},{},{},{},{},{}] (frames; buckets [0,64,128,192,256,320,+))",
            obs.device,
            count,
            mean,
            max,
            stats.hist[0].load(Ordering::Relaxed),
            stats.hist[1].load(Ordering::Relaxed),
            stats.hist[2].load(Ordering::Relaxed),
            stats.hist[3].load(Ordering::Relaxed),
            stats.hist[4].load(Ordering::Relaxed),
            stats.hist[5].load(Ordering::Relaxed),
        );
    }
}

/// Recover a direct-capture xrun (EPIPE/ESTRPIPE): count it, forward the xrun
/// event, `try_recover` the PCM, restart it if not Running, and reset the
/// resampler (a discontinuity). Mirrors `recover_resampler_input_xrun` for the
/// direct lane. Best-effort — a failed recover just leaves the PCM for the next
/// period's `avail_update` to re-observe (which will classify a hard failure as
/// a device loss).
fn recover_direct_xrun(
    pcm: &PCM,
    input: &mut Input,
    error: alsa::Error,
    period_frames: usize,
    xrun_tx: &Sender<XrunEvent>,
    operation: &str,
) {
    let count = input.xrun_count.fetch_add(1, Ordering::Relaxed) + 1;
    warn!(
        "event=fanin.xrun source=input label={} count={} op={} (usb_direct lane)",
        input.label, count, operation,
    );
    let _ = xrun_tx.send(XrunEvent {
        source: XrunSource::Input,
        label: input.label.clone(),
        frames: period_frames as u32,
        count,
    });
    if pcm.try_recover(error, true).is_ok() && pcm.state() != State::Running {
        let _ = pcm.start();
    }
    if let Some(r) = input.resampler.as_mut() {
        r.reset();
    }
}

/// Hand a retired gadget handle to the opener thread, which closes it and
/// immediately attempts the replacement open (#2533). Both are blocking device
/// calls and neither may run in the render loop — a `PCM`'s `Drop` IS
/// `snd_pcm_close`, so the handle has to travel rather than fall out of scope.
///
/// With no opener at all (spawn failed at construction — logged once there) the
/// handle is dropped here: a close on the render thread, the only alternative
/// to leaking the descriptor on a lane that has no worker to hand it to. With
/// an opener present the handle is always taken — queued, or parked for the
/// next request — and never closed here.
fn hand_retired_handle_to_opener(input: &mut Input, retire: Option<PCM>) {
    let device = direct_device(input);
    let open_period = direct_open_period(input);
    let queued = match input.direct_opener.as_mut() {
        Some(opener) => opener.request(retire, &device, open_period),
        None => {
            drop(retire);
            false
        }
    };
    if queued {
        if let Some(obs) = &input.direct_obs {
            obs.retries.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// The device this lane opens, from the ONE place it is recorded (the lane's
/// observability, seeded at construction), so a reopen uses the same geometry as
/// the initial open rather than a hardcoded default.
fn direct_device(input: &Input) -> String {
    input
        .direct_obs
        .as_ref()
        .map(|o| o.device.clone())
        .unwrap_or_default()
}

/// The open period this lane negotiated at construction.
fn direct_open_period(input: &Input) -> u32 {
    input
        .direct_obs
        .as_ref()
        .map(|o| o.period_frames)
        .unwrap_or(DIRECT_PERIOD_FRAMES)
}

/// While `Absent`: adopt a finished open if the opener thread has one ready, else
/// count the period-based retry latch down and QUEUE the next attempt when it
/// reaches 0. No wall clock — the countdown is one decrement per render
/// period — and no blocking: the `snd_pcm_open` runs on
/// `fanin-direct-opener`, so an unattachable gadget costs this loop one
/// non-blocking `try_recv` per period instead of a full open attempt every ~2 s
/// inside the period budget. A successful reopen transitions to `Present` and
/// re-primes the resampler from fresh input; a failed one re-arms the latch (one
/// retry per ~2 s) and stays Absent. Exactly one `present`/`absent` transition
/// log line.
fn maybe_reopen_direct(direct: DirectCapture, input: &mut Input) -> DirectCapture {
    let DirectCapture::Absent {
        periods_until_retry,
    } = direct
    else {
        return direct;
    };
    // 1. Did a queued open finish? One non-blocking `try_recv`.
    if let Some(outcome) = input.direct_opener.as_mut().and_then(|o| o.poll()) {
        return adopt_open_outcome(outcome, input);
    }
    // 2. Nothing ready: count down, then queue one attempt.
    if periods_until_retry > 0 {
        return DirectCapture::Absent {
            periods_until_retry: periods_until_retry - 1,
        };
    }
    let device = direct_device(input);
    let open_period = direct_open_period(input);
    // A `None` opener (spawn failed) cannot self-heal without blocking the render
    // loop, and blocking it is the defect: stay Absent (silence) with the latch
    // re-armed. `request` is also a no-op while one is already in flight, so a
    // slow open can never be re-queued into a backlog.
    let queued = match input.direct_opener.as_mut() {
        Some(opener) => opener.request(None, &device, open_period),
        None => false,
    };
    if queued {
        if let Some(obs) = &input.direct_obs {
            obs.retries.fetch_add(1, Ordering::Relaxed);
        }
    }
    DirectCapture::Absent {
        periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
    }
}

/// Adopt (or discard) one finished open from the opener thread. Pure bookkeeping
/// plus the two transition log lines — no device calls of its own.
fn adopt_open_outcome(outcome: DirectOpenOutcome, input: &mut Input) -> DirectCapture {
    match outcome {
        DirectOpenOutcome::Opened {
            pcm,
            negotiated_buffer,
        } => {
            if let Some(r) = input.resampler.as_mut() {
                r.reset();
            }
            if let Some(obs) = &input.direct_obs {
                obs.present.store(true, Ordering::Relaxed);
                // Fresh handle: clear the flowing→dead latch. The reopened PCM
                // must observe avail > 0 at least once before a new zero-avail run
                // can be classified as a zombie again — so a reopen that lands back
                // on a still-zombie gadget (card node exists, avail stays 0) does
                // NOT immediately re-fire, bounding reopens to one per real rebuild.
                obs.frames_flowed_since_open.store(false, Ordering::Relaxed);
                // The liveness probe needs no per-handle re-capture — it queries
                // the LIVE reopened handle each tick — so there is no baseline
                // to (re)arm and no way for a racy open to permanently disarm
                // the signal. Re-base only the cadence anchor to
                // the current (cumulative-since-boot) drain count so the first probe
                // on the fresh handle waits a full interval PAST this reopen rather
                // than firing immediately (the drain count never resets, so storing 0
                // here would look like a full interval had already elapsed). If the
                // reopen landed back on a still-dead gadget, the next probe one
                // interval out re-detects it and re-fires — bounded to one reopen per
                // interval, never a permanent disarm.
                obs.liveness_last_checked_drain.store(
                    obs.drain_stats.count.load(Ordering::Relaxed),
                    Ordering::Relaxed,
                );
                // Re-store the freshly negotiated buffer: a device re-enumeration
                // could in principle land a different (still valid) geometry, so
                // STATUS tracks the live PCM, not the initial open's number.
                obs.buffer_frames
                    .store(negotiated_buffer as u64, Ordering::Relaxed);
                let opens = obs.opens.fetch_add(1, Ordering::Relaxed) + 1;
                info!(
                    "event=fanin.usb_direct.present device={} buffer_frames={} opens={} retries={} (reopened)",
                    obs.device,
                    negotiated_buffer,
                    opens,
                    obs.retries.load(Ordering::Relaxed),
                );
            }
            DirectCapture::Present(pcm)
        }
        DirectOpenOutcome::Failed { errno, detail } => {
            // Still absent — re-arm the retry latch. No per-retry log (only the
            // present/absent transitions log); the errno and detail are kept
            // at debug so a persistent open failure is diagnosable without
            // per-2 s journal spam on an unplugged host.
            if let Some(obs) = &input.direct_obs {
                log::debug!(
                    "event=fanin.usb_direct.reopen_failed device={} errno={} detail={} \
                     (still absent; retrying ~every {} periods)",
                    obs.device,
                    errno,
                    detail,
                    DIRECT_REOPEN_RETRY_PERIODS,
                );
            }
            DirectCapture::Absent {
                periods_until_retry: DIRECT_REOPEN_RETRY_PERIODS,
            }
        }
    }
}
